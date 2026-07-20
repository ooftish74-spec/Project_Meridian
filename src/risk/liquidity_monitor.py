"""
Liquidity Risk Monitor — 유동성 리스크 감시
=============================================

포지션별 유동성 리스크를 측정하고 거래 가능 여부를 판단.

Metrics:
  - ADV ratio: position_value / average_daily_volume
  - Days to liquidate: position_shares / (ADV * participation_rate)
  - Liquidity score: 0-1 (1=highly liquid)

Usage:
    from src.risk.liquidity_monitor import LiquidityMonitor
    lm = LiquidityMonitor()
    result = lm.measure(positions={...})
    judgment = lm.judge(result)
"""
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

class LiquidityMonitor:
    """포지션별 유동성 리스크 측정.

    measure()/judge() 패턴 준수.
    """
    DEFAULT_PARTICIPATION_RATE = 0.1
    ETF_DEFAULT_ADV = {'069500': 5000000, '122630': 3000000, '252670': 2000000, '114800': 1500000, '233740': 1000000, '133690': 2000000, '379800': 1500000, '091160': 500000, '395160': 800000}

    def measure(self, positions: Dict, market_data: Dict=None) -> Dict:
        """각 포지션의 유동성 측정.

        Args:
            positions: {ticker: {value, price, shares, avg_daily_volume}}
            market_data: 시장 데이터 (optional)

        Returns:
            포트폴리오 유동성 요약
        """
        results = {}
        total_liquidity_score = 0.0
        n_positions = 0
        for ticker, pos in positions.items():
            pos_value = pos.get('value', 0)
            price = pos.get('price', 0) or 1
            shares = pos.get('shares', pos_value / price if price > 0 else 0)
            adv = pos.get('avg_daily_volume', 0) or self._estimate_adv(ticker, market_data)
            adv_value = adv * price
            adv_ratio = pos_value / adv_value if adv_value > 0 else float('inf')
            days_to_liq = shares / (adv * self.DEFAULT_PARTICIPATION_RATE) if adv > 0 else float('inf')
            liq_score = math.exp(-0.5 * days_to_liq) if days_to_liq < 100 else 0.0
            results[ticker] = {'adv_ratio': round(adv_ratio, 4), 'days_to_liquidate': round(min(days_to_liq, 999), 2), 'liquidity_score': round(liq_score, 4), 'position_value': pos_value, 'adv': adv}
            total_liquidity_score += liq_score
            n_positions += 1
        portfolio_score = total_liquidity_score / max(n_positions, 1)
        summary = {'portfolio_liquidity_score': round(portfolio_score, 4), 'illiquid_positions': [t for t, r in results.items() if r['liquidity_score'] < 0.5], 'n_positions': n_positions, 'positions': results, 'timestamp': datetime.now().isoformat()}
        try:
            (_RESULTS / 'liquidity_monitor.json').write_text(json.dumps(summary, indent=2, default=str))
        except Exception as _e0:
            logger.critical(f'  [liquidity_monitor] 유동성 모니터 데이터: {_e0}', exc_info=True)
        if summary['illiquid_positions']:
            logger.warning(f'  ⚠️ 유동성 경고: {len(summary['illiquid_positions'])}개 포지션 유동성 부족 ({summary['illiquid_positions']})')
        return summary

    def judge(self, measurement: Dict) -> Dict:
        """유동성 판단: 거래 가능 여부 + 사이즈 제한.

        Args:
            measurement: measure() 결과

        Returns:
            {'tradeable': bool, 'actions': {...}}
        """
        actions = {}
        for ticker in measurement.get('illiquid_positions', []):
            pos_data = measurement.get('positions', {}).get(ticker, {})
            days = pos_data.get('days_to_liquidate', 0)
            if days > 5:
                actions[ticker] = {'action': 'reduce', 'max_daily_pct': 0.02, 'reason': f'days_to_liquidate={days:.1f}'}
            elif pos_data.get('liquidity_score', 1) < 0.3:
                actions[ticker] = {'action': 'flag', 'reason': 'low_liquidity'}
        portfolio_liq = measurement.get('portfolio_liquidity_score', 0)
        tradeable = portfolio_liq > 0.3
        return {'tradeable': tradeable, 'portfolio_liquidity_score': portfolio_liq, 'actions': actions, 'n_illiquid': len(measurement.get('illiquid_positions', [])), 'timestamp': datetime.now().isoformat()}

    def _estimate_adv(self, ticker: str, market_data: Dict=None) -> float:
        """ADV 추정 (feature_store 또는 기본값).

        Args:
            ticker: 종목코드
            market_data: 시장 데이터

        Returns:
            추정 ADV (주)
        """
        if market_data:
            vol_data = market_data.get('volume_data', {})
            if ticker in vol_data:
                return vol_data[ticker].get('adv_20', 100000)
        return self.ETF_DEFAULT_ADV.get(ticker, 100000)

    def check_liquidity(self, ticker: str, order_amount: float, market_data: Dict=None) -> Dict:
        """단일 주문의 유동성 적합성 검사.

        ExecutionEngine에서 호출: 주문 전 유동성 체크.

        Args:
            ticker: 종목코드
            order_amount: 주문 금액 (원)
            market_data: 시장 데이터 (optional)

        Returns:
            {'ok': bool, 'estimated_impact_pct': float,
             'adjusted_ratio': float}
        """
        adv = self._estimate_adv(ticker, market_data)
        est_price = order_amount / max(adv * 0.01, 1)
        adv_value = adv * max(est_price, 1000)
        participation = order_amount / max(adv_value, 1)
        impact_pct = 0.1 * math.sqrt(participation) * 100
        ok = participation <= self.DEFAULT_PARTICIPATION_RATE
        adjusted_ratio = min(1.0, self.DEFAULT_PARTICIPATION_RATE / max(participation, 0.001))
        return {'ok': ok, 'participation_rate': round(participation, 4), 'estimated_impact_pct': round(impact_pct, 4), 'adjusted_ratio': round(adjusted_ratio, 4), 'adv_estimated': adv}

    def calculate_market_impact_bps(self, ticker: str, order_value_krw: float, is_etf: bool=False, market_data: Dict=None) -> float:
        """
        주문 금액에 따른 비선형 예상 시장 충격 비용(BPS)을 계산합니다.
        (Almgren-Chriss 모델 축소판)
        
        공식: Impact(bps) = Base_Spread + Scale * (Order_Value / ADV_Value)^1.5
        
        Args:
            ticker: 종목 코드
            order_value_krw: 체결할 원화 금액
            is_etf: ETF 여부
            
        Returns:
            총 예상 슬리피지 (bps, 100 bps = 1%)
        """
        base_bps = 5.0
        if is_etf:
            base_bps = 2.0
        if order_value_krw <= 0:
            return base_bps
        adv_shares = self._estimate_adv(ticker, market_data)
        est_price = max(5000, min(order_value_krw, 100000))
        adv_value = adv_shares * est_price
        participation_rate = order_value_krw / max(adv_value, 1)
        market_impact = 250.0 * math.pow(participation_rate, 1.5)
        total_bps = base_bps + market_impact
        return min(500.0, total_bps)
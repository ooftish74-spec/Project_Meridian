"""
Advanced Slippage Model — 비선형 시장충격 모델 (Almgren-Chriss 완전체)
========================================================================

P2-3 업그레이드: σ(변동성) 동적 연결 완료.

핵심 공식 (Almgren-Chriss Temporary Impact):
  slippage_bps = base_bps + η × σ × (Q/V)^δ × 10000

  - base_bps: 기본 호가 스프레드 비용 (DynamicConfig)
  - η (eta):  시장충격 계수 (DynamicConfig: slippage.impact_coefficient)
  - σ (sigma): 일간 변동성 (동적 계산 또는 DynamicConfig 폴백)
  - Q:         주문 금액 (원)
  - V:         ADV — Average Daily Volume (원)
  - δ (delta): 멱지수 (default 0.5 = Square-root law)

유동성 조정:
  - 소형주 할증 (시가총액 < threshold)
  - 시간대별 유동성 할인/할증 (장초/장중/장마감)
  - 레짐별 스프레드 확대 (crash 시 2~3x)

변동성 소스 (우선순위):
  1. estimate() 호출 시 volatility 직접 전달
  2. KRX daily CSV에서 20일 실현 변동성 동적 계산 (캐시)
  3. DynamicConfig 폴백: slippage.default_daily_vol (default: 0.02)

모든 파라미터 DynamicConfig 동적 로드. 하드코딩 0.

Usage:
    from src.execution.slippage_model import AdvancedSlippageModel
    model = AdvancedSlippageModel()
    # 기본 (변동성 자동 계산)
    cost = model.estimate(order_size=5_000_000, adv=100_000_000_000)
    # 변동성 명시 (백테스트 등)
    cost = model.estimate(order_size=5_000_000, adv=100_000_000_000, volatility=0.025)
"""
import logging
import math
from datetime import datetime, time
from pathlib import Path
from typing import Dict, List, Optional
from src.utils.emergency_pager import send_emergency_page
try:
    from config.dynamic_config import DynamicConfig
except ImportError as e:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class AdvancedSlippageModel:
    """비선형 시장충격 + 유동성 조정 슬리피지 모델.

    Almgren-Chriss Temporary Impact (완전체):
      slippage_bps = base_bps + η × σ × (Q/V)^δ × 10000

    변동성 σ는 동적으로 계산하거나 estimate() 호출 시 직접 전달 가능.
    ADV가 없으면 DynamicConfig 폴백 적용.
    """

    def __init__(self):
        self._vol_cache: Dict[str, float] = {}

    def _compute_realized_vol(self, ticker: str) -> float:
        """KRX daily CSV에서 종목별 20일 실현 변동성 계산.

        Returns:
            일간 실현 변동성 (소수, 예: 0.022 = 2.2%)
            데이터 없으면 DynamicConfig 폴백 반환.
        """
        if ticker and ticker in self._vol_cache:
            return self._vol_cache[ticker]
        lookback = cfg.get('slippage.vol_lookback_days', 20)
        fallback_vol = cfg.get('slippage.default_daily_vol', 0.02)
        if not ticker:
            return fallback_vol
        try:
            import pandas as pd
            krx_dir = _PROJECT_ROOT / 'data' / 'raw' / 'krx_stock_daily'
            if not krx_dir.exists():
                return fallback_vol
            csv_files = sorted(krx_dir.glob('kospi_*.csv'), reverse=True)
            closes: List[float] = []
            for csv_file in csv_files[:lookback + 5]:
                try:
                    df = pd.read_csv(csv_file)
                    ticker_cols = ['ISU_CD', '종목코드', 'Code', 'ticker']
                    close_cols = ['TDD_CLSPRC', '종가', 'close']
                    for tc in ticker_cols:
                        if tc not in df.columns:
                            continue
                        df[tc] = df[tc].astype(str).str.zfill(6)
                        row = df[df[tc] == ticker]
                        if row.empty:
                            continue
                        for cc in close_cols:
                            if cc in row.columns:
                                val = float(row[cc].iloc[0])
                                if val > 0:
                                    closes.append(val)
                                    break
                        break
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
                if len(closes) >= lookback:
                    break
            if len(closes) >= 2:
                log_rets = [math.log(closes[i] / closes[i + 1]) for i in range(len(closes) - 1) if closes[i] > 0 and closes[i + 1] > 0]
                if log_rets:
                    mean_r = sum(log_rets) / len(log_rets)
                    var = sum(((r - mean_r) ** 2 for r in log_rets)) / len(log_rets)
                    vol = math.sqrt(var)
                    vol = max(0.005, min(0.15, vol))
                    self._vol_cache[ticker] = vol
                    logger.debug(f'  [SlippageModel] {ticker} 실현변동성: {vol:.4f} ({len(log_rets)}일 데이터)')
                    return vol
        except Exception as e:
            logger.critical(f'  [SlippageModel] 변동성 계산 실패 ({ticker}): {e}', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at slippage_model.py:146', exc_info=e)
        return fallback_vol

    def estimate(self, order_size: float, adv: float=0, market_cap: float=0, regime: str='caution', current_time: Optional[datetime]=None, volatility: float=0.0, ticker: str='') -> Dict:
        """슬리피지 추정 (Almgren-Chriss 완전체).

        Args:
            order_size:  주문 금액 (원)
            adv:         일평균 거래대금 (원)
            market_cap:  시가총액 (원, optional)
            regime:      현재 레짐
            current_time: 현재 시각
            volatility:  일간 변동성 σ (0이면 자동 계산)
            ticker:      종목 코드 (변동성 자동 계산 시 사용)

        Returns:
            {
              'slippage_bps': float,
              'total_cost': float,      # 원화
              'components': {
                'base_bps': float,
                'market_impact_bps': float,   # η × σ × (Q/V)^δ × 10000
                'sigma': float,               # 사용된 변동성 σ
                'eta': float,                 # 시장충격 계수
                'participation_rate': float,  # Q/V
                'liquidity_premium_bps': float,
                'time_adjustment': float,
                'regime_multiplier': float,
              }
            }
        """
        if order_size <= 0:
            return {'slippage_bps': 0, 'total_cost': 0, 'components': {}}
        if volatility > 0:
            sigma = volatility
            vol_source = 'provided'
        elif ticker:
            sigma = self._compute_realized_vol(ticker)
            vol_source = 'realized'
        else:
            sigma = cfg.get('slippage.default_daily_vol', 0.02)
            vol_source = 'default'
        base_bps = cfg.get('slippage.base_bps', 3.0)
        eta = cfg.get('slippage.impact_coefficient', 10.0)
        delta = cfg.get('slippage.impact_exponent', 0.5)
        if adv > 0:
            participation = order_size / adv
            market_impact_bps = eta * sigma * math.pow(participation, delta) * 10000
        else:
            market_impact_bps = eta * sigma * cfg.get('slippage.default_impact_bps', 5.0)
        liquidity_premium = 0.0
        mcap_threshold = cfg.get('slippage.small_cap_threshold', 500000000000)
        small_cap_premium = cfg.get('slippage.small_cap_premium_bps', 3.0)
        if market_cap > 0 and market_cap < mcap_threshold:
            ratio = mcap_threshold / max(market_cap, 1)
            liquidity_premium = small_cap_premium * min(3.0, math.log(ratio))
        time_adj = 1.0
        if current_time is None:
            current_time = datetime.now()
        t = current_time.time()

        def _parse_time(val, fallback):
            """cfg 값을 datetime.time 객체로 안전하게 변환."""
            from datetime import time as dtime
            if isinstance(val, str):
                try:
                    return datetime.strptime(val, '%H:%M').time()
                except ValueError:
                    return fallback
            elif isinstance(val, dtime):
                return val
            return fallback
        open_start = _parse_time(cfg.get('slippage.volatile_open_start', '09:00'), datetime.strptime('09:00', '%H:%M').time())
        open_end = _parse_time(cfg.get('slippage.volatile_open_end', '09:15'), datetime.strptime('09:15', '%H:%M').time())
        close_start = _parse_time(cfg.get('slippage.volatile_close_start', '15:15'), datetime.strptime('15:15', '%H:%M').time())
        close_end = _parse_time(cfg.get('slippage.volatile_close_end', '15:30'), datetime.strptime('15:30', '%H:%M').time())
        if open_start <= t <= open_end:
            time_adj = cfg.get('slippage.open_multiplier', 1.5)
        elif close_start <= t <= close_end:
            time_adj = cfg.get('slippage.close_multiplier', 1.3)
        else:
            time_adj = cfg.get('slippage.midday_multiplier', 1.0)
        regime_mult = cfg.get(f'slippage.regime_multiplier.{regime}', cfg.get('slippage.regime_multiplier.caution', 1.0))
        total_bps = (base_bps + market_impact_bps + liquidity_premium) * time_adj * regime_mult
        max_bps = cfg.get('slippage.max_total_bps', 50.0)
        total_bps = min(total_bps, max_bps)
        total_cost = order_size * total_bps / 10000
        return {'slippage_bps': round(total_bps, 2), 'total_cost': round(total_cost, 0), 'components': {'base_bps': round(base_bps, 2), 'market_impact_bps': round(market_impact_bps, 2), 'sigma': round(sigma, 6), 'sigma_source': vol_source, 'eta': round(eta, 4), 'delta': round(delta, 4), 'liquidity_premium_bps': round(liquidity_premium, 2), 'time_adjustment': round(time_adj, 2), 'regime_multiplier': round(regime_mult, 2), 'participation_rate': round(order_size / adv, 6) if adv > 0 else None}}

    def estimate_batch(self, orders: list, regime: str='caution') -> Dict:
        """다건 주문 슬리피지 일괄 추정."""
        results = []
        total_cost = 0
        for order in orders:
            r = self.estimate(order_size=order.get('amount', 0), adv=order.get('adv', 0), market_cap=order.get('market_cap', 0), regime=regime, volatility=order.get('volatility', 0.0), ticker=order.get('ticker', ''))
            r['ticker'] = order.get('ticker', '')
            results.append(r)
            total_cost += r['total_cost']
        return {'orders': results, 'total_cost': round(total_cost, 0), 'n_orders': len(results), 'avg_slippage_bps': round(sum((r['slippage_bps'] for r in results)) / max(len(results), 1), 2)}

    def clear_vol_cache(self):
        """변동성 캐시 초기화 (일일 파이프라인 재시작 시 호출)."""
        self._vol_cache.clear()
        logger.debug('  [SlippageModel] 변동성 캐시 초기화')
"""
LeverageJudge — 측정 기반 레버리지/인버스 판정기
==================================================

Top Quant 원칙 3: 측정과 판정의 분리.
  - measure(): Sharpe, MDD, 상관, 연속 승일 등 객관적 수치
  - judge(): 측정값 위에 레버리지 정책 적용

레버리지 기준:
  1X (기본): 항상 허용
  2X 허용: Sharpe(30d) ≥ 1.5, confidence ≥ 0.7, 연속 5승, VaR < 1.5%
  3X 허용: Sharpe(30d) ≥ 2.0, MDD(30d) > -3%, 최대 ρ < 0.3

인버스 허용:
  - 레짐 = bear or crash
  - VIX > 25 or VKOSPI > 20
  - 최대 배분: 포트폴리오 20%

Usage:
    from src.risk.leverage_judge import LeverageJudge
    judge = LeverageJudge()
    result = judge.assess(portfolio, stream_metrics, regime='bull')
"""
import logging
import math
from datetime import datetime
from typing import Any, Dict, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class LeverageJudge:
    """측정 기반 레버리지/인버스 판정기 (측정/판정 분리)."""

    def __init__(self):
        self.etf_state = {}

    def measure(self, portfolio: Dict, stream_metrics: Dict) -> Dict:
        """순수 측정: Sharpe, MDD, 상관, 연속 승일 등.

        Args:
            portfolio: 포트폴리오 상태
            stream_metrics: 스트림별 성과 지표
                - s1_sharpe, s2_sharpe, ...
                - correlation_matrix
                - consecutive_wins
                - mdd_30d_pct

        Returns:
            측정 결과 (수치만)
        """
        sharpe_30d = stream_metrics.get('composite_sharpe_30d', 0)
        mdd_30d = stream_metrics.get('mdd_30d_pct', 0)
        corr_matrix = stream_metrics.get('correlation_matrix', {})
        max_corr = 0
        for key, val in corr_matrix.items():
            if isinstance(val, (int, float)) and val < 1.0:
                max_corr = max(max_corr, abs(val))
        daily_returns = portfolio.get('daily_returns', [])
        consecutive_wins = 0
        for r in reversed(daily_returns):
            if r > 0:
                consecutive_wins += 1
            else:
                break
        var_pct = 0
        if len(daily_returns) >= 5:
            mean_r = sum(daily_returns) / len(daily_returns)
            var = sum(((r - mean_r) ** 2 for r in daily_returns)) / len(daily_returns)
            std = math.sqrt(var) if var > 0 else 0
            var_pct = std * 1.645 * 100
        regime_confidence = stream_metrics.get('regime_confidence', 0.5)
        vix = stream_metrics.get('vix', 20)
        vkospi = stream_metrics.get('vkospi', 18)
        return {'sharpe_30d': round(sharpe_30d, 3), 'mdd_30d_pct': round(mdd_30d, 2), 'max_inter_stream_corr': round(max_corr, 3), 'consecutive_wins': consecutive_wins, 'var_95_pct': round(var_pct, 2), 'regime_confidence': round(regime_confidence, 3), 'vix': vix, 'vkospi': vkospi, 'n_return_days': len(daily_returns), 'timestamp': datetime.now().isoformat()}

    def judge(self, measurement: Dict, regime: str='caution') -> Dict:
        """정책 판정: 레버리지 수준 결정.

        Args:
            measurement: measure()의 반환값
            regime: 현재 레짐

        Returns:
            판정 결과 (레버리지 수준, 인버스 허용 등)
        """
        if not cfg.get('leverage.enabled', True):
            return {'leverage_level': 1, 'inverse_allowed': False, 'reason': '레버리지 비활성화'}
        leverage = 2 if regime in ('bull', 'caution') else 1
        reasons = []
        min_sharpe_3x = cfg.get('leverage.3x_min_sharpe', 2.0)
        max_mdd_3x = cfg.get('leverage.3x_max_mdd_pct', -3.0)
        max_corr_3x = cfg.get('leverage.3x_max_correlation', 0.3)
        if measurement['sharpe_30d'] >= min_sharpe_3x and measurement['mdd_30d_pct'] > max_mdd_3x and (measurement['max_inter_stream_corr'] < max_corr_3x) and (regime == 'bull'):
            leverage = 3
            reasons.append(f'3X: Sharpe={measurement['sharpe_30d']:.2f}≥{min_sharpe_3x}, MDD={measurement['mdd_30d_pct']:.1f}%>{max_mdd_3x}%, ρ={measurement['max_inter_stream_corr']:.2f}<{max_corr_3x}')
        elif leverage < 2:
            min_sharpe_2x = cfg.get('leverage.2x_min_sharpe', 1.5)
            min_confidence = cfg.get('leverage.2x_min_confidence', 0.7)
            min_wins = cfg.get('leverage.2x_min_consecutive_wins', 5)
            max_var = cfg.get('leverage.2x_max_var_pct', 1.5)
            if measurement['sharpe_30d'] >= min_sharpe_2x and measurement['regime_confidence'] >= min_confidence and (measurement['consecutive_wins'] >= min_wins) and (measurement['var_95_pct'] < max_var) and (regime in ('bull', 'caution')):
                leverage = 2
                reasons.append(f'2X: Sharpe={measurement['sharpe_30d']:.2f}≥{min_sharpe_2x}, 승연속={measurement['consecutive_wins']}≥{min_wins}')
        if leverage == 1:
            reasons.append('1X: 레버리지 조건 미충족')
        inverse_allowed = False
        inverse_reason = ''
        vix_threshold = cfg.get('regime.vix_caution_threshold', 25)
        if regime in ('bear', 'crash'):
            if measurement['vix'] > vix_threshold:
                inverse_allowed = True
                inverse_reason = f'인버스 허용: VIX={measurement['vix']:.1f}>{vix_threshold}'
            elif regime == 'crash':
                inverse_allowed = True
                inverse_reason = f'인버스 허용: CRASH 레짐'
        return {'leverage_level': leverage, 'inverse_allowed': inverse_allowed, 'inverse_max_pct': cfg.get('leverage.inverse_max_pct', 0.2) if inverse_allowed else 0, 'etf_ticker': self._get_etf_ticker(leverage), 'inverse_ticker': cfg.get('leverage.inverse_ticker', '114800') if inverse_allowed else None, 'reasons': reasons, 'inverse_reason': inverse_reason, 'regime': regime}

    def _get_etf_ticker(self, leverage: int) -> str:
        """레버리지 수준에 맞는 ETF 티커."""
        if leverage >= 3:
            return cfg.get('leverage.etf_3x', '233740')
        elif leverage >= 2:
            return cfg.get('leverage.etf_2x', '122630')
        return cfg.get('leverage.etf_1x', '069500')

    def assess(self, portfolio: Dict, stream_metrics: Dict, regime: str='caution') -> Dict:
        """통합: 측정 + 판정 (2-layer 반환).

        Returns:
            {
                'measurement': { ... },
                'judgment': { ... },
            }
        """
        measurement = self.measure(portfolio, stream_metrics)
        judgment = self.judge(measurement, regime)
        if judgment['leverage_level'] > 1 or judgment['inverse_allowed']:
            try:
                from src.measurement.event_ledger import log_event
                log_event('LEVERAGE', {'level': judgment['leverage_level'], 'inverse': judgment['inverse_allowed'], 'sharpe': measurement['sharpe_30d'], 'regime': regime, 'reasons': judgment['reasons']}, source='leverage_judge')
            except Exception as _e0:
                logger.critical(f'  [leverage_judge] 레버리지 판단 데이터 로드: {_e0}', exc_info=True)
        return {'measurement': measurement, 'judgment': judgment}

    def check_etf_stops(self, positions: Dict, market_data: Dict) -> List[Dict]:
        """
        ETF(레버리지/인버스) 포지션에 대한 Tracking 상태(HWM 등)를 업데이트하고,
        청산 조건에 도달한 포지션의 청산 주문(reason, urgency 포함)을 반환합니다.
        브릿지워터(Macro/Risk-Parity)와 메달리온(Signal Decay) 철학을 반영한 동적 임계값을 사용합니다.
        """
        exit_orders = []
        etf_tickers_str = cfg.get('leverage_judge.etf_tickers', '114800,252670,122630,233740,470450,470480')
        etf_tickers = [t.strip() for t in etf_tickers_str.split(',')]
        base_max_hold = int(cfg.get('leverage_judge.max_hold_days', 5))
        macro_vix_threshold = float(cfg.get('leverage_judge.macro_vix_threshold', 22.0))
        atr_sl_mult = float(cfg.get('leverage_judge.atr_sl_mult', 2.0))
        atr_trail_mult = float(cfg.get('leverage_judge.atr_trail_mult', 1.5))
        current_date = market_data.get('date', datetime.now().strftime('%Y-%m-%d'))
        vix = float(market_data.get('signal_cache', {}).get('vix', 20.0))
        dynamic_max_hold = max(1, int(base_max_hold * (10.0 / max(vix, 10.0))))
        for pos_key, pos in positions.items():
            ticker = pos.get('ticker', pos_key.split(':')[-1])
            stream_id = pos_key.split(':')[0] if ':' in pos_key else pos.get('stream_id', '')
            is_leverage_or_inverse = ticker in etf_tickers or stream_id == 'SYS_HEDGE'
            if not is_leverage_or_inverse:
                continue
            qty = pos.get('quantity', pos.get('qty', 0))
            if qty <= 0:
                continue
            entry_price = float(pos.get('entry_price', 0))
            current_price = float(pos.get('current_price', 0))
            if entry_price <= 0 or current_price <= 0:
                continue
            if pos_key not in self.etf_state:
                self.etf_state[pos_key] = {'hwm': current_price, 'entry_price': entry_price, 'entry_date': pos.get('entry_date', current_date)}
            state = self.etf_state[pos_key]
            if current_price > state['hwm']:
                state['hwm'] = current_price
            pnl_pct = (current_price / entry_price - 1) * 100
            drawdown_from_hwm = (state['hwm'] - current_price) / state['hwm'] * 100 if state['hwm'] > 0 else 0
            try:
                import pandas as pd
                entry_dt = pd.to_datetime(state['entry_date']).date()
                curr_dt = pd.to_datetime(current_date).date()
                days_held = (curr_dt - entry_dt).days
            except Exception:
                days_held = pos.get('days_held', 0)
            reason = None
            urgency = 0
            try:
                from src.execution.risk_params import _estimate_atr_pct
                atr_pct = _estimate_atr_pct(ticker, market_data)
            except Exception:
                atr_pct = 0.02
            dynamic_hard_sl = -abs(atr_pct * 100 * atr_sl_mult)
            dynamic_hard_sl = min(dynamic_hard_sl, -2.0)
            dynamic_trail_stop = abs(atr_pct * 100 * atr_trail_mult)
            dynamic_trail_stop = max(dynamic_trail_stop, 1.5)
            if pnl_pct <= dynamic_hard_sl:
                reason = f'[ETF Stop] Hard SL (ATR 기반): 손실률 {pnl_pct:.2f}% (<= {dynamic_hard_sl:.2f}%)'
                urgency = 3
            elif days_held >= dynamic_max_hold:
                reason = f'[ETF Stop] Medallion Decay: {days_held}일 보유 >= 동적 임계값 {dynamic_max_hold}일 (VIX={vix:.1f})'
                urgency = 2
            elif ticker in cfg.get('leverage_judge.inverse_tickers', ['114800', '252670']) and vix < macro_vix_threshold:
                reason = f'[ETF Stop] Macro 안정화 (VIX={vix:.1f} < {macro_vix_threshold:.1f}) → 인버스 동적 청산'
                urgency = 3
            elif drawdown_from_hwm >= dynamic_trail_stop:
                reason = f'[ETF Stop] Trailing Stop (ATR 기반): HWM 대비 {drawdown_from_hwm:.1f}% 하락 (>= {dynamic_trail_stop:.2f}%)'
                urgency = 2
            if reason:
                exit_orders.append({'stream_id': stream_id, 'ticker': ticker, 'name': pos.get('name', ''), 'direction': 'short', 'amount_krw': pos.get('amount', 0), 'price': current_price, 'confidence': 1.0, 'strategy': 'etf_dynamic_exit', 'reason': reason, 'sell_type': 'etf_stop', 'urgency': urgency, 'pos_key': pos_key, 'pnl_pct': pnl_pct})
        active_keys = set(positions.keys())
        for key in list(self.etf_state.keys()):
            if key not in active_keys or positions.get(key, {}).get('quantity', positions.get(key, {}).get('qty', 0)) <= 0:
                del self.etf_state[key]
        return exit_orders
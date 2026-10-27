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
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import logging
import math
import os
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
        leverage = 1
        reasons = []
        min_sharpe_3x = cfg.get('leverage.3x_min_sharpe', 2.0)
        max_mdd_3x = pget('leverage.3x_max_mdd_pct', -3.0)
        max_corr_3x = pget('leverage.3x_max_correlation', 0.3)
        if measurement['sharpe_30d'] >= min_sharpe_3x and measurement['mdd_30d_pct'] > max_mdd_3x and (measurement['max_inter_stream_corr'] < max_corr_3x) and (regime == 'bull'):
            leverage = 3
            reasons.append(f"3X: Sharpe={measurement['sharpe_30d']:.2f}≥{min_sharpe_3x}, MDD={measurement['mdd_30d_pct']:.1f}%>{max_mdd_3x}%, ρ={measurement['max_inter_stream_corr']:.2f}<{max_corr_3x}")
        elif leverage < 2:
            min_sharpe_2x = pget('leverage.2x_min_sharpe', 1.5)
            min_confidence = pget('leverage.2x_min_confidence', 0.7)
            min_wins = pget('leverage.2x_min_consecutive_wins', 5)
            max_var = pget('leverage.2x_max_var_pct', 1.5)
            if measurement['sharpe_30d'] >= min_sharpe_2x and measurement['regime_confidence'] >= min_confidence and (measurement['consecutive_wins'] >= min_wins) and (measurement['var_95_pct'] < max_var) and (regime in ('bull', 'caution')):
                leverage = 2
                reasons.append(f"2X: Sharpe={measurement['sharpe_30d']:.2f}≥{min_sharpe_2x}, 승연속={measurement['consecutive_wins']}≥{min_wins}")
        if leverage == 1:
            reasons.append('1X: 레버리지 조건 미충족')
        inverse_allowed = False
        inverse_reason = ''
        vix_threshold = pget('regime.vix_caution_threshold', 25)
        if regime in ('bear', 'crash'):
            if measurement['vix'] > vix_threshold:
                inverse_allowed = True
                inverse_reason = f"인버스 허용: VIX={measurement['vix']:.1f}>{vix_threshold}"
            elif regime == 'crash':
                inverse_allowed = True
                inverse_reason = f'인버스 허용: CRASH 레짐'
        _etf = self._get_etf_ticker(leverage)
        _etf_factor = self._etf_leverage_factor(_etf, leverage)
        if _etf_factor is not None and abs(_etf_factor - leverage) > 1e-9 and leverage > 1:
            logger.warning(f"  [LeverageJudge] leverage_level={leverage}X 이지만 매핑 ETF {_etf} 의 실제 배수는 {_etf_factor:g}X "
                           f"(ticker_master) — 노출 계산은 etf_leverage_factor 사용 권장")
        return {'etf_leverage_factor': _etf_factor, 'leverage_level': leverage, 'inverse_allowed': inverse_allowed, 'inverse_max_pct': pget('leverage.inverse_max_pct', 0.5) if inverse_allowed else 0, 'etf_ticker': self._get_etf_ticker(leverage), 'inverse_ticker': cfg.get('leverage.inverse_ticker', '114800') if inverse_allowed else None, 'reasons': reasons, 'inverse_reason': inverse_reason, 'regime': regime}

    @staticmethod
    def _etf_leverage_factor(etf_ticker: str, level: int):
        """[Wave2 TrackF] 매핑 ETF 의 '진짜' 배수 (ticker_master → STRUCTURAL_ETP). 미상이면 level."""
        try:
            from src.data_collection.ticker_master import get_ticker_master
            lev = get_ticker_master().leverage(etf_ticker)
            return float(lev) if lev is not None else float(level)
        except Exception:
            return float(level)

    def _get_etf_ticker(self, leverage: int) -> str:
        """레버리지 수준에 맞는 ETF 티커."""
        if leverage >= 3:
            # NOTE(Wave2 TrackF): 키 이름은 'etf_3x' 이지만 233740(KODEX 코스닥150레버리지)은 실제 2X 상품
            # (KIS 마스터 실측). KRX 에는 3X ETF 가 없음. 실제 배수는 judge() 의 etf_leverage_factor 참조.
            return cfg.get('leverage.etf_3x', '233740')
        elif leverage >= 2:
            return cfg.get('leverage.etf_2x', '122630')
        return cfg.get('leverage.etf_1x', '069500')

    def calculate_holding_decay_haircut(self, holding_days: int, regime: str) -> float:
        """Phase 6: 보유 기간별 2X ETP 변동성 누수(Decay) 감쇄 인자 계산.

        - 추세장 (bull/bear): 3~5일 오버나이트 보유 승인 (haircut = 1.0)
        - 횡보장 (caution/range): 2일 차부터 매일 30~50% 비중 자동 감축 (haircut = max(0.2, 1.0 - 0.35 * (days-1)))
        """
        if holding_days <= 1:
            return 1.0
        if regime in ('bull', 'bear', 'crash'):
            return max(pget('leverage_judge.trend_haircut_floor', 0.6), 1.0 - pget('leverage_judge.trend_haircut_slope', 0.1) * (holding_days - 1))
        # 횡보장 (caution/range)
        haircut = max(pget('leverage_judge.range_haircut_floor', 0.2), 1.0 - pget('leverage_judge.range_haircut_slope', 0.35) * (holding_days - 1))
        logger.warning(f"📉 [LeverageJudge] Phase 6 횡보장 보유 감쇄 발화 (보유일={holding_days}일, 감쇄비율={haircut:.2f})")
        return haircut

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

    def check_etf_stops(self, positions: Dict, market_data: Dict, regime: Any = None) -> List[Dict]:
        """
        ETF(레버리지/인버스) 포지션에 대한 Tracking 상태(영구 HWM 원장 동기화)를 업데이트하고,
        청산 조건에 도달한 포지션의 청산 주문(reason, urgency 포함)을 반환합니다.
        100% 수학적 동적 모델 원칙:
          1) 고정 VIX 22.0 상수를 폐기하고 DynamicQuantEngine 로그정규 분위수(Q_caution)와 VKOSPI를 결합
          2) 한국/시장 레짐이 bear/crash이거나 양(+)의 추세 수익(pnl_pct > 0, HWM 트레일링 미이탈) 진행 중일 때는
             'Macro 안정화' 명목의 인위적 강제 청산을 금지하고 동적 ATR 트레일링 스탑으로 수익 추종
          3) 레버리지 롱은 고변동성에서 변동성 누수(Volatility Drag) 방지를 위해 보유기한을 단축하되,
             인버스 헤지는 bear/crash 레짐 동안 일수 제한(Medallion Decay) 면제
        """
        exit_orders = []
        etf_tickers_str = cfg.get('leverage_judge.etf_tickers', '114800,252670,122630,233740,0195R0,0195S0')
        etf_tickers = [t.strip() for t in etf_tickers_str.split(',')]
        inverse_tickers = set(cfg.get('leverage_judge.inverse_tickers', ['114800', '252670']))
        base_max_hold = int(pget('leverage_judge.max_hold_days', 5))
        atr_sl_mult = float(cfg.get('leverage_judge.atr_sl_mult', 2.0))
        atr_trail_mult = float(pget('leverage_judge.atr_trail_mult', 1.5))

        signal_cache = (market_data or {}).get('signal_cache', {}) if isinstance(market_data, dict) else {}
        current_date = (market_data or {}).get('date', datetime.now().strftime('%Y-%m-%d')) if isinstance(market_data, dict) else datetime.now().strftime('%Y-%m-%d')

        # 1) 현재 레짐 동적 해석 (인자 -> market_data -> signal_cache -> pipeline_state -> config)
        resolved_regime = ''
        if isinstance(regime, dict):
            resolved_regime = str(regime.get('primary_regime') or regime.get('regime') or '').lower()
        elif isinstance(regime, str) and regime.strip():
            resolved_regime = regime.strip().lower()
        if not resolved_regime and isinstance(market_data, dict):
            _md_reg = market_data.get('regime') or market_data.get('current_regime') or signal_cache.get('regime')
            if isinstance(_md_reg, dict):
                resolved_regime = str(_md_reg.get('primary_regime') or _md_reg.get('regime') or '').lower()
            elif isinstance(_md_reg, str):
                resolved_regime = _md_reg.strip().lower()
        if not resolved_regime:
            resolved_regime = str(cfg.get('market.current_regime', 'caution')).lower()

        # 2) VIX 및 VKOSPI 실측치 + 로그정규 분포 동적 분위수(DynamicQuantEngine) 산출
        vix_fallback = float(pget('leverage_judge.macro_vix_threshold', 22.0))
        vix_raw = signal_cache.get('vix', vix_fallback)
        vkospi_raw = signal_cache.get('vkospi', 0.0)
        try:
            vix = float(vix_raw)
            if math.isnan(vix) or vix <= 0:
                vix = vix_fallback
        except (ValueError, TypeError):
            vix = vix_fallback
        try:
            vkospi = float(vkospi_raw or 0.0)
            if math.isnan(vkospi) or vkospi < 0:
                vkospi = 0.0
        except (ValueError, TypeError):
            vkospi = 0.0

        effective_vol_index = max(vix, vkospi)
        dynamic_vix_caution = vix_fallback
        dynamic_vix_baseline = float(pget('leverage_judge.vix_hold_ref', 10.0))
        dqe = None
        try:
            from src.utils.dynamic_quant_engine import get_quant_engine
            dqe = get_quant_engine()
            vq = dqe.get_vix_regime_quantiles(signal_cache)
            dynamic_vix_caution = float(vq.get('caution', vix_fallback) or vix_fallback)
            dynamic_vix_baseline = float(vq.get('baseline', dynamic_vix_baseline) or dynamic_vix_baseline)
            inverse_tickers.update(dqe.get_inverse_hedge_tickers())
        except Exception:
            pass

        vix_floor = float(pget('leverage_judge.vix_hold_floor', 10.0))
        long_dynamic_max_hold = max(1, int(round(base_max_hold * (max(dynamic_vix_baseline, vix_floor) / max(effective_vol_index, vix_floor)))))

        ledger_mgr = None
        try:
            from src.execution.position_ledger import PositionLedgerManager
            ledger_mgr = PositionLedgerManager()
        except Exception:
            ledger_mgr = None

        is_bear_or_crash = resolved_regime in ('bear', 'crash', 'panic')

        for pos_key, pos in positions.items():
            ticker = str(pos.get('ticker', pos_key.split(':')[-1]))
            _tk_u_lj = ticker.strip().upper()
            _lm_lj = ledger_mgr.get_position_meta(_tk_u_lj) if ledger_mgr is not None else {}
            stream_id = pos_key.split(':')[0] if ':' in pos_key else str(pos.get('stream_id') or _lm_lj.get('stream_id') or '')
            _strat_lj = str(pos.get('strategy') or _lm_lj.get('strategy') or '').lower()
            _held_tks_lj = {
                str(_pv.get('ticker', str(_pk).split(':')[-1])).strip().upper()
                for _pk, _pv in positions.items()
                if isinstance(_pv, dict) and float(_pv.get('quantity', _pv.get('qty', 0)) or 0) > 0
            }
            from src.allocation.sector_pair_alpha_engine import get_dynamic_s14_pair_sets as _get_s14_sets_lj
            _kr_l_lj, _kr_i_lj, _us_l_lj, _us_i_lj = _get_s14_sets_lj()
            _is_s14_pair = (
                stream_id in ('S14', 'S14_PAIR_ALPHA')
                or 'pair' in _strat_lj
                or bool(pos.get('pair_id'))
                or (_tk_u_lj in _kr_i_lj and bool(_held_tks_lj & _kr_l_lj))
                or (_tk_u_lj in _us_i_lj and bool(_held_tks_lj & _us_l_lj))
            )
            if _is_s14_pair:
                continue
            is_inverse_pos = (ticker in inverse_tickers) or (stream_id == 'SYS_HEDGE')
            is_leverage_or_inverse = (ticker in etf_tickers) or is_inverse_pos
            if not is_leverage_or_inverse:
                continue
            qty = pos.get('quantity', pos.get('qty', 0))
            if qty <= 0:
                continue
            entry_price = float(pos.get('entry_price') or pos.get('avg_price', 0) or 0)
            current_price = float(pos.get('current_price', 0))
            if entry_price <= 0 or current_price <= 0:
                continue

            # 3) 영구 원장(PositionLedgerManager) 및 포지션 메타데이터에서 실제 HWM / 진입일 복원
            saved_peak = max(
                current_price,
                entry_price,
                float(pos.get('peak_price', 0.0) or 0.0),
                float(pos.get('highest_price', 0.0) or 0.0),
            )
            saved_entry_date = pos.get('entry_date')
            if ledger_mgr is not None:
                try:
                    hwm_info = ledger_mgr.get_hwm(ticker)
                    saved_peak = max(saved_peak, float(hwm_info.get('peak_price', 0.0) or 0.0))
                    if not saved_entry_date:
                        saved_entry_date = ledger_mgr.get_entry_date(ticker)
                except Exception:
                    pass
            if not saved_entry_date:
                saved_entry_date = current_date

            if pos_key not in self.etf_state:
                self.etf_state[pos_key] = {
                    'hwm': saved_peak,
                    'entry_price': entry_price,
                    'entry_date': saved_entry_date,
                }
            state = self.etf_state[pos_key]
            state['hwm'] = max(float(state.get('hwm', current_price) or current_price), saved_peak, current_price)
            if not state.get('entry_date'):
                state['entry_date'] = saved_entry_date

            pnl_pct = (current_price / entry_price - 1) * 100
            if ledger_mgr is not None and not os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('MERIDIAN_TEST_MODE') != '1':
                try:
                    hwm_updated = ledger_mgr.update_hwm(
                        ticker,
                        current_price=current_price,
                        current_pnl_pct=pnl_pct,
                        avg_price=entry_price,
                        quantity=float(qty),
                    )
                    if float(hwm_updated.get('peak_price', 0.0) or 0.0) > 0:
                        state['hwm'] = float(hwm_updated['peak_price'])
                except Exception:
                    pass
            drawdown_from_hwm = (state['hwm'] - current_price) / state['hwm'] * 100 if state['hwm'] > 0 else 0
            peak_pnl_pct = (state['hwm'] / entry_price - 1) * 100 if entry_price > 0 and state['hwm'] > 0 else 0.0
            try:
                import pandas as pd
                entry_dt = pd.to_datetime(state['entry_date']).date()
                curr_dt = pd.to_datetime(current_date).date()
                days_held = (curr_dt - entry_dt).days
            except Exception:
                days_held = pos.get('days_held', 0)

            reason = None
            urgency = 'normal'
            atr_pct = 0.0
            try:
                from src.execution.risk_params import _estimate_atr_pct
                atr_pct = float(_estimate_atr_pct(ticker, market_data) or 0.0)
            except Exception:
                atr_pct = 0.0
            if atr_pct <= 0 and dqe is not None:
                try:
                    atr_pct = float(dqe.get_asset_metrics(ticker).get('vol_daily', 0.0) or 0.0)
                except Exception:
                    atr_pct = 0.0
            if atr_pct <= 0:
                atr_pct = max(pget('leverage_judge.atr_proxy_floor', 0.01), effective_vol_index / (100.0 * 15.87))

            # [이산 호가 양자화 노이즈 하한 (Discrete Tick Quantization Floor)]
            # 저가형 ETF(예: 252670 ₩114, 1호가=₩1=0.88%)에서 2~3틱 호가 진동만으로 트레일링 스탑이 오발동하지 않도록
            # KRX 법정 호가단위(tick_pct)의 최소 6틱을 손절/트레일링 폭 하한으로 보장하고, 최소 3틱 이상 수익 구간 진입 후에만 트레일링 활성화
            tick_pct = 0.0
            if current_price > 0 and str(ticker).isdigit():
                try:
                    tick_sz = float(dqe.get_krx_tick_size(current_price)) if (dqe is not None and hasattr(dqe, 'get_krx_tick_size')) else 1.0
                    tick_pct = (tick_sz / current_price) * 100.0
                except Exception:
                    tick_pct = 0.0

            dynamic_hard_sl = -abs(atr_pct * 100 * atr_sl_mult)
            dynamic_hard_sl = min(dynamic_hard_sl, pget('leverage_judge.hard_sl_cap_pct', -2.0), -(2.0 + 2.0 + 1.0) * tick_pct)
            dynamic_trail_stop = abs(atr_pct * 100 * atr_trail_mult)
            dynamic_trail_stop = max(dynamic_trail_stop, pget('leverage_judge.trail_stop_floor_pct', 1.5), (2.0 + 2.0 + 1.0) * tick_pct)
            trail_activate_min_pct = max((2.0 + 1.0) * tick_pct, pget('leverage_judge.trail_stop_floor_pct', 1.5) * 2.0)

            # 인버스 헤지는 bear/crash 레짐이거나 변동성 스트레스가 caution 분위수 이상일 때 보유기한 만료(Decay) 면제
            effective_max_hold = base_max_hold if (is_inverse_pos and (is_bear_or_crash or effective_vol_index >= dynamic_vix_caution)) else long_dynamic_max_hold
            allow_time_decay_exit = not (is_inverse_pos and is_bear_or_crash)

            if pnl_pct <= dynamic_hard_sl:
                reason = f'[ETF Stop] Hard SL (ATR 기반): 손실률 {pnl_pct:.2f}% (<= {dynamic_hard_sl:.2f}%)'
                urgency = 'panic'
            elif drawdown_from_hwm >= dynamic_trail_stop and state['hwm'] > entry_price and peak_pnl_pct >= trail_activate_min_pct and pnl_pct > 0:
                reason = f'[ETF Stop] Trailing Stop (ATR 기반): HWM 대비 {drawdown_from_hwm:.1f}% 하락 (>= {dynamic_trail_stop:.2f}%)'
                urgency = 'stop'
            elif is_inverse_pos and (not is_bear_or_crash) and (effective_vol_index < dynamic_vix_caution) and (pnl_pct <= 0):
                reason = (
                    f'[ETF Stop] Macro 안정화 (Regime={resolved_regime}, '
                    f'Vol={effective_vol_index:.1f} < Q_caution={dynamic_vix_caution:.1f}, PnL={pnl_pct:+.2f}%) → 인버스 동적 청산'
                )
                urgency = 'stop'
            elif allow_time_decay_exit and days_held >= effective_max_hold:
                reason = f'[ETF Stop] Medallion Decay: {days_held}일 보유 >= 동적 임계값 {effective_max_hold}일 (Vol={effective_vol_index:.1f})'
                urgency = 'stop'

            if reason:
                exit_orders.append({
                    'stream_id': stream_id,
                    'ticker': ticker,
                    'name': pos.get('name', ''),
                    'action': 'sell',
                    'direction': 'sell',
                    'quantity': int(qty),
                    'amount_krw': pos.get('amount') or pos.get('eval_krw') or round(qty * current_price),
                    'price': current_price,
                    'confidence': 1.0,
                    'strategy': 'etf_dynamic_exit',
                    'reason': reason,
                    'sell_type': 'etf_stop',
                    'urgency': urgency,
                    'pos_key': pos_key,
                    'pnl_pct': pnl_pct,
                })
        active_keys = set(positions.keys())
        for key in list(self.etf_state.keys()):
            if key not in active_keys or positions.get(key, {}).get('quantity', positions.get(key, {}).get('qty', 0)) <= 0:
                del self.etf_state[key]
        return exit_orders
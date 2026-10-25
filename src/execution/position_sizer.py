"""
Position Sizer — 마찰비용 기반 동적 하한선 (Friction-based Dynamic Lower Bound)
================================================================================
[Phase 11: Dynamic Balance] Phase 11-A: 동적 포지션 사이징
[Phase 46: Entry-Exit ATR-EV Sync + SNR Hurdle]

핵심 철학:
  "최소 비중 10%" 같은 자의적 하한선 포기.
  어떤 Kelly 비중이라도 → 기대 수익(EV) > 마찰 비용(Friction)이면 집행.
  EV <= Friction이면 수학적으로 Drop(기각).

수식:
  EV      = up_prob * tp_pct - (1 - up_prob) * sl_pct
  Friction = (slippage_bps + commission_bps) / 100 * 2  (왕복)
  SNR      = |EV| / (ATR% * 100) >= snr_threshold  (기본 0.5)
  -> EV > Friction 구도 충족, SNR 통과 시에만 집행 가능

[Phase 46] ATR-EV SSOT 동기화:
  기존: 하드코딩 sl_pct=5%, tp_pct=12% → EV 계산
  변경: compute_dynamic_sl_tp() → DynamicExit와 동일 ATR 기반 SL/TP
         → Whipsaw 근본 해소

VIX 반응형 스케일링:
  final_size = kelly_size * vix_scale_factor (신호에서 주입)
  vix_scale_factor = clip(vix_neutral / vix, 0.40, 1.20)
"""
import logging
import math
from pathlib import Path
from typing import Dict, Optional
logger = logging.getLogger(__name__)
try:
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
except ImportError as e:
    cfg = None
try:
    from src.execution.risk_params import compute_dynamic_sl_tp, _estimate_atr_pct
    _RISK_PARAMS_AVAILABLE = True
except Exception as _rpe:
    _RISK_PARAMS_AVAILABLE = False
    logging.getLogger(__name__).debug(f"  [PositionSizer] risk_params 로드 실패 (하드코딩 Fallback): {_rpe}")
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class PositionSizer:
    """마찰비용 기반 동적 포지션 사이저.

    [Phase 11: Dynamic Balance] Phase 11-A
    전통적인 "최소 비중 X%" 하드코딩을 버리고
    수학적으로 EV > Friction 조건을 만족하는 신호만 통과시킨다.
    Almgren-Chriss 슬리피지 모델(AdvancedSlippageModel)과 완전 연동.
    """

    def __init__(self):
        self._slippage = None
        try:
            from src.execution.slippage_model import AdvancedSlippageModel
            self._slippage = AdvancedSlippageModel()
        except Exception as e:
            logger.critical(f"  [PositionSizer] SlippageModel 연결 실패 (기본값 사용): {e}", exc_info=True)
        self._cfg_get = (lambda k, d=None: cfg.get(k, d)) if cfg else lambda k, d=None: d

    def compute(self, signal: Dict, portfolio_value: float=0.0, regime: str='caution', vix: float=0.0, adv: float=0.0, market_cap: float=0.0) -> Dict:
        """신호의 최종 포지션 비중 결정.

        [Phase 11: Dynamic Balance]
        1. Kelly 비중(size_pct) 또는 suggested_weight 추출
        2. 마찰비용(Friction) 계산 (슬리피지 모델 연동)
        3. EV > Friction 조건 검사 -> 통과 시 집행, 실패 시 Drop
        4. VIX 스케일 팩터 적용 (신호 내 vix_scale_factor 우선)

        Returns:
            {
                'approved':    bool,
                'final_size':  float,
                'kelly_size':  float,
                'ev_pct':      float,
                'friction_pct':float,
                'ev_margin':   float,
                'vix_scale':   float,
                'reason':      str,
                'drop_reason': str,
            }
        """
        ticker = signal.get('ticker', '')
        conf = float(signal.get('confidence', 0.5))
        market_data = signal.get('market_data')
        if market_data is None:
            market_data = {}
        kelly_size = float(signal.get('kelly_size', signal.get('size_pct', pget('sizer.default_kelly_size', 0.1))))
        _sentinel_barrier = 500
        _pct_unit_cutoff = 5.0
        _raw_sig_tp = float(signal.get('tp_pct') or 0.0)
        _raw_sig_sl = abs(float(signal.get('sl_pct') or 0.0))
        _erp_val = float(signal.get('expected_return_pct') or 0.0)
        if _erp_val <= 0.0 and signal.get('expected_return') is not None:
            _er = float(signal.get('expected_return') or 0.0)
            _erp_val = _er if abs(_er) > _pct_unit_cutoff else (_er * 100.0)
        _sig_tp = _raw_sig_tp if (0.0 < _raw_sig_tp < _sentinel_barrier) else (_erp_val if (0.0 < _erp_val < _sentinel_barrier) else 0.0)
        _sig_sl = _raw_sig_sl if (0.0 < _raw_sig_sl < _sentinel_barrier) else 0.0
        if signal.get('strategy') == 'risk_free_parking' or bool(signal.get('risk_free_parking')):
            final_size = round(min(1.0, max(0.0, float(kelly_size))), 4)
            _er_pk = float(signal.get('expected_return') or 0.0)
            _pk_ev = _sig_tp if _sig_tp > 0.0 else (_er_pk if abs(_er_pk) > _pct_unit_cutoff else (_er_pk * 100.0))
            _pk_tp_out = _raw_sig_tp if _raw_sig_tp > 0.0 else _sig_tp
            _pk_sl_out = _raw_sig_sl if _raw_sig_sl > 0.0 else _sig_sl
            return {
                'approved': final_size > 0.0,
                'final_size': final_size,
                'kelly_size': kelly_size,
                'ev_pct': round(_pk_ev, 4),
                'friction_pct': 0.0,
                'ev_margin': round(_pk_ev, 4),
                'vix_scale': 1.0,
                'reason': f"[Phase 11] Risk-Free Parking bypass: size={final_size:.2%}",
                'drop_reason': '' if final_size > 0.0 else 'zero_size',
                'sl_pct': _pk_sl_out,
                'tp_pct': _pk_tp_out,
                'is_probe': False,
            }
        if _sig_tp > 0.0 and _sig_sl > 0.0:
            tp_pct = _sig_tp
            sl_pct = _sig_sl
        elif _RISK_PARAMS_AVAILABLE and ticker:
            try:
                sl_pct, tp_pct = compute_dynamic_sl_tp(ticker=ticker, regime=regime, market_data=market_data)
                if _sig_tp > 0.0:
                    tp_pct = _sig_tp
                if _sig_sl > 0.0:
                    sl_pct = _sig_sl
            except Exception as _ssot_e:
                logger.error(f"  [PositionSizer] SSOT compute_dynamic_sl_tp 실패, Fallback 적용: {_ssot_e}", exc_info=True)
                tp_pct = _sig_tp if _sig_tp > 0.0 else float(pget('s2.exit.tp.caution', 12.0))
                sl_pct = _sig_sl if _sig_sl > 0.0 else abs(float(pget('s2.exit.sl.caution', -5)))
        else:
            vix_val = float(market_data.get('vix', pget('sizer.fallback_vix', 20.0)))
            daily_vol_pct = vix_val / math.sqrt(252)
            tp_pct = _sig_tp if _sig_tp > 0.0 else float(daily_vol_pct * pget('sizer.fallback_tp_vol_mult', 3.0))
            sl_pct = _sig_sl if _sig_sl > 0.0 else abs(float(daily_vol_pct * pget('sizer.fallback_sl_vol_mult', 1.5)))
        mu_pct = self._compute_ev(signal, conf, tp_pct, sl_pct)
        # [Dynamic Non-Linear Volatility Neutral Base] 롤링 252일 VIX 중간값(Median) 동적 연산 (15.5 고정 상수 100% 제거)
        _vix_neutral_param = pget('sizer.vix_neutral', 17.5)  # L2: trailing 252d median VIX (tighten_only, quality-gated)
        vix_neutral = float(market_data.get('vix_rolling_median_252', market_data.get('vix_ma_252', _vix_neutral_param)) or _vix_neutral_param)
        vix_raw = vix if (vix is not None and float(vix) > 0) else (market_data.get('vix', vix_neutral) if market_data else vix_neutral)
        try:
            vix_val = float(vix_raw)
            if math.isnan(vix_val) or vix_val <= 0:
                vix_val = vix_neutral
        except (ValueError, TypeError):
            vix_val = vix_neutral
        market_var = (max(vix_val, pget('sizer.vix_floor', 10.0)) / vix_neutral) ** 2
        sigma_pct = max(0.01, sl_pct)
        friction_pct = self._compute_friction(signal=signal, kelly_size=kelly_size, portfolio_value=portfolio_value, regime=regime, adv=adv, market_cap=market_cap, ticker=ticker)
        ev_margin = mu_pct - friction_pct
        if vix is None or float(vix) <= 0:
            vix = float(signal.get('vix', market_data.get('vix', 0) if market_data else 0) or 0.0)
            if vix <= 0 or math.isnan(vix):
                logger.warning(f"  [PositionSizer] {ticker}: VIX 데이터 누락. 진입 차단 (Stale-Halt 방어)")
                return {'approved': False, 'final_size': 0.0, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': 1.0, 'reason': 'VIX 데이터 누락', 'drop_reason': 'missing_vix_stale_halt'}
        # [Model 4] OU Kalman prediction variance inflation & continuous Kelly shrinkage on stale VIX
        vix_age_h = market_data.get('vix_age_hours', signal.get('vix_age_hours', 0.0))
        kelly_stale_scale = 1.0
        if vix_age_h is not None and float(vix_age_h or 0.0) > 0.0:
            from src.params.dynamic_quant_models import ou_kalman_vix_state
            ou_st = ou_kalman_vix_state(last_vix=vix, age_hours=float(vix_age_h), long_run_vix=vix_neutral)
            vix = ou_st.effective_vix
            vix_val = max(vix_val, ou_st.effective_vix)
            market_var = (max(vix_val, pget('sizer.vix_floor', 10.0)) / vix_neutral) ** 2
            kelly_stale_scale = ou_st.kelly_entry_scale
            if ou_st.exit_only:
                return {'approved': False, 'final_size': 0.0, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': 0.0, 'reason': 'VIX 정보 품질 임계치 미달 (OU Kalman Stale Halt)', 'drop_reason': 'stale_vix_ou_halt'}
        regime_min_ev = pget('allocator.min_net_ev_pct', 0.35, ctx={'regime': regime})
        vix_margin_sc = pget('sizer.vix_extra_margin_slope', 0.002)
        extra_margin = max(0.0, (vix - vix_neutral) * vix_margin_sc)
        effective_min = max(regime_min_ev, extra_margin)
        if ev_margin < effective_min:
            reason = f"[Phase 11] Drop: EV={mu_pct:.3f}% - Friction={friction_pct:.3f}%={ev_margin:+.3f}% < regime_min={effective_min:.3f}%(Regime={regime}, VIX={vix:.1f})"
            logger.debug(f"  [PositionSizer] {ticker}: {reason}")
            return {'approved': False, 'final_size': 0.0, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': 1.0, 'reason': reason, 'drop_reason': 'ev_lt_friction'}
        base_snr_threshold = 0.5
        snr_threshold = base_snr_threshold * math.exp((vix_val - vix_neutral) / 20.0)
        snr = abs(mu_pct) / sigma_pct
        snr_scale = min(1.0, snr / snr_threshold) if snr_threshold > 0.0 else 1.0
        
        # [Dynamic Cornish-Fisher Non-Linear Kelly] 실시간 수익률 왜도/첨도 롤링 분포 동적 적용
        _skew_d = pget('sizer.default_skew', -0.2)
        _kurt_d = pget('sizer.default_kurtosis', 3.0)
        _raw_sk = market_data.get('skewness', market_data.get('rolling_skew_20'))
        _raw_kt = market_data.get('kurtosis', market_data.get('rolling_kurt_20'))
        S_skew = float(_raw_sk) if _raw_sk is not None else float(_skew_d)
        K_kurt = float(_raw_kt) if (_raw_kt is not None and float(_raw_kt) > 0.0) else float(_kurt_d)
        raw_size = kelly_size
        mu_dec = mu_pct / 100.0
        sigma_dec = max(0.005, sigma_pct / 100.0)
        _excess_kurt = max(0.0, K_kurt - 3.0)
        cf_penalty = 1.0 + S_skew * mu_dec / (3.0 * sigma_dec) - _excess_kurt * (mu_dec ** 2) / (4.0 * (sigma_dec ** 2))
        half_kelly_factor = pget('sizer.half_kelly_factor', 0.5)
        vix_scale = (1.0 / market_var) * kelly_stale_scale * snr_scale
        final_size = raw_size * vix_scale * max(pget('sizer.cf_penalty_floor', 0.1), min(1.0, cf_penalty)) * half_kelly_factor
        
        # ★ 대표님 지시 반영: 횡보장(caution) 미세 정찰대(Probe Sizing = 3% NAV) 공격적 투입!
        is_probe = False
        if regime in ('caution', 'neutral'):
            probe_cap = pget('sizer.probe_sizing_pct.caution', 0.03)
            final_size = min(final_size, probe_cap)
            is_probe = True
            logger.info(f"  🎯 [Probe Sizing] {ticker}: 횡보장 미세 정찰대({final_size:.2%} NAV) 공격적 투입 확정")

        final_size = round(max(0.0, min(1.0, final_size)), 4)
        
        if final_size <= 0.0:
            reason = "Drop: 최종 포지션 사이즈 0%"
            return {'approved': False, 'final_size': 0.0, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': 1.0, 'reason': reason, 'drop_reason': 'zero_size'}

        _prb = " [PROBE]" if is_probe else ""
        reason = f"[Phase 11] OK: EV={mu_pct:.3f}%>Friction={friction_pct:.3f}%(margin={ev_margin:+.3f}%), size={final_size:.2%}{_prb}"
        logger.debug(f"  [PositionSizer] {ticker}: {reason}")
        return {'approved': True, 'final_size': final_size, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': round(vix_scale, 4), 'reason': reason, 'drop_reason': '', 'sl_pct': sl_pct, 'tp_pct': tp_pct, 'is_probe': is_probe}

    def filter_signals(self, signals: list, portfolio_value: float=0.0, regime: str='caution', vix: float=0.0) -> list:
        """신호 리스트 일괄 필터링 (EV > Friction 통과 신호만 반환).

        [Phase 11: Dynamic Balance]
        """
        approved = []
        dropped = 0
        for sig in signals:
            result = self.compute(signal=sig, portfolio_value=portfolio_value, regime=regime, vix=vix)
            if result['approved']:
                _orig_tp = float(sig.get('tp_pct') or 0.0)
                _orig_sl = abs(float(sig.get('sl_pct') or 0.0))
                sig = dict(sig)
                sig['size_pct'] = result['final_size']
                sig['ev_pct'] = result['ev_pct']
                sig['friction_pct'] = result['friction_pct']
                sig['ev_margin'] = result['ev_margin']
                sig['vix_scale'] = result['vix_scale']
                sig['sizer_reason'] = result['reason']
                sig['sl_pct'] = _orig_sl if _orig_sl > 0.0 else result['sl_pct']
                sig['tp_pct'] = _orig_tp if _orig_tp > 0.0 else result['tp_pct']
                approved.append(sig)
            else:
                dropped += 1
                _tk_ps = sig.get('ticker', '')
                _dr_ps = result.get('drop_reason', '')
                logger.debug(f"  [Phase 11] PositionSizer Drop: {_tk_ps} -> {_dr_ps}")
        if dropped:
            logger.info(f"  [Phase 11: Dynamic Balance] PositionSizer: {len(approved)}/{len(signals)} 통과 ({dropped}건 EV<Friction Drop)")
        return approved

    def _compute_ev(self, signal: Dict, conf: float, tp_pct: float, sl_pct: float) -> float:
        """기대 수익률(Drift, mu) 계산. Continuous Kelly를 위한 기초 mu.
        이항 분포 대신 수익/손실의 기대 밀도값을 추정.
        """
        stream_id = signal.get('stream_id', signal.get('stream', ''))
        dividend_tax_streams = ('S0', 'S_YIELD', 'S5')
        effective_tp_pct = tp_pct
        if stream_id in dividend_tax_streams:
            effective_tp_pct = tp_pct * pget('tax.kr_dividend_net_factor', 0.846)
        mu = conf * effective_tp_pct - (1.0 - conf) * sl_pct
        return round(mu, 6)

    def _compute_friction(self, signal: Dict, kelly_size: float, portfolio_value: float, regime: str, adv: float, market_cap: float, ticker: str) -> float:
        """총 마찰비용(%) = 매수(슬리피지+수수료) + 매도(슬리피지+수수료+증권거래세).
        """
        from src.params.dynamic_quant_models import statutory_sell_tax_bps
        commission_bps = pget('sizer.commission_bps', 1.5)
        stream_id = signal.get('stream_id', signal.get('stream', ''))
        asset_type = str(signal.get('asset_type', '')).lower()
        t_str = str(ticker)
        name_str = str(signal.get('name', '')).lower()
        explicit_etf = (
            stream_id in ('S0', 'S1', 'S3_A', 'S5', 'S_YIELD') or 
            asset_type in ('etf', 'etn') or
            any(t_str.startswith(prefix) for prefix in ('0695', '1226', '2526', '1148', '4491', '4595', '3811', '2527')) or
            'etf' in t_str.lower() or 'etn' in t_str.lower() or 'kodex' in name_str or 'tiger' in name_str or 'ace' in name_str
        )
        as_of_dt = signal.get('as_of_date') or signal.get('date')
        tax_bps = statutory_sell_tax_bps(
            ticker=t_str,
            as_of_date=as_of_dt,
            is_etf=True if explicit_etf else None,
            market=signal.get('market'),
            base_kr_tax_bps=pget('tax.kr_sell_tax_bps', 18.0),
        )
        if self._slippage and portfolio_value > 0 and (kelly_size > 0):
            try:
                order_size = portfolio_value * kelly_size
                slip_result = self._slippage.estimate(order_size=order_size, adv=adv, market_cap=market_cap, regime=regime, ticker=ticker)
                slip_bps = slip_result.get('slippage_bps', 0.0)
            except Exception as _se:
                logger.critical(f"  [PositionSizer] 슬리피지 추정 실패: {_se}", exc_info=True)
                slip_bps = pget('sizer.default_slippage_bps', 5.0)
        else:
            slip_bps = pget('sizer.default_slippage_bps', 5.0)
        # Total bps = slip_bps * 2 + commission_bps * 2 + tax_bps
        # In percentage points (%): 31.0 bps / 100 = 0.31% friction
        total_bps = (slip_bps * 2.0) + (commission_bps * 2.0) + tax_bps
        roundtrip_pct = total_bps / 100.0  # 31.0 bps -> 0.31%
        return round(roundtrip_pct, 6)
_sizer_singleton: Optional[PositionSizer] = None

def get_sizer() -> PositionSizer:
    """전역 PositionSizer 싱글톤 반환."""
    global _sizer_singleton
    if _sizer_singleton is None:
        _sizer_singleton = PositionSizer()
    return _sizer_singleton

def filter_by_friction(signals: list, portfolio_value: float=0.0, regime: str='caution', vix: float=0.0) -> list:
    """신호 리스트에서 EV > Friction 통과 신호만 반환.

    [Phase 11: Dynamic Balance] 파이프라인 진입점.
    """
    return get_sizer().filter_signals(signals, portfolio_value, regime, vix)
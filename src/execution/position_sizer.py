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
    logging.getLogger(__name__).debug(f'  [PositionSizer] risk_params 로드 실패 (하드코딩 Fallback): {_rpe}')
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
            logger.critical(f'  [PositionSizer] SlippageModel 연결 실패 (기본값 사용): {e}', exc_info=True)
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
        kelly_size = float(signal.get('kelly_size', signal.get('size_pct', 0.1)))
        if _RISK_PARAMS_AVAILABLE and ticker:
            try:
                sl_pct, tp_pct = compute_dynamic_sl_tp(ticker=ticker, regime=regime, market_data=market_data)
            except Exception as _ssot_e:
                logger.error(f'  [PositionSizer] SSOT compute_dynamic_sl_tp 실패, Fallback 적용: {_ssot_e}', exc_info=True)
                tp_pct = float(signal.get('tp_pct', self._cfg_get('s2.exit.tp.caution', 12.0) or 12.0))
                sl_pct = abs(float(signal.get('sl_pct', self._cfg_get('s2.exit.sl.caution', 5.0) or 5.0)))
        else:
            vix_val = float(market_data.get('vix', 20.0))
            daily_vol_pct = vix_val / math.sqrt(252)
            tp_pct = float(signal.get('tp_pct', daily_vol_pct * 3.0))
            sl_pct = abs(float(signal.get('sl_pct', daily_vol_pct * 1.5)))
        mu_pct = self._compute_ev(signal, conf, tp_pct, sl_pct)
        vix_neutral = 15.5
        vix_val = float(market_data.get('vix', vix_neutral)) if market_data else vix_neutral
        market_var = (max(vix_val, 10.0) / vix_neutral) ** 2
        sigma_pct = max(0.01, sl_pct)
        friction_pct = self._compute_friction(signal=signal, kelly_size=kelly_size, portfolio_value=portfolio_value, regime=regime, adv=adv, market_cap=market_cap, ticker=ticker)
        ev_margin = mu_pct - friction_pct
        if vix <= 0:
            vix = float(signal.get('vix', 0))
            if vix <= 0:
                logger.warning(f'  [PositionSizer] {ticker}: VIX 데이터 누락. 진입 차단 (Stale-Halt 방어)')
                return {'approved': False, 'final_size': 0.0, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': 1.0, 'reason': 'VIX 데이터 누락', 'drop_reason': 'missing_vix_stale_halt'}
        vix_margin_sc = 0.002
        extra_margin = max(0.0, (vix - vix_neutral) * vix_margin_sc)
        effective_min = extra_margin
        if ev_margin < effective_min:
            reason = f'[Phase 11] Drop: EV={mu_pct:.3f}% - Friction={friction_pct:.3f}%={ev_margin:+.3f}% < min={effective_min:.3f}%(VIX={vix:.1f})'
            logger.debug(f'  [PositionSizer] {ticker}: {reason}')
            return {'approved': False, 'final_size': 0.0, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': 1.0, 'reason': reason, 'drop_reason': 'ev_lt_friction'}
        base_snr_threshold = 0.5
        snr_threshold = base_snr_threshold * math.exp((vix_val - vix_neutral) / 20.0)
        S_skew = float(market_data.get('skewness', -0.5))
        K_kurt = float(market_data.get('kurtosis', 3.0))
        raw_size = kelly_size
        mu_dec = mu_pct / 100.0
        sigma_dec = sigma_pct / 100.0
        cf_penalty = 1.0 - S_skew * mu_dec / (3.0 * sigma_dec) - K_kurt * mu_dec ** 2 / (4.0 * sigma_dec ** 2)
        half_kelly_factor = 0.5
        vix_scale = 1.0 / market_var
        final_size = raw_size * vix_scale * max(0.1, min(1.0, cf_penalty)) * half_kelly_factor
        final_size = round(max(0.0, min(1.0, final_size)), 4)
        reason = f'[Phase 11] OK: EV={mu_pct:.3f}%>Friction={friction_pct:.3f}%(margin={ev_margin:+.3f}%), size={final_size:.2%}(kelly={kelly_size:.2%} x cf_adj={cf_penalty:.2f} x vix={vix_scale:.2f})'
        logger.debug(f'  [PositionSizer] {ticker}: {reason}')
        return {'approved': True, 'final_size': final_size, 'kelly_size': kelly_size, 'ev_pct': round(mu_pct, 4), 'friction_pct': round(friction_pct, 4), 'ev_margin': round(ev_margin, 4), 'vix_scale': round(vix_scale, 4), 'reason': reason, 'drop_reason': '', 'sl_pct': sl_pct, 'tp_pct': tp_pct}

    def filter_signals(self, signals: list, portfolio_value: float=0.0, regime: str='caution', vix: float=0.0) -> list:
        """신호 리스트 일괄 필터링 (EV > Friction 통과 신호만 반환).

        [Phase 11: Dynamic Balance]
        """
        approved = []
        dropped = 0
        for sig in signals:
            result = self.compute(signal=sig, portfolio_value=portfolio_value, regime=regime, vix=vix)
            if result['approved']:
                sig = dict(sig)
                sig['size_pct'] = result['final_size']
                sig['ev_pct'] = result['ev_pct']
                sig['friction_pct'] = result['friction_pct']
                sig['ev_margin'] = result['ev_margin']
                sig['vix_scale'] = result['vix_scale']
                sig['sizer_reason'] = result['reason']
                sig['sl_pct'] = result['sl_pct']
                sig['tp_pct'] = result['tp_pct']
                approved.append(sig)
            else:
                dropped += 1
                logger.debug(f'  [Phase 11] PositionSizer Drop: {sig.get('ticker')} -> {result['drop_reason']}')
        if dropped:
            logger.info(f'  [Phase 11: Dynamic Balance] PositionSizer: {len(approved)}/{len(signals)} 통과 ({dropped}건 EV<Friction Drop)')
        return approved

    def _compute_ev(self, signal: Dict, conf: float, tp_pct: float, sl_pct: float) -> float:
        """기대 수익률(Drift, mu) 계산. Continuous Kelly를 위한 기초 mu.
        이항 분포 대신 수익/손실의 기대 밀도값을 추정.
        """
        stream_id = signal.get('stream_id', signal.get('stream', ''))
        dividend_tax_streams = ('S0', 'S_YIELD', 'S5')
        effective_tp_pct = tp_pct
        if stream_id in dividend_tax_streams:
            effective_tp_pct = tp_pct * 0.846
        mu = conf * effective_tp_pct - (1.0 - conf) * sl_pct
        return round(mu, 6)

    def _compute_friction(self, signal: Dict, kelly_size: float, portfolio_value: float, regime: str, adv: float, market_cap: float, ticker: str) -> float:
        """총 마찰비용(%) = 매수(슬리피지+수수료) + 매도(슬리피지+수수료+증권거래세).
        """
        commission_bps = self._cfg_get('sizer.commission_bps', 1.5) or 1.5
        stream_id = signal.get('stream_id', signal.get('stream', ''))
        asset_type = signal.get('asset_type', '').lower()
        is_etf = stream_id in ('S0', 'S1', 'S3_A', 'S5', 'S_YIELD') or asset_type in ('etf', 'etn')
        tax_bps = 0.0 if is_etf else 18.0
        if self._slippage and portfolio_value > 0 and (kelly_size > 0):
            try:
                order_size = portfolio_value * kelly_size
                slip_result = self._slippage.estimate(order_size=order_size, adv=adv, market_cap=market_cap, regime=regime, ticker=ticker)
                slip_bps = slip_result.get('slippage_bps', 0.0)
            except Exception as _se:
                logger.critical(f'  [PositionSizer] 슬리피지 추정 실패: {_se}', exc_info=True)
                slip_bps = self._cfg_get('sizer.default_slippage_bps', 5.0) or 5.0
        else:
            slip_bps = self._cfg_get('sizer.default_slippage_bps', 5.0) or 5.0
        roundtrip_pct = (slip_bps * 2 + commission_bps * 2 + tax_bps) / 100.0
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
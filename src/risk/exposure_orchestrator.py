"""
Exposure Orchestrator — 다층 노출 제어
=========================================
F&G + VIX + VKOSPI + 레짐을 종합하여 목표 노출도 산출.
단순 레짐별 배분을 넘어 실시간 감성 기반 동적 노출.

Usage:
    from src.risk.exposure_orchestrator import ExposureOrchestrator
    eo = ExposureOrchestrator()
    result = eo.calculate()
"""
import json, logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import numpy as np
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None
try:
    from src.risk.intraday_regime import IntradayRegimeDetector
    _INTRADAY_AVAILABLE = True
except ImportError as e:
    _INTRADAY_AVAILABLE = False

def _compute_joint_risk_matrix(nav_histories: dict, lookback: int=None) -> dict:
    """스트림 간 상관관계 기반 Joint Risk Matrix 계산.

    모든 파라미터는 DynamicConfig에서 로드 (하드코딩 없음).

    Args:
        nav_histories: {stream_id: [nav_values]} 딕셔너리
        lookback: 수익률 계산 기간 (None이면 YAML에서 로드)

    Returns:
        {
          'corr_matrix': {s1: {s2: corr}},  # 페어별 상관계수
          'avg_pairwise_corr': float,         # 평균 상관계수
          'high_corr_pairs': [(s1,s2,corr)], # 위험 쌍 (상한 초과)
          'concentration_risk': float,        # 포트 집중도 위험 지수
          'joint_adj': float,                 # 최종 포트폴리오 조정 계수
        }
    """
    try:
        from config.dynamic_config import DynamicConfig as _DynCfg
        _jcfg = _DynCfg()
    except Exception:
        _jcfg = None

    def _jget(key, default):
        try:
            return _jcfg.get(key, default) if _jcfg else default
        except Exception:
            return default
    _lookback = lookback or int(_jget('risk.joint_matrix_lookback', 60))
    _corr_warn = float(_jget('risk.joint_corr_warn_threshold', 0.7))
    _adj_scale = float(_jget('risk.joint_adj_scale', 0.15))
    rets = {}
    for sid, navs in nav_histories.items():
        nav_arr = navs[-_lookback:] if len(navs) >= _lookback else navs
        if len(nav_arr) < 2:
            continue
        r = [(nav_arr[i] - nav_arr[i - 1]) / max(nav_arr[i - 1], 1e-09) for i in range(1, len(nav_arr))]
        rets[sid] = r
    sids = list(rets.keys())
    n = len(sids)
    if n < 2:
        return {'corr_matrix': {}, 'avg_pairwise_corr': 0.0, 'high_corr_pairs': [], 'concentration_risk': 0.0, 'joint_adj': 1.0}
    import math as _math

    def _corr(a, b):
        min_len = min(len(a), len(b))
        a, b = (a[-min_len:], b[-min_len:])
        if min_len < 3:
            return 0.0
        ma = sum(a) / min_len
        mb = sum(b) / min_len
        cov = sum(((a[i] - ma) * (b[i] - mb) for i in range(min_len))) / max(min_len - 1, 1)
        sa = _math.sqrt(sum(((x - ma) ** 2 for x in a)) / max(min_len - 1, 1))
        sb = _math.sqrt(sum(((x - mb) ** 2 for x in b)) / max(min_len - 1, 1))
        return cov / (sa * sb) if sa * sb > 1e-12 else 0.0
    corr_matrix = {s: {} for s in sids}
    pairwise_corrs = []
    high_corr_pairs = []
    for i in range(n):
        for j in range(i, n):
            s1, s2 = (sids[i], sids[j])
            c = 1.0 if i == j else _corr(rets[s1], rets[s2])
            corr_matrix[s1][s2] = round(c, 4)
            corr_matrix[s2][s1] = round(c, 4)
            if i != j:
                pairwise_corrs.append(abs(c))
                if abs(c) > _corr_warn:
                    high_corr_pairs.append((s1, s2, round(c, 4)))
    avg_corr = sum(pairwise_corrs) / max(len(pairwise_corrs), 1)
    total_pairs = max(n * (n - 1) / 2, 1)
    concentration_risk = len(high_corr_pairs) / total_pairs
    joint_adj = 1.0 - _adj_scale * concentration_risk
    joint_adj = max(float(_jget('risk.joint_adj_min', 0.7)), joint_adj)
    return {'corr_matrix': corr_matrix, 'avg_pairwise_corr': round(avg_corr, 4), 'high_corr_pairs': high_corr_pairs, 'concentration_risk': round(concentration_risk, 4), 'joint_adj': round(joint_adj, 4)}

class ExposureOrchestrator:
    """다층 노출 제어 엔진."""

    def __init__(self):
        self.W_REGIME = _cfg.get('exposure.w_regime', 0.3) if _cfg else 0.3
        self.W_VIX = _cfg.get('exposure.w_vix', 0.25) if _cfg else 0.25
        self.W_FNG = _cfg.get('exposure.w_fng', 0.25) if _cfg else 0.25
        self.W_VKOSPI = _cfg.get('exposure.w_vkospi', 0.1) if _cfg else 0.1
        self.W_TREND = _cfg.get('exposure.w_trend', 0.1) if _cfg else 0.1
        self._intraday = IntradayRegimeDetector() if _INTRADAY_AVAILABLE else None

    def calculate(self, sentiment: Optional[Dict]=None, data_penalty: float=1.0) -> Dict:
        """목표 노출도 산출.

        Returns:
            {'target_exposure': float, 'components': dict, 'reason': str}
        """
        if sentiment is None:
            sentiment = self._load_sentiment()
        components = {}
        reasons = []
        transition_adj = 1.0
        is_v_recovery = False
        try:
            from src.regime.transition_signal import TransitionSignalDetector
            tsd = TransitionSignalDetector()
            t_signal = tsd.detect()
            transition_adj = t_signal.get('exposure_adjustment', 1.0)
            sig_type = t_signal.get('signal_type', 'none')
            is_v_recovery = sig_type == 'v_recovery'
            if sig_type != 'none':
                reasons.append(f'Transition={sig_type}(str={t_signal.get('strength', 0):.2f})')
                components['transition'] = {'score': transition_adj, 'value': sig_type, 'strength': t_signal.get('strength', 0)}
        except Exception as e:
            logger.critical(f'  ExposureOrchestrator: TransitionSignalDetector 실패: {e}', exc_info=True)
        _cfg_get = (lambda key, default=None: _cfg.get(key, default)) if _cfg else lambda key, default=None: default
        regime = sentiment.get('regime', 'caution')
        regime_map = {'bull': _cfg_get('exposure.regime_score.bull', 1.0), 'caution': _cfg_get('exposure.regime_score.caution', 0.65), 'bear': _cfg_get('exposure.regime_score.bear', 0.3), 'crash': _cfg_get('exposure.regime_score.crash', 0.0)}
        regime_score = regime_map.get(regime, _cfg_get('exposure.regime_score.caution', 0.65))
        if regime == 'crash':
            flash_score = self._compute_flash_crash_gate(regime_score, sentiment, _cfg_get, is_v_recovery)
            if flash_score > regime_score:
                regime_score = flash_score
                reasons.append('Regime=crash (FlashCrash Override)')
                logger.info(f'  ⚡ FlashCrash 예외 발동! Regime Score 조정: 0.0 -> {regime_score:.4f}')
            else:
                reasons.append(f'Regime={regime}')
        elif regime == 'bear':
            reasons.append(f'Regime={regime}')
        components['regime'] = {'score': regime_score, 'value': regime}
        vix = sentiment.get('vix', 20)
        options_skew = sentiment.get('options_skew', 0.0)
        ois = sentiment.get('ois', 0.5)
        if vix > 45.0 or options_skew > 3.5 or ois > 2.0:
            target = 0.0
            reasons.append(f'Boundary Box Exceeded (VIX={vix:.1f}, Skew={options_skew:.1f}, OIS={ois:.1f}) -> 0% Hard Stop')
            components['vol_surface'] = {'score': 0.0, 'vix': vix, 'skew': options_skew, 'ois': ois}
        else:
            k_vix, x0_vix = (0.2, 30.0)
            vix_score = 1.0 / (1.0 + np.exp(k_vix * (vix - x0_vix)))
            k_skew, x0_skew = (1.5, 1.5)
            skew_score = 1.0 / (1.0 + np.exp(k_skew * (options_skew - x0_skew)))
            k_ois, x0_ois = (4.0, 1.2)
            ois_score = 1.0 / (1.0 + np.exp(k_ois * (ois - x0_ois)))
            raw_multiplier = min(vix_score, skew_score, ois_score)
            target = max(0.1, min(1.0, raw_multiplier))
            reasons.append(f'Vol-Surface Melting (VIX={vix_score:.2f}, Skew={skew_score:.2f}, OIS={ois_score:.2f}) -> {target:.2f}')
            components['vol_surface'] = {'score': target, 'vix': vix, 'skew': options_skew, 'ois': ois}
        target = round(max(0, min(1, target * regime_score)), 3)
        intraday_adj = 1.0
        if self._intraday:
            try:
                intraday = self._intraday.detect()
                intraday_adj = intraday.get('exposure_adjustment', 1.0)
                if intraday_adj < 1.0:
                    reasons.append(f'Intraday={intraday.get('regime', '?')}')
                    components['intraday'] = {'score': intraday_adj, 'value': intraday.get('regime', 'normal')}
            except Exception as e:
                logger.critical(f'  ExposureOrchestrator: IntradayRegimeDetector 실패 (bitrading 무시): {e}', exc_info=True)
        sigma_adj = 1.0
        try:
            from src.risk.realtime_var import RealtimeVaR
            _var_calc = RealtimeVaR()
            _positions = _var_calc._load_positions()
            if _positions and len(_positions) > 0:
                _returns = _var_calc._load_returns(list(_positions.keys()), _cfg.get('risk.sigma_lookback', 60) if _cfg else 60)
                if _returns is not None and _returns.shape[1] > 0:
                    _weights = np.array([_positions[t].get('weight', 1.0 / len(_positions)) for t in list(_positions.keys())[:_returns.shape[1]]])
                    _weights = _weights / _weights.sum() if _weights.sum() > 0 else _weights
                    _port_rets = _returns @ _weights
                    _ewma_lambda = _cfg.get('risk.ewma_lambda', 0.94) if _cfg else 0.94
                    _ewma_var = _var_calc._ewma_variance(_port_rets, _ewma_lambda)
                    _realized_vol = float(np.sqrt(_ewma_var)) * np.sqrt(252)
                else:
                    _realized_vol = None
            else:
                _realized_vol = None
            if _realized_vol and _realized_vol > 0:
                _sigma_target = _cfg.get('risk.sigma_target_annual', 0.15) if _cfg else 0.15
                _raw_ratio = _sigma_target / _realized_vol
                _sigma_floor = _cfg.get('risk.sigma_scale_floor', 0.4) if _cfg else 0.4
                _sigma_cap = _cfg.get('risk.sigma_scale_cap', 1.3) if _cfg else 1.3
                sigma_adj = max(_sigma_floor, min(_sigma_cap, _raw_ratio))
                if sigma_adj < 0.9:
                    reasons.append(f'σ-target={_sigma_target:.0%}/realized={_realized_vol:.0%}→{sigma_adj:.2f}')
                components['sigma_target'] = {'score': round(sigma_adj, 3), 'realized_vol': round(_realized_vol, 4), 'target_vol': _sigma_target, 'raw_ratio': round(_raw_ratio, 3)}
        except Exception as e:
            logger.critical(f'  σ-target 계산 실패: {e}', exc_info=True)
        try:
            from src.streams import _STREAM_NAV_HIST
            _jrm = _compute_joint_risk_matrix(_STREAM_NAV_HIST)
        except ImportError as e:
            _jrm = {'joint_adj': 1.0, 'avg_pairwise_corr': 0.0, 'high_corr_pairs': []}
        _jrm_adj = _jrm.get('joint_adj', 1.0)
        deadlock_mode = _cfg_get('risk.deadlock_resolution_mode', 'linear')
        max_combined = _cfg_get('risk.max_combined_hedge_ratio', 0.8)
        is_v_recovery = locals().get('is_v_recovery', False)
        if is_v_recovery:
            v_penalty = _cfg_get('exposure.v_recovery_conf_adj_penalty', 0.2)
            max_combined = min(max_combined, v_penalty)
            v_min = _cfg_get('exposure.v_recovery_min_exposure', 0.5)
            if target < v_min:
                target = v_min
                reasons.append('V-Recovery Override')
        if deadlock_mode == 'joint_prob':
            p_intraday = 1.0 - min(1.0, intraday_adj)
            p_transition = 1.0 - min(1.0, transition_adj)
            p_sigma = 1.0 - min(1.0, sigma_adj)
            joint_defense = 1.0 - (1.0 - p_intraday) * (1.0 - p_transition) * (1.0 - p_sigma)
            joint_adj = 1.0 - joint_defense
            if transition_adj > 1.0:
                joint_adj *= transition_adj
            floor_adj = 1.0 - max_combined
            final_adj = max(floor_adj, joint_adj * _jrm_adj)
            target = round(max(0, min(_cfg_get('exposure.max_leveraged_exposure', 1.3), target * final_adj)), 3)
            components['joint_risk'] = {'final_adj': final_adj, 'defense': joint_defense, 'jrm_adj': _jrm_adj, 'avg_pairwise_corr': _jrm.get('avg_pairwise_corr', 0.0), 'high_corr_pairs': _jrm.get('high_corr_pairs', [])}
            if final_adj < intraday_adj * transition_adj * sigma_adj:
                reasons.append(f'JointRisk(adj={final_adj:.2f})')
        else:
            combined_adj = intraday_adj * transition_adj * sigma_adj * _jrm_adj
            target = round(max(0, min(_cfg_get('exposure.max_leveraged_exposure', 1.3), target * combined_adj)), 3)
        if data_penalty < 1.0:
            _pre_penalty = target
            target = round(target * data_penalty, 4)
            logger.info(f'  [Phase 62] 데이터 결손 패널티 적용: {_pre_penalty:.4f} × {data_penalty:.2f} = {target:.4f}')
            reasons.append(f'DataDegradation(x{data_penalty:.1f})')
        components['data_penalty'] = data_penalty
        result = {'target_exposure': min(1.0, target), 'target_raw': target, 'sigma_adjustment': round(sigma_adj, 3), 'components': components, 'reason': ' + '.join(reasons) if reasons else 'Normal conditions', 'timestamp': datetime.now().isoformat()}
        try:
            (_RESULTS / 'exposure_orchestrator.json').write_text(json.dumps(result, indent=2, default=str))
        except Exception as e:
            logger.critical(f'  ExposureOrchestrator: 결과 저장 실패 (비치명적): {e}', exc_info=True)
        logger.info(f'  Exposure: {min(1.0, target):.0%} ({result['reason']})')
        return result

    def _compute_flash_crash_gate(self, default_score: float, sentiment: Dict, cfg_get, is_v_recovery: bool=False) -> float:
        """Flash Crash 시 거시 지표 기반 동적 포지션 스케일 계산.

        ★ 핵심: 전역 캐시(_cfg)가 아닌 호출 시점마다 DynamicConfig()를 신규 생성.
           → dynamic_overrides.json 변경이 다음 calculate_exposure() 호출에 즉시 반영.
           → 킬스위치, 가중치, 임계값 모두 런타임 실시간 조정 가능.
        """
        try:
            from config.dynamic_config import DynamicConfig as _FreshCfg
            _fcfg = _FreshCfg()
            _get = lambda key, default=None: _fcfg.get(key, default)
        except Exception:
            _get = cfg_get
        if not _get('flash_crash_gate.enabled', True):
            return default_score
        crash_type = sentiment.get('crash_type', 'unknown')
        allowed_types = _get('flash_crash_gate.crash_type_filter', ['flash_crash'])
        if crash_type not in allowed_types:
            return default_score
        cross_stress = float(sentiment.get('cross_asset_stress', 1.0))
        vkospi = float(sentiment.get('vkospi', 99.0))
        s3_conf = float(sentiment.get('s3_avg_confidence', 0.0))
        gate_open = is_v_recovery and cross_stress < float(_get('flash_crash_gate.stress_gate', 0.3)) and (vkospi < float(_get('flash_crash_gate.vkospi_gate', 23.0))) and (s3_conf >= float(_get('flash_crash_gate.v_recovery_conf_discount', 0.35)))
        if not gate_open:
            if cross_stress < float(_get('flash_crash_gate.stress_gate', 0.3)) and (not is_v_recovery):
                logger.debug(f'  ⚡ [FlashCrashGate] 거시 평온(stress={cross_stress:.3f}) 충족되나 V-Recovery 부재로 게이트 차단.')
            return default_score
        stress_cap = float(_get('flash_crash_gate.stress_normalization_cap', 0.5))
        vkospi_floor = float(_get('flash_crash_gate.vkospi_floor', 10.0))
        vkospi_cap = float(_get('flash_crash_gate.vkospi_cap', 23.0))
        oversold_ref = float(_get('flash_crash_gate.oversold_reference_pct', 5.0))
        oversold_sat = float(_get('flash_crash_gate.oversold_saturation_pct', 15.0))
        conf_floor = float(_get('flash_crash_gate.signal_conf_floor', 0.45))
        ma20_dist = float(sentiment.get('kospi_ma20_dist', 0.0))
        f_stress = max(0.0, 1.0 - cross_stress / max(stress_cap, 1e-09))
        f_vkospi = min(1.0, max(0.0, (vkospi - vkospi_floor) / max(vkospi_cap - vkospi_floor, 1e-09)))
        f_oversold = min(1.0, max(0.0, (-ma20_dist - oversold_ref) / max(oversold_sat - oversold_ref, 1e-09)))
        f_signal = max(0.0, (s3_conf - conf_floor) / max(1.0 - conf_floor, 1e-09))
        w_stress = float(_get('flash_crash_gate.w_stress', 0.35))
        w_vkospi = float(_get('flash_crash_gate.w_vkospi', 0.25))
        w_oversold = float(_get('flash_crash_gate.w_oversold', 0.2))
        w_signal = float(_get('flash_crash_gate.w_signal', 0.2))
        w_total = w_stress + w_vkospi + w_oversold + w_signal
        if w_total <= 0:
            return default_score
        gate_score = w_stress / w_total * f_stress + w_vkospi / w_total * f_vkospi + w_oversold / w_total * f_oversold + w_signal / w_total * f_signal
        scale_floor = float(_get('flash_crash_gate.regime_score_floor', 0.15))
        scale_cap = float(_get('flash_crash_gate.regime_score_cap', 0.45))
        flash_score = scale_floor + gate_score * (scale_cap - scale_floor)
        flash_score = round(min(scale_cap, max(scale_floor, flash_score)), 4)
        logger.info(f'  ⚡ [FlashCrashGate] type={crash_type} | stress={cross_stress:.3f} vkospi={vkospi:.1f} ma20={ma20_dist:.1f}% s3_conf={s3_conf:.3f} | f=({f_stress:.2f},{f_vkospi:.2f},{f_oversold:.2f},{f_signal:.2f}) | gate={gate_score:.3f} → score {default_score:.3f}→{flash_score:.4f}')
        return flash_score

    def _load_sentiment(self) -> Dict:
        """signal_cache에서 감성 데이터 로드.

        signal_cache의 두 가지 형식 지원:
          - flat: {'vix': 16.59, ...}
          - nested: {'VIX': {'value': 16.59}, ...}
        """
        sentiment = {'regime': 'caution', 'vix': 20, 'fear_greed': 50, 'vkospi': 20, 'kospi_ma20_dist': 0}
        try:
            cache = json.loads((_RESULTS / 'signal_cache.json').read_text())
            if 'vix' in cache and (not isinstance(cache['vix'], dict)):
                sentiment['vix'] = float(cache['vix'])
            elif 'VIX' in cache and isinstance(cache['VIX'], dict):
                sentiment['vix'] = float(cache['VIX'].get('value', 20))
            if 'fear_greed' in cache and (not isinstance(cache['fear_greed'], dict)):
                sentiment['fear_greed'] = float(cache['fear_greed'])
            elif 'fng' in cache and (not isinstance(cache['fng'], dict)):
                sentiment['fear_greed'] = float(cache['fng'])
            elif 'FnG' in cache and isinstance(cache['FnG'], dict):
                sentiment['fear_greed'] = float(cache['FnG'].get('value', 50))
            if 'vkospi' in cache and (not isinstance(cache['vkospi'], dict)):
                sentiment['vkospi'] = float(cache['vkospi'])
            elif 'VKOSPI' in cache and isinstance(cache['VKOSPI'], dict):
                sentiment['vkospi'] = float(cache['VKOSPI'].get('value', 20))
            if 'kospi_ma20_dist' in cache:
                sentiment['kospi_ma20_dist'] = float(cache['kospi_ma20_dist'])
            sentiment['ois'] = float(cache.get('ois', 0.5))
            sentiment['options_skew'] = float(cache.get('options_skew', 0.0))
            sentiment['crash_type'] = cache.get('crash_type', 'unknown')
            sentiment['cross_asset_stress'] = float(cache.get('cross_asset_stress', 0.0))
            sentiment['s3_avg_confidence'] = float(cache.get('s3_avg_confidence', 0.0))
        except Exception as e:
            logger.critical(f'  ExposureOrchestrator: signal_cache 로드 실패: {e}', exc_info=True)
        try:
            state_file = _RESULTS / 'pipeline_state.json'
            if state_file.exists():
                state = json.loads(state_file.read_text())
                sentiment['regime'] = state.get('regime', 'caution')
            else:
                cache = json.loads((_RESULTS / 'signal_cache.json').read_text())
                us_regime = cache.get('us_regime', '')
                if us_regime in ('bull', 'caution', 'bear', 'crash'):
                    sentiment['regime'] = us_regime
                elif 'ois' in cache:
                    ois = float(cache['ois'])
                    if ois >= 70:
                        sentiment['regime'] = 'bull'
                    elif ois >= 45:
                        sentiment['regime'] = 'caution'
                    elif ois >= 25:
                        sentiment['regime'] = 'bear'
                    else:
                        sentiment['regime'] = 'crash'
        except Exception as e:
            logger.critical(f'  ExposureOrchestrator: 레징 로드 실패: {e}', exc_info=True)
        return sentiment
    _HEDGE_INSTRUMENTS_DEFAULT = {'1x': {'ticker': '114800', 'name': 'KODEX 인버스', 'beta': -1.0, 'leverage': -1, 'cost_bps': 5}, '2x': {'ticker': '252670', 'name': 'KODEX 200선물인버스2X', 'beta': -2.0, 'leverage': -2, 'cost_bps': 10}}
    HEDGE_INSTRUMENTS = _HEDGE_INSTRUMENTS_DEFAULT
    _ATTACKER_INSTRUMENTS_DEFAULT = {'leverage': {'ticker': '122630', 'name': 'KODEX 레버리지', 'beta': 2.0, 'cost_bps': 10}, 'inverse': {'ticker': '252670', 'name': 'KODEX 200선물인버스2X', 'beta': -2.0, 'cost_bps': 10}, 'vix': {'ticker': '500030', 'name': '신한 S&P500 VIX S/T ETN', 'beta': -1.0, 'cost_bps': 15}}
    ATTACKER_INSTRUMENTS = _ATTACKER_INSTRUMENTS_DEFAULT

    def calculate_hedge_position(self, regime: str=None) -> Dict:
        """S0 양방향 포지션 산출 (Predictive Leverage Attacker / Super Boost).

        ★ S0 Attacker Refactoring (2026-07-17)
        ────────────────────────────────────────────────────────────────
        [Case 1] target_beta > 1.0  → 수퍼부스트: KODEX 레버리지(Beta 2.0) 매수
          buy_amount = total_nav × (target_beta - 1.0) / abs(inst_beta)

        [Case 2] 0.0 ≤ target_beta ≤ 1.0 → 부분 방어: 기존 헤지 로직 유지
          hedge_needed = long_exposure × (portfolio_beta - target_beta)
          position_amount = hedge_needed / abs(inst_beta)

        [Case 3] target_beta < 0.0  → 어태커 넷숏: 롱 전면 방어 + 순매도 구축
          hedge_amount = (long_exposure × portfolio_beta
                          + total_nav × abs(target_beta)) / abs(inst_beta)
          max_hedge_ratio 제한 바이패스 — 필요한 만큼 폭격 허용.

          현금 부족 시 동시 방정식 (Simultaneous Equation):
          설정: 매도 X원어치 현물 → 롱 노출도 감소 → 숏 Y도 재계산 필요.
          β_target × total_nav = (long - X) × portfolio_beta - Y × |inst_beta|
          X + available_cash = Y  (매도 현금이 바로 숏 매수에 쓰임)
          → 연립 방정식 해: X = (hedge_target - available_cash × |inst_beta|
                                   + long × beta_delta)
                               / (1 + |inst_beta| - portfolio_beta × |inst_beta|)
                             Y = X + available_cash

        Args:
            regime: 현재 레짐 (None이면 자동 감지)

        Returns:
            dict with action/ticker/name/amount/target_beta/regime/…
        """
        hedge_enabled = _cfg.get('hedge.enabled', True) if _cfg else True
        if not hedge_enabled:
            return {'action': 'DISABLED', 'reason': 'hedge.enabled=false'}
        if regime is None:
            sentiment = self._load_sentiment()
            regime = sentiment.get('regime', 'caution')
        target_beta = self._compute_dynamic_target_beta(regime)
        long_exposure, current_hedge_amount, current_hedge_ticker = self._read_portfolio_exposure()
        sp = self._load_shadow_portfolio()
        total_nav = float(sp.get('total_nav', sp.get('total_value', long_exposure)))
        if long_exposure <= 0 and target_beta >= 0:
            if current_hedge_amount > 0:
                return self._build_hedge_result('SELL', current_hedge_ticker, 0, 0, target_beta, long_exposure, 0, current_hedge_amount, regime, reason='Long exposure=0 → 헤지 해제')
            return {'action': 'HOLD', 'reason': 'Long exposure=0', 'regime': regime, 'target_beta': target_beta}
        portfolio_beta = 1.0
        try:
            from src.risk.beta_hedge import BetaHedge
            _bh = BetaHedge()
            _sp_path = _RESULTS / 'shadow_portfolio.json'
            if _sp_path.exists():
                try:
                    _sp_raw = json.loads(_sp_path.read_text())
                    _positions = _sp_raw.get('positions', {})
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    _positions = {}
            else:
                _positions = {}
            _beta_result = _bh.compute()
            if _beta_result and _beta_result.get('portfolio_beta') is not None:
                portfolio_beta = float(_beta_result['portfolio_beta'])
        except Exception as _beta_err:
            logger.critical(f'  BetaHedge 계산 실패, β=1.0 가정: {_beta_err}', exc_info=True)
        min_amount = _cfg.get('hedge.min_amount', 500000) if _cfg else 500000
        if target_beta > 1.0:
            inst = dict(self.ATTACKER_INSTRUMENTS.get('leverage', self._ATTACKER_INSTRUMENTS_DEFAULT['leverage']))
            inst['ticker'] = str(_cfg.get('exposure.instrument.leverage.ticker', inst['ticker']) if _cfg else inst['ticker'])
            inst['beta'] = float(_cfg.get('exposure.instrument.leverage.beta', inst['beta']) if _cfg else inst['beta'])
            inst['name'] = str(_cfg.get('exposure.instrument.leverage.name', inst['name']) if _cfg else inst['name'])
            inst_beta_abs = abs(inst['beta'])
            buy_amount = total_nav * (target_beta - 1.0) / inst_beta_abs
            available_cash = float(sp.get('cash', sp.get('s5_cash', total_nav)))
            if buy_amount > available_cash:
                logger.warning(f'  ⚠️ [수퍼부스트] 현금 부족 — 필요 ₩{buy_amount / 1000000.0:.1f}M, 가용 ₩{available_cash / 1000000.0:.1f}M → 현금 한도로 조정')
                buy_amount = available_cash
            diff = buy_amount - current_hedge_amount
            tolerance = _cfg.get('hedge.rebalance_tolerance', 0.1) if _cfg else 0.1
            if current_hedge_amount > 0 and abs(diff) / max(current_hedge_amount, 1) < tolerance:
                action = 'HOLD'
            else:
                action = 'BUY' if current_hedge_amount == 0 else 'ADJUST'
            result = self._build_hedge_result(action, inst['ticker'], buy_amount, diff, target_beta, long_exposure, buy_amount, current_hedge_amount, regime, reason=f'수퍼부스트 β={target_beta:.2f} → {inst['name']} ₩{buy_amount / 1000000.0:.1f}M')
            result['name'] = inst['name']
            result['mode'] = 'super_boost'
            logger.info(f'  🚀 [수퍼부스트] β={target_beta:.2f} → {inst['name']} ₩{buy_amount / 1000000.0:.1f}M ({action})')
            return result
        if target_beta < 0.0:
            inst = dict(self.ATTACKER_INSTRUMENTS.get('inverse', self._ATTACKER_INSTRUMENTS_DEFAULT['inverse']))
            inst['ticker'] = str(_cfg.get('exposure.instrument.inverse.ticker', inst['ticker']) if _cfg else inst['ticker'])
            inst['beta'] = float(_cfg.get('exposure.instrument.inverse.beta', inst['beta']) if _cfg else inst['beta'])
            inst['name'] = str(_cfg.get('exposure.instrument.inverse.name', inst['name']) if _cfg else inst['name'])
            inst_beta_abs = abs(inst['beta'])
            hedge_amount = (long_exposure * portfolio_beta + total_nav * abs(target_beta)) / inst_beta_abs
            logger.info(f'  ⚡ [어태커 넷숏] max_hedge_ratio 바이패스 활성화 — β={target_beta:.2f}, hedge_target=₩{hedge_amount / 1000000.0:.1f}M')
            available_cash = float(sp.get('cash', sp.get('s5_cash', 0.0)))
            if hedge_amount <= available_cash:
                final_short_buy = hedge_amount
                final_equity_sell = 0.0
            else:
                numerator = long_exposure * portfolio_beta - available_cash * inst_beta_abs - target_beta * total_nav
                denominator = portfolio_beta + inst_beta_abs
                if denominator > 1e-09:
                    X = numerator / denominator
                else:
                    X = hedge_amount - available_cash
                X = max(0.0, X)
                X = min(X, long_exposure)
                Y = X + available_cash
                final_equity_sell = X
                final_short_buy = Y
                logger.warning(f'  🔢 [동시 방정식] 현금 부족 ₩{available_cash / 1000000.0:.1f}M < 필요 ₩{hedge_amount / 1000000.0:.1f}M → 현물 매도 X=₩{final_equity_sell / 1000000.0:.1f}M, 숏 매수 Y=₩{final_short_buy / 1000000.0:.1f}M 재계산 완료')
                hedge_amount = final_short_buy
            diff = hedge_amount - current_hedge_amount
            tolerance = _cfg.get('hedge.rebalance_tolerance', 0.1) if _cfg else 0.1
            if current_hedge_amount > 0 and abs(diff) / max(current_hedge_amount, 1) < tolerance:
                action = 'HOLD'
            else:
                action = 'BUY' if current_hedge_amount == 0 else 'ADJUST'
            result = self._build_hedge_result(action, inst['ticker'], hedge_amount, diff, target_beta, long_exposure, hedge_amount, current_hedge_amount, regime, reason=f'어태커 넷숏 β={target_beta:.2f} → {inst['name']} ₩{hedge_amount / 1000000.0:.1f}M')
            result['name'] = inst['name']
            result['mode'] = 'attacker_net_short'
            result['equity_sell_amount'] = final_equity_sell if hedge_amount < available_cash + long_exposure else 0.0
            logger.warning(f'  🚨 [어태커 넷숏] β={target_beta:.2f} → {inst['name']} ₩{hedge_amount / 1000000.0:.1f}M ({action})')
            return result
        hedge_needed = long_exposure * max(0.0, portfolio_beta - target_beta)
        max_ratio = _cfg.get('hedge.max_hedge_ratio', 0.5) if _cfg else 0.5
        max_hedge = long_exposure * max_ratio
        hedge_needed = min(hedge_needed, max_hedge)
        if hedge_needed < min_amount:
            if current_hedge_amount > 0:
                return self._build_hedge_result('SELL', current_hedge_ticker, 0, 0, target_beta, long_exposure, hedge_needed, current_hedge_amount, regime, reason=f'헤지 필요액 ₩{hedge_needed:,.0f} < 최소 ₩{min_amount:,.0f}')
            return {'action': 'HOLD', 'reason': '필요 헤지 < 최소금액', 'regime': regime, 'target_beta': target_beta, 'hedge_needed': hedge_needed}
        use_2x_regimes = _cfg.get('hedge.use_2x_regime', ['bear', 'crash']) if _cfg else ['bear', 'crash']
        if regime in use_2x_regimes:
            inst = self.HEDGE_INSTRUMENTS['2x']
        else:
            inst = self.HEDGE_INSTRUMENTS['1x']
        ticker = inst['ticker']
        inst_beta_abs = abs(inst.get('beta', inst.get('leverage', 1)))
        position_amount = hedge_needed / inst_beta_abs
        diff = position_amount - current_hedge_amount
        tolerance = _cfg.get('hedge.rebalance_tolerance', 0.1) if _cfg else 0.1
        if current_hedge_amount > 0 and abs(diff) / max(current_hedge_amount, 1) < tolerance:
            return self._build_hedge_result('HOLD', ticker, position_amount, 0, target_beta, long_exposure, hedge_needed, current_hedge_amount, regime, reason=f'변동 {abs(diff) / max(current_hedge_amount, 1) * 100:.0f}% < 허용 {tolerance * 100:.0f}%')
        if current_hedge_amount == 0:
            action = 'BUY'
        elif diff > 0:
            action = 'ADJUST'
        else:
            action = 'ADJUST'
        result = self._build_hedge_result(action, ticker, position_amount, diff, target_beta, long_exposure, hedge_needed, current_hedge_amount, regime)
        result['mode'] = 'partial_hedge'
        logger.info(f'  🛡️ β헤지: long=₩{long_exposure / 1000000.0:.0f}M → β={target_beta:.1f} → {inst['name']} ₩{position_amount / 1000000.0:.1f}M ({action})')
        return result

    def _read_portfolio_exposure(self):
        """shadow_portfolio.json에서 S2~S4 long + H: 헤지 현황 읽기."""
        long_exposure = 0
        current_hedge_amount = 0
        current_hedge_ticker = '114800'
        try:
            pf_path = _RESULTS / 'shadow_portfolio.json'
            if pf_path.exists():
                pf = json.loads(pf_path.read_text())
                positions = pf.get('positions', {})
                for pk, pos in positions.items():
                    sid = pk.split(':')[0] if ':' in pk else ''
                    mv = pos.get('market_value', pos.get('amount', 0))
                    if sid in ('S2', 'S3', 'S4'):
                        long_exposure += mv
                    elif sid == 'H':
                        current_hedge_amount += mv
                        current_hedge_ticker = pk.split(':')[1] if ':' in pk else '114800'
        except Exception as e:
            logger.critical(f'포트폴리오 읽기 실패: {e}', exc_info=True)
        return (long_exposure, current_hedge_amount, current_hedge_ticker)

    def _compute_dynamic_target_beta(self, regime: str) -> float:
        """포트폴리오 상태 기반 동적 β 목표 계산.

        ★ S0 Predictive Leverage Attacker (2026-07-17)
        ────────────────────────────────────────────────
        [Phase A] 웩더독 어태커 (Wag-the-Dog Strike):
                  gex_crash_warning 또는 wag_the_dog_active 시그널 감지 시
                  모든 조정값을 덮어쓰고 target_beta를 -0.5 ~ -1.0으로 꽂음.
        [Phase B] VIX Trailing Stop:
                  어태커 모드 중 vix_momentum 음수 전환(공포 완화) 감지 시
                  스퀴즈 방지를 위해 target_beta를 0.0 이상으로 강제 복귀.
        [Phase C] 골디락스 수퍼부스트:
                  bull 레짐 + cross_asset_stress < 0.2일 때 1.0 → 최대 2.5 스케일업.

        기존 입력 요소 (훼손 없음):
        1. 레짐 기본값 (DynamicConfig)
        2. VIX 수준 → 고VIX면 β 축소
        3. 포트폴리오 현재 드로다운 → 드로다운 깊으면 β 축소
        4. 포트폴리오 변동성 → 고변동이면 β 축소
        """
        cfg = _cfg
        default_base = {'bull': float(cfg.get('hedge.base_beta.bull', 0.75)) if cfg else 0.75, 'caution': float(cfg.get('hedge.base_beta.caution', 0.55)) if cfg else 0.55, 'bear': float(cfg.get('hedge.base_beta.bear', 0.35)) if cfg else 0.35, 'crash': float(cfg.get('hedge.base_beta.crash', 0.15)) if cfg else 0.15}
        base = cfg.get(f'hedge.base_beta.{regime}', default_base.get(regime, 0.5)) if cfg else default_base.get(regime, 0.5)
        signal_cache = self._load_signal_cache()
        vix = signal_cache.get('vix', 18.0)
        vix_neutral = cfg.get('hedge.vix_neutral', 18.0) if cfg else 18.0
        vix_scale = cfg.get('hedge.vix_beta_scale', 0.005) if cfg else 0.005
        vix_adj = -(vix - vix_neutral) * vix_scale
        sp = self._load_shadow_portfolio()
        current_dd = self._get_current_drawdown(sp)
        dd_threshold = cfg.get('hedge.dd_beta_threshold', -3.0) if cfg else -3.0
        dd_scale = cfg.get('hedge.dd_beta_scale', 0.05) if cfg else 0.05
        dd_adj = 0.0
        if current_dd < dd_threshold:
            dd_adj = (current_dd - dd_threshold) * dd_scale
        port_vol = self._get_portfolio_volatility(sp)
        vol_neutral = cfg.get('hedge.vol_neutral', 0.015) if cfg else 0.015
        vol_scale = cfg.get('hedge.vol_beta_scale', 2.0) if cfg else 2.0
        vol_adj = -(port_vol - vol_neutral) * vol_scale if port_vol > vol_neutral else 0.0
        target = base + vix_adj + dd_adj + vol_adj
        gex_crash = signal_cache.get('gex_crash_warning', False)
        wag_the_dog = signal_cache.get('wag_the_dog_active', False)
        attacker_mode = False
        if gex_crash or wag_the_dog:
            gex_severity = float(signal_cache.get('gex_crash_severity', 0.5))
            wtd_severity = float(signal_cache.get('wag_the_dog_severity', 0.5))
            combined_severity = max(gex_severity if gex_crash else 0.0, wtd_severity if wag_the_dog else 0.0)
            min_beta = float(cfg.get('exposure.min_target_beta', -1.0) if cfg else -1.0)
            attacker_beta = -0.5 + (min_beta + 0.5) * combined_severity
            attacker_beta = round(max(min_beta, min(-0.5, attacker_beta)), 3)
            logger.warning(f'  ⚡ [S0 어태커 발동] gex_crash={gex_crash} | wag_the_dog={wag_the_dog} | severity={combined_severity:.2f} → target_beta={attacker_beta:.3f} (넷숏 구축 시작)')
            target = attacker_beta
            attacker_mode = True
        if attacker_mode:
            vix_trailing_enabled = cfg.get('exposure.vix_trailing_stop', True) if cfg else True
            vix_momentum = float(signal_cache.get('vix_momentum', 0.0))
            if vix_trailing_enabled and vix_momentum < 0:
                prior_target = target
                target = max(0.0, target)
                logger.warning(f'  🛑 [VIX Trailing Stop 발동] vix_momentum={vix_momentum:.4f} < 0 (공포 완화) → target_beta {prior_target:.3f} → {target:.3f} (넷숏 강제 청산)')
                attacker_mode = False
        if not attacker_mode and regime == 'bull':
            stress_threshold = float(cfg.get('exposure.super_boost_stress_threshold', 0.2) if cfg else 0.2)
            cross_stress = float(signal_cache.get('cross_asset_stress', 1.0))
            if cross_stress < stress_threshold:
                max_lev = float(cfg.get('exposure.max_leveraged_exposure', 2.5) if cfg else 2.5)
                stress_ratio = 1.0 - cross_stress / stress_threshold
                boost_beta = 1.0 + (max_lev - 1.0) * stress_ratio
                boost_beta = round(min(max_lev, boost_beta), 3)
                logger.info(f'  🚀 [골디락스 수퍼부스트] cross_asset_stress={cross_stress:.3f} < {stress_threshold} | stress_ratio={stress_ratio:.3f} → target_beta {target:.3f} → {boost_beta:.3f} (레버리지 ETF 매수)')
                target = boost_beta
        if attacker_mode or target < 0 or target > 1.0:
            floor = float(cfg.get('exposure.min_target_beta', -1.0) if cfg else -1.0)
            ceiling = float(cfg.get('exposure.max_leveraged_exposure', 2.5) if cfg else 2.5)
        else:
            floor = cfg.get('hedge.beta_floor', 0.1) if cfg else 0.1
            ceiling = cfg.get('hedge.beta_ceiling', 0.9) if cfg else 0.9
        result = round(max(floor, min(ceiling, target)), 3)
        try:
            _s0_brake = float(cfg.get('risk.s0_drawdown_brake', 0.1) if cfg else 0.1)
            _s0_sp = self._load_shadow_portfolio()
            _s0_dd = self._get_current_drawdown(_s0_sp)
            if _s0_dd != 0.0 and abs(_s0_dd) >= _s0_brake * 100:
                logger.error(f'  🛑 [S0 Drawdown Brake] S0 MDD={_s0_dd:.1f}% >= {_s0_brake * 100:.0f}% — S0 beta 강제 중립화 (0.0)', exc_info=False)
                result = 0.0
        except Exception as _brake_e:
            logger.error(f'  [S0 Brake] 드로다운 계산 실패 (Brake 미적용): {_brake_e}', exc_info=True)
        logger.debug(f'  β-dynamic: base={base:.3f} vix_adj={vix_adj:+.3f} dd_adj={dd_adj:+.3f} vol_adj={vol_adj:+.3f} attacker={attacker_mode} → target_β={result:.3f}')
        return result

    def _load_signal_cache(self) -> Dict:
        """results/signal_cache.json 로드.

        Returns:
            signal_cache dict (vix, fng 등). 실패 시 빈 dict.
        """
        try:
            path = _RESULTS / 'signal_cache.json'
            if path.exists():
                cache = json.loads(path.read_text())
                result: Dict = {}
                if 'vix' in cache and (not isinstance(cache['vix'], dict)):
                    result['vix'] = float(cache['vix'])
                elif 'VIX' in cache and isinstance(cache['VIX'], dict):
                    result['vix'] = float(cache['VIX'].get('value', 18.0))
                else:
                    result['vix'] = 18.0
                return result
        except Exception as e:
            logger.critical(f'signal_cache 로드 실패: {e}', exc_info=True)
        return {'vix': 18.0}

    def _load_shadow_portfolio(self) -> Dict:
        """results/shadow_portfolio.json 로드.

        Returns:
            shadow_portfolio dict. 실패 시 빈 dict.
        """
        try:
            path = _RESULTS / 'shadow_portfolio.json'
            if path.exists():
                return json.loads(path.read_text())
        except Exception as e:
            logger.critical(f'shadow_portfolio 로드 실패: {e}', exc_info=True)
        return {}

    def _get_current_drawdown(self, sp: Dict) -> float:
        """daily_snapshots에서 현재 드로다운(%) 계산.

        Args:
            sp: shadow_portfolio dict

        Returns:
            현재 드로다운 (%). 예: -5.0 (= -5%).
            데이터 부족 시 0.0.
        """
        try:
            snapshots = sp.get('daily_snapshots', [])
            if not snapshots:
                return 0.0
            navs = []
            for snap in snapshots:
                nav = snap.get('nav', snap.get('total_value', 0))
                if nav and nav > 0:
                    navs.append(float(nav))
            if len(navs) < 2:
                return 0.0
            current_nav = navs[-1]
            peak_nav = max(navs)
            if peak_nav <= 0:
                return 0.0
            dd_pct = (current_nav - peak_nav) / peak_nav * 100.0
            return round(dd_pct, 2)
        except Exception as e:
            logger.critical(f'드로다운 계산 실패: {e}', exc_info=True)
            return 0.0

    def _get_portfolio_volatility(self, sp: Dict) -> float:
        """최근 N일 daily_return std (일변동성) 계산.

        Args:
            sp: shadow_portfolio dict

        Returns:
            일별 수익률 표준편차. 데이터 부족 시 0.0.
        """
        try:
            lookback = _cfg.get('hedge.vol_lookback_days', 20) if _cfg else 20
            snapshots = sp.get('daily_snapshots', [])
            if len(snapshots) < 3:
                return 0.0
            navs = []
            for snap in snapshots:
                nav = snap.get('nav', snap.get('total_value', 0))
                if nav and nav > 0:
                    navs.append(float(nav))
            if len(navs) < 3:
                return 0.0
            recent_navs = navs[-(lookback + 1):] if len(navs) > lookback else navs
            returns = []
            for i in range(1, len(recent_navs)):
                if recent_navs[i - 1] > 0:
                    r = (recent_navs[i] - recent_navs[i - 1]) / recent_navs[i - 1]
                    returns.append(r)
            if len(returns) < 2:
                return 0.0
            return float(np.std(returns, ddof=1))
        except Exception as e:
            logger.critical(f'포트폴리오 변동성 계산 실패: {e}', exc_info=True)
            return 0.0

    @staticmethod
    def _build_hedge_result(action, ticker, target_amount, diff, target_beta, long_exposure, hedge_needed, current_hedge, regime, reason=None):
        """헤지 결과 Dict 생성."""
        return {'action': action, 'ticker': ticker, 'stream_id': 'H', 'target_amount': round(target_amount), 'adjustment': round(diff), 'target_beta': target_beta, 'long_exposure': round(long_exposure), 'hedge_needed': round(hedge_needed), 'current_hedge': round(current_hedge), 'regime': regime, 'reason': reason or f'β={target_beta:.1f}, {action}', 'timestamp': datetime.now().isoformat()}

    def apply_bear_score(self, bear_score: float, regime: str='bear', vix: float=18.0) -> dict:
        """S2가 넘긴 Bear Score를 받아 인버스/곱버스 동적 레버리지 스위칭.

        [Phase 11: Dynamic Balance] Phase 11-B: Confidence-driven Beta Hedging

        인버스 ETF 선택 기준 (하드코딩 없음, VIX & confidence 수학적 연동):
          - bear_score >= extreme_bear_threshold(0.80) → 252670 (곱버스 2x)
          - 0.60 <= bear_score < 0.80                 → 114800 (인버스 1x)
          - bear_score < 0.60                         → 헷지 불필요 (HOLD)

        size_pct 계산 (수식):
          base_size = bear_score * max_hedge_ratio (최대 30%)
          vix_amp   = clip(vix / vix_neutral, 1.0, 2.0)  (VIX 상승 시 증폭)
          final_size = clip(base_size * vix_amp, 0, max_hedge_ratio)

        Args:
            bear_score: 0~1 하락 확신 스코어 (S2 산출)
            regime:     현재 레짐
            vix:        현재 VIX

        Returns:
            {
                'action':         'BUY'/'HOLD',
                'ticker':         '252670' / '114800' / '',
                'name':           str,
                'size_pct':       float,
                'leverage_label': '2x' / '1x' / 'none',
                'bear_score':     float,
                'vix_amp':        float,
                'reason':         str,
            }
        """
        cfg_get = (lambda k, d=None: _cfg.get(k, d)) if _cfg else lambda k, d=None: d
        extreme_threshold = cfg_get('hedge.bear_score.extreme_threshold', 0.8)
        moderate_threshold = cfg_get('hedge.bear_score.moderate_threshold', 0.6)
        max_hedge_ratio = cfg_get('hedge.bear_score.max_size_pct', 0.3)
        vix_neutral = cfg_get('s2.vix_neutral_level', 18.0) or 18.0
        vix_amp = max(1.0, min(cfg_get('hedge.bear_score.vix_amp_cap', 2.0) or 2.0, vix / max(vix_neutral, 1.0)))
        base_size = bear_score * max_hedge_ratio
        final_size = min(max_hedge_ratio, base_size * vix_amp)
        final_size = round(final_size, 4)
        if bear_score >= extreme_threshold:
            inst = self.HEDGE_INSTRUMENTS['2x']
            leverage_label = '2x'
            reason = f'[Phase 11] Extreme Bear: score={bear_score:.3f}>={extreme_threshold} -> 곱버스(2x) size={final_size:.2%}(VIX={vix:.1f} amp={vix_amp:.2f})'
        elif bear_score >= moderate_threshold:
            inst = self.HEDGE_INSTRUMENTS['1x']
            leverage_label = '1x'
            reason = f'[Phase 11] Moderate Bear: score={bear_score:.3f}>={moderate_threshold} -> 인버스(1x) size={final_size:.2%}(VIX={vix:.1f} amp={vix_amp:.2f})'
        else:
            logger.debug(f'  [Phase 11] Bear Score={bear_score:.3f} < {moderate_threshold} -> HOLD')
            return {'action': 'HOLD', 'ticker': '', 'name': 'no hedge', 'size_pct': 0.0, 'leverage_label': 'none', 'bear_score': round(bear_score, 4), 'vix_amp': round(vix_amp, 4), 'reason': f'bear_score={bear_score:.3f} < threshold={moderate_threshold}'}
        ticker = inst['ticker']
        name = inst['name']
        result = {'action': 'BUY' if final_size > 0 else 'HOLD', 'ticker': ticker, 'name': f'{name} (Bear Score 헷지)', 'stream_id': 'H', 'size_pct': final_size, 'leverage_label': leverage_label, 'bear_score': round(bear_score, 4), 'vix_amp': round(vix_amp, 4), 'regime': regime, 'reason': reason, 'timestamp': datetime.now().isoformat()}
        logger.info(f'  ⚔️ [Phase 11: Dynamic Balance] Bear Score 헷지 발동: score={bear_score:.3f} -> {name}({leverage_label}) {final_size:.1%} (VIX amp={vix_amp:.2f})')
        try:
            _bear_score_path = _RESULTS / 'bear_score_hedge.json'
            _bear_score_path.write_text(json.dumps(result, indent=2, default=str))
        except Exception as _e0:
            logger.critical(f'  [exposure_orchestrator] 오케스트레이터 상태 저장: {_e0}', exc_info=True)
        return result
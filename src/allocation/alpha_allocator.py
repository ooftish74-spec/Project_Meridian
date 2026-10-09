"""
AlphaAllocator — 4-Stream 리스크 패리티 기반 동적 배분
=======================================================

4개 스트림(S1~S4) 간 자본 배분을 동적으로 결정합니다.

배분 공식 (리스크 패리티 하이브리드):
  1. 스트림별 변동성(σ) 계산
  2. 리스크 패리티 가중치: inv_vol[i] = 1 / σ[i]
  3. Sharpe² 보정: adj_w[i] = inv_vol[i] × (1 + α × Sharpe[i]²)
  4. 상관 패널티: penalty[i] = Σ(|corr[i][j]| × w[j]) × rate
  5. 정규화

설계 원칙:
  - 리스크 패리티: 각 스트림이 동일한 리스크 기여
  - Sharpe 보정: 성과 좋은 스트림에 약간 더 배분
  - 상관 패널티: 상관 높은 스트림은 감산
  - 레짐별 base weight 조절: 레짐에 따라 S1(공격) vs S4(방어) 비중 시프트

Usage:
    from src.allocation.alpha_allocator import AlphaAllocator
    alloc = AlphaAllocator()
    weights = alloc.allocate(stream_metrics, regime='bull')
"""
import json
import logging
import math
import numpy as np
from pathlib import Path as _Path
from src.utils.file_ops import atomic_write_json

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
from src.data.market_context import MarketContextManager
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
market_ctx = MarketContextManager()

class AlphaAllocator:
    """4-Stream 리스크 패리티 하이브리드 배분기.

    모든 파라미터는 DynamicConfig에서 동적 로드.
    SelfLearning이 IC 기반으로 base_weight를 자동 갱신.
    """
    STREAMS = None

    def _default_regime_weights(self) -> Dict[str, Dict[str, float]]:
        """레짐별 기본 배분 (DynamicConfig 동적 로드).
        S4(Advisory) 예산을 제외하고 S1, S2, S3, S5로 재분배.
        """
        active_streams = self.STREAMS or ['S0', 'S1', 'S2', 'S3', 'S5', 'S10', 'S11_HIGHBETA_SNIPER', 'S12_DERIVATIVE_SQUEEZE', 'S13_JAPAN_ALPHA', 'S14_PAIR_ALPHA']
        regimes = ['bull', 'caution', 'bear', 'crash']
        defaults = {
            'bull': {'S0': 0.20, 'S1': 0.25, 'S2': 0.20, 'S3': 0.10, 'S5': 0.00, 'S10': 0.05, 'S11_HIGHBETA_SNIPER': 0.10, 'S12_DERIVATIVE_SQUEEZE': 0.05, 'S13_JAPAN_ALPHA': 0.00, 'S14_PAIR_ALPHA': 0.05},
            'caution': {'S0': 0.15, 'S1': 0.15, 'S2': 0.15, 'S3': 0.15, 'S5': 0.00, 'S10': 0.05, 'S11_HIGHBETA_SNIPER': 0.10, 'S12_DERIVATIVE_SQUEEZE': 0.05, 'S13_JAPAN_ALPHA': 0.05, 'S14_PAIR_ALPHA': 0.15},
            'bear': {'S0': 0.20, 'S1': 0.15, 'S2': 0.10, 'S3': 0.05, 'S5': 0.00, 'S10': 0.05, 'S11_HIGHBETA_SNIPER': 0.10, 'S12_DERIVATIVE_SQUEEZE': 0.05, 'S13_JAPAN_ALPHA': 0.05, 'S14_PAIR_ALPHA': 0.15},
            'crash': {'S0': 0.30, 'S1': 0.20, 'S2': 0.10, 'S3': 0.00, 'S5': 0.00, 'S10': 0.05, 'S11_HIGHBETA_SNIPER': 0.10, 'S12_DERIVATIVE_SQUEEZE': 0.05, 'S13_JAPAN_ALPHA': 0.00, 'S14_PAIR_ALPHA': 0.10}
        }
        result = {}
        for regime in regimes:
            d = defaults[regime]
            result[regime] = {sid: cfg.get(f'allocator.base_weight.{regime}.{sid}', d.get(sid, 0.0)) for sid in active_streams}
        return result

    def __init__(self):
        self._last_weights: Dict[str, float] = {}
        self._allocation_history: List[Dict] = []
        _cfg_streams = cfg.get('allocator.active_streams', None)
        self.STREAMS = _cfg_streams or ['S0', 'S1', 'S2', 'S3', 'S5', 'S10', 'S11_HIGHBETA_SNIPER', 'S12_DERIVATIVE_SQUEEZE', 'S13_JAPAN_ALPHA', 'S14_PAIR_ALPHA']
        self.us_night_cap = cfg.get('allocator.us_night_max_cap_pct', 0.50)  # Phase 8: 24h Recycling US Cap (50%)

    def compute_entry_score(self, signal: Dict, stream_id: str='unknown') -> Dict:
        """진입 필터 점수 계산 (병렬 가중합 구조).

        Hard Stop (AND 조건, 절대 차단):
          - KillSwitch 발동 중
          - DrawdownGuard Stage 5+

        Soft Score (가중합):
          - IC Score: 알파 신호의 OOS IC 품질
          - Confidence Score: 신호 확신도
          - Regime Score: 현재 레짐 적합성
          - Expected Return Score: 기대수익률 품질

        최종 entry_score ≥ threshold 시 진입 허가.
        포지션 크기 = base_size × position_scale (점수에 비례)

        Args:
            signal: 스트림에서 생성된 개별 시그널 dict
            stream_id: 스트림 식별자

        Returns:
            {
                'entry_allowed': bool,
                'entry_score': float,      # 0.0~1.0
                'hard_stop': bool,
                'hard_stop_reason': str,
                'soft_scores': dict,       # 각 소프트 필터 점수
                'position_scale': float,   # 포지션 크기 스케일 (0~1)
            }
        """
        result = {'entry_allowed': False, 'entry_score': 0.0, 'hard_stop': False, 'hard_stop_reason': '', 'soft_scores': {}, 'position_scale': 0.0}
        try:
            from src.risk.kill_switch import KillSwitch
            ks = KillSwitch()
            if getattr(ks, '_prev_triggered', False):
                result['hard_stop'] = True
                result['hard_stop_reason'] = 'KillSwitch 발동 중'
                logger.debug(f"  [EntryScore:{stream_id}] Hard Stop: KillSwitch")
                return result
        except Exception as _ks_e:
            logger.critical(f'  [EntryScore] KillSwitch 로드 실패: {_ks_e}', exc_info=True)
        try:
            from src.risk.drawdown_guard import DrawdownGuard
            import json as _j
            _PROJECT_ROOT = _Path(__file__).resolve().parent.parent.parent
            dg = DrawdownGuard()
            _sp = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if _sp.exists():
                _sp_data = _j.loads(_sp.read_text())
                _nav = _sp_data.get('total_nav', cfg.get('portfolio.initial_capital', 0))
                dd_result = dg.check(_nav)
                dd_stage = dd_result.get('dd_stage', 0)
                hard_stop_stage = int(cfg.get('filter.hard_stop_dd_stage', 5))
                if dd_stage >= hard_stop_stage:
                    result['hard_stop'] = True
                    result['hard_stop_reason'] = f"DrawdownGuard Stage {dd_stage}"
                    logger.warning(f"  [EntryScore:{stream_id}] Hard Stop: DD Stage {dd_stage}")
                    return result
        except Exception as _dg_e:
            logger.critical(f'  [EntryScore] DrawdownGuard 로드 실패: {_dg_e}', exc_info=True)
        ic_raw = float(signal.get('oos_ic', signal.get('ic', 0.0)))
        ic_min = float(cfg.get('filter.ic_score_min', 0.02))
        ic_good = float(cfg.get('filter.ic_score_good', 0.1))
        ic_score = min(1.0, max(0.0, (ic_raw - ic_min) / max(ic_good - ic_min, 1e-09)))
        confidence = float(signal.get('confidence', signal.get('predict_proba', 0.5)))
        conf_min = float(cfg.get('filter.conf_min', 0.5))
        conf_good = float(cfg.get('filter.conf_good', 0.75))
        conf_score = min(1.0, max(0.0, (confidence - conf_min) / max(conf_good - conf_min, 1e-09)))
        regime = signal.get('regime', 'caution')
        strategy = signal.get('strategy', '')
        regime_score = float(cfg.get(f'filter.regime_score.{regime}.{strategy}', cfg.get(f'filter.regime_score.{regime}', 0.5)))
        exp_ret = float(signal.get('expected_return', 0.0))
        exp_ret_min = float(cfg.get('filter.exp_ret_min', 0.005))
        exp_ret_good = float(cfg.get('filter.exp_ret_good', 0.03))
        exp_ret_score = min(1.0, max(0.0, (exp_ret - exp_ret_min) / max(exp_ret_good - exp_ret_min, 1e-09)))
        soft_scores = {'ic_score': round(ic_score, 4), 'conf_score': round(conf_score, 4), 'regime_score': round(regime_score, 4), 'exp_ret_score': round(exp_ret_score, 4)}
        result['soft_scores'] = soft_scores
        w_ic = float(cfg.get('filter.w_ic', 0.3))
        w_conf = float(cfg.get('filter.w_conf', 0.35))
        w_regime = float(cfg.get('filter.w_regime', 0.2))
        w_exp_ret = float(cfg.get('filter.w_exp_ret', 0.15))
        total_w = w_ic + w_conf + w_regime + w_exp_ret
        if total_w > 1e-09:
            w_ic /= total_w
            w_conf /= total_w
            w_regime /= total_w
            w_exp_ret /= total_w
        entry_score = w_ic * ic_score + w_conf * conf_score + w_regime * regime_score + w_exp_ret * exp_ret_score
        result['entry_score'] = round(entry_score, 4)
        threshold = float(cfg.get('filter.entry_threshold', 0.4))
        result['entry_allowed'] = entry_score >= threshold
        if result['entry_allowed']:
            scale_max = float(cfg.get('filter.position_scale_max', 1.0))
            position_scale = min(scale_max, (entry_score - threshold) / max(1.0 - threshold, 1e-09))
            result['position_scale'] = round(position_scale, 4)
        logger.debug(f"  [EntryScore:{stream_id}] score={entry_score:.3f} ({('✅ 허가' if result['entry_allowed'] else '❌ 거부')}) (IC={ic_score:.3f}, conf={conf_score:.3f}, regime={regime_score:.3f}, exp_ret={exp_ret_score:.3f})")
        return result

    def allocate(self, stream_metrics: Dict, regime: str='caution', market_data: Dict=None, s0_sigs: List[Dict]=None) -> Dict[str, float]:
        """스트림 간 배분 비율 결정 (리스크 패리티 하이브리드).

        Args:
            stream_metrics: 스트림별 성과 지표
                {
                    'S1': {'sharpe': 1.2, 'daily_returns': [...], ...},
                    'S2': {'sharpe': 0.8, 'daily_returns': [...], ...},
                    'S3': {'sharpe': 1.5, 'daily_returns': [...], ...},
                }
            regime: 현재 레짐

        Returns:
            배분 비율 {'S1': 0.20, 'S2': 0.50, 'S3': 0.30}
        """
        # ★ [SSoT Architecture] 경쟁적 EV(Expected Value) 100% 몰아주기 자산 배분 엔진
        ev_scores = {}
        for sid in self.STREAMS:
            metrics = (stream_metrics or {}).get(sid, {})
            if not metrics:
                if sid == 'S10':
                    metrics = (stream_metrics or {}).get('S10_MEGA_TREND', {})
                elif sid == 'S10_MEGA_TREND':
                    metrics = (stream_metrics or {}).get('S10', {})
            import math
            p_win = float(metrics.get('win_rate', metrics.get('p_win', 0.50)) or 0.50)
            reward = float(metrics.get('avg_win', metrics.get('expected_return', 0.02)) or 0.02)
            risk = float(metrics.get('avg_loss', metrics.get('volatility', 0.01)) or 0.01)
            if math.isnan(p_win): p_win = 0.50
            if math.isnan(reward): reward = 0.02
            if math.isnan(risk) or risk <= 0: risk = 0.01
            # Expected Value: EV = p * Reward - (1 - p) * Risk
            ev = p_win * reward - (1.0 - p_win) * risk
            if math.isnan(ev): ev = 0.0

            # 🚀 [GapDriftAlphaEngine] Renaissance / Citadel Bayesian Continuous Prior Update
            if sid == 'S1':
                signal_cache = (market_data or {}).get('signal_cache', {})
                ois = float(signal_cache.get('ois', 50.0) or 50.0)
                if ois > 0:
                    prior_boost = round(((ois - 50.0) / 100.0) * float(reward), 4)
                    ev += prior_boost
                    logger.info(f"  🚀 [GapDriftAlphaEngine] S1 Bayesian Continuous Prior Boost: {prior_boost:+.4f} (OIS={ois:.1f})")

            # 🚀 [CorporateActionProxyEngine] Mega-Cap DART Corporate Action Proxy Boost
            try:
                from src.intelligence.corporate_action_proxy_engine import CorporateActionProxyEngine
                dart_feats = (market_data or {}).get('dart_features', {})
                proxy_boosts = CorporateActionProxyEngine.evaluate_proxy_boost(dart_feats)
                if sid in ('S1', 'S11') and proxy_boosts:
                    max_p_boost = max(proxy_boosts.values())
                    ev += max_p_boost
                    logger.info(f"  🚀 [CorporateActionProxy] {sid} Proxy Boost applied: +{max_p_boost:.4f}")
            except Exception as _p_e:
                pass

            # 🚀 [S14 Market-Neutral Pair Alpha] Continuous Kaufman Noise Bayesian Prior
            if sid == 'S14_PAIR_ALPHA':
                sc_data = (market_data or {}).get('signal_cache', {})
                ker_val = float(sc_data.get('qqq_ker', sc_data.get('ker', (market_data or {}).get('qqq_ker', 0.25))) or 0.25)
                noise_intensity = max(0.0, min(1.0, 1.0 - ker_val))
                prior_boost = round((noise_intensity - 0.50) * float(reward), 4)
                ev += prior_boost
                logger.info(f"  🚀 [S14 Continuous Noise Prior] KER={ker_val:.3f}, Noise={noise_intensity:.1%} ➔ EV boost: {prior_boost:+.4f}")

            ev_scores[sid] = ev
            logger.info(f"  [Competitive EV Engine] {sid}: EV = {ev:+.5f} (p_win={p_win:.2f}, reward={reward:.4f}, risk={risk:.4f})")

        # EV 경쟁 모델: 최우수 EV 전략 탐색 (Winner-Take-All)
        best_stream = max(ev_scores, key=ev_scores.get) if ev_scores else 'S1'
        best_ev = ev_scores.get(best_stream, 0.0)

        # 100% 몰아주기 또는 Softmax EV 유연 분할 배분 (기본값 True로 승자독식 독점 타파)
        use_softmax = cfg.get('allocator.use_softmax_ev', True)
        final_weights = {sid: 0.0 for sid in self.STREAMS}

        # [Offensive Decision Model 4] Continuous Volatility-Contracted Offense Multiplier
        vix_val = float((market_data or {}).get('signal_cache', {}).get('vix', 18.0) or 18.0)
        vix_neutral = float((market_data or {}).get('vix_neutral', 18.0) or 18.0)
        if regime == 'bull' and vix_val < vix_neutral and vix_val > 0:
            golden_bull_mult = round(1.0 + min(0.30, max(0.0, (vix_neutral - vix_val) / vix_neutral * 0.5)), 4)
            logger.info(f"  🚀 [Golden Bull Offense Multiplier] Regime={regime}, VIX={vix_val:.1f} < {vix_neutral:.1f} ➔ {golden_bull_mult}x 동적 자본 승수 적용!")
        else:
            golden_bull_mult = 1.0

        if use_softmax:
            import math
            temp = max(float(cfg.get('allocator.softmax_temperature', 0.005)), 1e-4)
            max_ev = max(ev_scores.values()) if ev_scores else 0.0
            exp_evs = {k: math.exp((v - max_ev) / temp) for k, v in ev_scores.items()}
            total_exp = max(sum(exp_evs.values()), 1e-9)
            final_weights = {k: round((v / total_exp) * golden_bull_mult, 4) for k, v in exp_evs.items()}
            logger.info(f"  📊 [Competitive EV Softmax] 유연 분할 배분 확정 (Golden Multiplier={golden_bull_mult}x): {final_weights}")
        elif best_ev > 0:
            final_weights[best_stream] = round(1.0 * golden_bull_mult, 4)
            logger.info(f"  🏆 [Competitive EV Winner-Take-All] 우위 전략 {best_stream} 선택 (EV = {best_ev:+.5f}) → 100% 몰아주기 배분 확정 (Golden Mult={golden_bull_mult}x)")
        else:
            fallback_stream = 'S0' if 'S0' in self.STREAMS else self.STREAMS[0]
            final_weights[fallback_stream] = 1.0
            logger.warning(f"  ⚠️ [Competitive EV Safety] 모든 전략 EV <= 0 → 안전 자산({fallback_stream}) 100% 현금 파킹 배분")

        # [Pure Dynamic Math Architecture] 고정 15% 하드코딩 오버라이드 전면 폐지
        # S0~S13 모든 스트림의 비중은 순수 Softmax EV(기대값, 승률, 손익비, 노이즈 온도)의 수학적 계산 결과에만 100% 종속됩니다.

        total_w = sum(final_weights.values())
        if total_w > 0:
            target_sum = golden_bull_mult if golden_bull_mult > 1.0 else 1.0
            final_weights = {k: round((v / total_w) * target_sum, 4) for k, v in final_weights.items()}

        min_weight = cfg.get('allocator.min_stream_weight', 0.0)
        stream_signals = stream_metrics.get('_stream_signals')
        if stream_signals:
            stream_signals = self._apply_sentiment_penalty(stream_signals)
            final_weights = self._enforce_single_asset_limit(final_weights, stream_signals)
            final_weights = self._enforce_sector_limits(final_weights, stream_signals)
        final_weights = self._apply_s2_performance_fallback(final_weights, stream_metrics, regime, market_data, s0_sigs)
        threshold = cfg.get('allocator.rebalance_threshold', 0.05)
        needs_rebalance = self._needs_rebalance(final_weights, threshold)
        if needs_rebalance:
            self._last_weights = final_weights
            self._allocation_history.append({'date': datetime.now().isoformat(), 'weights': final_weights, 'regime': regime, 'method': 'competitive_ev_winner_take_all'})
            try:
                from src.measurement.event_ledger import log_event
                log_event('ALLOCATION', {'weights': final_weights, 'regime': regime, 'trigger': 'rebalance', 'method': 'competitive_ev_winner_take_all'}, source='alpha_allocator')
            except Exception as _e0:
                logger.critical(f'  [alpha_allocator] Allocation Event Logging: {_e0}', exc_info=True)
            logger.info(f"  📊 AlphaAllocator (Competitive EV): {final_weights} (regime={regime})")
        try:
            _micro_bound = set(cfg.get('allocator.micro_bound_streams', ['S1', 'S2']))
            _macro_bound = set((sid for sid in final_weights if sid not in _micro_bound and sid not in ('S5', 'S_YIELD')))
            _crowding_active = False
            try:
                import json as _json
                from pathlib import Path as _Path
                from datetime import datetime as _dt, timedelta as _td
                _ca_path = _Path('results') / 'crowding_alert.json'
                if _ca_path.exists():
                    _ca = _json.loads(_ca_path.read_text(encoding='utf-8'))
                    _ca_ts = _dt.fromisoformat(_ca.get('timestamp', '2000-01-01'))
                    if _dt.now() - _ca_ts < _td(hours=24) and _ca.get('crowding_detected'):
                        _crowding_active = True
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                logger.critical('[SILENT_BYPASS] Suppressed exception at alpha_allocator.py:450', exc_info=True)
            _in_macro_defense = _crowding_active or regime in ('bear', 'crash', 'caution')
            if _in_macro_defense:
                from src.risk.intraday_micro_guard import IntradayMicroGuard
                _guard = IntradayMicroGuard()
                _micro_ok, _micro_reason = _guard.check()
                _cut_bc = float(cfg.get('allocator.macro_cut.bear_crash', 0.6))
                _cut_ca = float(cfg.get('allocator.macro_cut.caution', 0.3))
                _macro_cut = _cut_bc if regime in ('bear', 'crash') else _cut_ca
                _freed = 0.0
                for _sid in list(_macro_bound):
                    if _sid in final_weights:
                        _cut = final_weights[_sid] * _macro_cut
                        final_weights[_sid] = round(final_weights[_sid] - _cut, 4)
                        _freed += _cut
                if _micro_ok:
                    _micro_boost = _freed * float(cfg.get('allocator.micro_boost_ratio', 0.3))
                    for _sid in _micro_bound:
                        if _sid in final_weights:
                            final_weights[_sid] = round(final_weights[_sid] + _micro_boost / max(1, len(_micro_bound)), 4)
                    _remaining_defense = _freed - _micro_boost
                    if 'S5' in final_weights:
                        final_weights['S5'] = round(final_weights['S5'] + _remaining_defense, 4)
                    logger.info(f"  [Phase 76 TwoTrack] MICRO 보호 및 S5 방어 파킹 증액: regime={regime}, crowding={_crowding_active}")
                else:
                    for _sid in _micro_bound:
                        if _sid in final_weights:
                            final_weights[_sid] = 0.0
                    if 'S5' in final_weights:
                        final_weights['S5'] = round(final_weights['S5'] + _freed, 4)
                    logger.warning(f"  [Phase 76 TwoTrack] MICRO HALT: {_micro_reason} -> 전액 S5 현금 파킹 전환")
                _total = sum(final_weights.values())
                if _total > 0:
                    final_weights = {k: round(v / _total, 4) for k, v in final_weights.items()}
        except Exception as _tt_err:
            logger.critical(f'  [Phase 76 TwoTrack] 실패, fallback: {_tt_err}', exc_info=True)
        try:
            _corr_alert = float(cfg.get('risk.stream_corr_alert', 0.8))
            _corr_scale = float(cfg.get('risk.corr_breakdown_scale', 0.7))
            _monitor_streams = ['S1', 'S2', 'S3']
            _directions = []
            for _sid in _monitor_streams:
                _tratio = float(stream_metrics.get(f'{_sid}_target_ratio', 0.0))
                if abs(_tratio) > 0.01:
                    _directions.append(1.0 if _tratio > 0 else -1.0)
            if len(_directions) >= 2:
                _mean_dir = sum(_directions) / len(_directions)
                _avg_corr = abs(_mean_dir)
                if _avg_corr > _corr_alert:
                    logger.warning(f"  📊 [CORR_BREAKDOWN] 스트림 포지션 쏠림 발생 (방향성={_avg_corr:.2f} > {_corr_alert:.2f}) — S1/S2/S3 배분 {_corr_scale:.0%} 축소 적용")
                    for _sid in _monitor_streams:
                        if _sid in final_weights:
                            final_weights[_sid] = round(final_weights[_sid] * _corr_scale, 4)
                    _total = sum(final_weights.values())
                    if _total > 0:
                        final_weights = {k: round(v / _total, 4) for k, v in final_weights.items()}
        except Exception as _corr_e:
            logger.error(f'  [CORR_BREAKDOWN] 쏠림 감지 실패 (비치명적): {_corr_e}', exc_info=True)
        try:
            _aum = float(cfg.get('portfolio.aum_krw', 3000000000))
            _s1_cap_thr = float(cfg.get('portfolio.s1_capacity_threshold_krw', 5000000000))
            _s1_cap_scale = float(cfg.get('portfolio.s1_capacity_scale_rate', 0.5))
            if _aum > _s1_cap_thr and 'S1' in final_weights:
                _s1_orig = final_weights['S1']
                _s1_reduced = round(_s1_orig * _s1_cap_scale, 4)
                _s1_freed = round(_s1_orig - _s1_reduced, 4)
                final_weights['S1'] = _s1_reduced
                if 'S5' in final_weights:
                    final_weights['S5'] = round(final_weights['S5'] + _s1_freed, 4)
                logger.warning(f"  [Capacity-Aware] AUM ₩{_aum / 1000000000.0:.1f}B > 임계 ₩{_s1_cap_thr / 1000000000.0:.1f}B → S1 {_s1_orig:.1%} → {_s1_reduced:.1%} (해제분 {_s1_freed:.1%} → S5 이관)")
                _cap_total = sum(final_weights.values())
                if _cap_total > 0:
                    final_weights = {k: round(v / _cap_total, 4) for k, v in final_weights.items()}
        except Exception as _cap_e:
            logger.error(f'  [Capacity-Aware] S1 사이징 실패 (비치명적): {_cap_e}', exc_info=True)
        return final_weights

    def _apply_s2_performance_fallback(self, weights: Dict[str, float], stream_metrics: Dict, regime: str, market_data: Dict=None, s0_sigs: List[Dict]=None) -> Dict[str, float]:
        """[Phase 40] S2 ML Alpha 성과 기반 자동 예산 몰수 → S3 이관.

        측정 지표:
            - S2 최근 5일 실현 WR (measurement_engine._compute_s2_rolling_metrics 결과)
            - S2 최근 5일 Out-of-Sample IC

        패널티 조건 (OR):
            - WR < s2.wr_threshold (기본 40%)
            - IC < s2.ic_threshold (기본 -0.02)

        조치:
            - S2 예산 × s2.penalty_ratio (기본 0.2x)
            - 잉여 예산 → S3_A(60%) + S3_B(40%) 비례 배분
            - S3 없으면 현금(HOLD) 처리

        Returns:
            조정된 weights dict
        """
        _cfg = DynamicConfig()
        penalty_ratio = float(_cfg.get('s2.penalty_ratio', 0.2))
        s3a_ratio = float(_cfg.get('s2.fallback_target_s3a', 0.6))
        s3b_ratio = float(_cfg.get('s2.fallback_target_s3b', 0.4))
        wr_threshold = float(_cfg.get('s2.wr_threshold', 0.4))
        ic_threshold = float(_cfg.get('s2.ic_threshold', -0.02))
        try:
            s2_rolling = stream_metrics.get('_s2_rolling', {})
            penalty_triggered = bool(s2_rolling.get('penalty_triggered', False))
            wr_5d = s2_rolling.get('wr_5d')
            ic_5d = s2_rolling.get('ic_5d')
            n_trades = int(s2_rolling.get('n_trades_5d', 0) or 0)
            if wr_5d is None or n_trades < 3:
                logger.info('  [Phase 40] S2 Auto-Fallback: 데이터 부족 (n=%d) → skip', n_trades)
                return weights
            penalty_triggered = float(wr_5d) < wr_threshold or (ic_5d is not None and float(ic_5d) < ic_threshold)
            if not penalty_triggered:
                logger.info(f"  [Phase 40] S2 Auto-Fallback: 정상 (WR={wr_5d:.1%}, IC={ic_5d})")
                return weights
            weights = dict(weights)
            s2_keys = [k for k in weights if k.upper().startswith('S2')]
            if not s2_keys:
                return weights
            total_surplus = 0.0
            for k in s2_keys:
                original = weights[k]
                penalized = original * penalty_ratio
                surplus = original - penalized
                weights[k] = penalized
                total_surplus += surplus
            s3a_keys = [k for k in weights if 'S3' in k.upper() and 'A' in k.upper()]
            s3b_keys = [k for k in weights if 'S3' in k.upper() and 'B' in k.upper()]
            if not s3a_keys:
                s3a_keys = [k for k in weights if k.upper().startswith('S3')][:1]
            if not s3b_keys and len([k for k in weights if k.upper().startswith('S3')]) > 1:
                s3b_keys = [k for k in weights if k.upper().startswith('S3')][1:]
            if s3a_keys:
                weights[s3a_keys[0]] = weights.get(s3a_keys[0], 0.0) + total_surplus * s3a_ratio
            if s3b_keys:
                weights[s3b_keys[0]] = weights.get(s3b_keys[0], 0.0) + total_surplus * s3b_ratio
            if not s3a_keys and (not s3b_keys):
                for k in s2_keys:
                    weights[k] += total_surplus / len(s2_keys)
            total_w = sum(weights.values())
            if total_w > 0:
                weights = {k: v / total_w for k, v in weights.items()}
            logger.warning(f"  ⚠️ [Phase 40] S2 Auto-Fallback 발동! WR={wr_5d:.1%}, IC={ic_5d}, 잉여 {total_surplus:.3f} → S3 이관. 조정 weights={weights}")
            try:
                from src.measurement.event_ledger import log_event
                log_event('S2_FALLBACK', {'wr_5d': wr_5d, 'ic_5d': ic_5d, 'surplus_transferred': total_surplus, 'new_weights': weights}, source='alpha_allocator')
            except Exception as _e1:
                logger.critical(f'  [alpha_allocator] 알파 배분 로직: {_e1}', exc_info=True)
            return weights
        except Exception as e:
            logger.critical(f'  [Phase 40] S2 Auto-Fallback 오류 (skip): {e}', exc_info=True)
        try:
            is_bull_market = regime == 'bull' or (market_data and market_data.get('prob_recovery', 0.0) > 0.5)
            if is_bull_market:
                s2_rolling = stream_metrics.get('_s2_rolling', {})
                wr_5d = s2_rolling.get('wr_5d')
                n_trades = int(s2_rolling.get('n_trades_5d', 0) or 0)
                _min_wr = float(_cfg.get('allocation.kelly.min_wr', 0.6))
                _min_trades = int(_cfg.get('allocation.kelly.min_trades', 3))
                _score_floor = _cfg.get('allocation.macro_score_floor', 40.0)
                if wr_5d is not None and float(wr_5d) >= _min_wr and (n_trades >= _min_trades):
                    weights = dict(weights)
                    s2_keys = [k for k in weights if k.upper().startswith('S2')]
                    if s2_keys:
                        upscale_ratio = _cfg.get('allocation.s2_upscale_ratio', 0.5)
                        current_s2 = sum((weights.get(k, 0.0) for k in s2_keys))
                        if current_s2 < upscale_ratio:
                            diff = upscale_ratio - current_s2
                            _defense_pool = _cfg.get('allocator.kelly_defense_streams', ['S5'])
                            defense_keys = [k for k in weights if k in _defense_pool]
                            total_defense = sum((weights.get(k, 0.0) for k in defense_keys))
                            if total_defense > 0:
                                _take_cap = float(cfg.get('allocator.kelly_defense_take_cap', 0.8))
                                take_ratio = min(diff, total_defense * _take_cap)
                                for dk in defense_keys:
                                    take_amt = weights[dk] / total_defense * take_ratio
                                    weights[dk] -= take_amt
                                for k in s2_keys:
                                    weights[k] += take_ratio / len(s2_keys)
                                total_w = sum(weights.values())
                                if total_w > 0:
                                    weights = {k: v / total_w for k, v in weights.items()}
                                logger.info(f"  [AlphaAllocator] 🚀 Dynamic Kelly Upscaling 발동! (S2 WR={wr_5d:.1%}). 방어 예산 {take_ratio:.3f} 회수하여 S2 집중 (weights={weights})")
        except Exception as e:
            logger.critical(f'  [AlphaAllocator] Kelly Upscaling 에러 무시: {e}', exc_info=True)
        if s0_sigs and any((s.get('trigger_cash_sweep', False) for s in s0_sigs)):
            weights = self._apply_s0_cash_sweep(weights, s0_sigs, stream_metrics)
        return weights

    def _apply_s0_cash_sweep(self, weights: Dict[str, float], s0_sigs: List[Dict], stream_metrics: Dict) -> Dict[str, float]:
        """메달리온/브릿지워터 스타일의 폭포수 현금화 (Waterfall Liquidation).

        ★ Mathematical Cash Sweep Refactoring (2026-07-17)
        ─────────────────────────────────────────────────────────
        [폐지] Tier 4 — MDD 기반 강제 청산: S3/S10을 낙폭(DrawDown) 임계치로
                         회수하던 로직을 영구 삭제합니다.
                         근거: MDD는 후행 지표이며 수익성 판단의 기준이 될 수 없음.
                         충분한 MDD가 없어도 기대수익률이 낮으면 징발되어야 하고,
                         MDD가 크더라도 기대수익률이 높다면 보호되어야 합니다.

        [신설] Tier 2 — 순수 기대수익률 경쟁 (Expected-Return Competition):
                         S0의 기대수익률이 (대상 스트림의 기대수익률 + 마찰비용)을
                         초과하는 경우에만 징발 적격. S3/S10도 동일한 수학적 기준 적용.

        폭포수 구조:
          Tier 1: S5(유휴 현금/파킹) — 무조건 최우선 회수, 기회비용 없음
          Tier 2: 수학적 경쟁 — 기대수익률 오름차순 정렬 후 순차 징발
                   조건: S0 기대수익률 > (스트림 기대수익률 + sweep_friction_buffer)
                   면제: S4(Advisory/Pension) — 자동매매 불가 구조적 이유
        """
        try:
            from config.dynamic_config import DynamicConfig as _SwCfg
            _cfg = _SwCfg()
            target_sweep_ratio = 0.0
            s0_expected_return = 0.0
            for s in s0_sigs:
                if s.get('trigger_cash_sweep', False):
                    target_sweep_ratio = max(target_sweep_ratio, s.get('target_sweep_ratio', 0.2))
                    s0_expected_return = max(s0_expected_return, s.get('expected_return', 0.05))
            if target_sweep_ratio <= 0:
                return weights
            exempt_streams = set(_cfg.get('allocator.exempt_streams', ['S4']))
            friction_buffer = float(_cfg.get('allocator.sweep_friction_buffer', 0.003))
            current_total_sweep = 0.0
            needed_sweep = target_sweep_ratio
            if 'S5' in weights and weights['S5'] > 0:
                available = weights['S5']
                take = min(available, needed_sweep)
                weights['S5'] -= take
                current_total_sweep += take
                needed_sweep -= take
                if take > 0:
                    logger.info(f"  🌊 [Waterfall Tier 1] 무위험 파킹(S5)에서 {take:.1%} 징발 완료 (S5 잔여={weights['S5']:.1%})")
            if needed_sweep > 0:
                competition_candidates = []
                for sid in weights:
                    if sid in ('S0', 'S5') or sid in exempt_streams:
                        continue
                    if weights.get(sid, 0) <= 0:
                        continue
                    metrics = stream_metrics.get(sid, {})
                    exp_ret = metrics.get('expected_return', None)
                    if exp_ret is None:
                        ic = metrics.get('ic_5d', 0.0)
                        vol = self._compute_volatility(metrics.get('daily_returns', []))
                        if vol == 0:
                            vol = 0.15
                        exp_ret = ic * vol
                    exp_ret = float(exp_ret)
                    competition_candidates.append((sid, exp_ret, weights[sid]))
                competition_candidates.sort(key=lambda x: x[1])
                for sid, exp_ret, current_weight in competition_candidates:
                    if needed_sweep <= 0:
                        break
                    hurdle = exp_ret + friction_buffer
                    if s0_expected_return <= hurdle:
                        logger.info(f"  🛡️ [Waterfall Tier 2] {sid} 징발 스킵 — S0 기대수익률({s0_expected_return:.2%}) ≤ {sid} 기대수익률({exp_ret:.2%}) + 마찰비용({friction_buffer:.2%}) = 허들({hurdle:.2%})")
                        continue
                    take = min(current_weight, needed_sweep)
                    weights[sid] -= take
                    current_total_sweep += take
                    needed_sweep -= take
                    logger.warning(f"  🚨 [Waterfall Tier 2] S0 기대수익률({s0_expected_return:.2%})이 {sid} 기대수익률({exp_ret:.2%}) + 마찰비용({friction_buffer:.2%}) = 허들({hurdle:.2%})을 압도하여 {sid}에서 {take:.1%} 자본 징발")
            if 'S0' in weights:
                weights['S0'] += current_total_sweep
            else:
                weights['S0'] = current_total_sweep
            total_w = sum(weights.values())
            if total_w > 0:
                weights = {k: round(v / total_w, 4) for k, v in weights.items()}
            shortfall = max(0.0, target_sweep_ratio - current_total_sweep)
            logger.warning(f"  🏁 [Waterfall 완료] S0 Beta 베팅에 {current_total_sweep:.1%} 투입 (목표={target_sweep_ratio:.1%}, 미달={shortfall:.1%}, 마찰버퍼={friction_buffer:.2%})")
        except Exception as e:
            logger.critical(f'  [AlphaAllocator] 폭포수 현금화(Waterfall Cash Sweep) 에러: {e}', exc_info=True)
        return weights

    def _compute_volatility(self, returns: List[float], window: int=None) -> float:
        """일간 수익률에서 연율 변동성 계산."""
        if window is None:
            window = cfg.get('allocator.vol_window', 60)
        _min_data = cfg.get('allocator.vol_min_data', 5)
        if len(returns) < _min_data:
            return 0.0
        data = returns[-window:]
        n = len(data)
        mean = sum(data) / n
        var = sum(((r - mean) ** 2 for r in data)) / n
        daily_vol = math.sqrt(var)
        return daily_vol * math.sqrt(cfg.get('common.trading_days_per_year', 252))

    def _get_base_weights(self, regime: str, market_data: Dict=None) -> Dict[str, float]:
        from config.dynamic_config import DynamicConfig
        _cfg = DynamicConfig()
        '레짐별 Base weights (DynamicConfig 우선).'
        custom = {}
        for sid in self.STREAMS:
            key = f'allocator.{sid.lower()}_base_weight'
            val = cfg.get(key)
            if val is not None:
                custom[sid] = val
        if custom:
            base = self._default_regime_weights().get(regime, {}).copy()
            base.update(custom)
            total = sum(base.values())
            if total > 0:
                return {k: v / total for k, v in base.items()}
        regime_weights = self._default_regime_weights()
        base = regime_weights.get(regime, regime_weights['caution']).copy()
        if market_data:
            prob_fc = market_data.get('prob_flash_crash', 0.0)
            prob_rec = market_data.get('prob_recession', 0.0)
            prob_lr = market_data.get('prob_liquidity_rally', 0.0)
            prob_recovery = market_data.get('prob_recovery', 0.0)
            if prob_fc > 0.0 or prob_rec > 0.0 or prob_lr > 0.0 or (prob_recovery > 0.0):
                if prob_fc > 0.5:
                    fc_alloc = 0.2 * prob_fc
                    logger.info(f"  [AlphaAllocator] Probabilistic Flash Crash (P={prob_fc:.2f}) 감지! 🔫 Sniper Mode (S1 += {fc_alloc:.2f})")
                    base['S1'] = base.get('S1', 0.0) + fc_alloc
                    if 'S5' in base:
                        base['S5'] = max(0.0, base['S5'] - fc_alloc / 2)
                    if 'S7' in base:
                        base['S7'] = max(0.0, base['S7'] - fc_alloc / 2)
                if prob_rec > 0.5:
                    rec_cash_alloc = 0.3 * prob_rec
                    logger.info(f"  [AlphaAllocator] Probabilistic Recession (P={prob_rec:.2f}) 감지! 🛡️ 구조적 침체 대비 현금화 (S5 += {rec_cash_alloc:.2f})")
                    base['S1'] = 0.0
                    if 'S3_A' in base:
                        base['S3_A'] = max(0.0, base['S3_A'] - 0.1)
                    base['S5'] = base.get('S5', 0.0) + rec_cash_alloc
                if prob_lr > 0.5:
                    lr_cash_alloc = 0.15 * prob_lr
                    logger.info(f"  [AlphaAllocator] Probabilistic Liquidity Rally (P={prob_lr:.2f}) 감지! 🚨 가치주 함정 (S5 += {lr_cash_alloc:.2f})")
                    if 'S2' in base:
                        base['S2'] = max(0.0, base['S2'] - lr_cash_alloc)
                    base['S5'] = base.get('S5', 0.0) + lr_cash_alloc
                uncertainty = 1.0 - max(prob_fc, prob_rec, prob_lr, prob_recovery, 0.0)
                if uncertainty > 0.4:
                    _uncertainty_add = float(_cfg.get('allocator.prob_uncertainty_cash_add', 0.1))
                    logger.info(f"  [AlphaAllocator] High Uncertainty (U={uncertainty:.2f}) 🌫️ 국면 불확실, 현금 비중 {_uncertainty_add * 100:.0f}% 추가 확보")
                    base['S5'] = base.get('S5', 0.0) + _uncertainty_add
            else:
                divergence_state = market_data.get('divergence_state')
                if divergence_state == 'flash_crash':
                    base['S1'] = 0.2
                    if 'S5' in base:
                        base['S5'] = max(0.0, base['S5'] - 0.1)
                    if 'S7' in base:
                        base['S7'] = max(0.0, base['S7'] - 0.1)
                elif divergence_state == 'liquidity_rally':
                    _legacy_lr_adj = float(_cfg.get('allocator.legacy_lr_adjustment', 0.15))
                    if 'S2' in base:
                        base['S2'] = max(0.0, base['S2'] - _legacy_lr_adj)
                    base['S5'] = base.get('S5', 0.0) + _legacy_lr_adj
            total = sum(base.values())
            if total > 0:
                base = {k: round(v / total, 4) for k, v in base.items()}
        return base

    def _enforce_min_weights(self, weights: Dict[str, float], min_w: float) -> Dict[str, float]:
        """최소 비중 보장 + 정규화."""
        for sid in self.STREAMS:
            if weights.get(sid, 0) < min_w:
                weights[sid] = min_w
        total = sum(weights.values())
        if total > 0:
            weights = {k: round(v / total, 4) for k, v in weights.items()}
        return weights

    def _compute_correlation_matrix(self, stream_metrics: Dict) -> Dict[str, float]:
        """스트림 간 상관계수 매트릭스 계산."""
        matrix = {}
        for i, sid_i in enumerate(self.STREAMS):
            for j, sid_j in enumerate(self.STREAMS):
                if j <= i:
                    continue
                returns_i = stream_metrics.get(sid_i, {}).get('daily_returns', [])
                returns_j = stream_metrics.get(sid_j, {}).get('daily_returns', [])
                corr = self._calc_correlation(returns_i, returns_j)
                key = f'{sid_i}_{sid_j}'
                matrix[key] = round(corr, 3)
        return matrix

    def _calc_correlation(self, x: List[float], y: List[float]) -> float:
        """피어슨 상관계수 계산."""
        n = min(len(x), len(y))
        _corr_min = cfg.get('allocator.corr_min_data', 5)
        if n < _corr_min:
            return 0.0
        x = x[-n:]
        y = y[-n:]
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum(((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))) / n
        std_x = math.sqrt(sum(((xi - mean_x) ** 2 for xi in x)) / n)
        std_y = math.sqrt(sum(((yi - mean_y) ** 2 for yi in y)) / n)
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)

    def _needs_rebalance(self, new_weights: Dict[str, float], threshold: float) -> bool:
        """리밸런싱 필요 여부 확인."""
        if not self._last_weights:
            return True
        for sid in self.STREAMS:
            old = self._last_weights.get(sid, 0)
            new = new_weights.get(sid, 0)
            if abs(new - old) > threshold:
                return True
        return False

    def get_current_weights(self) -> Dict[str, float]:
        """현재 배분 비율."""
        if self._last_weights:
            return self._last_weights
        return self._default_regime_weights()['caution'].copy()

    def get_allocation_history(self) -> List[Dict]:
        """배분 이력."""
        return self._allocation_history[-cfg.get('allocator.max_history', 30):]

    def get_risk_contribution(self, stream_metrics: Dict) -> Dict[str, float]:
        """스트림별 리스크 기여도 계산.

        Returns:
            {'S1': 0.33, 'S2': 0.33, 'S3': 0.33}
            (리스크 패리티이면 모두 ~33%)
        """
        weights = self.get_current_weights()
        vols = {}
        for sid in self.STREAMS:
            returns = stream_metrics.get(sid, {}).get('daily_returns', [])
            vols[sid] = self._compute_volatility(returns)
        risk_contribs = {}
        total_risk = sum((weights.get(sid, 0) * vols.get(sid, 0) for sid in self.STREAMS))
        if total_risk <= 0:
            return {sid: round(1.0 / len(self.STREAMS), 3) for sid in self.STREAMS}
        for sid in self.STREAMS:
            rc = weights.get(sid, 0) * vols.get(sid, 0) / total_risk
            risk_contribs[sid] = round(rc, 3)
        return risk_contribs
    _ETF_UNDERLYING_MAP_DEFAULT: Dict[str, str] = {'500050': '005930', '500051': '005930', '500052': '005930', '500053': '005930', '500060': '000660', '500061': '000660', '500063': '000660', '500064': '000660', '500065': '000660', '122630': 'KOSPI200', '114800': 'KOSPI200', '252670': 'KOSPI200'}
    _STREAM_PRIORITY_DEFAULT: Dict[str, int] = {'S3': 3, 'S2': 2, 'S1': 1}

    def _enforce_single_asset_limit(self, weights: Dict[str, float], stream_signals: Dict[str, List[Dict]]) -> Dict[str, float]:
        """[Phase 17: Ultimate Boosters] Kelly Sizing — 동적 단일 자산 한도.

        [원본] 모든 자산에 동일한 max_single_asset_exposure 적용 (기본 5%)

        [Phase 17 신규] 고확신 시그널에 한해 한도를 동적으로 해제:
          ┌─ S2 (ML Alpha):
          │   predict_proba > kelly.s2_cap_extreme(0.90) → 최대 20% 허용
          │   predict_proba > kelly.s2_cap_high_conviction(0.85) → 최대 15%
          │   그 외 → 기본 5% 유지
          └─ S3 (Factor/섹터):
              momentum_z > kelly.s3_zscore_threshold(2.5) → 최대 12% 허용
              그 외 → 기본 5% 유지

        모든 임계값은 DynamicConfig에서 로드 (매직 넘버 절대 금지).
        """

        def _get_dynamic_cap(sid: str, signals: List[Dict]) -> float:
            """[Phase 17: Ultimate Boosters] 스트림별 Kelly 동적 한도 결정."""
            base_cap = cfg.get('allocator.max_single_asset_exposure', 0.05)
            if sid == 'S2':
                proba_threshold = cfg.get('kelly.s2_proba_threshold', 0.9)
                cap_high_conv = cfg.get('kelly.s2_cap_high_conviction', 0.15)
                cap_extreme = cfg.get('kelly.s2_cap_extreme', 0.2)
                cap_normal = cfg.get('kelly.s2_cap_normal', 0.05)
                max_proba = max((float(sig.get('confidence') or sig.get('predict_proba') or 0) for sig in signals), default=0.0)
                if max_proba >= proba_threshold:
                    cap = cap_extreme
                    logger.info(f"  [Phase 17] Kelly S2: proba={max_proba:.2f} ≥ {proba_threshold} → 극단 고확신 캡 {cap:.0%}")
                elif max_proba >= proba_threshold - cfg.get('kelly.s2_proba_high_band', 0.05):
                    cap = cap_high_conv
                    logger.info(f"  [Phase 17] Kelly S2: proba={max_proba:.2f} → 고확신 캡 {cap:.0%}")
                else:
                    cap = cap_normal
                return cap
            elif sid == 'S3':
                z_threshold = cfg.get('kelly.s3_zscore_threshold', 2.5)
                cap_momentum = cfg.get('kelly.s3_cap_momentum', 0.12)
                cap_normal = cfg.get('kelly.s3_cap_normal', 0.05)
                max_z = max((float(sig.get('momentum_z') or sig.get('z_score') or 0) for sig in signals), default=0.0)
                if max_z >= z_threshold:
                    logger.info(f"  [Phase 17] Kelly S3: z={max_z:.2f} ≥ {z_threshold} → 모멘텀 강세 캡 {cap_momentum:.0%}")
                    return cap_momentum
                return cap_normal
            else:
                return base_cap
        asset_exposure: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        for sid in self.STREAMS:
            stream_w = weights.get(sid, 0)
            signals = stream_signals.get(sid, [])
            if not signals or stream_w <= 0:
                continue
            for sig in signals:
                ticker = sig.get('ticker', '')
                size_pct = sig.get('size_pct', 0)
                etf_map = cfg.get('allocator.etf_underlying_map', self._ETF_UNDERLYING_MAP_DEFAULT)
                underlying = etf_map.get(ticker, ticker)
                contribution = stream_w * size_pct
                if contribution > 0:
                    asset_exposure[underlying].append((sid, contribution))
        scale_factors: Dict[str, float] = {sid: 1.0 for sid in self.STREAMS}
        for underlying, exposures in asset_exposure.items():
            total_exp = sum((c for _, c in exposures))
            effective_cap = max((_get_dynamic_cap(exp_sid, stream_signals.get(exp_sid, [])) for exp_sid, _ in exposures))
            if total_exp <= effective_cap:
                if effective_cap > cfg.get('allocator.max_single_asset_exposure', 0.05):
                    logger.info(f"  [Phase 17] Kelly 캡 해제: {underlying} = {total_exp:.2%} ≤ 동적캡 {effective_cap:.2%} (허용)")
                continue
            excess = total_exp - effective_cap
            logger.warning(f"  ⚠️ 단일자산 초과: {underlying} = {total_exp:.2%} (동적캡 {effective_cap:.2%}, 초과 {excess:.2%})")
            _priority = cfg.get('allocator.stream_priority', self._STREAM_PRIORITY_DEFAULT)
            sorted_exp = sorted(exposures, key=lambda x: _priority.get(x[0], 0))
            remaining_excess = excess
            for exp_sid, exp_contrib in sorted_exp:
                if remaining_excess <= 0:
                    break
                reducible = min(exp_contrib, remaining_excess)
                if exp_contrib > 0:
                    reduction_ratio = reducible / exp_contrib
                    new_scale = scale_factors[exp_sid] * (1.0 - reduction_ratio)
                    scale_factors[exp_sid] = max(0.0, new_scale)
                    remaining_excess -= reducible
                    logger.info(f"    → {exp_sid} 축소: {underlying} 기여 {exp_contrib:.2%} → {exp_contrib - reducible:.2%} (스케일 {scale_factors[exp_sid]:.3f})")
        any_scaled = any((sf < 1.0 for sf in scale_factors.values()))
        if not any_scaled:
            return weights
        adjusted = {sid: round(weights.get(sid, 0) * scale_factors[sid], 6) for sid in self.STREAMS}
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {sid: round(w / total, 4) for sid, w in adjusted.items()}
        logger.info(f"  📊 [Phase 17] Kelly 동적 단일자산 제한: {weights} → {adjusted} (스케일: {scale_factors})")
        return adjusted

    def _enforce_sector_limits(self, weights: Dict[str, float], stream_signals: Dict[str, List[Dict]]) -> Dict[str, float]:
        """Cross-stream 섹터 한도 노출 제한 (Quasi-Neutralization).
        
        특정 섹터에 포트폴리오 비중이 과도하게 쏠리지 않도록 
        MarketContextManager의 sector_cap을 참조하여 비중을 축소합니다.
        축소된 잉여 자본은 강제로 S5(현금) 비중을 늘려 방어합니다.
        """
        sector_exposure: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        for sid in self.STREAMS:
            stream_w = weights.get(sid, 0)
            signals = stream_signals.get(sid, [])
            if not signals or stream_w <= 0:
                continue
            for sig in signals:
                ticker = sig.get('ticker', '')
                size_pct = sig.get('size_pct', 0)
                sector = market_ctx.get_sector(ticker)
                contribution = stream_w * size_pct
                if contribution > 0:
                    sector_exposure[sector].append((sid, contribution))
        scale_factors: Dict[str, float] = {sid: 1.0 for sid in self.STREAMS}
        cash_sweep_addition = 0.0
        for sector, exposures in sector_exposure.items():
            total_exp = sum((c for _, c in exposures))
            max_cap = market_ctx.get_sector_cap(sector)
            if total_exp <= max_cap:
                continue
            excess = total_exp - max_cap
            logger.warning(f"  🛡️ 섹터 쏠림 감지: [{sector}] = {total_exp:.2%} (한도 {max_cap:.2%}, 초과 {excess:.2%})")
            _priority = cfg.get('allocator.stream_priority', self._STREAM_PRIORITY_DEFAULT)
            sorted_exp = sorted(exposures, key=lambda x: _priority.get(x[0], 0))
            remaining_excess = excess
            for exp_sid, exp_contrib in sorted_exp:
                if remaining_excess <= 0:
                    break
                reducible = min(exp_contrib, remaining_excess)
                if exp_contrib > 0:
                    reduction_ratio = reducible / exp_contrib
                    new_scale = scale_factors[exp_sid] * (1.0 - reduction_ratio)
                    scale_factors[exp_sid] = max(0.0, new_scale)
                    remaining_excess -= reducible
                    cash_sweep_addition += reducible
                    logger.info(f"    → {exp_sid} 축소: {sector} 섹터 오버웨이트 방지")
        any_scaled = any((sf < 1.0 for sf in scale_factors.values()))
        if not any_scaled:
            return weights
        adjusted = {sid: round(weights.get(sid, 0) * scale_factors[sid], 6) for sid in self.STREAMS}
        if 'S5' in adjusted:
            adjusted['S5'] += cash_sweep_addition
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {sid: round(w / total, 4) for sid, w in adjusted.items()}
        logger.info(f"  📊 섹터 중립화 적용 완료. S5 현금 전환 = +{cash_sweep_addition:.2%}")
        return adjusted

    def _apply_sentiment_penalty(self, stream_signals: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
        """대안 데이터(감성 분석) 기반 종목별 비중 페널티 적용.
        
        Sentiment 점수가 극도로 낮을 경우 편입 제외 또는 비중을 대폭 삭감합니다.
        """
        try:
            from src.data.alternative_data_bridge import AlternativeDataBridge
            alt_bridge = AlternativeDataBridge()
        except ImportError as e:
            return stream_signals
        modified_signals = {}
        for sid, signals in stream_signals.items():
            valid_signals = []
            for sig in signals:
                ticker = sig.get('ticker')
                if not ticker:
                    valid_signals.append(sig)
                    continue
                sentiment = alt_bridge.fetch_sentiment_score(ticker)
                if sentiment < 0.2:
                    logger.warning(f"  🔻 감성 악화 (Sentiment={sentiment:.2f}): {ticker} 편입 제외")
                    continue
                elif sentiment < 0.4:
                    penalty_ratio = cfg.get('allocation.penalty_ratio', 0.5)
                    sig['size_pct'] = sig.get('size_pct', 0) * penalty_ratio
                    if sig['size_pct'] > 0:
                        logger.info(f"  ⚠️ 감성 주의 (Sentiment={sentiment:.2f}): {ticker} 비중 50% 축소")
                        valid_signals.append(sig)
                else:
                    valid_signals.append(sig)
            modified_signals[sid] = valid_signals
        return modified_signals

    def compute_global_market_weights(self, krx_metrics: Dict, us_metrics: Dict, regime: str = 'caution') -> Dict[str, float]:
        """[User Directive 2: Global Unified Capital Allocation Engine]

        국장(KRX)과 미장(US) 자금을 50:50 고정하지 않고,
        전체 NAV(100%) 관점에서 양 시장의 기대값($EV$) 및 Sharpe 비율에 따라
        자금을 동적으로 배분(Softmax / Risk Parity Hybrid)합니다.
        """
        try:
            ev_krx = float(krx_metrics.get('expected_return', 0.01)) / max(0.001, float(krx_metrics.get('volatility', 0.015)))
            ev_us = float(us_metrics.get('expected_return', 0.02)) / max(0.001, float(us_metrics.get('volatility', 0.015)))

            tau = cfg.get('allocator.global_softmax_temperature', 0.5)
            exp_krx = math.exp(min(10.0, max(-10.0, ev_krx / tau)))
            exp_us  = math.exp(min(10.0, max(-10.0, ev_us / tau)))

            w_krx = exp_krx / (exp_krx + exp_us)
            w_us = 1.0 - w_krx

            # 레짐별 캡 제약 (DynamicConfig에서 로드)
            max_single_mkt = cfg.get('allocator.global_max_single_market_weight', 0.85)
            min_single_mkt = cfg.get('allocator.global_min_single_market_weight', 0.15)

            w_krx = max(min_single_mkt, min(max_single_mkt, w_krx))
            w_us = 1.0 - w_krx

            logger.info(f"🌐 [Global Capital Allocator] NAV 배분: KRX {w_krx*100:.1f}% | US {w_us*100:.1f}% (EV_KRX={ev_krx:.2f}, EV_US={ev_us:.2f})")
            return {'w_krx': round(w_krx, 4), 'w_us': round(w_us, 4)}
        except Exception as e:
            logger.warning(f"⚠️ Global Capital Allocator 계산 예외: {e}")
            return {'w_krx': 0.5, 'w_us': 0.5}

    def compute_unified_factor_score(self, signal: Dict[str, Any], market_data: Optional[Dict[str, Any]] = None) -> float:
        """[Stage 2: Unified Multi-Factor Scoring Engine]
        
        대표님 승인 4대 첨단 공격 모델(옵션 스큐, 수급 불균형, 어닝 서프라이즈)을
        독립적 누더기 신호가 아닌 단일 팩터 점수(Q_unified)로 유기적 융합(Harmonize)합니다.
        
        수식:
          Q_unified = w1*Q_base + w2*Z_skew + w3*Z_flow + w4*Z_earnings
        """
        if market_data is None:
            market_data = signal.get('market_data', {}) or {}
            
        w_qvm = cfg.get('allocator.weight_qvm', 0.40) or 0.40
        w_skew = cfg.get('allocator.weight_option_skew', 0.20) or 0.20
        w_flow = cfg.get('allocator.weight_flow_imbalance', 0.20) or 0.20
        w_earn = cfg.get('allocator.weight_earnings_surprise', 0.20) or 0.20
        
        # 1. Base Factor Quality Score (QVM / ML score)
        base_q = float(signal.get('confidence', signal.get('qvm_score', 0.50)))
        
        # 2. Option Skew Score (Gaussian Standard Normal CDF)
        z_skew = float(market_data.get('option_skew_z', market_data.get('skewness', 0.0)))
        skew_score = 0.5 * (1.0 + math.erf(z_skew / math.sqrt(2.0)))
        
        # 3. Institutional Flow Imbalance Score (Gaussian Standard Normal CDF)
        z_flow = float(market_data.get('flow_zscore', market_data.get('net_flow_z', 0.0)))
        flow_score = 0.5 * (1.0 + math.erf(z_flow / math.sqrt(2.0)))
        
        # 4. Earnings Surprise Score (Gaussian Standard Normal CDF)
        z_earn = float(market_data.get('earnings_surprise_z', market_data.get('surprise_z', 0.0)))
        earn_score = 0.5 * (1.0 + math.erf(z_earn / math.sqrt(2.0)))
        
        # 5. Dynamic Weighted Combination
        q_unified = (w_qvm * base_q) + (w_skew * skew_score) + (w_flow * flow_score) + (w_earn * earn_score)
        q_unified = round(max(0.0, min(1.0, q_unified)), 4)
        
        logger.debug(f"  🎯 [Q_unified Score] {signal.get('ticker')}: Q={q_unified:.4f} (Base={base_q:.2f}, Skew={skew_score:.2f}, Flow={flow_score:.2f}, Earn={earn_score:.2f})")
        return q_unified
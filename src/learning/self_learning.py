"""
SelfLearning — IC 기반 자가학습 엔진
======================================

MeasurementEngine의 IC (Information Coefficient) 측정값을 기반으로
DynamicConfig의 파라미터를 자동 갱신합니다.

Project First의 SelfLearning을 계승 + Meridian 4-Stream 확장:
  - 기존: A1/A2/A3 슬리브별 IC → 가중치 조정
  - 확장: S1/S2/S3/S4 스트림별 Sharpe → 배분 비중 조정
  - 확장: Fallback 가중치 동적 갱신

설계 원칙:
  - 측정 (IC/Sharpe) → 판정 (파라미터 업데이트) 분리
  - 안전 경계 (safety bounds) 적용
  - 모든 변경은 EventLedger에 기록

Usage:
    from src.learning.self_learning import SelfLearning
    sl = SelfLearning()
    changes = sl.update(measurement_results)
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import logging
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class SelfLearning:
    """IC 기반 자가학습 엔진 (측정/판정 분리).

    MeasurementEngine의 IC 결과를 받아서
    DynamicConfig의 파라미터를 최적 방향으로 조정합니다.
    """
    IC_PARAM_MAP = {'rsi_ic': 'a3.fb.rsi_oversold_bonus', 'bb_ic': 'a3.fb.bb_low_bonus', 'macd_ic': 'a3.fb.macd_scale', 'volume_ic': 'a3.fb.volume_spike_bonus', 'momentum_ic': 'a3.fb.momentum_scale', 'sector_ic': 'a2.sector_momentum_weight', 'quality_ic': 'a3.fb.quality_bonus', 's1_sharpe': 'allocator.s1_base_weight', 's2_sharpe': 'allocator.s2_base_weight', 's3_sharpe': 'allocator.s3_base_weight', 's4_sharpe': 'allocator.s4_base_weight'}
    SAFETY_BOUNDS = {'a3.fb.rsi_oversold_bonus': (0.01, 0.25), 'a3.fb.bb_low_bonus': (0.01, 0.2), 'a3.fb.macd_scale': (0.5, 5.0), 'a3.fb.volume_spike_bonus': (0.01, 0.15), 'a3.fb.momentum_scale': (0.001, 0.02), 'a2.sector_momentum_weight': (0.1, 0.5), 'a3.fb.quality_bonus': (0.01, 0.15), 'allocator.s1_base_weight': (0.1, 0.4), 'allocator.s2_base_weight': (0.1, 0.5), 'allocator.s3_base_weight': (0.05, 0.3), 'allocator.s4_base_weight': (0.1, 0.4)}
    _BASE_LEARNING_RATE = 0.1

    def __init__(self):
        self._update_history: List[Dict] = []
        self._overrides_file = _PROJECT_ROOT / 'results' / 'dynamic_overrides.json'
        self._param_momentum: Dict[str, Dict] = {}
        self._momentum_file = _PROJECT_ROOT / 'results' / 'self_learning_momentum.json'
        self._load_momentum()

    def _load_momentum(self) -> None:
        """파라미터 모멘텀 상태 복원 (재시작 시 연속성 유지)."""
        try:
            if self._momentum_file.exists():
                self._param_momentum = json.loads(self._momentum_file.read_text(encoding='utf-8'))
                logger.debug(f"  SelfLearning: 모멘텀 상태 복원 ({len(self._param_momentum)}개 파라미터)")
        except Exception as e:
            logger.debug(f"  SelfLearning: 모멘텀 상태 로드 실패 (초기화): {e}")
            self._param_momentum = {}

    def _save_momentum(self) -> None:
        """파라미터 모멘텀 상태 저장."""
        try:
            self._momentum_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._momentum_file, self._param_momentum, indent=2, ensure_ascii=False)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f"  SelfLearning: 모멘텀 저장 실패: {e}")

    def _compute_adaptive_lr(self, param_key: str, ic_val: float, ic_std: float=0.0, regime: Optional[int] = None) -> float:
        """IC 안정성, 모멘텀 및 시장 국면(Regime)을 기반으로 적응형 학습률 계산.

        Regime Factors:
          - Regime 0 (Low Vol Trend): eta_scale = 0.5 (base_lr = 0.05, steady convergence)
          - Regime 1 (High Vol Crash): eta_scale = 2.0 (base_lr = 0.20, rapid shock adaptation)
          - Regime 2 (Sideways Range): eta_scale = 1.0 (base_lr = 0.10, balanced)
        """
        raw_base_lr = float(cfg.get('self_learning.base_lr', self._BASE_LEARNING_RATE))
        if regime == 1:
            regime_factor = 2.0
        elif regime == 0:
            regime_factor = 0.5
        else:
            regime_factor = 1.0

        base_lr = raw_base_lr * regime_factor
        min_lr = float(pget('self_learning.min_lr', 0.01))
        max_lr = float(pget('self_learning.max_lr', 0.3))
        momentum_boost = float(pget('self_learning.momentum_boost', 0.05))
        max_momentum = int(pget('self_learning.max_momentum_steps', 5))
        stability_scale = float(cfg.get('self_learning.stability_scale', 1.0))
        if ic_std > 1e-09:
            stability_factor = max(0.5, min(1.5, stability_scale / (1.0 + ic_std * 2.0)))
        else:
            stability_factor = 1.0
        adjusted_lr = base_lr * stability_factor
        state = self._param_momentum.get(param_key, {'prev_ic': None, 'direction_count': 0, 'current_lr': base_lr})
        current_direction = 1 if ic_val > 0 else -1
        prev_ic = state.get('prev_ic')
        if prev_ic is not None:
            prev_direction = 1 if prev_ic > 0 else -1
            if current_direction == prev_direction:
                direction_count = min(state.get('direction_count', 0) + 1, max_momentum)
                momentum_factor = 1.0 + momentum_boost * direction_count
            else:
                direction_count = 0
                momentum_factor = float(pget('self_learning.direction_change_lr_factor', 0.7))
        else:
            direction_count = 0
            momentum_factor = 1.0
        final_lr = max(min_lr, min(max_lr, adjusted_lr * momentum_factor))
        self._param_momentum[param_key] = {'prev_ic': round(ic_val, 5), 'direction_count': direction_count, 'current_lr': round(final_lr, 5), 'stability_factor': round(stability_factor, 4), 'momentum_factor': round(momentum_factor, 4)}
        logger.debug(f"  [AdaptiveLR] {param_key}: lr={final_lr:.4f} (base={base_lr:.3f}, stability={stability_factor:.3f}, momentum={momentum_factor:.3f}, dir_count={direction_count})")
        return final_lr

    def measure_ic(self, measurement_results: Dict = None) -> Dict:
        """IC 측정값 추출 (순수 측정).

        Args:
            measurement_results: MeasurementEngine의 결과

        Returns:
            IC 측정 딕셔너리
        """
        if measurement_results is None:
            measurement_results = {}
        res_dir = self._overrides_file.parent if hasattr(self, '_overrides_file') else (_PROJECT_ROOT / 'results')
        ic_values = {}

        # 1. Feature IC 수집: measurement_results['feature_ic'] + results/ic_report.json['features']
        feature_ic = dict(measurement_results.get('feature_ic', {}) or {})
        ic_report_path = res_dir / 'ic_report.json'
        if ic_report_path.exists():
            try:
                ic_rep = json.loads(ic_report_path.read_text(encoding='utf-8'))
                if isinstance(ic_rep, dict):
                    rep_feats = ic_rep.get('features', ic_rep.get('feature_ic', {}))
                    if isinstance(rep_feats, dict):
                        for fk, fv in rep_feats.items():
                            norm_k = fk if fk in self.IC_PARAM_MAP else (f"{fk}_ic" if f"{fk}_ic" in self.IC_PARAM_MAP else fk)
                            if isinstance(fv, dict):
                                val = fv.get('ic', fv.get('value', fv.get('mean_ic')))
                                std_v = fv.get('std', fv.get('ic_std', 0.0))
                                if val is not None and norm_k not in feature_ic:
                                    feature_ic[norm_k] = float(val)
                                    feature_ic[f"{norm_k}_std"] = float(std_v or 0.0)
                            elif isinstance(fv, (int, float)):
                                if norm_k not in feature_ic:
                                    feature_ic[norm_k] = float(fv)
            except Exception as e:
                logger.debug(f"  SelfLearning: ic_report.json 로드 실패: {e}")

        for feature_key, raw_ic_val in list(feature_ic.items()):
            norm_key = feature_key if feature_key in self.IC_PARAM_MAP else (f"{feature_key}_ic" if f"{feature_key}_ic" in self.IC_PARAM_MAP else feature_key)
            if norm_key in self.IC_PARAM_MAP:
                if isinstance(raw_ic_val, dict):
                    ic_val = float(raw_ic_val.get('ic', raw_ic_val.get('value', 0.0)) or 0.0)
                    ic_std = float(raw_ic_val.get('std', raw_ic_val.get('ic_std', 0.0)) or 0.0)
                else:
                    ic_val = float(raw_ic_val)
                    ic_std = float(feature_ic.get(f'{norm_key}_std', feature_ic.get(f'{feature_key}_std', 0.0)) or 0.0)
                ic_values[norm_key] = {
                    'value': ic_val,
                    'abs_value': abs(ic_val),
                    'direction': 'positive' if ic_val > 0 else 'negative',
                    'std': ic_std,
                }

        # 2. Stream Sharpe 수집: measurement_results / measurement_engine.json['official']['per_stream'] / stream_metrics.json['raw_data']
        merged_streams: Dict[str, Dict] = {}
        me_disk = {}
        me_path = res_dir / 'measurement_engine.json'
        if me_path.exists():
            try:
                me_disk = json.loads(me_path.read_text(encoding='utf-8'))
            except Exception:
                me_disk = {}
        sm_disk = {}
        sm_path = res_dir / 'stream_metrics.json'
        if sm_path.exists():
            try:
                sm_disk = json.loads(sm_path.read_text(encoding='utf-8'))
            except Exception:
                sm_disk = {}

        stream_sources = [
            sm_disk.get('raw_data', {}) if isinstance(sm_disk, dict) else {},
            sm_disk.get('metrics', {}) if isinstance(sm_disk, dict) else {},
            (me_disk.get('views') or {}).get('sleeves', {}) if isinstance(me_disk, dict) else {},
            (me_disk.get('official') or {}).get('per_stream', {}) if isinstance(me_disk, dict) else {},
            (measurement_results.get('views') or {}).get('sleeves', {}),
            (measurement_results.get('official') or {}).get('per_stream', {}),
            measurement_results.get('streams', {}),
        ]
        for src in stream_sources:
            if isinstance(src, dict):
                for sk, sv in src.items():
                    if isinstance(sv, dict):
                        base_s = merged_streams.get(sk, {})
                        for k_f, v_f in sv.items():
                            if v_f is not None:
                                base_s[k_f] = v_f
                        merged_streams[sk] = base_s

        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        active_streams = list(cfg.get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
        for extra_s in ('S1', 'S2', 'S3', 'S4'):
            if extra_s not in active_streams:
                active_streams.append(extra_s)
        for sid in active_streams:
            short_sid = str(sid).split('_')[0]
            sharpe_key = f'{short_sid.lower()}_sharpe'
            s_info = merged_streams.get(sid) or merged_streams.get(short_sid) or {}
            sharpe = s_info.get('sharpe')
            if sharpe is not None:
                sh_val = float(sharpe)
                ic_values[sharpe_key] = {
                    'value': sh_val,
                    'abs_value': abs(sh_val),
                    'direction': 'positive' if sh_val > 0 else 'negative',
                }
        return {'ic_values': ic_values, 'n_features': len(ic_values), 'timestamp': datetime.now().isoformat()}

    def judge_updates(self, ic_measurement: Dict) -> Dict:
        """판정: IC 기반 파라미터 변경 결정.

        Args:
            ic_measurement: measure_ic()의 반환값

        Returns:
            파라미터 변경 목록
        """
        changes = []
        ic_values = ic_measurement.get('ic_values', {})
        bounds_enabled = cfg.get('fallback.safety_bounds_enabled', True)
        ic_values = self._winsorize_ic_values(ic_values)
        for ic_key, ic_data in ic_values.items():
            param_key = self.IC_PARAM_MAP.get(ic_key)
            if not param_key:
                continue
            current_val = cfg.get(param_key)
            if current_val is None:
                continue
            ic_val = ic_data['value']
            if ic_key.endswith('_sharpe'):
                ic_std = ic_data.get('std', 0.0)
                new_val = self._adjust_stream_weight(param_key, current_val, ic_val, ic_std)
            else:
                ic_std = ic_data.get('std', 0.0)
                adaptive_lr = self._compute_adaptive_lr(param_key, ic_val, ic_std)
                direction = 1 if ic_val > 0 else -1
                magnitude = min(adaptive_lr, abs(ic_val) * adaptive_lr)
                delta = current_val * magnitude * direction
                new_val = current_val + delta
            if bounds_enabled and param_key in self.SAFETY_BOUNDS:
                lo, hi = self.SAFETY_BOUNDS[param_key]
                new_val = max(lo, min(hi, new_val))
            if abs(new_val - current_val) > 1e-06:
                changes.append({'param': param_key, 'ic_key': ic_key, 'ic_value': round(ic_val, 4), 'old_value': round(current_val, 6), 'new_value': round(new_val, 6), 'delta_pct': round((new_val / current_val - 1) * 100, 2) if current_val != 0 else 0})
        return {'changes': changes, 'n_changes': len(changes), 'bounds_enabled': bounds_enabled}

    def _winsorize_ic_values(self, ic_values: dict) -> dict:
        """IC/Sharpe 아웃라이어 Winsorization (95 percentile 절사).

        르네상스 스타일 노이즈 방어: 코로나 블랙스완 같은 1일짜리 아웃라이어가
        자기진화 파라미터를 한 방향으로 폭주시키는 것을 방지.

        알고리즘:
          1. IC 값들의 rolling 히스토리를 momentum 파일에서 로드
          2. 현재 ic_val이 히스토리의 95 percentile을 초과하면 절사
          3. 히스토리 부족 시(< min_samples) 고정 상한(max_abs_ic)으로 클리핑

        Args:
            ic_values: {ic_key: {'value': float, ...}} 딕셔너리

        Returns:
            동일 구조, value가 절사된 딕셔너리
        """
        winsor_enabled = bool(cfg.get('self_learning.winsorize_enabled', True))
        winsor_pct = float(pget('self_learning.winsorize_pct', 95.0))
        winsor_min_hist = int(pget('self_learning.winsorize_min_history', 20))
        max_abs_ic = float(pget('self_learning.winsorize_max_abs_ic', 0.3))
        max_abs_sharpe = float(pget('self_learning.winsorize_max_abs_sharpe', 3.0))
        if not winsor_enabled:
            return ic_values
        ic_hist_file = self._overrides_file.parent / 'self_learning_ic_history.json'
        ic_history: dict = {}
        try:
            if ic_hist_file.exists():
                ic_history = json.loads(ic_hist_file.read_text(encoding='utf-8'))
        except Exception:
            ic_history = {}
        import numpy as np
        clipped_values = {}
        for ic_key, ic_data in ic_values.items():
            raw_val = ic_data.get('value', 0.0)
            is_sharpe = ic_key.endswith('_sharpe')
            hist = ic_history.get(ic_key, [])
            if len(hist) >= winsor_min_hist:
                lo_pct = 100.0 - winsor_pct
                hi_pct = winsor_pct
                lo = float(np.percentile(hist, lo_pct))
                hi = float(np.percentile(hist, hi_pct))
                clipped_val = float(np.clip(raw_val, lo, hi))
                if clipped_val != raw_val:
                    logger.debug(f"  [Winsorize] {ic_key}: {raw_val:.4f} → {clipped_val:.4f} (범위 [{lo:.4f}, {hi:.4f}], N={len(hist)})")
            else:
                max_abs = max_abs_sharpe if is_sharpe else max_abs_ic
                clipped_val = float(np.clip(raw_val, -max_abs, max_abs))
                if clipped_val != raw_val:
                    logger.debug(f"  [Winsorize] {ic_key}: {raw_val:.4f} → {clipped_val:.4f} (절대 상한 ±{max_abs}, 히스토리 부족 {len(hist)}/{winsor_min_hist})")
            new_data = dict(ic_data)
            new_data['value'] = clipped_val
            new_data['abs_value'] = abs(clipped_val)
            new_data['direction'] = 'positive' if clipped_val > 0 else 'negative'
            new_data['raw_value'] = raw_val
            clipped_values[ic_key] = new_data
            max_hist_size = int(pget('self_learning.winsorize_history_size', 252))
            ic_history[ic_key] = (hist + [raw_val])[-max_hist_size:]
        try:
            atomic_write_json(ic_hist_file, ic_history, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f"  IC 히스토리 저장 실패 (비치명적): {e}")
        return clipped_values

    def _adjust_stream_weight(self, param_key: str, current_val: float, sharpe: float, ic_std: float=0.0) -> float:
        """스트림 배분 조정 (Sharpe 기반, 적응형 학습률).

        Sharpe > 1.0: 가중치 적극 증가
        Sharpe > 0:   가중치 소폭 증가
        Sharpe < 0:   가중치 감소
        """
        adaptive_lr = self._compute_adaptive_lr(param_key, sharpe, ic_std)
        if sharpe > float(cfg.get('self_learning.sharpe_strong_bull', 1.0)):
            factor = 1 + adaptive_lr * float(pget('self_learning.sharpe_strong_factor', 0.5))
        elif sharpe > 0:
            factor = 1 + adaptive_lr * float(pget('self_learning.sharpe_mild_factor', 0.2))
        elif sharpe > float(pget('self_learning.sharpe_mild_bear', -0.5)):
            factor = 1 - adaptive_lr * float(pget('self_learning.sharpe_mild_bear_factor', 0.2))
        else:
            factor = 1 - adaptive_lr * float(pget('self_learning.sharpe_strong_bear_factor', 0.5))
        return current_val * factor

    def update(self, measurement_results: Dict) -> Dict:
        """전체 자가학습 사이클: 측정 → 판정 → 적용.

        Args:
            measurement_results: MeasurementEngine의 결과

        Returns:
            {
                'measurement': { ... },
                'judgment': { ... },
                'applied': bool,
            }
        """
        if not cfg.get('fallback.auto_update_enabled', True):
            return {'measurement': {}, 'judgment': {'changes': [], 'n_changes': 0}, 'applied': False, 'reason': 'auto_update 비활성화'}
        qa_enabled = bool(cfg.get('self_learning.data_qa_enabled', True))
        qa_min_score = float(pget('self_learning.data_qa_min_score', 90.0))
        qa_freeze_log = bool(cfg.get('self_learning.data_qa_log_freeze', True))
        if qa_enabled:
            data_confidence = float(measurement_results.get('data_confidence_score', 100.0))
            if data_confidence < qa_min_score:
                if qa_freeze_log:
                    logger.warning(f"  ❄️  [DataQA] 학습 동결: data_confidence={data_confidence:.1f} < 기준 {qa_min_score:.0f}. 파라미터 업데이트 Skip (오염 데이터 방어)")
                return {'measurement': {}, 'judgment': {'changes': [], 'n_changes': 0}, 'applied': False, 'reason': f'DataQA 동결: confidence={data_confidence:.1f}', 'data_confidence_score': data_confidence}
        ic_measurement = self.measure_ic(measurement_results)
        update_judgment = self.judge_updates(ic_measurement)
        applied = False
        if update_judgment['n_changes'] > 0:
            applied = self._apply_changes(update_judgment['changes'])
        self._update_history.append({'date': datetime.now().isoformat(), 'n_changes': update_judgment['n_changes'], 'applied': applied})
        if applied:
            try:
                from src.measurement.event_ledger import log_event
                log_event('SELF_LEARNING', {'n_changes': update_judgment['n_changes'], 'changes': [{'param': c['param'], 'old': c['old_value'], 'new': c['new_value']} for c in update_judgment['changes'][:5]]}, source='self_learning')
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                pass
            logger.info(f"  🧠 SelfLearning: {update_judgment['n_changes']}개 파라미터 갱신")
        return {'measurement': ic_measurement, 'judgment': update_judgment, 'applied': applied}

    def _apply_changes(self, changes: List[Dict]) -> bool:
        """변경사항을 dynamic_overrides.json에 적용."""
        try:
            from src.utils.file_ops import safe_json_update
            self._overrides_file.parent.mkdir(parents=True, exist_ok=True)

            def _mutate_overrides(cur):
                base = cur if isinstance(cur, dict) else {}
                for change in changes:
                    base[change['param']] = change['new_value']
                base['_last_updated'] = datetime.now().isoformat()
                base['_updated_by'] = 'self_learning'
                return base

            safe_json_update(self._overrides_file, _mutate_overrides, default={}, indent=2, ensure_ascii=False, strict=True)
            cfg.reload()
            self._save_momentum()
            return True
        except Exception as e:
            logger.error(f"  SelfLearning 적용 실패: {e}")
            return False

    def get_history(self) -> List[Dict]:
        """학습 이력."""
        return self._update_history[-30:]
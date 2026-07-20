#!/usr/bin/env python3
"""
Project Meridian — Gap Feedback Engine
========================================
갭분석(gap_analysis.json) 결과를 읽어 DynamicConfig 파라미터를
자동 조정하는 피드백 루프.

파이프라인 위치:
  evening phase: gap_analysis → gap_feedback → retrain (if needed)

5가지 피드백 규칙:
  1. high_confidence_miss → Confidence Calibrator 감도 조정
  2. stream_concentration → 해당 스트림 position_scale 감점
  3. consecutive_losses → SL 타이트닝 + 최소 confidence 상향
  4. regime_mismatch → 해당 레짐 invest_ratio 축소
  5. large_loss_cluster → SL ATR multiplier 축소

모든 조정은 config_overrides.json에 기록.
재학습 트리거 필요 시 retrain_request.json 생성.

Usage:
    python3 scripts/gap_feedback.py
    # 또는 daily_pipeline.py evening phase에서 자동 실행

DynamicConfig 키:
    gap_feedback.enabled (default: true)
    gap_feedback.max_adjust_per_day (default: 3)
    gap_feedback.cooldown_hours (default: 12)
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_RESULTS = _PROJECT_ROOT / 'results'
_GAP_FILE = _RESULTS / 'gap_analysis.json'
_FEEDBACK_LOG = _RESULTS / 'gap_feedback_log.json'
_RETRAIN_REQUEST = _RESULTS / 'retrain_request.json'


class GapFeedbackEngine:
    """갭분석 결과 → 파라미터 자동 조정 엔진.

    gap_analysis.json의 failure_patterns + summary를 읽어
    DynamicConfig 파라미터를 동적으로 조정합니다.
    """

    def __init__(self):
        self.enabled = cfg.get('gap_feedback.enabled', True)
        self.max_adjustments = cfg.get('gap_feedback.max_adjust_per_day', 3)
        self.cooldown_hours = cfg.get('gap_feedback.cooldown_hours', 12)
        self.adjustments_made: List[Dict] = []
        self.retrain_needed = False
        self.retrain_trigger = ''

    def run(self) -> Dict[str, Any]:
        """피드백 엔진 실행.

        Returns:
            피드백 결과 (adjustments, retrain_needed 등)
        """
        result = {
            'timestamp': datetime.now().isoformat(),
            'status': 'skip',
            'enabled': self.enabled,
        }

        if not self.enabled:
            result['reason'] = 'gap_feedback.enabled=false'
            logger.info("  ⏭️ Gap Feedback: 비활성화")
            return result

        # 쿨다운 체크
        if self._in_cooldown():
            result['reason'] = f'쿨다운 중 ({self.cooldown_hours}h)'
            result['status'] = 'cooldown'
            logger.info(f"  ⏳ Gap Feedback: 쿨다운 중")
            return result

        # 갭분석 결과 로드
        gap_data = self._load_gap_analysis()
        if not gap_data:
            result['reason'] = 'gap_analysis.json 없음 또는 미완료'
            return result

        if gap_data.get('status') != 'completed':
            result['reason'] = f"gap status={gap_data.get('status')}"
            return result

        result['gap_n_trades'] = gap_data.get('n_trades', 0)

        # ━━━ 피드백 규칙 적용 ━━━
        patterns = gap_data.get('failure_patterns', {})
        summary = gap_data.get('summary', {})

        # 규칙 1: 고신뢰도 실패 → Confidence Calibrator 감도 조정
        self._rule_high_confidence_miss(patterns)

        # 규칙 2: 스트림 집중 실패 → 해당 스트림 position_scale 감점
        self._rule_stream_concentration(patterns)

        # 규칙 3: 연속 손실 → SL 타이트닝 + 최소 confidence 상향
        self._rule_consecutive_losses(patterns)

        # 규칙 4: 레짐 미스매치 → 해당 레짐 invest_ratio 축소
        self._rule_regime_mismatch(patterns)

        # 규칙 5: 큰 손실 집중 → SL ATR multiplier 축소
        self._rule_large_loss_cluster(patterns)

        # 규칙 6: 전체 DA < 50% → 재학습 트리거
        self._rule_overall_da_failure(summary)

        # 규칙 7: IC < 0 → 재학습 트리거
        self._rule_ic_negative(summary)

        # ━━━ 결과 기록 ━━━
        result['status'] = 'completed'
        result['n_adjustments'] = len(self.adjustments_made)
        result['adjustments'] = self.adjustments_made
        result['retrain_needed'] = self.retrain_needed
        result['retrain_trigger'] = self.retrain_trigger

        # 조정사항 적용
        if self.adjustments_made:
            self._apply_adjustments()

        # 재학습 요청 생성
        if self.retrain_needed:
            self._create_retrain_request()

        # 피드백 로그 저장
        self._save_log(result)

        logger.info(
            f"  ✅ Gap Feedback: {len(self.adjustments_made)}건 조정, "
            f"retrain={'필요' if self.retrain_needed else '불필요'}")

        return result

    # ──────────────────────────────────────────────
    # 피드백 규칙들
    # ──────────────────────────────────────────────

    def _rule_high_confidence_miss(self, patterns: Dict):
        """규칙 1: 고신뢰도 예측이 실패 → confidence 보정."""
        if len(self.adjustments_made) >= self.max_adjustments:
            return
        for p in patterns.get('patterns', []):
            if p.get('type') != 'high_confidence_miss':
                continue
            ratio = p.get('ratio', 0)
            threshold = cfg.get('gap_feedback.high_conf_miss_ratio', 0.30)
            if ratio <= threshold:
                continue

            # confidence 임계값 상향
            current_min = cfg.get('s2.min_confidence', 0.55)
            adjust_step = cfg.get('gap_feedback.conf_adjust_step', 0.02)
            max_conf = cfg.get('gap_feedback.conf_adjust_max', 0.70)
            new_min = min(max_conf, current_min + adjust_step)

            self.adjustments_made.append({
                'rule': 'high_confidence_miss',
                'key': 's2.min_confidence',
                'old': current_min,
                'new': round(new_min, 4),
                'reason': f'고신뢰도 실패 비율 {ratio:.0%} > {threshold:.0%}',
            })
            logger.info(
                f"    📐 Rule 1: s2.min_confidence "
                f"{current_min:.2f} → {new_min:.2f}")

    def _rule_stream_concentration(self, patterns: Dict):
        """규칙 2: 특정 스트림에 실패 집중 → position_scale 감점."""
        if len(self.adjustments_made) >= self.max_adjustments:
            return
        for p in patterns.get('patterns', []):
            if p.get('type') != 'stream_concentration':
                continue
            stream = p.get('stream', '')
            fail_rate = p.get('fail_rate', 0)
            if not stream or fail_rate <= 0.50:
                continue

            key = f'sizer.stream_scale.{stream}'
            current = cfg.get(key, 1.0)
            reduction = cfg.get('gap_feedback.stream_scale_reduction', 0.10)
            floor = cfg.get('gap_feedback.stream_scale_floor', 0.50)
            new_val = max(floor, current - reduction)

            self.adjustments_made.append({
                'rule': 'stream_concentration',
                'key': key,
                'old': current,
                'new': round(new_val, 4),
                'reason': f'{stream} 실패율 {fail_rate:.0%}',
            })
            logger.info(
                f"    📐 Rule 2: {key} "
                f"{current:.2f} → {new_val:.2f}")

    def _rule_consecutive_losses(self, patterns: Dict):
        """규칙 3: 연속 손실 → SL 타이트닝 + confidence 상향."""
        if len(self.adjustments_made) >= self.max_adjustments:
            return
        for p in patterns.get('patterns', []):
            if p.get('type') != 'consecutive_losses':
                continue
            streak = p.get('max_streak', 0)
            sl_trigger = cfg.get('gap_feedback.consecutive_loss_sl_trigger', 4)
            if streak < sl_trigger:
                continue

            # SL multiplier 축소
            key = 'exit.sl_atr_multiplier'
            current = cfg.get(key, 2.0)
            step = cfg.get('gap_feedback.sl_tighten_step', 0.1)
            floor = cfg.get('gap_feedback.sl_atr_floor', 1.2)
            new_val = max(floor, current - step)

            self.adjustments_made.append({
                'rule': 'consecutive_losses',
                'key': key,
                'old': current,
                'new': round(new_val, 4),
                'reason': f'연속 {streak}건 손실',
            })
            logger.info(
                f"    📐 Rule 3: {key} "
                f"{current:.1f} → {new_val:.1f}")

            # 재학습 트리거
            retrain_trigger = cfg.get(
                'gap_feedback.consecutive_loss_retrain_trigger', 5)
            if streak >= retrain_trigger:
                self.retrain_needed = True
                self.retrain_trigger = 'gap_consecutive_losses'

    def _rule_regime_mismatch(self, patterns: Dict):
        """규칙 4: 특정 레짐에서 DA < 40% → invest_ratio 축소."""
        if len(self.adjustments_made) >= self.max_adjustments:
            return
        for p in patterns.get('patterns', []):
            if p.get('type') != 'regime_mismatch':
                continue
            regime = p.get('regime', '')
            da = p.get('da', 1.0)
            if not regime or da >= 0.40:
                continue

            key = f'sizer.regime_invest_ratio.{regime}'
            current = cfg.get(key, 0.50)
            reduction = cfg.get('gap_feedback.regime_ratio_reduction', 0.10)
            floor = cfg.get('gap_feedback.regime_ratio_floor', 0.20)
            new_val = max(floor, current - reduction)

            self.adjustments_made.append({
                'rule': 'regime_mismatch',
                'key': key,
                'old': current,
                'new': round(new_val, 4),
                'reason': f'{regime} 레짐 DA={da:.0%}',
            })
            logger.info(
                f"    📐 Rule 4: {key} "
                f"{current:.2f} → {new_val:.2f}")

    def _rule_large_loss_cluster(self, patterns: Dict):
        """규칙 5: 큰 손실 집중 → SL ATR multiplier 축소."""
        if len(self.adjustments_made) >= self.max_adjustments:
            return
        for p in patterns.get('patterns', []):
            if p.get('type') != 'large_loss_cluster':
                continue
            count = p.get('count', 0)
            min_count = cfg.get('gap_feedback.large_loss_min_count', 2)
            if count < min_count:
                continue

            avg_loss = abs(p.get('avg_loss_pct', 0))

            # SL multiplier 축소 (큰 손실에 비례)
            key = 'exit.sl_atr_multiplier'
            current = cfg.get(key, 2.0)
            # 평균 손실이 클수록 더 많이 축소
            scale = cfg.get('gap_feedback.large_loss_sl_scale', 0.05)
            step = min(0.3, avg_loss * scale)
            floor = cfg.get('gap_feedback.sl_atr_floor', 1.2)
            new_val = max(floor, current - step)

            if new_val < current:
                self.adjustments_made.append({
                    'rule': 'large_loss_cluster',
                    'key': key,
                    'old': current,
                    'new': round(new_val, 4),
                    'reason': f'-5%+ 손실 {count}건, 평균 {avg_loss:.1f}%',
                })
                logger.info(
                    f"    📐 Rule 5: {key} "
                    f"{current:.1f} → {new_val:.1f}")

    def _rule_overall_da_failure(self, summary: Dict):
        """규칙 6: 전체 DA < 50% → 재학습 트리거."""
        da = summary.get('overall_da', 1.0)
        n = summary.get('n_trades', 0)
        da_threshold = cfg.get('gap_feedback.da_retrain_threshold', 0.50)
        min_trades = cfg.get('gap_feedback.da_retrain_min_trades', 15)

        if n >= min_trades and da < da_threshold:
            self.retrain_needed = True
            self.retrain_trigger = f'gap_da_failure_{da:.0%}'
            logger.info(
                f"    🔄 Rule 6: DA={da:.1%} < {da_threshold:.0%} "
                f"({n}건) → 재학습 트리거")

    def _rule_ic_negative(self, summary: Dict):
        """규칙 7: IC < 0 → 재학습 트리거."""
        ic = summary.get('overall_ic', 0)
        n = summary.get('n_trades', 0)
        min_trades = cfg.get('gap_feedback.ic_retrain_min_trades', 20)

        if n >= min_trades and ic < 0:
            self.retrain_needed = True
            self.retrain_trigger = f'gap_ic_negative_{ic:.3f}'
            logger.info(
                f"    🔄 Rule 7: IC={ic:.3f} < 0 "
                f"({n}건) → 재학습 트리거")

    # ──────────────────────────────────────────────
    # 유틸리티
    # ──────────────────────────────────────────────

    def _load_gap_analysis(self) -> Optional[Dict]:
        """gap_analysis.json 로드."""
        if not _GAP_FILE.exists():
            return None
        try:
            data = json.loads(_GAP_FILE.read_text())
            # 24시간 이내의 결과만 사용
            ts = data.get('timestamp', '')
            if ts:
                gap_time = datetime.fromisoformat(ts)
                age_hours = (datetime.now() - gap_time).total_seconds() / 3600
                max_age = cfg.get('gap_feedback.max_gap_age_hours', 24)
                if age_hours > max_age:
                    logger.info(
                        f"  ⏭️ Gap Feedback: gap_analysis {age_hours:.0f}h 경과 > {max_age}h")
                    return None
            return data
        except Exception as e:
            logger.warning(f"  gap_analysis 로드 실패: {e}")
            return None

    def _in_cooldown(self) -> bool:
        """마지막 피드백 이후 쿨다운 중인지 확인."""
        if not _FEEDBACK_LOG.exists():
            return False
        try:
            log = json.loads(_FEEDBACK_LOG.read_text())
            last_ts = log.get('timestamp', '')
            if last_ts:
                last = datetime.fromisoformat(last_ts)
                elapsed = (datetime.now() - last).total_seconds() / 3600
                return elapsed < self.cooldown_hours
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return False

    def _apply_adjustments(self):
        """조정 사항을 config_overrides.json에 적용."""
        overrides_file = _PROJECT_ROOT / 'config' / 'config_overrides.json'
        try:
            overrides = {}
            if overrides_file.exists():
                overrides = json.loads(overrides_file.read_text())

            for adj in self.adjustments_made:
                key = adj['key']
                overrides[key] = adj['new']

            # 원자적 쓰기
            import tempfile, os
            fd, tmp = tempfile.mkstemp(
                dir=str(overrides_file.parent), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(overrides, f, indent=2, ensure_ascii=False)
            os.replace(tmp, str(overrides_file))

            logger.info(
                f"    💾 {len(self.adjustments_made)}건 조정 → "
                f"config_overrides.json 적용 완료")
        except Exception as e:
            logger.warning(f"  config_overrides 적용 실패: {e}")

    def _create_retrain_request(self):
        """retrain_request.json 생성 → should_retrain()이 읽어감."""
        try:
            request = {
                'timestamp': datetime.now().isoformat(),
                'trigger': self.retrain_trigger,
                'source': 'gap_feedback',
                'gap_analysis_timestamp': '',
            }
            # gap timestamp 가져오기
            if _GAP_FILE.exists():
                gap = json.loads(_GAP_FILE.read_text())
                request['gap_analysis_timestamp'] = gap.get('timestamp', '')

            _RETRAIN_REQUEST.write_text(
                json.dumps(request, indent=2, ensure_ascii=False))
            logger.info(
                f"    🔄 retrain_request.json 생성: {self.retrain_trigger}")
        except Exception as e:
            logger.warning(f"  retrain_request 생성 실패: {e}")

    def _save_log(self, result: Dict):
        """피드백 실행 로그 저장."""
        try:
            import tempfile, os
            _FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(_FEEDBACK_LOG.parent), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(result, f, indent=2, ensure_ascii=False,
                          default=str)
            os.replace(tmp, str(_FEEDBACK_LOG))
        except Exception as e:
            logger.warning(f"  피드백 로그 저장 실패: {e}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    engine = GapFeedbackEngine()
    result = engine.run()
    print(json.dumps(result, indent=2, ensure_ascii=False))

#!/usr/bin/env python3
"""
Data Quality Validator — 데이터 품질 검증 시스템
==================================================

Medallion Upgrade Phase 2-D-2.

기능:
  1. 결측값 검증 (missing ratio)
  2. 이상값 검증 (Z-score outliers)
  3. 시계열 연속성 검증 (gaps)
  4. 값 범위 검증 (domain checks)
  5. 실시간 데이터 품질 스코어

모든 파라미터 DynamicConfig 동적 로드.
"""

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class DataQualityValidator:
    """데이터 품질 검증."""

    def validate_timeseries(self, data: List[Dict],
                              required_fields: List[str] = None) -> Dict:
        """시계열 데이터 품질 검증.

        Args:
            data: 일별 데이터 [{date, price, volume, ...}, ...]
            required_fields: 필수 필드 목록

        Returns:
            품질 지표 딕셔너리
        """
        if not data:
            return {'quality_score': 0, 'issues': ['빈 데이터'], 'valid': False}

        if required_fields is None:
            required_fields = ['date', 'close', 'volume']

        n = len(data)
        issues = []
        scores = []

        # 1. 결측값 검증
        missing = self._check_missing(data, required_fields)
        scores.append(missing['score'])
        if missing['score'] < 0.95:
            issues.append(f"결측값: {missing['missing_ratio']*100:.1f}%")

        # 2. 이상값 검증
        if 'close' in data[0]:
            closes = [d.get('close', 0) for d in data if d.get('close')]
            outliers = self._check_outliers(closes)
            scores.append(outliers['score'])
            if outliers['n_outliers'] > 0:
                issues.append(f"이상값: {outliers['n_outliers']}건")
        else:
            scores.append(1.0)

        # 3. 시계열 연속성
        continuity = self._check_continuity(data)
        scores.append(continuity['score'])
        if continuity['n_gaps'] > 0:
            issues.append(f"갭: {continuity['n_gaps']}건")

        # 4. 값 범위 검증
        domain = self._check_domain(data)
        scores.append(domain['score'])
        if domain['n_violations'] > 0:
            issues.append(f"범위 위반: {domain['n_violations']}건")

        # 종합 품질 점수 (가중 평균)
        weights = cfg.get('data_quality.score_weights',
                            [0.30, 0.25, 0.25, 0.20])
        quality_score = sum(s * w for s, w in zip(scores, weights))
        min_quality = cfg.get('data_quality.min_score', 0.80)

        return {
            'quality_score': round(quality_score, 3),
            'valid': quality_score >= min_quality,
            'n_records': n,
            'missing': missing,
            'outliers': outliers if 'close' in data[0] else None,
            'continuity': continuity,
            'domain': domain,
            'issues': issues,
            'timestamp': datetime.now().isoformat(),
        }

    def _check_missing(self, data: List[Dict],
                         required_fields: List[str]) -> Dict:
        """결측값 검증."""
        n = len(data)
        total_fields = n * len(required_fields)
        missing_count = 0

        for record in data:
            for field in required_fields:
                if field not in record or record[field] is None:
                    missing_count += 1

        ratio = missing_count / total_fields if total_fields > 0 else 0
        return {
            'missing_count': missing_count,
            'missing_ratio': round(ratio, 4),
            'score': round(1 - ratio, 4),
        }

    def _check_outliers(self, values: List[float]) -> Dict:
        """Z-score 기반 이상값 검출."""
        n = len(values)
        if n < 5:
            return {'n_outliers': 0, 'score': 1.0}

        # 수익률 기반 이상값 (price → return)
        returns = [(values[i] - values[i-1]) / values[i-1]
                     if values[i-1] != 0 else 0
                     for i in range(1, n)]

        if not returns:
            return {'n_outliers': 0, 'score': 1.0}

        mean_r = sum(returns) / len(returns)
        std_r = math.sqrt(
            sum((r - mean_r) ** 2 for r in returns) / len(returns))

        threshold = cfg.get('data_quality.outlier_zscore', 4.0)
        outliers = []
        for i, r in enumerate(returns):
            if std_r > 0 and abs(r - mean_r) / std_r > threshold:
                outliers.append({'index': i + 1, 'return': round(r, 4),
                                   'zscore': round((r - mean_r) / std_r, 2)})

        score = 1 - len(outliers) / len(returns) if returns else 1.0

        return {
            'n_outliers': len(outliers),
            'outliers': outliers[:5],  # Top 5
            'score': round(max(0, score), 4),
        }

    def _check_continuity(self, data: List[Dict]) -> Dict:
        """시계열 연속성 검증 (영업일 기준 갭 감지)."""
        gaps = []
        max_gap_days = cfg.get('data_quality.max_gap_days', 5)

        dates = [d.get('date', '') for d in data if d.get('date')]
        for i in range(1, len(dates)):
            # 간단한 날짜 차이 (ISO format 기준)
            try:
                d1 = dates[i-1][:10]
                d2 = dates[i][:10]
                if d1 == d2:
                    continue
                # 대략적 갭 계산 (정밀하지 않지만 경량)
                y1, m1, d1_ = int(d1[:4]), int(d1[5:7]), int(d1[8:10])
                y2, m2, d2_ = int(d2[:4]), int(d2[5:7]), int(d2[8:10])
                rough_diff = (y2 - y1) * 365 + (m2 - m1) * 30 + (d2_ - d1_)
                if rough_diff > max_gap_days:
                    gaps.append({
                        'from': dates[i-1], 'to': dates[i],
                        'gap_days': rough_diff})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue

        score = 1 - len(gaps) / max(len(dates) - 1, 1) if dates else 1.0

        return {
            'n_gaps': len(gaps),
            'gaps': gaps[:5],
            'score': round(max(0, score), 4),
        }

    def _check_domain(self, data: List[Dict]) -> Dict:
        """값 범위 검증."""
        violations = []

        for i, record in enumerate(data):
            # 가격은 양수여야 함
            close = record.get('close', None)
            if close is not None and close <= 0:
                violations.append({
                    'index': i, 'field': 'close',
                    'value': close, 'rule': '> 0'})

            # 거래량은 0 이상
            volume = record.get('volume', None)
            if volume is not None and volume < 0:
                violations.append({
                    'index': i, 'field': 'volume',
                    'value': volume, 'rule': '>= 0'})

        score = 1 - len(violations) / max(len(data), 1)

        return {
            'n_violations': len(violations),
            'violations': violations[:5],
            'score': round(max(0, score), 4),
        }

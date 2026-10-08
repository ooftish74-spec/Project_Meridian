"""
Project Meridian — Telemetry Validation Guard
================================================
리포팅 및 요약 스크립트에서 키 미존재로 인한 Silent Default (0.00%) 반환 착시를 100% 방지하는 텔레메트리 검증기.

주요 기능:
1. signal_cache.json 내 정량 지표 키의 존재 여부를 엄격히 검증.
2. 정량 지표 표준 키 맵핑 (예: Korea Night Market -> ewy_change_1d).
3. 기본값(0.00) 은폐(Swallowing) 차단.
"""

from typing import Dict, Any, Tuple
import logging

logger = logging.getLogger(__name__)

CANONICAL_METRIC_MAP = {
    'korea_night_market_1d': ['ewy_change_1d', 'flkr_change_1d', 'cme_korea_futures'],
    'call_skew_zscore': ['CALL_SKEW_ZSCORE', 'vix_std_20', 'options_pcr'],
    'us_market_1d': ['sp500_change_1d', 'nasdaq_change_1d', 'sox_change_1d']
}

class TelemetryValidationGuard:
    """텔레메트리 키 검증 및 표시 착시 방어 엔진."""

    @staticmethod
    def extract_validated_metric(data: Dict[str, Any], metric_name: str, default_val: float = 0.0) -> Tuple[float, str, bool]:
        """표준 지표 키에서 실제 수치를 정밀 추출하고, 데이터 존재 여부를 검증.

        Returns:
            (value, resolved_key, is_valid)
        """
        candidate_keys = CANONICAL_METRIC_MAP.get(metric_name, [metric_name])
        for key in candidate_keys:
            if key in data and data[key] is not None:
                try:
                    val = float(data[key])
                    return val, key, True
                except (ValueError, TypeError):
                    continue

        logger.warning(f"  ⚠️ [Telemetry Guard Warning] 지표 '{metric_name}' 후보 키 {candidate_keys} 모두 누락 ➔ 기본값({default_val}) 사용 중")
        return default_val, "MISSING_KEY_FALLBACK", False

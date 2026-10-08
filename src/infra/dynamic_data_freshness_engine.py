"""
Project Meridian V3 — Multi-Tier Dynamic Data Freshness Engine.

Replaces naive uniform 60-minute threshold with domain-specific, multi-tier SLAs:
1. Realtime Stock & ETF Price Ticks (KRX/US Market Hours): Max Age = 3 minutes (180s)
2. Premarket & Futures & VIX / VKOSPI / FX Data: Max Age = 15 minutes (900s)
3. DART Disclosures & Macro Fundamentals (FRED/MOTIE): Max Age = 24 hours (86400s)
"""

import time
import logging
from typing import Dict, Any, Tuple
from datetime import datetime

logger = logging.getLogger("dynamic_data_freshness")

class DynamicDataFreshnessEngine:
    """다단계 동적 데이터 신선도 산출 엔진 (Multi-Tier Data Freshness Engine)."""

    # Data Category SLAs (in seconds)
    SLA_REALTIME_STREAM_SEC = 5        # 5 seconds (1초 라이브 스트리밍 장중 하트비트)
    SLA_PREMARKET_MACRO_SEC = 900      # 15 minutes (장전 선물/VIX/환율)
    SLA_DAILY_FUNDAMENTAL_SEC = 86400  # 24 hours (DART 공시/FRED 매크로)

    @classmethod
    def evaluate_feature_freshness(cls, feature_name: str, last_updated_mtime: float, is_market_open: bool = True) -> Tuple[bool, str, float]:
        """
        특정 데이터 피처별 다단계 신선도 검증.
        
        Returns:
            Tuple[bool, str, float]: (is_fresh, category_name, age_seconds)
        """
        now = time.time()
        age_sec = max(now - last_updated_mtime, 0.0)

        # 1. 실시간 주식/ETF 시세 (Realtime Stock & ETF Ticks)
        if any(kw in feature_name.lower() for kw in ["price", "ohlcv", "tick", "quote", "stck_prpr"]):
            if not is_market_open:
                # 장 마감 후(Closed Market): 전일 마감가 스냅샷은 24시간 이내 정상 인정!
                max_allowed = cls.SLA_DAILY_FUNDAMENTAL_SEC
                category = "Closed Market Final Close Snapshot (Valid Baseline)"
            else:
                max_allowed = cls.SLA_REALTIME_STREAM_SEC
                category = "1-Sec Live Stream Heartbeat (5s SLA)"
            is_fresh = age_sec <= max_allowed
        # 2. VIX / VKOSPI / 환율 / 오버나이트 선물
        elif any(kw in feature_name.lower() for kw in ["vix", "vkospi", "fx", "usdkrw", "futures", "ois"]):
            max_allowed = cls.SLA_PREMARKET_MACRO_SEC
            is_fresh = age_sec <= max_allowed
            category = "Volatility & Macro Feed (15m SLA)"
        # 3. DART 공시 / FRED 금리 / 산업 통계
        else:
            max_allowed = cls.SLA_DAILY_FUNDAMENTAL_SEC
            is_fresh = age_sec <= max_allowed
            category = "Daily Fundamental Feed (24h SLA)"

        if not is_fresh:
            logger.warning(f"  ⛔ [Data Freshness SLA Violation] {feature_name} ({category}): age = {age_sec/60.0:.1f}m > max_allowed = {max_allowed/60.0:.1f}m ➔ STALE")
        else:
            logger.info(f"  🟢 [Data Freshness SLA Pass] {feature_name} ({category}): age = {age_sec/60.0:.1f}m <= max_allowed = {max_allowed/60.0:.1f}m ➔ FRESH")

        return is_fresh, category, age_sec

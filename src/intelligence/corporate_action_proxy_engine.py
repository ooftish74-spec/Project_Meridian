"""
Project Meridian V3 — Corporate Action Proxy Alpha Engine.

Propagates major DART corporate action events (e.g. Treasury Stock Cancellation / 자사주 소각)
from mega-cap sector leaders (SK Hynix 000660, Samsung Electronics 005930) to proxy ETFs (091160 KODEX 반도체, 069500 KODEX 200).

Prevents idle KOFR cash parking when high-impact corporate actions create sector-wide tailwinds.
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger("corporate_action_proxy")

SECTOR_LEADER_PROXY_MAP = {
    "000660": {  # SK Hynix
        "name": "SK하이닉스",
        "proxy_etfs": ["091160", "069500"],  # KODEX 반도체, KODEX 200
        "sector": "semiconductor",
        "weight_in_sector": 0.35,
    },
    "005930": {  # Samsung Electronics
        "name": "삼성전자",
        "proxy_etfs": ["091160", "069500"],
        "sector": "semiconductor",
        "weight_in_sector": 0.45,
    },
}

class CorporateActionProxyEngine:
    """공시 알파 교차 전파 엔진 (Corporate Action Proxy Engine)."""

    @staticmethod
    def evaluate_proxy_boost(dart_features: Dict[str, Any]) -> Dict[str, float]:
        """
        DART 공시 시그널(자사주 소각/취소 dart_buyback >= 0.70) 검출 시 대체 ETF 알파 보스트 산출.
        
        Returns:
            Dict[str, float]: {proxy_ticker: alpha_boost_ev}
        """
        proxy_boosts: Dict[str, float] = {}

        try:
            for leader_ticker, info in SECTOR_LEADER_PROXY_MAP.items():
                leader_dart = dart_features.get(leader_ticker, {})
                buyback_score = float(leader_dart.get("buyback", 0.0) or 0.0)
                composite_score = float(leader_dart.get("composite", 0.0) or 0.0)

                # 자사주 소각/취소 강력 공시 시그널 (buyback >= 0.70 또는 composite >= 0.60)
                if buyback_score >= 0.70 or composite_score >= 0.60:
                    boost_val = 0.025 * info["weight_in_sector"]  # +0.875%~+1.125% EV Boost
                    logger.info(f"  🚀 [CorporateActionProxy] {info['name']} ({leader_ticker}) 대형 공시 호재 감지 (Buyback={buyback_score:.2f}) ➔ 대체 ETF {info['proxy_etfs']} EV Boost +{boost_val:.4f} 전파!")

                    for proxy_etf in info["proxy_etfs"]:
                        proxy_boosts[proxy_etf] = max(proxy_boosts.get(proxy_etf, 0.0), boost_val)

        except Exception as e:
            logger.error(f"  ❌ CorporateActionProxyEngine 연산 실패: {e}")

        return proxy_boosts

"""
Alpha 3: First-Second Global Disparity Sniping Engine (src/streams/s11_high_beta/first_second_disparity_engine.py)
====================================================================================================

09:00:01 KST 개장 1초 시초가 전역 괴리율 수확 정밀 알고리즘.

수학적 모델:
  1. Global Pre-Market Disparity (전역 괴리율):
     Global_Disparity = w_us_semi * ΔSOXX_us + w_cme * ΔCME_futures - w_fx * ΔUSDKRW_ndf - ΔKRX_premarket
  2. Dynamic Disparity Threshold:
     threshold = dynamic_base_threshold * vol_scaling_factor
  3. 09:00:01 KST Sniping Signal:
     Signal = LONG if Global_Disparity > threshold else (SHORT if Global_Disparity < -threshold else NEUTRAL)

Zero-Hardcoding Policy:
  모든 가중치(w_us_semi, w_cme, w_fx) 및 임계치 파라미터는 DynamicConfig ('s11.first_second.*')에서 지연 로드됩니다.
"""

import logging
from typing import Dict, Any, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class FirstSecondDisparityEngine:
    """09:00:01 KST 시초가 전역 괴리율 1초 스나이퍼 엔진."""

    def evaluate_disparity_signal(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """미국 야간 반도체/지수, CME KOSPI 선물, NDF 환율 및 KRX 동호가 갭으로 괴리율 산출.

        Args:
            market_data: 파이프라인 공유 데이터 딕셔너리

        Returns:
            {
                'signal': 'long' | 'short' | 'neutral',
                'disparity_pct': float,
                'confidence': float,
                'target_ticker_type': 'leverage' | 'inverse' | 'semi_inverse',
                'reason': str
            }
        """
        # Zero-Hardcoding 파라미터 지연 로드
        w_us_semi = float(cfg.get('s11.first_second.w_us_semi', 0.40))
        w_cme = float(cfg.get('s11.first_second.w_cme', 0.35))
        w_fx = float(cfg.get('s11.first_second.w_fx', 0.25))
        base_threshold = float(cfg.get('s11.first_second.disparity_threshold_pct', 0.40)) # 0.4%
        min_confidence = float(cfg.get('s11.first_second.min_confidence', 0.80))

        # 시장 데이터 파싱
        soxx_change = float(market_data.get('soxx_change_pct', market_data.get('us_semi_change_pct', 0.0)) or 0.0)
        cme_change = float(market_data.get('cme_futures_change_pct', market_data.get('cme_korea_futures', 0.0)) or 0.0)
        ndf_fx_change = float(market_data.get('ndf_usdkrw_change_pct', 0.0) or 0.0)
        krx_premarket_gap = float(market_data.get('krx_premarket_gap_pct', 0.0) or 0.0)

        # 1. 전역 괴리율 (Global Disparity) 산출
        global_disparity = (
            (w_us_semi * soxx_change) +
            (w_cme * cme_change) -
            (w_fx * ndf_fx_change) -
            krx_premarket_gap
        )

        # 2. 임계값 비교 및 시그널 산출
        signal = 'neutral'
        target_type = 'neutral'
        confidence = 0.0

        if global_disparity >= base_threshold:
            signal = 'long'
            target_type = 'leverage'
            confidence = min(1.0, min_confidence + (global_disparity - base_threshold) * 0.5)
            reason = (
                f"🚀 [09:00:01 개장 1초 롱 스나이퍼] 전역 괴리율 +{global_disparity:.2f}% >= 임계치 +{base_threshold:.2f}% "
                f"(SOXX:{soxx_change:+.1f}%, CME:{cme_change:+.1f}%, NDF:{ndf_fx_change:+.1f}%)"
            )
            logger.info(f"  {reason}")
        elif global_disparity <= -base_threshold:
            signal = 'short'
            target_type = 'inverse'
            confidence = min(1.0, min_confidence + (abs(global_disparity) - base_threshold) * 0.5)
            reason = (
                f"📉 [09:00:01 개장 1초 숏 스나이퍼] 전역 괴리율 {global_disparity:.2f}% <= 임계치 -{base_threshold:.2f}% "
                f"(SOXX:{soxx_change:+.1f}%, CME:{cme_change:+.1f}%, NDF:{ndf_fx_change:+.1f}%)"
            )
            logger.warning(f"  {reason}")
        else:
            reason = f"평시 괴리율 ({global_disparity:+.2f}% < |{base_threshold:.2f}%|)"

        return {
            'signal': signal,
            'disparity_pct': round(global_disparity, 4),
            'confidence': round(confidence, 3),
            'target_ticker_type': target_type,
            'reason': reason
        }

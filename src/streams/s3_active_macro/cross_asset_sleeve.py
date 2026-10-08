"""
Alpha 4: S3 Track C Cross-Asset Macro Sleeve (src/streams/s3_active_macro/cross_asset_sleeve.py)
========================================================================================

미국 상장 대체자산(금, 원유, 구리, 국채, 달러)의 상대 모멘텀 스피레드 및 Flight-to-Quality 신호를 수확하는 엔진.

대체자산 유니버스:
  - Gold: GLD (SPDR Gold Shares) / IAU
  - Crude Oil: USO (United States Oil Fund)
  - Copper: CPER (United States Copper Index Fund)
  - Treasury: TLT (iShares 20+ Year Treasury) / IEF
  - US Dollar: UUP (Invesco DB US Dollar Index Bullish)

수학적 모델:
  1. Cross-Asset Momentum Spread:
     Spread_k = Return_k(N-days) - Return_SPY(N-days)
  2. Conviction Signal:
     LONG if (Spread_k >= dynamic_threshold OR (regime in ['bear','crash'] AND Return_k > 0))

Zero-Hardcoding Policy:
  모든 임계치 및 유니버스 설정은 DynamicConfig ('s3.track_c.*')에서 지연 로드됩니다.
"""

import logging
from typing import Dict, List, Any
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S3CrossAssetSleeve:
    """S3 Track C: 대체자산/원자재/FX 교차 모멘텀 슬리브 엔진."""

    def __init__(self):
        pass

    def generate_track_c_signals(self, regime: str, market_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """대체자산 유니버스 대상 Track C 알파 4 신호 생성.

        Args:
            regime: HMM 레짐 레이블 ('bull', 'caution', 'bear', 'crash')
            market_data: 파이프라인 공유 데이터 딕셔너리

        Returns:
            대체자산 롱 신호 리스트 (미국 전용)
        """
        # Zero-Hardcoding 파라미터 지연 로드
        spread_threshold = float(cfg.get('s3.track_c.spread_threshold', 0.005)) # +0.5%
        min_confidence = float(cfg.get('s3.track_c.min_confidence', 0.75))

        cross_universe = cfg.get('s3.track_c.universe', {
            'GLD': {'name': 'SPDR Gold Shares', 'category': 'gold'},
            'USO': {'name': 'United States Oil Fund', 'category': 'oil'},
            'CPER': {'name': 'United States Copper Index Fund', 'category': 'copper'},
            'TLT': {'name': 'iShares 20+ Year Treasury Bond', 'category': 'treasury'},
            'UUP': {'name': 'Invesco DB US Dollar Index Bullish', 'category': 'dollar'}
        })

        signals = []
        spy_return = float(market_data.get('spy_return_5d', market_data.get('sp500_return_5d', 0.0)) or 0.0)

        for ticker, meta in cross_universe.items():
            key_ret = f"{ticker.lower()}_return_5d"
            asset_ret = float(market_data.get(key_ret, market_data.get('asset_returns', {}).get(ticker, 0.0)) or 0.0)
            spread = asset_ret - spy_return

            is_flight_to_quality = (regime in ['bear', 'crash', 'caution']) and (asset_ret > 0.0)
            is_momentum_outperformance = spread >= spread_threshold

            if is_flight_to_quality or is_momentum_outperformance:
                confidence = min(1.0, min_confidence + abs(spread) * 2.0)
                reason = (
                    f"🛡️ [Alpha 4: Track C Cross-Asset] {meta['name']} ({ticker}) 롱 진입 | "
                    f"Ret5d={asset_ret:+.2%} vs SPY={spy_return:+.2%} (Spread={spread:+.2%}, Regime={regime})"
                )
                logger.info(f"  {reason}")

                sig = {
                    'stream_id': 'S3_C',
                    'ticker': ticker,
                    'name': meta['name'],
                    'direction': 'long',
                    'confidence': round(confidence, 3),
                    'size_pct': round(min(0.20, confidence * 0.15), 4),
                    'strategy': 's3_cross_asset_momentum',
                    'category': meta['category'],
                    'market': 'US',
                    'tax_drag_pct': 0.00, # 통합증거금 가상환전 0원
                    'reason': reason
                }
                signals.append(sig)

        return signals

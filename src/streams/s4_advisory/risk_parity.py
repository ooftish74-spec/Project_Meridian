#!/usr/bin/env python3
"""
Risk Parity Allocator — S4 동적 자산배분
=========================================

Medallion Upgrade #12: Risk Parity + 레짐 Tilt 하이브리드.

전략:
  - Risk Parity: 각 자산의 위험기여도를 균등화
  - Regime Tilt: 레짐에 따른 전술적 비중 조정
  - Hybrid: blend_ratio로 두 방법 혼합

Risk Parity 원리:
  w_i ∝ 1/σ_i  (역변동성 비중)
  각 자산이 포트폴리오 총위험에 동일하게 기여

레짐 Tilt:
  - Bull: 성장 ETF 오버웨이트, 채권 언더웨이트
  - Bear/Crash: 안전자산 오버웨이트
  - DynamicConfig에서 레짐별 기본 비중 읽기

Usage:
    from src.streams.s4_advisory.risk_parity import RiskParityAllocator
    allocator = RiskParityAllocator()
    weights = allocator.compute_weights(
        assets=['kodex_dividend', 'kr_bond', 'gold'],
        vol_history={'kodex_dividend': [...], 'kr_bond': [...], 'gold': [...]},
        regime='bull')
"""

import logging
import math
from typing import Any, Dict, List, Optional

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class RiskParityAllocator:
    """Risk Parity + 레짐 Tilt 하이브리드 자산배분.

    모든 파라미터는 DynamicConfig에서 동적 로드.
    """

    def compute_weights(self, assets: List[str],
                         vol_history: Optional[Dict[str, List[float]]] = None,
                         regime: str = 'caution',
                         regime_base_weights: Optional[Dict[str, float]] = None,
                         ) -> Dict[str, float]:
        """하이브리드 비중 계산: Risk Parity × Regime Tilt.

        Args:
            assets: 자산 식별자 리스트
            vol_history: 자산별 일간 수익률 히스토리 (역변동성 계산용)
            regime: 현재 레짐
            regime_base_weights: 레짐 기반 기본 비중 (없으면 균등)

        Returns:
            자산별 비중 딕셔너리 {'asset_key': weight, ...}
        """
        if not assets:
            return {}

        enabled = cfg.get('s4.risk_parity.enabled', True)
        blend = cfg.get('s4.risk_parity.blend_ratio', 0.50)

        if not enabled or blend <= 0:
            # Risk Parity 비활성: 순수 레짐 Tilt
            return self._normalize_weights(
                regime_base_weights or
                {a: 1.0 / len(assets) for a in assets},
                assets)

        # 1) Risk Parity 비중 (역변동성)
        rp_weights = self._inverse_volatility_weights(assets, vol_history)

        # 2) 레짐 Tilt 비중
        if regime_base_weights:
            tilt_weights = self._normalize_weights(regime_base_weights, assets)
        else:
            tilt_weights = {a: 1.0 / len(assets) for a in assets}

        # 3) 하이브리드: blend * RP + (1 - blend) * Tilt
        hybrid = {}
        for asset in assets:
            rp = rp_weights.get(asset, 0)
            tilt = tilt_weights.get(asset, 0)
            hybrid[asset] = round(blend * rp + (1 - blend) * tilt, 6)

        # 4) 비중 제한 적용
        hybrid = self._apply_weight_constraints(hybrid)

        # 정규화
        total = sum(hybrid.values())
        if total > 0:
            hybrid = {k: round(v / total, 4) for k, v in hybrid.items()}

        return hybrid

    def _inverse_volatility_weights(self, assets: List[str],
                                     vol_history: Optional[Dict[str, List[float]]]
                                     ) -> Dict[str, float]:
        """역변동성 기반 Risk Parity 비중.

        w_i = (1/σ_i) / Σ(1/σ_j)
        """
        lookback = cfg.get('s4.risk_parity.vol_lookback_days', 60)
        vols = {}

        for asset in assets:
            if vol_history and asset in vol_history:
                returns = vol_history[asset][-lookback:]
                if len(returns) >= 5:
                    vol = self._compute_volatility(returns)
                    vols[asset] = max(vol, 1e-8)
                else:
                    vols[asset] = 0.01  # 데이터 부족 → 기본 변동성
            else:
                # 변동성 히스토리 없음 → 자산 유형별 기본값
                vols[asset] = self._default_volatility(asset)

        # 역변동성 가중
        inv_vols = {a: 1.0 / v for a, v in vols.items()}
        total_inv = sum(inv_vols.values())

        if total_inv < 1e-12:
            return {a: 1.0 / len(assets) for a in assets}

        return {a: round(iv / total_inv, 6) for a, iv in inv_vols.items()}

    @staticmethod
    def _compute_volatility(returns: List[float]) -> float:
        """실현 변동성 계산."""
        n = len(returns)
        if n < 2:
            return 0.01

        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        return math.sqrt(variance) if variance > 0 else 1e-6

    @staticmethod
    def _default_volatility(asset: str) -> float:
        """자산 유형별 기본 변동성 (데이터 없을 때).

        채권 < 금 < 배당 ETF < 성장 ETF 순서.
        """
        asset_lower = asset.lower()
        if 'bond' in asset_lower:
            return 0.003  # 채권: 연 5% 변동성 기준
        elif 'gold' in asset_lower:
            return 0.008  # 금: 연 12% 변동성 기준
        elif 'dividend' in asset_lower or 'div' in asset_lower:
            return 0.010  # 배당 ETF: 연 15%
        elif 'covered_call' in asset_lower:
            return 0.009  # 커버드콜: 연 14%
        else:
            return 0.012  # 성장 ETF 등: 연 19%

    def _apply_weight_constraints(self, weights: Dict[str, float]
                                    ) -> Dict[str, float]:
        """비중 제한 적용: min_weight ~ max_weight 클램핑.

        클램핑 후 잔여 비중을 나머지 자산에 재배분.
        """
        min_w = cfg.get('s4.risk_parity.min_weight', 0.05)
        max_w = cfg.get('s4.risk_parity.max_weight', 0.40)

        constrained = {}
        overflow = 0.0

        for asset, w in weights.items():
            if w < min_w:
                overflow += min_w - w
                constrained[asset] = min_w
            elif w > max_w:
                overflow += w - max_w  # will be negative (recovering)
                constrained[asset] = max_w
            else:
                constrained[asset] = w

        # overflow를 나머지 자산에 균등 분배
        unconstrained = [a for a in constrained
                         if min_w < constrained[a] < max_w]
        if unconstrained and abs(overflow) > 1e-8:
            adj = overflow / len(unconstrained)
            for a in unconstrained:
                constrained[a] = max(min_w,
                                     min(max_w, constrained[a] - adj))

        return constrained

    @staticmethod
    def _normalize_weights(weights: Dict[str, float],
                            assets: List[str]) -> Dict[str, float]:
        """비중 정규화 (합계 = 1.0)."""
        filtered = {a: weights.get(a, 0) for a in assets}
        total = sum(filtered.values())
        if total < 1e-12:
            return {a: 1.0 / len(assets) for a in assets}
        return {a: round(v / total, 6) for a, v in filtered.items()}

    def get_diagnostics(self, assets: List[str],
                          vol_history: Optional[Dict[str, List[float]]] = None,
                          regime: str = 'caution') -> Dict:
        """진단 정보 반환 (대시보드/로깅용).

        각 자산의 변동성, RP 비중, 최종 비중 등.
        """
        lookback = cfg.get('s4.risk_parity.vol_lookback_days', 60)
        vols = {}

        for asset in assets:
            if vol_history and asset in vol_history:
                returns = vol_history[asset][-lookback:]
                if len(returns) >= 5:
                    vols[asset] = round(self._compute_volatility(returns), 6)
                else:
                    vols[asset] = round(self._default_volatility(asset), 6)
            else:
                vols[asset] = round(self._default_volatility(asset), 6)

        rp_weights = self._inverse_volatility_weights(assets, vol_history)
        final_weights = self.compute_weights(
            assets, vol_history, regime)

        return {
            'volatilities': vols,
            'rp_weights': rp_weights,
            'final_weights': final_weights,
            'blend_ratio': cfg.get('s4.risk_parity.blend_ratio', 0.50),
            'regime': regime,
            'lookback_days': lookback,
        }

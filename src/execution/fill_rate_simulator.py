#!/usr/bin/env python3
"""
Fill Rate Simulator — 지정가 체결 확률 시뮬레이션
===================================================

백테스트/섀도우 트레이딩의 신뢰도 극대화를 위한 보수적 체결 모델.

핵심 원칙:
  "내가 원하는 가격에 항상 체결된다고 가정하지 않는다."

체결 판정 로직:
  ┌─────────────────────────────────────────────────────┐
  │  매수 (Buy) 지정가:                                   │
  │    limit_price = current_price × (1 - discount_bps) │
  │    체결 조건: next_candle['low'] ≤ limit_price        │
  │                                                      │
  │  매도 (Sell) 지정가:                                  │
  │    limit_price = current_price × (1 + discount_bps) │
  │    체결 조건: next_candle['high'] ≥ limit_price       │
  └─────────────────────────────────────────────────────┘

체결 확률 추정:
  - 과거 N봉의 Low/High 범위를 분석하여 지정가 통과 확률 계산
  - S1 단기 방향성 전략에서 특히 유효

모드 설정 (DynamicConfig):
  execution.fill_sim_order_type: 'limit' (보수적) or 'market' (기존 방식)

Usage:
    from src.execution.fill_rate_simulator import FillRateSimulator
    sim = FillRateSimulator()

    # 단봉 체결 판정
    result = sim.simulate_limit_fill(
        order={'action': 'buy', 'price': 10000},
        next_candle={'open': 10050, 'high': 10100, 'low': 9900, 'close': 10020}
    )
    # → {'filled': True, 'fill_price': 10000.0, 'limit_price': 9995.0, ...}

    # 체결 확률 추정
    prob = sim.estimate_fill_probability(
        order={'action': 'buy', 'price': 10000},
        hist_candles=[{'low': 9800, 'high': 10200}, ...]
    )
    # → {'fill_probability': 0.75, 'n_candles': 20, ...}
"""

import logging
import math
from typing import Dict, List, Optional

try:
    from config.dynamic_config import DynamicConfig
except ImportError as e:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class FillRateSimulator:
    """지정가 체결 확률 시뮬레이터.

    shadow 백테스트 전용 — live/paper에서는 KIS API 체결 결과를 그대로 사용.
    """

    def __init__(self):
        self.order_type = cfg.get('execution.fill_sim_order_type', 'limit')
        self.discount_bps = cfg.get('execution.fill_sim_limit_discount_bps', 5.0)
        self.window_candles = cfg.get('execution.fill_sim_window_candles', 20)

    def _compute_limit_price(self, action: str, current_price: float) -> float:
        """지정가 계산.

        매수: 현재가 - discount_bps (살짝 낮게 — 슬리피지 최소화 시도)
        매도: 현재가 + discount_bps (살짝 높게 — 최적 매도)
        """
        discount = self.discount_bps / 10000
        if action.lower() == 'buy':
            return current_price * (1.0 - discount)
        else:
            return current_price * (1.0 + discount)

    def simulate_limit_fill(
        self,
        order: Dict,
        next_candle: Dict,
    ) -> Dict:
        """다음 봉 Low/High로 지정가 체결 여부 판정.

        매수: next_candle['low'] ≤ limit_price → 체결
        매도: next_candle['high'] ≥ limit_price → 체결

        Args:
            order: {
                'action': 'buy' | 'sell',
                'price': float,          # 현재가 (시그널 가격)
                'limit_price': float,    # (optional) 직접 지정 시
            }
            next_candle: {
                'open': float, 'high': float,
                'low': float,  'close': float
            }

        Returns:
            {
                'filled': bool,
                'fill_price': float,      # 실제 체결가 (limit_price or 시장가)
                'limit_price': float,     # 지정가
                'action': str,
                'fill_mode': str,         # 'limit' or 'market'
                'detail': str,
            }
        """
        action = order.get('action', 'buy').lower()
        current_price = order.get('price', 0.0)

        if current_price <= 0:
            return {
                'filled': False,
                'fill_price': 0.0,
                'limit_price': 0.0,
                'action': action,
                'fill_mode': self.order_type,
                'detail': '가격 없음 — 미체결',
            }

        # market 모드: 항상 100% 체결 (기존 방식)
        if self.order_type == 'market':
            return {
                'filled': True,
                'fill_price': current_price,
                'limit_price': current_price,
                'action': action,
                'fill_mode': 'market',
                'detail': '시장가 — 무조건 체결',
            }

        # limit 모드: 지정가 체결 판정
        limit_price = order.get('limit_price') or self._compute_limit_price(
            action, current_price)

        next_low  = next_candle.get('low',  float('inf'))
        next_high = next_candle.get('high', 0.0)

        if action == 'buy':
            # 매수: 다음 봉 저가가 지정가 이하로 내려오면 체결
            filled = next_low <= limit_price
            fill_price = limit_price if filled else 0.0
            detail = (
                f"Low({next_low:,.0f}) ≤ Limit({limit_price:,.0f}) → 체결"
                if filled else
                f"Low({next_low:,.0f}) > Limit({limit_price:,.0f}) → 미체결"
            )
        else:
            # 매도: 다음 봉 고가가 지정가 이상으로 올라오면 체결
            filled = next_high >= limit_price
            fill_price = limit_price if filled else 0.0
            detail = (
                f"High({next_high:,.0f}) ≥ Limit({limit_price:,.0f}) → 체결"
                if filled else
                f"High({next_high:,.0f}) < Limit({limit_price:,.0f}) → 미체결"
            )

        return {
            'filled': filled,
            'fill_price': round(fill_price, 2),
            'limit_price': round(limit_price, 2),
            'action': action,
            'fill_mode': 'limit',
            'detail': detail,
        }

    def simulate_market_fill(self, order: Dict) -> Dict:
        """시장가 체결 시뮬레이션 (항상 체결, 기존 방식 호환).

        슬리피지는 별도 AdvancedSlippageModel에서 처리.
        """
        action = order.get('action', 'buy').lower()
        price = order.get('price', 0.0)
        return {
            'filled': True,
            'fill_price': price,
            'limit_price': price,
            'action': action,
            'fill_mode': 'market',
            'detail': '시장가 — 무조건 체결',
        }

    def estimate_fill_probability(
        self,
        order: Dict,
        hist_candles: List[Dict],
    ) -> Dict:
        """과거 N봉 데이터로 지정가 체결 확률 추정.

        매수: hist_candles 중 low ≤ limit_price인 봉 비율
        매도: hist_candles 중 high ≥ limit_price인 봉 비율

        Args:
            order: {'action': str, 'price': float}
            hist_candles: [{'low': float, 'high': float}, ...]
                          최신 봉이 마지막 원소

        Returns:
            {
                'fill_probability': float,  # 0.0 ~ 1.0
                'n_candles': int,
                'limit_price': float,
                'action': str,
                'avg_favorable_range': float,  # 유리한 봉의 평균 Low/High 범위
            }
        """
        action = order.get('action', 'buy').lower()
        current_price = order.get('price', 0.0)

        if current_price <= 0 or not hist_candles:
            return {
                'fill_probability': 0.5,  # 데이터 없으면 50% 기본값
                'n_candles': 0,
                'limit_price': 0.0,
                'action': action,
                'avg_favorable_range': 0.0,
                'detail': '데이터 없음 — 50% 기본값',
            }

        window = min(self.window_candles, len(hist_candles))
        recent = hist_candles[-window:]

        limit_price = self._compute_limit_price(action, current_price)

        filled_count = 0
        favorable_ranges = []

        for candle in recent:
            if action == 'buy':
                low = candle.get('low', float('inf'))
                if low <= limit_price:
                    filled_count += 1
                    # 유리한 범위: 얼마나 깊이 내려왔는지
                    favorable_ranges.append((limit_price - low) / limit_price)
            else:
                high = candle.get('high', 0.0)
                if high >= limit_price:
                    filled_count += 1
                    favorable_ranges.append((high - limit_price) / limit_price)

        prob = filled_count / window if window > 0 else 0.5
        avg_range = (sum(favorable_ranges) / len(favorable_ranges)
                     if favorable_ranges else 0.0)

        return {
            'fill_probability': round(prob, 4),
            'n_candles': window,
            'limit_price': round(limit_price, 2),
            'action': action,
            'avg_favorable_range': round(avg_range * 10000, 2),  # bps 단위
            'detail': f"{filled_count}/{window}봉 체결 조건 충족",
        }

    def simulate_with_slippage(
        self,
        order: Dict,
        next_candle: Dict,
        slippage_bps: float = 0.0,
    ) -> Dict:
        """Fill 시뮬레이션 + 슬리피지 통합.

        체결 시 슬리피지까지 반영한 최종 체결가를 반환.
        AdvancedSlippageModel과 연계하여 사용.

        Args:
            order:        주문
            next_candle:  다음 봉 OHLC
            slippage_bps: AdvancedSlippageModel에서 계산된 슬리피지 (bps)

        Returns:
            fill 결과 + 슬리피지 반영 final_fill_price
        """
        result = self.simulate_limit_fill(order, next_candle)

        if result['filled'] and slippage_bps > 0:
            action = order.get('action', 'buy').lower()
            slip_rate = slippage_bps / 10000
            base_fill = result['fill_price']

            if action == 'buy':
                # 매수: 지정가 체결 후 시장충격 추가 비용
                result['final_fill_price'] = round(
                    base_fill * (1.0 + slip_rate), 2)
            else:
                # 매도: 지정가 체결 후 시장충격 차감
                result['final_fill_price'] = round(
                    base_fill * (1.0 - slip_rate), 2)

            result['slippage_bps_applied'] = round(slippage_bps, 2)
        else:
            result['final_fill_price'] = result.get('fill_price', 0.0)
            result['slippage_bps_applied'] = 0.0

        return result

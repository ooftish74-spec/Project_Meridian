"""
Project Meridian — Citadel-Style Internal Netting Engine
==========================================================
동일 종목에 대한 다중 스트림 신호 수집 시 
전략 가중치 w_j 및 확신도 c_j 기반 순 노출도 수량 산출 및 수수료/슬리피지 절감 텔레메트리 기록.
"""

import logging
from typing import List, Dict, Any, Tuple, Optional
from src.orchestration.intent_schema import SignalIntent

logger = logging.getLogger(__name__)

class NettingEngine:
    """다중 스트림 내부 주문 상쇄(Internal Netting) 엔진."""

    def __init__(self, estimated_fee_rate: float = 0.0015, estimated_slippage_rate: float = 0.0010):
        self.estimated_fee_rate = estimated_fee_rate
        self.estimated_slippage_rate = estimated_slippage_rate

    def process_intents(
        self,
        intents: List[SignalIntent],
        portfolio_value: float,
        ticker_prices: Dict[str, float],
        stream_weights: Optional[Dict[str, float]] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        신호 의도(SignalIntent) 리스트를 수집하여 종목별 순 노출 주문(Netted Orders)으로 집계.

        Args:
            intents: 수신된 SignalIntent 개체 리스트
            portfolio_value: 총 포트폴리오 자산 가치 (KRW)
            ticker_prices: 종목별 현재가 딕셔너리
            stream_weights: 스트림별 할당 가중치 (기본값: 균등 배분)

        Returns:
            netted_orders: [
                {
                    'ticker': str,
                    'direction': int (+1 or -1 or 0),
                    'net_shares': int,
                    'net_amount': float,
                    'net_weight': float,
                    'contributing_streams': List[str],
                    'is_netted_out': bool
                }
            ]
            telemetry: {
                'total_gross_orders': int,
                'total_net_orders': int,
                'saved_fee_krw': float,
                'saved_slippage_krw': float,
                'gross_volume_krw': float,
                'net_volume_krw': float,
                'netting_efficiency_pct': float
            }
        """
        if not intents:
            return [], {
                'total_gross_orders': 0,
                'total_net_orders': 0,
                'saved_fee_krw': 0.0,
                'saved_slippage_krw': 0.0,
                'gross_volume_krw': 0.0,
                'net_volume_krw': 0.0,
                'netting_efficiency_pct': 0.0
            }

        if stream_weights is None:
            unique_streams = list(set(i.stream_id for i in intents))
            stream_weights = {s: 1.0 / len(unique_streams) for s in unique_streams}

        grouped_by_ticker: Dict[str, List[SignalIntent]] = {}
        for intent in intents:
            grouped_by_ticker.setdefault(intent.ticker, []).append(intent)

        netted_orders = []
        gross_volume_krw = 0.0
        net_volume_krw = 0.0
        saved_volume_krw = 0.0
        total_gross_orders_count = len(intents)

        for ticker, ticker_intents in grouped_by_ticker.items():
            price = ticker_prices.get(ticker, 0.0)
            if price <= 0:
                logger.warning(f"[NettingEngine] {ticker} 가격 무효 ({price}) → 스킵")
                continue

            net_exposure_krw = 0.0
            contributing_streams = []

            for intent in ticker_intents:
                sw = stream_weights.get(intent.stream_id, 1.0)
                # 요청 금액 = Portfolio Value * Stream Weight * Target Weight * Conviction
                intent_amount_krw = portfolio_value * sw * intent.target_weight * intent.conviction
                signed_amount_krw = intent.direction * intent_amount_krw
                net_exposure_krw += signed_amount_krw
                gross_volume_krw += intent_amount_krw
                contributing_streams.append(intent.stream_id)

            net_direction = 1 if net_exposure_krw > 0 else (-1 if net_exposure_krw < 0 else 0)
            abs_net_amount = abs(net_exposure_krw)
            net_shares = int(abs_net_amount // price) if price > 0 else 0
            actual_net_amount = net_shares * price

            # 상쇄 여부 판정
            is_netted_out = (net_shares == 0)
            if not is_netted_out:
                net_volume_krw += actual_net_amount
            
            # 상쇄로 인해 절감된 거래 대금
            ticker_gross_amount = sum(
                portfolio_value * stream_weights.get(i.stream_id, 1.0) * i.target_weight * i.conviction
                for i in ticker_intents
            )
            saved_volume_krw += max(0.0, ticker_gross_amount - actual_net_amount)

            netted_orders.append({
                'ticker': ticker,
                'direction': net_direction,
                'net_shares': net_shares,
                'net_amount': round(actual_net_amount, 2),
                'net_weight': round(actual_net_amount / portfolio_value, 4) if portfolio_value > 0 else 0.0,
                'contributing_streams': list(set(contributing_streams)),
                'is_netted_out': is_netted_out,
                'price': price
            })

        saved_fee_krw = saved_volume_krw * self.estimated_fee_rate
        saved_slippage_krw = saved_volume_krw * self.estimated_slippage_rate
        efficiency = (saved_volume_krw / gross_volume_krw * 100.0) if gross_volume_krw > 0 else 0.0

        telemetry = {
            'total_gross_orders': total_gross_orders_count,
            'total_net_orders': len([o for o in netted_orders if not o['is_netted_out']]),
            'saved_fee_krw': round(saved_fee_krw, 2),
            'saved_slippage_krw': round(saved_slippage_krw, 2),
            'gross_volume_krw': round(gross_volume_krw, 2),
            'net_volume_krw': round(net_volume_krw, 2),
            'saved_volume_krw': round(saved_volume_krw, 2),
            'netting_efficiency_pct': round(efficiency, 2)
        }

        logger.info(f"[NettingEngine] 상쇄 완료: Gross {total_gross_orders_count}건({gross_volume_krw:,.0f}원) → Net {telemetry['total_net_orders']}건({net_volume_krw:,.0f}원) | 절감수수료: ₩{saved_fee_krw:,.0f}")
        return netted_orders, telemetry

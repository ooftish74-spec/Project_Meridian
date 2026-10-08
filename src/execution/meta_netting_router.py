"""
MetaNettingRouter — 월가 헤지펀드 스탠다드 중앙화 포트폴리오 넷팅 및 라우팅 모듈
================================================================================

핵심 기능:
1. Cross-Stream Signal Netting: 여러 전략 스트림(S0, S1, S2, S3, S4)의 목표 비중을 종목별로 통합 합산.
   - S1이 100주 매도를 요청하고 S3가 100주 매수를 요청 시 중앙 오케스트레이터에서 넷팅(Netting)되어 0주로 상쇄.
2. Hysteresis Band (히스테리시스 밴드 2.0%):
   - 목표 비중 변화가 계좌 총 NAV의 2.0% 미만일 경우 매매를 수프레션(Suppress)하여 잔파도 노이즈 거래 차단.
3. Friction-Aware Net EV Filter:
   - 예상 알파 수익이 매매 마찰 비용(세금 0.18% + 슬리피지 0.10% = 0.28%)보다 작은 시그널 기각.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class MetaNettingRouter:
    """월가 퀀트 멀티-매니저 포트폴리오 넷팅 라우터."""

    def __init__(self, hysteresis_pct: float = 0.02, friction_bps: float = 28.0):
        self.hysteresis_pct = hysteresis_pct  # 2.0% NAV hysteresis threshold
        self.friction_pct = friction_bps / 10000.0  # 0.28% round-trip friction

    def resolve_signals(
        self,
        stream_signals: List[Dict],
        current_positions: List[Dict],
        total_nav: float
    ) -> List[Dict]:
        """다중 스트림 시그널을 합산 넷팅하여 최종 정제된 매매 주문 리스트를 생성.

        Args:
            stream_signals: 각 스트림에서 생성된 원시 시그널 리스트
            current_positions: 계좌 내 실시간 보유 포지션 리스트
            total_nav: 계좌 총 순자산 (KRW/USD)

        Returns:
            중앙 넷팅 및 히스테리시스 밴드를 통과한 최종 실행 시그널 리스트
        """
        if not stream_signals:
            return []

        # 1. Ticker별 목표 비중 (Target Weight Vector) 합산
        target_weights: Dict[str, float] = {}
        signal_metadata: Dict[str, Dict] = {}

        for sig in stream_signals:
            ticker = sig.get('ticker', '')
            if not ticker:
                continue
            direction = sig.get('direction', 'long').lower()
            size_pct = float(sig.get('size_pct', sig.get('weight', 0.1)))
            weight_delta = size_pct if direction in ('long', 'buy') else -size_pct

            target_weights[ticker] = target_weights.get(ticker, 0.0) + weight_delta
            if ticker not in signal_metadata:
                signal_metadata[ticker] = sig

        # 2. 현재 포지션 비중 (Current Weight Vector) 파싱
        current_weights: Dict[str, float] = {}
        for pos in current_positions:
            ticker = pos.get('ticker', pos.get('ovrs_pdno', ''))
            val = float(pos.get('market_value', pos.get('evlu_amt', 0)))
            if total_nav > 0:
                current_weights[ticker] = val / total_nav

        # 3. Cross-Stream Netting & Hysteresis Band 필터링
        resolved_orders: List[Dict] = []
        for ticker, t_weight in target_weights.items():
            c_weight = current_weights.get(ticker, 0.0)
            delta_weight = t_weight - c_weight

            # [Rule 1] Hysteresis Band: 2.0% 미만 미세 변화는 주문 무시
            if abs(delta_weight) < self.hysteresis_pct:
                logger.info(
                    f"  🛡️ [MetaNettingRouter] {ticker}: Delta ({delta_weight:+.2%}) < Hysteresis Band ({self.hysteresis_pct:.1%}) -> Order Suppressed"
                )
                continue

            meta = signal_metadata.get(ticker, {})
            confidence = float(meta.get('confidence', 0.6))

            # [Rule 2] Net EV Filter: Expected Return < Friction (0.28%) 경우 차단
            expected_alpha = confidence * 0.05  # 5% baseline alpha * confidence
            if expected_alpha < self.friction_pct:
                logger.info(
                    f"  🛡️ [MetaNettingRouter] {ticker}: Net EV ({expected_alpha:.2%}) < Friction ({self.friction_pct:.2%}) -> Order Suppressed"
                )
                continue

            action = 'BUY' if delta_weight > 0 else 'SELL'
            order_weight = abs(delta_weight)

            resolved_orders.append({
                'ticker': ticker,
                'name': meta.get('name', ticker),
                'action': action,
                'net_weight_pct': round(order_weight, 4),
                'target_weight_pct': round(t_weight, 4),
                'current_weight_pct': round(c_weight, 4),
                'confidence': confidence,
                'stream': 'NETTED',
                'reason': f"Cross-Stream Netting (Delta={delta_weight:+.2%})",
                'tp_pct': meta.get('tp_pct', 5.0),
                'sl_pct': meta.get('sl_pct', -3.0)
            })

        logger.info(
            f"  🏛️ [MetaNettingRouter] Total Raw Signals ({len(stream_signals)}) -> Netted Orders ({len(resolved_orders)})"
        )
        return resolved_orders

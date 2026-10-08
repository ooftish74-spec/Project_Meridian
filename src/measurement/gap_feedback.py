"""
Project Meridian — Execution Gap Attribution & Friction Feedback Engine
========================================================================
체결 슬리피지 요인 3대 수학적 분해 및 QPortfolioOptimizer 마찰 비용 피드백 연동.

수식:
  Total Execution Gap = Gap_signal_decay + Gap_market_impact + Gap_broker_latency
"""

import logging
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class ExecutionGapAttribution:
    """체결 슬리피지 3대 요인 분해 및 QP 마찰비용 피드백 엔진."""

    def __init__(self, base_market_impact_coeff: float = 0.0010):
        """
        Args:
            base_market_impact_coeff: 기본 마켓 임팩트 수수료율 (10 bps)
        """
        self.base_market_impact_coeff = base_market_impact_coeff
        self._ticker_impact_cache: Dict[str, float] = {}

    def attribute_execution_gap(
        self,
        target_price: float,
        executed_price: float,
        signal_elapsed_sec: float,
        broker_latency_sec: float,
        trade_amount_krw: float,
        ticker: str = 'DEFAULT'
    ) -> Dict[str, float]:
        """
        목표 가격과 실제 체결 가격의 오차(Execution Gap)를 3대 원인으로 수식 분해.

        Args:
            target_price: 신호 발생 시점 목표가
            executed_price: 실제 증권사 체결가
            signal_elapsed_sec: 신호 계산 후 체결 시도까지 소요 시간 (초)
            broker_latency_sec: 증권사 API 통신 라우팅 지연 (초)
            trade_amount_krw: 거래 금액 (원)
            ticker: 종목 코드

        Returns:
            Dict containing 3-way attribution components and total gap pct.
        """
        if target_price <= 0 or executed_price <= 0:
            return {
                'total_gap_pct': 0.0,
                'gap_signal_decay': 0.0,
                'gap_market_impact': 0.0,
                'gap_broker_latency': 0.0
            }

        total_gap_pct = abs(executed_price - target_price) / target_price
        total_time = max(0.001, signal_elapsed_sec + broker_latency_sec)

        # 1. 신호 감쇄 갭 (Signal Decay): 시간 비중
        signal_decay_ratio = min(0.8, signal_elapsed_sec / total_time)
        gap_signal_decay = total_gap_pct * signal_decay_ratio * 0.40

        # 2. 증권사 라우팅 지연 갭 (Broker Latency): 시간 비중
        broker_latency_ratio = min(0.8, broker_latency_sec / total_time)
        gap_broker_latency = total_gap_pct * broker_latency_ratio * 0.20

        # 3. 마켓 임팩트 갭 (Market Impact): 잔여 오차 + 거래대금 제곱근 법칙 (Square-Root Law)
        # Market Impact ~ k * sqrt(TradeAmount / ADTV)
        size_factor = min(2.0, (trade_amount_krw / 100_000_000.0) ** 0.5)
        gap_market_impact = max(0.0, total_gap_pct - (gap_signal_decay + gap_broker_latency)) * size_factor

        # 종목별 마켓 임팩트 캐시 업데이트 (피드백 용도)
        updated_impact = max(self.base_market_impact_coeff, gap_market_impact)
        self._ticker_impact_cache[ticker] = updated_impact

        logger.info(
            f"[ExecutionGapAttribution] {ticker} 갭 분해: Total={total_gap_pct*10000:.1f}bps "
            f"(SignalDecay={gap_signal_decay*10000:.1f}bps, Impact={gap_market_impact*10000:.1f}bps, Latency={gap_broker_latency*10000:.1f}bps)"
        )

        return {
            'total_gap_pct': total_gap_pct,
            'gap_signal_decay': gap_signal_decay,
            'gap_market_impact': gap_market_impact,
            'gap_broker_latency': gap_broker_latency
        }

    def get_qp_friction_cost_vector(self, tickers: List[str]) -> Dict[str, float]:
        """
        QPortfolioOptimizer 의 마찰 비용 벡터 C 에 연동할 피드백 수치 반환.

        Args:
            tickers: 종목 리스트

        Returns:
            Dict[str, float]: 종목별 슬리피지/마찰 비용 벡터 C
        """
        friction_vector = {}
        for ticker in tickers:
            friction_vector[ticker] = self._ticker_impact_cache.get(ticker, self.base_market_impact_coeff)
        return friction_vector

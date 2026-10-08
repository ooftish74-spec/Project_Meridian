"""
Project Meridian — Historical Scenario Stress Tester Engine
============================================================
역사적/가상 폭락 시나리오(2008 리만, 2020 코로나, 2024 엔캐리) 충격 매트릭스 시뮬레이터.

수식:
  Delta NAV_shock = sum_i (w_i * beta_i * Delta S_scenario)
"""

import numpy as np
import logging
from typing import Dict, List, Any, Tuple, Optional

logger = logging.getLogger(__name__)

class HistoricalScenarioStressTester:
    """역사적 시나리오 스트레스 테스트 시뮬레이터."""

    SCENARIOS = {
        '2008_LEHMAN_COLLAPSE': {
            'description': '2008년 리만 브라더스 파산 충격 (1일 시장 -8.0% 폭락)',
            'market_drop_pct': -0.080,
            'vol_spike_factor': 2.50
        },
        '2020_COVID_PANIC': {
            'description': '2020년 코로나 락다운 패닉 (1일 시장 -12.0% 폭락)',
            'market_drop_pct': -0.120,
            'vol_spike_factor': 3.00
        },
        '2024_YEN_CARRY_UNWIND': {
            'description': '2024년 8월 엔캐리 트레이드 청산 충격 (1일 시장 -6.0% 폭락)',
            'market_drop_pct': -0.060,
            'vol_spike_factor': 1.80
        }
    }

    def __init__(self, max_stress_loss_limit_pct: float = 0.15):
        """
        Args:
            max_stress_loss_limit_pct: 시나리오 스트레스 테스트 시 허용 가능한 최대 손실률 (15%)
        """
        self.max_stress_loss_limit_pct = max_stress_loss_limit_pct

    def simulate_scenario_shocks(
        self,
        portfolio_weights: Dict[str, float],
        portfolio_value: float,
        ticker_betas: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        현재 포트폴리오에 3대 역사적 폭락 시나리오 가상 충격 적용.

        Args:
            portfolio_weights: {ticker: weight} 비중 딕셔너리
            portfolio_value: 현재 NAV (원)
            ticker_betas: {ticker: beta} 종목별 시장 베터 값 (기본값 1.0)

        Returns:
            Dict containing scenario results and max loss assessment.
        """
        betas = ticker_betas or {}
        scenario_results = {}
        worst_scenario_name = ""
        worst_loss_pct = 0.0
        worst_loss_krw = 0.0

        for name, scenario in self.SCENARIOS.items():
            market_drop = scenario['market_drop_pct']
            total_port_drop = 0.0

            for ticker, weight in portfolio_weights.items():
                beta = betas.get(ticker, 1.0)
                # PnL impact = weight * beta * market_drop
                total_port_drop += weight * beta * market_drop

            simulated_loss_pct = abs(min(0.0, total_port_drop))
            simulated_loss_krw = portfolio_value * simulated_loss_pct

            scenario_results[name] = {
                'description': scenario['description'],
                'market_drop_pct': market_drop,
                'simulated_loss_pct': simulated_loss_pct,
                'simulated_loss_krw': simulated_loss_krw,
                'exceeds_limit': simulated_loss_pct > self.max_stress_loss_limit_pct
            }

            if simulated_loss_pct > worst_loss_pct:
                worst_loss_pct = simulated_loss_pct
                worst_loss_krw = simulated_loss_krw
                worst_scenario_name = name

        stress_test_passed = worst_loss_pct <= self.max_stress_loss_limit_pct
        suggested_scaling = min(1.0, self.max_stress_loss_limit_pct / (worst_loss_pct + 1e-9))

        logger.info(
            f"[HistoricalScenarioStressTester] 최악 시나리오={worst_scenario_name} "
            f"(예상 손실={worst_loss_pct*100:.2f}%, ₩{worst_loss_krw:,.0f}) -> Passed={stress_test_passed}"
        )

        return {
            'stress_test_passed': stress_test_passed,
            'worst_scenario_name': worst_scenario_name,
            'worst_loss_pct': worst_loss_pct,
            'worst_loss_krw': worst_loss_krw,
            'suggested_scaling_factor': suggested_scaling,
            'scenario_details': scenario_results
        }

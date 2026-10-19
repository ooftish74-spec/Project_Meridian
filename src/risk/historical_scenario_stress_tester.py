"""
Project Meridian — Historical Scenario Stress Tester Engine
============================================================
역사적 폭락(2008 리만, 2020 코로나, 2024 엔캐리) 및 폭등/숏스퀴즈(2020 V-바운스, 2023 AI 감마스퀴즈)
양방향 시나리오 충격 매트릭스 시뮬레이터.

수식:
  ΔNAV_shock(s) = Σ_i (w_i * β_i * ΔS_s)
  Loss_pct(s)   = max(0, -ΔNAV_shock(s))
"""

try:
    from src.params import pget
except Exception:
    def pget(name, fallback, ctx=None):
        return fallback

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HistoricalScenarioStressTester:
    """역사적 폭락 및 폭등(Short-Squeeze) 양방향 시나리오 스트레스 테스트 시뮬레이터."""

    SCENARIOS = {
        '2008_LEHMAN_COLLAPSE': {
            'description': '2008년 리만 브라더스 파산 충격 (1일 시장 -8.0% 폭락)',
            'market_drop_pct': -0.080,
            'vol_spike_factor': 2.50,
        },
        '2020_COVID_PANIC': {
            'description': '2020년 코로나 락다운 패닉 (1일 시장 -12.0% 폭락)',
            'market_drop_pct': -0.120,
            'vol_spike_factor': 3.00,
        },
        '2024_YEN_CARRY_UNWIND': {
            'description': '2024년 8월 엔캐리 트레이드 청산 충격 (1일 시장 -6.0% 폭락)',
            'market_drop_pct': -0.060,
            'vol_spike_factor': 1.80,
        },
    }

    UPSIDE_SURGE_SCENARIOS = {
        '2020_V_BOUNCE_LIQUIDITY_SURGE': {
            'description': '2020년 3월 유동성 공급 V자 폭등 (1일 시장 +9.5% 급등 / 인버스 숏스퀴즈)',
            'market_drop_pct': +0.095,
            'vol_spike_factor': 0.75,
        },
        '2023_AI_GAMMA_SQUEEZE_SURGE': {
            'description': '2023년 AI/반도체 감마 스퀴즈 폭등 (1일 시장 +7.5% 급등)',
            'market_drop_pct': +0.075,
            'vol_spike_factor': 0.85,
        },
    }

    def __init__(self, max_stress_loss_limit_pct: float = 0.15):
        self.max_stress_loss_limit_pct = float(max_stress_loss_limit_pct)

    @classmethod
    def resolve_ticker_beta(cls, ticker: str, ticker_betas: Optional[Dict[str, float]] = None) -> float:
        """종목별 명시적 베타 또는 TickerMaster(L0) 레버리지/인버스/파킹 베타 조회."""
        if ticker_betas and ticker in ticker_betas:
            return float(ticker_betas[ticker])
        tk_up = str(ticker).upper()
        try:
            from src.utils.dynamic_quant_engine import get_quant_engine
            if tk_up in get_quant_engine().get_cash_equivalent_tickers():
                return 0.0
        except Exception:
            pass
        try:
            from src.data_collection.ticker_master import get_ticker_master
            lev = float(get_ticker_master().leverage(tk_up))
            if lev != 1.0:
                return lev
        except Exception:
            pass
        return 1.0

    def simulate_scenario_shocks(
        self,
        portfolio_weights: Dict[str, float],
        portfolio_value: float,
        ticker_betas: Optional[Dict[str, float]] = None,
        include_surge_scenarios: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """현재 포트폴리오에 역사적 폭락 및 폭등(인버스 숏스퀴즈) 시나리오 충격 적용."""
        betas = ticker_betas or {}
        scenario_results = {}
        worst_scenario_name = ''
        worst_loss_pct = 0.0
        worst_loss_krw = 0.0

        # 포트폴리오의 순 베타가 음수(인버스 헤지 우세)이거나 명시 요청 시 상방 폭등 시나리오도 함께 검증
        net_beta_exposure = sum(
            float(w) * self.resolve_ticker_beta(tk, betas)
            for tk, w in (portfolio_weights or {}).items()
        )
        run_surge = bool(include_surge_scenarios) if include_surge_scenarios is not None else (net_beta_exposure < 0.0)

        active_suite = dict(self.SCENARIOS)
        if run_surge:
            active_suite.update(self.UPSIDE_SURGE_SCENARIOS)

        for name, scenario in active_suite.items():
            market_move = float(scenario['market_drop_pct'])
            total_port_return = 0.0

            for ticker, weight in (portfolio_weights or {}).items():
                beta = self.resolve_ticker_beta(ticker, betas)
                total_port_return += float(weight) * beta * market_move

            simulated_loss_pct = abs(min(0.0, total_port_return))
            simulated_loss_krw = float(portfolio_value) * simulated_loss_pct

            scenario_results[name] = {
                'description': scenario['description'],
                'market_drop_pct': market_move,
                'simulated_return_pct': round(total_port_return, 6),
                'simulated_loss_pct': simulated_loss_pct,
                'simulated_loss_krw': simulated_loss_krw,
                'exceeds_limit': simulated_loss_pct > self.max_stress_loss_limit_pct,
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
            'net_beta_exposure': round(net_beta_exposure, 4),
            'suggested_scaling_factor': suggested_scaling,
            'scenario_details': scenario_results,
        }

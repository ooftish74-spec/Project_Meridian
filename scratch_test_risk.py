from src.risk.exposure_orchestrator import ExposureOrchestrator
from src.risk.portfolio_risk_budget import PortfolioRiskBudget
import logging
from config.dynamic_config import DynamicConfig

logging.basicConfig(level=logging.INFO)

print("\n--- Test 1: Exposure Orchestrator Soft-Landing (High Risk) ---")
eo = ExposureOrchestrator()
sentiment_high_risk = {'vix': 50.0, 'options_skew': 4.0, 'ois': 15.0}
comp_high = eo.calculate(sentiment=sentiment_high_risk)
print(f"Target Multiplier: {comp_high.get('target_exposure', 'N/A')}")
print(f"Reasons: {comp_high.get('reasons', [])}")

print("\n--- Test 2: Portfolio Risk Budget (High Risk) ---")
prb = PortfolioRiskBudget()
portfolio_high_risk = {
    'nav': 1000000,
    'market_data': {'vix': 50.0, 'regime': 'bear'},
    'streams': {'S2': {'exposure': 100000}, 'S4': {'exposure': 100000}}
}
budget_high = prb.compute_stream_budget(portfolio_high_risk)
print(f"Satellite Max Pct: {budget_high.get('max_satellite_pct')}")
print(f"Core Max Pct: {budget_high.get('max_core_pct')}")

print("\n--- Test 3: Portfolio Risk Budget (Low Risk / Bull) ---")
portfolio_low_risk = {
    'nav': 1000000,
    'market_data': {'vix': 12.0, 'regime': 'bull'},
    'streams': {'S2': {'exposure': 100000}, 'S4': {'exposure': 100000}}
}
budget_low = prb.compute_stream_budget(portfolio_low_risk)
print(f"Satellite Max Pct: {budget_low.get('max_satellite_pct')}")
print(f"Core Max Pct: {budget_low.get('max_core_pct')}")


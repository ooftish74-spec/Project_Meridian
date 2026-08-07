import sys
import logging
sys.path.append('.')
from src.intelligence.macro_feature_integrator import run_macro_integration
from src.intelligence.regime_engine import RegimeEngine

logging.basicConfig(level=logging.INFO)

print("\n--- 1. Testing MacroFeatureIntegrator ---")
result = run_macro_integration()
features = result.get('macro_features', {})
print("\nGenerated Features:")
for k, v in features.items():
    if k.startswith('fund_'):
        print(f"  {k}: {v}")

print("\n--- 2. Testing RegimeEngine ---")
re = RegimeEngine()
regime = re.detect()
print(f"\nFinal Regime Result:")
for k, v in regime.items():
    print(f"  {k}: {v}")

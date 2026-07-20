import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.risk.exposure_orchestrator import ExposureOrchestrator

def test():
    eo = ExposureOrchestrator()
    print("=== Test 1: Flash Crash Override (Stress Low) ===")
    sentiment_1 = {
        'regime': 'crash',
        'crash_type': 'flash_crash',
        'cross_asset_stress': 0.04,
        'vkospi': 22.0,
        'kospi_ma20_dist': -10.0,
        's3_avg_confidence': 0.60
    }
    res1 = eo.calculate(sentiment=sentiment_1)
    print(f"Target Exposure: {res1['target_exposure']:.3f} | Reason: {res1['reason']}")
    print(f"Components: {res1['components']['regime']}")
    
    print("\n=== Test 2: Real Crash (Stress High) ===")
    sentiment_2 = {
        'regime': 'crash',
        'crash_type': 'flash_crash',
        'cross_asset_stress': 0.50,
        'vkospi': 22.0,
        'kospi_ma20_dist': -10.0,
        's3_avg_confidence': 0.60
    }
    res2 = eo.calculate(sentiment=sentiment_2)
    print(f"Target Exposure: {res2['target_exposure']:.3f} | Reason: {res2['reason']}")
    print(f"Components: {res2['components']['regime']}")

if __name__ == '__main__':
    test()

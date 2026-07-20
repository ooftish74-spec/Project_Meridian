import json
import numpy as np

def _compute_vol_surface_multiplier(vix: float, skew: float, ois: float) -> float:
    """
    Vol-Surface Continuous Melting Function
    VIX, Skew, OIS 값을 기반으로 노출도(Exposure Multiplier) 산출.
    출력값: 0.1 ~ 1.0 (10% ~ 100%)
    """
    # 1. Base Score calculation using sigmoid (logistic) mapping
    # VIX: 15 (normal) -> ~1.0, 30 (high) -> ~0.5, 45 (extreme) -> ~0.1
    # We use a logistic curve: 1 / (1 + exp(k * (x - x0)))
    k_vix = 0.2
    x0_vix = 30.0
    vix_score = 1.0 / (1.0 + np.exp(k_vix * (vix - x0_vix)))
    
    # Skew: 0 (normal) -> ~1.0, 1.5 (high put demand) -> 0.5, 3.5 -> ~0.1
    k_skew = 1.5
    x0_skew = 1.5
    skew_score = 1.0 / (1.0 + np.exp(k_skew * (skew - x0_skew)))
    
    # OIS: 0.5% (normal) -> ~1.0, 1.2% (stress) -> ~0.5, 2.0% (crunch) -> ~0.1
    k_ois = 4.0
    x0_ois = 1.2
    ois_score = 1.0 / (1.0 + np.exp(k_ois * (ois - x0_ois)))
    
    # Combine scores (weighted average or geometric mean)
    # Using minimum or geometric mean ensures that one extreme metric drags down the whole exposure
    raw_multiplier = min(vix_score, skew_score, ois_score)
    
    # Map to [0.1, 1.0]
    final_multiplier = max(0.1, min(1.0, raw_multiplier))
    return float(final_multiplier)

print(_compute_vol_surface_multiplier(20, 0.5, 0.5))
print(_compute_vol_surface_multiplier(30, 1.5, 1.2))
print(_compute_vol_surface_multiplier(40, 3.0, 1.8))

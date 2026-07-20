"""[Phase 75] 거친 경로 이론 Level-2 Truncated Signature (Pure NumPy)."""
from __future__ import annotations
import logging
import numpy as np
logger = logging.getLogger(__name__)

def compute_signature_level2(path: np.ndarray) -> np.ndarray:
    if path.ndim == 1:
        path = path.reshape(-1, 1)
    T, d = path.shape
    if T < 2:
        return np.zeros(d + d * d)
    level1 = path[-1] - path[0]
    level2 = np.zeros((d, d))
    acc = np.zeros((d, d))
    for t in range(1, T):
        dx = path[t] - path[t - 1]
        acc += np.outer(path[t - 1] - path[0], dx)
        level2 += acc + 0.5 * np.outer(dx, dx)
    return np.concatenate([level1, level2.flatten()])

def extract_signature_features(prices: np.ndarray, volumes: np.ndarray, windows=(5, 10, 20)) -> dict:
    result = {}
    for w in windows:
        try:
            n = min(len(prices), len(volumes), w)
            p = prices[-n:].astype(float)
            v = volumes[-n:].astype(float)
            p_std = p.std() if p.std() > 1e-9 else 1.0
            v_std = v.std() if v.std() > 1e-9 else 1.0
            p = (p - p.mean()) / p_std
            v = (v - v.mean()) / v_std
            path = np.column_stack([p, v])
            sig = compute_signature_level2(path)
            result[f'sig{w}_level1_p'] = float(sig[0])
            result[f'sig{w}_level1_v'] = float(sig[1])
            result[f'sig{w}_area']     = float(sig[3] - sig[4]) / 2.0
            result[f'sig{w}_cross']    = float(sig[3] + sig[4]) / 2.0
            result[f'sig{w}_mom_qual'] = float(sig[0]**2 - abs(sig[3] - sig[4]))
        except Exception as e:
            logger.error(f'sig w={w}: {e}', exc_info=True)
            for k in [f'sig{w}_level1_p', f'sig{w}_level1_v', f'sig{w}_area', f'sig{w}_cross', f'sig{w}_mom_qual']:
                result[k] = 0.0
    return result

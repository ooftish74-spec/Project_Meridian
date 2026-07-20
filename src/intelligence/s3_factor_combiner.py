"""[Phase 75] S3 Factor Combiner with Sharpe Loss (E2E optimization)."""
from __future__ import annotations
import logging
from typing import Dict, List, Optional
import numpy as np

logger = logging.getLogger(__name__)

try:
    import torch, torch.nn as nn, torch.optim as optim
    _TORCH_OK = True
except ImportError as e:
    torch = None; nn = None; optim = None  # type: ignore
    _TORCH_OK = False
    logger.error('[Phase75] PyTorch 미설치 - NumPy fallback', exc_info=True)

_DEFAULT_FACTORS = ['value', 'momentum', 'quality', 'signature']

def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max()); return e / e.sum()

def _numpy_sharpe(factor_returns: np.ndarray, w: np.ndarray, ann=252.0) -> float:
    port = factor_returns @ w
    std  = port.std()
    return (port.mean() / std * ann**0.5) if std > 1e-9 else 0.0

def _numpy_optimize(factor_returns: np.ndarray, lr=0.01, epochs=200, ann=252.0) -> np.ndarray:
    n   = factor_returns.shape[1]
    raw = np.zeros(n)
    best_sharpe, best_w = -np.inf, _softmax(raw)
    for _ in range(epochs):
        w = _softmax(raw)
        s = _numpy_sharpe(factor_returns, w, ann)
        if s > best_sharpe:
            best_sharpe, best_w = s, w.copy()
        grad = np.zeros(n)
        for i in range(n):
            r_ = raw.copy(); r_[i] += 0.01
            grad[i] = (_numpy_sharpe(factor_returns, _softmax(r_), ann) - s) / 0.01
        raw += lr * grad
    return best_w

class S3FactorCombiner:
    """[Phase 75] Sharpe Loss E2E S3 Factor Combiner."""
    def __init__(self, factors: List[str] = None, lr=0.01, epochs=200, ann=252.0):
        self._factors = factors or _DEFAULT_FACTORS
        self._lr, self._epochs, self._ann = lr, epochs, ann
        self._weights: Optional[np.ndarray] = None
        self._last_sharpe = 0.0
        self._fitted = False

    def fit(self, factor_returns: np.ndarray, epochs: Optional[int] = None) -> float:
        ep = epochs or self._epochs
        T, n = factor_returns.shape
        if T < 20 or n != len(self._factors):
            self._weights = np.ones(n) / n; return 0.0
        if _TORCH_OK:
            X = torch.tensor(factor_returns, dtype=torch.float32)
            raw = nn.Parameter(torch.zeros(n))
            opt = optim.Adam([raw], lr=self._lr)
            for _ in range(ep):
                opt.zero_grad()
                w    = torch.softmax(raw, 0)
                port = X @ w
                loss = -(port.mean() / (port.std() + 1e-8)) * (self._ann**0.5)
                loss.backward(); opt.step()
            with torch.no_grad():
                self._weights = torch.softmax(raw, 0).numpy()
        else:
            self._weights = _numpy_optimize(factor_returns, self._lr, ep, self._ann)
        self._last_sharpe = _numpy_sharpe(factor_returns, self._weights, self._ann)
        self._fitted = True
        logger.info(f'[Phase75 S3] {dict(zip(self._factors, self._weights.round(4)))} Sharpe={self._last_sharpe:.3f}')
        return self._last_sharpe

    def get_weights(self) -> Dict[str, float]:
        if self._weights is None:
            n = len(self._factors); self._weights = np.ones(n) / n
        return {f: round(float(w), 4) for f, w in zip(self._factors, self._weights)}

    def score(self, factor_signals: Dict[str, float]) -> float:
        w = self.get_weights()
        return round(sum(w[f] * float(factor_signals.get(f, 0.0)) for f in self._factors), 4)

    @property
    def last_sharpe(self) -> float: return self._last_sharpe
    @property
    def is_fitted(self) -> bool: return self._fitted

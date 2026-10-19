"""
Cross-Sectional Empirical-Bayes IC-IR Shrinkage & Orthogonalized Factor Ensemble
================================================================================
Top-Tier Quant Fund (Renaissance / Two Sigma / Citadel GQS) Cross-Sectional Alpha Architecture:

1. Per-Date Blom/Rankit Normal-Score Standardization (`cross_sectional_rank_z_matrix`):
   Transforms raw non-stationary features into cross-sectionally invariant Gaussian
   quantiles within each date t (zero look-ahead across dates):
       u_{i,t,j} = (rank(X_{i,t,j}) - 0.5) / N_t
       Z_{i,t,j} = clip(Phi^{-1}(u_{i,t,j}), -z_max, +z_max)

2. Continuous Cross-Sectional Rank Reconstruction (`compute_cross_sectional_targets`):
   Decomposes centered cross-sectional return rank u_{i,t} - 0.5 into:
       y_{i,t} = I(u_{i,t} > 0.5) in {0, 1}          (50/50 market-neutral direction)
       w_{i,t} = max(w_min, 2 * |u_{i,t} - 0.5|)     (rank-distance sample weight)
   preserving exact continuous rank order (2 y_{i,t} - 1) * w_{i,t} == 2 (u_{i,t} - 0.5)
   while maintaining 100% compatibility with binary sklearn classifiers.

3. Empirical-Bayes Block IC-IR Shrinkage + Volatility-Skewness Orthogonalization
   (`CrossSectionalEBClassifier`):
   - Splits training data chronologically into B blocks and estimates each factor's
     mean IC, IC Information Ratio (IR = mean_IC / std_IC), and temporal sign consistency.
   - Damps unsigned volatility/dispersion factors (which create negative rank-IC vs
     positive right-tail decile-spread seesaws) via Barra/GQS style orthogonalization.
   - Penalizes collinear factor clusters (|rho| > crowd_threshold).
   - Combines the Empirical-Bayes linear factor predictor with a shallow regularized
     Gradient Boosting tree trained on the top-K orthogonalized factors.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple
import numpy as np
from scipy import stats
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import GradientBoostingClassifier

try:
    from sklearn.utils._tags import ClassifierTags
    _HAS_SKLEARN_TAGS = True
except ImportError:
    _HAS_SKLEARN_TAGS = False

_EPS = 1e-12
_STD_EPS = 1e-8
_IR_REG = 1e-3
_PROB_MIN = 0.001
_PROB_MAX = 0.999
_LOGIT_CLIP = 5.0
_HALF = 0.5
_MIN_CS_NAMES = 5
_MIN_SPLIT_ROWS = 4
_MIN_LEAF_ROWS = 20
_MAX_LEAF_ROWS = 400
_MIN_TREE_ROWS = 500


def cross_sectional_rank_z_matrix(
    X: np.ndarray,
    dates: Optional[Sequence] = None,
    clip_z: float = 3.0,
) -> np.ndarray:
    """Per-date cross-sectional Blom/Rankit normal-score standardization.

    Operates strictly within each date cross-section (zero look-ahead across dates).
    When ``dates`` is None, treats ``X`` as a single cross-section (e.g. live inference).
    """
    arr = np.asarray(X, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    n_rows, n_cols = arr.shape
    if n_rows == 0 or n_cols == 0:
        return np.zeros_like(arr, dtype=np.float64)

    out = np.zeros_like(arr, dtype=np.float64)
    z_bound = float(abs(clip_z))
    p_lo = float(_PROB_MIN)
    p_hi = float(_PROB_MAX)
    half = float(_HALF)
    std_eps = float(_STD_EPS)
    min_names = int(_MIN_CS_NAMES)

    if dates is None:
        groups = [np.arange(n_rows)]
    else:
        d_arr = np.asarray(dates)
        _, inv = np.unique(d_arr, return_inverse=True)
        n_groups = int(inv.max()) + 1 if len(inv) else 0
        groups = [np.where(inv == g)[0] for g in range(n_groups)]

    for idx in groups:
        n_d = len(idx)
        if n_d <= 1:
            continue
        sub = np.nan_to_num(arr[idx], nan=0.0, posinf=0.0, neginf=0.0)
        if n_d < min_names:
            med = np.median(sub, axis=0, keepdims=True)
            mad = np.median(np.abs(sub - med), axis=0, keepdims=True)
            scale = np.where(mad > std_eps, mad * 1.4826, np.std(sub, axis=0, keepdims=True))
            valid = (scale > std_eps).ravel()
            z_sub = np.zeros_like(sub)
            if np.any(valid):
                z_sub[:, valid] = (sub[:, valid] - med[:, valid]) / scale[:, valid]
            out[idx] = np.clip(z_sub, -z_bound, z_bound)
            continue

        stds = np.std(sub, axis=0)
        for j in range(n_cols):
            if stds[j] <= std_eps:
                continue
            col = sub[:, j]
            ranks = stats.rankdata(col, method='average')
            u = np.clip((ranks - half) / float(n_d), p_lo, p_hi)
            out[idx, j] = np.clip(stats.norm.ppf(u), -z_bound, z_bound)

    return out


def compute_cross_sectional_targets(
    fwd_ret: np.ndarray,
    dates: Optional[Sequence] = None,
    min_weight: float = 0.02,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute per-date cross-sectional median binary label and rank-magnitude weight.

    Returns:
        y_bin: int array in {0, 1} where 1 indicates above-median forward return on date t.
        sw_rank: float array in [min_weight, 1.0] proportional to 2 * |u_{i,t} - 0.5|.
    """
    r = np.nan_to_num(np.asarray(fwd_ret, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    n = len(r)
    if n == 0:
        return np.zeros(0, dtype=int), np.zeros(0, dtype=np.float64)

    y_bin = np.zeros(n, dtype=int)
    sw_rank = np.ones(n, dtype=np.float64)
    w_floor = float(min_weight)
    half = float(_HALF)

    if dates is None:
        groups = [np.arange(n)]
    else:
        d_arr = np.asarray(dates)
        _, inv = np.unique(d_arr, return_inverse=True)
        n_groups = int(inv.max()) + 1 if len(inv) else 0
        groups = [np.where(inv == g)[0] for g in range(n_groups)]

    for idx in groups:
        n_d = len(idx)
        if n_d <= 1:
            y_bin[idx] = (r[idx] > 0.0).astype(int)
            sw_rank[idx] = 1.0
            continue
        ranks = stats.rankdata(r[idx], method='average')
        u = (ranks - half) / float(n_d)
        y_bin[idx] = (u > half).astype(int)
        raw_w = 2.0 * np.abs(u - half)
        sw_rank[idx] = np.maximum(w_floor, raw_w)

    return y_bin, sw_rank


class CrossSectionalEBClassifier(ClassifierMixin, BaseEstimator):
    """Empirical-Bayes Cross-Sectional IC-IR Shrinkage + Regularized Tree Classifier."""

    _estimator_type = "classifier"

    def __init__(
        self,
        n_blocks: int = 5,
        top_k: int = 8,
        ortho_strength: float = 0.95,
        crowd_threshold: float = 0.35,
        tree_blend: float = 0.25,
        vol_cols: Tuple[int, ...] = (4, 10, 18),
        random_state: int = 42,
    ):
        self.n_blocks = n_blocks
        self.top_k = top_k
        self.ortho_strength = ortho_strength
        self.crowd_threshold = crowd_threshold
        self.tree_blend = tree_blend
        self.vol_cols = vol_cols
        self.random_state = random_state

    def __sklearn_tags__(self):
        tags = super().__sklearn_tags__()
        if _HAS_SKLEARN_TAGS:
            tags.classifier_tags = ClassifierTags()
            tags.estimator_type = 'classifier'
        return tags

    def fit(self, X: np.ndarray, y: np.ndarray, sample_weight: Optional[np.ndarray] = None):
        X_arr = np.nan_to_num(np.asarray(X, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
        y_arr = np.asarray(y, dtype=int)
        n, p = X_arr.shape
        self.classes_ = np.array([0, 1], dtype=int)
        self.n_features_in_ = int(p)

        if n == 0 or p == 0:
            self.w_ = np.zeros(p, dtype=np.float64)
            self.top_idx_ = np.arange(min(1, p), dtype=int)
            self.feature_importances_ = np.zeros(p, dtype=np.float64)
            self.tree_ = None
            return self

        sign_y = 2.0 * y_arr.astype(np.float64) - 1.0
        if sample_weight is not None and len(sample_weight) == n:
            sw = np.asarray(sample_weight, dtype=np.float64)
            yc = sign_y * sw
        else:
            sw = None
            yc = sign_y
        yc = yc - np.mean(yc)

        min_block_rows = 240
        min_split_rows = int(_MIN_SPLIT_ROWS)
        max_blocks = max(2, n // min_block_rows)
        n_b = max(2, min(int(self.n_blocks), max_blocks)) if n >= min_split_rows else 1
        blocks = np.array_split(np.arange(n), n_b)
        block_ics = np.zeros((len(blocks), p), dtype=np.float64)

        std_eps = float(_STD_EPS)
        for b_idx, idx in enumerate(blocks):
            if len(idx) < 2:
                continue
            xb = X_arr[idx]
            yb = yc[idx]
            yb_std = float(np.std(yb))
            if yb_std <= std_eps:
                continue
            xb_c = xb - np.mean(xb, axis=0, keepdims=True)
            xb_std = np.std(xb_c, axis=0)
            valid = xb_std > std_eps
            if np.any(valid):
                block_ics[b_idx, valid] = (xb_c[:, valid].T @ yb) / (
                    float(len(idx)) * xb_std[valid] * yb_std
                )

        mean_ic = np.mean(block_ics, axis=0)
        std_ic = np.std(block_ics, axis=0) + float(_IR_REG)
        ir = mean_ic / std_ic
        pos_frac = np.mean(block_ics > 0.0, axis=0)
        neg_frac = np.mean(block_ics < 0.0, axis=0)
        consistency = np.maximum(pos_frac, neg_frac)
        score = np.abs(mean_ic) * np.abs(ir) * (consistency ** 2)

        # Barra/GQS Volatility-Skewness Orthogonalization
        vcs = [int(c) for c in (self.vol_cols or ()) if 0 <= int(c) < p]
        if len(vcs) >= 2:
            # Verify collinearity of volatility clique so synthetic small-p inputs stay safe
            c0 = X_arr[:, vcs[0]]
            if np.std(c0) > std_eps:
                verified_vcs = [vcs[0]]
                for vc in vcs[1:]:
                    if np.std(X_arr[:, vc]) > std_eps:
                        corr_0 = float(np.corrcoef(c0, X_arr[:, vc])[0, 1])
                        if np.isfinite(corr_0) and corr_0 > self.crowd_threshold:
                            verified_vcs.append(vc)
                vcs = verified_vcs if len(verified_vcs) >= 2 else []
            else:
                vcs = []

        ortho = float(self.ortho_strength)
        if len(vcs) > 0 and ortho > 0.0:
            v_comp = np.mean(X_arr[:, vcs], axis=1)
            v_comp = v_comp - np.mean(v_comp)
            v_std = float(np.std(v_comp))
            if v_std > std_eps:
                x_std = np.std(X_arr, axis=0)
                valid = x_std > std_eps
                corr_v = np.zeros(p, dtype=np.float64)
                corr_v[valid] = (
                    (X_arr[:, valid] - np.mean(X_arr[:, valid], axis=0)).T @ v_comp
                ) / (float(n) * x_std[valid] * v_std)
                damping = np.maximum(0.0, 1.0 - ortho * (corr_v ** 2))
                score = score * damping

        k = max(1, min(int(self.top_k), p))
        top_idx = np.argsort(score)[::-1][:k]
        self.top_idx_ = np.asarray(top_idx, dtype=int)

        raw_w = np.zeros(p, dtype=np.float64)
        raw_w[top_idx] = np.sign(mean_ic[top_idx]) * score[top_idx]

        active = np.where(np.abs(raw_w) > _EPS)[0]
        c_thr = float(self.crowd_threshold)
        if len(active) > 1 and c_thr < 1.0:
            Xa = X_arr[:, active]
            C = np.corrcoef(Xa, rowvar=False)
            C = np.nan_to_num(C, nan=0.0, posinf=0.0, neginf=0.0)
            np.fill_diagonal(C, 1.0)
            crowd = 1.0 + np.sum(np.maximum(0.0, np.abs(C) - c_thr), axis=1) - (1.0 - c_thr)
            crowd = np.maximum(1.0, crowd)
            raw_w[active] = raw_w[active] / crowd

        if len(vcs) > 0 and ortho > 0.0:
            s_raw = X_arr @ raw_w
            v_comp = np.mean(X_arr[:, vcs], axis=1)
            denom = float(np.dot(v_comp, v_comp)) + std_eps
            beta_v = float(np.dot(s_raw, v_comp)) / denom
            for vc in vcs:
                raw_w[vc] -= ortho * beta_v / float(len(vcs))

        norm = float(np.linalg.norm(raw_w))
        if norm > _EPS:
            self.w_ = raw_w / norm
        else:
            fallback_norm = float(np.linalg.norm(mean_ic))
            self.w_ = mean_ic / fallback_norm if fallback_norm > _EPS else np.zeros(p, dtype=np.float64)

        imp_sum = float(np.sum(np.abs(self.w_)))
        if imp_sum > _EPS:
            self.feature_importances_ = np.abs(self.w_) / imp_sum
        else:
            self.feature_importances_ = np.ones(p, dtype=np.float64) / float(p)

        self.tree_ = None
        tb = float(self.tree_blend)
        min_rows_for_tree = int(_MIN_TREE_ROWS)
        if tb > 0.0 and len(self.top_idx_) > 0 and n >= min_rows_for_tree and len(np.unique(y_arr)) >= 2:
            try:
                min_leaf = int(_MIN_LEAF_ROWS)
                max_leaf = int(_MAX_LEAF_ROWS)
                leaf_min = max(min_leaf, min(max_leaf, n // 15))
                tree = GradientBoostingClassifier(
                    n_estimators=40,
                    max_depth=2,
                    learning_rate=0.02,
                    subsample=0.7,
                    min_samples_leaf=leaf_min,
                    random_state=int(self.random_state),
                )
                tree.fit(X_arr[:, self.top_idx_], y_arr, sample_weight=sw)
                self.tree_ = tree
            except Exception:
                self.tree_ = None

        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        X_arr = np.nan_to_num(np.asarray(X, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
        if X_arr.ndim == 1:
            X_arr = X_arr.reshape(1, -1)
        n = X_arr.shape[0]
        if n == 0:
            return np.zeros((0, 2), dtype=np.float64)

        s = X_arr @ self.w_
        std = float(np.std(s))
        if std > _STD_EPS:
            z = (s - float(np.mean(s))) / std
        else:
            z = s
        logit_bound = float(_LOGIT_CLIP)
        z_clipped = np.clip(z, -logit_bound, logit_bound)
        p_eb = 1.0 / (1.0 + np.exp(-z_clipped))

        tb = float(self.tree_blend)
        if self.tree_ is not None and tb > 0.0:
            try:
                p_tr = self.tree_.predict_proba(X_arr[:, self.top_idx_])[:, 1]
                p = (1.0 - tb) * p_eb + tb * p_tr
            except Exception:
                p = p_eb
        else:
            p = p_eb

        p = np.clip(p, _PROB_MIN, _PROB_MAX)
        return np.column_stack([1.0 - p, p])

    def predict(self, X: np.ndarray) -> np.ndarray:
        proba = self.predict_proba(X)[:, 1]
        return (proba >= _HALF).astype(int)

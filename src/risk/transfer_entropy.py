"""[Phase 75] 전이 엔트로피(Transfer Entropy) + HRP 리스크 패리티 모듈 구현.

[TE] 인과성 네트워크: Nasdaq→KOSPI 등 선행성이 뚜렷한 자산에 패널티/가중치
[HRP] 계층적으로 S1~S6 스트림 비중 할당 (Crowding Crash 방어)

Usage:
    from src.risk.transfer_entropy import TEHRPAllocator
    alloc = TEHRPAllocator()
    weights, alert = alloc.allocate(stream_returns, regime='bull')
    if alert['crowding_detected']:
        # S4 Advisory 현금 보유 모드
        ...
"""
from __future__ import annotations

import logging
import warnings
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    from scipy.cluster.hierarchy import linkage, leaves_list, fcluster
    from scipy.spatial.distance import squareform
    _SCIPY_OK = True
except ImportError as e:
    _SCIPY_OK = False
    warnings.warn('[Phase 75] scipy 미설치 — HRP fallback 모드')

logger = logging.getLogger(__name__)


# ── Transfer Entropy ──────────────────────────────────────────────────

def _discretize(x: np.ndarray, n_bins: int = 5) -> np.ndarray:
    """1D 연속시계열 이산화 (Shannon 엔트로피 계산용)."""
    edges = np.percentile(x, np.linspace(0, 100, n_bins + 1))
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    return np.digitize(x, edges) - 1


def _joint_entropy(x: np.ndarray, y: np.ndarray, n_bins: int = 5) -> float:
    """H(X, Y) 결합 엔트로피."""
    joint = x * n_bins + y
    counts = np.bincount(joint, minlength=n_bins**2)
    probs  = counts / len(joint)
    return -np.sum(probs[probs > 0] * np.log2(probs[probs > 0]))


def compute_transfer_entropy(
    x: np.ndarray,
    y: np.ndarray,
    k: int = 1,
    n_bins: int = 5,
) -> float:
    """[Phase 75] 전이 엔트로피 TE(X → Y).

    TE(X→Y) = H(Y_t|Y_{t-1}) - H(Y_t|Y_{t-1}, X_{t-1})
             = H(Y_t, Y_{t-1}) - H(Y_{t-1}) - H(Y_t, Y_{t-1}, X_{t-1}) + H(Y_{t-1}, X_{t-1})

    Args:
        x: 원인 시계열 (ex: Nasdaq 수익률)
        y: 결과 시계열 (ex: KOSPI 수익률)
        k: 래그 스텝
        n_bins: 이산화 빈도 계수

    Returns:
        TE 값 (비트) >= 0. 클수록 X가 Y를 강하게 리드.
    """
    min_len = min(len(x), len(y)) - k
    if min_len < 20:
        return 0.0

    x_d   = _discretize(x[-min_len - k: -k], n_bins)
    y_d   = _discretize(y[-min_len:],         n_bins)
    y_lag = _discretize(y[-min_len - k: -k],  n_bins)

    def _h(arr: np.ndarray) -> float:
        cnt = np.bincount(arr, minlength=n_bins)
        p   = cnt / cnt.sum()
        return -np.sum(p[p > 0] * np.log2(p[p > 0]))

    h_y_given_ylag  = _joint_entropy(y_d, y_lag) - _h(y_lag)
    h_y_given_xylag = (
        _joint_entropy(y_d * n_bins + y_lag, x_d) - _joint_entropy(y_lag, x_d)
    )
    te = max(0.0, h_y_given_ylag - h_y_given_xylag)
    return round(te, 6)


def build_te_matrix(
    returns: Dict[str, np.ndarray],
    n_bins: int = 5,
) -> Tuple[np.ndarray, List[str]]:
    """[Phase 75] 스트림마다 TE 행렬 구성.

    te_matrix[i][j] = TE(stream_i -> stream_j)
    큰 값 = stream_i가 stream_j를 리드하는 강도.
    """
    streams = list(returns.keys())
    n = len(streams)
    mat = np.zeros((n, n))
    for i, si in enumerate(streams):
        for j, sj in enumerate(streams):
            if i != j:
                mat[i, j] = compute_transfer_entropy(returns[si], returns[sj], n_bins=n_bins)
    logger.debug(f'[Phase 75 TE] 행렬 \n{mat.round(4)}')
    return mat, streams


# ── HRP (Hierarchical Risk Parity) ───────────────────────────────────

def _cov_to_corr(cov: np.ndarray) -> np.ndarray:
    std = np.sqrt(np.diag(cov))
    std = np.where(std < 1e-9, 1e-9, std)
    return cov / np.outer(std, std)


def _quasi_diag(link: np.ndarray, n: int) -> List[int]:
    """HRP의 Quasi-Diagonal 계층 리오더링."""
    if not _SCIPY_OK:
        return list(range(n))
    return list(leaves_list(link))


def _recursive_bisect(
    cov: np.ndarray,
    sort_ix: List[int],
) -> np.ndarray:
    """HRP Recursive Bisection (Lopez de Prado 2016)."""
    n = len(sort_ix)
    weights = np.ones(n)

    def _cluster_var(indices: List[int]) -> float:
        sub_cov = cov[np.ix_(indices, indices)]
        inv_diag = 1.0 / np.maximum(np.diag(sub_cov), 1e-9)
        w = inv_diag / inv_diag.sum()
        return float(w @ sub_cov @ w)

    def _bisect(items: List[int]) -> None:
        if len(items) <= 1:
            return
        mid = len(items) // 2
        left, right = items[:mid], items[mid:]
        lvar = _cluster_var(left)
        rvar = _cluster_var(right)
        total = lvar + rvar if (lvar + rvar) > 1e-9 else 1.0
        alpha = 1.0 - lvar / total
        weights[left]  *= (1.0 - alpha)
        weights[right] *= alpha
        _bisect(left)
        _bisect(right)

    _bisect(list(range(n)))
    return weights


class TEHRPAllocator:
    """[Phase 75] TE 기반 공리 행렬로 HRP 비중 할당.

    기존 alpha_allocator.py의 역변동성+상관패널티를
    TE(X->Y) 기반 HRP로 대체.
    """

    def __init__(
        self,
        n_bins: int = 5,
        crowding_te_threshold: float = 0.3,
        min_weight: float = 0.03,
        max_weight: float = 0.50,
    ):
        self._n_bins      = n_bins
        self._te_thresh   = crowding_te_threshold
        self._min_w       = min_weight
        self._max_w       = max_weight
        self._last_te_mat: Optional[np.ndarray] = None
        self._last_streams: List[str]           = []

    def allocate(
        self,
        stream_returns: Dict[str, np.ndarray],
        cov_override:   Optional[np.ndarray] = None,
        base_weights:   Optional[Dict[str, float]] = None,
        blend: float    = 0.50,
    ) -> Tuple[Dict[str, float], Dict]:
        """[Phase 75] TE-HRP 비중 할당.

        Args:
            stream_returns: {stream_id: returns_array}
            cov_override:   외부 공분산 행렬 (없으면 내부 계산)
            base_weights:   레짐 기반 기본 비중 (블렌드 용)
            blend:          HRP 블렌드 가중치 (0=기본모드만, 1=HRP만)

        Returns:
            (weights_dict, crowding_alert_dict)
        """
        streams = list(stream_returns.keys())
        n = len(streams)
        if n == 0:
            return {}, {'crowding_detected': False}

        # 수익률 행렬
        lens  = [len(v) for v in stream_returns.values()]
        min_l = min(lens)
        R = np.array([
            list(stream_returns[s])[-min_l:] for s in streams
        ]).T  # (T, n)

        # 1. TE 행렬
        te_mat, _ = build_te_matrix(stream_returns, n_bins=self._n_bins)
        self._last_te_mat  = te_mat
        self._last_streams = streams

        # 2. Crowding 감지 (TE 단일 유사성 높음)
        crowding_alert = self._detect_crowding(te_mat, streams)

        # 3. 공분산 행렬
        if cov_override is not None:
            cov = cov_override
        else:
            cov = np.cov(R.T) if min_l > 5 else np.eye(n)

        # 4. TE 리스크 행렬 (TE가 높을수록 위험 증가)
        te_risk = te_mat.sum(axis=0)  # 해당 스트림으로의 유입 TE 합계
        te_penalty = 1.0 + te_risk / (te_risk.max() + 1e-9) * 0.20
        cov_te = cov * np.outer(te_penalty, te_penalty)

        # 5. HRP
        if _SCIPY_OK:
            corr = _cov_to_corr(cov_te)
            corr = np.clip(corr, -0.9999, 0.9999)
            dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0, 1))
            dist_sq = squareform(dist, checks=False)
            link = linkage(dist_sq, method='ward')
            sort_ix = _quasi_diag(link, n)
            sorted_cov = cov_te[np.ix_(sort_ix, sort_ix)]
            hrp_w = _recursive_bisect(sorted_cov, sort_ix)
            # 원래 인덱스로 복원
            w_arr = np.zeros(n)
            for idx, orig_idx in enumerate(sort_ix):
                w_arr[orig_idx] = hrp_w[idx]
        else:
            # Fallback: 역변동성
            stds = np.array([R[:, i].std() for i in range(n)])
            stds = np.where(stds < 1e-9, 1e-9, stds)
            w_arr = (1.0 / stds) / (1.0 / stds).sum()

        # 6. min/max 클램프 + 정규화
        w_arr = np.clip(w_arr, self._min_w, self._max_w)
        w_arr /= w_arr.sum()

        # 7. 레짐 기본 비중과 블렌드
        if base_weights:
            base_arr = np.array([base_weights.get(s, 1.0/n) for s in streams])
            base_arr /= base_arr.sum()
            w_arr = blend * w_arr + (1.0 - blend) * base_arr
            w_arr = np.clip(w_arr, self._min_w, self._max_w)
            w_arr /= w_arr.sum()

        weights_dict = {s: round(float(w_arr[i]), 4) for i, s in enumerate(streams)}
        logger.info(
            f'[Phase 75 TE-HRP] 비중: {weights_dict} '
            f'| Crowding={crowding_alert["crowding_detected"]}'
        )
        return weights_dict, crowding_alert

    def _detect_crowding(
        self, te_mat: np.ndarray, streams: List[str]
    ) -> Dict:
        """Crowding 및 Entropy Alert 감지."""
        if len(streams) < 2:
            return {'crowding_detected': False, 'entropy_alert': 0.0, 'cluster_risk': []}

        # 단일 근접성: TE 평균이 임계값 초과
        avg_te = te_mat[te_mat > 0].mean() if (te_mat > 0).any() else 0.0
        crowding = bool(avg_te > self._te_thresh)

        # 위험 클러스터: TE가 높은 스트림
        te_sum = te_mat.sum(axis=0) + te_mat.sum(axis=1)
        high_risk = [
            streams[i] for i in range(len(streams))
            if te_sum[i] > self._te_thresh * len(streams)
        ]

        return {
            'crowding_detected': crowding,
            'entropy_alert':     round(float(avg_te), 4),
            'cluster_risk':      high_risk,
            'recommendation':    'CASH' if crowding else 'NORMAL',
        }

    def get_last_te_matrix(self) -> Optional[np.ndarray]:
        return self._last_te_mat

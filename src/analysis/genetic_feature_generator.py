"""
[Phase 70-C] Genetic Feature Generator — 유전 알고리즘 기반 피처 자동 발견.

등록 기준 (두 조건 모두 충족시만):
    1. IC (Information Coefficient) > ic_threshold (default 0.05)
    2. |corr| < orthogonality_threshold (default 0.30) — 기존 피처와 직교

실행 주기: _phase_weekly_retrain에서 20세대, 대략 3분
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_OPS: List[Tuple[str, Callable]] = [
    ('add',          lambda a, b: a + b),
    ('sub',          lambda a, b: a - b),
    ('mul',          lambda a, b: a * b),
    ('div',          lambda a, b: np.where(np.abs(b) > 1e-8, a / b, 0.0)),
    ('rank',         lambda a, _: pd.Series(a).rolling(60, min_periods=5).rank(pct=True).fillna(0.5).values),
    ('zscore',       lambda a, _: ((pd.Series(a) - pd.Series(a).rolling(60, min_periods=5).mean()) / (pd.Series(a).rolling(60, min_periods=5).std() + 1e-8)).fillna(0.0).values),
    ('log',          lambda a, _: np.log1p(np.abs(a)) * np.sign(a)),
    ('diff',         lambda a, _: np.diff(a, prepend=a[0])),
    ('rolling_mean', lambda a, _: pd.Series(a).rolling(5, min_periods=1).mean().values),
    ('rolling_std',  lambda a, _: pd.Series(a).rolling(5, min_periods=1).std().fillna(0).values),
]


@dataclass
class GeneticFeature:
    """[Phase 70-C] 유전 알고리즘으로 생성된 피처."""
    expression: str
    ic: float = 0.0
    max_corr_existing: float = 1.0
    generation: int = 0
    values: Optional[np.ndarray] = field(default=None, repr=False)

    @property
    def is_valid(self) -> bool:
        return self.values is not None and not np.isnan(self.values).all()


class GeneticFeatureGenerator:
    """[Phase 70-C] 유전 알고리즘 기반 직교 피처 자동 발견.

    사용 예시::

        gen = GeneticFeatureGenerator(cfg)
        features = gen.evolve(raw_df, forward_returns, existing_features, generations=20)
    """

    def __init__(self, cfg: Optional[Any] = None, seed: int = 42) -> None:
        self._cfg = cfg
        _get = (lambda k, d: cfg.get(k, d)) if cfg and hasattr(cfg, 'get') else (lambda k, d: d)
        self._ic_threshold    = float(_get('genetic.ic_threshold', 0.05))
        self._orth_threshold  = float(_get('genetic.orthogonality_threshold', 0.30))
        self._population_size = int(_get('genetic.population_size', 50))
        self._mutation_rate   = float(_get('genetic.mutation_rate', 0.20))
        random.seed(seed)
        np.random.seed(seed)

    def evolve(
        self,
        raw_df: pd.DataFrame,
        forward_returns: pd.Series,
        existing_features: Optional[pd.DataFrame] = None,
        generations: int = 20,
    ) -> List[GeneticFeature]:
        """[Phase 70-C] 피처 진화 추진.

        Args:
            raw_df: 원시 데이터 DataFrame
            forward_returns: 미래 수익률 Series
            existing_features: 기존 등록된 피처 (직교성 제약용)
            generations: 진화 세대 수

        Returns:
            IC/직교성 조건을 통과한 신규 피처 목록
        """
        _cols = list(raw_df.columns)
        _population = self._initialize_population(_cols)
        _registered: List[GeneticFeature] = []

        for gen in range(generations):
            _scored: List[Tuple[float, GeneticFeature]] = []
            for expr in _population:
                _feat = self._evaluate_feature(expr, raw_df, forward_returns, gen)
                if _feat.is_valid and self._passes_ic(_feat):
                    if self._is_orthogonal(_feat, existing_features, _registered):
                        _scored.append((_feat.ic, _feat))

            _scored.sort(key=lambda x: -x[0])
            _survivors = [f for _, f in _scored[:max(1, len(_scored) // 2)]]
            _registered.extend(_survivors)

            if _survivors:
                logger.info(
                    f'[Phase 70-C] Gen {gen+1}/{generations}: '
                    f'{len(_survivors)}개 통과 (IC>{self._ic_threshold:.2f})'
                )

            _population = self._next_generation(
                [f.expression for _, f in _scored], _cols
            )

        _unique = list({f.expression: f for f in _registered}.values())
        logger.info(
            f'[Phase 70-C] 진화 완료: {len(_unique)}개 직교 피처 등록 '
            f'(IC>{self._ic_threshold:.2f}, |corr|<{self._orth_threshold:.2f})'
        )
        return _unique

    def _initialize_population(self, cols: List[str]) -> List[str]:
        """[Phase 70-C] 초기 세대 생성."""
        _pop = []
        for _ in range(self._population_size):
            _c1 = random.choice(cols)
            _c2 = random.choice(cols)
            _op = random.choice(_OPS)[0]
            _pop.append(f"{_op}({_c1},{_c2})")
        return _pop

    @staticmethod
    def _split_top_level_args(arg_str: str) -> List[str]:
        """괄호 깊이를 고려하여 최상위 콤마(,) 기준으로 인자 분리."""
        args: List[str] = []
        depth = 0
        current: List[str] = []
        for ch in arg_str:
            if ch == '(':
                depth += 1
                current.append(ch)
            elif ch == ')':
                depth = max(0, depth - 1)
                current.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        if current:
            args.append(''.join(current).strip())
        return args

    def _eval_expr_node(self, expr: str, raw_df: pd.DataFrame) -> Optional[np.ndarray]:
        """단일 컬럼명 또는 중첩 함수 표현식을 재귀적으로 평가."""
        expr = expr.strip()
        if not expr:
            return None
        if '(' not in expr:
            s = raw_df.get(expr)
            if s is None or len(s) == 0:
                return None
            return s.values.astype(float)
        if not expr.endswith(')'):
            return None
        lparen = expr.find('(')
        op_name = expr[:lparen].strip()
        op_fn = dict(_OPS).get(op_name)
        if op_fn is None:
            return None
        inner = expr[lparen + 1:-1]
        args = self._split_top_level_args(inner)
        if not args:
            return None
        a = self._eval_expr_node(args[0], raw_df)
        b = self._eval_expr_node(args[1] if len(args) > 1 else args[0], raw_df)
        if a is None or b is None or len(a) == 0 or len(b) == 0:
            return None
        m = min(len(a), len(b))
        return np.asarray(op_fn(a[:m], b[:m]), dtype=float)

    def _evaluate_feature(
        self,
        expression: str,
        raw_df: pd.DataFrame,
        forward_returns: pd.Series,
        generation: int,
    ) -> GeneticFeature:
        """[Phase 70-C] 피처 표현식 평가 (중첩 트리 표현식 재귀 지원)."""
        try:
            _raw_vals = self._eval_expr_node(expression, raw_df)
            if _raw_vals is None or len(_raw_vals) == 0 or len(forward_returns) == 0:
                return GeneticFeature(expression, generation=generation)

            _min_len = min(len(_raw_vals), len(forward_returns))
            _vals = np.nan_to_num(_raw_vals[:_min_len].astype(float), nan=0.0, posinf=0.0, neginf=0.0)

            _ic = self._compute_ic(_vals, forward_returns.values[:_min_len])
            return GeneticFeature(expression=expression, ic=_ic,
                                  generation=generation, values=_vals)
        except Exception as e:  # noqa: BLE001 — 진화 중 결함 피처 계속 진행
            import logging
            logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
            return GeneticFeature(expression, generation=generation)

    @staticmethod
    def _compute_ic(feature: np.ndarray, returns: np.ndarray) -> float:
        """[Phase 70-C] Spearman 상관계수 (IC) 계산."""
        try:
            from scipy.stats import spearmanr
            _corr, _ = spearmanr(feature, returns)
            return float(abs(_corr)) if np.isfinite(_corr) else 0.0
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
            return 0.0

    def _passes_ic(self, feat: GeneticFeature) -> bool:
        return feat.ic >= self._ic_threshold

    def _is_orthogonal(
        self,
        feat: GeneticFeature,
        existing: Optional[pd.DataFrame],
        registered: List[GeneticFeature],
    ) -> bool:
        """[Phase 70-C] 직교성 여부 확인."""
        if feat.values is None:
            return False
        _all_vals = []
        if existing is not None and not existing.empty:
            _all_vals.extend([existing[c].values for c in existing.columns])
        _all_vals.extend([f.values for f in registered if f.values is not None])
        for _ev in _all_vals:
            _min_len = min(len(feat.values), len(_ev))
            try:
                _corr = np.corrcoef(feat.values[:_min_len], _ev[:_min_len])[0, 1]
                if np.isfinite(_corr) and abs(_corr) >= self._orth_threshold:
                    return False
            except Exception as e:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
                pass
        return True

    def _next_generation(
        self, survivors: List[str], cols: List[str]
    ) -> List[str]:
        """[Phase 70-C] 선발 + 돌연변이로 다음 세대 생성."""
        _next: List[str] = list(survivors)
        _op_names = [op[0] for op in _OPS]
        while len(_next) < self._population_size:
            if survivors and random.random() > self._mutation_rate:
                _op = random.choice(_op_names)
                _next.append(f"{_op}({random.choice(survivors)},{random.choice(survivors)})")
            else:
                _op = random.choice(_op_names)
                _next.append(f"{_op}({random.choice(cols)},{random.choice(cols)})")
        return _next[:self._population_size]

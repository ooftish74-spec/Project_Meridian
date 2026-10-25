#!/usr/bin/env python3
"""
Purged K-Fold Cross Validation
==============================

시계열 머신러닝 모델의 과적합(Overfitting)과 데이터 누수(Data Leakage)를
방지하기 위한 동적 교차 검증 모듈입니다.

테스트 셋과 시간적으로 겹치는 훈련 셋의 관측치를 제거(Purge)하고,
테스트 셋 직후 일정 기간 동안의 데이터를 훈련 셋에서 배제(Embargo)합니다.
"""

import logging
from typing import Generator, Tuple, Optional

import numpy as np
import pandas as pd

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)

class PurgedKFold:
    """[DEPRECATED — WAVE3 Track D] 행(row) 단위 분할 레거시 구현.

    알려진 결함 (패널 데이터에서 누수 발생):
      * 행 단위 등분 → 동일 날짜의 종목들이 train/test 양쪽에 분산 (횡단면 누수)
      * 테스트 '이전' 구간의 라벨 overlap purge 없음 (embargo만 테스트 이후 적용)
      * embargo가 달력일(calendar day) 기준
    신규 코드는 ``DatePurgedWalkForward`` / ``CombinatorialPurgedCV`` 를 사용할 것.
    하위 호환을 위해 동작은 변경하지 않고 유지한다.
    """

    def __init__(self, n_splits: Optional[int] = None, embargo_days: Optional[int] = None):
        self.cfg = DynamicConfig()
        self.n_splits = n_splits or int(self.cfg.get('ml.purged_kfold_splits', 5))
        self.embargo_days = embargo_days or int(self.cfg.get('ml.embargo_days', 5))
        
    def split(self, 
              X: pd.DataFrame, 
              y: Optional[pd.Series] = None, 
              groups=None) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
        """Purged K-Fold의 분할 인덱스를 생성합니다.
        
        가정:
        - X의 인덱스는 시간(DatetimeIndex)순으로 오름차순 정렬되어 있어야 합니다.
        """
        if not isinstance(X.index, pd.DatetimeIndex):
            logger.warning("  [PurgedKFold] X.index가 DatetimeIndex가 아닙니다. 단순 순차 분할을 시도합니다.")
            
        n_samples = len(X)
        indices = np.arange(n_samples)
        
        # 단순 등분할 기반 Test 시작/종료 인덱스 계산
        test_starts = [int(i) for i in np.linspace(0, n_samples, self.n_splits + 1)[:-1]]
        test_ends = [int(i) for i in np.linspace(0, n_samples, self.n_splits + 1)[1:]]
        
        for split_idx in range(self.n_splits):
            test_start = test_starts[split_idx]
            test_end = test_ends[split_idx]
            test_indices = indices[test_start:test_end]
            
            # Train indices 계산
            # 1. 테스트 이전 기간 (0 ~ test_start - 1)
            train_before = indices[:test_start]
            
            # 2. 테스트 이후 기간 (Embargo 적용)
            if isinstance(X.index, pd.DatetimeIndex):
                # 테스트 셋의 마지막 날짜
                test_end_time = X.index[test_end - 1]
                # Embargo 끝나는 날짜
                embargo_end_time = test_end_time + pd.Timedelta(self.embargo_days, unit='D')
                
                # Embargo 이후의 인덱스 찾기
                post_test_indices = np.where(X.index > embargo_end_time)[0]
                # post_test_indices 중 test_end 이후인 것만
                post_test_indices = post_test_indices[post_test_indices >= test_end]
                train_after = post_test_indices
            else:
                # 시계열 인덱스가 없는 경우, 단순히 test_end + embargo_days 인덱스부터 시작
                embargo_idx = min(test_end + self.embargo_days, n_samples)
                train_after = indices[embargo_idx:]
                
            train_indices = np.concatenate([train_before, train_after])
            
            yield train_indices, test_indices


# ═══════════════════════════════════════════════════════════════════════
# [WAVE3 Track D] Date-major Purged Walk-Forward / Combinatorial Purged CV
# ═══════════════════════════════════════════════════════════════════════
#
# 패널 데이터(한 날짜에 여러 종목 행)를 위한 날짜 단위(date-major) 분할기.
#
#   * 같은 날짜의 모든 행은 반드시 같은 fold(train 또는 test)에 들어간다.
#   * 모든 기간 계산은 "거래일 인덱스"(데이터의 고유 정렬 날짜 또는 외부 제공
#     거래일 캘린더) 기준이며, 달력일(calendar day)을 사용하지 않는다.
#   * Purge   : train 날짜 t 의 라벨 구간 [t, t+h] 가 test 라벨 구간
#               [test_start, test_end + h] 와 겹치면 제거.
#   * Embargo : test 라벨 구간 종료(test_end + h) 이후 embargo_days 거래일 동안의
#               train 날짜 제거 (train 이 test 이후 날짜를 쓰는 경우에만 의미 있음).
#
# 기호:  p = 캘린더 상 위치(거래일 인덱스), h = label_horizon_days, e = embargo_days
#   train 구간 [p, p+h] 와 test 구간 [a, b+h] 의 overlap ⇔ p <= b+h  and  p+h >= a
#   embargo 구간: b+h < p <= b+h+e
# ═══════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, Iterator, List, Sequence


def resolve_row_dates(X=None, dates=None, groups=None) -> pd.DatetimeIndex:
    """패널 행별 날짜를 tz-naive, 자정 정규화된 DatetimeIndex 로 반환.

    우선순위: dates → groups → X.index(MultiIndex 'date' 레벨 / DatetimeIndex)
    → X['date'] 컬럼.
    """
    src = None
    if dates is not None:
        src = dates
    elif groups is not None:
        src = groups
    elif isinstance(X, (pd.DataFrame, pd.Series)):
        idx = X.index
        if isinstance(idx, pd.MultiIndex):
            level = 'date' if 'date' in (idx.names or []) else 0
            src = idx.get_level_values(level)
        elif isinstance(idx, pd.DatetimeIndex):
            src = idx
        elif isinstance(X, pd.DataFrame) and 'date' in X.columns:
            src = X['date'].values
    if src is None:
        raise ValueError(
            "날짜 정보를 찾을 수 없습니다: dates=, groups= 를 전달하거나 "
            "X.index 에 'date' 레벨/DatetimeIndex 를 제공하세요.")
    out = pd.DatetimeIndex(pd.to_datetime(np.asarray(src)))
    if out.tz is not None:
        out = out.tz_convert(None)
    return out.normalize()


@dataclass
class DateFold:
    """한 fold 의 날짜 단위 분할 결과 (리포트/테스트용)."""
    fold: int
    train_dates: pd.DatetimeIndex
    test_dates: pd.DatetimeIndex
    purged_dates: pd.DatetimeIndex = field(default_factory=lambda: pd.DatetimeIndex([]))
    embargoed_dates: pd.DatetimeIndex = field(default_factory=lambda: pd.DatetimeIndex([]))

    def summary(self) -> Dict:
        def _d(ix):
            return str(ix.min().date()) if len(ix) else None

        def _e(ix):
            return str(ix.max().date()) if len(ix) else None
        return {
            'fold': self.fold,
            'train_start': _d(self.train_dates), 'train_end': _e(self.train_dates),
            'test_start': _d(self.test_dates), 'test_end': _e(self.test_dates),
            'n_train_dates': int(len(self.train_dates)),
            'n_test_dates': int(len(self.test_dates)),
            'n_purged_dates': int(len(self.purged_dates)),
            'n_embargoed_dates': int(len(self.embargoed_dates)),
        }


class _BaseDatePurgedCV:
    """공통: 캘린더 구성, purge/embargo 계산, 날짜→행 인덱스 매핑."""

    method_name = 'base'

    def __init__(self, label_horizon_days: int = 5, embargo_days: int = 5,
                 calendar: Optional[Sequence] = None, min_embargo_days: int = 5):
        if label_horizon_days is None or int(label_horizon_days) < 0:
            raise ValueError("label_horizon_days 는 0 이상의 거래일 수여야 합니다.")
        if embargo_days is None or int(embargo_days) < 0:
            raise ValueError("embargo_days 는 0 이상이어야 합니다.")
        if int(embargo_days) < int(min_embargo_days):
            raise ValueError(
                f"embargo_days={embargo_days} < min_embargo_days={min_embargo_days}. "
                f"(의도적으로 작은 embargo 가 필요하면 min_embargo_days 를 낮추세요)")
        self.label_horizon_days = int(label_horizon_days)
        self.embargo_days = int(embargo_days)
        self.min_embargo_days = int(min_embargo_days)
        self.calendar = None if calendar is None else resolve_row_dates(dates=calendar).unique().sort_values()

    # ── helpers ────────────────────────────────────────────────────────
    def _build_calendar(self, unique_dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if self.calendar is None:
            return unique_dates
        return self.calendar.union(unique_dates).unique().sort_values()

    def _purge_mask(self, cal_pos: np.ndarray, test_mask: np.ndarray):
        """고유 날짜 단위 purge/embargo 마스크.

        Args:
            cal_pos:   고유 데이터 날짜들의 캘린더 위치 (정렬, int)
            test_mask: test 로 선택된 고유 날짜 bool 마스크

        Returns:
            (purged, embargoed) bool 마스크 (test 날짜는 False)
        """
        h, e = self.label_horizon_days, self.embargo_days
        purged = np.zeros(len(cal_pos), dtype=bool)
        embargoed = np.zeros(len(cal_pos), dtype=bool)
        test_idx = np.where(test_mask)[0]
        if len(test_idx) == 0:
            return purged, embargoed
        # 연속된 test 블록별로 처리 (CPCV 는 여러 블록 가능)
        breaks = np.where(np.diff(test_idx) > 1)[0]
        starts = np.concatenate([[0], breaks + 1])
        ends = np.concatenate([breaks, [len(test_idx) - 1]])
        for s, en in zip(starts, ends):
            a = cal_pos[test_idx[s]]          # test 시작 위치
            b_h = cal_pos[test_idx[en]] + h   # test 라벨 종료 위치
            overlap = (cal_pos <= b_h) & (cal_pos + h >= a)
            emb = (cal_pos > b_h) & (cal_pos <= b_h + e)
            purged |= overlap
            embargoed |= emb
        purged &= ~test_mask
        embargoed &= ~(test_mask | purged)
        return purged, embargoed

    @staticmethod
    def _rows_for(codes: np.ndarray, date_sel: np.ndarray) -> np.ndarray:
        return np.where(date_sel[codes])[0]

    def _prepare(self, X=None, dates=None, groups=None):
        row_dates = resolve_row_dates(X, dates, groups)
        uniq = row_dates.unique().sort_values()
        codes = uniq.get_indexer(row_dates)
        cal = self._build_calendar(uniq)
        cal_pos = cal.get_indexer(uniq)
        if (cal_pos < 0).any():
            raise ValueError("데이터 날짜가 캘린더에 존재하지 않습니다.")
        return row_dates, uniq, codes, cal_pos

    # ── public API ─────────────────────────────────────────────────────
    def _fold_masks(self, n_unique: int, cal_pos: np.ndarray) -> Iterator:
        raise NotImplementedError

    def split_dates(self, X=None, y=None, groups=None, dates=None) -> List[DateFold]:
        """날짜 단위 fold 정보 리스트 (train/test/purged/embargoed 날짜)."""
        _, uniq, _, cal_pos = self._prepare(X, dates, groups)
        out = []
        for k, (train_m, test_m, purged_m, emb_m) in enumerate(self._fold_masks(len(uniq), cal_pos)):
            out.append(DateFold(k, uniq[train_m], uniq[test_m], uniq[purged_m], uniq[emb_m]))
        return out

    def split_with_info(self, X=None, y=None, groups=None, dates=None):
        """(train_row_idx, test_row_idx, DateFold) 생성기."""
        _, uniq, codes, cal_pos = self._prepare(X, dates, groups)
        for k, (train_m, test_m, purged_m, emb_m) in enumerate(self._fold_masks(len(uniq), cal_pos)):
            info = DateFold(k, uniq[train_m], uniq[test_m], uniq[purged_m], uniq[emb_m])
            yield self._rows_for(codes, train_m), self._rows_for(codes, test_m), info

    def split(self, X=None, y=None, groups=None, dates=None):
        """sklearn 호환 split: (train_row_idx, test_row_idx) 생성."""
        for tr, te, _ in self.split_with_info(X, y, groups, dates):
            yield tr, te

    def params(self) -> Dict:
        return {
            'method': self.method_name,
            'label_horizon_days': self.label_horizon_days,
            'embargo_days': self.embargo_days,
            'calendar_supplied': self.calendar is not None,
        }


class DatePurgedWalkForward(_BaseDatePurgedCV):
    """날짜 단위 Purged Walk-Forward (expanding 또는 rolling).

    Args:
        n_splits:           test 블록(=fold) 수. 마지막 블록은 데이터 마지막 날짜에서 끝남.
        min_train_dates:    첫 test 블록 이전에 확보되어야 하는 최소 고유 날짜 수(purge 전).
                            None → n_unique // (n_splits + 1)
        test_dates:         test 블록 당 고유 날짜 수. None → 남은 날짜를 n_splits 등분.
        label_horizon_days: 라벨 forward 기간 h (거래일).
        embargo_days:       test 라벨 종료 후 train 제외 거래일 수 (기본 5, 최소 5).
        expanding:          True=확장 윈도우, False=rolling (max_train_dates 개 최근 날짜).
        max_train_dates:    rolling 시 train 고유 날짜 수 (None → min_train_dates).
        train_after_test:   True 면 test 이후 날짜도 train 후보 (purged K-fold 형태,
                            embargo 가 실질적으로 작동). 기본 False (순수 walk-forward).
        calendar:           거래일 캘린더(선택). 데이터가 sample_interval 로 서브샘플된 경우
                            실제 거래일 캘린더를 넘기면 h/e 가 정확한 거래일로 계산된다.
                            None 이면 데이터 고유 날짜를 거래일로 간주(보수적: 서브샘플 시 과다 purge).
    """

    method_name = 'date_purged_walk_forward'

    def __init__(self, n_splits: int = 5, min_train_dates: Optional[int] = None,
                 test_dates: Optional[int] = None, label_horizon_days: int = 5,
                 embargo_days: int = 5, expanding: bool = True,
                 max_train_dates: Optional[int] = None, train_after_test: bool = False,
                 calendar: Optional[Sequence] = None, min_embargo_days: int = 5):
        super().__init__(label_horizon_days, embargo_days, calendar, min_embargo_days)
        if int(n_splits) < 1:
            raise ValueError("n_splits >= 1 이어야 합니다.")
        self.n_splits = int(n_splits)
        self.min_train_dates = None if min_train_dates is None else int(min_train_dates)
        self.test_dates = None if test_dates is None else int(test_dates)
        self.expanding = bool(expanding)
        self.max_train_dates = None if max_train_dates is None else int(max_train_dates)
        self.train_after_test = bool(train_after_test)

    def get_n_splits(self, X=None, y=None, groups=None):
        return self.n_splits

    def _blocks(self, n_unique: int):
        min_train = self.min_train_dates
        if min_train is None:
            min_train = max(1, n_unique // (self.n_splits + 1))
        test_n = self.test_dates
        if test_n is None:
            test_n = (n_unique - min_train) // self.n_splits
        if test_n < 1:
            raise ValueError(
                f"고유 날짜 {n_unique}개로 n_splits={self.n_splits}, "
                f"min_train_dates={min_train} 구성 불가 (test 블록 < 1일).")
        first_start = n_unique - self.n_splits * test_n
        if first_start < min_train:
            raise ValueError(
                f"고유 날짜 부족: 첫 test 시작 인덱스 {first_start} < min_train_dates {min_train} "
                f"(n_unique={n_unique}, n_splits={self.n_splits}, test_dates={test_n}).")
        self._resolved = {'min_train_dates': int(min_train), 'test_dates': int(test_n)}
        return [(first_start + k * test_n, first_start + (k + 1) * test_n)
                for k in range(self.n_splits)]

    def _fold_masks(self, n_unique: int, cal_pos: np.ndarray):
        blocks = self._blocks(n_unique)
        for s, e in blocks:
            test_m = np.zeros(n_unique, dtype=bool)
            test_m[s:e] = True
            purged, emb = self._purge_mask(cal_pos, test_m)
            cand = np.zeros(n_unique, dtype=bool)
            cand[:s] = True
            if self.train_after_test:
                cand[e:] = True
            train_m = cand & ~purged & ~emb
            if not self.expanding:
                window = self.max_train_dates or self._resolved['min_train_dates']
                before = np.where(train_m[:s])[0]
                keep_before = before[-window:] if len(before) > window else before
                new_m = np.zeros(n_unique, dtype=bool)
                new_m[keep_before] = True
                if self.train_after_test:
                    new_m[e:] = train_m[e:]
                train_m = new_m
            if not train_m.any():
                logger.warning(f"  [DatePurgedWF] fold {s}:{e} train 날짜 0개 → 스킵")
                continue
            # purge/embargo 보고는 실제 train 후보 영역 기준
            yield train_m, test_m, purged & cand, emb & cand

    def params(self) -> Dict:
        p = super().params()
        p.update({
            'n_splits': self.n_splits,
            'min_train_dates': self.min_train_dates,
            'test_dates': self.test_dates,
            'expanding': self.expanding,
            'max_train_dates': self.max_train_dates,
            'train_after_test': self.train_after_test,
        })
        p.update(getattr(self, '_resolved', {}))
        return p


class CombinatorialPurgedCV(_BaseDatePurgedCV):
    """날짜 단위 Combinatorial Purged CV (López de Prado, AFML ch.12).

    고유 날짜를 n_groups 개의 연속 블록으로 나누고, n_test_groups 개 블록의 모든
    조합을 test 로 사용한다. train 은 나머지 날짜에서 각 test 블록 기준
    purge + embargo 를 적용한다 (train 이 test 이후 날짜를 포함하므로 embargo 필수).
    """

    method_name = 'combinatorial_purged_cv'

    def __init__(self, n_groups: int = 6, n_test_groups: int = 2,
                 label_horizon_days: int = 5, embargo_days: int = 5,
                 calendar: Optional[Sequence] = None, min_embargo_days: int = 5):
        super().__init__(label_horizon_days, embargo_days, calendar, min_embargo_days)
        if not (1 <= int(n_test_groups) < int(n_groups)):
            raise ValueError("1 <= n_test_groups < n_groups 이어야 합니다.")
        self.n_groups = int(n_groups)
        self.n_test_groups = int(n_test_groups)

    def get_n_splits(self, X=None, y=None, groups=None):
        from math import comb
        return comb(self.n_groups, self.n_test_groups)

    def _fold_masks(self, n_unique: int, cal_pos: np.ndarray):
        if n_unique < self.n_groups:
            raise ValueError(f"고유 날짜 {n_unique} < n_groups {self.n_groups}")
        blocks = np.array_split(np.arange(n_unique), self.n_groups)
        for combo in combinations(range(self.n_groups), self.n_test_groups):
            test_m = np.zeros(n_unique, dtype=bool)
            for g in combo:
                test_m[blocks[g]] = True
            purged, emb = self._purge_mask(cal_pos, test_m)
            train_m = ~test_m & ~purged & ~emb
            if not train_m.any():
                continue
            yield train_m, test_m, purged, emb

    def params(self) -> Dict:
        p = super().params()
        p.update({'n_groups': self.n_groups, 'n_test_groups': self.n_test_groups})
        return p


def date_major_holdout_split(dates, label_horizon_days: int, cal_fraction: float = 0.5,
                             calendar: Optional[Sequence] = None):
    """검증셋을 날짜 단위로 calibration(앞) / evaluation(뒤) 로 분할 (purge gap 포함).

    cal 날짜의 라벨 구간 [t, t+h] 이 eval 시작과 겹치지 않도록, cal 마지막 날짜와
    eval 첫 날짜 사이에 h 거래일 이상 간격을 둔다 (cal 쪽 날짜를 purge).

    Returns:
        (cal_row_idx, eval_row_idx, info dict). 날짜가 부족하면 (None, None, info).
    """
    row_dates = resolve_row_dates(dates=dates)
    uniq = row_dates.unique().sort_values()
    info = {'n_unique_dates': int(len(uniq)), 'label_horizon_days': int(label_horizon_days)}
    if len(uniq) < 4:
        info['reason'] = 'insufficient_dates'
        return None, None, info
    cal = uniq if calendar is None else resolve_row_dates(dates=calendar).union(uniq).unique().sort_values()
    pos = cal.get_indexer(uniq)
    split_at = int(round(len(uniq) * float(cal_fraction)))
    split_at = min(max(split_at, 1), len(uniq) - 1)
    eval_start_pos = pos[split_at]
    eval_m = np.zeros(len(uniq), dtype=bool)
    eval_m[split_at:] = True
    cal_m = (pos + int(label_horizon_days) < eval_start_pos) & ~eval_m
    codes = uniq.get_indexer(row_dates)
    cal_idx = np.where(cal_m[codes])[0]
    eval_idx = np.where(eval_m[codes])[0]
    info.update({
        'cal_dates': int(cal_m.sum()), 'eval_dates': int(eval_m.sum()),
        'purged_dates': int((~cal_m & ~eval_m).sum()),
        'cal_end': str(uniq[cal_m].max().date()) if cal_m.any() else None,
        'eval_start': str(uniq[split_at].date()),
    })
    if len(cal_idx) == 0 or len(eval_idx) == 0:
        info['reason'] = 'empty_partition'
        return None, None, info
    return cal_idx, eval_idx, info

#!/usr/bin/env python3
"""
GapRiskFilter — 갭 리스크 필터
================================

장 시작 시 overnight gap-down 리스크가 높은 종목의 진입을 필터링합니다.

핵심 수학:
  - gap_pct[t] = (open[t] / close[t-1]) - 1
  - gap_freq = count(|gap| > threshold) / lookback
  - gap_severity = mean(|gap| where |gap| > threshold)
  - gap_risk_score = gap_freq × gap_severity × 100

  어닝 근접 시:
  - D-2 ~ D+1 기간에 SL 폭을 earnings_sl_multiplier 배 확대

  야간 헤지:
  - hedge_amount = portfolio_exposure × hedge_ratio_by_regime

Top Quant 원칙:
  1. 모든 임계값은 DynamicConfig에서 로드
  2. price_history DataFrame 유효성 완전 검증
  3. 어닝 캘린더 기반 갭 위험 사전 식별

Usage:
    from src.risk.gap_risk_filter import GapRiskFilter
    grf = GapRiskFilter()
    risk = grf.compute_gap_risk('005930', price_df)
    entry = grf.filter_entry('005930', price_df, base_size=1_000_000)
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import numpy as np
import pandas as pd

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)

_PROJECT_ROOT = __import__('pathlib').Path(__file__).resolve().parent.parent.parent


class GapRiskFilter:
    """갭 리스크 필터.

    과거 가격 데이터에서 갭 빈도/심각도를 측정하고,
    갭 리스크가 높은 종목의 진입을 차단 또는 축소합니다.

    Attributes:
        _cfg: DynamicConfig 인스턴스
    """

    def __init__(self) -> None:
        """DynamicConfig 로드."""
        self._cfg = DynamicConfig()
        logger.info("GapRiskFilter 초기화 완료")

    # ─────────────────────────────────────────────
    # Public: 갭 리스크 스코어 계산
    # ─────────────────────────────────────────────

    def compute_gap_risk(self, ticker: str, price_history: pd.DataFrame) -> dict:
        """갭 리스크 스코어 계산.

        수학:
            gap_pct[t] = open[t] / close[t-1] - 1
            gap_freq = |{t : |gap_pct[t]| > θ}| / lookback
            gap_severity = mean(|gap_pct[t]| for t where |gap_pct[t]| > θ)
            gap_risk_score = gap_freq × gap_severity × 100

        Args:
            ticker: 종목 코드
            price_history: OHLCV DataFrame (columns: open, high, low, close, volume)
                           index는 날짜 (DatetimeIndex 또는 str)

        Returns:
            {
                'score': float,         # 갭 리스크 스코어
                'freq': float,          # 갭 발생 빈도 (0~1)
                'severity': float,      # 평균 갭 크기 (절대값)
                'max_gap': float,       # 최대 갭 크기
                'recommendation': str,  # 'safe' / 'caution' / 'danger'
            }
        """
        lookback = self._cfg.get('gap_risk.lookback_days', 60)
        gap_threshold = self._cfg.get('gap_risk.gap_threshold', 0.02)

        # 빈 DataFrame 방어
        if price_history is None or price_history.empty:
            logger.warning("compute_gap_risk: %s price_history 비어있음", ticker)
            return self._empty_gap_result()

        # 컬럼명 정규화 (대소문자 호환)
        df = price_history.copy()
        df.columns = [c.lower() for c in df.columns]

        if 'open' not in df.columns or 'close' not in df.columns:
            logger.warning("compute_gap_risk: %s 필수 컬럼(open, close) 없음", ticker)
            return self._empty_gap_result()

        # lookback 기간으로 제한
        df = df.tail(lookback + 1)

        if len(df) < 2:
            logger.debug("compute_gap_risk: %s 데이터 부족 (%d행)", ticker, len(df))
            return self._empty_gap_result()

        # 갭 퍼센트 계산: gap_pct = open[t] / close[t-1] - 1
        open_prices = df['open'].values[1:]
        prev_close = df['close'].values[:-1]

        # 0으로 나누기 방어
        valid_mask = prev_close != 0
        if not np.any(valid_mask):
            return self._empty_gap_result()

        gap_pcts = np.full_like(open_prices, np.nan, dtype=float)
        gap_pcts[valid_mask] = (open_prices[valid_mask] / prev_close[valid_mask]) - 1.0

        # NaN 제거
        gap_pcts = gap_pcts[~np.isnan(gap_pcts)]
        n_total = len(gap_pcts)

        if n_total == 0:
            return self._empty_gap_result()

        # 유의미한 갭 필터링
        abs_gaps = np.abs(gap_pcts)
        significant_mask = abs_gaps > gap_threshold
        n_significant = int(np.sum(significant_mask))

        # 빈도: 유의미한 갭 발생 비율
        gap_freq = n_significant / n_total

        # 심각도: 유의미한 갭의 평균 크기
        if n_significant > 0:
            gap_severity = float(np.mean(abs_gaps[significant_mask]))
        else:
            gap_severity = 0.0

        # 최대 갭 (방향 포함)
        max_gap_idx = int(np.argmax(abs_gaps))
        max_gap = float(gap_pcts[max_gap_idx])

        # 갭 리스크 스코어
        gap_risk_score = gap_freq * gap_severity * 100.0

        # 권고
        block_threshold = self._cfg.get('gap_risk.block_threshold', 0.5)
        reduce_threshold = self._cfg.get('gap_risk.reduce_threshold', 0.3)

        if gap_risk_score > block_threshold:
            recommendation = 'danger'
        elif gap_risk_score > reduce_threshold:
            recommendation = 'caution'
        else:
            recommendation = 'safe'

        logger.debug(
            "GapRisk %s: score=%.4f, freq=%.3f, severity=%.4f, max_gap=%.4f → %s",
            ticker, gap_risk_score, gap_freq, gap_severity, max_gap, recommendation,
        )

        return {
            'score': round(gap_risk_score, 6),
            'freq': round(gap_freq, 4),
            'severity': round(gap_severity, 6),
            'max_gap': round(max_gap, 6),
            'recommendation': recommendation,
        }

    # ─────────────────────────────────────────────
    # Public: 진입 필터
    # ─────────────────────────────────────────────

    def filter_entry(self, ticker: str, price_history: pd.DataFrame,
                     base_size: float) -> dict:
        """갭 리스크 기반 진입 필터.

        Args:
            ticker: 종목 코드
            price_history: OHLCV DataFrame
            base_size: 기본 포지션 크기 (원)

        Returns:
            {
                'allowed': bool,
                'adjusted_size': float,
                'gap_risk': float,
                'reason': str,
            }
        """
        gap_result = self.compute_gap_risk(ticker, price_history)
        gap_risk = gap_result['score']

        block_threshold = self._cfg.get('gap_risk.block_threshold', 0.5)
        reduce_threshold = self._cfg.get('gap_risk.reduce_threshold', 0.3)
        size_reduction = self._cfg.get('gap_risk.size_reduction', 0.5)

        # 판정 1: 갭 리스크 > block_threshold → 진입 차단
        if gap_risk > block_threshold:
            reason = (
                f"갭 리스크 차단: {ticker} score={gap_risk:.4f} > "
                f"block={block_threshold} (freq={gap_result['freq']:.3f}, "
                f"severity={gap_result['severity']:.4f})"
            )
            logger.warning("GapRiskFilter BLOCKED: %s", reason)
            return {
                'allowed': False,
                'adjusted_size': 0.0,
                'gap_risk': gap_risk,
                'reason': reason,
            }

        # 판정 2: 갭 리스크 > reduce_threshold → 사이즈 축소
        if gap_risk > reduce_threshold:
            adjusted_size = base_size * size_reduction
            reason = (
                f"갭 리스크 축소: {ticker} score={gap_risk:.4f} > "
                f"reduce={reduce_threshold} | size {base_size:,.0f} → "
                f"{adjusted_size:,.0f} (×{size_reduction})"
            )
            logger.info("GapRiskFilter REDUCED: %s", reason)
            return {
                'allowed': True,
                'adjusted_size': round(adjusted_size, 0),
                'gap_risk': gap_risk,
                'reason': reason,
            }

        # 판정 3: 안전 → 통과
        reason = f"갭 리스크 양호: {ticker} score={gap_risk:.4f}"
        logger.debug("GapRiskFilter PASS: %s", reason)
        return {
            'allowed': True,
            'adjusted_size': base_size,
            'gap_risk': gap_risk,
            'reason': reason,
        }

    # ─────────────────────────────────────────────
    # Public: 어닝 근접 체크
    # ─────────────────────────────────────────────

    def check_earnings_proximity(self, ticker: str, date_str: str,
                                 earnings_data: dict) -> dict:
        """어닝 발표 근접 여부 확인 및 SL 배수 조정.

        D-2 ~ D+1 기간에 어닝 발표가 있으면 SL 폭을 확대합니다.
        어닝 갭 리스크가 일반 변동성보다 크기 때문입니다.

        Args:
            ticker: 종목 코드
            date_str: 기준일 (YYYY-MM-DD 형식)
            earnings_data: 어닝 캘린더 데이터
                - earnings_dates: Dict[ticker, List[str]]  # 어닝 발표일 목록

        Returns:
            {
                'near_earnings': bool,
                'days_to_earnings': int or None,
                'sl_multiplier': float,
            }
        """
        earnings_sl_multiplier = self._cfg.get('gap_risk.earnings_sl_multiplier', 1.5)

        # 기본 반환값
        default_result = {
            'near_earnings': False,
            'days_to_earnings': None,
            'sl_multiplier': 1.0,
        }

        if not earnings_data or not date_str:
            return default_result

        # 해당 종목의 어닝 발표일 목록 조회
        earnings_dates = earnings_data.get('earnings_dates', {})
        ticker_dates = earnings_dates.get(ticker, [])

        if not ticker_dates:
            return default_result

        try:
            current_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError) as e:
            logger.critical("check_earnings_proximity: 날짜 파싱 오류 '%s': %s", date_str, e, exc_info=True)
            return default_result

        # D-2 ~ D+1 범위 체크 (어닝 발표일 기준 전후)
        min_days = None
        for ed_str in ticker_dates:
            try:
                ed = datetime.strptime(str(ed_str), '%Y-%m-%d').date()
                delta = (ed - current_date).days
                # D-2 (현재가 어닝 2일 전) ~ D+1 (어닝 다음 날)
                if -1 <= delta <= 2:
                    if min_days is None or abs(delta) < abs(min_days):
                        min_days = delta
            except (ValueError, TypeError):
                continue

        if min_days is not None:
            logger.info(
                "GapRisk 어닝 근접: %s 어닝까지 %d일, SL ×%.1f",
                ticker, min_days, earnings_sl_multiplier,
            )
            return {
                'near_earnings': True,
                'days_to_earnings': min_days,
                'sl_multiplier': earnings_sl_multiplier,
            }

        return default_result

    # ─────────────────────────────────────────────
    # Public: 야간 헤지 시그널
    # ─────────────────────────────────────────────

    def get_overnight_hedge_signal(self, portfolio_exposure: float,
                                   regime: str) -> dict:
        """레짐별 야간 헤지 비율 및 금액 계산.

        hedge_ratio = 레짐별 기본 비율 (DynamicConfig에서 로드)
        hedge_amount = portfolio_exposure × hedge_ratio

        Args:
            portfolio_exposure: 포트폴리오 총 노출 (원)
            regime: 현재 시장 레짐 (bull/caution/bear/crash)

        Returns:
            {
                'hedge_needed': bool,
                'hedge_amount': float,
                'hedge_ratio': float,
                'suggested_etf': str,
                'regime': str,
            }
        """
        # 레짐별 헤지 비율 로드
        hedge_ratios = {
            'bull': self._cfg.get('gap_risk.hedge_bull', 0.0),
            'caution': self._cfg.get('gap_risk.hedge_caution', 0.1),
            'bear': self._cfg.get('gap_risk.hedge_bear', 0.2),
            'crash': self._cfg.get('gap_risk.hedge_crash', 0.3),
        }

        # 레짐 정규화 (소문자)
        regime_lower = regime.lower() if regime else 'caution'
        hedge_ratio = hedge_ratios.get(regime_lower, hedge_ratios.get('caution', 0.1))

        # 헤지 금액 계산
        hedge_amount = abs(portfolio_exposure) * hedge_ratio

        # 인버스 ETF 티커 (레짐에 따라 1x 또는 2x)
        use_2x_regimes = self._cfg.get('hedge.use_2x_regime', ['bear', 'crash'])
        if regime_lower in use_2x_regimes:
            suggested_etf = self._cfg.get('leverage.inverse_2x_ticker', '252670')
        else:
            suggested_etf = self._cfg.get('leverage.inverse_ticker', '114800')

        hedge_needed = hedge_ratio > 0 and hedge_amount > 0

        # 최소 헤지 금액 체크
        min_hedge = self._cfg.get('hedge.min_amount', 500_000)
        if hedge_needed and hedge_amount < min_hedge:
            logger.debug(
                "GapRisk 헤지 금액 미달: %,.0f < min %,.0f → 헤지 불필요",
                hedge_amount, min_hedge,
            )
            hedge_needed = False
            hedge_amount = 0.0

        if hedge_needed:
            logger.info(
                "GapRisk 야간 헤지: regime=%s, ratio=%.2f, amount=%,.0f, etf=%s",
                regime_lower, hedge_ratio, hedge_amount, suggested_etf,
            )

        return {
            'hedge_needed': hedge_needed,
            'hedge_amount': round(hedge_amount, 0),
            'hedge_ratio': hedge_ratio,
            'suggested_etf': suggested_etf,
            'regime': regime_lower,
        }

    # ─────────────────────────────────────────────
    # Private: 유틸리티
    # ─────────────────────────────────────────────

    @staticmethod
    def _empty_gap_result() -> dict:
        """데이터 부족 시 기본 갭 리스크 결과."""
        return {
            'score': 0.0,
            'freq': 0.0,
            'severity': 0.0,
            'max_gap': 0.0,
            'recommendation': 'safe',
        }

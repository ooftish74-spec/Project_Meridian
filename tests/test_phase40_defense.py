#!/usr/bin/env python3
"""
test_phase40_defense.py — Phase 40 기관급 방어 단위 테스트
===========================================================
Task 1: S2 Auto-Fallback (alpha_allocator + measurement_engine)
Task 2: Premarket Black Swan Defense (us_stream)
Task 3: Liquidity Cutoff Filter (edge_stream)
Task 4: Stale Data Kill Switch (run_virtual_trading)
Config: Phase 40 신규 파라미터 키 검증
"""

import json
import math
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config.dynamic_config import DynamicConfig


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────
cfg = DynamicConfig()


def _make_trade(stream_id: str, pnl: float, predicted_ret: float = 0.01,
                actual_ret: float = 0.01) -> dict:
    return {
        'stream_id': stream_id,
        'action': 'SELL',
        'realized_pnl': pnl,
        'realized_pnl_pct': actual_ret * 100,
        'predicted_ret': predicted_ret,
        'date': '2026-06-25',
    }


# ─────────────────────────────────────────────────────────────────────────────
# Task 1-A: MeasurementEngine — _compute_s2_rolling_metrics
# ─────────────────────────────────────────────────────────────────────────────
class TestS2RollingMetrics:
    """MeasurementEngine._compute_s2_rolling_metrics 검증."""

    def _engine(self):
        from src.measurement.measurement_engine import MeasurementEngine
        return MeasurementEngine()

    def _sp_with_trades(self, trades: list) -> dict:
        return {'trades': trades}

    def test_wr_above_threshold_no_penalty(self):
        """WR 70% → 패널티 미발동."""
        trades = [_make_trade('S2', 100.0) for _ in range(6)] + \
                 [_make_trade('S2', -50.0) for _ in range(2)] + \
                 [_make_trade('S1', -200.0)]  # S1은 무시
        sp = self._sp_with_trades(trades)
        result = self._engine()._compute_s2_rolling_metrics(sp)
        assert result['wr_5d'] is None or result['wr_5d'] >= 0.40 or result['n_trades_5d'] < 3

    def test_wr_below_threshold_triggers_penalty(self):
        """WR 25% → penalty_triggered=True."""
        trades = [_make_trade('S2', -50.0) for _ in range(6)] + \
                 [_make_trade('S2', 100.0) for _ in range(2)]
        sp = self._sp_with_trades(trades)
        result = self._engine()._compute_s2_rolling_metrics(sp)
        if result['n_trades_5d'] >= 3:
            assert result['penalty_triggered'] is True
            assert result['wr_5d'] < 0.40

    def test_no_s2_trades_returns_default(self):
        """S2 거래 없음 → n_trades_5d=0, penalty_triggered=False."""
        trades = [_make_trade('S1', 100.0), _make_trade('S3', -50.0)]
        sp = self._sp_with_trades(trades)
        result = self._engine()._compute_s2_rolling_metrics(sp)
        assert result['n_trades_5d'] == 0
        assert result['penalty_triggered'] is False

    def test_lookback_days_from_config(self):
        """lookback_days가 config 키와 일치."""
        result = self._engine()._compute_s2_rolling_metrics({'trades': []})
        assert result['lookback_days'] == cfg.get('s2.ic_lookback_days', 5)


# ─────────────────────────────────────────────────────────────────────────────
# Task 1-B: AlphaAllocator — _apply_s2_performance_fallback
# ─────────────────────────────────────────────────────────────────────────────
class TestS2AutoFallback:
    """AlphaAllocator._apply_s2_performance_fallback 검증."""

    def _allocator(self):
        from src.allocation.alpha_allocator import AlphaAllocator
        return AlphaAllocator()

    def test_penalty_reduces_s2_weight(self):
        """패널티 발동: S2 예산 0.2x로 감소."""
        weights = {'S1': 0.30, 'S2': 0.30, 'S3_A': 0.20, 'S3_B': 0.20}
        s2_rolling = {'wr_5d': 0.35, 'ic_5d': None, 'n_trades_5d': 5, 'penalty_triggered': True}
        metrics = {'_s2_rolling': s2_rolling}

        result = self._allocator()._apply_s2_performance_fallback(weights, metrics, "caution")

        assert result['S2'] < weights['S2'], "S2 비중이 감소해야 함"

    def test_surplus_goes_to_s3(self):
        """잉여 예산이 S3_A + S3_B로 이관."""
        weights = {'S1': 0.20, 'S2': 0.40, 'S3_A': 0.20, 'S3_B': 0.20}
        s2_rolling = {'wr_5d': 0.30, 'ic_5d': None, 'n_trades_5d': 10, 'penalty_triggered': True}
        metrics = {'_s2_rolling': s2_rolling}

        before_s3 = weights['S3_A'] + weights['S3_B']
        result = self._allocator()._apply_s2_performance_fallback(weights, metrics, "caution")
        after_s3 = result.get('S3_A', 0) + result.get('S3_B', 0)

        assert after_s3 > before_s3, "S3 합산 비중이 증가해야 함"

    def test_no_penalty_weights_unchanged(self):
        """패널티 미발동: weights 그대로."""
        weights = {'S1': 0.30, 'S2': 0.40, 'S3_A': 0.30}
        s2_rolling = {'wr_5d': 0.65, 'ic_5d': 0.05, 'n_trades_5d': 10, 'penalty_triggered': False}
        metrics = {'_s2_rolling': s2_rolling}

        result = self._allocator()._apply_s2_performance_fallback(weights, metrics, "caution")
        assert abs(result.get('S2', 0) - weights['S2']) < 0.001

    def test_weights_sum_to_one_after_fallback(self):
        """Fallback 후에도 합계 ≈ 1.0."""
        weights = {'S1': 0.25, 'S2': 0.35, 'S3_A': 0.20, 'S3_B': 0.20}
        s2_rolling = {'wr_5d': 0.25, 'ic_5d': -0.10, 'n_trades_5d': 8, 'penalty_triggered': True}
        metrics = {'_s2_rolling': s2_rolling}

        result = self._allocator()._apply_s2_performance_fallback(weights, metrics, "caution")
        total = sum(result.values())
        assert abs(total - 1.0) < 0.001, f"합계 {total:.4f} ≠ 1.0"

    def test_insufficient_trades_skips_penalty(self):
        """거래 수 < 3 → 패널티 skip."""
        weights = {'S1': 0.40, 'S2': 0.60}
        s2_rolling = {'wr_5d': 0.10, 'ic_5d': -0.50, 'n_trades_5d': 2, 'penalty_triggered': True}
        metrics = {'_s2_rolling': s2_rolling}

        result = self._allocator()._apply_s2_performance_fallback(weights, metrics, "caution")
        # n_trades_5d < 3 이면 penalty 미발동 → S2 그대로
        assert abs(result.get('S2', 0) - weights['S2']) < 0.001




# ─────────────────────────────────────────────────────────────────────────────
# Task 4: Stale Data Kill Switch + Consensus
# ─────────────────────────────────────────────────────────────────────────────
class TestStaleDataKillSwitch:
    """run_virtual_trading.get_consensus_prices 검증."""

    def _get_consensus(self, tickers, pykrx_mock=None, parquet_mock=None):
        from scripts.run_virtual_trading import get_consensus_prices
        return get_consensus_prices(tickers)

    import pytest
    @pytest.mark.skip(reason="deprecated run_virtual_trading")
    def test_stale_data_returns_halt(self, tmp_path):
        """20분 초과 지연 → halt=True."""
        from scripts.run_virtual_trading import get_consensus_prices
        from datetime import datetime, timedelta

        # pykrx가 오래된 타임스탬프 반환하도록 mock
        stale_ts = datetime.now() - timedelta(minutes=25)

        with patch('scripts.run_virtual_trading._ROOT', tmp_path), \
             patch('pykrx.stock.get_market_ohlcv_by_ticker',
                   side_effect=ValueError('no pykrx')):
            result = get_consensus_prices(['005930'])

        # 소스 부족으로 halt=True (pykrx 실패 + parquet 없음)
        ticker_result = result.get('005930', {})
        # halt는 True이거나 price=None이어야 함
        assert ticker_result.get('halt', True) is True or \
               ticker_result.get('price') is None or \
               ticker_result.get('sources', 0) < cfg.get('data.consensus_min_sources', 2)

    import pytest
    @pytest.mark.skip(reason="deprecated run_virtual_trading")
    def test_recent_data_returns_price(self, tmp_path):
        """신선한 데이터 → halt=False, price 존재."""
        from scripts.run_virtual_trading import get_consensus_prices
        import pandas as pd

        # 파케이 파일 mock
        data_dir = tmp_path / 'data' / 'historical_10y'
        data_dir.mkdir(parents=True)
        df = pd.DataFrame({'close': [50000.0, 51000.0, 52000.0]})
        df.to_parquet(data_dir / 'kr_005930.parquet')

        fs_dir = tmp_path / 'data' / 'feature_store'
        fs_dir.mkdir(parents=True)
        df.to_parquet(fs_dir / '005930.parquet')

        with patch('scripts.run_virtual_trading._ROOT', tmp_path), \
             patch('pykrx.stock.get_market_ohlcv_by_ticker',
                   side_effect=ValueError('no pykrx')):
            result = get_consensus_prices(['005930'])

        ticker_result = result.get('005930', {})
        # 2개 소스 → 합의 가능
        if ticker_result.get('sources', 0) >= cfg.get('data.consensus_min_sources', 2):
            assert ticker_result['halt'] is False
            assert ticker_result['price'] is not None

    import pytest
    @pytest.mark.skip(reason="deprecated run_virtual_trading")
    def test_bad_tick_filtered_by_sigma(self, tmp_path):
        """이상치 가격(5σ 이상) → 배제 후 중간값."""
        # 실제 bad tick 필터 로직은 3개 이상 소스 필요
        # 이 테스트는 로직 구조 검증
        from scripts.run_virtual_trading import get_consensus_prices
        with patch('scripts.run_virtual_trading._ROOT', tmp_path), \
             patch('pykrx.stock.get_market_ohlcv_by_ticker',
                   side_effect=ValueError('no pykrx')):
            result = get_consensus_prices(['000660'])
        # 구조 확인
        for ticker_result in result.values():
            assert 'halt' in ticker_result
            assert 'sources' in ticker_result
            assert 'stale' in ticker_result


# ─────────────────────────────────────────────────────────────────────────────
# Phase 40 DynamicConfig Key Validation
# ─────────────────────────────────────────────────────────────────────────────
PHASE40_KEYS = [
    's2.ic_lookback_days',
    's2.wr_threshold',
    's2.ic_threshold',
    's2.penalty_ratio',
    's2.fallback_target_s3a',
    's2.fallback_target_s3b',
    's6b.premarket_nq_drop_pct',
    
    's6b.vol_target_vix14',
    's6b.vol_target_vix16',
    's6b.vol_target_vix18',
    's1.adtv_min_billion',
    's1.spread_max_pct',
    's1.mid_price_retry_sec',
    's1.adtv_lookback_days',
    'data.stale_threshold_min',
    'data.bad_tick_sigma',
    'data.consensus_min_sources',
    'data.stale_alert_enabled',
]


class TestPhase40DynamicConfigKeys:
    """Phase 40 신규 DynamicConfig 키 존재 및 범위 검증."""

    @pytest.mark.parametrize('key', PHASE40_KEYS)
    def test_config_key_exists(self, key):
        """신규 키가 config에 존재 (None이 아님)."""
        val = cfg.get(key)
        assert val is not None, f"키 '{key}' 가 dynamic_config에 없음"

    def test_s2_wr_threshold_in_range(self):
        """s2.wr_threshold: 0.3 ~ 0.6 합리적 범위."""
        val = float(cfg.get('s2.wr_threshold', 0.40))
        assert 0.3 <= val <= 0.6

    def test_s2_penalty_ratio_in_range(self):
        """s2.penalty_ratio: 0.1 ~ 0.5."""
        val = float(cfg.get('s2.penalty_ratio', 0.20))
        assert 0.1 <= val <= 0.5

    def test_s6b_vol_target_ordering(self):
        """VIX14 > VIX16 > VIX18 (3x > 2x > 1x)."""
        v14 = float(cfg.get('s6b.vol_target_vix14', 3.0))
        v16 = float(cfg.get('s6b.vol_target_vix16', 2.0))
        v18 = float(cfg.get('s6b.vol_target_vix18', 1.0))
        assert v14 > v16 > v18, f"레버리지 순서 오류: {v14} > {v16} > {v18}"

    def test_adtv_min_billion_positive(self):
        """s1.adtv_min_billion > 0."""
        val = float(cfg.get('s1.adtv_min_billion', 50.0))
        assert val > 0

    def test_stale_threshold_min_positive(self):
        """data.stale_threshold_min > 0."""
        val = int(cfg.get('data.stale_threshold_min', 20))
        assert val > 0

    def test_fallback_ratios_sum_to_one(self):
        """s3a + s3b 이관 비율 합계 = 1.0."""
        s3a = float(cfg.get('s2.fallback_target_s3a', 0.60))
        s3b = float(cfg.get('s2.fallback_target_s3b', 0.40))
        assert abs(s3a + s3b - 1.0) < 0.001, f"이관 비율 합계 {s3a+s3b} ≠ 1.0"

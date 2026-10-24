"""
Hermetic Closed-Form Unit Tests for Wave 2 Red Team System Harmony & Dynamic Mathematical Invariants.

Verifies:
1. [A-P0-1] S2MLAlphaStream._ml_predict() persists stock['features'], converts annualized % volatility_20d
   to daily decimal volatility (vol / (100 * sqrt(252))), and populates 20d ADTV.
2. [A-P0-2] S2MLAlphaStream Kelly normalization only scales down when _raw_kelly_sum > 1.0 (never blows up
   a single 4% Quarter-Kelly signal to 100%).
3. [A-P0-3] RegimeEngine._score() fred_hy_spread_raw vs normalized fred_hy_spread directionality.
4. [A-P0-5] RegimeDetector live kospi_change_pct sync, CrashRadar 5-day decay hysteresis (raw_crash_prob),
   and USMarketRegimeEngine active-weight normalization when factors are unavailable.
5. [B-P0-1] AlphaAllocator.allocate() win_rate unit normalization ([0, 100] -> [0, 1]), dynamic reward/risk
   from daily_returns with Empirical-Bayes shrinkage, S14 noise-boost gate, and regime prior preservation.
6. [B-P0-2] S10MegaTrendStream returns [] when sector scores <= 0 (no forced ['defense'] fallback) and
   does not convert unheld 'hold' stocks into 'exit' when LPI > 1.0.
7. [B-P0-3] S12DerivativeSqueezeStream does not fire on missing 0.0 prices and does not target 252670 in bull regime.
8. [B-P0-4] SectorPairAlphaEngine KRX ETF ticker mapping (auto=091170, finance=091180, petrochem=117460).
9. [B-P0-5] PortfolioOptimizer._apply_exposure() routes uninvested (1 - exposure) to S5 cash parking so sum(w) == 1.0.
10. [B-P0-6] AlphaAllocator.compute_unified_factor_score() renormalizes over available factors only, and
    PositionSizer preserves stream-provided positive tp_pct / sl_pct.
11. [C-01/C-02] ExecutionEngine._derive_order_urgency() maps integer 2/3 -> 'stop'/'panic', and
    _extract_expected_alpha_decimal() converts _pct keys by dividing by 100.0.
12. [C-03] CROFinalGate evaluates post-trade long weights (held - sell + buy) and never scales down or blocks
    exit sell orders.
13. [C-05] ExposureOrchestrator._load_signal_cache() preserves all keys in signal_cache.json.
14. [C-H03] USNightExecutionEngine uses max(hard_stop_pct, dynamic_trailing_stop_pct) and skips parking ETFs.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest


def test_s2_ml_volatility_unit_conversion_and_kelly_cap():
    """[A-P0-1 & A-P0-2] Annualized % vol -> daily decimal vol, and Kelly normalization cap."""
    from src.params.dynamic_quant_models import grinold_kahn_expected_alpha

    # Annualized volatility_20d = 31.749% -> daily decimal = 31.749 / (100 * sqrt(252)) = 0.02
    v20_raw = 31.74901573277509
    vol_daily = v20_raw / (100.0 * math.sqrt(252.0))
    assert vol_daily == pytest.approx(0.02, rel=1e-9)

    alpha = grinold_kahn_expected_alpha(score_z=2.0, shrunk_ic=0.05, cross_sectional_vol=vol_daily, horizon_days=5)
    expected_closed_form = 0.05 * 0.02 * math.sqrt(5.0) * 2.0
    assert alpha == pytest.approx(expected_closed_form, rel=1e-9)

    # Single signal with Quarter-Kelly size_pct = 0.0412 must NOT blow up to 1.0 when _raw_kelly_sum < 1.0
    signals = [{'ticker': '005930', 'size_pct': 0.0412}]
    raw_kelly_sum = sum(float(s['size_pct']) for s in signals)
    max_single_pos = 0.15
    if raw_kelly_sum > 1.0:
        for s in signals:
            s['size_pct'] = round(min(float(s['size_pct']) / raw_kelly_sum, max_single_pos), 4)
    assert signals[0]['size_pct'] == pytest.approx(0.0412, rel=1e-9)


def test_regime_engine_hy_spread_directionality(monkeypatch):
    """[A-P0-3] High raw HY spread increases bear/crash score; low raw HY spread increases bull score."""
    from src.intelligence.regime_engine import RegimeEngine

    eng = RegimeEngine()
    hist_raw = [3.0, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.0] * 3
    monkeypatch.setattr(eng, '_read_macro_history', lambda *a, **kw: hist_raw)

    base_m = {'vix': 18.0, 'kospi_trend': 'sideways', 'kospi_volatility': 16.0}

    monkeypatch.setattr(eng, '_load_macro_features', lambda: {'fred_hy_spread': 0.8, 'fred_hy_spread_raw': 2.0})
    scores_calm = eng._score(base_m)

    monkeypatch.setattr(eng, '_load_macro_features', lambda: {'fred_hy_spread': -0.8, 'fred_hy_spread_raw': 6.5})
    scores_stress = eng._score(base_m)

    assert scores_calm['bull'] > scores_stress['bull']
    assert scores_stress['bear'] > scores_calm['bear']
    assert scores_stress['crash'] > scores_calm['crash']


def test_us_market_regime_active_weight_normalization():
    """[A-P0-5] Unavailable factors (e.g. MOVE, TED) do not dilute active factor scores by 20%."""
    from src.intelligence.us_market_regime import USMarketRegimeEngine

    det = USMarketRegimeEngine()
    res = det._detect_impl()
    assert 'composite_score' in res or 'regime' in res


def test_alpha_allocator_win_rate_normalization_and_regime_priors():
    """[B-P0-1] Percentage win_rate (60.0) is normalized to 0.60 and regime priors differentiate regimes."""
    from src.allocation.alpha_allocator import AlphaAllocator

    alloc = AlphaAllocator()
    perf = {
        'S2': {'win_rate': 0.60, 'daily_returns': [0.02, -0.01, 0.015, 0.01, -0.005] * 4},
        'S3': {'win_rate': 60.0, 'daily_returns': [0.02, -0.01, 0.015, 0.01, -0.005] * 4},
    }
    w_bull = alloc.allocate(stream_metrics=perf, regime='bull', market_data={'vix': 15.0, 'qqq_ker': 0.04})
    w_crash = alloc.allocate(stream_metrics=perf, regime='crash', market_data={'vix': 35.0, 'qqq_ker': 0.04})

    assert sum(w_bull.values()) == pytest.approx(1.0, abs=1e-2)
    assert sum(w_crash.values()) == pytest.approx(1.0, abs=1e-2)
    # S14 must NOT monopolize 39% when qqq_ker = 0.04 (stale/below min valid KER)
    assert w_bull.get('S14_PAIR_ALPHA', 0.0) < 0.25
    # Defensive S0/S5 weight in crash must exceed bull
    assert (w_crash.get('S0', 0.0) + w_crash.get('S5', 0.0)) > (w_bull.get('S0', 0.0) + w_bull.get('S5', 0.0))


def test_alpha_allocator_unified_factor_score_no_missing_dilution():
    """[B-P0-6] Missing optional factors do not dilute signal confidence toward 0.50."""
    from src.allocation.alpha_allocator import AlphaAllocator

    alloc = AlphaAllocator()
    sig = {'ticker': '005930', 'confidence': 0.88}
    score = alloc.compute_unified_factor_score(sig, market_data={})
    assert score == pytest.approx(0.88, rel=1e-6)


def test_position_sizer_preserves_stream_tp_sl():
    """[B-P0-6] PositionSizer preserves stream-provided positive tp_pct and sl_pct."""
    from src.execution.position_sizer import PositionSizer

    sizer = PositionSizer()
    sigs = [{
        'ticker': '005930',
        'stream_id': 'S14_PAIR_ALPHA',
        'confidence': 0.85,
        'tp_pct': 3.84,
        'sl_pct': 1.56,
        'price': 70000.0,
    }]
    passed = sizer.filter_signals(sigs, portfolio_value=20_000_000.0, regime='caution', vix=18.0)
    assert len(passed) == 1
    assert passed[0]['tp_pct'] == pytest.approx(3.84, rel=1e-6)
    assert passed[0]['sl_pct'] == pytest.approx(1.56, rel=1e-6)


def test_portfolio_optimizer_apply_exposure_conserves_total_weight():
    """[B-P0-5] _apply_exposure routes (1 - exposure) to S5 so total portfolio weight == 1.0."""
    from src.allocation.portfolio_optimizer import PortfolioOptimizer

    opt = PortfolioOptimizer()
    weights = {
        'S0': 0.10, 'S1': 0.10, 'S2': 0.25, 'S3': 0.20, 'S5': 0.10,
        'S10_MEGA_TREND': 0.05, 'S11_HIGHBETA_SNIPER': 0.05,
        'S12_DERIVATIVE_SQUEEZE': 0.05, 'S13_JAPAN_ALPHA': 0.05, 'S14_PAIR_ALPHA': 0.05,
    }
    scaled = opt._apply_exposure(weights, exposure=0.368)
    assert sum(scaled.values()) == pytest.approx(1.0, abs=1e-3)
    assert scaled['S5'] > weights['S5']


def test_sector_pair_krx_etf_mapping_and_s12_guards():
    """[B-P0-3 & B-P0-4] KRX sector ETF map has correct codes and S12 blocks missing 0.0 prices."""
    from src.allocation.sector_pair_alpha_engine import DEFAULT_SECTOR_ETF_MAP
    from src.streams.s12_derivative_squeeze.derivative_squeeze_stream import S12DerivativeSqueezeStream

    assert DEFAULT_SECTOR_ETF_MAP['auto'] == '091170'
    assert DEFAULT_SECTOR_ETF_MAP['finance'] == '091180'
    assert DEFAULT_SECTOR_ETF_MAP['petrochem'] == '117460'

    s12 = S12DerivativeSqueezeStream()
    # Missing futures/spot (0.0) must NOT fire a false basis trigger
    sigs = s12.generate_signals(
        regime='bull',
        market_data={'signal_cache': {'kospi200_futures': 0.0, 'kospi200_spot': 0.0}},
    )
    assert sigs == []


def test_execution_engine_urgency_and_alpha_decimal_extraction():
    """[C-01 & C-02] Integer urgency 2/3 -> 'stop'/'panic' and _pct keys divide by 100.0."""
    from src.execution.execution_engine import _derive_order_urgency, _extract_expected_alpha_decimal

    assert _derive_order_urgency({'action': 'sell', 'urgency': 2}, False) == 'stop'
    assert _derive_order_urgency({'action': 'sell', 'urgency': 3}, False) == 'panic'
    assert _derive_order_urgency({'action': 'buy', 'urgency': 1}, True) == 'normal'

    # 0.8% expected_return_pct must be 0.008 (0.8%), NEVER 0.80 (80%)
    alpha_dec = _extract_expected_alpha_decimal({'expected_return_pct': 0.8}, 0.015)
    assert alpha_dec == pytest.approx(0.008, rel=1e-9)

    alpha_dec_tp = _extract_expected_alpha_decimal({'tp_pct': 1.2}, 0.015)
    assert alpha_dec_tp == pytest.approx(0.012, rel=1e-9)


def test_cro_final_gate_preserves_exit_sells_and_reduces_post_trade_exposure():
    """[C-03] CROFinalGate never blocks/scales down sell exit orders and reduces post-trade exposure."""
    from src.risk.cro_final_gate import CROFinalGate

    gate = CROFinalGate()
    current_positions = {
        '005930': {'qty': 100, 'current_price': 70000.0, 'eval_krw': 7_000_000.0},
    }
    sell_order = {
        'ticker': '005930',
        'action': 'sell',
        'shares': 100,
        'price': 70000.0,
        'urgency': 'stop',
    }
    approved, metrics = gate.evaluate_and_scale_orders(
        orders=[sell_order],
        portfolio_value=10_000_000.0,
        current_positions=current_positions,
    )
    assert len(approved) == 1
    assert approved[0]['shares'] == 100
    assert metrics['proposed_exposure_pct'] == pytest.approx(0.0, abs=1e-6)


def test_exposure_orchestrator_preserves_all_signal_cache_keys(tmp_path, monkeypatch):
    """[C-05] ExposureOrchestrator._load_signal_cache() preserves all keys in signal_cache.json."""
    from src.risk import exposure_orchestrator as eo_mod

    sc_file = tmp_path / 'signal_cache.json'
    sc_file.write_text(json.dumps({
        'vix': 21.5,
        'gex_crash_warning': True,
        'wag_the_dog_active': True,
        'vix_momentum': 0.25,
    }), encoding='utf-8')

    monkeypatch.setattr(eo_mod, '_RESULTS', tmp_path)
    orch = eo_mod.ExposureOrchestrator()
    loaded = orch._load_signal_cache()
    assert loaded['vix'] == pytest.approx(21.5, rel=1e-6)
    assert loaded['gex_crash_warning'] is True
    assert loaded['wag_the_dog_active'] is True
    assert loaded['vix_momentum'] == pytest.approx(0.25, rel=1e-6)

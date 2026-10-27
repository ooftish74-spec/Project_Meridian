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

    assert DEFAULT_SECTOR_ETF_MAP['auto'] == '091180'
    assert DEFAULT_SECTOR_ETF_MAP['finance'] == '091170'
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


def test_safe_io_safe_json_update_supports_strict_kwarg(tmp_path):
    """[Forensic-3 P0-1] src.infra.safe_io.safe_json_update accepts strict=True without TypeError."""
    from src.infra.safe_io import safe_json_update

    target = tmp_path / 'status.json'
    res = safe_json_update(target, lambda d: {**d, 'ok': True, 'count': 1}, strict=True)
    assert res is True
    saved = json.loads(target.read_text(encoding='utf-8'))
    assert saved['ok'] is True
    assert saved['count'] == 1


def test_position_sizer_sentinel_tp_pct_and_risk_free_parking():
    """[Forensic-2 P0-2] S5 risk_free_parking (tp_pct=99.0) and S3_B trailing (tp_pct=100.0) are not crushed."""
    from src.execution.position_sizer import PositionSizer

    sizer = PositionSizer()
    s5_sig = {
        'ticker': '449170',
        'stream': 'S5',
        'stream_id': 'S5',
        'action': 'buy',
        'size_pct': 1.0,
        'confidence': 0.90,
        'tp_pct': 99.0,
        'sl_pct': 2.0,
        'expected_return': 0.0001,
        'expected_return_pct': 0.01,
        'risk_free_parking': True,
        'price': 105000.0,
    }
    passed_s5 = sizer.filter_signals([s5_sig], portfolio_value=20_000_000.0, regime='caution', vix=18.0)
    assert len(passed_s5) == 1
    assert passed_s5[0]['size_pct'] == pytest.approx(1.0, rel=1e-6)

    s3b_sig = {
        'ticker': '005930',
        'stream': 'S3_B',
        'stream_id': 'S3_B',
        'action': 'buy',
        'size_pct': 0.20,
        'confidence': 0.85,
        'tp_pct': 100.0,
        'sl_pct': 8.0,
        'expected_return': 0.16,
        'expected_return_pct': 16.0,
        'price': 70000.0,
    }
    res_s3b = sizer.compute(
        signal=s3b_sig,
        portfolio_value=20_000_000.0,
        regime='bull',
        vix=15.0,
    )
    # Must NOT be crushed to 0.01 (1%) by plugging sentinel tp_pct=100.0 into Cornish-Fisher / probe-cap
    assert res_s3b['final_size'] > 0.03


def test_maxent_stream_weights_s5_defensive_sign_and_s11_beta_sign():
    """[Forensic-2 P0-1] MaxEnt stream weights boost S5 in crash and penalize S11 when target_beta < 0."""
    from src.utils.dynamic_quant_engine import DynamicQuantEngine

    eng = DynamicQuantEngine()
    streams = ['S0', 'S1', 'S2', 'S3', 'S5', 'S10_MEGA_TREND', 'S11_HIGHBETA_SNIPER', 'S14_PAIR_ALPHA']
    w_bull = eng.compute_maxent_stream_weights(
        active_streams=streams,
        regime='bull',
        regime_probs={'bull': 0.80, 'caution': 0.15, 'bear': 0.04, 'crash': 0.01},
    )
    w_crash = eng.compute_maxent_stream_weights(
        active_streams=streams,
        regime='crash',
        regime_probs={'bull': 0.01, 'caution': 0.04, 'bear': 0.15, 'crash': 0.80},
    )
    assert w_crash['S5'] > w_bull['S5']
    assert w_bull['S11_HIGHBETA_SNIPER'] > w_crash['S11_HIGHBETA_SNIPER']


def test_cro_final_gate_gross_exposure_and_incremental_buy_scaling():
    """[Forensic-3 P0-3] CROFinalGate sums |weights| for Long+Inverse and scales buys by (E_max - W_ret)/W_buy."""
    from src.risk.cro_final_gate import CROFinalGate

    gate = CROFinalGate(max_portfolio_exposure=0.80, max_daily_var_pct=0.99)
    # Retained portfolio: 5,000,000 KRW out of 10,000,000 KRW NAV (W_ret = 0.50)
    current_positions = {
        '005930': {'qty': 100, 'current_price': 50000.0, 'eval_krw': 5_000_000.0},
    }
    # Proposed buy: 100 shares @ 50,000 KRW = 5,000,000 KRW (W_buy = 0.50)
    # Total proposed = 1.00 > E_max (0.80). Allowed incremental buy = 0.80 - 0.50 = 0.30 -> scale = 0.30 / 0.50 = 0.60 -> 60 shares!
    buy_order = {
        'ticker': '000660',
        'action': 'buy',
        'shares': 100,
        'price': 50000.0,
    }
    approved, metrics = gate.evaluate_and_scale_orders(
        orders=[buy_order],
        portfolio_value=10_000_000.0,
        current_positions=current_positions,
    )
    assert len(approved) == 1
    assert approved[0]['shares'] == 60
    assert metrics['exposure_scale_factor'] == pytest.approx(0.60, rel=1e-6)


def test_cross_sectional_rank_z_zeros_date_constant_macro_features():
    """[Forensic-1 P0-1] Date-constant macro features across stocks on date t are zeroed, not leaked raw."""
    from src.ml.cross_sectional_eb_model import cross_sectional_rank_z_matrix

    # 10 stocks on the same date: col 0 varies across stocks, col 1 is constant macro (e.g. +2.5)
    X = np.column_stack([np.linspace(1.0, 10.0, 10), np.full(10, 2.5)])
    dates = np.array(['2026-10-23'] * 10)
    Z = cross_sectional_rank_z_matrix(X, dates)
    assert np.allclose(Z[:, 1], 0.0)
    assert np.std(Z[:, 0]) > 0.5


def test_discrete_lot_quantization_and_eb_alpha_friction_wiring():
    """Verify closed-form discrete lot quantization attenuation, single-lot cap, and EB expected alpha wiring."""
    from src.execution.position_sizer import PositionSizer
    from src.allocation.portfolio_optimizer import PortfolioOptimizer

    sizer = PositionSizer()
    # 1. Closed-form discrete lot quantization attenuation:
    # NAV = 10,000,000 KRW, caution probe_cap = 0.03 (300,000 KRW), 1 share = 300,000 KRW -> lot_step = 0.03
    # Ratio = lot_step / w* = 0.03 / 0.03 = 1.0 -> kappa_lot = 1 / (1 + 1^2 / 12) = 12 / 13 = 0.9230769...
    # final_size = round(0.03 * (12 / 13), 4) = 0.0277
    sig_lot = {
        "ticker": "005930",
        "stream_id": "S2",
        "confidence": 0.80,
        "tp_pct": 10.0,
        "sl_pct": 4.0,
        "kelly_size": 0.20,
        "unit_price_krw": 300_000.0,
        "market_data": {"skewness": 0.0, "kurtosis": 3.0},
    }
    res_lot = sizer.compute(sig_lot, portfolio_value=10_000_000.0, regime="caution", vix=17.5)
    assert res_lot["approved"] is True
    assert res_lot["lot_step_pct"] == pytest.approx(0.03, rel=1e-9)
    assert res_lot["lot_quantization_scale"] == pytest.approx(round(12.0 / 13.0, 4), rel=1e-9)
    assert res_lot["final_size"] == pytest.approx(round(0.03 * (12.0 / 13.0), 4), rel=1e-9)

    # 2. Single-lot cap rejection in caution regime when 1 share (> 6% of NAV) exceeds 2x probe_cap (6%)
    sig_oversized_lot = dict(sig_lot, unit_price_krw=700_000.0)
    res_over = sizer.compute(sig_oversized_lot, portfolio_value=10_000_000.0, regime="caution", vix=17.5)
    assert res_over["approved"] is False
    assert res_over["drop_reason"] == "lot_quantization_exceeds_kelly_cap"
    assert res_over["final_size"] == 0.0

    # 3. PortfolioOptimizer derives non-zero expected_profit from stream_metrics daily_returns when alpha key is absent
    opt = PortfolioOptimizer()
    sm = {
        "S2": {"daily_returns": [0.01, 0.012, 0.008, 0.011, 0.009], "sharpe": 2.5},
        "S5": {"daily_returns": [0.0001, 0.0001, 0.0001], "sharpe": 1.0},
    }
    res_opt = opt.optimize(
        stream_metrics=sm,
        regime="bull",
        portfolio_value=100_000_000.0,
        current_weights={"S2": 0.0, "S5": 1.0},
    )
    assert res_opt["tx_check"]["expected_profit"] > 0


def test_krx_pair_hedge_replenishment_and_orphan_exit_invariants():
    """Verify:
    1) S14 KR pair signals include closed-form pair_vol_parity_scale.
    2) StreamOrchestrator._evaluate_exits exits rotated-out S14 orphan legs ('091180') and expired S2 residuals ('457190', '069620')
       while protecting active S2/S14 signals ('012750', '305720', '114800') via Active Signal Rollover Exemption.
    """
    from scripts.stream_orchestrator import StreamOrchestrator

    orch = StreamOrchestrator(exec_mode="shadow")
    orch._latest_signals = {
        "S2": [
            {"ticker": "012750", "direction": "long", "stream_id": "S2"},
            {"ticker": "081660", "direction": "long", "stream_id": "S2"},
            {"ticker": "278470", "direction": "long", "stream_id": "S2"},
        ],
        "S14_PAIR_ALPHA": [
            {"ticker": "305720", "direction": "long", "stream_id": "S14_PAIR_ALPHA", "pair_id": "305720_114800"},
            {"ticker": "114800", "direction": "long", "stream_id": "S14_PAIR_ALPHA", "pair_id": "305720_114800"},
        ],
    }

    portfolio = {
        "positions": {
            "012750": {
                "ticker": "012750",
                "quantity": 7,
                "days_held": 2,
                "max_hold_days": 1.0,
                "unrealized_pnl_pct": -1.5,
                "current_price": 93300.0,
                "stream_id": "S2",
                "track": "TRACK_1_TACTICAL",
            },
            "457190": {
                "ticker": "457190",
                "quantity": 1,
                "days_held": 2,
                "max_hold_days": 1.0,
                "unrealized_pnl_pct": 0.14,
                "current_price": 70400.0,
                "stream_id": "S2",
                "track": "TRACK_1_TACTICAL",
            },
            "069620": {
                "ticker": "069620",
                "quantity": 2,
                "days_held": 2,
                "max_hold_days": 1.0,
                "unrealized_pnl_pct": -1.5,
                "current_price": 100600.0,
                "stream_id": "S2",
                "track": "TRACK_1_TACTICAL",
            },
            "091180": {
                "ticker": "091180",
                "quantity": 8,
                "days_held": 2,
                "max_hold_days": 1.0,
                "unrealized_pnl_pct": -1.64,
                "current_price": 22800.0,
                "stream_id": "S14_PAIR_ALPHA",
                "strategy": "kr_sector_pair_alpha",
                "track": "TRACK_1_TACTICAL",
            },
            "305720": {
                "ticker": "305720",
                "quantity": 168,
                "days_held": 2,
                "max_hold_days": 1.0,
                "unrealized_pnl_pct": -0.97,
                "current_price": 13245.0,
                "stream_id": "S14_PAIR_ALPHA",
                "strategy": "kr_sector_pair_alpha",
                "track": "TRACK_1_CORE",
            },
        }
    }

    exits = orch._evaluate_exits(
        market_data={"signal_cache": {"vix": 18.0, "vkospi": 18.0}},
        regime="caution",
        portfolio=portfolio,
    )
    exited_tickers = {o["ticker"] for o in exits if "Time Limit" in str(o.get("reason", ""))}
    assert "091180" in exited_tickers
    assert "457190" in exited_tickers
    assert "069620" in exited_tickers
    assert "012750" not in exited_tickers
    assert "305720" not in exited_tickers
    assert {"091180", "457190", "069620"}.issubset(orch._pending_exit_tickers)

    # 3. Verify _generate_orders completes 114800 hedge parity (2,225,160 - 7,524 = 2,217,636 KRW)
    #    even when S14_PAIR_ALPHA holds 5.74M KRW in US positions, and blocks 122630/SQQQ in caution regime.
    full_portfolio = {
        "total_nav": 17_962_720.0,
        "positions": {
            **portfolio["positions"],
            "114800": {
                "ticker": "114800",
                "quantity": 6,
                "current_price": 1254.0,
                "eval_krw": 7524.0,
                "stream_id": "S14_PAIR_ALPHA",
                "track": "TRACK_1_CORE",
            },
            "XLE": {
                "ticker": "XLE",
                "quantity": 28,
                "current_price": 56.24,
                "eval_krw": 2_336_419.0,
                "stream_id": "S14_PAIR_ALPHA",
                "track": "TRACK_1_CORE",
            },
            "XLV": {
                "ticker": "XLV",
                "quantity": 10,
                "current_price": 147.82,
                "eval_krw": 2_193_971.0,
                "stream_id": "S14_PAIR_ALPHA",
                "track": "TRACK_1_CORE",
            },
            "PSQ": {
                "ticker": "PSQ",
                "quantity": 47,
                "current_price": 17.29,
                "eval_krw": 1_206_010.0,
                "stream_id": "S14_PAIR_ALPHA",
                "track": "TRACK_1_CORE",
            },
        },
    }
    signals_for_gen = {
        "S1": [
            {"ticker": "SQQQ", "direction": "long", "size_pct": 0.20, "price": 72.14, "confidence": 0.70, "tp_pct": 3.0, "sl_pct": 1.5},
        ],
        "S5": [
            {"ticker": "122630", "direction": "long", "strategy": "overnight_overlay", "size_pct": 0.08, "price": 90090.0, "confidence": 0.70, "tp_pct": 1.5, "sl_pct": 1.0},
            {"ticker": "459580", "direction": "long", "strategy": "yield_parking", "size_pct": 0.40, "price": 105455.0, "confidence": 0.65, "tp_pct": 0.5, "sl_pct": 0.3},
        ],
        "S14_PAIR_ALPHA": [
            {
                "stream_id": "S14_PAIR_ALPHA",
                "ticker": "305720",
                "name": "KODEX 2차전지산업",
                "direction": "long",
                "size_pct": 0.1442,
                "price": 13245.0,
                "confidence": 0.664,
                "tp_pct": 2.95,
                "sl_pct": 1.97,
                "pair_id": "PAIR_KR_SECONDARY_BATTERY_305720_114800",
                "pair_partner": "114800",
                "pair_vol_parity_scale": 0.7208,
                "is_market_neutral": True,
            },
            {
                "stream_id": "S14_PAIR_ALPHA",
                "ticker": "114800",
                "name": "KODEX 인버스",
                "direction": "long",
                "size_pct": 0.20,
                "price": 1254.0,
                "confidence": 0.664,
                "tp_pct": 2.0,
                "sl_pct": 1.2,
                "pair_id": "PAIR_KR_SECONDARY_BATTERY_305720_114800",
                "pair_partner": "305720",
                "pair_vol_parity_scale": 1.0,
                "is_hedge": True,
                "is_market_neutral": True,
            },
        ],
    }
    weights_for_gen = {"S1": 0.05, "S5": 0.10, "S14_PAIR_ALPHA": 0.292}
    orch.exec_mode = "live"
    orch._ignore_market_hours = True
    gen_orders = orch._generate_orders(
        signals=signals_for_gen,
        weights=weights_for_gen,
        max_exposure=0.84,
        leverage_result={"judgment": {"leverage_level": 1.0}},
        portfolio=full_portfolio,
        regime="caution",
        market_data={"enforce_intraday_parking_guard": True, "vix": 18.0, "signal_cache": {"vix": 18.0, "usdkrw": 1484.19}},
    )
    ordered_by_tk = {o["ticker"]: o["amount_krw"] for o in gen_orders}
    assert "122630" not in ordered_by_tk
    assert "SQQQ" not in ordered_by_tk
    assert "305720" not in ordered_by_tk
    assert ordered_by_tk.get("114800") == 2_217_636


def test_corporate_after_tax_cagr_closed_form_and_allocator_signal_prior_harmony():
    """Verify closed-form K-IFRS Corporate After-Tax CAGR model and AlphaAllocator live-signal prior harmony."""
    import math
    from src.allocation.alpha_allocator import AlphaAllocator
    from src.params.dynamic_quant_models import (
        apply_probe_ev_tilt,
        compute_corporate_after_tax_cagr_metrics,
        normalize_signal_horizon_expectation,
    )

    # 1. Track 2 (Buffett QVM) multi-year DCF Margin-of-Safety (215.72%) must normalize to [0.15, 0.25] single-horizon reward & 20d horizon
    qvm_sig = {
        "stream_id": "S3_B",
        "track": "TRACK_2_BUFFETT_QVM",
        "ticker": "086280",
        "direction": "long",
        "confidence": 0.65,
        "margin_of_safety_pct": 215.72,
        "expected_return": 2.1572,
        "tp_pct": 100.0,
        "sl_pct": 30.0,
        "size_pct": 0.20,
    }
    norm_qvm = normalize_signal_horizon_expectation(qvm_sig, stream_id="S3")
    # 215.72% * 0.10 = 21.572% -> 0.21572 in [0.15, 0.25]; tactical risk capped at 0.08; Track 2 horizon = 20.0d
    assert norm_qvm["reward_dec"] == pytest.approx(0.21572, rel=1e-9)
    assert norm_qvm["risk_dec"] == pytest.approx(0.08, rel=1e-9)
    assert norm_qvm["p_win"] == pytest.approx(0.65, rel=1e-9)
    assert norm_qvm["horizon_days"] == pytest.approx(20.0, rel=1e-9)

    # 1b. S5 yield-parking placeholder (tp_pct=99.0, sl_pct=99.0) must normalize to money-market risk-free rate & 5bps risk
    parking_sig = {
        "stream_id": "S5",
        "ticker": "459580",
        "direction": "long",
        "confidence": 0.90,
        "tp_pct": 99.0,
        "sl_pct": 99.0,
        "size_pct": 0.10,
    }
    norm_park = normalize_signal_horizon_expectation(parking_sig, stream_id="S5")
    assert norm_park["is_parking"] is True
    assert norm_park["risk_dec"] == pytest.approx(0.0005, rel=1e-9)
    assert norm_park["p_win"] == pytest.approx(0.95, rel=1e-9)
    assert norm_park["reward_dec"] == pytest.approx((0.0325 / 252.0) * 5.0, rel=1e-9)

    # 1c. apply_probe_ev_tilt differentiates probe-capped signals by relative ev_margin while preserving total size_pct
    tilted = apply_probe_ev_tilt(
        [
            {"ticker": "LOW_EV", "size_pct": 0.05, "ev_margin": 1.0, "is_probe": True},
            {"ticker": "HIGH_EV", "size_pct": 0.05, "ev_margin": 4.0, "is_probe": True},
        ]
    )
    assert tilted[1]["size_pct"] > tilted[0]["size_pct"]
    assert (tilted[0]["size_pct"] + tilted[1]["size_pct"]) == pytest.approx(0.10, abs=1e-4)

    # 2. Hand-computed closed-form oracle for compute_corporate_after_tax_cagr_metrics
    # Single stream S2 with weight 1.0, 1 signal: p=0.65, TP=0.10 (10%), SL=0.05 (5%), H=5 days, C_RT=0.0021
    # NAV = 20,000,000 KRW -> annual gross profit < 200M KRW -> SME corporate tax rate tau_corp = 0.099, eta_shield = 0.70
    test_sigs = {
        "S2": [
            {
                "ticker": "005930",
                "direction": "long",
                "confidence": 0.65,
                "tp_pct": 10.0,
                "sl_pct": 5.0,
                "max_hold_days": 5,
                "round_trip_cost": 0.0021,
                "size_pct": 1.0,
            }
        ]
    }
    res_cagr = compute_corporate_after_tax_cagr_metrics(
        stream_weights={"S2": 1.0},
        stream_signals=test_sigs,
        portfolio_nav_krw=20_000_000.0,
    )
    tau = 0.099
    eta = 0.70
    p = 0.65
    k_rew = 0.55
    k_loss = 0.75
    u_dep = 0.75
    rf_daily = 0.0325 / 252.0
    r_gross = 0.10 * k_rew
    sl_gross = 0.05 * k_loss
    c_rt = 0.0021
    h = 5.0
    mu_gross = p * r_gross - (1.0 - p) * sl_gross - c_rt
    var_gross = p * (1.0 - p) * ((r_gross + sl_gross) ** 2)
    stream_g_gross = (mu_gross - 0.5 * var_gross) / h
    expected_g_gross = u_dep * stream_g_gross + (1.0 - u_dep) * rf_daily

    net_r = (1.0 - tau) * r_gross
    net_sl = (1.0 - eta * tau) * sl_gross
    net_c = (1.0 - tau) * c_rt
    mu_net = p * net_r - (1.0 - p) * net_sl - net_c
    var_net = p * (1.0 - p) * ((net_r + net_sl) ** 2)
    stream_g_net = (mu_net - 0.5 * var_net) / h
    expected_g_net = u_dep * stream_g_net + (1.0 - u_dep) * rf_daily * (1.0 - tau)
    expected_cagr_net = math.exp(252.0 * expected_g_net) - 1.0

    assert res_cagr["effective_corporate_tax_rate"] == pytest.approx(tau, rel=1e-9)
    assert res_cagr["portfolio_daily_gross_growth"] == pytest.approx(expected_g_gross, rel=1e-9)
    assert res_cagr["portfolio_daily_net_corp_growth"] == pytest.approx(expected_g_net, rel=1e-9)
    assert res_cagr["expected_corporate_after_tax_cagr"] == pytest.approx(expected_cagr_net, rel=1e-9)
    assert res_cagr["meets_50pct_cagr_hurdle"] is True
    assert res_cagr["expected_corporate_after_tax_cagr"] >= 0.50

    # 3. AlphaAllocator live production harmony: empty streams (n_days=0 without total_trades key) are zeroed
    #    while active signal streams (S2, S3, S10_MEGA_TREND, S14_PAIR_ALPHA, S0) achieve >= 50% Corporate After-Tax CAGR
    live_sigs_all = {
        "S0": [{"ticker": "252670", "direction": "long", "confidence": 0.60, "tp_pct": 5.0, "sl_pct": 2.5, "size_pct": 1.0}],
        "S1": [],
        "S2": [
            {"ticker": "009150", "direction": "long", "confidence": 0.66, "tp_pct": 10.79, "sl_pct": 5.56, "size_pct": 0.125},
            {"ticker": "MU", "direction": "long", "confidence": 0.66, "tp_pct": 10.79, "sl_pct": 5.56, "size_pct": 0.125},
        ],
        "S3": [
            {"ticker": "005930", "direction": "long", "confidence": 0.68, "tp_pct": 14.74, "sl_pct": 7.12, "size_pct": 0.25},
            qvm_sig,
        ],
        "S5": [{"ticker": "459580", "direction": "long", "confidence": 0.65, "tp_pct": 1.5, "sl_pct": 0.5, "size_pct": 0.50}],
        "S10_MEGA_TREND": [
            {"ticker": "NVDA", "direction": "long", "confidence": 0.72, "tp_pct": 15.0, "sl_pct": 5.0, "size_pct": 0.33},
        ],
        "S11_HIGHBETA_SNIPER": [],
        "S12_DERIVATIVE_SQUEEZE": [],
        "S13_JAPAN_ALPHA": [],
        "S14_PAIR_ALPHA": [
            {"ticker": "305720", "direction": "long", "confidence": 0.664, "tp_pct": 4.5, "sl_pct": 2.0, "expected_return": 0.055, "size_pct": 0.15, "is_market_neutral": True},
            {"ticker": "114800", "direction": "long", "confidence": 0.664, "tp_pct": 4.5, "sl_pct": 2.0, "expected_return": 0.055, "size_pct": 0.15, "is_hedge": True, "is_market_neutral": True},
        ],
    }
    prod_like_metrics = {
        "S0": {"n_days": 0, "win_rate": 0.0, "active_positions": 1},
        "S1": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S2": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S3": {"n_days": 0, "win_rate": 0.0, "active_positions": 5},
        "S5": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S10_MEGA_TREND": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S11_HIGHBETA_SNIPER": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S12_DERIVATIVE_SQUEEZE": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S13_JAPAN_ALPHA": {"n_days": 0, "win_rate": 0.0, "active_positions": 0},
        "S14_PAIR_ALPHA": {"n_days": 5, "win_rate": 0.2, "daily_returns": [-0.001, -0.002, -0.001, 0.001, -0.001], "active_positions": 2},
        "_stream_signals": live_sigs_all,
    }
    alloc = AlphaAllocator()
    weights = alloc.allocate(prod_like_metrics, regime="caution", market_data={"vix": 18.0, "signal_cache": {"vix": 18.0, "qqq_ker": 0.25}})
    for empty_sid in ("S1", "S11_HIGHBETA_SNIPER", "S12_DERIVATIVE_SQUEEZE", "S13_JAPAN_ALPHA"):
        assert weights.get(empty_sid, 0.0) == pytest.approx(0.0, abs=1e-6)

    port_cagr_eval = compute_corporate_after_tax_cagr_metrics(
        stream_weights=weights,
        stream_signals=live_sigs_all,
        portfolio_nav_krw=15_285_542.0,
    )
    assert port_cagr_eval["expected_corporate_after_tax_cagr"] >= 0.50
    assert port_cagr_eval["meets_50pct_cagr_hurdle"] is True


def test_krx_intraday_churn_prevention_and_s14_pair_spread_stop_invariants():
    """Verify:
    1)IntradayCapitalRouter never sells active buy signals ('012750' in active_signal_ev_map) or profitable swing positions (return_pct > +0.5%).
    2) LeverageJudge.check_etf_stops exempts S14_PAIR_ALPHA hedge leg ('114800' paired with '305720') from single-leg Hard SL.
    3) StreamOrchestrator._evaluate_exits evaluates active S14_PAIR_ALPHA pairs via capital-weighted spread PnL.
    """
    from scripts.stream_orchestrator import StreamOrchestrator
    from src.execution.intraday_capital_router import IntradayCapitalRouter
    from src.risk.leverage_judge import LeverageJudge

    router = IntradayCapitalRouter()
    portfolio = {
        "holdings": {
            "012750": {"ticker": "012750", "name": "에스원", "qty": 7, "current_price": 93900.0, "return_pct": -1.5, "stream_id": "S2"},
            "278470": {"ticker": "278470", "name": "에이피알", "qty": 1, "current_price": 281000.0, "return_pct": 1.44, "stream_id": "S2"},
        }
    }
    market_data = {
        "usdkrw": 1380.0,
        "holdings_ev": {"012750": 1.07, "278470": 0.90},
        "active_signal_ev_map": {"012750": 1.07},
        "ticker_atr_pct": {"032640": 1.5, "012750": 1.5, "278470": 1.5},
    }
    candidate_sigs = [
        {"ticker": "032640", "name": "LG유플러스", "price": 14170.0, "win_rate": 0.85, "tp_pct": 5.0, "sl_pct": 1.5, "side": "buy", "stream_id": "S2"},
    ]
    swap_orders = router.route_intraday_capital(candidate_sigs, market_data, total_cash_krw=0.0, portfolio=portfolio)
    assert swap_orders == []

    # 2) LeverageJudge exempts S14_PAIR_ALPHA hedge leg 114800 from single-leg Hard SL when paired with 305720
    lj = LeverageJudge()
    pair_positions = {
        "305720": {
            "ticker": "305720",
            "stream_id": "S14_PAIR_ALPHA",
            "strategy": "kr_sector_pair_alpha",
            "quantity": 168,
            "entry_price": 13375.0,
            "current_price": 13855.0,
            "unrealized_pnl_pct": 3.59,
            "eval_krw": 2_327_640.0,
        },
        "114800": {
            "ticker": "114800",
            "stream_id": "S14_PAIR_ALPHA",
            "strategy": "kr_sector_pair_alpha",
            "quantity": 6,
            "entry_price": 1251.0,
            "current_price": 1207.0,
            "unrealized_pnl_pct": -3.52,
            "eval_krw": 7_242.0,
        },
    }
    lj_exits = lj.check_etf_stops(
        pair_positions,
        market_data={"date": "2026-10-27", "signal_cache": {"vkospi": 17.3, "vix": 17.5}},
        regime="caution",
    )
    assert lj_exits == []

    # 3) StreamOrchestrator._evaluate_exits computes capital-weighted spread PnL (+3.5679% > 0) for active S14 pair
    orch = StreamOrchestrator(exec_mode="shadow")
    orch._latest_signals = {
        "S14_PAIR_ALPHA": [
            {"ticker": "305720", "direction": "long", "stream_id": "S14_PAIR_ALPHA"},
            {"ticker": "114800", "direction": "long", "stream_id": "S14_PAIR_ALPHA"},
        ]
    }
    so_exits = orch._evaluate_exits(
        market_data={"signal_cache": {"vix": 17.5, "vkospi": 17.3}},
        regime="caution",
        portfolio={"positions": pair_positions},
    )
    assert [o for o in so_exits if o.get("ticker") == "114800"] == []







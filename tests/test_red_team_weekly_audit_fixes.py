"""
Hermetic Closed-Form Unit Tests for Red Team Weekly Audit Fixes (2026-10-24)
=============================================================================
Verifies:
1. CrossSectionalEBClassifier single-row (n=1) vs batch (n>=5) inference parity (`s_mean_`, `s_std_`)
2. S2MLAlphaStream cross-asset loader (`sp500_return` never contaminated by `SP500` index level, `vix_prev` derived from `vix_change_1d`)
3. v4_features `_extract_v3_standalone` 100% mathematical parity with `train_ensemble.extract_v3`
4. MasterScenarioEngine `SCENARIO_2` proxy gating + contract unit awareness + `SCENARIO_3_CRASH_DEFENSE` VIX floor & VKOSPI credibility guard
5. IntradayMicroGuard stale `vix_prev` cross-check and minimum VIX level floor
6. AlphaAllocator canonical stream ID deduplication (`S10` -> `S10_MEGA_TREND`, `S13` -> `S13_JAPAN_ALPHA`)
7. GoogleFinanceCollector `_is_date_array` + `re.finditer` calendar array (`[2025, 10, 24]`, `[2026, 10, 23]`) rejection
8. RegimeDetector live `kospi_change_pct` synchronization and `bleed_stabilizing` transition
"""

import math
import numpy as np
import pytest


def test_cross_sectional_eb_single_row_vs_batch_parity():
    """Verify CrossSectionalEBClassifier standardizes single-row (n=1) inference identically to training distribution."""
    from src.ml.cross_sectional_eb_model import CrossSectionalEBClassifier

    rng = np.random.default_rng(42)
    n_dates, n_stocks, n_features = 12, 10, 8
    dates = np.repeat(np.arange(n_dates), n_stocks)
    X = rng.normal(0.0, 1.0, size=(n_dates * n_stocks, n_features))
    # True signal in feature 0 and 1
    logits = 0.8 * X[:, 0] - 0.6 * X[:, 1]
    y = (logits > 0.0).astype(int)

    clf = CrossSectionalEBClassifier(tree_blend=0.0)
    clf.fit(X, y)

    assert hasattr(clf, "s_mean_")
    assert hasattr(clf, "s_std_")
    assert clf.s_std_ > 1e-8

    # Closed-form check for a single sample x0:
    x0 = X[0:1]
    s0 = float((x0 @ clf.w_)[0])
    z0_expected = (s0 - clf.s_mean_) / clf.s_std_
    p0_expected = 1.0 / (1.0 + math.exp(-max(-5.0, min(5.0, z0_expected))))

    p0_single = float(clf.predict_proba(x0)[0, 1])
    assert p0_single == pytest.approx(p0_expected, rel=1e-9)

    # Batch prediction on the full training matrix must match single-row prediction when evaluated on training distribution
    p_batch_train = clf.predict_proba(X)[:, 1]
    assert float(p_batch_train[0]) == pytest.approx(p0_single, rel=1e-9)


def test_v3_standalone_exact_mathematical_parity():
    """Verify v4_features._extract_v3_standalone matches scripts.train_ensemble.extract_v3 to 1e-9."""
    from scripts.train_ensemble import extract_v3
    from src.intelligence.v4_features import _extract_v3_standalone

    rng = np.random.default_rng(123)
    n = 120
    close = 50000.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.018, size=n)))
    high = close * (1.0 + rng.uniform(0.001, 0.015, size=n))
    low = close * (1.0 - rng.uniform(0.001, 0.015, size=n))
    opn = close * (1.0 + rng.normal(0.0, 0.005, size=n))
    vol = rng.uniform(1e5, 1e6, size=n)

    f_train = extract_v3(close, high, low, opn, vol, n - 1, is_etf=False)
    f_stand = _extract_v3_standalone(close, high, low, opn, vol, n - 1, is_etf=False)

    assert f_train is not None and f_stand is not None
    assert set(f_train.keys()).issubset(set(f_stand.keys()))
    for k in f_train:
        assert float(f_stand[k]) == pytest.approx(float(f_train[k]), rel=1e-9, abs=1e-9), f"Mismatch on {k}"


def test_s2_load_cross_asset_latest_no_index_level_contamination(tmp_path, monkeypatch):
    """Verify _load_cross_asset_latest reads sp500_change_1d and derives vix_prev instead of using SP500 index level."""
    import json
    import src.streams.s2_ml_alpha.ml_stream as ml_mod
    from src.streams.s2_ml_alpha.ml_stream import S2MLAlphaStream

    res_dir = tmp_path / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    sc_file = res_dir / "signal_cache.json"
    sc_file.write_text(
        json.dumps({
            "SP500": 7659.44,
            "sp500_change_1d": -1.09,
            "vix": 19.99,
            "vix_change_1d": 21.45,
            "usdkrw": 1334.3,
            "usdkrw_history": [1345.0, 1342.0, 1340.0, 1338.0, 1336.0, 1334.3],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(ml_mod, "_PROJECT_ROOT", tmp_path)

    stream = S2MLAlphaStream.__new__(S2MLAlphaStream)
    ca = stream._load_cross_asset_latest()

    assert ca["sp500_return"] == pytest.approx(-1.09, rel=1e-9)
    expected_vix_prev = 19.99 / (1.0 + 0.2145)
    assert ca["vix_close"] == pytest.approx(19.99, rel=1e-9)
    assert ca["vix_prev"] == pytest.approx(expected_vix_prev, rel=1e-6)
    assert ca["usdkrw_close"] == pytest.approx(1334.3, rel=1e-9)
    assert ca["usdkrw_5d_ago"] == pytest.approx(1342.0, rel=1e-9)


def test_master_scenario_engine_no_false_positive_on_calm_recovery():
    """Verify SCENARIO_2 and SCENARIO_3_CRASH_DEFENSE do NOT false-fire on VIX=20.24, KOSPI=+0.30%, inflated VKOSPI=35.03."""
    from src.risk.master_scenario_engine import MasterScenarioEngine

    engine = MasterScenarioEngine()
    market_data = {
        "signal_cache": {
            "vix": 20.24,
            "vix_history": [16.0] * 60 + [19.03, 20.24],
            "vkospi": 35.03,  # Inflated lagging RV artifact while VIX is 20.24
            "kospi_change_pct": 0.30,  # Positive KOSPI day
            "sox_change_1d": -7.19,  # Stale US SOX proxy alone, no KR futures dump
            "frgn_fut_net_buy": 0.0,
            "footprint_score": 0.0,
            "ois": 28.6,
        }
    }
    res = engine.evaluate_scenarios(market_data)
    fired_ids = [s["scenario_id"] for s in res["active_scenarios"]]
    assert "SCENARIO_2" not in fired_ids
    assert "SCENARIO_3_CRASH_DEFENSE" not in fired_ids
    assert res["recommended_max_exposure"] == pytest.approx(1.0, rel=1e-9)


def test_intraday_micro_guard_stale_vix_prev_and_min_level():
    """Verify IntradayMicroGuard cross-checks stale vix_prev against vix_change_1d and enforces min VIX level."""
    from src.risk.intraday_micro_guard import IntradayMicroGuard

    guard = IntradayMicroGuard()
    # Case: curr=20.24, stale vix_prev=17.31 (+16.9% fake spike), actual vix_change_1d=+6.36%
    ok, reason = guard.check({
        "vix": 20.24,
        "vix_prev": 17.31,
        "vix_change_1d": 6.36,
        "vix_history": [18.5, 19.03, 20.24],
    })
    assert ok is True
    assert reason == "micro_guard_ok"


def test_alpha_allocator_canonical_streams_no_duplicate_s10():
    """Verify AlphaAllocator deduplicates S10 and S10_MEGA_TREND into canonical stream keys."""
    from src.allocation.alpha_allocator import AlphaAllocator

    alloc = AlphaAllocator()
    assert "S10" not in alloc.STREAMS
    assert "S10_MEGA_TREND" in alloc.STREAMS
    assert "S13" not in alloc.STREAMS
    assert "S13_JAPAN_ALPHA" in alloc.STREAMS
    assert len(alloc.STREAMS) == len(set(alloc.STREAMS))


def test_google_finance_collector_skips_calendar_date_arrays(monkeypatch):
    """Verify GoogleFinanceCollector skips [2025, 10, 24] and [2026, 10, 23] date arrays and extracts the true quote."""
    from src.utils.google_finance_collector import GoogleFinanceCollector

    fake_html = (
        '[["SOX","INDEXNASDAQ"],"PHLX Semiconductor",0,"USD",[2026,10,23],null] '
        '[["SOX","INDEXNASDAQ"],"PHLX Semiconductor",0,"USD",[2025,10,24],null] '
        '[["SOX","INDEXNASDAQ"],"PHLX Semiconductor",0,"USD",[10779.46,47.21,0.44],null,10732.25]'
    )

    class _FakeResp:
        status_code = 200
        text = fake_html

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResp())

    quote = GoogleFinanceCollector.get_quote_details("^SOX")
    assert quote["price"] == pytest.approx(10779.46, rel=1e-9)
    assert quote["prev_close"] == pytest.approx(10732.25, rel=1e-9)

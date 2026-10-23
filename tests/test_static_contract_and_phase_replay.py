"""Hermetic tests for Static AST Contract Audit, Phase Aliasing, and Level-vs-Rate Sanity Guard."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from scripts.ops.static_contract_audit import audit_codebase
from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
from src.data_collection.intraday_flow_collector import IntradayFlowCollector
import src.data_collection.macro_realtime_refresher as mrr_mod


def test_full_codebase_static_ast_contract_zero_violations():
    """Verify all 329+ Python files have zero broken imports, missing script paths, or undefined names."""
    report = audit_codebase()
    assert report["broken_imports"] == [], f"Broken imports: {report['broken_imports']}"
    assert report["missing_scripts"] == [], f"Missing script paths: {report['missing_scripts']}"
    assert report["undefined_names"] == [], f"Undefined names: {report['undefined_names']}"


def test_macro_refresher_sanity_check_separates_levels_from_rates(tmp_path, monkeypatch):
    """Verify rate-of-change variables crossing zero (e.g. kosdaq_change_pct +4.46% -> -2.39%) are NOT rejected,
    while bogus price-level spikes (e.g. sox=2026.0 vs 10731.98) ARE rejected."""
    cache_file = tmp_path / "signal_cache.json"
    initial_cache = {
        "sox": 10731.978,
        "kosdaq_close": 820.0,
        "kosdaq_change_pct": 4.4636,
        "intraday_range_pct": 3.8745,
        "atr_5m": 0.007689,
    }
    cache_file.write_text(json.dumps(initial_cache), encoding="utf-8")
    monkeypatch.setattr(mrr_mod, "_SIGNAL_CACHE", cache_file)

    refresher = MacroRealtimeRefresher()
    refresher._update_cache(
        {
            "sox": 2026.0,  # Bogus calendar year scraped as index price -> MUST be rejected
            "kosdaq_change_pct": -2.3963,  # Rate crossing zero -> MUST be accepted
            "intraday_range_pct": 2.6934,  # Range rate change -> MUST be accepted
            "atr_5m": 0.005611,  # Volatility metric -> MUST be accepted
        }
    )
    updated = json.loads(cache_file.read_text(encoding="utf-8"))
    assert updated["sox"] == pytest.approx(10731.978, rel=1e-9)
    assert updated["kosdaq_change_pct"] == pytest.approx(-2.3963, rel=1e-9)
    assert updated["intraday_range_pct"] == pytest.approx(2.6934, rel=1e-9)
    assert updated["atr_5m"] == pytest.approx(0.005611, rel=1e-9)


def test_intraday_flow_collector_load_cache_missing_file_returns_empty_quietly(tmp_path):
    """Verify IntradayFlowCollector.load_cache returns {} without raising FileNotFoundError."""
    res = IntradayFlowCollector.load_cache(results_dir=tmp_path)
    assert res == {}


def test_critical_cross_module_aliases_import_cleanly():
    """Verify all historical cross-module symbol aliases import without ImportError."""
    from src.streams.s2_ml_alpha.ml_stream import MLStream, S2MLAlphaStream
    from src.streams.s4_advisory.advisory_stream import AdvisoryStream, S4AdvisoryStream
    from config.universe import UNIVERSE, Universe
    from src.data_collection.dart_collector import DARTCollector, run_dart_collection
    from src.measurement.pnl_attribution import PnLAttribution, compute_attribution_view
    from src.streams import _STREAM_NAV_HIST

    assert MLStream is S2MLAlphaStream
    assert AdvisoryStream is S4AdvisoryStream
    assert isinstance(UNIVERSE, dict) and "kr_stocks" in UNIVERSE
    assert callable(run_dart_collection)
    assert callable(compute_attribution_view)
    assert isinstance(_STREAM_NAV_HIST, dict)

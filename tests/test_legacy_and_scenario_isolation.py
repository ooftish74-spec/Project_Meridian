"""Hermetic unit tests verifying legacy file cleanup, scenario/shadow isolation, and SSOT contract integrity."""

import json
from pathlib import Path
import pytest


def test_universe_builder_and_train_ensemble_reject_non_equity_stems(tmp_path, monkeypatch):
    """build_universe and train_ensemble must reject macro/index/column-name parquets and return 0.0 liquidity on missing data."""
    import scripts.build_universe as bu
    import scripts.train_ensemble as te

    invalid_stems = [
        "^VIX", "^TNX", "KRW=X", "CL=F", "GC=F", "DX-Y.NYB",
        "BTC", "ETH", "us_vix", "investor_flow_kospi",
        "Capital_Goods", "Open", "close", "시가", "00680K",
        "us_MSFT", "test_atomic",
    ]
    for stem in invalid_stems:
        assert not te._is_valid_equity_ticker(stem), f"Expected {stem} to be rejected by _is_valid_equity_ticker"

    assert te._is_valid_equity_ticker("005930")
    assert te._is_valid_equity_ticker("069500")
    assert te._is_valid_equity_ticker("NVDA")
    assert te._is_valid_equity_ticker("QQQ")

    # Missing file liquidity must be 0.0 (never 10B KRW fallback)
    monkeypatch.setattr(bu, "_KR_MARKETS", tmp_path)
    monkeypatch.setattr(bu, "_US_PRICES", tmp_path)
    assert bu.compute_liquidity("999999", lookback_days=20) == pytest.approx(0.0, abs=1e-12)


def test_dynamic_universe_json_contains_only_valid_equities():
    """results/dynamic_universe.json must not contain any macro, futures, FX, or corrupted column-name entries."""
    import scripts.train_ensemble as te

    uni_path = Path(__file__).resolve().parent.parent / "results" / "dynamic_universe.json"
    if uni_path.exists():
        data = json.loads(uni_path.read_text(encoding="utf-8"))
        tickers = data if isinstance(data, list) else data.get("tickers", [])
        assert len(tickers) > 0
        invalid = [t for t in tickers if not te._is_valid_equity_ticker(str(t))]
        assert invalid == [], f"Polluted non-equity tickers found in dynamic_universe.json: {invalid}"


def test_drawdown_guard_live_mode_isolates_kis_portfolio_from_shadow_hwm(tmp_path, monkeypatch):
    """In live mode, DrawdownGuard must read kis_portfolio.json HWM and never use shadow_portfolio.json sleeve_a_hwm."""
    import src.risk.drawdown_guard as dg_mod

    kis_file = tmp_path / "kis_portfolio.json"
    shadow_file = tmp_path / "shadow_portfolio.json"

    kis_file.write_text(
        json.dumps({
            "nav": 19_607_437.0,
            "initial_capital": 19_600_000.0,
            "hwm": 19_607_437.0,
            "daily_returns": [0.0004],
        }),
        encoding="utf-8",
    )
    shadow_file.write_text(
        json.dumps({
            "nav": 18_500_000.0,
            "initial_capital": 20_000_000.0,
            "hwm": 21_899_409.0,
            "sleeve_a_nav": 11_700_000.0,
            "sleeve_a_hwm": 21_899_409.0,
            "daily_returns": [-0.05],
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(dg_mod, "_RESULTS_DIR", tmp_path)
    guard = dg_mod.DrawdownGuard()

    res = guard.check(nav=19_607_437.0)
    assert res["drawdown_pct"] == pytest.approx(0.0, abs=1e-9)
    assert res["dd_stage"] == 0
    assert res["action_required"] == "none"


def test_us_night_engine_does_not_revive_zombie_holdings_when_kis_us_empty(tmp_path, monkeypatch):
    """When kis_portfolio.json exists with 0 US holdings, USNightExecutionEngine must not fall back to us_night_holdings.json."""
    import src.execution.us_night_engine as une_mod

    kis_file = tmp_path / "kis_portfolio.json"
    zombie_file = tmp_path / "us_night_holdings.json"

    kis_file.write_text(
        json.dumps({
            "nav": 19_607_437.0,
            "holdings": {
                "005930": {"qty": 10, "avg_price": 70000, "current_price": 71000}
            },
        }),
        encoding="utf-8",
    )
    zombie_file.write_text(
        json.dumps([
            {"ticker": "TLT", "quantity": 5, "entry_price": 95.0, "highest_price": 96.0},
            {"ticker": "XLV", "quantity": 3, "entry_price": 140.0, "highest_price": 142.0},
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(une_mod, "_RESULTS_DIR", tmp_path)
    monkeypatch.setattr(une_mod, "_US_HOLDINGS_FILE", zombie_file)
    engine = une_mod.USNightExecutionEngine()
    assert engine.holdings == []


def test_stream_orchestrator_dd_overlay_never_loads_shadow_in_live_mode():
    """_apply_dd_overlay must never load shadow_portfolio.json when exec_mode == 'live' and live positions are empty."""
    from scripts.stream_orchestrator import StreamOrchestrator

    orch = StreamOrchestrator.__new__(StreamOrchestrator)
    orch.exec_mode = "live"
    empty_live_portfolio = {"nav": 19_600_000.0, "cash": 19_600_000.0, "positions": {}, "holdings": {}}

    res = orch._apply_dd_overlay(dd_action="reduce_50pct", portfolio=empty_live_portfolio, regime="caution")
    sell_orders = [o for o in res if o.get("action") == "sell"]
    assert sell_orders == []


def test_s2_ml_stream_paths_and_safe_pickle_alias():
    """S2MLAlphaStream must point _HISTORICAL_DIR to kr_markets, _FEATURE_STORE_DIR to feature_store, and export MLStream alias."""
    from src.streams.s2_ml_alpha.ml_stream import S2MLAlphaStream, MLStream, _HISTORICAL_DIR, _FEATURE_STORE_DIR

    assert MLStream is S2MLAlphaStream
    assert _HISTORICAL_DIR.name == "kr_markets"
    assert _FEATURE_STORE_DIR.name == "feature_store"

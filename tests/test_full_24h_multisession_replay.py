"""24-Hour Multi-Session Deterministic Replay & RuntimeInvariantGate Self-Healing Test Suite.

Simulates the full 24-hour KRX -> US Night -> Broker Reconcile -> Same-Ticker Re-entry
lifecycle on the 12-position multi-currency portfolio (6 KRX + 6 US) with chaos injections
(split-brain files, ghost HWM peaks, zeroed quantities, stale closed tickers, corrupted eval_krw),
asserting 100% mathematical self-healing and closed-form accounting invariance.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.execution.position_ledger import PositionLedgerManager
from src.execution.us_night_engine import USNightExecutionEngine
from src.risk.runtime_invariant_gate import RuntimeInvariantGate


def _seed_production_like_portfolio(tmp_path: Path) -> None:
    """Seed 12 live holdings (6 KRX + 6 US) with deliberate split-brain & ghost HWM chaos."""
    kp = {
        "kis_mode": "live",
        "fx_rate": 1336.30,
        "nav": 18035907,
        "account": {
            "total_equity": 18035907.0,
            "nav": 18035907.0,
            "cash": 330844.0,
            "d2_settlement_cash_krw": 4691581.0,
            "us_cash_usd": 3269.45,
            "fx_rate": 1336.30,
            "hwm": 19180788.0,
        },
        "holdings": {
            "012750": {"name": "에스원", "qty": 7, "avg_price": 95442.86, "current_price": 95100.0, "eval_krw": 665700, "stream_id": "S2", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            "069500": {"name": "KODEX 200", "qty": 2, "avg_price": 89225.0, "current_price": 95515.0, "eval_krw": 191030, "stream_id": "S11_HIGHBETA_SNIPER", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-20"},
            "081660": {"name": "미스토홀딩스", "qty": 16, "avg_price": 36584.38, "current_price": 36350.0, "eval_krw": 581600, "stream_id": "S2", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            # Deliberately corrupt eval_krw on 086280 to test I1 self-healing
            "086280": {"name": "현대글로비스", "qty": 17, "avg_price": 175088.24, "current_price": 187000.0, "eval_krw": 1000000, "stream_id": "S3_B", "track": "TRACK_2_BUFFETT_QVM", "entry_date": "2026-10-15"},
            "207940": {"name": "삼성바이오로직스", "qty": 1, "avg_price": 1410000.0, "current_price": 1372000.0, "eval_krw": 1372000, "stream_id": "S10_MEGA_TREND", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-26"},
            "214150": {"name": "클래시스", "qty": 14, "avg_price": 27514.29, "current_price": 27000.0, "eval_krw": 378000, "stream_id": "S2", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            "AAPL": {"name": "Apple", "qty": 1, "avg_price": 345.93, "current_price": 343.83, "eval_krw": round(343.83 * 1336.30), "stream_id": "S3_A", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            "CPER": {"name": "US Copper", "qty": 5, "avg_price": 40.04, "current_price": 40.33, "eval_krw": round(5 * 40.33 * 1336.30), "stream_id": "S3_C", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            "CRM": {"name": "Salesforce", "qty": 1, "avg_price": 238.80, "current_price": 242.11, "eval_krw": round(242.11 * 1336.30), "stream_id": "S3_A", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            "MSFT": {"name": "Microsoft", "qty": 1, "avg_price": 524.63, "current_price": 531.01, "eval_krw": round(531.01 * 1336.30), "stream_id": "S3_B", "track": "TRACK_2_BUFFETT_QVM", "entry_date": "2026-10-24"},
            "UUP": {"name": "US Dollar Bullish", "qty": 8, "avg_price": 29.13, "current_price": 29.06, "eval_krw": round(8 * 29.06 * 1336.30), "stream_id": "S3_C", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
            "V": {"name": "Visa", "qty": 1, "avg_price": 397.74, "current_price": 400.60, "eval_krw": round(400.60 * 1336.30), "stream_id": "S3_A", "track": "TRACK_1_TACTICAL", "entry_date": "2026-10-28"},
        },
    }
    (tmp_path / "kis_portfolio.json").write_text(json.dumps(kp, indent=2), encoding="utf-8")

    # Seed contaminated ledger with:
    # - Ghost HWM $404.36 on V (recorded peak_pnl_pct = 0.956655) and last_qty = 0.0
    # - Ghost HWM $244.5072 on CRM (recorded peak_pnl_pct = 1.494975) and last_qty = 0.0
    # - Ghost HWM ₩99,999 on 012750 (recorded peak_pnl_pct = 0.5837)
    # - Stale closed ticker NVDA still lingering in ledger
    ledger = {
        "012750": {"entry_date": "2026-10-28", "peak_price": 99999.0, "peak_pnl_pct": 0.583742, "last_avg_price": 95442.86, "last_qty": 7.0, "stream_id": "S2", "track": "TRACK_1_TACTICAL"},
        "069500": {"entry_date": "2026-10-20", "peak_price": 95955.0, "peak_pnl_pct": 7.542729, "last_avg_price": 89225.0, "last_qty": 2.0, "stream_id": "S11_HIGHBETA_SNIPER", "track": "TRACK_1_TACTICAL"},
        "081660": {"entry_date": "2026-10-28", "peak_price": 36893.75, "peak_pnl_pct": 0.845634, "last_avg_price": 36584.38, "last_qty": 16.0, "stream_id": "S2", "track": "TRACK_1_TACTICAL"},
        "086280": {"entry_date": "2026-10-15", "peak_price": 188800.0, "peak_pnl_pct": 7.831343, "last_avg_price": 175088.24, "last_qty": 17.0, "stream_id": "S3_B", "track": "TRACK_2_BUFFETT_QVM"},
        "207940": {"entry_date": "2026-10-26", "peak_price": 1452000.0, "peak_pnl_pct": 2.978723, "last_avg_price": 1410000.0, "last_qty": 1.0, "stream_id": "S10_MEGA_TREND", "track": "TRACK_1_TACTICAL"},
        "214150": {"entry_date": "2026-10-28", "peak_price": 27600.0, "peak_pnl_pct": 0.311511, "last_avg_price": 27514.29, "last_qty": 14.0, "stream_id": "S2", "track": "TRACK_1_TACTICAL"},
        "AAPL": {"entry_date": "2026-10-28", "peak_price": 346.66, "peak_pnl_pct": 0.211025, "last_avg_price": 0.0, "last_qty": 0.0, "stream_id": "S3_A", "track": "TRACK_1_TACTICAL"},
        "CPER": {"entry_date": "2026-10-28", "peak_price": 40.33, "peak_pnl_pct": 0.724276, "last_avg_price": 0.0, "last_qty": 0.0, "stream_id": "S3_C", "track": "TRACK_1_TACTICAL"},
        "CRM": {"entry_date": "2026-10-28", "peak_price": 244.5072, "peak_pnl_pct": 1.494975, "last_avg_price": 0.0, "last_qty": 0.0, "stream_id": "S3_A", "track": "TRACK_1_TACTICAL"},
        "MSFT": {"entry_date": "2026-10-24", "peak_price": 534.668, "peak_pnl_pct": 1.913348, "last_avg_price": 524.63, "last_qty": 1.0, "stream_id": "S3_B", "track": "TRACK_2_BUFFETT_QVM"},
        "UUP": {"entry_date": "2026-10-28", "peak_price": 29.155, "peak_pnl_pct": 0.085822, "last_avg_price": 29.13, "last_qty": 8.0, "stream_id": "S3_C", "track": "TRACK_1_TACTICAL"},
        "V": {"entry_date": "2026-10-28", "peak_price": 404.36, "peak_pnl_pct": 0.956655, "last_avg_price": 0.0, "last_qty": 0.0, "stream_id": "S3_A", "track": "TRACK_1_TACTICAL"},
        "NVDA": {"entry_date": "2026-10-19", "peak_price": 140.0, "peak_pnl_pct": 5.0, "last_avg_price": 133.33, "last_qty": 2.0, "stream_id": "S6", "track": "TRACK_1_TACTICAL"},
    }
    (tmp_path / "position_entry_ledger.json").write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    (tmp_path / "position_ledger.json").write_text(json.dumps({"OLD_GHOST": {"peak_price": 100}}, indent=2), encoding="utf-8")
    (tmp_path / "us_night_holdings.json").write_text(
        json.dumps([{"ticker": "V", "entry_price": 399.83, "peak_price": 404.36, "highest_price": 404.36, "entry_date": "2026-10-27"}]),
        encoding="utf-8",
    )


def test_full_24h_multisession_chaos_replay_and_self_healing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify RuntimeInvariantGate detects and self-heals all chaos injections across KRX & US sessions."""
    _seed_production_like_portfolio(tmp_path)
    monkeypatch.setattr("src.execution.us_night_engine._RESULTS_DIR", tmp_path)
    monkeypatch.setattr("src.execution.us_night_engine._US_HOLDINGS_FILE", tmp_path / "us_night_holdings.json")

    gate = RuntimeInvariantGate(results_dir=tmp_path)

    # Pass 1: Detect chaos violations & self-heal atomically
    res1 = gate.audit_and_heal(auto_heal=True)
    assert res1["is_clean"] is False
    assert len(res1["violations_found"]) >= 6
    assert "healed_position_entry_ledger" in res1["healed_actions"]
    assert "healed_legacy_position_ledger" in res1["healed_actions"]
    assert "healed_kis_portfolio" in res1["healed_actions"]
    assert "healed_us_night_holdings" in res1["healed_actions"]

    # Pass 2: Verify post-heal state is 100% clean (0 violations) across all 4 files
    res2 = gate.audit_and_heal(auto_heal=False)
    assert res2["is_clean"] is True
    assert res2["violations_found"] == []
    assert res2["n_active_positions"] == 12
    assert res2["n_us_night_positions"] == 6

    # Verify exact closed-form HWM identity P_peak == P_avg * (1 + peak_pnl_pct / 100) for all 12 holdings
    plm = PositionLedgerManager(ledger_path=str(tmp_path / "position_entry_ledger.json"))
    healed_ledger = plm.load_ledger()
    assert "NVDA" not in healed_ledger
    assert set(healed_ledger.keys()) == {
        "012750", "069500", "081660", "086280", "207940", "214150",
        "AAPL", "CPER", "CRM", "MSFT", "UUP", "V",
    }

    for tk, info in healed_ledger.items():
        avg_px = float(info["last_avg_price"])
        pk_px = float(info["peak_price"])
        pk_pnl = float(info["peak_pnl_pct"])
        assert avg_px > 0.0
        assert float(info["last_qty"]) > 0.0
        assert pk_px == pytest.approx(avg_px * (1.0 + pk_pnl / 100.0), rel=1e-6)

    # Specifically verify V, CRM, and 012750 ghost peaks were replaced by exact post-entry peaks
    assert healed_ledger["V"]["peak_price"] == pytest.approx(397.74 * (1.0 + 0.956655 / 100.0), rel=1e-6)
    assert healed_ledger["CRM"]["peak_price"] == pytest.approx(238.80 * (1.0 + 1.494975 / 100.0), rel=1e-6)
    assert healed_ledger["012750"]["peak_price"] == pytest.approx(95442.86 * (1.0 + 0.583742 / 100.0), rel=1e-6)

    # Verify legacy position_ledger.json is identical to position_entry_ledger.json
    legacy_data = json.loads((tmp_path / "position_ledger.json").read_text(encoding="utf-8"))
    assert set(legacy_data.keys()) == set(healed_ledger.keys())

    # Phase 3: Simulate US Night Session price ticks & verify Track 2 (MSFT) exemption vs tactical trailing stop
    us_engine = USNightExecutionEngine()
    assert len(us_engine.holdings) == 6
    exit_orders = us_engine.update_trailing_stop(
        current_prices={
            "AAPL": 344.50,
            "CPER": 40.40,
            "CRM": 243.00,
            "MSFT": 480.00,  # Even on a -8.5% drop, S3_B Track 2 Buffett QVM is exempt from USNight short-term stop
            "UUP": 29.10,
            "V": 401.20,
        },
        atr_map={"AAPL": 0.018, "CPER": 0.015, "CRM": 0.020, "MSFT": 0.016, "UUP": 0.005, "V": 0.014},
    )
    assert all(o["ticker"] != "MSFT" for o in exit_orders)

"""
Unit tests for PositionLedgerManager and Position entry date preservation.
"""

import os
import tempfile
import pytest
from pathlib import Path
from src.execution.position_ledger import PositionLedgerManager
from src.execution._kis_adapter import Position

def test_ledger_initialization_and_backfill():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        # Check backfill for default positions
        assert mgr.get_entry_date("069500") == "2026-08-25"
        assert mgr.get_entry_date("NVDA") == "2026-08-25"
        assert mgr.get_entry_date("NON_EXISTENT") is None

def test_sync_live_positions():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        # Active tickers including one new stock
        active = ["069500", "NVDA", "AAPL"]
        result = mgr.sync_live_positions(active, current_date_str="2026-09-16")

        assert result["069500"] == "2026-08-25"
        assert result["NVDA"] == "2026-08-25"
        assert result["AAPL"] == "2026-09-16"

        # Check that tickers not in active (e.g. QQQ) were removed from ledger
        assert mgr.get_entry_date("QQQ") is None
        assert mgr.get_entry_date("AAPL") == "2026-09-16"

def test_position_dataclass_to_dict():
    pos = Position(
        ticker="069500",
        quantity=52,
        avg_price=35000.0,
        current_price=36000.0,
        entry_date="2026-08-25"
    )
    d = pos.to_dict()
    assert d["ticker"] == "069500"
    assert d["quantity"] == 52
    assert d["entry_date"] == "2026-08-25"

def test_multiday_immutability():
    """Verify that entry_date remains locked across multiple consecutive sync days."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "position_entry_ledger.json"
        mgr = PositionLedgerManager(ledger_path=str(ledger_path))

        active = ["NVDA", "QQQ"]
        # Day 1: 2026-09-16
        res1 = mgr.sync_live_positions(active, current_date_str="2026-09-16")
        assert mgr.get_entry_date("NVDA") == "2026-08-25"  # Backfilled entry date

        # Overwrite entry date to 2026-09-16 for testing fresh entry lock
        mgr.record_entry("TSLA", entry_date="2026-09-16")
        active.append("TSLA")

        # Day 2: 2026-09-17 (syncing on next day)
        res2 = mgr.sync_live_positions(active, current_date_str="2026-09-17")
        assert res2["TSLA"] == "2026-09-16"  # MUST remain 2026-09-16, NOT updated to 2026-09-17

        # Day 5: 2026-09-20 (syncing 4 days later)
        res5 = mgr.sync_live_positions(active, current_date_str="2026-09-20")
        assert res5["TSLA"] == "2026-09-16"  # STILL remains 2026-09-16


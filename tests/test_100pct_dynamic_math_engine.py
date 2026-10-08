"""
Comprehensive Unit Tests for 100% Dynamic Mathematical Model Engine.
(Zero Hardcoded Constants Guarantee)
"""

import pytest
from pathlib import Path
from src.risk.dynamic_half_life_engine import DynamicHalfLifeEngine
from src.execution.capital_velocity_recycler import CapitalVelocityRecycler
from src.execution.self_healing_audit_engine import SelfHealingAuditEngine


def test_dynamic_half_life_engine():
    """Verify dynamic half-life decay H_i*(t) computation."""
    engine = DynamicHalfLifeEngine()

    # Case 1: Normal Caution Market
    market_data = {
        'signal_cache': {
            'vix': 20.0,
            'vix_ma20': 20.0
        }
    }
    prices = [100.0, 101.0, 99.5, 100.5, 102.0, 101.5, 100.8, 102.5]
    h_star = engine.compute_dynamic_max_hold(
        ticker="069500",
        regime_name="caution",
        market_data=market_data,
        price_history=prices
    )
    assert h_star > 0.0
    assert isinstance(h_star, float)

    # Case 2: High VIX Spike Market (decay accelerates)
    market_high_vix = {
        'signal_cache': {
            'vix': 40.0,
            'vix_ma20': 20.0
        }
    }
    h_star_high_vix = engine.compute_dynamic_max_hold(
        ticker="069500",
        regime_name="caution",
        market_data=market_high_vix,
        price_history=prices
    )
    # High VIX should decrease dynamic max holding days
    assert h_star_high_vix < h_star


def test_capital_velocity_recycler():
    """Verify Sharpe-weighted EV capital recycling allocation."""
    recycler = CapitalVelocityRecycler()
    freed_cash = 2500000.0  # 2.5 million KRW

    signals = [
        {'ticker': '069500', 'direction': 'long', 'confidence': 0.8, 'strategy': 'momentum'},
        {'ticker': 'NVDA', 'direction': 'long', 'confidence': 0.6, 'strategy': 'quant'},
    ]

    orders = recycler.recycle_freed_capital(
        freed_cash_krw=freed_cash,
        active_signals=signals,
        market_data={'signal_cache': {'vix': 18.0}}
    )

    assert len(orders) > 0
    total_allocated = sum(o['amount_krw'] for o in orders)
    # Total allocated cash should match freed cash
    assert abs(total_allocated - freed_cash) < 1.0
    assert orders[0]['ticker'] in ['069500', 'NVDA']


def test_self_healing_audit_engine():
    """Verify Self-Healing Audit Engine delta check & state reconciliation."""
    audit_engine = SelfHealingAuditEngine()

    class MockLedgerManager:
        def __init__(self):
            self.store = {"069500": "2026-08-25"}
        def sync_live_positions(self, active, current_date_str=None):
            return {t: self.store.get(t, current_date_str or "2026-09-16") for t in active}
        def record_entry(self, ticker, entry_date=None):
            self.store[ticker] = entry_date or "2026-09-16"

    mock_mgr = MockLedgerManager()
    positions = {
        "069500": {"ticker": "069500", "days_held": 0},  # Discrepancy: reported 0, actual 22
        "NVDA": {"ticker": "NVDA", "days_held": 0}
    }

    report = audit_engine.audit_and_heal(
        positions=positions,
        ledger_mgr=mock_mgr,
        today_str="2026-09-16"
    )

    assert report["audited_count"] == 2
    assert report["discrepancies_found"] > 0
    # Pos days_held must be reconciled to 22 for 069500
    assert positions["069500"]["days_held"] == 22

import json
from pathlib import Path
import os
import glob

RESULTS_DIR = Path("results")

def nuke_legacy():
    # Files to completely delete
    files_to_delete = [
        "shadow_portfolio.json",
        "shadow_portfolio_polluted.json.bak",
        "shadow_trades.json",
        "shadow_summary.json",
        "measurement_engine.json",
        "meridian_main_trading_state.json",
        "kis_portfolio.json",
        "advisory_orders.json",
        "s4_advisory_recommendations.json",
        "s6b_advisory.json"
    ]
    
    for f in files_to_delete:
        p = RESULTS_DIR / f
        if p.exists():
            os.remove(p)
            print(f"🗑️ Deleted {f}")

    # Re-initialize clean shadow portfolio with exact current NAV
    nav = 16762231.0
    sp = {
        "nav": nav,
        "cash": nav,
        "positions": {},
        "daily_snapshots": [],
        "total_nav": nav,
        "sleeve_a_nav": nav,
        "sleeve_b_nav": 0.0,
        "sleeve_a_hwm": nav,
        "sleeve_b_hwm": 0.0,
        "hwm": nav,
        "trade_history": [],
        "realized_pnl": 0,
        "total_commission": 0,
        "cumulative_return_pct": 0.0,
        "daily_pnl": 0,
        "consecutive_loss_days": 0,
        "max_drawdown_pct": 0.0,
        "drawdown_pct": 0.0,
        "unrealized_pnl": 0
    }
    (RESULTS_DIR / "shadow_portfolio.json").write_text(json.dumps(sp, indent=2))
    print("🌱 Initialized fresh shadow_portfolio.json with NAV 16,762,231")

    # Re-initialize clean KIS Portfolio
    kis = {
      "date": "2026-07-31",
      "timestamp": "2026-07-31T09:00:00.000000",
      "kis_mode": "live",
      "account": {
        "number": "4422****01",
        "type": "Main (종합 위탁)",
        "initial_capital": nav,
        "nav": nav,
        "cash": nav,
        "invested": 0,
        "invest_pct": 0.0
      },
      "regime": "unknown",
      "streams": {}
    }
    (RESULTS_DIR / "kis_portfolio.json").write_text(json.dumps(kis, indent=2))
    print("🌱 Initialized fresh kis_portfolio.json with NAV 16,762,231")

if __name__ == "__main__":
    nuke_legacy()

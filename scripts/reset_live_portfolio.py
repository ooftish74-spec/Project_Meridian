import json
from pathlib import Path

results_dir = Path("results")
sp_path = results_dir / "shadow_portfolio.json"
me_path = results_dir / "measurement_engine.json"

if sp_path.exists():
    sp_path.rename(results_dir / "shadow_portfolio_polluted.json.bak")
if me_path.exists():
    me_path.rename(results_dir / "measurement_engine_polluted.json.bak")

initial_capital = 1601718.0

fresh_sp = {
    "nav": initial_capital,
    "cash": initial_capital,
    "positions": {},
    "daily_snapshots": [],
    "total_nav": initial_capital,
    "sleeve_a_nav": initial_capital,
    "sleeve_b_nav": 0.0,
    "sleeve_a_hwm": initial_capital,
    "sleeve_b_hwm": 0.0,
    "hwm": initial_capital
}

fresh_me = {
    "portfolio": {
        "nav": initial_capital,
        "cash": initial_capital
    },
    "daily_series": []
}

sp_path.write_text(json.dumps(fresh_sp, indent=2))
me_path.write_text(json.dumps(fresh_me, indent=2))

print("Portfolio reset successfully.")

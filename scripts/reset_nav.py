import json
import os

PORTFOLIO_PATH = "results/shadow_portfolio.json"
HALT_FLAG_PATH = "results/SYSTEM_HALT.flag"

def reset_nav():
    # 1. Remove Halt Flag
    if os.path.exists(HALT_FLAG_PATH):
        os.remove(HALT_FLAG_PATH)
        print(f"✅ Removed {HALT_FLAG_PATH}")
    
    # 2. Update internal portfolio NAV to 16.7M
    target_nav = 16762231.0
    if os.path.exists(PORTFOLIO_PATH):
        from src.utils.file_ops import atomic_write_json

        with open(PORTFOLIO_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        data["total_nav"] = target_nav
        data["cash"] = target_nav
        data["positions"] = {}
        
        atomic_write_json(PORTFOLIO_PATH, data, indent=4)
        print(f"✅ Updated Portfolio NAV to {target_nav}")
    else:
        # Create it if missing
        os.makedirs("results", exist_ok=True)
        data = {
            "total_nav": target_nav,
            "cash": target_nav,
            "positions": {}
        }
        from src.utils.file_ops import atomic_write_json

        atomic_write_json(PORTFOLIO_PATH, data, indent=4)
        print(f"✅ Created Portfolio NAV at {target_nav}")

if __name__ == "__main__":
    reset_nav()

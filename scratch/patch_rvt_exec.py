import re

with open('Project_Meridian/scripts/run_virtual_trading.py', 'r') as f:
    content = f.read()

# 1. Inject import
if "from src.execution.execution_engine import ExecutionEngine" not in content:
    content = content.replace(
        "from src.portfolio.shadow_manager import ShadowPortfolioManager",
        "from src.portfolio.shadow_manager import ShadowPortfolioManager\nfrom src.execution.execution_engine import ExecutionEngine\nfrom config.dynamic_config import DynamicConfig"
    )

# 2. Inject helper function to run real execution
exec_helper = """
def _run_real_execution(orders, prices, mgr):
    try:
        mode = DynamicConfig().get('execution.current_mode', 'mock')
        ee = ExecutionEngine(mode=mode)
        real_orders = []
        for o in orders:
            qty = o.get('quantity', 0)
            if qty <= 0: continue
            action = 'buy' if 'confidence' in o else 'sell'
            real_orders.append({
                'stream': o.get('stream_id', 'H' if 'streams' in o else 'S1'),
                'action': action,
                'ticker': o.get('ticker'),
                'amount': qty * prices.get(o.get('ticker'), 0),
                'quantity': qty,
                'reason': o.get('reason', 'V3 Engine')
            })
        if real_orders:
            res = ee.execute(real_orders, portfolio=mgr.get_summary())
            return res
    except Exception as e:
        print(f"  ❌ ExecutionEngine 연동 실패: {e}")
    return None

"""

if "def _run_real_execution" not in content:
    content = content.replace("def main(target_date: str = None):", exec_helper + "\ndef main(target_date: str = None):")

# 3. Patch step 6 (Sells)
content = content.replace(
    "if sell_orders:\n        logger.info(f\"\\n📋 Step 7: 매도 실행 ({len(sell_orders)}건)\")",
    "if sell_orders:\n        logger.info(f\"\\n📋 Step 7: 매도 실행 ({len(sell_orders)}건)\")\n        _run_real_execution(sell_orders, prices, mgr)"
)

# 4. Patch step 8 (Buys)
content = content.replace(
    "if buy_orders:\n        logger.info(f\"\\n📋 Step 9: 매수 실행\")",
    "if buy_orders:\n        logger.info(f\"\\n📋 Step 9: 매수 실행\")\n        _run_real_execution(buy_orders, prices, mgr)"
)

# 5. Patch MTM sells (intraday exits)
content = content.replace(
    "if s1_sells:\n                logger.info(f\"    📊 S1 ETF 매도",
    "_run_real_execution(s1_sells + other_sells, prices, mgr)\n            if s1_sells:\n                logger.info(f\"    📊 S1 ETF 매도"
)

with open('Project_Meridian/scripts/run_virtual_trading.py', 'w') as f:
    f.write(content)
print("run_virtual_trading.py patched successfully.")

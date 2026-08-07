import sys
sys.path.append('.')
try:
    from src.execution._kis_adapter import KISAdapter
    adapter = KISAdapter()
    print("Connecting to KIS API...")
    # Typically adapters have a method to fetch open orders or cancel all
    # We will try to call cancel_unfilled_premarket_orders() which we saw earlier,
    # or just use the generic order manager emergency cancel
except Exception as e:
    print(f"Error: {e}")

from src.execution.order_manager import OrderManager
try:
    om = OrderManager()
    res = om._emergency_cancel(orders=[], stream_id='MANUAL_CANCEL', reason='User requested flush')
    print("OrderManager Emergency Cancel Result:", res)
except Exception as e:
    print(f"OM Error: {e}")

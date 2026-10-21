"""
Verification script for 3 Software Enhancement Pillars
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_collection.kis_websocket_manager import KISWebSocketStreamingManager
from src.alpha_factory.symbolic_alpha_generator import DynamicSymbolicAlphaGenerator
from src.execution.smart_order_router import DynamicSmartOrderRouter

def test_pillar1_websocket():
    ws_mgr = KISWebSocketStreamingManager()
    lag1 = ws_mgr.push_tick("069500", 111880.0, 1000)
    lag2 = ws_mgr.push_tick("091160", 136695.0, 500)
    ob = ws_mgr.push_orderbook("069500", 111890.0, 111880.0, 500, 800)
    tick = ws_mgr.get_latest_tick("069500")
    assert tick is not None
    assert tick['price'] == 111880.0
    print("✅ Pillar 1 WebSocket Manager Test PASSED!")

def test_pillar2_symbolic_alpha():
    gen = DynamicSymbolicAlphaGenerator()
    features = [
        {'ticker': '091160', 'return_20d': 0.065, 'high_low_range': 0.02, 'intraday_return': 0.01, 'ma20_dist': 0.03},
        {'ticker': '069500', 'return_20d': 0.038, 'high_low_range': 0.015, 'intraday_return': 0.005, 'ma20_dist': 0.015},
        {'ticker': 'NVDA', 'return_20d': 0.064, 'high_low_range': 0.025, 'intraday_return': 0.012, 'ma20_dist': 0.04},
        {'ticker': 'SOXX', 'return_20d': 0.105, 'high_low_range': 0.03, 'intraday_return': 0.02, 'ma20_dist': 0.05}
    ]
    alphas = gen.generate_symbolic_alphas(features)
    assert len(alphas) == 4
    assert alphas[0]['ticker'] == 'SOXX'  # Top dynamic rank
    print("✅ Pillar 2 Symbolic Alpha Generator Test PASSED!")

def test_pillar3_smart_order_router():
    sor = DynamicSmartOrderRouter(nxt_enabled=True)
    krx_depth = {'ask1': 1000.0, 'bid1': 995.0, 'ask_vol1': 500, 'bid_vol1': 500, 'spread': 5.0}
    nxt_depth = {'ask1': 998.0, 'bid1': 995.0, 'ask_vol1': 200, 'bid_vol1': 200, 'spread': 3.0}
    
    route = sor.route_order("069500", 300, krx_depth, nxt_depth, is_buy=True)
    assert route['nxt_quantity'] == 200
    assert route['krx_quantity'] == 100
    assert route['recommended_venue'] == 'SPLIT'
    print("✅ Pillar 3 Smart Order Router Test PASSED!")

if __name__ == '__main__':
    test_pillar1_websocket()
    test_pillar2_symbolic_alpha()
    test_pillar3_smart_order_router()
    print("\n🎉 ALL 3 PILLARS PASSED 100% VERIFICATION!")

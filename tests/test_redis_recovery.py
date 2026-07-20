import pytest
import time
from src.portfolio.state_backend import RedisStateBackend
from src.portfolio.shadow_manager import ShadowPortfolioManager

@pytest.fixture
def state_backend():
    # Use fakeredis for reliable testing without external daemon
    sb = RedisStateBackend(host='invalid_host_to_force_fake')
    sb.use_fake = True
    import fakeredis
    sb.r = fakeredis.FakeRedis(decode_responses=True)
    sb.clear_state()
    return sb

def test_save_and_load_position(state_backend):
    pos_data = {
        'ticker': '005930',
        'quantity': 100,
        'avg_price': 60000,
        'market_value': 6000000
    }
    
    # Save
    start_time = time.time()
    state_backend.save_position('S2:005930', pos_data)
    elapsed = time.time() - start_time
    assert elapsed < 0.05, f"Save took {elapsed}s, should be < 50ms"
    
    # Load Full State
    state = state_backend.load_full_state()
    
    assert 'S2:005930' in state['positions']
    assert state['positions']['S2:005930']['quantity'] == 100
    assert state['positions']['S2:005930']['avg_price'] == 60000

def test_order_intents(state_backend):
    intent = {
        'ticker': '000660',
        'quantity': 50,
        'action': 'BUY'
    }
    state_backend.log_order_intent('ord_123', intent)
    
    state = state_backend.load_full_state()
    assert 'ord_123' in state['pending_intents']
    assert state['pending_intents']['ord_123']['ticker'] == '000660'
    
    state_backend.remove_order_intent('ord_123')
    state2 = state_backend.load_full_state()
    assert 'ord_123' not in state2['pending_intents']

def test_capital_and_history(state_backend):
    cap = {'cash': 1000000, 'nav': 5000000}
    hist = [{'action': 'BUY', 'ticker': '005930'}]
    
    state_backend.save_capital(cap)
    state_backend.save_trade_history(hist)
    
    state = state_backend.load_full_state()
    assert state['capital']['cash'] == 1000000
    assert len(state['trade_history']) == 1
    assert state['trade_history'][0]['ticker'] == '005930'

if __name__ == '__main__':
    pytest.main(['-v', __file__])

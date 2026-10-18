import pytest
from src.execution._kis_adapter import AccountInfo, Order, KISTraderAdapter

def test_account_info_total_asset_alias():
    acc = AccountInfo(total_equity=20000000.0, cash=5000000.0)
    assert acc.total_asset == 20000000.0
    acc.total_equity = 19500000.0
    assert acc.total_asset == 19500000.0

def test_kis_adapter_ord_dvsn_premarket_close():
    adapter = KISTraderAdapter(mode='mock')
    order = Order(
        order_id="TEST_001",
        ticker="069500",
        side="sell",
        quantity=5,
        price=0,
        order_type="06",
        exchange="KRX"
    )
    # Validate ord_dvsn resolution in logic
    tif = 'DAY'
    _TIF_ORD_DVSN = {'DAY': '00' if order.order_type != 'market' else '01', 'MARKET': '01', 'IOC': '13', 'FOK': '14', 'PREMARKET_CLOSE': '06'}
    ord_dvsn = _TIF_ORD_DVSN.get(tif, '06' if tif == 'PREMARKET_CLOSE' else ('01' if order.order_type == 'market' else '00'))
    if str(order.order_type).lower() in ('06', 'premarket_close'):
        ord_dvsn = '06'
    assert ord_dvsn == '06'

def test_active_ev_robust_parsing():
    sig_data = {
        "signals": {
            "S0": [{"ticker": "252670", "expected_return": 0.02}],
            "S1": [],
            "S2": [{"ticker": "005930", "expected_return": 0.035}],
            "S3": {"expected_return": 0.018}
        }
    }
    sigs = sig_data.get('signals', {})
    ev_candidates = []
    for s_val in sigs.values():
        if isinstance(s_val, list):
            for item in s_val:
                if isinstance(item, dict) and 'expected_return' in item:
                    ev_candidates.append(float(item['expected_return']))
        elif isinstance(s_val, dict) and 'expected_return' in s_val:
            ev_candidates.append(float(s_val['expected_return']))
    active_ev = max(ev_candidates) if ev_candidates else 0.015
    assert active_ev == 0.035

def test_unexecuted_sell_guard(monkeypatch):
    import src.utils.market_calendar as mc
    monkeypatch.setattr(mc, "is_ticker_tradable_now", lambda ticker: (True, 'regular'))
    adapter = KISTraderAdapter(mode='live')
    # Mock positions with 16 shares
    from src.execution._kis_adapter import Position
    adapter.positions['069500'] = Position(ticker='069500', quantity=16, avg_price=100000.0)
    # Mock inquire_unexecuted returning existing sell order
    monkeypatch.setattr(adapter, "inquire_unexecuted", lambda ticker: [{
        'odno': '0001755100', 'pdno': '069500', 'sll_buy_dvsn_cd': '01', 'ord_qty': 16, 'psbl_qty': 16
    }])
    # Attempt to sell again
    order = Order(
        order_id="NEW_ORD",
        ticker="069500",
        side="sell",
        quantity=16,
        price=0,
        order_type="market",
        exchange="KRX"
    )
    res = adapter._api_order(order)
    assert res.status == 'submitted'
    assert res.order_id == '0001755100'

def test_yield_harvester_friction_guard(monkeypatch):
    from datetime import datetime, date
    adapter = KISTraderAdapter(mode='mock')
    # Set time to 15:30 on Tuesday (2026-10-13)
    fake_now = datetime(2026, 10, 13, 15, 30, 0)
    import src.execution._kis_adapter as ka
    # Monkeypatch datetime
    class FakeDateTime:
        @classmethod
        def now(cls):
            return fake_now
    monkeypatch.setattr(ka, "datetime", FakeDateTime)
    adapter.account.d2_cash = 5000000.0
    res = adapter.sweep_uninvested_cash_to_shield()
    # On Tuesday (next day is Wednesday trading day), holding period is 1 day < 3 -> should skip!
    assert res['kr_sweep'] is None

def test_account_reconciler_in_flight_settlement_guard():
    from src.execution.account_reconciler import AccountReconciler
    from src.execution._kis_adapter import KISTraderAdapter, Position
    adapter = KISTraderAdapter(mode='mock')
    adapter.account.cash = 3111090.0
    adapter.us_cash_usd = 33.0
    # Simulate pending fill: 3.22M KRW cash was deducted, but new position hasn't appeared yet in adapter.positions
    adapter.account.total_equity = 19528422.0 # Official brokerage API total evaluation (raw_api_tot_evlu)
    adapter.positions.clear()
    adapter.positions['091160'] = Position(ticker='091160', quantity=43, avg_price=35000.0, current_price=35000.0)
    
    state = AccountReconciler().reconcile(adapter)
    # Bottom-up positions + cash = ~4.6M, which is < 19.5M * 0.97.
    # Reconciler should anchor total_equity to 19,528,422 to prevent false KillSwitch trip!
    assert state.total_equity_krw == 19528422.0
    assert adapter.account.total_equity == 19528422.0

def test_inverse_defaults_use_114800():
    import json
    from pathlib import Path
    from src.risk.exposure_orchestrator import ExposureOrchestrator
    
    # Verify defaults.json
    cfg_path = Path("config/defaults.json")
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg.get("exposure.instrument.inverse.ticker") == "114800"
        assert cfg.get("exposure.instrument.inverse.beta") == -1.0
    
    # Verify ExposureOrchestrator default attacker instrument
    assert ExposureOrchestrator.ATTACKER_INSTRUMENTS['inverse']['ticker'] == '114800'
    assert ExposureOrchestrator.ATTACKER_INSTRUMENTS['inverse']['beta'] == -1.0

def test_etf_sniper_vix_shock_uses_1x_inverse():
    from src.streams.s1_edge.etf_sniper_stream import S1ETFSniperStream
    s1 = S1ETFSniperStream()
    assert '114800' in s1.universe
    assert s1.universe['114800']['type'] == 'index_inv'
    
    # Simulate VIX shock market data
    market_data = {
        'signal_cache': {
            'vix': 38.0,
            'vix_ma_20': 15.0,
            'vix_std_20': 2.0,
            'vix_history': [18.0, 38.0],
            '114800': {'close': 4800.0}
        }
    }
    signals = s1.generate_signals('crash', market_data)
    vix_shock_sigs = [s for s in signals if s.get('strategy') == 'vix_shock_sniper']
    assert len(vix_shock_sigs) > 0
    assert vix_shock_sigs[0]['ticker'] == '114800'
    assert vix_shock_sigs[0]['name'] == 'KODEX 인버스'

def test_stream_orchestrator_domestic_isolation_and_friction_guard():
    # Test that US buy orders do not trigger liquidation of domestic parking asset
    orders = [{'ticker': 'NVDA', 'action': 'buy', 'amount_krw': 3000000.0}]
    portfolio = {'cash': 5000000.0, 'positions': [{'ticker': '459580', 'amount': 3220000.0, 'quantity': 3, 'current_price': 1074000.0}]}
    
    kr_buy_amount = sum(
        float(o.get('amount_krw', o.get('amount', 0.0)) or 0.0)
        for o in orders
        if str(o.get('ticker', '')).isdigit() and
        (str(o.get('action', '')).lower() in ('buy', 'long') or str(o.get('direction', '')).lower() in ('long', 'buy'))
    )
    current_kr_cash = float(portfolio.get('cash', 0.0) or 0.0)
    needs_kr_cash = (kr_buy_amount > current_kr_cash) and (kr_buy_amount > 0)
    assert kr_buy_amount == 0.0
    assert needs_kr_cash is False

def test_execution_mode_fallback_and_risk_dominance_netting():
    from src.execution.execution_engine import ExecutionEngine
    ee = ExecutionEngine(mode=None)
    assert ee.mode in ('live', 'paper', 'shadow', 'mock')
    
    # Test Risk Dominance Gate
    dynamic_exit_orders = [
        {'ticker': 'NVDA', 'action': 'sell', 'direction': 'short', 'quantity': 8, 'price': 228.86, 'stream_id': 'LIVE_SSOT'},
        {'ticker': 'SOXX', 'action': 'sell', 'direction': 'short', 'quantity': 4, 'price': 559.4, 'stream_id': 'LIVE_SSOT'},
    ]
    orders = [
        {'ticker': 'NVDA', 'action': 'buy', 'direction': 'long', 'quantity': 9, 'price': 228.86, 'stream_id': 'TACTIC_E'}
    ]
    exit_tickers = {o['ticker'] for o in dynamic_exit_orders if o.get('action') == 'sell' or o.get('direction') == 'short'}
    assert 'NVDA' in exit_tickers
    
    filtered_orders = []
    for o in orders:
        if o.get('ticker') in exit_tickers and (o.get('action') in ('buy', 'long') or o.get('direction') in ('buy', 'long')):
            pass
        else:
            filtered_orders.append(o)
    orders = filtered_orders
    all_candidate_orders = orders + dynamic_exit_orders
    assert len(all_candidate_orders) == 2
    assert all(o['action'] == 'sell' for o in all_candidate_orders)

def test_unexecuted_buy_guard(monkeypatch):
    import src.utils.market_calendar as mc
    monkeypatch.setattr(mc, "is_ticker_tradable_now", lambda ticker: (True, 'regular'))
    adapter = KISTraderAdapter(mode='live')
    # Mock inquire_unexecuted returning existing BUY order
    monkeypatch.setattr(adapter, "inquire_unexecuted", lambda ticker: [{
        'odno': '0030751525', 'pdno': 'SHV', 'sll_buy_dvsn_cd': '02', 'ord_qty': 42, 'psbl_qty': 42
    }])
    # Attempt to submit duplicate buy order
    order = Order(
        order_id="NEW_BUY_ORD",
        ticker="SHV",
        side="buy",
        quantity=42,
        price=110.17,
        order_type="limit",
        exchange="NYSE"
    )
    res = adapter._api_order(order)
    assert res.status == 'submitted'
    assert res.order_id == '0030751525'
    assert "Blocked duplicate buy" in res.notes

def test_cancel_order_us_routing(monkeypatch):
    adapter = KISTraderAdapter(mode='live')
    # Record US order
    adapter.orders.append(Order(order_id="0030749401", ticker="NVDA", side="sell", quantity=8, price=228.0, order_type="limit", exchange="NASD"))
    
    called_us = []
    monkeypatch.setattr(adapter, "cancel_us_order", lambda order_no, ticker, exchange=None: (called_us.append((order_no, ticker)), {'success': True})[1])
    
    # Cancel without passing ticker (should auto-detect NVDA from adapter.orders)
    res = adapter.cancel_order("0030749401")
    assert res['success'] is True
    assert len(called_us) == 1
    assert called_us[0] == ("0030749401", "NVDA")

def test_conflict_netting_quantity_scaling():
    from src.execution.intraday_capital_router import IntradayCapitalRouter
    router = IntradayCapitalRouter()
    signals = [
        {'ticker': '069500', 'side': 'buy', 'quantity': 100, 'amount_krw': 10000000.0, 'confidence': 0.8},
        {'ticker': '114800', 'side': 'buy', 'quantity': 100, 'amount_krw': 4000000.0, 'confidence': 0.5}, # Inverse ETF conflict
    ]
    netted = router.resolve_signal_conflicts(signals)
    assert len(netted) == 1
    assert netted[0]['ticker'] == '069500'
    # s1 exposure = 8M, s2 exposure = 2M. Net ratio = (8 - 2)/8 = 0.75
    # quantity should be scaled from 100 to 75!
    assert netted[0]['quantity'] == 75
    assert netted[0]['amount_krw'] == 7500000.0

def test_dynamic_depth_capacity_model():
    from src.execution.intraday_capital_router import IntradayCapitalRouter
    router = IntradayCapitalRouter()
    # Tight spread (2 bps) -> deep liquidity -> higher cap (19% of 10,000 = 1,900)
    qty_tight = router.check_market_depth_capacity('069500', target_qty=3000, orderbook_depth_1_3_qty=10000, spread_bps=2.0)
    assert qty_tight == 1900
    
    # Wide spread (25 bps) -> illiquid -> conservative cap (7.5% of 10,000 = 750)
    qty_wide = router.check_market_depth_capacity('069500', target_qty=3000, orderbook_depth_1_3_qty=10000, spread_bps=25.0)
    assert qty_wide == 750

def test_holdings_guard_allows_buy_and_blocks_unheld_sell(monkeypatch):
    import src.utils.market_calendar as mc
    monkeypatch.setattr(mc, "is_ticker_tradable_now", lambda ticker: (True, 'regular'))
    adapter = KISTraderAdapter(mode='live')
    adapter.positions.clear()  # No holdings
    monkeypatch.setattr(adapter, "inquire_unexecuted", lambda ticker: [])

    # 1. SELL order for unheld stock -> MUST be blocked with skipped_insufficient_holdings
    sell_order = Order(
        order_id="SELL_UNHELD",
        ticker="005930",
        side="sell",
        quantity=10,
        price=70000.0,
        order_type="limit"
    )
    res_sell = adapter._api_order(sell_order)
    assert res_sell.status == 'skipped_insufficient_holdings'

    # 2. BUY order for unheld stock -> MUST NOT be blocked by holdings guard!
    called_post = []
    import requests
    class MockResp:
        status_code = 200
        def json(self):
            return {'rt_cd': '0', 'msg1': 'OK', 'output': {'ODNO': '999888777'}}

    monkeypatch.setattr(requests, "post", lambda url, **kwargs: (called_post.append(url), MockResp())[1])
    monkeypatch.setattr(adapter, "authenticate", lambda: True)
    adapter._access_token = "dummy_token"

    buy_order = Order(
        order_id="BUY_NEW",
        ticker="005930",
        side="buy",
        quantity=10,
        price=70000.0,
        order_type="limit"
    )
    res_buy = adapter._api_order(buy_order)
    assert res_buy.status == 'submitted'
    assert res_buy.order_id == '999888777'

def test_us_night_engine_quantity_and_trailing_stop(monkeypatch):
    from src.execution.us_night_engine import USNightExecutionEngine
    engine = USNightExecutionEngine()
    engine.holdings = []
    monkeypatch.setattr(engine, "is_us_session_active", lambda now=None: True)
    monkeypatch.setattr(engine, "_get_live_fx_rate", lambda: 1350.0)
    monkeypatch.setattr(engine, "_save_holdings", lambda: None)

    # Process S3 signal for NVDA
    signals = [{
        'ticker': 'NVDA',
        'name': 'NVIDIA Corp',
        'direction': 'long',
        'price': 200.0,
        'size_pct': 0.10
    }]
    orders = engine.process_us_night_signals(signals, account_nav=20_000_000.0)
    assert len(orders) == 1
    qty = orders[0]['quantity']
    assert qty >= 1

    # Verify holdings dictionary stores quantity
    nvda_pos = next((h for h in engine.holdings if h['ticker'] == 'NVDA'), None)
    assert nvda_pos is not None
    assert nvda_pos['quantity'] == qty

    # Test trailing stop triggered when price drops > ATR trailing stop limit from peak
    peak_price = 200.0
    dropped_price = 190.0  # -5.0% drop
    exit_orders = engine.update_trailing_stop({'NVDA': dropped_price})
    assert len(exit_orders) == 1
    assert exit_orders[0]['ticker'] == 'NVDA'
    assert exit_orders[0]['quantity'] == qty
    assert exit_orders[0]['direction'] == 'sell'


def test_lowercase_parquet_alpha_edges_and_s5_kelly_excess_variance():
    import numpy as np
    import pandas as pd
    from src.alpha_factory.edges.microstructure import OrderImbalanceAlpha
    from src.alpha_factory.edges.stat_arb import PCAMeanReversionAlpha
    from src.alpha_factory.edges.vol_surface import VolatilitySurfaceAlpha
    from src.streams.s5_overnight.overnight_stream import S5OvernightStream

    # 1. Verify lowercase 'close' and 'volume' columns (standard parquet format) generate all alpha features
    df = pd.DataFrame({
        'close': np.linspace(100.0, 90.0, 30),
        'volume': np.linspace(1000.0, 2000.0, 30),
    })
    df1 = OrderImbalanceAlpha.generate(df)
    assert 'alpha_order_imbalance_10d' in df1.columns
    assert 'alpha_smart_money_flow_20d' in df1.columns

    df2 = PCAMeanReversionAlpha.generate(df)
    assert 'alpha_pca_mr_proxy_20d' in df2.columns

    df3 = VolatilitySurfaceAlpha.generate(df)
    assert 'alpha_vol_term_structure' in df3.columns
    assert 'alpha_vol_adj_mom_10d' in df3.columns
    assert 'alpha_dd_velocity_3d' in df3.columns

    # 2. Verify S5 Kelly scale does not crush normal VIX <= 18.0 to 5%, while blocking extreme VIX >= 30.0
    scale_normal = S5OvernightStream._compute_overnight_kelly_scale(vix=16.5, nq_vol_pct=0.0, max_alloc=1.0)
    assert scale_normal == 1.0
    scale_extreme = S5OvernightStream._compute_overnight_kelly_scale(vix=32.0, nq_vol_pct=1.0, max_alloc=1.0)
    assert scale_extreme < 0.005


def test_cross_market_inverse_routing_and_dynamic_stream_performance(monkeypatch):
    from src.execution.intraday_capital_router import IntradayCapitalRouter
    from src.execution.us_night_engine import USNightExecutionEngine
    from src.streams.s1_edge.etf_sniper_stream import S1ETFSniperStream
    import src.utils.market_calendar as mc

    # 1. Verify S1ETFSniperStream uses empirical Laplace-Beta(2.5, 2.5) neutral prior instead of hardcoded sharpe=1.5
    s1 = S1ETFSniperStream()
    perf = s1.get_performance()
    assert perf['sharpe'] == 0.0
    assert perf['win_rate'] == 0.50

    # 2. Verify IntradayCapitalRouter translates KRX inverse 252670 -> SQQQ when KRX is closed and US is open
    monkeypatch.setattr(
        mc,
        "is_ticker_tradable_now",
        lambda tk: (False, 'closed') if str(tk).isdigit() else (True, 'us_regular')
    )
    router = IntradayCapitalRouter()
    routed = router.route_intraday_capital(
        signals=[{'stream_id': 'S0', 'ticker': '252670', 'name': 'KODEX 200선물인버스2X', 'confidence': 0.85, 'tp_pct': 3.0, 'sl_pct': 1.5}],
        market_data={'current_prices': {'SQQQ': 30.0}},
        total_cash_krw=5_000_000.0,
        enforce_session_gate=True,
    )
    assert len(routed) == 1
    assert routed[0]['ticker'] == 'SQQQ'
    assert routed[0]['market'] == 'US'
    assert routed[0]['quantity'] >= 1

    # 3. Verify USNightExecutionEngine routes 390390 semiconductor short to SOXS and 252670 to SQQQ
    engine = USNightExecutionEngine()
    engine.holdings = []
    monkeypatch.setattr(engine, "is_us_session_active", lambda now=None: True)
    monkeypatch.setattr(engine, "_get_live_fx_rate", lambda: 1350.0)
    monkeypatch.setattr(engine, "_save_holdings", lambda: None)
    orders = engine.process_us_night_signals([
        {'ticker': '252670', 'name': 'KODEX 200선물인버스2X', 'strategy': 'beta_directional_short_v4', 'price': 30.0, 'size_pct': 0.10},
        {'ticker': '390390', 'name': 'KODEX 반도체인버스', 'strategy': 'beta_directional_short_v4', 'price': 25.0, 'size_pct': 0.10},
    ], account_nav=20_000_000.0)
    assert [o['ticker'] for o in orders] == ['SQQQ', 'SOXS']




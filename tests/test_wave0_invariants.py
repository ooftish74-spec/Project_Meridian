"""
Wave 0 P0 invariant regression tests (2026-10-19).

Each test pins down a money-critical invariant that the 467-test suite did not cover
and that was violated in production code before Wave 0:

  E1  shadow portfolio load preserves positions / never silently recreates / never shrinks history
  E2  exit (sell) orders are never netted away by same-ticker buys
  E4  panic sells stay true market orders (no last-price limit conversion)
  E5  incomplete broker snapshot is flagged and never cached as truth; reconcile refuses mass ghost-delete
  E6  ambiguous submit failure (read timeout) → exactly one POST, status 'unknown' (no duplicate order)
  E7  cancel not confirmed after fill timeout → no market fallback order
  E8  no HWM auto-reset; partial NAV → halt new entries (not liquidation); consistent NAV → hard floor fires
  S3  NQ crash / missing NQ data blocks the overnight leveraged long
  S2/S5 final execution gate: exit-only drops buys; buys scaled to available cash per currency
  D2  FX: no constant fallback (None when no fresh real observation)
  D4  data bus cache works with Redis disconnected (no AttributeError / RuntimeError)
  O1  market phase never auto-liquidates on generic errors / timeouts / desync
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────
# E1 — Shadow portfolio loader / saver
# ─────────────────────────────────────────────────────────────
def _sample_portfolio(n_pos=6, n_trades=12):
    positions = {f"S3:{100000 + i}": {'stream_id': 'S3', 'ticker': f"{100000 + i}", 'quantity': 10 + i,
                                      'avg_price': 1000.0, 'entry_price': 1000.0, 'current_price': 1010.0,
                                      'amount': 10000.0, 'market_value': 10100.0} for i in range(n_pos)}
    trades = [{'action': 'BUY', 'ticker': f"{100000 + (i % n_pos)}", 'price': 1000.0, 'quantity': 1,
               'stream_id': 'S3', 'date': '2026-10-16'} for i in range(n_trades)]
    return {'virtual_nav': 18_000_000.0, 'cash': 1_000_000.0, 'hwm': 18_500_000.0, 'initial_capital': 18_000_000.0,
            'positions': positions, 'trade_history': trades, 'daily_snapshots': []}


@pytest.fixture
def shadow_mod(tmp_path, monkeypatch):
    import src.portfolio.shadow_manager as sm
    monkeypatch.setattr(sm, '_RESULTS', tmp_path)
    return sm


def test_e1_normal_load_preserves_positions_and_history(shadow_mod, tmp_path):
    (tmp_path / 'shadow_portfolio.json').write_text(json.dumps(_sample_portfolio()), encoding='utf-8')
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    assert len(mgr.data['positions']) == 6
    assert len(mgr.data['trade_history']) == 12
    assert mgr.data['hwm'] == 18_500_000.0
    # file on disk must not have been rewritten to a stub
    on_disk = json.loads((tmp_path / 'shadow_portfolio.json').read_text())
    assert len(on_disk['positions']) == 6


def test_e1_corrupt_without_backup_fails_closed(shadow_mod, tmp_path):
    (tmp_path / 'shadow_portfolio.json').write_text("{ truncated", encoding='utf-8')
    with pytest.raises(shadow_mod.ShadowPortfolioCorruptError):
        shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)


def test_e1_corrupt_with_backup_recovers(shadow_mod, tmp_path):
    (tmp_path / 'shadow_portfolio.json').write_text("{ truncated", encoding='utf-8')
    (tmp_path / 'shadow_portfolio.json.bak').write_text(json.dumps(_sample_portfolio()), encoding='utf-8')
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    assert len(mgr.data['positions']) == 6


def test_e1_save_refuses_history_shrink_and_stub(shadow_mod, tmp_path):
    (tmp_path / 'shadow_portfolio.json').write_text(json.dumps(_sample_portfolio()), encoding='utf-8')
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    mgr.data['trade_history'] = mgr.data['trade_history'][:3]
    assert mgr.save() is False
    mgr.data = {'cash': 1.0}
    assert mgr.save() is False
    # disk still intact, rejected states preserved for forensics
    assert len(json.loads((tmp_path / 'shadow_portfolio.json').read_text())['trade_history']) == 12
    assert len(list(tmp_path.glob('shadow_portfolio.rejected_*.json'))) == 2


def test_e1_save_keeps_previous_version_as_backup(shadow_mod, tmp_path):
    (tmp_path / 'shadow_portfolio.json').write_text(json.dumps(_sample_portfolio()), encoding='utf-8')
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    mgr.data['cash'] = 123.0
    mgr.save()
    bak = json.loads((tmp_path / 'shadow_portfolio.json.bak').read_text())
    assert bak['cash'] == 1_000_000.0          # previous version, not the same content
    assert json.loads((tmp_path / 'shadow_portfolio.json').read_text())['cash'] == 123.0


def test_e5_reconcile_refuses_mass_ghost_delete(shadow_mod, tmp_path):
    (tmp_path / 'shadow_portfolio.json').write_text(json.dumps(_sample_portfolio()), encoding='utf-8')
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    mgr.force_reconcile({}, live_cash=0.0)                       # empty snapshot
    assert len(mgr.data['positions']) == 6
    mgr.force_reconcile({'100000': 10}, live_cash=0.0)           # would delete 5/6 → suspicious
    assert len(mgr.data['positions']) == 6


# ─────────────────────────────────────────────────────────────
# E2 — Intraday capital router never cancels exit sells
# ─────────────────────────────────────────────────────────────
def test_e2_exit_sell_survives_same_ticker_buy():
    from src.execution.intraday_capital_router import IntradayCapitalRouter
    router = IntradayCapitalRouter()
    sell = {'ticker': '069500', 'side': 'sell', 'quantity': 51, 'reason': 'stop_loss', 'exit_type': 'stop'}
    buy = {'ticker': '069500', 'side': 'buy', 'amount_krw': 500_000, 'confidence': 0.5}
    out = router.resolve_signal_conflicts([buy, sell])
    sells = [o for o in out if str(o.get('side')).lower() == 'sell']
    buys = [o for o in out if str(o.get('side')).lower() == 'buy']
    assert len(sells) == 1 and sells[0]['quantity'] == 51
    assert buys == []


def test_d3_legacy_code_canonicalized_not_silent():
    from src.utils.ticker_canonical import canonical_ticker
    assert canonical_ticker('430740') == '423160'
    assert canonical_ticker('069500') == '069500'


# ─────────────────────────────────────────────────────────────
# KIS adapter fixtures (no network, no init side effects)
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def adapter(monkeypatch):
    from src.execution import _kis_adapter as ka
    a = ka.KISTraderAdapter.__new__(ka.KISTraderAdapter)
    a._lock = threading.RLock()
    a.mode = 'paper'
    a.orders = []
    a.positions = {}
    a.trade_history = []
    a._access_token = 'TEST'
    a._cb = None
    a._dlq = None
    a.base_url = 'https://example.invalid'
    a.account_no = '12345678-01'
    a.account = type('A', (), {'cash': 10_000_000.0, 'total_equity': 10_000_000.0})()
    a._cached_live_positions = {'069500': 51}
    a._last_positions_complete = True
    a._recently_canceled_odnos = set()
    counter = {'n': 0}

    def _gen():
        counter['n'] += 1
        return f"T{counter['n']:04d}"
    monkeypatch.setattr(a, '_gen_order_id', _gen, raising=False)
    monkeypatch.setattr(a, '_save_state', lambda: None, raising=False)
    monkeypatch.setattr(a, '_get_headers', lambda: {}, raising=False)
    monkeypatch.setattr(a, '_get_live_fx_rate', lambda: 1350.0, raising=False)
    return a


def test_e4_panic_sell_stays_market(adapter, monkeypatch):
    captured = {}
    monkeypatch.setattr(adapter, '_get_current_price', lambda t: 90000.0, raising=False)
    monkeypatch.setattr(adapter, 'get_live_overseas_price', lambda *a, **k: None, raising=False)
    monkeypatch.setattr(adapter, '_api_order', lambda o: captured.setdefault('o', o) or o, raising=False)
    import src.execution._kis_adapter as ka
    monkeypatch.setattr(ka, 'datetime', type('D', (), {'now': staticmethod(lambda: datetime(2026, 10, 19, 10, 0))}))
    adapter.sell('069500', 51, price=0, order_type='market', urgency='panic')
    assert captured['o'].order_type == 'market'
    assert 'urgency=panic' in captured['o'].notes
    # normal sell still uses the smart-limit conversion
    captured.clear()
    adapter.sell('069500', 1, price=0, order_type='market', urgency='normal')
    assert captured['o'].order_type == 'limit'


def _patch_api_order_env(monkeypatch):
    import src.utils.market_calendar as mc
    monkeypatch.setattr(mc, 'is_ticker_tradable_now', lambda t: (True, 'regular'), raising=False)


def test_e6_ambiguous_timeout_never_resends(adapter, monkeypatch):
    import requests
    from src.execution._kis_adapter import Order
    _patch_api_order_env(monkeypatch)
    monkeypatch.setattr(adapter, 'inquire_unexecuted', lambda t='': [], raising=False)
    calls = {'n': 0}

    def _post(*a, **k):
        calls['n'] += 1
        raise requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr(requests, 'post', _post)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='X1', ticker='069500', side='buy', quantity=1, price=90000.0, order_type='limit')
    res = adapter._api_order(o)
    assert calls['n'] == 1
    assert res.status == 'unknown'


def test_e6_connect_failure_is_retried(adapter, monkeypatch):
    import requests
    from src.execution._kis_adapter import Order
    _patch_api_order_env(monkeypatch)
    monkeypatch.setattr(adapter, 'inquire_unexecuted', lambda t='': [], raising=False)
    calls = {'n': 0}

    def _post(*a, **k):
        calls['n'] += 1
        raise requests.exceptions.ConnectTimeout("connect timed out")
    monkeypatch.setattr(requests, 'post', _post)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='X2', ticker='069500', side='buy', quantity=1, price=90000.0, order_type='limit')
    res = adapter._api_order(o)
    assert calls['n'] >= 2            # definitely-not-sent → retry is safe
    assert res.status == 'rejected'


def test_e7_unconfirmed_cancel_blocks_market_fallback(adapter, monkeypatch):
    from src.execution._kis_adapter import Order
    monkeypatch.setattr(adapter, 'check_order_status', lambda *a, **k: {'status': 'pending', 'filled_qty': 0}, raising=False)
    monkeypatch.setattr(adapter, 'cancel_order', lambda *a, **k: {'success': False, 'message': 'network error'}, raising=False)
    api_calls = {'n': 0}
    monkeypatch.setattr(adapter, '_api_order', lambda o: api_calls.__setitem__('n', api_calls['n'] + 1) or o, raising=False)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='OD1', ticker='069500', side='sell', quantity=10, price=90000.0, order_type='limit', status='submitted')
    res = adapter.wait_for_fill(o, timeout_sec=0, fallback='market')
    assert api_calls['n'] == 0
    assert res.status == 'unknown'


def test_e7_confirmed_cancel_allows_remaining_market(adapter, monkeypatch):
    from src.execution._kis_adapter import Order

    def _status(order_no, **k):
        if order_no == 'OD2':
            return {'status': 'canceled', 'filled_qty': 4}
        return {'status': 'filled', 'filled_qty': 6, 'filled_price': 89900.0}
    monkeypatch.setattr(adapter, 'check_order_status', _status, raising=False)
    monkeypatch.setattr(adapter, 'cancel_order', lambda *a, **k: {'success': True}, raising=False)
    sent = []

    def _api(o):
        sent.append(o.quantity)
        o.status = 'submitted'
        return o
    monkeypatch.setattr(adapter, '_api_order', _api, raising=False)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='OD2', ticker='069500', side='sell', quantity=10, price=90000.0, order_type='limit', status='submitted')
    res = adapter.wait_for_fill(o, timeout_sec=0, fallback='market')
    assert sent == [6]                       # only the confirmed remaining quantity
    assert res.status == 'filled_market_fallback'
    assert res.filled_quantity == 10


def test_e5_incomplete_snapshot_flagged_and_not_cached(adapter, monkeypatch):
    import requests

    class R:
        def __init__(self, d): self._d = d
        def json(self): return self._d

    def _get(url, **k):
        if 'domestic' in url:
            return R({'rt_cd': '0', 'output1': [{'pdno': '069500', 'hldg_qty': '51'}]})
        return R({'rt_cd': '1', 'msg_cd': 'EGW00201', 'msg1': 'rate limit'})
    monkeypatch.setattr(requests, 'get', _get)
    adapter.mode = 'live'
    adapter._cached_live_positions = {'069500': 51, 'TLT': 2}
    adapter._last_positions_fetch_ts = 0
    snap = adapter.fetch_live_positions()
    assert snap == {'069500': 51}
    assert adapter._last_positions_complete is False
    assert adapter._cached_live_positions == {'069500': 51, 'TLT': 2}   # truth cache untouched


# ─────────────────────────────────────────────────────────────
# E8 — Kill switch
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def ks(tmp_path, monkeypatch):
    import src.risk.kill_switch as k
    monkeypatch.setattr(k, '_RESULTS', tmp_path)
    obj = k.KillSwitch.__new__(k.KillSwitch)
    return k, obj, tmp_path


def _write_components(tmp_path, cash, evals, usd_cash=0.0, fx=1350.0):
    from src.utils.time_utils import now_kst
    d = {'timestamp': now_kst().replace(tzinfo=None).isoformat(),
         'account': {'cash': cash, 'us_cash_usd': usd_cash, 'fx_rate': fx,
                     'positions': {f"T{i}": {'eval_krw': v} for i, v in enumerate(evals)}}}
    (tmp_path / 'kis_portfolio.json').write_text(json.dumps(d), encoding='utf-8')


def test_e8_no_hwm_reset(ks):
    k, obj, tmp = ks
    m = obj.measure_metrics({'total_nav': 100.0, 'hwm': 200.0, 'initial_capital': 100.0, 'daily_returns': []})
    assert m['dd_pct'] == pytest.approx(-50.0)


def test_e8_partial_nav_halts_entries_not_liquidation(ks):
    k, obj, tmp = ks
    _write_components(tmp, cash=10.0, evals=[60.0, 30.0])          # components = 100
    m = obj.measure_metrics({'total_nav': 70.0, 'hwm': 100.0, 'initial_capital': 100.0, 'daily_returns': []})
    assert m['nav_consistent'] is False
    j = obj.judge_action(m, regime='caution')
    types = {t['type'] for t in j['triggers']}
    assert 'nav_unverified' in types and 'hard_floor_mdd' not in types and 'dd_critical' not in types
    assert j['action'] == 'halt_new_entry'


def test_e8_consistent_nav_drawdown_fires_hard_floor(ks):
    k, obj, tmp = ks
    _write_components(tmp, cash=1.0, evals=[69.0])                 # components = 70 == nav
    m = obj.measure_metrics({'total_nav': 70.0, 'hwm': 100.0, 'initial_capital': 100.0, 'daily_returns': []})
    assert m['nav_consistent'] is True
    j = obj.judge_action(m, regime='caution')
    types = {t['type'] for t in j['triggers']}
    assert 'hard_floor_mdd' in types
    assert j['action'] == 'halt_all'


def test_e8_crash_regime_does_not_relax_dd_kill(ks):
    k, obj, tmp = ks
    m = obj.measure_metrics({'total_nav': 70.0, 'hwm': 100.0, 'initial_capital': 100.0, 'daily_returns': []})
    j = obj.judge_action(m, regime='crash')
    assert 'dd_critical' in {t['type'] for t in j['triggers']}     # -30% ≤ -25% (no relaxation to -40%)


# ─────────────────────────────────────────────────────────────
# S3 — Overnight leveraged long blocked on NQ crash / missing data
# ─────────────────────────────────────────────────────────────
def _s5_signals(monkeypatch, nq_payload):
    import src.data.market_data_bridge as mdb
    monkeypatch.setattr(mdb.MarketDataBridge, 'get_nq_futures_change', lambda self, force=False: nq_payload, raising=False)
    from src.streams.s5_overnight.overnight_stream import S5OvernightStream
    s = S5OvernightStream()
    md = {'signal_cache': {'kospi_change_1d': -3.0, 'vix': 15.0}, 'backtest_mode': True}
    return S5OvernightStream, s.generate_signals('caution', md)


def test_s3_nq_crash_blocks_leveraged_long(monkeypatch):
    cls, sigs = _s5_signals(monkeypatch, {'chg_1d_pct': -3.0, 'source': 'macro_proxy_file'})
    overlay_longs = [x for x in sigs if x.get('strategy') == 'overnight_overlay' and x.get('ticker') == cls.lev_ticker]
    assert overlay_longs == []


def test_s3_missing_nq_data_fails_closed(monkeypatch):
    cls, sigs = _s5_signals(monkeypatch, {'chg_1d_pct': 0.0, 'source': 'fallback'})
    assert [x for x in sigs if x.get('strategy') == 'overnight_overlay'] == []


# ─────────────────────────────────────────────────────────────
# S2 / S5 — final execution gate
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def orch():
    from scripts.stream_orchestrator import StreamOrchestrator
    o = StreamOrchestrator.__new__(StreamOrchestrator)
    o._exit_only_reason = None
    o.execution_engine = type('E', (), {'_estimate_price': staticmethod(lambda t: 0.0)})()
    return o


def test_s2_exit_only_drops_all_buys(orch):
    orders = [{'ticker': '069500', 'action': 'buy', 'quantity': 5, 'price': 90000},
              {'ticker': '091160', 'action': 'sell', 'quantity': 10, 'price': 116000}]
    out = orch._apply_final_execution_gate(orders, {'cash': 1e9, 'cash_usd': 1e6}, {'exit_only': True})
    assert [o['action'] for o in out] == ['sell']


def test_s5_buys_scaled_to_available_cash(orch):
    orders = [{'ticker': '069500', 'action': 'buy', 'quantity': 10, 'price': 100_000},     # 1.0M
              {'ticker': '091160', 'action': 'buy', 'quantity': 10, 'price': 100_000},     # 1.0M
              {'ticker': 'TLT', 'action': 'buy', 'quantity': 10, 'price': 80.0}]           # $800
    out = orch._apply_final_execution_gate(orders, {'cash': 1_000_000.0, 'cash_usd': 400.0}, {})
    krw = sum(o['quantity'] * o['price'] for o in out if o['ticker'].isdigit())
    usd = sum(o['quantity'] * o['price'] for o in out if not o['ticker'].isdigit())
    assert krw <= 1_000_000.0
    assert usd <= 400.0


def test_s5_unknown_cash_does_not_silently_drop(orch):
    orders = [{'ticker': 'TLT', 'action': 'buy', 'quantity': 1, 'price': 80.0}]
    out = orch._apply_final_execution_gate(orders, {'cash': 1_000_000.0}, {})
    assert len(out) == 1


# ─────────────────────────────────────────────────────────────
# D2 — FX: no constant fallback
# ─────────────────────────────────────────────────────────────
def test_d2_fx_none_without_fresh_real_observation(tmp_path, monkeypatch):
    from src.data_collection.central_data_gateway import CentralDataGateway
    g = CentralDataGateway.__new__(CentralDataGateway)
    monkeypatch.setattr(CentralDataGateway, '_FX_LAST_GOOD', tmp_path / 'lg.json')
    monkeypatch.setattr(g, 'get_macro_indicator', lambda m: {'price': None, 'status': 'DATA_UNCOLLECTED'}, raising=False)
    assert g.get_usdkrw_rate() is None
    (tmp_path / 'lg.json').write_text(json.dumps({'rate': 1388.5, 'ts': time.time() - 3600}))
    assert g.get_usdkrw_rate() == 1388.5
    (tmp_path / 'lg.json').write_text(json.dumps({'rate': 1388.5, 'ts': time.time() - 30 * 86400}))
    assert g.get_usdkrw_rate() is None


# ─────────────────────────────────────────────────────────────
# D4 — data bus with Redis disconnected
# ─────────────────────────────────────────────────────────────
def test_d4_cache_without_redis():
    from src.data_collection.realtime_data_bus import StalenessAwareCache, DataSourceConfig
    c = StalenessAwareCache.__new__(StalenessAwareCache)
    c.source_name = 'test'
    c.config = DataSourceConfig(name='test', staleness_critical_sec=60, staleness_warn_sec=10)
    c._cache = {}
    c._lock = threading.RLock()
    c._redis = None
    assert c.get('X') is None
    c.set('X', {'price': 1.0})
    dp = c.get('X')
    assert dp is not None and dp.quality == 1.0


# ─────────────────────────────────────────────────────────────
# O1 — no automatic liquidation on generic failures
# ─────────────────────────────────────────────────────────────
def test_o1_pipeline_has_no_auto_liquidation_calls():
    src = (_ROOT / 'scripts' / 'daily_pipeline.py').read_text(encoding='utf-8')
    code_lines = [l for l in src.splitlines() if 'hard_liquidate_all(' in l and not l.strip().startswith('#')]
    assert code_lines == []
    assert '_engage_exit_only(' in src


# ─────────────────────────────────────────────────────────────
# E6 (round 2) — order ledger lookup after ambiguous submit
# ─────────────────────────────────────────────────────────────
class _R:
    def __init__(self, d): self._d = d
    def json(self): return self._d


def test_e6_ledger_match_tracks_order_without_resend(adapter, monkeypatch):
    import requests
    from src.execution._kis_adapter import Order
    _patch_api_order_env(monkeypatch)
    monkeypatch.setattr(adapter, 'inquire_unexecuted', lambda t='': [], raising=False)
    monkeypatch.setattr(adapter, '_find_broker_order_since', lambda o, s, attempts=2: {'odno': '0000777'}, raising=False)
    calls = {'n': 0}

    def _post(*a, **k):
        calls['n'] += 1
        raise requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr(requests, 'post', _post)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='X3', ticker='069500', side='sell', quantity=5, price=90000.0, order_type='limit')
    res = adapter._api_order(o)
    assert calls['n'] == 1
    assert res.status == 'submitted' and res.order_id == '0000777'


def test_e6_preexisting_resting_order_not_mistaken(adapter, monkeypatch):
    """제출 이전부터 걸려 있던 동일 종목·수량 미체결 주문을 방금 보낸 주문으로 오인하면 안 된다."""
    import requests
    from src.execution._kis_adapter import Order
    _patch_api_order_env(monkeypatch)
    _seq = iter([[], [{'pdno': '069500', 'sll_buy_dvsn_cd': '01', 'ord_qty': '5', 'ord_tmd': '000001', 'odno': '0000111'}]])
    monkeypatch.setattr(adapter, 'inquire_unexecuted', lambda t='': next(_seq, []), raising=False)
    monkeypatch.setattr(adapter, '_find_broker_order_since', lambda o, s, attempts=2: None, raising=False)
    monkeypatch.setattr(requests, 'post', lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("t")))
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='X4', ticker='069500', side='sell', quantity=5, price=90000.0, order_type='limit')
    res = adapter._api_order(o)
    assert res.status == 'unknown'
    assert res.order_id != '0000111'


def test_e6_find_broker_order_since_filters_rows(adapter, monkeypatch):
    import requests
    from src.execution._kis_adapter import Order
    adapter.mode = 'live'
    since = datetime.now().replace(microsecond=0)
    today = since.strftime('%Y%m%d')
    later = (since.replace(second=0) if since.second < 50 else since).strftime('%H%M%S')
    later = max(later, since.strftime('%H%M%S'))
    rows = [
        {'pdno': '069500', 'sll_buy_dvsn_cd': '01', 'ord_qty': '4', 'ord_dt': today, 'ord_tmd': later, 'odno': 'A'},     # qty mismatch
        {'pdno': '069500', 'sll_buy_dvsn_cd': '02', 'ord_qty': '5', 'ord_dt': today, 'ord_tmd': later, 'odno': 'B'},     # side mismatch
        {'pdno': '069500', 'sll_buy_dvsn_cd': '01', 'ord_qty': '5', 'ord_dt': today, 'ord_tmd': '000000', 'odno': 'C'},  # before submit
        {'pdno': '069500', 'sll_buy_dvsn_cd': '01', 'ord_qty': '5', 'ord_dt': today, 'ord_tmd': later, 'odno': 'D'},     # match
    ]
    monkeypatch.setattr(requests, 'get', lambda url, **k: _R({'rt_cd': '0', 'output1': rows}))
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    o = Order(order_id='X5', ticker='069500', side='sell', quantity=5, price=90000.0, order_type='limit')
    m = adapter._find_broker_order_since(o, since)
    assert m is not None and m['odno'] == 'D'
    monkeypatch.setattr(requests, 'get', lambda url, **k: _R({'rt_cd': '0', 'output1': rows[:3]}))
    assert adapter._find_broker_order_since(o, since) is None


# ─────────────────────────────────────────────────────────────
# Domestic order status parsing (fields verified against live KIS)
# ─────────────────────────────────────────────────────────────
def _status_with_rows(adapter, monkeypatch, rows, odno='0000123'):
    import requests
    adapter.mode = 'live'
    monkeypatch.setattr(requests, 'get', lambda url, **k: _R({'rt_cd': '0', 'output1': rows}))
    return adapter.check_order_status(odno, ticker='069500')


def test_domestic_status_rejected(adapter, monkeypatch):
    r = _status_with_rows(adapter, monkeypatch, [{'odno': '0000123', 'ord_qty': '10', 'tot_ccld_qty': '0', 'rmn_qty': '0', 'rjct_qty': '10', 'cncl_yn': 'N'}])
    assert r['status'] == 'rejected'


def test_domestic_status_dead_order_is_terminal(adapter, monkeypatch):
    r = _status_with_rows(adapter, monkeypatch, [{'odno': '0000123', 'ord_qty': '10', 'tot_ccld_qty': '0', 'rmn_qty': '0', 'rjct_qty': '0', 'cncl_yn': 'N'}])
    assert r['status'] == 'canceled'


def test_domestic_status_partial_and_filled(adapter, monkeypatch):
    r = _status_with_rows(adapter, monkeypatch, [{'odno': '0000123', 'ord_qty': '10', 'tot_ccld_qty': '4', 'rmn_qty': '6', 'cncl_yn': 'N'}])
    assert r['status'] == 'partial' and r['filled_qty'] == 4
    r = _status_with_rows(adapter, monkeypatch, [{'odno': '0000123', 'ord_qty': '10', 'tot_ccld_qty': '10', 'rmn_qty': '0', 'cncl_yn': 'N', 'avg_prvs': '90100'}])
    assert r['status'] == 'filled' and r['filled_price'] == 90100.0


def test_domestic_status_lone_foreign_row_not_attributed(adapter, monkeypatch):
    r = _status_with_rows(adapter, monkeypatch, [{'odno': '0000999', 'ord_qty': '10', 'tot_ccld_qty': '10', 'rmn_qty': '0'}])
    assert r['status'] == 'not_found'


# ─────────────────────────────────────────────────────────────
# O2 — US regular window judged in New York local time (DST-proof)
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize('month,day', [(7, 15), (12, 15)])   # EDT, EST
def test_o2_ny_window_dst_invariant(month, day):
    from zoneinfo import ZoneInfo
    from scripts.pipeline.sub_phases import _us_regular_window_open
    ny = ZoneInfo('America/New_York')
    assert _us_regular_window_open(datetime(2026, month, day, 9, 25, tzinfo=ny))
    assert _us_regular_window_open(datetime(2026, month, day, 15, 59, tzinfo=ny))
    assert not _us_regular_window_open(datetime(2026, month, day, 8, 25, tzinfo=ny))
    assert not _us_regular_window_open(datetime(2026, month, day, 16, 0, tzinfo=ny))
    assert not _us_regular_window_open(datetime(2026, month, day, 13, 30, tzinfo=ny), half_day=True)


# ─────────────────────────────────────────────────────────────
# D5 — orderbook parse + SpreadGuard semantics
# ─────────────────────────────────────────────────────────────
def test_d5_orderbook_parse():
    from src.data_collection.kis_data_collector import KISDataCollector
    c = KISDataCollector.__new__(KISDataCollector)
    c._base_url = 'https://example.invalid'
    payload = {'output1': {'askp1': '90100', 'bidp1': '90000', 'askp_rsqn1': '120', 'bidp_rsqn1': '300'}}
    c._call = lambda url, tr, params: payload
    ob = c.get_orderbook('069500')
    assert ob['best_ask'] == 90100 and ob['best_bid'] == 90000
    assert abs(ob['spread_pct'] - 100 / 90050) < 1e-12
    assert ob['ask_volumes'][0] == 120 and ob['bid_volumes'][0] == 300
    assert c.get_orderbook('TLT') is None
    c._call = lambda url, tr, params: {'output1': {'askp1': '0', 'bidp1': '0'}}
    assert c.get_orderbook('069500') is None


def test_d5_extract_spread_schemas():
    from src.execution.execution_engine import _extract_spread_pct
    assert _extract_spread_pct({'spread_pct': 0.004}) == 0.004
    assert abs(_extract_spread_pct({'best_ask': 101, 'best_bid': 99}) - 0.02) < 1e-12
    assert abs(_extract_spread_pct({'ask1': 101, 'bid1': 99}) - 0.02) < 1e-12
    assert abs(_extract_spread_pct({'asks': [{'price': 101}], 'bids': [{'price': 99}]}) - 0.02) < 1e-12
    assert _extract_spread_pct({}) is None
    assert _extract_spread_pct(None) is None
    assert _extract_spread_pct({'best_ask': 99, 'best_bid': 101}) is None   # crossed book → unknown


def test_d5_spread_guard_never_blocks_exit(monkeypatch):
    import src.execution.execution_engine as ee
    import src.data_collection.realtime_data_bus as rdb

    class _Bus:
        def get_orderbook(self, t):
            return type('DP', (), {'value': {'spread_pct': 0.05}})()
    monkeypatch.setattr(rdb.RealtimeDataBus, 'get_instance', classmethod(lambda cls: _Bus()))
    order = {'tp_pct': 0.015}
    blocked, sp, lim = ee._s1_spread_guard(order, '069500', 'buy')
    assert blocked and sp == 0.05 and abs(lim - 0.005) < 1e-12
    assert ee._s1_spread_guard(order, '069500', 'sell')[0] is False


# ─────────────────────────────────────────────────────────────
# Engine — action normalization + sell-first ordering
# ─────────────────────────────────────────────────────────────
def test_engine_action_normalization():
    from src.execution.execution_engine import _normalize_order_actions
    orders = [{'ticker': 'A', 'action': 'BUY'}, {'ticker': 'B', 'action': 'SELL'},
              {'ticker': 'C', 'action': 'HOLD'}, {'ticker': 'D'}, {'ticker': 'E', 'side': 'sell'}]
    trade, rej = _normalize_order_actions(orders)
    assert [(o['ticker'], o['action']) for o in trade] == [('A', 'buy'), ('B', 'sell'), ('E', 'sell')]
    assert {o['ticker'] for o in rej} == {'C', 'D'}


def _fake_engine(monkeypatch, log):
    import src.execution.execution_engine as ee

    class _Fill:
        def __init__(self, side, t, q):
            self.status = 'filled'; self.filled_quantity = q; self.filled_price = 100.0
            self.commission = 0.0; self.slippage = 0.0; self.fill_timestamp = 't'; self.exchange = 'KRX'

    class _Trader:
        def buy(self, t, q, *a):
            log.append(('buy', t)); return _Fill('buy', t, q)

        def sell(self, t, q, *a):
            time.sleep(0.05)   # slow sells — buys must still wait for them
            log.append(('sell', t)); return _Fill('sell', t, q)

    eng = ee.ExecutionEngine.__new__(ee.ExecutionEngine)
    eng.mode = 'live'
    eng._mode = 'live'
    monkeypatch.setattr(eng, '_get_trader', lambda s=None: _Trader(), raising=False)
    monkeypatch.setattr(eng, '_calculate_alpha_decay_slippage_limit',
                        lambda o: {'max_allowed_slippage': 1.0, 'expected_alpha': 1.0, 'fee_rate': 0.0}, raising=False)
    monkeypatch.setattr(eng, '_estimate_price', lambda t: 100.0, raising=False)
    return eng, ee


def test_engine_sells_complete_before_buys(monkeypatch):
    log = []
    eng, ee = _fake_engine(monkeypatch, log)
    orders = [{'ticker': '000001', 'action': 'buy', 'quantity': 1, 'price': 100, 'stream': 'S3'},
              {'ticker': '000002', 'action': 'sell', 'quantity': 1, 'price': 100, 'stream': 'S3'},
              {'ticker': '000003', 'action': 'buy', 'quantity': 1, 'price': 100, 'stream': 'S3'},
              {'ticker': '000004', 'action': 'sell', 'quantity': 1, 'price': 100, 'stream': 'S3'}]
    res = eng._execute_live(orders, ee.ExecutionResult(mode='live', n_orders=4))
    sides = [s for s, _ in log]
    assert sides == ['sell', 'sell', 'buy', 'buy']
    assert res.n_filled == 4


def test_engine_uppercase_buy_is_not_sold(monkeypatch):
    log = []
    eng, ee = _fake_engine(monkeypatch, log)
    import src.execution.intraday_capital_router as icr
    monkeypatch.setattr(icr.IntradayCapitalRouter, 'resolve_signal_conflicts', lambda self, o: o, raising=False)
    monkeypatch.setattr(eng, '_save_shadow_record', lambda *a, **k: None, raising=False)
    res = eng.execute([{'ticker': '000005', 'action': 'BUY', 'quantity': 1, 'price': 100, 'stream': 'S3'},
                       {'ticker': '000006', 'action': 'HOLD', 'quantity': 1, 'price': 100, 'stream': 'S3'}])
    assert log == [('buy', '000005')]
    assert res.n_rejected == 1


# ─────────────────────────────────────────────────────────────
# A3 — extreme detector tail direction
# ─────────────────────────────────────────────────────────────
def test_a3_low_tail_is_not_upper_extreme():
    from src.utils.adaptive_thresholds import VolatilityScaledThreshold as V
    import numpy as np
    rng = np.random.default_rng(0)
    hist = list(20 + rng.normal(0, 2, 250))
    assert V.is_extreme(30.0, hist, z_score_limit=2.0, percentile_limit=95.0)          # VKOSPI spike → crash
    assert not V.is_extreme(10.0, hist, z_score_limit=2.0, percentile_limit=95.0)      # VKOSPI collapse ≠ crash
    assert V.is_extreme(10.0, hist, z_score_limit=2.0, percentile_limit=95.0, tail='lower')
    assert V.is_extreme(-10.0, [-h for h in hist], z_score_limit=2.0, percentile_limit=95.0)  # negation idiom
    assert not V.is_extreme(float('nan'), hist)
    assert not V.is_extreme(25.0, hist[:5])


# ─────────────────────────────────────────────────────────────
# F — broker fill import (pure functions)
# ─────────────────────────────────────────────────────────────
def test_f_fill_import_normalize_merge_fifo():
    import importlib.util
    spec = importlib.util.spec_from_file_location('ibf', _ROOT / 'scripts' / 'ops' / 'import_broker_fills.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    kr = [
        {'odno': '1', 'pdno': '069500', 'sll_buy_dvsn_cd': '02', 'ord_dt': '20261001', 'ord_tmd': '090501', 'tot_ccld_qty': '30', 'avg_prvs': '88000'},
        {'odno': '2', 'pdno': '069500', 'sll_buy_dvsn_cd': '02', 'ord_dt': '20261010', 'ord_tmd': '091000', 'tot_ccld_qty': '31', 'avg_prvs': '90000'},
        {'odno': '3', 'pdno': '069500', 'sll_buy_dvsn_cd': '01', 'ord_dt': '20261012', 'ord_tmd': '100000', 'tot_ccld_qty': '10', 'avg_prvs': '91000'},
        {'odno': '4', 'pdno': '069500', 'sll_buy_dvsn_cd': '02', 'ord_dt': '20261013', 'ord_tmd': '100000', 'tot_ccld_qty': '0', 'avg_prvs': '0'},  # unfilled
    ]
    us = [{'odno': '9', 'pdno': 'TLT', 'sll_buy_dvsn_cd': '02', 'dmst_ord_dt': '20261016', 'thco_ord_tmd': '233000', 'ft_ccld_qty': '2'}]  # no price
    fills = m.normalize_fill_rows(kr, 'KR') + m.normalize_fill_rows(us, 'US')
    assert len(fills) == 4
    assert fills[0]['action'] == 'BUY' and fills[0]['amount'] == 30 * 88000
    tlt = [f for f in fills if f['ticker'] == 'TLT'][0]
    assert tlt['price'] is None and tlt.get('price_missing')
    # FIFO: buy 30 (10-01) + 31 (10-10) − sell 10 → 20 left from 10-01 lot + 31 → 51 = broker
    assert m.fifo_entry_dates(fills, {'069500': 51})['069500'] == '2026-10-01'
    assert '069500' not in m.fifo_entry_dates(fills, {'069500': 60})       # incomplete window → no override
    existing = [{'odno': '1', 'market': 'KR', 'date': '2026-10-01', 'timestamp': '2026-10-01T09:05:01', 'keep': True}]
    merged, n = m.merge_history(existing, fills)
    assert n == 3 and merged[0].get('keep') is True
    merged2, n2 = m.merge_history(merged, fills)                           # idempotent
    assert n2 == 0 and len(merged2) == len(merged)


# ─────────────────────────────────────────────────────────────
# Price-service token: fail fast + process cooldown (pipeline hang root cause)
# ─────────────────────────────────────────────────────────────
def test_price_service_token_fail_fast(monkeypatch):
    import requests
    from src.execution.kis_price_service import KISPriceService
    svc = KISPriceService.__new__(KISPriceService)
    svc._base_url, svc._token_endpoint, svc._app_key, svc._app_secret = 'https://x.invalid', '/oauth2/tokenP', 'k', 's'
    calls, slept = {'n': 0}, []

    def _post(*a, **k):
        calls['n'] += 1
        raise requests.exceptions.ConnectionError('down')
    monkeypatch.setattr(requests, 'post', _post)
    monkeypatch.setattr(time, 'sleep', lambda s: slept.append(s))
    monkeypatch.setattr(KISPriceService, '_token_fail_until', 0.0, raising=False)
    assert svc._request_new_token() is False
    assert calls['n'] == 2 and sum(slept) <= 2          # was 65+130s per call
    assert svc._request_new_token() is False            # cooldown → no network, no wait
    assert calls['n'] == 2

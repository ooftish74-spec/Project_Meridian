"""
Wave 1 Track C — execution FX & broker invariants (2026-10-19).

  FX1  no literal USD/KRW fallback is reachable: all sources failing → 0.0 / None path
  FX2  last-good FX honoured only within fx.max_stale_hours
  FX3  _api_order with FX unknown: US BUY rejected (no POST), US SELL proceeds, KRX unaffected
  FX4  valuation sites (account NAV, buying power, VaR, entry monitor, exit engine) never fabricate a rate
  PG1  KIS balance pagination (tr_cont M/F + CTX_AREA_*): 2-3 pages merged
  PG2  mid-page failure → snapshot marked incomplete, never cached / never overwrites positions
  PG3  overseas holdings: single query (no exchange iteration → no double counting)
"""
from __future__ import annotations

import io
import json
import threading
import time
import tokenize
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, d, headers=None, status_code=200):
        self._d = d
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self._d


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
    a.account = type('A', (), {'cash': 10_000_000.0, 'total_equity': 10_000_000.0, 'positions_value': 0.0,
                               'unrealized_pnl': 0.0})()
    a._cached_live_positions = {}
    a._last_positions_complete = True
    a._recently_canceled_odnos = set()
    monkeypatch.setattr(a, '_gen_order_id', lambda: 'T0001', raising=False)
    monkeypatch.setattr(a, '_save_state', lambda: None, raising=False)
    monkeypatch.setattr(a, '_get_headers', lambda: {}, raising=False)
    monkeypatch.setattr(ka.KISTraderAdapter, '_fx_cache_val', 0.0, raising=False)
    monkeypatch.setattr(ka.KISTraderAdapter, '_fx_cache_ts', 0.0, raising=False)
    monkeypatch.setattr(time, 'sleep', lambda s: None)
    return a


@pytest.fixture
def all_fx_sources_down(monkeypatch, tmp_path):
    """KIS / Naver / BOK all fail, no last-good file → gateway must return None."""
    import src.data_collection.central_data_gateway as cdg
    import src.data_collection.kis_data_collector as kdc
    import src.data_collection.macro_realtime_refresher as mrr

    class _DeadKIS:
        def __init__(self, *a, **k):
            pass

        def get_usdkrw_exchange_rate(self):
            return None

    monkeypatch.setattr(kdc, 'KISDataCollector', _DeadKIS)
    monkeypatch.setattr(mrr.MacroRealtimeRefresher, '_fetch_usdkrw_naver', staticmethod(lambda: None))
    monkeypatch.setattr(mrr.MacroRealtimeRefresher, '_fetch_usdkrw_bok', staticmethod(lambda: None))
    gw = cdg.CentralDataGateway.__new__(cdg.CentralDataGateway)
    gw._memory_cache = {}
    gw._cache_ttl_sec = 5.0
    gw._FX_LAST_GOOD = tmp_path / 'usdkrw_last_good.json'
    monkeypatch.setattr(cdg, '_gateway_instance', gw)
    # a stale signal_cache value must NOT be picked up any more (old unbounded fallback)
    return gw


# ─────────────────────────────────────────────────────────────
# FX1 / FX2 — source resolution
# ─────────────────────────────────────────────────────────────
def test_fx_all_sources_down_returns_zero_not_literal(adapter, all_fx_sources_down):
    assert adapter._get_live_fx_rate() == 0.0
    assert adapter._get_dynamic_usdkrw_rate() == 0.0


def test_fx_last_good_respects_max_stale(adapter, all_fx_sources_down):
    gw = all_fx_sources_down
    gw._FX_LAST_GOOD.write_text(json.dumps({'rate': 1333.3, 'ts': time.time() - 3600}), encoding='utf-8')
    assert adapter._get_live_fx_rate() == pytest.approx(1333.3)       # 1h old → usable (q<1)

    from src.execution._kis_adapter import KISTraderAdapter
    KISTraderAdapter._fx_cache_val = 0.0
    gw._cached_usdkrw = None
    gw._FX_LAST_GOOD.write_text(json.dumps({'rate': 1333.3, 'ts': time.time() - 100 * 3600}), encoding='utf-8')
    assert adapter._get_live_fx_rate() == 0.0                         # 100h > 72h → refused


def test_no_fx_literal_constants_in_track_c_files():
    """Static guard: no numeric USD/KRW-looking constant (1200~1500) left in code (comments ignored)."""
    files = ['src/execution/_kis_adapter.py', 'src/execution/realtime_entry_monitor.py',
             'src/risk/universal_exit_engine.py', 'src/risk/realtime_var.py', 'scripts/stream_orchestrator.py']
    offenders = []
    for rel in files:
        src = (_ROOT / rel).read_text(encoding='utf-8')
        line_range = None
        if rel.endswith('stream_orchestrator.py'):
            # Track C owns only the portfolio-load FX region (_fx_orch)
            lines = src.splitlines()
            anchor = next(i for i, ln in enumerate(lines, 1) if '_fx_orch_tried = False' in ln)
            line_range = (anchor - 10, anchor + 50)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NUMBER:
                if line_range and not (line_range[0] <= tok.start[0] <= line_range[1]):
                    continue
                try:
                    v = float(tok.string)
                except ValueError:
                    continue
                if 1200.0 <= v <= 1500.0 and '.' in tok.string:
                    offenders.append(f"{rel}:{tok.start[0]} {tok.string}")
    assert offenders == []


# ─────────────────────────────────────────────────────────────
# FX3 — order firewall
# ─────────────────────────────────────────────────────────────
def _order_env(adapter, monkeypatch, fx=0.0):
    import requests
    import src.utils.market_calendar as mc
    monkeypatch.setattr(mc, 'is_ticker_tradable_now', lambda t: (True, 'regular'), raising=False)
    monkeypatch.setattr(adapter, 'inquire_unexecuted', lambda t='': [], raising=False)
    fx_calls = {'n': 0}

    def _fx():
        fx_calls['n'] += 1
        return fx
    monkeypatch.setattr(adapter, '_get_live_fx_rate', _fx, raising=False)
    posts = []

    def _post(url, headers=None, json=None, timeout=None):
        posts.append((url, dict(json or {})))
        return _Resp({'rt_cd': '0', 'output': {'ODNO': '0000999'}, 'msg1': 'ok'})
    monkeypatch.setattr(requests, 'post', _post)
    return posts, fx_calls


def test_fx_unknown_us_buy_rejected(adapter, monkeypatch):
    from src.execution._kis_adapter import Order
    posts, _ = _order_env(adapter, monkeypatch, fx=0.0)
    o = Order(order_id='B1', ticker='AAPL', side='buy', quantity=1, price=200.0, order_type='limit')
    res = adapter._api_order(o)
    assert res.status == 'rejected'
    assert 'FX_UNKNOWN' in res.notes
    assert posts == []


def test_fx_unknown_us_sell_proceeds(adapter, monkeypatch):
    from src.execution._kis_adapter import Order
    posts, _ = _order_env(adapter, monkeypatch, fx=0.0)
    adapter._cached_live_positions = {'AAPL': 3}
    o = Order(order_id='S1', ticker='AAPL', side='sell', quantity=2, price=200.0, order_type='limit')
    res = adapter._api_order(o)
    assert res.status == 'submitted'
    assert len(posts) == 1 and 'overseas-stock' in posts[0][0]
    assert posts[0][1]['ORD_QTY'] == '2'


def test_fx_unknown_krx_unaffected(adapter, monkeypatch):
    from src.execution._kis_adapter import Order
    posts, fx_calls = _order_env(adapter, monkeypatch, fx=0.0)
    o = Order(order_id='K1', ticker='069500', side='buy', quantity=1, price=90000.0, order_type='limit')
    res = adapter._api_order(o)
    assert res.status == 'submitted'
    assert len(posts) == 1 and 'domestic-stock' in posts[0][0]
    assert fx_calls['n'] == 0


def test_fx_known_us_buy_still_works(adapter, monkeypatch):
    from src.execution._kis_adapter import Order
    posts, _ = _order_env(adapter, monkeypatch, fx=1334.3)
    o = Order(order_id='B2', ticker='AAPL', side='buy', quantity=1, price=200.0, order_type='limit')
    res = adapter._api_order(o)
    assert res.status == 'submitted' and len(posts) == 1


# ─────────────────────────────────────────────────────────────
# FX4 — valuation sites
# ─────────────────────────────────────────────────────────────
def test_update_account_fx_unknown_keeps_nav(adapter, monkeypatch):
    from src.execution._kis_adapter import Position
    monkeypatch.setattr(adapter, '_get_live_fx_rate', lambda: 0.0, raising=False)
    adapter.positions = {'TLT': Position(ticker='TLT', quantity=2, avg_price=80.0, current_price=81.0)}
    adapter.account.total_equity = 12_345_678.0
    adapter._update_account()
    assert adapter.account.total_equity == 12_345_678.0
    assert adapter.nav_fx_degraded is True
    # KRX-only book: FX irrelevant, NAV recomputed
    adapter.positions = {'069500': Position(ticker='069500', quantity=10, avg_price=90000.0, current_price=91000.0)}
    adapter._update_account()
    assert adapter.account.total_equity == pytest.approx(10_000_000.0 + 910_000.0)
    assert adapter.nav_fx_degraded is False


def test_buying_power_fx_unknown_no_cross_conversion(adapter, monkeypatch):
    adapter.mode = 'mock'
    adapter.us_cash_usd = 100.0
    monkeypatch.setattr(adapter, '_get_live_fx_rate', lambda: 0.0, raising=False)
    bp = adapter.get_integrated_margin_purchase_power()
    assert bp['fx_degraded'] is True
    assert bp['total_buying_power_krw'] == pytest.approx(10_000_000.0)
    assert bp['total_buying_power_usd'] == pytest.approx(100.0)


def test_entry_monitor_fx_unknown_skips_us_only(all_fx_sources_down):
    from src.execution.realtime_entry_monitor import RealtimeEntryMonitor
    m = RealtimeEntryMonitor.__new__(RealtimeEntryMonitor)
    assert m.check_liquidity_and_spread('AAPL', trading_value=5e9) is False      # threshold needs FX → skip
    assert m.check_liquidity_and_spread('AAPL', trading_value=0.0) is True       # threshold not applied (unchanged)
    assert m.check_liquidity_and_spread('069500', trading_value=5e12) is True


def test_exit_engine_fx_unknown_still_exits_us():
    from src.risk.universal_exit_engine import UniversalExitEngine
    uee = UniversalExitEngine()
    positions = {'S3:AAPL': {'ticker': 'AAPL', 'stream_id': 'S3', 'quantity': 2, 'entry_price': 100.0,
                            'current_price': 60.0, 'holding_minutes': 50000.0}}
    res = uee.evaluate_all_positions(positions, market_data={'vix': 18.0, 'signal_cache': {'vix': 18.0}},
                                     regime='caution')
    sells = [e for e in res.get('exit_orders', []) if e.get('ticker') == 'AAPL' and e.get('action') == 'sell']
    assert sells, res


def test_var_fx_unknown_uses_eval_krw_or_excludes(monkeypatch, tmp_path, all_fx_sources_down):
    import src.risk.realtime_var as rv
    monkeypatch.setattr(rv, '_RESULTS', tmp_path)
    (tmp_path / 'kis_portfolio.json').write_text(json.dumps({'holdings': {
        '069500': {'qty': 10, 'current_price': 90000.0, 'currency': 'KRW'},
        'TLT': {'qty': 2, 'current_price': 80.0, 'currency': 'USD', 'eval_krw': 213_000},
        'XLV': {'qty': 2, 'current_price': 170.0, 'currency': 'USD'},
    }}), encoding='utf-8')
    v = rv.RealtimeVaR()
    v._fx_degraded = False
    pos = v._load_positions()
    assert set(pos) == {'069500', 'TLT'}
    assert pos['TLT']['current_value'] == pytest.approx(213_000)
    assert v._fx_degraded is True


# ─────────────────────────────────────────────────────────────
# PG1 / PG2 — fetch_live_positions pagination
# ─────────────────────────────────────────────────────────────
def _paged_positions_get(calls, kr_pages, us_pages):
    def _get(url, headers=None, params=None, timeout=None):
        calls.append((url, dict(headers or {}), dict(params or {})))
        pages = kr_pages if 'domestic' in url else us_pages
        sfx = '100' if 'domestic' in url else '200'
        key = params.get(f'CTX_AREA_NK{sfx}', '') or ''
        idx = 0 if not key else int(key.replace('NK', ''))
        page = pages[idx]
        if isinstance(page, Exception):
            raise page
        body, tr_cont = page
        body = dict(body)
        if tr_cont in ('M', 'F'):
            body[f'ctx_area_fk{sfx}'] = f'FK{idx + 1}'
            body[f'ctx_area_nk{sfx}'] = f'NK{idx + 1}'
        return _Resp(body, headers={'tr_cont': tr_cont})
    return _get


def test_positions_pagination_merges_pages(adapter, monkeypatch):
    import requests
    adapter.mode = 'live'
    adapter._last_positions_fetch_ts = 0
    kr = [({'rt_cd': '0', 'output1': [{'pdno': '069500', 'hldg_qty': '51'}]}, 'M'),
          ({'rt_cd': '0', 'output1': [{'pdno': '091160', 'hldg_qty': '10'}]}, 'M'),
          ({'rt_cd': '0', 'output1': [{'pdno': '305720', 'hldg_qty': '334'}]}, 'D')]
    us = [({'rt_cd': '0', 'output1': [{'ovrs_pdno': 'TLT', 'ovrs_cblc_qty': '2'}]}, 'F'),
          ({'rt_cd': '0', 'output1': [{'ovrs_pdno': 'XLV', 'ovrs_cblc_qty': '2'}]}, 'E')]
    calls = []
    monkeypatch.setattr(requests, 'get', _paged_positions_get(calls, kr, us))
    snap = adapter.fetch_live_positions()
    assert snap == {'069500': 51, '091160': 10, '305720': 334, 'TLT': 2, 'XLV': 2}
    assert adapter._last_positions_complete is True
    assert adapter._cached_live_positions == snap
    dom = [c for c in calls if 'domestic' in c[0]]
    assert len(dom) == 3
    assert dom[0][1].get('tr_cont', '') == ''                     # first page: no continuation header
    assert dom[1][1]['tr_cont'] == 'N' and dom[1][2]['CTX_AREA_FK100'] == 'FK1'
    assert dom[2][2]['CTX_AREA_NK100'] == 'NK2'
    ovs = [c for c in calls if 'overseas' in c[0]]
    assert len(ovs) == 2 and ovs[1][2]['CTX_AREA_NK200'] == 'NK1'


def test_positions_mid_page_failure_incomplete(adapter, monkeypatch):
    import requests
    adapter.mode = 'live'
    adapter._last_positions_fetch_ts = 0
    adapter._cached_live_positions = {'069500': 51, '091160': 10, 'TLT': 2}
    kr = [({'rt_cd': '0', 'output1': [{'pdno': '069500', 'hldg_qty': '51'}]}, 'M'),
          ({'rt_cd': '1', 'msg_cd': 'EGW00201', 'msg1': 'rate limit'}, 'D')]
    us = [({'rt_cd': '0', 'output1': [{'ovrs_pdno': 'TLT', 'ovrs_cblc_qty': '2'}]}, 'D')]
    monkeypatch.setattr(requests, 'get', _paged_positions_get([], kr, us))
    snap = adapter.fetch_live_positions()
    assert adapter._last_positions_complete is False
    assert adapter._cached_live_positions == {'069500': 51, '091160': 10, 'TLT': 2}   # truth cache untouched
    assert '091160' not in snap


def test_positions_us_mid_page_exception_incomplete(adapter, monkeypatch):
    import requests
    adapter.mode = 'live'
    adapter._last_positions_fetch_ts = 0
    kr = [({'rt_cd': '0', 'output1': [{'pdno': '069500', 'hldg_qty': '51'}]}, 'D')]
    us = [({'rt_cd': '0', 'output1': [{'ovrs_pdno': 'TLT', 'ovrs_cblc_qty': '2'}]}, 'M'),
          requests.exceptions.ReadTimeout('timeout')]
    monkeypatch.setattr(requests, 'get', _paged_positions_get([], kr, us))
    adapter.fetch_live_positions()
    assert adapter._last_positions_complete is False


def test_paging_max_page_cap(adapter, monkeypatch):
    import requests
    calls = {'n': 0}

    def _get(url, headers=None, params=None, timeout=None):
        calls['n'] += 1
        return _Resp({'rt_cd': '0', 'output1': [{'pdno': f"{calls['n']:06d}", 'hldg_qty': '1'}],
                      'ctx_area_fk100': 'x', 'ctx_area_nk100': 'y'}, headers={'tr_cont': 'M'})
    monkeypatch.setattr(requests, 'get', _get)
    monkeypatch.setattr(adapter, '_BALANCE_MAX_PAGES_DEFAULT', 3, raising=False)
    import config.dynamic_config as dc
    monkeypatch.setattr(dc.DynamicConfig, 'get', lambda self, k, d=None: d, raising=False)
    merged, complete = adapter._kis_paged_get('https://x/inquire-balance', {}, {}, '100')
    assert complete is False and calls['n'] == 3 and len(merged['output1']) == 3


# ─────────────────────────────────────────────────────────────
# PG1/PG2/PG3 + FX4 — fetch_live_balance
# ─────────────────────────────────────────────────────────────
def _balance_env(adapter, monkeypatch, us_pages, ctrp_fx='1334.2', fx=1334.3):
    import requests
    import src.execution.position_ledger as pl
    import src.execution.account_reconciler as ar

    class _Ledger:
        def load_ledger(self):
            return {}

        def sync_live_positions(self, tickers):
            return {}
    monkeypatch.setattr(pl, 'PositionLedgerManager', _Ledger)
    rec = {'n': 0}

    class _Rec:
        def reconcile(self, a):
            rec['n'] += 1
    monkeypatch.setattr(ar, 'AccountReconciler', _Rec)
    monkeypatch.setattr(adapter, '_get_live_fx_rate', lambda: fx, raising=False)
    adapter.mode = 'live'
    kr = [({'rt_cd': '0', 'output1': [{'pdno': '069500', 'hldg_qty': '51', 'prdt_name': 'KODEX200', 'prpr': '90000', 'pchs_avg_pric': '88000'}],
            'output2': [{'prvs_rcdl_excc_amt': '1000000', 'tot_evlu_amt': '5590000'}]}, 'M'),
          ({'rt_cd': '0', 'output1': [{'pdno': '091160', 'hldg_qty': '10', 'prdt_name': 'SEMI', 'prpr': '50000', 'pchs_avg_pric': '48000'}],
            'output2': [{'prvs_rcdl_excc_amt': '1000000', 'tot_evlu_amt': '5590000'}]}, 'D')]
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        headers = dict(headers or {})
        params = dict(params or {})
        calls.append((url, headers, params))
        tr = headers.get('tr_id', '')
        if 'inquire-psbl-order' in url:
            return _Resp({'rt_cd': '0', 'output': {'nrcvb_buy_amt': '1000000'}})
        if 'inquire-psamount' in url:
            return _Resp({'rt_cd': '0', 'output': {'ord_psbl_frcr_amt': '100', 'ovrs_ord_psbl_amt': '100', 'frcr_ord_psbl_amt1': '100'}})
        if tr.endswith('RP6504R'):
            row = {'crcy_cd': 'USD', 'frcr_dncl_amt_2': '100', 'frcr_drwg_psbl_amt_1': '100', 'frcr_buy_mgn_amt': '0'}
            if ctrp_fx:
                row['frst_bltn_exrt'] = ctrp_fx
            return _Resp({'rt_cd': '0', 'output2': [row]})
        pages, sfx = (kr, '100') if 'domestic' in url else (us_pages, '200')
        key = params.get(f'CTX_AREA_NK{sfx}', '') or ''
        idx = 0 if not key else int(key.replace('NK', ''))
        page = pages[idx]
        if isinstance(page, Exception):
            raise page
        body, tr_cont = page
        body = dict(body)
        if tr_cont in ('M', 'F'):
            body[f'ctx_area_fk{sfx}'] = f'FK{idx + 1}'
            body[f'ctx_area_nk{sfx}'] = f'NK{idx + 1}'
        return _Resp(body, headers={'tr_cont': tr_cont})
    monkeypatch.setattr(requests, 'get', _get)
    return calls, rec


_US_P1 = ({'rt_cd': '0', 'output1': [{'ovrs_pdno': 'TLT', 'ovrs_cblc_qty': '2', 'now_pric2': '80', 'pchs_avg_pric': '79', 'ovrs_item_name': 'TLT'}],
           'output2': {}}, 'F')
_US_P2 = ({'rt_cd': '0', 'output1': [{'ovrs_pdno': 'XLV', 'ovrs_cblc_qty': '2', 'now_pric2': '176', 'pchs_avg_pric': '178', 'ovrs_item_name': 'XLV'}],
           'output2': {}}, 'E')


def test_balance_pagination_merges_and_single_overseas_query(adapter, monkeypatch):
    calls, rec = _balance_env(adapter, monkeypatch, [_US_P1, _US_P2])
    assert adapter.fetch_live_balance() is True
    assert set(adapter.positions) == {'069500', '091160', 'TLT', 'XLV'}
    ovs = [c for c in calls if 'overseas-stock/v1/trading/inquire-balance' in c[0]]
    assert len(ovs) == 2                                            # 2 pages, ONE exchange query
    assert {c[2]['OVRS_EXCG_CD'] for c in ovs} == {'NASD'}
    assert ovs[1][1]['tr_cont'] == 'N' and ovs[1][2]['CTX_AREA_FK200'] == 'FK1'
    # NAV = KR tot_evlu + (US stock 2*80 + 2*176 + USD cash 100) * CTRP bulletin rate 1334.2
    assert adapter.account.total_equity == pytest.approx(5_590_000 + (160 + 352 + 100) * 1334.2)
    assert rec['n'] == 1
    assert adapter.nav_fx_degraded is False


def test_balance_us_mid_page_failure_restores_positions(adapter, monkeypatch):
    from src.execution._kis_adapter import Position
    import requests
    prev = {'069500': Position(ticker='069500', quantity=51, avg_price=88000.0, current_price=90000.0),
            'TLT': Position(ticker='TLT', quantity=2, avg_price=79.0, current_price=80.0),
            'XLV': Position(ticker='XLV', quantity=2, avg_price=178.0, current_price=176.0)}
    adapter.positions = dict(prev)
    adapter.account.total_equity = 7_000_000.0
    _balance_env(adapter, monkeypatch, [_US_P1, requests.exceptions.ReadTimeout('t/o')])
    assert adapter.fetch_live_balance() is False
    assert set(adapter.positions) == set(prev)                      # partial ledger discarded
    assert adapter._last_positions_complete is False
    assert adapter.account.total_equity == 7_000_000.0


def test_balance_domestic_mid_page_failure_no_mutation(adapter, monkeypatch):
    from src.execution._kis_adapter import Position
    adapter.positions = {'069500': Position(ticker='069500', quantity=51, avg_price=88000.0, current_price=90000.0)}
    calls, _ = _balance_env(adapter, monkeypatch, [_US_P2])
    import requests
    real_get = requests.get

    def _get(url, headers=None, params=None, timeout=None):
        if 'domestic-stock/v1/trading/inquire-balance' in url and (params or {}).get('CTX_AREA_NK100'):
            return _Resp({'rt_cd': '1', 'msg_cd': 'EGW00201', 'msg1': '초당 거래건수를 초과'})
        return real_get(url, headers=headers, params=params, timeout=timeout)
    monkeypatch.setattr(requests, 'get', _get)
    assert adapter.fetch_live_balance() is False
    assert set(adapter.positions) == {'069500'}
    assert adapter._last_positions_complete is False


def test_balance_fx_unknown_with_us_exposure_degraded(adapter, monkeypatch):
    calls, rec = _balance_env(adapter, monkeypatch, [_US_P1, _US_P2], ctrp_fx='', fx=0.0)
    adapter.account.total_equity = 7_777_777.0
    assert adapter.fetch_live_balance() is False
    assert adapter.nav_fx_degraded is True
    assert adapter.account.total_equity == 7_777_777.0             # NAV not fabricated
    assert set(adapter.positions) == {'069500', '091160', 'TLT', 'XLV'}   # quantities still refreshed (exits work)
    assert rec['n'] == 0                                            # reconciler (own literal) skipped


def test_balance_fx_from_ctrp_bulletin_when_gateway_down(adapter, monkeypatch):
    from src.execution._kis_adapter import KISTraderAdapter
    calls, rec = _balance_env(adapter, monkeypatch, [_US_P1, _US_P2], ctrp_fx='1334.2', fx=0.0)
    assert adapter.fetch_live_balance() is True
    assert adapter.nav_fx_degraded is False
    assert KISTraderAdapter._fx_cache_val == pytest.approx(1334.2)

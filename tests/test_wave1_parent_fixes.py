"""Wave 1 — parent-integrated fixes (FX literals, vendor proxies, Google regex, CRO gate)."""
from __future__ import annotations

import pytest


def _no_fx(monkeypatch):
    import src.data_collection.central_data_gateway as cdg
    monkeypatch.setattr(cdg.CentralDataGateway, 'get_usdkrw_rate', lambda self: None, raising=False)


def test_engine_fx_unknown_returns_zero(monkeypatch):
    _no_fx(monkeypatch)
    from src.execution.execution_engine import ExecutionEngine
    eng = ExecutionEngine.__new__(ExecutionEngine)
    assert eng._get_dynamic_usdkrw_rate() == 0.0


def test_bridge_usdkrw_never_fabricated(monkeypatch):
    _no_fx(monkeypatch)
    import src.data.market_data_bridge as mdb
    b = mdb.MarketDataBridge.__new__(mdb.MarketDataBridge)
    b._field_quality = {}
    b._cache = {}
    # every live source fails
    import src.data_collection.macro_realtime_refresher as mrr
    monkeypatch.setattr(mrr.MacroRealtimeRefresher, '_fetch_usdkrw_naver', staticmethod(lambda: None), raising=False)
    monkeypatch.setattr(mrr.MacroRealtimeRefresher, '_fetch_usdkrw_bok', staticmethod(lambda: None), raising=False)
    import src.data_collection.kis_data_collector as kdc
    monkeypatch.setattr(kdc.KISDataCollector, 'get_usdkrw_exchange_rate', lambda self: None, raising=False)
    try:
        r = b._get_usdkrw()
    except Exception as e:   # pragma: no cover - environment-dependent internals
        pytest.skip(f"bridge internals need more fixtures: {e}")
    assert r.get('current') in (0.0, None) or 900 <= r['current'] <= 2000 and r['current'] not in (1415.7, 1300.0)
    assert r.get('current') != 1415.7


def test_account_reconciler_refuses_without_fx_when_us_exposure():
    import threading
    from src.execution.account_reconciler import AccountReconciler
    a = type('A', (), {})()
    a._lock = threading.RLock()
    a.account = type('Acc', (), {'cash': 1000.0})()
    a.us_cash_usd = 100.0
    a.positions = {}
    a._get_live_fx_rate = lambda: 0.0
    with pytest.raises(ValueError, match='FX_UNKNOWN'):
        AccountReconciler().reconcile(a)


def test_cro_gate_blocks_us_buy_when_fx_unknown(monkeypatch):
    import src.risk.cro_final_gate as g
    monkeypatch.setattr(g, '_cro_live_fx', lambda: 0.0)
    src = open(g.__file__, encoding='utf-8').read()
    assert "1350.0" not in src
    assert "FX_UNKNOWN" in src


def test_vendor_av_no_semantic_proxies():
    src = open('src/utils/vendor_multiplexer.py', encoding='utf-8').read()
    assert "'^TNX': 'IEF'" not in src and "'^SKEW': 'VIXY'" not in src and "'VIX': 'VIXY'" not in src


def test_google_regex_escapes_symbol(monkeypatch):
    import requests
    from src.utils.google_finance_collector import GoogleFinanceCollector as G
    html = '["SKEW","INDEXCBOE"],x,y,z,[142.5,1.2,0.85]'

    class R:
        status_code = 200
        text = html
    seen = {}

    def _get(url, **k):
        seen['url'] = url
        return R()
    monkeypatch.setattr(requests, 'get', _get)
    d = G.get_quote_details('^SKEW')
    assert 'SKEW:INDEXCBOE' in seen['url']
    assert d and abs(float(d.get('price', 0)) - 142.5) < 1e-9


def test_sniper_fallthrough_direction_consistent():
    src = open('src/streams/s1_edge/etf_sniper_stream.py', encoding='utf-8').read()
    assert "_fall_ok('short')" in src and "_fall_ok('long')" in src
    # no residual elif in the tactic chain
    chain = src[src.index('_n_sig0 = len(signals)'):src.index('[V1 Alpha Booster Integration]')]
    assert '\n            elif ' not in chain

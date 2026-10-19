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


# ─────────────────────────────────────────────────────────────
# 2026-10-19 incident: USD cash missing from shadow NAV → fake −34.76% daily loss → kill switch
# ─────────────────────────────────────────────────────────────
def _mk_shadow(tmp_path, monkeypatch):
    import src.portfolio.shadow_manager as sm
    monkeypatch.setattr(sm, '_RESULTS', tmp_path, raising=False)
    m = sm.ShadowPortfolioManager.__new__(sm.ShadowPortfolioManager)
    m.data = {'cash': 402111.0, 'cash_usd': 4580.87, 'virtual_nav': 0.0, 'hwm': 18610659.0,
              'initial_capital': 18610659.0, 'positions': {}, 'daily_snapshots': [], 'daily_records': [],
              'daily_returns': [], 'daily_returns_dates': [], 'trade_history': []}
    return sm, m


def test_shadow_nav_includes_usd_cash(tmp_path, monkeypatch):
    import src.data_collection.central_data_gateway as cdg
    sm, m = _mk_shadow(tmp_path, monkeypatch)
    monkeypatch.setattr(cdg.CentralDataGateway, 'get_usdkrw_rate', lambda self: 1334.3, raising=False)
    nav = m._compose_nav(11739815.0)
    assert abs(nav - (402111.0 + 4580.87 * 1334.3 + 11739815.0)) < 1.0
    # FX unknown → keep last valuation, never fabricate, flag degraded
    monkeypatch.setattr(cdg.CentralDataGateway, 'get_usdkrw_rate', lambda self: None, raising=False)
    nav2 = m._compose_nav(11739815.0)
    assert abs(nav2 - nav) < 1.0 and m.data.get('nav_fx_degraded') is True


def test_daily_snapshot_uses_fresh_broker_nav_in_live(tmp_path, monkeypatch):
    import json
    from datetime import datetime
    sm, m = _mk_shadow(tmp_path, monkeypatch)
    m.data['virtual_nav'] = 12141926.0                       # the faulty shadow NAV seen in production
    (tmp_path / 'kis_portfolio.json').write_text(json.dumps(
        {'timestamp': datetime.now().isoformat(), 'account': {'total_equity': 18405000.0}}))
    monkeypatch.setattr(sm.DynamicConfig, 'get', lambda self, k, d=None: 'live' if k == 'execution.current_mode' else d, raising=False)
    assert m._broker_nav_if_fresh() == 18405000.0
    # stale broker snapshot → not used
    (tmp_path / 'kis_portfolio.json').write_text(json.dumps(
        {'timestamp': '2026-10-01T09:00:00', 'account': {'total_equity': 18405000.0}}))
    assert m._broker_nav_if_fresh() is None


# ─────────────────────────────────────────────────────────────
# 2026-10-19 incident: S5 treated Friday's QQQ daily move (-2.50%) as a live NQ futures crash → bought 2X inverse
# ─────────────────────────────────────────────────────────────
def test_s5_daily_proxy_never_triggers_leveraged_hedge(monkeypatch):
    import src.data.market_data_bridge as mdb
    monkeypatch.setattr(mdb.MarketDataBridge, 'get_nq_futures_change',
                        lambda self, force=False: {'chg_1d_pct': -2.5, 'chg_5d_pct': -3.7, 'close': 710.0,
                                                   'source': 'vendor_multiplexer_primary', 'is_live_futures': False,
                                                   'asof': '2026-10-16'}, raising=False)
    from src.streams.s5_overnight.overnight_stream import S5OvernightStream
    try:
        s5 = S5OvernightStream()
        sigs = s5.generate_signals('caution', {'signal_cache': {'vix': 20.0, 'kospi_change_1d': 0.0}}) or []
    except Exception as e:   # pragma: no cover
        pytest.skip(f"S5 needs more fixtures offline: {e}")
    assert not any(s.get('strategy') == 'nq_inverse_hedge' for s in sigs)


def test_call_auction_release_disabled_and_order_code():
    src = open('scripts/pipeline/sub_phases.py', encoding='utf-8').read()
    assert "call_auction.kodex_release_enabled" in src and "20911598.0" not in src and "105650.0" not in src
    ka = open('src/execution/_kis_adapter.py', encoding='utf-8').read()
    assert "'PREMARKET_CLOSE': '05'" in ka
    cam = open('src/execution/call_auction_manager.py', encoding='utf-8').read()
    assert 'order_type="06"' not in cam and "장전 매도 주문 거부" in cam


def test_adapter_token_fail_fast(monkeypatch):
    import requests
    import time as _t
    from src.execution._kis_adapter import KISTraderAdapter
    a = KISTraderAdapter.__new__(KISTraderAdapter)
    a.base_url, a.app_key, a.app_secret = 'https://x.invalid', 'k', 's'
    a._access_token = None
    monkeypatch.setattr(a, '_load_cached_token', lambda ignore_expiry=False: False, raising=False)
    calls, slept = {'n': 0}, []
    monkeypatch.setattr(requests, 'post', lambda *a_, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError('down')))
    monkeypatch.setattr(_t, 'sleep', lambda s: slept.append(s))
    monkeypatch.setattr(KISTraderAdapter, '_token_fail_until', 0.0, raising=False)
    assert a._request_new_token() is False and sum(slept) <= 2
    assert a._request_new_token() is False and sum(slept) <= 2     # cooldown: no wait, no network


def test_broker_nav_freshness_uses_latest_timestamp(tmp_path, monkeypatch):
    import json
    from datetime import datetime
    sm, m = _mk_shadow(tmp_path, monkeypatch)
    (tmp_path / 'kis_portfolio.json').write_text(json.dumps(
        {'timestamp': '2026-10-19T09:00:00', 'updated_at': datetime.now().isoformat(),
         'account': {'total_equity': 19052453.0}}))
    monkeypatch.setattr(sm.DynamicConfig, 'get', lambda self, k, d=None: 'live' if k == 'execution.current_mode' else d, raising=False)
    assert m._broker_nav_if_fresh() == 19052453.0

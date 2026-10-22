from __future__ import annotations

import pandas as pd
import pytest

from src.data_collection.cross_market_collector import CrossMarketCollector
from src.data_collection.motie_collector import MotieCollector
from src.data_collection.usa_collector import USADataCollector, _AV_SERIES_CACHE
from src.execution._kis_adapter import KISTraderAdapter
from src.utils.vendor_multiplexer import VendorMultiplexer


def test_resolve_overseas_exchange_prioritizes_ticker_mapping():
    """PSQ/UUP/XLV (AMEX) and PG/KO (NYSE) must not be overridden by default 'NASD' caller arg."""
    assert KISTraderAdapter.resolve_overseas_exchange("PSQ", "NASD") == "AMEX"
    assert KISTraderAdapter.resolve_overseas_exchange("UUP", "NASD") == "AMEX"
    assert KISTraderAdapter.resolve_overseas_exchange("XLV", "NASD") == "AMEX"
    assert KISTraderAdapter.resolve_overseas_exchange("PG", "NASD") == "NYSE"
    assert KISTraderAdapter.resolve_overseas_exchange("KO", "NASD") == "NYSE"
    assert KISTraderAdapter.resolve_overseas_exchange("NVDA", "NYSE") == "NASD"


def test_usa_and_cross_market_multi_vendor_primary_when_fred_down(monkeypatch):
    """When FRED is in maintenance (_fred_down=True), DGS10/DGS2/UNRATE succeed via Alpha Vantage."""
    _AV_SERIES_CACHE.clear()
    idx = pd.date_range("2026-09-01", periods=10, freq="B")
    fake_df = pd.DataFrame(
        {
            "open": [4.1 + i * 0.01 for i in range(10)],
            "high": [4.1 + i * 0.01 for i in range(10)],
            "low": [4.1 + i * 0.01 for i in range(10)],
            "close": [4.1 + i * 0.01 for i in range(10)],
            "volume": [0] * 10,
        },
        index=idx,
    )
    monkeypatch.setattr(
        "src.data_collection.alpha_vantage_collector.collect_econ_indicator_daily_ohlcv",
        lambda fn, maturity=None, interval="daily": fake_df,
    )

    usa = USADataCollector()
    usa._fred_down = True
    s_10y = usa._safe_fred_get_series("DGS10", "2026-09-01", "2026-09-30")
    assert s_10y is not None and len(s_10y) == 10
    assert float(s_10y.iloc[-1]) == pytest.approx(4.19, rel=1e-9)

    cm = CrossMarketCollector()
    cm._fred_down = True
    s_cm = cm._safe_fred_get_series("DGS2", "2026-09-01")
    assert s_cm is not None and len(s_cm) == 10


def test_vendor_multiplexer_google_finance_uses_historical_series(monkeypatch):
    """VendorMultiplexer must return real varying historical series for ^SKEW instead of flat constant."""
    idx = pd.date_range("2026-09-01", periods=15, freq="B")
    vals = [140.0 + float(i) for i in range(15)]
    fake_df = pd.DataFrame(
        {"open": vals, "high": vals, "low": vals, "close": vals, "volume": [0] * 15},
        index=idx,
    )
    monkeypatch.setattr(
        "src.data_collection.alpha_vantage_collector.fetch_yahoo_chart_ohlcv",
        lambda ticker, range_str="2y": fake_df,
    )
    vmx = VendorMultiplexer()
    s = vmx._fetch_from_source("^SKEW", "2026-09-01", "2026-09-30", "google_finance", "Close")
    assert s is not None
    assert s.nunique() == 15
    assert float(s.iloc[0]) == pytest.approx(140.0, rel=1e-9)
    assert float(s.iloc[-1]) == pytest.approx(154.0, rel=1e-9)


def test_motie_collector_public_news_fallback_without_naver_creds(monkeypatch):
    """MotieCollector must extract export YoY, trade balance, and semi YoY via public news when NAVER_CLIENT_ID is empty."""
    mc = MotieCollector()
    mc.client_id = ""
    mc.client_secret = ""
    monkeypatch.setattr(
        MotieCollector,
        "_fetch_public_news_texts",
        staticmethod(
            lambda queries: [
                "산업부 발표: 수출 전년대비 +11.4% 증가, 무역수지 65.2억 달러 흑자, 반도체 수출 +28.5% 호조"
            ]
        ),
    )
    res = mc._fetch_from_naver_news(2026, 10)
    assert res["export_yoy_pct"] == pytest.approx(11.4, rel=1e-9)
    assert res["trade_balance_usd"] == pytest.approx(65.2, rel=1e-9)
    assert res["semi_yoy_pct"] == pytest.approx(28.5, rel=1e-9)


def test_atomic_pair_guard_rejects_orphan_inverse_on_preheld_partner():
    """S14 114800 buy must be rejected when its long partner 305720 is only pre-held and not bought in the same batch."""
    from scripts.stream_orchestrator import StreamOrchestrator

    orphan_orders = [
        {
            "stream": "S14_PAIR_ALPHA",
            "stream_id": "S14_PAIR_ALPHA",
            "ticker": "114800",
            "pair_partner": "305720",
            "pair_partners": ["305720"],
            "action": "buy",
            "direction": "long",
            "quantity": 12,
            "price": 1209.0,
        }
    ]
    # Even though 305720 has 84 pre-held shares, 305720 is NOT in active buy orders -> 114800 must be dropped
    filtered = StreamOrchestrator._enforce_pair_and_inverse_mutex_guards(
        orphan_orders, held_qty_by_ticker={"305720": 84}
    )
    assert filtered == []

    # When both 305720 and 114800 are actively bought in the same batch, both survive
    paired_orders = [
        {
            "stream": "S14_PAIR_ALPHA",
            "stream_id": "S14_PAIR_ALPHA",
            "ticker": "305720",
            "pair_partner": "114800",
            "pair_partners": ["114800"],
            "action": "buy",
            "direction": "long",
            "quantity": 2,
            "price": 14160.0,
        },
        {
            "stream": "S14_PAIR_ALPHA",
            "stream_id": "S14_PAIR_ALPHA",
            "ticker": "114800",
            "pair_partner": "305720",
            "pair_partners": ["305720"],
            "action": "buy",
            "direction": "long",
            "quantity": 12,
            "price": 1209.0,
        },
    ]
    filtered_both = StreamOrchestrator._enforce_pair_and_inverse_mutex_guards(
        paired_orders, held_qty_by_ticker={}
    )
    assert [o["ticker"] for o in filtered_both] == ["305720", "114800"]


def test_inverse_mutex_and_consolidation_guard():
    """114800 (1X Inverse) and 252670 (2X Inverse) must never be bought or held simultaneously."""
    from scripts.stream_orchestrator import StreamOrchestrator

    # Case 1: Both 114800 and 252670 in same batch -> only larger notional (252670) survives
    batch = [
        {"ticker": "114800", "action": "buy", "quantity": 12, "price": 1209.0},
        {"ticker": "252670", "action": "buy", "quantity": 2571, "price": 105.0},
    ]
    res1 = StreamOrchestrator._enforce_pair_and_inverse_mutex_guards(batch, held_qty_by_ticker={})
    assert len(res1) == 1
    assert res1[0]["ticker"] == "252670"
    assert res1[0]["quantity"] * res1[0]["price"] == pytest.approx(269955.0, rel=1e-9)

    # Case 2: 114800 (12 shares) already held, 252670 buy arrives -> auto-inserts sell order for 114800 (12 shares)
    batch2 = [{"ticker": "252670", "action": "buy", "quantity": 2571, "price": 105.0}]
    res2 = StreamOrchestrator._enforce_pair_and_inverse_mutex_guards(
        batch2, held_qty_by_ticker={"114800": 12}
    )
    assert len(res2) == 2
    assert res2[0]["ticker"] == "114800"
    assert res2[0]["action"] == "sell"
    assert res2[0]["quantity"] == 12
    assert res2[1]["ticker"] == "252670"
    assert res2[1]["action"] == "buy"

    # Case 3: 252670 already held, 114800 buy arrives -> 114800 buy is blocked
    batch3 = [{"ticker": "114800", "action": "buy", "quantity": 12, "price": 1209.0}]
    res3 = StreamOrchestrator._enforce_pair_and_inverse_mutex_guards(
        batch3, held_qty_by_ticker={"252670": 2571}
    )
    assert res3 == []


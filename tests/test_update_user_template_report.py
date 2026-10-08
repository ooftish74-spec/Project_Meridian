import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from scripts.update_user_template_report import categorize_asset, load_live_portfolio, get_dividend_schedule

def test_categorize_asset():
    assert categorize_asset("CASH", {}) == ("현금", "KRW")
    assert categorize_asset("005930", {"name": "삼성전자"}) == ("국내주식", "KRW")
    assert categorize_asset("233740", {"name": "KODEX 레버리지"}) == ("국내ETF", "KRW")
    assert categorize_asset("NVDA", {"name": "NVIDIA"}) == ("해외주식", "USD")
    assert categorize_asset("TQQQ", {"name": "ProShares UltraPro QQQ"}) == ("해외ETF", "USD")
    assert categorize_asset("XLK", {"name": "Technology Select Sector SPDR Fund"}) == ("해외ETF", "USD")

def test_get_dividend_schedule():
    sched_xlk = get_dividend_schedule("XLK", "USD", "Technology Select Sector SPDR Fund", 189.60)
    assert len(sched_xlk) == 2
    assert sched_xlk[0]["ex_date"] == "2026-09-21"

    sched_kodex = get_dividend_schedule("069500", "KRW", "KODEX 200", 35000)
    assert len(sched_kodex) == 2
    assert sched_kodex[0]["status"] == "실제 수령"
    assert sched_kodex[1]["status"] == "지급 예정"

def test_load_live_portfolio():
    portfolio = load_live_portfolio()
    assert isinstance(portfolio, dict)
    assert "holdings" in portfolio or portfolio == {}

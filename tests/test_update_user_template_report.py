import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import scripts.update_user_template_report as report_mod
from scripts.update_user_template_report import (
    categorize_asset,
    get_dividend_schedule,
    load_live_portfolio,
    load_recent_closed_trades,
    sync_template_sheets,
)


def test_categorize_asset():
    assert categorize_asset("CASH", {}) == ("현금", "KRW")
    assert categorize_asset("USD_CASH", {}) == ("현금", "USD")
    assert categorize_asset("005930", {"name": "삼성전자"}) == ("국내주식", "KRW")
    assert categorize_asset("233740", {"name": "KODEX 레버리지"}) == ("국내ETF", "KRW")
    assert categorize_asset("459580", {"name": "KODEX CD금리액티브(합성)"}) == ("국내ETF", "KRW")
    assert categorize_asset("NVDA", {"name": "NVIDIA"}) == ("해외주식", "USD")
    assert categorize_asset("TQQQ", {"name": "ProShares UltraPro QQQ"}) == ("해외ETF", "USD")
    assert categorize_asset("XLK", {"name": "Technology Select Sector SPDR Fund"}) == ("해외ETF", "USD")
    assert categorize_asset("XLE", {"name": "SS ENERGY SELECT SECTOR SPDR", "market": "US"}) == ("해외ETF", "USD")


def test_get_dividend_schedule():
    sched_xlk = get_dividend_schedule("XLK", "USD", "Technology Select Sector SPDR Fund", 189.60)
    assert len(sched_xlk) == 2
    assert sched_xlk[0]["ex_date"] == "2026-09-21"

    sched_kodex = get_dividend_schedule("069500", "KRW", "KODEX 200", 35000)
    assert len(sched_kodex) == 2
    assert sched_kodex[0]["status"] == "실제 수령"
    assert sched_kodex[1]["status"] == "지급 예정"


def test_load_recent_closed_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "PROJECT_ROOT", tmp_path)
    ledger_dir = tmp_path / "results" / "corporate_ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    csv_file = ledger_dir / "Corporate_Trade_Ledger_20261030.csv"
    csv_file.write_text(
        "거래일자,거래구분,종목코드,종목명,통화,체결수량,체결단가,적용환율(USD/KRW),위탁수수료(KRW),제세공과금(KRW),이동평균취득단가,스트림\n"
        "2026-10-30,매도(SELL),207940,삼성바이오로직스,KRW,1,1384000,1335.3,208.0,2076.0,1336000.0,S10_MEGA_TREND\n",
        encoding="utf-8-sig",
    )
    closed = load_recent_closed_trades()
    assert len(closed) == 1
    rec = closed[0]
    assert rec["ticker"] == "207940"
    assert rec["action"] == "SELL"
    assert rec["quantity"] == pytest.approx(1.0, rel=1e-9)
    assert rec["price"] == pytest.approx(1384000.0, rel=1e-9)
    assert rec["commission"] == pytest.approx(208.0, rel=1e-9)
    assert rec["tax"] == pytest.approx(2076.0, rel=1e-9)
    assert rec["avg_price"] == pytest.approx(1336000.0, rel=1e-9)


def test_sync_template_sheets_unmerges_and_syncs_all_tabs(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "PROJECT_ROOT", tmp_path)

    fake_portfolio = {
        "fx_rate": 1335.30,
        "account": {
            "fx_rate": 1335.30,
            "d2_settlement_cash_krw": 999031,
            "orderable_cash_krw": 943983,
            "cash_usd": 203.72,
            "d0_cash_usd": 259.65,
        },
        "holdings": {
            "271560": {
                "name": "오리온",
                "qty": 2,
                "avg_price": 112600,
                "current_price": 114800,
                "eval_krw": 229600,
                "market": "KR",
                "stream": "S2_EARNINGS",
            },
            "XLE": {
                "name": "SS ENERGY SELECT SECTOR SPDR",
                "qty": 9,
                "avg_price": 56.60,
                "current_price": 56.51,
                "eval_krw": 679112,
                "market": "US",
                "stream": "S9_CROSS_ASSET",
            },
        },
        "closed_trades": [
            {
                "date": "2026-10-30",
                "action": "SELL",
                "ticker": "207940",
                "name": "삼성바이오로직스",
                "qty": 1,
                "price": 1384000,
                "avg_price": 1336000,
                "commission": 208,
                "tax": 2076,
                "reason": "S10 익절",
            }
        ],
    }

    monkeypatch.setattr(report_mod, "load_live_portfolio", lambda: fake_portfolio)
    monkeypatch.setattr(report_mod, "load_position_entry_dates", lambda: {})
    monkeypatch.setattr(report_mod, "load_position_ledger_map", lambda: {})

    class FakeWorksheet:
        def __init__(self, title, sheet_id):
            self.title = title
            self.id = sheet_id
            self.updates = {}

        def update(self, values=None, range_name=None, value_input_option=None):
            self.updates[range_name] = values

    class FakeSpreadsheet:
        def __init__(self):
            self.title = "나의 투자 레포트"
            self.id = "11iFYFay6E7xgVLoQvuAG66J-L1Ee7FzpBcprXqOfwYg"
            self.ws_map = {
                "보유종목": FakeWorksheet("보유종목", 101),
                "배당캘린더": FakeWorksheet("배당캘린더", 102),
                "매매일지": FakeWorksheet("매매일지", 103),
                "대시보드": FakeWorksheet("대시보드", 104),
            }
            self.batch_calls = []

        def worksheet(self, name):
            if name not in self.ws_map:
                import gspread
                raise gspread.WorksheetNotFound(name)
            return self.ws_map[name]

        def fetch_sheet_metadata(self):
            return {
                "sheets": [
                    {
                        "properties": {"sheetId": 101},
                        "merges": [
                            {"sheetId": 101, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 4},
                            {"sheetId": 101, "startRowIndex": 11, "endRowIndex": 12, "startColumnIndex": 0, "endColumnIndex": 8},
                        ],
                    },
                    {"properties": {"sheetId": 102}, "merges": []},
                ]
            }

        def batch_update(self, body):
            self.batch_calls.append(body)

    fake_sh = FakeSpreadsheet()

    class FakeClient:
        def open_by_key(self, key):
            return fake_sh

        def open(self, name):
            return fake_sh

    import gspread

    monkeypatch.setattr(gspread, "service_account", lambda *a, **kw: FakeClient())

    assert sync_template_sheets("11iFYFay6E7xgVLoQvuAG66J-L1Ee7FzpBcprXqOfwYg") is True

    # 1. Verify rogue merge on '보유종목' (startRowIndex=11) was unmerged while startRowIndex=0 was kept
    first_batch = fake_sh.batch_calls[0]["requests"]
    assert len(first_batch) == 1
    assert first_batch[0]["unmergeCells"]["range"]["startRowIndex"] == 11

    # 2. Verify '보유종목' H1 uses KIS fx_rate and Col J prioritizes I{r}
    ws_h = fake_sh.ws_map["보유종목"]
    assert ws_h.updates["G1:H1"][0][1] == '=IF(1335.3>0, 1335.3, GOOGLEFINANCE("CURRENCY:USDKRW"))'
    # 4 items: 271560, XLE, CASH, USD_CASH -> r_start=4, r_end=7, r_sum=8
    rows_h = ws_h.updates["A4:V8"]
    assert rows_h[0][0] == "국내주식" and rows_h[0][3] == "오리온"
    assert rows_h[1][0] == "해외ETF" and rows_h[1][2] == "XLE"
    assert rows_h[0][9].startswith('=IF(OR(C4="CASH",C4="USD_CASH",C4=""),I4,IF(I4>0,I4,')

    # 3. Verify '매매일지' Card 3 is at Col I (index 8) and sell tax is recorded
    ws_t = fake_sh.ws_map["매매일지"]
    assert ws_t.updates["A3:M4"][0][8] == "누적 순실현손익(원화)"
    assert ws_t.updates["A3:M4"][1][8] == "=SUM(K7:K50)"
    sell_rows = [r for r in ws_t.updates["A7:M9"] if r[1] == "매도"]
    assert len(sell_rows) == 1
    assert sell_rows[0][8] == pytest.approx(2076.0, rel=1e-9)

    # 4. Verify '대시보드' C4 and Rows 19..24 PnL Attribution formulas
    ws_d = fake_sh.ws_map["대시보드"]
    assert ws_d.updates["A4:I4"][0][2] == "='보유종목'!O8 + '매매일지'!I4"
    pnl_rows = ws_d.updates["A19:E24"]
    assert pnl_rows[0][1] == "='매매일지'!A4"
    assert pnl_rows[1][1] == "='보유종목'!O8"
    assert pnl_rows[4][1] == "=SUM(B19:B22)"
    assert pnl_rows[5][1] == "=IFERROR(B23/'보유종목'!M8, 0)"


def test_load_live_portfolio():
    portfolio = load_live_portfolio()
    assert isinstance(portfolio, dict)
    assert "holdings" in portfolio or portfolio == {}

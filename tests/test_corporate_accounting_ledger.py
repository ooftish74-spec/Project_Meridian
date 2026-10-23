import json
import pytest
from pathlib import Path
import scripts.export_corporate_ledger as ecl


def test_corporate_tax_bracket_closed_form():
    # 1) Loss or zero -> 0 tax
    assert ecl.calculate_corporate_tax_bracket(-50_000_000.0) == pytest.approx(0.0, abs=1e-9)
    assert ecl.calculate_corporate_tax_bracket(0.0) == pytest.approx(0.0, abs=1e-9)

    # 2) Tier 1: ₩100,000,000 * 9.9% = ₩9,900,000
    assert ecl.calculate_corporate_tax_bracket(100_000_000.0) == pytest.approx(9_900_000.0, rel=1e-9)

    # 3) Tier 2: ₩300,000,000 -> 200M * 9.9% (19.8M) + 100M * 20.9% (20.9M) = ₩40,700,000
    assert ecl.calculate_corporate_tax_bracket(300_000_000.0) == pytest.approx(40_700_000.0, rel=1e-9)


def test_moving_average_cost_and_fx_pnl_decomposition(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    ledger_dir = results_dir / "corporate_ledger"
    results_dir.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)

    monkeypatch.setattr(ecl, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(ecl, "LEDGER_DIR", ledger_dir)
    monkeypatch.setattr(ecl, "DAILY_NAV_JOURNAL_PATH", ledger_dir / "daily_nav_journal.json")
    monkeypatch.setattr(ecl, "BROKER_FILLS_MASTER_PATH", ledger_dir / "broker_fills_master.json")
    monkeypatch.setattr(ecl, "_get_live_fx_rate", lambda: 1400.0)
    monkeypatch.setattr(ecl, "_load_historical_fx_map", lambda default_fx: {"2026-10-01": 1300.0, "2026-10-10": 1400.0})

    # Seed kis_portfolio.json with exact multi-currency NAV identity:
    # Cash KRW = 1,000,000, Cash USD = $1,000 (@1400 = 1,400,000)
    # Holding 000270: 2 shares @ 100,000 = 200,000 KRW
    # Holding SOXX: 1 share @ $200 (@1400 = 280,000 KRW)
    # Total NAV = 1,000,000 + 1,400,000 + 200,000 + 280,000 = 2,880,000 KRW
    kis_payload = {
        "date": "2026-10-10",
        "nav": 2_880_000.0,
        "account": {
            "cash_balance": 1_000_000.0,
            "usd_cash": 1_000.0,
            "fx_rate": 1400.0,
            "total_equity": 2_880_000.0,
        },
        "holdings": {
            "000270": {
                "name": "기아",
                "qty": 2,
                "avg_price": 100000.0,
                "current_price": 100000.0,
                "currency": "KRW",
                "market": "KR",
                "stream_id": "S3_B",
                "track": "TRACK_2_BUFFETT_QVM",
            },
            "SOXX": {
                "name": "ISHARES SEMICONDUCTOR",
                "qty": 1,
                "avg_price": 150.0,
                "current_price": 200.0,
                "currency": "USD",
                "market": "US",
                "stream_id": "S3_A",
            },
        },
    }
    (results_dir / "kis_portfolio.json").write_text(json.dumps(kis_payload), encoding="utf-8")

    # Seed broker_fills_master.json:
    # Buy 2 shares of SOXX on 2026-10-01 @ $150.00 (FX=1300.0)
    # Sell 1 share of SOXX on 2026-10-10 @ $200.00 (FX=1400.0)
    fills = [
        {
            "date": "2026-10-01",
            "timestamp": "2026-10-01T22:30:00",
            "action": "BUY",
            "ticker": "SOXX",
            "name": "ISHARES SEMICONDUCTOR",
            "quantity": 2,
            "price": 150.0,
            "currency": "USD",
            "market": "US",
            "odno": "OD01",
        },
        {
            "date": "2026-10-10",
            "timestamp": "2026-10-10T22:30:00",
            "action": "SELL",
            "ticker": "SOXX",
            "name": "ISHARES SEMICONDUCTOR",
            "quantity": 1,
            "price": 200.0,
            "currency": "USD",
            "market": "US",
            "odno": "OD02",
        },
    ]
    (ledger_dir / "broker_fills_master.json").write_text(json.dumps({"fills": fills}), encoding="utf-8")

    bundle = ecl.build_corporate_accounting_ledgers(fetch_live=False)
    trades = bundle["trade_rows"]
    assert len(trades) == 2

    buy_row, sell_row = trades[0], trades[1]
    # Buy: 2 * $150 = $300, FX=1300 -> Gross KRW = 390,000, Fee USD = round(300*0.0007, 2) = $0.21 -> Fee KRW = round(0.21*1300) = 273
    # Total Buy Cost KRW = 390,273 -> Per share moving avg cost KRW = 195,136.5 -> COGS for 1 share = round(195136.5) = 195,136
    assert buy_row["원화약정금액(KRW)"] == 390_000
    assert buy_row["위탁수수료(KRW)"] == 273
    assert sell_row["매도취득원가(KRW)"] == 195_136

    # Sell: 1 * $200 = $200, FX=1400 -> Gross KRW = 280,000
    # Fee USD = round(200 * 0.0007, 2) = $0.14 -> Fee KRW = round(0.14 * 1400) = 196
    # Tax USD = round(200 * 0.0000278, 2) = $0.01 -> Tax KRW = round(0.01 * 1400) = 14
    # Net Proceeds KRW = 280,000 - 196 - 14 = 279,790
    # Realized Net PnL KRW = 279,790 - 195,136 = +84,654 KRW
    assert sell_row["원화약정금액(KRW)"] == 280_000
    assert sell_row["실현순손익(KRW)"] == 84_654
    assert sell_row["매매처분손익(KRW)"] + sell_row["외환차손익(KRW)"] == sell_row["실현순손익(KRW)"]

    # Daily NAV Journal check
    nav_rows = bundle["daily_nav_rows"]
    assert len(nav_rows) == 1
    assert nav_rows[0]["기말총자산_NAV(KRW)"] == 2_880_000

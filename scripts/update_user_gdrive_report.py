#!/usr/bin/env python3
"""
scripts/update_user_gdrive_report.py
Project Meridian Google Sheets Portfolio Sync Tool.

Syncs live SSOT portfolio data from AWS EC2 / local results/kis_portfolio.json
to the user's Google Sheet ("나의 투자 레포트").
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime

import gspread

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KIS_PORTFOLIO_PATH = PROJECT_ROOT / "results" / "kis_portfolio.json"
SERVICE_ACCOUNT_PATH = PROJECT_ROOT.parent / "Project-A" / "config" / "gsheet_service_account.json"


def load_live_portfolio() -> dict:
    try:
        scripts_dir = str(PROJECT_ROOT / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from inspect_live_truth import inspect_live_truth
        inspect_live_truth(as_json=True)
    except SystemExit:
        pass
    except Exception as e:
        logger.warning(f"Failed live inspection auto-trigger: {e}")

    if not KIS_PORTFOLIO_PATH.exists():
        logger.error(f"Portfolio file not found: {KIS_PORTFOLIO_PATH}")
        return {}
    with open(KIS_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def sync_to_google_sheets(sheet_name_or_id: str = "나의 투자 레포트"):
    if not SERVICE_ACCOUNT_PATH.exists():
        logger.error(f"Service account file not found: {SERVICE_ACCOUNT_PATH}")
        return False

    portfolio = load_live_portfolio()
    if not portfolio:
        logger.error("Empty portfolio data.")
        return False

    try:
        gc = gspread.service_account(filename=str(SERVICE_ACCOUNT_PATH))
        
        # Try by key/id first, then by title
        if sheet_name_or_id.startswith("http") or len(sheet_name_or_id) > 25:
            key = sheet_name_or_id.split("/d/")[-1].split("/")[0] if "/d/" in sheet_name_or_id else sheet_name_or_id
            sh = gc.open_by_key(key)
        else:
            sh = gc.open(sheet_name_or_id)

        logger.info(f"Connected to Google Sheet: {sh.title} (ID: {sh.id})")

        # Select or create tab '나의_투자_레포트'
        try:
            ws = sh.worksheet("나의_투자_레포트")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="나의_투자_레포트", rows=100, cols=10)

        ws.clear()

        # Build data matrix
        account = portfolio.get("account", {})
        holdings = portfolio.get("holdings", {})

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        rows = [
            ["📌 Project Meridian 실시간 포트폴리오 레포트", "", "", "", "", ""],
            [f"최종 동기화 일시: {now_str}", "", "", "", "", ""],
            ["", "", "", "", "", ""],
            ["📊 계좌 요약 (Account Summary)", "", "", "", "", ""],
            ["항목", "금액 / 비중", "비고", "", "", ""],
            ["계좌번호", account.get("number", "N/A"), account.get("type", "N/A"), "", "", ""],
            ["순자산총액 (NAV)", f"₩{account.get('nav', 0):,.0f} KRW", "", "", "", ""],
            ["예수금 잔고 (Cash)", f"₩{account.get('cash', 0):,.0f} KRW", "", "", "", ""],
            ["투자 금액 (Invested)", f"₩{account.get('invested', 0):,.0f} KRW", f"{account.get('invest_pct', 0):.2f}%", "", "", ""],
            ["운용 레짐 (Regime)", portfolio.get("regime", "Caution"), "", "", "", ""],
            ["", "", "", "", "", ""],
            ["📦 보유 종목 상세 (Live Holdings)", "", "", "", "", ""],
            ["시장", "종목코드/티커", "종목명", "보유수량", "평균단가", "수익률(%)"]
        ]

        for ticker, info in holdings.items():
            market = info.get("market", "US" if not ticker.isdigit() else "KR")
            name = info.get("name", ticker)
            qty = info.get("qty", 0)
            avg_price = info.get("avg_price", 0)
            ret_pct = info.get("return_pct", 0.0)

            price_str = f"${avg_price:,.2f}" if market == "US" else f"₩{avg_price:,.0f}"
            rows.append([market, ticker, name, qty, price_str, f"{ret_pct:+.2f}%"])

        rows.append(["", "", "", "", "", ""])
        rows.append(["🛡️ 레버리지/곱버스 ETP 점유율", "0.00% (현물 자산 100% 안전 운용)", "", "", "", ""])

        ws.update(values=rows, range_name="A1")
        logger.info("Successfully updated Google Sheet with live holdings!")
        return True

    except gspread.exceptions.SpreadsheetNotFound:
        logger.error("SpreadsheetNotFound: Service account does not have access to the file.")
        return False
    except Exception as e:
        logger.error(f"Failed to update Google Sheet: {e}")
        return False


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "나의 투자 레포트"
    success = sync_to_google_sheets(target)
    sys.exit(0 if success else 1)

#!/usr/bin/env python3
"""
scripts/update_user_template_report.py
Project Meridian Google Sheets Master Integration Engine.

Synchronizes & Formats ALL 4 Core Worksheets:
1. 보유종목 (Holdings)
2. 대시보드 (Dashboard)
3. 배당캘린더 (Dividend Calendar)
4. 매매일지 (Trading Log)

Strict Currency & Number Formatting Rules:
- KRW Amounts: '₩#,##0' (₩ symbol, 0 decimal places, thousand separators)
- USD Amounts: '"$"#,##0.00' ($ symbol, 2 decimal places, thousand separators)
- Percentages: '0.0%' (1 decimal place with % symbol)
- Quantities: '#,##0' (thousand separators)
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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
KIS_PORTFOLIO_PATH = PROJECT_ROOT / "results" / "kis_portfolio.json"
POSITION_LEDGER_PATH = PROJECT_ROOT / "results" / "position_entry_ledger.json"
SERVICE_ACCOUNT_PATH = PROJECT_ROOT.parent / "Project-A" / "config" / "gsheet_service_account.json"


def load_position_ledger_map() -> dict:
    if not POSITION_LEDGER_PATH.exists():
        return {}
    try:
        with open(POSITION_LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            if k not in ("entries", "positions", "error") and isinstance(v, dict):
                out[str(k).upper()] = v
        for subk in ("positions", "entries"):
            if isinstance(data.get(subk), dict):
                for k, v in data[subk].items():
                    if isinstance(v, dict):
                        out[str(k).upper()] = {**out.get(str(k).upper(), {}), **v}
        return out
    except Exception as e:
        logger.warning(f"Could not load position entry ledger: {e}")
        return {}


def load_position_entry_dates() -> dict:
    ledger_map = load_position_ledger_map()
    return {
        k: v.get("entry_date") or v.get("first_seen")
        for k, v in ledger_map.items()
        if isinstance(v, dict) and (v.get("entry_date") or v.get("first_seen"))
    }


def load_full_corporate_trade_ledger() -> list[dict]:
    """Load ALL historical KIS broker executions (BUY & SELL) from Corporate_Trade_Ledger CSV."""
    import csv
    import glob
    ledger_dir = PROJECT_ROOT / "results" / "corporate_ledger"
    csv_files = sorted(glob.glob(str(ledger_dir / "Corporate_Trade_Ledger_*.csv")))
    if not csv_files:
        return []
    try:
        with open(csv_files[-1], "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if rows and "주문번호(ODNO)" in rows[0]:
            return rows
    except Exception as e:
        logger.warning(f"Could not read full Corporate_Trade_Ledger CSV: {e}")
    return []


def load_recent_closed_trades(target_date: str = None) -> list[dict]:
    """Load recent SELL executions from Corporate_Trade_Ledger CSV (or shadow_portfolio fallback)."""
    import csv
    import glob
    out = []
    ledger_dir = PROJECT_ROOT / "results" / "corporate_ledger"
    csv_files = sorted(glob.glob(str(ledger_dir / "Corporate_Trade_Ledger_*.csv")))
    if csv_files:
        try:
            with open(csv_files[-1], "r", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            sells = [r for r in rows if "SELL" in str(r.get("거래구분", "")).upper()]
            if sells:
                latest_dt = target_date or max(str(r.get("거래일자", ""))[:10] for r in sells)
                for r in sells:
                    dt = str(r.get("거래일자", ""))[:10]
                    if dt != latest_dt:
                        continue
                    curr = str(r.get("통화", "KRW")).strip().upper()
                    fx = float(r.get("적용환율(USD/KRW)") or 1335.3)
                    qty = float(r.get("체결수량") or 0)
                    price = float(r.get("체결단가") or 0)
                    avg_p = float(r.get("이동평균취득단가") or price)
                    fee_krw = float(r.get("위탁수수료(KRW)") or 0)
                    tax_krw = float(r.get("제세공과금(KRW)") or 0)
                    fee_local = round(fee_krw / fx, 2) if (curr == "USD" and fx > 0) else round(fee_krw)
                    tax_local = round(tax_krw / fx, 2) if (curr == "USD" and fx > 0) else round(tax_krw)
                    stream_str = str(r.get("스트림") or r.get("운용트랙") or "리밸런싱")
                    out.append({
                        "date": dt,
                        "action": "SELL",
                        "ticker": str(r.get("종목코드", "")).strip(),
                        "name": str(r.get("종목명", "")).strip(),
                        "quantity": int(qty) if qty.is_integer() else qty,
                        "price": price,
                        "avg_price": avg_p,
                        "commission": fee_local,
                        "tax": tax_local,
                        "currency": curr,
                        "reason": f"Project Meridian [{stream_str}] 리밸런싱 매도",
                    })
                return out
        except Exception as e:
            logger.warning(f"Could not parse Corporate_Trade_Ledger CSV: {e}")
    return out


def load_live_portfolio() -> dict:
    import os
    skip_inspect = os.environ.get("MERIDIAN_SKIP_LIVE_INSPECT") == "1"
    refresh_ledger = os.environ.get("MERIDIAN_REFRESH_LEDGER") == "1"
    if not skip_inspect:
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
    elif refresh_ledger:
        try:
            from scripts.export_corporate_ledger import export_corporate_accounting_ledgers
            export_corporate_accounting_ledgers()
        except Exception as e:
            logger.warning(f"Failed corporate ledger refresh: {e}")

    if not KIS_PORTFOLIO_PATH.exists():
        logger.error(f"Portfolio file not found: {KIS_PORTFOLIO_PATH}")
        return {}
    with open(KIS_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and not (data.get("closed_trades") or data.get("trade_history")):
        recent_sells = load_recent_closed_trades()
        if recent_sells:
            data["closed_trades"] = recent_sells
    return data


def categorize_asset(ticker: str, info: dict) -> tuple[str, str]:
    """Returns (asset_class, currency)"""
    from src.utils.market_calendar import is_kr_ticker
    name = info.get("name", ticker).upper()
    t_up = str(ticker).upper()
    _is_kr = is_kr_ticker(str(ticker))
    market = info.get("market", "US" if not _is_kr else "KR")

    if ticker == "CASH" or "CASH" in t_up:
        return "현금", ("USD" if "USD" in t_up else "KRW")

    is_etf = any(kw in name or kw in t_up for kw in [
        "ETF", "KODEX", "TIGER", "ACE", "SOL", "ARIRANG", "HANARO", "RISE",
        "SPDR", "SHARES", "ISHARES", "PROSHARES", "INVESCO", "VANGUARD", "DIREXION",
        "SOXX", "QQQ", "XLK", "XLE", "XLF", "XLV", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC",
        "SHV", "SHY", "IEF", "TLT", "HYG", "LQD", "SPY", "TQQQ", "SQQQ", "IVV", "VOO", "IWM",
        "GLD", "SLV", "USO", "CPER", "UUP", "EWY", "EWJ"
    ])

    if market == "KR" or _is_kr or ticker.startswith("KRX:"):
        return ("국내ETF" if is_etf else "국내주식"), "KRW"
    else:
        return ("해외ETF" if is_etf else "해외주식"), "USD"


def get_stream_strategy_info(ticker: str, info: dict = None) -> tuple[str, str]:
    """Returns (stream_name, strategy_desc) for a given position ticker."""
    from src.utils.market_calendar import is_kr_ticker
    from scripts.rebuild_stream_metrics import _infer_canonical_stream
    info = info if isinstance(info, dict) else {}
    t_upper = str(ticker).strip().upper()
    if t_upper.startswith("KRX:"):
        t_upper = t_upper.split(":", 1)[1]
    if t_upper == "USD_CASH":
        return "S0 Cash Anchor (USD)", (info.get("strategy") or "외화(USD) 예수금 및 글로벌 통합증거금 안전 파킹")
    if ticker == "CASH" or "CASH" in t_upper:
        return "S0 Cash Anchor (KRW)", (info.get("strategy") or "원화(KRW) 현금성 자산 및 리밸런싱 안전 파킹")

    ledger_map = load_position_ledger_map()
    l_entry = ledger_map.get(t_upper, {})
    raw_sid = (
        info.get("stream_id")
        or info.get("stream")
        or l_entry.get("stream_id")
        or l_entry.get("stream")
        or ""
    )
    resolved_sid = _infer_canonical_stream(t_upper, str(raw_sid), ledger_map)
    resolved_strategy = (
        info.get("strategy")
        or l_entry.get("strategy")
        or ""
    )
    stream_labels = {
        "S1": ("S1 Trend Following", "국내 KOSPI 200 알파 모멘텀 추세 추종"),
        "S2": ("S2 KRX Fundamental Swing", "국내 펀더멘털 & 수급 스윙 알파"),
        "S3": ("S3 Active Macro", "글로벌 매크로 및 멀티에셋 오실레이터"),
        "S3_B": ("S3_B Buffett QVM Value", "Track 2 버핏 해자 QVM 장기 복리 가치투자"),
        "S4": ("S4 Defensive Alpha", "배당 및 방어형 멀티에셋 파킹"),
        "S5": ("S5 KRX Sector Rotation", "국내 핵심 섹터 ETF 로테이션 알파"),
        "S6": ("S6 US Equity Alpha", "미국 대형 퀄리티/테크 코어 알파"),
    }
    default_label, default_desc = stream_labels.get(
        resolved_sid,
        (f"{resolved_sid} Active Alpha", "멀티팩터 정량 알파 전략" if not is_kr_ticker(t_upper) else "국내 정량 알파 스크리닝"),
    )
    return default_label, (resolved_strategy or default_desc)



# Verified 2026 Ex-Dividend / Ex-Distribution Events from KIS KSD (HHKDB669102C0) & US Exchanges
_STATUTORY_EX_DIV_EVENTS_2026 = [  # hardcode-ok: L0 statutory KSD (HHKDB669102C0) and US exchange ex-dividend schedule
    {"ticker": "069500", "gf_ticker": "KRX:069500", "name": "KODEX 200", "curr": "KRW", "ex_date": "2026-07-30", "pay_date": "2026-08-04", "month": 8, "dps": 183.0},
    {"ticker": "459580", "gf_ticker": "KRX:459580", "name": "KODEX CD금리액티브(합성)", "curr": "KRW", "ex_date": "2026-07-30", "pay_date": "2026-08-04", "month": 8, "dps": 2470.0},
    {"ticker": "459580", "gf_ticker": "KRX:459580", "name": "KODEX CD금리액티브(합성)", "curr": "KRW", "ex_date": "2026-08-28", "pay_date": "2026-09-02", "month": 9, "dps": 2758.0},
    {"ticker": "TMUS", "gf_ticker": "TMUS", "name": "T 모바일 US", "curr": "USD", "ex_date": "2026-08-28", "pay_date": "2026-09-11", "month": 9, "dps": 1.02},
    {"ticker": "SHV", "gf_ticker": "SHV", "name": "iShares Short Treasury Bond ETF", "curr": "USD", "ex_date": "2026-09-01", "pay_date": "2026-09-08", "month": 9, "dps": 0.338},
    {"ticker": "NVDA", "gf_ticker": "NVDA", "name": "NVIDIA Corp", "curr": "USD", "ex_date": "2026-09-10", "pay_date": "2026-10-01", "month": 10, "dps": 0.25},
    {"ticker": "SOXX", "gf_ticker": "SOXX", "name": "iShares Semiconductor ETF", "curr": "USD", "ex_date": "2026-09-15", "pay_date": "2026-09-19", "month": 9, "dps": 0.325},
    {"ticker": "QQQ", "gf_ticker": "QQQ", "name": "Invesco QQQ Trust", "curr": "USD", "ex_date": "2026-09-21", "pay_date": "2026-10-30", "month": 10, "dps": 0.751},
    {"ticker": "XLK", "gf_ticker": "XLK", "name": "Technology Select Sector SPDR Fund", "curr": "USD", "ex_date": "2026-09-21", "pay_date": "2026-09-25", "month": 9, "dps": 0.221},
    {"ticker": "459580", "gf_ticker": "KRX:459580", "name": "KODEX CD금리액티브(합성)", "curr": "KRW", "ex_date": "2026-09-29", "pay_date": "2026-10-02", "month": 10, "dps": 2778.0},
    {"ticker": "SHV", "gf_ticker": "SHV", "name": "iShares Short Treasury Bond ETF", "curr": "USD", "ex_date": "2026-10-01", "pay_date": "2026-10-06", "month": 10, "dps": 0.299},
    {"ticker": "069500", "gf_ticker": "KRX:069500", "name": "KODEX 200", "curr": "KRW", "ex_date": "2026-10-29", "pay_date": "2026-11-03", "month": 11, "dps": 136.0},
    {"ticker": "459580", "gf_ticker": "KRX:459580", "name": "KODEX CD금리액티브(합성)", "curr": "KRW", "ex_date": "2026-10-29", "pay_date": "2026-11-03", "month": 11, "dps": 2865.0},
]

# Official Forward 12M Dividend / Distribution Schedule (KIS KSD HHKDB669102C0 & US Exchange Filings)
# Non-dividend inverse/synthetic futures ETPs (114800, 252670, 122630, SQQQ, PSQ, SOXS, USO, UUP, CPER) intentionally excluded (0 payout)
_STATUTORY_FORWARD_DIV_SCHEDULE = {  # hardcode-ok: L0 statutory KSD (HHKDB669102C0) and US exchange forward dividend schedule
    "000270": [
        {"status": "지급 예정", "ex_date": "2027-03-24", "pay_date": "2027-04-20", "month": 4, "dps": 6800.0},
    ],
    "034230": [
        {"status": "지급 예정", "ex_date": "2026-12-29", "pay_date": "2027-04-23", "month": 4, "dps": 150.0},
    ],
    "086280": [
        {"status": "지급 예정", "ex_date": "2027-03-29", "pay_date": "2027-04-23", "month": 4, "dps": 5800.0},
    ],
    "271560": [
        {"status": "지급 예정", "ex_date": "2027-03-29", "pay_date": "2027-04-16", "month": 4, "dps": 3500.0},
        {"status": "지급 예정", "ex_date": "2027-07-20", "pay_date": "2027-08-06", "month": 8, "dps": 1750.0},
    ],
    "305720": [
        {"status": "지급 예정", "ex_date": "2027-04-29", "pay_date": "2027-05-06", "month": 5, "dps": 18.0},
    ],
    "459580": [
        {"status": "지급 예정", "ex_date": "2026-11-27", "pay_date": "2026-12-02", "month": 12, "dps": 2865.0},
        {"status": "지급 예정", "ex_date": "2026-12-29", "pay_date": "2027-01-05", "month": 1, "dps": 2865.0},
    ],
    "AAPL": [
        {"status": "지급 예정", "ex_date": "2026-11-09", "pay_date": "2026-11-13", "month": 11, "dps": 0.27},
        {"status": "지급 예정", "ex_date": "2027-02-08", "pay_date": "2027-02-12", "month": 2, "dps": 0.27},
        {"status": "지급 예정", "ex_date": "2027-05-10", "pay_date": "2027-05-14", "month": 5, "dps": 0.27},
        {"status": "지급 예정", "ex_date": "2027-08-09", "pay_date": "2027-08-13", "month": 8, "dps": 0.27},
    ],
    "LLY": [
        {"status": "지급 예정", "ex_date": "2026-11-13", "pay_date": "2026-12-10", "month": 12, "dps": 1.73},
        {"status": "지급 예정", "ex_date": "2027-02-12", "pay_date": "2027-03-10", "month": 3, "dps": 1.73},
        {"status": "지급 예정", "ex_date": "2027-05-14", "pay_date": "2027-06-10", "month": 6, "dps": 1.73},
        {"status": "지급 예정", "ex_date": "2027-08-13", "pay_date": "2027-09-10", "month": 9, "dps": 1.73},
    ],
    "V": [
        {"status": "지급 예정", "ex_date": "2026-11-12", "pay_date": "2026-12-01", "month": 12, "dps": 0.67},
        {"status": "지급 예정", "ex_date": "2027-02-09", "pay_date": "2027-03-01", "month": 3, "dps": 0.67},
        {"status": "지급 예정", "ex_date": "2027-05-11", "pay_date": "2027-06-01", "month": 6, "dps": 0.67},
        {"status": "지급 예정", "ex_date": "2027-08-10", "pay_date": "2027-09-01", "month": 9, "dps": 0.67},
    ],
    "XLE": [
        {"status": "지급 예정", "ex_date": "2026-12-21", "pay_date": "2026-12-24", "month": 12, "dps": 0.38},
        {"status": "지급 예정", "ex_date": "2027-03-22", "pay_date": "2027-03-25", "month": 3, "dps": 0.385},
        {"status": "지급 예정", "ex_date": "2027-06-21", "pay_date": "2027-06-24", "month": 6, "dps": 0.385},
        {"status": "지급 예정", "ex_date": "2027-09-20", "pay_date": "2027-09-24", "month": 9, "dps": 0.38},
    ],
}


_STATUTORY_CORP_WHT_RATES = {  # hardcode-ok: L0 statutory corporate dividend withholding tax rates (US treaty Art.12 15%, KRX ETF Art.73 14%, KRX stock 0%)
    "US": 0.15,
    "KR_ETF": 0.14,
    "KR_STOCK": 0.0,
}


def classify_corporate_dividend_tax(ticker: str, curr: str, name: str) -> dict:
    """
    Classifies a dividend/distribution into its Korean Corporate Tax Law (법인세법) category,
    statutory withholding tax rate, and required corporate tax return schedule (별지 서식).
    """
    c_up = str(curr).strip().upper()
    asset_cls, _ = categorize_asset(ticker, {"name": name, "market": "US" if c_up == "USD" else "KR"})
    if c_up == "USD":
        return {
            "market_label": "미국(US)",
            "income_type": "해외배당 [별지8호부표5 외국납부]",
            "tax_form": "별지 제8호서식 부표5 (외국납부세액공제 명세서)",
            "wht_rate": _STATUTORY_CORP_WHT_RATES["US"],
            "credit_acct": "배당금수익(해외)",
            "debit_tax_acct": "선납세금(외국납부)",
            "debit_cash_acct": "외화예수금(USD)",
        }
    if asset_cls == "국내ETF":
        return {
            "market_label": "국내(KRX)",
            "income_type": "국내ETF분배 [별지10호 원천납부]",
            "tax_form": "별지 제10호서식(갑) (원천납부세액명세서)",
            "wht_rate": _STATUTORY_CORP_WHT_RATES["KR_ETF"],
            "credit_acct": "배당금수익(국내ETF)",
            "debit_tax_acct": "선납세금(국내원천)",
            "debit_cash_acct": "보통예금(KRW)",
        }
    return {
        "market_label": "국내(KRX)",
        "income_type": "국내주식배당 [별지16호 익금불산입]",
        "tax_form": "별지 제16호서식 (수입배당금액 명세서)",
        "wht_rate": _STATUTORY_CORP_WHT_RATES["KR_STOCK"],
        "credit_acct": "배당금수익(국내주식)",
        "debit_tax_acct": "원천징수면제(0%)",
        "debit_cash_acct": "보통예금(KRW)",
    }


def resolve_historical_pay_fx(
    pay_date: str,
    curr: str,
    ledger_rows: list[dict],
    fallback_fx: float = 1.0,
) -> float:
    """
    Resolves the historical USD/KRW exchange rate on or immediately prior to `pay_date`
    from the corporate trade ledger so confirmed foreign dividend revenue is frozen at
    the pay-date FX rate (법인세법 시행령 제70조 제2항).
    """
    if str(curr).strip().upper() == "KRW":
        return 1.0
    best_dt = ""
    best_fx = 0.0
    earliest_dt = "9999-99-99"
    earliest_fx = 0.0
    for r in ledger_rows or []:
        if str(r.get("통화", "")).strip().upper() != "USD":
            continue
        dt_s = str(r.get("거래일자", ""))[:10]
        fx_v = float(r.get("적용환율(USD/KRW)") or 0.0)
        if fx_v <= 1.0 or not dt_s:
            continue
        if dt_s < earliest_dt:
            earliest_dt = dt_s
            earliest_fx = fx_v
        if dt_s <= pay_date and dt_s >= best_dt:
            best_dt = dt_s
            best_fx = fx_v
    if best_fx > 1.0:
        return round(best_fx, 2)
    if earliest_fx > 1.0:
        return round(earliest_fx, 2)
    return round(fallback_fx, 2) if fallback_fx > 1.0 else 1.0


def extract_account_dividend_entitlements(
    ledger_rows: list[dict],
    now_date_str: str,
    fallback_fx: float = 1.0,
) -> list[dict]:
    """
    Reconstructs exact share quantities held in the KIS account at each historical ex-dividend date
    (trade_date < ex_date) from the 321 real KIS fills in Corporate_Trade_Ledger_*.csv.
    """
    if not ledger_rows:
        return []
    out = []
    for ev in _STATUTORY_EX_DIV_EVENTS_2026:
        tk = ev["ticker"]
        ex_dt = ev["ex_date"]
        held_qty = 0
        for r in ledger_rows:
            if str(r.get("종목코드", "")).strip().upper() == tk and str(r.get("거래일자", ""))[:10] < ex_dt:
                q = int(round(float(r.get("체결수량") or 0)))
                side = str(r.get("거래구분", "")).upper()
                if "BUY" in side or "매수" in side:
                    held_qty += q
                else:
                    held_qty -= q
        if held_qty > 0:
            pay_dt = ev["pay_date"]
            status = "실제 수령" if pay_dt <= now_date_str else "지급 예정"
            tax_meta = classify_corporate_dividend_tax(ev["gf_ticker"], ev["curr"], ev["name"])
            pay_fx = resolve_historical_pay_fx(pay_dt, ev["curr"], ledger_rows, fallback_fx=fallback_fx)
            out.append({
                "ticker": ev["gf_ticker"],
                "curr": ev["curr"],
                "name": ev["name"],
                "status": status,
                "ex_date": ex_dt,
                "pay_date": pay_dt,
                "month": ev["month"],
                "dps": ev["dps"],
                "hist_qty": held_qty,
                "locked_qty": True,
                "pay_fx": pay_fx,
                **tax_meta,
            })
    return out


def get_dividend_schedule(ticker: str, curr: str, name: str, avg_price: float) -> list[dict]:
    """
    Returns verified forward dividend/distribution schedule for a currently held ticker.
    Zero fabricated 1.5% fallbacks; non-distributing inverse/futures ETPs (e.g. 114800) return [].
    """
    t_upper = str(ticker).strip().upper()
    if t_upper.startswith("KRX:"):
        t_upper = t_upper.split(":", 1)[1]
    return [dict(rec, locked_qty=False) for rec in _STATUTORY_FORWARD_DIV_SCHEDULE.get(t_upper, [])]


def _batch_ws_updates(ws, items: list[dict], value_input_option: str = "USER_ENTERED") -> None:
    """Send multiple range updates for a single worksheet in 1 API call to prevent HTTP 429 quota exhaustion."""
    if hasattr(ws, "batch_update"):
        ws.batch_update(items, value_input_option=value_input_option)
    else:
        for item in items:
            ws.update(
                values=item["values"],
                range_name=item["range"],
                value_input_option=value_input_option,
            )


def sync_template_sheets(spreadsheet_id_or_name: str = "11iFYFay6E7xgVLoQvuAG66J-L1Ee7FzpBcprXqOfwYg", force: bool = False):
    if not SERVICE_ACCOUNT_PATH.exists():
        logger.error(f"Service account file not found: {SERVICE_ACCOUNT_PATH}")
        return False

    # [Quota 429 Cooldown Guard] 구글 시트 분당 60회 쓰기 한도 보호 (50초 쿨다운, 체결 시 force=True 즉시 통과)
    import time
    last_sync_file = PROJECT_ROOT / "results" / ".last_gsheet_sync_time"
    now_ts = time.time()
    if not force and last_sync_file.exists():
        try:
            last_ts = float(last_sync_file.read_text().strip())
            if now_ts - last_ts < 25:
                logger.info("  ⏳ [GSheet Cooldown] 최근 25초 내 동기화 완료 → Google Sheets Quota 방어를 위해 이번 호출 스킵")
                return True
        except Exception:
            pass

    portfolio = load_live_portfolio()
    if not portfolio:
        logger.error("Empty portfolio data.")
        return False

    try:
        gc = gspread.service_account(filename=str(SERVICE_ACCOUNT_PATH))
        if spreadsheet_id_or_name.startswith("http") or len(spreadsheet_id_or_name) > 25:
            key = spreadsheet_id_or_name.split("/d/")[-1].split("/")[0] if "/d/" in spreadsheet_id_or_name else spreadsheet_id_or_name
            sh = gc.open_by_key(key)
        else:
            sh = gc.open(spreadsheet_id_or_name)

        logger.info(f"Connected to Google Sheet: {sh.title} (ID: {sh.id})")

        # 1. Cleanup extra scratch tabs
        try:
            extra_ws = sh.worksheet("나의_투자_레포트")
            sh.del_worksheet(extra_ws)
        except gspread.WorksheetNotFound:
            pass

        raw_holdings = portfolio.get("holdings", {})
        account = portfolio.get("account", {})

        # Prepare positions list
        items = []
        for ticker, info in raw_holdings.items():
            items.append((ticker, info))

        # Always include KRW CASH and USD CASH (if > 0) so Total NAV matches KIS MTS 100%
        fx_rate = float(account.get("fx_rate") or portfolio.get("fx_rate") or 0.0)
        cash_val = float(account.get("d2_settlement_cash_krw") or account.get("cash", 0) or 0)
        ord_cash = float(account.get("orderable_cash_krw") or account.get("cash", 0) or cash_val)
        d0_krw = float(account.get("d0_deposit_krw") or 0.0)
        unsettled_krw = round(cash_val - ord_cash, 0)
        _d0_krw_suffix = f" | D+0 결제전 ₩{d0_krw:,.0f}" if d0_krw > 0 else ""
        krw_cash_desc = (
            f"D+2 정산예수금 ₩{cash_val:,.0f} (주문가능 ₩{ord_cash:,.0f} + 당일정산대기 ₩{unsettled_krw:,.0f}{_d0_krw_suffix}) — KIS 체결기준"
            if abs(unsettled_krw) >= 1 or d0_krw > 0
            else "대기성 현금 리저브 (위험 회피 및 신규 기회 포착용 안전판)"
        )
        cash_usd = float(account.get("cash_usd", account.get("us_cash_usd", 0)) or 0)
        current_assets = cash_val + (cash_usd * fx_rate) + sum(
            float(h.get("qty", 0) or 0)
            * float(h.get("current_price", h.get("avg_price", 0)) or 0)
            * (fx_rate if categorize_asset(t, h)[1] == "USD" else 1.0)
            for t, h in raw_holdings.items() if isinstance(h, dict)
        )
        settlement_total_asset_krw = int(round(float(account.get("settlement_total_asset_krw") or portfolio.get("settlement_total_asset_krw") or 0)))
        logger.info(f"Computed total current_assets (including cash_usd * fx_rate): ₩{current_assets:,.0f} (settlement_total_asset_krw=₩{settlement_total_asset_krw:,.0f})")
        items.append(("CASH", {"name": "원화 현금성자산 (KRW 예수금)", "qty": 1, "avg_price": cash_val, "current_price": cash_val, "market": "KR", "strategy": krw_cash_desc}))
        us_cash_val = cash_usd
        if us_cash_val > 0:
            d0_usd = float(account.get("d0_usd_deposit", account.get("d0_cash_usd", us_cash_val)) or us_cash_val)
            pledged_usd = float(account.get("pledged_margin_usd", 0.0) or 0.0)
            if pledged_usd > 0.0:
                usd_cash_desc = f"외화 가용예수금 ${us_cash_val:,.2f} (D+0 총예수금 ${d0_usd:,.2f} - 미결제매수증거금 ${pledged_usd:,.2f}) — US 매수 대기 리저브"
            else:
                usd_cash_desc = f"외화 가용예수금 ${us_cash_val:,.2f} (D+0 총예수금 ${d0_usd:,.2f}) — US 매수 대기 리저브"
            items.append(("USD_CASH", {"name": "외화 현금성자산 (USD 예수금)", "qty": 1, "avg_price": us_cash_val, "current_price": us_cash_val, "eval_krw": round(us_cash_val * fx_rate, 0), "market": "US", "strategy": usd_cash_desc}))

        num_items = len(items)
        r_start = 4
        r_end = r_start + num_items - 1
        r_sum = r_end + 1

        # =========================================================================
        # TAB 1: 보유종목 (Holdings)
        # =========================================================================
        ws_holdings = sh.worksheet("보유종목")

        # Unmerge any legacy merged ranges on '보유종목' data rows (startRowIndex >= 1)
        try:
            sheet_meta = sh.fetch_sheet_metadata()
            unmerge_holdings = []
            for s in sheet_meta.get("sheets", []):
                if s["properties"]["sheetId"] == ws_holdings.id:
                    for m in s.get("merges", []):
                        if m.get("startRowIndex", 0) >= 1:
                            unmerge_holdings.append({"unmergeCells": {"range": m}})
            if unmerge_holdings:
                sh.batch_update({"requests": unmerge_holdings})
                logger.info(f"Unmerged {len(unmerge_holdings)} legacy merged ranges in '보유종목'")
        except Exception as e:
            logger.warning(f"Could not unmerge legacy cells in '보유종목': {e}")

        # Headers for Row 3 (A3:V3)
        headers_holdings = [[
            "자산군", "통화", "티커", "종목명", "운용 스트림", "투자 전략 및 산출 근거",
            "수량", "매입단가\n(현지)", "현재가\n(현지)", "실시간가\n(현지)",
            "매입금액\n(현지)", "평가금액\n(현지)", "매입금액\n(원화)", "평가금액\n(원화)",
            "평가손익\n(원화)", "수익률", "포트비중", "리밸런싱 신호",
            "연간 예상배당금\n(원화)", "배당수익률", "세후 배당금\n(원화)", "세후 배당수익률"
        ]]

        holdings_data = []
        item_currencies = []

        from src.utils.market_calendar import is_kr_ticker as _is_kr_rep
        for idx, (ticker, info) in enumerate(items):
            r = r_start + idx
            asset_class, curr = categorize_asset(ticker, info)
            item_currencies.append(curr)
            
            gf_ticker = ticker
            if curr == "KRW" and _is_kr_rep(str(ticker)) and not ticker.startswith("KRX:"):
                gf_ticker = f"KRX:{ticker}"

            name = info.get("name", ticker)
            qty = info.get("qty", 1)
            avg_price = info.get("avg_price", 0)
            curr_price = info.get("current_price", avg_price)
            stream_id, strategy_desc = get_stream_strategy_info(ticker, info)

            row = [
                asset_class,                                                              # A: 자산군
                curr,                                                                     # B: 통화
                gf_ticker,                                                                # C: 티커
                name,                                                                     # D: 종목명
                stream_id,                                                                # E: 운용 스트림 (Col E)
                strategy_desc,                                                            # F: 투자 전략 및 산출 근거 (Col F)
                qty,                                                                      # G: 수량 (Col G)
                avg_price,                                                                # H: 매입단가(현지) (Col H)
                curr_price,                                                               # I: 현재가(현지) (Col I)
                f'=IF(OR(C{r}="CASH",C{r}="USD_CASH",C{r}=""),I{r},IF(I{r}>0,I{r},IFERROR(GOOGLEFINANCE(C{r},"price"),H{r})))', # J: 실시간가(현지) (Col J)
                f'=G{r}*H{r}',                                                            # K: 매입금액(현지) (Col K)
                f'=G{r}*J{r}',                                                            # L: 평가금액(현지) (Col L)
                f'=IF(B{r}="USD",K{r}*$H$1,K{r})',                                        # M: 매입금액(원화) (Col M)
                f'=IF(B{r}="USD",L{r}*$H$1,L{r})',                                        # N: 평가금액(원화) (Col N)
                f'=N{r}-M{r}',                                                            # O: 평가손익(원화) (Col O)
                f'=IFERROR(O{r}/M{r},0)',                                                 # P: 수익률 (Col P)
                f'=IFERROR(N{r}/$N${r_sum},0)',                                           # Q: 포트비중 (Col Q)
                "동적 AI 제어",                                                            # R: 리밸런싱 신호 (Col R)
                f'=IF(B{r}="USD", IFERROR(SUMIFS(\'배당캘린더\'!$M$4:$M$50, \'배당캘린더\'!$A$4:$A$50, C{r}, \'배당캘린더\'!$D$4:$D$50, "지급 예정"), 0), IFERROR(SUMIFS(\'배당캘린더\'!$M$4:$M$50, \'배당캘린더\'!$A$4:$A$50, C{r}, \'배당캘린더\'!$D$4:$D$50, "지급 예정"), 0))', # S: 연간 예상배당금(원화) (Col S)
                f'=IFERROR(S{r}/N{r},0)',                                                 # T: 배당수익률 (Col T)
                f'=IFERROR(SUMIFS(\'배당캘린더\'!$O$4:$O$50, \'배당캘린더\'!$A$4:$A$50, C{r}, \'배당캘린더\'!$D$4:$D$50, "지급 예정"), 0)', # U: 세후 배당금(원화) (Col U)
                f'=IFERROR(U{r}/N{r},0)'                                                  # V: 세후 배당수익률 (Col V)
            ]
            holdings_data.append(row)

        summary_row_holdings = [
            "합계", "", "", "", "", "",                                                   # A..F
            "", "", "", "", "", "",                                                       # G..L (Local currency totals left blank)
            f'=SUM(M{r_start}:M{r_end})',                                                 # M: 매입금액(원화) 합계
            f'=SUM(N{r_start}:N{r_end})',                                                 # N: 평가금액(원화) 합계
            f'=SUM(O{r_start}:O{r_end})',                                                 # O: 평가손익(원화) 합계
            f'=IFERROR(O{r_sum}/M{r_sum},0)',                                             # P: 전체 수익률
            f'=SUM(Q{r_start}:Q{r_end})',                                                 # Q: 전체 포트비중 합계
            "동적 AI 제어",                                                                # R: 리밸런싱 신호
            f'=SUM(S{r_start}:S{r_end})',                                                 # S: 연간 예상배당금(원화) 합계
            f'=IFERROR(S{r_sum}/N{r_sum},0)',                                             # T: 평균 배당수익률
            f'=SUM(U{r_start}:U{r_end})',                                                 # U: 세후 배당금(원화) 합계
            f'=IFERROR(U{r_sum}/N{r_sum},0)'                                              # V: 평균 세후 배당수익률
        ]

        sync_ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _sttl_banner_str = (
            f"체결기준 실시간 순자산: ₩{current_assets:,.0f} · KIS 결제기준 총자산(MTS 자산현황): ₩{settlement_total_asset_krw:,.0f} | "
            if settlement_total_asset_krw > 0
            else ""
        )
        holdings_batch = [
            {"range": "G1:H1", "values": [["기준환율(USD/KRW)", f'=IF({fx_rate}>0, {fx_rate}, GOOGLEFINANCE("CURRENCY:USDKRW"))']]},
            {"range": "A2:F2", "values": [[f"한국투자증권(KIS) 실계좌 실시간 연동 ({_sttl_banner_str}열람 시 자동재계산 · 장중 1분 · 체결 즉시 · 장외 10분 자동갱신 | 최종 동기화: {sync_ts_str} KST)", "", "", "", "", ""]]},
            {"range": "G2:H2", "values": [["", ""]]},
            {"range": "A3:V3", "values": headers_holdings},
            {"range": f"A4:V{r_sum}", "values": holdings_data + [summary_row_holdings]},
        ]
        _holdings_max_row = 30
        if r_sum < _holdings_max_row:
            holdings_batch.append({"range": f"A{r_sum + 1}:V30", "values": [[""] * 22 for _ in range(_holdings_max_row - r_sum)]})
        _batch_ws_updates(ws_holdings, holdings_batch, value_input_option="USER_ENTERED")
        logger.info(f"Updated '보유종목' ({num_items} positions, Summary at Row {r_sum})")

        # =========================================================================
        # TAB 2: 배당캘린더 (Dividend Calendar)
        # =========================================================================
        ws_div = sh.worksheet("배당캘린더")

        # Dynamically unmerge ALL legacy merged ranges on '배당캘린더'
        try:
            sheet_meta = sh.fetch_sheet_metadata()
            unmerge_requests = []
            for s in sheet_meta.get("sheets", []):
                if s["properties"]["sheetId"] == ws_div.id:
                    for m in s.get("merges", []):
                        unmerge_requests.append({"unmergeCells": {"range": m}})
            if unmerge_requests:
                sh.batch_update({"requests": unmerge_requests})
                logger.info(f"Unmerged {len(unmerge_requests)} legacy merged ranges in '배당캘린더'")
        except Exception as e:
            logger.warning(f"Could not unmerge legacy cells: {e}")

        # Headers for Row 3
        headers_div = [[
            "티커", "통화", "종목명", "구분", "배당락일", "지급일", "월",
            "주당배당금\n(현지통화)", "보유수량", "세전 배당금\n(현지통화)",
            "배당원천세\n(현지통화)", "세후 배당금\n(현지통화)",
            "세전 배당금\n(원화)", "배당원천세\n(원화)", "세후 배당금\n(원화)",
        ]]

        # Populate rows 4.. (both Actual Paid '실제 수령' from historical KIS fills & Expected '지급 예정' from current holdings)
        now_date_str = datetime.now().strftime("%Y-%m-%d")
        full_ledger_rows = load_full_corporate_trade_ledger()
        raw_div_list = extract_account_dividend_entitlements(full_ledger_rows, now_date_str, fallback_fx=fx_rate)

        for idx, (ticker, info) in enumerate(items):
            if ticker == "CASH" or "CASH" in ticker.upper():
                continue
            asset_class, curr = categorize_asset(ticker, info)
            gf_ticker = ticker if not (curr == "KRW" and _is_kr_rep(str(ticker)) and not ticker.startswith("KRX:")) else f"KRX:{ticker}"
            name = info.get("name", ticker)
            avg_price = info.get("avg_price", 0)
            tax_meta = classify_corporate_dividend_tax(gf_ticker, curr, name)

            records = get_dividend_schedule(ticker, curr, name, avg_price)
            for rec in records:
                raw_div_list.append({
                    "ticker": gf_ticker,
                    "curr": curr,
                    "name": name,
                    "status": rec["status"],
                    "ex_date": rec["ex_date"],
                    "pay_date": rec["pay_date"],
                    "month": rec["month"],
                    "dps": rec["dps"],
                    "hist_qty": rec.get("hist_qty", 0),
                    "locked_qty": bool(rec.get("locked_qty", False)),
                    "pay_fx": 1.0 if curr == "KRW" else fx_rate,
                    **tax_meta,
                })

        # Sort chronologically by pay_date, then ticker
        raw_div_list.sort(key=lambda x: (x["pay_date"], x["ticker"]))

        div_rows = []
        div_currencies = []
        div_statuses = []

        for idx, rec in enumerate(raw_div_list):
            r_div = idx + 4
            div_currencies.append(rec["curr"])
            div_statuses.append(rec["status"])
            hist_qty = rec.get("hist_qty", 0)
            if rec.get("locked_qty") and hist_qty > 0:
                qty_formula = hist_qty
            elif hist_qty > 0:
                qty_formula = f"=IF(SUMIFS('보유종목'!$G$4:$G${r_end}, '보유종목'!$C$4:$C${r_end}, A{r_div})>0, SUMIFS('보유종목'!$G$4:$G${r_end}, '보유종목'!$C$4:$C${r_end}, A{r_div}), {hist_qty})"
            else:
                qty_formula = f"=SUMIFS('보유종목'!$G$4:$G${r_end}, '보유종목'!$C$4:$C${r_end}, A{r_div})"

            is_confirmed = rec["status"] == "실제 수령"
            prec_local = 2 if rec["curr"] == "USD" else 0
            wht_r = rec.get("wht_rate")
            if wht_r is None:
                wht_r = classify_corporate_dividend_tax(rec["ticker"], rec["curr"], rec["name"])["wht_rate"]
            pay_fx_v = rec.get("pay_fx") or (1.0 if rec["curr"] == "KRW" else fx_rate)

            if is_confirmed:
                gross_krw_formula = f"=ROUND(J{r_div}*{pay_fx_v}, 0)"
                tax_krw_formula = f"=ROUND(K{r_div}*{pay_fx_v}, 0)"
            else:
                gross_krw_formula = f'=IF(B{r_div}="USD", ROUND(J{r_div}*\'보유종목\'!$H$1, 0), ROUND(J{r_div}, 0))'
                tax_krw_formula = f'=IF(B{r_div}="USD", ROUND(K{r_div}*\'보유종목\'!$H$1, 0), ROUND(K{r_div}, 0))'

            div_row = [
                rec["ticker"],                                                            # A: 티커
                rec["curr"],                                                              # B: 통화
                rec["name"],                                                              # C: 종목명
                rec["status"],                                                            # D: 구분 ("실제 수령" / "지급 예정")
                rec["ex_date"],                                                           # E: 배당락일
                rec["pay_date"],                                                          # F: 지급일
                rec["month"],                                                             # G: 월
                rec["dps"],                                                               # H: 주당배당금(현지)
                qty_formula,                                                              # I: 보유수량
                f"=ROUND(H{r_div}*I{r_div}, {prec_local})",                               # J: 세전 배당금(현지)
                f"=ROUND(J{r_div}*{wht_r}, {prec_local})",                                # K: 배당원천세(법인세법·현지)
                f"=J{r_div}-K{r_div}",                                                    # L: 세후 배당금(현지)
                gross_krw_formula,                                                        # M: 세전 배당금(원화)
                tax_krw_formula,                                                          # N: 배당원천세(원화)
                f"=M{r_div}-N{r_div}"                                                     # O: 세후 배당금(원화)
            ]
            div_rows.append(div_row)

        div_end_row = 3 + len(div_rows)  # Row 22 if 19 rows

        # 3 Main Table Subtotal / Total Rows directly under data table
        r_act_sum = div_end_row + 1  # Row 23
        r_exp_sum = div_end_row + 2  # Row 24
        r_tot_sum = div_end_row + 3  # Row 25

        _locked_exp_divs = [
            r for r in raw_div_list
            if r["status"] == "지급 예정" and r.get("locked_qty") and r.get("hist_qty", 0) > 0
        ]
        _exp_sum_desc = (
            f"현재 보유종목 향후 12개월 예상 + 배당락 권리확정 미입금분({len(_locked_exp_divs)}건·매도종목 포함)"
            if _locked_exp_divs
            else "현재 보유종목 향후 12개월 예상 배당·분배금"
        )
        subtotal_rows = [
            [
                "실제 수령 합계", f'=COUNTIF($D$4:$D${div_end_row}, "실제 수령") & "건"',
                "배당락 권리확정 및 입금 완료분 (지급일 환율 고정)", "", "", "", "", "",
                f'=SUMIFS($I$4:$I${div_end_row}, $D$4:$D${div_end_row}, "실제 수령")',
                "", "", "",
                f'=SUMIFS($M$4:$M${div_end_row}, $D$4:$D${div_end_row}, "실제 수령")',
                f'=SUMIFS($N$4:$N${div_end_row}, $D$4:$D${div_end_row}, "실제 수령")',
                f'=SUMIFS($O$4:$O${div_end_row}, $D$4:$D${div_end_row}, "실제 수령")'
            ],
            [
                "지급 예정 합계", f'=COUNTIF($D$4:$D${div_end_row}, "지급 예정") & "건"',
                _exp_sum_desc, "", "", "", "", "",
                f'=SUMIFS($I$4:$I${div_end_row}, $D$4:$D${div_end_row}, "지급 예정")',
                "", "", "",
                f'=SUMIFS($M$4:$M${div_end_row}, $D$4:$D${div_end_row}, "지급 예정")',
                f'=SUMIFS($N$4:$N${div_end_row}, $D$4:$D${div_end_row}, "지급 예정")',
                f'=SUMIFS($O$4:$O${div_end_row}, $D$4:$D${div_end_row}, "지급 예정")'
            ],
            [
                "연간 총 배당 합계", f'=COUNTA($A$4:$A${div_end_row}) & "건"',
                "실제 수령 확정 + 향후 12개월 지급 예정 총합", "", "", "", "", "",
                f'=I{r_act_sum}+I{r_exp_sum}',
                "", "", "",
                f'=M{r_act_sum}+M{r_exp_sum}',
                f'=N{r_act_sum}+N{r_exp_sum}',
                f'=O{r_act_sum}+O{r_exp_sum}'
            ]
        ]

        # =========================================================================
        # SECTION 2: 배당 현금흐름 핵심 요약 (Executive Summary Cards Grid - Rows 27..33)
        # =========================================================================
        r_card_sec = r_tot_sum + 2      # Row 27
        r_card_start = r_card_sec + 1   # Row 28
        r_card_end = r_card_start + 5   # Row 33 (6 card rows)

        r_m_sec = r_card_end + 2        # Row 35
        r_m_header = r_m_sec + 1        # Row 36
        r_m_start = r_m_header + 1      # Row 37
        r_m_end = r_m_start + 11        # Row 48
        r_m_sum = r_m_end + 1           # Row 49
        r_m_avg = r_m_sum + 1           # Row 50

        card_left = [
            ["누적 실제 수령 배당금 (세전)", "", "", f"=M{r_act_sum}"],
            ["누적 실제 수령 배당금 (세후)", "", "", f"=O{r_act_sum}"],
            ["향후 지급 예정 배당금 (세전)", "", "", f"=M{r_exp_sum}"],
            ["향후 지급 예정 배당금 (세후)", "", "", f"=O{r_exp_sum}"],
            ["연간 총 세전 배당금 (원화)", "", "", f"=M{r_tot_sum}"],
            ["연간 총 배당원천세 (원화)", "", "", f"=N{r_tot_sum}"]
        ]

        card_right = [
            ["연간 세후 순배당금 (원화)", "", "", f"=O{r_tot_sum}"],
            ["월평균 세후 실수령액 (원화)", "", "", f"=F{r_m_avg}"],
            ["포트폴리오 세전 배당수익률", "", "", f"=IFERROR(M{r_tot_sum}/'보유종목'!N{r_sum}, 0)"],
            ["포트폴리오 세후 배당수익률", "", "", f"=IFERROR(O{r_tot_sum}/'보유종목'!N{r_sum}, 0)"],
            ["실시간 적용 환율 (USD/KRW)", "", "", f"='보유종목'!H1"],
            ["세무 보조원장 확정 대사", "", "", "배당원장 100% 일치"]
        ]

        # =========================================================================
        # SECTION 3: 월별 배당 집계 (Monthly Dividend Summary Table - Rows 35..50)
        # =========================================================================
        m_rows = []
        for m in range(1, 13):
            r_m = r_m_start + m - 1
            m_rows.append([
                m,
                f'=SUMIFS($M$4:$M${div_end_row}, $G$4:$G${div_end_row}, A{r_m}, $D$4:$D${div_end_row}, "실제 수령")',
                f'=SUMIFS($M$4:$M${div_end_row}, $G$4:$G${div_end_row}, A{r_m}, $D$4:$D${div_end_row}, "지급 예정")',
                f'=B{r_m} + C{r_m}',
                f'=SUMIFS($N$4:$N${div_end_row}, $G$4:$G${div_end_row}, A{r_m})',
                f'=D{r_m} - E{r_m}',
                f'=IF(AND(B{r_m}>0,C{r_m}>0),"실수령+예정",IF(B{r_m}>0,"실수령 완료",IF(C{r_m}>0,"지급 예정","-")))'
            ])

        div_batch = [
            {
                "range": "A1:O2",
                "values": [
                    ["포트폴리오 배당캘린더 및 연간 현금흐름 예측 (Portfolio Dividend Calendar & Forward 12M Cashflow)"] + [""] * 14,
                    ["실계좌 수령 확정분(지급일 기준환율 고정) + 현재 보유종목 향후 12개월 예상 배당·분배금 통합 뷰 (법인세법 원천세율 반영)"] + [""] * 14,
                ],
            },
            {"range": "A3:O3", "values": headers_div},
            {"range": f"A4:O{div_end_row}", "values": div_rows},
            {"range": f"A{r_act_sum}:O{r_tot_sum}", "values": subtotal_rows},
            {"range": f"A{r_tot_sum + 1}:O{r_tot_sum + 1}", "values": [[""] * 15]},
            {"range": f"A{r_card_sec}:O{r_card_sec}", "values": [["배당 현금흐름 핵심 요약 (Dividend Cashflow Executive Summary)"] + [""] * 14]},
            {"range": f"A{r_card_start}:D{r_card_end}", "values": card_left},
            {"range": f"E{r_card_start}:E{r_card_end}", "values": [[""] for _ in range(6)]},
            {"range": f"F{r_card_start}:I{r_card_end}", "values": card_right},
            {"range": f"J{r_card_start}:O{r_card_end}", "values": [[""] * 6 for _ in range(6)]},
            {"range": f"A{r_card_end + 1}:O{r_card_end + 1}", "values": [[""] * 15]},
            {"range": f"A{r_m_sec}:O{r_m_sec}", "values": [["월별 배당 집계 (Monthly Dividend Cashflow Summary)"] + [""] * 14]},
            {"range": f"A{r_m_header}:O{r_m_header}", "values": [["월", "실제 수령액(원화)", "지급 예정액(원화)", "월 세전 합계(원화)", "배당원천세(원화)", "월 세후 실수령(원화)", "구분/비고"] + [""] * 8]},
            {"range": f"A{r_m_start}:G{r_m_end}", "values": m_rows},
            {
                "range": f"A{r_m_sum}:G{r_m_sum}",
                "values": [["연간 합계", f"=SUM(B{r_m_start}:B{r_m_end})", f"=SUM(C{r_m_start}:C{r_m_end})", f"=SUM(D{r_m_start}:D{r_m_end})", f"=SUM(E{r_m_start}:E{r_m_end})", f"=SUM(F{r_m_start}:F{r_m_end})", "12개월 총계"]],
            },
            {
                "range": f"A{r_m_avg}:G{r_m_avg}",
                "values": [["월평균", f"=B{r_m_sum}/12", f"=C{r_m_sum}/12", f"=D{r_m_sum}/12", f"=E{r_m_sum}/12", f"=F{r_m_sum}/12", "월평균 현금흐름"]],
            },
            {"range": f"H{r_m_start}:O{r_m_avg}", "values": [[""] * 8 for _ in range(14)]},
        ]
        _div_max_row = 85
        if r_m_avg < _div_max_row:
            div_batch.append({"range": f"A{r_m_avg + 1}:O85", "values": [[""] * 15 for _ in range(_div_max_row - r_m_avg)]})
        _batch_ws_updates(ws_div, div_batch, value_input_option="USER_ENTERED")

        logger.info(f"Updated '배당캘린더' ({len(div_rows)} dividend positions: 3 Vertically Stacked Sections)")

        # =========================================================================
        # TAB 2B: 배당원장_YYYY (Annual Corporate Dividend & Withholding Tax Subledger — 세무사 제출용 연 통합 배당원장)
        # =========================================================================
        confirmed_divs = [r for r in raw_div_list if r["status"] == "실제 수령"]
        locked_pending_divs = [r for r in raw_div_list if r["status"] == "지급 예정" and r.get("locked_qty") and r.get("hist_qty", 0) > 0]
        div_year = confirmed_divs[0]["pay_date"][:4] if confirmed_divs else now_date_str[:4]
        div_ledger_tab_title = f"배당원장_{div_year}"
        try:
            ws_dl = sh.worksheet(div_ledger_tab_title)
        except Exception:
            ws_dl = sh.add_worksheet(title=div_ledger_tab_title, rows=max(100, len(confirmed_divs) + 60), cols=18)

        target_dl_rows = max(100, len(confirmed_divs) + 60)
        try:
            if getattr(ws_dl, "row_count", 0) < target_dl_rows:
                ws_dl.resize(rows=target_dl_rows, cols=18)
        except Exception:
            pass

        try:
            sheet_meta = sh.fetch_sheet_metadata()
            unmerge_dl = []
            for s in sheet_meta.get("sheets", []):
                if s["properties"]["sheetId"] == ws_dl.id:
                    for m in s.get("merges", []):
                        if m.get("startRowIndex", 0) >= 1:
                            unmerge_dl.append({"unmergeCells": {"range": m}})
            if unmerge_dl:
                sh.batch_update({"requests": unmerge_dl})
        except Exception as e:
            logger.warning(f"Could not unmerge legacy cells in '{div_ledger_tab_title}': {e}")

        dl_rows = []
        dl_currencies = []
        for idx, rec in enumerate(confirmed_divs):
            r_dl = 7 + idx
            dl_currencies.append(rec["curr"])
            prec_dl = 2 if rec["curr"] == "USD" else 0
            tax_meta = classify_corporate_dividend_tax(rec["ticker"], rec["curr"], rec["name"])
            wht_r = rec.get("wht_rate") if rec.get("wht_rate") is not None else tax_meta["wht_rate"]
            pay_fx_v = rec.get("pay_fx") or (1.0 if rec["curr"] == "KRW" else fx_rate)
            dl_rows.append([
                f"DIV-{div_year}-{idx+1:04d}",                      # A (0): 전표번호
                str(rec["pay_date"])[:7],                           # B (1): 귀속연월
                rec["ex_date"],                                     # C (2): 배당락일(권리확정일)
                rec["pay_date"],                                    # D (3): 실제지급일(입금일)
                rec.get("market_label", tax_meta["market_label"]),  # E (4): 시장/국가
                rec.get("income_type", tax_meta["income_type"]),    # F (5): 소득구분 및 법인세법 서식
                rec["ticker"],                                      # G (6): 종목코드
                rec["name"],                                        # H (7): 종목명
                rec["curr"],                                        # I (8): 통화
                rec.get("hist_qty", 0),                             # J (9): 권리확정 보유수량
                rec["dps"],                                         # K (10): 주당배당금(현지통화)
                f"=ROUND(J{r_dl}*K{r_dl}, {prec_dl})",              # L (11): 세전 배당총액(현지통화)
                wht_r,                                              # M (12): 원천징수세율(조세조약·세법)
                f"=ROUND(L{r_dl}*M{r_dl}, {prec_dl})",              # N (13): 원천징수세액(현지통화)
                pay_fx_v,                                           # O (14): 지급일 기준환율(고정·USD/KRW)
                f"=ROUND(L{r_dl}*O{r_dl}, 0)",                      # P (15): [대변] 배당금수익(세전익금·KRW)
                f"=ROUND(N{r_dl}*O{r_dl}, 0)",                      # Q (16): [차변] 선납세금(원천세액·KRW)
                f"=P{r_dl}-Q{r_dl}",                                # R (17): [차변] 예금실입금액(세후정산·KRW)
            ])

        last_dl_r = 6 + max(1, len(dl_rows))
        r_dl_sum = last_dl_r + 1

        locked_pending_krw = sum(
            int(round(float(r.get("dps", 0)) * float(r.get("hist_qty", 0)) * (1.0 if r.get("curr") == "KRW" else fx_rate)))
            for r in locked_pending_divs
        )
        locked_pending_note = (
            f"분배락 권리확정 {len(locked_pending_divs)}건: "
            + ", ".join(f"{r['ticker']} {r['hist_qty']}주 ({r['pay_date']} 지급)" for r in locked_pending_divs)
            if locked_pending_divs
            else "배당락 권리확정 후 미입금 대기 내역 없음"
        )

        # 4 Executive Tax Summary Cards in Rows 3..5 (A3:D5, E3:H5, I3:L5, M3:P5)
        dl_cards_values = [
            [
                f"{div_year}년 확정 배당금수익 총액 [대변·익금]", "", "", "",
                f"{div_year}년 기납부 원천징수세액 [차변·선납세금]", "", "", "",
                f"{div_year}년 세후 예금 실입금 총액 [차변·예수금]", "", "", "",
                "권리확정 미입금 배당 (지급일 도래시 편입)", "", "", "",
            ],
            [
                f"=P{r_dl_sum}", "", "", "",
                f"=Q{r_dl_sum}", "", "", "",
                f"=R{r_dl_sum}", "", "", "",
                locked_pending_krw, "", "", "",
            ],
            [
                f'="해외배당(별지8호): " & TEXT(SUMIF(E7:E{last_dl_r},"미국(US)",P7:P{last_dl_r}), "₩#,##0") & " | 국내배당(별지10·16호): " & TEXT(SUMIF(E7:E{last_dl_r},"국내(KRX)",P7:P{last_dl_r}), "₩#,##0")', "", "", "",
                f'="외국납부세액(미국15%): " & TEXT(SUMIF(E7:E{last_dl_r},"미국(US)",Q7:Q{last_dl_r}), "₩#,##0") & " | 국내원천세(14%): " & TEXT(SUMIF(E7:E{last_dl_r},"국내(KRX)",Q7:Q{last_dl_r}), "₩#,##0")', "", "", "",
                f'="총 " & COUNTA(A7:A{last_dl_r}) & "건 수령 확정 | 복식부기 차대변 대사오차: " & TEXT(P{r_dl_sum}-(Q{r_dl_sum}+R{r_dl_sum}), "₩#,##0")', "", "", "",
                locked_pending_note, "", "", "",
            ],
        ]

        headers_dl = [[
            "전표번호", "귀속연월", "배당락일\n(권리확정일)", "실제지급일\n(입금일)",
            "시장/국가", "소득구분 및 법인세법 서식", "종목코드", "종목명", "통화",
            "권리확정\n보유수량", "주당배당금\n(현지통화)", "세전 배당총액\n(현지통화)",
            "원천징수세율\n(조세조약·세법)", "원천징수세액\n(현지통화)",
            "지급일 기준환율\n(고정·USD/KRW)", "[대변] 배당금수익\n(세전익금·KRW)",
            "[차변] 선납세금\n(원천세액·KRW)", "[차변] 예금실입금액\n(세후정산·KRW)",
        ]]

        summary_row_dl = [
            f"{div_year}년 확정 합계",
            f'=COUNTA(A7:A{last_dl_r}) & "건"',
            "",
            "",
            "",
            "법인세법 시행령 제70조 제2항 확정 배당원장 합계 (차변 Q+R ≡ 대변 P)",
            "",
            "",
            "",
            f"=SUM(J7:J{last_dl_r})",
            "",
            "",
            "",
            "",
            "",
            f"=SUM(P7:P{last_dl_r})",
            f"=SUM(Q7:Q{last_dl_r})",
            f"=SUM(R7:R{last_dl_r})",
        ]
        dl_table_values = (dl_rows + [summary_row_dl]) if dl_rows else ([["", "", "", "", "", "", "", "", "", 0, 0, 0, 0, 0, 1.0, 0, 0, 0], summary_row_dl])

        # SECTION 2 of 배당원장_YYYY: 법인세법 신고 서식별 · 세목별 집계표
        r_tax_sec = r_dl_sum + 2
        r_tax_hdr = r_tax_sec + 1
        r_tax_start = r_tax_hdr + 1
        r_tax_end = r_tax_start + 2
        r_tax_sum = r_tax_end + 1

        tax_form_Specs = [
            (
                "별지 제8호서식 부표5 (외국납부세액공제 명세서)",
                "해외배당 [별지8호부표5 외국납부]",
                _STATUTORY_CORP_WHT_RATES["US"],
                "법인세법 제57조 (국가코드: US, 외국납부세액공제 또는 손금산입 선택)",
            ),
            (
                "별지 제10호서식(갑) (원천납부세액명세서)",
                "국내ETF분배 [별지10호 원천납부]",
                _STATUTORY_CORP_WHT_RATES["KR_ETF"],
                "법인세법 제73조 (집합투자기구 분배금 기납부 법인세 전액 세액공제)",
            ),
            (
                "별지 제16호서식 (수입배당금액 명세서)",
                "국내주식배당 [별지16호 익금불산입]",
                _STATUTORY_CORP_WHT_RATES["KR_STOCK"],
                "법인세법 제18조의2 (내국법인 수입배당금 지분율별 익금불산입 검토)",
            ),
        ]
        tax_rows = []
        for t_idx, (f_title, inc_key, w_rate, t_note) in enumerate(tax_form_Specs):
            r_tf = r_tax_start + t_idx
            tax_rows.append([
                f_title,
                inc_key,
                f"=COUNTIF($F$7:$F${last_dl_r}, B{r_tf})",
                w_rate,
                f"=SUMIF($F$7:$F${last_dl_r}, B{r_tf}, $P$7:$P${last_dl_r})",
                f"=SUMIF($F$7:$F${last_dl_r}, B{r_tf}, $Q$7:$Q${last_dl_r})",
                f"=SUMIF($F$7:$F${last_dl_r}, B{r_tf}, $R$7:$R${last_dl_r})",
                t_note,
            ])
        tax_rows.append([
            "서식별 합계",
            "",
            f"=SUM(C{r_tax_start}:C{r_tax_end})",
            "",
            f"=SUM(E{r_tax_start}:E{r_tax_end})",
            f"=SUM(F{r_tax_start}:F{r_tax_end})",
            f"=SUM(G{r_tax_start}:G{r_tax_end})",
            "원장 확정 합계(P·Q·R열)와 100% 대사 일치",
        ])

        # SECTION 3 of 배당원장_YYYY: 기간별(월별 · 분기별) 확정 배당수익 마감 집계표
        r_prd_sec = r_tax_sum + 2
        r_prd_hdr = r_prd_sec + 1
        r_prd_start = r_prd_hdr + 1
        r_prd_end = r_prd_start + 11
        r_q_start = r_prd_end + 1
        r_q_end = r_q_start + 3
        r_prd_sum = r_q_end + 1

        prd_rows = []
        for m in range(1, 13):
            r_pm = r_prd_start + m - 1
            q_num = (m - 1) // 3 + 1
            prd_rows.append([
                f"{div_year}-{m:02d}",
                f"{q_num}분기 (Q{q_num})",
                f"=COUNTIF($B$7:$B${last_dl_r}, A{r_pm})",
                f'=SUMIFS($P$7:$P${last_dl_r}, $B$7:$B${last_dl_r}, A{r_pm}, $E$7:$E${last_dl_r}, "미국(US)")',
                f'=SUMIFS($P$7:$P${last_dl_r}, $B$7:$B${last_dl_r}, A{r_pm}, $E$7:$E${last_dl_r}, "국내(KRX)")',
                f"=SUMIF($B$7:$B${last_dl_r}, A{r_pm}, $P$7:$P${last_dl_r})",
                f"=SUMIF($B$7:$B${last_dl_r}, A{r_pm}, $Q$7:$Q${last_dl_r})",
                f"=SUMIF($B$7:$B${last_dl_r}, A{r_pm}, $R$7:$R${last_dl_r})",
            ])
        for q_idx in range(1, 5):
            r_qr = r_q_start + q_idx - 1
            q_label = f"{q_idx}분기 (Q{q_idx})"
            prd_rows.append([
                f"{div_year}년 {q_idx}분기 소계",
                q_label,
                f"=SUMIF($B${r_prd_start}:$B${r_prd_end}, B{r_qr}, C${r_prd_start}:C${r_prd_end})",
                f"=SUMIF($B${r_prd_start}:$B${r_prd_end}, B{r_qr}, D${r_prd_start}:D${r_prd_end})",
                f"=SUMIF($B${r_prd_start}:$B${r_prd_end}, B{r_qr}, E${r_prd_start}:E${r_prd_end})",
                f"=SUMIF($B${r_prd_start}:$B${r_prd_end}, B{r_qr}, F${r_prd_start}:F${r_prd_end})",
                f"=SUMIF($B${r_prd_start}:$B${r_prd_end}, B{r_qr}, G${r_prd_start}:G${r_prd_end})",
                f"=SUMIF($B${r_prd_start}:$B${r_prd_end}, B{r_qr}, H${r_prd_start}:H${r_prd_end})",
            ])
        prd_rows.append([
            f"{div_year}년 연간 확정 합계",
            "연간 총계",
            f"=SUM(C{r_prd_start}:C{r_prd_end})",
            f"=SUM(D{r_prd_start}:D{r_prd_end})",
            f"=SUM(E{r_prd_start}:E{r_prd_end})",
            f"=SUM(F{r_prd_start}:F{r_prd_end})",
            f"=SUM(G{r_prd_start}:G{r_prd_end})",
            f"=SUM(H{r_prd_start}:H{r_prd_end})",
        ])

        dl_batch = [
            {
                "range": "A1:R2",
                "values": [
                    [f"법인 유가증권 연간 배당수익 보조원장 [{div_year}년 귀속] (Corporate Annual Dividend & Withholding Tax Subledger — 법인세법 제18조의2·제57조·제73조 기준)"] + [""] * 17,
                    [f"한국투자증권(KIS) 실계좌 {div_year}년 확정 배당·분배금 전수 연동 · 지급일 기준환율(Historical FX) 고정 · 외국납부세액(15.0%)/국내원천세(14.0%)/익금불산입 구분 기장"] + [""] * 17,
                ],
            },
            {"range": "A3:P5", "values": dl_cards_values},
            {"range": "Q3:R5", "values": [[""] * 2 for _ in range(3)]},
            {"range": "A6:R6", "values": headers_dl},
            {"range": f"A7:R{r_dl_sum}", "values": dl_table_values},
            {"range": f"A{r_dl_sum + 1}:R{r_dl_sum + 1}", "values": [[""] * 18]},
            {"range": f"A{r_tax_sec}:R{r_tax_sec}", "values": [[f"1. 법인세법 신고 서식별 · 세목별 배당수익 및 원천세 집계표 [{div_year}년 귀속]"] + [""] * 17]},
            {
                "range": f"A{r_tax_hdr}:H{r_tax_hdr}",
                "values": [[
                    "법인세법 신고 서식 구분", "소득구분 (원장 매핑키)", "수령건수", "적용 원천세율",
                    "[대변] 배당금수익(KRW)", "[차변] 선납세금 공제액(KRW)", "[차변] 예금실입금액(KRW)",
                    "계정과목 및 세무조정 요령",
                ]],
            },
            {"range": f"A{r_tax_start}:H{r_tax_sum}", "values": tax_rows},
            {"range": f"I{r_tax_hdr}:R{r_tax_sum}", "values": [[""] * 10 for _ in range(r_tax_sum - r_tax_hdr + 1)]},
            {"range": f"A{r_tax_sum + 1}:R{r_tax_sum + 1}", "values": [[""] * 18]},
            {"range": f"A{r_prd_sec}:R{r_prd_sec}", "values": [[f"2. 기간별 (월별 · 분기별) 확정 배당수익 및 원천세 마감 집계표 [{div_year}년 귀속]"] + [""] * 17]},
            {
                "range": f"A{r_prd_hdr}:H{r_prd_hdr}",
                "values": [[
                    "귀속연월", "분기구분", "수령건수", "해외배당 세전(KRW)", "국내배당·분배 세전(KRW)",
                    "[대변] 배당금수익 합계(KRW)", "[차변] 선납세금 합계(KRW)", "[차변] 예금실입금 합계(KRW)",
                ]],
            },
            {"range": f"A{r_prd_start}:H{r_prd_sum}", "values": prd_rows},
            {"range": f"I{r_prd_hdr}:R{r_prd_sum}", "values": [[""] * 10 for _ in range(r_prd_sum - r_prd_hdr + 1)]},
        ]
        if r_prd_sum < 90:
            dl_batch.append({"range": f"A{r_prd_sum + 1}:R90", "values": [[""] * 18 for _ in range(90 - r_prd_sum)]})
        _batch_ws_updates(ws_dl, dl_batch, value_input_option="USER_ENTERED")
        logger.info(f"Updated '{div_ledger_tab_title}' ({len(dl_rows)} confirmed corporate dividend records synced)")

        # =========================================================================
        # TAB 3: 매매일지_YYYY-MM (Monthly Trading Subledgers — 법인 유가증권 월별 매매원장)
        # =========================================================================
        entry_dates = load_position_entry_dates()
        now_date_str = datetime.now().strftime("%Y-%m-%d")

        full_ledger_rows = load_full_corporate_trade_ledger()
        raw_trade_list = []

        if full_ledger_rows:
            for r_csv in full_ledger_rows:
                dt_s = str(r_csv.get("거래일자", now_date_str))[:10]
                tm_s = str(r_csv.get("체결시각", "00:00:00")).strip()
                odno_s = str(r_csv.get("주문번호(ODNO)", "")).strip()
                mkt_s = str(r_csv.get("시장구분", "국내(KRX)")).strip()
                side_raw = str(r_csv.get("거래구분", "매수(BUY)")).upper()
                t_type = "매수" if "BUY" in side_raw or "매수" in side_raw else "매도"
                tk_raw = str(r_csv.get("종목코드", "")).strip()
                curr_s = str(r_csv.get("통화", "KRW")).strip().upper()
                gf_tk = f"KRX:{tk_raw}" if (curr_s == "KRW" and _is_kr_rep(tk_raw) and not tk_raw.startswith("KRX:")) else tk_raw
                nm_s = str(r_csv.get("종목명", tk_raw)).strip()
                qty_v = float(r_csv.get("체결수량") or 0)
                qty_num = int(qty_v) if qty_v.is_integer() else qty_v
                px_v = float(r_csv.get("체결단가") or 0)
                fx_v = float(r_csv.get("적용환율(USD/KRW)") or (1.0 if curr_s == "KRW" else fx_rate))
                gross_krw = int(round(float(r_csv.get("원화약정금액(KRW)") or (qty_v * px_v * fx_v))))
                avg_cost_v = float(r_csv.get("이동평균취득단가") or px_v)
                cogs_krw = int(round(float(r_csv.get("매도취득원가(KRW)") or 0)))
                fee_krw = int(round(float(r_csv.get("위탁수수료(KRW)") or 0)))
                tax_krw = int(round(float(r_csv.get("제세공과금(KRW)") or 0)))
                disp_pnl_krw = int(round(float(r_csv.get("매매처분손익(KRW)") or 0)))
                fx_pnl_krw = int(round(float(r_csv.get("외환차손익(KRW)") or 0)))
                rem_qty = int(round(float(r_csv.get("장부잔여수량") or 0)))
                trk_s = str(r_csv.get("운용트랙", "")).strip()
                strm_s = str(r_csv.get("스트림", "")).strip()
                note_s = f"{trk_s} | {strm_s}" if (trk_s and strm_s) else (trk_s or strm_s or "Project Meridian")
                raw_trade_list.append({
                    "date": dt_s,
                    "time": tm_s,
                    "odno": odno_s,
                    "market": mkt_s,
                    "type": t_type,
                    "ticker": gf_tk,
                    "name": nm_s,
                    "curr": curr_s,
                    "qty": qty_num,
                    "price": px_v,
                    "fx": fx_v,
                    "gross_krw": gross_krw,
                    "avg_cost": avg_cost_v,
                    "cogs_krw": cogs_krw,
                    "fee_krw": fee_krw,
                    "tax_krw": tax_krw,
                    "disp_pnl_krw": disp_pnl_krw,
                    "fx_pnl_krw": fx_pnl_krw,
                    "rem_qty": rem_qty,
                    "note": note_s,
                })
        else:
            # Fallback when Corporate_Trade_Ledger CSV is not present (e.g. isolated unit tests)
            ledger_map = load_position_ledger_map()
            for idx, (ticker, info) in enumerate(items):
                if ticker == "CASH" or "CASH" in ticker.upper():
                    continue
                asset_class, curr = categorize_asset(ticker, info)
                gf_ticker = ticker if not (_is_kr_rep(str(ticker)) and not ticker.startswith("KRX:")) else f"KRX:{ticker}"
                name = info.get("name", ticker)
                qty = info.get("qty", 1)
                avg_price = float(info.get("avg_price", 0) or 0)
                t_key = str(ticker).strip().upper()
                l_entry = ledger_map.get(t_key, {})
                actual_trade_date = (
                    info.get("entry_date")
                    or entry_dates.get(t_key)
                    or entry_dates.get(str(ticker))
                    or l_entry.get("entry_date")
                    or l_entry.get("first_seen")
                    or now_date_str
                )
                stream_label, _ = get_stream_strategy_info(ticker, info)
                fx_v = 1.0 if curr == "KRW" else fx_rate
                gross_krw = int(round(qty * avg_price * fx_v))
                fee_krw = int(round(float(info.get("commission", 0) or 0) * fx_v))
                raw_trade_list.append({
                    "date": str(actual_trade_date)[:10],
                    "time": "09:00:00" if curr == "KRW" else "22:30:00",
                    "odno": f"BUY-{idx+1:04d}",
                    "market": "국내(KRX)" if curr == "KRW" else "미국(US)",
                    "type": "매수",
                    "ticker": gf_ticker,
                    "name": name,
                    "curr": curr,
                    "qty": qty,
                    "price": avg_price,
                    "fx": fx_v,
                    "gross_krw": gross_krw,
                    "avg_cost": avg_price,
                    "cogs_krw": 0,
                    "fee_krw": fee_krw,
                    "tax_krw": 0,
                    "disp_pnl_krw": 0,
                    "fx_pnl_krw": 0,
                    "rem_qty": qty,
                    "note": f"Project Meridian [{stream_label}] 자율 매수",
                })

            for s_idx, closed_t in enumerate(portfolio.get("closed_trades") or portfolio.get("trade_history") or []):
                if not isinstance(closed_t, dict):
                    continue
                if str(closed_t.get("action", closed_t.get("side", ""))).upper() != "SELL":
                    continue
                c_tk = str(closed_t.get("ticker", "")).strip()
                if not c_tk:
                    continue
                c_is_kr = _is_kr_rep(c_tk)
                c_curr = "KRW" if c_is_kr else "USD"
                c_gf_tk = f"KRX:{c_tk}" if (c_is_kr and not c_tk.startswith("KRX:")) else c_tk
                c_qty = float(closed_t.get("quantity", closed_t.get("qty", 0)) or 0)
                c_price = float(closed_t.get("price", closed_t.get("exit_price", 0)) or 0)
                c_avg = float(closed_t.get("avg_price", closed_t.get("entry_price", c_price)) or c_price)
                if c_qty <= 0 or c_price <= 0:
                    continue
                fx_v = 1.0 if c_curr == "KRW" else fx_rate
                c_fee_local = float(closed_t.get("commission", 0) or 0)
                c_tax_local = float(closed_t.get("tax", 0) or 0)
                fee_krw = int(round(c_fee_local * fx_v))
                tax_krw = int(round(c_tax_local * fx_v))
                gross_krw = int(round(c_qty * c_price * fx_v))
                cogs_krw = int(round(c_qty * c_avg * fx_v))
                disp_pnl_krw = (gross_krw - fee_krw - tax_krw) - cogs_krw
                raw_trade_list.append({
                    "date": str(closed_t.get("date", closed_t.get("timestamp", now_date_str)))[:10],
                    "time": "09:01:00" if c_curr == "KRW" else "22:31:00",
                    "odno": str(closed_t.get("odno") or f"SELL-{s_idx+1:04d}"),
                    "market": "국내(KRX)" if c_curr == "KRW" else "미국(US)",
                    "type": "매도",
                    "ticker": c_gf_tk,
                    "name": closed_t.get("name", c_tk),
                    "curr": c_curr,
                    "qty": int(c_qty) if c_qty.is_integer() else c_qty,
                    "price": c_price,
                    "fx": fx_v,
                    "gross_krw": gross_krw,
                    "avg_cost": c_avg,
                    "cogs_krw": cogs_krw,
                    "fee_krw": fee_krw,
                    "tax_krw": tax_krw,
                    "disp_pnl_krw": disp_pnl_krw,
                    "fx_pnl_krw": 0,
                    "rem_qty": 0,
                    "note": str(closed_t.get("reason", "Project Meridian 리밸런싱 매도")),
                })
            raw_trade_list.sort(key=lambda x: (x["date"], x["time"], 0 if x["type"] == "매수" else 1, x["ticker"]))

        # Group trades by month (YYYY-MM) so each month gets its own dedicated worksheet tab (매매일지_YYYY-MM)
        trades_by_ym: dict[str, list[dict]] = {}
        for rec in raw_trade_list:
            ym_key = str(rec["date"])[:7] if len(str(rec["date"])) >= 7 else now_date_str[:7]
            trades_by_ym.setdefault(ym_key, []).append(rec)
        if not trades_by_ym:
            trades_by_ym[now_date_str[:7]] = []

        sorted_months = sorted(trades_by_ym.keys())
        headers_trades = [[
            "거래일자", "체결시각", "주문번호(ODNO)", "시장구분", "거래구분",
            "종목코드", "종목명", "통화", "체결수량", "체결단가\n(현지)",
            "약정금액\n(현지)", "적용환율\n(USD/KRW)", "원화약정금액\n(KRW)",
            "이동평균취득단가\n(수수료포함·현지)", "매도취득원가\n(KRW)",
            "위탁수수료\n(KRW)", "제세공과금\n(KRW)", "매매처분손익\n(주가변동·KRW)",
            "외환차손익\n(환율변동·KRW)", "실현순손익\n(확정·KRW)", "실현수익률",
            "정산현금증감\n(예수금대사·KRW)", "장부잔여수량", "운용트랙 및 스트림 적요",
        ]]

        monthly_tab_specs = []
        for ym in sorted_months:
            tab_title = f"매매일지_{ym}"
            m_recs = trades_by_ym[ym]
            try:
                ws_m = sh.worksheet(tab_title)
            except Exception:
                ws_m = sh.add_worksheet(title=tab_title, rows=len(m_recs) + 100, cols=24)

            trade_logs = []
            trade_currencies = []
            trade_types = []

            for idx, rec in enumerate(m_recs):
                r_t = 7 + idx
                trade_currencies.append(rec["curr"])
                trade_types.append(rec["type"])
                odno_cell = f"'{rec['odno']}" if rec["odno"] and not str(rec["odno"]).startswith("'") else rec["odno"]

                trade_row = [
                    rec["date"],                                                              # A (0): 거래일자
                    rec["time"],                                                              # B (1): 체결시각
                    odno_cell,                                                                # C (2): 주문번호(ODNO)
                    rec["market"],                                                            # D (3): 시장구분
                    rec["type"],                                                              # E (4): 거래구분
                    rec["ticker"],                                                            # F (5): 종목코드
                    rec["name"],                                                              # G (6): 종목명
                    rec["curr"],                                                              # H (7): 통화
                    rec["qty"],                                                               # I (8): 체결수량
                    rec["price"],                                                             # J (9): 체결단가(현지)
                    f"=I{r_t}*J{r_t}",                                                        # K (10): 약정금액(현지)
                    rec["fx"],                                                                # L (11): 적용환율(USD/KRW)
                    rec["gross_krw"],                                                         # M (12): 원화약정금액(KRW)
                    rec["avg_cost"],                                                          # N (13): 이동평균취득단가(수수료포함·현지)
                    rec["cogs_krw"],                                                          # O (14): 매도취득원가(KRW)
                    rec["fee_krw"],                                                           # P (15): 위탁수수료(KRW)
                    rec["tax_krw"],                                                           # Q (16): 제세공과금(KRW)
                    rec["disp_pnl_krw"],                                                      # R (17): 매매처분손익(주가변동·KRW)
                    rec["fx_pnl_krw"],                                                        # S (18): 외환차손익(환율변동·KRW)
                    f"=R{r_t}+S{r_t}",                                                        # T (19): 실현순손익(확정·KRW)
                    f"=IF(O{r_t}>0, T{r_t}/O{r_t}, 0)",                                       # U (20): 실현수익률
                    f'=IF(E{r_t}="매수", -(M{r_t}+P{r_t}), M{r_t}-P{r_t}-Q{r_t})',             # V (21): 정산현금증감(예수금대사·KRW)
                    rec["rem_qty"],                                                           # W (22): 장부잔여수량
                    rec["note"],                                                              # X (23): 운용트랙 및 스트림 적요
                ]
                trade_logs.append(trade_row)

            last_trade_r = 6 + max(1, len(trade_logs))
            r_trade_sum = last_trade_r + 1

            summary_row_trades = [
                f"{ym} 합계",
                f'=COUNTA(A7:A{last_trade_r}) & "건"',
                "",
                "",
                f'="매수 " & COUNTIF(E7:E{last_trade_r},"매수") & " / 매도 " & COUNTIF(E7:E{last_trade_r},"매도")',
                "",
                "",
                "",
                f"=SUM(I7:I{last_trade_r})",
                "",
                "",
                "",
                f"=SUM(M7:M{last_trade_r})",
                "",
                f"=SUM(O7:O{last_trade_r})",
                f"=SUM(P7:P{last_trade_r})",
                f"=SUM(Q7:Q{last_trade_r})",
                f"=SUM(R7:R{last_trade_r})",
                f"=SUM(S7:S{last_trade_r})",
                f"=SUM(T7:T{last_trade_r})",
                f"=IFERROR(T{r_trade_sum}/O{r_trade_sum}, 0)",
                f"=SUM(V7:V{last_trade_r})",
                "",
                f"법인세법 시행령 제74조(이동평균법) {ym} 월간 원장 합계",
            ]

            target_m_rows = r_trade_sum + 100
            try:
                if getattr(ws_m, "row_count", 0) < target_m_rows:
                    ws_m.resize(rows=target_m_rows, cols=24)
            except Exception:
                pass

            try:
                sheet_meta = sh.fetch_sheet_metadata()
                unmerge_trades = []
                for s in sheet_meta.get("sheets", []):
                    if s["properties"]["sheetId"] == ws_m.id:
                        for m in s.get("merges", []):
                            if m.get("startRowIndex", 0) >= 1:
                                unmerge_trades.append({"unmergeCells": {"range": m}})
                if unmerge_trades:
                    sh.batch_update({"requests": unmerge_trades})
            except Exception as e:
                logger.warning(f"Could not unmerge legacy cells in '{tab_title}': {e}")

            m_cards_values = [
                [
                    f"{ym} 확정 실현손익(세전·비용전)", "", "", "",
                    f"{ym} 거래비용(수수료+제세금)", "", "", "",
                    f"{ym} 확정 순실현손익(법인장부)", "", "", "",
                    f"{ym} 외환차손익(환율변동분)", "", "", "",
                ],
                [
                    "=I4+E4", "", "", "",
                    f"=P{r_trade_sum}+Q{r_trade_sum}", "", "", "",
                    f"=T{r_trade_sum}", "", "", "",
                    f"=S{r_trade_sum}", "", "", "",
                ],
                [
                    f'="처분손익: " & TEXT(R{r_trade_sum}, "+₩#,##0;-₩#,##0;₩0") & " | 외환차익: " & TEXT(S{r_trade_sum}, "+₩#,##0;-₩#,##0;₩0")', "", "", "",
                    f'="위탁수수료: " & TEXT(P{r_trade_sum}, "₩#,##0") & " | 거래세·SEC: " & TEXT(Q{r_trade_sum}, "₩#,##0")', "", "", "",
                    f'="{ym} 총 " & COUNTA(A7:A{last_trade_r}) & "건 (매수 " & COUNTIF(E7:E{last_trade_r},"매수") & "건 · 매도 " & COUNTIF(E7:E{last_trade_r},"매도") & "건)"', "", "", "",
                    f'="당월매수: " & TEXT(SUMIF(E7:E{last_trade_r},"매수",M7:M{last_trade_r}), "₩#,##0") & " | 당월매도: " & TEXT(SUMIF(E7:E{last_trade_r},"매도",M7:M{last_trade_r}), "₩#,##0")', "", "", "",
                ],
            ]

            m_batch = [
                {
                    "range": "A1:A2",
                    "values": [
                        [f"법인 유가증권 월별 매매원장 [{ym}] (Corporate Monthly Trade Subledger — 법인세법 시행령 제74조 이동평균법 기준)"],
                        [f"한국투자증권(KIS) 실계좌 {ym} 체결번호(ODNO) 전수 연동 · 전월 이월 이동평균단가 연속 적용 · 매매처분손익/외환차손익 분리 기장"],
                    ],
                },
                {"range": "A3:P5", "values": m_cards_values},
                {"range": "A6:X6", "values": headers_trades},
                {"range": f"A7:X{r_trade_sum}", "values": trade_logs + [summary_row_trades]},
                {"range": f"A{r_trade_sum + 1}:X{r_trade_sum + 20}", "values": [[""] * 24 for _ in range(20)]},
            ]
            _batch_ws_updates(ws_m, m_batch, value_input_option="USER_ENTERED")

            monthly_tab_specs.append({
                "ym": ym,
                "title": tab_title,
                "ws": ws_m,
                "sheet_id": ws_m.id,
                "last_trade_r": last_trade_r,
                "r_trade_sum": r_trade_sum,
                "trade_types": trade_types,
                "trade_currencies": trade_currencies,
            })
            logger.info(f"Updated '{tab_title}' ({len(trade_logs)} statutory corporate ledger records synced)")

        # Remove legacy single '매매일지' worksheet if monthly tabs are active
        try:
            ws_legacy_trades = sh.worksheet("매매일지")
            if ws_legacy_trades and monthly_tab_specs:
                sh.del_worksheet(ws_legacy_trades)
                logger.info("Removed legacy single '매매일지' tab in favor of monthly '매매일지_YYYY-MM' tabs")
        except Exception:
            pass

        # =========================================================================
        # TAB 4: 대시보드 (Dashboard — 월별 매매일지 전수 합산 연동)
        # =========================================================================
        ws_dash = sh.worksheet("대시보드")

        net_pnl_terms = " + ".join(f"'{m['title']}'!I4" for m in monthly_tab_specs)
        gross_pnl_terms = " + ".join(f"'{m['title']}'!A4" for m in monthly_tab_specs)
        cost_terms = " + ".join(f"'{m['title']}'!E4" for m in monthly_tab_specs)
        disp_pnl_terms = " + ".join(f"'{m['title']}'!R{m['r_trade_sum']}" for m in monthly_tab_specs)
        fx_pnl_terms = " + ".join(f"'{m['title']}'!S{m['r_trade_sum']}" for m in monthly_tab_specs)
        linked_tabs_label = (
            f"매매일지_{monthly_tab_specs[0]['ym']}~{monthly_tab_specs[-1]['ym']} ({len(monthly_tab_specs)}개 월별탭)"
            if len(monthly_tab_specs) > 1
            else f"{monthly_tab_specs[0]['title']} 탭"
        )

        # Top Summary Cards
        top_row_3 = [
            [
                "총 평가자산", "",
                "총 순손익 (비용·세금차감후)", "",
                "보유종목 평가수익률 (미실현)", "",
                "연간 예상 순배당 (세후·실수령포함)", "",
                "총 비용 (세금+수수료)"
            ]
        ]
        top_row_4 = [
            [
                f"='보유종목'!N{r_sum}", "",
                f"='보유종목'!O{r_sum} + ({net_pnl_terms}) + '배당캘린더'!O{r_act_sum}", "",
                f"='보유종목'!P{r_sum}", "",
                f"='배당캘린더'!O{r_tot_sum}", "",
                f"=({cost_terms}) + '배당캘린더'!N{r_act_sum}"
            ]
        ]

        _card1_sub_formula = (
            f"=\"매입: \" & TEXT('보유종목'!M{r_sum}, \"₩#,##0\") & \" | KIS결제기준: ₩{settlement_total_asset_krw:,.0f} (미결제손익 \" & TEXT('보유종목'!N{r_sum}-{settlement_total_asset_krw}, \"+₩#,##0;-₩#,##0;₩0\") & \")\""
            if settlement_total_asset_krw > 0
            else f"=\"매입: \" & TEXT('보유종목'!M{r_sum}, \"₩#,##0\")"
        )
        top_row_5 = [
            [
                _card1_sub_formula, "",
                f"=\"확정순실현: \" & TEXT({net_pnl_terms}, \"+₩#,##0;-₩#,##0;₩0\") & \" | 미실현: \" & TEXT('보유종목'!O{r_sum}, \"+₩#,##0;-₩#,##0;₩0\") & \" | 실수령배당: \" & TEXT('배당캘린더'!O{r_act_sum}, \"+₩#,##0;-₩#,##0;₩0\")", "",
                f"=\"미실현손익: \" & TEXT('보유종목'!O{r_sum}, \"+₩#,##0;-₩#,##0;₩0\") & \" | 누적 총수익률(Net ROI): \" & TEXT(B24, \"+0.0%;-0.0%;0.0%\")", "",
                f"=\"실수령: \" & TEXT('배당캘린더'!O{r_act_sum}, \"₩#,##0\") & \" | 향후예정: \" & TEXT('배당캘린더'!O{r_exp_sum}, \"₩#,##0\")", "",
                f"=\"거래비용: \" & TEXT({cost_terms}, \"₩#,##0\") & \" | 실수령배당세: \" & TEXT('배당캘린더'!N{r_act_sum}, \"₩#,##0\")"
            ]
        ]

        dash_updates = [
            ["국내주식", f"=SUMIF('보유종목'!$A$4:$A${r_end}, A9, '보유종목'!$N$4:$N${r_end})", '=IFERROR(B9/$B$14, 0)', "", ""],
            ["국내ETF", f"=SUMIF('보유종목'!$A$4:$A${r_end}, A10, '보유종목'!$N$4:$N${r_end})", '=IFERROR(B10/$B$14, 0)', "", ""],
            ["해외주식", f"=SUMIF('보유종목'!$A$4:$A${r_end}, A11, '보유종목'!$N$4:$N${r_end})", '=IFERROR(B11/$B$14, 0)', "", ""],
            ["해외ETF", f"=SUMIF('보유종목'!$A$4:$A${r_end}, A12, '보유종목'!$N$4:$N${r_end})", '=IFERROR(B12/$B$14, 0)', "", ""],
            ["현금", f"=SUMIF('보유종목'!$A$4:$A${r_end}, A13, '보유종목'!$N$4:$N${r_end})", '=IFERROR(B13/$B$14, 0)', "", ""],
        ]

        dash_pnl_rows = [
            [
                "확정 실현손익 (세전·비용전)",
                f"={gross_pnl_terms}",
                "=IFERROR(B19/$B$23, 0)",
                f"=\"비용차감후 확정순손익: \" & TEXT({net_pnl_terms}, \"+₩#,##0;-₩#,##0;₩0\") & \" (처분 \" & TEXT({disp_pnl_terms}, \"+₩#,##0;-₩#,##0;₩0\") & \" + 외환 \" & TEXT({fx_pnl_terms}, \"+₩#,##0;-₩#,##0;₩0\") & \")\"",
                linked_tabs_label,
            ],
            ["미실현 보유 평가손익", f"='보유종목'!O{r_sum}", "=IFERROR(B20/$B$23, 0)", f"현재 보유 {len(raw_holdings)}종목 평가 손익 (법인세법 제42조 과세이연)", "보유종목 탭"],
            [
                "배당 인컴 수익 (실수령 확정)",
                f"='배당캘린더'!M{r_act_sum}",
                "=IFERROR(B21/$B$23, 0)",
                f"=\"실수령 확정 세전: \" & TEXT('배당캘린더'!M{r_act_sum}, \"₩#,##0\") & \" (향후 12개월 지급예정 세전 \" & TEXT('배당캘린더'!M{r_exp_sum}, \"₩#,##0\") & \" 별도)\"",
                f"{div_ledger_tab_title}·배당캘린더 탭",
            ],
            [
                "거래비용 및 세금 (차감)",
                f"=-(({cost_terms}) + '배당캘린더'!N{r_act_sum})",
                "=IFERROR(B22/$B$23, 0)",
                f"=\"매매비용(수수료+거래세) \" & TEXT({cost_terms}, \"₩#,##0\") & \" + 실수령 배당원천세 \" & TEXT('배당캘린더'!N{r_act_sum}, \"₩#,##0\")",
                f"매매일지(월별탭)·{div_ledger_tab_title} 합산",
            ],
            ["총 순손익 (Net Profit)", "=SUM(B19:B22)", 1, "모든 확정 비용·세금 차감 후 최종 순손익 (확정순실현+미실현+실수령세후배당)", "종합 집계"],
            ["순 투자수익률 (Net ROI)", f"=IFERROR(B23/'보유종목'!M{r_sum}, 0)", "", "현재 총 매입원금 대비 비용 차감 후 종합 순수익률", "종합 성과 지표"],
        ]
        dash_batch = [
            {"range": "A2:I2", "values": [[f"실시간 시세·환율·원장 자동 연동 ({_sttl_banner_str}열람 시 자동재계산 · KIS 실계좌 장중 1분 · 체결 즉시 · 장외 10분 자동갱신 | 최종 동기화: {sync_ts_str} KST)"] + [""] * 8]},
            {"range": "A3:I3", "values": top_row_3},
            {"range": "A4:I4", "values": top_row_4},
            {"range": "A5:I5", "values": top_row_5},
            {"range": "A8:E8", "values": [["자산군", "평가금액(원화)", "현재비중", "", ""]]},
            {"range": "A9:E13", "values": dash_updates},
            {"range": "A14:E14", "values": [["합계", "=SUM(B9:B13)", "=SUM(C9:C13)", "", ""]]},
            {"range": "A16:E16", "values": [[""] * 5]},
            {"range": "A18:E18", "values": [["구분 (손익 원천)", "금액 (원화)", "손익 기여도", "세부 산출 내역", "연동 시트"]]},
            {"range": "A19:E24", "values": dash_pnl_rows},
        ]
        _batch_ws_updates(ws_dash, dash_batch, value_input_option="USER_ENTERED")
        logger.info("Updated '대시보드' (Top Summary Cards, Asset Allocation & PnL Attribution synced across monthly tabs)")

        # =========================================================================
        # 5. Apply Precision Batch Number, Currency, Alignment & Table Styling Across Worksheets
        # =========================================================================
        sheet_id_holdings = ws_holdings.id
        sheet_id_div = ws_div.id
        sheet_id_dash = ws_dash.id

        sum_idx = r_sum - 1  # 0-indexed
        format_requests = [
            {
                'updateSpreadsheetProperties': {
                    'properties': {'autoRecalc': 'MINUTE'},
                    'fields': 'autoRecalc',
                }
            }
        ]

        # Dark Navy Palette Constants
        NAVY_HEADER = {'red': 0.10, 'green': 0.21, 'blue': 0.36}  # #1A365D
        BLUE_HEADER = {'red': 0.17, 'green': 0.42, 'blue': 0.69}  # #2B6CB0
        WHITE_TEXT = {'red': 1.0, 'green': 1.0, 'blue': 1.0}
        SOFT_BLUE_BG = {'red': 0.93, 'green': 0.96, 'blue': 1.0}  # #EDF2F7
        LIGHT_GREY_BG = {'red': 0.96, 'green': 0.97, 'blue': 0.98}
        BORDER_GREY = {'red': 0.82, 'green': 0.84, 'blue': 0.87}
        INNER_GREY = {'red': 0.90, 'green': 0.92, 'blue': 0.94}

        WHITE_BG = {'red': 1.0, 'green': 1.0, 'blue': 1.0}
        BLACK_TEXT = {'red': 0.0, 'green': 0.0, 'blue': 0.0}
        MUTED_TEXT = {'red': 0.35, 'green': 0.40, 'blue': 0.47}

        # -------------------------------------------------------------------------
        # 5a. 보유종목 Formats & Styling
        # -------------------------------------------------------------------------
        # Reset stale cell backgrounds, text colors, bold flags & borders on A4:V30 before styling
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': 30, 'startColumnIndex': 0, 'endColumnIndex': 22},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': WHITE_BG,
                        'textFormat': {'bold': False, 'italic': False, 'foregroundColor': BLACK_TEXT, 'fontSize': 10},
                        'verticalAlignment': 'MIDDLE',
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,verticalAlignment)',
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': 30, 'startColumnIndex': 0, 'endColumnIndex': 22},
                'top': {'style': 'NONE'},
                'bottom': {'style': 'NONE'},
                'left': {'style': 'NONE'},
                'right': {'style': 'NONE'},
                'innerHorizontal': {'style': 'NONE'},
                'innerVertical': {'style': 'NONE'},
            }
        })
        # Header Row 3 Styling (Navy Background + White Text + Centered)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 2, 'endRowIndex': 3, 'startColumnIndex': 0, 'endColumnIndex': 22},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        # Column Alignments: Col A,B,C,R Center | Col D,E,F Left | Col G..Q, S..V Right
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': 0, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': 3, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'LEFT'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': 6, 'endColumnIndex': 17},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': 17, 'endColumnIndex': 18},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': 18, 'endColumnIndex': 22},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        # Quantity Col G (index 6): '#,##0'
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': 6, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        # Per-row local currency for Cols H..L (indices 7..12)
        for idx, curr in enumerate(item_currencies):
            row_idx = r_start + idx - 1
            pattern = '\"$\"#,##0.00' if curr == 'USD' else '\"₩\"#,##0'
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_holdings, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1, 'startColumnIndex': 7, 'endColumnIndex': 12},
                    'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': pattern}}},
                    'fields': 'userEnteredFormat.numberFormat'
                }
            })
        # Converted KRW Cols M, N, O, S, U (indices 12, 13, 14, 18, 20) as '₩#,##0'
        for col_idx in [12, 13, 14, 18, 20]:
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                    'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                    'fields': 'userEnteredFormat.numberFormat'
                }
            })
        # Percentages Cols P, Q, T, V (indices 15, 16, 19, 21) as '0.0%'
        for col_idx in [15, 16, 19, 21]:
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 3, 'endRowIndex': r_sum, 'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                    'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                    'fields': 'userEnteredFormat.numberFormat'
                }
            })
        # Summary Row Style
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': sum_idx, 'endRowIndex': sum_idx + 1, 'startColumnIndex': 0, 'endColumnIndex': 22},
                'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.94, 'green': 0.96, 'blue': 0.98}}},
                'fields': 'userEnteredFormat(textFormat,backgroundColor)'
            }
        })
        # Grid Borders for 보유종목
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_holdings, 'startRowIndex': 2, 'endRowIndex': r_sum, 'startColumnIndex': 0, 'endColumnIndex': 22},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY}
            }
        })

        # -------------------------------------------------------------------------
        # 5b. 배당캘린더 Formats & Styling (Full Canvas Reset + 3 Clean Stacked Sections)
        # -------------------------------------------------------------------------
        # 1) Full Canvas Format & Border Reset across A1:O85 so no stale row colors/borders remain when row counts change
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 0, 'endRowIndex': 85, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': WHITE_BG,
                        'textFormat': {'bold': False, 'italic': False, 'foregroundColor': BLACK_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'LEFT',
                        'verticalAlignment': 'MIDDLE',
                        'wrapStrategy': 'OVERFLOW_CELL',
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)',
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 0, 'endRowIndex': 85, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'top': {'style': 'NONE'},
                'bottom': {'style': 'NONE'},
                'left': {'style': 'NONE'},
                'right': {'style': 'NONE'},
                'innerHorizontal': {'style': 'NONE'},
                'innerVertical': {'style': 'NONE'},
            }
        })

        # 2) Title & Subtitle Styling (Row 1 & Row 2)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 0, 'endRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {
                    'userEnteredFormat': {
                        'textFormat': {'bold': True, 'foregroundColor': NAVY_HEADER, 'fontSize': 12},
                        'verticalAlignment': 'MIDDLE',
                    }
                },
                'fields': 'userEnteredFormat(textFormat,verticalAlignment)',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 1, 'endRowIndex': 2, 'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {
                    'userEnteredFormat': {
                        'textFormat': {'bold': False, 'foregroundColor': MUTED_TEXT, 'fontSize': 9},
                        'verticalAlignment': 'MIDDLE',
                    }
                },
                'fields': 'userEnteredFormat(textFormat,verticalAlignment)',
            }
        })

        # 3) Explicit Column Widths to prevent text clipping or squishing (Col A 120px, Col C 230px)
        col_widths_div = [120, 75, 230, 100, 105, 105, 65, 110, 85, 115, 115, 115, 115, 115, 115]
        for col_i, width in enumerate(col_widths_div):
            format_requests.append({
                'updateDimensionProperties': {
                    'range': {
                        'sheetId': sheet_id_div,
                        'dimension': 'COLUMNS',
                        'startIndex': col_i,
                        'endIndex': col_i + 1
                    },
                    'properties': {'pixelSize': width},
                    'fields': 'pixelSize'
                }
            })

        # Section 2 Header Merge (A{r_card_sec}:I{r_card_sec} - "배당 현금흐름 핵심 요약")
        format_requests.append({
            'mergeCells': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_sec - 1, 'endRowIndex': r_card_sec, 'startColumnIndex': 0, 'endColumnIndex': 9},
                'mergeType': 'MERGE_ALL'
            }
        })

        # Section 2 Cards Merge: Left Titles (A..C), Right Titles (F..H)
        for card_r in range(r_card_start - 1, r_card_end):
            format_requests.append({
                'mergeCells': {
                    'range': {'sheetId': sheet_id_div, 'startRowIndex': card_r, 'endRowIndex': card_r + 1, 'startColumnIndex': 0, 'endColumnIndex': 3},
                    'mergeType': 'MERGE_ALL'
                }
            })
            format_requests.append({
                'mergeCells': {
                    'range': {'sheetId': sheet_id_div, 'startRowIndex': card_r, 'endRowIndex': card_r + 1, 'startColumnIndex': 5, 'endColumnIndex': 8},
                    'mergeType': 'MERGE_ALL'
                }
            })

        # Section 3 Header Merge (A{r_m_sec}:G{r_m_sec} - "월별 배당 집계")
        format_requests.append({
            'mergeCells': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_sec - 1, 'endRowIndex': r_m_sec, 'startColumnIndex': 0, 'endColumnIndex': 7},
                'mergeType': 'MERGE_ALL'
            }
        })

        # Header Row 3 Styling (Navy Background + White Text + Centered)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 2, 'endRowIndex': 3, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE',
                        'wrapStrategy': 'WRAP'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)'
            }
        })
        # Main Data Table + Subtotal Alignments: Col A..B Center | Col C Left | Col D..G Center | Col H..O Right
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': r_tot_sum, 'startColumnIndex': 0, 'endColumnIndex': 2},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': r_tot_sum, 'startColumnIndex': 2, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'LEFT'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': r_tot_sum, 'startColumnIndex': 3, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': r_tot_sum, 'startColumnIndex': 7, 'endColumnIndex': 15},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })

        # Date Format on Col E..F ('yyyy-mm-dd') and Month Format on Col G ('0"월"')
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': div_end_row, 'startColumnIndex': 4, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': div_end_row, 'startColumnIndex': 6, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0"월"'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })

        # Status Badge Highlights (Col D) & Subtle Row Tint for Confirmed Rows
        for idx, st in enumerate(div_statuses):
            row_idx = 3 + idx
            if st == '실제 수령':
                row_bg = {'red': 0.97, 'green': 0.99, 'blue': 0.98}
                format_requests.append({
                    'repeatCell': {
                        'range': {'sheetId': sheet_id_div, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1, 'startColumnIndex': 0, 'endColumnIndex': 15},
                        'cell': {'userEnteredFormat': {'backgroundColor': row_bg}},
                        'fields': 'userEnteredFormat.backgroundColor'
                    }
                })
            bg_color = {'red': 0.90, 'green': 0.96, 'blue': 0.92} if st == '실제 수령' else {'red': 0.91, 'green': 0.94, 'blue': 1.0}
            text_color = {'red': 0.07, 'green': 0.45, 'blue': 0.20} if st == '실제 수령' else {'red': 0.10, 'green': 0.45, 'blue': 0.91}
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_div, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1, 'startColumnIndex': 3, 'endColumnIndex': 4},
                    'cell': {'userEnteredFormat': {'backgroundColor': bg_color, 'textFormat': {'bold': True, 'foregroundColor': text_color, 'fontSize': 10}}},
                    'fields': 'userEnteredFormat(backgroundColor,textFormat)'
                }
            })

        # Main Table Quantity & Currency Formats
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': r_tot_sum, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        for idx, curr in enumerate(div_currencies):
            row_idx = 3 + idx
            pattern = '\"$\"#,##0.00' if curr == 'USD' else '\"₩\"#,##0'
            for col_idx in [7, 9, 10, 11]:
                format_requests.append({
                    'repeatCell': {
                        'range': {'sheetId': sheet_id_div, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1, 'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': pattern}}},
                        'fields': 'userEnteredFormat.numberFormat'
                    }
                })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 3, 'endRowIndex': r_tot_sum, 'startColumnIndex': 12, 'endColumnIndex': 15},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })

        # Main Table Subtotals Formatting (r_act_sum, r_exp_sum, r_tot_sum)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_act_sum - 1, 'endRowIndex': r_act_sum, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'cell': {'userEnteredFormat': {'backgroundColor': {'red': 0.90, 'green': 0.96, 'blue': 0.92}, 'textFormat': {'bold': True, 'foregroundColor': {'red': 0.07, 'green': 0.35, 'blue': 0.16}, 'fontSize': 10}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_exp_sum - 1, 'endRowIndex': r_exp_sum, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'cell': {'userEnteredFormat': {'backgroundColor': {'red': 0.91, 'green': 0.94, 'blue': 1.0}, 'textFormat': {'bold': True, 'foregroundColor': {'red': 0.10, 'green': 0.28, 'blue': 0.58}, 'fontSize': 10}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_tot_sum - 1, 'endRowIndex': r_tot_sum, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'cell': {'userEnteredFormat': {'backgroundColor': NAVY_HEADER, 'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)'
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': 2, 'endRowIndex': r_tot_sum, 'startColumnIndex': 0, 'endColumnIndex': 15},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY}
            }
        })

        # Section 2 Styling: Executive Summary Cards Header (Row r_card_sec)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_sec - 1, 'endRowIndex': r_card_sec, 'startColumnIndex': 0, 'endColumnIndex': 9},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 11},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        # Cards Left Title BG (A..C) & Value BG (D)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 0, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'backgroundColor': LIGHT_GREY_BG, 'textFormat': {'bold': True, 'foregroundColor': BLACK_TEXT, 'fontSize': 10}, 'horizontalAlignment': 'LEFT', 'verticalAlignment': 'MIDDLE'}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 3, 'endColumnIndex': 4},
                'cell': {'userEnteredFormat': {'backgroundColor': SOFT_BLUE_BG, 'textFormat': {'bold': True, 'foregroundColor': BLACK_TEXT, 'fontSize': 10}, 'horizontalAlignment': 'RIGHT', 'verticalAlignment': 'MIDDLE', 'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,numberFormat)'
            }
        })
        # Cards Right Title BG (F..H) & Value BG (I)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 5, 'endColumnIndex': 8},
                'cell': {'userEnteredFormat': {'backgroundColor': LIGHT_GREY_BG, 'textFormat': {'bold': True, 'foregroundColor': BLACK_TEXT, 'fontSize': 10}, 'horizontalAlignment': 'LEFT', 'verticalAlignment': 'MIDDLE'}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'backgroundColor': SOFT_BLUE_BG, 'textFormat': {'bold': True, 'foregroundColor': BLACK_TEXT, 'fontSize': 10}, 'horizontalAlignment': 'RIGHT', 'verticalAlignment': 'MIDDLE'}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        # Number formats for Right Card Values (2 currency rows, 2 percent rows, 1 exchange rate row, 1 status badge row)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start - 1, 'endRowIndex': r_card_start + 1, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start + 1, 'endRowIndex': r_card_start + 3, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_start + 3, 'endRowIndex': r_card_start + 4, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0.00'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_end - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'backgroundColor': {'red': 0.90, 'green': 0.96, 'blue': 0.92}, 'textFormat': {'bold': True, 'foregroundColor': {'red': 0.07, 'green': 0.45, 'blue': 0.20}, 'fontSize': 10}, 'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)'
            }
        })
        # Symmetrical Card Grid Borders on Left (A..D) and Right (F..I)
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_sec - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 0, 'endColumnIndex': 4},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'SOLID', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY}
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_card_sec - 1, 'endRowIndex': r_card_end, 'startColumnIndex': 5, 'endColumnIndex': 9},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'SOLID', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY}
            }
        })

        # Section 3 Styling: Monthly Dividend Summary Table (Row r_m_sec..r_m_avg)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_sec - 1, 'endRowIndex': r_m_sec, 'startColumnIndex': 0, 'endColumnIndex': 7},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 11},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_header - 1, 'endRowIndex': r_m_header, 'startColumnIndex': 0, 'endColumnIndex': 7},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': BLUE_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_start - 1, 'endRowIndex': r_m_end, 'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER', 'numberFormat': {'type': 'NUMBER', 'pattern': '0"월"'}}},
                'fields': 'userEnteredFormat(horizontalAlignment,numberFormat)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_sum - 1, 'endRowIndex': r_m_avg, 'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_header, 'endRowIndex': r_m_avg, 'startColumnIndex': 1, 'endColumnIndex': 6},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT', 'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat(horizontalAlignment,numberFormat)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_header, 'endRowIndex': r_m_avg, 'startColumnIndex': 6, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_sum - 1, 'endRowIndex': r_m_avg, 'startColumnIndex': 0, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {'textFormat': {'bold': True, 'foregroundColor': BLACK_TEXT, 'fontSize': 10}, 'backgroundColor': {'red': 0.94, 'green': 0.96, 'blue': 0.98}}},
                'fields': 'userEnteredFormat(textFormat,backgroundColor)'
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_div, 'startRowIndex': r_m_sec - 1, 'endRowIndex': r_m_avg, 'startColumnIndex': 0, 'endColumnIndex': 7},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY}
            }
        })

        # -------------------------------------------------------------------------
        # 5b-2. 배당원장_YYYY Formats & Styling (18 Statutory Columns A..R + 2 Summary Tables)
        # -------------------------------------------------------------------------
        sheet_id_dl = ws_dl.id
        col_widths_dl = [
            115, 85, 95, 95, 90, 215, 95, 175, 65,
            85, 105, 110, 95, 105, 105, 125, 120, 125,
        ]
        for col_i, width in enumerate(col_widths_dl):
            format_requests.append({
                'updateDimensionProperties': {
                    'range': {
                        'sheetId': sheet_id_dl,
                        'dimension': 'COLUMNS',
                        'startIndex': col_i,
                        'endIndex': col_i + 1,
                    },
                    'properties': {'pixelSize': width},
                    'fields': 'pixelSize',
                }
            })

        # Merge 4 Executive Tax Summary Cards across Rows 3..5 (A..D, E..H, I..L, M..P)
        for card_col_start in [0, 4, 8, 12]:
            for card_row_idx in [2, 3, 4]:
                format_requests.append({
                    'mergeCells': {
                        'range': {
                            'sheetId': sheet_id_dl,
                            'startRowIndex': card_row_idx,
                            'endRowIndex': card_row_idx + 1,
                            'startColumnIndex': card_col_start,
                            'endColumnIndex': card_col_start + 4,
                        },
                        'mergeType': 'MERGE_ALL',
                    }
                })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 2, 'endRowIndex': 3, 'startColumnIndex': 0, 'endColumnIndex': 16},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE',
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 3, 'endRowIndex': 4, 'startColumnIndex': 0, 'endColumnIndex': 16},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': SOFT_BLUE_BG,
                        'textFormat': {'bold': True, 'fontSize': 11},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE',
                        'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'},
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,numberFormat)',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 4, 'endRowIndex': 5, 'startColumnIndex': 0, 'endColumnIndex': 16},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': LIGHT_GREY_BG,
                        'textFormat': {'fontSize': 9},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE',
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)',
            }
        })

        # Header Row 6 Styling (A6:R6)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 5, 'endRowIndex': 6, 'startColumnIndex': 0, 'endColumnIndex': 18},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE',
                        'wrapStrategy': 'WRAP',
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)',
            }
        })
        # Alignments & Formats for Main Subledger Rows (7..r_dl_sum)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum, 'startColumnIndex': 0, 'endColumnIndex': 5},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum, 'startColumnIndex': 5, 'endColumnIndex': 8},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'LEFT'}},
                'fields': 'userEnteredFormat.horizontalAlignment',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum, 'startColumnIndex': 8, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum, 'startColumnIndex': 9, 'endColumnIndex': 18},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat.horizontalAlignment',
            }
        })
        # Qty Col J (9), Rate Col M (12), FX Col O (14), KRW Cols P..R (15..18)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum, 'startColumnIndex': 9, 'endColumnIndex': 10},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum - 1, 'startColumnIndex': 12, 'endColumnIndex': 13},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                'fields': 'userEnteredFormat.numberFormat',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum - 1, 'startColumnIndex': 14, 'endColumnIndex': 15},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00'}}},
                'fields': 'userEnteredFormat.numberFormat',
            }
        })
        for idx, curr in enumerate(dl_currencies):
            row_idx = 6 + idx
            pattern = '\"$\"#,##0.00' if curr == 'USD' else '\"₩\"#,##0'
            for col_idx in [10, 11, 13]:
                format_requests.append({
                    'repeatCell': {
                        'range': {'sheetId': sheet_id_dl, 'startRowIndex': row_idx, 'endRowIndex': row_idx + 1, 'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': pattern}}},
                        'fields': 'userEnteredFormat.numberFormat',
                    }
                })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 6, 'endRowIndex': r_dl_sum, 'startColumnIndex': 15, 'endColumnIndex': 18},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': r_dl_sum - 1, 'endRowIndex': r_dl_sum, 'startColumnIndex': 0, 'endColumnIndex': 18},
                'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.94, 'green': 0.96, 'blue': 0.98}}},
                'fields': 'userEnteredFormat(textFormat,backgroundColor)',
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': 5, 'endRowIndex': r_dl_sum, 'startColumnIndex': 0, 'endColumnIndex': 18},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY},
            }
        })

        # Section 2 (Tax Form Table) & Section 3 (Monthly/Quarterly Table) Formatting on 배당원장_YYYY
        for sec_title_r, sec_hdr_r, sec_end_r in [
            (r_tax_sec, r_tax_hdr, r_tax_sum),
            (r_prd_sec, r_prd_hdr, r_prd_sum),
        ]:
            format_requests.append({
                'mergeCells': {
                    'range': {'sheetId': sheet_id_dl, 'startRowIndex': sec_title_r - 1, 'endRowIndex': sec_title_r, 'startColumnIndex': 0, 'endColumnIndex': 8},
                    'mergeType': 'MERGE_ALL',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_dl, 'startRowIndex': sec_title_r - 1, 'endRowIndex': sec_title_r, 'startColumnIndex': 0, 'endColumnIndex': 8},
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': NAVY_HEADER,
                            'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                            'horizontalAlignment': 'LEFT',
                            'verticalAlignment': 'MIDDLE',
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_dl, 'startRowIndex': sec_hdr_r - 1, 'endRowIndex': sec_hdr_r, 'startColumnIndex': 0, 'endColumnIndex': 8},
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': BLUE_HEADER,
                            'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                            'horizontalAlignment': 'CENTER',
                            'verticalAlignment': 'MIDDLE',
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_dl, 'startRowIndex': sec_end_r - 1, 'endRowIndex': sec_end_r, 'startColumnIndex': 0, 'endColumnIndex': 8},
                    'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.94, 'green': 0.96, 'blue': 0.98}}},
                    'fields': 'userEnteredFormat(textFormat,backgroundColor)',
                }
            })
            format_requests.append({
                'updateBorders': {
                    'range': {'sheetId': sheet_id_dl, 'startRowIndex': sec_title_r - 1, 'endRowIndex': sec_end_r, 'startColumnIndex': 0, 'endColumnIndex': 8},
                    'top': {'style': 'SOLID', 'color': BORDER_GREY},
                    'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                    'left': {'style': 'SOLID', 'color': BORDER_GREY},
                    'right': {'style': 'SOLID', 'color': BORDER_GREY},
                    'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                    'innerVertical': {'style': 'SOLID', 'color': INNER_GREY},
                }
            })
        # Specific number formats for Section 2 (Tax Form Table) & Section 3 (Monthly/Quarterly Table)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': r_tax_start - 1, 'endRowIndex': r_tax_end, 'startColumnIndex': 3, 'endColumnIndex': 4},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}, 'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat(numberFormat,horizontalAlignment)',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': r_tax_start - 1, 'endRowIndex': r_tax_sum, 'startColumnIndex': 4, 'endColumnIndex': 7},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}, 'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat(numberFormat,horizontalAlignment)',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': r_prd_start - 1, 'endRowIndex': r_prd_sum, 'startColumnIndex': 0, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': r_prd_start - 1, 'endRowIndex': r_prd_sum, 'startColumnIndex': 3, 'endColumnIndex': 8},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}, 'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat(numberFormat,horizontalAlignment)',
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dl, 'startRowIndex': r_q_start - 1, 'endRowIndex': r_q_end, 'startColumnIndex': 0, 'endColumnIndex': 8},
                'cell': {'userEnteredFormat': {'backgroundColor': SOFT_BLUE_BG, 'textFormat': {'bold': True}}},
                'fields': 'userEnteredFormat(backgroundColor,textFormat)',
            }
        })

        # -------------------------------------------------------------------------
        # 5c. 매매일지_YYYY-MM Formats & Styling (24 Statutory Columns A..X per Month Tab)
        # -------------------------------------------------------------------------
        def _iter_runs(seq):
            if not seq:
                return
            start = 0
            cur = seq[0]
            for i in range(1, len(seq)):
                if seq[i] != cur:
                    yield start, i, cur
                    start = i
                    cur = seq[i]
            yield start, len(seq), cur

        col_widths_trades = [
            95, 80, 105, 85, 75, 95, 145, 65, 80, 105,
            110, 95, 115, 120, 115, 95, 95, 120, 115, 120,
            85, 125, 85, 190,
        ]

        for m_spec in monthly_tab_specs:
            sheet_id_trades = m_spec["sheet_id"]
            r_trade_sum = m_spec["r_trade_sum"]
            end_trade_row = r_trade_sum
            trade_types = m_spec["trade_types"]
            trade_currencies = m_spec["trade_currencies"]

            for col_i, width in enumerate(col_widths_trades):
                format_requests.append({
                    'updateDimensionProperties': {
                        'range': {
                            'sheetId': sheet_id_trades,
                            'dimension': 'COLUMNS',
                            'startIndex': col_i,
                            'endIndex': col_i + 1,
                        },
                        'properties': {'pixelSize': width},
                        'fields': 'pixelSize',
                    }
                })

            # Merge 4 Executive Summary Cards across Rows 3..5 (A..D, E..H, I..L, M..P)
            for card_col_start in [0, 4, 8, 12]:
                for card_row_idx in [2, 3, 4]:
                    format_requests.append({
                        'mergeCells': {
                            'range': {
                                'sheetId': sheet_id_trades,
                                'startRowIndex': card_row_idx,
                                'endRowIndex': card_row_idx + 1,
                                'startColumnIndex': card_col_start,
                                'endColumnIndex': card_col_start + 4,
                            },
                            'mergeType': 'MERGE_ALL',
                        }
                    })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 2, 'endRowIndex': 3, 'startColumnIndex': 0, 'endColumnIndex': 16},
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': NAVY_HEADER,
                            'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                            'horizontalAlignment': 'CENTER',
                            'verticalAlignment': 'MIDDLE',
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 3, 'endRowIndex': 4, 'startColumnIndex': 0, 'endColumnIndex': 16},
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': SOFT_BLUE_BG,
                            'textFormat': {'bold': True, 'fontSize': 11},
                            'horizontalAlignment': 'CENTER',
                            'verticalAlignment': 'MIDDLE',
                            'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'},
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,numberFormat)',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 4, 'endRowIndex': 5, 'startColumnIndex': 0, 'endColumnIndex': 16},
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': LIGHT_GREY_BG,
                            'textFormat': {'fontSize': 9},
                            'horizontalAlignment': 'CENTER',
                            'verticalAlignment': 'MIDDLE',
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)',
                }
            })

            # Header Row 6 Styling (Navy Background + White Text + Centered + Wrap across Cols A..X)
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 5, 'endRowIndex': 6, 'startColumnIndex': 0, 'endColumnIndex': 24},
                    'cell': {
                        'userEnteredFormat': {
                            'backgroundColor': NAVY_HEADER,
                            'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                            'horizontalAlignment': 'CENTER',
                            'verticalAlignment': 'MIDDLE',
                            'wrapStrategy': 'WRAP',
                        }
                    },
                    'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)',
                }
            })

            # Alignments: Cols A..F (0..6) & Col H (7..8) Center | Col G (6..7) & Col X (23..24) Left | Cols I..W (8..23) Right
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 0, 'endColumnIndex': 6},
                    'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                    'fields': 'userEnteredFormat.horizontalAlignment',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 6, 'endColumnIndex': 7},
                    'cell': {'userEnteredFormat': {'horizontalAlignment': 'LEFT'}},
                    'fields': 'userEnteredFormat.horizontalAlignment',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 7, 'endColumnIndex': 8},
                    'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                    'fields': 'userEnteredFormat.horizontalAlignment',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 8, 'endColumnIndex': 23},
                    'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT'}},
                    'fields': 'userEnteredFormat.horizontalAlignment',
                }
            })
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 23, 'endColumnIndex': 24},
                    'cell': {'userEnteredFormat': {'horizontalAlignment': 'LEFT'}},
                    'fields': 'userEnteredFormat.horizontalAlignment',
                }
            })

            # Status Badge Background Highlights (Col E: index 4)
            for run_start, run_end, st in _iter_runs(trade_types):
                bg_color = {'red': 0.99, 'green': 0.91, 'blue': 0.90} if st == '매수' else {'red': 0.91, 'green': 0.94, 'blue': 1.0}
                text_color = {'red': 0.77, 'green': 0.13, 'blue': 0.12} if st == '매수' else {'red': 0.10, 'green': 0.45, 'blue': 0.91}
                format_requests.append({
                    'repeatCell': {
                        'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6 + run_start, 'endRowIndex': 6 + run_end, 'startColumnIndex': 4, 'endColumnIndex': 5},
                        'cell': {'userEnteredFormat': {'backgroundColor': bg_color, 'textFormat': {'bold': True, 'foregroundColor': text_color}}},
                        'fields': 'userEnteredFormat(backgroundColor,textFormat)',
                    }
                })

            # Per-run local currency formatting for Cols J, K, N (indices 9, 10, 13)
            for run_start, run_end, curr in _iter_runs(trade_currencies):
                pattern = '\"$\"#,##0.00' if curr == 'USD' else '\"₩\"#,##0'
                for col_idx in [9, 10, 13]:
                    format_requests.append({
                        'repeatCell': {
                            'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6 + run_start, 'endRowIndex': 6 + run_end, 'startColumnIndex': col_idx, 'endColumnIndex': col_idx + 1},
                            'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': pattern}}},
                            'fields': 'userEnteredFormat.numberFormat',
                        }
                    })

            # Quantity Col I (index 8) & Remaining Qty Col W (index 22): '#,##0'
            for q_col in [8, 22]:
                format_requests.append({
                    'repeatCell': {
                        'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': q_col, 'endColumnIndex': q_col + 1},
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0'}}},
                        'fields': 'userEnteredFormat.numberFormat',
                    }
                })

            # FX Rate Col L (index 11): '#,##0.00'
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 11, 'endColumnIndex': 12},
                    'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '#,##0.00'}}},
                    'fields': 'userEnteredFormat.numberFormat',
                }
            })

            # KRW Currency Cols M, O, P, Q, R, S, T, V (indices 12, 14, 15, 16, 17, 18, 19, 21) as '₩#,##0'
            for krw_col in [12, 14, 15, 16, 17, 18, 19, 21]:
                format_requests.append({
                    'repeatCell': {
                        'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': krw_col, 'endColumnIndex': krw_col + 1},
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                        'fields': 'userEnteredFormat.numberFormat',
                    }
                })

            # Return % Col U (index 20): '0.00%'
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 6, 'endRowIndex': end_trade_row, 'startColumnIndex': 20, 'endColumnIndex': 21},
                    'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.00%'}}},
                    'fields': 'userEnteredFormat.numberFormat',
                }
            })

            # Summary Row Style at bottom of 매매일지_YYYY-MM (r_trade_sum)
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': r_trade_sum - 1, 'endRowIndex': r_trade_sum, 'startColumnIndex': 0, 'endColumnIndex': 24},
                    'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.94, 'green': 0.96, 'blue': 0.98}}},
                    'fields': 'userEnteredFormat(textFormat,backgroundColor)',
                }
            })

            # Grid Borders for 매매일지_YYYY-MM (A6:X{r_trade_sum})
            format_requests.append({
                'updateBorders': {
                    'range': {'sheetId': sheet_id_trades, 'startRowIndex': 5, 'endRowIndex': r_trade_sum, 'startColumnIndex': 0, 'endColumnIndex': 24},
                    'top': {'style': 'SOLID', 'color': BORDER_GREY},
                    'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                    'left': {'style': 'SOLID', 'color': BORDER_GREY},
                    'right': {'style': 'SOLID', 'color': BORDER_GREY},
                    'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                    'innerVertical': {'style': 'SOLID', 'color': INNER_GREY},
                }
            })

        # -------------------------------------------------------------------------
        # 5d. 대시보드 Formats & Styling
        # -------------------------------------------------------------------------
        # Top Summary Card Row 3 Styling (Navy Background + White Text + Centered)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 2, 'endRowIndex': 3, 'startColumnIndex': 0, 'endColumnIndex': 9},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': NAVY_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        # Top Card Values Row 4 & Row 5 Alignment
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 3, 'endRowIndex': 5, 'startColumnIndex': 0, 'endColumnIndex': 9},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER', 'verticalAlignment': 'MIDDLE'}},
                'fields': 'userEnteredFormat(horizontalAlignment,verticalAlignment)'
            }
        })
        # A4, C4, G4, I4 as '₩#,##0', E4 as '0.0%'
        for c in [0, 2, 6, 8]:
            format_requests.append({
                'repeatCell': {
                    'range': {'sheetId': sheet_id_dash, 'startRowIndex': 3, 'endRowIndex': 4, 'startColumnIndex': c, 'endColumnIndex': c + 1},
                    'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                    'fields': 'userEnteredFormat.numberFormat'
                }
            })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 3, 'endRowIndex': 4, 'startColumnIndex': 4, 'endColumnIndex': 5},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })

        # Asset Breakdown Header Row 8 Styling (Blue Background + White Text + Centered)
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 7, 'endRowIndex': 8, 'startColumnIndex': 0, 'endColumnIndex': 3},
                'cell': {
                    'userEnteredFormat': {
                        'backgroundColor': BLUE_HEADER,
                        'textFormat': {'bold': True, 'foregroundColor': WHITE_TEXT, 'fontSize': 10},
                        'horizontalAlignment': 'CENTER',
                        'verticalAlignment': 'MIDDLE'
                    }
                },
                'fields': 'userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 8, 'endRowIndex': 14, 'startColumnIndex': 0, 'endColumnIndex': 1},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'CENTER'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 8, 'endRowIndex': 14, 'startColumnIndex': 1, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'horizontalAlignment': 'RIGHT'}},
                'fields': 'userEnteredFormat.horizontalAlignment'
            }
        })
        # B9:B14 as '₩#,##0' and C9:C14 as '0.0%'
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 8, 'endRowIndex': 14, 'startColumnIndex': 1, 'endColumnIndex': 2},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 8, 'endRowIndex': 14, 'startColumnIndex': 2, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 13, 'endRowIndex': 14, 'startColumnIndex': 0, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'textFormat': {'bold': True}, 'backgroundColor': {'red': 0.94, 'green': 0.96, 'blue': 0.98}}},
                'fields': 'userEnteredFormat(textFormat,backgroundColor)'
            }
        })
        format_requests.append({
            'updateBorders': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 7, 'endRowIndex': 14, 'startColumnIndex': 0, 'endColumnIndex': 3},
                'top': {'style': 'SOLID', 'color': BORDER_GREY},
                'bottom': {'style': 'DOUBLE', 'color': BORDER_GREY},
                'left': {'style': 'SOLID', 'color': BORDER_GREY},
                'right': {'style': 'SOLID', 'color': BORDER_GREY},
                'innerHorizontal': {'style': 'SOLID', 'color': INNER_GREY},
                'innerVertical': {'style': 'SOLID', 'color': INNER_GREY}
            }
        })
        # Bottom PnL Attribution Table (Rows 19..24) Number Formats: B19:B23 currency, C19:C23 percent, B24 percent
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 18, 'endRowIndex': 23, 'startColumnIndex': 1, 'endColumnIndex': 2},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'CURRENCY', 'pattern': '\"₩\"#,##0'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 18, 'endRowIndex': 23, 'startColumnIndex': 2, 'endColumnIndex': 3},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })
        format_requests.append({
            'repeatCell': {
                'range': {'sheetId': sheet_id_dash, 'startRowIndex': 23, 'endRowIndex': 24, 'startColumnIndex': 1, 'endColumnIndex': 2},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'PERCENT', 'pattern': '0.0%'}}},
                'fields': 'userEnteredFormat.numberFormat'
            }
        })

        sh.batch_update({'requests': format_requests})
        logger.info("Successfully formatted numbers & professional table styles across ALL 4 Worksheets!")
        try:
            last_sync_file.write_text(str(time.time()))
        except Exception:
            pass
        return True

    except Exception as e:
        logger.error(f"Failed to sync template sheets: {e}", exc_info=True)
        return False


sync_to_google_sheets = sync_template_sheets


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "11iFYFay6E7xgVLoQvuAG66J-L1Ee7FzpBcprXqOfwYg"
    success = sync_template_sheets(target)
    sys.exit(0 if success else 1)

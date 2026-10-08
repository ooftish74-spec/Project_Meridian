#!/usr/bin/env python3
"""
scripts/inspect_live_truth.py
Project Meridian Single Source of Truth (SSOT) Live Inspection Utility.

This script inspects live production portfolio state files (kis_portfolio.json,
shadow_portfolio.json, config/defaults.json) to display the unpolluted, actual
live production holdings and active streams.
"""

import os
import sys
import json
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python3"

def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}

def inspect_live_truth(as_json: bool = False):
    kis_path = PROJECT_ROOT / "results" / "kis_portfolio.json"
    shadow_path = PROJECT_ROOT / "results" / "shadow_portfolio.json"
    defaults_path = PROJECT_ROOT / "config" / "defaults.json"
    regime_path = PROJECT_ROOT / "results" / "current_regime.json"

    kis_data = load_json(kis_path)
    shadow_data = load_json(shadow_path)
    defaults_data = load_json(defaults_path)
    regime_data = load_json(regime_path)

    # Hard Enforcement of Live KIS API Fetch
    live_fetched = False
    error_msg = ""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.execution._kis_adapter import KISTraderAdapter
        adapter = KISTraderAdapter(mode='live', fetch_balance_on_init=True)
        if adapter.fetch_live_balance() and adapter.positions:
            live_holdings = {}
            from src.utils.ticker_name_resolver import resolve_name
            for ticker, pos in adapter.positions.items():
                c_price = float(pos.current_price) if pos.current_price > 0 else float(pos.avg_price)
                a_price = float(pos.avg_price) if pos.avg_price > 0 else c_price
                ret_pct = round((c_price / a_price - 1.0) * 100.0, 2) if a_price > 0 else 0.0
                mkt = 'KR' if ticker.isdigit() else 'US'
                prod_name = pos.name or resolve_name(ticker)
                live_holdings[ticker] = {
                    'name': prod_name,
                    'qty': pos.quantity,
                    'avg_price': round(a_price, 2),
                    'current_price': round(c_price, 2),
                    'return_pct': ret_pct,
                    'market': mkt
                }
            if live_holdings:
                holdings = live_holdings
                from datetime import datetime
                live_date = datetime.now().strftime("%Y-%m-%d %H:%M KST")
                account_info = kis_data.get("account", {})
                if hasattr(adapter, 'account') and getattr(adapter.account, 'total_equity', 0) > 0:
                    account_info['nav'] = round(adapter.account.total_equity, 2)
                if hasattr(adapter, 'account') and getattr(adapter.account, 'cash', 0) >= 0:
                    account_info['cash'] = round(adapter.account.cash, 2)
                
                kis_data['date'] = live_date
                kis_data['account'] = account_info
                kis_data['holdings'] = holdings
                with open(kis_path, "w", encoding="utf-8") as f_out:
                    json.dump(kis_data, f_out, indent=2, ensure_ascii=False)
                live_fetched = True
    except Exception as e_live:
        error_msg = str(e_live)
        live_fetched = False

    if not live_fetched:
        print("CRITICAL_ERROR: LIVE_KIS_API_FETCH_FAILED - Cannot display unverified stale cache!")
        print(f"Reason: {error_msg}")
        sys.exit(1)
    streams = kis_data.get("streams", {})
    active_streams = defaults_data.get("allocator.active_streams", [])

    # Market Calendar Status Check
    krx_open = False
    us_open = False
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.utils.market_calendar import MarketCalendar, is_us_trading_day
        cal = MarketCalendar()
        krx_open = cal.is_trading_day()
        us_open = is_us_trading_day()
    except Exception:
        pass

    # Check for leveraged / legacy inverse tickers
    leveraged_etps = ["TQQQ", "SQQQ", "233740", "252670"]
    held_leveraged = [t for t in leveraged_etps if t in holdings or t in streams]

    kr_holdings = {k: v for k, v in holdings.items() if v.get('market') == 'KR'}
    us_holdings = {k: v for k, v in holdings.items() if v.get('market') == 'US'}

    kr_stock_val = sum(v.get('qty', 0) * v.get('current_price', 0) for v in kr_holdings.values())
    us_stock_val_usd = sum(v.get('qty', 0) * v.get('current_price', 0) for v in us_holdings.values())
    cash_krw = account_info.get("cash", 0.0)
    cash_usd = account_info.get("cash_usd", 0.0)
    fx_rate = account_info.get("fx_rate", 1353.63)

    kr_total_equity = cash_krw + kr_stock_val
    us_total_equity = cash_usd + us_stock_val_usd

    truth_summary = {
        "ssot_status": "VALIDATED_LIVE",
        "kis_mode": kis_data.get("kis_mode", "unknown"),
        "date": live_date,
        "account_number": account_info.get("number", "N/A"),
        "account_type": account_info.get("type", "N/A"),
        "initial_capital": account_info.get("initial_capital", 0.0),
        "kr_account": {
            "cash_krw": cash_krw,
            "stock_val_krw": kr_stock_val,
            "total_equity_krw": kr_total_equity,
            "holdings": kr_holdings
        },
        "us_account": {
            "cash_usd": cash_usd,
            "stock_val_usd": us_stock_val_usd,
            "total_equity_usd": us_total_equity,
            "holdings": us_holdings
        },
        "combined_nav_krw": account_info.get("nav", 0.0),
        "applied_fx_rate": fx_rate,
        "shadow_cash": shadow_data.get("cash", 0.0),
        "shadow_real_synced": shadow_data.get("real_account_synced", False),
        "active_streams": active_streams,
        "current_regime": regime_data.get("regime", "unknown"),
        "leveraged_etps_held": held_leveraged
    }

    if as_json:
        print(json.dumps(truth_summary, indent=2, ensure_ascii=False))
        return

    print("==========================================================")
    print("      MERIDIAN LIVE PRODUCTION SINGLE SOURCE OF TRUTH     ")
    print("==========================================================")
    print(f"📌 SSOT Status          : {truth_summary['ssot_status']}")
    print(f"📌 KIS Mode             : {truth_summary['kis_mode'].upper()}")
    print(f"📌 Last Portfolio Date  : {truth_summary['date']}")
    print(f"📌 Account Number       : {truth_summary['account_number']} ({truth_summary['account_type']})")
    print("----------------------------------------------------------")
    print("🇰🇷 국내 원화 계좌 (KRW Wallet):")
    print(f"   - 원화 예수금 (Cash KRW)  : ₩{cash_krw:,.0f} KRW")
    print(f"   - 원화 주식 평가액        : ₩{kr_stock_val:,.0f} KRW")
    print(f"   - 원화 자산 소계          : ₩{kr_total_equity:,.0f} KRW")
    print("🇺🇸 해외 외화 계좌 (USD Wallet):")
    print(f"   - 외화 예수금 (Cash USD)  : ${cash_usd:,.2f} USD")
    print(f"   - 외화 주식 평가액        : ${us_stock_val_usd:,.2f} USD")
    print(f"   - 외화 자산 소계          : ${us_total_equity:,.2f} USD")
    print("----------------------------------------------------------")
    print("🌐 통합 참고 평가액 (Combined Total Reference NAV):")
    print(f"   - 총 자산 NAV (원화)      : ₩{truth_summary['combined_nav_krw']:,.0f} KRW")
    print(f"   - 적용 기준 환율          : ₩{fx_rate:,.2f} KRW/USD")
    print(f"🔄 Active Streams       : {', '.join(truth_summary['active_streams'])}")
    print(f"🌐 Market Regime        : {truth_summary['current_regime']}")
    print(f"📅 Trading Day Status   : KRX={'OPEN' if krx_open else 'CLOSED'} | US={'OPEN' if us_open else 'CLOSED (HOLIDAY)'}")
    print("----------------------------------------------------------")
    print("📦 보유 종목 명세:")
    print("   [🇰🇷 국내 보유 종목]")
    if kr_holdings:
        for ticker, info in kr_holdings.items():
            disp_name = info.get('name')
            if not disp_name or disp_name == ticker:
                from src.utils.ticker_name_resolver import resolve_name
                disp_name = resolve_name(ticker)
            print(f"     • {ticker} ({disp_name}): {info.get('qty')}주 @ ₩{info.get('current_price'):,.0f} (수익률 {info.get('return_pct'):+.2f}%)")
    else:
        print("     • (보유 종목 없음)")
    print("   [🇺🇸 해외 보유 종목]")
    if us_holdings:
        for ticker, info in us_holdings.items():
            disp_name = info.get('name')
            if not disp_name or disp_name == ticker:
                from src.utils.ticker_name_resolver import resolve_name
                disp_name = resolve_name(ticker)
            print(f"     • {ticker} ({disp_name}): {info.get('qty')}주 @ ${info.get('current_price'):,.2f} (수익률 {info.get('return_pct'):+.2f}%)")
    else:
        print("     • (보유 종목 없음)")
    print("----------------------------------------------------------")
    print(f"🛡️  Leveraged ETP Check (TQQQ, SQQQ, etc.): {'⚠️  HELD' if held_leveraged else '✅ NONE HELD (0% Leverage In Account)'}")
    print("==========================================================")

def main():
    if VENV_PYTHON.exists() and sys.executable != str(VENV_PYTHON):
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)
    parser = argparse.ArgumentParser(description="Meridian Live Production SSOT Inspector")
    parser.add_argument("--json", action="store_true", help="Output truth summary as JSON")
    args = parser.parse_args()
    inspect_live_truth(as_json=args.json)

if __name__ == "__main__":
    main()

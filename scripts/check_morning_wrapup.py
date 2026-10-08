#!/usr/bin/env python3
"""
scripts/check_morning_wrapup.py
===============================
Project Meridian — Morning OIS Breakdown & US Market Wrap-Up Inspector
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

def run_wrapup_inspection():
    from src.intelligence.overnight_intelligence import OvernightIntelligenceScore
    
    ois_calc = OvernightIntelligenceScore()
    ois_res = ois_calc.calculate(include_premarket=True)

    sc_path = PROJECT_ROOT / "results" / "signal_cache.json"
    sc = {}
    if sc_path.exists():
        with open(sc_path, "r", encoding="utf-8") as f:
            sc = json.load(f)

    nf_path = PROJECT_ROOT / "data" / "macro" / "night_futures.json"
    nf = {}
    if nf_path.exists():
        with open(nf_path, "r", encoding="utf-8") as f:
            nf = json.load(f)

    print("==========================================================")
    print("     PROJECT MERIDIAN 2026-09-22 OIS & US MARKET WRAP-UP  ")
    print("==========================================================")
    print("📌 Total Calculated OIS:", json.dumps(ois_res, indent=2, ensure_ascii=False))
    print("\n📌 KRX Night Futures:")
    print(f"    - Symbol: {nf.get('symbol')} ({nf.get('front_code')})")
    print(f"    - Close: {nf.get('close')}")
    print(f"    - Change %: {nf.get('change_pct')} %")
    print(f"    - Source: {nf.get('source')}")

    print("\n📌 US Market Wrap-Up (Overnight Close & Telemetry):")
    print(f"    - S&P 500 (SPY): {sc.get('SP500')} ({sc.get('sp500_change_1d', 0.0):+.2f}%)")
    print(f"    - NASDAQ (QQQ): {sc.get('NASDAQ')} ({sc.get('nasdaq_change_1d', 0.0):+.2f}%)")
    print(f"    - SOX Semiconductor (SOXX): {sc.get('SOX')} ({sc.get('sox_change_1d', 0.0):+.2f}%)")
    print(f"    - Dow Jones (DIA): {sc.get('DJI')}")
    print(f"    - VIX Volatility: {sc.get('vix')}")
    print(f"    - USD/KRW Exchange Rate: ₩{sc.get('USDKRW')} KRW/USD")
    print(f"    - US 10Y Yield (^TNX): {sc.get('US10Y')} %")
    print(f"    - WTI Crude Oil (CL=F): ${sc.get('WTI')}")
    print(f"    - Gold (GC=F / GLD): ${sc.get('GOLD_US')}")
    print(f"    - Silver (SI=F / SLV): ${sc.get('SILVER')}")
    print(f"    - Dollar Index (DXY / UUP): {sc.get('DXY')}")
    print(f"    - Options Put-Call Ratio: {sc.get('options_pcr')}")
    print(f"    - News Sentiment Score: +{sc.get('news_sentiment')}")
    print("==========================================================")

if __name__ == "__main__":
    run_wrapup_inspection()

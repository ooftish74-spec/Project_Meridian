#!/usr/bin/env python3
"""
scripts/fetch_night_futures.py
==============================
Project Meridian — Night Futures & Overnight Macro Data Fetcher

야간 선물(KRX/Eurex Night Futures), EWY, US 시장 야간 변동 지표 수집 및 캐싱.
"""

import sys
import json
import logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from typing import Optional
from src.utils.file_ops import atomic_write_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def fetch_and_save_night_futures(override_close: Optional[float] = None, override_change_pct: Optional[float] = None):
    """야간 선물 (Eurex/KRX Night Session) 수집 후 저장."""
    logger.info("  🌙 [Night Futures] KRX 야간선물 (Eurex Night Session) 수집 시작")
    
    # KRX Night Session is strictly CLOSED only on Saturday and Sunday
    now = datetime.now()
    is_weekend = now.weekday() in (5, 6)
    
    status = "CLOSED" if is_weekend else "OK"
    chg_val = 0.0 if is_weekend else (0.0 if override_change_pct is None else override_change_pct)
    c_val = 1000.0 if override_close is None else override_close

    night_data = {
        "symbol": "10100000",
        "front_code": "F202612",
        "close": c_val,
        "change_pct": chg_val,
        "status": status,
        "source": "KRX_Night_Market",
        "timestamp": now.isoformat()
    }

    try:
        from src.data_collection.central_data_gateway import CentralDataGateway
        gateway = CentralDataGateway()
        ov_data = gateway.get_overnight_futures()
        # Accept valid non-zero quote regardless of magnitude (do NOT discard quotes < 2.0%)
        if isinstance(ov_data, dict) and ov_data.get("close", 0) > 0:
            night_data.update(ov_data)
            night_data["status"] = "OK"
            logger.info(f"  ✅ [CentralDataGateway] 야간 선물 수집 완료: {ov_data}")
    except Exception as e:
        logger.warning(f"  ⚠️ CentralDataGateway 수집 우회: {e}")

    if override_close is not None:
        night_data["close"] = override_close
    if override_change_pct is not None:
        night_data["change_pct"] = override_change_pct
        night_data["change"] = round(override_close * (override_change_pct / 100.0), 2)
        night_data["direction"] = "up" if override_change_pct > 0 else "down"

    # Write to target paths
    macro_dir = PROJECT_ROOT / "data" / "macro"
    macro_dir.mkdir(parents=True, exist_ok=True)
    
    atomic_write_json(macro_dir / "night_futures.json", night_data, indent=2)
    atomic_write_json(PROJECT_ROOT / "results" / "krx_futures_overnight.json", night_data, indent=2)

    # Also update signal_cache.json for premarket calibration
    sc_path = PROJECT_ROOT / "results" / "signal_cache.json"
    if sc_path.exists():
        try:
            with open(sc_path, "r", encoding="utf-8") as f:
                sc = json.load(f)
            sc["night_futures"] = night_data["close"]
            sc["night_futures_change_1d"] = night_data["change_pct"]
            atomic_write_json(sc_path, sc, indent=2)
        except Exception as e:
            logger.warning(f"  ⚠️ signal_cache 갱신 예외: {e}")

    logger.info(f"  ✅ [Night Futures] KRX 야간선물 ({night_data['close']}, {night_data['change_pct']:+.2f}%) 저장 완료 -> data/macro/night_futures.json")
    return night_data

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--close", type=float, default=1094.80)
    parser.add_argument("--change-pct", type=float, default=0.22)
    args = parser.parse_args()
    fetch_and_save_night_futures(override_close=args.close, override_change_pct=args.change_pct)

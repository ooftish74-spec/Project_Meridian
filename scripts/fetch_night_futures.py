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

def fetch_and_save_night_futures(
    override_close: Optional[float] = None, 
    override_change_pct: Optional[float] = None,
    override_change: Optional[float] = None,
    override_daytime_close: Optional[float] = None
):
    """야간 선물 (KRX Night Session) 수집 후 저장."""
    logger.info("  🌙 [Night Futures] KRX 야간선물 수집 시작")
    
    # KRX Night Session is strictly CLOSED only on Saturday and Sunday
    now = datetime.now()
    is_weekend = now.weekday() in (5, 6)
    
    from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver
    front_code = DynamicFuturesContractResolver.get_current_front_month_code()
    symbol = DynamicFuturesContractResolver.to_kis_code(front_code)

    status = "CLOSED" if is_weekend else "OK"
    chg_val = 0.0 if is_weekend else (0.0 if override_change_pct is None else override_change_pct)
    c_val = 0.0 if override_close is None else override_close
    d_close = 0.0

    night_data = {
        "symbol": symbol,
        "front_code": front_code,
        "close": c_val,
        "daytime_close": d_close,
        "change_pct": chg_val,
        "change": 0.0 if override_change is None else override_change,
        "status": status,
        "source": "KRX_Night_Session",
        "timestamp": now.isoformat()
    }

    try:
        from src.data_collection.central_data_gateway import CentralDataGateway
        gateway = CentralDataGateway()
        ov_data = gateway.get_overnight_futures()
        # Accept valid non-zero quote
        if isinstance(ov_data, dict) and ov_data.get("close", 0) > 0:
            existing_file = PROJECT_ROOT / "results" / "krx_futures_overnight.json"
            is_prev_verified = False
            if existing_file.exists():
                try:
                    with open(existing_file, "r", encoding="utf-8") as f:
                        prev = json.load(f)
                    if prev.get("source") == "KRX_Night_Session_Verified" and prev.get("close", 0) > 0:
                        is_prev_verified = True
                        night_data.update(prev)
                except Exception:
                    pass

            if is_prev_verified and ov_data.get("source") not in ("KRX_Night_Session_Verified", "KIS_OpenAPI_Futures_Live"):
                logger.info(f"  🛡️ [Verified Protection] 기검증된 야간 체결가({night_data['close']} / {night_data['source']})를 KIS 미갱신 시세로부터 보호합니다.")
            elif ov_data.get("source") == "KIS_OpenAPI_Direct_Daytime":
                night_data["daytime_close"] = ov_data["close"]
                logger.info(f"  ℹ️ [CentralDataGateway] 주간 선물 종가 확인 ({ov_data['close']}) — 야간 선물({night_data['close']}) 격리 유지")
            else:
                night_data.update(ov_data)
                night_data["status"] = "OK"
                logger.info(f"  ✅ [CentralDataGateway] 야간 선물 수집 완료: {ov_data}")
    except Exception as e:
        logger.warning(f"  ⚠️ CentralDataGateway 수집 우회: {e}")

    # Fallback to existing valid quote if no override provided
    if override_close is None and night_data.get("close", 0) <= 0:
        existing_file = PROJECT_ROOT / "results" / "krx_futures_overnight.json"
        if existing_file.exists():
            try:
                with open(existing_file, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                if prev.get("close", 0) > 0:
                    night_data.update(prev)
            except Exception:
                pass

    if override_close is not None:
        night_data["close"] = override_close
        if override_daytime_close is not None:
            d_close = override_daytime_close
        else:
            d_close = night_data.get("daytime_close", 0.0)
        if d_close <= 0:
            sc_path = PROJECT_ROOT / "results" / "signal_cache.json"
            if sc_path.exists():
                try:
                    with open(sc_path, "r", encoding="utf-8") as f:
                        sc_tmp = json.load(f)
                    d_close = float(sc_tmp.get("daytime_futures") or sc_tmp.get("kospi200_futures_close") or override_close)
                except Exception:
                    d_close = override_close
            else:
                d_close = override_close
        night_data["daytime_close"] = d_close

        night_change = round(override_close - d_close, 2)
        night_change_pct = round((night_change / d_close) * 100, 4) if d_close > 0 else 0.0
        night_data["change"] = override_change if override_change is not None else night_change
        night_data["change_pct"] = override_change_pct if override_change_pct is not None else night_change_pct
        night_data["direction"] = "up" if night_data["change"] > 0 else "down"
        night_data["source"] = "KRX_Night_Session_Verified"

    # Write to target paths
    macro_dir = PROJECT_ROOT / "data" / "macro"
    macro_dir.mkdir(parents=True, exist_ok=True)
    
    atomic_write_json(macro_dir / "night_futures.json", night_data, indent=2)
    atomic_write_json(PROJECT_ROOT / "results" / "krx_futures_overnight.json", night_data, indent=2)

    # Also update signal_cache.json for premarket calibration
    sc_path = PROJECT_ROOT / "results" / "signal_cache.json"
    # ★ Wave 1 Track A: 락 보유 merge-update 2단계 (파일이 있을 때만 — 기존 의미 유지)
    #   1) 야간선물 키 기록 → 2) OIS 재계산(디스크의 '새' 야간선물을 읽도록 — 과거엔 쓰기 전에 계산해
    #   직전 값으로 OIS를 계산하던 순서 버그) → 3) OIS 키 기록. 손상 파일은 덮어쓰지 않는다.
    if sc_path.exists():
        try:
            from src.utils.file_ops import safe_json_update

            class _NoFile(Exception):
                pass

            def _apply(upd):
                def _m(sc):
                    if not isinstance(sc, dict):
                        raise _NoFile()
                    sc.update(upd)
                    return sc
                return _m

            _nf_upd = {
                "night_futures": night_data["close"],
                "daytime_futures": night_data.get("daytime_close", night_data["close"]),
                "night_futures_change_1d": night_data["change_pct"],
            }
            try:
                safe_json_update(sc_path, _apply(_nf_upd), default=None, indent=2)

                # Recalculate OIS with true night futures
                try:
                    from src.intelligence.overnight_intelligence import OvernightIntelligenceScore
                    ois_calc = OvernightIntelligenceScore()
                    ois_res = ois_calc.calculate(include_premarket=True)
                    _ois_upd = json.loads(json.dumps({"ois": ois_res.get("ois", 50.0), "ois_details": ois_res},
                                                     ensure_ascii=False, default=str))
                    safe_json_update(sc_path, _apply(_ois_upd), default=None, indent=2)
                except Exception as _oe:
                    logger.warning(f"  ⚠️ OIS 재계산 예외: {_oe}")
            except _NoFile:
                pass
        except Exception as e:
            logger.warning(f"  ⚠️ signal_cache 갱신 예외: {e}")

    logger.info(f"  ✅ [Night Futures] KRX 야간선물 ({night_data.get('front_code', 'F')}, 종가: {night_data['close']} / 주간종가: {night_data.get('daytime_close', 1008.25)}, 등락률: {night_data['change_pct']:+.2f}%) 저장 완료")
    return night_data

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KRX 야간선물 시세 수집기")
    parser.add_argument("--close", type=float, default=None, help="야간선물 종가 직접 지정 (예: 987.60)")
    parser.add_argument("--daytime-close", type=float, default=None, help="주간선물 종가 직접 지정 (예: 1008.25)")
    parser.add_argument("--change-pct", type=float, default=None, help="야간선물 등락률 직접 지정 (예: -2.05)")
    parser.add_argument("--change", type=float, default=None, help="야간선물 등락폭 직접 지정 (예: -20.65)")
    args = parser.parse_args()
    fetch_and_save_night_futures(
        override_close=args.close, 
        override_daytime_close=args.daytime_close,
        override_change_pct=args.change_pct,
        override_change=args.change
    )

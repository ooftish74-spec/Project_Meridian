"""
Project Meridian — Fetch Night Futures & EWY Fallback
=====================================================
Reads the latest KIS Night Futures price recorded by the night monitor daemon.
If not available, falls back to EWY (iShares MSCI South Korea ETF).
Calculates the divergence (gap) between Night Futures and EWY if both are present.
"""
import sys
import os
import json
import logging
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_PROJECT_ROOT))
from src.utils.credential_manager import CredentialManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("fetch_night_futures")

OUTPUT_FILE = _PROJECT_ROOT / "data" / "macro" / "night_futures.json"

def fetch_night_data():
    logger.info("🌙 야간 시그널 종합 수집 시작...")
    
    krx_pct = None
    ewy_pct = None
    nasdaq_pct = None
    
    # 1. KIS 야간선물 (Primary) 수집
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        collector = KISDataCollector()
        krx_pct = collector.get_night_futures_close()
        if krx_pct is not None:
            logger.info(f"✅ [Primary] KIS 야간선물 수집 성공: {krx_pct:+.2f}%")
        else:
            logger.warning("⚠️ KIS 야간선물 데이터를 받아오지 못했습니다.")
    except Exception as e:
        logger.error(f"❌ KIS 야간선물 REST API 호출 에러: {e}")
        
    # 2. EWY 수집 (Fallback & Gap Analysis)
    logger.info("📊 EWY (iShares MSCI South Korea ETF) 대용치 수집 시도...")
    try:
        from src.data_collection.alpha_vantage_collector import collect_us_daily_ohlcv
        ewy = collect_us_daily_ohlcv('EWY')
        if ewy is not None and not ewy.empty and len(ewy) >= 2:
            prev_close = float(ewy['close'].iloc[-2])
            curr_close = float(ewy['close'].iloc[-1])
            ewy_pct = round(((curr_close - prev_close) / prev_close) * 100, 4)
            logger.info(f"✅ [AlphaVantage] EWY 수집 성공: {curr_close:.2f} ({ewy_pct:+.2f}%)")
        else:
            logger.warning("⚠️ EWY 데이터를 충분히 받아오지 못했습니다.")
    except Exception as e:
        logger.error(f"❌ EWY 수집 실패: {e}")
        
    # 3. 나스닥 (QQQ) 수집
    try:
        from src.data_collection.alpha_vantage_collector import collect_us_daily_ohlcv
        nq = collect_us_daily_ohlcv('QQQ')
        if nq is not None and not nq.empty and len(nq) >= 2:
            prev_nq = float(nq['close'].iloc[-2])
            curr_nq = float(nq['close'].iloc[-1])
            nasdaq_pct = round(((curr_nq - prev_nq) / prev_nq) * 100, 2)
            logger.info(f"✅ [AlphaVantage] QQQ 수집 성공: {nasdaq_pct:+.2f}%")
        else:
            nasdaq_pct = 0.0
    except Exception as e:
        logger.error(f"❌ QQQ 수집 실패: {e}")
        nasdaq_pct = 0.0

    # 4. 종합 및 Gap Analysis
    final_pct = 0.0
    gap = 0.0
    source = "UNKNOWN"
    
    if krx_pct is not None and ewy_pct is not None:
        final_pct = krx_pct
        source = "KRX_NIGHT_FUTURES"
        gap = round(abs(krx_pct - ewy_pct), 4)
        logger.info(f"🔍 두 지표 모두 수집됨. Gap(괴리율): {gap}p (KRX: {krx_pct:+.2f}%, EWY: {ewy_pct:+.2f}%)")
    elif krx_pct is not None:
        final_pct = krx_pct
        source = "KRX_NIGHT_FUTURES"
        logger.info("🔍 KRX 야간선물만 수집됨.")
    elif ewy_pct is not None:
        final_pct = ewy_pct
        source = "EWY_FALLBACK"
        logger.info("🔍 EWY만 수집됨 (Fallback 적용).")
    else:
        logger.error("🚨 모든 야간 데이터 수집 실패! (Fail-Safe 0.0%)")
        final_pct = 0.0
        source = "FAIL_SAFE"

    # 5. 저장
    payload = {
        "timestamp": datetime.now().isoformat(),
        "final_pct": final_pct,
        "kospi200_night_futures_pct": final_pct,
        "source": source,
        "krx_pct": krx_pct,
        "ewy_pct": ewy_pct,
        "gap": gap,
        "nasdaq_pct": nasdaq_pct
    }
    
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    from src.utils.file_ops import atomic_write_json

    atomic_write_json(OUTPUT_FILE, payload, indent=4)
    logger.info(f"💾 저장 완료: {OUTPUT_FILE} (Final: {final_pct:+.2f}%)")

if __name__ == '__main__':
    fetch_night_data()

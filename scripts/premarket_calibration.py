#!/usr/bin/env python3
"""
Premarket Calibration Script (08:50 AM)
야간 선물 수익률과 KOSPI 동시호가 예상 등락률 간의 괴리를 분석하여,
비정상적인 갭(Fake Gap-Up / Sudden Crash)이 발생할 경우 시그널(Kelly Fraction)을 강제로 삭감합니다.
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime

base = Path(__file__).parent.parent
sys.path.append(str(base))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('PremarketCalibration')

def _get_expected_kospi_gap():
    """
    KIS API (FHKST01010100)를 호출하여 KODEX 200 (069500)의 동시호가 예상 등락률을 수집.
    """
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        logger.info("  📡 [KIS API] KODEX 200 예상 체결가 조회 (TR: FHKST01010100) ...")
        collector = KISDataCollector()
        data = collector.get_current_price('069500')
        if data:
            antc_change = float(data.get('antc_change_pct', 0.0))
            if antc_change != 0.0:
                logger.info(f"  ✅ [HOTFIX] 동시호가 예상체결 등락률 수집 성공: {antc_change:+.2f}%")
                return antc_change
            elif 'change_pct' in data:
                return float(data['change_pct'])
        return 0.0 
    except Exception as e:
        logger.warning(f"  KIS API 예상 체결가 조회 실패 (fallback to 0.0): {e}")
        return 0.0

def _get_expected_stock_gap(ticker: str):
    """개별 종목의 동시호가 예상 등락률 수집."""
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        collector = KISDataCollector()
        data = collector.get_current_price(ticker)
        if data:
            antc_change = float(data.get('antc_change_pct', 0.0))
            if antc_change != 0.0:
                return antc_change
            elif 'change_pct' in data:
                return float(data['change_pct'])
        return 0.0
    except Exception:
        return 0.0

def run_calibration():
    logger.info("==================================================")
    logger.info(f" 🌅 08:50 Premarket Calibration Started")
    logger.info("==================================================")
    
    signals_file = base / 'results' / 'latest_signals.json'
    if not signals_file.exists():
        logger.error("  🚨 latest_signals.json 파일이 없습니다. 모닝 파이프라인(07:50)이 실패했거나 실행되지 않았습니다.")
        return

    from src.utils.file_ops import atomic_write_json


    with open(signals_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if not data or 'signals' not in data:
        logger.warning("  ⚠️ 시그널이 비어있습니다. (Exit-Only 모드 등). Calibration 생략.")
        return

    signals = data['signals']

    # ----------------------------------------------------
    # 1. Macro Calibration (KOSPI 200 vs Night Futures)
    # ----------------------------------------------------
    cache_file = base / 'data' / 'macro' / 'night_futures.json'
    night_futures_ret = 0.0
    if cache_file.exists():
        with open(cache_file, 'r') as f:
            cache = json.load(f)
            night_futures_ret = float(cache.get('final_pct', 0.0))
    
    expected_gap = _get_expected_kospi_gap()
    
    logger.info(f"  📊 야간 선물(Nasdaq) 등락률: {night_futures_ret:+.2f}%")
    logger.info(f"  📊 KOSPI 예상 체결 등락률 : {expected_gap:+.2f}%")
    
    divergence = expected_gap - night_futures_ret
    penalty_ratio = 1.0
    
    if night_futures_ret > 0 and expected_gap <= -1.0:
        logger.critical(f"  🚨 [MACRO ANOMALY] 야간선물 상승 불구 예상체결가 폭락! (Divergence: {divergence:+.2f}%)")
        penalty_ratio = 0.0
    elif abs(divergence) >= 1.5:
        logger.warning(f"  ⚠️ [MACRO DIVERGENCE] 글로벌 증시와 국내 동시호가 간 괴리 심각 (Divergence: {divergence:+.2f}%)")
        penalty_ratio = 0.5
        
    if penalty_ratio < 1.0:
        logger.info(f"  🛡️ 매크로 안전장치 가동: 전체 시그널 size_pct에 Penalty {penalty_ratio} 적용")
        for stream_id, stream_signals in signals.items():
            for sig in stream_signals:
                if 'size_pct' in sig:
                    sig['size_pct'] = round(sig['size_pct'] * penalty_ratio, 4)
    else:
        logger.info("  ✅ 매크로 이상징후 없음.")

    # ----------------------------------------------------
    # 2. Micro Calibration (S2 개별종목 갭 역이용)
    # ----------------------------------------------------
    logger.info("  🔍 [Micro Calibration] S2 개별 종목 동시호가 분석 시작...")
    s2_signals = signals.get('S2', [])
    modified_s2 = False
    for sig in s2_signals:
        if sig.get('direction', '') != 'long':
            continue
        ticker = sig.get('ticker')
        if not ticker:
            continue
        
        stock_gap = _get_expected_stock_gap(ticker)
        name = sig.get('name', ticker)
        original_size = sig.get('size_pct', 0.0)
        
        if stock_gap >= 3.0:
            # Unjustified Gap Up -> 추격 매수 금지 (Penalty)
            new_size = round(original_size * 0.5, 4)
            sig['size_pct'] = new_size
            logger.warning(f"  ⚠️ [S2 Gap-Up Penalty] {name}({ticker}) 갭상승 {stock_gap:+.2f}% 과열! 추격매수 방어 (size: {original_size:.3f} -> {new_size:.3f})")
            modified_s2 = True
        elif stock_gap <= -2.0:
            # Overreaction Gap Down -> 저점 매수 기회 (Boost)
            new_size = round(min(1.0, original_size * 1.3), 4)
            sig['size_pct'] = new_size
            logger.info(f"  🚀 [S2 Gap-Down Boost] {name}({ticker}) 갭하락 {stock_gap:+.2f}% 투매! 저점매수 증폭 (size: {original_size:.3f} -> {new_size:.3f})")
            modified_s2 = True
        else:
            logger.info(f"     └ {name}({ticker}) 동시호가 {stock_gap:+.2f}% (정상 범위)")

    if penalty_ratio < 1.0 or modified_s2:
        atomic_write_json(signals_file, data, indent=2)
        logger.info("  ✅ 교정된 시그널(latest_signals.json) 저장 완료.")
    else:
        logger.info("  ✅ 마이크로 이상징후 없음. 시그널 변동 없이 100% 유지.")
        
    logger.info("==================================================")
    logger.info(f" 🏁 08:50 Premarket Calibration Finished")
    logger.info("==================================================")

if __name__ == "__main__":
    run_calibration()

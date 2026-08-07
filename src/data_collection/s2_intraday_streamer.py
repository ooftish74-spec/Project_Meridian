#!/usr/bin/env python3
"""
S2 Intraday Streamer — KIS API 전용 실시간 가격 수집 데몬
=========================================================
메인 매크로 리프레셔(macro_realtime_refresher.py)의 블로킹을 방지하기 위해,
s2_universe.json에 지정된 5~10종목의 가격을 독립적으로 KIS API를 통해 폴링합니다.

이 스크립트는 장중에만 실행되며, yfinance 의존성을 완전히 제거합니다.
"""

import sys
import time
import json
import logging
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import setup_logger
from src.infra.safe_io import safe_json_update
from src.data_collection.kis_data_collector import KISDataCollector
from config.dynamic_config import DynamicConfig

logger = setup_logger('s2_streamer')
cfg = DynamicConfig()
_RESULTS_DIR = _PROJECT_ROOT / 'results'
_S2_UNIVERSE_FILE = _RESULTS_DIR / 's2_universe.json'
_SIGNAL_CACHE_FILE = _RESULTS_DIR / 'signal_cache.json'

def get_trading_hours() -> bool:
    """한국장 거래 시간 확인 (09:00 ~ 15:30)"""
    now = datetime.now()
    if now.hour == 9 and now.minute >= 0: return True
    if 10 <= now.hour <= 14: return True
    if now.hour == 15 and now.minute <= 30: return True
    return False

def run_streamer():
    logger.info("🚀 S2 Intraday Streamer 시작 (KIS API 전용, yfinance 퇴출 완료)")
    
    try:
        kis = KISDataCollector()
    except Exception as e:
        logger.error(f"KISDataCollector 초기화 실패: {e}")
        return

    while True:
        if not get_trading_hours():
            logger.info("장외 시간 대기 중...")
            time.sleep(60)
            continue
            
        if not _S2_UNIVERSE_FILE.exists():
            logger.warning(f"S2 유니버스 파일이 없음: {_S2_UNIVERSE_FILE}")
            time.sleep(30)
            continue
            
        try:
            tickers = json.loads(_S2_UNIVERSE_FILE.read_text())
        except Exception as e:
            logger.error(f"S2 유니버스 읽기 오류: {e}")
            time.sleep(10)
            continue
            
        if not tickers:
            logger.info("S2 유니버스가 비어 있음.")
            time.sleep(30)
            continue
            
        logger.info(f"🔄 S2 타겟 {len(tickers)}종목 KIS 실시간 호가 스니핑 시작...")
        
        # KIS API로 최신 호가 수집
        updated_stocks = {}
        for ticker in tickers:
            # [Red Team V6] API 도미노 붕괴 방어막 (Exponential Backoff)
            # 단순 time.sleep 대신 실패 시 대기 시간을 지수적으로 늘리며 재시도 (최대 5회)
            max_retries = 5
            base_delay = float(cfg.get('data.kis_rate_limit_delay', 0.15))
            for attempt in range(max_retries):
                try:
                    quote = kis.get_current_price(ticker)
                    if quote and quote.get('price', 0) > 0:
                        updated_stocks[ticker] = {
                            'close': float(quote['price']),
                            'name': ticker
                        }
                    # 정상 수집 완료 시 기본 딜레이 후 다음 종목으로
                    time.sleep(base_delay)
                    break
                except Exception as e:
                    err_msg = str(e).lower()
                    if '초과' in err_msg or 'limit' in err_msg or '429' in err_msg or 'timeout' in err_msg:
                        backoff = base_delay * (2 ** attempt)
                        logger.warning(f"  ⚠️ [API 방어막] S2 KIS Rate Limit 감지 ({ticker}). {attempt+1}/{max_retries} 재시도 준비... ({backoff:.2f}s 대기)")
                        time.sleep(backoff)
                    else:
                        logger.warning(f"  ⚠️ S2 종목 KIS 갱신 실패 ({ticker}): {e}")
                        break
            else:
                logger.critical(f"  🚨 [API 방어막] {ticker} 갱신 최종 실패 (최대 재시도 {max_retries}회 초과)")
        if updated_stocks:
            # [Red Team V5] 다중 프로세스 충돌 방지 (TOC/TOU Data Race 제거)
            def _update_cache(cache: dict) -> dict:
                st = cache.get('stock_technicals', {})
                for k, v in updated_stocks.items():
                    if k not in st:
                        st[k] = {}
                    st[k].update(v)
                cache['stock_technicals'] = st
                cache['s2_refresh_ts'] = datetime.now().isoformat()
                return cache
                
            success = safe_json_update(_SIGNAL_CACHE_FILE, _update_cache)
            if success:
                logger.info(f"  ✅ S2 유니버스 {len(updated_stocks)}종목 실시간 갱신 완료 (safe_json_update)")
            else:
                logger.error(f"  ❌ S2 유니버스 갱신 실패 (File Lock 획득 실패)")
            
        # [Red Team V4] 폴링 주기 (동적 설정)
        poll_interval = float(cfg.get('data.s2_kis_polling_interval_sec', 3.0))
        time.sleep(poll_interval)

if __name__ == '__main__':
    run_streamer()

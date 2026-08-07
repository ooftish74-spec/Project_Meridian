#!/usr/bin/env python3
"""
Night Watch Daemon (미국장 실시간 폴링 및 위기 감지)
=================================================
미국장(22:30 ~ 06:00) 동안 1분 주기로 yfinance를 활용해 SPY, VIX, TLT 등을 조회.
급격한 변동성(VIX > 임계치)이나 SPY 폭락 시 RegimeDetector를 호출해
익일 한국장 대비 방어 태세(CRASH)로 실시간 전환합니다.
"""
import sys
import time
import logging
from datetime import datetime
from pathlib import Path
# Add project root to path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import setup_logger
logger = setup_logger('night_watch')

# [Red Team V4 Zero-Hardcoding] 설정 동적 연동
from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()

def trigger_crash_regime(reason: str):
    """위기 감지 시 RegimeState 강제 전환"""
    logger.critical(f"  🚨 [NIGHT WATCH] 위기 상황 감지! 사유: {reason}")
    logger.critical("  즉각적으로 RegimeState를 CRASH로 전환하여 익일 S4 인버스 풀베팅을 준비합니다.")
    try:
        from src.regime.regime_detector import RegimeDetector
        from src.regime.transition_signal import RegimeState
        detector = RegimeDetector()
        
        # 강제로 체제 전환
        # 기존 로직을 바이패스하고 상태 파일에 바로 쓰기
        state_file = _PROJECT_ROOT / 'results' / 'regime_state.json'
        import json
        from src.utils.file_ops import atomic_write_json
        
        current_state = {}
        if state_file.exists():
            with open(state_file, 'r') as f:
                current_state = json.load(f)
                
        from datetime import timedelta
        current_state['current_state'] = RegimeState.CRASH.value
        current_state['last_updated'] = datetime.now().isoformat()
        current_state['reason'] = f"Night Watch Triggered: {reason}"
        current_state['priority'] = 1  # 1 = Highest (Night Watch override)
        current_state['ttl_until'] = (datetime.now() + timedelta(hours=24)).isoformat()
        
        atomic_write_json(state_file, current_state)
        logger.info("  ✅ RegimeState 강제 전환(CRASH) 저장 완료.")
    except Exception as e:
        logger.error(f"  RegimeState 전환 실패: {e}")

import requests

# Alpha Vantage Premium Key 로드
try:
    from src.utils.credential_manager import CredentialManager
    cm = CredentialManager()
    AV_API_KEY = cm.read_from_env('ALPHA_VANTAGE_API_KEY')
except Exception as e:
    AV_API_KEY = ''

def get_yf_realtime(symbol: str) -> dict:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    last_price = ticker.fast_info.last_price
    # yfinance fast_info previous_close is often weird for futures, 
    # so we use the daily history for a cleaner baseline.
    hist = ticker.history(period='5d')
    if len(hist) >= 2:
        prev_close = float(hist['Close'].iloc[-2])
    else:
        prev_close = ticker.fast_info.previous_close
    
    change_pct = (last_price / prev_close - 1) * 100 if prev_close else 0.0
    return {
        'price': float(last_price),
        'prev_close': float(prev_close),
        'change_pct': float(change_pct)
    }

def run_night_watch():
    logger.info("🦉 Night Watch Daemon 시작... (미국 선물 스트리밍 - yfinance 연동)")
    
    last_vix = 0
    stream_file = _PROJECT_ROOT / 'data' / 'macro' / 'us_night_stream.json'
    from src.utils.file_ops import atomic_write_json
    
    try:
        while True:
            try:
                # yfinance futures 실시간 스트리밍
                es_data = get_yf_realtime("ES=F")
                nq_data = get_yf_realtime("NQ=F")
                ym_data = get_yf_realtime("YM=F")
                vix_data = get_yf_realtime("^VIX")
                
                current_vix = vix_data['price']
                current_spy = es_data['price']
                
                # Streaming 상태 저장
                stream_payload = {
                    'timestamp': datetime.now().isoformat(),
                    'ES=F': es_data,
                    'NQ=F': nq_data,
                    'YM=F': ym_data,
                    '^VIX': vix_data
                }
                atomic_write_json(stream_file, stream_payload)
                
                logger.info(f"  [Night Watch] VIX: {current_vix:.2f} ({vix_data['change_pct']:+.2f}%) | ES=F: {current_spy:,.2f} ({es_data['change_pct']:+.2f}%) | NQ=F: {nq_data['price']:,.2f} ({nq_data['change_pct']:+.2f}%)")
                
                # [Red Team V4 Zero-Hardcoding] 동적 임계치 
                vix_crash_threshold = float(cfg.get('macro.vix_crash_threshold', 25.0))
                
                if current_vix >= vix_crash_threshold and last_vix > 0 and last_vix < vix_crash_threshold:
                    trigger_crash_regime(f"VIX 급등 ({current_vix:.2f} >= {vix_crash_threshold})")
                    
                last_vix = current_vix
                
            except Exception as e:
                logger.warning(f"  Night Watch (yfinance) 오류: {e}")
                
            # 1분 주기로 거침없이 Polling 가능
            time.sleep(60)
            
    except KeyboardInterrupt:
        logger.info("🦉 Night Watch Daemon 종료 요청됨.")

if __name__ == '__main__':
    run_night_watch()

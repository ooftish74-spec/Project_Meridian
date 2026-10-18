#!/usr/bin/env python3
"""
Project Meridian — API Health Check
===================================
매일 오전 6시(또는 파이프라인 최상단)에 실행되어 KIS API 상태를 사전 점검합니다.
인증 실패, 타임아웃, 토큰 만료 임박 시 텔레그램으로 경고(Early Warning)를 발송합니다.
"""

import sys
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# .env 자동 로드
_env_file = _PROJECT_ROOT / '.env'
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _, _val = _line.partition('=')
                os.environ[_key.strip()] = _val.strip()

from src.utils.logger import setup_logger
from src.execution._kis_adapter import KISTraderAdapter
from src.utils.telegram_notifier import TelegramNotifier

logger = setup_logger('api_health_check')

import json

def check_api_health():
    logger.info("🔍 KIS API Health Check 시작...")
    
    bot = TelegramNotifier()
    status_file = _PROJECT_ROOT / 'results' / 'api_health_status.json'
    
    # 기존 상태 로드
    prev_status = {}
    if status_file.exists():
        try:
            prev_status = json.loads(status_file.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            pass
            
    today_str = datetime.now().strftime('%Y-%m-%d')
    last_notified_date = prev_status.get('last_success_notified_date', '')
    prev_state = prev_status.get('status', 'UNKNOWN')

    fail_count = int(prev_status.get('consecutive_failures', 0))

    # 상태 저장 및 알림 발송 헬퍼 함수
    def save_and_notify(is_success: bool, msg: str, token_expires=None):
        nonlocal fail_count
        if is_success:
            current_fail_count = 0
            current_state = 'OK'
        else:
            current_fail_count = fail_count + 1
            current_state = 'ERROR' if current_fail_count >= 3 else prev_state

        # 텔레그램 발송 조건 판별 (스팸 완전 차단)
        should_notify = False
        if not is_success:
            # 3회 연속 실패 시에만 정식 에러 알림 발송 (단발성 네트워크 오류 억제)
            if current_fail_count == 3:
                should_notify = True
                msg = f"🚨 [API Health Check 지속 장애] KIS API 3회 연속 접속 실패: {msg}"
        else:
            # 성공 시: 3회 이상 장애(ERROR) 상태였다가 처음 복구된 경우에만 복구 알림 발송
            if prev_state == 'ERROR':
                should_notify = True
                msg = f"✅ [API Health Check 장애 복구] KIS API 정상 작동 복구 완료"

        # 대시보드 표시용 상태 파일 저장 (매번 갱신)
        status_data = {
            'status': current_state,
            'consecutive_failures': current_fail_count,
            'timestamp': datetime.now().isoformat(),
            'message': msg,
            'token_expires': token_expires.isoformat() if token_expires else None,
            'last_success_notified_date': last_notified_date,
            'token_warn_date': prev_status.get('token_warn_date', ''),
        }
        try:
            status_file.parent.mkdir(parents=True, exist_ok=True)
            from src.utils.file_ops import atomic_write_json
            atomic_write_json(status_file, status_data)
        except Exception as e:
            logger.warning(f"상태 파일 저장 실패: {e}")

        if should_notify:
            bot.send_message(msg)

    # 1. KISTraderAdapter 초기화
    try:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        
        # mock 모드가 아닌 실계좌(또는 모의투자) 모드로 테스트
        mode = os.environ.get('KIS_MODE', 'live')
        
        prefix = 'KIS_PAPER' if mode == 'paper' else 'KIS'
        app_key = cm.read_from_env(f"{prefix}_APP_KEY")
        app_secret = cm.read_from_env(f"{prefix}_APP_SECRET")
        account_no = cm.read_from_env(f"{prefix}_ACCOUNT_NO")
        
        adapter = KISTraderAdapter(
            mode=mode,
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no
        )
        
        # 2. 인증 시도
        auth_success = adapter.authenticate()
        
        if not auth_success:
            err_msg = "🚨 [API Health Check 실패] KIS API 인증에 실패했습니다! APP_KEY 또는 토큰을 확인하세요."
            logger.error(err_msg)
            save_and_notify(False, err_msg)
            return False
            
        # 3. 토큰 만료 임박 시 선제 재발급 시도 (장 개장 전 만료 원천 차단)
        token_expires = getattr(adapter, '_token_expires', None)
        if token_expires:
            time_left = token_expires - datetime.now()
            if time_left < timedelta(hours=2):
                logger.info(f"  🔄 [API Health Check] KIS Token 만료 임박 ({max(0, int(time_left.total_seconds() // 60))}분 남음) → 선제 재발급 실행")
                try:
                    if hasattr(adapter, '_request_new_token') and adapter._request_new_token():
                        token_expires = getattr(adapter, '_token_expires', token_expires)
                        logger.info(f"  ✅ [API Health Check] KIS Token 선제 재발급 완료 (신규 만료: {token_expires})")
                    else:
                        warn_msg = f"⚠️ [API Health Check 경고] KIS Token 만료가 {time_left.seconds // 3600}시간 남았습니다."
                        logger.warning(warn_msg)
                except Exception as _re_e:
                    logger.warning(f"  ⚠️ KIS Token 선제 재발급 예외: {_re_e}")
                
        success_msg = "✅ [API Health Check] KIS API 정상 작동 확인"
        logger.info(success_msg)
        save_and_notify(True, success_msg, token_expires)
        return True
        
    except Exception as e:
        err_msg = f"🚨 [API Health Check 시스템 에러] {e}"
        logger.error(err_msg)
        save_and_notify(False, err_msg)
        return False

if __name__ == '__main__':
    success = check_api_health()
    if not success:
        sys.exit(1)
    sys.exit(0)

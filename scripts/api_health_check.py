#!/usr/bin/env python3
"""
Project Meridian — API Health Check
===================================
매일 오전 6시(또는 파이프라인 최상단)에 실행되어 KIS API 상태를 사전 점검합니다.
인증 실패, 타임아웃, 토큰 만료 임박 시 텔레그램으로 경고(Early Warning)를 발송합니다.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# .env 자동 로드
import os
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
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
            
    today_str = datetime.now().strftime('%Y-%m-%d')
    last_notified_date = prev_status.get('last_success_notified_date', '')
    prev_state = prev_status.get('status', 'UNKNOWN')

    # 상태 저장 및 알림 발송 헬퍼 함수
    def save_and_notify(is_success: bool, msg: str, token_expires=None):
        current_state = 'OK' if is_success else 'ERROR'
        new_notified_date = last_notified_date
        
        # 텔레그램 발송 조건 판별 (스팸 방지)
        should_notify = False
        if not is_success:
            should_notify = True  # 에러는 무조건 발송
        else:
            # 성공 시: 상태가 에러에서 복구되었거나, 오늘 첫 발송인 경우에만
            if prev_state != 'OK' or last_notified_date != today_str:
                should_notify = True
                new_notified_date = today_str

        # 대시보드 표시용 상태 파일 저장 (매번 갱신)
        status_data = {
            'status': current_state,
            'timestamp': datetime.now().isoformat(),
            'message': msg,
            'token_expires': token_expires.isoformat() if token_expires else None,
            'last_success_notified_date': new_notified_date
        }
        try:
            status_file.parent.mkdir(parents=True, exist_ok=True)
            status_file.write_text(json.dumps(status_data, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"상태 파일 저장 실패: {e}")

        if should_notify:
            bot.send_message(msg)

    # 1. KISTraderAdapter 초기화
    try:
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        
        # mock 모드가 아닌 실계좌(또는 모의투자) 모드로 테스트
        mode = os.environ.get('KIS_MODE', 'mock')
        if mode == 'mock':
            logger.info("🔵 Mock 모드이므로 API 테스트를 패스합니다.")
            save_and_notify(True, "🔵 [API Health Check] Mock 모드 정상 작동 중")
            return True
            
        prefix = 'KIS_PAPER' if mode == 'paper' else 'KIS'
        app_key = cm.read_from_env(f'{prefix}_APP_KEY')
        app_secret = cm.read_from_env(f'{prefix}_APP_SECRET')
        account_no = cm.read_from_env(f'{prefix}_ACCOUNT_NO')
        
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
            
        # 3. 토큰 만료 임박 체크 (하루 1회 Debounce)
        token_expires = getattr(adapter, '_token_expires', None)
        if token_expires:
            time_left = token_expires - datetime.now()
            if time_left < timedelta(hours=2):
                warn_msg = f"⚠️ [API Health Check 경고] KIS Token 만료가 {time_left.seconds // 3600}시간 남았습니다."
                logger.warning(warn_msg)
                # [Debounce] 오늘 이미 경고를 발송한 경우 스킵 (스팸 방지)
                _token_warn_sent_date = prev_status.get('token_warn_date', '')
                if _token_warn_sent_date != today_str:
                    bot.send_message(warn_msg)
                    # 오늘 발송 기록 저장 (status_file에 추가 기록)
                    try:
                        _warn_state = {}
                        if status_file.exists():
                            _warn_state = json.loads(status_file.read_text())
                        _warn_state['token_warn_date'] = today_str
                        status_file.write_text(json.dumps(_warn_state, indent=2, ensure_ascii=False))
                        logger.info(f"  📲 토큰 만료 경고 발송 완료 (오늘: {today_str})")
                    except Exception as _we:
                        logger.debug(f"  token_warn_date 기록 실패: {_we}")
                else:
                    logger.info(f"  ⏳ 토큰 만료 경고 Debounce — 오늘({today_str}) 이미 발송됨. 스킵.")
                
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

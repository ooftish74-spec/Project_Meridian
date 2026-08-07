"""
src/utils/emergency_pager.py
==============================
Project Meridian — Emergency Pager
====================================
[Phase 43: Zero-Tolerance Execution Architecture]

자금 집행 레이어에서 치명적 에러 발생 시 운용역에게
즉각 텔레그램 긴급 알람을 발송하는 Emergency Pager.

설계 원칙:
    1. Fire-and-Forget: 텔레그램 발송 자체가 실패해도 메인 루프 차단 금지
    2. Timeout 방어: requests timeout=4초, 백그라운드 스레드 이용
    3. 로컬 Fallback: 발송 실패 시 logs/emergency_pager.log에 기록
    4. 중복 억제: 동일 메시지 1분 내 재발송 차단 (Rate Limiter)

Usage:
    from src.utils.emergency_pager import send_emergency_page
    send_emergency_page("🚨 [FATAL] 잔고 조회 실패", exc_info=exc)
    send_emergency_page(fatal_error)   # ExecutionFatalError 직접 전달
"""
from __future__ import annotations
import hashlib
import logging
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_FILE = _PROJECT_ROOT / 'logs' / 'emergency_pager.log'
_sent_hashes: dict[str, float] = {}
_RATE_LIMIT_SEC = 60.0
_lock = threading.Lock()

def _hash_msg(message: str) -> str:
    return hashlib.md5(message[:200].encode(), usedforsecurity=False).hexdigest()

def _is_rate_limited(message: str) -> bool:
    h = _hash_msg(message)
    now = time.monotonic()
    with _lock:
        last = _sent_hashes.get(h, 0.0)
        if now - last < _RATE_LIMIT_SEC:
            return True
        _sent_hashes[h] = now
    return False

def _write_local_log(message: str, exc_text: str='') -> None:
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat()
        line = f'[{ts}] {message}'
        if exc_text:
            line += f'\n--- TRACEBACK ---\n{exc_text}\n---'
        with _LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

def _send_telegram_async(message: str) -> None:
    """백그라운드 스레드에서 텔레그램 발송 — 메인 스레드를 절대 차단하지 않음."""

    def _worker():
        try:
            import requests as _req
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
            bot_token = cm.read_from_env('TELEGRAM_BOT_TOKEN') or ''
            chat_id = cm.read_from_env('TELEGRAM_CHAT_ID') or ''
            if not (bot_token and chat_id):
                logger.warning('  [EmergencyPager] 텔레그램 자격증명 없음 → 로컬 로그만 기록')
                return
            url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
            payload = {'chat_id': chat_id, 'text': message[:4096], 'parse_mode': 'Markdown'}
            resp = _req.post(url, json=payload, timeout=4)
            if resp.ok:
                logger.info('  ✉️ [EmergencyPager] 텔레그램 발송 OK')
            else:
                logger.warning(f'  [EmergencyPager] 텔레그램 발송 실패: HTTP {resp.status_code}')
        except Exception as _tg_e:
            logger.warning(f'  [EmergencyPager] 텔레그램 예외: {_tg_e}')
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def send_emergency_page(message: Union[str, Exception], exc_info: Optional[BaseException]=None, stream_id: str='') -> None:
    """[Phase 43] 자금 집행 치명적 에러 → 운용역 긴급 알람.

    Args:
        message:   str 또는 ExecutionFatalError 인스턴스.
                   ExecutionFatalError이면 .as_page_text() 자동 포맷.
        exc_info:  함께 기록할 예외 객체 (exc_info=True 스타일 로그용).
        stream_id: 어느 스트림에서 발생했는지 (S1~S6 등).

    이 함수는 절대 예외를 raise 하지 않습니다.
    텔레그램 발송 실패 시 로컬 로그에만 기록하고 즉시 반환합니다.
    """
    try:
        _execution_fatal = _try_import_fatal()
        if _execution_fatal and isinstance(message, _execution_fatal):
            text = message.as_page_text()
            if not exc_info:
                exc_info = message
        else:
            text = str(message)
        if stream_id:
            text = f'[stream={stream_id}]\n' + text
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S KST')
        text = f'*🚨 MERIDIAN EMERGENCY*\n`{ts}`\n\n{text}'
        exc_text = ''
        if exc_info and isinstance(exc_info, BaseException):
            exc_text = ''.join(traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__))
            text += f'\n\n```\n{exc_text[:1500]}\n```'
    except Exception as _fmt_e:
        text = f'🚨 MERIDIAN EMERGENCY (포맷 실패): {message}'
        exc_text = ''
        logger.warning(f'  [EmergencyPager] 메시지 포맷 실패: {_fmt_e}')
    logger.error(f'  [EMERGENCY_PAGE] {text[:200]}', exc_info=exc_info is not None)
    _write_local_log(text, exc_text)
    if _is_rate_limited(text):
        logger.debug('  [EmergencyPager] 중복 메시지 억제 (60s 내 동일 알람)')
        return
    _send_telegram_async(text)

def _try_import_fatal():
    """ExecutionFatalError를 순환 임포트 없이 lazy 로드."""
    try:
        from src.execution.exceptions import ExecutionFatalError
        return ExecutionFatalError
    except ImportError as e:
        from src.utils.error_logger import log_warning_rate_limited
        log_warning_rate_limited(__name__, f"⚠️ [Fallback] 파일/모듈 누락 예외 우회: {e}")
        return None
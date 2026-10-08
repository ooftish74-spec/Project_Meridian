"""
Project Meridian — Telegram Notifier
====================================
텔레그램 봇을 활용하여 시스템 알림(수익률, 경고, 긴급 중단)을 전송.

Usage:
    from src.utils.telegram_notifier import TelegramNotifier
    notifier = TelegramNotifier()
    notifier.send_message("🚨 긴급 매도 절차 개시!")
"""

import os
import requests
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

import time
_SENT_ALERTS_CACHE = {}

class TelegramNotifier:
    def __init__(self):
        self._root_dir = Path(__file__).resolve().parent.parent.parent
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        self.bot_token = cm.read_from_env("TELEGRAM_BOT_TOKEN") or ""
        self.chat_id = cm.read_from_env("TELEGRAM_CHAT_ID") or ""

        self.enabled = bool(self.bot_token and self.chat_id)

    def send_message(self, message: str) -> bool:
        if not self.enabled:
            logger.debug("텔레그램 알림이 비활성화되어 있습니다 (토큰/ChatID 없음).")
            return False

        # 🎯 [SSoT Spam Defense Rule] 타임스탬프를 제외한 핵심 메시지 기준 15분(900초) 이내 동일 메시지 발송 100% 원천 차단
        import re
        clean_msg = re.sub(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', '', message)
        msg_key = clean_msg[:120]
        now = time.time()
        last_sent = _SENT_ALERTS_CACHE.get(msg_key, 0.0)
        if now - last_sent < 900.0:  # 15분 쿨다운
            logger.debug(f"[TELEGRAM_SPAM_PREVENTED] Duplicate message suppressed: {msg_key[:40]}...")
            return False
        _SENT_ALERTS_CACHE[msg_key] = now

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            logger.info("  ✉️ 텔레그램 알림 전송 성공")
            return True
        except Exception as e:
            logger.error(f"  ❌ 텔레그램 알림 전송 실패: {e}")
            return False

    def send_alert(self, title: str, details: str):
        """중요 경고 알림 전송 (15분 스팸 방지 쿨다운 적용)"""
        msg_key = f"{title}_{details[:40]}"
        now = time.time()
        last_sent = _SENT_ALERTS_CACHE.get(msg_key, 0.0)
        if now - last_sent < 900.0:  # 15분 쿨다운
            logger.debug(f"[TELEGRAM_SPAM_PREVENTED] Duplicate alert suppressed: {title}")
            return False
        _SENT_ALERTS_CACHE[msg_key] = now
        msg = f"🚨 *{title}*\n\n{details}"
        return self.send_message(msg)

    def send_info(self, title: str, details: str):
        """일반 정보성 알림 전송 (5분 스팸 방지 쿨다운 적용)"""
        msg_key = f"INFO_{title}_{details[:40]}"
        now = time.time()
        last_sent = _SENT_ALERTS_CACHE.get(msg_key, 0.0)
        if now - last_sent < 300.0:  # 5분 쿨다운
            logger.debug(f"[TELEGRAM_SPAM_PREVENTED] Duplicate info suppressed: {title}")
            return False
        _SENT_ALERTS_CACHE[msg_key] = now
        msg = f"ℹ️ *{title}*\n\n{details}"
        return self.send_message(msg)

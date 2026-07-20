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
        """중요 경고 알림 전송"""
        msg = f"🚨 *{title}*\n\n{details}"
        return self.send_message(msg)

    def send_info(self, title: str, details: str):
        """일반 정보성 알림 전송"""
        msg = f"ℹ️ *{title}*\n\n{details}"
        return self.send_message(msg)

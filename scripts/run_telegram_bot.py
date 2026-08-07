#!/usr/bin/env python3
"""
Meridian 텔레그램 양방향 통신 봇
- /kill : 긴급 매매 중단
- /status : 현재 계좌 잔고, 일일 수익률, 마켓 레짐 상태
- /report : V1 야간 리포트(S4 Advisory) 즉시 발송
"""

import os
import sys
import time
import json
import subprocess
import requests
from pathlib import Path
import logging

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("MeridianBot")

# 환경 변수 로드 (systemd에서 주입됨)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN or not ALLOWED_CHAT_ID:
    # 혹시 환경변수가 안 들어왔다면 CredentialManager 시도
    from src.utils.credential_manager import CredentialManager
    cm = CredentialManager()
    TOKEN = cm.read_from_env("TELEGRAM_BOT_TOKEN")
    ALLOWED_CHAT_ID = cm.read_from_env("TELEGRAM_CHAT_ID")

if not TOKEN or not ALLOWED_CHAT_ID:
    logger.error("🚨 TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing!")
    sys.exit(1)

URL = f"https://api.telegram.org/bot{TOKEN}/"

def get_python_cmd():
    venv_python = _PROJECT_ROOT / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return "python3"

def send_message(text: str):
    try:
        requests.post(URL + "sendMessage", json={"chat_id": ALLOWED_CHAT_ID, "text": text})
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

def handle_kill():
    send_message("🚨 [Kill Switch] 가동 시작. 모든 매매 파이프라인을 중지합니다.")
    try:
        subprocess.run([get_python_cmd(), "scripts/execute_hard_exit.py"], check=False)
        send_message("✅ [Kill Switch] 시스템 셧다운 완료.")
    except Exception as e:
        send_message(f"❌ [Kill Switch] 실패: {e}")

def handle_status():
    try:
        # 상태 정보 읽기
        signal_file = _PROJECT_ROOT / "results" / "signal_cache.json"
        shadow_file = _PROJECT_ROOT / "results" / "shadow_portfolio.json"
        
        regime = "알 수 없음"
        if signal_file.exists():
            with open(signal_file, "r") as f:
                sig_data = json.load(f)
                regime = sig_data.get("regime", "알 수 없음")
                
        nav = 0
        if shadow_file.exists():
            with open(shadow_file, "r") as f:
                port_data = json.load(f)
                nav = port_data.get("nav", 0)
                
        msg = (
            f"📊 **Meridian Status** 📊\n"
            f"• 마켓 레짐: `{regime.upper()}`\n"
            f"• 추정 NAV: `₩{nav:,.0f}`\n"
        )
        send_message(msg)
    except Exception as e:
        send_message(f"❌ 상태 조회 실패: {e}")

def handle_report():
    send_message("📝 V1 S4 Advisory 리포트 생성을 시작합니다...")
    try:
        result = subprocess.run([get_python_cmd(), "scripts/generate_s4_advisory.py"], capture_output=True, text=True, check=True)
        report_text = result.stdout.strip()
        
        if not report_text:
            report_text = "⚠️ 리포트 내용이 비어 있습니다."
            
        # 텔레그램 길이 제한을 고려해 분할 전송
        for i in range(0, len(report_text), 4000):
            send_message(report_text[i:i+4000])
            
        send_message("✅ 리포트 발송 완료.")
    except Exception as e:
        send_message(f"❌ 리포트 생성 실패: {e}")

def get_updates(offset=None):
    try:
        url = URL + "getUpdates"
        params = {"timeout": 30}
        if offset:
            params["offset"] = offset
        r = requests.get(url, params=params, timeout=40)
        return r.json()
    except Exception as e:
        logger.error(f"getUpdates error: {e}")
        return None

def main():
    logger.info("🤖 Meridian Telegram Bot Started.")
    send_message("🤖 Meridian 봇이 시작되었습니다. 명령어: /status, /report, /kill")
    
    offset = None
    while True:
        updates = get_updates(offset)
        if updates and updates.get("ok"):
            for item in updates["result"]:
                offset = item["update_id"] + 1
                msg = item.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                
                if chat_id != str(ALLOWED_CHAT_ID):
                    continue
                
                text = msg.get("text", "").strip().lower()
                if text == "/kill":
                    handle_kill()
                elif text == "/status":
                    handle_status()
                elif text == "/report":
                    handle_report()
                
        time.sleep(1)

if __name__ == "__main__":
    main()

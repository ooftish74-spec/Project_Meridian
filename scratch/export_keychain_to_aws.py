#!/usr/bin/env python3
"""
[자동화 스크립트] macOS Keychain 추출 -> AWS SCP 주입
이 스크립트는 로컬의 CredentialManager를 이용해 모든 민감 키를 뽑아내어
임시 .env 파일을 만들고 AWS 인스턴스로 안전하게 복사한 뒤 즉시 파기합니다.
"""

import os
import sys
import subprocess
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.credential_manager import CredentialManager, SENSITIVE_KEYS

AWS_HOST = "ubuntu@ip-172-31-53-32.tail865438.ts.net"
AWS_PATH = "/home/ubuntu/Project_Meridian/.env"
SSH_KEY = str(Path.home() / ".ssh" / "meridian-key.pem")
TEMP_ENV_PATH = _PROJECT_ROOT / ".env.tmp.meridian"

def main():
    print("🔐 [1/3] 로컬 Keychain에서 민감 키를 추출합니다...")
    cm = CredentialManager()
    
    extracted_keys = {}
    for key in SENSITIVE_KEYS:
        val = cm.read_from_env(key)
        if val:
            extracted_keys[key] = val
    
    # 추가로 필요한 키들 (S8 계좌 등)
    extra_keys = [
        "KIS_PAPER_APP_KEY", "KIS_PAPER_APP_SECRET", "KIS_PAPER_ACCOUNT_NO",
        "KIS_PAPER_S8_APP_KEY", "KIS_PAPER_S8_APP_SECRET", "KIS_PAPER_S8_ACCOUNT_NO",
        "KIS_S8_APP_KEY", "KIS_S8_APP_SECRET", "KIS_S8_ACCOUNT_NO"
    ]
    for key in extra_keys:
        val = cm.read_from_env(key)
        if val:
            extracted_keys[key] = val
            
    if not extracted_keys:
        print("⚠️ 추출된 키가 없습니다!")
        return

    print(f"✅ {len(extracted_keys)}개의 키를 추출했습니다.")
    
    # 환경 설정
    extracted_keys["ENVIRONMENT"] = "production"
    
    # 임시 파일 작성
    with open(TEMP_ENV_PATH, "w", encoding="utf-8") as f:
        for k, v in extracted_keys.items():
            f.write(f"{k}={v}\n")
            
    print(f"🚀 [2/3] AWS 서버({AWS_HOST})로 SCP 전송을 시작합니다...")
    try:
        scp_cmd = [
            "scp",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-i", SSH_KEY,
            str(TEMP_ENV_PATH),
            f"{AWS_HOST}:{AWS_PATH}"
        ]
        
        result = subprocess.run(scp_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ AWS 서버 전송 성공!")
        else:
            print(f"❌ 전송 실패: {result.stderr}")
            
    finally:
        print("🧹 [3/3] 로컬 임시 파일을 즉시 파기합니다 (보안).")
        if TEMP_ENV_PATH.exists():
            TEMP_ENV_PATH.unlink()
            
    print("🎉 API Key AWS 이관 프로세스 완료!")

if __name__ == "__main__":
    main()

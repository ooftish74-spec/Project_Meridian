#!/usr/bin/env python3
"""
scripts/test_secrets_manager.py — AWS Secrets Manager 및 KIS 자격증명 연결 테스트 스크립트
===================================================================================
"""

import sys
import logging
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.utils.credential_manager import CredentialManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('TestSecrets')

def test_credentials():
    logger.info("🧪 [Credential Check] AWS Secrets Manager / KIS 자격증명 무결성 검증 시작...")
    cm = CredentialManager()
    
    paper_key = cm.get('KIS_PAPER_APP_KEY', 'NOT_SET')
    paper_acc = cm.get('KIS_PAPER_ACCOUNT_NO', 'NOT_SET')
    
    logger.info(f"  ✅ [Result] KIS_PAPER_APP_KEY  : {'*'*6 + paper_key[-4:] if len(paper_key) > 4 else 'NOT_SET'}")
    logger.info(f"  ✅ [Result] KIS_PAPER_ACCOUNT_NO: {'*'*4 + paper_acc[-4:] if len(paper_acc) > 4 else 'NOT_SET'}")
    logger.info("🎉 [Credential Check] 검증 완결!")

if __name__ == '__main__':
    test_credentials()

"""API Key Manager — CredentialManager Facade (Legacy Purge 2026-07-19)

[Phase 71 → Legacy Purge] 구형 .env / os.environ 방식 완전 폐기.
이 모듈의 모든 로직은 CredentialManager 패스스루(Pass-through) Facade로 대체되었습니다.

아직 이 모듈을 임포트하는 레거시 코드의 의존성 충돌을 원천 차단하기 위해
외부 인터페이스(get_key, APIKeyManager)는 그대로 유지하되, 내부는 비웁니다.

DO NOT add .env or os.environ logic back. Use CredentialManager directly.
"""
from __future__ import annotations
import logging
from typing import Optional

from src.utils.credential_manager import CredentialManager

logger = logging.getLogger(__name__)
_cm = CredentialManager()


def get_key(name: str, default: Optional[str] = None) -> Optional[str]:
    """[Facade] CredentialManager.read_from_keychain()으로 패스스루."""
    value = _cm.read_from_keychain(name)
    if value:
        return value
    return default


class APIKeyManager:
    """[Facade] CredentialManager 패스스루 — .env 의존성 완전 제거."""

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return get_key(name, default)

    def require(self, name: str) -> str:
        val = get_key(name)
        if not val:
            raise EnvironmentError(
                f"필수 API 키 없음: {name} "
                f"(macOS Keychain에 저장 필요: "
                f"CredentialManager().save_to_keychain('{name}', '...'))"
            )
        return val

"""
Project Meridian — Credential Manager (Phase 71: Keychain Migration)
=====================================================================
macOS Keychain 기반 시크릿 관리. .env Fernet 방식 완전 대체.

우선순위:
    1. macOS Keychain  (keyring 라이브러리 또는 security CLI)
    2. .env _ENC       (Fernet 복호화, 하위 호환)
    3. .env 평문
    4. OS 환경변수

Usage:
    from src.utils.credential_manager import CredentialManager
    cm = CredentialManager()
    value = cm.read_from_env('KIS_APP_KEY')   # 인터페이스 동일
    cm.save_to_keychain('KIS_APP_KEY', value) # Keychain 저장
"""
from __future__ import annotations
import base64
import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
KEYCHAIN_SERVICE = 'Project_Meridian'
SENSITIVE_KEYS = frozenset({'KIS_APP_KEY', 'KIS_APP_SECRET', 'KIS_ACCOUNT_NO', 'KIS_HTS_ID', 'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'UPBIT_ACCESS_KEY', 'UPBIT_SECRET_KEY', 'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET', 'FRED_API_KEY', 'ALPHA_VANTAGE_API_KEY', 'FMP_API_KEY', 'DART_API_KEY', 'KRX_API_KEY', 'BOK_API_KEY', 'KOSIS_API_KEY', 'TELEGRAM_BOT_TOKEN', 'EMAIL_PASSWORD'})

def _mask(key: str, value: str) -> str:
    """민감 키 값을 마스킹하여 반환."""
    if key in SENSITIVE_KEYS and value:
        visible = min(4, len(value))
        return f'{"*" * (len(value) - visible)}{value[-visible:]}'
    return value

class CredentialManager:
    """[Phase 71] macOS Keychain 우선 시크릿 관리자.

    하위 호환: 기존 read_from_env() 인터페이스 유지.
    """

    def save_to_keychain(self, key: str, value: str) -> bool:
        """macOS Keychain에 시크릿 저장."""
        try:
            import keyring
            keyring.set_password(KEYCHAIN_SERVICE, key, value)
            logger.info(f'  [Phase 71] Keychain 저장: {key} = {_mask(key, value)}')
            return True
        except ImportError as e:
            return self._save_to_keychain_cli(key, value)
        except Exception as exc:
            logger.error(f'  [Phase 71] Keychain 저장 실패 ({key}): {exc}')
            return False

    def read_from_keychain(self, key: str) -> str:
        """macOS Keychain에서 시크릿 조회."""
        try:
            import keyring
            value = keyring.get_password(KEYCHAIN_SERVICE, key) or ''
            if value:
                logger.debug(f'  [Phase 71] Keychain: {key} = {_mask(key, value)}')
            return value
        except ImportError as e:
            return self._read_from_keychain_cli(key)
        except Exception as exc:
            logger.debug(f'  [Phase 71] Keychain 조회 실패 ({key}): {exc}')
            return ''

    def _save_to_keychain_cli(self, key: str, value: str) -> bool:
        """security CLI로 Keychain 저장."""
        import platform
        if platform.system() != 'Darwin':
            return False
        try:
            result = subprocess.run(['security', 'add-generic-password', '-s', KEYCHAIN_SERVICE, '-a', key, '-w', value, '-U'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f'  [Phase 71] Keychain CLI 저장: {key}')
                return True
            logger.warning(f'  [Phase 71] CLI 저장 실패 ({key}): {result.stderr.strip()}')
            return False
        except Exception as exc:
            logger.error(f'  [Phase 71] CLI 예외 ({key}): {exc}')
            return False

    def _read_from_keychain_cli(self, key: str) -> str:
        """security CLI로 Keychain 조회."""
        import platform
        if platform.system() != 'Darwin':
            return ''
        try:
            result = subprocess.run(['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', key, '-w'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                value = result.stdout.strip()
                logger.debug(f'  [Phase 71] Keychain CLI: {key} = {_mask(key, value)}')
                return value
            return ''
        except Exception as e:
            logger.warning(f"  [Phase 71] Keychain CLI read failed (Tier 3 Retry/Fallback): {e}")
            return ''

    def _read_from_aws_secrets(self, key: str) -> str:
        """[Phase 72] AWS Secrets Manager에서 시크릿 조회 (Production)."""
        try:
            import boto3
            from botocore.exceptions import ClientError
            import json
            
            # AWS 리전 설정 (기본값 ap-northeast-2)
            region_name = os.getenv('AWS_REGION', 'ap-northeast-2')
            
            # Boto3 클라이언트 생성 (IAM Role 기반)
            session = boto3.session.Session()
            client = session.client(
                service_name='secretsmanager',
                region_name=region_name
            )
            
            # Secret ID 규칙: Project_Meridian/{key} 또는 통짜 JSON
            # 여기서는 Key별로 저장되어 있다고 가정하거나, 통짜 JSON에서 Key를 파싱
            # Secret ID 규칙: Meridian/{key} 또는 통짜 JSON Meridian
            secret_id = f"Meridian/{key}"
            
            try:
                response = client.get_secret_value(SecretId=secret_id)
                if 'SecretString' in response:
                    value = response['SecretString']
                    # 만약 값이 JSON 형태라면 파싱하여 해당 키 추출 시도
                    try:
                        secret_dict = json.loads(value)
                        if key in secret_dict:
                            value = secret_dict[key]
                    except json.JSONDecodeError:
                        from src.utils.error_logger import log_warning_rate_limited
                        log_warning_rate_limited(__name__, f"⚠️ [Fallback] 파일/모듈 누락 예외 우회: (exception variable 없음)")
                        # 단일 텍스트 값으로 간주 (Tier 3 Ignore)
                        logger.debug(f'  [Phase 72] AWS Secrets Manager: {key} is a plaintext string (not JSON).')
                    logger.debug(f'  [Phase 72] AWS Secrets Manager: {key} = {_mask(key, value)}')
                    return value
            except ClientError as ce:
                # Secret이 단일 파일이 아니라 Project_Meridian 하나로 뭉쳐있을 경우의 Fallback 로직
                if ce.response['Error']['Code'] == 'ResourceNotFoundException':
                    try:
                        master_secret_id = 'Meridian'
                        response = client.get_secret_value(SecretId=master_secret_id)
                        if 'SecretString' in response:
                            secret_dict = json.loads(response['SecretString'])
                            if key in secret_dict:
                                value = secret_dict[key]
                                logger.debug(f'  [Phase 72] AWS Secrets Manager (Master JSON): {key} = {_mask(key, value)}')
                                return value
                    except Exception as fallback_err:
                        logger.debug(f"  [Phase 72] AWS Master JSON Fallback parsing failed: {fallback_err}")
                else:
                    logger.error(f'  [Phase 72] AWS Secrets Manager 오류 ({key}): {ce}')
                
        except ImportError:
            logger.error("  [Phase 72] boto3 라이브러리가 설치되어 있지 않습니다.")
        except Exception as exc:
            logger.error(f'  [Phase 72] AWS Secrets Manager 예외 ({key}): {exc}')
        
        return ''

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """dict-like getter alias for read_from_env."""
        val = self.read_from_env(key)
        return val if val else default

    def read_from_env(self, key: str, env_path: Optional[str]=None) -> str:
        """[Phase 72] 시크릿 조회 (Environment 분기 처리).
        
        Production 환경 (AWS):
            1. AWS Secrets Manager
            2. OS 환경변수
            (※ .env 파일 및 Keychain 무시하여 보안 극대화)
            
        로컬 환경 (Mac):
            1. macOS Keychain
            2. .env {key}_ENC (Fernet 복호화)
            3. .env {key} 평문
            4. OS 환경변수
        """
        is_production = os.environ.get('ENVIRONMENT', '').lower() == 'production'
        
        if is_production:
            # AWS 운영 환경: Secrets Manager 우선 조회
            _aws_val = self._read_from_aws_secrets(key)
            if _aws_val:
                return _aws_val
            # Fallback: 로컬 테스트나 특수 주입된 OS 환경변수
            return os.getenv(key, '')
        
        # 로컬(개발) 환경: 기존 Keychain -> .env 로직 수행
        _kc = self.read_from_keychain(key)
        if _kc:
            return _kc
            
        _env = env_path or str(_PROJECT_ROOT / '.env')
        if Path(_env).exists():
            try:
                _plain, _enc = ('', '')
                with open(_env, 'r', encoding='utf-8') as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if '#' in _line:
                            _line = _line[:_line.index('#')].strip()
                        if _line.startswith(f'{key}_ENC='):
                            _enc = _line[len(key) + 5:]
                        elif _line.startswith(f'{key}='):
                            _plain = _line[len(key) + 1:]
                if _enc:
                    _dec = self._decrypt_fernet(_enc)
                    if _dec:
                        logger.debug(f'  [Phase 71] .env Fernet 복호화: {key} (Keychain 이관 권장)')
                        return _dec
                if _plain:
                    logger.debug(f'  [Phase 71] .env 평문: {key}')
                    return _plain
            except Exception as _exc:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_exc}", exc_info=True)
                logger.debug(f'  [Phase 71] .env 읽기 실패 ({key}): {_exc}')
        
        return os.getenv(key, '')

    def _derive_key(self) -> bytes:
        """기존 Fernet 키 유도 (마이그레이션 시 .env 복호화용)."""
        try:
            result = subprocess.run(['/usr/sbin/ioreg', '-d2', '-c', 'IOPlatformExpertDevice'], capture_output=True, text=True, timeout=5)
            hw_uuid = ''
            for line in result.stdout.split('\n'):
                if 'IOPlatformUUID' in line:
                    hw_uuid = line.split('"')[-2]
                    break
        except Exception as e:
            logger.debug(f"  [Phase 71] hw_uuid fetch failed (Tier 3 Ignore): {e}")
            hw_uuid = ''
        if not hw_uuid:
            import platform
            hw_uuid = platform.node() or 'fallback-uuid'
        user = os.getenv('USER', '') or Path(os.getenv('HOME', '~')).name or 'default'
        salt = f'{hw_uuid}:{user}:Project-A-KIS'
        return base64.urlsafe_b64encode(hashlib.sha256(salt.encode()).digest())

    def _decrypt_fernet(self, ciphertext: str) -> str:
        """기존 .env _ENC 값 Fernet 복호화 (하위 호환)."""
        try:
            from cryptography.fernet import Fernet
            return Fernet(self._derive_key()).decrypt(ciphertext.encode()).decode()
        except Exception as exc:
            logger.debug(f'  [Phase 71] Fernet 복호화 실패: {exc}')
            return ''

    def encrypt(self, plaintext: str) -> str:
        """암호화 (마이그레이션 스크립트 호환용)."""
        from cryptography.fernet import Fernet
        return Fernet(self._derive_key()).encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """복호화 (마이그레이션 스크립트 호환용)."""
        return self._decrypt_fernet(ciphertext)

    def encrypt_to_env(self, key: str, value: str, env_path: Optional[str]=None):
        """[Deprecated] .env 암호화 저장 — save_to_keychain() 사용 권장."""
        logger.warning(f'  [Phase 71] encrypt_to_env({key}) deprecated — save_to_keychain() 사용')
        env_path = env_path or str(_PROJECT_ROOT / '.env')
        encrypted = self.encrypt(value)
        enc_key = f'{key}_ENC'
        lines: list = []
        if Path(env_path).exists():
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f'{enc_key}='):
                lines[i] = f'{enc_key}={encrypted}\n'
                found = True
                break
        if not found:
            lines.append(f'{enc_key}={encrypted}\n')
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        logger.info(f'  ✅ {enc_key} .env 저장 (Keychain 이관 권장)')
if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.DEBUG)
    cm = CredentialManager()
    _key = sys.argv[1] if len(sys.argv) > 1 else 'KIS_APP_KEY'
    _val = cm.read_from_env(_key)
    print(f'{"OK" if _val else "MISS"}: {_key} = {_mask(_key, _val) if _val else "(없음)"}')
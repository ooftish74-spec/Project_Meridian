"""[Phase 71] Keychain 연동 및 마스킹 검증 스크립트.

실행:
    cd Project_Meridian
    PYTHONPATH=. python scripts/test_keychain.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s')

_PASSED = 0
_FAILED = 0


def _ok(msg: str) -> None:
    global _PASSED; _PASSED += 1; print(f'  ✅ {msg}')


def _fail(msg: str, detail: str = '') -> None:
    global _FAILED; _FAILED += 1; print(f'  ❌ {msg}' + (f': {detail}' if detail else ''))


print('\n' + '='*60)
print('[Phase 71] Keychain 연동 검증')
print('='*60)

# Scenario 1: 저장/조회
print('\n[Scenario 1] Keychain 저장 / 조회')
try:
    from src.utils.credential_manager import CredentialManager, KEYCHAIN_SERVICE
    cm = CredentialManager()
    _K, _V = '__MERIDIAN_TEST__', 'hello_keychain_2026'
    if cm.save_to_keychain(_K, _V):
        _ok('save_to_keychain() 성공')
    else:
        _fail('save_to_keychain() 실패')
    if cm.read_from_keychain(_K) == _V:
        _ok('read_from_keychain() 값 정확')
    else:
        _fail('read_from_keychain() 불일치')
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _fail('Scenario 1', str(e))

# Scenario 2: 마스킹
print('\n[Scenario 2] 마스킹 검증')
try:
    from src.utils.credential_manager import _mask, SENSITIVE_KEYS
    _sample = 'ABCDEFGHIJ1234567890'
    for _key in list(SENSITIVE_KEYS)[:5]:
        m = _mask(_key, _sample)
        assert '****' in m, f'{_key} 마스킹 미적용'
        assert _sample not in m, f'{_key} 평문 노출'
    _ok(f'민감 키 5건 마스킹 확인')
    assert _mask('KIS_MODE', 'mock') == 'mock', 'KIS_MODE 오마스킹'
    _ok('비민감 키(KIS_MODE) 마스킹 미적용 확인')
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _fail('Scenario 2', str(e))

# Scenario 3: read_from_env Keychain 우선
print('\n[Scenario 3] read_from_env() Keychain 우선')
try:
    _K2, _V2 = '__MERIDIAN_TEST2__', 'priority_check'
    cm.save_to_keychain(_K2, _V2)
    if cm.read_from_env(_K2) == _V2:
        _ok('read_from_env()가 Keychain 1순위 조회')
    else:
        _fail('Keychain 우선 조회 실패')
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _fail('Scenario 3', str(e))

# Scenario 4: 실제 FRED_API_KEY
print('\n[Scenario 4] FRED_API_KEY 조회')
try:
    from src.utils.credential_manager import _mask
    v = cm.read_from_env('FRED_API_KEY')
    if v:
        _ok(f'FRED_API_KEY: {_mask("FRED_API_KEY", v)}')
    else:
        print('  ℹ️  FRED_API_KEY 없음 (마이그레이션 전이거나 미설정)')
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _fail('Scenario 4', str(e))

# Scenario 5: KIS 키 Keychain 보유 여부
print('\n[Scenario 5] KIS 핵심 키 Keychain 보유')
for _k in ['KIS_APP_KEY', 'KIS_APP_SECRET', 'KIS_ACCOUNT_NO']:
    try:
        from src.utils.credential_manager import _mask
        v = cm.read_from_keychain(_k)
        if v:
            _ok(f'{_k}: {_mask(_k, v)}')
        else:
            print(f'  ℹ️  {_k}: 미등록 (migrate_to_keychain.py 실행 필요)')
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        _fail(_k, str(e))

print('\n' + '-'*60)
print(f'[Phase 71] {_PASSED}성공 / {_FAILED}실패')
sys.exit(0 if _FAILED == 0 else 1)

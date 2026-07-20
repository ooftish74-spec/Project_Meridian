"""[Phase 71] macOS Keychain 마이그레이션 스크립트.

기존 .env(평문 + Fernet _ENC) 시크릿을 macOS Keychain으로 일괄 이관,
민감 키를 .env에서 완전 삭제(Wipe).

실행:
    cd Project_Meridian
    python scripts/migrate_to_keychain.py            # 실제 실행
    python scripts/migrate_to_keychain.py --dry-run  # 드라이런
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('migrate_to_keychain')

_WIPE_KEYS = {
    'KIS_APP_KEY', 'KIS_APP_SECRET', 'KIS_ACCOUNT_NO', 'KIS_HTS_ID',
    'BINANCE_API_KEY', 'BINANCE_API_SECRET',
    'UPBIT_ACCESS_KEY', 'UPBIT_SECRET_KEY',
    'NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET',
    'FRED_API_KEY', 'ALPHA_VANTAGE_API_KEY', 'FMP_API_KEY',
    'DART_API_KEY', 'KRX_API_KEY', 'BOK_API_KEY', 'KOSIS_API_KEY',
    'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
    'EMAIL_SENDER', 'EMAIL_PASSWORD', 'REPORT_RECIPIENT',
}


def _mask(value: str) -> str:
    if not value:
        return ''
    v = min(4, len(value))
    return f"{'*' * max(0, len(value) - v)}{value[-v:]}"


def parse_env(env_path: Path) -> dict[str, str]:
    raw: dict[str, str] = {}
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '#' in line:
                line = line[:line.index('#')].strip()
            m = re.match(r'^([A-Z0-9_]+)=(.*)$', line)
            if m:
                raw[m.group(1)] = m.group(2).strip()
    return raw


def migrate(dry_run: bool = False) -> None:
    from src.utils.credential_manager import CredentialManager, KEYCHAIN_SERVICE
    env_path = ROOT / '.env'
    if not env_path.exists():
        logger.error(f'.env 없음: {env_path}')
        sys.exit(1)
    cm = CredentialManager()
    raw = parse_env(env_path)
    print(f'\n{"="*60}')
    print(f'[Phase 71] macOS Keychain 마이그레이션')
    print(f'서비스명 : {KEYCHAIN_SERVICE}')
    print(f'Dry-run  : {dry_run}')
    print(f'{"="*60}\n')
    ok_cnt, skip_cnt, fail_cnt = 0, 0, 0
    for raw_key, raw_val in raw.items():
        is_enc = raw_key.endswith('_ENC')
        base_key = raw_key[:-4] if is_enc else raw_key
        if base_key not in _WIPE_KEYS:
            skip_cnt += 1
            continue
        plain_val = cm.decrypt(raw_val) if is_enc else raw_val
        if is_enc and not plain_val:
            logger.warning(f'  복호화 실패: {raw_key}')
            fail_cnt += 1
            continue
        print(f'  {base_key}: {_mask(plain_val)}  →  Keychain', end='')
        if dry_run:
            print(' [DRY-RUN]')
            ok_cnt += 1
            continue
        if cm.save_to_keychain(base_key, plain_val):
            print(' ✅')
            ok_cnt += 1
        else:
            print(' ❌')
            fail_cnt += 1
    print(f'\n결과: {ok_cnt}건 등록, {skip_cnt}건 생략, {fail_cnt}건 실패')
    if fail_cnt:
        logger.error('.env Wipe 중단 — 실패 항목 확인 후 재시도')
        sys.exit(1)
    if not dry_run:
        _wipe_env(env_path)
        print(f'\n✅ .env 민감 키 삭제 완료: {env_path}')
        print('ℹ️  KIS_MODE, SMTP 등 비민감 설정은 유지됨')
    else:
        print('\n[DRY-RUN] .env Wipe 건너뜀')


def _wipe_env(env_path: Path) -> None:
    with open(env_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    out: list[str] = []
    cnt = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            out.append(line)
            continue
        m = re.match(r'^([A-Z0-9_]+)=', stripped)
        if not m:
            out.append(line)
            continue
        rk = m.group(1)
        bk = rk[:-4] if rk.endswith('_ENC') else rk
        if bk in _WIPE_KEYS:
            out.append(f'# [Phase 71 Wiped] {rk}=REMOVED_TO_KEYCHAIN\n')
            cnt += 1
            logger.info(f'  Wiped: {rk}')
        else:
            out.append(line)
    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(out)
    logger.info(f'  .env Wipe: {cnt}건 삭제')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    migrate(dry_run=p.parse_args().dry_run)

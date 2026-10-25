#!/usr/bin/env python3
"""
Project Meridian Legacy Artifact Purger & Isolation Script
==========================================================
results/ 디렉터리 내에 축적된 레거시 백업 파일, 구형 아카이브, 임시 .bak 파일들을
.legacy_quarantine/ 디렉터리로 안전하게 이동하여 SSoT 데이터 오염을 원천 차단합니다.
"""

import os
import shutil
import logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
QUARANTINE_DIR = PROJECT_ROOT / ".legacy_quarantine"

def purge_legacy_artifacts():
    if not RESULTS_DIR.exists():
        logger.warning(f"results 디렉터리가 존재하지 않습니다: {RESULTS_DIR}")
        return

    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    purged_count = 0

    # 레거시 파일/디렉터리 패턴 정의 (즉시 영구 삭제)
    legacy_patterns = [
        "s6*",
        "*s6*",
        "archive*",
        "backups*",
        "archive_pre_*",
        "*.bak_pre_live",
        "*.bak",
        "*.tmp*",
        "tmpbu2_*",
        "Daily_Quant_Report_202607*.md",
        "shadow_report_202607*.md",
        "shadow_report_202607*.json",
    ]

    # 프로젝트 루트 archive/backups/scratch/catboost_info 디렉터리 영구 삭제
    for p_dir in [
        PROJECT_ROOT / "archive",
        PROJECT_ROOT / "backups",
        PROJECT_ROOT / ".legacy_quarantine",
        PROJECT_ROOT / "scratch",
        PROJECT_ROOT / "catboost_info",
    ]:
        if p_dir.exists():
            shutil.rmtree(p_dir)
            purged_count += 1
            logger.info(f"  🧹 [Permanent Purge] 루트 레거시 디렉터리 즉시 영구 삭제: {p_dir.name}")

    # 레거시 락 파일 및 임시 파일 삭제
    stale_files = [
        RESULTS_DIR / ".lock_shadow_portfolio.lock",
        RESULTS_DIR / "virtual_ledger.json.lock",
        RESULTS_DIR / "us_night_holdings.json.lock",
    ]
    for sf in stale_files:
        if sf.exists():
            try:
                sf.unlink()
                purged_count += 1
                logger.info(f"  🧹 [Permanent Purge] 레거시 락 파일 삭제: {sf.name}")
            except Exception as e:
                logger.error(f"  ⚠️ {sf.name} 삭제 실패: {e}")

    for sub_tmp in list((RESULTS_DIR / "shadow_trades").glob("*.tmp*")) if (RESULTS_DIR / "shadow_trades").exists() else []:
        try:
            sub_tmp.unlink()
            purged_count += 1
            logger.info(f"  🧹 [Permanent Purge] 임시 파일 삭제: {sub_tmp}")
        except Exception:
            pass

    data_dir = PROJECT_ROOT / "data"
    if data_dir.exists():
        for d_tmp in list(data_dir.rglob("*.tmp*")):
            try:
                d_tmp.unlink()
                purged_count += 1
                logger.info(f"  🧹 [Permanent Purge] 데이터 임시 파일 삭제: {d_tmp.name}")
            except Exception:
                pass

    # 소스(.py)가 삭제된 고아 .pyc 바이트코드 파일 정리
    for code_root in [PROJECT_ROOT / "scripts", PROJECT_ROOT / "src", PROJECT_ROOT / "tests"]:
        if not code_root.exists():
            continue
        for pyc in list(code_root.rglob("__pycache__/*.pyc")):
            stem = pyc.name.split(".")[0]
            src_py = pyc.parent.parent / f"{stem}.py"
            if not src_py.exists():
                try:
                    pyc.unlink()
                    purged_count += 1
                    logger.info(f"  🧹 [Permanent Purge] 고아 바이트코드 삭제: {pyc.name}")
                except Exception:
                    pass

    for pattern in legacy_patterns:
        for item in RESULTS_DIR.glob(pattern):
            if item.name in ("signal_cache.lock",):
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                purged_count += 1
                logger.info(f"  🧹 [Permanent Purge] 레거시 아티팩트 영구 삭제: {item.name}")
            except Exception as e:
                logger.error(f"  ⚠️ {item.name} 삭제 중 오류 발생: {e}")

    logger.info(f"✨ 레거시 아티팩트 청소 완료: 총 {purged_count}개 항목 격리됨.")

if __name__ == "__main__":
    purge_legacy_artifacts()

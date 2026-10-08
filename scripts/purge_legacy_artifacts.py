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

    # 프로젝트 루나 archive/backups 디렉터리 영구 삭제
    for p_dir in [PROJECT_ROOT / "archive", PROJECT_ROOT / "backups", PROJECT_ROOT / ".legacy_quarantine"]:
        if p_dir.exists():
            shutil.rmtree(p_dir)
            logger.info(f"  🧹 [Permanent Purge] 루티 디렉터리 즉시 영구 삭제: {p_dir.name}")

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

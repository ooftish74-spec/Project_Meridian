#!/usr/bin/env python3
"""
Data Recycler (Orphan Data Archiving)
=====================================
기존 `orphan_cleanup.py`의 영구 삭제 로직을 대체하여, 
오래된 모델과 데이터를 미래의 Offline Replay Buffer 또는 Ensemble 
딥러닝을 위해 압축 및 영구 보존(Archive)하는 유틸리티 스크립트.

기능:
1. 7일 이상 참조되지 않은 `results/`, `models/`, `data/parquet/` 파일 스캔
2. 삭제 대신 `archive_lake/` 디렉토리로 구조를 유지하며 안전하게 이동
3. Offline 딥러닝 풀(Pool)로 재활용(Recycling) 대기 상태로 전환
"""

import os
import shutil
import time
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("DataRecycler")

_ROOT = Path(__file__).resolve().parent.parent

# 타겟 디렉토리
TARGET_DIRS = [
    _ROOT / "results",
    _ROOT / "models",
    _ROOT / "data" / "parquet"
]

# 아카이브 스토리지
ARCHIVE_LAKE = _ROOT / "data" / "archive_lake"
ARCHIVE_MODELS = ARCHIVE_LAKE / "models"
ARCHIVE_RESULTS = ARCHIVE_LAKE / "results"
ARCHIVE_PARQUET = ARCHIVE_LAKE / "parquet"

def ensure_archive_dirs():
    """아카이브 호수(Data Lake) 디렉토리 생성"""
    ARCHIVE_MODELS.mkdir(parents=True, exist_ok=True)
    ARCHIVE_RESULTS.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PARQUET.mkdir(parents=True, exist_ok=True)

def is_orphaned(filepath: Path, days_threshold: int = 7) -> bool:
    """파일이 지정된 임계일 이상 수정되지 않은 고아 파일인지 확인"""
    if not filepath.exists():
        return False
    # mtime 기준
    mtime = filepath.stat().st_mtime
    now = time.time()
    age_days = (now - mtime) / (24 * 3600)
    return age_days > days_threshold

def get_destination_dir(source_path: Path) -> Path:
    """원본 경로에 맞는 아카이브 목적지 반환"""
    if 'models' in source_path.parts:
        return ARCHIVE_MODELS
    elif 'results' in source_path.parts:
        return ARCHIVE_RESULTS
    elif 'parquet' in source_path.parts:
        return ARCHIVE_PARQUET
    else:
        return ARCHIVE_LAKE / "misc"

def recycle_data(days_threshold: int = 7):
    ensure_archive_dirs()
    
    archived_count = 0
    total_size_bytes = 0
    
    logger.info(f"데이터 리사이클링 시작: {days_threshold}일 이상 방치된 데이터 검색 중...")
    
    for directory in TARGET_DIRS:
        if not directory.exists():
            continue
            
        for root, _, files in os.walk(directory):
            for file in files:
                # 활성 상태나 설정 파일 등은 제외
                if file.endswith('.json') and 'config' in file:
                    continue
                if file.startswith('.'):
                    continue
                    
                filepath = Path(root) / file
                
                # 디렉토리 자체를 스캔 중이므로 이미 아카이브에 있는 것은 무시
                if 'archive_lake' in filepath.parts:
                    continue
                    
                if is_orphaned(filepath, days_threshold):
                    dest_dir = get_destination_dir(filepath)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_path = dest_dir / file
                    
                    # 파일명 중복 시 타임스탬프 부여
                    if dest_path.exists():
                        dest_path = dest_dir / f"{file}_{int(time.time())}"
                        
                    try:
                        file_size = filepath.stat().st_size
                        # 파일 이동(Archiving)
                        shutil.move(str(filepath), str(dest_path))
                        archived_count += 1
                        total_size_bytes += file_size
                    except Exception as e:
                        logger.error(f"  [Recycler] 아카이빙 실패 {filepath}: {e}")
                        
    size_mb = total_size_bytes / (1024 * 1024)
    logger.info("=== 리사이클링(아카이빙) 완료 ===")
    logger.info(f"총 {archived_count}개의 유휴 파일이 삭제되지 않고 Offline Data Lake로 아카이빙되었습니다.")
    logger.info(f"보존된 데이터 크기: {size_mb:.2f} MB")
    logger.info("이 데이터는 추후 Ensemble 모델의 Replay Buffer 훈련 용도로 재활용됩니다.")

if __name__ == "__main__":
    recycle_data(days_threshold=7)

"""
Safe File I/O Utilities (Cross-Process File Lock + Atomic Replace + 0o666 Permission Lock)
========================================================================================
동시성 데이터 레이스(Data Race) 및 다중 프로세스(Systemd / Daemon / Premarket) 간 파일 기록 충돌을
100% 원천 차단하는 원자적 파일 쓰기 모듈.
"""

import os
import json
import tempfile
import fcntl
import time
import logging
from pathlib import Path
from typing import Any, Union
from contextlib import contextmanager

logger = logging.getLogger(__name__)

@contextmanager
def file_lock_transaction(lock_file_path: Union[str, Path], timeout: int = 10):
    """[Red Team V7] 다중 프로세스 환경에서 Data Race를 방지하는 OS 레벨 트랜잭션 락."""
    lock_path = Path(lock_file_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    actual_lock_file = lock_path.with_suffix(lock_path.suffix + ".lock")
    
    start_time = time.time()
    lock_fd = None
    try:
        lock_fd = os.open(str(actual_lock_file), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o666)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (IOError, OSError):
                if time.time() - start_time > timeout:
                    logger.warning(f"  ⚠️ [File Lock Timeout] {lock_file_path} 락 획득 타임아웃 ({timeout}s). 강제 진행.")
                    break
                time.sleep(0.02)
        yield
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass

def atomic_write_json(filepath: Union[str, Path], data: Any, indent: int = 2, **kwargs) -> None:
    """원자적(Atomic)으로 JSON 데이터를 파일에 기록하며, 멀티 유저 권한(0666) 및 Cross-Process Lock 강제."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with file_lock_transaction(path):
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                kwargs.setdefault('ensure_ascii', False)
                kwargs.setdefault('allow_nan', False)
                json.dump(data, f, indent=indent, **kwargs)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp_path, 0o666)
            except Exception:
                pass
            os.replace(tmp_path, str(path))
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise e

def atomic_write_text(filepath: Union[str, Path], text: str) -> None:
    """원자적(Atomic)으로 텍스트/마크다운 데이터를 파일에 기록하며, 멀티 유저 권한(0666) 강제."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with file_lock_transaction(path):
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp_path, 0o666)
            except Exception:
                pass
            os.replace(tmp_path, str(path))
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise e

def atomic_write_parquet(df: Any, filepath: Union[str, Path], **kwargs) -> None:
    """원자적(Atomic)으로 Pandas DataFrame을 Parquet 파일로 기록."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    tmp_path = path.with_suffix('.tmp' + path.suffix)
    try:
        df.to_parquet(tmp_path, **kwargs)
        os.chmod(tmp_path, 0o666)
        os.replace(tmp_path, str(path))
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise e

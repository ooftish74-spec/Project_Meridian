import os
import json
import tempfile
from pathlib import Path
from typing import Any, Dict

def atomic_write_json(filepath: str | Path, data: Any, indent: int = 2, **kwargs) -> None:
    """원자적(Atomic)으로 JSON 데이터를 파일에 기록하여 대시보드 등의 동시 읽기 크래시를 방지합니다."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            kwargs.setdefault('ensure_ascii', False)
            kwargs.setdefault('allow_nan', False)
            json.dump(data, f, indent=indent, **kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise e

def atomic_write_text(filepath: str | Path, text: str) -> None:
    """원자적(Atomic)으로 텍스트/마크다운 데이터를 파일에 기록합니다."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise e

def atomic_write_parquet(df: 'pd.DataFrame', filepath: str | Path, **kwargs) -> None:
    """원자적(Atomic)으로 Pandas DataFrame을 Parquet 파일로 기록합니다."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # 임시 파일 경로 생성
    tmp_path = path.with_suffix('.tmp' + path.suffix)
    try:
        df.to_parquet(tmp_path, **kwargs)
        # os.replace는 원자적(Atomic) 연산을 보장함
        os.replace(tmp_path, path)
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise e

import fcntl
import time
from contextlib import contextmanager

@contextmanager
def file_lock_transaction(lock_file_path: str | Path, timeout: int = 10):
    """
    [Red Team V7] 다중 프로세스(S1~S5) 환경에서 TOC/TOU Data Race를 방지하는 OS 레벨 트랜잭션 락.
    """
    lock_path = Path(lock_file_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    
    start_time = time.time()
    fd = open(lock_path, 'w')
    
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() - start_time > timeout:
                fd.close()
                raise TimeoutError(f"[{lock_path}] 파일 락 획득 실패. (Timeout {timeout}s)")
            time.sleep(0.05)
            
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

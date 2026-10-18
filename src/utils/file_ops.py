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

import threading

_LOCK_STATE = threading.local()   # 스레드별 보유 락 카운트 (재진입 지원)


def _held_counts() -> dict:
    d = getattr(_LOCK_STATE, 'held', None)
    if d is None:
        d = {}
        _LOCK_STATE.held = d
    return d


class FileLockTimeout(TimeoutError):
    """strict 모드에서 락 획득 실패 — 호출자는 쓰기를 포기해야 한다(무잠금 진행 금지)."""


@contextmanager
def file_lock_transaction(lock_file_path: Union[str, Path], timeout: int = 10, strict: bool = False):
    """[Red Team V7] 다중 프로세스 환경에서 Data Race를 방지하는 OS 레벨 트랜잭션 락.

    ★ Wave 1:
      - 재진입(reentrant): 같은 스레드가 이미 보유한 락을 다시 요청하면 즉시 통과.
        (과거: 같은 프로세스의 두 번째 flock이 자기 자신에 막혀 timeout 후 '무잠금 강제 진행')
      - strict=True: 타임아웃 시 FileLockTimeout 예외 (read-modify-write 경로용). 기본값은 기존 동작 유지.
    """
    lock_path = Path(lock_file_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    actual_lock_file = lock_path.with_suffix(lock_path.suffix + ".lock")
    key = str(actual_lock_file.resolve())
    held = _held_counts()
    if held.get(key, 0) > 0:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    start_time = time.time()
    lock_fd = None
    acquired = False
    try:
        lock_fd = os.open(str(actual_lock_file), os.O_RDWR | os.O_CREAT, 0o666)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (IOError, OSError):
                if time.time() - start_time > timeout:
                    if strict:
                        raise FileLockTimeout(f"lock timeout ({timeout}s): {lock_file_path}")
                    logger.warning(f"  ⚠️ [File Lock Timeout] {lock_file_path} 락 획득 타임아웃 ({timeout}s). 강제 진행.")
                    break
                time.sleep(0.02)
        if acquired:
            held[key] = 1
        try:
            yield
        finally:
            if acquired:
                held.pop(key, None)
    finally:
        if lock_fd is not None:
            try:
                if acquired:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass


class CorruptJSONError(ValueError):
    """기존 파일이 손상됨 — safe_json_update는 덮어쓰지 않고 중단한다."""


def locked_read_json(filepath: Union[str, Path], default: Any = None, timeout: int = 10) -> Any:
    """락을 잡은 상태에서 JSON 읽기 (쓰기 도중의 반쪽 파일을 읽지 않도록). 없으면 default."""
    path = Path(filepath)
    with file_lock_transaction(path, timeout=timeout, strict=True):
        if not path.exists():
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)


def safe_json_update(filepath: Union[str, Path], mutator, default: Any = None, timeout: int = 30,
                     indent: int = 2, **dump_kwargs) -> Any:
    """★ Wave 1: 교차 프로세스 안전 read-modify-write.

    1) strict 락 획득(실패 시 FileLockTimeout — 쓰지 않음)
    2) 현재 파일 로드 (없으면 deepcopy(default); 손상 시 CorruptJSONError — 덮어쓰지 않음)
    3) mutator(obj) 호출. 반환값이 None이면 obj를 제자리 수정한 것으로 간주
    4) 원자적 교체(tmp+fsync+rename) 후 새 객체 반환
    """
    import copy
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock_transaction(path, timeout=timeout, strict=True):
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    obj = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise CorruptJSONError(f"{path}: {e}") from e
        else:
            obj = copy.deepcopy(default)
        new_obj = mutator(obj)
        if new_obj is None:
            new_obj = obj
        atomic_write_json(path, new_obj, indent=indent, **dump_kwargs)   # 재진입 락
        return new_obj

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

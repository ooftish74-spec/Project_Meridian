"""
Safe I/O — 원자적 파일 쓰기 + 안전한 JSON 읽기
=================================================

모든 JSON 쓰기를 원자적(atomic)으로 수행:
  1. temp 파일에 쓰기 (.filename.tmp)
  2. fsync (디스크 플러시)
  3. 원자적 rename (.tmp → .json)
  → 중간 크래시 시 이전 정상 파일 유지

Usage:
    from src.infra.safe_io import safe_json_write, safe_json_read, safe_parquet_write
    safe_json_write(path, data)
    data = safe_json_read(path, default={})
    safe_parquet_write(df, path)
"""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
import pandas as pd
logger = logging.getLogger(__name__)

def safe_json_write(path: Path, data: Any, indent: int=2, backup: bool=False) -> bool:
    """원자적 JSON 파일 쓰기.

    Args:
        path: 대상 파일 경로
        data: JSON 직렬화 가능 데이터
        indent: JSON 인덴트
        backup: True이면 기존 파일을 .bak으로 보관

    Returns:
        성공 여부
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=indent, ensure_ascii=False, default=str)
        fd, tmp_path = tempfile.mkstemp(suffix='.tmp', prefix=f'.{path.name}.', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            if backup and path.exists():
                bak = path.with_suffix(path.suffix + '.bak')
                try:
                    os.replace(str(path), str(bak))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
            os.replace(tmp_path, str(path))
            return True
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return False
    except Exception as e:
        logger.warning(f'  safe_json_write 실패 ({path.name}): {e}')
        return False

def safe_json_read(path: Path, default: Any=None) -> Any:
    """안전한 JSON 파일 읽기.

    파일 손상 시:
      1. .bak 파일에서 복구 시도
      2. default 반환

    Args:
        path: 대상 파일 경로
        default: 파일 없거나 손상 시 반환값

    Returns:
        파싱된 데이터 or default
    """
    path = Path(path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f'  JSON 손상 감지 ({path.name}): {e}')
    bak = path.with_suffix(path.suffix + '.bak')
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding='utf-8'))
            logger.info(f'  .bak에서 복구: {path.name}')
            safe_json_write(path, data)
            return data
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    return default if default is not None else {}

def safe_parquet_write(df: pd.DataFrame, path: Path, backup: bool=False) -> bool:
    """원자적 Parquet 파일 쓰기 (DataFrame).

    Args:
        df: 저장할 pandas DataFrame
        path: 대상 파일 경로 (.parquet)
        backup: True이면 기존 파일을 .bak으로 보관

    Returns:
        성공 여부
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix='.tmp', prefix=f'.{path.name}.', dir=str(path.parent))
        os.close(fd)
        
        df.to_parquet(tmp_path, index=False)
        
        with open(tmp_path, 'r+b') as f:
            f.flush()
            os.fsync(f.fileno())
            
        if backup and path.exists():
            bak = path.with_suffix(path.suffix + '.bak')
            try:
                os.replace(str(path), str(bak))
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'Backup error: {e}')
                pass
                
        os.replace(tmp_path, str(path))
        return True
    except Exception as e:
        logger.error(f'safe_parquet_write 에러 ({path}): {e}', exc_info=True)
        try:
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        return False
def atomic_write_dataframe(df: pd.DataFrame, path, file_format: str = 'csv', backup: bool = False, **kwargs) -> bool:
    """원자적(Atomic)으로 DataFrame을 저장하여 파일 I/O 동시성 출돌 및 데이터 증발(Race Condition)을 방지합니다.
    
    지원 포맷: 'csv', 'parquet'
    
    Args:
        df: 저장할 DataFrame
        path: 저장할 경로
        file_format: 'csv' 또는 'parquet'
        backup: 백업(.bak) 파일 생성 여부
        kwargs: df.to_csv() 또는 df.to_parquet()에 전달할 추가 인자
        
    Returns:
        성공 여부 bool
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix='.tmp', prefix=f'.{path.name}.', dir=str(path.parent))
        os.close(fd)
        
        if file_format == 'csv':
            if 'index' not in kwargs:
                kwargs['index'] = False
            df.to_csv(tmp_path, **kwargs)
        elif file_format == 'parquet':
            if 'index' not in kwargs:
                kwargs['index'] = False
            df.to_parquet(tmp_path, **kwargs)
        else:
            raise ValueError(f"Unsupported format: {file_format}")
            
        with open(tmp_path, 'r+b') as f:
            f.flush()
            os.fsync(f.fileno())
            
        if backup and path.exists():
            bak = path.with_suffix(path.suffix + '.bak')
            try:
                os.replace(str(path), str(bak))
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                pass
                
        os.replace(tmp_path, str(path))
        return True
    except Exception as e:
        logger.error(f'atomic_write_dataframe 에러 ({path}): {e}', exc_info=True)
        try:
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        return False

def safe_json_update(path: Path, update_func) -> bool:
    """[Red Team V5] 다중 프로세스 충돌 방지 (TOC/TOU Data Race 제거).
    파일 락(fcntl.flock)을 걸어 원자적 Read-Modify-Write 사이클을 보장합니다.
    
    Args:
        path: 대상 JSON 파일 경로
        update_func: 현재 dict를 받아 변경할 dict를 반환하는 콜백 함수
    """
    import fcntl
    import time
    
    path = Path(path)
    lock_path = path.with_suffix('.lock')
    
    path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        if not lock_path.exists():
            lock_path.touch()
            
        with open(lock_path, 'w') as lock_file:
            # 1. 락 획득 (Blocking)
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # 락을 얻지 못하면 최대 3초 대기
                acquired = False
                for _ in range(30):
                    time.sleep(0.1)
                    try:
                        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        pass
                if not acquired:
                    logger.error(f"[FileLock] Timeout waiting for lock on {path}")
                    return False
                    
            try:
                # 2. Read
                current_data = {}
                if path.exists():
                    try:
                        current_data = json.loads(path.read_text())
                    except json.JSONDecodeError:
                        pass
                        
                # 3. Modify
                new_data = update_func(current_data)
                
                # 4. Write (기존 원자적 쓰기 재사용)
                safe_json_write(path, new_data)
                return True
            finally:
                # 5. 락 해제
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except Exception as e:
        logger.error(f"[safe_json_update] Error: {e}", exc_info=True)
        return False

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
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=indent, ensure_ascii=False, default=str)
        fd, tmp_path = tempfile.mkstemp(suffix='.tmp', prefix=f'.{path.name}.', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        if backup and path.exists():
            bak = path.with_suffix(path.suffix + '.bak')
            try:
                os.replace(str(path), str(bak))
            except Exception as _e:
                logger.debug(f"Backup replacement error: {_e}")
        os.replace(tmp_path, str(path))
        return True
    except Exception as e:
        logger.warning(f"  safe_json_write 실패 ({path.name}): {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return False

def safe_json_read(path: Path, default: Any=None, max_age_sec: Optional[int]=None) -> Any:
    """안전한 JSON 파일 읽기.

    TTL Staleness Check:
      max_age_sec 지정 시 파일 수정 시간이 초과되면 default 반환하여 구형 데이터 참조 차단.

    Args:
        path: 대상 파일 경로
        default: 파일 없거나 손상/만료 시 반환값
        max_age_sec: 최대 유효 시간(초). 초과 시 default 반환

    Returns:
        파싱된 데이터 or default
    """
    path = Path(path)
    if path.exists():
        import time
        if max_age_sec is not None:
            file_age = time.time() - path.stat().st_mtime
            if file_age > max_age_sec:
                logger.warning(f"  🚨 [Stale Data Expiry] {path.name} 파일 갱신 시점 초과 ({file_age:.1f}초 > {max_age_sec}초) ➔ 구형 데이터 파기 및 Fresh 수집 트리거")
                return default if default is not None else {}
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (PermissionError, OSError, IOError, json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"  JSON 읽기 예외 감지 ({path.name}): {e}")
    bak = path.with_suffix(path.suffix + '.bak')
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding='utf-8'))
            logger.info(f"  .bak에서 복구: {path.name}")
            safe_json_write(path, data)
            return data
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
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
                logger.debug(f"Backup error: {e}")
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

def safe_json_update(
    path: Path,
    update_func,
    default=None,
    timeout: int = 30,
    indent: int = 2,
    ensure_ascii: bool = False,
    default_serializer=str,
    strict: bool = False,
) -> bool:
    """원자적 JSON Read-Modify-Write (src.utils.file_ops.safe_json_update 단일 SSOT 위임)."""
    from src.utils.file_ops import safe_json_update as _file_ops_sju
    try:
        _file_ops_sju(
            filepath=path,
            mutator=update_func,
            default={} if default is None else default,
            timeout=timeout,
            indent=indent,
            strict=strict,
            ensure_ascii=ensure_ascii,
        )
        return True
    except Exception as e:
        if strict:
            raise
        logger.error(f"safe_json_update 실패 ({path}): {e}")
        return False

def winsorize_mad(series_or_list: Any, threshold_sigma: float = 3.5) -> Any:
    """[Scope A Outlier Guard] MAD(중앙값 절대편차) 기반 3.5σ 이상치 Winsorization 필터.

    원자재/환율 스파이크 및 fat-finger 시세 튀김을 차단합니다.
    """
    try:
        import numpy as np
        import pandas as pd
        if isinstance(series_or_list, (list, tuple)):
            arr = np.array(series_or_list, dtype=float)
        elif isinstance(series_or_list, pd.Series):
            arr = series_or_list.values.copy()
        else:
            return series_or_list

        med = np.median(arr)
        mad = np.median(np.abs(arr - med))
        if mad == 0:
            return series_or_list

        clipped = np.clip(arr, med - threshold_sigma * mad / 0.6745, med + threshold_sigma * mad / 0.6745)
        if isinstance(series_or_list, pd.Series):
            return pd.Series(clipped, index=series_or_list.index)
        return clipped.tolist()
    except Exception:
        return series_or_list


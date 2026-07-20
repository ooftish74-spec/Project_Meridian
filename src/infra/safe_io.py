"""
Safe I/O — 원자적 파일 쓰기 + 안전한 JSON 읽기
=================================================

모든 JSON 쓰기를 원자적(atomic)으로 수행:
  1. temp 파일에 쓰기 (.filename.tmp)
  2. fsync (디스크 플러시)
  3. 원자적 rename (.tmp → .json)
  → 중간 크래시 시 이전 정상 파일 유지

Usage:
    from src.infra.safe_io import safe_json_write, safe_json_read
    safe_json_write(path, data)
    data = safe_json_read(path, default={})
"""
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional
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
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
            raise
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
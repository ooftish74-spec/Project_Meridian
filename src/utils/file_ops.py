import os
import json
import tempfile
from pathlib import Path
from typing import Any, Dict

def atomic_write_json(filepath: str | Path, data: Any, indent: int = 2) -> None:
    """원자적(Atomic)으로 JSON 데이터를 파일에 기록하여 대시보드 등의 동시 읽기 크래시를 방지합니다."""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
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

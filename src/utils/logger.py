"""
Project_First — 중앙 로깅 설정
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def setup_logger(
    name: str = 'project_first',
    level: int = logging.INFO,
    log_to_file: bool = True,
) -> logging.Logger:
    """중앙 로거 설정.

    Args:
        name: 로거 이름
        level: 로깅 레벨
        log_to_file: 파일 로깅 여부

    Returns:
        설정된 Logger
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(
        '%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 콘솔 핸들러
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 파일 핸들러
    if log_to_file:
        log_dir = _PROJECT_ROOT / 'results' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime('%Y%m%d')
        fh = logging.FileHandler(
            log_dir / f'{name}_{today}.log', encoding='utf-8'
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def get_logger(name: str = 'project_first') -> logging.Logger:
    """기존 로거 반환 (없으면 기본 설정으로 생성)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger

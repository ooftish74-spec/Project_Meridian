"""Wave 2 Track F — versioned data tables (universe / ticker-name maps) moved out of code.

Tables live in ``config/params/tables/<name>.json`` (JSON round-trip verified equal to the former literal at
extraction time). ``load_table`` never raises: on a missing/corrupt file it logs CRITICAL and returns the
provided default (empty container) so the caller degrades visibly instead of crashing at import.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
TABLE_DIR = Path(__file__).resolve().parent.parent.parent / 'config' / 'params' / 'tables'
_CACHE: dict = {}


def load_table(name: str, default: Any = None) -> Any:
    if name in _CACHE:
        return json.loads(_CACHE[name])  # fresh copy: callers may mutate module-level dicts
    p = TABLE_DIR / f"{name}.json"
    try:
        raw = p.read_text(encoding='utf-8')
        val = json.loads(raw)
    except Exception as e:
        logger.critical(f"  [tables] {name} unavailable ({e}) — degraded (empty table)")
        return {} if default is None else default
    _CACHE[name] = raw
    return val

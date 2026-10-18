"""
Ticker canonicalization — single source of truth for legacy/dead code migration.

Background (Wave 0, 2026-10-19):
    Four modules each carried a private, silent `_ALIAS = {'430740': '423160', '357330': '357870'}`
    (central_data_gateway, resilient_market_data, intraday_capital_router, stream_orchestrator).
    430740 / 357330 are delisted legacy codes of money-market (KOFR / CD-rate) ETFs; the targets are
    their economically equivalent successors. The mapping itself is legitimate, but silent and
    duplicated substitution is not: every remap is now explicit, logged, and defined here only.

Wave 2 will replace LEGACY_CODE_MAP with the KRX listing master (delisting / code-change events).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# legacy (delisted) code -> successor code. Only economically equivalent instruments may be listed.
LEGACY_CODE_MAP: dict[str, str] = {
    '430740': '423160',  # KODEX KOFR금리액티브(합성) (구) -> KODEX KOFR금리액티브(합성)
    '357330': '357870',  # TIGER CD금리액티브 (구)      -> TIGER CD금리투자KIS(합성)
}

_warned: set[str] = set()


def canonical_ticker(ticker, context: str = '') -> str:
    """Return the current listing code for `ticker` (identity for non-legacy codes)."""
    raw = str(ticker or '').strip()
    mapped = LEGACY_CODE_MAP.get(raw)
    if mapped is None:
        return raw
    key = f"{raw}:{context}"
    if key not in _warned:
        _warned.add(key)
        logger.warning(f"  [TickerCanonical] 상장폐지 구코드 {raw} → 후속코드 {mapped} 로 정규화 (context={context or 'n/a'})")
    return mapped

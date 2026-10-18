"""
Rebase results/shadow_portfolio.json on the broker snapshot (Wave 0, 2026-10-19).

Why
---
The E1 loader bug (fixed in Wave 0) overwrote the production shadow portfolio with an empty book
(positions=0, trade_history=0, NAV booked as 100% cash). The local `results/shadow_trades/*.json`
files cannot be used to rebuild history: they contain simulated/test fills (quantity 0, mode=shadow).

What this does
--------------
* Positions and cash are taken from the broker SSOT `results/kis_portfolio.json` (account.positions).
* NAV = broker total_equity. HWM and initial_capital are REBASED to the broker NAV at this moment
  (recorded in `track_record_rebase`), so drawdown restarts from 0 instead of being measured against
  an unverifiable historical peak.
* trade_history starts empty; Wave 1 reconstructs it from broker fill records (inquire-daily-ccld).
* The previous file is kept as shadow_portfolio.json.pre_rebase_<ts>.

Usage
-----
    python3 scripts/ops/rebase_shadow_from_broker.py            # dry run: prints the plan only
    python3 scripts/ops/rebase_shadow_from_broker.py --apply    # writes (daemon must be stopped)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from src.utils.file_ops import atomic_write_json  # noqa: E402

RESULTS = _ROOT / 'results'
KIS = RESULTS / 'kis_portfolio.json'
SHADOW = RESULTS / 'shadow_portfolio.json'
MAX_SNAPSHOT_AGE_H = 24.0


def build_rebased_portfolio(kis: dict, prev: dict | None, now: datetime) -> dict:
    acct = kis.get('account') or {}
    holdings = kis.get('holdings') or {}
    positions_src = acct.get('positions') or {}
    if not positions_src and holdings:
        positions_src = holdings
    nav = float(acct.get('total_equity') or kis.get('nav') or 0)
    cash_krw = float(acct.get('cash') or 0)
    if nav <= 0:
        raise ValueError("broker NAV (account.total_equity) missing or non-positive — refusing to rebase")

    positions = {}
    for ticker, p in positions_src.items():
        qty = int(float(p.get('qty', p.get('quantity', 0)) or 0))
        if qty <= 0:
            continue
        stream = (holdings.get(ticker) or {}).get('stream_id') or p.get('stream_id') or 'S_RECON'
        is_us = not str(ticker).isdigit()
        avg = float(p.get('avg_price') or 0)
        cur = float(p.get('current_price') or avg or 0)
        positions[f"{stream}:{ticker}"] = {
            'stream_id': stream,
            'ticker': ticker,
            'name': p.get('name', ticker),
            'quantity': qty,
            'avg_price': avg,
            'entry_price': avg,
            'current_price': cur,
            'currency': p.get('currency', 'USD' if is_us else 'KRW'),
            'market': (holdings.get(ticker) or {}).get('market', 'US' if is_us else 'KR'),
            'market_value': float(p.get('eval_krw') or 0),
            'amount': float(p.get('eval_krw') or 0),
            'pnl_pct': float(p.get('pnl_pct', p.get('return_pct', 0)) or 0),
            'entry_date': p.get('entry_date', ''),
            'source': 'broker_rebase_2026-10-19',
        }

    prev = prev or {}
    return {
        'created': prev.get('created', now.strftime('%Y-%m-%d')),
        'updated': now.isoformat(),
        'virtual_nav': nav,
        'cash': cash_krw,
        'cash_usd': float(acct.get('us_cash_usd') or acct.get('cash_usd') or 0),
        'hwm': nav,
        'initial_capital': nav,
        'positions': positions,
        'trade_history': [],
        'daily_snapshots': [],
        'daily_records': [],
        'daily_returns': [],
        'realized_pnl': 0,
        'unrealized_pnl': 0,
        'total_commission': 0,
        'strategy_pnl': {},
        'track_record_rebase': {
            'at': now.isoformat(),
            'reason': 'Wave0 E1: production shadow book was wiped by loader bug; rebased on broker snapshot',
            'broker_snapshot_ts': kis.get('timestamp') or kis.get('updated_at'),
            'broker_nav': nav,
            'previous_file_summary': {
                'positions': len(prev.get('positions') or {}),
                'trade_history': len(prev.get('trade_history') or []),
                'virtual_nav': prev.get('virtual_nav'),
                'hwm': prev.get('hwm'),
            },
            'history_reconstruction': 'pending (Wave 1: broker inquire-daily-ccld)',
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    if not KIS.exists():
        print("❌ results/kis_portfolio.json not found"); return 2
    kis = json.loads(KIS.read_text(encoding='utf-8'))
    ts_raw = kis.get('timestamp') or kis.get('updated_at')
    now = datetime.now()
    if ts_raw:
        age_h = (now - datetime.fromisoformat(str(ts_raw)).replace(tzinfo=None)).total_seconds() / 3600.0
        print(f"broker snapshot: {ts_raw} (age {age_h:.1f}h)")
        if age_h > MAX_SNAPSHOT_AGE_H:
            print(f"❌ broker snapshot older than {MAX_SNAPSHOT_AGE_H}h — refresh kis_portfolio.json first"); return 3
    prev = json.loads(SHADOW.read_text(encoding='utf-8')) if SHADOW.exists() else None
    new = build_rebased_portfolio(kis, prev, now)

    print(f"NAV={new['virtual_nav']:,.0f}  cash_krw={new['cash']:,.0f}  cash_usd={new['cash_usd']:,.2f}  hwm(rebased)={new['hwm']:,.0f}")
    for k, p in new['positions'].items():
        print(f"  {k:<16} qty={p['quantity']:>5}  avg={p['avg_price']:>12,.2f}  cur={p['current_price']:>12,.2f}  {p['currency']}")
    print(f"previous: {new['track_record_rebase']['previous_file_summary']}")

    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply (daemon stopped).")
        return 0
    if SHADOW.exists():
        bk = SHADOW.with_name(f"shadow_portfolio.json.pre_rebase_{now.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(SHADOW, bk)
        print(f"backup: {bk}")
    atomic_write_json(SHADOW, new, indent=2, ensure_ascii=False, default=str)
    print("✅ shadow_portfolio.json rebased on broker snapshot")
    return 0


if __name__ == '__main__':
    sys.exit(main())

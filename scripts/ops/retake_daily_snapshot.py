"""
Re-take today's shadow daily snapshot on the broker NAV basis (2026-10-19 incident remediation).

Context: the 15:35 aftermarket snapshot recorded a fake −34.76% daily return because the KRW-only shadow
ledger omitted USD cash (and KR closes were not marked). The kill switch read it and halted new entries.
With the Wave 1 fix (daily_snapshot uses a fresh broker total equity in live mode), re-running the snapshot
replaces today's record/return in place (same-date replacement is built into daily_snapshot()).

Usage (production, after deploy):
    venv/bin/python3 scripts/ops/retake_daily_snapshot.py            # dry run: shows before/after
    venv/bin/python3 scripts/ops/retake_daily_snapshot.py --apply    # writes under the shadow lock
"""
from __future__ import annotations

import argparse

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--regime', default='caution')
    args = ap.parse_args()
    from src.portfolio.shadow_manager import ShadowPortfolioManager

    if not args.apply:
        m = ShadowPortfolioManager()
        before = (m.data.get('daily_returns') or [None])[-1], m.data.get('virtual_nav')
        broker = m._broker_nav_if_fresh()
        print(f"before: daily_return={before[0]} virtual_nav={before[1]}  broker_nav(fresh)={broker}")
        if not broker:
            print("❌ fresh broker NAV not available (kis_portfolio.json older than 60 min or not live) — refresh first")
            return 2
        from src.portfolio.shadow_manager import _today
        today = _today()
        prev_nav = float(m.data.get('initial_capital') or 0)
        for s in reversed(m.data.get('daily_snapshots') or []):
            if s.get('date') != today:
                prev_nav = float(s.get('nav') or prev_nav)
                break
        exp = (broker / prev_nav - 1) if prev_nav > 0 else None
        print(f"after (expected): virtual_nav={broker:,.0f} vs prev_nav={prev_nav:,.0f} → daily_return={exp}")
        print("DRY RUN — nothing written. Re-run with --apply.")
        return 0

    with ShadowPortfolioManager().transaction() as m:
        if not m._broker_nav_if_fresh():
            print("❌ fresh broker NAV not available — aborting (nothing written)")
            return 2
        m.daily_snapshot(regime=args.regime)
        print(f"✅ snapshot retaken: daily_return={(m.data.get('daily_returns') or [None])[-1]} "
              f"virtual_nav={m.data.get('virtual_nav')} nav_source={m.data.get('nav_source')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

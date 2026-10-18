#!/usr/bin/env python3
"""
Project Meridian — Live Diagnostics CLI (Track E / Wave 3)
==========================================================
Go/No-Go 게이트를 대체하는 소액 실전 일일 진단. 항상 비차단(decision=DIAGNOSE_CONTINUE).

Usage:
    python3 scripts/live_diagnostics.py                       # results/ 기준, 결과를 results/ 에 기록
    python3 scripts/live_diagnostics.py --results-dir /tmp/snap/results --out-dir /tmp/out --no-record-nav
"""
import argparse
import json
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.measurement.live_diagnostics import run_live_diagnostics  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Meridian live diagnostics (non-blocking)')
    ap.add_argument('--results-dir', type=Path, default=None, help='입력 results 디렉터리 (기본: 프로젝트 results/)')
    ap.add_argument('--out-dir', type=Path, default=None, help='출력 디렉터리 (기본: results-dir)')
    ap.add_argument('--no-record-nav', action='store_true', help='live_nav_history.json 축적 생략')
    ap.add_argument('--no-write', action='store_true', help='파일 기록 없이 stdout 요약만')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
    res = run_live_diagnostics(results_dir=args.results_dir, write=not args.no_write, out_dir=args.out_dir,
                               record_nav=not args.no_record_nav)
    print(json.dumps(res.get('summary', {}), ensure_ascii=False, indent=2, default=str))
    return 0  # 진단은 절대 실패 코드로 파이프라인을 막지 않는다


if __name__ == '__main__':
    sys.exit(main())

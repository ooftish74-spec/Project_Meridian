#!/usr/bin/env python3
"""
Wave 2 Track F — hardcoding scanner + ratchet.

AST-based count of hardcoded market literals in src/ and scripts/ (tests, venv, .archive, scratch,
__pycache__ excluded):

  kr_ticker_literals   string constants that look like KRX codes: 6 digits ('005930') or the new
                       alphanumeric form ('0193L0'); well-known HHMMSS time strings are ignored.
  us_ticker_literals   string constants in KNOWN_US (curated; ambiguous English words excluded).
  threshold_literals   numeric constants used directly in comparisons (a > 0.3) or as direct
                       arguments of min/max/clip/np.clip/np.minimum/np.maximum.
  fallback_literals    numeric constants used as fallbacks: ``x.get(key, 0.5)``, ``_cfg_get(key, 0.5)`` (any
                       callable whose name ends in 'get'), ``get(key, default=0.5)``,
                       ``getattr(o, 'a', 0.5)``, ``x or 0.5``.

Trivial numbers {0, 1, -1, 2, 100} are excluded from the numeric categories. Docstrings and
f-string fragments are ignored. A statement whose first line carries ``# hardcode-ok`` (reason
required by convention) is excluded and reported under ``allowed`` — reserved for SSOT tables of
L0 facts (e.g. src/data_collection/ticker_master.STRUCTURAL_ETP). Call-site fallbacks of
``pget(name, fallback)`` are registry-bound (pinned to config/params/parameter_registry.json by tests)
and are therefore not counted.

Usage:
  python scripts/ops/hardcoding_scan.py                 # write baseline (default config/params/hardcoding_baseline.json)
  python scripts/ops/hardcoding_scan.py --check         # exit 1 if any ratchet total exceeds the baseline
  python scripts/ops/hardcoding_scan.py --out results/hardcoding_baseline.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOTS = ('src', 'scripts')
EXCLUDE_PARTS = {'tests', 'test', 'venv', '.venv', '.archive', 'scratch', '__pycache__', 'node_modules'}
# Tracked (results/*.json is git-ignored) so CI can ratchet against it; outside config/*.json so the pytest
# SSOT snapshot/restore (tests/conftest.py) never reverts a freshly lowered baseline.
DEFAULT_BASELINE = ROOT / 'config' / 'params' / 'hardcoding_baseline.json'
CATEGORIES = ('kr_ticker_literals', 'us_ticker_literals', 'threshold_literals', 'fallback_literals')
PRAGMA = 'hardcode-ok'

TRIVIAL_NUMBERS = {0.0, 1.0, -1.0, 2.0, 100.0}
KR_TICKER_RX = re.compile(r'^(\d{6}|\d{4}[A-Z]\d)$')
# HHMMSS strings commonly used for session times — not tickers.
TIME_STRINGS = frozenset({'000000', '080000', '083000', '084000', '085000', '085500', '090000', '090500',
                          '091000', '093000', '100000', '110000', '120000', '130000', '140000', '150000',
                          '151500', '152000', '152500', '153000', '153500', '154000', '160000', '170000',
                          '180000', '200000', '220000', '230000', '235959', '235900'})
KNOWN_US = frozenset("""
SHV QQQ SPY SQQQ TQQQ SOXS SOXX SOXL XLK SGOV EWY GLD TLT UUP USO BIL PSQ SPXS FXI UPRO BDRY XLF XLE
XLI XLV HYG QID SDS LIT IWM IEF VIXY QLD SSO SLX URA XLP XLU ITA FLKR XLY CPER XBI VOO IVV SPXL TECL
XLB VNQ DIA EWJ SPXU TNA TZA BOTZ IGV XLC ROBO QTUM CBON WEBL BOIL KOLD SVIX UVIX LABU LABD BULZ CONL
MSTX MSTU SLV NVDL TSLL FNGU SHY UVXY SVXY SH SMH AGG BND LQD EMB EEM VWO EFA VEA GDX KWEB MCHI
NVDA TSLA AAPL TSM MSFT PBR GOOGL GOOG META AMZN MU TMUS AVGO JPM BAC UNH JNJ WMT AMD INTC CRM ORCL ADBE
LLY PFE ABBV XOM CVX PG COST DIS NFLX IONQ RGTI QBTS ISRG ROK FANUY PLTR WFC COP SLB LIN APD FCX NEM
PEP NEE DUK AEP LMT RTX NOC PLD AMT EQIX SPG ALB ENPH FSLR QCOM AMAT LRCX KLAC MRVL MRK MCD HBM ASML
""".split())

_RATCHET_DOC = 'current totals must be <= baseline totals for every category (see --check)'


def _is_num(node: ast.AST) -> Optional[float]:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _is_num(node.operand)
        if v is not None:
            return -v if isinstance(node.op, ast.USub) else v
    return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ''


_CLAMP_FUNCS = {'min', 'max', 'clip', 'minimum', 'maximum'}
_PRAGMA_STMTS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Return)


class _Visitor(ast.NodeVisitor):
    def __init__(self, allowed_lines: Set[int]):
        self.allowed_lines = allowed_lines
        self.counts = {c: 0 for c in CATEGORIES}
        self.allowed = 0
        self.samples: Dict[str, List[Tuple[int, str]]] = {c: [] for c in CATEGORIES}
        self._skip: Set[int] = set()   # id() of Constant nodes to ignore (docstrings, f-strings, pget)
        self._counted: Set[int] = set()

    def _hit(self, cat: str, node: ast.AST, text: str) -> None:
        if id(node) in self._counted:
            return
        self._counted.add(id(node))
        if getattr(node, 'lineno', -1) in self.allowed_lines:
            self.allowed += 1
            return
        self.counts[cat] += 1
        if len(self.samples[cat]) < 5:
            self.samples[cat].append((node.lineno, text))

    # docstrings
    def _mark_docstring(self, body):
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            self._skip.add(id(body[0].value))

    def visit_Module(self, node):
        self._mark_docstring(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._mark_docstring(node.body)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._mark_docstring(node.body)
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        for v in ast.walk(node):
            if isinstance(v, ast.Constant):
                self._skip.add(id(v))
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _call_name(node.func)
        if name == 'pget':  # registry-bound call-site fallback
            for a in list(node.args[1:]) + [k.value for k in node.keywords]:
                for v in ast.walk(a):
                    self._skip.add(id(v))
        elif name.endswith('get') and name != 'getattr':  # .get / _cfg_get / _jget / cfg_get helpers
            cand = list(node.args[1:2]) + [k.value for k in node.keywords if k.arg == 'default']
            for a in cand:
                v = _is_num(a)
                if v is not None and v not in TRIVIAL_NUMBERS:
                    self._hit('fallback_literals', a, f".get(…, {v:g})")
        elif name == 'getattr' and len(node.args) >= 3:
            v = _is_num(node.args[2])
            if v is not None and v not in TRIVIAL_NUMBERS:
                self._hit('fallback_literals', node.args[2], f"getattr(…, {v:g})")
        elif name in _CLAMP_FUNCS:
            for a in node.args:
                v = _is_num(a)
                if v is not None and v not in TRIVIAL_NUMBERS:
                    self._hit('threshold_literals', a, f"{name}(…, {v:g})")
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.Or):
            for a in node.values[1:]:
                v = _is_num(a)
                if v is not None and v not in TRIVIAL_NUMBERS:
                    self._hit('fallback_literals', a, f"or {v:g}")
        self.generic_visit(node)

    def visit_Compare(self, node):
        for a in [node.left] + list(node.comparators):
            v = _is_num(a)
            if v is not None and v not in TRIVIAL_NUMBERS:
                self._hit('threshold_literals', a, f"cmp {v:g}")
        self.generic_visit(node)

    def visit_Constant(self, node):
        if id(node) in self._skip or not isinstance(node.value, str):
            return
        s = node.value
        if KR_TICKER_RX.match(s) and s not in TIME_STRINGS:
            self._hit('kr_ticker_literals', node, repr(s))
        elif s in KNOWN_US:
            self._hit('us_ticker_literals', node, repr(s))


def _allowed_lines(tree: ast.AST, source: str) -> Set[int]:
    lines = source.splitlines()
    pragma_lines = {i + 1 for i, ln in enumerate(lines) if PRAGMA in ln and '#' in ln}
    allowed: Set[int] = set()
    if not pragma_lines:
        return allowed
    for node in ast.walk(tree):
        if isinstance(node, _PRAGMA_STMTS) and node.lineno in pragma_lines:
            allowed.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return allowed | pragma_lines


def scan_source(source: str, filename: str = '<src>') -> Dict:
    tree = ast.parse(source, filename=filename)
    v = _Visitor(_allowed_lines(tree, source))
    v.visit(tree)
    return {'counts': v.counts, 'allowed': v.allowed,
            'samples': {k: s for k, s in v.samples.items() if s}}


def iter_files(roots: Iterable[str], base: Path = ROOT) -> Iterable[Path]:
    for r in roots:
        rp = base / r
        if not rp.exists():
            continue
        for p in sorted(rp.rglob('*.py')):
            rel = p.relative_to(base)
            if any(part in EXCLUDE_PARTS for part in rel.parts):
                continue
            yield p


def scan(roots: Iterable[str] = DEFAULT_ROOTS, base: Path = ROOT) -> Dict:
    files: Dict[str, Dict] = {}
    totals = {c: 0 for c in CATEGORIES}
    allowed = 0
    errors: List[str] = []
    for p in iter_files(roots, base):
        rel = str(p.relative_to(base))
        try:
            res = scan_source(p.read_text(encoding='utf-8'), rel)
        except (SyntaxError, UnicodeDecodeError) as e:
            errors.append(f"{rel}: {e.__class__.__name__}")
            continue
        allowed += res['allowed']
        if any(res['counts'].values()) or res['allowed']:
            files[rel] = {**res['counts'], 'allowed': res['allowed']}
        for c in CATEGORIES:
            totals[c] += res['counts'][c]
    return {'version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'roots': list(roots), 'ratchet': _RATCHET_DOC, 'totals': totals,
            'total_all': sum(totals.values()), 'allowed_pragmas': allowed,
            'n_files_scanned': sum(1 for _ in iter_files(roots, base)), 'parse_errors': errors,
            'files': dict(sorted(files.items(), key=lambda kv: -sum(kv[1][c] for c in CATEGORIES)))}


def check(current: Dict, baseline: Dict) -> List[str]:
    """Return violations (category exceeded) — empty list means ratchet OK."""
    out = []
    bt = baseline.get('totals', {})
    for c in CATEGORIES:
        if c in bt and current['totals'][c] > bt[c]:
            grew = [f"{f} ({sum(v[k] for k in CATEGORIES)}→)" for f, v in current['files'].items()
                    if v[c] > baseline.get('files', {}).get(f, {}).get(c, 0)][:10]
            out.append(f"{c}: {current['totals'][c]} > baseline {bt[c]} (files grown: {grew})")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='compare to baseline; exit 1 on growth')
    ap.add_argument('--baseline', default=str(DEFAULT_BASELINE))
    ap.add_argument('--out', default=None, help='where to write the scan (default: --baseline path)')
    ap.add_argument('--roots', nargs='*', default=list(DEFAULT_ROOTS))
    a = ap.parse_args(argv)
    cur = scan(a.roots)
    summary = {'totals': cur['totals'], 'total_all': cur['total_all'], 'allowed_pragmas': cur['allowed_pragmas'],
               'n_files_scanned': cur['n_files_scanned']}
    if a.check:
        bpath = Path(a.baseline)
        if not bpath.exists():
            print(f"baseline missing: {bpath}", file=sys.stderr)
            return 2
        base = json.loads(bpath.read_text(encoding='utf-8'))
        viol = check(cur, base)
        print(json.dumps({**summary, 'baseline_totals': base.get('totals'), 'violations': viol},
                         ensure_ascii=False, indent=2))
        return 1 if viol else 0
    out = Path(a.out or a.baseline)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cur, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(json.dumps({**summary, 'written': str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

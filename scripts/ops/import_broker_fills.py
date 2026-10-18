"""
Import real broker fills into shadow_portfolio.json trade_history (Wave 0 'F', 2026-10-19).

Why
---
The E1 loader bug wiped the production shadow book (trade_history=0). `rebase_shadow_from_broker.py`
restores positions/cash from the broker; this tool restores the *history* from the broker's own fill
ledger, so measurement (realized PnL, hold days, turnover) is based on real executions, never on
simulated/test fills.

Safety
------
* READ-ONLY against the broker: every `requests.post` except token issuance raises.
* Default is a dry run that writes only `results/broker_fills_import_<ts>.json` (audit artifact).
* `--apply` merges into results/shadow_portfolio.json (daemon must be stopped):
    - dedupe key = (market, order date, odno) → idempotent, re-runnable
    - existing records are never modified or removed (history can only grow)
    - position entry_date is replaced only when the FIFO lot reconstruction reproduces the broker
      quantity exactly (otherwise the window is incomplete and the existing value is kept)

Field names verified against the live account on 2026-10-19 (scripts/ops/verify_kis_readonly.py):
  KRX TTTC8001R : odno, pdno, prdt_name, sll_buy_dvsn_cd(01 sell/02 buy), ord_dt, ord_tmd, tot_ccld_qty, avg_prvs
  US  TTTS3035R : odno, pdno, prdt_name, sll_buy_dvsn_cd, dmst_ord_dt, thco_ord_tmd, ft_ccld_qty
                  (price: ft_ccld_unpr3 — not yet observed on a filled row; missing price is flagged, not guessed)

Usage (production host, project root as CWD):
    venv/bin/python3 scripts/ops/import_broker_fills.py --days 90            # dry run
    venv/bin/python3 scripts/ops/import_broker_fills.py --days 90 --apply    # merge (daemon stopped)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

RESULTS = _ROOT / 'results'
SHADOW = RESULTS / 'shadow_portfolio.json'
KIS = RESULTS / 'kis_portfolio.json'
SOURCE_TAG = 'broker_fill_import'

_US_PRICE_FIELDS = ('ft_ccld_unpr3', 'ft_ccld_unpr', 'ovrs_ccld_unpr', 'ccld_unpr')


# ─────────────────────────────────────────────────────────────
# Pure transformation (unit-tested; no I/O)
# ─────────────────────────────────────────────────────────────
def _f(x) -> Optional[float]:
    try:
        v = float(str(x).replace(',', ''))
        return v
    except (TypeError, ValueError):
        return None


def normalize_fill_rows(rows: Iterable[dict], market: str) -> List[dict]:
    """Broker rows → trade_history records. Rows with zero filled quantity are dropped."""
    out = []
    for r in rows or []:
        if market == 'KR':
            qty = int(_f(r.get('tot_ccld_qty')) or 0)
            px = _f(r.get('avg_prvs'))
            d, t = str(r.get('ord_dt', '')), str(r.get('ord_tmd', '') or '000000')
            ccy = 'KRW'
        else:
            qty = int(_f(r.get('ft_ccld_qty')) or 0)
            px = next((p for p in (_f(r.get(k)) for k in _US_PRICE_FIELDS) if p and p > 0), None)
            d, t = str(r.get('dmst_ord_dt', r.get('ord_dt', ''))), str(r.get('thco_ord_tmd', r.get('ord_tmd', '')) or '000000')
            ccy = 'USD'
        if qty <= 0 or len(d) != 8:
            continue
        side = str(r.get('sll_buy_dvsn_cd', '')).strip()
        if side not in ('01', '02'):
            continue
        t = (t + '000000')[:6]
        rec = {
            'date': f"{d[:4]}-{d[4:6]}-{d[6:]}",
            'timestamp': f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}",
            'action': 'SELL' if side == '01' else 'BUY',
            'ticker': str(r.get('pdno', '')).strip(),
            'name': str(r.get('prdt_name', '') or r.get('pdno', '')).strip(),
            'quantity': qty,
            'price': px,
            'amount': (px * qty) if px else None,
            'currency': ccy,
            'market': market,
            'odno': str(r.get('odno', '')).strip(),
            'stream': 'S_RECON',
            'stream_id': 'S_RECON',
            'source': SOURCE_TAG,
        }
        if px is None:
            rec['price_missing'] = True
        out.append(rec)
    out.sort(key=lambda x: x['timestamp'])
    return out


def _dedupe_key(rec: dict) -> Tuple[str, str, str]:
    return (str(rec.get('market', '')), str(rec.get('date', '')), str(rec.get('odno', '')))


def merge_history(existing: List[dict], imported: List[dict]) -> Tuple[List[dict], int]:
    """Append-only, idempotent merge. Existing records are untouched."""
    seen = {_dedupe_key(r) for r in existing if r.get('odno')}
    added = []
    for r in imported:
        k = _dedupe_key(r)
        if not r.get('odno') or k in seen:
            continue
        seen.add(k)
        added.append(r)
    merged = list(existing) + added
    merged.sort(key=lambda x: str(x.get('timestamp', x.get('date', ''))))
    return merged, len(added)


def fifo_entry_dates(fills: List[dict], broker_qty: Dict[str, int]) -> Dict[str, str]:
    """Earliest open-lot date per ticker under FIFO — only when lots reproduce the broker quantity exactly."""
    lots: Dict[str, deque] = {}
    for f in sorted(fills, key=lambda x: x['timestamp']):
        dq = lots.setdefault(f['ticker'], deque())
        q = int(f['quantity'])
        if f['action'] == 'BUY':
            dq.append([f['date'], q])
        else:
            while q > 0 and dq:
                take = min(q, dq[0][1])
                dq[0][1] -= take
                q -= take
                if dq[0][1] == 0:
                    dq.popleft()
    out = {}
    for tk, want in broker_qty.items():
        dq = lots.get(tk)
        if dq and sum(l[1] for l in dq) == int(want):
            out[tk] = dq[0][0]
    return out


# ─────────────────────────────────────────────────────────────
# Broker I/O (read-only)
# ─────────────────────────────────────────────────────────────
def _install_post_guard():
    import requests
    real_post = requests.post

    def _no_post(url, *a, **k):
        if '/oauth2/tokenP' in str(url):
            return real_post(url, *a, **k)
        raise RuntimeError(f"READ-ONLY IMPORT: POST blocked -> {url}")
    requests.post = _no_post


def _paged_get(adapter, tr_id: str, path: str, params: dict, rows_key: str, ctx_fk: str, ctx_nk: str, max_pages: int = 50) -> List[dict]:
    import requests
    import time as _t
    rows, tr_cont = [], ''
    for _ in range(max_pages):
        h = adapter._get_headers()
        h['tr_id'] = tr_id
        if tr_cont:
            h['tr_cont'] = 'N'
        r = requests.get(f"{adapter.base_url}{path}", headers=h, params=params, timeout=15)
        d = r.json()
        if d.get('rt_cd') != '0':
            raise RuntimeError(f"{tr_id} rt_cd={d.get('rt_cd')} msg_cd={d.get('msg_cd')} msg={str(d.get('msg1', ''))[:80]}")
        rows.extend(d.get(rows_key) or [])
        tr_cont = str(r.headers.get('tr_cont', '') or '')
        if tr_cont not in ('M', 'F'):
            break
        params = {**params, ctx_fk: d.get(ctx_fk.lower(), ''), ctx_nk: d.get(ctx_nk.lower(), '')}
        _t.sleep(0.25)   # KIS rate limit
    return rows


def fetch_broker_fills(days: int) -> Tuple[List[dict], List[dict]]:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / '.env')
    from src.execution._kis_adapter import KISTraderAdapter
    a = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
    if not a.authenticate():
        raise RuntimeError('KIS auth failed')
    acnt = a.account_no.split('-') if '-' in a.account_no else [a.account_no[:8], a.account_no[8:] or '01']
    base = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01'}
    end = datetime.now()
    start = end - timedelta(days=min(int(days), 92))   # TTTC8001R covers ≤ 3 months
    kr = _paged_get(a, 'TTTC8001R', '/uapi/domestic-stock/v1/trading/inquire-daily-ccld',
                    {**base, 'INQR_STRT_DT': start.strftime('%Y%m%d'), 'INQR_END_DT': end.strftime('%Y%m%d'),
                     'SLL_BUY_DVSN_CD': '00', 'INQR_DVSN': '01', 'PDNO': '', 'CCLD_DVSN': '01', 'ORD_GNO_BRNO': '',
                     'ODNO': '', 'INQR_DVSN_3': '00', 'INQR_DVSN_1': '', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''},
                    'output1', 'CTX_AREA_FK100', 'CTX_AREA_NK100')
    us = _paged_get(a, 'TTTS3035R', '/uapi/overseas-stock/v1/trading/inquire-ccnl',
                    {**base, 'PDNO': '%', 'ORD_STRT_DT': start.strftime('%Y%m%d'), 'ORD_END_DT': end.strftime('%Y%m%d'),
                     'SLL_BUY_DVSN': '00', 'CCLD_NCCS_DVSN': '01', 'OVRS_EXCG_CD': '%', 'SORT_SQN': 'AS', 'ORD_DT': '',
                     'ORD_GNO_BRNO': '', 'ODNO': '', 'CTX_AREA_NK200': '', 'CTX_AREA_FK200': ''},
                    'output', 'CTX_AREA_FK200', 'CTX_AREA_NK200')
    return kr, us


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    _install_post_guard()
    kr_rows, us_rows = fetch_broker_fills(args.days)
    fills = normalize_fill_rows(kr_rows, 'KR') + normalize_fill_rows(us_rows, 'US')
    fills.sort(key=lambda x: x['timestamp'])
    now = datetime.now()
    audit = (RESULTS if args.apply else Path('/tmp')) / f"broker_fills_import_{now.strftime('%Y%m%d_%H%M%S')}.json"
    from src.utils.file_ops import atomic_write_json
    atomic_write_json(audit, {'fetched_at': now.isoformat(), 'days': args.days, 'raw_counts': {'KR': len(kr_rows), 'US': len(us_rows)},
                              'fills': fills}, indent=2, ensure_ascii=False, default=str)
    n_missing_px = sum(1 for f in fills if f.get('price_missing'))
    print(f"broker fills: KR={sum(f['market'] == 'KR' for f in fills)} US={sum(f['market'] == 'US' for f in fills)} "
          f"(price_missing={n_missing_px}) → audit {audit.name}")

    broker_qty = {}
    if KIS.exists():
        acct = (json.loads(KIS.read_text(encoding='utf-8')).get('account') or {})
        broker_qty = {t: int(float(p.get('qty', p.get('quantity', 0)) or 0)) for t, p in (acct.get('positions') or {}).items()}
    entry = fifo_entry_dates(fills, broker_qty)
    for tk, q in broker_qty.items():
        print(f"  {tk:<8} broker_qty={q:>5}  fifo_entry_date={entry.get(tk, '(window incomplete — keep existing)')}")

    if not SHADOW.exists():
        print("❌ results/shadow_portfolio.json missing — run rebase_shadow_from_broker.py first"); return 2
    shadow = json.loads(SHADOW.read_text(encoding='utf-8'))
    _importable = [f for f in fills if not f.get('price_missing')]
    merged, n_added = merge_history(shadow.get('trade_history') or [], _importable)
    print(f"trade_history: existing={len(shadow.get('trade_history') or [])} +{n_added} → {len(merged)} "
          f"(price-missing rows kept in audit only: {n_missing_px})")
    if not args.apply:
        print("DRY RUN — shadow_portfolio.json not modified. Re-run with --apply (daemon stopped).")
        return 0

    bk = SHADOW.with_name(f"shadow_portfolio.json.pre_fill_import_{now.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(SHADOW, bk)
    shadow['trade_history'] = merged
    for key, p in (shadow.get('positions') or {}).items():
        tk = p.get('ticker') or str(key).split(':')[-1]
        if tk in entry:
            p['entry_date'] = entry[tk]
            p['entry_date_source'] = 'broker_fifo'
    rb = shadow.setdefault('track_record_rebase', {})
    rb['history_reconstruction'] = {'at': now.isoformat(), 'added': n_added, 'audit_file': audit.name,
                                    'price_missing': n_missing_px, 'window_days': args.days}
    atomic_write_json(SHADOW, shadow, indent=2, ensure_ascii=False, default=str)
    print(f"✅ merged (+{n_added}); backup {bk.name}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

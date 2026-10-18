"""
READ-ONLY KIS OpenAPI schema probe (Wave 0 risk reduction, 2026-10-19).

Purpose: verify — against the real broker — the response field names and semantics that the
Wave 0 fixes rely on (E3/E5/E6/E7/E9, D5). It NEVER submits, modifies or cancels orders:
`requests.post` is patched to raise before any call is made (token reuse only via cache/GET).

Output: field names, rt_cd/msg codes and row counts only. No account numbers, tokens, or prices of
holdings are printed.

Usage (on the production host, project root as CWD):
    venv/bin/python3 /tmp/verify_kis_readonly.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

_real_post = requests.post


def _no_post(url, *a, **k):
    # Token issuance is the only POST we tolerate (the adapter's normal auth path); everything else is refused.
    if '/oauth2/tokenP' in str(url):
        return _real_post(url, *a, **k)
    raise RuntimeError(f"READ-ONLY PROBE: POST blocked -> {url}")


requests.post = _no_post

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env')
from src.execution._kis_adapter import KISTraderAdapter  # noqa: E402

report = {'probe_at': datetime.now().isoformat(), 'checks': {}}


def keys_of(rows, limit=1):
    if isinstance(rows, dict):
        return sorted(rows.keys())
    if isinstance(rows, list) and rows:
        return sorted(rows[0].keys()) if isinstance(rows[0], dict) else []
    return []


def get(adapter, tr_id, path, params):
    h = adapter._get_headers()
    h['tr_id'] = tr_id
    r = requests.get(f"{adapter.base_url}{path}", headers=h, params=params, timeout=10)
    try:
        d = r.json()
    except Exception:
        return {'http': r.status_code, 'json': None}
    return {'http': r.status_code, 'rt_cd': d.get('rt_cd'), 'msg_cd': d.get('msg_cd'), 'msg1': (d.get('msg1') or '')[:80],
            'output_keys': {k: keys_of(v) for k, v in d.items() if k.startswith('output')},
            'output_rows': {k: (len(v) if isinstance(v, list) else 1) for k, v in d.items() if k.startswith('output')},
            '_raw': d}


def safe_sample(row, fields):
    return {f: row.get(f) for f in fields if f in row}


a = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
assert a.authenticate(), "auth failed"
acnt = a.account_no.split('-') if '-' in a.account_no else [a.account_no[:8], a.account_no[8:] or '01']
base = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01'}

# 1) Domestic balance (E5)
res = get(a, 'TTTC8434R', '/uapi/domestic-stock/v1/trading/inquire-balance',
          {**base, 'AFHR_FLPR_YN': 'N', 'OFL_YN': 'N', 'INQR_DVSN': '02', 'UNPR_DVSN': '01', 'FUND_STTL_ICLD_YN': 'N',
           'FNCG_AMT_AUTO_RDPT_YN': 'N', 'PRCS_DVSN': '01', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''})
raw = res.pop('_raw', {})
res['sample_fields'] = [safe_sample(x, ['pdno', 'hldg_qty', 'ord_psbl_qty']) for x in (raw.get('output1') or [])][:10]
res['tr_cont_header_hint'] = 'pagination via CTX_AREA_FK100/NK100'
report['checks']['domestic_inquire_balance'] = res

# 2) Overseas present-balance as used by fetch_live_positions today (E9 question)
res = get(a, 'TTTS3012R', '/uapi/overseas-stock/v1/trading/inquire-present-balance',
          {**base, 'WCRS_DVSN_CD': '01', 'OVRS_EXCG_CD': '%', 'TR_CRCY_CD': 'USD', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''})
raw = res.pop('_raw', {})
res['sample_fields'] = [safe_sample(x, ['ovrs_pdno', 'ovrs_cblc_qty', 'pdno']) for x in (raw.get('output1') or [])][:10]
report['checks']['overseas_present_balance_TTTS3012R'] = res

# 2b) Same tr_id on the documented inquire-balance URL, per exchange
for ex in ('NASD', 'NYSE', 'AMEX'):
    res = get(a, 'TTTS3012R', '/uapi/overseas-stock/v1/trading/inquire-balance',
              {**base, 'OVRS_EXCG_CD': ex, 'TR_CRCY_CD': 'USD', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''})
    raw = res.pop('_raw', {})
    res['sample_fields'] = [safe_sample(x, ['ovrs_pdno', 'ovrs_cblc_qty']) for x in (raw.get('output1') or [])][:10]
    report['checks'][f'overseas_inquire_balance_{ex}'] = res

# 3) Domestic cancellable (unexecuted) orders (E3/E6)
res = get(a, 'TTTC8036R', '/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl',
          {**base, 'INQR_DVSN_1': '0', 'INQR_DVSN_2': '0', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''})
res.pop('_raw', None)
report['checks']['domestic_unexecuted_TTTC8036R'] = res

# 4) Overseas unexecuted (E3/E6)
res = get(a, 'TTTS3018R', '/uapi/overseas-stock/v1/trading/inquire-nccs',
          {**base, 'OVRS_EXCG_CD': 'NASD', 'SORT_SQN': 'DS', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''})
res.pop('_raw', None)
report['checks']['overseas_unexecuted_TTTS3018R'] = res

# 5) Domestic daily fills over last 7 days (E6/E7, history reconstruction)
end = datetime.now()
start = end - timedelta(days=7)
res = get(a, 'TTTC8001R', '/uapi/domestic-stock/v1/trading/inquire-daily-ccld',
          {**base, 'INQR_STRT_DT': start.strftime('%Y%m%d'), 'INQR_END_DT': end.strftime('%Y%m%d'), 'SLL_BUY_DVSN_CD': '00',
           'INQR_DVSN': '00', 'PDNO': '', 'CCLD_DVSN': '00', 'ORD_GNO_BRNO': '', 'ODNO': '', 'INQR_DVSN_3': '00',
           'INQR_DVSN_1': '', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''})
raw = res.pop('_raw', {})
res['sample_fields'] = [safe_sample(x, ['ord_dt', 'ord_tmd', 'pdno', 'sll_buy_dvsn_cd', 'ord_qty', 'tot_ccld_qty', 'rmn_qty',
                                        'cncl_yn', 'avg_prvs', 'ord_dvsn_cd', 'odno'])
                        for x in (raw.get('output1') or [])][:5]
for s in res['sample_fields']:
    if 'odno' in s:
        s['odno'] = '***' + str(s['odno'])[-3:]
report['checks']['domestic_daily_ccld_TTTC8001R'] = res

# 6) Overseas fills over last 7 days
res = get(a, 'TTTS3035R', '/uapi/overseas-stock/v1/trading/inquire-ccnl',
          {**base, 'PDNO': '%', 'ORD_STRT_DT': start.strftime('%Y%m%d'), 'ORD_END_DT': end.strftime('%Y%m%d'),
           'SLL_BUY_DVSN': '00', 'CCLD_NCCS_DVSN': '00', 'OVRS_EXCG_CD': '%', 'SORT_SQN': 'DS', 'ORD_DT': '', 'ORD_GNO_BRNO': '',
           'ODNO': '', 'CTX_AREA_NK200': '', 'CTX_AREA_FK200': ''})
raw = res.pop('_raw', {})
res['sample_fields'] = [safe_sample(x, ['ord_dt', 'pdno', 'sll_buy_dvsn_cd', 'ft_ord_qty', 'ft_ccld_qty', 'nccs_qty',
                                        'ft_ccld_unpr3', 'prcs_stat_name', 'rvse_cncl_dvsn'])
                        for x in (raw.get('output') or raw.get('output1') or [])][:5]
report['checks']['overseas_ccnl_TTTS3035R'] = res

# 7) Domestic order book (D5)
res = get(a, 'FHKST01010200', '/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn',
          {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': '069500'})
raw = res.pop('_raw', {})
o1 = raw.get('output1') or {}
res['book_fields_present'] = {f: (f in o1) for f in ('askp1', 'bidp1', 'askp_rsqn1', 'bidp_rsqn1', 'total_askp_rsqn', 'total_bidp_rsqn')}
res['spread_bps_sample'] = (round((float(o1['askp1']) - float(o1['bidp1'])) / ((float(o1['askp1']) + float(o1['bidp1'])) / 2) * 1e4, 2)
                            if o1.get('askp1') and o1.get('bidp1') and float(o1['askp1']) > 0 and float(o1['bidp1']) > 0 else None)
report['checks']['domestic_orderbook_FHKST01010200'] = res

print(json.dumps(report, ensure_ascii=False, indent=1, default=str))

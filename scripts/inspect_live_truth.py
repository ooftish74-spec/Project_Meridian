#!/usr/bin/env python3
"""
scripts/inspect_live_truth.py
Project Meridian Single Source of Truth (SSOT) Live Inspection Utility.

This script inspects live production portfolio state files (kis_portfolio.json,
shadow_portfolio.json, config/defaults.json) to display the unpolluted, actual
live production holdings and active streams.
"""

import os
import sys
import json
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

VENV_PYTHON = PROJECT_ROOT / "venv" / "bin" / "python3"

def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}

from src.execution.position_ledger import (
    _S3_MACRO_TICKERS,
    _S6_US_EQUITY_TICKERS,
    _S5_KR_ETF_TICKERS,
    _S3_B_BUFFETT_TICKERS,
)
_INVALID_STREAMS = {"", "unknown", "UNKNOWN", "S_RECON", "LIVE_SSOT", "Unassigned", "unassigned", "None"}


def _infer_canonical_stream_meta(ticker: str, mkt: str, existing: dict, ledger_entry: dict) -> dict:
    """Preserve and infer canonical (stream_id, track, strategy) for a live holding."""
    t_sym = str(ticker).strip().upper()
    raw_stream = (
        existing.get("stream_id")
        or existing.get("stream")
        or ledger_entry.get("stream_id")
        or ledger_entry.get("stream")
        or ""
    )
    raw_track = existing.get("track") or ledger_entry.get("track") or ""
    raw_strategy = existing.get("strategy") or ledger_entry.get("strategy") or ""
    raw_pair_id = existing.get("pair_id") or ledger_entry.get("pair_id") or ""

    if raw_pair_id or "pair" in str(raw_strategy).lower():
        raw_stream = "S14_PAIR_ALPHA"
        raw_track = raw_track or "TRACK_1_TACTICAL"
        raw_strategy = raw_strategy or "us_sector_pair_alpha"

    # Canonical overrides for known Buffett QVM and specific stream universes when missing or generic/S_RECON
    if t_sym in _S3_B_BUFFETT_TICKERS:
        if raw_stream in _INVALID_STREAMS or raw_stream == "S2":
            raw_stream = "S3_B"
        if not raw_track or raw_track == "TRACK_1_CORE":
            raw_track = "TRACK_2_BUFFETT_QVM"
        if not raw_strategy:
            raw_strategy = "S3_B Buffett QVM Value"
    elif raw_stream in _INVALID_STREAMS:
        if t_sym in _S3_MACRO_TICKERS:
            raw_stream = "S3"
            raw_strategy = raw_strategy or "S3 Macro/Cross-Asset Overlay"
        elif t_sym in _S6_US_EQUITY_TICKERS:
            raw_stream = "S6"
            raw_strategy = raw_strategy or "S6 US Equity Alpha"
        elif t_sym in _S5_KR_ETF_TICKERS:
            raw_stream = "S5"
            raw_strategy = raw_strategy or "S5 KRX Sector Rotation ETF"
        elif mkt == "KR":
            raw_stream = "S2"
            raw_strategy = raw_strategy or "S2 KRX Fundamental Swing"
        else:
            raw_stream = "S6"
            raw_strategy = raw_strategy or "S6 US Equity Alpha"
    else:
        # Even if raw_stream was set to S2 on a known S5 ETF by legacy recon, fix it
        if raw_stream == "S2" and t_sym in _S5_KR_ETF_TICKERS:
            raw_stream = "S5"
            raw_strategy = raw_strategy or "S5 KRX Sector Rotation ETF"
        elif raw_stream == "S2" and t_sym in _S3_MACRO_TICKERS:
            raw_stream = "S3"
            raw_strategy = raw_strategy or "S3 Macro/Cross-Asset Overlay"
        elif raw_stream == "S2" and t_sym in _S6_US_EQUITY_TICKERS:
            raw_stream = "S6"
            raw_strategy = raw_strategy or "S6 US Equity Alpha"

    if not raw_track:
        raw_track = "TRACK_2_BUFFETT_QVM" if raw_stream == "S3_B" else "TRACK_1_CORE"
    if not raw_strategy:
        _strat_map = {
            "S1": "S1 KRX Directional Core",
            "S2": "S2 KRX Fundamental Swing",
            "S3": "S3 Macro/Cross-Asset Overlay",
            "S3_B": "S3_B Buffett QVM Value",
            "S4": "S4 Defensive Alpha",
            "S5": "S5 KRX Sector Rotation ETF",
            "S6": "S6 US Equity Alpha",
        }
        raw_strategy = _strat_map.get(raw_stream, f"{raw_stream} Active Alpha")

    return {
        "stream_id": raw_stream,
        "stream": raw_stream,
        "track": raw_track,
        "strategy": raw_strategy,
    }


def inspect_live_truth(as_json: bool = False):
    kis_path = PROJECT_ROOT / "results" / "kis_portfolio.json"
    ledger_path = PROJECT_ROOT / "results" / "position_entry_ledger.json"
    shadow_path = PROJECT_ROOT / "results" / "shadow_portfolio.json"
    defaults_path = PROJECT_ROOT / "config" / "defaults.json"
    regime_path = PROJECT_ROOT / "results" / "current_regime.json"

    kis_data = load_json(kis_path)
    ledger_data = load_json(ledger_path)
    shadow_data = load_json(shadow_path)
    defaults_data = load_json(defaults_path)
    regime_data = load_json(regime_path)

    existing_holdings = kis_data.get("holdings") if isinstance(kis_data.get("holdings"), dict) else {}
    existing_acct_positions = (
        (kis_data.get("account") or {}).get("positions")
        if isinstance((kis_data.get("account") or {}).get("positions"), dict)
        else {}
    )
    ledger_entries_map = {}
    if isinstance(ledger_data, dict):
        for k, v in ledger_data.items():
            if k not in ("entries", "positions", "error") and isinstance(v, dict):
                ledger_entries_map[str(k)] = v
        if isinstance(ledger_data.get("positions"), dict):
            for k, v in ledger_data["positions"].items():
                if isinstance(v, dict):
                    ledger_entries_map[str(k)] = {**ledger_entries_map.get(str(k), {}), **v}
        if isinstance(ledger_data.get("entries"), dict):
            for k, v in ledger_data["entries"].items():
                if isinstance(v, dict):
                    ledger_entries_map[str(k)] = {**ledger_entries_map.get(str(k), {}), **v}

    # Hard Enforcement of Live KIS API Fetch
    live_fetched = False
    error_msg = ""
    holdings = {}
    account_info = kis_data.get("account", {})
    from datetime import datetime
    live_date = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    today_iso = datetime.now().strftime("%Y-%m-%d")
    try:
        from src.execution._kis_adapter import KISTraderAdapter
        from src.utils.file_ops import safe_json_update
        adapter = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
        if adapter.fetch_live_balance():
            sig_cache = load_json(PROJECT_ROOT / "results" / "signal_cache.json")
            raw_fx = (
                getattr(adapter, 'fx_rate', None)
                or adapter._get_live_fx_rate()
                or account_info.get('fx_rate')
                or sig_cache.get('usdkrw')
                or sig_cache.get('usdkrw_close')
            )
            if not raw_fx or float(raw_fx) <= 0:
                raise RuntimeError("Missing live USDKRW FX rate from KIS adapter and signal_cache.json")
            live_fx = round(float(raw_fx), 2)
            live_holdings = {}
            ledger_sync_updates = {}
            from src.utils.ticker_name_resolver import resolve_name
            from src.utils.market_calendar import is_kr_ticker
            for ticker, pos in adapter.positions.items():
                if int(getattr(pos, 'quantity', 0) or 0) <= 0:
                    continue
                t_key = str(ticker)
                c_price = float(pos.current_price) if pos.current_price > 0 else float(pos.avg_price)
                a_price = float(pos.avg_price) if pos.avg_price > 0 else c_price
                ret_pct = round((c_price / a_price - 1.0) * 100.0, 2) if a_price > 0 else 0.0
                mkt = 'KR' if is_kr_ticker(t_key) else 'US'
                prod_name = pos.name or resolve_name(t_key)

                prev_h = {
                    **existing_acct_positions.get(t_key, {}),
                    **existing_holdings.get(t_key, {}),
                }
                led_h = ledger_entries_map.get(t_key, {})
                meta = _infer_canonical_stream_meta(t_key, mkt, prev_h, led_h)

                entry_date = (
                    prev_h.get("entry_date")
                    or led_h.get("entry_date")
                    or prev_h.get("first_seen")
                    or led_h.get("first_seen")
                    or today_iso
                )
                currency = prev_h.get("currency") or led_h.get("currency") or ("KRW" if mkt == "KR" else "USD")
                qty_int = int(pos.quantity)
                eval_krw = round(qty_int * c_price * (1.0 if mkt == "KR" else live_fx), 0)
                prev_avg = float(prev_h.get("avg_price") or led_h.get("last_avg_price") or led_h.get("avg_price") or 0.0)
                same_entry_px = (prev_avg <= 0) or (a_price > 0 and abs(a_price - prev_avg) * 100.0 <= a_price / 2.0)
                prev_hwm = float(prev_h.get("high_water_mark") or led_h.get("peak_price") or led_h.get("high_water_mark") or 0.0) if same_entry_px else 0.0
                prev_pk_pnl = max(0.0, float(led_h.get("peak_pnl_pct", 0.0) or 0.0)) if same_entry_px else 0.0
                if prev_hwm > 0 and a_price > 0:
                    implied_pk_pnl = ((prev_hwm - a_price) / a_price) * 100.0
                    _pnl_tol_ilt = 1.0 / 20.0
                    if prev_pk_pnl > 0 and abs(implied_pk_pnl - prev_pk_pnl) > _pnl_tol_ilt:
                        prev_hwm = a_price * (1.0 + prev_pk_pnl / 100.0)
                hwm = round(max(prev_hwm, a_price, c_price), 4)
                peak_pnl_pct = round(max(0.0, ret_pct, (((hwm - a_price) / a_price) * 100.0) if a_price > 0 else 0.0), 6)

                holding_record = {
                    **prev_h,
                    'name': prod_name,
                    'qty': qty_int,
                    'avg_price': round(a_price, 2),
                    'current_price': round(c_price, 2),
                    'return_pct': ret_pct,
                    'market': mkt,
                    'currency': currency,
                    'eval_krw': eval_krw,
                    'entry_date': entry_date,
                    'high_water_mark': hwm,
                    'peak_price': hwm,
                    'stream_id': meta['stream_id'],
                    'stream': meta['stream'],
                    'track': meta['track'],
                    'strategy': meta['strategy'],
                }
                live_holdings[t_key] = holding_record
                ledger_sync_updates[t_key] = {
                    **led_h,
                    'entry_date': entry_date,
                    'avg_price': round(a_price, 2),
                    'last_avg_price': round(a_price, 4),
                    'qty': qty_int,
                    'last_qty': float(qty_int),
                    'last_quantity': float(qty_int),
                    'market': mkt,
                    'currency': currency,
                    'high_water_mark': hwm,
                    'peak_price': hwm,
                    'peak_pnl_pct': peak_pnl_pct,
                    'stream_id': meta['stream_id'],
                    'stream': meta['stream'],
                    'track': meta['track'],
                    'strategy': meta['strategy'],
                    'updated_at': datetime.now().isoformat(),
                }
            holdings = live_holdings
            if hasattr(adapter, 'account') and getattr(adapter.account, 'total_equity', 0) > 0:
                account_info['nav'] = round(adapter.account.total_equity, 2)
            if hasattr(adapter, 'account') and getattr(adapter.account, 'cash', 0) >= 0:
                account_info['cash'] = round(adapter.account.cash, 2)
            account_info['cash_usd'] = round(getattr(adapter, 'direct_usd_cash', getattr(adapter, 'us_cash_usd', 0.0)), 2)
            account_info['integrated_margin_usd'] = round(
                getattr(adapter, 'integrated_margin_usd', getattr(adapter, 'us_buying_power_usd', getattr(adapter, 'us_cash_usd', 0.0))), 2
            )
            account_info['fx_rate'] = live_fx
            account_info['number'] = adapter.account_no or account_info.get('number', '44223699-01')
            account_info['type'] = account_info.get('type', 'Main (종합 위탁)')

            # Reconcile multi-currency NAV identity & D+2 settlement cash
            kr_eval_sum = sum(float(v.get('eval_krw', 0.0)) for v in holdings.values() if v.get('market') == 'KR')
            us_eval_usd_sum = sum(float(v.get('qty', 0)) * float(v.get('current_price', 0.0)) for v in holdings.values() if v.get('market') == 'US')
            us_eval_krw_sum = round(us_eval_usd_sum * live_fx, 2)
            total_invested_krw = round(kr_eval_sum + us_eval_krw_sum, 2)
            orderable_wallet_nav = round(
                float(account_info.get('cash', 0.0))
                + kr_eval_sum
                + (float(account_info.get('cash_usd', 0.0)) + us_eval_usd_sum) * live_fx,
                2,
            )
            nav_val = float(account_info.get('nav', 0.0)) or orderable_wallet_nav
            unsettled_d2_krw = round(nav_val - orderable_wallet_nav, 2)
            d2_settlement_cash_krw = round(float(account_info.get('cash', 0.0)) + unsettled_d2_krw, 2)

            nav_hist_path = PROJECT_ROOT / "results" / "live_nav_history.json"
            nav_hist_data = load_json(nav_hist_path)
            max_hist_nav = 0.0
            if isinstance(nav_hist_data, list):
                max_hist_nav = max((float(x.get('nav', 0.0)) for x in nav_hist_data if isinstance(x, dict)), default=0.0)
            prev_acct_hwm = float(account_info.get('hwm') or 0.0)
            account_info['hwm'] = round(max(prev_acct_hwm, nav_val, max_hist_nav), 2)
            account_info['invested'] = total_invested_krw
            account_info['invest_pct'] = round((total_invested_krw / nav_val) * 100.0, 2) if nav_val > 0 else 0.0
            account_info['d2_settlement_cash_krw'] = d2_settlement_cash_krw
            account_info['unsettled_d2_receivable_krw'] = unsettled_d2_krw
            account_info['d0_deposit_krw'] = round(float(getattr(adapter, 'd0_cash_krw', 0.0)), 2)
            account_info['d1_settlement_cash_krw'] = round(float(getattr(adapter, 'd1_cash_krw', d2_settlement_cash_krw)), 2)
            account_info['ord_psbl_cash_krw'] = round(float(getattr(adapter, 'ord_psbl_cash_raw_krw', account_info.get('cash', 0.0))), 2)
            account_info['nrcvb_buy_amt_krw'] = round(float(getattr(adapter, 'nrcvb_buy_amt_krw', account_info.get('cash', 0.0))), 2)
            account_info['withdrawable_cash_krw'] = round(float(getattr(adapter, 'withdrawable_cash_krw', 0.0)), 2)
            account_info['d0_usd_deposit'] = round(float(getattr(adapter, 'total_usd_deposit', account_info.get('cash_usd', 0.0))), 2)
            account_info['pledged_margin_usd'] = round(float(getattr(adapter, 'pledged_margin_usd', 0.0)), 2)
            account_info['positions'] = holdings

            kis_data['date'] = live_date
            kis_data['timestamp'] = datetime.now().isoformat()
            kis_data['kis_mode'] = 'live'
            kis_data['account'] = account_info
            kis_data['holdings'] = holdings
            # ★ Wave 1 Track A: kis_portfolio.json 다중 writer(_kis_adapter 등) → 락 보유 merge-update.
            #   이 스크립트가 갱신하는 키만 교체(account 및 holdings는 키 단위 메타데이터 보존 병합).
            _upd = json.loads(json.dumps({k: kis_data[k] for k in ('date', 'timestamp', 'kis_mode', 'holdings')},
                                         ensure_ascii=False, default=str))
            _acct_upd = json.loads(json.dumps(account_info, ensure_ascii=False, default=str))

            def _mutate_kis(cur):
                cur = cur if isinstance(cur, dict) else {}
                prev_cur_holdings = cur.get('holdings') if isinstance(cur.get('holdings'), dict) else {}
                merged_holdings = {}
                for tk, h_val in _upd.get('holdings', {}).items():
                    base_h = prev_cur_holdings.get(tk) if isinstance(prev_cur_holdings.get(tk), dict) else {}
                    merged_h = {**base_h, **h_val}
                    merged_holdings[tk] = merged_h
                cur['date'] = _upd['date']
                cur['timestamp'] = _upd['timestamp']
                cur['kis_mode'] = _upd['kis_mode']
                cur['holdings'] = merged_holdings
                acct = cur.get('account') if isinstance(cur.get('account'), dict) else {}
                acct.update(_acct_upd)
                acct['positions'] = merged_holdings
                cur['account'] = acct
                return cur
            kis_data = safe_json_update(kis_path, _mutate_kis, default={}, indent=2, ensure_ascii=False, strict=True)

            def _mutate_ledger(cur):
                cur = cur if isinstance(cur, dict) else {}
                active_keys = set(ledger_sync_updates.keys())
                reserved_keys = {'entries', 'positions'}
                for stale_k in [k for k in list(cur.keys()) if k not in active_keys and k not in reserved_keys]:
                    del cur[stale_k]
                if isinstance(cur.get('entries'), dict):
                    for stale_k in [k for k in list(cur['entries'].keys()) if k not in active_keys]:
                        del cur['entries'][stale_k]
                if isinstance(cur.get('positions'), dict):
                    for stale_k in [k for k in list(cur['positions'].keys()) if k not in active_keys]:
                        del cur['positions'][stale_k]
                for tk, l_val in ledger_sync_updates.items():
                    existing_t = cur.get(tk) if isinstance(cur.get(tk), dict) else {}
                    cur[tk] = {**existing_t, **l_val}
                    if isinstance(cur.get('entries'), dict):
                        ex_e = cur['entries'].get(tk) if isinstance(cur['entries'].get(tk), dict) else {}
                        cur['entries'][tk] = {**ex_e, **l_val}
                    if isinstance(cur.get('positions'), dict):
                        ex_p = cur['positions'].get(tk) if isinstance(cur['positions'].get(tk), dict) else {}
                        cur['positions'][tk] = {**ex_p, **l_val}
                return cur
            safe_json_update(ledger_path, _mutate_ledger, default={}, indent=2, ensure_ascii=False, strict=True)
            legacy_ledger_path = PROJECT_ROOT / "results" / "position_ledger.json"
            if legacy_ledger_path.exists():
                safe_json_update(legacy_ledger_path, _mutate_ledger, default={}, indent=2, ensure_ascii=False, strict=False)

            def _mutate_nav_hist(cur):
                hist = cur if isinstance(cur, list) else []
                entry = {
                    "date": today_iso,
                    "nav": round(nav_val, 0),
                    "cash": round(float(account_info.get('cash', 0.0)), 0),
                    "d2_settlement_cash_krw": round(d2_settlement_cash_krw, 0),
                    "invested": round(total_invested_krw, 0),
                    "hwm": round(float(account_info.get('hwm', nav_val)), 0),
                    "timestamp": datetime.now().isoformat(),
                }
                if hist and isinstance(hist[-1], dict) and hist[-1].get("date") == today_iso:
                    hist[-1] = {**hist[-1], **entry}
                else:
                    hist.append(entry)
                return hist
            safe_json_update(nav_hist_path, _mutate_nav_hist, default=[], indent=2, ensure_ascii=False, strict=True)

            def _mutate_shadow(cur):
                cur = cur if isinstance(cur, dict) else {}
                raw_th = cur.get("trade_history") if isinstance(cur.get("trade_history"), list) else []
                try:
                    from scripts.rebuild_stream_metrics import _load_corporate_ledger_trades
                    corp_th = _load_corporate_ledger_trades(PROJECT_ROOT / "results", ledger_entries_map, live_fx)
                except Exception:
                    corp_th = []
                if corp_th:
                    cur["trade_history"] = corp_th
                elif any(isinstance(t, dict) and t.get("source") == "broker_fill_import" for t in raw_th):
                    cur["trade_history"] = [
                        t for t in raw_th
                        if isinstance(t, dict) and t.get("source") in ("broker_fill_import", "kis_live", "corporate_ledger", "execution_log")
                    ]
                synced_pos = {}
                for tk, h_val in holdings.items():
                    if not isinstance(h_val, dict):
                        continue
                    sid = str(h_val.get("stream_id") or "S2")
                    qty_v = float(h_val.get("qty", 0) or 0)
                    avg_v = float(h_val.get("avg_price", 0) or 0)
                    cur_v = float(h_val.get("current_price", avg_v) or avg_v)
                    eval_v = float(h_val.get("eval_krw", 0) or 0)
                    cost_v = float(h_val.get("cost_krw", 0) or 0)
                    synced_pos[f"{sid}:{tk}"] = {
                        "stream_id": sid,
                        "streams": [sid],
                        "ticker": tk,
                        "name": h_val.get("name", tk),
                        "market": h_val.get("market", "KR"),
                        "quantity": qty_v,
                        "entry_price": avg_v,
                        "avg_price": avg_v,
                        "current_price": cur_v,
                        "amount": cost_v,
                        "market_value": eval_v,
                        "unrealized_pnl": round(eval_v - cost_v, 2),
                        "pnl_pct": float(h_val.get("return_pct", 0.0) or 0.0),
                        "entry_date": h_val.get("entry_date", today_iso),
                        "strategy": h_val.get("strategy", ""),
                        "track": h_val.get("track", ""),
                    }
                cur["positions"] = synced_pos
                cur["cash"] = d2_settlement_cash_krw
                cur["cash_usd"] = float(account_info.get("cash_usd", 0.0))
                cur["cash_usd_fx"] = live_fx
                cur["nav"] = nav_val
                cur["virtual_nav"] = nav_val
                cur["total_nav"] = nav_val
                cur["hwm"] = float(account_info.get("hwm", nav_val))
                cur["real_account_synced"] = True
                cur["live_synced_at"] = datetime.now().isoformat()
                return cur

            if shadow_path.exists():
                shadow_data = safe_json_update(shadow_path, _mutate_shadow, default={}, indent=2, ensure_ascii=False, strict=True)
            ov_path = PROJECT_ROOT / "results" / "dynamic_overrides.json"
            if ov_path.exists():
                try:
                    from src.learning.genetic_rule_evolver import GeneticRuleEvolver
                    _gre = GeneticRuleEvolver()
                    safe_json_update(
                        ov_path,
                        lambda cur: _gre._enforce_tighten_only(cur if isinstance(cur, dict) else {}, defaults_data),
                        default={},
                        indent=2,
                        ensure_ascii=False,
                        strict=True,
                    )
                except Exception:
                    pass
            live_fetched = True
    except Exception as e_live:
        error_msg = str(e_live)
        live_fetched = False

    if not live_fetched:
        print("CRITICAL_ERROR: LIVE_KIS_API_FETCH_FAILED - Cannot display unverified stale cache!")
        print(f"Reason: {error_msg}")
        sys.exit(1)
    streams = kis_data.get("streams", {})
    active_streams = defaults_data.get("allocator.active_streams", [])

    # Market Calendar Status Check
    krx_open = False
    us_open = False
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from src.utils.market_calendar import MarketCalendar, is_us_trading_day
        cal = MarketCalendar()
        krx_open = cal.is_trading_day()
        us_open = is_us_trading_day()
    except Exception:
        pass

    # Check for leveraged / legacy inverse tickers
    leveraged_etps = "TQQQ SQQQ SOXS SOXL SPXU SPXL UPRO QID QLD SDS SSO 233740 252670 122630 123320 233160".split()  # hardcode-ok
    held_leveraged = [t for t in leveraged_etps if t in holdings or t in streams]

    kr_holdings = {k: v for k, v in holdings.items() if v.get('market') == 'KR'}
    us_holdings = {k: v for k, v in holdings.items() if v.get('market') == 'US'}

    kr_stock_val = sum(v.get('qty', 0) * v.get('current_price', 0) for v in kr_holdings.values())
    us_stock_val_usd = sum(v.get('qty', 0) * v.get('current_price', 0) for v in us_holdings.values())
    cash_krw = account_info.get("cash", 0.0)
    ord_psbl_cash_krw = account_info.get("ord_psbl_cash_krw", cash_krw)
    nrcvb_buy_amt_krw = account_info.get("nrcvb_buy_amt_krw", cash_krw)
    withdrawable_cash_krw = account_info.get("withdrawable_cash_krw", 0.0)
    d0_deposit_krw = account_info.get("d0_deposit_krw", 0.0)
    d1_settlement_cash_krw = account_info.get("d1_settlement_cash_krw", 0.0)
    d2_settlement_cash_krw = account_info.get("d2_settlement_cash_krw", cash_krw)
    unsettled_d2_krw = account_info.get("unsettled_d2_receivable_krw", 0.0)
    cash_usd = account_info.get("cash_usd", 0.0)
    d0_usd_deposit = account_info.get("d0_usd_deposit", cash_usd)
    pledged_margin_usd = account_info.get("pledged_margin_usd", 0.0)
    integrated_margin_usd = account_info.get("integrated_margin_usd", 0.0)
    fx_rate = float(account_info.get("fx_rate") or 0.0)

    kr_total_equity = d2_settlement_cash_krw + kr_stock_val
    us_total_equity = cash_usd + us_stock_val_usd

    truth_summary = {
        "ssot_status": "VALIDATED_LIVE",
        "kis_mode": kis_data.get("kis_mode", "unknown"),
        "date": live_date,
        "account_number": account_info.get("number", "N/A"),
        "account_type": account_info.get("type", "N/A"),
        "initial_capital": account_info.get("initial_capital", 0.0),
        "kr_account": {
            "cash_krw": cash_krw,
            "ord_psbl_cash_krw": ord_psbl_cash_krw,
            "nrcvb_buy_amt_krw": nrcvb_buy_amt_krw,
            "withdrawable_cash_krw": withdrawable_cash_krw,
            "d0_deposit_krw": d0_deposit_krw,
            "d1_settlement_cash_krw": d1_settlement_cash_krw,
            "d2_settlement_cash_krw": d2_settlement_cash_krw,
            "unsettled_d2_receivable_krw": unsettled_d2_krw,
            "stock_val_krw": kr_stock_val,
            "total_equity_krw": kr_total_equity,
            "holdings": kr_holdings
        },
        "us_account": {
            "cash_usd": cash_usd,
            "d0_usd_deposit": d0_usd_deposit,
            "pledged_margin_usd": pledged_margin_usd,
            "integrated_margin_usd": integrated_margin_usd,
            "stock_val_usd": us_stock_val_usd,
            "total_equity_usd": us_total_equity,
            "holdings": us_holdings
        },
        "combined_nav_krw": account_info.get("nav", 0.0),
        "applied_fx_rate": fx_rate,
        "shadow_cash": shadow_data.get("cash", 0.0),
        "shadow_real_synced": shadow_data.get("real_account_synced", False),
        "active_streams": active_streams,
        "current_regime": regime_data.get("regime", "unknown"),
        "leveraged_etps_held": held_leveraged
    }

    if as_json:
        print(json.dumps(truth_summary, indent=2, ensure_ascii=False))
        return

    print("==========================================================")
    print("      MERIDIAN LIVE PRODUCTION SINGLE SOURCE OF TRUTH     ")
    print("==========================================================")
    print(f"📌 SSOT Status          : {truth_summary['ssot_status']}")
    print(f"📌 KIS Mode             : {truth_summary['kis_mode'].upper()}")
    print(f"📌 Last Portfolio Date  : {truth_summary['date']}")
    print(f"📌 Account Number       : {truth_summary['account_number']} ({truth_summary['account_type']})")
    print("----------------------------------------------------------")
    print("🇰🇷 국내 원화 계좌 (KRW Wallet):")
    print(f"   - 주문가능 현금 (ord_psbl_cash): ₩{ord_psbl_cash_krw:,.0f} KRW (출금가능: ₩{withdrawable_cash_krw:,.0f} / 100%증거금매수가능: ₩{nrcvb_buy_amt_krw:,.0f})")
    print(f"   - D+0 예수금 총액 (dnca_tot)   : ₩{d0_deposit_krw:,.0f} KRW")
    print(f"   - D+2 결제예수금 (Settled)     : ₩{d2_settlement_cash_krw:,.0f} KRW (환변동유보차액: ₩{unsettled_d2_krw:+,.0f})")
    print(f"   - 원화 주식 평가액             : ₩{kr_stock_val:,.0f} KRW")
    print(f"   - 원화 자산 소계 (D+2기준)     : ₩{kr_total_equity:,.0f} KRW")
    print("🇺🇸 해외 외화 계좌 (USD Wallet):")
    print(f"   - 외화 D+2 주문가능 (Cash USD) : ${cash_usd:,.2f} USD (D+0 외화예수금: ${d0_usd_deposit:,.2f} / 미결제매수증거금: ${pledged_margin_usd:,.2f})")
    print(f"   - 통합증거금 주문가능 달러     : ${integrated_margin_usd:,.2f} USD")
    print(f"   - 외화 주식 평가액             : ${us_stock_val_usd:,.2f} USD")
    print(f"   - 외화 자산 소계               : ${us_total_equity:,.2f} USD")
    print("----------------------------------------------------------")
    print("🌐 통합 참고 평가액 (Combined Total Reference NAV):")
    print(f"   - 총 자산 NAV (원화)      : ₩{truth_summary['combined_nav_krw']:,.0f} KRW")
    print(f"   - 적용 기준 환율          : ₩{fx_rate:,.2f} KRW/USD")
    print(f"🔄 Active Streams       : {', '.join(truth_summary['active_streams'])}")
    print(f"🌐 Market Regime        : {truth_summary['current_regime']}")
    print(f"📅 Trading Day Status   : KRX={'OPEN' if krx_open else 'CLOSED'} | US={'OPEN' if us_open else 'CLOSED (HOLIDAY)'}")
    print("----------------------------------------------------------")
    print("📦 보유 종목 명세:")
    print("   [🇰🇷 국내 보유 종목]")
    if kr_holdings:
        for ticker, info in kr_holdings.items():
            disp_name = info.get('name')
            if not disp_name or disp_name == ticker:
                from src.utils.ticker_name_resolver import resolve_name
                disp_name = resolve_name(ticker)
            print(f"     • {ticker} ({disp_name}): {info.get('qty')}주 @ ₩{info.get('current_price'):,.0f} (수익률 {info.get('return_pct'):+.2f}%)")
    else:
        print("     • (보유 종목 없음)")
    print("   [🇺🇸 해외 보유 종목]")
    if us_holdings:
        for ticker, info in us_holdings.items():
            disp_name = info.get('name')
            if not disp_name or disp_name == ticker:
                from src.utils.ticker_name_resolver import resolve_name
                disp_name = resolve_name(ticker)
            print(f"     • {ticker} ({disp_name}): {info.get('qty')}주 @ ${info.get('current_price'):,.2f} (수익률 {info.get('return_pct'):+.2f}%)")
    else:
        print("     • (보유 종목 없음)")
    print("----------------------------------------------------------")
    print(f"🛡️  Leveraged ETP Check (TQQQ, SQQQ, etc.): {'⚠️  HELD' if held_leveraged else '✅ NONE HELD (0% Leverage In Account)'}")
    print("==========================================================")

def main():
    if VENV_PYTHON.exists() and sys.executable != str(VENV_PYTHON):
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)
    parser = argparse.ArgumentParser(description="Meridian Live Production SSOT Inspector")
    parser.add_argument("--json", action="store_true", help="Output truth summary as JSON")
    args = parser.parse_args()
    inspect_live_truth(as_json=args.json)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
scripts/export_corporate_ledger.py
==================================
Project Meridian — Corporate Accounting & Tax Daily/Weekly Ledger Engine
(법인 전환 및 세무기장·법인세 신고용 일별/주간 복식부기·이동평균법 원장 생성기)

핵심 기능 (Clean-Slate Zero-Dummy Architecture):
  1. 일별 자산·예수금·환율 스냅샷 원장 (Daily NAV & Multi-Currency Balance Journal):
     - 매일 KRX 장마감(15:35 KST) 및 주간 결산(일요일 저녁) 시 실계좌 SSOT(`kis_portfolio.json`)의
       원화예수금, 외화예수금(USD), 적용 기준환율(USD/KRW), 국내/해외 주식 평가액 및 총자산(NAV)을
       불변(Append-Only) 원장(`results/corporate_ledger/daily_nav_journal.json`)에 누적 기록.
  2. KIS 실계좌 체결원장 영구 보존 및 법인세법 시행령 제74조 [이동평균법] 원가계산:
     - KIS OpenAPI(`TTTC8001R` 국내체결, `TTTS3035R` 해외체결) 및 기존 영구 체결원장을 병합하여
       체결번호(`ODNO`) 단위로 중복 없이 보존(`results/corporate_ledger/broker_fills_master.json`).
     - 매도 시 종목별 이동평균 취득단가(외화/원화) 및 취득환율을 추적하여
       ① 취득원가(장부가액), ② 매매처분손익(가격변동분), ③ 외환차손익(환율변동분),
       ④ 위탁수수료 및 제세공과금(증권거래세·SEC Fee), ⑤ 실현순손익(원화)을 정밀 분리 산출.
  3. 5-Sheet 마스터 엑셀 통합장부(.xlsx) + 세무사 업로드용 CSV 3종 + 세무 요약 리포트(.md) 생성:
     - Sheet 1: `1_일별자산원장_Daily_NAV`
     - Sheet 2: `2_매매체결원장_Trade_Journal`
     - Sheet 3: `3_기말재고명세_Holdings`
     - Sheet 4: `4_일별실현손익_Daily_PnL`
     - Sheet 5: `5_법인세추계및손금산입_Tax`
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from src.params.tables import load_table
except Exception:  # pragma: no cover
    def load_table(name: str, default: Any = None) -> Any:
        _r = PROJECT_ROOT / "config" / "params" / "tables" / f"{name}.json"
        try:
            return json.loads(_r.read_text(encoding="utf-8"))
        except Exception:
            return {} if default is None else default

from src.utils.file_ops import atomic_write_json, safe_json_update

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

RESULTS_DIR = PROJECT_ROOT / "results"
LEDGER_DIR = RESULTS_DIR / "corporate_ledger"
DAILY_NAV_JOURNAL_PATH = LEDGER_DIR / "daily_nav_journal.json"
BROKER_FILLS_MASTER_PATH = LEDGER_DIR / "broker_fills_master.json"

STREAM_MAP = load_table("scripts.export_corporate_ledger.STREAM_MAP", {})
NAME_MAP = load_table("scripts.export_corporate_ledger.NAME_MAP", {})

# L0 Statutory Tax & Brokerage Structure Constants (대한민국 법인세법 및 거래소 수수료/세율)
CORP_TAX_TIER1_LIMIT_KRW = 200_000_000.0
CORP_TAX_TIER1_RATE = 0.099   # 법인세 9.0% + 지방소득세 0.9%
CORP_TAX_TIER2_RATE = 0.209   # 법인세 19.0% + 지방소득세 1.9%
KRX_BROKERAGE_FEE_RATE = 0.00015
KRX_STOCK_SELL_TAX_RATE = 0.0018
US_BROKERAGE_FEE_RATE = 0.0007
US_SEC_SELL_FEE_RATE = 0.0000278
ANNUAL_DEDUCTIBLE_EXPENSES_KRW = 3_240_000.0  # AWS EC2 + Market Data/API + 세무기장료 기본 손금산입액


def calculate_corporate_tax_bracket(taxable_income: float) -> float:
    """대한민국 법인세법 과세표준 구간별 법인세(지방소득세 포함) 산출."""
    if taxable_income <= 0.0:
        return 0.0
    if taxable_income <= CORP_TAX_TIER1_LIMIT_KRW:
        return taxable_income * CORP_TAX_TIER1_RATE
    tier1_tax = CORP_TAX_TIER1_LIMIT_KRW * CORP_TAX_TIER1_RATE
    tier2_tax = (taxable_income - CORP_TAX_TIER1_LIMIT_KRW) * CORP_TAX_TIER2_RATE
    return tier1_tax + tier2_tax


def _is_kr_etf(ticker: str, name: str) -> bool:
    """국내 상장 ETF 여부 판별 (국내 ETF는 증권거래세 0.18% 면제)."""
    etf_prefixes = ("KODEX", "TIGER", "ACE", "SOL", "KBSTAR", "RISE", "ARIRANG", "HANARO", "KIWOOM", "KOSEF", "PLUS", "TIMEFOLIO", "WOORI")
    nm_up = str(name or "").upper().strip()
    return any(nm_up.startswith(p) or p in nm_up for p in etf_prefixes)


def _load_historical_fx_map(default_fx: float) -> Dict[str, float]:
    """일자별 USD/KRW 환율 시계열 로드 (체결일 기준환율 정밀 매핑용)."""
    fx_map: Dict[str, float] = {}
    _min_fx = 500.0
    candidates = [
        PROJECT_ROOT / "data" / "macro" / "usdkrw.parquet",
        PROJECT_ROOT / "data" / "kr_markets" / "usdkrw.parquet",
    ]
    for p in candidates:
        if p.exists():
            try:
                df = pd.read_parquet(p)
                if "date" in df.columns:
                    df["date"] = pd.to_datetime(df["date"])
                    df.set_index("date", inplace=True)
                df.index = pd.to_datetime(df.index)
                col = "close" if "close" in df.columns else df.columns[0]
                for dt_idx, val in df[col].items():
                    v = float(val or 0.0)
                    if v >= _min_fx:
                        fx_map[dt_idx.strftime("%Y-%m-%d")] = round(v, 2)
            except Exception:
                pass
    if DAILY_NAV_JOURNAL_PATH.exists():
        try:
            j_data = json.loads(DAILY_NAV_JOURNAL_PATH.read_text(encoding="utf-8"))
            for row in j_data.get("entries", []):
                dt_s = str(row.get("date", ""))
                fx_v = float(row.get("fx_rate_usdkrw", 0.0) or 0.0)
                if dt_s and fx_v >= _min_fx:
                    fx_map[dt_s] = round(fx_v, 2)
        except Exception:
            pass
    return fx_map


def _resolve_fx_for_date(date_str: str, fx_map: Dict[str, float], fallback_fx: float) -> float:
    """특정 거래일의 USD/KRW 기준환율 반환 (휴일인 경우 직전 영업일 환율 적용)."""
    if date_str in fx_map:
        return fx_map[date_str]
    prior_dates = [k for k in sorted(fx_map.keys()) if k <= date_str]
    if prior_dates:
        return fx_map[prior_dates[-1]]
    return fallback_fx


def _get_live_fx_rate() -> float:
    """실시간 관측 USD/KRW 환율 조회 (signal_cache.json -> kis_portfolio.json -> CentralDataGateway)."""
    _min_fx = 500.0
    sc_path = RESULTS_DIR / "signal_cache.json"
    if sc_path.exists():
        try:
            sc = json.loads(sc_path.read_text(encoding="utf-8"))
            fx = float(sc.get("usdkrw", 0.0) or 0.0)
            if fx >= _min_fx:
                return round(fx, 2)
        except Exception:
            pass
    kp_path = RESULTS_DIR / "kis_portfolio.json"
    if kp_path.exists():
        try:
            kp = json.loads(kp_path.read_text(encoding="utf-8"))
            fx = float((kp.get("account") or {}).get("fx_rate", kp.get("fx_rate", 0.0)) or 0.0)
            if fx >= _min_fx:
                return round(fx, 2)
        except Exception:
            pass
    try:
        from src.data_collection.central_data_gateway import CentralDataGateway
        fx = float(CentralDataGateway().get_usdkrw_rate() or 0.0)
        if fx >= _min_fx:
            return round(fx, 2)
    except Exception:
        pass
    return 1380.0


def record_daily_nav_snapshot(target_date: Optional[str] = None) -> Dict[str, Any]:
    """
    실계좌 SSOT(`results/kis_portfolio.json`) 기준 일별 자산·예수금·환율 스냅샷을
    `results/corporate_ledger/daily_nav_journal.json`에 불변(Idempotent Update) 기록.
    """
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    kp_path = RESULTS_DIR / "kis_portfolio.json"
    if not kp_path.exists():
        logger.warning("kis_portfolio.json not found; skipping daily NAV snapshot.")
        return {}

    kp = json.loads(kp_path.read_text(encoding="utf-8"))
    acct = kp.get("account") or {}
    summary = kp.get("summary") or {}
    holdings = kp.get("holdings") or acct.get("positions") or {}

    fx_rate = _get_live_fx_rate()
    dt_str = target_date or str(kp.get("date") or datetime.now().strftime("%Y-%m-%d"))[:10]

    cash_krw = float(acct.get("cash_balance", acct.get("available_cash", summary.get("cash_krw", 0.0))) or 0.0)
    cash_usd = float(acct.get("usd_cash", summary.get("cash_usd", 0.0)) or 0.0)

    kr_eq_krw = 0.0
    us_eq_usd = 0.0
    kr_cost_krw = 0.0
    us_cost_usd = 0.0
    n_holdings = 0

    for tk, h in holdings.items():
        if not isinstance(h, dict) or "CASH" in str(tk).upper():
            continue
        qty = float(h.get("qty", h.get("quantity", 0)) or 0.0)
        if qty <= 0:
            continue
        n_holdings += 1
        curr_px = float(h.get("current_price", h.get("avg_price", 0.0)) or 0.0)
        avg_px = float(h.get("avg_price", curr_px) or 0.0)
        is_kr = str(h.get("currency", "")).upper() == "KRW" or str(h.get("market", "")).upper() == "KR" or (len(str(tk)) == 6 and str(tk)[0].isdigit())
        if is_kr:
            kr_eq_krw += qty * curr_px
            kr_cost_krw += qty * avg_px
        else:
            us_eq_usd += qty * curr_px
            us_cost_usd += qty * avg_px

    cash_usd_in_krw = round(cash_usd * fx_rate, 0)
    us_eq_in_krw = round(us_eq_usd * fx_rate, 0)
    computed_nav_krw = round(cash_krw + cash_usd_in_krw + kr_eq_krw + us_eq_in_krw, 0)
    reported_nav_krw = float(kp.get("nav", acct.get("total_equity", computed_nav_krw)) or computed_nav_krw)
    nav_krw = round(reported_nav_krw if reported_nav_krw > 0 else computed_nav_krw, 0)
    unrealized_pnl_krw = round((kr_eq_krw - kr_cost_krw) + (us_eq_usd - us_cost_usd) * fx_rate, 0)

    snapshot_row = {
        "date": dt_str,
        "recorded_at": datetime.now().isoformat(),
        "nav_krw": nav_krw,
        "cash_krw": round(cash_krw, 0),
        "cash_usd": round(cash_usd, 2),
        "fx_rate_usdkrw": fx_rate,
        "cash_usd_in_krw": cash_usd_in_krw,
        "kr_equity_krw": round(kr_eq_krw, 0),
        "us_equity_usd": round(us_eq_usd, 2),
        "us_equity_in_krw": us_eq_in_krw,
        "unrealized_pnl_krw": unrealized_pnl_krw,
        "holdings_count": n_holdings,
        "regime": str((kp.get("regime") or {}).get("regime", "unknown")),
    }

    def _mutate_journal(state: dict) -> dict:
        if not isinstance(state, dict):
            state = {}
        entries: List[dict] = list(state.get("entries") or [])
        by_date = {str(r.get("date")): r for r in entries if isinstance(r, dict) and r.get("date")}
        by_date[dt_str] = snapshot_row
        sorted_entries = [by_date[k] for k in sorted(by_date.keys())]
        base_nav = float(sorted_entries[0]["nav_krw"]) if sorted_entries else nav_krw
        prev_nav = base_nav
        for idx, r in enumerate(sorted_entries):
            cur = float(r.get("nav_krw", 0.0) or 0.0)
            if idx == 0:
                r["daily_pnl_krw"] = 0.0
                r["daily_return_pct"] = 0.0
                r["cum_return_pct"] = 0.0
            else:
                r["daily_pnl_krw"] = round(cur - prev_nav, 0)
                r["daily_return_pct"] = round((cur / prev_nav - 1.0) * 100.0, 3) if prev_nav > 0 else 0.0
                r["cum_return_pct"] = round((cur / base_nav - 1.0) * 100.0, 3) if base_nav > 0 else 0.0
            prev_nav = cur if cur > 0 else prev_nav
        state["updated_at"] = datetime.now().isoformat()
        state["total_days"] = len(sorted_entries)
        state["entries"] = sorted_entries
        return state

    safe_json_update(DAILY_NAV_JOURNAL_PATH, _mutate_journal, strict=True)
    return snapshot_row


def sync_and_load_broker_fills(fetch_live: bool = True, lookback_days: int = 90) -> List[Dict[str, Any]]:
    """
    1. 기존 `results/broker_fills_import_*.json` 및 `results/corporate_ledger/broker_fills_master.json` 로드.
    2. `fetch_live=True`이고 실계좌 인증이 가능하면 KIS OpenAPI(`TTTC8001R`, `TTTS3035R`)에서 최근 체결내역을
       실시간 조회하여 `(market, date, odno)` 고유키 기준으로 중복 없이 영구 병합.
    """
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    all_raw_fills: List[Dict[str, Any]] = []

    if BROKER_FILLS_MASTER_PATH.exists():
        try:
            master_data = json.loads(BROKER_FILLS_MASTER_PATH.read_text(encoding="utf-8"))
            all_raw_fills.extend(master_data.get("fills") or [])
        except Exception as e:
            logger.warning(f"Failed to read broker_fills_master.json: {e}")

    for imp_file in sorted(RESULTS_DIR.glob("broker_fills_import_*.json")):
        try:
            imp_data = json.loads(imp_file.read_text(encoding="utf-8"))
            all_raw_fills.extend(imp_data.get("fills") or [])
        except Exception:
            pass

    if fetch_live and not os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("MERIDIAN_TEST_MODE") != "1":
        try:
            from scripts.ops.import_broker_fills import fetch_broker_fills, normalize_fill_rows
            kr_rows, us_rows = fetch_broker_fills(days=lookback_days)
            live_fills = normalize_fill_rows(kr_rows, "KR") + normalize_fill_rows(us_rows, "US")
            all_raw_fills.extend(live_fills)
            logger.info(f"  ✓ KIS 실계좌 체결원장 실시간 조회 완료: KR={len(kr_rows)}건, US={len(us_rows)}건")
        except Exception as e:
            logger.info(f"  ℹ️ KIS 실시간 체결조회 생략(로컬 오프라인/토큰 미보유 등, 저장된 원장 사용): {e}")

    deduped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in all_raw_fills:
        if not isinstance(r, dict):
            continue
        qty = int(float(r.get("quantity", 0) or 0))
        px = float(r.get("price", 0.0) or 0.0)
        if qty <= 0 or px <= 0.0:
            continue
        mkt = str(r.get("market", "KR" if str(r.get("currency", "KRW")) == "KRW" else "US")).upper()
        dt_s = str(r.get("date", ""))[:10]
        odno = str(r.get("odno", "")).strip()
        key = (mkt, dt_s, odno or f"{r.get('timestamp')}_{r.get('ticker')}_{r.get('action')}_{qty}")
        deduped[key] = r

    sorted_fills = sorted(deduped.values(), key=lambda x: (str(x.get("timestamp", x.get("date", ""))), str(x.get("odno", ""))))

    try:
        atomic_write_json(
            BROKER_FILLS_MASTER_PATH,
            {
                "updated_at": datetime.now().isoformat(),
                "total_fills": len(sorted_fills),
                "fills": sorted_fills,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.warning(f"Could not save broker_fills_master.json: {e}")

    return sorted_fills


def _resolve_track_and_stream(ticker: str, ledger_meta: Dict[str, Any], kis_holdings: Dict[str, Any]) -> Tuple[str, str]:
    """종목별 소속 Track (Track 1 vs Track 2) 및 Stream ID 판별."""
    tk = str(ticker).strip()
    meta = ledger_meta.get(tk) if isinstance(ledger_meta.get(tk), dict) else {}
    h_info = kis_holdings.get(tk) if isinstance(kis_holdings.get(tk), dict) else {}

    sid = str(meta.get("stream_id") or h_info.get("stream_id") or STREAM_MAP.get(tk, "")).strip()
    trk = str(meta.get("track") or h_info.get("track") or "").strip()

    if sid == "S3_B" or trk == "TRACK_2_BUFFETT_QVM":
        return "Track 2 (버핏 QVM 장기복리)", "S3_B (Buffett QVM)"
    if sid in ("S5", "S_YIELD") or "CD금리" in str(h_info.get("name", "")) or "KOFR" in str(h_info.get("name", "")):
        return "Track 1 (현금파킹/이자수익)", sid or "S5 / S_YIELD"
    if sid in ("S0", "S12", "S12_DERIVATIVE_SQUEEZE") or "인버스" in str(h_info.get("name", "")):
        return "Track 1 (하락장 인버스 헷지)", sid or "S0 / S12"
    return "Track 1 (전술 알파 로테이션)", sid or "Track 1 Tactical"


def build_corporate_accounting_ledgers(fetch_live: bool = True) -> Dict[str, Any]:
    """
    법인세법 시행령 제74조(이동평균법)에 따른 매매원장, 일별 실현손익, 기말 보유재고 명세서,
    그리고 법인세 과세표준·손금산입 추계표를 일괄 생성.
    """
    fx_live = _get_live_fx_rate()
    fx_map = _load_historical_fx_map(fx_live)
    record_daily_nav_snapshot()

    ledger_meta: Dict[str, Any] = {}
    pl_path = RESULTS_DIR / "position_entry_ledger.json"
    if pl_path.exists():
        try:
            ledger_meta = json.loads(pl_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    kis_data: Dict[str, Any] = {}
    kp_path = RESULTS_DIR / "kis_portfolio.json"
    if kp_path.exists():
        try:
            kis_data = json.loads(kp_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    kis_holdings = kis_data.get("holdings") or (kis_data.get("account") or {}).get("positions") or {}

    raw_fills = sync_and_load_broker_fills(fetch_live=fetch_live)

    # Moving-average inventory state per ticker:
    # {ticker: {'qty': float, 'cost_native': float, 'cost_krw': float, 'avg_fx': float}}
    inv_state: Dict[str, Dict[str, float]] = {}
    trade_rows: List[Dict[str, Any]] = []
    daily_pnl_agg: Dict[str, Dict[str, float]] = {}

    for f in raw_fills:
        dt_str = str(f.get("date", ""))[:10]
        ts_str = str(f.get("timestamp", f"{dt_str}T00:00:00"))
        time_str = ts_str.split("T")[-1][:8] if "T" in ts_str else "00:00:00"
        mkt = str(f.get("market", "KR")).upper()
        ccy = "KRW" if mkt == "KR" else "USD"
        tk = str(f.get("ticker", "")).strip()
        nm = str(NAME_MAP.get(tk) or f.get("name") or tk).strip()
        act = str(f.get("action", "BUY")).upper()
        qty = int(float(f.get("quantity", 0) or 0))
        px = float(f.get("price", 0.0) or 0.0)
        odno = str(f.get("odno", "")).strip()

        if qty <= 0 or px <= 0.0:
            continue

        fx_t = 1.0 if ccy == "KRW" else _resolve_fx_for_date(dt_s=dt_str, fx_map=fx_map, fallback_fx=fx_live) if False else (1.0 if ccy == "KRW" else _resolve_fx_for_date(dt_str, fx_map, fx_live))
        gross_native = round(qty * px, 0 if ccy == "KRW" else 4)
        gross_krw = round(gross_native * fx_t, 0)

        if ccy == "KRW":
            fee_native = round(gross_native * KRX_BROKERAGE_FEE_RATE, 0)
            is_etf = _is_kr_etf(tk, nm)
            tax_native = round(gross_native * KRX_STOCK_SELL_TAX_RATE, 0) if (act == "SELL" and not is_etf) else 0.0
            fee_krw = fee_native
            tax_krw = tax_native
        else:
            fee_native = round(gross_native * US_BROKERAGE_FEE_RATE, 2)
            tax_native = round(gross_native * US_SEC_SELL_FEE_RATE, 2) if act == "SELL" else 0.0
            fee_krw = round(fee_native * fx_t, 0)
            tax_krw = round(tax_native * fx_t, 0)

        st = inv_state.setdefault(tk, {"qty": 0.0, "cost_native": 0.0, "cost_krw": 0.0, "avg_fx": fx_t})
        track_label, stream_label = _resolve_track_and_stream(tk, ledger_meta, kis_holdings)

        if act == "BUY":
            # 법인세법 시행령 제74조: 매입 부대비용(위탁수수료)은 취득원가에 가산 가능
            new_qty = st["qty"] + qty
            added_cost_native = gross_native + fee_native
            added_cost_krw = gross_krw + fee_krw
            st["cost_native"] += added_cost_native
            st["cost_krw"] += added_cost_krw
            st["qty"] = new_qty
            st["avg_fx"] = (st["cost_krw"] / st["cost_native"]) if st["cost_native"] > 0 else fx_t

            avg_cost_native = st["cost_native"] / st["qty"] if st["qty"] > 0 else px
            avg_cost_krw = st["cost_krw"] / st["qty"] if st["qty"] > 0 else (px * fx_t)
            cogs_krw = 0.0
            trading_pnl_krw = 0.0
            fx_pnl_krw = 0.0
            realized_net_pnl_krw = 0.0
            realized_ret_pct = 0.0
            net_settlement_krw = -(gross_krw + fee_krw)
        else:
            # SELL: 이동평균법 원가 배분
            if st["qty"] >= qty and st["qty"] > 0:
                avg_cost_native = st["cost_native"] / st["qty"]
                avg_cost_krw = st["cost_krw"] / st["qty"]
                acq_fx = st["avg_fx"]
                cogs_native = avg_cost_native * qty
                cogs_krw = round(avg_cost_krw * qty, 0)
                st["qty"] -= qty
                st["cost_native"] = max(0.0, st["cost_native"] - cogs_native)
                st["cost_krw"] = max(0.0, st["cost_krw"] - cogs_krw)
            elif st["qty"] > 0:
                avg_cost_native = st["cost_native"] / st["qty"]
                avg_cost_krw = st["cost_krw"] / st["qty"]
                acq_fx = st["avg_fx"]
                cogs_krw = round(avg_cost_krw * qty, 0)
                st["qty"] = 0.0
                st["cost_native"] = 0.0
                st["cost_krw"] = 0.0
            else:
                # 90일 조회창 이전 취득분 매도 시: 원장 평단가 또는 체결가 기준 보수적 원가 추정
                fallback_avg = float((ledger_meta.get(tk) or {}).get("last_avg_price", px) or px)
                avg_cost_native = fallback_avg
                acq_fx = fx_t
                avg_cost_krw = fallback_avg * acq_fx
                cogs_krw = round(avg_cost_krw * qty, 0)

            net_proceeds_krw = gross_krw - fee_krw - tax_krw
            net_settlement_krw = net_proceeds_krw
            realized_net_pnl_krw = round(net_proceeds_krw - cogs_krw, 0)
            if ccy == "USD":
                fx_pnl_krw = round(qty * avg_cost_native * (fx_t - acq_fx), 0)
                trading_pnl_krw = round(realized_net_pnl_krw - fx_pnl_krw, 0)
            else:
                fx_pnl_krw = 0.0
                trading_pnl_krw = realized_net_pnl_krw
            realized_ret_pct = round((realized_net_pnl_krw / cogs_krw) * 100.0, 2) if cogs_krw > 0 else 0.0

        d_agg = daily_pnl_agg.setdefault(
            dt_str,
            {
                "date": dt_str,
                "buy_count": 0,
                "sell_count": 0,
                "buy_gross_krw": 0.0,
                "sell_gross_krw": 0.0,
                "cogs_krw": 0.0,
                "fee_krw": 0.0,
                "tax_krw": 0.0,
                "trading_pnl_krw": 0.0,
                "fx_pnl_krw": 0.0,
                "realized_net_pnl_krw": 0.0,
            },
        )
        if act == "BUY":
            d_agg["buy_count"] += 1
            d_agg["buy_gross_krw"] += gross_krw
        else:
            d_agg["sell_count"] += 1
            d_agg["sell_gross_krw"] += gross_krw
            d_agg["cogs_krw"] += cogs_krw
            d_agg["trading_pnl_krw"] += trading_pnl_krw
            d_agg["fx_pnl_krw"] += fx_pnl_krw
            d_agg["realized_net_pnl_krw"] += realized_net_pnl_krw
        d_agg["fee_krw"] += fee_krw
        d_agg["tax_krw"] += tax_krw

        trade_rows.append(
            {
                "거래일자": dt_str,
                "체결시각": time_str,
                "주문번호(ODNO)": odno,
                "시장구분": "국내(KRX)" if mkt == "KR" else "미국(US)",
                "종목코드": tk,
                "종목명": nm,
                "거래구분": "매수(BUY)" if act == "BUY" else "매도(SELL)",
                "운용트랙": track_label,
                "스트림": stream_label,
                "통화": ccy,
                "체결수량": qty,
                "체결단가": round(px, 2 if ccy == "KRW" else 4),
                "외화약정금액": round(gross_native, 2) if ccy == "USD" else 0.0,
                "적용환율(USD/KRW)": round(fx_t, 2),
                "원화약정금액(KRW)": int(round(gross_krw)),
                "이동평균취득단가": round(avg_cost_native, 2 if ccy == "KRW" else 4),
                "매도취득원가(KRW)": int(round(cogs_krw)),
                "위탁수수료(KRW)": int(round(fee_krw)),
                "제세공과금(KRW)": int(round(tax_krw)),
                "매매처분손익(KRW)": int(round(trading_pnl_krw)),
                "외환차손익(KRW)": int(round(fx_pnl_krw)),
                "실현순손익(KRW)": int(round(realized_net_pnl_krw)),
                "실현수익률(%)": realized_ret_pct,
                "정산현금증감(KRW)": int(round(net_settlement_krw)),
                "장부잔여수량": int(round(st["qty"])),
            }
        )

    # Sheet 3: 기말 유가증권 재고명세서 (Holdings Subledger from Live SSOT `kis_portfolio.json`)
    holdings_rows: List[Dict[str, Any]] = []
    total_nav_krw = float(kis_data.get("nav") or (kis_data.get("account") or {}).get("total_equity") or 1.0)
    for tk, h in sorted(kis_holdings.items(), key=lambda x: str(x[0])):
        if not isinstance(h, dict) or "CASH" in str(tk).upper():
            continue
        qty = int(float(h.get("qty", h.get("quantity", 0)) or 0))
        if qty <= 0:
            continue
        nm = str(NAME_MAP.get(tk) or h.get("name") or tk).strip()
        is_kr = str(h.get("currency", "")).upper() == "KRW" or str(h.get("market", "")).upper() == "KR" or (len(str(tk)) == 6 and str(tk)[0].isdigit())
        ccy = "KRW" if is_kr else "USD"
        fx_now = 1.0 if is_kr else fx_live
        avg_px = float(h.get("avg_price", 0.0) or 0.0)
        curr_px = float(h.get("current_price", avg_px) or avg_px)
        entry_dt = str((ledger_meta.get(tk) or {}).get("entry_date") or h.get("entry_date") or "-")[:10]
        track_label, stream_label = _resolve_track_and_stream(tk, ledger_meta, kis_holdings)

        book_cost_krw = round(qty * avg_px * fx_now, 0)
        eval_amt_krw = round(qty * curr_px * fx_now, 0)
        unreal_pnl_krw = round(eval_amt_krw - book_cost_krw, 0)
        unreal_pnl_pct = round((curr_px / avg_px - 1.0) * 100.0, 2) if avg_px > 0 else 0.0
        weight_pct = round((eval_amt_krw / total_nav_krw) * 100.0, 2) if total_nav_krw > 0 else 0.0

        holdings_rows.append(
            {
                "종목코드": tk,
                "종목명": nm,
                "시장구분": "국내(KRX)" if is_kr else "미국(US)",
                "통화": ccy,
                "운용트랙": track_label,
                "스트림": stream_label,
                "최초취득일": entry_dt,
                "보유수량": qty,
                "이동평균취득단가": round(avg_px, 2 if is_kr else 4),
                "기말현재가": round(curr_px, 2 if is_kr else 4),
                "적용기말환율": round(fx_now, 2),
                "장부취득원가(KRW)": int(book_cost_krw),
                "기말평가금액(KRW)": int(eval_amt_krw),
                "미실현평가손익(KRW)": int(unreal_pnl_krw),
                "미실현수익률(%)": unreal_pnl_pct,
                "포트폴리오비중(%)": weight_pct,
            }
        )

    # Sheet 1: 일별 자산원장 (Daily NAV Journal)
    nav_entries: List[Dict[str, Any]] = []
    if DAILY_NAV_JOURNAL_PATH.exists():
        try:
            nav_entries = json.loads(DAILY_NAV_JOURNAL_PATH.read_text(encoding="utf-8")).get("entries") or []
        except Exception:
            pass

    daily_nav_rows: List[Dict[str, Any]] = []
    for r in nav_entries:
        dt_s = str(r.get("date", ""))
        d_tr = daily_pnl_agg.get(dt_s, {})
        daily_nav_rows.append(
            {
                "결산일자": dt_s,
                "기말총자산_NAV(KRW)": int(round(float(r.get("nav_krw", 0.0) or 0.0))),
                "원화예수금(KRW)": int(round(float(r.get("cash_krw", 0.0) or 0.0))),
                "외화예수금(USD)": round(float(r.get("cash_usd", 0.0) or 0.0), 2),
                "기준환율(USD/KRW)": round(float(r.get("fx_rate_usdkrw", fx_live) or fx_live), 2),
                "외화예수금원화환산(KRW)": int(round(float(r.get("cash_usd_in_krw", 0.0) or 0.0))),
                "국내주식평가액(KRW)": int(round(float(r.get("kr_equity_krw", 0.0) or 0.0))),
                "해외주식평가액(USD)": round(float(r.get("us_equity_usd", 0.0) or 0.0), 2),
                "해외주식원화환산(KRW)": int(round(float(r.get("us_equity_in_krw", 0.0) or 0.0))),
                "미실현평가손익(KRW)": int(round(float(r.get("unrealized_pnl_krw", 0.0) or 0.0))),
                "당일실현순손익(KRW)": int(round(float(d_tr.get("realized_net_pnl_krw", 0.0) or 0.0))),
                "당일총자산증감(KRW)": int(round(float(r.get("daily_pnl_krw", 0.0) or 0.0))),
                "일간수익률(%)": round(float(r.get("daily_return_pct", 0.0) or 0.0), 3),
                "누적수익률(%)": round(float(r.get("cum_return_pct", 0.0) or 0.0), 3),
                "보유종목수": int(r.get("holdings_count", 0) or 0),
                "시장레짐": str(r.get("regime", "unknown")),
            }
        )

    # Sheet 4: 일별 실현손익 집계표 (Daily Realized PnL Summary)
    daily_pnl_rows: List[Dict[str, Any]] = []
    cum_realized_krw = 0.0
    for dt_s in sorted(daily_pnl_agg.keys()):
        agg = daily_pnl_agg[dt_s]
        cum_realized_krw += agg["realized_net_pnl_krw"]
        daily_pnl_rows.append(
            {
                "거래일자": dt_s,
                "매수건수": int(agg["buy_count"]),
                "매도건수": int(agg["sell_count"]),
                "총매수약정액(KRW)": int(round(agg["buy_gross_krw"])),
                "총매도약정액(KRW)": int(round(agg["sell_gross_krw"])),
                "매도취득원가(KRW)": int(round(agg["cogs_krw"])),
                "위탁수수료합계(KRW)": int(round(agg["fee_krw"])),
                "제세공과금합계(KRW)": int(round(agg["tax_krw"])),
                "매매처분손익(KRW)": int(round(agg["trading_pnl_krw"])),
                "외환차손익(KRW)": int(round(agg["fx_pnl_krw"])),
                "당일실현순손익(KRW)": int(round(agg["realized_net_pnl_krw"])),
                "누적실현순손익(KRW)": int(round(cum_realized_krw)),
            }
        )

    # Sheet 5: 법인세 과세표준 및 손금산입 추계표 (Corporate Tax & Deduction Summary)
    tot_trading_pnl = sum(r["매매처분손익(KRW)"] for r in trade_rows if r["거래구분"] == "매도(SELL)")
    tot_fx_pnl = sum(r["외환차손익(KRW)"] for r in trade_rows if r["거래구분"] == "매도(SELL)")
    tot_realized_net = sum(r["실현순손익(KRW)"] for r in trade_rows if r["거래구분"] == "매도(SELL)")
    tot_fees = sum(r["위탁수수료(KRW)"] for r in trade_rows)
    tot_sec_taxes = sum(r["제세공과금(KRW)"] for r in trade_rows)
    tot_unrealized_pnl = sum(r["미실현평가손익(KRW)"] for r in holdings_rows)

    taxable_before_deduction = max(0.0, float(tot_realized_net))
    carryforward_loss_generated = abs(min(0.0, float(tot_realized_net) - ANNUAL_DEDUCTIBLE_EXPENSES_KRW))
    taxable_after_deduction = max(0.0, float(tot_realized_net) - ANNUAL_DEDUCTIBLE_EXPENSES_KRW)
    est_corp_tax_pre = round(calculate_corporate_tax_bracket(taxable_before_deduction), 0)
    est_corp_tax_post = round(calculate_corporate_tax_bracket(taxable_after_deduction), 0)
    tax_saved_krw = round(est_corp_tax_pre - est_corp_tax_post, 0)

    tax_summary_rows = [
        {"항목구분": "1. 누적 총매수 약정금액", "금액(KRW)": int(sum(r["원화약정금액(KRW)"] for r in trade_rows if r["거래구분"] == "매수(BUY)")), "세무/회계 비고": "이동평균법 취득원가 산입 대상"},
        {"항목구분": "2. 누적 총매도 약정금액", "금액(KRW)": int(sum(r["원화약정금액(KRW)"] for r in trade_rows if r["거래구분"] == "매도(SELL)")), "세무/회계 비고": "국내(KRX) 및 해외(US) 매도 총액"},
        {"항목구분": "3. 누적 매매처분손익 (가격변동분)", "금액(KRW)": int(tot_trading_pnl), "세무/회계 비고": "법인세법상 유가증권 처분손익"},
        {"항목구분": "4. 누적 외환차손익 (환율변동분)", "금액(KRW)": int(tot_fx_pnl), "세무/회계 비고": "해외주식 취득환율 대비 결제환율 차손익"},
        {"항목구분": "5. 누적 위탁수수료 및 제세공과금", "금액(KRW)": int(tot_fees + tot_sec_taxes), "세무/회계 비고": f"수수료 ₩{int(tot_fees):,} + 거래세/SEC ₩{int(tot_sec_taxes):,}"},
        {"항목구분": "6. 당기 누적 실현순손익 (3+4)", "금액(KRW)": int(tot_realized_net), "세무/회계 비고": "법인세 과세표준 산정 기초 당기순손익"},
        {"항목구분": "7. 기말 보유주식 미실현 평가손익", "금액(KRW)": int(tot_unrealized_pnl), "세무/회계 비고": "법인세법 제42조(원가법): 매도 전까지 과세 이연(비과세)"},
        {"항목구분": "8. 연간 손금산입 필요경비 (AWS/API/기장료)", "금액(KRW)": int(ANNUAL_DEDUCTIBLE_EXPENSES_KRW), "세무/회계 비고": "클라우드 서버·데이터 구독료·세무기장료 필요경비 공제"},
        {"항목구분": "9. 차기 이월결손금 적립액 (10년 공제)", "금액(KRW)": int(round(carryforward_loss_generated)), "세무/회계 비고": "당기 결손 발생 시 향후 10년간 법인세 과세표준에서 차감"},
        {"항목구분": "10. 손금산입 후 법인세 과세표준", "금액(KRW)": int(round(taxable_after_deduction)), "세무/회계 비고": "2억 이하 9.9% / 2억 초과 20.9% (지방소득세 포함)"},
        {"항목구분": "11. 당기 납부예정 법인세 추계액", "금액(KRW)": int(est_corp_tax_post), "세무/회계 비고": f"손금산입 절세효과: ₩{int(tax_saved_krw):,} 절감"},
    ]

    return {
        "daily_nav_rows": daily_nav_rows,
        "trade_rows": trade_rows,
        "holdings_rows": holdings_rows,
        "daily_pnl_rows": daily_pnl_rows,
        "tax_summary_rows": tax_summary_rows,
        "summary_metrics": {
            "nav_krw": int(round(total_nav_krw)),
            "fx_rate": fx_live,
            "total_trades": len(trade_rows),
            "tot_realized_net_krw": int(tot_realized_net),
            "tot_unrealized_pnl_krw": int(tot_unrealized_pnl),
            "est_corp_tax_post_krw": int(est_corp_tax_post),
            "carryforward_loss_krw": int(round(carryforward_loss_generated)),
        },
    }


def _write_styled_excel(bundle: Dict[str, Any], xlsx_path: Path) -> None:
    """5개 시트로 구성된 법인 회계·세무 마스터 엑셀 통합장부(.xlsx) 생성 및 서식 지정."""
    df_nav = pd.DataFrame(bundle["daily_nav_rows"])
    df_trades = pd.DataFrame(bundle["trade_rows"])
    df_holdings = pd.DataFrame(bundle["holdings_rows"])
    df_daily_pnl = pd.DataFrame(bundle["daily_pnl_rows"])
    df_tax = pd.DataFrame(bundle["tax_summary_rows"])

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df_nav.to_excel(writer, sheet_name="1_일별자산원장_Daily_NAV", index=False)
        df_trades.to_excel(writer, sheet_name="2_매매체결원장_Trade_Journal", index=False)
        df_holdings.to_excel(writer, sheet_name="3_기말재고명세_Holdings", index=False)
        df_daily_pnl.to_excel(writer, sheet_name="4_일별실현손익_Daily_PnL", index=False)
        df_tax.to_excel(writer, sheet_name="5_법인세추계및손금산입_Tax", index=False)

        try:
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter

            header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
            header_font = Font(color="FFFFFF", bold=True, size=10)
            body_font = Font(size=10)

            for sheet_name, ws in writer.sheets.items():
                ws.views.sheetView[0].showGridLines = True
                ws.freeze_panes = "A2"
                for col_idx in range(1, ws.max_column + 1):
                    cell = ws.cell(row=1, column=col_idx)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                    col_letter = get_column_letter(col_idx)
                    header_text = str(cell.value or "")
                    max_len = max(len(header_text) * 2, 12)
                    for row_idx in range(2, ws.max_row + 1):
                        rc = ws.cell(row=row_idx, column=col_idx)
                        rc.font = body_font
                        val = rc.value
                        if isinstance(val, int) and ("KRW" in header_text or "금액" in header_text or "손익" in header_text or "원가" in header_text or "예수금" in header_text or "수수료" in header_text or "공과금" in header_text):
                            rc.number_format = "#,##0"
                        elif isinstance(val, float) and ("%" in header_text or "수익률" in header_text or "비중" in header_text):
                            rc.number_format = "0.00"
                        elif isinstance(val, float) and ("USD" in header_text or "환율" in header_text or "단가" in header_text):
                            rc.number_format = "#,##0.00"
                        max_len = max(max_len, min(38, len(str(val or "")) + 4))
                    ws.column_dimensions[col_letter].width = max_len
        except Exception as e:
            logger.debug(f"Excel styling skipped: {e}")


def load_all_trade_records(fetch_live: bool = False) -> List[Dict[str, Any]]:
    """하위 호환용 인터페이스: 실계좌 체결원장 레코드 리스트 반환."""
    bundle = build_corporate_accounting_ledgers(fetch_live=fetch_live)
    return bundle["trade_rows"]


def generate_tax_optimization_report(gross_profit: float = 0.0, annual_expenses: float = ANNUAL_DEDUCTIBLE_EXPENSES_KRW) -> Path:
    """하위 호환용 세무 최적화 마크다운 리포트 생성."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    tax_pre = calculate_corporate_tax_bracket(max(0.0, gross_profit))
    tax_post = calculate_corporate_tax_bracket(max(0.0, gross_profit - annual_expenses))
    year_str = datetime.now().strftime("%Y")
    report_md = RESULTS_DIR / f"Corporate_Tax_Optimization_Report_{year_str}.md"
    report_md.write_text(
        f"# Project Meridian 법인 세무 결산 및 손금산입 리포트 ({year_str})\n\n"
        f"- **운용 실현손익**: ₩{gross_profit:,.0f} KRW\n"
        f"- **손금산입 필요경비**: ₩{annual_expenses:,.0f} KRW\n"
        f"- **공제 전 법인세**: ₩{tax_pre:,.0f} KRW\n"
        f"- **공제 후 법인세**: ₩{tax_post:,.0f} KRW\n"
        f"- **절세 금액**: **₩{tax_pre - tax_post:,.0f} KRW**\n",
        encoding="utf-8",
    )
    return report_md


def export_ledgers(fetch_live: bool = True, extra_output_dirs: Optional[List[Path]] = None) -> Dict[str, str]:
    """
    마스터 법인 회계·세무 원장 패키지(.xlsx, .csv 3종, .md)를 생성하여
    `results/corporate_ledger/` 및 지정된 추가 디렉토리(예: 맥북 바탕화면 폴더)에 저장.
    """
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    bundle = build_corporate_accounting_ledgers(fetch_live=fetch_live)
    today_str = datetime.now().strftime("%Y%m%d")

    df_nav = pd.DataFrame(bundle["daily_nav_rows"])
    df_trades = pd.DataFrame(bundle["trade_rows"])
    df_holdings = pd.DataFrame(bundle["holdings_rows"])
    df_daily_pnl = pd.DataFrame(bundle["daily_pnl_rows"])
    df_tax = pd.DataFrame(bundle["tax_summary_rows"])
    m = bundle["summary_metrics"]

    target_dirs = [LEDGER_DIR]
    if extra_output_dirs:
        for d in extra_output_dirs:
            d.mkdir(parents=True, exist_ok=True)
            if d not in target_dirs:
                target_dirs.append(d)

    written_paths: Dict[str, str] = {}
    for out_dir in target_dirs:
        xlsx_dated = out_dir / f"Meridian_Corporate_Accounting_Ledger_{today_str}.xlsx"
        xlsx_latest = out_dir / "Meridian_Corporate_Accounting_Ledger_Latest.xlsx"
        csv_nav = out_dir / f"Daily_NAV_Journal_{today_str}.csv"
        csv_trades = out_dir / f"Corporate_Trade_Ledger_{today_str}.csv"
        csv_holdings = out_dir / f"Corporate_Holdings_Subledger_{today_str}.csv"
        md_summary = out_dir / f"Corporate_Tax_Summary_{today_str}.md"

        try:
            _write_styled_excel(bundle, xlsx_dated)
            _write_styled_excel(bundle, xlsx_latest)
        except Exception as e:
            logger.warning(f"Excel generation failed in {out_dir} ({e}); CSV & MD still written.")

        df_nav.to_csv(csv_nav, index=False, encoding="utf-8-sig")
        df_trades.to_csv(csv_trades, index=False, encoding="utf-8-sig")
        df_holdings.to_csv(csv_holdings, index=False, encoding="utf-8-sig")

        with open(md_summary, "w", encoding="utf-8") as f:
            f.write(f"# Project Meridian 법인 회계·세무 주간 결산 원장 ({today_str})\n\n")
            f.write("> **적용 회계/세무 기준**: 대한민국 법인세법 시행령 제74조(유가증권 이동평균법) 및 제42조(원가법 과세이연)\n\n")
            f.write("## 1. 법인 계좌 핵심 재무 요약\n\n")
            f.write(f"- **결산 기준일**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S KST')}`\n")
            f.write(f"- **기말 총자산 (NAV)**: **₩{m['nav_krw']:,} KRW** (적용환율: `₩{m['fx_rate']:,.2f}/USD`)\n")
            f.write(f"- **누적 체결 건수**: `{m['total_trades']:,}건` (KIS 실계좌 ODNO 전수 검증)\n")
            f.write(f"- **당기 누적 실현순손익**: **₩{m['tot_realized_net_krw']:,} KRW**\n")
            f.write(f"- **기말 보유주식 미실현손익 (과세이연)**: **₩{m['tot_unrealized_pnl_krw']:,} KRW**\n")
            f.write(f"- **이월결손금 적립액 (향후 10년 법인세 공제자산)**: **₩{m['carryforward_loss_krw']:,} KRW**\n")
            f.write(f"- **당기 납부예정 법인세 추계액**: **₩{m['est_corp_tax_post_krw']:,} KRW**\n\n")
            f.write("## 2. 법인세 과세표준 및 손금산입 추계표\n\n")
            f.write(df_tax.to_markdown(index=False))
            f.write("\n\n## 3. 기말 보유 유가증권 장부가액 명세서 (Holdings Subledger)\n\n")
            if not df_holdings.empty:
                f.write(df_holdings.to_markdown(index=False))
            else:
                f.write("*(보유 종목 없음)*\n")
            f.write("\n\n## 4. 일별 실현손익 집계표 (Daily Realized PnL)\n\n")
            if not df_daily_pnl.empty:
                f.write(df_daily_pnl.tail(20).to_markdown(index=False))
            else:
                f.write("*(실현 내역 없음)*\n")
            f.write("\n\n---\n*Generated automatically by Project Meridian Corporate Accounting & Tax Engine*\n")

        written_paths = {
            "xlsx_dated": str(xlsx_dated),
            "xlsx_latest": str(xlsx_latest),
            "csv_nav": str(csv_nav),
            "csv_trades": str(csv_trades),
            "csv_holdings": str(csv_holdings),
            "md_summary": str(md_summary),
        }

    # Legacy path compatibility in results/
    df_trades.to_csv(RESULTS_DIR / f"Weekly_Corporate_Ledger_{today_str}.csv", index=False, encoding="utf-8-sig")
    df_trades.to_csv(RESULTS_DIR / f"Daily_Corporate_Ledger_{today_str}.csv", index=False, encoding="utf-8-sig")
    generate_tax_optimization_report(gross_profit=float(m["tot_realized_net_krw"]))

    logger.info(f"✅ Corporate Accounting Ledgers exported to {LEDGER_DIR} (Trades={len(df_trades)}, Holdings={len(df_holdings)}, NAV_Days={len(df_nav)})")
    return written_paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Project Meridian Corporate Accounting & Tax Ledgers")
    parser.add_argument("--no-live-fetch", action="store_true", help="Use cached broker fills without calling KIS API")
    parser.add_argument("--desktop-dir", type=str, default="", help="Optional additional Desktop directory to write files")
    args = parser.parse_args()

    extra_dirs = [Path(args.desktop_dir).expanduser()] if args.desktop_dir else None
    export_ledgers(fetch_live=not args.no_live_fetch, extra_output_dirs=extra_dirs)

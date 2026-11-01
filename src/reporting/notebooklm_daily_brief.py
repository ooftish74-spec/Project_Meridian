"""
Project Meridian — NotebookLM Daily Macro & Market Brief + 2-Host Audio Engine
===============================================================================
매일 오전 09:00 KST (08:45 Morning Pipeline + 08:50 Premarket Calibration 직후)
자동 실행되어 다음 산출물을 생성·동기화·전송합니다:

1. NotebookLM 연동용 종합 Daily Brief 문서 (.md / Google Drive Docs 동기화)
   - 글로벌 매크로 총괄 컨텍스트 & 레짐 진단
   - 5대 권역 Macro Key Index 동향 (미국 · 한국 · 중국 · 일본 · 유럽)
   - 국제 상품시장 (금 · 은 · 구리 · Gold/Copper Ratio) 및 에너지 시장 (WTI · 공급망 압력 · 원유 스트레스)
   - 전날 미국 증시 마감 결과 및 파생/옵션 미시구조 (GEX · Put/Call Ratio · CBOE Skew)
   - 당일 한국 증시 09:00 확정 전망 (OIS · 야간선물 · EWY · Morning Fusion) 및 실계좌 운용 전략
2. NotebookLM Audio Overview 스타일 2인 대담 오디오 브리핑 (.mp3)
   - 남성 모더레이터 (민준, ko-KR-InJoonNeural) & 여성 수석 퀀트 전략가 (서연, ko-KR-SunHiNeural)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VOICE_HOST_MALE = "ko-KR-InJoonNeural"
VOICE_STRATEGIST_FEMALE = "ko-KR-SunHiNeural"

NOTEBOOKLM_DRIVE_FOLDER_ID = "1js5GQffVhIn5Ikq27wO-3jj69FwoUiV8"
NOTEBOOKLM_MASTER_DOC_ID = "1xbvEntUKxFILGpd8blDvGictf13G8Ouwx8_qjQghbFE"
SERVICE_ACCOUNT_PATHS = (
    "/home/ubuntu/Project-A/config/gsheet_service_account.json",
    "/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project-A/config/gsheet_service_account.json",
)


def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, dict):
        for k in ("current", "close", "value", "price"):
            if k in val and val[k] is not None:
                return _to_float(val[k])
        return None
    try:
        f = float(val)
        if f != f:  # NaN check
            return None
        return f
    except (ValueError, TypeError):
        return None


def _fmt_num(val: float | None, decimals: int = 2, prefix: str = "", suffix: str = "") -> str:
    if val is None:
        return "미수신(N/A)"
    return f"{prefix}{val:,.{decimals}f}{suffix}"


def _fmt_chg(val: float | None, decimals: int = 2, suffix: str = "%") -> str:
    if val is None:
        return "N/A"
    return f"{val:+.{decimals}f}{suffix}"


def _spoken_num(val: float | None, decimals: int = 2, unit: str = "") -> str:
    if val is None:
        return "집계 대기 중"
    if decimals == 0:
        return f"{int(round(val)):,}{unit}"
    return f"{val:,.{decimals}f}{unit}"


def _spoken_chg(val: float | None, decimals: int = 2) -> str:
    if val is None:
        return "보합권"
    if val > 0:
        return f"{abs(val):.{decimals}f}퍼센트 상승"
    if val < 0:
        return f"{abs(val):.{decimals}f}퍼센트 하락"
    return "보합"


def _regime_kr_label(regime: str | None) -> str:
    mapping = {
        "bull": "강세장 (Bull — 위험자산 비중 확대)",
        "neutral": "중립장 (Neutral — 선별적 알파 운용)",
        "caution": "경계장 (Caution — 비대칭 리스크 방어 및 현금 버퍼 확보)",
        "bear": "약세장 (Bear — 자본 보전 최우선 및 인버스·페어 헤지)",
        "crash": "위기장 (Crash — 테일리스크 비상 방어)",
    }
    key = str(regime or "").strip().lower()
    return mapping.get(key, f"혼조장 ({regime or 'Neutral'})")


def extract_daily_brief_snapshot(
    signal_cache: dict[str, Any],
    latest_signals: dict[str, Any] | None = None,
    pipeline_state: dict[str, Any] | None = None,
    kis_portfolio: dict[str, Any] | None = None,
    report_date: str | None = None,
) -> dict[str, Any]:
    """SSOT JSON 데이터로부터 5대 권역 매크로, 상품·에너지, 미 증시, 한국 증시 전망 지표를 추출합니다."""
    sc = signal_cache or {}
    ls = latest_signals or {}
    ps = pipeline_state or {}
    kp = kis_portfolio or {}

    date_str = report_date or str(ls.get("date") or ps.get("date") or datetime.now().strftime("%Y-%m-%d"))[:10]

    # 1. US Macro & Equity Market Close
    sp500 = _to_float(sc.get("sp500"))
    sp500_chg_1d = _to_float(sc.get("sp500_change_1d"))
    sp500_chg_1m = _to_float(sc.get("sp500_change_1m"))
    nasdaq = _to_float(sc.get("nasdaq"))
    nasdaq_chg_1d = _to_float(sc.get("nasdaq_change_1d"))
    nasdaq_chg_1m = _to_float(sc.get("nasdaq_change_1m"))
    dji = _to_float(sc.get("dji"))
    dji_chg_1d = _to_float(sc.get("dji_change_1d"))
    sox = _to_float(sc.get("sox"))
    sox_chg_1d = _to_float(sc.get("sox_change_1d"))
    sox_chg_1m = _to_float(sc.get("sox_change_1m"))

    vix = _to_float(sc.get("vix"))
    vix_chg_1d = _to_float(sc.get("vix_change_1d"))
    vix_ma20 = _to_float(sc.get("vix_ma_20"))
    dxy = _to_float(sc.get("dxy"))
    dxy_chg_1d = _to_float(sc.get("dxy_change_1d"))

    us10y = _to_float(sc.get("us10y"))
    us10y_chg_1d = _to_float(sc.get("us10y_change_1d"))
    us5y = _to_float(sc.get("us5y"))
    us30y = _to_float(sc.get("us30y"))
    yc_10y_2y = _to_float(sc.get("yield_curve_10y_2y"))
    fedfunds = _to_float(sc.get("fedfunds"))
    hy_spread = _to_float(sc.get("fred_hy_spread"))
    unrate = _to_float(sc.get("unrate"))
    cpi = _to_float(sc.get("cpi"))
    us_lei = _to_float(sc.get("us_lei"))
    fear_greed = _to_float(sc.get("fear_greed_index"))

    # Options & Microstructure
    gex = _to_float(sc.get("gex"))
    gex_warn = bool(sc.get("gex_crash_warning"))
    pcr = _to_float(sc.get("options_pcr"))
    if pcr is None:
        pcr = _to_float(sc.get("put_call_ratio"))
    skew_z = _to_float(sc.get("cboe_skew_stress_z"))

    # 2. Korea Macro & Market Outlook
    kospi = _to_float(sc.get("kospi"))
    if kospi is None:
        kospi = _to_float(sc.get("kospi_close"))
    kospi_chg_1d = _to_float(sc.get("kospi_change_1d"))
    if kospi_chg_1d is None:
        kospi_chg_1d = _to_float(sc.get("kospi_change_pct"))
    kospi_20d = _to_float(sc.get("kospi_20d_return"))
    kosdaq = _to_float(sc.get("kosdaq"))
    if kosdaq is None:
        kosdaq = _to_float(sc.get("kosdaq_close"))
    kosdaq_chg_1d = _to_float(sc.get("kosdaq_change_1d"))
    if kosdaq_chg_1d is None:
        kosdaq_chg_1d = _to_float(sc.get("kosdaq_change_pct"))
    kospi200 = _to_float(sc.get("kospi200"))
    vkospi = _to_float(sc.get("vkospi"))
    usdkrw = _to_float(sc.get("usdkrw"))
    usdkrw_chg_1d = _to_float(sc.get("usdkrw_change_1d"))
    usdkrw_chg_5d = _to_float(sc.get("usdkrw_5d_change_pct"))
    ewy = _to_float(sc.get("ewy"))
    ewy_chg_1d = _to_float(sc.get("ewy_change_1d"))
    night_fut = _to_float(sc.get("night_futures"))
    night_fut_chg_1d = _to_float(sc.get("night_futures_change_1d"))
    kr_debt_z = _to_float(sc.get("kr_debt_domestic_stress_z"))

    inv_flow = sc.get("investor_flow") if isinstance(sc.get("investor_flow"), dict) else {}
    foreign_5d = _to_float(inv_flow.get("foreign_net_5d"))
    inst_5d = _to_float(inv_flow.get("inst_net_5d"))

    # 3. China Macro
    hangseng = _to_float(sc.get("hangseng"))
    hangseng_chg_1d = _to_float(sc.get("hangseng_change_1d"))
    fxi = _to_float(sc.get("fxi"))
    fxi_chg_1d = _to_float(sc.get("fxi_change_1d"))
    china_stress_z = _to_float(sc.get("china_recession_stress_z"))

    # 4. Japan & Asia Value Chain Macro
    nikkei = _to_float(sc.get("nikkei"))
    nikkei_chg_1d = _to_float(sc.get("nikkei_change_1d"))
    usdjpy = _to_float(sc.get("usdjpy"))
    usdjpy_chg_1d = _to_float(sc.get("usdjpy_change_1d"))
    taiex = _to_float(sc.get("taiex"))
    taiex_chg_1d = _to_float(sc.get("taiex_change_1d"))
    jp_stress_z = _to_float(sc.get("japan_stagflation_stress_z"))

    # 5. Europe Macro
    eurusd = _to_float(sc.get("eurusd"))
    eurusd_chg_1d = _to_float(sc.get("eurusd_change_1d"))
    eu_bank_z = _to_float(sc.get("eu_banking_stress_z"))
    eu_instab_z = _to_float(sc.get("eu_instability_stress_z"))

    # 6. International Commodities & Energy Markets
    wti = _to_float(sc.get("wti"))
    wti_chg_1d = _to_float(sc.get("wti_change_1d"))
    wti_chg_1m = _to_float(sc.get("wti_change_1m"))
    wti_5d_z = _to_float(sc.get("wti_5d_z"))
    oil_supply_z = _to_float(sc.get("geopolitical_oil_supply_z"))
    gscpi_z = _to_float(sc.get("gscpi_stress_z"))

    gold = _to_float(sc.get("gold"))
    gold_chg_1d = _to_float(sc.get("gold_change_1d"))
    gold_chg_1m = _to_float(sc.get("gold_change_1m"))
    silver = _to_float(sc.get("silver"))
    silver_chg_1d = _to_float(sc.get("silver_change_1d"))
    copper = _to_float(sc.get("copper"))
    copper_chg_1d = _to_float(sc.get("copper_change_1d"))
    gold_copper_ratio = (gold / copper) if (gold is not None and copper is not None and copper > 0) else None
    gold_silver_ratio = (gold / silver) if (gold is not None and silver is not None and silver > 0) else None

    # 7. Regime, OIS, Morning Fusion & Portfolio Strategy
    ois = _to_float(sc.get("ois"))
    ois_details = sc.get("ois_details") if isinstance(sc.get("ois_details"), dict) else {}
    ois_sentiment = str(ois_details.get("sentiment") or sc.get("ois_sentiment") or "neutral")
    global_regime = str(sc.get("regime") or ps.get("regime") or "caution")
    kr_regime = str(sc.get("kr_regime") or ps.get("kr_regime") or global_regime)
    us_regime = str(sc.get("us_regime") or ps.get("us_regime") or "neutral")
    regime_conf = _to_float(sc.get("regime_confidence"))
    if regime_conf is None:
        regime_conf = _to_float(ps.get("kr_regime_confidence"))
    global_stress_z = _to_float(sc.get("global_macro_stress_z"))

    mf = sc.get("morning_fusion") if isinstance(sc.get("morning_fusion"), dict) else {}
    mf_adj = _to_float(mf.get("regime_adjustment"))
    mf_sectors = mf.get("sector_overrides") if isinstance(mf.get("sector_overrides"), dict) else {}
    mf_flags = mf.get("risk_flags") if isinstance(mf.get("risk_flags"), list) else []

    # Active Signals from 08:45 Morning + 08:50 Premarket Calibration
    raw_sigs = ls.get("signals") if isinstance(ls.get("signals"), dict) else {}
    active_signals_summary: list[dict[str, Any]] = []
    for stream_id, sig_list in sorted(raw_sigs.items()):
        if not isinstance(sig_list, list):
            continue
        for item in sig_list:
            if not isinstance(item, dict):
                continue
            tk = str(item.get("ticker") or item.get("code") or "")
            if not tk:
                continue
            active_signals_summary.append({
                "stream": stream_id,
                "ticker": tk,
                "name": str(item.get("name") or tk),
                "direction": str(item.get("direction") or item.get("action") or "LONG"),
                "weight": _to_float(item.get("weight") or item.get("target_weight") or item.get("kelly_fraction")),
                "confidence": _to_float(item.get("confidence") or item.get("score")),
            })

    # Live KIS Portfolio Summary
    holdings_dict = kp.get("holdings") if isinstance(kp.get("holdings"), dict) else {}
    nav_krw = _to_float(kp.get("total_asset") or kp.get("nav"))
    cash_krw = _to_float(kp.get("d2_settlement_cash_krw") or kp.get("cash"))
    cash_usd = _to_float(kp.get("cash_usd") or kp.get("us_cash_usd"))
    fx_rate = _to_float(kp.get("fx_rate")) or usdkrw

    held_names: list[str] = []
    for tk, hinfo in holdings_dict.items():
        if isinstance(hinfo, dict) and (_to_float(hinfo.get("qty")) or 0) > 0:
            nm = str(hinfo.get("name") or tk)
            held_names.append(f"{nm}({tk})")

    return {
        "date": date_str,
        "generated_at_kst": datetime.now().strftime("%Y-%m-%d %H:%M:%S KST"),
        "regimes": {
            "global": global_regime,
            "kr": kr_regime,
            "us": us_regime,
            "confidence": regime_conf,
            "global_stress_z": global_stress_z,
            "ois": ois,
            "ois_sentiment": ois_sentiment,
        },
        "us": {
            "sp500": sp500,
            "sp500_chg_1d": sp500_chg_1d,
            "sp500_chg_1m": sp500_chg_1m,
            "nasdaq": nasdaq,
            "nasdaq_chg_1d": nasdaq_chg_1d,
            "nasdaq_chg_1m": nasdaq_chg_1m,
            "dji": dji,
            "dji_chg_1d": dji_chg_1d,
            "sox": sox,
            "sox_chg_1d": sox_chg_1d,
            "sox_chg_1m": sox_chg_1m,
            "vix": vix,
            "vix_chg_1d": vix_chg_1d,
            "vix_ma20": vix_ma20,
            "dxy": dxy,
            "dxy_chg_1d": dxy_chg_1d,
            "us10y": us10y,
            "us10y_chg_1d": us10y_chg_1d,
            "us5y": us5y,
            "us30y": us30y,
            "yc_10y_2y": yc_10y_2y,
            "fedfunds": fedfunds,
            "hy_spread": hy_spread,
            "unrate": unrate,
            "cpi": cpi,
            "us_lei": us_lei,
            "fear_greed": fear_greed,
            "gex": gex,
            "gex_warn": gex_warn,
            "pcr": pcr,
            "skew_z": skew_z,
        },
        "kr": {
            "kospi": kospi,
            "kospi_chg_1d": kospi_chg_1d,
            "kospi_20d": kospi_20d,
            "kosdaq": kosdaq,
            "kosdaq_chg_1d": kosdaq_chg_1d,
            "kospi200": kospi200,
            "vkospi": vkospi,
            "usdkrw": usdkrw,
            "usdkrw_chg_1d": usdkrw_chg_1d,
            "usdkrw_chg_5d": usdkrw_chg_5d,
            "ewy": ewy,
            "ewy_chg_1d": ewy_chg_1d,
            "night_fut": night_fut,
            "night_fut_chg_1d": night_fut_chg_1d,
            "kr_debt_z": kr_debt_z,
            "foreign_5d": foreign_5d,
            "inst_5d": inst_5d,
        },
        "cn": {
            "hangseng": hangseng,
            "hangseng_chg_1d": hangseng_chg_1d,
            "fxi": fxi,
            "fxi_chg_1d": fxi_chg_1d,
            "china_stress_z": china_stress_z,
        },
        "jp": {
            "nikkei": nikkei,
            "nikkei_chg_1d": nikkei_chg_1d,
            "usdjpy": usdjpy,
            "usdjpy_chg_1d": usdjpy_chg_1d,
            "taiex": taiex,
            "taiex_chg_1d": taiex_chg_1d,
            "jp_stress_z": jp_stress_z,
        },
        "eu": {
            "eurusd": eurusd,
            "eurusd_chg_1d": eurusd_chg_1d,
            "eu_bank_z": eu_bank_z,
            "eu_instab_z": eu_instab_z,
        },
        "commodities": {
            "wti": wti,
            "wti_chg_1d": wti_chg_1d,
            "wti_chg_1m": wti_chg_1m,
            "wti_5d_z": wti_5d_z,
            "oil_supply_z": oil_supply_z,
            "gscpi_z": gscpi_z,
            "gold": gold,
            "gold_chg_1d": gold_chg_1d,
            "gold_chg_1m": gold_chg_1m,
            "silver": silver,
            "silver_chg_1d": silver_chg_1d,
            "copper": copper,
            "copper_chg_1d": copper_chg_1d,
            "gold_copper_ratio": gold_copper_ratio,
            "gold_silver_ratio": gold_silver_ratio,
        },
        "morning_fusion": {
            "regime_adjustment": mf_adj,
            "sector_overrides": mf_sectors,
            "risk_flags": mf_flags,
        },
        "active_signals": active_signals_summary,
        "portfolio": {
            "nav_krw": nav_krw,
            "cash_krw": cash_krw,
            "cash_usd": cash_usd,
            "fx_rate": fx_rate,
            "holding_count": len(held_names),
            "held_names": held_names,
        },
    }


def _diagnose_macro_context(snap: dict[str, Any]) -> dict[str, str]:
    """수집된 지표를 바탕으로 권역별·자산군별 수리적 컨텍스트 해석 문장을 생성합니다."""
    us = snap["us"]
    kr = snap["kr"]
    cn = snap["cn"]
    jp = snap["jp"]
    eu = snap["eu"]
    cm = snap["commodities"]
    rg = snap["regimes"]

    # 1. US Equity & Tech Context
    sp_c = us["sp500_chg_1d"] or 0.0
    nq_c = us["nasdaq_chg_1d"] or 0.0
    sox_c = us["sox_chg_1d"] or 0.0
    _us_tech_drop = (sox_c < -2.0) or (nq_c < -1.5)  # hardcode-ok: narrative classification display
    _us_broad_rally = (sp_c > 0.5) and (sox_c > 0.5)  # hardcode-ok: narrative classification display
    if _us_tech_drop:
        us_eq_ctx = (
            f"미국 증시는 기술주(NASDAQ {_fmt_chg(us['nasdaq_chg_1d'])})와 반도체(SOX {_fmt_chg(us['sox_chg_1d'])}) 중심의 "
            f"차익실현 및 밸류에이션 조정 압력이 나타났으며, VIX가 {_fmt_num(us['vix'])}pt({_fmt_chg(us['vix_chg_1d'])})로 상승하며 단기 변동성 경계감이 유입되었습니다."
        )
    elif _us_broad_rally:
        us_eq_ctx = (
            f"미국 증시는 S&P 500({_fmt_chg(us['sp500_chg_1d'])})과 반도체 지수(SOX {_fmt_chg(us['sox_chg_1d'])})가 동반 상승하며 "
            f"위험자산 선호 심리가 견조하게 유지되었습니다."
        )
    else:
        us_eq_ctx = (
            f"미국 3대 지수는 S&P 500 {_fmt_chg(us['sp500_chg_1d'])}, NASDAQ {_fmt_chg(us['nasdaq_chg_1d'])}, "
            f"Dow Jones {_fmt_chg(us['dji_chg_1d'])}로 마감하며 업종별 순환매와 매크로 지표 소화 장세를 보였습니다."
        )

    # 2. US Rates & Macro Context
    yc = us["yc_10y_2y"]
    yc_desc = "정상 우상향(양수 스프레드)" if (yc is not None and yc >= 0) else "장단기 역전"
    us_rate_ctx = (
        f"미 국채 10년물 금리는 {_fmt_num(us['us10y'], suffix='%')}({_fmt_chg(us['us10y_chg_1d'])}), "
        f"30년물 {_fmt_num(us['us30y'], suffix='%')}, 10Y-2Y 스프레드는 {_fmt_num(yc, decimals=3, suffix='%p')}({yc_desc})를 기록 중입니다. "
        f"연준 기준금리 {_fmt_num(us['fedfunds'], suffix='%')}, 실업률 {_fmt_num(us['unrate'], decimals=1, suffix='%')}, "
        f"달러인덱스(DXY) {_fmt_num(us['dxy'])}({_fmt_chg(us['dxy_chg_1d'])}) 수준에서 실질 할인율 부담과 유동성 여건이 교차하고 있습니다."
    )

    # 3. China / Japan / Europe Context
    cn_c = cn["hangseng_chg_1d"] or 0.0
    fxi_c = cn["fxi_chg_1d"] or 0.0
    if cn_c > 0 or fxi_c > 0:
        cn_ctx = (
            f"중화권 증시는 항셍지수 {_fmt_chg(cn['hangseng_chg_1d'])}, FXI(중국 대형주 ETF) {_fmt_chg(cn['fxi_chg_1d'])}로 반등하며 "
            f"미국 기술주 조정과 디커플링(탈동조화) 흐름을 보였으나, 중국 경기침체 스트레스 Z-Score가 {_fmt_num(cn['china_stress_z'])}σ로 구조적 내수 둔화 경계는 유효합니다."
        )
    else:
        cn_ctx = (
            f"중화권 시장은 항셍지수 {_fmt_chg(cn['hangseng_chg_1d'])}, FXI {_fmt_chg(cn['fxi_chg_1d'])}, "
            f"중국 경기침체 스트레스 지수 {_fmt_num(cn['china_stress_z'])}σ를 기록하며 부양책 실효성과 제조업 수요 회복을 탐색 중입니다."
        )

    jp_ctx = (
        f"일본 증시는 닛케이 225가 {_fmt_num(jp['nikkei'])}pt({_fmt_chg(jp['nikkei_chg_1d'])})로 마감했고, "
        f"엔/달러 환율(USD/JPY)은 {_fmt_num(jp['usdjpy'])}엔({_fmt_chg(jp['usdjpy_chg_1d'])})에서 움직이고 있습니다. "
        f"아시아 반도체 밸류체인 동조화 지표인 대만 가권지수(TAIEX)는 {_fmt_chg(jp['taiex_chg_1d'])}를 기록하여 한국 대형 IT 섹터의 수급 방향성에 직접적인 시사점을 제공합니다."
    )

    eu_ctx = (
        f"유럽 권역은 유로/달러(EUR/USD) 환율 {_fmt_num(eu['eurusd'], decimals=4)}({_fmt_chg(eu['eurusd_chg_1d'])}), "
        f"유럽 은행 스트레스 Z-Score {_fmt_num(eu['eu_bank_z'])}σ, 지정학·정치 불안정성 Z-Score {_fmt_num(eu['eu_instab_z'])}σ로 "
        f"유로존 금융여건 긴축도와 에너지 비용 부담이 글로벌 달러 수요를 지지하는 요인으로 작용하고 있습니다."
    )

    # 4. Energy & Commodity Context
    wti_v = cm["wti"] or 0.0
    wti_1m = cm["wti_chg_1m"] or 0.0
    energy_ctx = (
        f"국제 에너지 시장에서 WTI 원유 선물은 배럴당 ${_fmt_num(cm['wti'])} (1일 {_fmt_chg(cm['wti_chg_1d'])}, 1개월 {_fmt_chg(cm['wti_chg_1m'])})를 기록하고 있으며, "
        f"글로벌 공급망 압력지수(GSCPI Z={_fmt_num(cm['gscpi_z'])}σ) 및 원유공급 스트레스(Z={_fmt_num(cm['oil_supply_z'])}σ)와 맞물려 "
        f"{'고유가에 따른 인플레이션 재점화 및 에너지 섹터 상대 강세' if wti_v >= 85.0 or wti_1m > 5.0 else '원유 수급 균형 탐색'} 국면을 형성하고 있습니다."  # hardcode-ok: narrative display
    )

    commodity_ctx = (
        f"국제 상품시장에서 금(Gold)은 온스당 ${_fmt_num(cm['gold'])}({_fmt_chg(cm['gold_chg_1d'])}), "
        f"은(Silver)은 ${_fmt_num(cm['silver'])}({_fmt_chg(cm['silver_chg_1d'])}), "
        f"경기 선행 지표인 구리(Dr. Copper)는 파운드당 ${_fmt_num(cm['copper'], decimals=4)}({_fmt_chg(cm['copper_chg_1d'])})를 기록했습니다. "
        f"금/구리 비율(Gold/Copper Ratio)은 {_fmt_num(cm['gold_copper_ratio'], decimals=1)}배로, "
        f"{'구리 대비 금의 상대적 고평가가 지속되며 글로벌 제조업 확장보다 스태그플레이션·안전자산 방어 심리가 우세한' if (cm['gold_copper_ratio'] or 0) > 500 else '산업금속과 귀금속 간 균형이 유지되는'} 신호를 보내고 있습니다."  # hardcode-ok: narrative display
    )

    # 5. Today's KRX Outlook at 09:00 KST
    nf_c = kr["night_fut_chg_1d"] or 0.0
    ewy_c = kr["ewy_chg_1d"] or 0.0
    _kr_down_gap = (nf_c < -1.0) or (ewy_c < -1.5)  # hardcode-ok: narrative classification display
    _kr_up_gap = (nf_c > 0.5) or (ewy_c > 0.5)  # hardcode-ok: narrative classification display
    if _kr_down_gap:
        kr_outlook_ctx = (
            f"당일 한국 증시는 전일 미 반도체(SOX {_fmt_chg(us['sox_chg_1d'])}) 조정과 야간선물({_fmt_chg(kr['night_fut_chg_1d'])}), "
            f"MSCI 한국 ETF(EWY {_fmt_chg(kr['ewy_chg_1d'])}) 약세의 영향으로 시초가 하방 압력 및 변동성 확대가 예상됩니다. "
            f"야간 종합 지수(OIS)는 {_fmt_num(rg['ois'], decimals=1)}점({rg['ois_sentiment']})이며, 메리디안 시스템은 한국장 운영 레짐을 "
            f"'{_regime_kr_label(rg['kr'])}'으로 설정하여 추격 매수를 억제하고 인버스·롱숏 페어 알파(S14) 및 퀄리티 가치주(S3_B)·방어형 현금 버퍼 중심으로 자본 보전을 최우선 수행합니다."
        )
    elif _kr_up_gap:
        kr_outlook_ctx = (
            f"당일 한국 증시는 야간선물({_fmt_chg(kr['night_fut_chg_1d'])}) 및 EWY({_fmt_chg(kr['ewy_chg_1d'])}) 강세에 힘입어 "
            f"우호적인 출발이 예상됩니다. OIS {_fmt_num(rg['ois'], decimals=1)}점 및 '{_regime_kr_label(rg['kr'])}' 체제 하에서 "
            f"수급 돌파 종목과 멀티팩터 상위 종목을 중심으로 선별적 비중 확대를 집행합니다."
        )
    else:
        kr_outlook_ctx = (
            f"당일 한국 증시는 야간선물({_fmt_chg(kr['night_fut_chg_1d'])}), EWY({_fmt_chg(kr['ewy_chg_1d'])}), "
            f"원/달러 환율 {_fmt_num(kr['usdkrw'])}원({_fmt_chg(kr['usdkrw_chg_1d'])}) 흐름 속에서 시초가 탐색 후 외국인·기관 장중 수급에 연동된 종목 장세가 예상됩니다. "
            f"메리디안은 '{_regime_kr_label(rg['kr'])}' 레짐 기준에 따라 정량 시그널이 검증된 포지션만 엄격히 운용합니다."
        )

    return {
        "us_eq_ctx": us_eq_ctx,
        "us_rate_ctx": us_rate_ctx,
        "cn_ctx": cn_ctx,
        "jp_ctx": jp_ctx,
        "eu_ctx": eu_ctx,
        "energy_ctx": energy_ctx,
        "commodity_ctx": commodity_ctx,
        "kr_outlook_ctx": kr_outlook_ctx,
    }


def build_notebooklm_brief_markdown(snap: dict[str, Any]) -> str:
    """NotebookLM 소스 연동 및 이메일/텔레그램 리포트용 종합 Daily Brief 마크다운 문서를 생성합니다."""
    date_str = snap["date"]
    gen_ts = snap["generated_at_kst"]
    rg = snap["regimes"]
    us = snap["us"]
    kr = snap["kr"]
    cn = snap["cn"]
    jp = snap["jp"]
    eu = snap["eu"]
    cm = snap["commodities"]
    mf = snap["morning_fusion"]
    pf = snap["portfolio"]
    ctx = _diagnose_macro_context(snap)

    # Risk flags formatting
    flag_lines: list[str] = []
    for fl in mf["risk_flags"]:
        if isinstance(fl, dict):
            flag_lines.append(f"`{fl.get('flag', 'RISK')}` ({fl.get('detail', '')})")
        else:
            flag_lines.append(str(fl))
    flags_str = ", ".join(flag_lines) if flag_lines else "특이 테일리스크 플래그 없음"

    sector_str = (
        ", ".join(f"{k}: {float(v):+.1%}" for k, v in mf["sector_overrides"].items() if _to_float(v) is not None)
        if mf["sector_overrides"]
        else "중립(기본 가중치 유지)"
    )

    sig_rows_md = ""
    if snap["active_signals"]:
        for s in snap["active_signals"][:12]:
            w_str = f"{s['weight']*100:.1f}%" if s["weight"] is not None else "-"
            c_str = f"{s['confidence']:.2f}" if s["confidence"] is not None else "-"
            sig_rows_md += f"| `{s['stream']}` | `{s['ticker']}` ({s['name']}) | {s['direction']} | {w_str} | {c_str} |\n"
    else:
        sig_rows_md = "| `DEFENSIVE` | 신규 진입 보류 (방어 모드) | HOLD / EXIT-GUARD | - | 기존 포지션 보호 |\n"

    held_preview = ", ".join(pf["held_names"][:12]) if pf["held_names"] else "보유 종목 없음 (전액 현금)"

    md = f"""# [Project Meridian] 글로벌 매크로 & 마켓 인텔리전스 데일리 브리프 ({date_str})

> **생성 시각**: `{gen_ts}` (08:45 Morning Intelligence + 08:50 동시호가 Calibration 확정 기준)
> **글로벌 레짐**: `{rg['global'].upper()}` | **한국장 운용 레짐**: `{_regime_kr_label(rg['kr'])}` (신뢰도 `{_fmt_num((rg['confidence'] or 0)*100, 1)}%`) | **미국장 레짐**: `{rg['us'].upper()}`
> **야간 종합 지수 (OIS)**: `{_fmt_num(rg['ois'], 1)}점 ({rg['ois_sentiment']})` | **글로벌 매크로 스트레스 Z**: `{_fmt_num(rg['global_stress_z'])}σ`

---

## 1. Executive Context Brief (오늘의 글로벌 매크로 핵심 맥락)

- **미국 증시 및 글로벌 기술주 동향**: {ctx['us_eq_ctx']}
- **금리·환율 및 유동성 환경**: {ctx['us_rate_ctx']}
- **국제 에너지 및 상품시장 시그널**: {ctx['energy_ctx']} {ctx['commodity_ctx']}
- **당일 한국 증시(KRX) 09:00 최종 전망**: {ctx['kr_outlook_ctx']}

---

## 2. 5대 권역 Macro Key Index 동향 및 컨텍스트 (한국 · 미국 · 중국 · 일본 · 유럽)

### 2.1 권역별 핵심 주가지수 · 환율 · 금리 · 스트레스 지표 종합표

| 권역 | 핵심 지표 (Key Index) | 현재 수치 / 종가 | 1일 등락률 (변동) | 보조 지표 및 매크로 스트레스 Z-Score |
| :--- | :--- | :--- | :--- | :--- |
| **미국 (US)** | **S&P 500 / NASDAQ** | `{_fmt_num(us['sp500'])}` / `{_fmt_num(us['nasdaq'])}` | `{_fmt_chg(us['sp500_chg_1d'])}` / `{_fmt_chg(us['nasdaq_chg_1d'])}` | 1개월 등락: S&P `{_fmt_chg(us['sp500_chg_1m'])}`, NQ `{_fmt_chg(us['nasdaq_chg_1m'])}` |
| **미국 (US)** | **Dow Jones / 필라델피아 반도체(SOX)** | `{_fmt_num(us['dji'])}` / `{_fmt_num(us['sox'])}` | `{_fmt_chg(us['dji_chg_1d'])}` / `{_fmt_chg(us['sox_chg_1d'])}` | SOX 1개월 등락: `{_fmt_chg(us['sox_chg_1m'])}` |
| **미국 (US)** | **미 국채 10Y / 5Y / 30Y 금리** | `{_fmt_num(us['us10y'], suffix='%')}` / `{_fmt_num(us['us5y'], suffix='%')}` / `{_fmt_num(us['us30y'], suffix='%')}` | 10Y `{_fmt_chg(us['us10y_chg_1d'])}` | 10Y-2Y 스프레드: `{_fmt_num(us['yc_10y_2y'], 3, suffix='%p')}` / 기준금리 `{_fmt_num(us['fedfunds'], suffix='%')}` |
| **미국 (US)** | **VIX 변동성 / 달러인덱스(DXY)** | `VIX {_fmt_num(us['vix'])}` / `DXY {_fmt_num(us['dxy'])}` | `VIX {_fmt_chg(us['vix_chg_1d'])}` / `DXY {_fmt_chg(us['dxy_chg_1d'])}` | 실업률 `{_fmt_num(us['unrate'], 1, suffix='%')}`, CPI `{_fmt_num(us['cpi'], 1)}`, LEI `{_fmt_num(us['us_lei'])}` |
| **한국 (KR)** | **KOSPI / KOSDAQ / KOSPI 200** | `{_fmt_num(kr['kospi'])}` / `{_fmt_num(kr['kosdaq'])}` / `{_fmt_num(kr['kospi200'])}` | KOSPI `{_fmt_chg(kr['kospi_chg_1d'])}` / KOSDAQ `{_fmt_chg(kr['kosdaq_chg_1d'])}` | VKOSPI `{_fmt_num(kr['vkospi'])}` / 내수·부채 스트레스 `{_fmt_num(kr['kr_debt_z'])}σ` |
| **한국 (KR)** | **USD/KRW 환율 / 야간선물 / EWY** | `{_fmt_num(kr['usdkrw'], suffix='원')}` / 선물 `{_fmt_num(kr['night_fut'])}` / EWY `${_fmt_num(kr['ewy'])}` | 환율 `{_fmt_chg(kr['usdkrw_chg_1d'])}` / 선물 `{_fmt_chg(kr['night_fut_chg_1d'])}` / EWY `{_fmt_chg(kr['ewy_chg_1d'])}` | 환율 5일 변동 `{_fmt_chg(kr['usdkrw_chg_5d'])}` / 외인 5일 수급Z `{_fmt_num(kr['foreign_5d'])}` |
| **중국 (CN)** | **홍콩 항셍(HSI) / 중국 대형주(FXI)** | `{_fmt_num(cn['hangseng'])}` / `${_fmt_num(cn['fxi'])}` | 항셍 `{_fmt_chg(cn['hangseng_chg_1d'])}` / FXI `{_fmt_chg(cn['fxi_chg_1d'])}` | 중국 경기침체 스트레스 Z-Score: `{_fmt_num(cn['china_stress_z'])}σ` |
| **일본·대만 (JP/TW)** | **닛케이 225 / USD/JPY / 대만 가권(TAIEX)** | `{_fmt_num(jp['nikkei'])}` / `{_fmt_num(jp['usdjpy'], suffix='엔')}` / `{_fmt_num(jp['taiex'])}` | 닛케이 `{_fmt_chg(jp['nikkei_chg_1d'])}` / 엔화 `{_fmt_chg(jp['usdjpy_chg_1d'])}` / 대만 `{_fmt_chg(jp['taiex_chg_1d'])}` | 일본 스태그플레이션 스트레스 Z-Score: `{_fmt_num(jp['jp_stress_z'])}σ` |
| **유럽 (EU)** | **유로/달러 (EUR/USD) 및 금융안정 지수** | `{_fmt_num(eu['eurusd'], 4, prefix='$')}` | `{_fmt_chg(eu['eurusd_chg_1d'])}` | 유럽 은행 스트레스 `{_fmt_num(eu['eu_bank_z'])}σ` / 불안정성 `{_fmt_num(eu['eu_instab_z'])}σ` |

### 2.2 권역별 매크로 상세 해설
- **미국 (US)**: {ctx['us_rate_ctx']} 하이일드 크레딧 스프레드는 `{_fmt_num(us['hy_spread'], 3)}%p`를 기록하고 있습니다.
- **한국 (KR)**: 원/달러 환율은 `{_fmt_num(kr['usdkrw'])}원`(1일 `{_fmt_chg(kr['usdkrw_chg_1d'])}`, 5일 `{_fmt_chg(kr['usdkrw_chg_5d'])}`)이며, VKOSPI는 `{_fmt_num(kr['vkospi'])}pt`입니다.
- **중국 (CN)**: {ctx['cn_ctx']}
- **일본 및 아시아 밸류체인 (JP/TW)**: {ctx['jp_ctx']}
- **유럽 (EU)**: {ctx['eu_ctx']}

---

## 3. 국제 상품시장 (Commodities) 및 에너지 시장 (Energy) 동향

| 구분 | 품목 / 지표명 | 현재 가격 / 지수 | 1일 변동률 | 1개월 변동 / Z-Score 및 경제적 함의 |
| :--- | :--- | :--- | :--- | :--- |
| **에너지 (Energy)** | **WTI 원유 선물 (Crude Oil)** | `${_fmt_num(cm['wti'])} / bbl` | `{_fmt_chg(cm['wti_chg_1d'])}` | 1개월 `{_fmt_chg(cm['wti_chg_1m'])}` / 5일 변동 Z: `{_fmt_num(cm['wti_5d_z'])}σ` |
| **에너지·공급망** | **원유공급 스트레스 / 공급망 압력(GSCPI)** | `Oil Z: {_fmt_num(cm['oil_supply_z'])}σ` | `GSCPI Z: {_fmt_num(cm['gscpi_z'])}σ` | 지정학 에너지 리스크 및 글로벌 해상·물류 병목 지표 |
| **귀금속 (Precious)** | **국제 금 (Gold)** | `${_fmt_num(cm['gold'])} / oz` | `{_fmt_chg(cm['gold_chg_1d'])}` | 1개월 `{_fmt_chg(cm['gold_chg_1m'])}` (실질금리·중앙은행 안전자산 수요) |
| **귀금속·산업재** | **국제 은 (Silver)** | `${_fmt_num(cm['silver'])} / oz` | `{_fmt_chg(cm['silver_chg_1d'])}` | 금/은 비율(Gold/Silver Ratio): `{_fmt_num(cm['gold_silver_ratio'], 1)}배` |
| **산업금속 (Base)** | **국제 구리 (Dr. Copper)** | `${_fmt_num(cm['copper'], 4)} / lb` | `{_fmt_chg(cm['copper_chg_1d'])}` | **금/구리 비율(Gold/Copper Ratio)**: `{_fmt_num(cm['gold_copper_ratio'], 1)}배` |

- **에너지 시장 진단**: {ctx['energy_ctx']}
- **상품시장 및 경기 사이클 진단**: {ctx['commodity_ctx']}

---

## 4. 전날 미국 증시 결과 심층 복기 (US Market Close & Options Microstructure)

- **주요 지수 마감 성적**:
  - **S&P 500**: `{_fmt_num(us['sp500'])}` (`{_fmt_chg(us['sp500_chg_1d'])}`)
  - **NASDAQ**: `{_fmt_num(us['nasdaq'])}` (`{_fmt_chg(us['nasdaq_chg_1d'])}`)
  - **Dow Jones**: `{_fmt_num(us['dji'])}` (`{_fmt_chg(us['dji_chg_1d'])}`)
  - **필라델피아 반도체 (SOX)**: `{_fmt_num(us['sox'])}` (`{_fmt_chg(us['sox_chg_1d'])}`)
- **파생상품 및 옵션 시장 미시구조 (Microstructure)**:
  - **딜러 감마 익스포저 (GEX)**: `{_fmt_num(us['gex'])}` (감마 급락 경보: `{'발령(WARNING)' if us['gex_warn'] else '정상(CLEAR)'}`)
  - **옵션 Put/Call Ratio (PCR)**: `{_fmt_num(us['pcr'])}` | **CBOE 꼬리위험 스큐 (Skew Stress Z)**: `{_fmt_num(us['skew_z'])}σ`
  - **VIX 변동성 구조**: 현재 `{_fmt_num(us['vix'])}pt` (20일 이동평균 `{_fmt_num(us['vix_ma20'])}pt` 대비 `{'상회 — 단기 헤지 수요 급증' if (us['vix'] or 0) > (us['vix_ma20'] or 0) else '하회 — 변동성 안정'}`)

---

## 5. 당일 한국 증시 예상 및 메리디안 09:00 실전 대응 전략 (KRX Outlook)

- **한국 증시 개장 전 핵심 선행 지표 (08:50 동시호가 보정 반영)**:
  - **야간 종합 지수 (OIS)**: `{_fmt_num(rg['ois'], 1)}점` (`{rg['ois_sentiment']}`)
  - **KOSPI 200 야간선물**: `{_fmt_num(kr['night_fut'])}pt` (`{_fmt_chg(kr['night_fut_chg_1d'])}`)
  - **MSCI 한국 ETF (EWY)**: `${_fmt_num(kr['ewy'])}` (`{_fmt_chg(kr['ewy_chg_1d'])}`)
  - **Morning Intelligence Fusion**: 레짐 보정치 `{_fmt_num(mf['regime_adjustment'])}` | 섹터 오버라이드 `{sector_str}` | 리스크 플래그: {flags_str}
- **당일 장세 전망 요약**: {ctx['kr_outlook_ctx']}

### 5.1 당일 메리디안 확정 시그널 및 실계좌 포트폴리오 현황
- **실계좌 총 평가자산 (체결기준)**: `₩{_fmt_num(pf['nav_krw'], 0)}` (원화 가용현금 `₩{_fmt_num(pf['cash_krw'], 0)}`, 외화 가용현금 `${_fmt_num(pf['cash_usd'], 2)}`, 적용환율 `{_fmt_num(pf['fx_rate'])}원`)
- **현재 보유 포지션 ({pf['holding_count']}종목)**: {held_preview}

| 스트림 (Stream) | 티커 (종목명) | 방향 (Action) | 목표 비중 | 신호 신뢰도 / 비고 |
| :--- | :--- | :---: | :---: | :---: |
{sig_rows_md}
"""
    return md.strip() + "\n"


def build_two_host_dialogue_script(snap: dict[str, Any]) -> list[dict[str, str]]:
    """NotebookLM Audio Overview 스타일의 2인 대담(남성 모더레이터 민준 & 여성 수석 전략가 서연) 대본을 생성합니다."""
    date_str = snap["date"]
    rg = snap["regimes"]
    us = snap["us"]
    kr = snap["kr"]
    cn = snap["cn"]
    jp = snap["jp"]
    eu = snap["eu"]
    cm = snap["commodities"]
    pf = snap["portfolio"]

    # Determine directional tone for natural spoken dialogue
    sox_c = us["sox_chg_1d"] or 0.0
    sp_c = us["sp500_chg_1d"] or 0.0
    nf_c = kr["night_fut_chg_1d"] or 0.0
    ewy_c = kr["ewy_chg_1d"] or 0.0
    wti_v = cm["wti"] or 0.0

    us_tone = "기술주와 반도체 중심의 뚜렷한 조정세" if (sox_c < -2.0 or sp_c < -1.0) else ("견조한 상승 흐름" if sp_c > 0.3 else "업종별 혼조세")  # hardcode-ok: spoken narrative
    kr_tone = "하방 압력을 방어해야 하는 경계 국면" if (nf_c < -1.0 or ewy_c < -1.5) else ("우호적인 반등 탐색 구간" if nf_c > 0.5 else "수급 공방이 예상되는 중립 탐색 구간")  # hardcode-ok: spoken narrative

    held_spoken = ", ".join(pf["held_names"][:6]) if pf["held_names"] else "현금 중심 포지션"

    dialogue: list[dict[str, str]] = [
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                f"안녕하세요, 프로젝트 메리디안 데일리 인텔리전스 브리프입니다. 오늘은 {date_str}, 아침 아홉 시 정각입니다. "
                f"오늘도 밤사이 글로벌 금융시장의 흐름과 한국, 미국, 중국, 일본, 유럽 5대 권역의 매크로 핵심 지표, "
                f"그리고 국제 원자재와 에너지 시장, 오늘 우리 증시 전망까지 수석 퀀트 전략가 서연 님과 함께 짚어보겠습니다. 서연 님, 안녕하세요."
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"안녕하세요, 민준 님. 아침 여덟 시 사십오 분 모닝 인텔리전스 파이프라인과 여덟 시 오십 분 동시호가 캘리브레이션이 모두 완료되었습니다. "
                f"오늘 시장을 관통하는 핵심 키워드는 한마디로 {us_tone}, 그리고 이에 대응하는 메리디안의 비대칭 리스크 방어입니다. "
                f"현재 글로벌 레짐은 {rg['global']}, 한국 증시 운용 레짐은 {rg['kr']} 단계로 설정되어 있습니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "네, 먼저 간밤에 마감된 미국 증시 결과부터 자세히 복기해 볼까요? "
                "3대 지수와 특히 우리 증시에 직접적인 영향을 주는 필라델피아 반도체 지수 움직임이 어땠습니까?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"네, 간밤 미국 증시에서 에스앤피 오백 지수는 {_spoken_num(us['sp500'], 2)}포인트로 {_spoken_chg(us['sp500_chg_1d'])}했고, "
                f"기술주 중심의 나스닥 지수는 {_spoken_num(us['nasdaq'], 2)}포인트로 {_spoken_chg(us['nasdaq_chg_1d'])}했습니다. "
                f"다우존스 지수는 {_spoken_num(us['dji'], 2)}포인트로 {_spoken_chg(us['dji_chg_1d'])}하며 상대적으로 낙폭이 작았는데요. "
                f"가장 주목해야 할 대목은 필라델피아 반도체 지수, SOX입니다. {_spoken_num(us['sox'], 1)}포인트로 하루 만에 {_spoken_chg(us['sox_chg_1d'])}하며 "
                f"AI 하드웨어와 반도체 섹터 전반에 강한 차익실현 매물이 출회되었습니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "반도체 지수의 변동폭이 상당히 컸군요. 옵션 시장의 미시구조나 공포지수 VIX에서는 어떤 시그널이 포착되었나요?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"월가의 공포지수로 불리는 빅스, VIX 지수는 {_spoken_num(us['vix'], 2)}포인트로 전일 대비 {_spoken_chg(us['vix_chg_1d'])}했습니다. "
                f"20일 이동평균인 {_spoken_num(us['vix_ma20'], 2)}포인트를 웃돌기 시작했다는 점이 중요합니다. "
                f"또한 옵션 시장의 풋콜 레이시오가 {_spoken_num(us['pcr'], 2)}를 기록했고, 블랙스완 꼬리위험을 보여주는 CBOE 스큐 스트레스 제트스코어가 "
                f"{_spoken_num(us['skew_z'], 2)} 시그마 수준까지 올라오면서 기관 투자자들의 하방 헤지 수요가 뚜렷하게 증가했습니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "그렇다면 이제 미국, 유럽, 일본, 중국, 한국까지 5대 권역의 매크로 키 인덱스와 금리, 환율 지형을 짚어보겠습니다. "
                "먼저 미국의 거시경제 지표와 국채 금리 흐름은 어떻습니까?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"미국 국채 10년물 금리는 {_spoken_num(us['us10y'], 2)}퍼센트, 30년물 금리는 {_spoken_num(us['us30y'], 2)}퍼센트를 기록 중이며, "
                f"10년물과 2년물 장단기 금리차는 {_spoken_num(us['yc_10y_2y'], 3)}퍼센트포인트로 플러스 영역에 자리 잡고 있습니다. "
                f"연준 기준금리가 {_spoken_num(us['fedfunds'], 2)}퍼센트, 실업률 {_spoken_num(us['unrate'], 1)}퍼센트, 경기선행지수 {_spoken_num(us['us_lei'], 2)}인 상황에서 "
                f"국채 10년물 금리가 4퍼센트 후반에서 5퍼센트 근방에 머물다 보니 성장주들의 밸류에이션 멀티플에 부담을 주고 있습니다. "
                f"달러인덱스 DXY는 {_spoken_num(us['dxy'], 2)}로 {_spoken_chg(us['dxy_chg_1d'])}하며 높은 수준을 유지 중입니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "미국 금리와 달러가 높은 수준을 유지하고 있군요. 그렇다면 유럽과 일본, 중국 등 다른 주요 경제권의 매크로 상황은 어떻게 연동되고 있나요?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"네, 먼저 유럽부터 보면 유로 달러 환율은 {_spoken_num(us['dxy'] and eu['eurusd'], 4)}달러로 {_spoken_chg(eu['eurusd_chg_1d'])}했습니다. "
                f"메리디안이 추적하는 유럽 은행권 스트레스 지수가 {_spoken_num(eu['eu_bank_z'], 2)} 시그마, 유럽 지정학 및 정치 불안정성 지수가 "
                f"{_spoken_num(eu['eu_instab_z'], 2)} 시그마로 나타나 유로존의 경기 회복 탄력이 미국 대비 상대적으로 둔화된 모습입니다. "
                f"반면 아시아로 넘어오면 일본과 중국의 흐름이 사뭇 다릅니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "일본과 중국이 어떻게 다른 흐름을 보이고 있는지 구체적인 지수로 설명해 주시죠."
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"일본 닛케이 이이오 지수는 {_spoken_num(jp['nikkei'], 0)}포인트로 {_spoken_chg(jp['nikkei_chg_1d'])}했고, "
                f"엔 달러 환율은 달러당 {_spoken_num(jp['usdjpy'], 2)}엔을 기록 중입니다. 대만 가권지수도 {_spoken_chg(jp['taiex_chg_1d'])}하며 "
                f"미국 반도체 조정의 영향을 함께 받았습니다. "
                f"반면 중국과 홍콩 시장은 홍콩 항셍지수가 {_spoken_num(cn['hangseng'], 0)}포인트로 {_spoken_chg(cn['hangseng_chg_1d'])}, "
                f"중국 대형주 ETF인 FXI가 {_spoken_chg(cn['fxi_chg_1d'])}하며 미국 기술주와 탈동조화된 반등을 보였습니다. "
                f"다만 중국의 경기침체 스트레스 지수가 {_spoken_num(cn['china_stress_z'], 2)} 시그마로 여전히 높기 때문에 추세적 전환보다는 밸류에이션 매력이 부각된 순환매로 해석해야 합니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "5대 권역의 자금 흐름이 한눈에 그려집니다. 이어서 국제 상품시장과 에너지 시장 동향도 살펴보겠습니다. "
                "최근 유가와 금, 구리 가격의 움직임이 인플레이션과 실물 경기에 어떤 신호를 주고 있습니까?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"네, 에너지 시장이 오늘 매크로 브리프에서 매우 중요한 축입니다. 서부텍사스산 원유, WTI 선물 가격이 배럴당 {_spoken_num(cm['wti'], 2)}달러로 "
                f"전일 대비 {_spoken_chg(cm['wti_chg_1d'])}, 최근 한 달 기준으로는 무려 {_spoken_chg(cm['wti_chg_1m'])}했습니다. "
                f"유가가 배럴당 {int(round(wti_v))}달러 선에서 움직이면서 기대 인플레이션을 자극하고 있고, 이것이 앞서 말씀드린 미국 국채 금리 하방을 막는 핵심 원인이 되고 있습니다. "
                f"실제로 우리 포트폴리오에서도 미국 에너지 섹터 ETF인 XLE가 견조한 수익을 내며 기술주 조정을 방어해 주고 있습니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "에너지 가격이 인플레이션 경로를 통해 금리와 기술주에까지 영향을 미치고 있군요. 금과 은, 그리고 실물 경기의 바로미터인 구리 가격은 어떻습니까?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"국제 금 가격은 온스당 {_spoken_num(cm['gold'], 1)}달러로 하루 동안 {_spoken_chg(cm['gold_chg_1d'])}, "
                f"은 가격은 온스당 {_spoken_num(cm['silver'], 2)}달러로 {_spoken_chg(cm['silver_chg_1d'])}하며 단기 숨고르기를 보였습니다. "
                f"주목할 점은 구리 가격인데요, 파운드당 {_spoken_num(cm['copper'], 2)}달러로 {_spoken_chg(cm['copper_chg_1d'])}했습니다. "
                f"금 가격을 구리 가격으로 나눈 금 구리 비율이 현재 {_spoken_num(cm['gold_copper_ratio'], 0)}배 수준인데, "
                f"이는 글로벌 자금이 공격적인 제조업 경기 확장보다는 안전자산과 인플레이션 헤지에 더 높은 프리미엄을 부여하고 있음을 뜻합니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "이제 가장 중요한 오늘 한국 증시 전망으로 넘어오겠습니다. "
                "직전 거래일 우리 시장 상황과 밤사이 야간선물, 그리고 오늘 아홉 시 정각 기준 메리디안의 한국 증시 예측 결과는 어떻습니까?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"직전 거래일 코스피는 {_spoken_num(kr['kospi'], 2)}포인트로 {_spoken_chg(kr['kospi_chg_1d'])}, "
                f"코스닥은 {_spoken_num(kr['kosdaq'], 2)}포인트로 {_spoken_chg(kr['kosdaq_chg_1d'])} 마감했습니다. "
                f"원 달러 환율은 달러당 {_spoken_num(kr['usdkrw'], 1)}원으로 {_spoken_chg(kr['usdkrw_chg_1d'])}하며 비교적 안정적인 흐름을 유지했는데요. "
                f"하지만 밤사이 미국 시장에서 한국 증시 선행지표인 MSCI 한국 ETF, EWY가 {_spoken_chg(kr['ewy_chg_1d'])}했고, "
                f"코스피 이백 야간선물도 {_spoken_num(kr['night_fut'], 1)}포인트로 {_spoken_chg(kr['night_fut_chg_1d'])}했습니다. "
                f"이에 따라 오늘 아침 산출된 메리디안 야간 종합 지수, OIS는 100점 만점에 {_spoken_num(rg['ois'], 1)}점을 기록해 {kr_tone}으로 판정되었습니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                "그렇다면 이런 시장 환경 속에서 오늘 프로젝트 메리디안 실계좌 포트폴리오는 어떻게 대응하고 있나요?"
            ),
        },
        {
            "speaker": "서연 (Chief Quant Strategist)",
            "voice": VOICE_STRATEGIST_FEMALE,
            "text": (
                f"현재 메리디안 실계좌의 체결기준 순자산은 약 {_spoken_num(pf['nav_krw'], 0, '원')}이며, "
                f"이미 선제적으로 강력한 양방향 방어 진용을 구축해 두었습니다. "
                f"현재 보유 중인 {pf['holding_count']}개 종목을 보면, {held_spoken} 등으로 구성되어 있는데요. "
                f"국내 시장에서는 코덱스 인버스와 코덱스 이차전지산업 페어 포지션, 그리고 현대글로비스, 기아, 오리온 같은 저평가 고배당 가치주와 CD금리 액티브가 하방을 단단히 지지하고 있고, "
                f"미국 시장에서도 일라이 릴리, 비자, 에너지 섹터 XLE, 헬스케어 XLV와 함께 에스앤피 오백 마이너스 일배 인버스인 SH를 페어로 배치해 기술주 급락 충격을 효과적으로 흡수하고 있습니다. "
                f"오늘 장중에도 성급한 추격 매수는 철저히 차단하고, 오후 세 시 십 분 장중 컷오프까지 외국인 수급과 휩소 방어 지표를 실시간 감시하며 안정적인 복리 운용을 이어가겠습니다."
            ),
        },
        {
            "speaker": "민준 (Host)",
            "voice": VOICE_HOST_MALE,
            "text": (
                f"네, 오늘 글로벌 5대 권역 매크로 동향부터 원자재, 에너지 시장, 그리고 한국 증시 대응 전략까지 명쾌하게 정리해 주셨습니다. "
                f"상세한 수치와 표가 담긴 브리프 원문은 함께 생성된 문서에서 바로 확인하실 수 있습니다. "
                f"오늘도 성공적인 투자 되시길 바라며, 이상으로 {date_str} 프로젝트 메리디안 데일리 브리프를 마칩니다. 감사합니다."
            ),
        },
    ]
    return dialogue


def format_dialogue_transcript(dialogue: list[dict[str, str]], date_str: str) -> str:
    """2인 대담 스크립트를 읽기 좋은 텍스트 원고로 포맷팅합니다."""
    lines = [
        f"# [Project Meridian] NotebookLM 2인 대담 오디오 브리프 대본 ({date_str})",
        "",
    ]
    for idx, turn in enumerate(dialogue, 1):
        lines.append(f"**[{idx:02d}] {turn['speaker']}**: {turn['text']}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# Valid MPEG-1 Layer 3 (24kHz, 48kbps, Mono) silent frame (144 bytes per 24ms frame)
# Used to insert natural ~360ms breathing pauses between speaker turns without requiring external ffmpeg
_SILENT_MP3_FRAME_24K_48KBPS = bytes([
    0xFF, 0xF3, 0x64, 0xC4, 0x00, 0x00, 0x00, 0x03, 0x48, 0x00, 0x00, 0x00,
    0x00, 0x4C, 0x41, 0x4D, 0x45, 0x33, 0x2E, 0x31, 0x30, 0x30,
] + [0x55] * 122)


async def _synthesize_turn_bytes(text: str, voice: str, rate: str = "+4%") -> bytes:
    import edge_tts

    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buf.extend(chunk["data"])
    return bytes(buf)


async def synthesize_two_host_audio_mp3_async(
    dialogue: list[dict[str, str]],
    output_mp3_path: str | Path,
) -> Path:
    """2인 대담 스크립트를 한국어 뉴럴 음성(민준 & 서연)으로 합성하여 단일 MP3 파일을 생성합니다."""
    out_path = Path(output_mp3_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pause_bytes = _SILENT_MP3_FRAME_24K_48KBPS * 14  # ~336ms natural pause between speakers
    combined = bytearray()

    for idx, turn in enumerate(dialogue):
        voice = turn.get("voice") or VOICE_HOST_MALE
        rate = "+4%" if voice == VOICE_HOST_MALE else "+5%"
        audio_bytes = await _synthesize_turn_bytes(turn["text"], voice=voice, rate=rate)
        if audio_bytes:
            if combined:
                combined.extend(pause_bytes)
            combined.extend(audio_bytes)
        logger.debug(f"  🎙️ Synthesized turn {idx+1}/{len(dialogue)} ({turn['speaker']}, {len(audio_bytes)} bytes)")

    tmp_path = out_path.with_suffix(".mp3.tmp")
    tmp_path.write_bytes(bytes(combined))
    os.replace(tmp_path, out_path)
    logger.info(f"✅ [Audio Brief] 2인 대담 MP3 생성 완료: {out_path} ({len(combined)/1024:.1f} KB)")
    return out_path


def synthesize_two_host_audio_mp3(
    dialogue: list[dict[str, str]],
    output_mp3_path: str | Path,
) -> Path:
    """동기 컨텍스트에서 2인 대담 MP3를 합성합니다."""
    return asyncio.run(synthesize_two_host_audio_mp3_async(dialogue, output_mp3_path))


def _md_inline_to_rl_xml(text: str) -> str:
    """Markdown 인라인 문법(**bold**, `code`)을 ReportLab Paragraph 호환 XML 태그로 변환합니다."""
    import html
    import re

    escaped = html.escape(str(text), quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`]+)`", r'<font color="#0f4c81"><b>\1</b></font>', escaped)
    return escaped


def render_daily_brief_pdf(
    markdown_text: str,
    output_pdf_path: str | Path,
    report_date: str = "",
) -> Path:
    """Markdown 데일리 브리프(표·본문·대담 스크립트)를 한글 폰트(NanumGothic)가 적용된 고품질 A4 PDF로 렌더링합니다."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    out_path = Path(output_pdf_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    project_root = Path(__file__).resolve().parent.parent.parent
    font_dir = project_root / "assets" / "fonts"
    reg_font = font_dir / "NanumGothic-Regular.ttf"
    bold_font = font_dir / "NanumGothic-Bold.ttf"

    font_name = "Helvetica"
    bold_font_name = "Helvetica-Bold"
    if reg_font.exists():
        pdfmetrics.registerFont(TTFont("NanumGothic", str(reg_font)))
        font_name = "NanumGothic"
        if bold_font.exists():
            pdfmetrics.registerFont(TTFont("NanumGothic-Bold", str(bold_font)))
            bold_font_name = "NanumGothic-Bold"
        else:
            bold_font_name = "NanumGothic"
        registerFontFamily(
            "NanumGothic",
            normal=font_name,
            bold=bold_font_name,
            italic=font_name,
            boldItalic=bold_font_name,
        )

    base_styles = getSampleStyleSheet()
    st_h1 = ParagraphStyle(
        name="BriefH1",
        parent=base_styles["Heading1"],
        fontName=bold_font_name,
        fontSize=15,
        leading=20,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=6,
        spaceAfter=8,
    )
    st_h2 = ParagraphStyle(
        name="BriefH2",
        parent=base_styles["Heading2"],
        fontName=bold_font_name,
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#1e3a8a"),
        spaceBefore=12,
        spaceAfter=6,
    )
    st_h3 = ParagraphStyle(
        name="BriefH3",
        parent=base_styles["Heading3"],
        fontName=bold_font_name,
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#1e293b"),
        spaceBefore=8,
        spaceAfter=4,
    )
    st_body = ParagraphStyle(
        name="BriefBody",
        parent=base_styles["BodyText"],
        fontName=font_name,
        fontSize=9.2,
        leading=13.8,
        textColor=colors.HexColor("#1e293b"),
        alignment=TA_LEFT,
        spaceBefore=2,
        spaceAfter=3,
    )
    st_bullet = ParagraphStyle(
        name="BriefBullet",
        parent=st_body,
        leftIndent=12,
        firstLineIndent=-8,
        spaceBefore=2,
        spaceAfter=3,
    )
    st_subbullet = ParagraphStyle(
        name="BriefSubBullet",
        parent=st_body,
        leftIndent=24,
        firstLineIndent=-8,
        spaceBefore=1.5,
        spaceAfter=2,
    )
    st_quote = ParagraphStyle(
        name="BriefQuote",
        parent=st_body,
        fontSize=8.8,
        leading=13,
        leftIndent=10,
        rightIndent=10,
        backColor=colors.HexColor("#f1f5f9"),
        borderPadding=6,
        textColor=colors.HexColor("#334155"),
        spaceBefore=4,
        spaceAfter=6,
    )
    st_th = ParagraphStyle(
        name="BriefTH",
        parent=st_body,
        fontName=bold_font_name,
        fontSize=8.3,
        leading=11,
        textColor=colors.white,
    )
    st_td = ParagraphStyle(
        name="BriefTD",
        parent=st_body,
        fontName=font_name,
        fontSize=8.1,
        leading=11,
        textColor=colors.HexColor("#0f172a"),
    )

    page_w = A4[0] - 56.0  # 28pt left/right margins -> ~539pt usable width
    story: list[Any] = []
    lines = markdown_text.splitlines()
    idx = 0
    n_lines = len(lines)

    while idx < n_lines:
        raw_line = lines[idx]
        stripped = raw_line.strip()

        if not stripped:
            idx += 1
            continue

        # Markdown Table Block
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines: list[str] = []
            while idx < n_lines and lines[idx].strip().startswith("|") and lines[idx].strip().endswith("|"):
                table_lines.append(lines[idx].strip())
                idx += 1

            parsed_rows: list[list[str]] = []
            for r_i, tline in enumerate(table_lines):
                cells = [c.strip() for c in tline.strip("|").split("|")]
                is_sep = r_i == 1 and all(set(c) <= {":", "-", " "} for c in cells if c)
                if is_sep:
                    continue
                parsed_rows.append(cells)

            if parsed_rows:
                n_cols = max(len(r) for r in parsed_rows)
                _is_5col = n_cols == 5  # hardcode-ok: 5-column table layout check
                if _is_5col:
                    col_widths = [page_w * 0.14, page_w * 0.21, page_w * 0.14, page_w * 0.12, page_w * 0.39]
                else:
                    col_widths = [page_w / float(n_cols)] * n_cols

                rl_table_data: list[list[Any]] = []
                for r_idx, row_cells in enumerate(parsed_rows):
                    padded = (row_cells + [""] * n_cols)[:n_cols]
                    cell_style = st_th if r_idx == 0 else st_td
                    rl_table_data.append([Paragraph(_md_inline_to_rl_xml(c), cell_style) for c in padded])

                tbl = Table(rl_table_data, colWidths=col_widths, repeatRows=1)
                tbl.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                            ("LEFTPADDING", (0, 0), (-1, -1), 5),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                        ]
                    )
                )
                story.append(Spacer(1, 4))
                story.append(tbl)
                story.append(Spacer(1, 6))
            continue

        if stripped == "---":
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cbd5e1")))
            story.append(Spacer(1, 4))
            idx += 1
            continue

        if stripped.startswith("# "):
            story.append(Paragraph(_md_inline_to_rl_xml(stripped[2:].strip()), st_h1))
            idx += 1
            continue

        if stripped.startswith("## "):
            story.append(Paragraph(_md_inline_to_rl_xml(stripped[3:].strip()), st_h2))
            idx += 1
            continue

        if stripped.startswith("### "):
            story.append(Paragraph(_md_inline_to_rl_xml(stripped[4:].strip()), st_h3))
            idx += 1
            continue

        if stripped.startswith("> "):
            story.append(Paragraph(_md_inline_to_rl_xml(stripped[2:].strip()), st_quote))
            idx += 1
            continue

        if raw_line.startswith("  - "):
            story.append(Paragraph("◦ " + _md_inline_to_rl_xml(stripped[2:].strip()), st_subbullet))
            idx += 1
            continue

        if stripped.startswith("- "):
            story.append(Paragraph("• " + _md_inline_to_rl_xml(stripped[2:].strip()), st_bullet))
            idx += 1
            continue

        story.append(Paragraph(_md_inline_to_rl_xml(stripped), st_body))
        idx += 1

    tmp_pdf = out_path.with_suffix(".pdf.tmp")
    doc = SimpleDocTemplate(
        str(tmp_pdf),
        pagesize=A4,
        leftMargin=28,
        rightMargin=28,
        topMargin=32,
        bottomMargin=32,
        title=f"Project Meridian Daily Brief ({report_date})",
        author="Project Meridian AI Quantitative Strategy Group",
    )
    doc.build(story)
    os.replace(tmp_pdf, out_path)
    logger.info(f"✅ [Daily Brief PDF] 생성 완료: {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
    return out_path


def upsert_brief_history_db(
    db_path: str | Path,
    snap: dict[str, Any],
    brief_md: str,
    dialogue_txt: str,
) -> list[dict[str, Any]]:
    """일별 스냅샷과 브리프 원문을 시계열 JSONL 데이터베이스(brief_history_db.jsonl)에 멱등(Idempotent) 누적합니다."""
    import json

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    by_date: dict[str, dict[str, Any]] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                d_key = str(rec.get("date") or "")
                if d_key:
                    by_date[d_key] = rec
            except Exception:
                continue

    date_str = str(snap.get("date") or datetime.now().strftime("%Y-%m-%d"))
    by_date[date_str] = {
        "date": date_str,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S KST"),
        "snapshot": snap,
        "brief_md": brief_md,
        "dialogue_txt": dialogue_txt,
    }

    sorted_entries = [by_date[k] for k in sorted(by_date.keys(), reverse=True)]
    tmp_db = path.with_suffix(".jsonl.tmp")
    tmp_db.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in sorted_entries) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_db, path)
    logger.info(f"✅ [Master DB] 시계열 DB 누적 완료: 총 {len(sorted_entries)}일 기록 ({path})")
    return sorted_entries


def build_master_db_markdown(entries: list[dict[str, Any]]) -> str:
    """NotebookLM 심층 질의응답 및 시계열 재분석을 위한 3단 구조 누적 마스터 DB 문서를 생성합니다."""
    if not entries:
        return "# [Project Meridian] NotebookLM Master Database (Empty)\n"

    latest = entries[0]
    latest_date = latest.get("date", "")
    total_days = len(entries)

    rows_2a: list[str] = []
    rows_2b: list[str] = []
    rows_2c: list[str] = []

    for rec in entries:
        d_str = rec.get("date", "N/A")
        sn = rec.get("snapshot", {})
        rg = sn.get("regimes", {})
        us = sn.get("us", {})
        kr = sn.get("kr", {})
        cn = sn.get("cn", {})
        jp = sn.get("jp", {})
        eu = sn.get("eu", {})
        cm = sn.get("commodities", {})
        pf = sn.get("portfolio", {})

        rows_2a.append(
            f"| **{d_str}** | `{str(rg.get('global', '')).upper()}/{str(rg.get('kr', '')).upper()}` (OIS `{_fmt_num(rg.get('ois'), 1)}`) "
            f"| `{_fmt_num(us.get('sp500'))}` (`{_fmt_chg(us.get('sp500_chg_1d'))}`) / `{_fmt_num(us.get('nasdaq'))}` (`{_fmt_chg(us.get('nasdaq_chg_1d'))}`) "
            f"| `{_fmt_num(us.get('sox'))}` (`{_fmt_chg(us.get('sox_chg_1d'))}`) / VIX `{_fmt_num(us.get('vix'))}` "
            f"| 항셍 `{_fmt_num(cn.get('hangseng'))}` (`{_fmt_chg(cn.get('hangseng_chg_1d'))}`) / 닛케이 `{_fmt_num(jp.get('nikkei'))}` (`{_fmt_chg(jp.get('nikkei_chg_1d'))}`) "
            f"| 코스피 `{_fmt_num(kr.get('kospi'))}` (`{_fmt_chg(kr.get('kospi_chg_1d'))}`) / 코스닥 `{_fmt_num(kr.get('kosdaq'))}` |"
        )

        rows_2b.append(
            f"| **{d_str}** | 미10Y `{_fmt_num(us.get('us10y'))}%` / 10-2Y `{_fmt_num(us.get('yc_10y_2y'), 3)}%p` "
            f"| DXY `{_fmt_num(us.get('dxy'))}` / EUR `{_fmt_num(eu.get('eurusd'), 4)}` "
            f"| KRW `{_fmt_num(kr.get('usdkrw'), 1)}` / JPY `{_fmt_num(jp.get('usdjpy'), 2)}` "
            f"| GEX `{_fmt_num(us.get('gex'), 0)}` / PCR `{_fmt_num(us.get('pcr'))}` / Skew `{_fmt_num(us.get('skew_z'))}σ` "
            f"| 야간선물 `{_fmt_num(kr.get('night_fut'))}` (`{_fmt_chg(kr.get('night_fut_chg_1d'))}`) / EWY `{_fmt_chg(kr.get('ewy_chg_1d'))}` |"
        )

        rows_2c.append(
            f"| **{d_str}** | WTI `${_fmt_num(cm.get('wti'))}` (`{_fmt_chg(cm.get('wti_chg_1d'))}`) / GSCPI `{_fmt_num(cm.get('gscpi_z'))}σ` "
            f"| 금 `${_fmt_num(cm.get('gold'), 1)}` (`{_fmt_chg(cm.get('gold_chg_1d'))}`) / 은 `${_fmt_num(cm.get('silver'))}` "
            f"| 구리 `${_fmt_num(cm.get('copper'), 3)}` / 금·구리 `{_fmt_num(cm.get('gold_copper_ratio'), 1)}배` "
            f"| G `{_fmt_num(rg.get('global_stress_z'))}` / KR `{_fmt_num(kr.get('kr_stress_z'))}` / CN `{_fmt_num(cn.get('cn_stress_z'))}` / EU `{_fmt_num(eu.get('eu_bank_stress_z'))}` "
            f"| `₩{_fmt_num(pf.get('nav_krw'), 0)}` (`{pf.get('holding_count', 0)}종목`) |"
        )

    parts: list[str] = [
        f"# [Part 1] 최신일 ({latest_date}) 데일리 인텔리전스 브리프 & 2인 대담",
        f"> **NotebookLM 마스터 지식베이스 안내**: 본 문서는 최신일(`{latest_date}`) 브리프(Part 1)와 누적 `{total_days}일` 시계열 정형 데이터베이스(Part 2), 그리고 과거 일자별 브리프 아카이브(Part 3)가 통합된 단일 마스터 DB입니다.",
        "",
        latest.get("brief_md", "").strip(),
        "",
        "---",
        "",
        latest.get("dialogue_txt", "").strip(),
        "",
        "---",
        "",
        f"# [Part 2] NotebookLM 시계열 정형 데이터베이스 (누적 {total_days}일 Master Time-Series DB)",
        "- **질의 활용 가이드**: NotebookLM에서 날짜 간 지수·금리·환율·원자재·레짐·옵션 수급 변화를 비교하거나 특정 조건(예: WTI 급등일, SOX 급락일, OIS 약세일)을 필터링해 재분석할 때 아래 3개 시계열 마스터 테이블이 직접 참조됩니다.",
        "",
        "### 표 2-A. 글로벌 레짐 & 5대 권역 주가지수 시계열 DB",
        "| 일자 (Date) | 글로벌/KR 레짐 (OIS) | 미국 S&P 500 / NASDAQ | 미국 SOX 반도체 / VIX | 중국 항셍 / 일본 닛케이 | 한국 코스피 / 코스닥 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        "\n".join(rows_2a),
        "",
        "### 표 2-B. 금리 · 환율 · 미 옵션 미시구조 · 한국장 선행지표 시계열 DB",
        "| 일자 (Date) | 미 10Y 금리 / 장단기차 | 달러(DXY) / 유로(EUR) | 원/달러 / 엔/달러 환율 | 미국 옵션 GEX / PCR / Skew | 한국 야간선물 / EWY |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        "\n".join(rows_2b),
        "",
        "### 표 2-C. 국제 원자재 · 에너지 · 권역별 스트레스 Z · 실계좌 NAV 시계열 DB",
        "| 일자 (Date) | WTI 원유 / 공급망(GSCPI) | 국제 금(Gold) / 은(Silver) | 구리(Copper) / 금·구리 비율 | 매크로 Stress Z (G/KR/CN/EU) | 실계좌 체결NAV / 보유종목 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
        "\n".join(rows_2c),
    ]

    _has_archive = len(entries) > 1  # hardcode-ok: archive existence check
    if _has_archive:
        parts.extend([
            "",
            "---",
            "",
            f"# [Part 3] 과거 일자별 데일리 브리프 누적 아카이브 ({len(entries) - 1}일분)",
        ])
        for past in entries[1:]:
            p_date = past.get("date", "")
            parts.extend([
                "",
                f"## [Archive: {p_date}] 데일리 브리프 기록",
                past.get("brief_md", "").strip(),
                "",
                "---",
            ])

    return "\n".join(parts).strip() + "\n"



def sync_brief_to_google_drive_notebooklm(
    markdown_text: str,
    report_date: str,
    folder_id: str = NOTEBOOKLM_DRIVE_FOLDER_ID,
) -> dict[str, Any]:
    """Google Drive 내 공유된 'Meridian_NotebookLM_Daily_Brief' 폴더에 사용자 소유 문서가 있으면 최신 브리프로 자동 갱신합니다."""
    sa_file = None
    for p in SERVICE_ACCOUNT_PATHS:
        if os.path.exists(p):
            sa_file = p
            break
    if not sa_file:
        return {"synced": False, "reason": "service_account_missing"}

    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            sa_file,
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/documents",
            ],
        )
        session = AuthorizedSession(creds)

        # Look for any Google Doc or text/markdown file in the NotebookLM folder or shared with service account
        q = (
            f"('{folder_id}' in parents or name contains 'Meridian Daily Brief' or name contains ' 데일리 브리프') "
            f"and trashed = false and mimeType != 'application/vnd.google-apps.folder' "
            f"and mimeType != 'application/vnd.google-apps.spreadsheet'"
        )
        resp = session.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": q, "pageSize": 10, "fields": "files(id,name,mimeType,webViewLink)"},
            timeout=15,
        )
        files = resp.json().get("files", []) if resp.status_code == 200 else []
        seen_ids = {f.get("id") for f in files if f.get("id")}
        if NOTEBOOKLM_MASTER_DOC_ID and NOTEBOOKLM_MASTER_DOC_ID not in seen_ids:
            files.insert(
                0,
                {
                    "id": NOTEBOOKLM_MASTER_DOC_ID,
                    "name": "Meridian Daily Brief",
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": f"https://docs.google.com/document/d/{NOTEBOOKLM_MASTER_DOC_ID}/edit",
                },
            )
        updated_files: list[dict[str, str]] = []

        for f in files:
            fid = f.get("id")
            mime = f.get("mimeType", "")
            if not fid:
                continue
            if mime == "application/vnd.google-apps.document":
                # Replace Google Doc content via Drive multipart upload conversion or Docs API
                up_resp = session.patch(
                    f"https://www.googleapis.com/upload/drive/v3/files/{fid}",
                    params={"uploadType": "media"},
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    data=markdown_text.encode("utf-8"),
                    timeout=20,
                )
                _ok_up = up_resp.status_code == 200  # hardcode-ok: HTTP 200 status check
                if _ok_up:
                    updated_files.append({"id": fid, "name": f.get("name", ""), "link": f.get("webViewLink", "")})
            else:
                up_resp = session.patch(
                    f"https://www.googleapis.com/upload/drive/v3/files/{fid}",
                    params={"uploadType": "media"},
                    headers={"Content-Type": "text/markdown; charset=utf-8"},
                    data=markdown_text.encode("utf-8"),
                    timeout=20,
                )
                _ok_up = up_resp.status_code == 200  # hardcode-ok: HTTP 200 status check
                if _ok_up:
                    updated_files.append({"id": fid, "name": f.get("name", ""), "link": f.get("webViewLink", "")})

        return {
            "synced": bool(updated_files),
            "folder_id": folder_id,
            "folder_url": f"https://drive.google.com/drive/folders/{folder_id}",
            "updated_files": updated_files,
        }
    except Exception as e:
        logger.warning(f"  ⚠️ [NotebookLM Drive Sync] 예외: {e}")
        return {"synced": False, "reason": str(e)}

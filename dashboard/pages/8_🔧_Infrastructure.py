#!/usr/bin/env python3
"""
Page 8: Infrastructure — 파이프라인 인프라 상태
# [SSOT Refactoring] health_check.json / pipeline_state.json SSoT만 읽음. 자체 판단 없음.
# [Live Polling] 10초 자동 새로고침.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
import plotly.express as px

_PAGES_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PAGES_DIR.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.utils.data_loader import (
    setup_live_polling, inject_common_css,
    load_json, safe_float, safe_fmt, RESULTS_DIR,
    get_launchd_status
)

_refresh_count = setup_live_polling(interval_ms=10_000, key="infra_refresh")
inject_common_css()

st.markdown(
    "<div class='main-header'><h1>🔧 Infrastructure</h1>"
    "<p>파이프라인 상태 · API 헬스 · 데이터 신선도 (health_check.json SSoT)</p></div>",
    unsafe_allow_html=True,
)

# [SSOT Refactoring] 인프라 데이터 로드 — data_loader.py 경유
health = load_json("health_check.json")
pipeline_chk = load_json("pipeline_checkpoint.json")
api_health = load_json("api_health_status.json")
data_health = load_json("data_health.json")
coll_status = load_json("data_collection_status.json")
watchdog = load_json("watchdog_heartbeat.json")
backup = load_json("backup_status.json")

# ── 전체 시스템 상태 ─────────────────────────────────────────────────────────
st.subheader("🔴🟡🟢 시스템 상태 (health_check.json SSoT)")

_overall = str(health.get("overall") or health.get("status") or "N/A")
_status_cls = {"healthy": "success", "ok": "success", "warning": "warning", "error": "error"}.get(
    _overall.lower(), "info"
)
getattr(st, _status_cls, st.info)(f"전체 상태: **{_overall}**")

# ── 파이프라인 단계별 상태 ────────────────────────────────────────────────────
st.subheader("⚙️ Pipeline Execution Timeline (pipeline_checkpoint.json + Legacy SSoT)")

_phases_data = []

if pipeline_chk:
    _phases = pipeline_chk.get("phases", {})
    if isinstance(_phases, dict) and _phases:
        for stage, info in _phases.items():
            if isinstance(info, dict):
                _start = info.get("started_at")
                _end = info.get("completed_at")
                _status = info.get("status", "N/A")
                _dur = info.get("duration_sec", 0.0)
                if _start and _end:
                    _phases_data.append({
                        "Phase": stage.upper(),
                        "Start": _start,
                        "Finish": _end,
                        "Duration (s)": str(round(safe_float(_dur), 1)),
                        "Status": "✅" if _status == "done" else ("❌" if _status == "failed" else _status)
                    })

# [Fallback] 파일 갱신 시간 기반 L1~L6 (이전 버전 호환성)
_legacy_phases = {
    "L1_Data_Collection": "data_collection_status.json",
    "L2_Feature_Engineering": "signal_cache.json",
    "L3_Signal_Generation": "latest_signals.json",
    "L4_Portfolio_Construction": "shadow_portfolio.json",
    "L5_Execution_Monitoring": "measurement_engine.json",
    "L6_Live_Order": "s6b_execution_results.json"
}

for p_name, f_name in _legacy_phases.items():
    if any(d["Phase"] == p_name for d in _phases_data):
        continue
    fpath = RESULTS_DIR / f_name
    if fpath.exists():
        _mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
        _phases_data.append({
            "Phase": p_name,
            "Start": _mtime.strftime("%Y-%m-%dT%H:%M:%S"),
            "Finish": _mtime.strftime("%Y-%m-%dT%H:%M:%S"),
            "Duration (s)": "N/A",
            "Status": "✅"
        })

if _phases_data:
    df_tl = pd.DataFrame(_phases_data)
    df_tl = df_tl.sort_values(by="Finish", ascending=True).reset_index(drop=True)
    st.dataframe(df_tl, hide_index=True, use_container_width=True)
else:
    st.info("파이프라인 구간 데이터를 찾을 수 없습니다.")

st.markdown("<br>", unsafe_allow_html=True)
with st.expander("🕒 Launchd 등록 스케줄 및 상태 (Background Tasks)"):
    _launchd_jobs = get_launchd_status()
    if _launchd_jobs:
        st.dataframe(pd.DataFrame(_launchd_jobs), hide_index=True, use_container_width=True)
    else:
        st.info("Launchd에 등록된 com.meridian.* 작업이 없습니다.")

st.markdown("---")

# ── API 헬스 ─────────────────────────────────────────────────────────────────
if api_health:
    st.subheader("🔗 API Health (api_health_status.json SSoT)")
    _api_services = api_health.get("services") or api_health.get("apis")
    if not _api_services:
        _api_services = {"KIS API": api_health}

    if isinstance(_api_services, dict):
        _api_rows = []
        for svc, info in _api_services.items():
            if isinstance(info, dict):
                _ok = bool(info.get("healthy") or info.get("ok") or info.get("status", "").lower() == "ok")
                _api_rows.append({
                    "서비스": svc,
                    "상태": "✅ OK" if _ok else "🔴 FAIL",
                    "Latency": f"{safe_float(info.get('latency_ms')):.0f}ms" if info.get("latency_ms") else "N/A",
                    "Updated": str(info.get("timestamp") or "")[:19],
                    "Message": str(info.get("message") or "")
                })
            else:
                _api_rows.append({"서비스": svc, "상태": str(info), "Latency": "N/A", "Updated": "", "Message": ""})
        if _api_rows:
            st.dataframe(pd.DataFrame(_api_rows), hide_index=True, use_container_width=True)

st.markdown("---")

# ── 데이터 신선도 ────────────────────────────────────────────────────────────
st.subheader("📅 데이터 신선도 (data_health.json SSoT)")

_now = datetime.now()
_freshness_rows = []

# [SSOT Refactoring] 신선도 판단은 data_health.json에서 읽음 — 재계산 없음
if data_health:
    _dh_items = data_health.get("items") or data_health.get("freshness", {})
    if isinstance(_dh_items, list):
        for item in _dh_items:
            _freshness_rows.append({
                "데이터": str(item.get("name") or item.get("key") or ""),
                "갱신시각": str(item.get("updated") or item.get("timestamp") or "")[:19],
                "신선도": str(item.get("status") or item.get("freshness") or ""),
                "경과(시간)": safe_fmt(item.get("age_hours"), ".1f"),
            })
    elif isinstance(_dh_items, dict):
        for k, v in _dh_items.items():
            if isinstance(v, dict):
                _freshness_rows.append({
                    "데이터": k,
                    "갱신시각": str(v.get("updated") or v.get("timestamp") or "")[:19],
                    "신선도": str(v.get("status") or ""),
                    "경과(시간)": safe_fmt(v.get("age_hours"), ".1f"),
                })

# 주요 results 파일 신선도 직접 체크 (Fallback)
if not _freshness_rows:
    files_to_check = [
        "shadow_summary.json", "signal_cache.json", "stream_metrics.json",
        "shadow_portfolio.json", "kis_portfolio.json", "measurement_engine.json", "kill_switch.json",
        "tca_summary.json", "advisory_orders.json", "s6a_execution_enter.json"
    ]
    for fname in files_to_check:
        fpath = RESULTS_DIR / fname
        if fpath.exists():
            _mtime = datetime.fromtimestamp(fpath.stat().st_mtime)
            _age_h = (_now - _mtime).total_seconds() / 3600
            
            # EOD(일 마감) 파일 분류
            is_eod = fname in ["shadow_summary.json", "stream_metrics.json", "measurement_engine.json"]
            
            if is_eod:
                # 일 마감 파일: 24시간 이내 Fresh, 주말 포함 72시간 이내 Stale
                _fresh = "✅ Fresh" if _age_h < 24 else ("🟡 Stale" if _age_h < 72 else "🔴 Old")
            else:
                # Intraday 파일: 기존의 엄격한 기준 적용 (1시간 이내 Fresh)
                _fresh = "✅ Fresh" if _age_h < 1 else ("🟡 Stale" if _age_h < 8 else "🔴 Old")
                
            _freshness_rows.append({
                "파일": fname,
                "최종 수정": _mtime.strftime("%Y-%m-%d %H:%M:%S"),
                "경과(시간)": f"{_age_h:.1f}h",
                "상태": _fresh,
            })
        else:
            _freshness_rows.append({
                "파일": fname,
                "최종 수정": "N/A",
                "경과(시간)": "N/A",
                "상태": "⚪ Missing",
            })

if _freshness_rows:
    st.dataframe(pd.DataFrame(_freshness_rows), hide_index=True, use_container_width=True)

st.markdown("---")

# ── 데이터 수집 상태 ──────────────────────────────────────────────────────────
if coll_status:
    st.subheader("📥 데이터 수집 상태 (data_collection_status.json SSoT)")
    _coll_phases = coll_status.get("phases", {})
    if isinstance(_coll_phases, dict) and _coll_phases:
        _coll_rows = []
        for p, info in _coll_phases.items():
            if isinstance(info, dict):
                _coll_rows.append({
                    "Phase": p.upper(),
                    "상태": info.get("status", ""),
                    "시작 시간": str(info.get("started_at") or "")[:19],
                    "완료 시간": str(info.get("completed_at") or "")[:19],
                    "경과(초)": f"{safe_float(info.get('duration_sec')):.1f}",
                })
        if _coll_rows:
            st.dataframe(pd.DataFrame(_coll_rows), hide_index=True, use_container_width=True)
            
    _overall_coll = coll_status.get("overall", {})
    if _overall_coll:
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total Parquets", _overall_coll.get("total_kr_parquets", 0))
        with c2:
            st.metric("Fresh Count", _overall_coll.get("fresh_count", 0))
        with c3:
            st.metric("Success Rate", f"{safe_float(_overall_coll.get('success_rate')):.1%}")

# ── Watchdog ─────────────────────────────────────────────────────────────────
if watchdog:
    st.markdown("---")
    st.subheader("🐕 Watchdog (watchdog_heartbeat.json SSoT)")
    _wd_ts = str(watchdog.get("timestamp") or watchdog.get("last_heartbeat") or "")[:19]
    _wd_alive = bool(_wd_ts != "")
    _wd_issues = int(watchdog.get("n_issues", 0))
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Watchdog", "✅ Alive" if _wd_alive else "🔴 Dead")
    with c2:
        st.metric("Last Heartbeat", _wd_ts or "N/A")
    with c3:
        st.metric("Issues", _wd_issues)

# ── 백업 상태 ────────────────────────────────────────────────────────────────
if backup:
    st.markdown("---")
    st.subheader("💾 백업 상태 (backup_status.json SSoT)")
    _bk_ts = str(backup.get("last_backup") or backup.get("timestamp") or "")[:19]
    _bk_status = str(backup.get("status") or "N/A")
    st.metric("마지막 백업", _bk_ts or "N/A", delta=_bk_status)

st.caption(f"🟢 Live Polling 활성 — 10초 자동 새로고침 #{_refresh_count}")

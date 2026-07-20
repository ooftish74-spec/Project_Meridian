#!/usr/bin/env python3
"""
Page 5: Risk — 리스크 게이트 상태
# [SSOT Refactoring] kill_switch.json / drawdown_guard.json / exposure_orchestrator.json SSoT.
# [Live Polling] 10초 자동 새로고침.
# [Phase 18] SS-ETF 단일종목 파생 리스크 스캐너 추가.
# [Phase 18] mtime 기반 캐시 무효화 + 수동 캐시 클리어 버튼.
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

_PAGES_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PAGES_DIR.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.utils.data_loader import (
    setup_live_polling, inject_common_css,
    load_risk_data, load_json, safe_float, safe_fmt,
    render_cache_clear_button,
    load_ss_etf_risk, _ss_etf_level, _get_ss_etf_mtime,
)

st.set_page_config(
    page_title="Risk | Meridian",
    page_icon="\U0001f6e1\ufe0f",
    layout="wide",
)

_refresh_count = setup_live_polling(interval_ms=10_000, key="risk_refresh")
inject_common_css()

# ── UI 상수 (변경 시 여기서만 수정) ──
_FONT_SIZE_TITLE  = 12
_FONT_SIZE_LABEL  = 10
_FONT_SIZE_TICK   = 9


# [Phase 18] 사이드바 캐시 클리어 버튼
render_cache_clear_button(location='sidebar', page_key='risk')

# [Phase 18] SS-ETF 추가 스타일
st.markdown("""
<style>
.ssetf-banner {
    border-radius: 10px; padding: 0.8rem 1.2rem;
    margin-bottom: 0.8rem; font-weight: 600;
}
.ssetf-critical { background: rgba(213,0,0,0.10); border-left: 5px solid #d50000; color: #b71c1c !important; }
.ssetf-warning  { background: rgba(230,81,0,0.10); border-left: 5px solid #e65100; color: #bf360c !important; }
.ssetf-caution  { background: rgba(245,127,23,0.10); border-left: 5px solid #f57f17; color: #e65100 !important; }
.ssetf-normal   { background: rgba(46,125,50,0.08); border-left: 5px solid #2e7d32; color: #1b5e20 !important; }
.ssetf-pending  { background: rgba(158,158,158,0.10); border-left: 5px solid #9e9e9e; color: #616161 !important; }
.ssetf-label { font-size: 0.72rem; color: #888 !important; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px; }
.ssetf-value { font-size: 1.5rem; font-weight: 800; }
.ssetf-bar-track { height: 8px; background: #e0e0e0; border-radius: 4px; overflow: hidden; margin: 4px 0; }
.ssetf-bar-fill  { height: 100%; border-radius: 4px; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    "<div class='main-header'><h1>\U0001f6e1\ufe0f Risk</h1>"
    "<p>리스크 게이트 · Kill Switch · Drawdown Guard · SS-ETF 파생 리스크 (SSoT)</p></div>",
    unsafe_allow_html=True,
)

# [SSOT Refactoring] 리스크 데이터 로드 — data_loader.py 경유
risk = load_risk_data()

ks     = risk.get("kill_switch", {})
dd     = risk.get("drawdown_guard", {})
cb     = risk.get("circuit_breaker", {})
bh     = risk.get("beta_hedge", {})
exp    = risk.get("exposure_orchestrator", {})
var_d  = risk.get("realtime_var", {})
conc   = risk.get("concentration_risk", {})
factor = risk.get("factor_risk", {})
budget = risk.get("risk_budget_state", {})

# ════════════════════════════════════════════════════════
# [Phase 18] SS-ETF 단일종목 파생 리스크 스캐너
# ════════════════════════════════════════════════════════
st.subheader("\U0001f52c SS-ETF 단일종목 파생 리스크 스캐너 (Wag-the-Dog Detector)")
st.caption(
    "2026-05-27 단일종목 레버리지/인버스 ETF 상장 이후 장 후반(15:20~) 변동성 40~50% 폭증 감시. "
    "vol_ratio >= 30% 시 S1 신규 진입 자동 차단."
)

# [Phase 18] mtime 기반 캐시 키: SS-ETF 파일 변경 즉시 캐시 갱신
_ss_etf_mtime = _get_ss_etf_mtime()
ss_etf = load_ss_etf_risk(_mtime=_ss_etf_mtime)

_thr          = ss_etf.get("thresholds", {})
_src          = ss_etf.get("source", "수집 대기 중")
_ss_ts        = str(ss_etf.get("timestamp", ""))[:19] or "수집 대기 중"
_combined_warn = ss_etf.get("combined_warning", False)

# 전체 상태 배너
if _combined_warn:
    st.markdown(
        "<div class='ssetf-banner ssetf-warning'>"
        "\U0001f6a8 <strong>SS-ETF Wag-the-Dog 경고</strong> — LP 델타 헤징 압력 감지. "
        "장 후반 신규 진입 주의 필요."
        "</div>",
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        "<div class='ssetf-banner ssetf-normal'>"
        "✅ <strong>SS-ETF 파생 리스크 정상</strong> — Wag-the-Dog 압력 임계치 미만."
        "</div>",
        unsafe_allow_html=True,
    )

# 삼성전자 / SK하이닉스 각각 렌더링
for _tkey, _tname in [("samsung", "삼성전자 (005930)"), ("hynix", "SK하이닉스 (000660)")]:
    _t       = ss_etf.get(_tkey, {})
    _vol_r   = safe_float(_t.get("vol_ratio",   _t.get("ss_etf_vol_ratio",     0.0)))
    _lp_p    = safe_float(_t.get("lp_pressure", _t.get("lp_delta_pressure",   0.0)))
    _vol_an  = safe_float(_t.get("vol_anomaly", _t.get("intraday_vol_anomaly", 0.0)))
    _status_txt = str(_t.get("status", "수집 대기 중"))

    _level_label, _level_color = _ss_etf_level(_vol_r, _thr)

    if "위험" in _level_label:
        _banner_cls = "ssetf-critical"
    elif "경고" in _level_label:
        _banner_cls = "ssetf-warning"
    elif "주의" in _level_label:
        _banner_cls = "ssetf-caution"
    elif "수집" in _status_txt:
        _banner_cls = "ssetf-pending"
    else:
        _banner_cls = "ssetf-normal"

    with st.expander(
        f"{_level_label}  `{_tname}` | vol_ratio: {_vol_r:.1%} | vol_anomaly: {_vol_an:.2f}x",
        expanded=(_vol_r >= _thr.get("vol_ratio_caution", 0.15)),
    ):
        st.markdown(
            f"<div class='ssetf-banner {_banner_cls}'>"
            f"{_level_label} &nbsp;|&nbsp; 데이터: <code>{_src}</code>"
            f"&nbsp;|&nbsp; 갱신: <code>{_ss_ts}</code>"
            "</div>",
            unsafe_allow_html=True,
        )

        col_a, col_b, col_c = st.columns(3)

        with col_a:
            _pct_fill = min(_vol_r / max(_thr.get("vol_ratio_critical", 0.50), 0.01) * 100, 100)
            _bar_col  = (
                "#d50000" if _vol_r >= _thr.get("vol_ratio_critical", 0.50) else
                ("#e65100" if _vol_r >= _thr.get("vol_ratio_warning",  0.30) else
                ("#f57f17" if _vol_r >= _thr.get("vol_ratio_caution",  0.15) else "#2e7d32"))
            )
            st.markdown(
                f"<div class='ssetf-label'>파생 거래량 비율 (vol_ratio)</div>"
                f"<div class='ssetf-value' style='color:{_bar_col}!important'>{_vol_r:.1%}</div>"
                f"<div class='ssetf-bar-track'>"
                f"<div class='ssetf-bar-fill' style='width:{_pct_fill:.0f}%;background:{_bar_col}'></div>"
                f"</div>"
                "<div style='font-size:0.7rem;color:#888!important'>주의>=15%  경고>=30%  위험>=50%</div>",
                unsafe_allow_html=True,
            )

        with col_b:
            _lp_col  = "#d50000" if abs(_lp_p) >= 500 else ("#e65100" if abs(_lp_p) >= 300 else "#2e7d32")
            _lp_sign = "+" if _lp_p > 0 else ""
            st.markdown(
                f"<div class='ssetf-label'>LP 델타 헤징 압력 (백만원)</div>"
                f"<div class='ssetf-value' style='color:{_lp_col}!important'>{_lp_sign}{_lp_p:,.0f}M</div>"
                "<div style='font-size:0.7rem;color:#888!important'>양수: LP 매도 헤징 / 음수: LP 매수 헤징</div>",
                unsafe_allow_html=True,
            )

        with col_c:
            _an_col = (
                "#d50000" if _vol_an >= _thr.get("vol_anomaly_warning", 1.5) else
                ("#f57f17" if _vol_an >= _thr.get("vol_anomaly_caution", 1.3) else "#2e7d32")
            )
            _an_label = "위험" if _vol_an >= 1.5 else ("주의" if _vol_an >= 1.3 else "정상")
            st.markdown(
                f"<div class='ssetf-label'>일중 변동성 이상치 (오늘/14일평균)</div>"
                f"<div class='ssetf-value' style='color:{_an_col}!important'>{_vol_an:.2f}x</div>"
                f"<div style='font-size:0.7rem;color:#888!important'>1.3x 주의 / 1.5x 경고 [{_an_label}]</div>",
                unsafe_allow_html=True,
            )

        if "수집" in _status_txt:
            st.info(
                "⚠️ SS-ETF 데이터 수집 대기 중 — "
                "ss_etf_liquidity_collector.py 실행 후 자동 갱신됩니다.\n"
                "상장일(2026-05-27) 이전 날짜는 0.0 표시됩니다."
            )

st.markdown(
    f"<div style='font-size:0.72rem;color:#888;text-align:right'>"
    f"\U0001f4c1 데이터: <code>{_src}</code> | "
    f"mtime 캐시 키: <code>{_ss_etf_mtime:.0f}</code> | TTL: 60초</div>",
    unsafe_allow_html=True,
)
st.markdown("---")

# ── Kill Switch 상태 ────────────────────────────────────────────────────────
st.subheader("\U0001f6a8 Kill Switch (kill_switch.json SSoT)")

_ks_active = bool(ks.get("active") or ks.get("triggered"))
_ks_reason = str(ks.get("reason") or ks.get("trigger") or "N/A")

if _ks_active:
    st.error(f"\U0001f198 **KILL SWITCH 활성화** — {_ks_reason}")
else:
    st.success("✅ Kill Switch 비활성 (정상 운영 중)")

c1, c2, c3 = st.columns(3)
with c1:
    st.metric("Kill Switch", "\U0001f198 ACTIVE" if _ks_active else "✅ OFF")
with c2:
    st.metric("Reason", _ks_reason[:30] if _ks_reason != "N/A" else "N/A")
with c3:
    _ks_ts = str(ks.get("timestamp") or ks.get("updated_at") or "")[:19]
    st.metric("Triggered At", _ks_ts or "N/A")

st.markdown("---")

# ── Drawdown Guard ───────────────────────────────────────────────────────────
st.subheader("\U0001f4c9 Drawdown Guard (drawdown_guard.json SSoT)")

_dd_active  = not bool(dd.get("safe", True))
_dd_current = safe_float(dd.get("drawdown_pct"))
_dd_stage   = str(dd.get("stage") or dd.get("dd_stage") or "normal")
_dd_exp     = safe_float(dd.get("exposure", 1.0))

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("DD Guard", "\U0001f534 GUARD ON" if _dd_active else "✅ Normal")
with c2:
    st.metric("현재 Drawdown", f"{_dd_current:.1f}%")
with c3:
    st.metric("Guard Stage", _dd_stage)
with c4:
    st.metric("Allowed Exposure", f"{_dd_exp:.1%}")

fig_gauge = go.Figure(go.Indicator(
    mode="gauge+number",
    value=abs(_dd_current),
    title={"text": "Drawdown %"},
    gauge={
        "axis": {"range": [0, max(abs(_dd_current) * 1.5, 10)]},
        "bar": {"color": "#d50000" if _dd_active else "#2196f3"},
    },
))
fig_gauge.update_layout(height=250, margin=dict(t=30, b=10, l=10, r=10))
st.plotly_chart(fig_gauge, use_container_width=True)

st.markdown("---")

# ── Circuit Breaker ──────────────────────────────────────────────────────────
if cb:
    st.subheader("⚡ Circuit Breaker (circuit_breaker.json SSoT)")
    cb_rows = []
    for comp_name, comp_data in cb.items():
        if isinstance(comp_data, dict):
            _state = comp_data.get("state", "UNKNOWN")
            _failures = comp_data.get("failures", 0)
            _successes = comp_data.get("successes", 0)
            _last_fail = comp_data.get("last_failure") or "N/A"
            if _last_fail != "N/A":
                _last_fail = str(_last_fail)[:19].replace("T", " ")
            
            _state_emoji = "🟢 CLOSED"
            if _state == "OPEN":
                _state_emoji = "🔴 OPEN"
            elif _state == "HALF_OPEN":
                _state_emoji = "🟡 HALF_OPEN"
            elif _state != "CLOSED":
                _state_emoji = _state
                
            cb_rows.append({
                "Component": comp_name.upper(),
                "State": _state_emoji,
                "Failures": _failures,
                "Successes": _successes,
                "Last Failure": _last_fail
            })
            
    if cb_rows:
        st.dataframe(pd.DataFrame(cb_rows), use_container_width=True, hide_index=True)
    else:
        st.json(cb)

st.markdown("---")

# ── Exposure Orchestrator ────────────────────────────────────────────────────
if exp:
    st.subheader("\U0001f4ca Exposure Orchestrator (exposure_orchestrator.json SSoT)")
    _total_exp = safe_float(exp.get("target_exposure") or exp.get("gross_exposure"))
    _raw_exp   = safe_float(exp.get("target_raw"))
    _sigma_adj = safe_float(exp.get("sigma_adjustment"))
    _reason    = str(exp.get("reason", ""))

    c1, c2, c3 = st.columns(3)
    with c1: st.metric("Target Exposure", f"{_total_exp:.1%}" if _total_exp else "N/A")
    with c2: st.metric("Raw Exposure",    f"{_raw_exp:.1%}"   if _raw_exp   else "N/A")
    with c3: st.metric("Sigma Adj",       f"{_sigma_adj:.2f}" if _sigma_adj else "N/A")
    if _reason:
        st.caption(f"\U0001f4dd 이유: {_reason}")

    _components = exp.get("components", {})
    if isinstance(_components, dict) and _components:
        comp_names, comp_scores, comp_values = [], [], []
        for cname, val in _components.items():
            if isinstance(val, dict):
                sc_v  = safe_float(val.get("score"))
                raw_v = val.get("value") or val.get("realized_vol") or ""
            else:
                sc_v, raw_v = safe_float(val), ""
            comp_names.append(cname.upper())
            comp_scores.append(round(sc_v, 4))
            comp_values.append(str(raw_v)[:12])

        col_pie, col_bar = st.columns([1, 1])

        with col_pie:
            total_s  = sum(abs(s) for s in comp_scores) or 1.0
            pie_vals = [abs(s) / total_s for s in comp_scores]
            _palette = ["#0078D4", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#EC4899", "#14B8A6"]
            fig_pie  = go.Figure(go.Pie(
                labels=comp_names, values=pie_vals,
                marker=dict(colors=_palette[:len(comp_names)]),
                hole=0.42, textinfo="label+percent",
                hovertemplate="%{label}<br>Score: %{customdata:.4f}<extra></extra>",
                customdata=comp_scores,
            ))
            fig_pie.update_layout(
                template="plotly_white", height=300,
                margin=dict(t=30, b=10, l=10, r=10),
                title=dict(text="Component Score 비중", font=dict(size=12), x=0.5),
                showlegend=True,
                legend=dict(orientation="h", y=-0.15, font=dict(size=10)),
            )
            st.plotly_chart(fig_pie, use_container_width=True)

        with col_bar:
            bar_colors = [
                "#10B981" if s >= 0.6 else ("#F59E0B" if s >= 0.3 else "#EF4444")
                for s in comp_scores
            ]
            fig_bar = go.Figure(go.Bar(
                x=comp_scores, y=comp_names, orientation="h",
                marker_color=bar_colors,
                text=[f"{s:.3f}" for s in comp_scores], textposition="outside",
                customdata=comp_values,
                hovertemplate="%{y}<br>Score: %{x:.4f}<br>Value: %{customdata}<extra></extra>",
            ))
            fig_bar.update_layout(
                template="plotly_white", height=300,
                margin=dict(t=30, b=20, l=10, r=60),
                xaxis=dict(range=[0, 1.08], title="Score (0~1)"),
                title=dict(text="Component Score 절대값", font=dict(size=12), x=0.5),
            )
            fig_bar.add_vline(
                x=_sigma_adj, line_dash="dash", line_color="#0078D4", line_width=1.5,
                annotation_text=f"sigma={_sigma_adj:.2f}",
                annotation_position="top right",
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        with st.expander("표 세부 데이터", expanded=False):
            rows_c = [{"Component": n, "Score": s, "Value": v}
                      for n, s, v in zip(comp_names, comp_scores, comp_values)]
            st.dataframe(pd.DataFrame(rows_c), hide_index=True, use_container_width=True)

st.markdown("---")

# ── Beta Hedge ───────────────────────────────────────────────────────────────
if bh:
    st.subheader("\U0001f517 Beta Hedge (beta_hedge.json SSoT)")
    _hedges = bh.get("hedges", {})
    if _hedges:
        st.json(_hedges)
    else:
        st.info("현재 구성된 헤지 포지션 없음.")

st.markdown("---")

# ── Realtime VaR ─────────────────────────────────────────────────────────────
if var_d:
    st.subheader("\U0001f4d0 Realtime VaR (realtime_var.json SSoT)")
    _var_95  = safe_float(var_d.get("var_pct"))
    _cvar_95 = safe_float(var_d.get("cvar_pct"))
    c1, c2 = st.columns(2)
    with c1: st.metric("VaR %",  f"{_var_95:.2f}%"  if _var_95  else "N/A")
    with c2: st.metric("CVaR %", f"{_cvar_95:.2f}%" if _cvar_95 else "N/A")

# ── Risk Budget ───────────────────────────────────────────────────────────────
if budget:
    st.markdown("---")
    st.subheader("\U0001f4b0 Risk Budget (risk_budget_state.json SSoT)")
    _last_daily = safe_float(budget.get("last_daily_budget", 1.0))
    _last_vol   = safe_float(budget.get("last_vol_target", 0.0))
    st.metric("Last Daily Budget", f"{_last_daily:.2%}")
    st.metric("Last Vol Target",   f"{_last_vol:.2%}")

# ── System Alerts ─────────────────────────────────────────────────────────────
alerts = load_json("system_alerts.json")
if alerts:
    st.markdown("---")
    st.subheader("\U0001f514 시스템 알림 (system_alerts.json SSoT)")
    _alert_list = alerts if isinstance(alerts, list) else alerts.get("alerts", [])
    for a in (_alert_list[:10] if isinstance(_alert_list, list) else []):
        _sev = str(a.get("severity") or "info").lower()
        _msg = str(a.get("message") or a.get("msg") or a)[:100]
        if _sev in ("critical", "error"):
            st.markdown(f"<div class='alert-critical'>\U0001f534 {_msg}</div>", unsafe_allow_html=True)
        elif _sev in ("warning", "warn"):
            st.markdown(f"<div class='alert-warning'>\U0001f7e1 {_msg}</div>", unsafe_allow_html=True)
        else:
            st.info(_msg)

st.caption(
    f"\U0001f7e2 Live Polling #{_refresh_count} | "
    f"SS-ETF mtime: {_ss_etf_mtime:.0f} | "
    "[Phase 18: SS-ETF Dashboard]"
)

# ══════════════════════════════════════════════════════════════
# ★ SURGERY-2026-07-10: CrashRadar 패널 + Sleeve A/B NAV
# ══════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("🚨 CrashRadar — 복합 충격 조기 경보 시스템")

try:
    from dashboard.utils.data_loader import load_crash_radar
    cr = load_crash_radar()

    _crash_prob  = cr.get('crash_prob', 0.0)
    _is_warning  = cr.get('is_crash_warning', False)
    _vix_score   = cr.get('vix_score', 0.0)
    _vol_score   = cr.get('volume_score', 0.0)
    _fear_score  = cr.get('fear_score', 0.0)
    _cr_regime   = cr.get('regime', 'unknown')

    # 경보 배너
    if _is_warning:
        st.error(f"🚨 **CrashRadar 경보 발령** | crash_prob={_crash_prob:.1%} | 레짐={_cr_regime.upper()}")
    elif _crash_prob > 0.3:
        st.warning(f"⚠️ **CrashRadar 주의** | crash_prob={_crash_prob:.1%} | 레짐={_cr_regime.upper()}")
    else:
        st.success(f"✅ CrashRadar 정상 | crash_prob={_crash_prob:.1%} | 레짐={_cr_regime.upper()}")

    _cr_c1, _cr_c2, _cr_c3, _cr_c4 = st.columns(4)
    with _cr_c1:
        st.metric("💥 Crash Prob", f"{_crash_prob:.1%}",
                  delta="⚠️ 경보" if _is_warning else "정상")
    with _cr_c2:
        st.metric("📊 VIX Velocity", f"{_vix_score:.2f}",
                  help="VIX 5일 변화율 기반 신호 (0~1)")
    with _cr_c3:
        st.metric("📈 Volume Anomaly", f"{_vol_score:.2f}",
                  help="거래량 Z-Score 기반 신호 (0~1)")
    with _cr_c4:
        st.metric("😱 Fear Composite", f"{_fear_score:.2f}",
                  help="PCR + VKOSPI 복합 공포 지수 (0~1)")

    # 게이지 차트
    import plotly.graph_objects as go
    fig_cr = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=_crash_prob * 100,
        number={'suffix': '%'},
        delta={'reference': 50, 'decreasing': {'color': 'green'}, 'increasing': {'color': 'red'}},
        gauge={
            'axis': {'range': [0, 100], 'tickwidth': 1},
            'bar': {'color': 'darkred' if _crash_prob > 0.5 else ('orange' if _crash_prob > 0.3 else 'green')},
            'steps': [
                {'range': [0, 30],  'color': '#e8f5e9'},
                {'range': [30, 50], 'color': '#fff9c4'},
                {'range': [50, 100],'color': '#ffebee'},
            ],
            'threshold': {'line': {'color': 'red', 'width': 4}, 'thickness': 0.75, 'value': 50},
        },
        title={'text': 'CrashRadar Probability'},
    ))
    fig_cr.update_layout(height=220, margin=dict(t=30, b=0, l=20, r=20))
    st.plotly_chart(fig_cr, use_container_width=True)

except Exception as _cr_e:
    st.info(f"CrashRadar 데이터 로드 중... ({_cr_e})")

# ── Sleeve A/B NAV 실측 ──────────────────────────────────────────
st.markdown("---")
st.subheader("🏦 Sleeve A (알파) vs Sleeve B (베타) — 실측 NAV")

try:
    from dashboard.utils.data_loader import load_sleeve_nav
    _sv = load_sleeve_nav()

    _sv_c1, _sv_c2, _sv_c3 = st.columns(3)
    with _sv_c1:
        st.metric("🔷 Sleeve A NAV",
                  f"₩{_sv['sleeve_a_nav']:,.0f}",
                  delta=f"{_sv['sleeve_a_ret']*100:+.2f}% (초기 대비)",
                  delta_color="normal")
        _a_dd = _sv['sleeve_a_dd'] * 100
        st.caption(f"Drawdown: {_a_dd:.2f}% (from HWM ₩{_sv['sleeve_a_hwm']:,.0f})")
    with _sv_c2:
        st.metric("🔶 Sleeve B NAV",
                  f"₩{_sv['sleeve_b_nav']:,.0f}",
                  delta=f"{_sv['sleeve_b_ret']*100:+.2f}% (초기 대비)",
                  delta_color="normal")
        _b_dd = _sv['sleeve_b_dd'] * 100
        st.caption(f"Drawdown: {_b_dd:.2f}% (from HWM ₩{_sv['sleeve_b_hwm']:,.0f})")
    with _sv_c3:
        st.metric("💰 Total NAV",
                  f"₩{_sv['total_nav']:,.0f}")
        _total_ret = (_sv['total_nav'] / _sv['initial_capital'] - 1) * 100 if _sv['initial_capital'] > 0 else 0
        st.caption(f"전체 수익률: {_total_ret:+.2f}%")

    # Sleeve 비율 파이 차트
    import plotly.express as px
    if _sv['sleeve_a_nav'] + _sv['sleeve_b_nav'] > 0:
        fig_sv = px.pie(
            values=[_sv['sleeve_a_nav'], _sv['sleeve_b_nav']],
            names=['Sleeve A (Alpha)', 'Sleeve B (Beta)'],
            color_discrete_sequence=['#3b82f6', '#f59e0b'],
            hole=0.4,
        )
        fig_sv.update_layout(
            height=200,
            margin=dict(t=10, b=10, l=10, r=10),
            showlegend=True,
        )
        st.plotly_chart(fig_sv, use_container_width=True)

except Exception as _sv_e:
    st.info(f"Sleeve NAV 데이터 로드 중... ({_sv_e})")

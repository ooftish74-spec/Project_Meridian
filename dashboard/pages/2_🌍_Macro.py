#!/usr/bin/env python3
# [Phase 17: Dashboard Refactor v3]
"""
2_🌍_Macro.py — 매크로 / 레짐 상태 관제 v3
=============================================
[v3 변경]
  - Morning Fusion / Cross-Asset: 상태 뱃지 + 컬러 히트맵 테이블
  - Intraday Regime: 세부 판단 근거(measurements) 섹션 추가
  - signal_cache.overnight_intel 파싱 강화
  - 모든 지표에 help= 툴팁
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_PAGES_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PAGES_DIR.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.utils.data_loader import (
    setup_live_polling, inject_common_css,
    load_macro_data, load_json, safe_float, safe_fmt, get_regime_icon,
)

_refresh_count = setup_live_polling(interval_ms=10_000, key="macro_refresh_v3")
inject_common_css()

st.markdown(
    "<div class='main-header'><h1>🌍 Macro</h1>"
    "<p>시장 레짐 · 크로스에셋 · Intraday 레짐 · 이벤트 (SSoT)</p></div>",
    unsafe_allow_html=True,
)

macro     = load_macro_data()
sc        = macro.get("signal_cache", {})
regime    = macro.get("current_regime", {})
us_regime = macro.get("us_market_regime", {})
intraday  = macro.get("intraday_regime", {})
cross     = macro.get("cross_asset_signals", {})
events    = macro.get("dynamic_events", {})

# overnight_intel에서 US 데이터 추출
oi = sc.get("overnight_intel", {})


def _get_measurement_judgment(key: str, val) -> str:
    """measurement 항목별 판단 근거 반환."""
    try:
        v = float(val)
    except Exception:
        return str(val)

    key_l = key.lower()
    if "vix" in key_l:
        if v > 30:   return "🔴 고공포 (30↑) — 방어 모드"
        if v > 20:   return "🟡 주의 (20~30)"
        return "🟢 안정 (20 미만)"
    if "kospi" in key_l and "trend" in key_l:
        return "🟢 상승" if str(val).lower() in ("up", "bull") else "🔴 하락"
    if "ma20_dist" in key_l or "ma60_dist" in key_l:
        if v > 5:   return "🟢 이격 확대 (강한 추세)"
        if v < -5:  return "🔴 이격 역전 (약세)"
        return "🟡 중립"
    if "volatility" in key_l:
        if v > 25:  return "🔴 고변동성"
        if v > 15:  return "🟡 보통"
        return "🟢 저변동성"
    if "sentiment" in key_l:
        return "🟢 강세" if "bull" in str(val).lower() else ("🔴 약세" if "bear" in str(val).lower() else "🟡 중립")
    if v > 0:    return "🟢 양호"
    if v < 0:    return "🔴 부정적"
    return "🟡 중립"



# ── 헬퍼: 컬러 뱃지 HTML ──────────────────────────────────────────────────────
def _badge(val, pos_thr=0, neg_thr=None, fmt=".2f", unit=""):
    """값에 따라 🟢/🟡/🔴 뱃지 반환."""
    try:
        v = float(val)
        if neg_thr is None:
            color = "#10B981" if v >= pos_thr else "#EF4444"
            icon  = "🟢" if v >= pos_thr else "🔴"
        else:
            if v >= pos_thr:
                color, icon = "#10B981", "🟢"
            elif v >= neg_thr:
                color, icon = "#F59E0B", "🟡"
            else:
                color, icon = "#EF4444", "🔴"
        return f'<span style="color:{color};font-weight:700;">{icon} {v:{fmt}}{unit}</span>'
    except Exception:
        return f'<span style="color:#6B7280;">— {val}</span>'


# ══════════════════════════════════════════════════════════════════════════════
# 1. 크로스에셋 KPI 바
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("#### 📊 크로스에셋 주요 지표")
_vix    = safe_float(sc.get("vix"))
_vkospi = safe_float(sc.get("vkospi"))
_kospi  = safe_float(sc.get("kospi"))
_usdkrw = safe_float(sc.get("usdkrw"))
_ois    = safe_float(sc.get("ois"), 50.0)

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.metric("VIX", f"{_vix:.1f}",
              delta="⚠️ Spike" if _vix > 30 else ("정상" if _vix > 0 else "N/A"),
              help="Cboe 변동성 지수. 30↑ 공포 구간. 낮을수록 위험자산 선호.")
with c2:
    st.metric("VKOSPI", f"{_vkospi:.1f}" if _vkospi > 0 else "N/A",
              help="코스피 옵션 내재변동성. 20↑ 주의 구간.")
with c3:
    st.metric("KOSPI", f"{_kospi:,.0f}" if _kospi > 0 else "N/A",
              help="코스피 종가 지수.")
with c4:
    st.metric("USD/KRW", f"{_usdkrw:,.0f}" if _usdkrw > 0 else "N/A",
              help="원달러 환율. 상승 시 수출주 유리.")
with c5:
    _oi_icon = "🟢" if _ois >= 60 else ("🟡" if _ois >= 40 else "🔴")
    st.metric("OIS", f"{_oi_icon} {_ois:.1f}",
              help="옵션 투자심리지수. 60↑ 낙관, 40↓ 비관.")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 2. KR / US 레짐
# ══════════════════════════════════════════════════════════════════════════════
col_kr, col_us = st.columns(2)

with col_kr:
    st.subheader("🇰🇷 KR 레짐 (current_regime.json)")
    _kr      = str(regime.get("regime", "N/A"))
    _kc      = safe_float(regime.get("confidence"))
    _kt      = str(regime.get("timestamp", ""))[:19]
    _mac_c   = safe_float(regime.get("macro_composite"))

    st.markdown(f"### {get_regime_icon(_kr)}")
    c1r, c2r = st.columns(2)
    with c1r:
        st.metric("Confidence", f"{_kc*100:.1f}%",
                  help="레짐 분류기 사후 확률.")
    with c2r:
        st.metric("Macro Composite", f"{_mac_c:.1f}",
                  help="거시 지표 종합 점수 (VIX, KOSPI 추세 등).")

    # 레짐 점수 히트맵
    _scores = regime.get("scores", {})
    if _scores:
        _score_items = sorted(_scores.items(), key=lambda x: -x[1])
        _score_df = pd.DataFrame([
            {"레짐": k.upper(), "스코어": round(float(v), 3),
             "상태": "🟢 현재" if k == _kr else ""}
            for k, v in _score_items
        ])
        # 히트맵 Bar 차트
        fig_scores = go.Figure(go.Bar(
            x=[r["레짐"] for _, r in _score_df.iterrows()],
            y=[r["스코어"] for _, r in _score_df.iterrows()],
            marker_color=["#10B981" if r["레짐"].lower() == _kr.lower() else "#CBD5E1"
                          for _, r in _score_df.iterrows()],
            text=[f"{r['스코어']:.3f}{r['상태']}" for _, r in _score_df.iterrows()],
            textposition="outside",
        ))
        fig_scores.update_layout(
            template="plotly_white", height=200,
            margin=dict(t=20, b=10, l=10, r=10),
            title=dict(text="레짐 스코어 분포", font=dict(size=12), x=0.5),
            yaxis=dict(range=[0, 1.0]),
        )
        st.plotly_chart(fig_scores, use_container_width=True)

    st.caption(f"Updated: {_kt or 'N/A'}")

with col_us:
    st.subheader("🇺🇸 US 레짐 (us_market_regime.json)")
    _us  = str(us_regime.get("regime", "N/A"))
    _uss = safe_float(us_regime.get("score"))
    _ust = str(us_regime.get("timestamp", ""))[:19]

    st.markdown(f"### {get_regime_icon(_us)}")
    st.metric("Score", f"{_uss:.3f}" if _uss else "N/A",
              help="미국 레짐 모델 종합 스코어.")

    # overnight intel
    _sp500_c  = safe_float(oi.get("sp500_change_pct")  or oi.get("sp500_change"))
    _nasdaq_c = safe_float(oi.get("nasdaq_change_pct") or oi.get("nasdaq_change"))
    _sox_c    = safe_float(oi.get("sox_change_pct"))
    _us_sent  = str(oi.get("us_sentiment", "N/A"))

    a, b, c = st.columns(3)
    with a: st.metric("S&P500", f"{_sp500_c:+.2f}%",  help="S&P 500 전일 대비.")
    with b: st.metric("NASDAQ", f"{_nasdaq_c:+.2f}%", help="나스닥 종합 변동.")
    with c: st.metric("SOX",    f"{_sox_c:+.2f}%",    help="필라델피아 반도체 지수.")

    _sent_color = "#10B981" if "bull" in _us_sent else ("#EF4444" if "bear" in _us_sent else "#F59E0B")
    st.markdown(
        f"<div style='background:#F8FAFC;border-left:3px solid {_sent_color};"
        f"padding:6px 12px;border-radius:6px;font-size:0.9rem;margin-top:8px;'>"
        f"🧭 <b>US 심리:</b> <span style='color:{_sent_color};font-weight:700;'>{_us_sent.upper()}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"Updated: {_ust or 'N/A'}")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 3. [Task 3] Morning Fusion / Overnight Context — 상태 뱃지 + 컬러 테이블
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🌅 Morning Fusion (signal_cache.overnight_intel)")

if oi:
    # 핵심 지표 카드
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _sp_chg = safe_float(oi.get("sp500_change_pct"))
        st.markdown(
            f"<div style='background:#F0FDF4;border:1px solid #BBF7D0;border-radius:10px;"
            f"padding:12px;text-align:center;'>"
            f"<div style='font-size:0.75rem;color:#6B7280;'>S&P 500 야간 변동</div>"
            f"<div style='font-size:1.4rem;font-weight:700;color:{'#10B981' if _sp_chg>=0 else '#EF4444'};'>"
            f"{'▲' if _sp_chg>=0 else '▼'} {abs(_sp_chg):.2f}%</div>"
            f"</div>", unsafe_allow_html=True,
        )
    with c2:
        _nq_chg = safe_float(oi.get("nasdaq_change_pct"))
        st.markdown(
            f"<div style='background:#EFF6FF;border:1px solid #BFDBFE;border-radius:10px;"
            f"padding:12px;text-align:center;'>"
            f"<div style='font-size:0.75rem;color:#6B7280;'>NASDAQ 야간 변동</div>"
            f"<div style='font-size:1.4rem;font-weight:700;color:{'#10B981' if _nq_chg>=0 else '#EF4444'};'>"
            f"{'▲' if _nq_chg>=0 else '▼'} {abs(_nq_chg):.2f}%</div>"
            f"</div>", unsafe_allow_html=True,
        )
    with c3:
        _sox_chg = safe_float(oi.get("sox_change_pct"))
        st.markdown(
            f"<div style='background:#FFF7ED;border:1px solid #FED7AA;border-radius:10px;"
            f"padding:12px;text-align:center;'>"
            f"<div style='font-size:0.75rem;color:#6B7280;'>SOX 야간 변동</div>"
            f"<div style='font-size:1.4rem;font-weight:700;color:{'#10B981' if _sox_chg>=0 else '#EF4444'};'>"
            f"{'▲' if _sox_chg>=0 else '▼'} {abs(_sox_chg):.2f}%</div>"
            f"</div>", unsafe_allow_html=True,
        )
    with c4:
        _sent = str(oi.get("us_sentiment", "N/A"))
        _sent_bg  = "#F0FDF4" if "bull" in _sent else ("#FFF1F2" if "bear" in _sent else "#FFFBEB")
        _sent_bd  = "#BBF7D0" if "bull" in _sent else ("#FECDD3" if "bear" in _sent else "#FEF08A")
        _sent_clr = "#10B981" if "bull" in _sent else ("#EF4444" if "bear" in _sent else "#F59E0B")
        st.markdown(
            f"<div style='background:{_sent_bg};border:1px solid {_sent_bd};border-radius:10px;"
            f"padding:12px;text-align:center;'>"
            f"<div style='font-size:0.75rem;color:#6B7280;'>US 심리</div>"
            f"<div style='font-size:1.4rem;font-weight:700;color:{_sent_clr};'>{_sent.upper()}</div>"
            f"</div>", unsafe_allow_html=True,
        )

    # 전체 overnight_intel 컬러 테이블
    with st.expander("📋 Overnight Intel 세부 지표", expanded=False):
        _oi_rows = []
        for k, v in oi.items():
            if isinstance(v, (int, float)):
                _sign = "🟢" if float(v) >= 0 else "🔴"
                _oi_rows.append({"지표": k, "값": f"{_sign} {v:.4f}"})
            else:
                _oi_rows.append({"지표": k, "값": str(v)})
        if _oi_rows:
            st.dataframe(pd.DataFrame(_oi_rows), hide_index=True, use_container_width=True)
else:
    st.info("morning_fusion.json / overnight_intel 데이터 없음 — 아침 파이프라인 실행 후 표시")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 4. [Task 3] Intraday Regime — 세부 판단 근거 보강
# ══════════════════════════════════════════════════════════════════════════════
if intraday:
    st.subheader("⚡ Intraday 레짐 (intraday_regime.json)")
    _ir      = str(intraday.get("current_regime") or intraday.get("regime", "N/A"))
    _ir_exp  = safe_float(intraday.get("exposure_adjustment"), 1.0)
    _trigger = str(intraday.get("trigger") or "없음")
    _n_trans = int(intraday.get("n_transitions", 0) or 0)
    _rec     = intraday.get("recovery", {}) or {}
    _rec_det = bool(_rec.get("detected", False))
    _rec_str = safe_float(_rec.get("strength", 0))
    _ir_ts   = str(intraday.get("timestamp", ""))[:19]
    _measure = intraday.get("measurements", {}) or {}

    _regime_colors = {
        "normal": "#10B981", "caution": "#F59E0B",
        "bear": "#EF4444", "crash": "#7C3AED",
    }
    _rc = _regime_colors.get(_ir.lower(), "#6B7280")

    st.markdown(f"""
<div style="background:#F8FAFC;border-left:4px solid {_rc};
     padding:12px 16px;border-radius:8px;margin-bottom:12px;">
  <b>레짐:</b>
  <span style="color:{_rc};font-size:1.05rem;font-weight:700;">{get_regime_icon(_ir)}</span>
  &nbsp;|&nbsp; <b>트리거:</b> <code>{_trigger}</code>
  &nbsp;|&nbsp; <b>갱신:</b> {_ir_ts}
</div>""", unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Exposure Adj", f"×{_ir_exp:.2f}",
                  help="Intraday 레짐이 적용하는 포지션 승수. 1.0=정상, 0.5=50% 축소.")
    with c2:
        st.metric("레짐 전환 횟수", f"{_n_trans}회",
                  help="장중 레짐 전환 카운트. 잦으면 변동성 높은 장세.")
    with c3:
        st.metric("Recovery 감지", "✅" if _rec_det else "⚫ 없음",
                  help="V자 반등 또는 회복 패턴 감지 여부.")
    with c4:
        st.metric("Recovery 강도", f"{_rec_str:.0%}" if _rec_det else "N/A",
                  help="회복 패턴 강도 스코어 (0~100%).")

    # Exposure Progress Bar
    _exp_pct = min(1.0, max(0.0, _ir_exp))
    _bar_clr = "#10B981" if _exp_pct >= 0.8 else ("#F59E0B" if _exp_pct >= 0.5 else "#EF4444")
    st.markdown("**Exposure Adjustment**")
    st.markdown(f"""
<div style="background:#E5E7EB;border-radius:8px;height:18px;overflow:hidden;">
  <div style="background:{_bar_clr};width:{_exp_pct*100:.1f}%;height:18px;border-radius:8px;
       display:flex;align-items:center;padding-left:8px;
       color:white;font-size:0.75rem;font-weight:600;">
    {_exp_pct*100:.0f}%
  </div>
</div>
<div style="font-size:0.75rem;color:#6B7280;margin-top:2px;">
  0%(완전 방어) ←→ 100%(풀 익스포저)
</div>""", unsafe_allow_html=True)

    # [Task 3] 세부 판단 근거 (measurements)
    if _measure:
        st.markdown("---")
        st.markdown("##### 🔬 레짐 판정 세부 근거 (measurements)")

        _m_rows = []
        for k, v in _measure.items():
            _fmt_v = f"{v:.3f}" if isinstance(v, float) else str(v)
            # 각 지표별 판단 기준 정의
            _judgment = _get_measurement_judgment(k, v)
            _m_rows.append({
                "지표":   k.replace("_", " ").title(),
                "값":     _fmt_v,
                "판단":   _judgment,
            })

        if _m_rows:
            # 컬러 뱃지 테이블
            fig_meas = go.Figure(data=[go.Table(
                header=dict(
                    values=["<b>지표</b>", "<b>값</b>", "<b>판단</b>"],
                    fill_color="#1e3a5f",
                    font=dict(color="white", size=12),
                    align="left", height=30,
                ),
                cells=dict(
                    values=[
                        [r["지표"] for r in _m_rows],
                        [r["값"]   for r in _m_rows],
                        [r["판단"] for r in _m_rows],
                    ],
                    fill_color=[
                        ["#f8fafc" if i % 2 == 0 else "#f0f9ff"
                         for i in range(len(_m_rows))],
                    ],
                    align="left", height=26, font=dict(size=11),
                ),
            )])
            fig_meas.update_layout(
                template="plotly_white",
                margin=dict(t=10, b=10, l=0, r=0),
                height=max(120, min(450, len(_m_rows) * 30 + 60)),
            )
            st.plotly_chart(fig_meas, use_container_width=True)
    else:
        st.caption("measurements 데이터 없음 — Intraday Engine 실행 후 근거 표시")

    st.markdown("---")

else:
    st.info("⚡ Intraday Regime 데이터 없음 (intraday_regime.json)")
    st.markdown("---")




# ══════════════════════════════════════════════════════════════════════════════
# 5. Cross-Asset 시그널 (Plotly Table)
# ══════════════════════════════════════════════════════════════════════════════
if cross:
    st.subheader("🔗 Cross-Asset 시그널")
    rows_c = []
    for k, v in cross.items():
        if isinstance(v, dict):
            _dir  = str(v.get("direction") or v.get("signal") or "—")
            _val  = str(v.get("value") or v.get("score") or "—")
            _conf = safe_float(v.get("confidence"))
            _conf_str = f"{_conf*100:.0f}%" if _conf else "—"
            _dir_badge = "🟢" if _dir.lower() in ("up", "long", "bullish") else \
                         ("🔴" if _dir.lower() in ("down", "short", "bearish") else "🟡")
            rows_c.append({"지표": k, "방향": f"{_dir_badge} {_dir}", "값": _val, "신뢰도": _conf_str})
        else:
            rows_c.append({"지표": k, "방향": "—", "값": str(v), "신뢰도": "—"})

    if rows_c:
        df_c = pd.DataFrame(rows_c)
        fig_c = go.Figure(data=[go.Table(
            header=dict(
                values=[f"<b>{col}</b>" for col in df_c.columns],
                fill_color="#1e3a5f", font=dict(color="white", size=12),
                align="left", height=30,
            ),
            cells=dict(
                values=[df_c[col].tolist() for col in df_c.columns],
                fill_color=[["#f8fafc" if i % 2 == 0 else "#f0f9ff" for i in range(len(df_c))]],
                align="left", height=26, font=dict(size=11),
            ),
        )])
        fig_c.update_layout(
            template="plotly_white",
            margin=dict(t=10, b=10, l=0, r=0),
            height=max(120, min(500, len(df_c) * 30 + 60)),
        )
        st.plotly_chart(fig_c, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# 6. 이벤트 캘린더
# ══════════════════════════════════════════════════════════════════════════════
if events:
    st.subheader("📅 이벤트 캘린더 (dynamic_events.json)")
    _ev = events if isinstance(events, list) else events.get("events", events.get("calendar", []))
    if isinstance(_ev, list) and _ev:
        st.dataframe(pd.DataFrame(_ev), hide_index=True, use_container_width=True)
    else:
        st.info("예정된 이벤트 없음")

st.caption(f"🟢 Live Polling 활성 — 10초 자동 새로고침 #{_refresh_count}")

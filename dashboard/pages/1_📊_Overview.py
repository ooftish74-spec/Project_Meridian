#!/usr/bin/env python3
# [Phase 17: Dashboard Refactor v3]
"""
1_📊_Overview.py — 통합 관제 센터 (Global Unified Mission Control)
===================================================================
[v3 변경]
  - Go/No-Go 전략별 요약 흡수 (strategy_summary 카드)
  - 2.32억 자본금 체제 반영 (shadow_portfolio.initial_capital SSoT)
  - Daily NAV 차트 안정화
"""

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

_DASH = Path(__file__).resolve().parent.parent
_ROOT = _DASH.parent
sys.path.insert(0, str(_ROOT))

from dashboard.utils.data_loader import (
    setup_live_polling, inject_common_css, get_ssot_kpis,
    load_shadow_summary, load_shadow_portfolio, load_shadow_trades,
    load_stream_metrics, load_go_nogo, load_json,
    safe_float, safe_fmt, get_regime_icon, get_verdict_class,
    metric_card_html, RESULTS_DIR,
    load_nav_history, load_global_kpis,
    load_moonshot_status,  # [Phase 39]
)

# ── 종목명 조회 헬퍼 (universe_loader 없는 환경도 크래시 없이 작동) ──
try:
    from src.data_collection.universe_loader import get_ticker_name as _get_ticker_name
except Exception:
    # universe_loader 미사용 환경 fallback
    def _get_ticker_name(ticker: str):  # type: ignore[misc]
        return None

# ── 페이지 설정 ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Overview | Meridian",
    page_icon="📊",
    layout="wide",
)
inject_common_css()

# ── UI 상수 (변경 시 여기서만 수정) ──
_FONT_SIZE_TITLE  = 12
_FONT_SIZE_LABEL  = 10
_FONT_SIZE_TICK   = 9


_refresh_count = setup_live_polling(interval_ms=10_000, key="overview_refresh_v3")

# ── 데이터 로드 ───────────────────────────────────────────────────────────────
kpis   = get_ssot_kpis()
gkpis  = load_global_kpis()
ss     = load_shadow_summary()
sp     = load_shadow_portfolio()
gn     = load_go_nogo()          # go_nogo.json (없으면 {})
nav_df = load_nav_history()
sc     = load_json("signal_cache.json") or {}
regime = load_json("current_regime.json") or {}

# shadow_portfolio 또는 kis_portfolio에서 핵심 수치 로드
import os
_is_live = os.environ.get("KIS_MODE", "").lower() == "live"
_default_cap = 1_000_000 if _is_live else 232_000_000

_account_data = sp.get("account", {})
_nav        = safe_float(_account_data.get("nav") or sp.get("virtual_nav") or sp.get("nav") or _default_cap)
_init_cap   = safe_float(_account_data.get("initial_capital") or sp.get("initial_capital") or _default_cap)
_realized   = safe_float(sp.get("summary", {}).get("total_realized_pnl") or sp.get("realized_pnl") or 0.0)
_unrealized = safe_float(sp.get("summary", {}).get("total_unrealized_pnl") or sp.get("unrealized_pnl") or 0.0)
_total_pnl  = _realized + _unrealized
_cum_ret    = (_nav / _init_cap - 1) * 100 if _init_cap > 0 else 0.0

# global_kpis 보완
_g_nav = gkpis.get("global_nav") or _nav or _init_cap
_g_pnl = gkpis.get("global_total_pnl") or _total_pnl

# ══════════════════════════════════════════════════════════════════════════════
# 1. 헤더
# ══════════════════════════════════════════════════════════════════════════════
_kr_reg = str(regime.get("regime", "N/A")).upper()
_kr_icon = get_regime_icon(_kr_reg)
st.markdown(
    f"<div class='main-header'>"
    f"<h1>📊 Global Mission Control</h1>"
    f"<p>S0~S10 통합 라이브 포트폴리오 · 레짐: {_kr_icon} · "
    f"10초 자동 새로고침 #{_refresh_count} · "
    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
    f"</div>",
    unsafe_allow_html=True,
)

# ══════════════════════════════════════════════════════════════════════════════
# 2. Global KPI 메트릭 바
# ══════════════════════════════════════════════════════════════════════════════
_grade   = str(kpis.get("grade", "?"))
_da      = safe_float(kpis.get("da"))
_sharpe  = safe_float(kpis.get("sharpe"))
_max_dd  = safe_float(kpis.get("max_dd"))
_alpha   = safe_float(kpis.get("alpha"))
_ic      = safe_float(kpis.get("ic"))

c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)

with c1:
    _nav_delta = "delta-pos" if _g_pnl >= 0 else "delta-neg"
    st.markdown(metric_card_html(
        "Total NAV", f"₩{_g_nav:,.0f}",
        f"초기: ₩{_init_cap:,.0f}", _nav_delta,
    ), unsafe_allow_html=True)
with c2:
    _pnl_delta = "delta-pos" if _g_pnl >= 0 else "delta-neg"
    _pnl_sign  = "+" if _g_pnl >= 0 else ""
    st.markdown(metric_card_html(
        "Total P&L", f"{_pnl_sign}₩{_g_pnl:,.0f}",
        f"수익률: {_cum_ret:+.2f}%", _pnl_delta,
    ), unsafe_allow_html=True)
with c3:
    _grade_icon  = "🟢 " if _grade in ("A", "B") else ("🟡 " if _grade == "C" else "🔴 ")
    _grade_color = {"A": "#00c853", "B": "#2196f3", "C": "#ffd740", "D": "#ff6d00", "F": "#d50000"}.get(_grade, "#888")
    st.markdown(metric_card_html("Grade", f"{_grade_icon}{_grade}", "Meridian Score", "delta-neutral", _grade_color), unsafe_allow_html=True)
with c4:
    _da_cls  = "delta-pos" if _da >= 0.55 else ("delta-neutral" if _da >= 0.5 else "delta-neg")
    _da_icon = "🟢" if _da >= 0.55 else ("🟡" if _da >= 0.5 else "🔴")
    st.markdown(metric_card_html(
        "DA", f"{_da_icon} {_da*100:.1f}%", "방향적중률",
        _da_cls,
    ), unsafe_allow_html=True)
with c5:
    _sh_cls  = "delta-pos" if _sharpe >= 1.0 else ("delta-neutral" if _sharpe >= 0 else "delta-neg")
    _sh_icon = "🟢" if _sharpe >= 1.0 else ("🟡" if _sharpe >= 0 else "🔴")
    st.markdown(metric_card_html("Sharpe", f"{_sh_icon} {_sharpe:.2f}", "위험조정수익", _sh_cls), unsafe_allow_html=True)
with c6:
    _dd_cls  = "delta-pos" if _max_dd > -5 else ("delta-neutral" if _max_dd > -8 else "delta-neg")
    _dd_icon = "🟢" if _max_dd > -5 else ("🟡" if _max_dd > -8 else "🔴")
    st.markdown(metric_card_html("Max DD", f"{_dd_icon} {_max_dd:.1f}%", "최대낙폭", _dd_cls), unsafe_allow_html=True)
with c7:
    _al_cls  = "delta-pos" if _alpha > 0 else "delta-neg"
    _al_icon = "🟢" if _alpha > 0 else "🔴"
    st.markdown(metric_card_html("Alpha", f"{_al_icon} {_alpha:+.2f}%", "vs KOSPI", _al_cls), unsafe_allow_html=True)
with c8:
    _ic_cls  = "delta-pos" if _ic > 0.03 else ("delta-neutral" if _ic >= 0 else "delta-neg")
    _ic_icon = "🟢" if _ic > 0.03 else ("🟡" if _ic >= 0 else "🔴")
    st.markdown(metric_card_html("IC", f"{_ic_icon} {_ic:.3f}", "정보계수", _ic_cls), unsafe_allow_html=True)

with st.expander("ℹ️ 주요 지표 설명 (DA · Sharpe · MDD · Alpha · IC)", expanded=False):
    st.markdown("""
| 지표 | 정의 | 해석 기준 |
|------|------|-----------|
| **DA (Direction Accuracy)** | 예측 방향 일치율 (업/다운) | 55% 이상 양호, 60% 이상 우수 |
| **Sharpe** | (연수익-무위험률) / 변동성 | 1.0↑ 양호, 2.0↑ 탁월 |
| **Max DD** | 최고점 대비 최대 낙폭 | 5% 이하 양호, 10% 이하 허용 |
| **Alpha** | 누적 초과 수익 vs KOSPI | 양수·클수록 우수 |
| **IC** | 예측↔실제 스피어만 상관계수 | 0.05↑ 우수, 0.10↑ 탁월 |
""")

st.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# [Phase 39] 🚀 Moonshot Booster System — 4대 부스터 현황
# ══════════════════════════════════════════════════════════════════════════════
try:
    _ms = load_moonshot_status()

    st.subheader("🚀 Moonshot Booster System")

    _mc1, _mc2 = st.columns(2)

    # ── Card 1: Kelly 압축 & 트레일링 ─────────────────────────────────────
    with _mc1:
        _kelly_on   = bool(_ms.get('kelly_active', False))
        _kelly_icon = "🔥" if _kelly_on else "⚪"
        _k_cash_pct = float(_ms.get('kelly_cash_ratio', 0.18)) * 100
        _k_max_pos  = int(_ms.get('kelly_max_pos', 3))
        _k_trail    = float(_ms.get('kelly_trail_mult', 2.0))
        _k_state    = "ON" if _kelly_on else "OFF"
        _k_color    = "#10B981" if _kelly_on else "#6B7280"
        st.markdown(f"""
<div style="background:#ffffff;border:1px solid {_k_color};border-radius:10px;
            padding:14px 16px;box-shadow:0 2px 6px rgba(0,0,0,0.05);">
  <div style="font-size:0.9rem;font-weight:700;color:{_k_color};">
    {_kelly_icon} Kelly Booster · <span style="font-size:0.8rem;">{_k_state}</span>
  </div>
  <div style="margin-top:10px;font-size:0.85rem;color:#374151;">
    💰 <b>현금 비율:</b> <code>{_k_cash_pct:.0f}%</code><br>
    📦 <b>최대 종목:</b> <code>{_k_max_pos}개</code><br>
    📉 <b>Trail ATR 배수:</b> <code>{_k_trail:.1f}x</code>
  </div>
</div>""", unsafe_allow_html=True)

    # ── Card 2: S1 Hard Limit & 야간 승계 ─────────────────────────────────
    with _mc2:
        _limit_min = int(_ms.get('hard_limit_minute', 20))
        _bull_ratio = float(_ms.get('us_budget_ratio_bull', 0.35)) * 100
        st.markdown(f"""
<div style="background:#ffffff;border:1px solid #3B82F6;border-radius:10px;
            padding:14px 16px;box-shadow:0 2px 6px rgba(0,0,0,0.05);">
  <div style="font-size:0.9rem;font-weight:700;color:#3B82F6;">
    ⏱️ S1 Hard Limit & 승계 · <span style="font-size:0.8rem;">ON</span>
  </div>
  <div style="margin-top:10px;font-size:0.85rem;color:#374151;">
    🛑 <b>강제 청산:</b> <code>15:{_limit_min:02d} KST</code><br>
    🌙 <b>야간 승계(Bull):</b> <code>{_bull_ratio:.0f}%</code><br>
    🔄 <b>예산 라우팅:</b> <code>자동</code>
  </div>
</div>""", unsafe_allow_html=True)

    st.markdown("---")
    
    with st.expander("ℹ️ Moonshot Booster System 엔진 설명 보기", expanded=False):
        st.markdown("""
### 💰 1. Kelly 압축 & 트레일링 (Kelly Booster)
* **기능**: 켈리 공식(Kelly Criterion)을 응용하여 자본의 집중도와 현금 비중을 동적으로 조절합니다.
* **현금 비율**: 시장 상황에 따라 최적으로 보유해야 할 안전 현금 비중.
* **최대 종목**: 승률이 높을 때는 종목 수를 압축하여 수익률을 극대화.
* **Trail ATR 배수**: 이익이 발생한 종목의 익절 라인을 변동성(ATR) 기반으로 타이트하게 추적.

### ⏱️ 2. S1 Hard Limit & 야간 예산 승계 (Time & Budget Routing)
* **기능**: 국내장(S1)과 미국장 간의 자본 효율성을 극대화하기 위해 시간과 예산을 통제합니다.
* **S1 Hard Limit**: 아침 장 초반에만 국내 주식 진입을 허용하고 이후 변동성 노이즈를 피하기 위해 강제 차단.
* **야간 예산 승계**: 아침에 사용하지 못한 대기 자본을 글로벌 마켓 레짐(Bull, Caution 등)에 맞춰 밤 미국 증시 예산으로 이관.
""")
        
    st.markdown("---")

except Exception as _ms_e:
    st.warning(f"🚀 Moonshot Booster 데이터 로드 중... ({_ms_e})")
    st.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# 3. 누적 NAV 차트
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📈 누적 성과 차트 (Daily NAV 추이)")

if not nav_df.empty:
    _nav_col = "nav" if "nav" in nav_df.columns else nav_df.columns[0]
    fig_nav = px.line(
        nav_df, y=_nav_col,
        title="포트폴리오 총자산(NAV) 추이",
        template="plotly_white",
    )
    fig_nav.update_layout(
        xaxis_title="Date", yaxis_title="NAV (KRW)",
        height=300, margin=dict(l=20, r=20, t=40, b=20),
    )
    fig_nav.update_traces(line_color="#2196f3", line_width=2)
    # 초기자본 기준선
    fig_nav.add_hline(
        y=_init_cap, line_dash="dash", line_color="#9E9E9E",
        annotation_text=f"초기자본 ₩{_init_cap/1e8:.2f}억",
        annotation_position="bottom right",
    )
    st.plotly_chart(fig_nav, use_container_width=True)
else:
    st.info("📊 시계열 데이터 없음 — Mock Test 실행 후 NAV 히스토리가 기록됩니다.")
    # 현재 NAV 단일 포인트 표시
    fig_single = go.Figure(go.Indicator(
        mode="number+delta",
        value=_nav or _init_cap,
        number={"prefix": "₩", "valueformat": ",.0f"},
        delta={"reference": _init_cap, "valueformat": ",.0f"},
        title={"text": "현재 NAV (초기자본 대비)"},
    ))
    fig_single.update_layout(template="plotly_white", height=180)
    st.plotly_chart(fig_single, use_container_width=True)

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 4. 스트림별 실현 P&L 카드
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🏦 투자 스트림별 누적 실현 P&L")
_stream_pnl = sp.get("strategy_pnl", {})
if _stream_pnl:
    _pnl_items = sorted(_stream_pnl.items(), key=lambda x: str(x[0]))
    _cols = st.columns(min(len(_pnl_items), 6))
    for i, (sid, pnl_val) in enumerate(_pnl_items):
        with _cols[i % len(_cols)]:
            _val = safe_float(pnl_val)
            _cls = "delta-pos" if _val > 0 else ("delta-neg" if _val < 0 else "delta-neutral")
            st.markdown(metric_card_html(sid, f"₩{_val:,.0f}", "실현 P&L", _cls), unsafe_allow_html=True)
else:
    st.info("스트림별 실적(P&L) 데이터 없음 — 거래 시작 후 표시됩니다.")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 5. 통합 성과 모니터링 (Live KPIs)
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🏆 통합 성과 모니터링 (Live Trading KPIs)")
c1, c2, c3, c4 = st.columns(4)

_target_cagr  = 0.20
_current_cagr = safe_float(kpis.get("cagr")) or (_g_pnl / _init_cap if _init_cap > 0 else 0)
_cagr_cls     = "delta-pos" if _current_cagr >= _target_cagr else ("delta-neutral" if _current_cagr >= 0 else "delta-neg")
with c1:
    st.markdown(metric_card_html(
        "Current CAGR", f"{_current_cagr*100:.2f}%",
        f"목표: {_target_cagr*100:.0f}%", _cagr_cls,
    ), unsafe_allow_html=True)

_total_fees  = safe_float(gkpis.get("total_fees_paid") or sp.get("total_commission"))
_total_tax   = safe_float(gkpis.get("total_tax_estimated"))
_leakage_cls = "delta-neg" if (_total_fees + _total_tax) > 500_000 else "delta-neutral"
with c2:
    st.markdown(metric_card_html(
        "Cost / Tax Leakage", f"₩{(_total_fees + _total_tax):,.0f}",
        f"수수료 ₩{_total_fees:,.0f}", _leakage_cls,
    ), unsafe_allow_html=True)

_rar     = (_alpha / abs(_max_dd)) if _max_dd < 0 else _alpha
_rar_cls = "delta-pos" if _rar > 1.0 else "delta-neutral"
with c3:
    st.markdown(metric_card_html("Risk-Adj Return", f"{_rar:.2f}", "Alpha / |MaxDD|", _rar_cls), unsafe_allow_html=True)

with c4:
    st.markdown(metric_card_html("Operation Status", "ACTIVE", "System Nominal", "delta-pos"), unsafe_allow_html=True)

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 6. 현황 및 포지션
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📋 스트림별 보유 포지션 현황")

_kr_nav    = gkpis.get("kr_nav", 0) or _nav
_kr_pnl    = gkpis.get("kr_real_pnl", 0) or _realized
_gnverdict = str(gn.get("verdict", gn.get("decision", "N/A")))
_vc        = get_verdict_class(_gnverdict)

st.markdown(
    f"<div class='gonogo-box'>"
    f"<p style='font-size:1.2rem;font-weight:700;'>🇰🇷 국내/주식 라이브(Live) 포트폴리오</p>"
    f"<p class='{_vc}'>{_gnverdict}</p>"
    f"<p>국내주식 NAV: ₩{_kr_nav:,.0f} | 실현손익: ₩{_kr_pnl:,.0f}</p>"
    f"</div>",
    unsafe_allow_html=True,
)

_positions = sp.get("positions", {})
_pos_rows  = []
for pos_key, pos in (_positions.items() if isinstance(_positions, dict) else []):
    _ticker = str(pos.get("ticker") or pos_key.split(":")[-1])
    _pos_rows.append({
        "Stream":    str(pos.get("stream_id") or ""),
        "Ticker":    _ticker,
        "종목명":    _get_ticker_name(_ticker) or str(pos.get("name") or "N/A"),
        "평가금액":  f"₩{safe_float(pos.get('market_value')):,.0f}",
        "미실현P&L": f"₩{safe_float(pos.get('unrealized_pnl')):+,.0f}",
    })
if _pos_rows:
    st.markdown("**보유 포지션**")
    st.dataframe(pd.DataFrame(_pos_rows), hide_index=True, use_container_width=True)
else:
    st.info("⚪ 현재 보유 중인 포지션 없음 (대기 중)")


st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# [Task 5] 용어 사전 (Glossary) — DA · MDD · IC · Sharpe · Alpha · Win Rate
# ══════════════════════════════════════════════════════════════════════════════
with st.expander("ℹ️ 주요 지표 설명 (DA · MDD · IC · Alpha · Sharpe · Profit Factor)", expanded=False):
    st.markdown("""
| 지표 | 정의 | 해석 기준 |
|------|------|-----------|
| **DA (Daily Alpha)** | 전략의 일일 초과 수익률 vs KOSPI (방향 적중률 기반) | 양수·클수록 우수 |
| **MDD (Max Drawdown)** | 최고 NAV 대비 최대 낙폭 | 절댓값 10% 이하 양호 |
| **IC (Information Coefficient)** | 예측치↔실제 수익률 스피어만 상관계수 | 0.05↑ 우수, 0.10↑ 탁월 |
| **Alpha** | 누적 초과 수익 vs 벤치마크(KOSPI) | 양수·클수록 우수 |
| **Sharpe Ratio** | 위험 조정 수익률 (연 초과수익 / 연 변동성) | 1.0↑ 양호, 2.0↑ 탁월 |
| **Win Rate** | 수익 실현 거래 비율 | 55% 이상 권장 |
| **Profit Factor** | 총 수익 / 총 손실 비율 | 1.5↑ 권장, 2.0↑ 탁월 |
| **NAV** | Net Asset Value — 포트폴리오 순자산 가치 | 초기 자본 대비 성장률로 해석 |
""")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# 7. [Task 1] Go/No-Go 전략 요약 흡수
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("🚥 전략 이행 현황 (go_nogo.json 요약)")

_gn_verdict = str(gn.get("verdict", gn.get("decision", "N/A")))
_gn_vc      = get_verdict_class(_gn_verdict)
_gn_color   = {"GO": "#10B981", "NO_GO": "#EF4444", "CAUTION": "#F59E0B",
               "PENDING": "#6B7280", "WAIT": "#6B7280"}.get(_gn_verdict.upper(), "#6B7280")

col_verd, col_detail = st.columns([1, 3])
with col_verd:
    st.markdown(
        f"<div style='text-align:center;background:#F8FAFC;border:2px solid {_gn_color};"
        f"border-radius:12px;padding:20px;'>"
        f"<div style='font-size:0.8rem;color:#6B7280;font-weight:600;text-transform:uppercase;"
        f"letter-spacing:.06em;'>최종 판정</div>"
        f"<div style='font-size:2rem;font-weight:800;color:{_gn_color};margin:8px 0;'>{_gn_verdict}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

with col_detail:
    # criteria 요약
    _criteria   = gn.get("criteria", {})
    _strat_sum  = gn.get("strategy_summary", {})
    _meridian   = gn.get("meridian", {})

    if _criteria:
        _pass_c = sum(1 for v in _criteria.values() if isinstance(v, dict) and v.get("pass"))
        _fail_c = sum(1 for v in _criteria.values() if isinstance(v, dict) and not v.get("pass"))
        _total_c = _pass_c + _fail_c
        _pct = (_pass_c / _total_c * 100) if _total_c > 0 else 0
        st.markdown(f"**진단 항목:** {_pass_c}/{_total_c} PASS ({_pct:.0f}%)")

        # 미달 항목 요약
        _fails = [k.replace("_", " ").title() for k, v in _criteria.items()
                  if isinstance(v, dict) and not v.get("pass")]
        if _fails:
            st.markdown(f"🔴 **미달 항목:** {', '.join(_fails)}")
        else:
            st.success("✅ 모든 조건 충족")

    if _meridian:
        _m_detail = str(_meridian.get("detail", ""))
        if _m_detail:
            st.caption(f"Meridian: {_m_detail}")

    if gn.get("has_pending"):
        st.warning("⏳ PENDING 지표 존재 — 데이터 누적 중")

# 전략별 거래 현황 (strategy_summary 흡수)
if _strat_sum:
    st.markdown("**전략별 거래 현황**")
    _s_rows = [
        {
            "스트림":   sid,
            "거래 건수": info.get("trades", 0),
            "실현 P&L": f"₩{safe_float(info.get('pnl')):+,.0f}",
        }
        for sid, info in _strat_sum.items()
        if isinstance(info, dict)
    ]
    if _s_rows:
        _df_s = pd.DataFrame(_s_rows)
        col_tbl, col_chart = st.columns([1, 1])
        with col_tbl:
            st.dataframe(_df_s, hide_index=True, use_container_width=True)
        with col_chart:
            _pnl_vals = [safe_float(info.get("pnl")) for info in _strat_sum.values()
                         if isinstance(info, dict)]
            _pnl_keys = [k for k in _strat_sum if isinstance(_strat_sum[k], dict)]
            if _pnl_vals:
                fig_pnl = go.Figure(go.Bar(
                    x=_pnl_keys, y=_pnl_vals,
                    marker_color=["#10B981" if v >= 0 else "#EF4444" for v in _pnl_vals],
                    text=[f"₩{v:+,.0f}" for v in _pnl_vals],
                    textposition="outside",
                ))
                fig_pnl.update_layout(
                    template="plotly_white", height=200,
                    margin=dict(t=20, b=10, l=10, r=10),
                    title=dict(text="스트림별 실현 P&L", font=dict(size=12), x=0.5),
                )
                st.plotly_chart(fig_pnl, use_container_width=True)
else:
    st.info("전략별 거래 현황: 거래 시작 후 자동 집계됩니다.")

st.caption(
    f"📅 데이터: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
    f"🟢 Live 10초 자동 새로고침 #{_refresh_count} | "
    f"초기자본: ₩{_init_cap/1e8:.2f}억"
)

# ══════════════════════════════════════════════════════════════
# ★ SURGERY-2026-07-10: 신규 KPI — Entry Score + Sleeve 수익률
# ══════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("🔬 수술 신규 지표 — Entry Score & Sleeve 분리 성과")

try:
    from dashboard.utils.data_loader import load_entry_scores, load_sleeve_nav

    _es = load_entry_scores()
    _sv = load_sleeve_nav()

    _es_c1, _es_c2, _es_c3, _es_c4 = st.columns(4)
    with _es_c1:
        st.metric("🎯 Entry Score 평균",
                  f"{_es.get('avg_score', 0):.3f}",
                  help="진입 필터 compute_entry_score() 평균 점수 (0=차단, 1=최고)")
    with _es_c2:
        _allow = _es.get('allow_rate', 0)
        st.metric("✅ 진입 허가율",
                  f"{_allow:.1%}",
                  delta=f"{_es.get('hard_stops', 0)}건 Hard Stop")
    with _es_c3:
        st.metric("🔷 Sleeve A 수익률",
                  f"{_sv.get('sleeve_a_ret', 0)*100:+.2f}%",
                  delta=f"DD: {_sv.get('sleeve_a_dd', 0)*100:.2f}%",
                  delta_color="inverse")
    with _es_c4:
        st.metric("🔶 Sleeve B 수익률",
                  f"{_sv.get('sleeve_b_ret', 0)*100:+.2f}%",
                  delta=f"DD: {_sv.get('sleeve_b_dd', 0)*100:.2f}%",
                  delta_color="inverse")

except Exception as _new_kpi_e:
    st.caption(f"신규 지표 로드 중... ({_new_kpi_e})")

#!/usr/bin/env python3
# [Phase 78: Dashboard Refactor v4 - Live Trading & 5-Stream Modernization]
"""
3_📡_Streams.py — S0, S1, S2, S3, S10 스트림 통합 관제 (Live)
=====================================================================
"""

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import plotly.express as px

_PAGES_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PAGES_DIR.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.utils.data_loader import (
    setup_live_polling, inject_common_css, load_json,
    load_moonshot_status, load_shadow_portfolio
)

# ── 종목명 조회 헬퍼 ──
try:
    from src.data_collection.universe_loader import get_ticker_name as _get_ticker_name
except Exception:
    def _get_ticker_name(ticker: str):
        return None

_refresh_count = setup_live_polling(interval_ms=10_000, key="streams_refresh_v4")
inject_common_css()

st.markdown(
    "<div class='main-header'><h1>📡 Live Stream Performance</h1>"
    "<p>S0(Beta) · S1(Edge) · S2(ML) · S3(Factor) · S10(Macro) — 라이브 통합 관제</p></div>",
    unsafe_allow_html=True,
)

# ── SSoT 로드 ──────────────────────────────────────────────────────────────────
sm   = load_json("stream_metrics.json") or {}
sp   = load_shadow_portfolio() or {}
ls   = load_json("latest_signals.json")  or {}
s0_kelly = load_json("s0_beta_kelly.json") or {}

_metrics  = sm.get("metrics",  {})
if not _metrics:
    _metrics = sm  # Fallback to flat structure
_raw_data = sm.get("raw_data", {})


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────
def sf(v, d=0.0):
    try:
        return float(v) if v is not None else d
    except Exception:
        return d

def _is_active(m: dict) -> bool:
    if m.get("active") is True:
        return True
    if sf(m.get("market_value")) > 0:
        return True
    if int(m.get("active_positions", 0) or 0) > 0:
        return True
    return False

def _get_returns(sid: str) -> list:
    ret = _raw_data.get(sid, {}).get("daily_returns", [])
    if not ret:
        ret = _metrics.get(sid, {}).get("daily_returns", [])
    if not ret:
        ret = sp.get("daily_returns", [])
    return [r for r in ret if isinstance(r, (int, float))]

def _normalize_kpi(m: dict) -> dict:
    da       = sf(m.get("daily_alpha") or m.get("da"))
    mdd      = sf(m.get("max_drawdown") or m.get("mdd"))
    ic       = sf(m.get("ic"))
    win_rate = sf(m.get("win_rate")) * 100
    alpha    = sf(m.get("alpha"))
    return {
        "일별 알파":  round(max(0, min(100, da * 10 + 50)), 1),
        "MDD 방어":  round(max(0, min(100, 100 + mdd * 2)), 1),
        "IC 품질":   round(max(0, min(100, (ic + 1) * 50)), 1),
        "승률":      round(max(0, min(100, win_rate)), 1),
        "누적 알파": round(max(0, min(100, alpha * 5 + 50)), 1),
    }

def _radar_chart(kpis: dict, title: str) -> go.Figure:
    cats = list(kpis.keys())
    vals = list(kpis.values())
    fig = go.Figure(go.Scatterpolar(
        r=vals + [vals[0]], theta=cats + [cats[0]],
        fill="toself", fillcolor="rgba(0,120,212,0.15)",
        line=dict(color="#0078D4", width=2), name=title,
    ))
    fig.update_layout(
        polar=dict(
            radialaxis=dict(visible=True, range=[0, 100], tickfont=dict(size=9)),
            angularaxis=dict(tickfont=dict(size=11)),
        ),
        showlegend=False, template="plotly_white", height=280,
        margin=dict(t=30, b=30, l=30, r=30),
        title=dict(text=title, font=dict(size=13), x=0.5),
    )
    return fig

def _returns_bar(returns: list, sid: str) -> go.Figure:
    colors = ["#10B981" if r >= 0 else "#EF4444" for r in returns]
    fig = go.Figure(go.Bar(
        x=list(range(1, len(returns) + 1)), y=returns,
        marker_color=colors, name="Daily Return",
    ))
    fig.update_layout(
        template="plotly_white", height=200,
        margin=dict(t=20, b=20, l=10, r=10),
        xaxis_title="최근 N일", yaxis_title="수익률 (%)",
        title=dict(text=f"{sid} Daily Returns", font=dict(size=12), x=0.5),
    )
    return fig

# ── 스트림 메타 정의 ──────────────────────────────────────────────────────────
STREAM_META = {
    "S0": {
        "label": "S0 Beta Tracker", "icon": "🔵",
        "desc":  "방향성 베타 스트림. 완전 Kelly 공식 운영. 레버리지/인버스 베타 추종.",
        "hint":  "시장 방향성에 능동적으로 대응",
    },
    "S1": {
        "label": "S1 Edge", "icon": "⚡",
        "desc":  "코스피 ETF 레버리지 모멘텀 + 인버스 스위칭. 단기 방향성 전략.",
        "hint":  "레버리지(2x)/인버스/현금 3선택 구조",
    },
    "S2": {
        "label": "S2 ML Alpha", "icon": "🧠",
        "desc":  "앙상블 ML 모델(LightGBM·XGB·CatBoost) 기반 개별주 롱 알파.",
        "hint":  "proba > 0.80 → 매수 시그널",
    },
    "S3": {
        "label": "S3 Factor", "icon": "🏭",
        "desc":  "섹터 로테이션 + 딥 밸류/스타일 팩터(모멘텀·밸류·퀄리티) + 동적 손절(ATR).",
        "hint":  "IC 가중 팩터 스코어 기반 롱 바스켓",
    },
    "S10": {
        "label": "S10 Macro Ensemble", "icon": "🌍",
        "desc":  "매크로 앙상블 모듈. 거시경제 지표 기반의 섹터 및 자산 배분 비중 조절.",
        "hint":  "최근 비중 축소 반영. 자본 배분 효율화.",
    },
}

tabs = st.tabs([f"{v['icon']} {v['label']}" for v in STREAM_META.values()])

for tab, (sid, meta) in zip(tabs, STREAM_META.items()):
    with tab:
        is_s0 = (sid == "S0")
        
        # 데이터 매핑 (S0는 s0_beta_kelly 등 복합 참조)
        m = _metrics.get(sid, _metrics.get(sid.lower(), {}))
        if is_s0:
            m = _metrics.get('S0', _metrics.get('s0', _metrics.get('S0_beta', {})))

        _active = _is_active(m)

        if _active:
            status       = "🟢 ACTIVE"
            status_color = "#10B981"
            status_tip   = "전략이 라이브 환경에서 현재 포지션을 보유 중입니다."
        else:
            status       = "⚪ 대기중(Idle)"
            status_color = "#9CA3AF"
            status_tip   = "현재 보유 포지션 없음. 에러가 아닌 정상 대기 상태입니다."

        # ── 전략 요약 박스 ────────────────────────────────────────────────────
        sig_val = m.get("signal") or m.get("current_signal", "—")
        pos_cnt = int(m.get("active_positions", 0) or 0)
        nav_val = sf(m.get("market_value"))
        ls_sigs = ls.get("signals", {}).get(sid, [])
        sig_cnt = len(ls_sigs) if isinstance(ls_sigs, list) else 0
        st.markdown(f"""
        <div style="background:#f8fafc;border-left:4px solid #0078D4;padding:14px 18px;
             border-radius:8px;margin-bottom:12px;">
          <div style="font-size:1.05rem;font-weight:700;color:#1e3a5f;">
            {meta['icon']} {meta['label']}
            <span style="color:{status_color};font-size:0.88rem;margin-left:8px;"
                  title="{status_tip}">{status}</span>
          </div>
          <div style="color:#374151;font-size:0.85rem;margin-top:4px;">{meta['desc']}</div>
          <hr style="margin:8px 0;border-color:#CBD5E1;">
          <div style="display:flex;gap:24px;flex-wrap:wrap;font-size:0.88rem;">
            <span>📊 <b>시그널:</b> <code>{sig_val}</code></span>
            <span>📂 <b>보유:</b> {pos_cnt}종목</span>
            <span>💰 <b>라이브 평가액:</b> ₩{nav_val:,.0f}</span>
            <span>🔔 <b>신규 시그널:</b> {sig_cnt}건</span>
          </div>
          <div style="color:#6B7280;font-size:0.78rem;margin-top:6px;">💡 {meta['hint']}</div>
        </div>""", unsafe_allow_html=True)

        # ── S0 Beta 전용 대시보드 ───────────────────────────────────────────
        if is_s0:
            st.markdown("##### 🎲 S0 Beta Kelly 실측 데이터")
            _s0_sub = sp.get('sub_accounts', {}).get('S0', {})
            _s0_nav = float(_s0_sub.get('nav', 0) or m.get('nav', 0) or 0)
            _s0_sharpe = float(m.get('sharpe', 0) or 0)
            _win_rate = float(s0_kelly.get('win_rate', 0) or m.get('win_rate', 0) or 0)
            _payoff = float(s0_kelly.get('payoff_ratio', 0) or m.get('payoff_ratio', 0) or 0)
            
            _s0_c1, _s0_c2, _s0_c3, _s0_c4 = st.columns(4)
            _s0_c1.metric("💰 라이브 S0 NAV", f"₩{_s0_nav:,.0f}" if _s0_nav > 0 else "N/A")
            _s0_c2.metric("📈 Sharpe", f"{_s0_sharpe:.3f}" if _s0_sharpe else "N/A")
            _s0_c3.metric("🎯 Win Rate", f"{_win_rate:.1%}" if _win_rate else "N/A")
            _s0_c4.metric("💱 Payoff Ratio", f"{_payoff:.2f}x" if _payoff else "N/A")

            _kelly_f = float(s0_kelly.get('kelly_f', 0) or 0)
            _hmm_prob = float(s0_kelly.get('hmm_prob', 0) or 0)
            _mixed_p  = float(s0_kelly.get('mixed_win_rate', 0) or 0)

            if any([_kelly_f, _hmm_prob, _mixed_p]):
                _k_c1, _k_c2, _k_c3 = st.columns(3)
                _k_c1.metric("🎲 HMM 확률", f"{_hmm_prob:.1%}" if _hmm_prob else "N/A")
                _k_c2.metric("🔀 혼합 승률", f"{_mixed_p:.1%}" if _mixed_p else "N/A")
                _k_c3.metric("🏦 Kelly f*", f"{_kelly_f:.1%}" if _kelly_f > 0 else "진입 차단")

        # ── 보유 포지션 & 실현 P&L ──────────────────────────────────────────
        _sp_positions = sp.get("positions", {})
        _stream_pos   = {}
        for pk, pos in (_sp_positions.items() if isinstance(_sp_positions, dict) else []):
            _pid_stream = pk.split(":")[0] if ":" in pk else pos.get("stream_id", "")
            if _pid_stream == sid:
                _stream_pos[pk] = pos

        _stream_pnl = sf(m.get("realized_pnl") or m.get("pnl"))
        _sp_pnl_map = sp.get("strategy_pnl", {})
        _sp_pnl_sid = sf(_sp_pnl_map.get(sid))

        st.markdown("---")
        st.markdown("##### 💼 실거래 보유 포지션 & P&L")
        col_pnl, col_pos = st.columns([1, 2])

        with col_pnl:
            _pnl_val   = _stream_pnl or _sp_pnl_sid
            _pnl_color = "#10B981" if _pnl_val >= 0 else "#EF4444"
            _pnl_icon  = "▲" if _pnl_val >= 0 else "▼"
            st.markdown(f"""
            <div style="background:#f8fafc;border:1px solid #E5E7EB;border-radius:10px;
                 padding:16px;text-align:center;">
              <div style="color:#6B7280;font-size:0.78rem;text-transform:uppercase;letter-spacing:.06em;">라이브 실현 P&L</div>
              <div style="color:{_pnl_color};font-size:1.6rem;font-weight:700;margin-top:6px;">
                {_pnl_icon} ₩{abs(_pnl_val):,.0f}
              </div>
              <div style="color:#9CA3AF;font-size:0.75rem;margin-top:4px;">
                총 거래: {int(m.get('total_trades', 0) or 0)}건
              </div>
            </div>""", unsafe_allow_html=True)

        with col_pos:
            if _stream_pos:
                rows_pos = []
                for pk, pos in list(_stream_pos.items())[:10]:
                    rows_pos.append({
                        "종목":      str(pos.get("name", pk)),
                        "수량":      int(pos.get("quantity") or pos.get("qty") or 0),
                        "평균단가":  f"₩{sf(pos.get('avg_price')):,.0f}",
                        "평가액":    f"₩{sf(pos.get('market_value')):,.0f}",
                        "미실현PnL": f"₩{sf(pos.get('unrealized_pnl')):+,.0f}",
                    })
                st.dataframe(pd.DataFrame(rows_pos), hide_index=True, use_container_width=True)
            else:
                st.markdown(
                    "<div style='background:#F9FAFB;border:1px dashed #D1D5DB;"
                    "border-radius:8px;padding:16px;text-align:center;color:#9CA3AF;'>"
                    "⚪ 보유 포지션 없음 (대기 중)<br>"
                    "<small>라이브 환경에서 매수 조건 충족 시 자동 진입합니다.</small>"
                    "</div>",
                    unsafe_allow_html=True,
                )

        # ── KPI 숫자 메트릭 ─────────────────────────────────────────────────
        st.markdown("---")
        pnl      = int(m.get("realized_pnl") or 0)
        mdd      = sf(m.get("max_drawdown") or m.get("mdd"))
        sortino  = sf(m.get("sortino"))
        win_rate = sf(m.get("win_rate"))
        alpha    = sf(m.get("alpha"))
        sharpe   = sf(m.get("sharpe"))
        
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("실현 손익",   f"₩{pnl:,.0f}")
        c2.metric("MDD",        f"{mdd:.1f}%")
        c3.metric("Sortino",    f"{sortino:.2f}")
        c4.metric("Win Rate",   f"{win_rate:.1%}")
        c5.metric("누적 Alpha", f"{alpha:+.2f}%")
        c6.metric("Sharpe",     f"{sharpe:.2f}")

        # ── Radar + Daily Returns ─────────────────────────────────────────────
        col_r, col_b = st.columns(2)
        with col_r:
            kpis_norm = _normalize_kpi(m)
            any_kpi   = any(v not in (50.0, 0.0) for v in kpis_norm.values())
            if any_kpi:
                st.plotly_chart(_radar_chart(kpis_norm, f"{sid} KPI Radar"), use_container_width=True)
            else:
                st.markdown(
                    "<div style='background:#F9FAFB;border:1px dashed #D1D5DB;"
                    "border-radius:10px;padding:24px;text-align:center;color:#9CA3AF;height:280px;"
                    "display:flex;flex-direction:column;justify-content:center;'>"
                    "⚪ Radar 데이터 수집 중<br>"
                    "<small>KPI 데이터는 실거래 완료 후 갱신됩니다.</small>"
                    "</div>", unsafe_allow_html=True,
                )
        with col_b:
            rets = _get_returns(sid)
            if rets:
                st.plotly_chart(_returns_bar(rets[-30:], sid), use_container_width=True)
            else:
                st.markdown(
                    "<div style='background:#F9FAFB;border:1px dashed #D1D5DB;"
                    "border-radius:10px;padding:24px;text-align:center;color:#9CA3AF;height:200px;"
                    "display:flex;flex-direction:column;justify-content:center;'>"
                    "⚪ 일간 수익률 데이터 없음<br>"
                    "</div>", unsafe_allow_html=True,
                )

        # ── Latest Signals 테이블 ───────────────────────────────────────────
        if ls_sigs and isinstance(ls_sigs, list):
            st.markdown("---")
            with st.expander(f"📋 라이브 최신 시그널 ({len(ls_sigs)}건)", expanded=False):
                sig_rows = []
                for s in ls_sigs[:15]:
                    _sig_tk = str(s.get("ticker", ""))
                    _sig_name = (_get_ticker_name(_sig_tk) or str(s.get("name", "")) or "N/A")[:14]
                    sig_rows.append({
                        "Ticker": _sig_tk,
                        "종목명": _sig_name,
                        "방향":   str(s.get("direction", s.get("action", ""))),
                        "Size":   f"{sf(s.get('size_pct'))*100:.1f}%" if s.get("size_pct") else "—",
                        "이유":   str(s.get("reason", ""))[:30],
                    })
                if sig_rows:
                    st.dataframe(pd.DataFrame(sig_rows), hide_index=True, use_container_width=True)

st.markdown("---")
_ts = sm.get("timestamp", sm.get("updated_at", "N/A"))
st.caption(f"📅 업데이트: {_ts} | 🟢 Live 10초 자동 새로고침 #{_refresh_count} | SSoT: stream_metrics.json")

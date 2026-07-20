#!/usr/bin/env python3
"""
Page 4: Execution — 주문 실행 / 거래내역
# [SSOT Refactoring] shadow_portfolio.json + tca_summary.json SSoT만 읽음. 자체 계산 없음.
# [Live Polling] 10초 자동 새로고침.
"""

import sys
from datetime import datetime
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
    load_execution_data, load_json, safe_float, safe_fmt,
    metric_card_html,
)

# ── 종목명 조회 헬퍼 (universe_loader 없는 환경도 크래시 없이 작동) ──
try:
    from src.data_collection.universe_loader import get_ticker_name as _get_ticker_name
except Exception:
    def _get_ticker_name(ticker: str):  # type: ignore[misc]
        return None

_refresh_count = setup_live_polling(interval_ms=10_000, key="execution_refresh")
inject_common_css()

st.markdown(
    "<div class='main-header'><h1>⚡ Execution</h1>"
    "<p>주문 실행 내역 · TCA 분석 (Live Execution SSoT)</p></div>",
    unsafe_allow_html=True,
)

# [SSOT Refactoring] 실행 데이터 로드 — data_loader.py 경유
ex = load_execution_data()

trades = ex.get("shadow_trades", [])
real_sells = ex.get("real_sells", [])
real_buys = ex.get("real_buys", [])
_pnl = ex.get("realized_pnl", 0.0)
_trades_n = ex.get("realized_trades", 0)
_wins_n = ex.get("realized_wins", 0)
_cash = ex.get("cash", 0.0)
_nav = ex.get("virtual_nav", 0.0)
_pending = ex.get("pending_orders", [])

# ── 핵심 지표 ───────────────────────────────────────────────────────────────
# [Phase 77] stMetric 폰트: COMMON_CSS(inject_common_css) 에서 SSOT 첫지로 관리
c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.metric("실현 P&L", f"₩{_pnl:+,.0f}")
with c2:
    _wr = (_wins_n / _trades_n * 100) if _trades_n > 0 else 0.0
    st.metric("승률", f"{_wr:.1f}%", delta=f"{_wins_n}W/{_trades_n-_wins_n}L")
with c3:
    st.metric("총 매도 거래", f"{_trades_n}건")
with c4:
    st.metric("총 매수 거래", f"{len(real_buys)}건")
with c5:
    st.metric("현금", f"₩{_cash:,.0f}")

st.markdown("---")

# ── 대기 주문 ─────────────────────────────────────────────────────────────────
if _pending:
    st.subheader(f"⏳ 대기 주문 ({len(_pending)}건)")
    _pend_rows = []
    for o in _pending[:20]:
        _pd_tk = str(o.get("ticker") or "")
        _pend_rows.append({
            "Ticker": _pd_tk,
            "종목명": _get_ticker_name(_pd_tk) or str(o.get("name") or "N/A"),
            "Stream": str(o.get("stream_id") or ""),
            "방향": str(o.get("direction") or o.get("action") or ""),
            "금액(원)": f"₩{safe_float(o.get('amount_krw')):,.0f}",
            "Confidence": f"{safe_float(o.get('confidence'))*100:.1f}%",
            "이유": str(o.get("reason") or "")[:50],
        })
    st.dataframe(pd.DataFrame(_pend_rows), hide_index=True, use_container_width=True)
else:
    st.info("대기 중인 주문 없음")

st.markdown("---")

# ── 거래 이력 ─────────────────────────────────────────────────────────────────
st.subheader(f"📋 실시간 체결 이력 (Live Execution SSoT) — 총 {len(trades)}건")

if trades:
    # 1) 전체 데이터 준비
    _trade_rows = []
    _today_str = datetime.now().strftime("%Y-%m-%d")
    
    for t in reversed(trades[-500:]):  # 최근 500건 (확장)
        _pnl_t = safe_float(t.get("realized_pnl"))
        _pnl_cls = "🟢" if _pnl_t > 0 else ("🔴" if _pnl_t < 0 else "⚪")
        _date_str = str(t.get("timestamp") or t.get("date") or "")[:19]
        
        # timestamp 값이 "YYYY-MM-DD" 형식이 아닐 경우 (e.g. timestamp가 비어있다면) date로 Fallback
        if not _date_str:
            _date_str = "N/A"
            
        _trade_rows.append({
            "날짜": _date_str,
            "Stream": str(t.get("stream_id") or t.get("stream") or "N/A"),
            "Ticker": str(t.get("ticker") or ""),
            # universe_loader 우선 → 기록된 name 필드 fallback
            "종목명": (
                _get_ticker_name(str(t.get("ticker") or ""))
                or str(t.get("name") or "")
            )[:15],
            "액션": str(t.get("action") or ""),
            "수량": int(t.get("qty") or t.get("quantity") or 0),
            "체결가": f"₩{safe_float(t.get('price')):,.0f}",
            "실현P&L": f"{_pnl_cls} ₩{_pnl_t:+,.0f}" if _pnl_t else "-",
            "이유": str(t.get("reason") or "")[:40],
        })
        
    df_trades = pd.DataFrame(_trade_rows)
    
    # 2) 당일 데이터 필터링
    df_today = df_trades[df_trades["날짜"].str.startswith(_today_str)]
    
    # 3) 스트림 목록 추출
    _streams = sorted(list(df_trades["Stream"].unique()))
    
    # 탭 구성: [전체] [당일] + [스트림별...]
    tab_labels = ["전체 거래내역", f"당일 거래내역 ({len(df_today)}건)"] + [f"스트림 {s}" for s in _streams]
    tabs = st.tabs(tab_labels)
    
    with tabs[0]:
        st.dataframe(df_trades.head(100), hide_index=True, use_container_width=True)
        st.caption("최근 100건 표시")
        
    with tabs[1]:
        if not df_today.empty:
            st.dataframe(df_today, hide_index=True, use_container_width=True)
        else:
            st.info(f"오늘({_today_str}) 발생한 거래가 없습니다.")
            
    for i, s in enumerate(_streams):
        with tabs[i+2]:
            df_s = df_trades[df_trades["Stream"] == s]
            st.dataframe(df_s.head(100), hide_index=True, use_container_width=True)
            st.caption(f"{s} 스트림 거래 총 {len(df_s)}건 중 최근 100건 표시")

else:
    st.info("거래 이력 없음 (Live Portfolio 구축 전 또는 cleanup 직후)")

st.markdown("---")

# ── P&L 히스토리 차트 ─────────────────────────────────────────────────────────
st.subheader("📈 누적 실현 P&L")
if real_sells:
    _pnl_running = []
    _cum_pnl = 0.0
    for t in real_sells:
        _cum_pnl += safe_float(t.get("realized_pnl"))
        _pnl_running.append({
            "날짜": str(t.get("timestamp") or "")[:10],
            "누적P&L": _cum_pnl,
            "Stream": str(t.get("stream_id") or ""),
        })
    if _pnl_running:
        df_pnl = pd.DataFrame(_pnl_running)
        fig = px.line(df_pnl, x="날짜", y="누적P&L", color="Stream",
                      title="스트림별 누적 실현 P&L (원)",
                      template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)

# ── TCA 분석 ────────────────────────────────────────────────────────────────────────────────────
tca = ex.get("tca_summary", {})
if tca:
    st.markdown("---")
    st.subheader("🔬 TCA (Transaction Cost Analysis)")

    # ───────────────────────────────────────────────────────────────────────────────────
    # ① 상단 핵심 메트릭 카드 (4개)
    # ───────────────────────────────────────────────────────────────────────────────────
    _n_trades       = int(tca.get("n_trades", 0))
    _total_amt      = safe_float(tca.get("total_amount"))
    _avg_cost_bps   = safe_float(tca.get("avg_total_cost_bps"))
    _total_cost_krw = safe_float(tca.get("total_cost_krw"))
    _tca_date       = str(tca.get("date") or tca.get("timestamp", "")[:10])

    _c1, _c2, _c3, _c4 = st.columns(4)
    with _c1:
        st.markdown(
            metric_card_html(
                "포지션 보유 종목",
                f"{_n_trades}건",
                _tca_date,
                "delta-neutral",
            ),
            unsafe_allow_html=True,
        )
    with _c2:
        st.markdown(
            metric_card_html(
                "종 거래대금",
                f"₩{_total_amt:,.0f}",
                delta="",
            ),
            unsafe_allow_html=True,
        )
    with _c3:
        # 평균 체결비용: 낮을수록 좋음 (10bps 이하 = 양호)
        _cost_grade = (
            "delta-positive" if _avg_cost_bps < 10
            else ("delta-negative" if _avg_cost_bps > 20 else "delta-neutral")
        )
        st.markdown(
            metric_card_html(
                "평균 체결비용",
                f"{_avg_cost_bps:.2f} bps",
                delta="▼ 양호" if _avg_cost_bps < 10 else ("▲ 주의" if _avg_cost_bps > 20 else "■ 보통"),
                delta_cls=_cost_grade,
            ),
            unsafe_allow_html=True,
        )
    with _c4:
        st.markdown(
            metric_card_html(
                "종 발생 비용",
                f"₩{_total_cost_krw:,.0f}",
                delta="트랜삭비용 합산",
                delta_cls="delta-negative" if _total_cost_krw > 0 else "delta-neutral",
                value_color="#DC2626" if _total_cost_krw > 500_000 else "#111111",
            ),
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ───────────────────────────────────────────────────────────────────────────────────
    # ② 비용 분해 차트 (Horizontal Bar) + 등급 분포 (도넛)
    # ───────────────────────────────────────────────────────────────────────────────────
    _col_left, _col_right = st.columns([1, 1])

    with _col_left:
        st.markdown("**📊 체결비용 세부 분해 (bps)**")
        _cost_labels = [
            "Implementation\nShortfall",
            "Market\nImpact",
            "VWAP\nSlippage",
        ]
        _cost_vals = [
            safe_float(tca.get("avg_is_bps")),
            safe_float(tca.get("avg_market_impact_bps")),
            safe_float(tca.get("avg_vwap_slippage_bps")),
        ]
        # 값 기준 색상 설정: IS > 15 → 주황혁, 나머지 파란 계열
        _bar_colors = [
            "#EF4444" if v > 15 else ("#F59E0B" if v > 8 else "#3B82F6")
            for v in _cost_vals
        ]
        import plotly.graph_objects as _go
        _fig_bar = _go.Figure(_go.Bar(
            x=_cost_vals,
            y=_cost_labels,
            orientation="h",
            marker=dict(color=_bar_colors, line=dict(width=0)),
            text=[f"{v:.2f} bps" for v in _cost_vals],
            textposition="auto",
        ))
        _fig_bar.update_layout(
            template="plotly_white",
            height=220,
            margin=dict(l=10, r=20, t=10, b=10),
            xaxis=dict(title="bps", showgrid=True, gridcolor="#F3F4F6"),
            yaxis=dict(showgrid=False),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(_fig_bar, use_container_width=True)

    with _col_right:
        st.markdown("**🎓 체결 품질 등급 분포**")
        _grade_dist = tca.get("grade_distribution", {})
        _grade_labels = list(_grade_dist.keys())
        _grade_vals   = [int(_grade_dist.get(g, 0)) for g in _grade_labels]
        # A:녹색, B:코발트, C:노란, D:주황, F:빨간
        _grade_colors = {
            "A": "#10B981", "B": "#3B82F6",
            "C": "#F59E0B", "D": "#F97316", "F": "#EF4444",
        }
        _pie_colors = [_grade_colors.get(g, "#9CA3AF") for g in _grade_labels]
        import plotly.express as _px
        _fig_pie = _go.Figure(_go.Pie(
            labels=[f"등급 {g}" for g in _grade_labels],
            values=_grade_vals,
            hole=0.45,
            marker=dict(colors=_pie_colors),
            textinfo="label+percent",
            hovertemplate="%{label}: %{value}건 (%{percent})<extra></extra>",
        ))
        _total_graded = sum(_grade_vals) or 1
        _dominant_grade = max(_grade_dist, key=lambda g: _grade_dist.get(g, 0))
        _fig_pie.add_annotation(
            text=f"<b>{_dominant_grade}</b><br>{_grade_dist.get(_dominant_grade, 0)}건",
            font=dict(size=16, color=_grade_colors.get(_dominant_grade, "#111")),
            showarrow=False, x=0.5, y=0.5,
        )
        _fig_pie.update_layout(
            template="plotly_white",
            height=220,
            margin=dict(l=0, r=0, t=10, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=-0.25, x=0.5, xanchor="center"),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(_fig_pie, use_container_width=True)

    # ───────────────────────────────────────────────────────────────────────────────────
    # ③ Best / Worst 체결 하이라이트
    # ───────────────────────────────────────────────────────────────────────────────────
    _best  = tca.get("best_trade", {})
    _worst = tca.get("worst_trade", {})

    if _best or _worst:
        _hc_left, _hc_right = st.columns(2)

        def _trade_badge(grade: str) -> str:
            _g_color = {
                "A": "#10B981", "B": "#3B82F6",
                "C": "#F59E0B", "D": "#F97316", "F": "#EF4444",
            }.get(grade, "#6B7280")
            return (
                f"<span style='background:{_g_color};color:#fff;"
                f"padding:2px 8px;border-radius:4px;font-size:0.75rem;"
                f"font-weight:700;'>{grade}</span>"
            )

        def _action_icon(action: str) -> str:
            return "🟢 매수" if str(action).lower() == "buy" else "🔴 매도"

        with _hc_left:
            _b_tk  = str(_best.get("ticker", "N/A"))
            _b_name = _get_ticker_name(_b_tk) or _b_tk
            _b_act  = _action_icon(_best.get("action", ""))
            _b_cost = safe_float(_best.get("total_cost_bps"))
            _b_grade = str(_best.get("quality_grade", "?"))
            _b_ts   = str(_best.get("timestamp", ""))[:16]
            st.markdown(
                f"""
                <div style='background:#F0FDF4;border-left:4px solid #10B981;
                padding:14px 18px;border-radius:8px;'>
                  <p style='margin:0 0 6px;font-size:0.8rem;color:#065F46;
                  font-weight:700;letter-spacing:.05em;'>✨ BEST EXECUTION</p>
                  <p style='margin:0;font-size:1.05rem;font-weight:700;color:#111;'>
                    {_b_name}&nbsp;&nbsp;<span style='font-size:0.85rem;color:#6B7280;'>({_b_tk})</span>
                  </p>
                  <p style='margin:4px 0;font-size:0.88rem;color:#374151;'>
                    {_b_act} &nbsp;|&nbsp; 총비용 {_b_cost:.2f} bps &nbsp;|&nbsp; {_trade_badge(_b_grade)}
                  </p>
                  <p style='margin:0;font-size:0.78rem;color:#9CA3AF;'>{_b_ts}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with _hc_right:
            _w_tk   = str(_worst.get("ticker", "N/A"))
            _w_name = _get_ticker_name(_w_tk) or _w_tk
            _w_act  = _action_icon(_worst.get("action", ""))
            _w_cost = safe_float(_worst.get("total_cost_bps"))
            _w_grade = str(_worst.get("quality_grade", "?"))
            _w_ts   = str(_worst.get("timestamp", ""))[:16]
            _w_opp  = safe_float(_worst.get("opportunity_cost_bps"))
            st.markdown(
                f"""
                <div style='background:#FEF2F2;border-left:4px solid #EF4444;
                padding:14px 18px;border-radius:8px;'>
                  <p style='margin:0 0 6px;font-size:0.8rem;color:#991B1B;
                  font-weight:700;letter-spacing:.05em;'>⚠️ WORST EXECUTION</p>
                  <p style='margin:0;font-size:1.05rem;font-weight:700;color:#111;'>
                    {_w_name}&nbsp;&nbsp;<span style='font-size:0.85rem;color:#6B7280;'>({_w_tk})</span>
                  </p>
                  <p style='margin:4px 0;font-size:0.88rem;color:#374151;'>
                    {_w_act} &nbsp;|&nbsp; 총비용 {_w_cost:.2f} bps &nbsp;|&nbsp; {_trade_badge(_w_grade)}
                  </p>
                  <p style='margin:2px 0;font-size:0.8rem;color:#B91C1C;'>
                    기회비용 {_w_opp:+.2f} bps
                  </p>
                  <p style='margin:0;font-size:0.78rem;color:#9CA3AF;'>{_w_ts}</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.caption(
        f"tca_summary.json SSoT | 체결 {_n_trades}건 종합 | "
        f"매일 체결 완료 후 자동 갱신"
    )


# ── Advisory 주문 ─────────────────────────────────────────────────────────────
s4_adv = ex.get("s4_advisory", {})
legacy_adv = ex.get("advisory_orders", {})

if s4_adv or legacy_adv:
    st.markdown("---")
    st.subheader("🏦 수동 매매 Advisory (ISA / 연금 / IRP)")

    if s4_adv:
        st.markdown("##### 🇰🇷 S4 (계좌별 자금 한도 기반 Advisory)")
        _recs = s4_adv.get("recommendations", {})
        for acct in ["ISA", "IRP", "PENSION"]:
            acct_recs = _recs.get(acct, {})
            buys = acct_recs.get("buy", [])
            sells = acct_recs.get("sell", [])
            if buys or sells:
                st.markdown(f"**[{acct}]**")
                rows = []
                for b in buys:
                    rows.append({
                        "방향": "🟢 매수", "종목": b.get("name", b.get("ticker")),
                        "수량": f"{b.get('quantity', 0)}주",
                        "예상금액": f"₩{safe_float(b.get('invest_amount')):,.0f}",
                        "이유": b.get("reason", "")[:30]
                    })
                for s in sells:
                    rows.append({
                        "방향": "🔴 매도", "종목": s.get("name", s.get("ticker")),
                        "수량": f"{s.get('quantity', 0)}주",
                        "예상금액": f"₩{safe_float(s.get('sell_amount')):,.0f}",
                        "이유": str(s.get("reasons", s.get("reason", "")))[:30]
                    })
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    if legacy_adv and not s4_adv:
        st.markdown("##### 🇰🇷 S4 (Legacy)")
        _adv_list = legacy_adv if isinstance(legacy_adv, list) else legacy_adv.get("orders", [])
        if isinstance(_adv_list, list) and _adv_list:
            st.dataframe(pd.DataFrame(_adv_list), hide_index=True, use_container_width=True)

st.caption(f"🟢 Live Polling 활성 — 10초 자동 새로고침 #{_refresh_count}")

#!/usr/bin/env python3
"""
Page 7: Alpha Factory v2 — 자가 발전형 알파 탐색 실시간 모니터
==============================================================
[Phase 18: SS-ETF + Alpha Factory v2]

변경사항:
  - OOS IC 기반 성과 지표 (기존 가짜 Sharpe 대체)
  - active/retired/inactive 상태 분류 탭
  - 동적 피처 사용 빈도 차트 (하드코딩 없음)
  - IC Decay 히스토리 라인 차트
  - mtime 기반 캐시 자동 무효화 (파일 변경 → 즉시 갱신)
  - 수동 캐시 클리어 버튼 (사이드바)
  - 모든 데이터 참조 Graceful Fallback (Crash-safe)
"""

import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

_PAGES_DIR    = Path(__file__).resolve().parent
_PROJECT_ROOT = _PAGES_DIR.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.utils.data_loader import (
    setup_live_polling,
    inject_common_css,
    render_cache_clear_button,
    load_alpha_factory_v2,
    load_alpha_factory,          # 하위 호환 (v1 데이터도 표시)
    safe_float,
    safe_fmt,
    _get_alpha_mtime,
)

# ── 페이지 설정 ───────────────────────────────────────────────
st.set_page_config(
    page_title="Alpha Factory | Meridian",
    page_icon="🧠",
    layout="wide",
)

_refresh_count = setup_live_polling(interval_ms=10_000, key="alpha_factory_v2_refresh")
inject_common_css()

# [Phase 18] 사이드바 캐시 클리어 버튼
render_cache_clear_button(location='sidebar', page_key='alpha_factory')

# ── 추가 스타일 (페이지 고유) ─────────────────────────────────
st.markdown("""
<style>
.ic-badge {
    display: inline-block; border-radius: 20px; padding: 3px 12px;
    font-size: 0.8rem; font-weight: 700; letter-spacing: 0.03em;
}
.ic-excellent { background: #00c853; color: #000 !important; }
.ic-good      { background: #2196f3; color: #fff !important; }
.ic-marginal  { background: #ff8f00; color: #fff !important; }
.ic-dead      { background: #9e9e9e; color: #fff !important; }

.status-active  { background: #00c853; color: #000 !important; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 700; }
.status-retired { background: #9e9e9e; color: #fff !important; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; }
.status-inactive{ background: #ffd740; color: #000 !important; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; }

.cache-info { font-size: 0.72rem; color: #666 !important; text-align: right; margin-top: 0.4rem; }
</style>
""", unsafe_allow_html=True)

# ── 헤더 (배경 없음, 검정 폰트) ──────────────────────────────
st.title("🧠 Alpha Factory v2")
st.caption("Genetic Programming 기반 자가 발전형 알파 발굴 · OOS IC 실시간 추적 · IC Decay 자동 퇴출")

# ── [Phase 18] mtime 기반 캐시 키 → 파일 변경 시 즉시 갱신 ──
_alpha_mtime = _get_alpha_mtime()

# v2 데이터 로드 (mtime 캐시 키 전달)
af2 = load_alpha_factory_v2(_mtime=_alpha_mtime)
af1 = load_alpha_factory()   # 하위 호환용 (v1 포맷 폴백)

n_total  = af2.get('n_total', 0)
n_active = af2.get('n_active', 0)
active   = af2.get('active', [])
retired  = af2.get('retired', [])
inactive = af2.get('inactive', [])
best     = af2.get('best_active', {})
feat_usage = af2.get('feature_usage', {})

# ── 캐시 상태 정보 ────────────────────────────────────────────
_ts_str = (
    datetime.fromtimestamp(_alpha_mtime).strftime('%Y-%m-%d %H:%M:%S')
    if _alpha_mtime > 0 else '파일 없음'
)
st.markdown(
    f"<div class='cache-info'>📁 discovered_alphas.json 최종 수정: "
    f"<code>{_ts_str}</code> | 캐시 TTL: 60초 | "
    f"폴링 #{_refresh_count}</div>",
    unsafe_allow_html=True,
)

# ── KPI 요약 메트릭 ───────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    st.metric("🧬 총 알파", n_total, help="전체 발굴 알파 수")
with c2:
    delta_active = f"+{n_active}" if n_active > 0 else "0"
    st.metric("✅ 활성", n_active, delta=delta_active if n_active > 0 else None)
with c3:
    st.metric("⚰️ 퇴출", len(retired), help="IC Decay로 퇴출된 알파")
with c4:
    best_ic = safe_float(best.get('oos_ic', best.get('sharpe_ratio', 0.0)))
    st.metric("🏆 최고 IC", f"{best_ic:.4f}" if best_ic else "N/A")
with c5:
    st.metric("📊 추적 피처", len(feat_usage), help="알파가 사용한 고유 피처 수")

st.markdown("---")

# ── 데이터 없는 경우 ──────────────────────────────────────────
if not af2.get('all'):
    st.warning(
        "⚠️ Alpha Factory 결과 없음 — `discovered_alphas.json` 아직 생성되지 않았습니다.\n\n"
        "```bash\npython3 src/alpha_factory/alpha_miner.py --mode mine --gens 5 --pop 100\n```"
    )
    st.caption(f"🟢 Live Polling #{_refresh_count} | mtime 기반 캐시 자동 갱신 활성")
    st.stop()

# ── 메인 탭 ──────────────────────────────────────────────────
tab_active, tab_gc, tab_all, tab_features, tab_gc_run = st.tabs([
    f"✅ 활성 알파 ({n_active})",
    "⚰️ 퇴출 알파",
    f"📜 전체 히스토리 ({n_total})",
    "🔬 피처 분석",
    "🗑️ GC 상태",
])

# ══════════════════════════════════════════════════════════════
# TAB 1: 활성 알파
# ══════════════════════════════════════════════════════════════
with tab_active:
    if not active:
        st.info(
            "현재 활성 알파 없음.\n\n"
            "AlphaMiner를 실행하거나, 기존 알파의 status를 'active'로 수동 변경하세요."
        )
    else:
        st.subheader(f"✅ 활성 알파 — {len(active)}개 (OOS IC 내림차순)")

        for rank, alpha in enumerate(active):
            _id       = str(alpha.get('id', f'alpha_{rank+1}'))
            _formula  = str(alpha.get('formula', 'N/A'))
            _py_expr  = str(alpha.get('py_expr', ''))
            _oos_ic   = safe_float(alpha.get('oos_ic', alpha.get('sharpe_ratio', 0.0)))
            _oos_std  = safe_float(alpha.get('oos_ic_std', 0.0))
            _ic_pval  = safe_float(alpha.get('ic_pvalue', 1.0))
            _fitness  = safe_float(alpha.get('fitness', 0.0))
            _col_name = str(alpha.get('col_name', f'auto_alpha_{rank+1:03d}'))
            _disc_at  = str(alpha.get('discovered_at', ''))[:19]
            _corr     = safe_float(alpha.get('max_corr', 0.0))
            _corr_f   = str(alpha.get('max_corr_feature', ''))
            _feats    = alpha.get('features_used') or []
            _ic_hist  = alpha.get('ic_history', [])

            # IC 등급 배지
            if _oos_ic >= 0.10:
                ic_cls, ic_label = 'ic-excellent', '탁월 (IC≥0.10)'
            elif _oos_ic >= 0.06:
                ic_cls, ic_label = 'ic-good',      '우수 (IC≥0.06)'
            elif _oos_ic >= 0.03:
                ic_cls, ic_label = 'ic-marginal',  '한계 (IC≥0.03)'
            else:
                ic_cls, ic_label = 'ic-dead',      '미달 (<0.03)'

            with st.expander(
                f"#{rank+1}  `{_col_name}`  |  OOS IC: {_oos_ic:.4f}  |  발굴일: {_disc_at}",
                expanded=(rank == 0),
            ):
                ca, cb_ = st.columns([3, 1])
                with ca:
                    st.markdown(
                        f"**ML 컬럼명:** `{_col_name}` "
                        f"<span class='status-active'>ACTIVE</span>  "
                        f"<span class='ic-badge {ic_cls}'>{ic_label}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**Alpha ID:** `{_id}`")

                with cb_:
                    st.metric("OOS IC",   f"{_oos_ic:.4f}")
                    st.metric("IC Std",   f"{_oos_std:.4f}")
                    st.metric("p-value",  f"{_ic_pval:.4f}")

                # gplearn 수식 (원본)
                st.markdown("**🧮 gplearn 원본 수식 (GP 폴란드 표기)**")
                st.code(_formula, language='python')

                # pandas eval 번역 수식
                if _py_expr and _py_expr != '0.0':
                    st.markdown("**🐍 pandas eval 번역 수식 (파이프라인 주입용)**")
                    st.code(_py_expr, language='python')

                # 품질 메트릭
                col_q1, col_q2, col_q3 = st.columns(3)
                with col_q1:
                    st.metric("Fitness", f"{_fitness:.5f}")
                with col_q2:
                    st.metric("Max Corr", f"{_corr:.3f}", help=f"가장 높은 상관 피처: {_corr_f}")
                with col_q3:
                    st.metric("피처 수",  len(_feats))

                # 사용 피처 목록 (동적 파싱)
                if _feats:
                    with st.expander("📖 사용된 피처 목록 (X0, X1, ...)", expanded=False):
                        for i, f in enumerate(_feats):
                            st.markdown(f"- **`X{i}`** → `{f}`")

                # IC Decay 히스토리 차트
                if _ic_hist and len(_ic_hist) >= 2:
                    try:
                        import plotly.graph_objects as go
                        _dates  = [h.get('date', '') for h in _ic_hist]
                        _ic_vals = [safe_float(h.get('ic', 0.0)) for h in _ic_hist]
                        fig_ic = go.Figure()
                        fig_ic.add_trace(go.Scatter(
                            x=_dates, y=_ic_vals,
                            mode='lines+markers', name='Rolling IC',
                            line=dict(color='#3f51b5', width=2),
                            marker=dict(size=5),
                        ))
                        fig_ic.add_hline(
                            y=0.02, line_dash='dash', line_color='#d50000',
                            annotation_text='Decay 임계값 (0.02)',
                            annotation_position='bottom right',
                        )
                        fig_ic.update_layout(
                            title=f'{_col_name} IC Decay 추적',
                            xaxis_title='날짜', yaxis_title='Spearman IC',
                            height=250, template='plotly_white',
                            margin=dict(t=35, b=20, l=10, r=10),
                        )
                        st.plotly_chart(fig_ic, use_container_width=True)
                    except Exception:
                        st.caption("(IC 히스토리 차트 렌더링 실패 — plotly 확인 필요)")
                elif _ic_hist:
                    st.caption(f"IC 히스토리: {len(_ic_hist)}개 (차트 최소 2개 필요)")
                else:
                    st.caption("IC 히스토리 없음 — 주간 GC 실행 후 축적됩니다.")


# ══════════════════════════════════════════════════════════════
# TAB 2: 퇴출 알파
# ══════════════════════════════════════════════════════════════
with tab_gc:
    st.subheader(f"⚰️ 퇴출된 알파 — {len(retired)}개")

    if not retired:
        st.info("퇴출된 알파 없음 — IC Decay threshold(0.02) 미달 시 자동 퇴출됩니다.")
    else:
        import pandas as pd
        _rows_r = []
        for a in reversed(retired):
            _ic_hist_r = a.get('ic_history', [])
            _last_ic = safe_float(_ic_hist_r[-1].get('ic', 0.0)) if _ic_hist_r else 0.0
            _rows_r.append({
                'ID':         str(a.get('id', ''))[:30],
                '발굴일':    str(a.get('discovered_at', ''))[:19],
                '퇴출일':    str(a.get('retired_at', ''))[:19],
                '퇴출 사유': str(a.get('retire_reason', 'N/A'))[:60],
                '최근 IC':   round(_last_ic, 5),
                'OOS IC':    safe_float(a.get('oos_ic', 0.0)),
                'Formula':   str(a.get('formula', ''))[:40],
            })
        st.dataframe(pd.DataFrame(_rows_r), hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════
# TAB 3: 전체 히스토리
# ══════════════════════════════════════════════════════════════
with tab_all:
    import pandas as pd

    st.subheader(f"📜 Alpha Discovery History — 전체 {n_total}개")

    all_alphas = af2.get('all', [])
    if not all_alphas:
        st.info("발굴된 알파 없음")
    else:
        _rows_all = []
        for a in reversed(all_alphas):
            _status = str(a.get('status', 'inactive')).lower()
            _status_badge = {
                'active':   '✅ active',
                'retired':  '⚰️ retired',
                'inactive': '💤 inactive',
            }.get(_status, f'❓ {_status}')

            # OOS IC 우선, 없으면 sharpe_ratio (v1 하위 호환)
            _ic_val = safe_float(a.get('oos_ic', a.get('sharpe_ratio', 0.0)))

            _rows_all.append({
                'ID':       str(a.get('id', ''))[:25],
                '발굴일':  str(a.get('discovered_at', ''))[:19],
                'Status':   _status_badge,
                'OOS IC':   round(_ic_val, 5),
                'IC Std':   safe_float(a.get('oos_ic_std', 0.0)),
                'Fitness':  safe_float(a.get('fitness', 0.0)),
                '컬럼명':   str(a.get('col_name', ''))[:20],
                '피처 수': len(a.get('features_used') or []),
                'Formula':  str(a.get('formula', ''))[:50],
            })

        df_all = pd.DataFrame(_rows_all)
        st.dataframe(df_all, hide_index=True, use_container_width=True)

        # OOS IC 히스토리 차트 (전체)
        try:
            import plotly.graph_objects as go
            _ic_vals_all = [safe_float(a.get('oos_ic', a.get('sharpe_ratio', 0.0))) for a in all_alphas]
            _ids_all     = [str(a.get('id', f'#{i}'))[-12:] for i, a in enumerate(all_alphas)]
            _colors      = [
                '#00c853' if str(a.get('status', '')).lower() == 'active'
                else ('#9e9e9e' if str(a.get('status', '')).lower() == 'retired' else '#ffd740')
                for a in all_alphas
            ]
            fig_hist = go.Figure(go.Bar(
                x=_ids_all, y=_ic_vals_all,
                marker_color=_colors,
                text=[f"{v:.4f}" for v in _ic_vals_all],
                textposition='outside',
                hovertemplate='%{x}<br>OOS IC: %{y:.4f}<extra></extra>',
            ))
            fig_hist.add_hline(
                y=0.05, line_dash='dash', line_color='#3f51b5',
                annotation_text='IC Threshold (0.05)',
            )
            fig_hist.update_layout(
                title='Alpha Discovery — OOS IC 전체 이력',
                yaxis_title='OOS Rank IC (Spearman)',
                xaxis_title='Alpha ID',
                height=350, template='plotly_white',
                showlegend=False,
            )
            st.plotly_chart(fig_hist, use_container_width=True)
        except Exception:
            st.caption("(차트 렌더링 실패)")


# ══════════════════════════════════════════════════════════════
# TAB 4: 피처 분석
# ══════════════════════════════════════════════════════════════
with tab_features:
    st.subheader("🔬 피처 사용 빈도 분석 — 동적 파싱")
    st.caption("AlphaMiner가 선택한 피처들의 중요도를 발굴 빈도로 추정합니다.")

    if not feat_usage:
        st.info("피처 사용 데이터 없음 — AlphaMiner 실행 후 확인하세요.")
    else:
        try:
            import plotly.express as px
            import pandas as pd

            df_feat = pd.DataFrame([
                {'Feature': k, 'Count': v}
                for k, v in list(feat_usage.items())[:30]  # Top 30
            ]).sort_values('Count', ascending=True)

            fig_feat = px.bar(
                df_feat, x='Count', y='Feature',
                orientation='h',
                color='Count',
                color_continuous_scale='Blues',
                title='피처별 알파 수식 사용 빈도 (Top 30)',
                labels={'Count': '사용 횟수', 'Feature': '피처명'},
            )
            fig_feat.update_layout(
                height=max(300, len(df_feat) * 22),
                template='plotly_white',
                coloraxis_showscale=False,
                margin=dict(t=40, b=10, l=10, r=10),
            )
            st.plotly_chart(fig_feat, use_container_width=True)

            # 테이블
            with st.expander("📋 전체 피처 사용 빈도 테이블", expanded=False):
                df_feat_full = pd.DataFrame([
                    {'Feature': k, '사용 횟수': v, '비율': f"{v / max(feat_usage.values()) * 100:.1f}%"}
                    for k, v in feat_usage.items()
                ])
                st.dataframe(df_feat_full, hide_index=True, use_container_width=True)

        except Exception as _e:
            st.caption(f"차트 오류: {_e}")
            for feat, cnt in list(feat_usage.items())[:20]:
                st.text(f"  {feat}: {cnt}회")

    st.markdown("---")
    # 활성 알파가 파이프라인에 주입하는 컬럼 목록
    st.subheader("💉 파이프라인 자동 주입 컬럼 (inject_auto_alphas)")
    if active:
        for rank, a in enumerate(active):
            col_nm = str(a.get('col_name', f'auto_alpha_{rank+1:03d}'))
            ic_val = safe_float(a.get('oos_ic', 0.0))
            st.markdown(f"- `{col_nm}` — OOS IC: **{ic_val:.4f}**")
    else:
        st.info("활성 알파 없음 — 주입 컬럼 없음")


# ══════════════════════════════════════════════════════════════
# TAB 5: GC 상태
# ══════════════════════════════════════════════════════════════
with tab_gc_run:
    st.subheader("🗑️ AlphaGarbageCollector 설정 및 상태")

    # DynamicConfig에서 GC 파라미터 읽기 (Graceful Fallback)
    try:
        import sys as _sys
        _sys.path.insert(0, str(_PROJECT_ROOT))
        from config.dynamic_config import DynamicConfig
        _cfg = DynamicConfig()
        _decay_window    = _cfg.get('alpha_factory.ic_decay_window',    30)
        _decay_threshold = _cfg.get('alpha_factory.ic_decay_threshold', 0.02)
        _ic_threshold    = _cfg.get('alpha_factory.ic_threshold',       0.05)
        _corr_threshold  = _cfg.get('alpha_factory.ic_corr_threshold',  0.70)
        _max_inject      = _cfg.get('alpha_factory.max_inject_alphas',  10)
    except Exception:
        _decay_window    = 30
        _decay_threshold = 0.02
        _ic_threshold    = 0.05
        _corr_threshold  = 0.70
        _max_inject      = 10

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("IC 최소 기준", f"{_ic_threshold:.2f}",  help="OOS IC ≥ 이 값이어야 생존")
    with c2:
        st.metric("Decay 창",     f"{_decay_window}일",    help="최근 N일 IC를 평균하여 판단")
    with c3:
        st.metric("Decay 임계값", f"{_decay_threshold:.2f}", help="이 이하 → 자동 퇴출")
    with c4:
        st.metric("직교화 기준", f"{_corr_threshold:.0%}", help="|Pearson| ≥ 이 값 → 기각")
    with c5:
        st.metric("최대 주입",   f"{_max_inject}개",       help="파이프라인에 주입할 최대 알파 수")

    st.markdown("---")
    st.subheader("🔧 수동 실행 가이드")
    st.code(
        "# 알파 탐색 (주간 실행 권장)\n"
        "python3 src/alpha_factory/alpha_miner.py --mode mine --gens 20 --pop 500\n\n"
        "# 가비지 컬렉션 (주말 재학습 파이프라인 말미)\n"
        "python3 src/alpha_factory/alpha_miner.py --mode gc\n\n"
        "# 현황 확인\n"
        "python3 src/alpha_factory/alpha_miner.py --mode status",
        language='bash',
    )

    st.info(
        "💡 **자동화**: `scripts/train_ensemble.py` 실행 말미에 `AlphaGarbageCollector().run()`을 "
        "호출하면 주간 재학습 시 자동으로 IC Decay 검사가 진행됩니다."
    )

st.markdown("---")
st.caption(
    f"🟢 Live Polling #{_refresh_count} | "
    f"mtime 캐시 키: {_alpha_mtime:.0f} | "
    f"Alpha Factory v2 | [Phase 18: SS-ETF + Alpha Factory Dashboard]"
)

# ══════════════════════════════════════════════════════════════
# ★ SURGERY-2026-07-10: AlphaMemoryStore 패널
# ══════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("🧬 AlphaMemoryStore — 실패 알파 메타학습 기억")
st.caption("퇴출된 알파의 피처·레짐 패턴을 기억하여 다음 탐색에서 같은 공간을 반복하지 않도록 안내합니다.")

try:
    from dashboard.utils.data_loader import load_alpha_memory_store
    _mem = load_alpha_memory_store()

    _m_c1, _m_c2, _m_c3 = st.columns(3)
    with _m_c1:
        st.metric("📚 기억된 실패 알파", _mem.get('total', 0))
    with _m_c2:
        _regimes = _mem.get('by_regime', {})
        top_regime = max(_regimes, key=_regimes.get, default='N/A') if _regimes else 'N/A'
        st.metric("⚠️ 최다 실패 레짐", top_regime)
    with _m_c3:
        top_feats = _mem.get('top_penalty_features', [])
        st.metric("🔴 최고 패널티 피처", top_feats[0]['feature'] if top_feats else 'N/A')

    if _mem.get('total', 0) > 0:
        # 패널티 피처 바 차트
        import plotly.express as px
        if top_feats:
            import pandas as pd
            _pf_df = pd.DataFrame(top_feats[:10])
            fig_pf = px.bar(
                _pf_df, x='penalty', y='feature', orientation='h',
                color='penalty',
                color_continuous_scale='Reds',
                title='피처별 실패 패널티 점수',
                labels={'penalty': '패널티 (실패 빈도)', 'feature': '피처명'},
            )
            fig_pf.update_layout(height=300, margin=dict(t=40, b=20), yaxis={'autorange': 'reversed'})
            st.plotly_chart(fig_pf, use_container_width=True)

        # 레짐별 실패 분포
        if _regimes:
            _reg_df = pd.DataFrame([{'regime': r, 'count': c} for r, c in _regimes.items()])
            fig_reg = px.pie(
                _reg_df, values='count', names='regime',
                title='레짐별 실패 알파 분포',
                color_discrete_sequence=px.colors.qualitative.Set2,
                hole=0.35,
            )
            fig_reg.update_layout(height=250, margin=dict(t=40, b=10))
            st.plotly_chart(fig_reg, use_container_width=True)

        # 최근 기억 목록
        with st.expander(f"📋 최근 실패 알파 기록 ({min(10, _mem.get('total',0))}개)", expanded=False):
            _records = _mem.get('raw_records', [])[-10:]
            for rec in reversed(_records):
                st.markdown(
                    f"**수식**: `{rec.get('formula','')[:60]}` | "
                    f"**레짐**: `{rec.get('retire_regime','?')}` | "
                    f"**IC**: `{rec.get('avg_ic_before_retire', 0):.4f}` | "
                    f"**복잡도**: `{rec.get('complexity', 0)}`"
                )
                st.caption(f"퇴출 이유: {rec.get('retire_reason','')[:80]}")
                st.divider()
    else:
        st.info("AlphaMemoryStore에 기록이 없습니다. 알파가 퇴출되면 자동으로 채워집니다.")

except Exception as _mem_e:
    st.info(f"AlphaMemoryStore 데이터 로드 중... ({_mem_e})")

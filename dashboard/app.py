#!/usr/bin/env python3
"""
Project Meridian — Dashboard Entrypoint (SSOT Refactored)
==========================================================
# [SSOT Refactoring] 이 파일은 순수 진입점(Entrypoint)입니다.
# [SSOT Refactoring] 모든 페이지 렌더링 로직은 dashboard/pages/ 에 분리됨.
# [SSOT Refactoring] 자체 데이터 계산 로직 없음. data_loader.py를 통해서만 읽음.

# [Live Polling] 10초 주기 자동 새로고침 → setup_live_polling() 호출.

Multipage 구조:
    pages/1_📊_Overview.py        — 전체 KPI + 포트폴리오 요약
    pages/2_🌍_Macro.py           — 매크로 / 레짐 상태
    pages/3_📡_Streams.py         — S1~S5 스트림 성과
    pages/4_⚡_Execution.py       — 주문 실행 / 거래내역
    pages/5_🛡️_Risk.py            — 리스크 게이트 상태
    pages/6_🔬_Signal_Model.py    — IC / 모델 품질
    pages/7_🧠_Alpha_Factory.py   — Alpha Factory 발굴 알파
    pages/8_🔧_Infrastructure.py  — 파이프라인 인프라

Usage:
    streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

import streamlit as st

# ── Path Setup ───────────────────────────────────────────────────────────────
_DASHBOARD_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _DASHBOARD_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# ── [SSOT Refactoring] 공통 데이터 로더 임포트 ───────────────────────────────
from dashboard.utils.data_loader import (
    setup_live_polling,
    load_shadow_summary,
    load_signal_cache,
    load_go_nogo,
    get_ssot_kpis,
    inject_common_css,
    COMMON_CSS,
    safe_float,
    render_cache_clear_button,
    load_alpha_factory_v2,
    _get_alpha_mtime, _get_ss_etf_mtime,
    load_ss_etf_risk,
)

# ── Page Config (진입점에서만 1회 설정) ─────────────────────────────────────
# [SSOT Refactoring] set_page_config는 app.py에서만 호출 (pages/*.py에서 호출 금지)
st.set_page_config(
    page_title="Project Meridian",
    page_icon="🔭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── [SSOT Refactoring] 공통 CSS 주입 ─────────────────────────────────────────
inject_common_css()

# ── [Live Polling] 10초 주기 자동 새로고침 설정 ─────────────────────────────
# [Live Polling] setup_live_polling()은 sidebar보다 먼저 호출되어야 함
try:
    from dashboard.utils.data_loader import setup_live_polling
    _refresh_count = setup_live_polling(interval_ms=10_000, key="meridian_global_refresh")
except Exception:
    _refresh_count = 0

# ── Sidebar: 공통 상태 패널 ──────────────────────────────────────────────────
# [SSOT Refactoring] 사이드바 데이터는 모두 data_loader.py를 통해 읽음
with st.sidebar:
    st.markdown("## 🔭 Meridian")

    # [Live Polling] 폴링 상태 표시
    st.markdown(
        f'<span class="poll-badge">🟢 Live · 10s · #{_refresh_count}</span>',
        unsafe_allow_html=True,
    )
    st.markdown("---")

    # [SSOT Refactoring] Quick Stats — data_loader SSoT만 참조
    try:
        _kpis = get_ssot_kpis()
        _signal = load_signal_cache()
        _ss = load_shadow_summary()

        _grade = str(_kpis.get("grade") or "?")
        _verdict = str(_kpis.get("verdict") or "N/A")
        _n_days = int(_kpis.get("n_days") or 0)

        st.markdown(f"**Grade:** `{_grade}` | **Verdict:** `{_verdict}`")
        st.markdown(f"**NAV:** ₩{_kpis.get('nav', 0):,.0f}")

        try:
            from config.dynamic_config import DynamicConfig
            _min_days = DynamicConfig().get("gonogo.shadow_min_days", 14)
        except Exception:
            _min_days = 14
        st.markdown(f"**Days:** {_n_days} / {_min_days}")

        _vix = safe_float(_signal.get("vix"))
        _ois = safe_float(_signal.get("ois"), 50.0)
        _vkospi = safe_float(_signal.get("vkospi"))
        st.markdown(f"**VIX:** {_vix:.1f} | **VKOSPI:** {_vkospi:.1f}")
        st.markdown(f"**OIS:** {_ois:.1f}")

    except Exception as _sidebar_e:
        st.caption(f"데이터 로드 중... ({_sidebar_e})")

    # [SSOT Refactoring] Alerts — medallion_validation.json SSoT
    try:
        from dashboard.utils.data_loader import load_json
        _med = load_json("medallion_validation.json")
        _n_issues = int(_med.get("total_issues") or 0)
        if _n_issues > 0:
            st.warning(f"⚠️ {_n_issues}건 알림 ({_med.get('overall', 'N/A')})")
        else:
            st.success("✅ No alerts")
    except Exception:
        pass

    st.markdown("---")


    # [Phase 16] Removed Legacy S6 section
    # [SSOT Refactoring] 타임스탬프 표시
    try:
        _updated = str(_ss.get("updated") or "")[:19]
        st.caption(f"Updated: {_updated or 'N/A'}")
    except Exception:
        pass

    st.markdown("---")

    # [Phase 18] 수동 캐시 클리어 버튼
    if st.button("🔄 캐시 비우고 최신 데이터 로드", key="_home_cache_clear_home"):
        try:
            st.cache_data.clear()
        except Exception:
            pass
        try:
            st.cache_resource.clear()
        except Exception:
            pass
        st.rerun()

    st.markdown("---")

    # [Phase 18] Alpha Factory v2 빠른 상태
    try:
        _alpha_mtime = _get_alpha_mtime()
        _af2 = load_alpha_factory_v2(_mtime=_alpha_mtime)
        _n_act  = _af2.get("n_active", 0)
        _n_tot  = _af2.get("n_total", 0)
        _best   = _af2.get("best_active", {})
        _best_ic = safe_float(_best.get("oos_ic", 0.0))
        _af_icon = "🟢" if _n_act > 0 else "⚪"
        st.markdown("🧠 **Alpha Factory v2**")
        st.markdown(f"{_af_icon} 생성 {_n_act}/{_n_tot}개 | 최고 IC: `{_best_ic:.4f}`")
    except Exception:
        st.caption("🧠 Alpha Factory: 로드 실패")

    st.markdown("---")

    # [Phase 18] SS-ETF 빠른 리스크
    try:
        _ss_mtime = _get_ss_etf_mtime()
        _sse = load_ss_etf_risk(_mtime=_ss_mtime)
        _warn = _sse.get("combined_warning", False)
        _sm_vr = safe_float(_sse.get("samsung", {}).get("vol_ratio", 0.0))
        _hx_vr = safe_float(_sse.get("hynix",   {}).get("vol_ratio", 0.0))
        st.markdown("🔬 **SS-ETF 리스크**")
        _sse_icon = "🔴" if _warn else ("🟡" if max(_sm_vr, _hx_vr) >= 0.15 else "🟢")
        st.markdown(f"{_sse_icon} 삼성: `{_sm_vr:.1%}` | 하이닉스: `{_hx_vr:.1%}`")
        if _warn:
            st.markdown("🚨 **Wag-the-Dog 경고**")
    except Exception:
        st.caption("🔬 SS-ETF: 로드 실패")

    st.markdown("---")

    # [Phase 39: Dashboard Integration] 🚀 Moonshot Boosters 사이드바 패널
    try:
        from dashboard.utils.data_loader import load_moonshot_status
        _ms = load_moonshot_status()

        # Kelly Booster
        _kelly_on  = bool(_ms.get('kelly_active', False))
        _kelly_icon = "🔥" if _kelly_on else "⚪"
        _kelly_label = "ON" if _kelly_on else "OFF"

        # VIX Gear
        _vix_gear = str(_ms.get('vix_gear', 'QQQ'))
        _gear_icon = {
            'TQQQ': '🚀', 'QQQ': '📊', 'SQQQ': '🔻', 'TLT': '🛡️'
        }.get(_vix_gear, '📊')

        # Crypto Leverage
        _crypto_lev = int(_ms.get('crypto_leverage', 1))
        _adl_pct    = float(_ms.get('adl_trigger_pct', 0))

        st.markdown("---")
        st.markdown("🚀 **Moonshot Boosters**")
        st.markdown(
            f"**Kelly:** {_kelly_icon} {_kelly_label} "
            f"| **VIX Gear:** {_gear_icon} `{_vix_gear}`"
        )
        _adl_text = f"(ADL Trigger: {_adl_pct:.1f}%)" if _adl_pct > 0 else "(ADL: 1x)"
        st.markdown(f"**Crypto:** `{_crypto_lev}x` {_adl_text}")

        # VIX 안전 구간 시각적 표시
        _vix_c  = float(_ms.get('vix_current', 20.0))
        _vix_pl = float(_ms.get('vix_p_low', 0.0))
        _vix_ph = float(_ms.get('vix_p_high', 0.0))
        if _vix_pl > 0:
            _vix_zone = (
                "🟢 TQQQ Zone" if _vix_c < _vix_pl else
                ("🔴 Danger Zone" if _vix_c >= _vix_ph else "🟡 QQQ Zone")
            )
            st.caption(f"VIX {_vix_c:.1f} · {_vix_zone} (P40={_vix_pl:.1f}/P70={_vix_ph:.1f})")
        else:
            _abs_max = float(_ms.get('vix_abs_max_tqqq', 20.0))
            _vix_zone = "🟢 TQQQ Safe" if _vix_c < _abs_max else "🟡 QQQ/방어"
            st.caption(f"VIX {_vix_c:.1f} · {_vix_zone}")
    except Exception:
        pass

    st.caption("Meridian Dashboard v4.0 [Phase 39]")
    st.caption("Pages: 사이드바 네비게이션으로 이동")

# ── 기본 홈 화면 (Overview redirect 안내) ────────────────────────────────────
st.markdown(
    "<div class='main-header'><h1>🔭 Project Meridian</h1>"
    "<p>4-Stream Quantitative Trading System — SSOT Dashboard</p></div>",
    unsafe_allow_html=True,
)

_col1, _col2, _col3, _col4 = st.columns(4)

try:
    _kpis = get_ssot_kpis()
    with _col1:
        st.metric("🎯 Grade", _kpis.get("grade", "?"),
                  delta=_kpis.get("verdict", "N/A"))
    with _col2:
        _nav = _kpis.get("nav", 0)
        st.metric("💰 NAV", f"₩{_nav:,.0f}")
    with _col3:
        _da = _kpis.get("da", 0.0)
        st.metric("📊 DA", f"{safe_float(_da)*100:.1f}%")
    with _col4:
        _sharpe = safe_float(_kpis.get("sharpe"))
        st.metric("⚡ Sharpe", f"{_sharpe:.2f}")
except Exception as _home_e:
    st.info(f"데이터 로드 중... L4 파이프라인이 실행되면 자동으로 업데이트됩니다.")

st.info(
    "👈 **좌측 사이드바에서 페이지를 선택하세요.**\n\n"
    "- **📊 Overview** — 전체 KPI 및 포트폴리오\n"
    "- **🌍 Macro** — 매크로 / 레짐\n"
    "- **📡 Streams** — S1~S5 스트림 성과\n"
    "- **⚡ Execution** — 주문 실행 내역\n"
    "- **🛡️ Risk** — 리스크 게이트\n"
    "- **🔬 Signal & Model** — IC / 모델 품질\n"
    "- **🧠 Alpha Factory** — 발굴 알파\n"
    "- **🔧 Infrastructure** — 파이프라인 상태\n\n"
    f"🟢 **Live Polling:** 10초 주기 자동 새로고침 (#{_refresh_count}회)"
)

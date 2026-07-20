#!/usr/bin/env python3
"""
Page 6: Signal & Model — IC / 모델 품질
# [SSOT Refactoring] measurement_engine.json / signal_quality_state.json SSoT만 읽음.
# [Live Polling] 10초 자동 새로고침.
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
    load_signal_model_data, load_json, safe_float, safe_fmt,
)

_refresh_count = setup_live_polling(interval_ms=10_000, key="signal_model_refresh")
inject_common_css()

st.markdown(
    "<div class='main-header'><h1>🔬 Signal & Model</h1>"
    "<p>IC · DA · 모델 품질 · Walk-Forward (measurement_engine.json SSoT)</p></div>",
    unsafe_allow_html=True,
)

# [SSOT Refactoring] 시그널/모델 데이터 로드 — data_loader.py 경유
sm_data = load_signal_model_data()
official = sm_data.get("official", {})
sqstate = sm_data.get("signal_quality_state", {})
calib = sm_data.get("calibration_metrics", {})
fi = sm_data.get("feature_importance", {})
shap_d = sm_data.get("shap_analysis", {})
wf = sm_data.get("walk_forward", {})
icir = sm_data.get("icir_validation", {})
decay = sm_data.get("alpha_decay", {})
qvm_ic = sm_data.get("qvm_ic_history", {})
medal = sm_data.get("medallion_validation", {})

# ── 핵심 모델 지표 ────────────────────────────────────────────────────────────
st.subheader("📊 핵심 모델 지표 (measurement_engine.json SSoT)")

# [SSOT Refactoring] 모든 값은 ME official에서 읽음 — 재계산 없음
_ic = sm_data.get("ic", 0.0)
_ic_n = sm_data.get("ic_n", 0)
_ic_method = sm_data.get("ic_method", "spearman")
_da = sm_data.get("da", 0.0)
_sharpe = sm_data.get("sharpe", 0.0)
_grade = sm_data.get("grade", "?")

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    _ic_cls = "delta-pos" if _ic > 0.05 else ("delta-neutral" if _ic > 0 else "delta-neg")
    st.metric("IC", f"{_ic:.4f}", delta=f"n={_ic_n} ({_ic_method})")
with c2:
    _da_cls = "delta-pos" if _da >= 0.55 else ("delta-neutral" if _da >= 0.45 else "delta-neg")
    st.metric("DA", f"{_da*100:.1f}%")
with c3:
    st.metric("Sharpe", f"{_sharpe:.2f}")
with c4:
    st.metric("Grade", _grade)
with c5:
    _me_ts = str(sm_data.get("me", {}).get("timestamp") or "")[:19]
    st.metric("Last Updated", _me_ts or "N/A")

st.markdown("---")

# ── IC 상세 / ICIR ─────────────────────────────────────────────────────────────
col_ic, col_cal = st.columns(2)

with col_ic:
    st.subheader("📐 IC / ICIR (icir_validation.json SSoT)")
    _icir_data = sm_data.get("icir_validation", {})
    if _icir_data and "factors" in _icir_data:
        _factors = _icir_data["factors"]
        _qvm = _factors.get("qvm_score", {})
        
        _qvm_ic = safe_float(_qvm.get("ic"))
        _qvm_icir = safe_float(_qvm.get("icir"))
        
        st.metric("QVM IC", f"{_qvm_ic:.4f}" if _qvm_ic else "N/A")
        st.metric("QVM ICIR", f"{_qvm_icir:.4f}" if _qvm_icir else "N/A")
        
        with st.expander("📋 세부 팩터별 IC/ICIR"):
            _factor_list = []
            for f_name, f_data in _factors.items():
                if f_name == "qvm_score": continue
                _factor_list.append({
                    "Factor": f_name,
                    "IC": f_data.get("ic"),
                    "ICIR": f_data.get("icir"),
                    "Valid": f_data.get("valid")
                })
            if _factor_list:
                st.dataframe(pd.DataFrame(_factor_list), hide_index=True, use_container_width=True)
    else:
        st.info("ICIR 데이터 없음")

with col_cal:
    st.subheader("🎯 Calibration (calibration_metrics.json SSoT)")
    if calib and "brier_before" in calib:
        _before = safe_float(calib.get("brier_before"))
        _after = safe_float(calib.get("brier_after"))
        _imp = safe_float(calib.get("brier_improvement"))
        _n_samp = int(calib.get("n_samples") or 0)
        st.metric("Brier Before", f"{_before:.4f}" if _before else "N/A")
        st.metric("Brier After", f"{_after:.4f}" if _after else "N/A")
        st.metric("Improvement", f"{_imp:+.4f}" if _imp else "N/A")
        st.metric("Samples", _n_samp)
    else:
        # Fallback to measurement_engine
        _me_brier = safe_float(sm_data.get("me", {}).get("views", {}).get("signal_quality", {}).get("brier_score"))
        if _me_brier:
            st.metric("Brier Score", f"{_me_brier:.4f}")
        else:
            st.info("calibration 데이터 없음")

st.markdown("---")

# ── Walk-Forward 결과 ─────────────────────────────────────────────────────────
st.subheader("🔄 Walk-Forward (walk_forward_results.json SSoT)")
if wf and "data" in wf:
    _wf_data = wf.get("data", [])
    if isinstance(_wf_data, list) and _wf_data:
        df_folds = pd.DataFrame(_wf_data)
        _wf_sharpe = df_folds["sharpe"].mean() if "sharpe" in df_folds.columns else 0.0
        _wf_wr = df_folds["win_rate"].mean() if "win_rate" in df_folds.columns else 0.0
        
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("평균 Sharpe", f"{_wf_sharpe:.2f}")
        with c2:
            st.metric("평균 Win Rate", f"{_wf_wr*100:.1f}%")
        with c3:
            st.metric("Fold 수", len(_wf_data))
        
        with st.expander(f"📋 Fold별 상세 ({len(_wf_data)}개)"):
            st.dataframe(df_folds, hide_index=True, use_container_width=True)
    else:
        st.info("Walk-Forward 기록이 없습니다.")
else:
    st.info("walk_forward_results 데이터 없음")

st.markdown("---")

# ── Feature Importance ─────────────────────────────────────────────────────────
st.subheader("🧠 Feature Importance (shap_analysis.json SSoT)")
_features = fi.get("importances") if fi and fi.get("importances") else shap_d.get("importances")

if _features:
    if isinstance(_features, dict):
        df_fi = pd.DataFrame(
            [{"feature": k, "importance": v} for k, v in _features.items()]
        ).sort_values("importance", ascending=False).head(20)
        
        if not df_fi.empty:
            fig_fi = px.bar(df_fi, x="importance", y="feature", orientation="h",
                            title="Top 20 Feature Importance", template="plotly_white")
            fig_fi.update_layout(yaxis={'categoryorder': 'total ascending'})
            st.plotly_chart(fig_fi, use_container_width=True)
    elif isinstance(_features, list):
        df_fi = pd.DataFrame(_features).sort_values("importance", ascending=False).head(20)
        if not df_fi.empty:
            fig_fi = px.bar(df_fi, x="importance", y="feature", orientation="h",
                            title="Top 20 Feature Importance", template="plotly_white")
            fig_fi.update_layout(yaxis={'categoryorder': 'total ascending'})
            st.plotly_chart(fig_fi, use_container_width=True)
else:
    st.info("Feature Importance 데이터 없음 (모델 학습 후 갱신됨)")

st.markdown("---")

# ── Medallion Validation ───────────────────────────────────────────────────────
if medal:
    st.subheader("🏅 Medallion Validation (medallion_validation.json SSoT)")
    _overall = str(medal.get("overall") or "N/A")
    _total_issues = int(medal.get("total_issues") or 0)
    c1, c2 = st.columns(2)
    with c1:
        st.metric("Overall", _overall)
    with c2:
        st.metric("Issues", _total_issues)
    _checks = medal.get("checks") or medal.get("results", [])
    if isinstance(_checks, list) and _checks:
        with st.expander("📋 검증 항목 상세"):
            df_med = pd.DataFrame(_checks)
            st.dataframe(df_med, hide_index=True, use_container_width=True)
    elif isinstance(_checks, dict) and _checks:
        with st.expander("📋 검증 항목 상세"):
            for k, v in _checks.items():
                _status_icon = "✅" if v.get("passed") else "❌"
                st.markdown(f"- {_status_icon} **{k}**: {v}")

# ── Signal Quality State ───────────────────────────────────────────────────────
if sqstate:
    st.markdown("---")
    st.subheader("📊 Signal Quality State (signal_quality_state.json SSoT)")
    _sq_stream_ic = sqstate.get("rolling_ic")
    _sq_samples = sqstate.get("samples")
    
    if isinstance(_sq_stream_ic, dict) and _sq_stream_ic:
        df_sq = pd.DataFrame([
            {
                "Stream": k, 
                "Rolling IC": safe_fmt(v),
                "Samples": _sq_samples.get(k, 0) if isinstance(_sq_samples, dict) else 0
            }
            for k, v in _sq_stream_ic.items()
        ])
        st.dataframe(df_sq, hide_index=True, use_container_width=True)
    
    if _sq_samples:
        _total_samples = sum(_sq_samples.values()) if isinstance(_sq_samples, dict) else _sq_samples
        st.metric("Total IC Samples", _total_samples)

st.caption(f"🟢 Live Polling 활성 — 10초 자동 새로고침 #{_refresh_count}")

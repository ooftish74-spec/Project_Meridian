#!/usr/bin/env python3
"""
Project Meridian — Dashboard Unified Data Loader
=================================================
# [SSOT Refactoring] 모든 대시보드 페이지의 데이터 소스를 이 파일 하나로 통합.
# [SSOT Refactoring] 대시보드는 L4 엔진이 확정해 놓은 results/*.json만 Read-only로 읽음.
# [SSOT Refactoring] 자체 수익률 계산, 레짐 판단 등의 독자 연산 로직 없음.

# [Live Polling] streamlit-autorefresh 컴포넌트로 10초 주기 무중단 폴링 구현.
# [Live Polling] 모든 @st.cache_data에 ttl=10 적용 → 폴링 주기와 동기화.

Usage:
    from dashboard.utils.data_loader import (
        setup_live_polling,
        load_json,
        load_shadow_summary,
        load_stream_metrics,
        load_signal_cache,
        load_shadow_portfolio,
        load_shadow_trades,
        load_go_nogo,
        load_measurement_engine,
        load_kill_switch,
        load_risk_data,
        load_stream_signals,
        load_alpha_factory,
        RESULTS_DIR,
    )
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import pandas as pd

# ── Path resolution ──────────────────────────────────────────────────────────
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _DASHBOARD_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# [SSOT Refactoring] 모든 데이터 소스는 이 단일 경로에서만 읽음
RESULTS_DIR = _PROJECT_ROOT / "results"

# .env 자동 로드 (스트림릿 런타임용)
_env_file = _PROJECT_ROOT / '.env'
if _env_file.exists():
    import os
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _, _val = _line.partition('=')
                # KIS_MODE 등은 기존 shell 환경변수 무시하고 .env를 최우선으로 강제 덮어쓰기
                os.environ[_key.strip()] = _val.strip()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# [Live Polling] 10초 주기 자동 새로고침 설정
# ─────────────────────────────────────────────────────────────────────────────

_POLL_INTERVAL_MS = 10_000  # 10초 (밀리초)


def setup_live_polling(interval_ms: int = _POLL_INTERVAL_MS, key: str = "data_refresh") -> int:
    """# [Live Polling] 10초 주기 무중단 라이브 폴링.

    streamlit-autorefresh 컴포넌트를 우선 사용하고,
    미설치 환경에서는 st.session_state 기반 타이머 Fallback 제공.

    Args:
        interval_ms: 새로고침 주기 (밀리초, 기본 10000)
        key: 컴포넌트 고유 키

    Returns:
        현재까지의 새로고침 횟수 (정수)
    """
    # [Live Polling] 1차: streamlit-autorefresh 컴포넌트 시도
    try:
        from streamlit_autorefresh import st_autorefresh  # type: ignore
        count = st_autorefresh(interval=interval_ms, key=key)
        return int(count) if count else 0
    except ImportError:
        pass
    except Exception as _e:
        logger.debug(f"[Live Polling] st_autorefresh 실패: {_e}")

    # [Live Polling] 2차 Fallback: session_state 타이머 + 수동 안내
    _now = datetime.now()
    _last_key = f"_last_refresh_{key}"
    _count_key = f"_refresh_count_{key}"

    if _last_key not in st.session_state:
        st.session_state[_last_key] = _now
        st.session_state[_count_key] = 0

    elapsed = (_now - st.session_state[_last_key]).total_seconds() * 1000
    if elapsed >= interval_ms:
        st.session_state[_last_key] = _now
        st.session_state[_count_key] = st.session_state.get(_count_key, 0) + 1
        st.rerun()

    return int(st.session_state.get(_count_key, 0))


# ─────────────────────────────────────────────────────────────────────────────
# [SSOT Refactoring] 핵심 Read-only JSON 로더
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)  # [Live Polling] 10초 캐시 → 폴링 주기와 동기화
def load_json(filename: str) -> Dict:
    """# [SSOT Refactoring] results/ 디렉토리의 JSON 파일을 안전하게 로드.

    Args:
        filename: results/ 하위 파일명 (예: 'shadow_summary.json')

    Returns:
        파싱된 dict. 파일 없음/파싱 실패 시 빈 dict 반환 (Crash-safe).
    """
    path = RESULTS_DIR / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning(f"[SSOT Refactoring] {filename} JSON 파싱 실패 (L4 쓰기 중?): {e}")
        return {}
    except Exception as e:
        logger.warning(f"[SSOT Refactoring] {filename} 로드 실패: {e}")
        return {}


@st.cache_data(ttl=10)
def load_json_nested(filename: str, *keys: str, default: Any = None) -> Any:
    """# [SSOT Refactoring] 중첩 키 안전 접근.

    예: load_json_nested('shadow_summary.json', 'go_nogo', 'verdict', default='N/A')
    """
    data: Any = load_json(filename)
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return default
        if data is None:
            return default
    return data

import subprocess
import plistlib
import os

@st.cache_data(ttl=30)
def get_launchd_status() -> list:
    """[SSOT Refactoring] launchd 상태 동적 수집 (plist 기반 스케줄 + launchctl list)"""
    d = os.path.expanduser("~/Library/LaunchAgents")
    if not os.path.exists(d):
        return []
        
    try:
        out = subprocess.check_output(["launchctl", "list"]).decode("utf-8")
    except Exception as e:
        logger.warning(f"launchctl list 실행 실패: {e}")
        out = ""
        
    status_map = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and "com.meridian" in parts[2]:
            status_map[parts[2]] = {"pid": parts[0], "status": parts[1]}

    res = []
    for f in os.listdir(d):
        if f.startswith("com.meridian.") and f.endswith(".plist"):
            p = os.path.join(d, f)
            try:
                with open(p, "rb") as fp:
                    pl = plistlib.load(fp)
                lbl = pl.get("Label", "")
                sci = pl.get("StartCalendarInterval", {})
                inter = pl.get("StartInterval", "")
                
                if isinstance(sci, list): sci = sci[0]
                if isinstance(sci, dict) and sci:
                    hr = sci.get("Hour", "*")
                    mi = sci.get("Minute", "*")
                    if hr != "*":
                        sched = f"{int(hr):02d}:{int(mi) if mi != '*' else 0:02d}"
                    else:
                        sched = f"매시 {int(mi) if mi != '*' else 0:02d}분"
                elif inter:
                    sched = f"{int(inter)//60}분 주기" if int(inter) >= 60 else f"{inter}초 주기"
                else:
                    sched = "수동 실행"
                
                pid_info = status_map.get(lbl, {})
                _pid = pid_info.get("pid", "-")
                _exit = pid_info.get("status", "-")
                
                # 가독성 개선
                _status_icon = "🟢 실행중" if _pid != "-" else ("🔴 실패" if _exit not in ("0", "-") else "⚪ 정상 (대기중)")
                
                res.append({
                    "Task": lbl.replace("com.meridian.", ""),
                    "Schedule": sched,
                    "PID": _pid,
                    "Last Exit": _exit,
                    "Status": _status_icon
                })
            except Exception as e:
                pass
                
    res.sort(key=lambda x: x["Schedule"])
    return res

# ─────────────────────────────────────────────────────────────────────────────
# [SSOT Refactoring] 도메인별 로더 함수
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)
def load_shadow_summary() -> Dict:
    """# [SSOT Refactoring] L4 메인 파이프라인 실행 요약 (SSoT 핵심).

    results/shadow_summary.json — L4가 매 사이클마다 업데이트.
    대시보드는 이 파일을 읽기만 함. 절대 재계산 없음.
    """
    return load_json("shadow_summary.json")


@st.cache_data(ttl=10)
def load_stream_metrics() -> Dict:
    """# [SSOT Refactoring] S1~S5 스트림별 성과 메트릭.

    results/stream_metrics.json — L4 StreamOrchestrator가 기록.
    """
    return load_json("stream_metrics.json")


@st.cache_data(ttl=10)
def load_signal_cache() -> Dict:
    """# [SSOT Refactoring] 시장 데이터 캐시 (VIX, KOSPI, OIS 등).

    results/signal_cache.json — MarketDataBridge가 주기적으로 갱신.
    """
    return load_json("signal_cache.json")


@st.cache_data(ttl=10)
def load_shadow_portfolio() -> Dict:
    """# [SSOT Refactoring] 포트폴리오 현황 (포지션, NAV, 거래내역).
    Live 모드일 때는 kis_portfolio.json, Shadow일 때는 shadow_portfolio.json
    """
    import os
    if os.environ.get("KIS_MODE", "shadow").lower() == "live":
        data = load_json("kis_portfolio.json")
        if "account" in data:
            data["virtual_nav"] = data["account"].get("nav", 1_000_000)
            data["initial_capital"] = data["account"].get("initial_capital", 1_000_000)
        return data
    return load_json("shadow_portfolio.json")


@st.cache_data(ttl=10)
def load_shadow_trades() -> List[Dict]:
    """# [SSOT Refactoring] 거래 이력 (SSOT: shadow_portfolio.trade_history).

    Cleanup 거래 제외 실거래만 반환 (is_cleanup=False).
    """
    sp = load_shadow_portfolio()
    th: List[Dict] = sp.get("trade_history", [])
    # [SSOT Refactoring] 클린업 거래 제외 (순수 전략 거래만)
    return [t for t in th if not t.get("is_cleanup", False)]


@st.cache_data(ttl=10)
def load_go_nogo() -> Dict:
    """# [SSOT Refactoring] Go/No-Go 판정 (SSoT: go_nogo.json 우선).

    go_nogo.json → shadow_summary.go_nogo 순으로 병합.
    대시보드는 판정 로직을 갖지 않음.
    """
    gn = load_json("go_nogo.json")
    ss_gn = load_json("shadow_summary.json").get("go_nogo", {})

    # go_nogo.json 우선, shadow_summary로 누락 키 보완
    merged = dict(ss_gn)
    merged.update(gn)

    # Meridian 4-Stream verdict 승격 (있는 경우)
    meridian = merged.get("meridian", {})
    if isinstance(meridian, dict) and meridian.get("verdict"):
        merged["verdict"] = meridian.get("verdict", merged.get("verdict", "N/A"))
        if meridian.get("criteria"):
            merged.setdefault("criteria", {}).update(meridian.get("criteria", {}))

    return merged


@st.cache_data(ttl=10)
def load_measurement_engine() -> Dict:
    """# [SSOT Refactoring] ME 공식 KPI (Sharpe, DA, IC, Grade 등).

    results/measurement_engine.json — MeasurementEngine이 확정한 값.
    대시보드는 이 값을 그대로 표시함. 재계산 없음.
    """
    return load_json("measurement_engine.json")


@st.cache_data(ttl=10)
def load_kill_switch() -> Dict:
    """# [SSOT Refactoring] Kill Switch 상태.

    results/kill_switch.json — 비상 정지 상태 SSoT.
    """
    ks = load_json("kill_switch.json")
    ks2 = load_json("kill_switch_state.json")
    # 두 파일 병합 (kill_switch.json 우선)
    merged = dict(ks2)
    merged.update(ks)
    return merged


@st.cache_data(ttl=10)
def load_risk_data() -> Dict:
    """# [SSOT Refactoring] 리스크 메트릭 종합 뷰.

    여러 리스크 파일을 하나의 dict로 병합해 반환.
    """
    return {
        "drawdown_guard": load_json("drawdown_guard.json"),
        "circuit_breaker": load_json("circuit_breaker.json"),
        "beta_hedge": load_json("beta_hedge.json"),
        "exposure_orchestrator": load_json("exposure_orchestrator.json"),
        "realtime_var": load_json("realtime_var.json"),
        "concentration_risk": load_json("concentration_risk.json"),
        "factor_risk": load_json("factor_risk.json"),
        "risk_budget_state": load_json("risk_budget_state.json"),
        "kill_switch": load_kill_switch(),
    }


@st.cache_data(ttl=10)
def load_stream_signals(stream_id: Optional[str] = None) -> List[Dict]:
    """# [SSOT Refactoring] 최신 스트림 시그널 목록.

    results/latest_signals.json → shadow_portfolio.pending_orders 순으로 시도.

    Args:
        stream_id: 필터할 스트림 ID (예: 'S1'). None이면 전체 반환.
    """
    signals: List[Dict] = []

    # 1차: latest_signals.json
    raw = load_json("latest_signals.json")
    if isinstance(raw, list):
        signals = raw
    elif isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                signals.extend(v)

    # 2차 Fallback: shadow_portfolio.pending_orders
    if not signals:
        sp = load_shadow_portfolio()
        signals = sp.get("pending_orders", [])

    # stream_id 필터
    if stream_id:
        signals = [s for s in signals if s.get("stream_id") == stream_id]

    return signals


@st.cache_data(ttl=10)
def load_alpha_factory() -> Dict:
    """# [SSOT Refactoring] Alpha Factory 발굴 알파 데이터.

    results/discovered_alphas.json — AlphaMiner가 기록.
    """
    raw = load_json("discovered_alphas.json")
    if isinstance(raw, list):
        alphas = raw
    elif isinstance(raw, dict):
        alphas = raw.get("alphas", [raw]) if raw else []
    else:
        alphas = []

    latest = alphas[-1] if alphas else {}
    return {
        "alphas": alphas,
        "latest": latest,
        "sharpe_ratio": float(latest.get("sharpe_ratio", latest.get("sharpe", 0.0)) or 0.0),
        "formula": str(latest.get("formula", latest.get("expression", "N/A"))),
        "fitness": float(latest.get("fitness", 0.0) or 0.0),
        "discovered_at": str(latest.get("discovered_at", latest.get("timestamp", "N/A"))),
        "status": str(latest.get("status", "inactive")),
    }


def load_execution_data() -> Dict:
    """# [SSOT Refactoring] 주문 실행 관련 데이터 종합.

    tca_summary, advisory_orders, shadow_trades 병합.
    """
    sp = load_shadow_portfolio()
    trades = load_shadow_trades()
    real_sells = [t for t in trades if t.get("action") == "SELL"]
    real_buys = [t for t in trades if t.get("action") == "BUY"]

    return {
        "tca_summary": load_json("tca_summary.json"),
        "advisory_orders": load_json("advisory_orders.json"),
        "s6b_advisory": load_json("s6b_advisory.json"),
        "s4_advisory": load_json("s4_advisory_recommendations.json"),
        "shadow_trades": trades,
        "real_sells": real_sells,
        "real_buys": real_buys,
        "realized_pnl": sum(
            float(t.get("realized_pnl") or 0.0) for t in real_sells
        ),
        "realized_trades": len(real_sells),
        "realized_wins": sum(1 for t in real_sells if float(t.get("realized_pnl") or 0.0) > 0),
        "pending_orders": sp.get("pending_orders", []),
        "positions": sp.get("positions", {}),
        "cash": float(sp.get("cash") or 0.0),
        "virtual_nav": float(sp.get("virtual_nav") or 0.0),
    }


@st.cache_data(ttl=10)
def load_macro_data() -> Dict:
    """# [SSOT Refactoring] 매크로 데이터 종합 뷰.

    signal_cache + current_regime + cross_asset_signals 병합.
    """
    sc = load_signal_cache()
    return {
        "signal_cache": sc,
        "vix": float(sc.get("vix") or 0.0),
        "vkospi": float(sc.get("vkospi") or 0.0),
        "kospi": float(sc.get("kospi") or 0.0),
        "usdkrw": float(sc.get("usdkrw") or 0.0),
        "ois": float(sc.get("ois") or 50.0),
        "current_regime": load_json("current_regime.json"),
        "us_market_regime": load_json("us_market_regime.json"),
        "intraday_regime": load_json("intraday_regime.json"),
        "cross_asset_signals": load_json("cross_asset_signals.json"),
        "morning_fusion": load_json("morning_fusion.json"),
        "dynamic_events": load_json("dynamic_events.json"),
    }


@st.cache_data(ttl=10)
def load_signal_model_data() -> Dict:
    """# [SSOT Refactoring] 시그널/모델 품질 데이터 종합.

    ME official + signal_quality_state + feature_importance 병합.
    """
    me = load_measurement_engine()
    official = me.get("official", {})
    return {
        "me": me,
        "official": official,
        "ic": float(official.get("ic") or 0.0),
        "ic_n": int(official.get("ic_n") or 0),
        "ic_p": official.get("ic_p_value"),
        "ic_method": str(official.get("ic_method") or "spearman"),
        "sharpe": float(official.get("sharpe") or 0.0),
        "da": float(official.get("da") or 0.0),
        "grade": str(official.get("grade") or "?"),
        "signal_quality_state": load_json("signal_quality_state.json"),
        "calibration_metrics": (
            load_json("models/calibration_metrics.json")
            if (RESULTS_DIR / "models" / "calibration_metrics.json").exists()
            else load_json("platt_calibration_state.json")
        ),
        "feature_importance": load_json("feature_importance_audit.json"),
        "shap_analysis": load_json("shap_analysis.json"),
        "shap_history": load_json("shap_history.json"),
        "icir_validation": load_json("icir_validation.json"),
        "walk_forward": load_json("walk_forward_results.json"),
        "alpha_decay": load_json("alpha_decay_history.json"),
        "qvm_ic_history": load_json("qvm_ic_history.json"),
        "medallion_validation": load_json("medallion_validation.json"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# [SSOT Refactoring] 통합 KPI 뷰 (모든 페이지가 공유)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)
def get_ssot_kpis() -> Dict:
    """# [SSOT Refactoring] 대시보드 전역 SSoT KPI.

    measurement_engine.json official 섹션을 1차 소스로 사용.
    shadow_portfolio로 실시간 NAV만 보완.
    대시보드는 이 함수의 값을 그대로 표시. 절대 재계산하지 않음.
    """
    me = load_measurement_engine()
    official: Dict = me.get("official", {})
    sp = load_shadow_portfolio()

    # [SSOT Refactoring] ME official 기반 — 재계산 없이 읽기만
    nav = float(official.get("nav") or 0.0)
    # 실시간 NAV: shadow_portfolio 우선 (더 최신)
    sp_nav = float(sp.get("virtual_nav") or 0.0)
    if sp_nav > 0:
        nav = sp_nav

    # [SSOT Refactoring] P&L: shadow_portfolio.trade_history SSoT
    trades = load_shadow_trades()
    real_sells = [t for t in trades if t.get("action") == "SELL"]
    realized_pnl = sum(float(t.get("realized_pnl") or 0.0) for t in real_sells)
    realized_wins = sum(1 for t in real_sells if float(t.get("realized_pnl") or 0.0) > 0)

    return {
        # Core — ME official SSoT
        "da": float(official.get("da") or 0.0),
        "wr": float(
            me.get("views", {}).get("execution", {}).get("l1_win_rate")
            or official.get("realized_win_rate")
            or 0.0
        ),
        "sharpe": float(official.get("sharpe") or 0.0),
        "max_dd": float(official.get("max_drawdown_pct") or 0.0),
        "ic": official.get("ic"),
        "alpha": float(official.get("alpha_pct") or 0.0),
        "nav": nav,
        "grade": str(official.get("grade") or "?"),
        "verdict": str(official.get("verdict") or "N/A"),
        "n_days": int(official.get("n_days") or official.get("total_days") or 0),
        # Risk
        "sortino": official.get("sortino"),
        "calmar": official.get("calmar"),
        "beta": official.get("portfolio_beta"),
        # DA detail
        "da_correct": int(official.get("da_correct") or 0),
        "da_total": int(official.get("da_total") or 0),
        # IC detail
        "ic_n": int(official.get("ic_n") or 0),
        "ic_p": official.get("ic_p_value"),
        "ic_method": str(official.get("ic_method") or "spearman"),
        # P&L — shadow_portfolio SSoT (cleanup 제외)
        "realized_pnl": realized_pnl,
        "realized_trades": len(real_sells),
        "realized_wins": realized_wins,
        "n_buys": sum(1 for t in trades if t.get("action") == "BUY"),
        # Metadata
        "me_timestamp": str(me.get("timestamp") or ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# [SSOT Refactoring] UI 공통 헬퍼 (스타일 무관 데이터 변환)
# ─────────────────────────────────────────────────────────────────────────────

def safe_float(value: Any, default: float = 0.0) -> float:
    """# [SSOT Refactoring] 형변환 Crash 방어 유틸.

    None, 빈 문자열, 잘못된 타입 모두 default로 처리.
    """
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def safe_pct(value: Any, default: float = 0.0, decimals: int = 2) -> str:
    """# [SSOT Refactoring] 퍼센트 포맷 (안전 변환 포함)."""
    v = safe_float(value, default)
    return f"{v:.{decimals}f}%"


def safe_fmt(value: Any, fmt: str = ".2f", default: str = "N/A") -> str:
    """# [SSOT Refactoring] 숫자 포맷 (None/변환오류 → default 문자열)."""
    if value is None:
        return default
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return default


def get_regime_icon(regime: str) -> str:
    """# [SSOT Refactoring] 레짐 아이콘 매핑."""
    return {
        "bull": "🟢 Bull",
        "caution": "🟡 Caution",
        "bear": "🔴 Bear",
        "crash": "🆘 Crash",
    }.get(str(regime).lower(), f"⚪ {regime}")


def get_verdict_class(verdict: str) -> str:
    """# [SSOT Refactoring] verdict → CSS 클래스 매핑."""
    v = str(verdict).upper()
    if "GO" in v and "NO" not in v:
        return "verdict-go"
    if "NO" in v or "STOP" in v:
        return "verdict-nogo"
    return "verdict-wait"


# ─────────────────────────────────────────────────────────────────────────────
# [SSOT Refactoring] 공통 CSS (모든 페이지가 import해서 사용)
# ─────────────────────────────────────────────────────────────────────────────

COMMON_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* ══════════════════════════════════════════════
   1. 글로벌 폰트
   ══════════════════════════════════════════════ */
html, body, .stApp {
    font-family: 'Inter', sans-serif !important;
}

/* ══════════════════════════════════════════════
   2. 메인 콘텐츠 영역: 흰 배경 + 검정 폰트
   ══════════════════════════════════════════════ */
.stApp {
    background-color: #ffffff !important;
    color: #111111 !important;
}

/* 메인 영역의 모든 텍스트 요소 — 검정 폰트 */
.stApp p,
.stApp span:not([data-testid="stSidebar"] span),
.stApp li,
.stApp label,
.stApp div:not([data-testid="stSidebar"] div),
.stApp td,
.stApp th,
.stApp h1,
.stApp h2,
.stApp h3,
.stApp h4,
.stApp h5,
.stApp h6,
.stApp a,
[data-testid="stSubheader"],
details summary,
.stTabs [data-baseweb="tab"],
.stTabs [aria-selected="true"] {
    color: #111111 !important;
}
.stTabs [aria-selected="true"] { font-weight: 600; }

/* 알림/테이블/입력 */
[data-testid="stAlert"] p,
[data-testid="stAlert"] span,
[data-testid="stAlert"] div,
.stDataFrame th,
.stDataFrame td,
[data-testid="stTable"] th,
[data-testid="stTable"] td,
.stTextInput input,
.stNumberInput input {
    color: #111111 !important;
}

/* ══════════════════════════════════════════════
   3. 사이드바: 다크 배경 + 흰 폰트
   ══════════════════════════════════════════════ */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f0c29 0%, #302b63 100%) !important;
}

/* 사이드바 내 모든 텍스트 → 흰색 */
[data-testid="stSidebar"],
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] div,
[data-testid="stSidebar"] li,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] h4,
[data-testid="stSidebar"] h5,
[data-testid="stSidebar"] h6,
[data-testid="stSidebar"] a,
[data-testid="stSidebar"] summary,
[data-testid="stSidebar"] .stMarkdown,
[data-testid="stSidebar"] .stMarkdown p,
[data-testid="stSidebar"] [data-testid="stText"],
[data-testid="stSidebar"] [data-testid="stCaption"] {
    color: #ffffff !important;
}

/* 사이드바 캐시 클리어 버튼: 배경 없애고 흰 테두리/텍스트 */
[data-testid="stSidebar"] .stButton > button {
    background: transparent !important;
    background-color: transparent !important;
    border: 1px solid rgba(255,255,255,0.45) !important;
    color: #ffffff !important;
    border-radius: 8px !important;
    font-size: 0.82rem !important;
    padding: 0.35rem 0.8rem !important;
    transition: border-color 0.2s, background 0.2s !important;
    width: 100% !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(255,255,255,0.1) !important;
    border-color: rgba(255,255,255,0.8) !important;
}
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span {
    color: #ffffff !important;
}

/* ══════════════════════════════════════════════
   4. 특수 박스 예외 (어두운 코드 박스 등)
      → formula-box, py-expr-box, ssetf-* 등
      → .stApp div 전역 규칙보다 높은 특이성 확보
   ══════════════════════════════════════════════ */

/* formula-box: 배경 없음, 검정 폰트, 왼쪽 회색 테두리로 구분 */
.formula-box,
.stApp .formula-box,
[data-testid="stMarkdownContainer"] .formula-box {
    background: #f8f9fa !important;
    background-color: #f8f9fa !important;
    color: #000000 !important;
    border-left: 4px solid #9e9e9e;
    border-top: 1px solid #e0e0e0;
    border-bottom: 1px solid #e0e0e0;
    border-right: 1px solid #e0e0e0;
    border-radius: 0 8px 8px 0;
    padding: 0.8rem 1.2rem;
    font-family: 'Fira Code', 'Courier New', monospace !important;
    font-size: 0.85rem;
    overflow-x: auto;
}
.formula-box *,
.stApp .formula-box * {
    color: #000000 !important;
    background: transparent !important;
    font-family: 'Fira Code', 'Courier New', monospace !important;
}

/* py-expr-box: 배경 없음, 검정 폰트, 초록 왼쪽 테두리 */
.py-expr-box,
.stApp .py-expr-box,
[data-testid="stMarkdownContainer"] .py-expr-box {
    background: #f8f9fa !important;
    background-color: #f8f9fa !important;
    color: #000000 !important;
    border-left: 4px solid #22c55e;
    border-top: 1px solid #e0e0e0;
    border-bottom: 1px solid #e0e0e0;
    border-right: 1px solid #e0e0e0;
    border-radius: 0 8px 8px 0;
    padding: 0.8rem 1.2rem;
    font-family: 'Fira Code', 'Courier New', monospace !important;
    font-size: 0.8rem;
    overflow-x: auto;
}
.py-expr-box *,
.stApp .py-expr-box * {
    color: #000000 !important;
    background: transparent !important;
    font-family: 'Fira Code', 'Courier New', monospace !important;
}

/* ══════════════════════════════════════════════
   5. 공통 컴포넌트
   ══════════════════════════════════════════════ */
.main-header {
    background: #ffffff;
    padding: 1.5rem 2rem;
    border-radius: 16px;
    margin-bottom: 1.5rem;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
    border: 1px solid #e0e0e0;
}
.main-header h1 { margin: 0; font-size: 1.8rem; font-weight: 700; color: #111111 !important; }
.main-header p  { margin: 0.3rem 0 0; font-size: 0.85rem; color: #555555 !important; }

.metric-card {
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 12px;
    padding: 1.2rem 1.4rem;
    transition: transform 0.2s, box-shadow 0.2s;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.metric-card:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.1); }
.metric-card .label { font-size: 0.75rem; color: #888888 !important; text-transform: uppercase; letter-spacing: 0.05em; }
.metric-card .value { font-size: 1.6rem; font-weight: 700; margin: 0.3rem 0; color: #111111 !important; }
.metric-card .delta { font-size: 0.8rem; color: #555555 !important; }

.delta-pos     { color: #00a63e !important; font-weight: 600; }
.delta-neg     { color: #c62828 !important; font-weight: 600; }
.delta-neutral { color: #e65100 !important; font-weight: 600; }

.gonogo-box {
    background: #ffffff; border-radius: 16px; padding: 1.5rem;
    border: 1px solid #e0e0e0; text-align: center;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.verdict-go   { color: #00a63e !important; font-size: 2rem; font-weight: 700; }
.verdict-nogo { color: #c62828 !important; font-size: 2rem; font-weight: 700; }
.verdict-wait { color: #e65100 !important; font-size: 2rem; font-weight: 700; }

.stream-active   { background: #00a63e; color: #fff !important; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; font-weight: 600; }
.stream-inactive { background: #e0e0e0; color: #555 !important; padding: 2px 10px; border-radius: 12px; font-size: 0.75rem; }

.grade-badge { display: inline-block; font-size: 3rem; font-weight: 800; padding: 0.5rem 1.5rem; border-radius: 16px; text-align: center; min-width: 80px; }
.grade-A { background: linear-gradient(135deg, #00c853, #00e676); color: #000 !important; }
.grade-B { background: linear-gradient(135deg, #2196f3, #42a5f5); color: #fff !important; }
.grade-C { background: linear-gradient(135deg, #ffd740, #ffab00); color: #000 !important; }
.grade-D { background: linear-gradient(135deg, #ff6d00, #ff9100); color: #fff !important; }
.grade-F { background: linear-gradient(135deg, #d50000, #ff5252); color: #fff !important; }

.alert-critical {
    background: rgba(198,40,40,0.07); border-left: 4px solid #c62828;
    padding: 0.6rem 1rem; border-radius: 0 8px 8px 0; margin: 0.4rem 0;
    color: #b71c1c !important;
}
.alert-critical * { color: #b71c1c !important; }

.alert-warning {
    background: rgba(230,81,0,0.07); border-left: 4px solid #e65100;
    padding: 0.6rem 1rem; border-radius: 0 8px 8px 0; margin: 0.4rem 0;
    color: #bf360c !important;
}
.alert-warning * { color: #bf360c !important; }

.poll-badge {
    display: inline-block;
    background: linear-gradient(135deg, #00c853, #00e676);
    color: #000 !important;
    padding: 3px 10px; border-radius: 12px;
    font-size: 0.72rem; font-weight: 600;
}

/* ══════════════════════════════════════════════
   6. [Phase 77] KPI metric 폰트 크기 축소
   ══════════════════════════════════════════════ */
[data-testid="stMetricValue"], 
[data-testid="stMetricValue"] > div, 
[data-testid="stMetricValue"] p,
div[data-testid="stMetricValue"] {
    font-size: 16px !important;
    font-weight: 600 !important;
}
[data-testid="stMetricLabel"], 
[data-testid="stMetricLabel"] > div, 
[data-testid="stMetricLabel"] p,
div[data-testid="stMetricLabel"] {
    font-size: 12px !important;
    color: #666666 !important;
}
[data-testid="stMetricDelta"], 
[data-testid="stMetricDelta"] > div,
div[data-testid="stMetricDelta"] {
    font-size: 12px !important;
}
</style>
"""


def inject_common_css() -> None:
    """# [SSOT Refactoring] 공통 CSS를 현재 페이지에 주입."""
    st.markdown(COMMON_CSS, unsafe_allow_html=True)


def metric_card_html(label: str, value: str, delta: str = "",
                     delta_cls: str = "delta-neutral",
                     value_color: str = "#111111") -> str:
    """# [SSOT Refactoring] 메트릭 카드 HTML 빌더."""
    style = f"color:{value_color}!important;" if value_color != "#111111" else ""
    return f"""
    <div class="metric-card">
        <div class="label">{label}</div>
        <div class="value" style="{style}">{value}</div>
        <div class="delta {delta_cls}">{delta}</div>
    </div>"""


# ═══════════════════════════════════════════════════════════════════
# [Phase 16: Dashboard Update] S6 글로벌 알파 데이터 로더
# ═══════════════════════════════════════════════════════════════════

@st.cache_data(ttl=10)
def load_s6a_data() -> dict:
    """[Phase 16: Dashboard Update] S6-A 크립토 차익거래 통합 데이터 로드.

    Returns:
        {
            'signal':      s6a_signal.json,
            'crypto':      crypto_cache.json → kimchi_intel,
            'exec_enter':  s6a_execution_enter.json,
            'exec_exit':   s6a_execution_exit.json,
        }
    """
    result = {
        'signal':     {},
        'crypto':     {},
        'exec_enter': {},
        'exec_exit':  {},
    }
    for key, fname in [
        ('signal',     's6a_signal.json'),
        ('exec_enter', 's6a_execution_enter.json'),
        ('exec_exit',  's6a_execution_exit.json'),
    ]:
        try:
            result[key] = load_json(fname)
        except Exception:
            pass
    try:
        cache = load_json('crypto_cache.json')
        result['crypto'] = cache.get('kimchi_intel', {})
    except Exception:
        pass
    return result


@st.cache_data(ttl=10)
def load_s6b_data() -> dict:
    """[Phase 16: Dashboard Update] S6-B 미국 듀얼 모멘텀 통합 데이터 로드.

    Returns:
        {
            'signal':     s6b_signal.json,
            'execution':  s6b_execution_results.json,
            'us_cache':   us_market_cache.json,
            'backtest':   s6b_backtest_result.json (metrics 요약),
        }
    """
    result = {
        'signal':    {},
        'execution': {},
        'us_cache':  {},
        'backtest':  {},
    }
    for key, fname in [
        ('signal',    's6b_signal.json'),
        ('execution', 's6b_execution_results.json'),
        ('us_cache',  'us_market_cache.json'),
        ('backtest',  's6b_backtest_result.json'),
    ]:
        try:
            result[key] = load_json(fname)
        except Exception:
            pass
    return result


@st.cache_data(ttl=10)
def load_s6_summary() -> dict:
    """[Phase 16: Dashboard Update] S6 Overview 요약 (사이드바용)."""
    s6a = load_s6a_data()
    s6b = load_s6b_data()
    return {
        's6a_signal':       s6a.get('signal', {}).get('signal', 'N/A'),
        's6a_kimchi_pct':   s6a.get('crypto', {}).get('kimchi_pct', 0.0),
        's6a_funding':      s6a.get('crypto', {}).get('funding_rate_annualized', 0.0),
        's6a_leg_failure':  s6a.get('exec_enter', {}).get('leg_failure', False),
        's6b_signal':       s6b.get('signal', {}).get('signal', 'N/A'),
        's6b_asset':        s6b.get('signal', {}).get('selected_asset', 'N/A'),
        's6b_vix_ratio':    s6b.get('signal', {}).get('vix_ratio', 1.0),
        's6b_vix_block':    s6b.get('signal', {}).get('vix_dynamic_block', False),
        's6b_alloc':        s6b.get('signal', {}).get('alloc_breakdown', {}),
    }


@st.cache_data(ttl=10)
def load_system_alerts_extended() -> list:
    """[Phase 16: Dashboard Update] 시스템 알림 + S6 Leg Failure 통합."""
    alerts = []
    try:
        raw = load_json('system_alerts.json')
        if isinstance(raw, list):
            alerts.extend(raw)
        elif isinstance(raw, dict):
            alerts.extend(raw.get('alerts', []))
    except Exception:
        pass
    # S6-A Leg Failure 알림 주입
    try:
        enter = load_json('s6a_execution_enter.json')
        if enter.get('leg_failure'):
            alerts.insert(0, {
                'level':   'CRITICAL',
                'message': f'S6-A LEG FAILURE: {enter.get("unwind_result", {}) or "Unwind 필요"}',
                'ts':      enter.get('timestamp', ''),
            })
    except Exception:
        pass
    return alerts


# ═══════════════════════════════════════════════════════════════════════════════
# [Phase 17: Global Unified Dashboard] NAV 히스토리 & 통합 메트릭 로더
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=10)
def load_nav_history() -> "pd.DataFrame":
    """[Phase 17: Global Unified Dashboard] 전체 NAV 시계열 재구성.

    데이터 소스 우선순위 (다중 소스 병합):
      1. measurement_engine.json → daily_series (가장 완전)
      2. shadow_portfolio.json   → daily_snapshots (보완)
      3. shadow_summary.json     → daily_stats (최신 날짜 보완)

    Returns:
        DataFrame: index=Date, columns=['nav', 'daily_ret_pct', 'cum_ret_pct']
    """
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        return {}

    rows = {}

    # ── 소스 1: measurement_engine daily_series ───────────────
    try:
        me = load_measurement_engine()
        for d in me.get("daily_series", []):
            date = str(d.get("date", ""))[:10]
            nav  = float(d.get("nav") or 0)
            ret  = float(d.get("daily_return_pct") or 0)
            if date and nav > 0:
                rows[date] = {"nav": nav, "daily_ret_pct": ret}
    except Exception:
        pass

    # ── 소스 2: shadow_portfolio daily_snapshots ──────────────
    try:
        sp = load_shadow_portfolio()
        for d in sp.get("daily_snapshots", []):
            date = str(d.get("date", ""))[:10]
            nav  = float(d.get("nav") or 0)
            ret  = float(d.get("daily_return_pct") or 0)
            if date and nav > 0 and date not in rows:
                rows[date] = {"nav": nav, "daily_ret_pct": ret}
        for d in sp.get("daily_records", []):
            date = str(d.get("date", ""))[:10]
            nav  = float(d.get("nav") or d.get("virtual_nav") or 0)
            ret  = float(d.get("daily_return_pct") or d.get("return_pct") or 0)
            if date and nav > 0 and date not in rows:
                rows[date] = {"nav": nav, "daily_ret_pct": ret}
        # 현재 NAV를 오늘 날짜로 추가
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        curr_nav = float(sp.get("virtual_nav") or 0)
        if curr_nav > 0 and today not in rows:
            rows[today] = {"nav": curr_nav, "daily_ret_pct": 0.0}
    except Exception:
        pass

    # ── 소스 3: shadow_summary daily_stats ────────────────────
    try:
        ss = load_shadow_summary()
        for d in ss.get("daily_stats", []):
            date = str(d.get("date", ""))[:10]
            nav  = float(d.get("nav") or 0)
            if date and nav > 0 and date not in rows:
                rows[date] = {"nav": nav, "daily_ret_pct": 0.0}
    except Exception:
        pass

    if not rows:
        return pd.DataFrame(columns=["nav", "daily_ret_pct", "cum_ret_pct"])

    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index = pd.to_datetime(df.index, errors="coerce")
    df = df.dropna(subset=["nav"]).sort_index()

    # [Phase 17] 누적 수익률 계산 (첫 NAV 기준)
    initial = df["nav"].iloc[0]
    df["cum_ret_pct"] = (df["nav"] / initial - 1) * 100
    df["daily_ret_pct"] = df["daily_ret_pct"].fillna(0)
    df["daily_pnl"] = df["nav"].diff().fillna(0)

    return df


@st.cache_data(ttl=10)
def load_global_kpis() -> dict:
    """[Phase 17: Global Unified Dashboard] S1~S6 통합 글로벌 KPI 계산.

    통합 로직:
      S1~S5: shadow_portfolio.json virtual_nav + realized_pnl
      S6-A:  s6a_execution_enter.json net_krw (투자금 기준)
      S6-B:  s6b_execution_results.json (체결 결과)
    """
    base = get_ssot_kpis()
    sp   = load_shadow_portfolio()

    # ── 기본 NAV (S1~S5) ─────────────────────────────────────
    kr_nav   = float(sp.get("virtual_nav") or base.get("nav") or 232_000_000)
    kr_init  = float(sp.get("initial_capital") or 232_000_000)
    kr_real_pnl = float(sp.get("realized_pnl") or 0)
    kr_unreal   = float(sp.get("unrealized_pnl") or 0)
    kr_ret_pct  = float(sp.get("total_return_pct") or 0)

    # ── S6-A 크립토 ───────────────────────────────────────────
    s6a_invested = 0.0
    s6a_pnl      = 0.0
    try:
        enter = load_json("s6a_execution_enter.json")
        if enter.get("success") and not enter.get("leg_failure"):
            s6a_invested = float(enter.get("net_krw") or 0)
    except Exception:
        pass

    # ── S6-B 미국 주식 ────────────────────────────────────────
    s6b_pnl   = 0.0
    s6b_fills = 0
    try:
        exec6b = load_json("s6b_execution_results.json")
        results = exec6b.get("results") or []
        s6b_fills = sum(1 for r in results if r.get("success"))
        # 수익은 현재 미확정이므로 투자금의 0으로 처리 (실매매 연동 전)
        s6b_pnl = 0.0
    except Exception:
        pass

    # ── 통합 글로벌 NAV ───────────────────────────────────────
    global_nav       = kr_nav + s6a_invested  # S6-A 투자금 합산
    global_init      = kr_init
    global_total_pnl = kr_real_pnl + kr_unreal + s6a_pnl + s6b_pnl
    global_ret_pct   = (global_nav / global_init - 1) * 100 if global_init > 0 else 0

    return {
        # ── 글로벌 통합 ──
        "global_nav":       global_nav,
        "global_init":      global_init,
        "global_ret_pct":   global_ret_pct,
        "global_total_pnl": global_total_pnl,
        # ── S1~S5 국내주식 ──
        "kr_nav":         kr_nav,
        "kr_init":        kr_init,
        "kr_ret_pct":     kr_ret_pct,
        "kr_real_pnl":    kr_real_pnl,
        "kr_unreal":      kr_unreal,
        # ── S6-A 크립토 ──
        "s6a_invested":   s6a_invested,
        "s6a_pnl":        s6a_pnl,
        # ── S6-B 미국주식 ──
        "s6b_fills":      s6b_fills,
        "s6b_pnl":        s6b_pnl,
        # ── 기존 KPI ──
        **{k: v for k, v in base.items() if k not in ("nav",)},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# [Phase 17: Global Unified Dashboard] NAV 히스토리 & 통합 메트릭 로더
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# [Phase 18: SS-ETF + Alpha Factory v2] 캐시 무효화 강화 로더
# ═══════════════════════════════════════════════════════════════════
import os as _os


def _file_mtime(filename: str) -> float:
    """results/ 파일의 수정 시간 반환 (캐시 키 갱신 트리거용).

    파일이 없으면 0.0 반환. 이 값을 @st.cache_data 함수의 인자로
    넘기면 파일 변경 즉시 캐시가 깨짐.
    """
    path = RESULTS_DIR / filename
    try:
        return float(_os.path.getmtime(str(path)))
    except Exception:
        return 0.0


def _get_ss_etf_mtime() -> float:
    """SS-ETF 관련 파일 중 가장 최신 mtime 반환."""
    candidates = [
        'ss_etf_risk.json',
        'signal_cache.json',
        'intraday_regime.json',
    ]
    mtimes = [_file_mtime(f) for f in candidates]
    return max(mtimes) if mtimes else 0.0


def _get_alpha_mtime() -> float:
    """discovered_alphas.json mtime 반환."""
    return _file_mtime('discovered_alphas.json')


# ── SS-ETF 리스크 데이터 로더 ─────────────────────────────────

@st.cache_data(ttl=60)
def load_ss_etf_risk(_mtime: float = 0.0) -> dict:
    """[Phase 18] SS-ETF 단일종목 파생 리스크 팩터 로드.

    소스 우선순위:
      1. results/ss_etf_risk.json (파이프라인이 써놓은 SSoT)
      2. signal_cache.json → ss_etf 키
      3. 모두 없으면 '수집 대기 중' 기본값 반환

    Args:
        _mtime: 파일 수정 시간 (캐시 키 — 파일 변경 시 자동 무효화)

    Returns:
        {
            'samsung': { 'vol_ratio': float, 'lp_pressure': float, 'vol_anomaly': float },
            'hynix':   { ... },
            'combined_warning': bool,
            'source': str,
            'timestamp': str,
        }
    """
    _DEFAULT_TICKER = {
        'vol_ratio':    0.0,
        'lp_pressure':  0.0,
        'vol_anomaly':  0.0,
        'status':       '수집 대기 중',
    }
    result = {
        'samsung':          dict(_DEFAULT_TICKER),
        'hynix':            dict(_DEFAULT_TICKER),
        'combined_warning': False,
        'source':           '수집 대기 중',
        'timestamp':        '',
        'thresholds': {
            'vol_ratio_caution':  0.15,
            'vol_ratio_warning':  0.30,
            'vol_ratio_critical': 0.50,
            'vol_anomaly_caution': 1.3,
            'vol_anomaly_warning': 1.5,
        },
    }

    # 소스 1: ss_etf_risk.json (파이프라인 출력물)
    try:
        raw = load_json('ss_etf_risk.json')
        if raw and isinstance(raw, dict):
            result.update({
                'samsung':   raw.get('samsung',   _DEFAULT_TICKER),
                'hynix':     raw.get('hynix',     _DEFAULT_TICKER),
                'combined_warning': bool(raw.get('combined_warning', False)),
                'timestamp': str(raw.get('timestamp', '')),
                'source':    'ss_etf_risk.json',
            })
            if raw.get('thresholds'):
                result['thresholds'].update(raw['thresholds'])
            return result
    except Exception:
        pass

    # 소스 2: signal_cache.json → ss_etf 키 (통합 캐시)
    try:
        sc = load_json('signal_cache.json')
        ss_etf_raw = sc.get('ss_etf', {})
        if ss_etf_raw and isinstance(ss_etf_raw, dict):
            def _parse_ticker(d: dict) -> dict:
                return {
                    'vol_ratio':   float(d.get('ss_etf_vol_ratio',     d.get('vol_ratio',    0.0)) or 0.0),
                    'lp_pressure': float(d.get('lp_delta_pressure',    d.get('lp_pressure',  0.0)) or 0.0),
                    'vol_anomaly': float(d.get('intraday_vol_anomaly', d.get('vol_anomaly',  0.0)) or 0.0),
                    'status':      '데이터 수신',
                }
            for key, ticker_key in [('samsung', '005930'), ('hynix', '000660')]:
                ticker_data = ss_etf_raw.get(key) or ss_etf_raw.get(ticker_key, {})
                if ticker_data:
                    result[key] = _parse_ticker(ticker_data)
            result['source'] = 'signal_cache.json → ss_etf'
            result['timestamp'] = str(sc.get('timestamp', ''))
            thr = result['thresholds']
            any_warning = (
                result['samsung']['vol_ratio'] >= thr['vol_ratio_warning'] or
                result['hynix']['vol_ratio']   >= thr['vol_ratio_warning'] or
                result['samsung']['vol_anomaly'] >= thr['vol_anomaly_warning'] or
                result['hynix']['vol_anomaly']   >= thr['vol_anomaly_warning']
            )
            result['combined_warning'] = any_warning
            return result
    except Exception:
        pass

    return result


def _ss_etf_level(vol_ratio: float, thresholds: dict) -> tuple:
    """vol_ratio 기준 레벨 문자열 + 색상 코드 반환."""
    critical = thresholds.get('vol_ratio_critical', 0.50)
    warning  = thresholds.get('vol_ratio_warning',  0.30)
    caution  = thresholds.get('vol_ratio_caution',  0.15)
    if vol_ratio >= critical:
        return '🔴 위험', '#d50000'
    if vol_ratio >= warning:
        return '🟠 경고', '#e65100'
    if vol_ratio >= caution:
        return '🟡 주의', '#f57f17'
    return '🟢 정상', '#2e7d32'


# ── Alpha Factory v2 로더 ─────────────────────────────────────

@st.cache_data(ttl=60)
def load_alpha_factory_v2(_mtime: float = 0.0) -> dict:
    """[Phase 18] Alpha Factory v2 — 동적 파싱 로더.

    features_used, oos_ic, py_expr 등 v2 필드를 완전히 지원.
    기존 v1 필드(sharpe_ratio, fitness)와 하위 호환.

    Args:
        _mtime: discovered_alphas.json mtime (캐시 키)

    Returns:
        {
            'all':         [dict, ...],           # 전체 알파 목록
            'active':      [dict, ...],           # status == 'active'
            'retired':     [dict, ...],
            'inactive':    [dict, ...],
            'n_total':     int,
            'n_active':    int,
            'best_active': dict,                  # OOS IC 최고 활성 알파
            'feature_usage': {feat: count, ...},  # 피처별 사용 빈도
        }
    """
    _empty = {
        'all': [], 'active': [], 'retired': [], 'inactive': [],
        'n_total': 0, 'n_active': 0,
        'best_active': {},
        'feature_usage': {},
    }

    try:
        raw = load_json('discovered_alphas.json')
    except Exception:
        return _empty

    # 리스트 또는 dict 형태 모두 처리
    if isinstance(raw, list):
        alphas = raw
    elif isinstance(raw, dict):
        alphas = raw.get('alphas', [raw]) if raw else []
    else:
        return _empty

    if not alphas:
        return _empty

    # 상태 분류
    active_list   = [a for a in alphas if str(a.get('status', '')).lower() == 'active']
    retired_list  = [a for a in alphas if str(a.get('status', '')).lower() == 'retired']
    inactive_list = [a for a in alphas if str(a.get('status', '')).lower() not in ('active', 'retired')]

    # 활성 알파 OOS IC 내림차순 정렬
    def _oos_ic(a: dict) -> float:
        return float(a.get('oos_ic', a.get('sharpe_ratio', 0.0)) or 0.0)

    active_sorted = sorted(active_list, key=_oos_ic, reverse=True)
    best = active_sorted[0] if active_sorted else (alphas[-1] if alphas else {})

    # 피처 사용 빈도 집계 (동적 파싱 — 하드코딩 금지)
    feature_usage: dict = {}
    for a in alphas:
        for f in (a.get('features_used') or []):
            feature_usage[f] = feature_usage.get(f, 0) + 1

    return {
        'all':           alphas,
        'active':        active_sorted,
        'retired':       retired_list,
        'inactive':      inactive_list,
        'n_total':       len(alphas),
        'n_active':      len(active_list),
        'best_active':   best,
        'feature_usage': dict(sorted(feature_usage.items(), key=lambda x: -x[1])),
    }


# ── 레거시 캐시 영구 차단 — 수동 클리어 버튼 헬퍼 ──────────────

def render_cache_clear_button(location: str = 'sidebar', page_key: str = 'default') -> None:
    """[Phase 18] 수동 캐시 클리어 버튼 렌더링.

    클릭 시 st.cache_data.clear() + st.rerun() 호출.
    모든 대시보드 페이지 최상단 또는 사이드바에서 호출.

    Args:
        location: 'sidebar' | 'main'
        page_key: 페이지마다 고유 키 — DuplicateWidgetID 방지
    """
    _container = st.sidebar if location == 'sidebar' else st
    _btn_key = f'_phase18_cache_clear_{page_key}'
    if _container.button('🔄 캐시 비우고 최신 데이터 로드', key=_btn_key):
        try:
            st.cache_data.clear()
        except Exception:
            pass
        try:
            st.cache_resource.clear()
        except Exception:
            pass
        st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# [Phase 39: Dashboard Integration] Moonshot Booster System 데이터 로더
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=10)
def load_moonshot_status() -> dict:
    """[Phase 39] Moonshot Booster 4대 부스터 통합 상태 로드 — SSoT 원칙 준수.

    Returns:
        {
            # Kelly Booster
            'kelly_active':       bool,   # 현금 비율 0%로 오버라이드 중인지
            'kelly_cash_ratio':   float,  # 현재 런타임 현금 비율 (0.0 or 0.18...)
            'kelly_max_pos':      int,    # 현재 압축 종목 수 한도
            'kelly_trail_mult':   float,  # 트레일링 ATR 배수 (1.5 or 2.0)
            # S6-B VIX 기어
            'vix_current':        float,
            'vix_gear':           str,    # TQQQ / QQQ / SQQQ / TLT
            'vix_p_low':          float,  # 현재 P40 경계값 (없으면 0)
            'vix_p_high':         float,  # 현재 P70 경계값 (없으면 0)
            'vix_abs_max_tqqq':   float,  # TQQQ 절대값 상한
            'vix_abs_min_sqqq':   float,  # SQQQ 절대값 하한
            'vix_tqqq_safe':      bool,   # VIX가 TQQQ 안전 구간
            'us_direct_budget':   float,  # 야간 US Direct 예산 (0이면 미산출)
            'us_direct_regime':   str,    # 예산 산출 기준 레짐
            # S6-A ADL
            'crypto_leverage':    int,    # 현재 바이낸스 레버리지
            'adl_trigger_pct':    float,  # ADL 동적 트리거 (%)
            'adl_safety_factor':  float,  # ADL 안전 계수
            # S1 Hard Limit
            'hard_limit_minute':  int,    # 15:XX 강제 청산 분 (기본 20)
            'us_budget_ratio_bull':    float,
            'us_budget_ratio_caution': float,
        }
    """
    result = {
        'kelly_active':            False,
        'kelly_cash_ratio':        0.18,
        'kelly_max_pos':           3,
        'kelly_trail_mult':        2.0,
        'vix_current':             20.0,
        'vix_gear':                'QQQ',
        'vix_p_low':               0.0,
        'vix_p_high':              0.0,
        'vix_abs_max_tqqq':        20.0,
        'vix_abs_min_sqqq':        25.0,
        'vix_tqqq_safe':           False,
        'us_direct_budget':        0.0,
        'us_direct_regime':        'caution',
        'crypto_leverage':         1,
        'adl_trigger_pct':         0.0,
        'adl_safety_factor':       0.7,
        'hard_limit_minute':       20,
        'us_budget_ratio_bull':    0.35,
        'us_budget_ratio_caution': 0.20,
    }

    # ── 1. DynamicConfig 파라미터 읽기 ──────────────────────────────────────
    try:
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        result['kelly_cash_ratio']        = float(cfg.get('kelly.booster_cash_ratio',    0.0))
        result['kelly_max_pos']           = int(  cfg.get('kelly.max_pos_fallback',       3))
        result['kelly_trail_mult']        = float(cfg.get('kelly.trail_atr_multiplier',  2.0))
        result['vix_abs_max_tqqq']        = float(cfg.get('s6b.vix_tqqq_abs_max',       20.0))
        result['vix_abs_min_sqqq']        = float(cfg.get('s6b.vix_sqqq_abs_min',       25.0))
        result['hard_limit_minute']       = int(  cfg.get('a1.hard_limit_close_minute',  20))
        result['adl_safety_factor']       = float(cfg.get('s6a.adl_safety_factor',       0.7))
        result['crypto_leverage']         = int(  cfg.get('s6a.binance_leverage',         1))
        result['us_budget_ratio_bull']    = float(cfg.get('s6b.us_direct_budget_ratio.bull',    0.35))
        result['us_budget_ratio_caution'] = float(cfg.get('s6b.us_direct_budget_ratio.caution', 0.20))
        # ADL 동적 트리거 계산
        lev = result['crypto_leverage']
        if lev > 1:
            result['adl_trigger_pct'] = (1.0 - (1.0 / lev) * result['adl_safety_factor']) * 100
    except Exception:
        pass

    # ── 2. s6b_signal.json — VIX 기어 상태 ─────────────────────────────────
    try:
        s6b = load_json('s6b_signal.json') or {}
        result['vix_current']  = float(s6b.get('vix_current', 20.0) or 20.0)
        result['vix_gear']     = str(s6b.get('vix_gear', s6b.get('selected_asset', 'QQQ')) or 'QQQ')
        result['vix_p_low']    = float(s6b.get('vix_p_low',  0.0) or 0.0)
        result['vix_p_high']   = float(s6b.get('vix_p_high', 0.0) or 0.0)
        # TQQQ 안전 구간 판단
        vix_c  = result['vix_current']
        vix_pl = result['vix_p_low']
        vix_ph = result['vix_p_high']
        abs_max = result['vix_abs_max_tqqq']
        result['vix_tqqq_safe'] = (vix_c < vix_pl and vix_c < abs_max) if vix_pl > 0 else (vix_c < abs_max)
    except Exception:
        pass

    # ── 3. signal_cache.json — VIX 이력에서 퍼센타일 보완 ──────────────────
    try:
        if result['vix_p_low'] == 0.0:
            import numpy as np
            sc = load_json('signal_cache.json') or {}
            vix_hist = [float(v) for v in sc.get('vix_history', []) if v]
            if len(vix_hist) >= 20:
                result['vix_p_low']  = float(np.percentile(vix_hist[-252:], 40))
                result['vix_p_high'] = float(np.percentile(vix_hist[-252:], 70))
    except Exception:
        pass

    # ── 4. shadow_portfolio.json — Kelly Booster 활성화 상태 ────────────────
    try:
        sp = load_shadow_portfolio() or {}
        # 현재 target_cash_ratio가 0%로 오버라이드 됐는지 확인
        # stream_orchestrator가 runtime에 적용한 값 저장 여부 확인
        runtime_cash = sp.get('runtime_cash_ratio', None)
        if runtime_cash is not None:
            result['kelly_active']     = (float(runtime_cash) == 0.0)
            result['kelly_cash_ratio'] = float(runtime_cash)
        # Kelly 압축 종목 수
        kelly_max = sp.get('kelly_max_positions', None)
        if kelly_max is not None:
            result['kelly_max_pos'] = int(kelly_max)
        # 트레일링 배수
        trail_mult = sp.get('runtime_trail_atr_multiplier', None)
        if trail_mult is not None:
            result['kelly_trail_mult'] = float(trail_mult)
    except Exception:
        pass

    # ── 5. pipeline_state.json — 레짐 + US Direct 야간 예산 ─────────────────
    try:
        pstate = load_json('pipeline_state.json') or {}
        regime = str(pstate.get('regime', 'caution') or 'caution')
        result['us_direct_regime']  = regime
        ratio_key = f's6b.us_direct_budget_ratio.{regime}'
        try:
            from config.dynamic_config import DynamicConfig
            ratio = float(DynamicConfig().get(ratio_key, 0.20))
        except Exception:
            ratio = 0.20
        # 야간 예산 = 잔고 정보가 있으면 계산 (없으면 0 반환)
        result['us_direct_budget'] = float(pstate.get('us_direct_budget_krw', 0) or 0)
        if result['us_direct_budget'] == 0:
            result['us_direct_budget'] = float(pstate.get('cash_krw', 0) or 0) * ratio
    except Exception:
        pass

    return result


# ══════════════════════════════════════════════════════════════
# ★ SURGERY-2026-07-10: 신규 로더 — AlphaMemoryStore, CrashRadar,
#   Sleeve NAV, Entry Score
# ══════════════════════════════════════════════════════════════

@st.cache_data(ttl=10)
def load_alpha_memory_store() -> dict:
    """AlphaMemoryStore 요약 로드 (failed_alpha_memory.json).

    Returns:
        {
          'total': int,
          'by_regime': {regime: count},
          'top_penalty_features': [{feature, penalty}],
          'raw_records': list  (최근 20개)
        }
    """
    try:
        path = RESULTS_DIR / 'failed_alpha_memory.json'
        if not path.exists():
            return {'total': 0, 'by_regime': {}, 'top_penalty_features': [], 'raw_records': []}
        records = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(records, list):
            return {'total': 0, 'by_regime': {}, 'top_penalty_features': [], 'raw_records': []}

        # 레짐별 집계
        by_regime: dict = {}
        feature_fail: dict = {}
        total = len(records)

        for rec in records:
            r = rec.get('retire_regime', 'unknown')
            by_regime[r] = by_regime.get(r, 0) + 1
            for feat in rec.get('features_used', []):
                feature_fail[feat] = feature_fail.get(feat, 0) + 1

        penalty = [
            {'feature': f, 'penalty': round(cnt / total, 4)}
            for f, cnt in sorted(feature_fail.items(), key=lambda x: x[1], reverse=True)
        ][:10]

        return {
            'total': total,
            'by_regime': by_regime,
            'top_penalty_features': penalty,
            'raw_records': records[-20:],  # 최근 20개
        }
    except Exception:
        return {'total': 0, 'by_regime': {}, 'top_penalty_features': [], 'raw_records': []}


@st.cache_data(ttl=10)
def load_crash_radar() -> dict:
    """CrashRadar 신호 로드.

    current_regime.json 또는 regime/current.json에서 crash_radar 키 추출.

    Returns:
        {
          'crash_prob': float (0~1),
          'is_crash_warning': bool,
          'vix_score': float,
          'volume_score': float,
          'fear_score': float,
          'regime': str,
        }
    """
    default = {
        'crash_prob': 0.0, 'is_crash_warning': False,
        'vix_score': 0.0, 'volume_score': 0.0, 'fear_score': 0.0,
        'regime': 'unknown',
    }
    try:
        # 1순위: current_regime.json
        p1 = RESULTS_DIR / 'current_regime.json'
        p2 = RESULTS_DIR.parent / 'data' / 'regime' / 'current.json'
        data = {}
        for p in [p1, p2]:
            if p.exists():
                data = json.loads(p.read_text(encoding='utf-8'))
                break

        result = dict(default)
        result['regime'] = str(data.get('regime', data.get('regime_lower', 'unknown')))

        # crash_radar 서브키
        cr = data.get('crash_radar', {})
        if cr:
            result['crash_prob'] = float(cr.get('crash_prob', 0.0))
            result['is_crash_warning'] = bool(cr.get('is_crash_warning', False))
            result['vix_score'] = float(cr.get('vix_score', 0.0))
            result['volume_score'] = float(cr.get('volume_score', 0.0))
            result['fear_score'] = float(cr.get('fear_score', 0.0))
        else:
            # crash_radar 키 없으면 레짐에서 추정
            regime_lower = result['regime'].lower()
            if 'crash' in regime_lower:
                result['crash_prob'] = 0.8
                result['is_crash_warning'] = True
            elif 'bear' in regime_lower:
                result['crash_prob'] = 0.35

        return result
    except Exception:
        return default


@st.cache_data(ttl=10)
def load_sleeve_nav() -> dict:
    """Sleeve A / Sleeve B NAV 실측 로드.

    shadow_portfolio.json에서 sleeve_a_nav, sleeve_b_nav 직접 읽음.
    DrawdownGuard 수술로 MTM 시 자동 갱신됨.

    Returns:
        {
          'sleeve_a_nav': float,   'sleeve_b_nav': float,
          'sleeve_a_hwm': float,   'sleeve_b_hwm': float,
          'sleeve_a_dd': float,    'sleeve_b_dd': float,   (0~1, negative)
          'sleeve_a_ret': float,   'sleeve_b_ret': float,  (초기 대비 수익률)
          'total_nav': float,
          'initial_capital': float,
        }
    """
    try:
        sp = load_shadow_portfolio() or {}
        total = float(sp.get('total_nav') or sp.get('virtual_nav') or 0)
        init = float(sp.get('initial_capital') or total or 1)
        sleeve_a_ratio = 0.60  # 기본값
        try:
            from config.dynamic_config import DynamicConfig
            sleeve_a_ratio = float(DynamicConfig().get('portfolio.sleeve_a_ratio', 0.60))
        except Exception:
            pass

        sleeve_a_nav = float(sp.get('sleeve_a_nav') or total * sleeve_a_ratio)
        sleeve_b_nav = float(sp.get('sleeve_b_nav') or total * (1 - sleeve_a_ratio))
        sleeve_a_hwm = float(sp.get('sleeve_a_hwm') or sleeve_a_nav)
        sleeve_b_hwm = float(sp.get('sleeve_b_hwm') or sleeve_b_nav)

        sleeve_a_init = init * sleeve_a_ratio
        sleeve_b_init = init * (1 - sleeve_a_ratio)

        sleeve_a_dd = (sleeve_a_nav / sleeve_a_hwm - 1) if sleeve_a_hwm > 0 else 0
        sleeve_b_dd = (sleeve_b_nav / sleeve_b_hwm - 1) if sleeve_b_hwm > 0 else 0
        sleeve_a_ret = (sleeve_a_nav / sleeve_a_init - 1) if sleeve_a_init > 0 else 0
        sleeve_b_ret = (sleeve_b_nav / sleeve_b_init - 1) if sleeve_b_init > 0 else 0

        return {
            'sleeve_a_nav': sleeve_a_nav, 'sleeve_b_nav': sleeve_b_nav,
            'sleeve_a_hwm': sleeve_a_hwm, 'sleeve_b_hwm': sleeve_b_hwm,
            'sleeve_a_dd': sleeve_a_dd,   'sleeve_b_dd': sleeve_b_dd,
            'sleeve_a_ret': sleeve_a_ret, 'sleeve_b_ret': sleeve_b_ret,
            'total_nav': total, 'initial_capital': init,
        }
    except Exception:
        return {
            'sleeve_a_nav': 0, 'sleeve_b_nav': 0,
            'sleeve_a_hwm': 0, 'sleeve_b_hwm': 0,
            'sleeve_a_dd': 0,  'sleeve_b_dd': 0,
            'sleeve_a_ret': 0, 'sleeve_b_ret': 0,
            'total_nav': 0, 'initial_capital': 0,
        }


@st.cache_data(ttl=10)
def load_entry_scores() -> dict:
    """진입 필터 Entry Score 현황 로드.

    stream_metrics.json에서 최근 entry_score 집계.

    Returns:
        {
          'avg_score': float,
          'allow_rate': float,  (0~1)
          'recent_scores': [{stream, score, allowed}],
          'hard_stops': int,
        }
    """
    try:
        sm = load_stream_metrics() or {}
        scores = []
        hard_stops = 0

        for sid, sdata in sm.items():
            if not isinstance(sdata, dict):
                continue
            entry = sdata.get('entry_score', {})
            if not entry:
                continue
            score = float(entry.get('entry_score', 0) or 0)
            allowed = bool(entry.get('entry_allowed', False))
            hard_stop = bool(entry.get('hard_stop', False))
            scores.append({'stream': sid, 'score': score, 'allowed': allowed})
            if hard_stop:
                hard_stops += 1

        if not scores:
            return {'avg_score': 0.0, 'allow_rate': 0.0, 'recent_scores': [], 'hard_stops': 0}

        avg = sum(s['score'] for s in scores) / len(scores)
        allow_rate = sum(1 for s in scores if s['allowed']) / len(scores)

        return {
            'avg_score': round(avg, 4),
            'allow_rate': round(allow_rate, 4),
            'recent_scores': scores,
            'hard_stops': hard_stops,
        }
    except Exception:
        return {'avg_score': 0.0, 'allow_rate': 0.0, 'recent_scores': [], 'hard_stops': 0}

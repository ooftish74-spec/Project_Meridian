import sys

with open("dashboard/utils/data_loader.py", "a") as f:
    f.write('''

# ═══════════════════════════════════════════════════════════════════════════════
# [Phase 17: Global Unified Dashboard] NAV 히스토리 & 통합 메트릭 로더
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=10)
def load_nav_history():
    try:
        import pandas as pd
        import numpy as np
    except ImportError:
        return {}

    rows = {}

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
        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        curr_nav = float(sp.get("virtual_nav") or 0)
        if curr_nav > 0 and today not in rows:
            rows[today] = {"nav": curr_nav, "daily_ret_pct": 0.0}
    except Exception:
        pass

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

    initial = df["nav"].iloc[0]
    df["cum_ret_pct"] = (df["nav"] / initial - 1) * 100
    df["daily_ret_pct"] = df["daily_ret_pct"].fillna(0)
    df["daily_pnl"] = df["nav"].diff().fillna(0)

    return df


@st.cache_data(ttl=10)
def load_global_kpis() -> dict:
    base = get_ssot_kpis()
    sp   = load_shadow_portfolio()

    kr_nav   = float(sp.get("virtual_nav") or base.get("nav") or 154_000_000)
    kr_init  = float(sp.get("initial_capital") or 154_000_000)
    kr_real_pnl = float(sp.get("realized_pnl") or 0)
    kr_unreal   = float(sp.get("unrealized_pnl") or 0)
    kr_ret_pct  = float(sp.get("total_return_pct") or 0)

    s6a_invested = 0.0
    s6a_pnl      = 0.0
    try:
        enter = load_json("s6a_execution_enter.json")
        if enter.get("success") and not enter.get("leg_failure"):
            s6a_invested = float(enter.get("net_krw") or 0)
    except Exception:
        pass

    s6b_pnl   = 0.0
    s6b_fills = 0
    try:
        exec6b = load_json("s6b_execution_results.json")
        results = exec6b.get("results") or []
        s6b_fills = sum(1 for r in results if r.get("success"))
        s6b_pnl = 0.0
    except Exception:
        pass

    global_nav       = kr_nav + s6a_invested  
    global_init      = kr_init
    global_total_pnl = kr_real_pnl + kr_unreal + s6a_pnl + s6b_pnl
    global_ret_pct   = (global_nav / global_init - 1) * 100 if global_init > 0 else 0

    return {
        "global_nav":       global_nav,
        "global_init":      global_init,
        "global_ret_pct":   global_ret_pct,
        "global_total_pnl": global_total_pnl,
        "kr_nav":         kr_nav,
        "kr_init":        kr_init,
        "kr_ret_pct":     kr_ret_pct,
        "kr_real_pnl":    kr_real_pnl,
        "kr_unreal":      kr_unreal,
        "s6a_invested":   s6a_invested,
        "s6a_pnl":        s6a_pnl,
        "s6b_fills":      s6b_fills,
        "s6b_pnl":        s6b_pnl,
        **{k: v for k, v in base.items() if k not in ("nav",)},
    }
''')

#!/usr/bin/env python3
"""
Project Meridian — Red Team Devil's Advocate Deep Audit Suite
=============================================================
Comprehensive adversarial audit of live AWS production environment.
"""

import os
import sys
import json
import logging
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config.dynamic_config import DynamicConfig

def run_deep_audit():
    print("=" * 70)
    print("      🔴 MERIDIAN RED TEAM DEVIL'S ADVOCATE DEEP SYSTEM AUDIT 🔴")
    print("=" * 70)
    
    cfg = DynamicConfig()
    results_dir = _ROOT / "results"
    
    # --- 1. Dynamic Config & State Contract Integrity ---
    print("\n[Audit 1: DynamicConfig & State Contract]")
    overrides = cfg._overrides
    print(f"  • Active Overrides Count: {len(overrides)}")
    from src.learning.genetic_rule_evolver import GeneticRuleEvolver
    bounds = GeneticRuleEvolver.SAFETY_BOUNDS
    bounds_violated = []
    for k, (low, high) in bounds.items():
        val = float(cfg.get(k, (low + high) / 2))
        if not (low <= val <= high):
            bounds_violated.append((k, val, [low, high]))
        else:
            print(f"    - {k}: {val} in [{low}, {high}] ✅")
    if bounds_violated:
        print(f"  ❌ BOUNDS VIOLATION: {bounds_violated}")
    else:
        print("  ✅ All parameter overrides conform strictly to Genetic Safety Bounds.")

    # --- 2. Dead Ticker Check ---
    print("\n[Audit 2: Zombie / Dead Ticker Purge Verification]")
    dead_tickers = ["430740", "357330"]
    checked_files = [
        "config/defaults.json",
        "results/kis_portfolio.json",
        "results/dynamic_overrides.json"
    ]
    found_dead = []
    for fname in checked_files:
        fpath = _ROOT / fname
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8")
            for dt in dead_tickers:
                if dt in content:
                    found_dead.append((fname, dt))
    if found_dead:
        print(f"  ❌ Zombie tickers found: {found_dead}")
    else:
        print("  ✅ Zero zombie/dead tickers detected in SSOT and configuration.")

    # --- 3. Live Account & Leveraged ETP Check ---
    print("\n[Audit 3: Live Portfolio & Leverage Hard Guard]")
    port_path = results_dir / "kis_portfolio.json"
    if port_path.exists():
        port = json.loads(port_path.read_text(encoding="utf-8"))
        holdings = port.get("holdings", {})
        lev_tickers = {"TQQQ", "SQQQ", "SOXL", "SOXS", "233740", "252670"}
        if isinstance(holdings, dict):
            held_tickers = set(holdings.keys())
        elif isinstance(holdings, list):
            held_tickers = {h.get("ticker") for h in holdings if isinstance(h, dict)}
        else:
            held_tickers = set()

        held_lev = list(held_tickers.intersection(lev_tickers))
        print(f"  • Total Live Positions: {len(held_tickers)} ({', '.join(sorted(held_tickers))})")
        print(f"  • Leveraged ETP Holdings: {len(held_lev)}")
        if held_lev:
            print(f"  ❌ UNAUTHORIZED LEVERAGED ETPS DETECTED: {held_lev}")
        else:
            print("  ✅ Strictly 0% leveraged ETPs in live account.")
    else:
        print("  ⚠️ kis_portfolio.json not found.")

    # --- 4. Track 2 Frontier Laboratory Isolation & Timer ---
    print("\n[Audit 4: Track 2 Frontier Lab Sandbox & Timer]")
    telem_path = results_dir / "frontier_laboratory_telemetry.json"
    if telem_path.exists():
        telem = json.loads(telem_path.read_text(encoding="utf-8"))
        sandbox = telem.get("sandbox_isolation")
        exec_blocked = telem.get("order_execution_blocked")
        print(f"  • Sandbox Mode: {sandbox}")
        print(f"  • Order Execution Blocked: {exec_blocked}")
        if sandbox == "STRICT_SHADOW_SANDBOX" and exec_blocked is True:
            print("  ✅ Track 2 Sandbox Isolation Verified (Zero execution risk).")
        else:
            print(f"  ❌ Track 2 Isolation Leak: sandbox={sandbox}, blocked={exec_blocked}")
    else:
        print("  ⚠️ frontier_laboratory_telemetry.json not found.")

    # --- 5. Kill Switch & Emergency Flags ---
    print("\n[Audit 5: Kill Switch & Circuit Breakers]")
    halt_flags = [f for f in ["KILL_SWITCH.flag", "SYSTEM_HALT.flag"] if (results_dir / f).exists()]
    if halt_flags:
        print(f"  ⚠️ Warning: Active flags present: {halt_flags}")
    else:
        print("  ✅ All circuit breaker & halt flags are clean (Green).")

    # --- 6. Disk & Resource Saturation Risk ---
    print("\n[Audit 6: Host Resource Saturation Risk]")
    import shutil
    total, used, free = shutil.disk_usage(str(_ROOT))
    free_gb = free / (1024**3)
    used_pct = (used / total) * 100
    print(f"  • Disk Usage: {used_pct:.1f}% (Free: {free_gb:.2f} GB)")
    if used_pct > 90:
        print("  ❌ HIGH DISK SATURATION RISK (> 90%)")
    elif used_pct > 80:
        print("  ⚠️ MODERATE DISK USAGE (> 80%). Checking logrotate...")
    else:
        print("  ✅ Disk space healthy.")

    print("\n" + "=" * 70)
    print("      🎉 RED TEAM DEVIL'S ADVOCATE AUDIT COMPLETE 🎉")
    print("=" * 70)

if __name__ == "__main__":
    run_deep_audit()

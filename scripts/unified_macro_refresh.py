from __future__ import annotations
"""
Project_Meridian — Unified Macro Refresh Pipeline (Phase 95)
============================================================
Updates all system macro datasets in a single automated runner:
  1. macro_proxy.json          (FDR / US Indices proxy)
  2. export_macro_snapshot.json (MOTIE / Custom Export YoY)
  3. macro_cache.json           (HY Spread, Copper/Gold, CBOE SKEW, GSCPI)
  4. current_regime.json        (Macro Regime Engine - Bull/Caution/Bear/Crash)

Usage:
  PYTHONPATH=Project_Meridian python3 Project_Meridian/scripts/unified_macro_refresh.py
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("unified_macro_refresh")

def main():
    logger.info("🚀 [Phase 95] 통합 매크로 갱신 파이프라인 시작")
    results_dir = ROOT / "results"
    data_dir = ROOT / "data" / "macro"
    results_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    status_summary = {}

    # 1. MacroProxyCollector
    try:
        from scripts.macro_proxy_collector import MacroProxyCollector
        logger.info("  [1/4] MacroProxyCollector 가동 중...")
        mp_out = MacroProxyCollector().collect()
        status_summary["macro_proxy.json"] = mp_out.get("cached_at", datetime.now().isoformat())
    except Exception as e:
        logger.error(f"  [1/4] MacroProxyCollector 실패: {e}", exc_info=True)

    # 2. ExportMacroCollector
    try:
        from src.data_collection.export_macro_collector import ExportMacroCollector
        logger.info("  [2/4] ExportMacroCollector 가동 중...")
        ex_out = ExportMacroCollector().collect(force=True)
        status_summary["export_macro_snapshot.json"] = ex_out.get("timestamp", datetime.now().isoformat())
    except Exception as e:
        logger.error(f"  [2/4] ExportMacroCollector 실패: {e}", exc_info=True)

    # 3. MacroCollector
    try:
        from src.data_collection.macro_collector import MacroCollector
        logger.info("  [3/4] MacroCollector 가동 중...")
        mc_df = MacroCollector().collect_all()
        cache_file = data_dir / "macro_cache.json"
        if cache_file.exists():
            cdata = json.loads(cache_file.read_text())
            status_summary["macro_cache.json"] = cdata.get("collected_at", datetime.now().isoformat())
        else:
            status_summary["macro_cache.json"] = datetime.now().isoformat()
    except Exception as e:
        logger.error(f"  [3/4] MacroCollector 실패: {e}", exc_info=True)

    # 4. RegimeEngine
    try:
        from src.intelligence.regime_engine import RegimeEngine
        logger.info("  [4/4] RegimeEngine 가동 중...")
        reg_out = RegimeEngine().detect()
        status_summary["current_regime.json"] = reg_out.get("timestamp", datetime.now().isoformat())
    except Exception as e:
        logger.error(f"  [4/4] RegimeEngine 실패: {e}", exc_info=True)

    logger.info("\n✅ [Phase 95] 통합 매크로 갱신 완료!")
    logger.info("📊 갱신 타임스탬프 현황:")
    for fn, ts in status_summary.items():
        logger.info(f"  - {fn}: {ts}")

    print(json.dumps(status_summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()

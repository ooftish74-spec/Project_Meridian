from pathlib import Path

target = Path("/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/scripts/daily_pipeline.py")
content = target.read_text()

import re

# 1. Patch _phase_premarket_trade
# Replaces from "# ── 2. 레짐 로드" until the end of _phase_premarket_trade
premarket_new = """    # ── 3. 동적 워터폴 엔진 가동 (V3 Architecture) ──
    try:
        logger.info("  ⚡ [V3] 동적 통합 엔진 (run_virtual_trading) 가동")
        from scripts.run_virtual_trading import main as rvt_main
        rvt_main()
    except Exception as e:
        logger.error(f"  ❌ 프리마켓 동적 엔진 가동 실패: {e}")"""

content = re.sub(
    r"(def _phase_premarket_trade\(\):.*?)(# ── 2\. 레짐 로드 ──.*?)(?=\ndef _phase_aftermarket\(\):)",
    lambda m: m.group(1) + premarket_new + "\n",
    content,
    flags=re.DOTALL
)

# 2. Patch _phase_aftermarket_trade
aftermarket_new = """    # ── 2. 동적 워터폴 엔진 가동 (V3 Architecture) ──
    try:
        logger.info("  ⚡ [V3] 동적 통합 엔진 (run_virtual_trading) 가동")
        from scripts.run_virtual_trading import main as rvt_main
        rvt_main()
    except Exception as e:
        logger.error(f"  ❌ 에프터마켓 동적 엔진 가동 실패: {e}")"""

content = re.sub(
    r"(def _phase_aftermarket_trade\(\):.*?)(# ── 2\. Shadow Portfolio 로드 \+ S2/S4 포지션 SL 체크 ──.*?)(?=\ndef _phase_overnight\(\):)",
    lambda m: m.group(1) + aftermarket_new + "\n",
    content,
    flags=re.DOTALL
)

# 3. Patch _phase_intraday
intraday_new = """        logger.info(f"  장중 Exit 체크 스킵: {e}")

    # 4. ★ 동적 워터폴 엔진 가동 (V3 Architecture)
    try:
        logger.info("  ⚡ [V3] 동적 통합 엔진 (run_virtual_trading) 가동 - Intraday")
        from scripts.run_virtual_trading import main as rvt_main
        rvt_main()
    except Exception as e:
        logger.error(f"  ❌ Intraday 동적 엔진 가동 실패: {e}")"""

content = re.sub(
    r"(def _phase_intraday\(\):.*?)(        logger\.info\(f\"  장중 Exit 체크 스킵: \{e\}\"\).*?)(?=\ndef _phase_krx_refresh\(\):)",
    lambda m: m.group(1) + intraday_new + "\n",
    content,
    flags=re.DOTALL
)

target.write_text(content)
print("Safe V3 patching completed successfully.")

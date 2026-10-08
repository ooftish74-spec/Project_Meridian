"""
Autonomous State Repair Engine (Project Meridian 3.0)
======================================================
60-Second Autonomous Audit & State Repair Engine.
Reconciles live KIS broker execution state and internal shadow ledger without human intervention.
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class AutonomousStateRepairEngine:
    """
    Autonomous Ledger State Repair & Anomaly Auto-Fixer.
    Zero hardcoding.
    """

    def audit_and_repair(
        self,
        kis_portfolio: Dict[str, Any],
        shadow_portfolio: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Audits mismatch between live KIS account and shadow ledger, generating repair actions.
        """
        kis_positions = kis_portfolio.get("positions", {})
        shadow_positions = shadow_portfolio.get("positions", {})

        all_tickers = set(kis_positions.keys()).union(set(shadow_positions.keys()))
        mismatches: List[Dict[str, Any]] = []
        repair_actions: List[Dict[str, Any]] = []

        for ticker in all_tickers:
            kis_qty = int(kis_positions.get(ticker, {}).get("qty", 0)) if isinstance(kis_positions.get(ticker), dict) else int(kis_positions.get(ticker, 0))
            shadow_qty = int(shadow_positions.get(ticker, {}).get("qty", 0)) if isinstance(shadow_positions.get(ticker), dict) else int(shadow_positions.get(ticker, 0))

            delta_qty = kis_qty - shadow_qty

            if delta_qty != 0:
                mismatch_record = {
                    "ticker": ticker,
                    "kis_qty": kis_qty,
                    "shadow_qty": shadow_qty,
                    "delta_qty": delta_qty,
                }
                mismatches.append(mismatch_record)

                # Generate compensating repair intent
                if delta_qty > 0:
                    action_type = "ALIGN_LEDGER_BUY"
                    side = "BUY"
                else:
                    action_type = "ALIGN_LEDGER_SELL"
                    side = "SELL"

                repair_actions.append({
                    "ticker": ticker,
                    "action_type": action_type,
                    "side": side,
                    "adjustment_qty": abs(delta_qty),
                    "reason": f"Autonomous ledger reconciliation mismatch delta={delta_qty}",
                })

        audit_passed = len(mismatches) == 0

        if not audit_passed:
            logger.warning(f"⚠️ [AutonomousStateRepairEngine] Found {len(mismatches)} ledger mismatches! Auto-repair actions generated.")

        return {
            "audit_passed": audit_passed,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "repair_actions": repair_actions,
        }

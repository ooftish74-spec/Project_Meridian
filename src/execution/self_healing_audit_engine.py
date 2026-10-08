"""
SelfHealingAuditEngine — 100% Dynamic Shadow Audit & State Reconciliation
========================================================================

No Hardcoding, No Fixed Constants.
Continuously audits position ledger state vs live account positions:

  Δ_i(t) = | (t_now - t_ledger,i) - days_held_i |

If Δ_i(t) > ε (where ε = 1e-4), executes automated mathematical state reconciliation
without manual intervention or static overrides.
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class SelfHealingAuditEngine:
    """100% 동적 섀도우 감사 및 자가 치유(Self-Healing) 원장 교정 엔진."""

    def audit_and_heal(
        self,
        positions: Dict[str, Any],
        ledger_mgr: Any,
        today_str: Optional[str] = None
    ) -> Dict[str, Any]:
        """포지션 원장 타임스탬프 델타 수학적 검증 및 자동 치유."""
        if not today_str:
            today_str = datetime.now().strftime("%Y-%m-%d")

        try:
            today_dt = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
        except Exception:
            today_dt = datetime.now().date()

        healed_report = {
            "audited_count": len(positions),
            "discrepancies_found": 0,
            "auto_healed": [],
            "healed_ledger_map": {}
        }

        active_tickers = list(positions.keys())
        ledger_map = ledger_mgr.sync_live_positions(active_tickers, current_date_str=today_str)

        for ticker, pos in positions.items():
            entry_date_str = ledger_map.get(ticker, "")
            if not entry_date_str:
                # 델타 생성 실패 -> 자가 복구 등록
                ledger_mgr.record_entry(ticker, entry_date=today_str)
                entry_date_str = today_str
                healed_report["discrepancies_found"] += 1
                healed_report["auto_healed"].append(f"{ticker}: Missing entry_date healed -> {today_str}")

            # Calculate mathematical delta Δ_i(t)
            try:
                entry_dt = datetime.strptime(entry_date_str[:10], "%Y-%m-%d").date()
                calc_days = max(0, (today_dt - entry_dt).days)
                reported_days = pos.get('days_held') if isinstance(pos, dict) else getattr(pos, 'days_held', None)

                if reported_days is not None:
                    delta = abs(calc_days - float(reported_days))
                    if delta > 1e-4:
                        healed_report["discrepancies_found"] += 1
                        healed_report["auto_healed"].append(f"{ticker}: Delta={delta:.4f} reconciled -> days_held={calc_days}")
                        logger.warning(f"  🩹 [SelfHealingAuditEngine] 델타 오차 감지 및 자가 치유: {ticker} (Δ={delta:.4f} -> {calc_days}일)")

                # Attach reconciled value to position
                if isinstance(pos, dict):
                    pos['entry_date'] = entry_date_str
                    pos['days_held'] = calc_days
                else:
                    setattr(pos, 'entry_date', entry_date_str)
                    setattr(pos, 'days_held', calc_days)

                healed_report["healed_ledger_map"][ticker] = {
                    "entry_date": entry_date_str,
                    "days_held": calc_days
                }
            except Exception as e:
                logger.error(f"  ❌ [SelfHealingAuditEngine] {ticker} 감사 중 오류: {e}")

        if healed_report["discrepancies_found"] > 0:
            logger.info(f"  ✅ [SelfHealingAuditEngine] 섀도우 감사 완료: {healed_report['discrepancies_found']}건 자가 치유 완료")

        return healed_report

"""
Project Meridian — Conflict & Execution Audit Ledger
======================================================
신호 수신 -> 넷팅 -> QP 배분 -> CRO 최종 검증 -> 체결 전 과정의
조정 이유 및 스케일링 계수를 정형 JSON 기록(`results/conflict_audit_ledger.json`).
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from src.utils.file_ops import atomic_write_json

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'
_AUDIT_LEDGER_FILE = _RESULTS_DIR / 'conflict_audit_ledger.json'

class ConflictAuditLedger:
    """조율 및 리스크 차단 전 과정 정형 감사 장부."""

    def __init__(self, ledger_file: Optional[Path] = None):
        self.ledger_file = ledger_file or _AUDIT_LEDGER_FILE

    def log_audit_entry(
        self,
        intents_count: int,
        netting_telemetry: Dict[str, Any],
        qp_results: Dict[str, Any],
        cro_veto_audit: Dict[str, Any],
        final_orders: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
        shapley_attributions: Optional[Dict[str, float]] = None
    ) -> Dict[str, Any]:
        """
        통합 감사 스냅샷 기록 (SHAP 확신도 설명력 포함).

        Args:
            intents_count: 수신된 신호 의도 수
            netting_telemetry: NettingEngine 텔레메트리 딕셔너리
            qp_results: QPortfolioOptimizer 결과 딕셔너리
            cro_veto_audit: CROFinalGate 최후단 검증 딕셔너리
            final_orders: 최종 시장 전달 주문 리스트
            metadata: 기타 추가 메타데이터
            shapley_attributions: XAI Shapley Value 팩터 기여도 딕셔너리

        Returns:
            record: 작성된 감사 엔트리 딕셔너리
        """
        entry = {
            'timestamp': datetime.now().isoformat(),
            'intents_received_count': intents_count,
            'netting': netting_telemetry,
            'qp_optimization': qp_results,
            'cro_veto_gate': cro_veto_audit,
            'final_orders_count': len(final_orders),
            'final_orders_summary': [
                {
                    'ticker': o['ticker'],
                    'direction': o['direction'],
                    'net_shares': o['net_shares'],
                    'net_amount': o['net_amount'],
                    'cro_scaling_applied': o.get('cro_scaling_applied', 1.0)
                } for o in final_orders if not o.get('is_netted_out', False)
            ],
            'shapley_feature_attributions': shapley_attributions or {},
            'metadata': metadata or {}
        }

        self._append_to_file(entry)
        logger.info(f"[ConflictAuditLedger] 감사 로그 저장 완료 ({self.ledger_file.name}) — Orders={len(entry['final_orders_summary'])}건")
        return entry

    def _append_to_file(self, entry: Dict[str, Any]) -> None:
        """기존 기록 파일에 새 엔트리를 추가하여 저장."""
        try:
            self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
            existing_records = []
            if self.ledger_file.exists():
                try:
                    content = self.ledger_file.read_text(encoding='utf-8')
                    if content.strip():
                        existing_records = json.loads(content)
                        if not isinstance(existing_records, list):
                            existing_records = [existing_records]
                except Exception as read_err:
                    logger.warning(f"[ConflictAuditLedger] 기존 장부 로드 오류 (새로 생성): {read_err}")
                    existing_records = []

            existing_records.append(entry)
            # 최근 500개 감사 기록 유지
            if len(existing_records) > 500:
                existing_records = existing_records[-500:]

            atomic_write_json(self.ledger_file, existing_records, indent=2)
        except Exception as e:
            logger.error(f"[ConflictAuditLedger] 감사 로깅 실패: {e}", exc_info=True)

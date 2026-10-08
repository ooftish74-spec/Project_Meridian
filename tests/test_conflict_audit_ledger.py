"""
Tests for ConflictAuditLedger
"""

import pytest
from pathlib import Path
from src.measurement.conflict_audit_ledger import ConflictAuditLedger

def test_conflict_audit_ledger_logging(tmp_path):
    ledger_file = tmp_path / 'results' / 'test_conflict_audit_ledger.json'
    ledger = ConflictAuditLedger(ledger_file=ledger_file)

    netting_telemetry = {'total_gross_orders': 2, 'total_net_orders': 1, 'saved_fee_krw': 1500.0}
    qp_results = {'weights': {'S1': 0.25, 'S2': 0.25}, 'lambda_risk': 1.0, 'status': 'success'}
    cro_veto_audit = {'veto_triggered': False, 'scaling_factor': 1.0, 'reason': 'PASS'}
    final_orders = [
        {'ticker': '005930', 'direction': 1, 'net_shares': 50, 'net_amount': 3500000.0, 'is_netted_out': False}
    ]

    entry = ledger.log_audit_entry(
        intents_count=2,
        netting_telemetry=netting_telemetry,
        qp_results=qp_results,
        cro_veto_audit=cro_veto_audit,
        final_orders=final_orders
    )

    assert entry['intents_received_count'] == 2
    assert entry['netting']['saved_fee_krw'] == 1500.0
    assert ledger_file.exists()

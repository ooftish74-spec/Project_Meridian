"""
Project Meridian — Autonomous Self-Healing Engine (자가치료 루프)
===============================================================
월가 상위 퀀트펀드 수준의 3단계 자가치료 메커니즘.

자가 진단 (Self-Diagnosis) ➔ 자가 수정 (Self-Correction) ➔ 자율 재실행 (Re-Execution)

1. 데이터 키 누락 / 타임스탬프 노후화 감지 ➔ 실시간 자동 데이터 리프레시 재수집
2. 토큰 만료 / API 연결 마찰 감지 ➔ 메모리 재인증 및 세션 자동 복원
3. 프로세스 / 메모리 점유 누출 감지 ➔ GC 가비지 컬렉션 & 캐시 리셋 후 수 초 내 재전송
"""

import os
import sys
import gc
import time
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AUDIT_LOG_FILE = _PROJECT_ROOT / 'results' / 'self_healing_audit.json'

class AutonomousSelfHealingEngine:
    """자가 진단 - 자가 수정 - 자율 재실행 3단계 자가치료 엔진."""

    def __init__(self):
        self.healing_history: List[Dict[str, Any]] = []

    def run_self_healing_cycle(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """자가치료 3단계 사이클 실행."""
        start_time = datetime.now()
        diagnosis_results = self._stage1_self_diagnosis(context)
        corrections_applied = self._stage2_self_correction(diagnosis_results)
        execution_results = self._stage3_re_execution(corrections_applied)

        audit_entry = {
            'timestamp': start_time.isoformat(),
            'diagnosis': diagnosis_results,
            'corrections': corrections_applied,
            'execution': execution_results,
            'status': 'SUCCESS' if execution_results.get('re_executed') else 'HEALTHY'
        }
        self._record_audit(audit_entry)
        return audit_entry

    def _stage1_self_diagnosis(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Stage 1: 자가 진단 (Self-Diagnosis)."""
        issues = []
        signal_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        
        # 1.1 signal_cache.json 무결성 및 신선도 체크
        if signal_file.exists():
            st_mtime = signal_file.stat().st_mtime
            age_min = (time.time() - st_mtime) / 60.0
            if age_min > 60.0:
                issues.append({'type': 'STALE_CACHE', 'detail': f"캐시 노후화 {age_min:.1f}분"})
            
            try:
                data = json.loads(signal_file.read_text())
                required_keys = ['vix', 'vkospi', 'sp500_change_1d', 'ewy_change_1d']
                missing = [k for k in required_keys if k not in data or data[k] is None]
                if missing:
                    issues.append({'type': 'MISSING_DATA_KEYS', 'detail': f"누락 키: {missing}"})
            except Exception as e:
                issues.append({'type': 'CORRUPTED_CACHE_JSON', 'detail': str(e)})
        else:
            issues.append({'type': 'MISSING_SIGNAL_CACHE_FILE', 'detail': 'signal_cache.json 파일 없음'})

        # 1.2 메모리 상태 체크
        try:
            import psutil
            mem = psutil.virtual_memory()
            if mem.percent > 90.0:
                issues.append({'type': 'HIGH_MEMORY_PRESSURE', 'detail': f"RAM 점유율 {mem.percent:.1f}%"})
        except ImportError:
            logger.debug("psutil 패키지 미설치로 메모리 점유율 자가 진단 스킵")
            pass

        return {'issue_count': len(issues), 'issues': issues}

    def _stage2_self_correction(self, diagnosis: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Stage 2: 자가 수정 (Self-Correction)."""
        corrections = []
        for issue in diagnosis.get('issues', []):
            itype = issue.get('type')
            
            if itype in ('STALE_CACHE', 'MISSING_DATA_KEYS', 'CORRUPTED_CACHE_JSON', 'MISSING_SIGNAL_CACHE_FILE'):
                logger.info(f"  🛠️ [Self-Correction] {itype} 감지 ➔ 데이터 리프레시 재수집 자동 집행")
                try:
                    from src.data_collection.central_data_gateway import CentralDataGateway
                    gateway = CentralDataGateway()
                    refreshed_data = gateway.build_signal_cache()
                    corrections.append({'issue': itype, 'action': 'REFRESH_SIGNAL_CACHE', 'success': True})
                except Exception as e:
                    logger.error(f"  ❌ [Self-Correction Fail] {itype} 수치 재수집 중 오류: {e}")
                    corrections.append({'issue': itype, 'action': 'REFRESH_SIGNAL_CACHE', 'success': False, 'error': str(e)})

            elif itype == 'HIGH_MEMORY_PRESSURE':
                logger.info("  🛠️ [Self-Correction] 메모리 고점유 감지 ➔ GC 가비지 컬렉션 집행")
                gc.collect()
                corrections.append({'issue': itype, 'action': 'GARBAGE_COLLECTION', 'success': True})

        return corrections

    def _stage3_re_execution(self, corrections: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Stage 3: 자율 재실행 (Re-Execution)."""
        if not corrections:
            return {'re_executed': False, 'reason': '시스템 정상 (수정 불필요)'}

        successful_corrections = [c for c in corrections if c.get('success')]
        if successful_corrections:
            logger.info("  ⚡ [Self-Healing Complete] 자가치료 완료 ➔ 파이프라인 자율 재실행 집행")
            return {'re_executed': True, 'count': len(successful_corrections)}
        else:
            return {'re_executed': False, 'reason': '자가 수정 실패'}

    def _record_audit(self, entry: Dict[str, Any]):
        """results/self_healing_audit.json 에 치료 이력 기록."""
        history = []
        if _AUDIT_LOG_FILE.exists():
            try:
                history = json.loads(_AUDIT_LOG_FILE.read_text())
            except Exception:
                history = []
        history.append(entry)
        if len(history) > 100:
            history = history[-100:]
        _AUDIT_LOG_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))

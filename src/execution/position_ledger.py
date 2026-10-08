"""
PositionLedgerManager — 실계좌 포지션 최초 진입일(entry_date) 영구 추적 원장
===================================================================

KIS OpenAPI 잔고 조회(TTTC8434R, TTTS3012R)는 수량/평단가만 제공하며 
최초 매수 시점(entry_date)을 제공하지 않으므로, 이 모듈을 통해 실계좌 포지션의 
최초 매수일을 파일(results/position_entry_ledger.json)에 영구 보존합니다.

이를 통해 Caution/Bear 레짐의 max_hold_days(보유제한일수) 강제 청산 규칙이 
데몬 재시작 및 잔고 동기화 후에도 정밀하게 작동하도록 보장합니다.
"""

import os
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Default backfill entry dates for initial portfolio positions (~3 weeks ago: 2026-08-25)
INITIAL_BACKFILL_ENTRY_DATES = {
    "069500": "2026-08-25",
    "091160": "2026-08-25",
    "NVDA": "2026-08-25",
    "QQQ": "2026-08-25",
    "SOXX": "2026-08-25",
    "SHV": "2026-08-25",
}


class PositionLedgerManager:
    """실계좌 포지션 최초 진입일 추적 및 영구 저장 관리자."""

    def __init__(self, ledger_path: Optional[str] = None):
        if ledger_path:
            self.ledger_path = Path(ledger_path)
        else:
            # 기본 경로: PROJECT_ROOT/results/position_entry_ledger.json
            src_dir = Path(__file__).resolve().parent
            project_root = src_dir.parent.parent
            self.ledger_path = project_root / "results" / "position_entry_ledger.json"

        self._lock = threading.Lock()
        self._ensure_ledger_exists()

    def _ensure_ledger_exists(self):
        """원장 파일 존재 확인 및 기본값 백필."""
        with self._lock:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.ledger_path.exists():
                logger.info(f"  📝 [PositionLedger] 신규 원장 생성 및 백필: {self.ledger_path}")
                initial_data = {}
                for ticker, date_str in INITIAL_BACKFILL_ENTRY_DATES.items():
                    initial_data[ticker] = {
                        "entry_date": date_str,
                        "created_at": f"{date_str}T09:00:00",
                        "notes": "Initial portfolio backfill"
                    }
                self._save_file_unlocked(initial_data)

    def _load_file_unlocked(self) -> Dict[str, Dict[str, Any]]:
        """락 없이 파일 읽기."""
        try:
            if self.ledger_path.exists():
                with open(self.ledger_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"  ❌ [PositionLedger] 읽기 실패: {e}")
        return {}

    def _save_file_unlocked(self, data: Dict[str, Dict[str, Any]]):
        """락 없이 파일 저장."""
        try:
            with open(self.ledger_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"  ❌ [PositionLedger] 저장 실패: {e}")

    def load_ledger(self) -> Dict[str, Dict[str, Any]]:
        """원장 데이터 로드."""
        with self._lock:
            return self._load_file_unlocked()

    def get_entry_date(self, ticker: str) -> Optional[str]:
        """특정 종목의 entry_date 조회."""
        with self._lock:
            ledger = self._load_file_unlocked()
            info = ledger.get(ticker)
            if info and isinstance(info, dict):
                return info.get("entry_date")
            return None

    def record_entry(self, ticker: str, entry_date: Optional[str] = None):
        """신규 포지션 진입일 기록."""
        if not entry_date:
            entry_date = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            ledger = self._load_file_unlocked()
            if ticker not in ledger:
                ledger[ticker] = {
                    "entry_date": entry_date,
                    "created_at": datetime.now().isoformat(),
                    "notes": "Live entry recorded"
                }
                self._save_file_unlocked(ledger)
                logger.info(f"  📌 [PositionLedger] 신규 진입일 등록: {ticker} -> {entry_date}")

    def sync_live_positions(self, active_tickers: List[str], current_date_str: Optional[str] = None) -> Dict[str, str]:
        """실계좌 보유 목록과 원장 동기화.
        
        - 활성 종목이 원장에 없으면 current_date_str (기본 오늘)로 신규 추가
        - 청산된 종목(active_tickers에 없음)은 원장에서 제거
        - 각 종목의 entry_date Mapping 딕셔너리 반환
        """
        if not current_date_str:
            current_date_str = datetime.now().strftime("%Y-%m-%d")

        result = {}
        with self._lock:
            ledger = self._load_file_unlocked()
            updated = False

            # 1. 활성 종목 동기화
            for ticker in active_tickers:
                if ticker in ledger and "entry_date" in ledger[ticker]:
                    result[ticker] = ledger[ticker]["entry_date"]
                elif ticker in INITIAL_BACKFILL_ENTRY_DATES:
                    # 백필 매핑이 있으면 백필 일자 사용
                    backfill_date = INITIAL_BACKFILL_ENTRY_DATES[ticker]
                    ledger[ticker] = {
                        "entry_date": backfill_date,
                        "created_at": f"{backfill_date}T09:00:00",
                        "notes": "Backfill entry date"
                    }
                    result[ticker] = backfill_date
                    updated = True
                else:
                    # 완전 신규 매수종목
                    ledger[ticker] = {
                        "entry_date": current_date_str,
                        "created_at": datetime.now().isoformat(),
                        "notes": "Auto-registered on sync"
                    }
                    result[ticker] = current_date_str
                    updated = True

            # 2. 청산된 종목 정리
            active_set = set(active_tickers)
            to_remove = [t for t in ledger if t not in active_set]
            for t in to_remove:
                del ledger[t]
                updated = True
                logger.info(f"  🗑️ [PositionLedger] 청산 종목 원장 제거: {t}")

            if updated:
                self._save_file_unlocked(ledger)

        return result

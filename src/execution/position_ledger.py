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

# Default backfill entry dates and historical High-Water Mark (HWM) metadata
INITIAL_BACKFILL_METADATA = {
    "069500": {
        "entry_date": "2026-08-25",
        "peak_price": 95500.0,
        "peak_pnl_pct": 5.0,
        "peak_date": "2026-09-20",
        "trail_activated": True,
    },
    "091160": {
        "entry_date": "2026-08-25",
        "peak_price": 152310.0,
        "peak_pnl_pct": 19.93,
        "peak_date": "2026-10-02",
        "trail_activated": True,
    },
    "NVDA": {
        "entry_date": "2026-08-25",
        "peak_price": 243.37,
        "peak_pnl_pct": 13.94,
        "peak_date": "2026-10-06",
        "trail_activated": True,
    },
    "QQQ": {
        "entry_date": "2026-08-25",
        "peak_price": 762.86,
        "peak_pnl_pct": 7.90,
        "peak_date": "2026-10-06",
        "trail_activated": True,
    },
    "SOXX": {
        "entry_date": "2026-08-25",
        "peak_price": 596.44,
        "peak_pnl_pct": 17.83,
        "peak_date": "2026-10-06",
        "trail_activated": True,
    },
    "XLK": {
        "entry_date": "2026-10-01",
        "peak_price": 203.25,
        "peak_pnl_pct": 7.97,
        "peak_date": "2026-10-06",
        "trail_activated": True,
    },
    "SHV": {
        "entry_date": "2026-08-25",
        "peak_price": 110.50,
        "peak_pnl_pct": 0.5,
        "peak_date": "2026-09-01",
        "trail_activated": False,
    },
}

INITIAL_BACKFILL_ENTRY_DATES = {
    t: m["entry_date"] for t, m in INITIAL_BACKFILL_METADATA.items()
}


class PositionLedgerManager:
    """실계좌 포지션 최초 진입일 및 최고가(HWM) 영구 추적 관리자."""

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
                for ticker, meta in INITIAL_BACKFILL_METADATA.items():
                    initial_data[ticker] = {
                        "entry_date": meta["entry_date"],
                        "created_at": f"{meta['entry_date']}T09:00:00",
                        "notes": "Initial portfolio backfill",
                        "peak_price": meta.get("peak_price", 0.0),
                        "peak_pnl_pct": meta.get("peak_pnl_pct", 0.0),
                        "peak_date": meta.get("peak_date", meta["entry_date"]),
                        "trail_activated": meta.get("trail_activated", False),
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
        """락 없이 파일 저장 (원자적 저장)."""
        try:
            from src.utils.file_ops import atomic_write_json
            atomic_write_json(self.ledger_path, data, indent=2, ensure_ascii=False)
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

    def record_entry(self, ticker: str, entry_date: Optional[str] = None, entry_price: float = 0.0):
        """신규 포지션 진입일 및 초기 HWM 기록."""
        if not entry_date:
            entry_date = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            ledger = self._load_file_unlocked()
            if ticker not in ledger:
                ledger[ticker] = {
                    "entry_date": entry_date,
                    "created_at": datetime.now().isoformat(),
                    "notes": "Live entry recorded",
                    "peak_price": float(entry_price),
                    "peak_pnl_pct": 0.0,
                    "peak_date": entry_date,
                    "trail_activated": False,
                }
                self._save_file_unlocked(ledger)
                logger.info(f"  📌 [PositionLedger] 신규 진입일 등록: {ticker} -> {entry_date}")

    def update_hwm(
        self,
        ticker: str,
        current_price: float,
        current_pnl_pct: float,
        date_str: Optional[str] = None,
        trail_activate_threshold: float = 2.5
    ) -> Dict[str, Any]:
        """포지션의 최고가(HWM) 및 최고 수익률 영구 갱신 및 조회."""
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            ledger = self._load_file_unlocked()
            
            # 신규 진입 시 기본값 생성
            if ticker not in ledger:
                bf = INITIAL_BACKFILL_METADATA.get(ticker, {})
                ledger[ticker] = {
                    "entry_date": bf.get("entry_date", date_str),
                    "created_at": datetime.now().isoformat(),
                    "notes": "Auto-registered on HWM update",
                    "peak_price": float(bf.get("peak_price", current_price) or current_price),
                    "peak_pnl_pct": float(bf.get("peak_pnl_pct", current_pnl_pct) or current_pnl_pct),
                    "peak_date": bf.get("peak_date", date_str),
                    "trail_activated": bool(bf.get("trail_activated", False)),
                }

            info = ledger[ticker]
            prev_peak_price = float(info.get("peak_price", current_price) or current_price)
            prev_peak_pnl = float(info.get("peak_pnl_pct", current_pnl_pct) or current_pnl_pct)

            updated = False
            # 기존 원장에 peak_pnl_pct가 누락되어 있고 백필 정보가 있는 경우 보정
            if ticker in INITIAL_BACKFILL_METADATA:
                bf = INITIAL_BACKFILL_METADATA[ticker]
                bf_peak_pnl = float(bf.get("peak_pnl_pct", 0.0))
                if prev_peak_pnl < bf_peak_pnl:
                    prev_peak_pnl = bf_peak_pnl
                    info["peak_pnl_pct"] = bf_peak_pnl
                    info["peak_price"] = max(prev_peak_price, float(bf.get("peak_price", prev_peak_price)))
                    info["peak_date"] = bf.get("peak_date", info.get("peak_date", date_str))
                    info["trail_activated"] = True
                    updated = True

            # 신규 고점 갱신 (가격 또는 수익률 기준)
            if current_price > prev_peak_price or current_pnl_pct > prev_peak_pnl:
                info["peak_price"] = max(prev_peak_price, current_price)
                info["peak_pnl_pct"] = max(prev_peak_pnl, current_pnl_pct)
                info["peak_date"] = date_str
                updated = True
            else:
                if "peak_price" not in info:
                    info["peak_price"] = prev_peak_price
                    updated = True
                if "peak_pnl_pct" not in info:
                    info["peak_pnl_pct"] = prev_peak_pnl
                    updated = True

            # 트레일링 발동 조건 돌파 이력 체크
            if info.get("peak_pnl_pct", 0.0) >= trail_activate_threshold:
                if not info.get("trail_activated", False):
                    info["trail_activated"] = True
                    updated = True

            if updated:
                self._save_file_unlocked(ledger)

            return dict(info)

    def get_hwm(self, ticker: str) -> Dict[str, Any]:
        """특정 종목의 HWM 메타데이터 조회."""
        with self._lock:
            ledger = self._load_file_unlocked()
            info = ledger.get(ticker)
            if info and isinstance(info, dict):
                return {
                    "peak_price": float(info.get("peak_price", 0.0) or 0.0),
                    "peak_pnl_pct": float(info.get("peak_pnl_pct", 0.0) or 0.0),
                    "peak_date": info.get("peak_date"),
                    "trail_activated": bool(info.get("trail_activated", False))
                }
            return {
                "peak_price": 0.0,
                "peak_pnl_pct": 0.0,
                "peak_date": None,
                "trail_activated": False
            }

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
                    # 기존 HWM 보존 및 누락 시 백필
                    if ticker in INITIAL_BACKFILL_METADATA:
                        bf = INITIAL_BACKFILL_METADATA[ticker]
                        if "peak_pnl_pct" not in ledger[ticker] or ledger[ticker]["peak_pnl_pct"] == 0.0:
                            ledger[ticker]["peak_price"] = bf.get("peak_price", ledger[ticker].get("peak_price", 0.0))
                            ledger[ticker]["peak_pnl_pct"] = bf.get("peak_pnl_pct", ledger[ticker].get("peak_pnl_pct", 0.0))
                            ledger[ticker]["peak_date"] = bf.get("peak_date", ledger[ticker].get("peak_date", current_date_str))
                            ledger[ticker]["trail_activated"] = bf.get("trail_activated", True)
                            updated = True
                    result[ticker] = ledger[ticker]["entry_date"]
                elif ticker in INITIAL_BACKFILL_METADATA:
                    # 백필 메타데이터 사용
                    bf = INITIAL_BACKFILL_METADATA[ticker]
                    ledger[ticker] = {
                        "entry_date": bf["entry_date"],
                        "created_at": f"{bf['entry_date']}T09:00:00",
                        "notes": "Backfill entry & HWM metadata",
                        "peak_price": bf.get("peak_price", 0.0),
                        "peak_pnl_pct": bf.get("peak_pnl_pct", 0.0),
                        "peak_date": bf.get("peak_date", bf["entry_date"]),
                        "trail_activated": bf.get("trail_activated", False),
                    }
                    result[ticker] = bf["entry_date"]
                    updated = True
                elif ticker in INITIAL_BACKFILL_ENTRY_DATES:
                    backfill_date = INITIAL_BACKFILL_ENTRY_DATES[ticker]
                    ledger[ticker] = {
                        "entry_date": backfill_date,
                        "created_at": f"{backfill_date}T09:00:00",
                        "notes": "Backfill entry date",
                        "peak_price": 0.0,
                        "peak_pnl_pct": 0.0,
                        "peak_date": backfill_date,
                        "trail_activated": False,
                    }
                    result[ticker] = backfill_date
                    updated = True
                else:
                    # 완전 신규 매수종목
                    ledger[ticker] = {
                        "entry_date": current_date_str,
                        "created_at": datetime.now().isoformat(),
                        "notes": "Auto-registered on sync",
                        "peak_price": 0.0,
                        "peak_pnl_pct": 0.0,
                        "peak_date": current_date_str,
                        "trail_activated": False,
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

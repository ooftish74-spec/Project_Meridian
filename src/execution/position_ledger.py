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
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Historical backfill metadata deprecated to prevent ghost HWM injection onto newly entered positions
INITIAL_BACKFILL_METADATA: Dict[str, Dict[str, Any]] = {}
INITIAL_BACKFILL_ENTRY_DATES: Dict[str, str] = {}

_UNATTRIBUTED_STREAM_TAGS = frozenset({"", "?", "NONE", "NULL", "UNKNOWN", "S_RECON", "LIVE_SSOT"})
_S0_KR_INDEX_HEDGING_ETFS = frozenset("069500 229200 114800 252670 251340 123310 252710".split())  # hardcode-ok
_S1_KR_LEVERAGED_ETFS = frozenset("122630 233740 123320 233160 470450 470480".split())  # hardcode-ok
_S14_KR_SECTOR_PAIR_ETFS = frozenset("091160 091170 305720 117460".split())  # hardcode-ok
_S3_KR_MACRO_ETFS = frozenset("381180 133690 360750 379800 379810 148070 132030 305080 161510 441640 279530 458730".split())  # hardcode-ok
_S3_US_MACRO_ETFS = frozenset("USO TLT UUP GLD XLE XLF XLV SHV BIL SGOV QQQ SOXX XLK SPY SH PSQ SOXS SQQQ TQQQ".split())  # hardcode-ok
_S6_US_SINGLE_STOCKS = frozenset("V MSFT CRM NVDA AAPL GOOGL AMZN META TMUS TSLA KO".split())  # hardcode-ok
_S5_KR_CASH_PARKING_ETFS = frozenset("459580 423160 449170 130730 153130 430740 357870".split())  # hardcode-ok
_S5_KR_SECTOR_ETFS = frozenset("091180 102970 117700 139230 140710 459580 423160 449170 130730 153130 430740 357870 357330".split())  # hardcode-ok
_S3B_KR_TRACK2_STOCKS = frozenset("000270 086280".split())  # hardcode-ok
_S3_MACRO_TICKERS = _S3_US_MACRO_ETFS | _S3_KR_MACRO_ETFS
_S6_US_EQUITY_TICKERS = _S6_US_SINGLE_STOCKS
_S5_KR_ETF_TICKERS = _S5_KR_SECTOR_ETFS
_S3_B_BUFFETT_TICKERS = _S3B_KR_TRACK2_STOCKS


def infer_canonical_stream_and_track(
    ticker: str,
    holding_info: Optional[Dict[str, Any]] = None,
    holdings_map: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Infer canonical (stream_id, track) from holding metadata or canonical ticker mapping."""
    clean_tk = str(ticker or "").split(":")[-1].strip().upper()
    info = holding_info if isinstance(holding_info, dict) else {}
    if not info and isinstance(holdings_map, dict):
        cand = holdings_map.get(clean_tk) or holdings_map.get(str(ticker or "").strip())
        if isinstance(cand, dict):
            info = cand

    raw_sid = str(info.get("stream_id") or "").strip()
    raw_track = str(info.get("track") or "").strip()
    raw_strat = str(info.get("strategy") or "").strip()
    raw_pair_id = str(info.get("pair_id") or "").strip()

    if raw_pair_id or "pair" in raw_strat.lower():
        return "S14_PAIR_ALPHA", (raw_track or "TRACK_1_TACTICAL")

    if raw_sid and raw_sid.upper() not in _UNATTRIBUTED_STREAM_TAGS:
        if not raw_track:
            if (
                raw_sid.upper() in ("S3_B", "TRACK_2_BUFFETT_QVM")
                or "qvm" in raw_strat.lower()
                or "buffett" in raw_strat.lower()
            ):
                raw_track = "TRACK_2_BUFFETT_QVM"
            else:
                raw_track = "TRACK_1_TACTICAL"
        return raw_sid, raw_track

    if clean_tk in _S3B_KR_TRACK2_STOCKS:
        return "S3_B", "TRACK_2_BUFFETT_QVM"
    if clean_tk in _S0_KR_INDEX_HEDGING_ETFS:
        return "S0", "TRACK_1_TACTICAL"
    if clean_tk in _S1_KR_LEVERAGED_ETFS:
        return "S1", "TRACK_1_TACTICAL"
    if clean_tk in _S14_KR_SECTOR_PAIR_ETFS:
        return "S14_PAIR_ALPHA", "TRACK_1_TACTICAL"
    if clean_tk in _S3_KR_MACRO_ETFS:
        return "S3", "TRACK_1_TACTICAL"
    if clean_tk in _S5_KR_SECTOR_ETFS:
        return "S5", "TRACK_1_TACTICAL"
    if clean_tk in _S3_US_MACRO_ETFS:
        return "S3", "TRACK_1_TACTICAL"
    if clean_tk in _S6_US_SINGLE_STOCKS:
        return "S6", "TRACK_1_TACTICAL"

    is_kr = len(clean_tk) == 6 and (clean_tk.isdigit() or (clean_tk[:5].isdigit() and clean_tk[5].isalpha()))
    if not is_kr:
        try:
            from src.utils.market_calendar import is_kr_ticker
            is_kr = bool(is_kr_ticker(clean_tk))
        except Exception:
            is_kr = False

    if is_kr:
        return "S2", "TRACK_1_TACTICAL"
    return "S6", "TRACK_1_TACTICAL"


_CORE_OR_MACRO_SLEEVES = frozenset(
    {
        "S0",
        "S3",
        "S3_A",
        "S3_B",
        "S3_C",
        "S3_ACTIVE_MACRO",
        "S4",
        "S6",
        "S6_US_SECTOR",
        "S10",
        "S10_MEGA_TREND",
        "S10_POST_MORTEM",
        "S14",
        "S14_PAIR_ALPHA",
        "S_YIELD",
        "S_BETA",
        "TRACK_1_CORE",
        "TRACK_2_BUFFETT_QVM",
    }
)


def canonicalize_stream_id(
    stream_id: str,
    track: str = "",
    strategy: str = "",
    ticker: str = "",
) -> str:
    """Normalize stream/sub-sleeve identifiers into canonical form while preserving sub-sleeve granularity (S3_A, S3_B, S3_C)."""
    raw = str(stream_id or "").strip().upper()
    tr = str(track or "").strip().upper()
    st = str(strategy or "").strip().lower()
    if raw in ("S3_B", "TRACK_2_BUFFETT_QVM") or tr == "TRACK_2_BUFFETT_QVM" or "buffett" in st or "qvm" in st:
        return "S3_B"
    if raw in ("S3_A", "S3_C"):
        return raw
    if raw in ("S3", "S3_ACTIVE_MACRO"):
        return "S3"
    if raw in ("S6", "S6_US_SECTOR"):
        return "S6"
    if raw in ("S10", "S10_POST_MORTEM", "S10_MEGA_TREND"):
        return "S10"
    if raw in ("S14", "S14_PAIR_ALPHA"):
        return "S14_PAIR_ALPHA"
    if raw and raw not in _UNATTRIBUTED_STREAM_TAGS:
        return raw
    if ticker:
        sid, _ = infer_canonical_stream_and_track(ticker)
        return sid
    return "S4"


def is_core_or_macro_sleeve(
    stream_id: str,
    track: str = "",
    strategy: str = "",
    ticker: str = "",
) -> bool:
    """Return True if the position belongs to a structural macro/sector/core or Buffett sleeve exempt from short-term day-trading Time-Decay/Time-Limit liquidation."""
    raw = str(stream_id or "").strip().upper()
    tr = str(track or "").strip().upper()
    st = str(strategy or "").strip().lower()
    if (
        raw in _CORE_OR_MACRO_SLEEVES
        or tr in ("TRACK_1_CORE", "TRACK_2_BUFFETT_QVM")
        or st in ("buffett_qvm", "qvm_value", "macro_asset", "safe_haven", "us_core_equity")
    ):
        return True
    canon = canonicalize_stream_id(stream_id, track=track, strategy=strategy, ticker=ticker)
    return canon in _CORE_OR_MACRO_SLEEVES


def count_trading_days_held(
    entry_date_str: str,
    is_kr: bool = True,
    as_of_date: Any = None,
) -> int:
    """Count exchange trading days in (entry_date, as_of_date], excluding weekends and market holidays."""
    ed_raw = str(entry_date_str or "").strip()[:10]
    if not ed_raw:
        return 0
    try:
        from datetime import date as _dt_date, timedelta as _td
        from src.utils.market_calendar import MarketCalendar, is_us_trading_day

        entry_d = datetime.strptime(ed_raw, "%Y-%m-%d").date()
        if as_of_date is None:
            curr_d = datetime.now().date()
        elif isinstance(as_of_date, datetime):
            curr_d = as_of_date.date()
        elif isinstance(as_of_date, _dt_date):
            curr_d = as_of_date
        else:
            curr_d = datetime.strptime(str(as_of_date)[:10], "%Y-%m-%d").date()

        raw_days = (curr_d - entry_d).days
        if raw_days <= 0:
            return 0
        if raw_days > 365:
            return raw_days
        mcal = MarketCalendar() if is_kr else None
        t_days = 0
        for step in range(1, raw_days + 1):
            d_chk = entry_d + _td(days=step)
            if is_kr:
                if mcal.is_trading_day(d_chk):
                    t_days += 1
            else:
                if is_us_trading_day(d_chk):
                    t_days += 1
        return t_days
    except Exception:
        return 0


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

        self.daily_exit_path = self.ledger_path.parent / "daily_exited_tickers.json"
        self._lock = threading.Lock()
        self._ensure_ledger_exists()

    def _ensure_ledger_exists(self):
        """원장 파일 존재 확인."""
        with self._lock:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.ledger_path.exists():
                logger.info(f"  📝 [PositionLedger] 신규 원장 생성: {self.ledger_path}")
                self._save_file_unlocked({})

    def _load_file_unlocked(self) -> Dict[str, Dict[str, Any]]:
        """락 없이 파일 읽기."""
        try:
            if self.ledger_path.exists():
                with open(self.ledger_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "entries" in data and isinstance(data["entries"], dict):
                        return data["entries"]
                    return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"  ❌ [PositionLedger] 읽기 실패: {e}")
        return {}

    def _save_file_unlocked(self, data: Dict[str, Dict[str, Any]]):
        """프로세스 간 엄격 파일 락(safe_json_update strict=True) 기반 원자적 저장."""
        try:
            from src.utils.file_ops import safe_json_update
            safe_json_update(self.ledger_path, lambda _cur: dict(data), default={}, strict=True)
        except Exception as e:
            logger.error(f"  ❌ [PositionLedger] 저장 실패: {e}")

    def _save_ledger(self, data: Dict[str, Dict[str, Any]]):
        """_save_file_unlocked 별칭."""
        self._save_file_unlocked(data)

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

    def infer_stream_and_track(
        self,
        ticker: str,
        holding_info: Optional[Dict[str, Any]] = None,
        holdings_map: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """Infer canonical (stream_id, track) from holding metadata, sibling kis_portfolio.json / latest_signals.json, or canonical ticker map."""
        h_map = holdings_map if isinstance(holdings_map, dict) else {}
        if not holding_info and not h_map:
            kp_candidate = self.ledger_path.parent / "kis_portfolio.json"
            if kp_candidate.exists():
                try:
                    kp_data = json.loads(kp_candidate.read_text(encoding="utf-8"))
                    if isinstance(kp_data, dict) and isinstance(kp_data.get("holdings"), dict):
                        h_map = kp_data["holdings"]
                except Exception:
                    h_map = {}
        clean_tk = str(ticker or "").split(":")[-1].strip().upper()
        info_cand = holding_info if isinstance(holding_info, dict) else (h_map.get(clean_tk) if isinstance(h_map, dict) else None)
        raw_sid_cand = str((info_cand or {}).get("stream_id") or "").strip().upper()
        if not raw_sid_cand or raw_sid_cand in _UNATTRIBUTED_STREAM_TAGS:
            ls_candidate = self.ledger_path.parent / "latest_signals.json"
            if ls_candidate.exists():
                try:
                    ls_data = json.loads(ls_candidate.read_text(encoding="utf-8"))
                    sigs_dict = ls_data.get("signals", ls_data) if isinstance(ls_data, dict) else {}
                    if isinstance(sigs_dict, dict):
                        s14_list = sigs_dict.get("S14_PAIR_ALPHA") or []
                        for sig in s14_list:
                            if isinstance(sig, dict) and str(sig.get("ticker", "")).strip().upper() == clean_tk:
                                return "S14_PAIR_ALPHA", "TRACK_1_TACTICAL"
                except Exception:
                    pass
        return infer_canonical_stream_and_track(ticker, holding_info=holding_info, holdings_map=h_map)

    def record_entry(
        self,
        ticker: str,
        entry_date: Optional[str] = None,
        entry_price: float = 0.0,
        quantity: float = 0.0,
        stream_id: Optional[str] = None,
        strategy: Optional[str] = None,
        track: Optional[str] = None,
    ):
        """신규 포지션 진입일, 초기 HWM 및 소속 스트림/트랙(Track 1 vs Track 2) 영구 기록."""
        if not entry_date:
            entry_date = datetime.now().strftime("%Y-%m-%d")

        resolved_stream = str(stream_id or "").strip()
        resolved_strategy = str(strategy or "").strip()
        resolved_track = str(track or "").strip()
        if not resolved_stream or resolved_stream.upper() in _UNATTRIBUTED_STREAM_TAGS:
            inferred_sid, inferred_trk = self.infer_stream_and_track(ticker)
            if not resolved_stream or resolved_stream.upper() in _UNATTRIBUTED_STREAM_TAGS:
                resolved_stream = inferred_sid
            if not resolved_track:
                resolved_track = inferred_trk
        if not resolved_track and resolved_stream:
            if resolved_stream.upper() in ("S3_B", "TRACK_2_BUFFETT_QVM") or "qvm" in resolved_strategy.lower() or "buffett" in resolved_strategy.lower():
                resolved_track = "TRACK_2_BUFFETT_QVM"
            else:
                resolved_track = "TRACK_1_TACTICAL"

        with self._lock:
            ledger = self._load_file_unlocked()
            if ticker not in ledger or not isinstance(ledger.get(ticker), dict):
                ledger[ticker] = {
                    "entry_date": entry_date,
                    "created_at": datetime.now().isoformat(),
                    "notes": "Live entry recorded",
                    "peak_price": float(entry_price),
                    "lowest_price": float(entry_price),
                    "peak_pnl_pct": 0.0,
                    "peak_date": entry_date,
                    "trail_activated": False,
                    "scaled_out": False,
                    "last_avg_price": float(entry_price),
                    "last_qty": float(quantity),
                    "last_quantity": float(quantity),
                    "stream_id": resolved_stream or "S2",
                    "strategy": resolved_strategy,
                    "track": resolved_track or "TRACK_1_TACTICAL",
                }
                self._save_file_unlocked(ledger)
                logger.info(f"  📌 [PositionLedger] 신규 진입일 등록: {ticker} -> {entry_date} (stream={resolved_stream}, track={resolved_track})")
            else:
                updated = False
                cur_sid = str(ledger[ticker].get("stream_id") or "").strip()
                if resolved_stream and (cur_sid.upper() in _UNATTRIBUTED_STREAM_TAGS or cur_sid == "S3") and resolved_stream != cur_sid:
                    ledger[ticker]["stream_id"] = resolved_stream
                    updated = True
                if resolved_strategy and not ledger[ticker].get("strategy"):
                    ledger[ticker]["strategy"] = resolved_strategy
                    updated = True
                if resolved_track and (not ledger[ticker].get("track") or resolved_track == "TRACK_2_BUFFETT_QVM"):
                    ledger[ticker]["track"] = resolved_track
                    updated = True
                if updated:
                    self._save_file_unlocked(ledger)

    def get_position_meta(self, ticker: str) -> Dict[str, Any]:
        """특정 종목의 소속 스트림/트랙/진입일 메타데이터 조회."""
        with self._lock:
            ledger = self._load_file_unlocked()
            info = ledger.get(ticker)
            if info and isinstance(info, dict):
                return dict(info)
            return {}

    def record_daily_exit(
        self,
        ticker: str,
        reason: str = "",
        date_str: Optional[str] = None,
        realized_pnl_pct: float = 0.0,
    ):
        """당일 리스크/트레일링 청산된 종목 및 실현손익률/시각을 기록하여 동적 재진입 허들(Sunk-Cost Decay) 평가에 활용."""
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            try:
                from src.utils.file_ops import atomic_write_json
                data = {}
                if self.daily_exit_path.exists():
                    with open(self.daily_exit_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
                day_map = data.get(date_str, {})
                if not isinstance(day_map, dict):
                    day_map = {}
                tk_key = str(ticker).upper()
                now_dt = datetime.now()
                now_iso = now_dt.isoformat()
                prev_entry = day_map.get(tk_key)
                history = []
                first_exited_at = now_iso
                _idem_sec = 60.0
                _eps = 1e-6
                if isinstance(prev_entry, dict):
                    first_exited_at = prev_entry.get("first_exited_at") or prev_entry.get("exited_at") or now_iso
                    if isinstance(prev_entry.get("history"), list):
                        history = list(prev_entry["history"])
                    elif prev_entry.get("exited_at"):
                        history = [{
                            "exited_at": prev_entry.get("exited_at"),
                            "reason": prev_entry.get("reason", ""),
                            "realized_pnl_pct": float(prev_entry.get("realized_pnl_pct", 0.0) or 0.0),
                        }]
                    # 60초 이내 동일 종목 중복 호출(엔진+데몬 이중 기록) 시 카운트 부풀림 방지 (Idempotency)
                    prev_ex_str = str(prev_entry.get("exited_at", ""))
                    if prev_ex_str:
                        try:
                            prev_dt = datetime.fromisoformat(prev_ex_str).replace(tzinfo=None)
                            if abs((now_dt.replace(tzinfo=None) - prev_dt).total_seconds()) < _idem_sec:
                                if reason and prev_entry.get("reason") in ("", "Position closed on live balance sync"):
                                    prev_entry["reason"] = reason
                                if abs(float(realized_pnl_pct or 0.0)) > _eps:
                                    prev_entry["realized_pnl_pct"] = float(realized_pnl_pct)
                                day_map[tk_key] = prev_entry
                                data[date_str] = day_map
                                atomic_write_json(self.daily_exit_path, data, indent=2, ensure_ascii=False)
                                return
                        except Exception:
                            pass
                new_event = {
                    "exited_at": now_iso,
                    "reason": reason,
                    "realized_pnl_pct": float(realized_pnl_pct or 0.0),
                }
                history.append(new_event)
                day_map[tk_key] = {
                    "first_exited_at": first_exited_at,
                    "exited_at": now_iso,
                    "reason": reason,
                    "realized_pnl_pct": float(realized_pnl_pct or 0.0),
                    "exit_count": len(history),
                    "history": history,
                }
                data[date_str] = day_map
                atomic_write_json(self.daily_exit_path, data, indent=2, ensure_ascii=False)
                logger.info(f"  🔒 [Sunk-Cost Exit Ledger] 당일 청산 이력 등록: {ticker} ({date_str}, PnL={realized_pnl_pct:+.2f}%, count={len(history)})")
            except Exception as e:
                logger.debug(f"  [PositionLedger] record_daily_exit 예외: {e}")

    def get_daily_exit_details(self, date_str: Optional[str] = None, lookback_hours: float = 16.0) -> Dict[str, Dict[str, Any]]:
        """당일(및 미국장 자정 경계를 가로지르는 연속 세션 lookback_hours 이내) 청산된 종목별 상세 메타데이터 반환."""
        explicit_date = date_str is not None
        now_dt = datetime.now()
        if not date_str:
            date_str = now_dt.strftime("%Y-%m-%d")
        with self._lock:
            try:
                if self.daily_exit_path.exists():
                    with open(self.daily_exit_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        return {}
                    merged: Dict[str, Dict[str, Any]] = {}
                    # 자정(00:00 KST)을 넘기는 미국 본장(22:30~05:00 KST) 연속성 보장을 위해 전일 이력 중 lookback_hours 이내 항목 병합
                    if not explicit_date and lookback_hours > 0:
                        from datetime import timedelta
                        prev_date_str = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                        prev_map = data.get(prev_date_str, {})
                        if isinstance(prev_map, dict):
                            for k, v in prev_map.items():
                                if isinstance(v, dict) and v.get("exited_at"):
                                    try:
                                        ex_dt = datetime.fromisoformat(str(v["exited_at"])).replace(tzinfo=None)
                                        elapsed_h = (now_dt.replace(tzinfo=None) - ex_dt).total_seconds() / 3600.0
                                        if 0.0 <= elapsed_h <= lookback_hours:
                                            merged[str(k).upper()] = v
                                    except Exception:
                                        pass
                    day_map = data.get(date_str, {})
                    if isinstance(day_map, dict):
                        for k, v in day_map.items():
                            merged[str(k).upper()] = v if isinstance(v, dict) else {"exited_at": "", "realized_pnl_pct": 0.0}
                    return merged
            except Exception:
                pass
            return {}

    def get_daily_exited_tickers(self, date_str: Optional[str] = None) -> set:
        """당일 리스크/트레일링 청산된 종목 집합 반환."""
        return set(self.get_daily_exit_details(date_str).keys())

    def mark_scaled_out(self, ticker: str):
        """포지션의 분할 익절/시간감쇠 반익절(scaled_out) 상태 영구 마킹."""
        with self._lock:
            ledger = self._load_file_unlocked()
            if ticker not in ledger or not isinstance(ledger.get(ticker), dict):
                ledger[ticker] = {
                    "entry_date": datetime.now().strftime("%Y-%m-%d"),
                    "created_at": datetime.now().isoformat(),
                    "notes": "Auto-registered on mark_scaled_out",
                    "peak_price": 0.0,
                    "lowest_price": 0.0,
                    "peak_pnl_pct": 0.0,
                    "peak_date": datetime.now().strftime("%Y-%m-%d"),
                    "trail_activated": False,
                    "scaled_out": True,
                }
            else:
                ledger[ticker]["scaled_out"] = True
            self._save_file_unlocked(ledger)
            logger.info(f"  📌 [PositionLedger] 분할 청산(scaled_out=True) 영구 기록: {ticker}")

    def purge_closed_positions(self, active_tickers) -> None:
        """실계좌 미보유(청산 완료) 종목 원장 제거 및 당일 청산 이력(Sunk-Cost Exit Ledger) 누락 방지 자동 등록."""
        active_set = set(str(t) for t in (active_tickers or []))
        purged_entries: List[ tuple ] = []
        with self._lock:
            ledger = self._load_file_unlocked()
            to_remove = [t for t in ledger if t not in active_set]
            if to_remove:
                for t in to_remove:
                    info = ledger.get(t, {}) if isinstance(ledger.get(t), dict) else {}
                    purged_entries.append((str(t), float(info.get("last_pnl_pct", 0.0) or 0.0)))
                    del ledger[t]
                    logger.info(f"  🗑️ [PositionLedger] 청산 종목 원장 제거: {t}")
                self._save_file_unlocked(ledger)
        # 원장에서 제거된 종목이 당일 청산 원장(daily_exited_tickers.json)에 누락되어 있다면 즉시 보강 기록
        if purged_entries:
            recent_exits = self.get_daily_exit_details()
            for tk_purged, last_pnl in purged_entries:
                if tk_purged.upper() not in recent_exits:
                    self.record_daily_exit(
                        tk_purged,
                        reason="Position closed on live balance sync",
                        realized_pnl_pct=last_pnl,
                    )

    def update_hwm(
        self,
        ticker: str,
        current_price: float,
        current_pnl_pct: float,
        date_str: Optional[str] = None,
        trail_activate_threshold: Optional[float] = None,
        avg_price: Optional[float] = None,
        quantity: Optional[float] = None,
        stream_id: Optional[str] = None,
        strategy: Optional[str] = None,
        track: Optional[str] = None,
    ) -> Dict[str, Any]:
        """포지션의 최고가(HWM), 최저가 및 최고 수익률 영구 갱신 및 조회 (실측 기반, 물타기/불타기 수량 가중 보존)."""
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        if trail_activate_threshold is None:
            try:
                from src.utils.dynamic_quant_engine import get_quant_engine
                trail_activate_threshold = float(
                    get_quant_engine().compute_optimal_stopping_params(str(ticker), price=current_price)["trailing_trigger_pct"]
                )
            except Exception:
                trail_activate_threshold = 2.0

        resolved_stream = str(stream_id or "").strip()
        resolved_track = str(track or "").strip()
        if not resolved_stream or resolved_stream.upper() in _UNATTRIBUTED_STREAM_TAGS:
            inferred_sid, inferred_trk = self.infer_stream_and_track(ticker)
            if not resolved_stream or resolved_stream.upper() in _UNATTRIBUTED_STREAM_TAGS:
                resolved_stream = inferred_sid
            if not resolved_track:
                resolved_track = inferred_trk
        if not resolved_track:
            resolved_track = "TRACK_2_BUFFETT_QVM" if resolved_stream.upper() in ("S3_B", "TRACK_2_BUFFETT_QVM") else "TRACK_1_TACTICAL"

        with self._lock:
            ledger = self._load_file_unlocked()

            if ticker not in ledger or not isinstance(ledger.get(ticker), dict):
                ledger[ticker] = {
                    "entry_date": date_str,
                    "created_at": datetime.now().isoformat(),
                    "notes": "Auto-registered on HWM update",
                    "peak_price": float(current_price or 0.0),
                    "lowest_price": float(current_price or 0.0),
                    "peak_pnl_pct": max(0.0, float(current_pnl_pct or 0.0)),
                    "peak_date": date_str,
                    "trail_activated": bool(float(current_pnl_pct or 0.0) >= trail_activate_threshold),
                    "scaled_out": False,
                    "last_avg_price": float(avg_price or 0.0),
                    "last_qty": float(quantity or 0.0),
                    "last_quantity": float(quantity or 0.0),
                    "stream_id": resolved_stream or "S2",
                    "strategy": str(strategy or ""),
                    "track": resolved_track,
                }

            info = ledger[ticker]
            entry_dt = str(info.get("entry_date", date_str))[:10]
            peak_dt = str(info.get("peak_date", entry_dt))[:10]

            if peak_dt < entry_dt:
                info["peak_price"] = float(current_price or 0.0)
                info["lowest_price"] = float(current_price or 0.0)
                info["peak_pnl_pct"] = max(0.0, float(current_pnl_pct or 0.0))
                info["peak_date"] = date_str
                info["trail_activated"] = bool(info["peak_pnl_pct"] >= trail_activate_threshold)

            updated = False
            cur_sid = str(info.get("stream_id") or "").strip()
            if resolved_stream and (cur_sid.upper() in _UNATTRIBUTED_STREAM_TAGS or (stream_id and cur_sid == "S3")) and resolved_stream != cur_sid:
                info["stream_id"] = resolved_stream
                updated = True
            if strategy and not info.get("strategy"):
                info["strategy"] = str(strategy)
                updated = True
            if resolved_track and (not info.get("track") or resolved_track == "TRACK_2_BUFFETT_QVM"):
                info["track"] = resolved_track
                updated = True

            prev_qty = float(info.get("last_quantity", info.get("last_qty", 0.0)) or 0.0)
            prev_avg_px = float(info.get("last_avg_price", 0.0) or 0.0)
            curr_qty = float(quantity or 0.0)
            curr_avg_px = float(avg_price or 0.0)
            _qty_eps = 1e-6
            _pnl_eps = 1e-4

            if curr_qty > 0 and prev_qty > 0 and curr_qty > prev_qty + _qty_eps and current_price > 0:
                w_old = prev_qty / curr_qty
                old_pk_px = float(info.get("peak_price", current_price) or current_price)
                blended_peak_px = max(float(current_price), w_old * old_pk_px + (1.0 - w_old) * float(current_price))
                info["peak_price"] = round(blended_peak_px, 6)
                if curr_avg_px > 0:
                    blended_peak_pnl = max(float(current_pnl_pct or 0.0), ((blended_peak_px - curr_avg_px) / curr_avg_px) * 100.0)
                else:
                    old_pk_pnl = float(info.get("peak_pnl_pct", current_pnl_pct) or current_pnl_pct)
                    blended_peak_pnl = max(float(current_pnl_pct or 0.0), w_old * old_pk_pnl + (1.0 - w_old) * float(current_pnl_pct or 0.0))
                info["peak_pnl_pct"] = round(max(0.0, blended_peak_pnl), 6)
                info["trail_activated"] = bool(info["peak_pnl_pct"] >= trail_activate_threshold)
                try:
                    now_naive = datetime.now().replace(tzinfo=None)
                    old_c_dt = datetime.fromisoformat(str(info.get("created_at", "")).replace("Z", "+00:00")).replace(tzinfo=None)
                    old_elapsed_s = max(0.0, (now_naive - old_c_dt).total_seconds())
                    from datetime import timedelta as _td_hwm
                    info["created_at"] = (now_naive - _td_hwm(seconds=w_old * old_elapsed_s)).isoformat()
                    if w_old * 2.0 < 1.0:
                        info["entry_date"] = date_str
                except Exception:
                    info["created_at"] = datetime.now().isoformat()
                updated = True
            elif curr_avg_px > 0 and prev_avg_px > 0 and abs(curr_avg_px - prev_avg_px) * 100.0 > prev_avg_px / 2.0 and current_price > 0:
                old_pk_px = max(float(info.get("peak_price", current_price) or current_price), float(current_price))
                reanchored_pnl = max(0.0, float(current_pnl_pct or 0.0), ((old_pk_px - curr_avg_px) / curr_avg_px) * 100.0)
                info["peak_pnl_pct"] = round(reanchored_pnl, 6)
                info["trail_activated"] = bool(info["peak_pnl_pct"] >= trail_activate_threshold)
                updated = True

            if curr_qty > 0 and curr_qty != prev_qty:
                info["last_qty"] = curr_qty
                info["last_quantity"] = curr_qty
                updated = True
            if curr_avg_px > 0 and curr_avg_px != prev_avg_px:
                info["last_avg_price"] = curr_avg_px
                updated = True

            raw_peak_price = float(info.get("peak_price", 0.0) or 0.0)
            prev_peak_price = raw_peak_price or float(current_price or 0.0)
            prev_lowest_price = float(info.get("lowest_price", current_price) or current_price)
            raw_peak_pnl = info.get("peak_pnl_pct", None)
            prev_peak_pnl = float(raw_peak_pnl) if (raw_peak_pnl is not None and raw_peak_price) else float(current_pnl_pct or 0.0)

            if (not raw_peak_price) or current_price > prev_peak_price or current_pnl_pct > prev_peak_pnl:
                info["peak_price"] = max(prev_peak_price, float(current_price or 0.0))
                info["peak_pnl_pct"] = max(prev_peak_pnl, float(current_pnl_pct or 0.0))
                info["peak_date"] = date_str
                updated = True
            else:
                if "peak_price" not in info:
                    info["peak_price"] = prev_peak_price
                    updated = True
                if "peak_pnl_pct" not in info:
                    info["peak_pnl_pct"] = prev_peak_pnl
                    updated = True

            if current_price > 0 and (prev_lowest_price <= 0 or current_price < prev_lowest_price):
                info["lowest_price"] = float(current_price)
                updated = True
            elif "lowest_price" not in info:
                info["lowest_price"] = float(prev_lowest_price)
                updated = True

            if "scaled_out" not in info:
                info["scaled_out"] = False
                updated = True

            if abs(float(current_pnl_pct or 0.0) - float(info.get("last_pnl_pct", 0.0) or 0.0)) > _pnl_eps:
                info["last_pnl_pct"] = float(current_pnl_pct or 0.0)
                updated = True

            if info.get("peak_pnl_pct", 0.0) >= trail_activate_threshold:
                if not info.get("trail_activated", False):
                    info["trail_activated"] = True
                    updated = True

            if updated:
                self._save_file_unlocked(ledger)

            return dict(info)

    def get_hwm(self, ticker: str) -> Dict[str, Any]:
        """특정 종목의 HWM 및 스트림/트랙 메타데이터 조회."""
        with self._lock:
            ledger = self._load_file_unlocked()
            info = ledger.get(ticker)
            if info and isinstance(info, dict):
                return {
                    "peak_price": float(info.get("peak_price", 0.0) or 0.0),
                    "lowest_price": float(info.get("lowest_price", 0.0) or 0.0),
                    "peak_pnl_pct": float(info.get("peak_pnl_pct", 0.0) or 0.0),
                    "peak_date": info.get("peak_date"),
                    "trail_activated": bool(info.get("trail_activated", False)),
                    "scaled_out": bool(info.get("scaled_out", False)),
                    "created_at": info.get("created_at"),
                    "stream_id": info.get("stream_id"),
                    "strategy": info.get("strategy"),
                    "track": info.get("track"),
                }
            return {
                "peak_price": 0.0,
                "lowest_price": 0.0,
                "peak_pnl_pct": 0.0,
                "peak_date": None,
                "trail_activated": False,
                "scaled_out": False,
                "created_at": None,
                "stream_id": None,
                "strategy": None,
                "track": None,
            }

    def sync_live_positions(
        self,
        active_tickers: Any,
        current_date_str: Optional[str] = None,
        holdings_map: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """실계좌 보유 목록과 원장 동기화 (하드코딩 백필 주입 제거 및 canonical stream/track 보존)."""
        if not current_date_str:
            current_date_str = datetime.now().strftime("%Y-%m-%d")

        h_map: Dict[str, Any] = {}
        if isinstance(holdings_map, dict):
            h_map = dict(holdings_map)
        elif isinstance(active_tickers, dict):
            if isinstance(active_tickers.get("holdings"), dict):
                h_map = dict(active_tickers["holdings"])
            else:
                h_map = dict(active_tickers)
            active_tickers = list(h_map.keys())
        else:
            kp_candidate = self.ledger_path.parent / "kis_portfolio.json"
            if kp_candidate.exists():
                try:
                    kp_data = json.loads(kp_candidate.read_text(encoding="utf-8"))
                    if isinstance(kp_data, dict) and isinstance(kp_data.get("holdings"), dict):
                        h_map = kp_data["holdings"]
                except Exception:
                    h_map = {}

        ticker_list = [str(t) for t in (active_tickers or [])]
        result = {}
        with self._lock:
            ledger = self._load_file_unlocked()
            updated = False

            # 1. 활성 종목 동기화
            for ticker in ticker_list:
                h_info = h_map.get(ticker) if isinstance(h_map, dict) else None
                inferred_sid, inferred_trk = infer_canonical_stream_and_track(
                    ticker,
                    holding_info=h_info if isinstance(h_info, dict) else None,
                    holdings_map=h_map,
                )
                if ticker in ledger and isinstance(ledger[ticker], dict) and "entry_date" in ledger[ticker]:
                    entry_dt = str(ledger[ticker]["entry_date"])[:10]
                    peak_dt = str(ledger[ticker].get("peak_date", entry_dt))[:10]
                    if peak_dt < entry_dt:
                        ledger[ticker]["peak_price"] = 0.0
                        ledger[ticker]["peak_pnl_pct"] = 0.0
                        ledger[ticker]["peak_date"] = current_date_str
                        ledger[ticker]["trail_activated"] = False
                        updated = True
                    if "created_at" not in ledger[ticker]:
                        ledger[ticker]["created_at"] = datetime.now().isoformat()
                        updated = True
                    cur_sid = str(ledger[ticker].get("stream_id") or "").strip()
                    if not cur_sid or cur_sid.upper() in _UNATTRIBUTED_STREAM_TAGS:
                        ledger[ticker]["stream_id"] = inferred_sid
                        updated = True
                    cur_trk = str(ledger[ticker].get("track") or "").strip()
                    if not cur_trk or (inferred_trk == "TRACK_2_BUFFETT_QVM" and cur_trk != "TRACK_2_BUFFETT_QVM"):
                        ledger[ticker]["track"] = inferred_trk
                        updated = True
                    result[ticker] = ledger[ticker]["entry_date"]
                else:
                    # 완전 신규 매수종목 (실측 0.0 초기화 + canonical stream/track 추론)
                    ledger[ticker] = {
                        "entry_date": current_date_str,
                        "created_at": datetime.now().isoformat(),
                        "notes": "Auto-registered on sync",
                        "peak_price": 0.0,
                        "peak_pnl_pct": 0.0,
                        "peak_date": current_date_str,
                        "trail_activated": False,
                        "stream_id": inferred_sid,
                        "track": inferred_trk,
                    }
                    result[ticker] = current_date_str
                    updated = True

            # 2. 청산된 종목 정리
            active_set = set(ticker_list)
            to_remove = [t for t in ledger if t not in active_set]
            for t in to_remove:
                del ledger[t]
                updated = True
                logger.info(f"  🗑️ [PositionLedger] 청산 종목 원장 제거: {t}")

            if updated:
                self._save_file_unlocked(ledger)

        return result

    def reconcile_with_broker(
        self,
        broker_holdings: Any = None,
        current_date_str: Optional[str] = None,
    ) -> Dict[str, str]:
        """실계좌 보유 목록(또는 kis_portfolio.json['holdings'])과 원장 대사 및 stream_id/track 정준 보정."""
        h_map: Dict[str, Any] = {}
        active_list: List[str] = []
        if isinstance(broker_holdings, dict):
            if isinstance(broker_holdings.get("holdings"), dict):
                h_map = dict(broker_holdings["holdings"])
            else:
                h_map = dict(broker_holdings)
            active_list = [
                str(k)
                for k, v in h_map.items()
                if not isinstance(v, dict)
                or float(v.get("qty", v.get("quantity", v.get("shares", 1))) or 0) > 0
            ]
        elif isinstance(broker_holdings, (list, set, tuple)):
            active_list = [str(t) for t in broker_holdings]
        else:
            kp_candidate = self.ledger_path.parent / "kis_portfolio.json"
            if kp_candidate.exists():
                try:
                    kp_data = json.loads(kp_candidate.read_text(encoding="utf-8"))
                    if isinstance(kp_data, dict) and isinstance(kp_data.get("holdings"), dict):
                        h_map = kp_data["holdings"]
                        active_list = [
                            str(k)
                            for k, v in h_map.items()
                            if isinstance(v, dict)
                            and float(v.get("qty", v.get("quantity", v.get("shares", 1))) or 0) > 0
                        ]
                except Exception:
                    pass
        return self.sync_live_positions(active_list, current_date_str=current_date_str, holdings_map=h_map)

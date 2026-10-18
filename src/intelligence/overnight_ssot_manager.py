"""
Project Meridian — Overnight Market SSoT Manager & Strict Gatekeeper
========================================================================
[Red Team Architecture Standard]
1. 단일 진실 원천(SSoT): results/overnight_market_ssot.json
2. Strict Trading Date & Timestamp Verification Gatekeeper
3. No Silent Fallback: 과거/파손 데이터 감지 시 KIS Direct Master 재수집 강제 가동
4. Cross-Asset Integrity & Discoupling Audit
"""

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.file_ops import atomic_write_json
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)

class SSOTValidationException(Exception):
    """[Red Team Standard] SSoT 데이터 신선도/유효성 검증 위반 시 발생."""

@dataclass
class OvernightDataRecord:
    trading_date: str                               # YYYY-MM-DD (target trading session date)
    updated_at: str                                 # ISO 8601 timestamp
    night_futures_close: Optional[float]            # KRX Night Futures Close (None = 결측)
    night_futures_change_pct: Optional[float]       # Change %
    sp500_change_1d: Optional[float]                # S&P 500 %
    nasdaq_change_1d: Optional[float]               # NASDAQ %
    sox_change_1d: Optional[float]                  # SOX Semiconductor %
    vix: Optional[float]                            # VIX Volatility Index
    usdkrw: Optional[float]                         # USD/KRW FX Rate
    us10y: Optional[float]                          # US 10Y Yield
    status: str = "VALIDATED_LIVE"                  # VALIDATED_LIVE / DEGRADED_MISSING:<fields> / STALE
    source_checksum: str = ""                       # Integrity verification checksum

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class OvernightSSoTManager:
    """[Red Team Architecture] 단일 진실 원천 야간 데이터 통합 게이트키퍼."""

    SSOT_FILE = _PROJECT_ROOT / "results" / "overnight_market_ssot.json"
    MAX_AGE_HOURS = 16.0  # 야간 마감 데이터 유효 시간 (최대 16시간)

    def __init__(self):
        self.SSOT_FILE.parent.mkdir(parents=True, exist_ok=True)

    def get_verified_overnight_data(self, force_refresh: bool = False) -> OvernightDataRecord:
        """[Strict Gatekeeper] 검증된 야간 SSoT 데이터 반환.
        
        날짜 불일치/경과시간 초과/파손 감지 시 KIS Direct Master 재수집 자동 트리거.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        if not force_refresh and self.SSOT_FILE.exists():
            try:
                with open(self.SSOT_FILE, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                
                record = OvernightDataRecord(**{k: v for k, v in raw_data.items() if k in OvernightDataRecord.__dataclass_fields__})
                
                # Strict Validation Gate 1: Trading Date Match
                # 월요일 새벽 수집 시 일요일/토요일 세션 데이터 허용, 평일은 오늘 날짜 일치 필수
                if self._validate_record_freshness(record, today_str):
                    logger.info(f"  🛡️ [SSoT Gatekeeper] 야간 마감 SSoT 검증 통과 (날짜: {record.trading_date}, 야간선물: {record.night_futures_change_pct:+.2f}%, VIX: {record.vix:.2f})")
                    return record
                else:
                    logger.warning(f"  ⚠️ [SSoT Gatekeeper] 과거/미갱신/결측 SSoT 데이터 감지 (기존: {record.trading_date}, status={record.status}) ➔ 재수집 가동")
            except Exception as e:
                logger.error(f"  ❌ [SSoT Gatekeeper] SSoT 파일 파손/파싱 에러: {e} ➔ 마스터 재수집 시작")

        # Fallback to mandatory active refetching if stale or forced
        return self.rebuild_and_sync_ssot()

    def _validate_record_freshness(self, record: OvernightDataRecord, today_str: str) -> bool:
        """레코드 신선도 및 수학적 타당성 검증 (결측(None)·DEGRADED는 불합격)."""
        if (not record.updated_at or record.night_futures_close is None or record.vix is None
                or record.night_futures_change_pct is None
                or record.night_futures_close <= 0 or record.vix <= 0
                or not str(record.status).startswith("VALIDATED")):
            return False

        try:
            up_dt = datetime.fromisoformat(record.updated_at)
            age_hours = (datetime.now() - up_dt).total_seconds() / 3600.0
            if age_hours > self.MAX_AGE_HOURS:
                logger.warning(f"  ⚠️ [SSoT Validation] 데이터 시간 경과 ({age_hours:.1f}시간 > {self.MAX_AGE_HOURS}시간)")
                return False
        except Exception:
            return False

        # 날짜 검증: 오늘 날짜이거나 주말 직후 세션인지 확인
        if record.trading_date == today_str:
            return True
        
        # 새벽 08:30 이전인 경우 전일/당일 세션 연속성 인정
        now_h = datetime.now().hour
        if now_h < 9:
            rec_dt = datetime.strptime(record.trading_date, "%Y-%m-%d")
            diff_days = (datetime.now() - rec_dt).days
            if diff_days <= 1 or (datetime.now().weekday() == 0 and diff_days <= 3):
                return True

        return False

    def rebuild_and_sync_ssot(self, override_night_futures: Optional[Tuple[float, float]] = None) -> OvernightDataRecord:
        """[Master Active Refetch] 모든 소스를 종합하여 단일 SSoT 파일 영속화.

        ★ Wave 0 D1: 수집 실패 값을 상수(VIX 16.04, USDKRW 1352.5, 야간선물 1106.55/+0.95% 등)로 날조하고
          'VALIDATED_LIVE'로 기록하던 로직 제거. 결측은 None, status='DEGRADED_MISSING:<필드>' 로 명시하며
          소비자는 결측 필드 기반 신규 진입을 보류(fail-closed)해야 한다.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        now_iso = datetime.now().isoformat()
        missing: List[str] = []

        logger.info("  🔄 [SSoT Master Engine] 야간 마감 데이터 통합 갱신 시작...")

        def _last_from_vendor(symbol: str) -> Optional[float]:
            try:
                from src.utils.vendor_multiplexer import VendorMultiplexer
                start_d = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
                s = VendorMultiplexer().fetch(symbol, start_d, today_str)
                if s is not None and not s.empty:
                    v = float(s.iloc[-1])
                    return v if v == v and v > 0 else None   # NaN/비양수 거부
            except Exception as e:
                logger.warning(f"  ⚠️ [SSoT] {symbol} 수집 실패: {e}")
            return None

        vix_val = _last_from_vendor('VIX')
        fx_val = _last_from_vendor('USDKRW')
        us10y_val = _last_from_vendor('US10Y')

        # US Indices (SP500, NASDAQ, SOX) — signal_cache 관측값만 사용 (없으면 None)
        sp500_chg = nasdaq_chg = sox_chg = None
        sc_path = _PROJECT_ROOT / "results" / "signal_cache.json"
        if sc_path.exists():
            try:
                with open(sc_path, "r", encoding="utf-8") as f:
                    sc = json.load(f)
                def _f(k):
                    v = sc.get(k)
                    return float(v) if isinstance(v, (int, float)) else None
                sp500_chg, nasdaq_chg, sox_chg = _f("sp500_change_1d"), _f("nasdaq_change_1d"), _f("sox_change_1d")
            except Exception as e:
                logger.warning(f"  ⚠️ [SSoT] signal_cache 미국지수 로드 실패: {e}")

        # Night Futures (KRX KOSPI200 Night Session)
        nf_close = nf_chg = None
        if override_night_futures:
            nf_close, nf_chg = override_night_futures
        else:
            try:
                from scripts.fetch_night_futures import fetch_and_save_night_futures
                nf_res = fetch_and_save_night_futures()
                if nf_res and float(nf_res.get("close", 0) or 0) > 0 and nf_res.get("change_pct") is not None:
                    nf_close = float(nf_res["close"])
                    nf_chg = float(nf_res["change_pct"])
            except Exception as e:
                logger.warning(f"  ⚠️ 야간선물 자동 재수집 예외: {e}")

        for name, val in (('night_futures_close', nf_close), ('night_futures_change_pct', nf_chg), ('vix', vix_val),
                          ('usdkrw', fx_val), ('sp500_change_1d', sp500_chg), ('nasdaq_change_1d', nasdaq_chg),
                          ('sox_change_1d', sox_chg), ('us10y', us10y_val)):
            if val is None:
                missing.append(name)
        status = "VALIDATED_LIVE" if not missing else f"DEGRADED_MISSING:{','.join(missing)}"
        if missing:
            logger.critical(f"  🚨 [SSoT] 결측 필드 {missing} — 날조 없이 DEGRADED 기록 (소비자 fail-closed 필요)")

        _r = lambda v: (round(float(v), 4) if v is not None else None)
        record = OvernightDataRecord(
            trading_date=today_str,
            updated_at=now_iso,
            night_futures_close=_r(nf_close),
            night_futures_change_pct=_r(nf_chg),
            sp500_change_1d=_r(sp500_chg),
            nasdaq_change_1d=_r(nasdaq_chg),
            sox_change_1d=_r(sox_chg),
            vix=_r(vix_val),
            usdkrw=_r(fx_val),
            us10y=_r(us10y_val),
            status=status,
            source_checksum=f"SSOT_{today_str}_{int(time.time())}"
        )

        # Write to SSoT file atomically
        atomic_write_json(self.SSOT_FILE, record.to_dict(), indent=2, ensure_ascii=False)
        
        # Mirror to secondary legacy files — ★ Wave 0 D1: 관측된 값만 미러링 (결측을 상수로 덮지 않음)
        try:
            if record.night_futures_close is not None and record.night_futures_change_pct is not None:
                try:
                    from src.data_collection.futures_contract_resolver import DynamicFuturesContractResolver
                    _front = DynamicFuturesContractResolver.get_current_front_month_code()
                except Exception as _fr_e:
                    logger.warning(f"  ⚠️ [SSoT] 근월물 코드 동적 조회 실패: {_fr_e}")
                    _front = None
                legacy_nf = {
                    "symbol": "10100000",
                    "front_code": _front,
                    "close": record.night_futures_close,
                    "change_pct": record.night_futures_change_pct,
                    "status": "OK",
                    "source": "OvernightSSoTManager",
                    "timestamp": now_iso
                }
                atomic_write_json(_PROJECT_ROOT / "data" / "macro" / "night_futures.json", legacy_nf, indent=2)
                atomic_write_json(_PROJECT_ROOT / "results" / "krx_futures_overnight.json", legacy_nf, indent=2)

            # Sync signal_cache.json (관측값만)
            # ★ Wave 1 Track A: 락 보유 merge-update (파일이 있을 때만 — 기존 의미 유지, 손상 파일 덮어쓰기 금지)
            if sc_path.exists():
                from src.utils.file_ops import safe_json_update
                _sc_upd = {_k_sc: _v_sc for _k_sc, _v_sc in (("night_futures", record.night_futures_close),
                                                           ("night_futures_change_1d", record.night_futures_change_pct),
                                                           ("vix", record.vix), ("usdkrw", record.usdkrw))
                           if _v_sc is not None}

                class _NoFile(Exception):
                    pass

                def _mutate_sc(sc_data):
                    if not isinstance(sc_data, dict):
                        raise _NoFile()   # 그 사이 삭제됨/비정상 루트 → 생성·덮어쓰기 하지 않음
                    sc_data.update(_sc_upd)
                    return sc_data
                try:
                    safe_json_update(sc_path, _mutate_sc, default=None, indent=2)
                except _NoFile:
                    pass
        except Exception as e:
            logger.warning(f"  ⚠️ 레거시 미러링 갱신 예외: {e}")

        logger.info(f"  [SSoT Master Engine] SSoT 기록 완료 (status={record.status}) -> {self.SSOT_FILE}")
        return record

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mgr = OvernightSSoTManager()
    rec = mgr.get_verified_overnight_data(force_refresh=True)
    print("SSoT Record:", json.dumps(rec.to_dict(), indent=2, ensure_ascii=False))

#!/usr/bin/env python3
"""
Project Meridian Legacy Artifact Purger & Zero-Data-Loss Disk Retention Engine
==============================================================================
1. 핵심 퀀트 자산 100% 영구 보존 (Zero-Data-Loss Invariant):
   - data/*.parquet (틱/호가창/분봉/일봉/수급/피처 시계열 전체)
   - results/corporate_ledger/broker_fills_master.json, daily_nav_journal.json
   - results/kis_portfolio.json, shadow_portfolio.json, position_entry_ledger.json
   - results/shadow_trades/YYYY-MM-DD.json (14일 경과분은 삭제 대신 .json.gz 무손실 압축 보관)
2. 비구조화 콘솔 텍스트 로그 및 렌더링 산출물 TTL 자동 삭제:
   - results/logs/*.log*, logs/*.log*, logs/market_email_*.lock (14일 경과분 삭제)
   - reports/*.pdf, results/reports/* (14일 경과분 삭제)
   - results/corporate_ledger/*_YYYYMMDD.xlsx/csv (Latest.xlsx 및 마스터 JSON 보존, 14일 경과 중복 스냅샷 삭제)
   - 루트 scratch/, archive/, backups/, catboost_info/, *.tmp*, 고아 .pyc 즉시 삭제
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROTECTED_SSOT_FILES = frozenset({
    "kis_portfolio.json",
    "shadow_portfolio.json",
    "position_entry_ledger.json",
    "position_ledger.json",
    "us_night_holdings.json",
    "signal_cache.json",
    "signal_cache.lock",
    "broker_fills_master.json",
    "daily_nav_journal.json",
    "Meridian_Corporate_Accounting_Ledger_Latest.xlsx",
})

_DATE_YYYYMMDD_RX = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")
_DATE_ISO_RX = re.compile(r"(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")


def _extract_file_date(name: str) -> Optional[datetime]:
    """Extract YYYYMMDD or YYYY-MM-DD date embedded in a filename."""
    m_iso = _DATE_ISO_RX.search(name)
    if m_iso:
        try:
            return datetime(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except ValueError:
            pass
    m_compact = _DATE_YYYYMMDD_RX.search(name)
    if m_compact:
        try:
            return datetime(int(m_compact.group(1)), int(m_compact.group(2)), int(m_compact.group(3)))
        except ValueError:
            pass
    return None


def _is_older_than_days(
    path: Path,
    retention_days: int,
    now_ts: float,
    ref_date: datetime,
) -> bool:
    """Return True if file mtime OR embedded filename date is older than retention_days."""
    cutoff_seconds = float(retention_days * 86400)
    try:
        mtime = path.stat().st_mtime
        if (now_ts - mtime) > cutoff_seconds:
            return True
    except OSError:
        return False

    file_dt = _extract_file_date(path.name)
    if file_dt is not None:
        age_days = (ref_date - file_dt).days
        if age_days > retention_days:
            return True
    return False


def _compress_json_lossless(src_json: Path) -> bool:
    """Compress a historical JSON trade log into .json.gz with integrity verification."""
    gz_path = src_json.with_suffix(src_json.suffix + ".gz")
    tmp_gz = src_json.with_suffix(src_json.suffix + ".gz.tmp")
    try:
        raw_bytes = src_json.read_bytes()
        with gzip.open(tmp_gz, "wb", compresslevel=6) as f_out:
            f_out.write(raw_bytes)
        with gzip.open(tmp_gz, "rb") as f_verify:
            verified = f_verify.read()
        if len(verified) != len(raw_bytes):
            tmp_gz.unlink(missing_ok=True)
            return False
        os.replace(tmp_gz, gz_path)
        src_json.unlink()
        lock_companion = src_json.with_suffix(src_json.suffix + ".lock")
        if lock_companion.exists():
            lock_companion.unlink(missing_ok=True)
        return True
    except Exception as exc:
        tmp_gz.unlink(missing_ok=True)
        logger.warning(f"  ⚠️ 무손실 압축 실패 ({src_json.name}): {exc}")
        return False


def purge_legacy_artifacts(
    project_root: Optional[Path] = None,
    now_ts: Optional[float] = None,
    log_retention_days: int = 14,
    report_retention_days: int = 14,
    trade_compress_after_days: int = 14,
) -> Dict[str, int]:
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    results_dir = root / "results"
    if not results_dir.exists():
        logger.warning(f"results 디렉터리가 존재하지 않습니다: {results_dir}")
        return {"purged_count": 0, "compressed_count": 0, "bytes_freed": 0}

    now_val = float(now_ts) if now_ts is not None else time.time()
    # Reference date is the maximum of wall-clock date and latest dated file in results/logs or logs
    ref_date = datetime.fromtimestamp(now_val)
    for scan_dir in (results_dir / "logs", root / "logs", results_dir / "shadow_trades"):
        if scan_dir.exists():
            for p in scan_dir.iterdir():
                dt = _extract_file_date(p.name)
                if dt is not None and dt > ref_date:
                    ref_date = dt

    purged_count = 0
    compressed_count = 0
    bytes_freed = 0

    # 1. 프로젝트 루트 레거시/임시 디렉터리 즉시 영구 삭제 (data/는 절대 포함하지 않음)
    for p_dir in (
        root / "archive",
        root / "backups",
        root / ".legacy_quarantine",
        root / "scratch",
        root / "catboost_info",
    ):
        if p_dir.exists():
            shutil.rmtree(p_dir, ignore_errors=True)
            purged_count += 1
            logger.info(f"  🧹 [Permanent Purge] 루트 레거시 디렉터리 삭제: {p_dir.name}")

    # 2. 레거시 락 파일 및 임시 파일 삭제
    stale_files = (
        results_dir / ".lock_shadow_portfolio.lock",
        results_dir / "virtual_ledger.json.lock",
        results_dir / "us_night_holdings.json.lock",
    )
    for sf in stale_files:
        if sf.exists():
            try:
                sf.unlink()
                purged_count += 1
            except OSError:
                pass

    # data/ 내에서는 오직 비정상 종료로 남은 *.tmp* 임시 조각만 제거 (원본 .parquet 등은 100% 보존)
    data_dir = root / "data"
    if data_dir.exists():
        for d_tmp in list(data_dir.rglob("*.tmp*")):
            try:
                sz = d_tmp.stat().st_size
                d_tmp.unlink()
                purged_count += 1
                bytes_freed += sz
            except OSError:
                pass

    # 3. 소스(.py)가 삭제된 고아 .pyc 바이트코드 파일 정리
    for code_root in (root / "scripts", root / "src", root / "tests"):
        if not code_root.exists():
            continue
        for pyc in list(code_root.rglob("__pycache__/*.pyc")):
            stem = pyc.name.split(".")[0]
            src_py = pyc.parent.parent / f"{stem}.py"
            if not src_py.exists():
                try:
                    sz = pyc.stat().st_size
                    pyc.unlink()
                    purged_count += 1
                    bytes_freed += sz
                except OSError:
                    pass

    # 4. results/ 루트 레거시 파일 패턴 삭제 (보호 대상 SSOT 절대 제외)
    legacy_patterns = (
        "s6*",
        "*s6*",
        "archive*",
        "backups*",
        "archive_pre_*",
        "*.bak_pre_live",
        "*.bak",
        "*.tmp*",
        "tmpbu2_*",
        "Daily_Quant_Report_*.md",
        "shadow_report_*.md",
        "shadow_report_*.json",
        "Weekly_Corporate_Ledger_*.csv",
        "Weekly_Corporate_Ledger_*.md",
    )
    for pattern in legacy_patterns:
        for item in list(results_dir.glob(pattern)):
            if item.name in PROTECTED_SSOT_FILES:
                continue
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    sz = item.stat().st_size
                    item.unlink()
                    bytes_freed += sz
                purged_count += 1
            except OSError:
                pass

    # 5. 비구조화 콘솔 텍스트 로그(results/logs, logs) 14일 TTL 자동 삭제
    for log_dir in (results_dir / "logs", root / "logs"):
        if not log_dir.exists():
            continue
        for log_file in list(log_dir.iterdir()):
            if not log_file.is_file():
                continue
            if log_file.name in PROTECTED_SSOT_FILES:
                continue
            is_log_or_lock = (
                ".log" in log_file.name
                or log_file.name.startswith("market_email_")
                or log_file.name.endswith(".tmp")
            )
            if not is_log_or_lock:
                continue
            if _is_older_than_days(log_file, log_retention_days, now_val, ref_date):
                try:
                    sz = log_file.stat().st_size
                    log_file.unlink()
                    purged_count += 1
                    bytes_freed += sz
                except OSError:
                    pass

    # 6. 렌더링된 일일 PDF 리포트 및 구형 중복 엑셀 스냅샷 14일 TTL 정리
    for rep_dir in (root / "reports", results_dir / "reports"):
        if not rep_dir.exists():
            continue
        for rep_file in list(rep_dir.iterdir()):
            if not rep_file.is_file():
                continue
            if rep_file.suffix.lower() in (".pdf", ".md", ".html") and _is_older_than_days(
                rep_file, report_retention_days, now_val, ref_date
            ):
                try:
                    sz = rep_file.stat().st_size
                    rep_file.unlink()
                    purged_count += 1
                    bytes_freed += sz
                except OSError:
                    pass

    corp_dir = results_dir / "corporate_ledger"
    if corp_dir.exists():
        for corp_file in list(corp_dir.iterdir()):
            if not corp_file.is_file() or corp_file.name in PROTECTED_SSOT_FILES:
                continue
            if corp_file.suffix.lower() in (".xlsx", ".csv") and _is_older_than_days(
                corp_file, report_retention_days, now_val, ref_date
            ):
                try:
                    sz = corp_file.stat().st_size
                    corp_file.unlink()
                    purged_count += 1
                    bytes_freed += sz
                except OSError:
                    pass

    # 7. 일일 상세 거래 감사 로그(results/shadow_trades/*.json): 삭제 금지, 14일 경과분 .json.gz 무손실 압축!
    shadow_trades_dir = results_dir / "shadow_trades"
    if shadow_trades_dir.exists():
        for st_file in list(shadow_trades_dir.iterdir()):
            if not st_file.is_file():
                continue
            if st_file.name.endswith(".tmp") or ".tmp" in st_file.name:
                try:
                    sz = st_file.stat().st_size
                    st_file.unlink()
                    purged_count += 1
                    bytes_freed += sz
                except OSError:
                    pass
                continue
            if st_file.suffix == ".json" and _is_older_than_days(
                st_file, trade_compress_after_days, now_val, ref_date
            ):
                try:
                    before_sz = st_file.stat().st_size
                    if _compress_json_lossless(st_file):
                        gz_file = st_file.with_suffix(".json.gz")
                        after_sz = gz_file.stat().st_size if gz_file.exists() else 0
                        compressed_count += 1
                        if before_sz > after_sz:
                            bytes_freed += before_sz - after_sz
                except OSError:
                    pass

    mb_freed = bytes_freed / (1024.0 * 1024.0)
    logger.info(
        f"✨ 디스크 정리 완료: 삭제 {purged_count}개, 거래로그 무손실 압축 {compressed_count}개 "
        f"(확보 용량: {mb_freed:.2f} MB)"
    )
    return {
        "purged_count": purged_count,
        "compressed_count": compressed_count,
        "bytes_freed": bytes_freed,
    }


if __name__ == "__main__":
    purge_legacy_artifacts()

#!/usr/bin/env python3
"""
Health Check — 데이터/시스템 상태 점검
========================================

launchd로 매 30분 실행. 데이터 품질/시스템 리소스를 점검.

기능:
  1. 데이터 freshness (signal_cache 등)
  2. 디스크 용량
  3. API Circuit Breaker 상태
  4. Watchdog heartbeat 확인
  5. 결과 파일 무결성

Usage:
    python scripts/health_check.py
"""

import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

cfg = DynamicConfig()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [HEALTH] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('health_check')

_RESULTS = _PROJECT_ROOT / 'results'
_DATA = _PROJECT_ROOT / 'data'


def check_data_freshness() -> List[Dict]:
    """핵심 데이터 파일 freshness 체크.

    ★ 거래일 기반 동적 한도:
      - 거래일: max_age_hours (기본 4시간)
      - 주말/휴장: 직전 거래일 장후(16:00) 이후 경과 시간 허용
        → 금요일 20:30 갱신 → 월요일 08:00까지 정상 (60h)
    """
    issues = []
    now = datetime.now()
    max_age_hours = cfg.get('health.max_data_age_hours', 24)  # 1일 1회 갱신 기준

    # ★ 거래일 판단: 주말/휴장이면 freshness 한도를 동적 확장
    try:
        from src.utils.market_calendar import get_calendar
        cal = get_calendar()
        is_td = cal.is_trading_day(now.strftime('%Y%m%d'))
        market_status = cal.get_market_status(now)

        if not is_td or market_status['status'] == 'pre_market':
            # 비거래일 or 장전: 직전 거래일 장후(16:00)부터 현재까지 허용
            prev_td = cal.get_previous_trading_day(now.strftime('%Y%m%d'))
            prev_dt = datetime.strptime(prev_td, '%Y%m%d').replace(hour=16)
            dynamic_max = (now - prev_dt).total_seconds() / 3600 + 2  # +2h 여유
            max_age_hours = max(max_age_hours, dynamic_max)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass  # market_calendar 실패 시 기본 한도 사용

    critical_files = [
        _RESULTS / 'signal_cache.json',
        _RESULTS / 'pipeline_state.json',
        _RESULTS / 'shadow_portfolio.json',
        _DATA / 'kr_markets' / 'kr_005930.parquet',
        _RESULTS / 'models' / 'stock_ranker_ensemble.pkl',
    ]

    for f in critical_files:
        if not f.exists():
            issues.append({
                'check': 'freshness', 'file': f.name,
                'status': 'missing', 'severity': 'CRITICAL',
            })
            continue

        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        age_hours = (now - mtime).total_seconds() / 3600

        if age_hours > max_age_hours:
            issues.append({
                'check': 'freshness', 'file': f.name,
                'age_hours': round(age_hours, 1),
                'max': round(max_age_hours, 1),
                'severity': 'WARNING',
            })

    return issues


def check_disk_space() -> List[Dict]:
    """디스크 용량 체크."""
    issues = []
    min_gb = cfg.get('health.min_disk_gb', 5)

    try:
        usage = shutil.disk_usage(str(_PROJECT_ROOT))
        free_gb = usage.free / (1024 ** 3)
        if free_gb < min_gb:
            issues.append({
                'check': 'disk', 'free_gb': round(free_gb, 1),
                'min_gb': min_gb, 'severity': 'CRITICAL',
            })
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    return issues


def check_circuit_breakers() -> List[Dict]:
    """Circuit Breaker 상태 체크."""
    issues = []
    try:
        from src.infra.circuit_breaker import CircuitBreaker
        statuses = CircuitBreaker.get_all_status()
        for name, status in statuses.items():
            if status.get('state') == 'OPEN':
                issues.append({
                    'check': 'circuit_breaker', 'api': name,
                    'state': 'OPEN',
                    'failures': status.get('failures', 0),
                    'severity': 'WARNING',
                })
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        pass

    return issues


def check_watchdog_heartbeat() -> List[Dict]:
    """Watchdog이 살아있는지 확인."""
    issues = []
    hb_file = _RESULTS / 'watchdog_heartbeat.json'

    if not hb_file.exists():
        issues.append({
            'check': 'watchdog', 'status': 'no_heartbeat',
            'severity': 'WARNING',
        })
        return issues

    try:
        data = json.loads(hb_file.read_text())
        ts = datetime.fromisoformat(data.get('timestamp', ''))
        age_min = (datetime.now() - ts).total_seconds() / 60
        max_min = cfg.get('health.watchdog_max_age_minutes', 30)

        if age_min > max_min:
            issues.append({
                'check': 'watchdog', 'status': 'stale',
                'age_minutes': round(age_min, 0),
                'max': max_min, 'severity': 'WARNING',
            })
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        issues.append({
            'check': 'watchdog', 'status': 'corrupted',
            'severity': 'WARNING',
        })

    return issues


def check_file_integrity() -> List[Dict]:
    """핵심 결과 파일 JSON 무결성."""
    issues = []
    for fname in _RESULTS.glob('*.json'):
        try:
            json.loads(fname.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            issues.append({
                'check': 'integrity', 'file': fname.name,
                'status': 'corrupted', 'severity': 'CRITICAL',
            })

    return issues


def check_data_size() -> Dict:
    """데이터 디렉토리 크기."""
    try:
        total = sum(
            f.stat().st_size for f in _DATA.rglob('*') if f.is_file())
        return {'data_size_mb': round(total / (1024 ** 2), 1)}
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return {'data_size_mb': 0}


def run() -> Dict:
    """전체 헬스 체크 실행."""
    logger.info("🏥 Health Check 시작")
    all_issues = []

    all_issues.extend(check_data_freshness())
    all_issues.extend(check_disk_space())
    all_issues.extend(check_circuit_breakers())
    all_issues.extend(check_watchdog_heartbeat())
    all_issues.extend(check_file_integrity())

    n_critical = sum(1 for i in all_issues
                       if i.get('severity') == 'CRITICAL')

    result = {
        'timestamp': datetime.now().isoformat(),
        'status': 'CRITICAL' if n_critical else (
            'WARNING' if all_issues else 'HEALTHY'),
        'n_issues': len(all_issues),
        'n_critical': n_critical,
        'issues': all_issues,
        **check_data_size(),
    }

    # 결과 저장
    try:
        from src.infra.safe_io import safe_json_write
        safe_json_write(_RESULTS / 'health_check.json', result)
    except ImportError as e:
        (_RESULTS / 'health_check.json').write_text(
            json.dumps(result, indent=2, default=str))

    # 로깅
    if all_issues:
        for issue in all_issues:
            icon = '🔴' if issue.get('severity') == 'CRITICAL' else '🟡'
            logger.warning(f"  {icon} {issue}")
    else:
        logger.info("  ✅ 모든 점검 통과")

    # Critical 알림
    if n_critical:
        try:
            import subprocess
            msg = f"{n_critical} critical issues detected"
            subprocess.run([
                'osascript', '-e',
                f'display notification "{msg}" '
                f'with title "Meridian Health" subtitle "⚠️ Critical"',
            ], timeout=5, capture_output=True)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    return result


if __name__ == '__main__':
    run()

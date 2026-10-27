"""
data_nogo_store — DATA_NOGO 플래그의 교차 프로세스 영속 저장소 (Track E / W1_2)
=================================================================================

문제 (수정 전):
  - ``scripts/pipeline/sub_phases.py`` 의 모듈 변수 ``_data_nogo_assets`` 에 append 하지만,
    ``scripts/daily_pipeline.py`` 는 **자기 모듈의 별도 리스트**(항상 빈 리스트)를 읽었다.
  - 수집 phase(06:30)와 장중 phase(09:00)는 서로 다른 프로세스(cron/systemd)로도 실행되므로
    모듈 변수 공유는 원천적으로 불가능 → DATA_NOGO 는 오케스트레이터에 **절대 도달하지 못했다**.

해결:
  - ``results/data_nogo.json`` 에 날짜·TTL 과 함께 영속화 (``safe_json_update`` 교차 프로세스 락).
  - 오케스트레이터는 매 실행 시 활성(만료 전) 항목을 읽는다.

의미론:
  - kind='ticker' : 해당 종목 **신규 진입 금지**, 청산은 허용.
  - kind='macro'  : 매크로 지표 신뢰도 상실 — 어느 종목에 영향을 주는지 특정 불가하므로
                    보수적으로 **전체 신규 진입 금지(Exit-Only)**, 청산은 허용 (기존의 '파이프라인 전체
                    중단'은 청산까지 막아 위험을 키우므로 폐기).
  - 항목은 기록 시점 + TTL(기본 ``data_nogo.ttl_hours``=20h, 최대 다음 거래일 장 시작 전) 후 자동 만료.
    파일 손상/락 실패 시 읽기는 fail-closed 로 ``corrupt=True`` 를 반환하여 호출자가 판단하게 한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = _PROJECT_ROOT / 'results' / 'data_nogo.json'
DEFAULT_TTL_HOURS = 20.0
_VALID_KINDS = ('macro', 'ticker')


def _now() -> datetime:
    try:
        from src.utils.time_utils import now_kst
        return now_kst().replace(tzinfo=None)
    except Exception:  # pragma: no cover
        return datetime.now()


def _ttl_hours() -> float:
    try:
        from config.dynamic_config import DynamicConfig
        v = DynamicConfig().get('data_nogo.ttl_hours', DEFAULT_TTL_HOURS)
        v = float(v)
        return v if 0 < v <= 72 else DEFAULT_TTL_HOURS
    except Exception:
        return DEFAULT_TTL_HOURS


def record_data_nogo(asset: str, reason: str, kind: str = 'macro', ttl_hours: Optional[float] = None,
                     path: Optional[Path] = None, now: Optional[datetime] = None,
                     source: str = 'unknown') -> Dict[str, Any]:
    """DATA_NOGO 항목 기록 (idempotent: 같은 asset 은 갱신)."""
    from src.utils.file_ops import safe_json_update
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid DATA_NOGO kind={kind!r}")
    asset = str(asset or '').strip()
    if not asset:
        raise ValueError("asset required")
    p = Path(path) if path else DEFAULT_PATH
    t = now or _now()
    ttl = float(ttl_hours) if ttl_hours is not None else _ttl_hours()
    entry = {
        'asset': asset, 'kind': kind, 'reason': str(reason)[:500], 'source': source,
        'date': t.date().isoformat(), 'recorded_at': t.isoformat(),
        'expires_at': (t + timedelta(hours=ttl)).isoformat(), 'ttl_hours': ttl,
    }

    def _mut(obj):
        if not isinstance(obj, dict):
            obj = {}
        entries = obj.get('entries') if isinstance(obj.get('entries'), dict) else {}
        # 만료 항목 정리 (파일 무한 증가 방지)
        for k in list(entries):
            try:
                if datetime.fromisoformat(entries[k]['expires_at']) <= t:
                    entries.pop(k)
            except Exception:
                entries.pop(k, None)
        entries[f"{kind}:{asset}"] = entry
        obj['entries'] = entries
        obj['updated_at'] = t.isoformat()
        obj['schema'] = 'data_nogo.v1'
        return obj

    safe_json_update(p, _mut, default={})
    logger.warning(f"  [DATA_NOGO] 영속 기록: {kind}:{asset} (TTL {ttl:.0f}h) — {reason}")
    return entry


def load_active_data_nogo(path: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """활성(만료 전) DATA_NOGO 항목 조회. 파일 없음 → 빈 결과, 손상 → corrupt=True."""
    p = Path(path) if path else DEFAULT_PATH
    t = now or _now()
    out: Dict[str, Any] = {'macro': [], 'ticker': [], 'entries': [], 'corrupt': False, 'path': str(p)}
    if not p.exists():
        return out
    try:
        try:
            from src.utils.file_ops import locked_read_json
            obj = locked_read_json(p, default={})
        except ImportError:  # pragma: no cover
            obj = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        logger.error(f"  [DATA_NOGO] {p.name} 읽기 실패(fail-closed 표기): {e}")
        out['corrupt'] = True
        out['error'] = f"{type(e).__name__}: {e}"
        return out
    entries = (obj or {}).get('entries') or {}
    if not isinstance(entries, dict):
        out['corrupt'] = True
        return out
    for e in entries.values():
        try:
            if datetime.fromisoformat(str(e['expires_at'])) <= t:
                continue
        except Exception:
            out['corrupt'] = True
            continue
        kind = e.get('kind')
        if kind in _VALID_KINDS:
            out[kind].append(e['asset'])
            out['entries'].append(e)
    return out


def is_data_nogo(asset: str, kind: str = 'ticker', path: Optional[Path] = None, now: Optional[datetime] = None) -> bool:
    """특정 자산(종목 또는 매크로)이 현재 활성 DATA_NOGO 상태인지 판정."""
    target = str(asset or '').strip().upper()
    if not target:
        return False
    active = load_active_data_nogo(path=path, now=now)
    bucket = active.get(kind, []) if isinstance(active, dict) else []
    return any(str(x).strip().upper() == target for x in (bucket or []))


def clear_data_nogo(asset: str, kind: str = 'macro', path: Optional[Path] = None) -> bool:
    """수동 해제 (운영자용)."""
    from src.utils.file_ops import safe_json_update
    p = Path(path) if path else DEFAULT_PATH
    removed = {'v': False}

    def _mut(obj):
        if not isinstance(obj, dict):
            return {}
        entries = obj.get('entries') or {}
        removed['v'] = entries.pop(f"{kind}:{asset}", None) is not None
        obj['entries'] = entries
        return obj

    if p.exists():
        safe_json_update(p, _mut, default={})
    return removed['v']


def apply_data_nogo_to_run(market_data: Optional[Dict[str, Any]], legacy_flag: bool = False,
                           legacy_reason: Optional[str] = None, path: Optional[Path] = None,
                           now: Optional[datetime] = None) -> tuple:
    """StreamOrchestrator Step 0.5 의 DATA_NOGO 판정 (단일 진입점, 비중단).

    입력: market_data(dict|None), 레거시 플래그(market_data['data_nogo'] / orch.data_nogo), 영속 저장소.
    출력: (market_data, info)
      - 매크로/레거시/저장소손상 → market_data['exit_only']=True (신규 진입 전면 금지, 청산 허용)
      - 종목 단위 → market_data['data_nogo_tickers']=[...] (해당 종목 신규 진입 금지 계약;
        최종 집행 게이트가 이 키를 소비해야 함)
    절대로 run 전체를 중단(halt)시키지 않는다 — 청산 경로 보존.
    """
    md = market_data if isinstance(market_data, dict) else {}
    legacy = bool(legacy_flag) or bool(md.get('data_nogo', False))
    active = load_active_data_nogo(path=path, now=now)
    reasons = []
    if legacy:
        reasons.append(f"legacy:{legacy_reason or md.get('data_nogo_reason') or 'unknown'}")
    if active['macro']:
        reasons.append(f"macro:{','.join(sorted(set(active['macro'])))}")
    if active['corrupt']:
        reasons.append('store_corrupt(fail-closed)')
    global_nogo = bool(legacy or active['macro'] or active['corrupt'])
    tickers = sorted(set(active['ticker']))
    if tickers and not reasons:
        reasons.append(f"ticker_entry_block:{','.join(tickers)}")
    info = {'active': bool(global_nogo or tickers), 'mode': None, 'reason': '; '.join(reasons) or None,
            'macro_assets': sorted(set(active['macro'])), 'tickers': tickers,
            'store_corrupt': active['corrupt'], 'legacy_flag': legacy}
    if global_nogo:
        md['exit_only'] = True
        md['data_nogo'] = True
        md['data_nogo_reason'] = info['reason']
        info['mode'] = 'exit_only'
    if tickers:
        prev = md.get('data_nogo_tickers') or []
        md['data_nogo_tickers'] = sorted(set(list(prev) + tickers))
        info['mode'] = info['mode'] or 'ticker_entry_block'
    return md, info

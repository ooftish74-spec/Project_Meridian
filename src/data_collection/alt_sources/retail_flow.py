"""
src/data_collection/alt_sources/retail_flow.py
================================================
Project Meridian — Retail Flow & Dark Pool Monitor
====================================================
[Phase 45: Alternative Data Expansion]

SqueezeMetrics 공개 CSV를 통해 시장 조성자/다크풀 지표를 수집합니다.
SPY/시장 전체의 숨겨진 수급 변화를 포착하는 선행 지표로 활용합니다.

데이터 소스:
    - DIX (Dark Index):  S&P500 다크풀 매수 비중 (%)
      URL: https://squeezemetrics.com/monitor/static/DIX.csv
      의미: DIX ↑ → 기관의 조용한 매수 (반등 선행)
            DIX ↓ → 기관 이탈 (추가 하락 선행)

    - GEX (Gamma Exposure): 마켓메이커 감마 익스포저 (단위: $10억)
      URL: https://squeezemetrics.com/monitor/static/GEX.csv
      의미: GEX ↑ → 마켓메이커가 변동성 흡수 (안정)
            GEX ↓ (음수 심화) → 변동성 증폭 위험

출력 피처:
    flow_dix_latest:           최신 DIX (%)
    flow_dix_5d_avg:           5일 이동평균 DIX
    flow_dix_20d_avg:          20일 이동평균 DIX
    flow_dix_signal:           DIX vs 5d평균 차이 (모멘텀)
    flow_dix_mom_delta:        전월 대비 DIX 변화 (%p)
    flow_gex_latest:           최신 GEX ($Bn)
    flow_gex_negative_flag:    GEX 음수 여부 (0/1) — 폭발성 변동성 경고
    flow_gex_5d_avg:           5일 평균 GEX
    flow_gex_mom_delta:        전월 대비 GEX 변화
    flow_hidden_vol_signal:    DIX·GEX 복합 숨겨진 변동성 지수 (0~100)

설계 원칙:
    - 외부 의존 없음 (requests만 사용)
    - Fail-Safe: 수집 실패 시 빈 dict 반환
    - 1일 캐시 (시장 데이터는 장중 1회 갱신)
    - logger 전용, print() 금지, except pass 금지
"""
from __future__ import annotations
import csv
import io
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CACHE_FILE = _PROJECT_ROOT / 'data' / 'alternative' / 'retail_flow_cache.json'
_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
_CACHE_TTL_SEC = 86400
_DIX_URL = 'https://squeezemetrics.com/monitor/static/DIX.csv'
_GEX_URL = 'https://squeezemetrics.com/monitor/static/GEX.csv'
_TIMEOUT_SEC = 15
_SHORT_WINDOW = 5
_LONG_WINDOW = 20

def _fetch_csv(url: str, name: str, as_of_date: Optional[date]=None) -> Optional[List[Dict[str, str]]]:
    """SqueezeMetrics CSV 다운로드 및 파싱.

    Args:
        url:  CSV 엔드포인트 URL
        name: 로그용 이름

    Returns:
        행 리스트 (최신순 정렬) 또는 None
    """
    try:
        resp = requests.get(url, timeout=_TIMEOUT_SEC, headers={'User-Agent': 'Mozilla/5.0 (compatible; ProjectMeridian/1.0)', 'Referer': 'https://squeezemetrics.com/monitor'})
        if resp.status_code == 403:
            logger.warning(f'  [RetailFlow] {name}: 403 차단 — SqueezeMetrics 접근 제한. Fail-Safe 적용.')
            return None
        if resp.status_code != 200:
            logger.warning(f'  [RetailFlow] {name}: HTTP {resp.status_code}')
            return None
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
        if not rows:
            logger.warning(f'  [RetailFlow] {name}: CSV 빈 파일')
            return None
        if as_of_date and rows:
            cutoff = as_of_date.isoformat()
            date_col = list(rows[0].keys())[0]
            rows = [r for r in rows if r.get(date_col, '9999') <= cutoff]
            if not rows:
                logger.warning(f'  [RetailFlow] {name}: as_of_date({cutoff}) 이후 행 없음')
                return None
        logger.debug(f'  [RetailFlow] {name}: {len(rows)}행 (as_of={as_of_date})')
        return rows
    except requests.exceptions.Timeout:
        logger.warning(f'  [RetailFlow] {name}: 타임아웃 ({_TIMEOUT_SEC}s)', exc_info=True)
        return None
    except requests.exceptions.ConnectionError as e:
        logger.warning(f'  [RetailFlow] {name}: 연결 오류: {e}', exc_info=True)
        return None
    except Exception as e:
        logger.error(f'  [RetailFlow] {name}: 예상치 못한 오류: {e}', exc_info=False)
        return None

def _extract_series(rows: List[Dict], value_col: str) -> List[float]:
    """CSV 행에서 숫자 시계열 추출 (NaN/빈값 제외).

    Args:
        rows:      CSV 행 리스트
        value_col: 값 열 이름

    Returns:
        float 리스트 (시간 순서)
    """
    values: List[float] = []
    for row in rows:
        raw = row.get(value_col, '').strip()
        if not raw or raw.lower() in ('nan', 'none', ''):
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values

def _moving_avg(series: List[float], window: int) -> Optional[float]:
    """최근 window개 이동평균."""
    if len(series) < window:
        return None
    return round(sum(series[-window:]) / window, 4)

def _build_dix_features(rows: List[Dict]) -> Dict[str, float]:
    """DIX 피처 계산."""
    features: Dict[str, float] = {}
    if not rows:
        return features
    sample_keys = list(rows[0].keys())
    val_col = next((k for k in sample_keys if 'dix' in k.lower() or 'dark' in k.lower()), sample_keys[1] if len(sample_keys) > 1 else None)
    if val_col is None:
        logger.warning(f'  [RetailFlow] DIX: 값 열 탐지 실패 (columns={sample_keys})')
        return features
    series = _extract_series(rows, val_col)
    if not series:
        return features
    latest = series[-1]
    features['flow_dix_latest'] = round(latest, 4)
    avg5 = _moving_avg(series, _SHORT_WINDOW)
    avg20 = _moving_avg(series, _LONG_WINDOW)
    if avg5 is not None:
        features['flow_dix_5d_avg'] = avg5
        features['flow_dix_signal'] = round(latest - avg5, 4)
    if avg20 is not None:
        features['flow_dix_20d_avg'] = avg20
    if len(series) >= 30:
        recent_avg = sum(series[-5:]) / 5
        month_ago_avg = sum(series[-30:-25]) / 5
        features['flow_dix_mom_delta'] = round(recent_avg - month_ago_avg, 4)
    return features

def _build_gex_features(rows: List[Dict]) -> Dict[str, float]:
    """GEX 피처 계산."""
    features: Dict[str, float] = {}
    if not rows:
        return features
    sample_keys = list(rows[0].keys())
    val_col = next((k for k in sample_keys if 'gex' in k.lower() or 'gamma' in k.lower()), sample_keys[1] if len(sample_keys) > 1 else None)
    if val_col is None:
        logger.warning(f'  [RetailFlow] GEX: 값 열 탐지 실패 (columns={sample_keys})')
        return features
    series = _extract_series(rows, val_col)
    if not series:
        return features
    latest = series[-1]
    features['flow_gex_latest'] = round(latest, 2)
    features['flow_gex_negative_flag'] = 1.0 if latest < 0 else 0.0
    avg5 = _moving_avg(series, _SHORT_WINDOW)
    if avg5 is not None:
        features['flow_gex_5d_avg'] = avg5
    if len(series) >= 30:
        recent_avg = sum(series[-5:]) / 5
        month_ago_avg = sum(series[-30:-25]) / 5
        features['flow_gex_mom_delta'] = round(recent_avg - month_ago_avg, 2)
    return features

def _build_composite(dix_f: Dict, gex_f: Dict) -> Dict[str, float]:
    """DIX·GEX 복합 숨겨진 변동성 지수 산출 (0~100).

    지수 해석:
        0~30:  기관 강한 매수 + GEX 안정 → 변동성 낮음 (긍정)
        50:    중립
        70~100: 기관 이탈 + GEX 음수 → 숨겨진 변동성 폭발 위험

    산출 로직:
        - DIX가 낮을수록 압력 증가 (DIX 정상 범위: 40~50%)
        - GEX 음수일수록 압력 증가
    """
    composite: Dict[str, float] = {}
    dix = dix_f.get('flow_dix_latest')
    gex = gex_f.get('flow_gex_latest')
    if dix is None and gex is None:
        return composite
    pressure = 50.0
    if dix is not None:
        dix_pressure = max(0.0, min(50.0, (45.0 - dix) * 2.5))
        pressure += dix_pressure * 0.6
    if gex is not None:
        gex_pressure = max(0.0, min(50.0, -gex / 2.0)) if gex < 0 else 0.0
        pressure += gex_pressure * 0.4
    composite['flow_hidden_vol_signal'] = round(min(100.0, max(0.0, pressure)), 2)
    return composite

class RetailFlowCollector:
    """[Phase 45] 리테일 자금 흐름 및 다크풀 수집기.

    SqueezeMetrics 공개 CSV → DIX/GEX → 숨겨진 변동성 시그널.
    403 차단 또는 연결 실패 시 Fail-Safe (빈 dict 반환).
    """

    def collect(self, target_date: Optional[date]=None) -> Dict[str, float]:
        """[Phase 46: Timemachine] DIX·GEX 다크풀 지표 수집.

        Args:
            target_date: 백테스트 기준일. Future Leakage 차단.

        Returns:
            {feature_name: value} — 실패 시 빈 dict
        """
        cached = _load_cache(target_date)
        if cached is not None:
            return cached
        features: Dict[str, float] = {}
        dix_rows = _fetch_csv(_DIX_URL, 'DIX', as_of_date=target_date)
        dix_features: Dict[str, float] = {}
        if dix_rows is not None:
            dix_features = _build_dix_features(dix_rows)
            features.update(dix_features)
            logger.info(f'  [RetailFlow] DIX: {dix_features.get('flow_dix_latest', 'N/A')} (5d={dix_features.get('flow_dix_5d_avg', 'N/A')})')
        gex_rows = _fetch_csv(_GEX_URL, 'GEX', as_of_date=target_date)
        gex_features: Dict[str, float] = {}
        if gex_rows is not None:
            gex_features = _build_gex_features(gex_rows)
            features.update(gex_features)
            logger.info(f'  [RetailFlow] GEX: {gex_features.get('flow_gex_latest', 'N/A')} (neg={gex_features.get('flow_gex_negative_flag', 'N/A')})')
        composite = _build_composite(dix_features, gex_features)
        features.update(composite)
        if features:
            _save_cache(features, target_date)
            logger.info(f'  [RetailFlow] 수집 완료: {len(features)}개 피처 (HiddenVol={features.get('flow_hidden_vol_signal', 'N/A')})')
        else:
            logger.warning('  [RetailFlow] DIX·GEX 모두 수집 실패 — SqueezeMetrics 접근 불가 (Fail-Safe 적용)')
        return features

def _load_cache(target_date: Optional[date]=None) -> Optional[Dict]:
    """[Phase 46] 캐시 로드 — 날짜별 키 분리 (TTL 체크)."""
    _cf = _CACHE_FILE.parent / (f'retail_flow_{target_date}.json' if target_date else _CACHE_FILE.name)
    try:
        if not _cf.exists():
            return None
        data = json.loads(_cf.read_text(encoding='utf-8'))
        ts = datetime.fromisoformat(data.get('timestamp', '2000-01-01'))
        if target_date or (datetime.now() - ts).total_seconds() < _CACHE_TTL_SEC:
            logger.debug(f'  [RetailFlow] 캐시 사용 (as_of={target_date})')
            return data.get('features', {})
        return None
    except Exception as e:
        logger.error(f'  [RetailFlow] 캐시 로드 실패: {e}', exc_info=True)
        return None

def _save_cache(features: Dict, target_date: Optional[date]=None) -> None:
    """[Phase 46] 캐시 저장 — 날짜별 키 분리."""
    _cf = _CACHE_FILE.parent / (f'retail_flow_{target_date}.json' if target_date else _CACHE_FILE.name)
    try:
        _cf.write_text(json.dumps({'timestamp': datetime.now().isoformat(), 'features': features}, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        logger.error(f'  [RetailFlow] 캐시 저장 실패: {e}', exc_info=True)
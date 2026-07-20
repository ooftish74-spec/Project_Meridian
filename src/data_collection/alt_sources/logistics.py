"""
src/data_collection/alt_sources/logistics.py
==============================================
Project Meridian — Logistics & Supply Chain Alt Data Source
==============================================================
[Phase 45: Alternative Data Expansion]

미국 연준(FRED) 무료 API를 활용한 글로벌 공급망·물류 지표 수집기.

수집 지표 (SCFI/BDI 무료 대체재):
    - TRUCKD11:  미국 트럭 화물량 지수 (ATA Truck Tonnage Index)
    - CSCICP03USM665S: 미국 소비자 신뢰 지수 (Conference Board)
    - AISRSA: 미국 자동차 재고/판매 비율
    - DGORDER: 미국 내구재 주문 (Durable Goods Orders)
    - NEWORDER: 제조업 신규 수주
    - GACDFSA066MSFRBNY: 뉴욕 연준 GSCPI (글로벌 공급망 압력 지수)
    - DREWRY_WCI_COMPOSITE: WCI 복합 컨테이너 운임 (Proxy via FRED)

설계 원칙:
    - FRED API Key 없이도 동작 (requests 직접 호출)
    - 실패 시 NaN 반환 (Fail-Safe), 시스템 중단 없음
    - logger 전용, print() 금지, except pass 금지

FRED API Base: https://fred.stlouisfed.org/graph/fredgraph.csv
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# FRED CSV 엔드포인트 (API Key 불필요 — 공개 데이터)
_FRED_CSV_BASE = 'https://fred.stlouisfed.org/graph/fredgraph.csv'

# 수집 대상 시리즈 ID → Feature 이름 매핑
_SERIES_MAP: Dict[str, str] = {
    'TRUCKD11':           'logistics_truck_tonnage',        # 트럭 화물량 지수
    'DGORDER':            'logistics_durable_goods_orders',  # 내구재 주문 (MoM%)
    'NEWORDER':           'logistics_mfg_new_orders',        # 제조업 신규 수주
    'AISRSA':             'logistics_auto_inventory_ratio',  # 자동차 재고비율
    'GACDFSA066MSFRBNY':  'macro_gscpi',                     # 뉴욕 연준 GSCPI
    'CSCICP03USM665S':    'logistics_consumer_confidence',   # 소비자 신뢰
}

# 요청 타임아웃 (초)
_TIMEOUT_SEC = 10

# 최근 데이터 취득 기간 (일)
_LOOKBACK_DAYS = 90


def _fetch_fred_series(series_id: str, as_of_date: Optional[date] = None) -> Optional[float]:
    """FRED에서 단일 시리즈의 최신값 취득.

    Args:
        series_id: FRED 시리즈 ID (예: 'TRUCKD11')

    Returns:
        최신 값 (float) 또는 None (실패 시)
    """
    _ref = datetime.combine(as_of_date, datetime.min.time()) if as_of_date else datetime.now()
    start_date = (_ref - timedelta(days=_LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    end_date   = _ref.strftime('%Y-%m-%d')  # 타임머신: as_of_date 이후 데이터 차단

    try:
        resp = requests.get(
            _FRED_CSV_BASE,
            params={'id': series_id, 'vintage_date': end_date, 'observation_start': start_date},
            timeout=_TIMEOUT_SEC,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; ProjectMeridian/1.0)'},
        )

        if resp.status_code != 200:
            logger.warning(f'  [Logistics] FRED {series_id} HTTP {resp.status_code}')
            return None

        # CSV 파싱: 마지막 유효 행의 값
        lines = [ln.strip() for ln in resp.text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            logger.warning(f'  [Logistics] FRED {series_id}: 데이터 없음')
            return None

        # 헤더 제외, 마지막 유효 데이터 행 (빈값/. 제외)
        values = []
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) >= 2:
                raw = parts[1].strip()
                if raw and raw != '.' and raw.lower() != 'nan':
                    try:
                        values.append(float(raw))
                    except ValueError:
                        continue

        if not values:
            logger.warning(f'  [Logistics] FRED {series_id}: 파싱 가능 값 없음')
            return None

        latest = values[-1]
        logger.debug(f'  [Logistics] FRED {series_id}: {latest:.4f} ({len(values)}개)')
        return latest

    except requests.exceptions.Timeout:
        logger.warning(f'  [Logistics] FRED {series_id}: 타임아웃 ({_TIMEOUT_SEC}s)', exc_info=True)
        return None
    except requests.exceptions.ConnectionError as e:
        logger.warning(f'  [Logistics] FRED {series_id}: 연결 오류: {e}', exc_info=True)
        return None
    except Exception as e:
        logger.error(f'  [Logistics] FRED {series_id}: 예상치 못한 오류: {e}', exc_info=False)
        return None


def _calc_mom_change(series_id: str, as_of_date: Optional[date] = None) -> Optional[float]:
    """FRED 시리즈의 전월 대비 변화율(MoM %) 계산.

    Args:
        series_id: FRED 시리즈 ID

    Returns:
        MoM 변화율 (%) 또는 None
    """
    _ref2 = datetime.combine(as_of_date, datetime.min.time()) if as_of_date else datetime.now()
    start_date = (_ref2 - timedelta(days=180)).strftime('%Y-%m-%d')
    end_date   = _ref2.strftime('%Y-%m-%d')

    try:
        resp = requests.get(
            _FRED_CSV_BASE,
            params={'id': series_id, 'vintage_date': end_date, 'observation_start': start_date},
            timeout=_TIMEOUT_SEC,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; ProjectMeridian/1.0)'},
        )
        if resp.status_code != 200:
            return None

        lines = [ln.strip() for ln in resp.text.strip().splitlines()]
        values = []
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) >= 2:
                raw = parts[1].strip()
                if raw and raw != '.':
                    try:
                        values.append(float(raw))
                    except ValueError:
                        continue

        if len(values) >= 2:
            prev, curr = values[-2], values[-1]
            if prev != 0:
                return round((curr - prev) / abs(prev) * 100, 4)

        return None

    except Exception as e:
        logger.error(f'  [Logistics] MoM 계산 실패 {series_id}: {e}', exc_info=False)
        return None


class LogisticsCollector:
    """[Phase 45] 물류·공급망 대안 데이터 수집기.

    FRED 무료 공개 API를 통해 글로벌 공급망 압력 및 물류 지표 수집합니다.
    모든 실패는 Fail-Safe (0.0 반환 또는 스킵)으로 처리되며 시스템을 중단시키지 않습니다.
    """

    def collect(self, target_date: Optional[date] = None) -> Dict[str, float]:
        """[Phase 46: Timemachine] FRED 물류·공급망 지표 수집.

        Args:
            target_date: 백테스트 기준일 (None이면 현재). Future Leakage 차단.

        Returns:
            {feature_name: value} — 실패한 항목은 제외
        """
        features: Dict[str, float] = {}
        collected = 0

        # ── 1. 최신값 수집 ──────────────────────────────────────────────
        for series_id, feat_name in _SERIES_MAP.items():
            val = _fetch_fred_series(series_id, as_of_date=target_date)
            if val is not None:
                features[feat_name] = val
                collected += 1

        # ── 2. 핵심 시리즈 MoM 변화율 추가 ────────────────────────────
        _mom_targets = {
            'TRUCKD11':  'logistics_truck_tonnage_mom_pct',
            'DGORDER':   'logistics_durable_goods_mom_pct',
            'NEWORDER':  'logistics_new_orders_mom_pct',
        }
        for series_id, feat_name in _mom_targets.items():
            mom = _calc_mom_change(series_id, as_of_date=target_date)
            if mom is not None:
                features[feat_name] = mom
                collected += 1

        # ── 3. 복합 공급망 압력 스코어 (단순 평균) ─────────────────────
        if features.get('macro_gscpi') is not None:
            # GSCPI: 음수 = 완화, 양수 = 압박
            gscpi = features['macro_gscpi']
            features['logistics_supply_pressure'] = round(
                max(0.0, min(100.0, 50.0 + gscpi * 10.0)), 2
            )
            collected += 1

        logger.info(
            f'  [Logistics] FRED 수집 완료: {collected}건 '
            f'(GSCPI={features.get("macro_gscpi", "N/A")}, '
            f'TruckTonnage={features.get("logistics_truck_tonnage", "N/A")})'
        )
        return features

#!/usr/bin/env python3
"""[Phase 55: SS-ETF Pipeline Glue] SS-ETF 단일종목 파생 리스크 JSON 생성기.

대시보드 Risk 탭의 "데이터 수집 대기 중" 배너를 제거하고
실지 Wag-the-Dog 리스크 지표를 표시하기 위해 다음을 수행한다:
  1. SSETFFeatureEngine으로 삼성전자(005930) / SK하이닉스(000660) 리스크 계산
  2. results/ss_etf_risk.json 생성

실행 시점: 한국 장 마감 후 (17:30 KST) daily_pipeline._phase_evening_data() 에서 호출
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('generate_ss_etf_risk')


def generate_ss_etf_risk_json() -> dict:
    """SSETFFeatureEngine을 호출하여 results/ss_etf_risk.json을 생성한다."""
    logger.info('  📊 [Phase 55] SS-ETF 단일종목 파생 리스크 수집 시작')

    try:
        from src.data_collection.ss_etf_feature_engine import SSETFFeatureEngine
        engine = SSETFFeatureEngine()
    except Exception as _e:
        logger.error(f'  ❌ SSETFFeatureEngine 임포트 실패: {_e}')
        return {}

    sam_feat: dict = {}
    try:
        sam_feat = engine.compute('005930') or {}
        logger.info(
            f'  삼성전자: vol_ratio={sam_feat.get("ss_etf_vol_ratio", 0):.4f} '
            f'lp_pressure={sam_feat.get("lp_delta_pressure", 0):.2f}')
    except Exception as _e:
        logger.warning(f'  ⚠️ 삼성전자 Feature 계산 실패: {_e}')

    hyn_feat: dict = {}
    try:
        hyn_feat = engine.compute('000660') or {}
        logger.info(
            f'  SK하이닉스: vol_ratio={hyn_feat.get("ss_etf_vol_ratio", 0):.4f} '
            f'lp_pressure={hyn_feat.get("lp_delta_pressure", 0):.2f}')
    except Exception as _e:
        logger.warning(f'  ⚠️ SK하이닉스 Feature 계산 실패: {_e}')

    _VOL_THRESHOLD = 0.30
    sam_vol = float(sam_feat.get('ss_etf_vol_ratio', 0.0))
    hyn_vol = float(hyn_feat.get('ss_etf_vol_ratio', 0.0))

    risk_data = {
        'samsung': {
            'vol_ratio':   sam_vol,
            'lp_pressure': float(sam_feat.get('lp_delta_pressure', 0.0)),
            'vol_anomaly': float(sam_feat.get('intraday_vol_anomaly', 0.0)),
            'status':      '주의' if sam_vol >= _VOL_THRESHOLD else '정상',
        },
        'hynix': {
            'vol_ratio':   hyn_vol,
            'lp_pressure': float(hyn_feat.get('lp_delta_pressure', 0.0)),
            'vol_anomaly': float(hyn_feat.get('intraday_vol_anomaly', 0.0)),
            'status':      '주의' if hyn_vol >= _VOL_THRESHOLD else '정상',
        },
        'combined_warning': sam_vol >= _VOL_THRESHOLD or hyn_vol >= _VOL_THRESHOLD,
        'vol_threshold':    _VOL_THRESHOLD,
        'source':           'generate_ss_etf_risk.py',
        'timestamp':        datetime.now().isoformat(),
    }

    out_path = ROOT / 'results' / 'ss_etf_risk.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(risk_data, indent=2, ensure_ascii=False),
        encoding='utf-8',
    )

    _flag = '🚨 Wag-the-Dog 경보!' if risk_data['combined_warning'] else '✅ 정상'
    logger.info(f'  [Phase 55] SS-ETF 리스크 SSoT 갱신 완료: {out_path} [{_flag}]')
    return risk_data


if __name__ == '__main__':
    result = generate_ss_etf_risk_json()
    if result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        sys.exit(1)

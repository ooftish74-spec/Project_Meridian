"""
Earnings Surprise Feature — DART 재무제표 기반 서프라이즈 피처
================================================================
4 피처: earnings_surprise, revenue_yoy, earnings_qoq, earnings_momentum

Usage:
    from src.data_collection.earnings_surprise import EarningsSurprise
    es = EarningsSurprise()
    features = es.get_features('005930')
"""

import json, logging, os
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EARN_DIR = _PROJECT_ROOT / 'data' / 'earnings'
_EARN_DIR.mkdir(parents=True, exist_ok=True)


class EarningsSurprise:
    """DART 재무제표 기반 Earnings Surprise 피처."""

    DART_API_URL = 'https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json'

    def __init__(self):
        from src.utils.credential_manager import CredentialManager
        self.api_key = CredentialManager().read_from_env('DART_API_KEY') or ''

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def get_features(self, ticker: str) -> Dict[str, float]:
        """4개 서프라이즈 피처 반환.

        Returns:
            {'earnings_surprise': float, 'revenue_yoy': float,
             'earnings_qoq': float, 'earnings_momentum': float}
        """
        cache = self._load_cache(ticker)
        if cache:
            return cache

        # DART API 없으면 0 반환
        if not self.is_available:
            return self._default_features()

        try:
            return self._compute_from_dart(ticker)
        except Exception as e:
            logger.error(f"Earnings surprise 계산 실패 ({ticker}): {e}", exc_info=True)
            return self._default_features()

    def _compute_from_dart(self, ticker: str) -> Dict[str, float]:
        """DART API에서 재무제표 로드 후 서프라이즈 계산."""
        import requests

        corp_code = self._get_corp_code(ticker)
        if not corp_code:
            return self._default_features()

        # 최근 4분기 영업이익 수집
        quarters = []
        year = datetime.now().year
        for y in range(year, year - 3, -1):
            for rpt in ['11013', '11012', '11014', '11011']:
                try:
                    resp = requests.get(self.DART_API_URL, params={
                        'crtfc_key': self.api_key,
                        'corp_code': corp_code,
                        'bsns_year': str(y),
                        'reprt_code': rpt,
                        'fs_div': 'OFS',
                    }, timeout=5)
                    data = resp.json()
                    if data.get('status') == '000':
                        for item in data.get('list', []):
                            if item.get('account_nm') == '영업이익':
                                val = float(item.get('thstrm_amount', '0').replace(',', ''))
                                quarters.append({'year': y, 'rpt': rpt, 'value': val})
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue

        if len(quarters) < 4:
            result = self._default_features()
        else:
            q = sorted(quarters, key=lambda x: (x['year'], x['rpt']))
            latest = q[-1]['value']
            prev_q = q[-2]['value']
            prev_y = q[-5]['value'] if len(q) >= 5 else q[-4]['value']

            # 서프라이즈: 실적 vs 추세 (4분기 평균 대비)
            avg_4q = np.mean([x['value'] for x in q[-4:]])
            std_4q = np.std([x['value'] for x in q[-4:]]) or 1

            result = {
                'earnings_surprise': float((latest - avg_4q) / std_4q),
                'revenue_yoy': float((latest / prev_y - 1) * 100) if prev_y != 0 else 0,
                'earnings_qoq': float((latest / prev_q - 1) * 100) if prev_q != 0 else 0,
                'earnings_momentum': float(sum(1 if q[i]['value'] > q[i-1]['value'] else -1
                                               for i in range(-3, 0))),
            }

        self._save_cache(ticker, result)
        return result

    def _default_features(self) -> Dict[str, float]:
        return {
            'earnings_surprise': 0.0,
            'revenue_yoy': 0.0,
            'earnings_qoq': 0.0,
            'earnings_momentum': 0.0,
        }

    def _get_corp_code(self, ticker: str) -> Optional[str]:
        try:
            from src.data_collection.dart_daily_collector import DARTDailyCollector
            dc = DARTDailyCollector()
            return dc._corp_codes.get(ticker)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return None

    def _load_cache(self, ticker: str) -> Optional[Dict]:
        path = _EARN_DIR / f'{ticker}.json'
        if path.exists():
            try:
                data = json.loads(path.read_text())
                # 30일 이내면 캐시 사용
                cached_date = datetime.fromisoformat(data.get('date', '2000-01-01'))
                if (datetime.now() - cached_date).days < 30:
                    return data.get('features', self._default_features())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning("[SILENT_BYPASS] Suppressed exception at earnings_surprise.py:144", exc_info=True)
        return None

    def _save_cache(self, ticker: str, features: Dict):
        path = _EARN_DIR / f'{ticker}.json'
        atomic_write_json(path, {
            'date': datetime.now().isoformat(),
            'ticker': ticker,
            'features': features,
        }, indent=2)

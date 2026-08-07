#!/usr/bin/env python3
"""
매크로 선행 지표 수집 데몬 — fdr 기반 무료 API
================================================================
[Phase 10: Alpha Breakthrough] Phase 10-C: 지능의 확장

수집 대상:
  - HG=F    구리 선물 (경기 선행 지표)
  - DX-Y.NYB 달러 인덱스 (글로벌 유동성 리스크)
  - ^BDI    BDI 해운지수 (또는 BDRY ETF 프록시)
  - NQ=F    나스닥 100 선물 (S5 야간 필터용)

결과 저장:
  results/macro_proxy.json

Cache:
  30분 TTL — Rate Limit 방어

Usage (standalone):
    python3 scripts/macro_proxy_collector.py

Usage (임베디드):
    from scripts.macro_proxy_collector import MacroProxyCollector
    collector = MacroProxyCollector()
    data = collector.collect()
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

_RESULTS = _PROJECT_ROOT / 'results'
_CACHE_FILE = _RESULTS / 'macro_proxy.json'

# [Phase 10: Alpha Breakthrough] 수집 대상 심볼 정의
MACRO_SYMBOLS = {
    'copper':        {'symbol': 'HG=F',     'name': '구리 선물',   'unit': '$/lb'},
    'dollar_index':  {'symbol': 'DX-Y.NYB', 'name': '달러 인덱스', 'unit': 'index'},
    'bdi_proxy':     {'symbol': 'BDRY',     'name': 'BDI ETF(Breakwave 드라이 벌크)', 'unit': 'USD'},
    'nq_futures':    {'symbol': 'NQ=F',     'name': '나스닥100 선물', 'unit': 'points'},
    'wti_crude':     {'symbol': 'CL=F',     'name': 'WTI 원유',    'unit': '$/bbl'},
}

# 섹터별 매크로 영향 매핑 (S3 팩터 통합용)
MACRO_SECTOR_IMPACT = {
    'copper':       {
        'semiconductor': +0.6,  # 설비투자 사이클
        'battery':       +0.7,  # 동 소재
        'industrial':    +0.5,
        'construction':  +0.4,
    },
    'dollar_index': {
        'semiconductor': -0.5,  # 달러 강세 → 수출주 불리
        'us_tech':       -0.4,
        'us_broad':      -0.3,
        'us_semiconductor': -0.4,
        'finance':       +0.2,  # 달러 강세 → 금리 상승 → 금융주 유리
    },
    'bdi_proxy':    {
        'industrial':    +0.6,  # 해운/물류
        'auto':          +0.3,  # 완성차 수출
        'construction':  +0.3,
    },
    'wti_crude':    {
        'energy':        +0.7,  # 에너지 직접 노출
        'industrial':    -0.2,  # 원가 부담
        'construction':  -0.1,
    },
}

# [Phase 10: Alpha Breakthrough] 캐시 TTL (초)
_CACHE_TTL_SEC = 30 * 60  # 30분


class MacroProxyCollector:
    """fdr 기반 매크로 선행 지표 수집기.

    [Phase 10: Alpha Breakthrough]
    구리/달러/BDI/NQ 선물을 수집하여 S3 섹터 로테이션 스코어에
    모멘텀 팩터로 주입하고, S5 야간 필터에도 활용.

    Rate Limit 방어:
      - 30분 TTL 캐시
      - 종목 간 1초 sleep
      - Timeout 10초
    """

    def __init__(self, cache_ttl_sec: int = _CACHE_TTL_SEC):
        self.cache_ttl_sec = cache_ttl_sec
        self._cache: Optional[Dict] = None
        self._cache_ts: Optional[datetime] = None

    def _is_cache_fresh(self) -> bool:
        """캐시가 유효한지 확인."""
        if self._cache is None or self._cache_ts is None:
            return False
        return (datetime.now() - self._cache_ts).total_seconds() < self.cache_ttl_sec

    def _load_file_cache(self) -> Optional[Dict]:
        """파일 캐시에서 로드 (프로세스 재시작 후에도 유효)."""
        try:
            if not _CACHE_FILE.exists():
                return None
            data = json.loads(_CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(data.get('cached_at', '2000-01-01'))
            age_sec = (datetime.now() - cached_at).total_seconds()
            if age_sec < self.cache_ttl_sec:
                return data
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return None

    def _fetch_symbol(self, key: str, config: Dict) -> Optional[Dict]:
        """fdr로 단일 심볼 수집.

        [Phase 10: Alpha Breakthrough]
        1일~5일치 데이터를 받아 당일 종가와 5일 모멘텀 산출.
        """
        symbol = config['symbol']
        try:
            import FinanceDataReader as fdr
            fdr_map = {'HG=F': 'HG', 'DX-Y.NYB': 'DX', 'NQ=F': 'US100', 'CL=F': 'CL', 'BDRY': 'BDRY'}
            fdr_symbol = fdr_map.get(symbol, symbol)
            
            start_dt = (datetime.now() - timedelta(days=20)).strftime('%Y-%m-%d')
            hist = fdr.DataReader(fdr_symbol, start=start_dt)

            if hist is None or hist.empty:
                logger.debug(f'  [MacroProxy] {symbol}: 빈 데이터')
                return None

            # 최신 2일치 → 당일 등락률 계산
            if 'Close' in hist.columns:
                closes = hist['Close'].dropna()
            else:
                closes = hist.iloc[:, 0].dropna()
            if len(closes) < 2:
                return None

            latest_close = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            chg_1d = (latest_close / prev_close - 1) * 100 if prev_close > 0 else 0.0

            # 5일 모멘텀 (5일치 이상 있으면)
            chg_5d = 0.0
            if len(closes) >= 6:
                close_5d_ago = float(closes.iloc[-6])
                chg_5d = (latest_close / close_5d_ago - 1) * 100 if close_5d_ago > 0 else 0.0

            return {
                'symbol': symbol,
                'name': config['name'],
                'unit': config['unit'],
                'close': round(latest_close, 4),
                'chg_1d_pct': round(chg_1d, 4),
                'chg_5d_pct': round(chg_5d, 4),
                'date': hist.index[-1].strftime('%Y-%m-%d'),
            }
        except Exception as e:
            logger.debug(f'  [MacroProxy] {symbol} 수집 실패: {e}')
            return None

    def _compute_sector_scores(self, raw_data: Dict) -> Dict[str, float]:
        """매크로 데이터 → 섹터별 추가 스코어 변환.

        [Phase 10: Alpha Breakthrough]
        각 매크로 지표의 5일 모멘텀에 섹터 임팩트 가중치를 곱해 합산.
        결과: {sector: 0.0~1.0 score (Sigmoid 정규화 전)}

        S3 _load_sector_macro_adjustments()가 이 파일을 읽어 활용.
        """
        raw_sector_scores: Dict[str, float] = {}

        for macro_key, impact_map in MACRO_SECTOR_IMPACT.items():
            macro_item = raw_data.get(macro_key)
            if not macro_item:
                continue
            # 5일 모멘텀 기반 방향성 (-100~+100 → -1~+1 정규화)
            mom5 = macro_item.get('chg_5d_pct', 0.0)
            norm_mom = max(-1.0, min(1.0, mom5 / 5.0))  # ÷5: ±5% = ±1.0

            for sector, weight in impact_map.items():
                raw_sector_scores[sector] = (
                    raw_sector_scores.get(sector, 0.0) + norm_mom * weight
                )

        # Sigmoid 정규화 → 0~1
        import math
        sector_scores = {}
        for sector, raw in raw_sector_scores.items():
            sigmoid = 1.0 / (1.0 + math.exp(-raw))
            sector_scores[sector] = round(sigmoid, 4)

        return sector_scores

    def collect(self, force: bool = False) -> Dict:
        """매크로 데이터 수집 및 캐시 저장.

        [Phase 10: Alpha Breakthrough]

        Args:
            force: True면 캐시 무시하고 강제 수집

        Returns:
            {
                'cached_at': str,
                'copper': {...},
                'dollar_index': {...},
                'bdi_proxy': {...},
                'nq_futures': {...},
                'wti_crude': {...},
                'sector_scores': {sector: 0.0~1.0},
                'nq_chg_1d_pct': float,  # S5 필터용 빠른 접근
            }
        """
        # 인메모리 캐시 확인
        if not force and self._is_cache_fresh():
            return self._cache  # type: ignore

        # 파일 캐시 확인 (30분 TTL)
        if not force:
            file_cache = self._load_file_cache()
            if file_cache:
                self._cache = file_cache
                self._cache_ts = datetime.now()
                logger.debug('  [MacroProxy] 파일 캐시 사용')
                return file_cache

        logger.info('  🌐 [Phase 10: Alpha Breakthrough] 매크로 선행 지표 수집 시작...')
        result: Dict = {
            'cached_at': datetime.now().isoformat(),
            'source': 'fdr',
        }

        for key, config in MACRO_SYMBOLS.items():
            data = self._fetch_symbol(key, config)
            if data:
                result[key] = data
                logger.info(
                    f'  ✅ {config["name"]}({config["symbol"]}): '
                    f'${data["close"]} ({data["chg_1d_pct"]:+.2f}% 1일, '
                    f'{data["chg_5d_pct"]:+.2f}% 5일)'
                )
            else:
                logger.debug(f'  ⚠️ {config["name"]} 수집 실패 — 스킵')
            # [Phase 10: Alpha Breakthrough] Rate Limit 방어: 심볼 간 1초 sleep
            time.sleep(1.0)

        # S3용 섹터 스코어 계산
        result['sector_scores'] = self._compute_sector_scores(result)

        # S5 야간 필터용 NQ 당일 등락률 빠른 접근
        nq = result.get('nq_futures', {})
        result['nq_chg_1d_pct'] = nq.get('chg_1d_pct', 0.0)
        result['nq_chg_5d_pct'] = nq.get('chg_5d_pct', 0.0)

        # 저장
        try:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str)
            )
            logger.info(
                f'  💾 [MacroProxy] 저장 완료: {_CACHE_FILE} '
                f'({len(result.get("sector_scores", {}))}개 섹터 스코어)'
            )
        except Exception as e:
            logger.warning(f'  [MacroProxy] 저장 실패: {e}')

        # 캐시 갱신
        self._cache = result
        self._cache_ts = datetime.now()

        return result

    def get_nq_change(self) -> float:
        """NQ 선물 당일 등락률 반환 (S5 야간 필터 전용 편의 메서드).

        [Phase 10: Alpha Breakthrough]
        캐시 우선 → 파일 → 0.0 fallback.
        """
        try:
            data = self.collect()
            return float(data.get('nq_chg_1d_pct', 0.0))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return 0.0

    def get_sector_scores(self) -> Dict[str, float]:
        """S3 섹터 스코어 반환 (캐시 기반).

        [Phase 10: Alpha Breakthrough]
        S3 active_macro_stream._load_sector_macro_adjustments()에서
        alpha_signal.json 대신 이 메서드를 fallback으로 활용.
        """
        try:
            data = self.collect()
            return dict(data.get('sector_scores', {}))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {}


# ══════════════════════════════════════════════════════
# [Phase 10: Alpha Breakthrough] 편의 함수
# ══════════════════════════════════════════════════════

_collector_singleton: Optional[MacroProxyCollector] = None


def get_collector() -> MacroProxyCollector:
    """전역 MacroProxyCollector 싱글톤 반환."""
    global _collector_singleton
    if _collector_singleton is None:
        _collector_singleton = MacroProxyCollector()
    return _collector_singleton


def get_nq_futures_change(force: bool = False) -> float:
    """NQ 선물 당일 등락률 반환 (외부 호출용).

    [Phase 10: Alpha Breakthrough]
    S5/MarketDataBridge에서 사용.

    Returns:
        float: 등락률 (%) — 수집 실패 시 0.0
    """
    try:
        return get_collector().get_nq_change()
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return 0.0


def get_macro_sector_scores() -> Dict[str, float]:
    """매크로 팩터 기반 섹터 스코어 반환.

    [Phase 10: Alpha Breakthrough]
    S3 섹터 로테이션에 통합.

    Returns:
        {sector: 0.0~1.0}
    """
    try:
        return get_collector().get_sector_scores()
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return {}


if __name__ == '__main__':
    result = MacroProxyCollector().collect(force=True)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

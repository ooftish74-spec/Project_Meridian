"""
Project_First — Factor Integrator
====================================
AQR 스타일 팩터 기반 종합 시그널 통합.
Momentum + Value + Quality + Flow 4개 팩터 Z-Score 합성.
IC 기반 동적 가중치 (SelfLearning 연동).

Usage:
    from src.intelligence.factor_integrator import FactorIntegrator
    fi = FactorIntegrator()
    scores = fi.compute_composite(universe=['005930', '000660', ...])
"""

import json, logging
import numpy as np
import pandas as pd
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = cfg.project_root()


class FactorIntegrator:
    """AQR 스타일 멀티팩터 통합기.

    4개 팩터:
      1. Momentum — 추세 추종 (1M, 3M, 12M-1M)
      2. Value — 저평가 (PER, PBR, 배당)
      3. Quality — 우량 (ROE, 부채비율)
      4. Flow — 수급 (외국인/기관)

    가중치는 IC 기반으로 동적 조정.
    """

    DEFAULT_WEIGHTS = {
        'momentum': 0.30,
        'value': 0.20,
        'quality': 0.20,
        'flow': 0.30,
    }

    def __init__(self):
        self._weights = self._load_weights()

    def compute_composite(self, universe: List[str] = None,
                          sector_scores: Dict[str, float] = None) -> List[Dict]:
        """종합 팩터 스코어 산출.

        Returns:
            [{'ticker': str, 'composite': float, 'factors': dict}, ...]
            composite 내림차순 정렬
        """
        tickers = universe or self._get_universe()
        results = []

        for ticker in tickers:
            factors = {}

            # 1. Momentum
            mom = self._compute_momentum(ticker)
            if mom is not None:
                factors['momentum'] = mom

            # 2. Value
            val = self._compute_value(ticker)
            if val is not None:
                factors['value'] = val

            # 3. Quality
            qual = self._compute_quality(ticker)
            if qual is not None:
                factors['quality'] = qual

            # 4. Flow
            flow = self._compute_flow(ticker)
            if flow is not None:
                factors['flow'] = flow

            if not factors:
                continue

            # 종합 스코어 = 가중 합산
            composite = sum(
                self._weights.get(k, 0.25) * v
                for k, v in factors.items()
            )

            # 섹터 오버레이
            if sector_scores:
                sector = self._get_sector(ticker)
                if sector and sector in sector_scores:
                    sector_adj = (sector_scores[sector] - 0.5) * 0.15
                    composite += sector_adj

            results.append({
                'ticker': ticker,
                'name': self._get_name(ticker),
                'composite': round(composite, 4),
                'factors': {k: round(v, 4) for k, v in factors.items()},
                'sector': self._get_sector(ticker) or 'unknown',
            })

        # Z-Score 정규화
        if results:
            composites = np.array([r['composite'] for r in results])
            if composites.std() > 0:
                z_scores = (composites - composites.mean()) / composites.std()
                for i, r in enumerate(results):
                    r['z_score'] = round(float(z_scores[i]), 4)
            else:
                for r in results:
                    r['z_score'] = 0.0

        results.sort(key=lambda x: x['composite'], reverse=True)
        return results

    def _compute_momentum(self, ticker: str) -> Optional[float]:
        """모멘텀 팩터: 1M + 3M + (12M - 1M) 복합."""
        price = self._read_price(ticker)
        if price is None or len(price) < 250:
            if price is not None and len(price) >= 20:
                close = price.values
                ret_1m = (close[-1] / close[-20] - 1) if close[-20] > 0 else 0
                return float(ret_1m)
            return None

        close = price.values
        ret_1m = close[-1] / close[-20] - 1 if close[-20] > 0 else 0
        ret_3m = close[-1] / close[-60] - 1 if close[-60] > 0 else 0
        ret_12m = close[-1] / close[-250] - 1 if close[-250] > 0 else 0
        # 12M - 1M (Jegadeesh-Titman 모멘텀)
        mom_12_1 = ret_12m - ret_1m

        return float(0.3 * ret_1m + 0.3 * ret_3m + 0.4 * mom_12_1)

    def _compute_value(self, ticker: str) -> Optional[float]:
        """밸류 팩터: PER 역수 + PBR 역수."""
        # 펀더멘탈 데이터 로드
        fund = self._load_fundamental(ticker)
        if not fund:
            return None

        per = fund.get('per', None)
        pbr = fund.get('pbr', None)

        score = 0
        count = 0
        if per and per > 0:
            # PER 역수 (낮을수록 좋음 → 역수가 클수록 좋음)
            score += min(1.0 / per, 0.2)  # 상한
            count += 1
        if pbr and pbr > 0:
            score += min(1.0 / pbr, 1.0)
            count += 1

        return float(score / max(count, 1)) if count > 0 else None

    def _compute_quality(self, ticker: str) -> Optional[float]:
        """퀄리티 팩터: ROE + 이익안정성."""
        fund = self._load_fundamental(ticker)
        if not fund:
            return None

        roe = fund.get('roe', None)
        if roe is None:
            return None

        # ROE 정규화 (0~1 범위)
        roe_score = min(max(roe / 30, 0), 1.0)  # ROE 30%+ = 1.0
        return float(roe_score)

    def _compute_flow(self, ticker: str) -> Optional[float]:
        """수급 팩터: 외국인/기관 순매수 트렌드."""
        price = self._read_price(ticker)
        if price is None or len(price) < 20:
            return None

        # 최근 20일 거래량 트렌드를 수급 프록시로 사용
        vol = self._read_volume(ticker)
        if vol is None or len(vol) < 20:
            return None

        recent_avg = float(np.mean(vol[-5:]))
        past_avg = float(np.mean(vol[-20:]))

        if past_avg > 0:
            flow_ratio = (recent_avg / past_avg) - 1  # 양수 = 수급 증가
            return float(np.clip(flow_ratio, -1, 1))
        return None

    def update_weights_from_ic(self, ic_data: Dict):
        """IC 기반 가중치 갱신 (SelfLearning 연동).

        IC가 높은 팩터에 더 큰 가중치 부여.
        """
        if not ic_data:
            return

        total_abs_ic = sum(abs(v) for v in ic_data.values() if isinstance(v, (int, float)))
        if total_abs_ic == 0:
            return

        new_weights = {}
        for factor in self.DEFAULT_WEIGHTS:
            ic_val = abs(ic_data.get(f'{factor}_ic', 0))
            # IC 비례 가중치 (최소 0.10 보장)
            new_weights[factor] = max(0.10, ic_val / total_abs_ic)

        # 정규화
        total = sum(new_weights.values())
        self._weights = {k: round(v / total, 3) for k, v in new_weights.items()}
        self._save_weights()
        logger.info(f"  팩터 가중치 갱신: {self._weights}")

    def _get_universe(self) -> List[str]:
        from config.universe import get_full_universe
        return get_full_universe()

    def _get_sector(self, ticker: str) -> Optional[str]:
        try:
            from config.universe import get_sector
            return get_sector(ticker)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return None

    def _get_name(self, ticker: str) -> str:
        try:
            names = json.loads((_PROJECT_ROOT / 'results' / 'ticker_names.json').read_text())
            return names.get(ticker, ticker)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return ticker

    def _read_price(self, ticker: str) -> Optional[pd.Series]:
        for prefix in ['kr_', '']:
            f = _PROJECT_ROOT / 'data' / 'historical_10y' / f'{prefix}{ticker}.parquet'
            if f.exists():
                try:
                    df = pd.read_parquet(f)
                    return pd.to_numeric(df['close'], errors='coerce').dropna()
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning("[SILENT_BYPASS] Suppressed exception at factor_integrator.py:252", exc_info=True)
        return None

    def _read_volume(self, ticker: str) -> Optional[np.ndarray]:
        for prefix in ['kr_', '']:
            f = _PROJECT_ROOT / 'data' / 'historical_10y' / f'{prefix}{ticker}.parquet'
            if f.exists():
                try:
                    df = pd.read_parquet(f)
                    return pd.to_numeric(df['volume'], errors='coerce').dropna().values
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning("[SILENT_BYPASS] Suppressed exception at factor_integrator.py:265", exc_info=True)
        return None

    def _load_fundamental(self, ticker: str) -> Dict:
        f = _PROJECT_ROOT / 'data' / 'fundamental' / f'{ticker}.json'
        if f.exists():
            try:
                return json.loads(f.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning("[SILENT_BYPASS] Suppressed exception at factor_integrator.py:276", exc_info=True)
        return {}

    def _load_weights(self) -> Dict:
        f = _PROJECT_ROOT / 'results' / 'factor_weights.json'
        if f.exists():
            try:
                return json.loads(f.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning("[SILENT_BYPASS] Suppressed exception at factor_integrator.py:287", exc_info=True)
        return dict(self.DEFAULT_WEIGHTS)

    def _save_weights(self):
        f = _PROJECT_ROOT / 'results' / 'factor_weights.json'
        f.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(f, self._weights, indent=2)

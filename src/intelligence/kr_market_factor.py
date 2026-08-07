"""
한국시장 수급 Factor 엔진
==============================
외국인/기관 수급 데이터를 통합하여 각 스트림에 factor 점수를 전달.
한국시장 특성:
  1) 대형주 주도 (시총 상위 10종목 = KOSPI ~70%)
  2) 외국인 순매수/매도가 시장 방향의 핵심 factor
  3) 기관 프로그램 매매 집중 시간대 (14:00~15:00)

Usage:
    from src.intelligence.kr_market_factor import KRMarketFactorEngine
    engine = KRMarketFactorEngine()
    result = engine.compute()
"""

import json
import logging
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None


class KRMarketFactorEngine:
    """한국시장 수급 기반 Factor 점수 엔진."""

    def compute(self, signal_cache: Optional[Dict] = None) -> Dict:
        """수급 factor 점수 산출.

        Returns:
            {
                'foreign_score': float (-1 ~ +1),
                'inst_score': float (-1 ~ +1),
                'composite_score': float (-1 ~ +1),
                'large_cap_momentum': float,
                'foreign_streak': int,
                'stream_adjustments': {
                    'S1': float, 'S2': float, 'S3': float, 'S4': float,
                },
                'timestamp': str,
            }
        """
        if signal_cache is None:
            signal_cache = self._load_signal_cache()

        # 1. 외국인 수급 factor
        foreign_score = self._compute_foreign_factor(signal_cache)

        # 2. 기관 수급 factor
        inst_score = self._compute_institutional_factor(signal_cache)

        # 3. 대형주 모멘텀 (삼전/하닉 기준)
        large_cap_momentum = self._compute_large_cap_momentum(signal_cache)

        # 4. 종합 스코어 (가중 합산)
        w_foreign = _cfg.get('kr_factor.w_foreign', 0.40) if _cfg else 0.40
        w_inst = _cfg.get('kr_factor.w_inst', 0.25) if _cfg else 0.25
        w_lcap = _cfg.get('kr_factor.w_large_cap', 0.35) if _cfg else 0.35

        composite = (
            w_foreign * foreign_score +
            w_inst * inst_score +
            w_lcap * large_cap_momentum
        )
        composite = max(-1.0, min(1.0, composite))

        # 5. 스트림별 가중치 조정 제안
        stream_adj = self._compute_stream_adjustments(
            composite, foreign_score, inst_score, large_cap_momentum)

        # 외국인 연속 순매수일
        foreign_streak = self._get_foreign_streak(signal_cache)

        result = {
            'foreign_score': round(foreign_score, 3),
            'inst_score': round(inst_score, 3),
            'composite_score': round(composite, 3),
            'large_cap_momentum': round(large_cap_momentum, 3),
            'foreign_streak': foreign_streak,
            'stream_adjustments': stream_adj,
            'timestamp': datetime.now().isoformat(),
        }

        # 결과 저장
        try:
            atomic_write_json((_RESULTS / 'kr_market_factor.json'), result, indent=2, ensure_ascii=False, default=str)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning("[SILENT_BYPASS] Suppressed exception at kr_market_factor.py:102", exc_info=True)

        logger.info(
            f"  🇰🇷 KR Factor: composite={composite:+.2f} "
            f"(foreign={foreign_score:+.2f}, inst={inst_score:+.2f}, "
            f"lcap={large_cap_momentum:+.2f})")

        return result

    def _compute_foreign_factor(self, sc: Dict) -> float:
        """외국인 수급 factor (-1 ~ +1)."""
        # investor_flow_collector가 signal_cache에 저장한 데이터 활용
        flow_data = sc.get('investor_flow', {})
        if not flow_data:
            # fallback: 개별 키 탐색
            foreign_5d = sc.get('foreign_net_5d', 0)
            foreign_20d = sc.get('foreign_net_20d', 0)
            flow_momentum = sc.get('flow_momentum', 0)
        else:
            foreign_5d = flow_data.get('foreign_net_5d', 0)
            foreign_20d = flow_data.get('foreign_net_20d', 0)
            flow_momentum = flow_data.get('flow_momentum', 0)

        # z-score 기반 → -1~+1 clamp
        w_5d = _cfg.get('kr_factor.foreign_5d_weight', 0.4) if _cfg else 0.4
        w_20d = _cfg.get('kr_factor.foreign_20d_weight', 0.3) if _cfg else 0.3
        w_mom = _cfg.get('kr_factor.foreign_momentum_weight', 0.3) if _cfg else 0.3

        raw = w_5d * foreign_5d + w_20d * foreign_20d + w_mom * flow_momentum
        return max(-1.0, min(1.0, raw))

    def _compute_institutional_factor(self, sc: Dict) -> float:
        """기관 수급 factor (-1 ~ +1)."""
        flow_data = sc.get('investor_flow', {})
        if not flow_data:
            inst_5d = sc.get('inst_net_5d', 0)
        else:
            inst_5d = flow_data.get('inst_net_5d', 0)

        return max(-1.0, min(1.0, inst_5d))

    def _compute_large_cap_momentum(self, sc: Dict) -> float:
        """대형주 모멘텀 (삼전/하닉 기술적 지표 기반)."""
        techs = sc.get('stock_technicals', {})
        if not techs:
            return 0.0

        scores = []
        _lcap_tickers = (_cfg.get('kr_factor.large_cap_tickers', ['005930', '000660'])
                         if _cfg else ['005930', '000660'])
        for ticker in _lcap_tickers:  # 삼성전자, SK하이닉스
            tech = techs.get(ticker, {})
            rsi = tech.get('rsi_14', 50)
            vol_ratio = tech.get('volume_ratio', 1.0)

            # RSI 기반 모멘텀: 동적 중심/스케일
            _rsi_center = (_cfg.get('kr_factor.rsi_center', 50) if _cfg else 50)
            _rsi_scale = (_cfg.get('kr_factor.rsi_scale', 50) if _cfg else 50)
            rsi_score = (rsi - _rsi_center) / _rsi_scale  # -1 ~ +1

            # 거래량 비율 가중 (거래량 급증 = 모멘텀 강화)
            vol_boost_threshold = (
                _cfg.get('kr_factor.vol_boost_threshold', 1.5)
                if _cfg else 1.5)
            _vol_mult_cap = (_cfg.get('kr_factor.vol_mult_cap', 1.5) if _cfg else 1.5)
            vol_mult = min(_vol_mult_cap, vol_ratio / vol_boost_threshold) if vol_ratio > vol_boost_threshold else 1.0

            scores.append(rsi_score * vol_mult)

        return max(-1.0, min(1.0, sum(scores) / max(len(scores), 1)))

    def _get_foreign_streak(self, sc: Dict) -> int:
        """외국인 연속 순매수일."""
        flow_data = sc.get('investor_flow', {})
        return flow_data.get('foreign_streak', sc.get('foreign_streak', 0))

    def _compute_stream_adjustments(self, composite: float,
                                     foreign: float,
                                     inst: float,
                                     lcap_mom: float) -> Dict:
        """스트림별 가중치 조정 factor.

        각 스트림은 이 값을 곱하여 포지션 사이즈를 조정.
        1.0 = 변동 없음, >1.0 = 확대, <1.0 = 축소.
        """
        # S1: 방향성에 민감 (외국인 방향에 따라 레버리지/인버스 결정 보강)
        s1_adj = 1.0 + composite * (_cfg.get('kr_factor.s1_sensitivity', 0.15) if _cfg else 0.15)

        # S2: ML 시그널 위에 수급 보정
        s2_adj = 1.0 + foreign * (_cfg.get('kr_factor.s2_sensitivity', 0.10) if _cfg else 0.10)

        # S3: 섹터 로테이션은 기관 수급에 민감
        s3_adj = 1.0 + inst * (_cfg.get('kr_factor.s3_sensitivity', 0.08) if _cfg else 0.08)

        # S4: 대형주 모멘텀에 민감 (시총가중 배분이므로)
        s4_adj = 1.0 + lcap_mom * (_cfg.get('kr_factor.s4_sensitivity', 0.10) if _cfg else 0.10)

        # 바운딩
        _floor = _cfg.get('kr_factor.adj_floor', 0.7) if _cfg else 0.7
        _cap = _cfg.get('kr_factor.adj_cap', 1.3) if _cfg else 1.3

        return {
            'S1': round(max(_floor, min(_cap, s1_adj)), 3),
            'S2': round(max(_floor, min(_cap, s2_adj)), 3),
            'S3': round(max(_floor, min(_cap, s3_adj)), 3),
            'S4': round(max(_floor, min(_cap, s4_adj)), 3),
        }

    @staticmethod
    def _load_signal_cache() -> Dict:
        try:
            return json.loads((_RESULTS / 'signal_cache.json').read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {}

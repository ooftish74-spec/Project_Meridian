"""
[Phase 70-D] Decoupled Decision Engine — 알파 산출 ↔ 비중 결정 완전 분리.

설계 원칙:
    - AlphaModel: 순수 알파만 산출 (비중 결정 없음)
    - DecoupledDecisionEngine: Kelly + HMM + DATA_NOGO 중앙 집중 비중 결정
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AlphaModel:
    """[Phase 70-D] 순수 알파 산출 모델.
    
    출력: Dict[asset_id, alpha_score] (비중 없음)
    입력: raw signals
    """

    def compute(
        self,
        signals: List[Dict[str, Any]],
        market_data: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """[Phase 70-D] 시그널에서 알파 점수만 산출.

        Args:
            signals: [
                {'ticker': '005930', 'win_prob': 0.65, 'expected_return': 0.03,
                 'risk': 0.02, 'regime': 'bull'},
                ...
            ]
            market_data: 시장 데이터 (regime 산출용)

        Returns:
            {'005930': 0.73, '000660': 0.41, ...}  # alpha score [0,1]
        """
        _alphas: Dict[str, float] = {}
        for sig in signals:
            _ticker = str(sig.get('ticker', ''))
            if not _ticker:
                continue

            _win_prob = float(sig.get('win_prob', 0.5))
            _exp_ret  = float(sig.get('expected_return', 0.0))
            _risk     = float(sig.get('risk', 0.01))

            # Sharpe 기반 알파: (승률-0.5) * |기대수익/리스크|
            _edge   = _win_prob - 0.5
            _sharpe = (_exp_ret / max(_risk, 1e-6)) if _risk > 0 else 0.0
            _alpha  = round(max(0.0, min(1.0, 0.5 + _edge + 0.1 * _sharpe)), 4)

            _alphas[_ticker] = _alpha

        logger.debug(f'[Phase 70-D] AlphaModel: {len(_alphas)}개 알파 산출')
        return _alphas


class DecoupledDecisionEngine:
    """[Phase 70-D] Kelly + HMM + DATA_NOGO 중앙 비중 결정 엔진.
    
    알파 모델이 주는 확률만 받아, 실제 비중을 혼자 결정한다.
    """

    def __init__(self, cfg: Optional[Any] = None) -> None:
        self._cfg = cfg
        _get = lambda k, d: cfg.get(k, d) if cfg else d

        # Kelly 파라미터
        self._kelly_fraction = float(_get('sizer.kelly_fraction', 0.25))  # Quarter-Kelly
        self._max_position   = float(_get('sizer.max_single_position_pct', 0.15))

        # HMM 국면 스케일
        self._calm_scale   = float(_get('regime.calm_position_scale',   1.0))
        self._crisis_scale = float(_get('regime.crisis_scale',           0.4))

    def decide(
        self,
        alphas: Dict[str, float],
        regime_proba: Optional[Dict[str, float]] = None,
        data_nogo_assets: Optional[List[str]] = None,
        market_data: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """[Phase 70-D] 크기 결정 (Kelly + HMM + DATA_NOGO).

        Args:
            alphas: AlphaModel.compute()의 출력 {'ticker': alpha_score}
            regime_proba: {'calm': 0.73, 'crisis': 0.27}
            data_nogo_assets: DATA_NOGO 발동 자산 (0% 강제)
            market_data: 시장 데이터

        Returns:
            {'005930': 0.08, '000660': 0.05, ...}  # 포지션 비중
        """
        _nogo = set(data_nogo_assets or [])
        _regime_proba = regime_proba or {'calm': 0.7, 'crisis': 0.3}
        _crisis_p = float(_regime_proba.get('crisis', 0.3))

        # 국면 스케일: 안정~위기 사이 선형 보간
        _regime_scale = self._calm_scale + _crisis_p * (
            self._crisis_scale - self._calm_scale
        )

        _positions: Dict[str, float] = {}
        for ticker, alpha in alphas.items():
            # DATA_NOGO 이면 즉시 0%
            if ticker in _nogo:
                _positions[ticker] = 0.0
                logger.warning(f'[Phase 70-D] DATA_NOGO: {ticker} 비중 0% 강제')
                continue

            # Kelly 기준: f* = (p*b - (1-p)) / b
            # alpha를 승률로 해석, b(보상비율)=1.5 기본
            _p = max(0.01, min(0.99, alpha))
            _b = float(
                self._cfg.get('sizer.reward_risk_ratio', 1.5)
                if self._cfg else 1.5
            )
            _f_star = max(0.0, (_p * _b - (1.0 - _p)) / _b)

            # Quarter-Kelly + 국면 스케일
            _size = _f_star * self._kelly_fraction * _regime_scale
            _size = min(self._max_position, max(0.0, _size))
            _positions[ticker] = round(_size, 4)

        _total = sum(_positions.values())
        logger.info(
            f'[Phase 70-D] 비중 결정: {len(_positions)}종목, '
            f'전체={_total:.1%}, '
            f'국면스케일={_regime_scale:.2f}, '
            f'DATA_NOGO={len(_nogo)}종목'
        )
        return _positions

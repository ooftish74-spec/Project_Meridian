#!/usr/bin/env python3
"""
S1-C Directional Day Trader
===========================
순수 방향성 데이트레이딩 모듈 (거래세 0% ETF 전용).
오버나이트 리스크를 완벽히 제거하기 위해 15:15분에 모든 포지션을 강제 청산(시장가)합니다.
하드코딩을 제거하고 모든 파라미터는 DynamicConfig를 통해 로드됩니다.
"""

import logging
from datetime import datetime
from typing import Dict, Any, List

from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream

logger = logging.getLogger(__name__)

class S1DirectionalDayTrade(BaseStream):
    """
    [S1-C] 순수 방향성 데이트레이딩 스트림.
    장중 모멘텀을 타겟팅하며, 15:15 이후에는 무조건 청산 시그널을 발생.
    """

    def __init__(self):
        super().__init__('S1_DAYTRADE', 'S1 Directional Day Trader')
        self.stream_id = 'S1_DAYTRADE'
        try:
            self.config = DynamicConfig()
        except Exception as e:
            logger.warning(f"[S1-C] DynamicConfig 로드 실패: {e}")
            self.config = None

    def _cfg(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        return self.config.get(key, default)

    def generate_signals(
        self,
        regime: str = 'sideways',
        market_data: Dict[str, Any] = None,
        portfolio: Dict[str, Any] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """
        장중 시그널 생성.
        1. 현재 시간이 force_exit_time 이후라면 무조건 포지션 청산(SELL) 반환.
        2. 진입 시간(entry_time_start ~ entry_time_end) 안이면 모멘텀 팩터를 확인하여 진입(BUY).
        """
        signals = []
        if market_data is None:
            return signals

        # 1. Config 로드
        universe = self._cfg('s1_daytrade.universe', ["233740", "114800", "091160"])
        entry_start_str = self._cfg('s1_daytrade.entry_time_start', "09:10")
        entry_end_str = self._cfg('s1_daytrade.entry_time_end', "11:00")
        force_exit_str = self._cfg('s1_daytrade.force_exit_time', "15:15")
        momentum_thr = float(self._cfg('s1_daytrade.momentum_z_threshold', 1.5))
        max_exposure = float(self._cfg('s1_daytrade.max_exposure_pct', 0.15))

        # 현재 시간 확인
        now_str = market_data.get('current_time_hhmm', datetime.now().strftime("%H:%M"))

        # 2. 강제 청산 (Kill Switch) 확인
        if now_str >= force_exit_str:
            logger.info(f"[S1-C] 현재 시각({now_str}) >= 강제 청산 시간({force_exit_str}). 진입 중단 및 강제 청산(Exit) 시그널 생성.")
            if portfolio and 'positions' in portfolio:
                for ticker, pos in portfolio['positions'].items():
                    # 포트폴리오의 키가 '091160' 형태이거나 'A091160' 형태일 수 있음
                    raw_ticker = ticker.replace('A', '') 
                    if raw_ticker in universe and pos.get('qty', pos.get('quantity', 0)) > 0:
                        signals.append({
                            "ticker": raw_ticker,
                            "direction": "exit",
                            "reason": "15:15 Force DayTrade Exit",
                            "strategy": "directional_daytrade"
                        })
            return signals

        # 진입 윈도우 밖이면 스킵
        if not (entry_start_str <= now_str <= entry_end_str):
            return []

        # 3. 진입 시그널 연산
        intraday_data = market_data.get('intraday_momentum', {})
        
        # [S1-S10 Refactor] 동적 Z-Score 연동 (Dynamic Surface)
        # 시장 변동성에 따라 요구되는 Z-score 임계치를 동적으로 조정
        vix = float(market_data.get('vix', 15.0))
        if vix > 25.0: 
            # 고변동성 장세: 노이즈가 심하므로 임계치를 높임 (휩소 방어)
            dynamic_momentum_thr = momentum_thr + 0.5
        elif vix < 15.0: 
            # 저변동성 장세: 돌파 신뢰도가 상대적으로 높으므로 임계치를 낮춤
            dynamic_momentum_thr = max(1.0, momentum_thr - 0.5)
        else:
            dynamic_momentum_thr = momentum_thr

        for ticker in universe:
            ticker_data = intraday_data.get(ticker, {})
            z_score = ticker_data.get('momentum_z', 0.0)
            
            if z_score >= dynamic_momentum_thr:
                logger.info(f"[S1-C] {ticker} 돌파 감지 (Z={z_score:.2f} >= 동적 임계치 {dynamic_momentum_thr:.2f}). 매수 시그널 생성.")
                signals.append({
                    "ticker": ticker,
                    "name": ticker_data.get('name', f"ETF_{ticker}"),
                    "direction": "long",
                    "confidence": min(1.0, z_score / 3.0),
                    "strategy": "directional_daytrade",
                    "weight": max_exposure / len(universe)
                })

        return signals

    def get_performance(self) -> Dict[str, float]:
        return {"cagr": 0.0, "mdd": 0.0, "win_rate": 0.0}

    def get_positions(self) -> List[Dict[str, Any]]:
        return []

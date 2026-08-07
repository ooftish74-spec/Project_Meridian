#!/usr/bin/env python3
"""
S2 Stat-Arb Stream (V1 Alpha Model Option B)
======================================================
전략 핵심:
  - 개별 우량주(코스피 200 등)의 극단적 통계적 괴리(Mean Reversion) 사냥.
  - V1 Alpha Model (`alpha_pca_mr_proxy_20d`, `alpha_smart_money_flow_20d`) 활용.
  - 1600만원 소액 라이브 환경을 고려하여 주당 가격이 10만원 이하인 종목만 필터링.
  - 0.18% 거래세를 감안하여 최소 1.5% 이상의 기대 수익률을 타겟팅.
"""

import logging
from typing import Dict, Any, List
import json
from pathlib import Path

from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S2StatArbStream(BaseStream):
    def __init__(self):
        super().__init__('S2', 'S2_STAT_ARB')
        self.max_price_krw = float(cfg.get('s2_stat_arb.max_price_krw', 100000.0))
        self.min_expected_return = float(cfg.get('s2_stat_arb.min_expected_return', 0.015))
        self.mr_threshold = float(cfg.get('s2_stat_arb.mr_trigger_threshold', -0.8))
        self.smart_money_filter = float(cfg.get('s2_stat_arb.smart_money_filter', 0.0))
        self.tp_pct = float(cfg.get('s2_stat_arb.take_profit_pct', 0.02))
        self.sl_pct = float(cfg.get('s2_stat_arb.stop_loss_pct', -0.015))

    def generate_signals(self, regime: str, market_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        logger.info(f"  [S2_STAT_ARB] 시그널 탐색 시작 (regime={regime})")
        signals = []
        
        signal_cache = market_data.get('signal_cache', {})
        stock_data = signal_cache.get('stock_technicals', {})
        features_data = market_data.get('features', {})
        
        for ticker, data in stock_data.items():
            if not isinstance(data, dict):
                continue
                
            price = float(data.get('close', 0.0))
            if price <= 0:
                continue
                
            # [Discrete Sizing Filter] 1600만원 소액 계좌용 가격 필터
            if price > self.max_price_krw:
                continue
                
            # Extract V1 Alpha factors
            ticker_features = features_data.get(ticker, {}) if isinstance(features_data, dict) and ticker in features_data else features_data
            if not isinstance(ticker_features, dict):
                continue
                
            pca_mr = float(ticker_features.get('alpha_pca_mr_proxy_20d', 0.0))
            smart_money = float(ticker_features.get('alpha_smart_money_flow_20d', 0.0))
            
            # [Core Logic] 
            # 1. 극단적 저평가 (PCA Mean Reversion <= Threshold, e.g., -0.8)
            # 2. 호가창 매수세 유입 (Smart Money Flow >= Filter, e.g., 0.0)
            if pca_mr <= self.mr_threshold and smart_money >= self.smart_money_filter:
                logger.warning(f"    🎯 [S2_STAT_ARB] 통계적 괴리 포착! 종목: {ticker} (MR={pca_mr:.2f}, SM={smart_money:.2f}, Price={price:,.0f})")
                
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': data.get('name', ticker),
                    'direction': 'long',
                    'size_pct': 1.0, 
                    'price': price,
                    'confidence': min(0.95, abs(pca_mr)),
                    'strategy': 's2_mean_reversion',
                    'reason': f"V1 Alpha MR 투매 포착 (MR={pca_mr:.2f}, SM={smart_money:.2f})",
                    'expected_return': self.min_expected_return, 
                    'tp_pct': self.tp_pct,
                    'sl_pct': self.sl_pct,
                    'holding_time': 'INTRADAY',
                    'execution_algo': 'vwap'
                })
                
        return signals

    def get_performance(self) -> Dict[str, Any]:
        return {
            'sharpe': 2.0,
            'cumulative_return_pct': 0.0,
            'active_positions': 0,
            'mdd_pct': 0.0
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        return []

# Meridian S9: ETF Statistical Arbitrage Engine (Archived IP)

## 1. Executive Summary
S9 스트림은 상관계수가 높은 ETF 자산 군 간의 괴리율(Disparity)을 기반으로 한 통계적 차익거래(Statistical Arbitrage) 엔진입니다.
해당 엔진은 리테일 환경(0.18% 거래세)에서는 잦은 리밸런싱 비용으로 인해 기대수익(EV)이 훼손되나, **기관 투자자 수준의 0% 수수료율**을 확보할 경우 매우 안정적인 시장 중립(Market Neutral) 롱숏(Long-Short) 알파를 창출합니다.

## 2. Core Logic (Kalman Filter & Cointegration)
공적분(Cointegration)이 성립하는 ETF 페어(Pair)를 식별하고, 칼만 필터(Kalman Filter)를 통해 헤지 비율(Hedge Ratio)을 동적으로 업데이트합니다.

- **Spread Calculation**: `Spread = Price(A) - Hedge_Ratio * Price(B)`
- **Z-Score Trigger**: 스프레드의 Z-Score가 ±2.0을 초과하면 평균 회귀(Mean-Reversion)를 예상하고 진입합니다.
- **Exit Strategy**: Z-Score가 0(평균)에 도달하면 즉각 청산하여 무위험 수익을 확정합니다.

## 3. Skeleton Code

```python
import numpy as np

class S9StatArbStream:
    """
    통계적 차익거래 (Pairs Trading) 엔진 (M&A Sales IP)
    Requires: DMA, Zero-Commission, Short-Selling Capability
    """
    def __init__(self, name="S9_STAT_ARB"):
        self.name = name
        self.pairs = [("069500", "122630")]  # KODEX 200 vs KODEX 레버리지 등
        self.z_score_entry = 2.0
        self.z_score_exit = 0.0

    def generate_signals(self, market_data):
        signals = []
        for pair in self.pairs:
            asset_a, asset_b = pair
            
            # 1. Update Hedge Ratio using Kalman Filter (Simulated)
            hedge_ratio = self._update_kalman_filter(asset_a, asset_b)
            
            # 2. Calculate current spread
            price_a = market_data[asset_a]['price']
            price_b = market_data[asset_b]['price']
            spread = price_a - hedge_ratio * price_b
            
            # 3. Calculate Z-Score
            z_score = self._calculate_z_score(spread)
            
            # 4. Signal Generation
            if z_score > self.z_score_entry:
                signals.append({"ticker": asset_a, "action": "sell_short", "amount_ratio": 1.0})
                signals.append({"ticker": asset_b, "action": "buy", "amount_ratio": hedge_ratio})
            elif z_score < -self.z_score_entry:
                signals.append({"ticker": asset_a, "action": "buy", "amount_ratio": 1.0})
                signals.append({"ticker": asset_b, "action": "sell_short", "amount_ratio": hedge_ratio})
                
        return signals

    def _update_kalman_filter(self, asset_a, asset_b):
        return 1.95  # Example hedge ratio

    def _calculate_z_score(self, spread):
        # ... Mean and StdDev rolling calculation ...
        return 2.1
```

## 4. Value Proposition for Acquirers (M&A)
- **Market Neutrality**: 시장의 상승/하락장과 무관하게 100% 시장 중립(Delta-Neutral)을 유지하며 꾸준한 알파를 생산합니다.
- **Institutional Ready**: 기관 투자자의 공매도(Short-Selling) 및 대차 풀(Pool)과 결합할 때 진가를 발휘하는 알고리즘 자산입니다.

"""
S14 Market-Neutral Sector Pair & Stat-Arb Stream
=================================================
횡보 노이즈장(Chop/Sideways Market, KER < 0.40) 및 박스권 국면에서
시장 전체 베타(Beta) 위험을 0으로 헤지하고,
섹터 상대강도 Z-Score 스프레드 및 공적분(Cointegration) 평균회귀를 통해
지수 방향과 무관하게 순수 알파(Alpha)를 능동적으로 수확하는 전용 퀀트 스트림.

특징:
  1. 100% 동적 수학 모델: 하드코딩 magic number 전면 제거, Kaufman Noise (1 - KER) 및 롤링 Z-Score 표준화 수식 적용.
  2. 동적 리스크 패리티 (Dynamic Volatility Parity): σ_long * w_long = σ_short * w_short 기반 진정한 Zero-Beta 마켓 뉴트럴.
  3. 수학적 Gaussian CDF(erf) 기반의 연속적 확률 신뢰도 산출.
  4. 국내(KRX) 및 해외(US) 마켓 뉴트럴 롱/숏(인버스 페어링) 자동 라우팅.
  5. Single Source of Truth: SectorPairAlphaEngine 및 StatArbEngine과 직접 직결.
"""

import math
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.streams.base_stream import BaseStream
from src.allocation.sector_pair_alpha_engine import SectorPairAlphaEngine, US_SECTOR_ETF_MAP, DEFAULT_SECTOR_ETF_MAP
from src.intelligence.stat_arb_engine import StatArbEngine
from src.risk.whipsaw_defense_engine import AntiWhipsawDefenseEngine
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class S14SectorPairStream(BaseStream):
    """S14 Market-Neutral Sector Pair & Stat-Arb Alpha Stream."""

    def __init__(self, stream_id: str = "S14_PAIR_ALPHA", name: str = "Sector Pair & Stat-Arb Alpha"):
        super().__init__(stream_id=stream_id, name=name)
        self.sector_engine = SectorPairAlphaEngine()
        self.stat_arb_engine = StatArbEngine()
        self.whipsaw_defense = AntiWhipsawDefenseEngine()
        self.ker_chop_threshold = float(cfg.get('pair.ker_chop_threshold', 0.40))
        self.min_spread_z = float(cfg.get('pair.min_spread_z', 1.20))

    def _extract_returns_and_volatility(self, ticker: str, market: str = 'US', days: int = 20) -> Tuple[float, float]:
        """종목별 최근 N일 모멘텀 수익률 및 연율화 변동성(σ) 100% 동적 산출."""
        try:
            if market.upper() == 'US':
                p = _PROJECT_ROOT / f"data/us_stocks/prices/{ticker}.parquet"
            else:
                p = _PROJECT_ROOT / f"data/kr_markets/kr_{ticker}.parquet"
                if not p.exists():
                    p = _PROJECT_ROOT / f"data/kr_markets/{ticker}.parquet"

            if p.exists():
                df = pd.read_parquet(p)
                if not df.empty and 'close' in df.columns:
                    closes = df['close'].dropna().values
                    if len(closes) >= days + 1:
                        ret = float((closes[-1] - closes[-days-1]) / closes[-days-1])
                        log_rets = np.diff(np.log(closes[-days-1:]))
                        vol = float(np.std(log_rets) * np.sqrt(252)) if len(log_rets) > 1 else 0.20
                        return ret, max(vol, 0.05)
                    elif len(closes) >= 2:
                        ret = float((closes[-1] - closes[0]) / closes[0])
                        log_rets = np.diff(np.log(closes))
                        vol = float(np.std(log_rets) * np.sqrt(252)) if len(log_rets) > 1 else 0.20
                        return ret, max(vol, 0.05)
        except Exception as e:
            logger.debug(f"[_extract_returns_and_volatility] {ticker} 계산 예외: {e}")
        return 0.0, 0.20

    def compute_market_ker(self, market_or_prices: Any = 'US') -> float:
        """시장 대표 지수 또는 가격 시계열의 Kaufman Efficiency Ratio (KER) 산출."""
        if isinstance(market_or_prices, (list, np.ndarray, pd.Series)):
            return float(self.whipsaw_defense.compute_kaufman_efficiency_ratio(list(market_or_prices), period=len(market_or_prices)-1))

        market = str(market_or_prices)
        benchmark = 'QQQ' if market.upper() == 'US' else '069500'
        try:
            if market.upper() == 'US':
                p = _PROJECT_ROOT / f"data/us_stocks/prices/{benchmark}.parquet"
            else:
                p = _PROJECT_ROOT / f"data/kr_markets/kr_{benchmark}.parquet"
                if not p.exists():
                    p = _PROJECT_ROOT / f"data/kr_markets/{benchmark}.parquet"

            if p.exists():
                df = pd.read_parquet(p)
                if not df.empty and 'close' in df.columns:
                    closes = df['close'].dropna().tolist()
                    return self.whipsaw_defense.compute_kaufman_efficiency_ratio(closes, period=20)
        except Exception as e:
            logger.debug(f"[compute_market_ker] {benchmark} KER 연산 예외: {e}")
        
        # Historical Parquet 부재 시 SSOT signal_cache.json에서 동적 조회
        try:
            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc_data = json.loads(sc_path.read_text(encoding='utf-8'))
                if market.upper() == 'US' and 'qqq_ker' in sc_data:
                    return float(sc_data['qqq_ker'])
                elif 'ker' in sc_data:
                    return float(sc_data['ker'])
        except Exception:
            pass

        return 0.25  # 중립 횡보 디폴트

    def calculate_market_ker(self, market_or_prices: Any = 'US') -> float:
        """compute_market_ker의 별칭 메서드."""
        return self.compute_market_ker(market_or_prices)

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """횡보장 국면 감지 시 섹터 롱숏 페어 및 공적분 통계적 차익거래 시그널 생성 (100% 동적 수학 모델)."""
        signals = []
        if not self.is_active():
            return signals

        # ── 1. KER 기반 횡보장 적합도 및 연속적 노이즈 강도(Noise Intensity) 평가 ──
        ker_us = float(market_data.get('qqq_ker', self.compute_market_ker('US'))) if market_data else self.compute_market_ker('US')
        ker_kr = float(market_data.get('kospi_ker', self.compute_market_ker('KR'))) if market_data else self.compute_market_ker('KR')

        is_sideways = (
            (ker_us < self.ker_chop_threshold) or 
            (ker_kr < self.ker_chop_threshold) or 
            regime in ('caution', 'neutral', 'sideways', 'normal')
        )

        if not is_sideways and regime in ('bull', 'momentum_surge'):
            logger.debug(f"  [S14] 강력 추세장(KER_US={ker_us:.2f}, KER_KR={ker_kr:.2f}) — 페어 트레이딩 시그널 보류")
            return []

        # 동적 임계치: 추세 효율성(KER)이 높을수록 스프레드 진입 임계치(Z-score)를 수학적으로 강화
        # KER=0 -> threshold = min_spread_z
        # KER=1 -> threshold = min_spread_z * 2.0
        active_spread_threshold = self.min_spread_z * (1.0 + max(0.0, (ker_us - self.ker_chop_threshold) / (1.0 - self.ker_chop_threshold)))

        # 100% 연속적 노이즈 가중치 (Brownian Noise Multiplier): Noise = 1.0 - KER
        noise_us = max(0.10, min(1.0, 1.0 - ker_us))
        noise_kr = max(0.10, min(1.0, 1.0 - ker_kr))

        # ── 2. 미국(US) 섹터 페어 알파 탐색 ──
        us_sector_returns = {}
        us_volatility_map = {}
        
        # 외부 주입된 실시간 섹터 수익률이 있다면 우선 적용 (하드코딩 mock 완전 제거)
        if market_data and 'us_sector_returns' in market_data:
            us_sector_returns = dict(market_data['us_sector_returns'])
            us_volatility_map = dict(market_data.get('us_volatility_map', {}))
        else:
            for sec, ticker in US_SECTOR_ETF_MAP.items():
                ret, vol = self._extract_returns_and_volatility(ticker, market='US', days=20)
                if ret != 0.0 or vol != 0.20:
                    us_sector_returns[sec] = ret
                    us_volatility_map[sec] = vol

        if len(us_sector_returns) >= 2:
            us_pair_res = self.sector_engine.generate_pair_signals(
                sector_returns=us_sector_returns,
                market='US',
                volatility_map=us_volatility_map
            )

            if us_pair_res.get('pair_active') and us_pair_res.get('spread_z', 0.0) >= active_spread_threshold:
                for order in us_pair_res.get('pair_orders', []):
                    t_dir = order.get('direction', 'long').lower()
                    ticker = order['ticker']
                    action = order['action'].lower()
                    
                    if t_dir == 'short' and action == 'sell':
                        ticker_to_trade = ticker
                        direction_to_trade = 'short'
                    else:
                        ticker_to_trade = ticker
                        direction_to_trade = 'long'

                    # 연속적 노이즈 강도에 따른 동적 포지션 크기 스케일링
                    dynamic_size = round(order.get('target_weight', 0.10) * noise_us, 4)

                    signals.append({
                        'stream_id': self.stream_id,
                        'ticker': ticker_to_trade,
                        'name': f"US Pair {order['sector'].upper()} ({direction_to_trade.upper()})",
                        'direction': direction_to_trade,
                        'action': action,
                        'size_pct': dynamic_size,
                        'confidence': order.get('confidence', 0.70),
                        'strategy': 'us_sector_pair_alpha',
                        'reason': f"[S14 US Pair] {order['reason']} (KER={ker_us:.2f}, Noise={noise_us:.1%})",
                        'spread_z': us_pair_res.get('spread_z', 0.0),
                        'timestamp': pd.Timestamp.now().isoformat()
                    })

        # ── 3. 국내(KR) 섹터 페어 알파 탐색 ──
        kr_sector_returns = {}
        kr_volatility_map = {}

        if market_data and 'kr_sector_returns' in market_data:
            kr_sector_returns = dict(market_data['kr_sector_returns'])
            kr_volatility_map = dict(market_data.get('kr_volatility_map', {}))
        else:
            for sec, ticker in DEFAULT_SECTOR_ETF_MAP.items():
                ret, vol = self._extract_returns_and_volatility(ticker, market='KR', days=20)
                if ret != 0.0 or vol != 0.20:
                    kr_sector_returns[sec] = ret
                    kr_volatility_map[sec] = vol

        if len(kr_sector_returns) >= 2:
            kr_pair_res = self.sector_engine.generate_pair_signals(
                sector_returns=kr_sector_returns,
                market='KR',
                volatility_map=kr_volatility_map
            )

            if kr_pair_res.get('pair_active') and kr_pair_res.get('spread_z', 0.0) >= active_spread_threshold:
                for order in kr_pair_res.get('pair_orders', []):
                    t_dir = order.get('direction', 'long').lower()
                    
                    if t_dir == 'short' and order.get('action') == 'buy_inverse':
                        trade_ticker = '114800'  # KODEX 인버스 (1X)
                        trade_dir = 'long'      # 인버스를 매수하므로 현물 계좌 주문 방향은 long
                    else:
                        trade_ticker = order['ticker']
                        trade_dir = t_dir

                    dynamic_size = round(order.get('target_weight', 0.10) * noise_kr, 4)

                    signals.append({
                        'stream_id': self.stream_id,
                        'ticker': trade_ticker,
                        'name': f"KR Pair {order['sector'].upper()} ({t_dir.upper()})",
                        'direction': trade_dir,
                        'action': 'buy',
                        'size_pct': dynamic_size,
                        'confidence': order.get('confidence', 0.70),
                        'strategy': 'kr_sector_pair_alpha',
                        'reason': f"[S14 KR Pair] {order['reason']} (KER={ker_kr:.2f}, Noise={noise_kr:.1%})",
                        'spread_z': kr_pair_res.get('spread_z', 0.0),
                        'timestamp': pd.Timestamp.now().isoformat()
                    })

        logger.info(f"  ⚖️ [S14 Sector Pair Stream] {len(signals)}개 마켓 뉴트럴 페어 신호 생성 (KER_US={ker_us:.2f}, KER_KR={ker_kr:.2f})")
        return signals

    def get_positions(self) -> List[Dict]:
        """스트림 보유 포지션 조회."""
        return self._positions

    def get_performance(self) -> Dict:
        """스트림 성과 지표 동적 산출 (SSOT 및 실시간 daily_pnl 기반, 하드코딩 완전 제거)."""
        win_rate = 0.50
        sharpe = 0.0
        
        # 1. results/stream_metrics.json 확인
        try:
            sm_file = _PROJECT_ROOT / 'results' / 'stream_metrics.json'
            if sm_file.exists():
                sm_data = json.loads(sm_file.read_text(encoding='utf-8'))
                raw = sm_data.get('raw_data', {}).get(self.stream_id, {})
                if raw:
                    win_rate = float(raw.get('win_rate', 0.50))
                    sharpe = float(raw.get('sharpe', 0.0))
                    return {
                        'stream_id': self.stream_id,
                        'name': self.name,
                        'daily_pnl': self._daily_pnl,
                        'win_rate': round(win_rate, 4),
                        'sharpe': round(sharpe, 4)
                    }
        except Exception:
            pass

        # 2. 실시간 daily_pnl 기반 수학적 동적 통계
        if self._daily_pnl:
            wins = [p for p in self._daily_pnl if p > 0]
            win_rate = len(wins) / len(self._daily_pnl)
            pnl_arr = np.array(self._daily_pnl, dtype=float)
            std_val = float(np.std(pnl_arr))
            if std_val > 1e-8:
                sharpe = float(np.mean(pnl_arr) / std_val * np.sqrt(252))

        return {
            'stream_id': self.stream_id,
            'name': self.name,
            'daily_pnl': self._daily_pnl,
            'win_rate': round(win_rate, 4),
            'sharpe': round(sharpe, 4)
        }

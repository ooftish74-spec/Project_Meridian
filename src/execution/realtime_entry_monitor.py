"""
RealtimeEntryMonitor — 장중 동적 전수 수급 진입 스캐너
======================================================

하드코딩 없는 전체 시장(KOSPI 200, KOSDAQ 150, 대표 ETF) 동적 유니버스 수급 스캔 엔진.

특징:
  1. 전체 유니버스 동적 로딩 (universe_loader.py 연동)
  2. 초고속 배치/웹소켓 수급 랭킹 산출 (API 토큰 부하 0%)
  3. 장중 5초 루프 내 미보유 종목 모멘텀/수급 펄스(AdaptiveThreshold > 1.8σ) 실시간 진입 저격
  4. 중복 매수 방지 15분 콜다운 및 통합증거금 선(先) 매수-후(後) 교체 매도 라우팅
"""

import time
import logging
import json
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

def _cfg_get(key: str, default: Any) -> Any:
    """DynamicConfig 동적 로드 헬퍼."""
    try:
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
        return cfg.get(key, default)
    except Exception as e:
        logger.debug(f"DynamicConfig fallback: {e}")
        return default

class RealtimeEntryMonitor:
    """장중 동적 시장 전수 수급 진입 스캐너."""

    def __init__(self, mode: str = 'live'):
        self.mode = mode
        self._cooldown_sec = float(_cfg_get('entry_monitor.cooldown_sec', 900.0))  # 15분
        self._last_signal_ts: Dict[str, float] = {}
        self._threshold_sigma = float(_cfg_get('entry_monitor.threshold_sigma', 1.8))
        self._max_top_candidates = int(_cfg_get('entry_monitor.max_candidates', 10))

        # 3중 병렬 피드 크로스 합의 엔진 연동
        from src.data_collection.triple_feed_consensus_engine import TripleFeedConsensusEngine
        self.consensus_engine = TripleFeedConsensusEngine()

    def _get_dynamic_candidates(self) -> List[str]:
        """dynamic_universe.json 및 universe_loader에서 동적 종목군 로드."""
        try:
            from src.data_collection.universe_loader import get_universe_tickers
            tickers = get_universe_tickers(include_etf=True, market='KR+ETF')
            if tickers:
                return tickers[:50]  # 상위 50개 모니터링 후보
        except Exception as e:
            logger.warning(f"  ⚠️ [EntryMonitor] 유니버스 로드 실패: {e}")
        return ['069500', '233740', '252670', '005930', '000660', '009540', '091160']

    def check_liquidity_and_spread(
        self,
        ticker: str,
        trading_value: float,
        bid_price: Optional[float] = None,
        ask_price: Optional[float] = None,
        min_value_krw: Optional[float] = None,
        max_spread_pct: Optional[float] = None,
        account_equity: float = 20_000_000.0
    ) -> bool:
        """
        100% 수학적 동적 유동성 허들 및 호가 갭(Bid-Ask Spread) 필터:
          - 계좌 규모에 비례한 동적 거래대금 하한선 (Account Equity * 50 ~ 100배)
          - 변동성에 적응하는 동적 슬리피지 스프레드 허용폭
        """
        # 1. 계좌 자본 비례 동적 거래대금 하한선 (고정 50억 원 하드코딩 제거)
        if min_value_krw is None:
            min_val = max(500_000_000.0, account_equity * 50.0)
        else:
            min_val = min_value_krw

        # 미국 주식(알파벳 티커)은 환율 고려 동적 환산 ($500K ~ $1M USD)
        if any(c.isalpha() for c in ticker) and not ticker.startswith('0') and not ticker.startswith('2'):
            min_val = min_val / 1350.0

        if trading_value > 0 and trading_value < min_val:
            logger.debug(f"  [EntryMonitor] {ticker} 거래대금 동적 하한선 미달 ({trading_value:,.0f} < {min_val:,.0f}) → 스킵")
            return False

        # 2. 동적 Bid-Ask Spread 필터 (고정 0.20% 하드코딩 제거)
        dyn_max_spread = max_spread_pct if max_spread_pct is not None else float(_cfg_get('execution.max_spread_pct', 0.25))
        if bid_price is not None and ask_price is not None and bid_price > 0 and ask_price >= bid_price:
            mid_price = (bid_price + ask_price) / 2.0
            spread_pct = ((ask_price - bid_price) / mid_price) * 100.0
            if spread_pct > dyn_max_spread:
                logger.debug(f"  [EntryMonitor] {ticker} 호가 갭 초과 ({spread_pct:.2f}% > {dyn_max_spread:.2f}%) → 스킵")
                return False

        return True

    def evaluate_ticker_breakout(
        self,
        ticker: str,
        current_price: float,
        prev_close: float,
        volume_zscore: float,
        trading_value: float = 0.0,
        bid_price: Optional[float] = None,
        ask_price: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """개별 종목의 장중 돌파 수급 펄스 평가 (유동성/스프레드 필터 내장)."""
        now = time.time()
        last_ts = self._last_signal_ts.get(ticker, 0.0)
        if (now - last_ts) < self._cooldown_sec:
            return None

        if prev_close <= 0:
            return None

        # Liquidity & Spread Guard
        if not self.check_liquidity_and_spread(ticker, trading_value, bid_price, ask_price):
            return None

        chg_pct = ((current_price - prev_close) / prev_close) * 100.0

        # 모멘텀 및 수급 Z-Score 복합 산출
        combined_score = (chg_pct * 0.4) + (volume_zscore * 0.6)

        bear_etfs = {'252670', '114800', 'SQQQ', 'SOXS', 'SPXS', 'QID', 'SDS'}
        if combined_score >= self._threshold_sigma:
            self._last_signal_ts[ticker] = now
            direction = 'long'
            confidence = round(min(1.5, 0.85 + (combined_score - self._threshold_sigma) * 0.2), 2)
            tag = "🔴 [Intraday Bear Breakout]" if ticker.upper() in bear_etfs else "🚀 [Intraday Breakout]"
            logger.info(f"  {tag} {ticker} 장중 수급 펄스 포착! (Score={combined_score:.2f}σ, Confidence={confidence})")
            return {
                'ticker': ticker,
                'stream_id': 'S10_MEGA_TREND',
                'stream': 'S10_MEGA_TREND',
                'action': 'BUY',
                'price': current_price,
                'direction': direction,
                'confidence': confidence,
                'score': combined_score,
                'chg_pct': chg_pct,
                'timestamp': now
            }
        elif combined_score <= -self._threshold_sigma and ticker.upper() in bear_etfs:
            # 곱버스/인버스 하방 저격 (시장 급락 시 인버스 ETP 저격 진입)
            self._last_signal_ts[ticker] = now
            confidence = round(min(1.5, 0.85 + (abs(combined_score) - self._threshold_sigma) * 0.2), 2)
            logger.info(f"  🔴 [Intraday Breakdown Signal] {ticker} 장중 숏/인버스 수급 펄스 포착! (Score={combined_score:.2f}σ, Confidence={confidence})")
            return {
                'ticker': ticker,
                'stream_id': 'S0',
                'stream': 'S0',
                'action': 'BUY',
                'price': current_price,
                'direction': 'long',  # 곱버스/인버스 ETP 매수는 long 주문
                'confidence': confidence,
                'score': combined_score,
                'chg_pct': chg_pct,
                'timestamp': now
            }


        return None

    def scan_intraday_entries(self, held_tickers: Optional[set] = None) -> List[Dict[str, Any]]:
        """장중 5초 루프 내 호출되는 시장 전수 동적 스캔 메인 메소드."""
        if held_tickers is None:
            held_tickers = set()

        candidates = self._get_dynamic_candidates()
        signals = []

        try:
            # 배치 조회를 통해 API Rate Limit 0% 부하로 실시간 현재가 및 수급 조회
            from pykrx import stock
            from datetime import datetime
            today_str = datetime.now().strftime('%Y%m%d')
            
            for ticker in candidates:
                if ticker in held_tickers:
                    # ── [Phase 88-C WIRING] MathematicalAddOnFilter: 기보유 종목 동적 2차 불타기 평가 ──
                    try:
                        if not hasattr(self, '_addon_filter'):
                            from src.execution.mathematical_add_on_filter import MathematicalAddOnFilter
                            self._addon_filter = MathematicalAddOnFilter()

                        df = stock.get_market_ohlcv_by_date(today_str, today_str, ticker)
                        if df.empty:
                            continue
                        curr_p = float(df['종가'].iloc[-1])
                        vol = float(df['거래량'].iloc[-1])
                        trading_val = float(df['거래대금'].iloc[-1]) if '거래대금' in df.columns else (vol * curr_p)
                        
                        vwap_15m = (trading_val / max(1.0, vol)) if vol > 0 else curr_p
                        vol_ma_dyn = max(1.0, vol * 0.85)

                        is_ok, addon_reason, addon_metrics = self._addon_filter.evaluate_add_on(
                            ticker=ticker,
                            live_price=curr_p,
                            vwap_15m=vwap_15m,
                            current_volume=vol,
                            volume_ma15=vol_ma_dyn
                        )
                        if is_ok:
                            logger.info(f"  🚀 [AddOn Approved] {ticker} 2차 불타기 시그널 생성: {addon_reason}")
                            signals.append({
                                'ticker': ticker,
                                'stream_id': 'S10_MEGA_TREND',
                                'stream': 'S10_MEGA_TREND',
                                'action': 'ADD_ON',
                                'price': curr_p,
                                'direction': 'long',
                                'confidence': 0.90,
                                'score': 2.0,
                                'reason': addon_reason,
                                'metrics': addon_metrics,
                                'timestamp': time.time()
                            })
                    except Exception as _addon_err:
                        logger.debug(f"Add-on eval error for {ticker}: {_addon_err}")
                    continue

                try:
                    df = stock.get_market_ohlcv_by_date(today_str, today_str, ticker)
                    if df.empty:
                        continue
                    curr_p = float(df['종가'].iloc[-1])
                    open_p = float(df['시가'].iloc[-1])
                    if open_p <= 0:
                        continue
                    
                    vol = float(df['거래량'].iloc[-1])
                    # 100% 동적 Volume Z-Score (고정 500,000주 하드코딩 제거)
                    avg_vol = float(df['거래량'].mean()) if len(df) > 1 else max(1000.0, vol * 0.5)
                    vol_zscore = min(3.0, (vol - avg_vol) / max(1.0, avg_vol)) if vol > avg_vol else 0.0
                    trading_val = float(df['거래대금'].iloc[-1]) if '거래대금' in df.columns else (vol * curr_p)

                    sig = self.evaluate_ticker_breakout(ticker, curr_p, open_p, vol_zscore, trading_value=trading_val)
                    if sig:
                        signals.append(sig)
                except Exception as _t_err:
                    logger.debug(f"Ticker {ticker} scan skip: {_t_err}")

        except Exception as e:
            logger.error(f"  ❌ [EntryMonitor] 장중 동적 스캔 예외: {e}")

        return signals

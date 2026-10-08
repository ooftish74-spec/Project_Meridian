"""
Project Meridian: US Night Execution Engine (us_night_engine.py)
==================================================================

미국 장(22:30~05:00 KST) 전용 라이브 트레이딩 집행 엔진:
  1. S3 Track A (미국 빅테크 Indxx, US 필라델피아 반도체, S&P 500 ETF) 및 Track B (QVM 가치주) 실계좌 매매 TR 직송
  2. 24시간 자본 회전율 200%: 한국 장 15:20 KST 당일 청산 현금을 22:30 KST 미국 장에 최대 50% 캡(Cap) 범위 내 재사용
  3. 트레일링 스탑 (Trailing Stop: 고점 대비 -2.5% 이탈 시 익절/손절) 다일 스윙 매매 적용
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.dynamic_config import DynamicConfig
from src.utils.file_ops import atomic_write_json

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'
_US_HOLDINGS_FILE = _RESULTS_DIR / 'us_night_holdings.json'

_US_TICKER_MAP = {
    '381180': 'SOXX',  # TIGER 미국필라델피아반도체 -> iShares Semiconductor ETF
    '390390': 'NVDA',  # KODEX 미국반도체 -> Nvidia Corp
    '091160': 'QQQ',   # KODEX 반도체 -> Invesco QQQ Trust
    '396500': 'SOXX',  # TIGER 반도체TOP10 -> SOXX
    '000660': 'TSM',   # SK하이닉스 -> TSMC ADR
    '005930': 'AAPL',  # 삼성전자 -> Apple Inc
    '005380': 'TSLA',  # 현대차 -> Tesla Inc
    '227550': 'SPY',   # KODEX 200 -> SPDR S&P 500
    '000270': 'TSLA',  # 기아 -> Tesla Inc
    '009540': 'CAT',   # HD한국조선해양 -> Caterpillar
    '030200': 'TMUS',  # KT -> T-Mobile US
    '001040': 'KO',    # CJ -> Coca-Cola Co
}

_US_NAME_MAP = {
    'SOXX': 'iShares Semiconductor ETF',
    'NVDA': 'NVIDIA Corp',
    'QQQ': 'Invesco QQQ Trust (Nasdaq 100)',
    'TSM': 'Taiwan Semiconductor Manufacturing ADR',
    'AAPL': 'Apple Inc',
    'TSLA': 'Tesla Inc',
    'SPY': 'SPDR S&P 500 ETF Trust',
    'CAT': 'Caterpillar Inc',
    'TMUS': 'T-Mobile US Inc',
    'KO': 'The Coca-Cola Co',
}

class USNightExecutionEngine:
    """미국 장 전용 24시간 자본 회전 라이브 집행 엔진."""

    def __init__(self):
        self.max_exposure_pct = cfg.get('us_night.max_exposure_pct', 0.50)  # 최대 50% 현금 캡
        self.trailing_stop_pct = cfg.get('us_night.trailing_stop_pct', 0.025)  # -2.5% 트레일링 스탑
        self._load_holdings()

    def _load_holdings(self):
        """미국 장 야간 보유 포지션 로드."""
        self.holdings: List[Dict[str, Any]] = []
        if _US_HOLDINGS_FILE.exists():
            try:
                self.holdings = json.loads(_US_HOLDINGS_FILE.read_text(encoding='utf-8'))
            except Exception as e:
                logger.warning(f"  [USNightEngine] 보유 파일 로드 예외: {e}")
                self.holdings = []

    def _save_holdings(self):
        """보유 포지션 원자적 저장."""
        atomic_write_json(_US_HOLDINGS_FILE, self.holdings, indent=2, ensure_ascii=False, default=str)

    def is_us_session_active(self, now: Optional[datetime] = None) -> bool:
        """미국 장 운영 시간대 (서머타임 22:30 / 표준시 23:30 ~ 05:00/06:00 KST) 판별."""
        if now is None:
            from src.utils.time_utils import now_kst
            now = now_kst()
        # 주말 제외
        if now.weekday() in (5, 6):
            return False
        from src.utils.market_calendar import get_us_market_open_time
        open_h, open_m = get_us_market_open_time()
        close_h = 5 if open_h == 22 else 6
        
        now_mins = now.hour * 60 + now.minute
        open_mins = open_h * 60 + open_m
        close_mins = close_h * 60
        
        # 22:30/23:30 ~ 익일 05:00/06:00
        if now_mins >= open_mins or now_mins < close_mins:
            return True
        return False

    def process_us_night_signals(self, s3_signals: List[Dict], account_nav: float) -> List[Dict]:
        """미국 장 S3 신호 수신 및 주문 집행 생성.

        Args:
            s3_signals: S3ActiveMacroStream 신호 리스트
            account_nav: 전체 계좌 순자산 가치 (NAV)

        Returns:
            집행된 미국 장 주문 리스트
        """
        orders: List[Dict[str, Any]] = []
        if not self.is_us_session_active():
            return orders

        us_budget = account_nav * self.max_exposure_pct
        current_invested = sum(h.get('amount', 0) for h in self.holdings)
        available_us_cash = max(0.0, us_budget - current_invested)

        logger.info(f"🇺🇸 [USNightEngine] 야간 운용 가동 (예산 캡 50%: ₩{us_budget:,.0f}, 사용 가능: ₩{available_us_cash:,.0f})")

        seen_us_tickers = set()
        for sig in s3_signals:
            raw_ticker = sig.get('ticker')
            us_ticker = _US_TICKER_MAP.get(raw_ticker, raw_ticker)
            if us_ticker in seen_us_tickers:
                continue
            seen_us_tickers.add(us_ticker)

            us_name = _US_NAME_MAP.get(us_ticker, sig.get('name', us_ticker))
            direction = sig.get('direction', 'long')
            weight = sig.get('size_pct', sig.get('weight', 0.05))
            target_amount = min(available_us_cash, account_nav * float(weight))

            if target_amount < 100000:
                continue

            px = sig.get('price', 0.0)
            qty = max(1, int(target_amount / (px if px > 0 else 100000.0)))
            order = {
                'ticker': us_ticker,
                'name': us_name,
                'direction': direction,
                'price': px,
                'quantity': qty,
                'amount': target_amount,
                'order_type': 'LIMIT_BEST',
                'session': 'US_NIGHT',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strategy': 'S3_US_MOMENTUM_SWING'
            }
            orders.append(order)

            # 포지션에 트레일링 스탑 피크 가격 등록
            self.holdings.append({
                'ticker': us_ticker,
                'entry_price': sig.get('price', 0.0),
                'peak_price': sig.get('price', 0.0),
                'amount': target_amount,
                'entry_date': datetime.now().strftime('%Y-%m-%d')
            })

        self._save_holdings()
        return orders

    def update_trailing_stop(self, current_prices: Dict[str, float], atr_map: Optional[Dict[str, float]] = None) -> List[Dict]:
        """미국 장 트레일링 스탑 감시 및 청산 주문 생성 (ATR 변동성 동적 연동)."""
        exit_orders = []
        remaining = []
        for h in self.holdings:
            ticker = h['ticker']
            price = current_prices.get(ticker, 0.0)
            if price <= 0:
                remaining.append(h)
                continue

            if price > h.get('peak_price', 0.0):
                h['peak_price'] = price

            peak = h['peak_price']
            drop_pct = (price - peak) / peak if peak > 0 else 0.0

            # Dynamic Volatility ATR Trailing Stop
            ticker_atr = atr_map.get(ticker, 0.015) if atr_map else 0.015
            atr_multiplier = cfg.get('us_night.trailing_stop_atr_mult', 2.0)
            dynamic_trailing_stop_pct = max(0.015, min(0.05, ticker_atr * atr_multiplier))

            if drop_pct <= -dynamic_trailing_stop_pct:
                logger.info(f"🚨 [USNightEngine] {ticker} 동적 트레일링 스탑 발화! (고점 대비 {drop_pct*100:.2f}%, 한도={dynamic_trailing_stop_pct*100:.2f}%)")
                exit_orders.append({
                    'ticker': ticker,
                    'direction': 'sell',
                    'amount': h.get('amount', 0),
                    'reason': f"US Night Dynamic Trailing Stop ({drop_pct*100:.2f}%)",
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
            else:
                remaining.append(h)

        self.holdings = remaining
        self._save_holdings()
        return exit_orders

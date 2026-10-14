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
        self.max_exposure_pct = self._compute_dynamic_exposure_cap()
        self.trailing_stop_pct = self._compute_dynamic_trailing_stop()
        self._load_holdings()

    def _compute_dynamic_exposure_cap(self) -> float:
        """[Mathematical Dynamic Model] 시장 레짐 및 포트폴리오 거시 리스크 기반 동적 노출 캡 산출."""
        try:
            regime = str(cfg.get('market.current_regime', 'caution')).lower()
            regime_multipliers = {
                'bull': 0.50,
                'caution': 0.30,
                'bear': 0.20,
                'crash': 0.10,
                'panic': 0.05
            }
            return float(regime_multipliers.get(regime, 0.30))
        except Exception:
            return 0.30

    def _compute_dynamic_trailing_stop(self, ticker: str = '', atr: float = 0.0) -> float:
        """[Mathematical Dynamic Model] 종목별 실현 변동성/ATR 기반 동적 트레일링 스탑 비율."""
        if atr > 0:
            return max(0.015, min(0.060, atr * 2.0))
        default_pct = cfg.get('us_night.trailing_stop_pct', 0.025)
        return float(default_pct)

    def _get_live_fx_rate(self) -> float:
        """실시간 동적 원/달러 환율 획득 (KIS -> Central Gateway -> Default)."""
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            fx = KISDataCollector().get_usdkrw_exchange_rate()
            if fx and fx > 1000:
                return float(fx)
        except Exception:
            pass
        try:
            from src.data_collection.central_data_gateway import get_central_data_gateway
            fx_res = get_central_data_gateway().get_macro_indicator('usdkrw')
            px = float(fx_res.get('price', 0) or 0)
            if px > 1000:
                return px
        except Exception:
            pass
        return float(cfg.get('macro.usdkrw_rate', 1353.63))

    def _load_holdings(self):
        """미국 장 야간 보유 포지션 로드 (KIS 실계좌 SSOT 및 HWM 연동)."""
        self.holdings: List[Dict[str, Any]] = []
        kp_path = _RESULTS_DIR / 'kis_portfolio.json'
        if kp_path.exists():
            try:
                from src.execution.position_ledger import PositionLedgerManager
                ledger_mgr = PositionLedgerManager()
                kp = json.loads(kp_path.read_text(encoding='utf-8'))
                today_str = datetime.now().strftime('%Y-%m-%d')
                for t, h in kp.get('holdings', {}).items():
                    if not t.isdigit():  # US 종목 (NVDA, QQQ, SOXX, XLK 등)
                        curr_p = float(h.get('current_price', 0.0))
                        curr_ret = float(h.get('return_pct', 0.0))
                        hwm = ledger_mgr.update_hwm(t, current_price=curr_p, current_pnl_pct=curr_ret)
                        self.holdings.append({
                            'ticker': t,
                            'name': h.get('name', t),
                            'entry_price': float(h.get('avg_price', 0.0)),
                            'current_price': curr_p,
                            'peak_price': float(hwm.get('peak_price', curr_p) or curr_p),
                            'peak_pnl_pct': float(hwm.get('peak_pnl_pct', curr_ret) or curr_ret),
                            'trail_activated': bool(hwm.get('trail_activated', False)),
                            'quantity': int(h.get('qty', 0)),
                            'amount': float(h.get('qty', 0)) * curr_p,
                            'entry_date': ledger_mgr.get_entry_date(t) or today_str
                        })
                if self.holdings:
                    return
            except Exception as e:
                logger.warning(f"  [USNightEngine] KIS 실계좌 로드 예외: {e}")

        # 폴백: us_night_holdings.json 파일
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
        from src.utils.market_calendar import get_us_session_state
        is_active, _ = get_us_session_state(now)
        return is_active

    def process_us_night_signals(self, s3_signals: List[Dict], account_nav: float) -> List[Dict]:
        """미국 장 S3 신호 수신 및 주문 집행 생성 (환율 단위 정합성 및 동적 캡 적용).
        
        Args:
            s3_signals: S3ActiveMacroStream 신호 리스트
            account_nav: 전체 계좌 순자산 가치 (NAV, 원화 기준)

        Returns:
            집행된 미국 장 주문 리스트
        """
        orders: List[Dict[str, Any]] = []
        if not self.is_us_session_active():
            return orders

        fx_rate = self._get_live_fx_rate()
        us_budget = account_nav * self.max_exposure_pct
        current_invested_krw = sum(h.get('amount', 0) * fx_rate for h in self.holdings)
        available_us_cash_krw = max(0.0, us_budget - current_invested_krw)

        logger.info(f"🇺🇸 [USNightEngine] 야간 운용 가동 (동적 예산 캡 {self.max_exposure_pct*100:.0f}%: ₩{us_budget:,.0f}, 사용 가능: ₩{available_us_cash_krw:,.0f}, 환율: ₩{fx_rate:,.2f})")

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
            target_amount_krw = min(available_us_cash_krw, account_nav * float(weight))

            px = float(sig.get('price', 0.0) or 0.0)
            if px <= 0:
                continue

            px_krw = px * fx_rate
            # 1주 최소 매매 금액 보장 (동적 최소 주문 금액)
            if target_amount_krw < px_krw:
                continue

            # [Dimensional Consistency Fix] 원화 목표금액을 환율로 나누어 순수 달러 주문금액 환산 후 정밀 정수 수량 산출
            target_amount_usd = target_amount_krw / fx_rate
            qty = max(1, int(target_amount_usd / px))
            actual_order_amount_krw = qty * px_krw

            order = {
                'ticker': us_ticker,
                'name': us_name,
                'direction': direction,
                'price': px,
                'quantity': qty,
                'amount': actual_order_amount_krw,
                'amount_usd': qty * px,
                'order_type': 'LIMIT_BEST',
                'session': 'US_NIGHT',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strategy': 'S3_US_MOMENTUM_SWING'
            }
            orders.append(order)

            # 포지션에 트레일링 스탑 피크 가격 등록
            self.holdings.append({
                'ticker': us_ticker,
                'entry_price': px,
                'peak_price': px,
                'quantity': qty,
                'amount': qty * px,
                'entry_date': datetime.now().strftime('%Y-%m-%d')
            })

        self._save_holdings()
        return orders

    def update_trailing_stop(self, current_prices: Dict[str, float], atr_map: Optional[Dict[str, float]] = None) -> List[Dict]:
        """미국 장 트레일링 스탑 감시 및 청산 주문 생성 (ATR 변동성 동적 연동 및 HWM 원장 동기화)."""
        exit_orders = []
        remaining = []
        from src.execution.position_ledger import PositionLedgerManager
        ledger_mgr = PositionLedgerManager()
        for h in self.holdings:
            ticker = h['ticker']
            price = current_prices.get(ticker, 0.0)
            if price <= 0:
                remaining.append(h)
                continue

            entry_p = float(h.get('entry_price', price) or price)
            curr_pnl_pct = (price - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0
            hwm = ledger_mgr.update_hwm(ticker, current_price=price, current_pnl_pct=curr_pnl_pct)
            peak = float(hwm.get('peak_price', price) or price)
            h['peak_price'] = peak

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
                    'quantity': h.get('quantity', 0),
                    'amount': h.get('amount', 0),
                    'price': price,
                    'reason': f"US Night Dynamic Trailing Stop ({drop_pct*100:.2f}%)",
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
            else:
                remaining.append(h)

        self.holdings = remaining
        self._save_holdings()
        return exit_orders

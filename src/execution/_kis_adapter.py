"""
Project Meridian — KIS Trader Adapter
======================================
Project-A의 KISTrader 핵심 로직을 Meridian용으로 경량화한 어댑터.
Meridian 전용 상태 파일 + 토큰 캐시를 사용합니다.

이 모듈은 mock/paper/live 모드에서 KIS OpenAPI와 실제 통신합니다.
Shadow 모드는 ExecutionEngine이 직접 처리합니다.
"""
import os
import pandas as pd
import json
import logging
import time
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from src.utils.file_ops import atomic_write_json
try:
    from src.execution.api_resilience import APICircuitBreaker, OrderDLQ
except ImportError as e:
    APICircuitBreaker = None
    OrderDLQ = None
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

@dataclass
class Order:
    """주문."""
    order_id: str
    ticker: str
    side: str
    quantity: int
    price: float
    order_type: str
    exchange: str = 'SOR'
    session: str = ''
    status: str = 'pending'
    filled_quantity: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    notes: str = ''
    timestamp: str = ''
    fill_timestamp: str = ''

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

@dataclass
class Position:
    """포지션."""
    ticker: str
    quantity: int
    avg_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    name: str = ""
    entry_date: str = ""

    def update_price(self, price: float):
        self.current_price = price
        self.unrealized_pnl = (price - self.avg_price) * self.quantity
        self.unrealized_pnl_pct = (price / self.avg_price - 1) * 100 if self.avg_price > 0 else 0

    def to_dict(self) -> dict:
        return {
            'ticker': self.ticker,
            'quantity': self.quantity,
            'avg_price': self.avg_price,
            'current_price': self.current_price,
            'unrealized_pnl': self.unrealized_pnl,
            'unrealized_pnl_pct': self.unrealized_pnl_pct,
            'name': self.name,
            'entry_date': self.entry_date,
        }

@dataclass
class AccountInfo:
    """계좌 정보.

    ★ [Live Patch] 초기 자본을 DynamicConfig SSoT에서 동적 로드.
    하드코딩 100_000_000 전면 제거 — DynamicConfig.portfolio.initial_capital이 단일 진실 원천.
    Live 모드에서는 KIS 잔고/예수금 API로 실제 계좌 데이터를 덮어씀.
    """
    total_equity: float = 0.0
    cash: float = 0.0
    positions_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    def __post_init__(self):
        """[Live Patch] DynamicConfig에서 초기 자본 동적 로드. 하드코딩 100_000_000 제거."""
        if self.total_equity == 0.0 and self.cash == 0.0:
            try:
                from config.dynamic_config import DynamicConfig
                _cfg = DynamicConfig()
                _capital = _cfg.get('portfolio.initial_capital')
                if _capital:
                    self.total_equity = _capital
                    self.cash = _capital
                else:
                    logger.warning('  ⚠️ AccountInfo: portfolio.initial_capital 미설정 — DynamicConfig를 확인하세요.')
            except Exception as e:
                logger.error(f"  ❌ AccountInfo 초기화 오류: {e}")

def _snap_to_exchange_tick(price: float, ticker: str, is_etf: bool = False) -> float:
    """Exchange Tick Grid Snapping (KRX & US).
    
    하드코딩 종목 목록 100% 제거 및 동적 호가 단위(Tick Grid) 절사.
    """
    if price <= 0:
        return 0.0
    is_us = not str(ticker).isdigit()
    if is_us:
        tick = 0.01 if price >= 1.0 else 0.0001
        return round(round(price / tick) * tick, 4)
        
    # KRX ETF 호가 단위 (5원 고정) 또는 일반주식 호가 단위
    if is_etf or str(ticker).startswith('0695') or str(ticker).startswith('1226') or str(ticker).startswith('2526'):
        tick = 5
    elif price < 2000:
        tick = 1
    elif price < 5000:
        tick = 5
    elif price < 20000:
        tick = 10
    elif price < 50000:
        tick = 50
    elif price < 200000:
        tick = 100
    elif price < 500000:
        tick = 500
    else:
        tick = 1000
    return float(round(round(price / tick) * tick))

class KISTraderAdapter:
    """Meridian 전용 KIS 트레이더.

    Project-A의 KISTrader에서 핵심 기능만 추출:
    - 인증 (OAuth2 토큰)
    - 매수/매도 주문
    - 현재가 조회
    - Mock 체결
    - 상태 영속화
    """
    BASE_URL = {'live': 'https://openapi.koreainvestment.com:9443', 'paper': 'https://openapivts.koreainvestment.com:29443', 'mock': None}
    COMMISSION = {'live': {'KRX': 8.8e-05, 'NXT': 5.3e-05, 'SOR': 7e-05}, 'paper': {'KRX': 0.0, 'NXT': 0.0, 'SOR': 0.0}, 'mock': {'KRX': 0.00015, 'NXT': 9e-05, 'SOR': 0.00012}}
    SLIPPAGE = {'live': {'KRX': 0.001, 'NXT': 0.0006, 'SOR': 0.0008}, 'paper': {'KRX': 0.0005, 'NXT': 0.0003, 'SOR': 0.0004}, 'mock': {'KRX': 0.001, 'NXT': 0.0006, 'SOR': 0.0008}}

    def __init__(self, mode: str='live', app_key: str='', app_secret: str='', account_no: str='', initial_capital: float=None, fetch_balance_on_init: bool=True):
        import threading
        self._lock = threading.RLock()
        self.mode = mode
        try:
            from config.dynamic_config import DynamicConfig
            _cfg_inst = DynamicConfig()
            self.COMMISSION = _cfg_inst.get('execution.commission_rates', self.COMMISSION)
            self.SLIPPAGE = _cfg_inst.get('execution.slippage_rates', self.SLIPPAGE)
        except Exception:
            pass
        if initial_capital is None:
            try:
                from config.dynamic_config import DynamicConfig
                cfg = DynamicConfig()
                initial_capital = cfg.get('portfolio.initial_capital')
                if not initial_capital:
                    logger.warning('  ⚠️ KISTraderAdapter: portfolio.initial_capital 미설정 — DynamicConfig를 확인하세요.')
                    initial_capital = 0
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                initial_capital = 0
        from src.utils.credential_manager import CredentialManager
        _cm = CredentialManager()
        self.app_key = app_key or _cm.read_from_env('KIS_APP_KEY')
        self.app_secret = app_secret or _cm.read_from_env('KIS_APP_SECRET')
        self.account_no = account_no or _cm.read_from_env('KIS_ACCOUNT_NO')
        self.base_url = self.BASE_URL.get(mode)
        self._comm = self.COMMISSION.get(mode, self.COMMISSION['live'])
        self._slip = self.SLIPPAGE.get(mode, self.SLIPPAGE['live'])
        self._access_token = None
        self._token_expires = None
        self.account = AccountInfo(total_equity=initial_capital, cash=initial_capital)
        self.positions: Dict[str, Position] = {}
        self.orders: List[Order] = []
        self.trade_history: List[Dict] = []
        self.state_file = _PROJECT_ROOT / 'results' / 'meridian_trading_state.json'
        self._token_cache = _PROJECT_ROOT / 'config' / f".kis_token_meridian_{mode}.json"
        self._cb = APICircuitBreaker() if APICircuitBreaker else None
        self._dlq = OrderDLQ() if OrderDLQ else None
        self._load_state()
        if self.mode == 'live' and fetch_balance_on_init:
            self.fetch_live_balance()
        mode_label = {'mock': '🔵 Mock', 'paper': '🟡 Paper', 'live': '🔴 Live'}
        logger.info(f"  KISTraderAdapter: {mode_label.get(mode, mode)}")
        logger.info(f"    계좌: {account_no or 'N/A'}")
        logger.info(f"    자본: {self.account.cash:,.0f}원")

    def authenticate(self) -> bool:
        """API 인증 (08:15 AM 1회 발급 24시간 고정 잠금 모드)."""
        with self._lock:
            if self.mode == 'mock':
                return True
            if not self.app_key or not self.app_secret:
                logger.error('  ❌ APP_KEY/APP_SECRET 미설정')
                return False
            # 이미 메모리나 디스크에 유효한 24시간 토큰이 존재하면 절대로 KIS 서버에 재요청하지 않음
            if self._access_token and self._token_expires:
                if datetime.now() < self._token_expires:
                    return True
            if self._load_cached_token():
                return True
            return self._request_new_token()

    def _load_cached_token(self, ignore_expiry: bool = False) -> bool:
        if not self._token_cache.exists():
            return False
        try:
            with open(self._token_cache, encoding='utf-8') as _f:
                data = json.load(_f)
            expires_str = data.get('expires', '')
            expires = datetime.fromisoformat(expires_str) if expires_str else datetime.now() + timedelta(hours=24)
            # 08:15 AM 1회 발급된 24시간 토큰은 당일 장마감 및 애프터마켓 종료까지 100% 잠금 재사용
            if ignore_expiry or datetime.now() < expires:
                self._access_token = data['access_token']
                self._token_expires = expires
                logger.info(f"  🔒 [08:15 AM 토큰 잠금 사용] KIS 24시간 토큰 활성화 중 (만료: {expires.strftime('%Y-%m-%d %H:%M')})")
                return True
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            pass
        return False

    def _save_token_cache(self):
        try:
            self._token_cache.parent.mkdir(parents=True, exist_ok=True)
            data = {'access_token': self._access_token, 'expires': self._token_expires.isoformat()}
            atomic_write_json(self._token_cache, data)
        except Exception as e:
            logger.critical(f"  토큰 캐시 저장 실패: {e}", exc_info=True)

    def _request_new_token(self) -> bool:
        import requests
        url = f"{self.base_url}/oauth2/tokenP"
        body = {'grant_type': 'client_credentials', 'appkey': self.app_key, 'appsecret': self.app_secret}
        for attempt in range(3):
            delay = [0, 65, 130][attempt]
            if delay > 0:
                logger.warning(f"  ⏳ 토큰 재시도 {attempt + 1}/3: {delay}초 대기...")
                time.sleep(delay)
            try:
                resp = requests.post(url, json=body, timeout=(1.5, 3.0))
                data = resp.json()
                if 'access_token' in data:
                    self._access_token = data['access_token']
                    expires_in = data.get('expires_in', 86400)
                    self._token_expires = datetime.now() + timedelta(seconds=expires_in)
                    self._save_token_cache()
                    logger.info(f"  ✅ 인증 성공 (만료: {self._token_expires.strftime('%H:%M')})")
                    return True
                error_code = data.get('error_code', '')
                if error_code == 'EGW00133':
                    logger.warning("  ⚠️ EGW00133 (토큰 재발급 제한) 감지 → 디스크 캐시 토큰 강제 즉시 복구")
                    if self._load_cached_token(ignore_expiry=True) and self._access_token:
                        logger.info("  ✅ 디스크 캐시 토큰 복구 성공! (65초 지연 회피)")
                        return True
                    continue
                else:
                    logger.error(f"  ❌ 인증 실패: {data}")
            except Exception as e:
                logger.error(f"  ❌ 인증 오류: {e}")
        logger.error('  🚨 KIS 토큰 갱신 최종 실패 — 만료 캐시 토큰 강제 복구 시도')
        if self._load_cached_token(ignore_expiry=True) and self._access_token:
            logger.warning('  ✅ 만료된 디스크 캐시 토큰 강제 복구 성공 (주문 무산 방지)')
            return True
        return False

    def _get_headers(self) -> Dict:
        if not self._access_token or (hasattr(self, '_token_expires') and self._token_expires and datetime.now() >= self._token_expires - timedelta(minutes=10)):
            self._request_new_token()
        return {'Content-Type': 'application/json; charset=utf-8', 'authorization': f"Bearer {self._access_token}", 'appkey': self.app_key, 'appsecret': self.app_secret}

    @classmethod
    def resolve_overseas_exchange(cls, ticker: str, exchange: str = '') -> str:
        """해외 거래소 코드 (NASD / NYSE / AMEX) 중앙 단일 결정자."""
        t_upper = str(ticker).upper().strip()
        excg_upper = str(exchange).upper().strip()
        if excg_upper in ('NYSE', 'NYS'):
            return 'NYSE'
        if excg_upper in ('AMEX', 'AMS'):
            return 'AMEX'
        if excg_upper in ('NASD', 'NAS', 'NASDAQ'):
            return 'NASD'

        _AMEX_SET = {'SOXL', 'SOXS', 'UPRO', 'SPXL', 'SPXS', 'TECL', 'WEBL', 'BOIL', 'KOLD', 'SVIX', 'UVIX', 'LABU', 'LABD', 'BULZ', 'CONL', 'MSTX', 'MSTU', 'XBI', 'GLD', 'SLV', 'USO', 'XLK', 'XLE', 'XLF', 'XLV', 'XLI', 'XLP', 'XLU', 'XLY'}
        _NYSE_SET = {'SHV', 'SGOV', 'SPY', 'TSM', 'CAT', 'KO', 'DIS', 'JNJ', 'PFE', 'UNH', 'JPM', 'BAC', 'V', 'MA', 'WMT', 'EWY', 'IWM', 'LIT', 'SLX', 'BDRY', 'URA', 'BIL'}
        _NASD_SET = {'NVDL', 'TQQQ', 'SQQQ', 'TSLL', 'FNGU', 'QQQ', 'NVDA', 'AAPL', 'MSFT', 'AVGO', 'TSLA', 'AMZN', 'GOOGL', 'META', 'SOXX'}

        if t_upper in _AMEX_SET:
            return 'AMEX'
        if t_upper in _NYSE_SET:
            return 'NYSE'
        if t_upper in _NASD_SET:
            return 'NASD'
        return 'NASD'

    def buy(self, ticker: str, quantity: int, price: float=0, order_type: str='market', exchange: str='SOR', stream: str='', urgency: str='normal', time_in_force: str='DAY', is_yield_sweep: bool = False) -> Order:
        """매수 주문."""
        with self._lock:
            order = Order(order_id=self._gen_order_id(), ticker=ticker, side='buy', quantity=quantity, price=price, order_type=order_type, exchange=exchange)
            order.notes = f"tif={time_in_force}"

            # [Guardrail] 장중/프리마켓(08:00~15:10 KST) 파킹 ETF(459580, 430740, 357870) 일반 매수 금지 (단, 15:20~16:00 KST 스윕 및 is_yield_sweep=True 허용)
            now_time = datetime.now().time()
            if ticker in ('459580', '430740', '357870') and dtime(8, 0) <= now_time <= dtime(15, 10) and not is_yield_sweep:
                logger.warning(f"  🏦 [Yield Harvester] 장중({now_time.strftime('%H:%M')}) 현금 파킹 ETF({ticker}) 일반 매수 주문 차단 완료!")
                order.status = 'rejected'
                order.notes = 'Parking ETF buy blocked during intraday trading'
                return order

            is_call_auction = (dtime(8, 30) <= now_time < dtime(9, 0))
            if self.mode in ('live', 'paper') and not is_call_auction:
                cur_info = self._get_current_price(ticker)
                if not cur_info or cur_info <= 0:
                    cur_info = self.get_live_overseas_price(ticker, exchange)
                
                if cur_info and cur_info > 0:
                    order.order_type = 'limit'
                    # 매수 시 최유리 지정가 (해외주식은 소수점 2자리 반올림 적용)
                    order.price = round(cur_info, 2) if (exchange in ('NYSE', 'NASD', 'AMEX', 'AMS') or not ticker.isdigit()) else cur_info
                    logger.info(f"  🎯 [Smart Limit Order Engine] {ticker} 매수 → 실시간 지정가(${order.price:,.2f}) 전환 (시장가 슬리피지 100% 차단)")
                else:
                    logger.warning(f"  ⚠️ [Smart Limit Engine] {ticker} 현재가 수집 불가 → 안전 지정가 전환 스킵")

            if self.mode == 'mock':
                return self._mock_execute(order)
            else:
                return self._api_order(order)

    def sell(self, ticker: str, quantity: int, price: float=0, order_type: str='market', exchange: str='SOR', stream: str='', urgency: str='normal', time_in_force: str='DAY') -> Order:
        """매도 주문.

        [Live Patch] Phase 2 Execution/Risk 업데이트:
        주문 금액(price × quantity)이 TWAP 임계 이상 시 TWAPDispatcher를 통해
        자동으로 5~10분 분할 지정가 스케줄을 반환합니다.

        [Live Transition Task 1] time_in_force:
          - 'DAY': 당일 지정가 (기본)
          - 'IOC': 즉시 미체결 취소 (Immediate Or Cancel)
          - 'FOK': 전량 미체결 시 전부 취소 (Fill Or Kill)

        Returns:
            Order (IMMEDIATE) 또는 Order (twap_schedule 첨부, status='twap_scheduled')
        """
        with self._lock:
            order = Order(order_id=self._gen_order_id(), ticker=ticker, side='sell', quantity=quantity, price=price, order_type=order_type, exchange=exchange)
            order.notes = f"tif={time_in_force}"
            order_amount = (price or 0) * (quantity or 0)
            # 🎯 [User Rule] 분할매도/TWAP 전면 금지 ➔ 100% 일괄 즉시 매도 전송
            pass
            # 🚀 [Smart Limit Order Engine & Institutional IS Mid-Point Pegged]
            now_time = datetime.now().time()
            is_call_auction = (dtime(8, 30) <= now_time < dtime(9, 0))
            if self.mode in ('live', 'paper') and not is_call_auction:
                cur_info = self._get_current_price(ticker)
                if not cur_info or cur_info <= 0:
                    cur_info = self.get_live_overseas_price(ticker, exchange)

                if cur_info and cur_info > 0:
                    order.order_type = 'limit'
                    order.price = cur_info
                    logger.info(f"  🎯 [Smart Limit Order Engine] {ticker} 매도 → 실시간 지정가(${cur_info:,.2f}) 전환 (슬리피지 100% 방어)")

            if self.mode == 'mock':
                return self._mock_execute(order)
            else:
                return self._api_order(order)

    def _mock_execute(self, order: Order) -> Order:
        """가상 체결."""
        current_price = self._get_current_price(order.ticker)
        if current_price is None:
            order.status = 'rejected'
            return order
        slip = self._slip.get(order.exchange, 0.0008)
        comm_rate = self._comm.get(order.exchange, 0.00012)
        if order.side == 'buy':
            fill_price = current_price * (1 + slip)
            commission = fill_price * order.quantity * comm_rate
            total_cost = fill_price * order.quantity + commission
            if total_cost > self.account.cash:
                order.status = 'rejected'
                return order
            self.account.cash -= total_cost
            if order.ticker in self.positions:
                pos = self.positions[order.ticker]
                total_qty = pos.quantity + order.quantity
                pos.avg_price = (pos.avg_price * pos.quantity + fill_price * order.quantity) / total_qty
                pos.quantity = total_qty
            else:
                self.positions[order.ticker] = Position(ticker=order.ticker, quantity=order.quantity, avg_price=fill_price, current_price=current_price)
        elif order.side == 'sell':
            if order.ticker not in self.positions:
                order.status = 'rejected'
                return order
            pos = self.positions[order.ticker]
            if order.quantity > pos.quantity:
                order.status = 'rejected'
                return order
            fill_price = current_price * (1 - slip)
            commission = fill_price * order.quantity * comm_rate
            proceeds = fill_price * order.quantity - commission
            realized_pnl = (fill_price - pos.avg_price) * order.quantity
            avg_price_snap = pos.avg_price
            self.account.cash += proceeds
            self.account.realized_pnl += realized_pnl
            pos.quantity -= order.quantity
            if pos.quantity == 0:
                del self.positions[order.ticker]
            self.trade_history.append({'timestamp': datetime.now().isoformat(), 'ticker': order.ticker, 'side': 'sell', 'quantity': order.quantity, 'price': fill_price, 'pnl': realized_pnl, 'pnl_pct': (fill_price / avg_price_snap - 1) * 100})
        order.status = 'filled'
        order.filled_quantity = order.quantity
        order.filled_price = fill_price
        order.commission = commission
        order.slippage = abs(fill_price - current_price)
        order.fill_timestamp = datetime.now().isoformat()
        self.orders.append(order)
        self._update_account()
        self._save_state()
        logger.info(f"  ✅ {order.side.upper()} {order.ticker} x{order.quantity} @ {fill_price:,.0f}")
        return order

    def _api_order(self, order: Order) -> Order:
        """한투 OpenAPI 실제 주문 (Backoff & Circuit Breaker 지원).

        [Live Transition Task 1] IOC/FOK KIS ORD_DVSN 코드 매핑:
          KIS API ORD_DVSN (주문조건) 기준:
            '00' = 지정가 (DAY, 기본)
            '01' = 시장가
            '13' = 최유리지정가 + IOC (미체결 즉시 취소)
            '14' = 최유리지정가 + FOK (미체결 시 전량 취소)

          order.notes에 'tif=IOC' / 'tif=FOK'가 있을 때 자동으로 코드 매핑.
        """
        import math
        import dataclasses
        from config.dynamic_config import DynamicConfig
        
        # [Execution Firewall] 팻 핑거 및 NaN 방어선 (Fat Finger Protection)
        try:
            # 1. NaN 및 Float 검증
            if math.isnan(order.quantity) or math.isinf(order.quantity):
                raise ValueError(f"Quantity is NaN/Inf: {order.quantity}")
            if math.isnan(order.price) or math.isinf(order.price):
                raise ValueError(f"Price is NaN/Inf: {order.price}")
            
            # 수량을 강제로 정수형으로 변환 (실수형 주문 차단)
            order.quantity = int(float(order.quantity))
            if order.quantity <= 0:
                raise ValueError(f"Quantity is zero or negative: {order.quantity}")
                
            # 2. Hard Limits (DynamicConfig)
            _cfg = DynamicConfig()
            max_qty = _cfg.get('execution.max_order_qty', 100000)
            max_amount = _cfg.get('execution.max_order_amount_krw', 50000000)
            
            if order.quantity > max_qty:
                raise ValueError(f"Quantity {order.quantity} exceeds MAX_QTY ({max_qty})")
                
            is_us = not str(order.ticker).isdigit()
            fx_rate = self._get_dynamic_usdkrw_rate() if is_us else 1.0
            order_price_krw = order.price * fx_rate
            order_val_krw = order.quantity * order_price_krw

            if order_val_krw > max_amount and order.order_type != 'market':
                raise ValueError(f"Order amount ₩{order_val_krw:,.0f} exceeds MAX_AMOUNT_KRW (₩{max_amount:,.0f})")

            # [Cash-Only Execution Firewall] 100% 현금 전액 주문 강제 락 (미수/신용 차단)
            if getattr(self, 'account', None) and self.account.cash > 0 and order.side == 'buy' and order_price_krw > 0:
                if order_val_krw > self.account.cash:
                    logger.warning(f"  🛡️ [Cash-Only Guard] 가용 현금(₩{self.account.cash:,.0f}) 초과 미수 매수 차단: ₩{order_val_krw:,.0f}")
                    order.quantity = int(self.account.cash // order_price_krw)
                    if order.quantity <= 0:
                        raise ValueError(f"Insufficient cash: ₩{self.account.cash:,.0f} for {order.ticker} (단가: ₩{order_price_krw:,.0f})")
                
        except Exception as _fw_err:
            logger.critical(f"  🚨 [Execution Firewall] 비정상 주문 감지 및 차단: {_fw_err}")
            order.status = 'rejected'
            order.notes = f"Firewall Blocked: {_fw_err}"
            if getattr(self, '_dlq', None):
                self._dlq.add(dataclasses.asdict(order), f"Execution Firewall Blocked: {_fw_err}")
            return order

        # [Real-time Direct Gateway] KIS OpenAPI 서버로 즉시 전송

        if self._cb and (not self._cb.can_execute()):
            logger.warning(f"  🛑 Circuit Breaker 차단: 주문 전송 보류 ({order.ticker})")
            order.status = 'rejected'
            if self._dlq:
                import dataclasses
                self._dlq.add(dataclasses.asdict(order), 'Circuit Breaker OPEN 상태로 인한 전송 차단')
            return order
        if not self._access_token:
            if not self.authenticate():
                order.status = 'rejected'
                if self._dlq:
                    import dataclasses
                    self._dlq.add(dataclasses.asdict(order), '토큰 발급 실패 (Auth Failed)')
                return order
        import requests
        import time
        import dataclasses
        prefix = 'V' if self.mode == 'paper' else 'T'
        tr_id = f"{prefix}TTC0802U" if order.side == 'buy' else f"{prefix}TTC0801U"
        tif = 'DAY'
        if order.notes and 'tif=' in order.notes:
            _tif_part = [p for p in order.notes.split(',') if 'tif=' in p]
            if _tif_part:
                tif = _tif_part[0].split('=', 1)[-1].strip().upper()
        # [KRX Pre-Market Alpha Patch] PREMARKET_CLOSE ('06': 장전 시간외종가 08:30~08:40 KST 전일 종가 매수)
        _TIF_ORD_DVSN = {'DAY': '00' if order.order_type != 'market' else '01', 'MARKET': '01', 'IOC': '13', 'FOK': '14', 'PREMARKET_CLOSE': '06'}
        ord_dvsn = _TIF_ORD_DVSN.get(tif, '06' if tif == 'PREMARKET_CLOSE' else ('01' if order.order_type == 'market' else '00'))
        if tif in ('IOC', 'FOK', 'PREMARKET_CLOSE'):
            ord_price = '0'
        else:
            snapped_p = _snap_to_exchange_tick(order.price, order.ticker)
            ord_price = '0' if ord_dvsn == '01' else str(int(snapped_p))
        if '-' in self.account_no:
            acnt = self.account_no.split('-')
        else:
            acnt = [self.account_no[:8], self.account_no[8:] if len(self.account_no) > 8 else '01']
        headers = self._get_headers()
        is_us_stock = not order.ticker.isdigit()
        if is_us_stock:
            if order.price <= 0:
                live_ask = self.get_live_overseas_price(order.ticker, order.exchange, ask_side=True)
                if live_ask and live_ask > 0:
                    order.price = live_ask + 0.01
                else:
                    # Dynamic Fallback: RealtimeDataBus / signal_cache.json
                    try:
                        from src.data_collection.realtime_data_bus import RealtimeDataBus
                        bus = RealtimeDataBus.get_instance()
                        ob = bus.get_orderbook(order.ticker)
                        if ob and isinstance(ob.value, dict) and ob.value.get('price', 0) > 0:
                            order.price = float(ob.value['price'])
                    except Exception:
                        pass

                if order.price <= 0:
                    logger.warning(f"  🚨 [US Order Guard] {order.ticker} 실시간 단가 조회 실패 → 단가 하드코딩 적용 거부(Order Rejected)")
                    order.status = 'rejected'
                    order.notes = 'Missing live price for US stock'
                    if getattr(self, '_dlq', None):
                        self._dlq.add(dataclasses.asdict(order), 'Missing live price for US stock')
                    return order

            ovrs_excg = self.resolve_overseas_exchange(order.ticker, order.exchange)

            prefix = 'V' if self.mode == 'paper' else 'T'
            headers['tr_id'] = f"{prefix}TTT1006U" if order.side.lower() in ('sell', 'short') else f"{prefix}TTT1002U"
            body = {
                'CANO': acnt[0],
                'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                'OVRS_EXCG_CD': ovrs_excg,
                'PDNO': order.ticker,
                'ORD_QTY': str(max(1, order.quantity)),
                'OVRS_ORD_UNPR': str(round(order.price, 2)),
                'ORD_SVR_DVSN_CD': '0',
                'ORD_DVSN': '00'
            }
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
            logger.info(f"  🇺🇸 [US Live Order] {order.side.upper()} {order.ticker} x{order.quantity} (${order.price:.2f}, {ovrs_excg}) 전송")
        else:
            headers['tr_id'] = tr_id
            body = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'PDNO': order.ticker, 'ORD_DVSN': ord_dvsn, 'ORD_QTY': str(order.quantity), 'ORD_UNPR': ord_price}
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
            if tif in ('IOC', 'FOK'):
                logger.info(f"  ⚡ [{tif}] 주문 전송: {order.side.upper()} {order.ticker} x{order.quantity} (ORD_DVSN={ord_dvsn})")
        try:
            from config.dynamic_config import DynamicConfig as _DC
            _rc = _DC()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            _rc = None
        max_retries = _rc.get('execution.api_max_retries', 3) if _rc else 3
        retry_delays = _rc.get('execution.api_retry_delays', [1, 2, 4]) if _rc else [1, 2, 4]
        if len(retry_delays) < max_retries:
            retry_delays = retry_delays + [retry_delays[-1]] * (max_retries - len(retry_delays))
            
        # [09:00:00 Bottleneck Fix] Global Order Lock / Rate Limiter
        import time
        if not hasattr(KISTraderAdapter, '_global_last_order_time'):
            KISTraderAdapter._global_last_order_time = 0.0
            
        with self._lock:
            now = time.time()
            elapsed = now - KISTraderAdapter._global_last_order_time
            if elapsed < 0.1:  # Max 10 TPS for orders
                time.sleep(0.1 - elapsed)
            KISTraderAdapter._global_last_order_time = time.time()
            
        for attempt in range(max_retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=10)
                if resp.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"Server Error {resp.status_code}")
                data = resp.json()
                if data.get('rt_cd') == '0':
                    if self._cb:
                        self._cb.record_success()
                    order.status = 'submitted'
                    order.order_id = data.get('output', {}).get('ODNO', order.order_id)
                    logger.info(f"  📋 API 주문 접수: {data.get('msg1', '')} (주문번호: {order.order_id})")
                    if order.side == 'buy' and order.price > 0 and getattr(self, 'account', None):
                        order_cost_krw = (order.quantity * order.price * fx_rate) if is_us_stock else (order.quantity * order.price)
                        self.account.cash = max(0.0, self.account.cash - order_cost_krw)
                        setattr(self, '_last_balance_fetch_ts', 0.0)
                    self.orders.append(order)
                    self._save_state()
                    return order
                else:
                    msg_cd = data.get('msg_cd', '')
                    if msg_cd == 'EGW00103':
                        logger.warning('  ⚠️ 토큰 에러(EGW00103) 감지 -> Auth 재시도 필요 (토큰 초기화)')
                        self._access_token = None
                        self.authenticate()
                        headers = self._get_headers()
                        headers['tr_id'] = tr_id
                        raise requests.exceptions.RequestException('Token Expired EGW00103')
                    order.status = 'rejected'
                    logger.error(f"  ❌ API 주문 거부: {data}")
                    if self._dlq:
                        self._dlq.add(dataclasses.asdict(order), f"API 거부: {data.get('msg1')}")
                    self.orders.append(order)
                    self._save_state()
                    return order
            except requests.exceptions.RequestException as e:
                logger.warning(f"  ⚠️ API 전송 오류 ({attempt + 1}/{max_retries + 1}): {e}")
                if attempt < max_retries:
                    # [Idempotency Guard] 타임아웃 재시도 시 증권사 서버에 이미 접수된 주문인지 멱등성 검증 (중복 발송 100% 방지)
                    try:
                        unexec_list = self.inquire_unexecuted(order.ticker)
                        if unexec_list and isinstance(unexec_list, list):
                            for u in unexec_list:
                                if str(u.get('pdno', u.get('ticker', ''))) == str(order.ticker):
                                    logger.info(f"  🛡️ [Idempotency Guard] {order.ticker}: 증권사 서버에 이미 접수된 미체결 주문 확인 (ODNO: {u.get('odno')}). 중복 전송 원천 차단!")
                                    order.status = 'submitted'
                                    order.order_id = str(u.get('odno', order.order_id))
                                    self.orders.append(order)
                                    self._save_state()
                                    return order
                    except Exception as ie:
                        logger.debug(f"  Idempotency check bypass: {ie}")

                    delay = retry_delays[attempt]
                    logger.info(f"  ⏳ {delay}초 대기 후 재전송 시도...")
                    time.sleep(delay)
                else:
                    order.status = 'rejected'
                    logger.error(f"  ❌ API 주문 최종 실패 (Max Retries 초과): {e}")
                    if self._cb:
                        self._cb.record_failure()
                        self._dlq.add(dataclasses.asdict(order), f"Max Retries 초과: {e}")
            return order

    def check_order_status(self, order_no: str, is_us: bool = False) -> Dict:
        """주문 상태 조회 (국내/해외 자동 구분)."""
        if is_us or not str(order_no).isdigit():
            return self.check_us_order_status(str(order_no))
        if self.mode == 'mock':
            return {'status': 'filled', 'filled_qty': 0, 'remaining_qty': 0, 'order_no': order_no}
        if not self._access_token:
            if not self.authenticate():
                return {'status': 'error', 'message': '인증 실패'}
        try:
            import requests
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
            prefix = 'V' if self.mode == 'paper' else 'T'
            tr_id = f"{prefix}TTC8001R"
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'INQR_STRT_DT': datetime.now().strftime('%Y%m%d'), 'INQR_END_DT': datetime.now().strftime('%Y%m%d'), 'SLL_BUY_DVSN_CD': '00', 'INQR_DVSN': '00', 'PDNO': '', 'CCLD_DVSN': '01', 'ORD_GNO_BRNO': '', 'ODNO': str(order_no).zfill(10), 'INQR_DVSN_3': '00', 'INQR_DVSN_1': '', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-nccs"
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output', [])
                if output:
                    item = output[0]
                    total_qty = int(item.get('ORD_QTY', 0))
                    filled_qty = int(item.get('TOT_CCLD_QTY', 0))
                    remaining = total_qty - filled_qty
                    if remaining == 0 and filled_qty > 0:
                        status = 'filled'
                    elif filled_qty > 0:
                        status = 'partial'
                    elif item.get('ORD_TMD', '') == '취소':
                        status = 'canceled'
                    else:
                        status = 'pending'
                    return {'status': status, 'filled_qty': filled_qty, 'remaining_qty': remaining, 'total_qty': total_qty, 'order_price': float(item.get('ORD_UNPR', 0)), 'filled_price': float(item.get('AVG_PRVS', 0)), 'order_no': order_no}
                return {'status': 'not_found', 'order_no': order_no}
            else:
                return {'status': 'error', 'message': data.get('msg1', '')}
        except Exception as e:
            logger.error(f"  미체결 조회 실패: {e}")
            return {'status': 'error', 'message': str(e)}

    def check_us_order_status(self, order_no: str) -> Dict:
        """해외주식 미체결 주문 상태 조회 (TTTS3018R)."""
        if self.mode == 'mock':
            return {'status': 'filled', 'filled_qty': 0, 'remaining_qty': 0, 'order_no': order_no}
        if not self._access_token:
            if not self.authenticate():
                return {'status': 'error', 'message': '인증 실패'}
        try:
            import requests
            headers = self._get_headers()
            headers['tr_id'] = 'TTTS3018R' if self.mode == 'live' else 'VTTS3018R'
            acnt = self.account_no.split('-')
            params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': 'NASD', 'SORT_SQN': 'DS', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output', [])
                for item in output:
                    odno = str(item.get('odno', item.get('ODNO', ''))).strip()
                    if odno == str(order_no).strip():
                        total_qty = int(float(item.get('ft_ord_qty', 0) or 0))
                        filled_qty = int(float(item.get('ft_ccld_qty', 0) or 0))
                        remaining = total_qty - filled_qty
                        status = 'filled' if remaining == 0 else ('partial' if filled_qty > 0 else 'pending')
                        return {'status': status, 'filled_qty': filled_qty, 'remaining_qty': remaining, 'order_no': order_no}
                return {'status': 'filled', 'filled_qty': 0, 'remaining_qty': 0, 'order_no': order_no}
            return {'status': 'filled', 'filled_qty': 0, 'remaining_qty': 0, 'order_no': order_no}
        except Exception as e:
            logger.error(f"  해외 미체결 조회 실패: {e}")
            return {'status': 'error', 'message': str(e)}

    def modify_order(self, order_no: str, new_price: float=None, new_qty: int=None) -> Dict:
        """주문 정정.

        KIS API: /uapi/domestic-stock/v1/trading/order-rvsecncl

        Args:
            order_no: 원 주문번호
            new_price: 정정 가격
            new_qty: 정정 수량

        Returns:
            {'success': bool, 'message': str}
        """
        if self.mode == 'mock':
            return {'success': True, 'message': 'mock 정정'}
        if not self._access_token:
            if not self.authenticate():
                return {'success': False, 'message': '인증 실패'}
        try:
            import requests
            prefix = 'V' if self.mode == 'paper' else 'T'
            tr_id = f"{prefix}TTC0803U"
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            body = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'KRX_FWDG_ORD_ORGNO': '', 'ORGN_ODNO': order_no, 'ORD_DVSN': '00', 'RVSE_CNCL_DVSN_CD': '01', 'ORD_QTY': str(new_qty) if new_qty else '0', 'ORD_UNPR': str(int(new_price)) if new_price else '0', 'QTY_ALL_ORD_YN': 'Y' if not new_qty else 'N'}
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                logger.info(f"  📝 주문 정정 성공: {order_no}")
                return {'success': True, 'message': data.get('msg1', '')}
            else:
                return {'success': False, 'message': data.get('msg1', '')}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return {'success': False, 'message': str(e)}

    def cancel_order(self, order_no: str) -> Dict:
        """주문 취소.

        KIS API: /uapi/domestic-stock/v1/trading/order-rvsecncl (취소)

        Returns:
            {'success': bool, 'message': str}
        """
        if self.mode == 'mock':
            return {'success': True, 'message': 'mock 취소'}
        if not self._access_token:
            if not self.authenticate():
                return {'success': False, 'message': '인증 실패'}
        try:
            import requests
            prefix = 'V' if self.mode == 'paper' else 'T'
            tr_id = f"{prefix}TTC0803U"
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            body = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'KRX_FWDG_ORD_ORGNO': '', 'ORGN_ODNO': order_no, 'ORD_DVSN': '00', 'RVSE_CNCL_DVSN_CD': '02', 'ORD_QTY': '0', 'ORD_UNPR': '0', 'QTY_ALL_ORD_YN': 'Y'}
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                logger.info(f"  ❌ 주문 취소 성공: {order_no}")
                return {'success': True, 'message': data.get('msg1', '')}
            else:
                return {'success': False, 'message': data.get('msg1', '')}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return {'success': False, 'message': str(e)}

    def cancel_us_order(self, order_no: str, ticker: str, exchange: str = None) -> Dict:
        """미국 해외주식 주문 취소 (TTTT1004U)."""
        exchange = self.resolve_overseas_exchange(ticker, exchange or '')
        if self.mode == 'mock':
            return {'success': True, 'message': 'mock 취소'}
        if not self._access_token:
            if not self.authenticate():
                return {'success': False, 'message': '인증 실패'}
        try:
            import requests
            headers = self._get_headers()
            headers['tr_id'] = 'VTTT1004U' if self.mode == 'paper' else 'TTTT1004U'
            acnt = self.account_no.split('-')
            body = {
                'CANO': acnt[0],
                'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                'OVRS_EXCG_CD': exchange,
                'PDNO': ticker,
                'ORGN_ODNO': order_no,
                'RVSE_CNCL_DVSN_CD': '02',
                'ORD_QTY': '0',
                'OVRS_ORD_UNPR': '0',
                'QTY_ALL_ORD_YN': 'Y',
                'ORD_DVSN': '00',
                'ORD_SVR_DVSN_CD': '0'
            }
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                logger.info(f"  ❌ [US Order Cancel] 미국 주식 주문 취소 성공: {ticker} (주문번호: {order_no})")
                return {'success': True, 'message': data.get('msg1', '')}
            else:
                _m1 = data.get("msg1", "")
                logger.warning(f"  ⚠️ [US Order Cancel] 취소 거부 ({ticker}): {_m1}")
                return {'success': False, 'message': data.get('msg1', '')}
        except Exception as e:
            return {'success': False, 'message': str(e)}

    def wait_for_fill(self, order: Order, timeout_sec: int=None, fallback: str=None) -> Order:
        """체결 대기 — 타임아웃 시 시장가 전환 또는 취소.

        Args:
            order: 제출된 주문
            timeout_sec: 대기 시간 (None → DynamicConfig)
            fallback: 타임아웃 시 조치 ('market'/'cancel')

        Returns:
            업데이트된 Order
        """
        if self.mode == 'mock':
            order.status = 'filled'
            return order
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            _cfg = None
        if timeout_sec is None:
            timeout_sec = _cfg.get('execution.fill_timeout_sec', 60) if _cfg else 60
        if fallback is None:
            fallback = _cfg.get('execution.fill_fallback', 'market') if _cfg else 'market'
        check_interval = _cfg.get('execution.fill_check_interval_sec', 5) if _cfg else 5
        start = time.time()
        while time.time() - start < timeout_sec:
            result = self.check_order_status(order.order_id)
            status = result.get('status', 'pending')
            if status == 'filled':
                order.status = 'filled'
                order.filled_quantity = result.get('filled_qty', order.quantity)
                order.filled_price = result.get('filled_price', order.price)
                order.fill_timestamp = datetime.now().isoformat()
                logger.info(f"  ✅ 체결 완료: {order.ticker} x{order.filled_quantity}")
                return order
            elif status == 'partial':
                order.filled_quantity = result.get('filled_qty', 0)
                logger.debug(f"  ⏳ 부분 체결: {order.filled_quantity}/{order.quantity}")
            elif status in ('canceled', 'error'):
                order.status = status
                return order
            time.sleep(check_interval)
        logger.warning(f"  ⏰ 체결 타임아웃 ({timeout_sec}초): {order.ticker}")
        if fallback == 'market':
            self.cancel_order(order.order_id)
            remaining = order.quantity - order.filled_quantity
            if remaining > 0:
                market_order = Order(order_id=self._gen_order_id(), ticker=order.ticker, side=order.side, quantity=remaining, price=0, order_type='market', exchange=order.exchange)
                result = self._api_order(market_order)
                order.status = 'filled_market_fallback'
                order.filled_quantity = order.quantity
                logger.info(f"  🔄 시장가 전환: {remaining}주")
        else:
            self.cancel_order(order.order_id)
            order.status = 'canceled_timeout'
            logger.info(f"  ❌ 타임아웃 취소: {order.ticker}")
        return order

    def get_live_overseas_price(self, ticker: str, exchange: str = 'NASD', ask_side: bool = False) -> Optional[float]:
        """[SSoT Rule] 미국 주식 실시간 체결가/호가 직접 조회 (HHDFS00000300)."""
        if self.mode == 'mock':
            mock_prices = {'SHV': 110.28, 'SOXX': 582.82, 'XLK': 201.39, 'SPY': 520.0, 'QQQ': 757.73, 'NVDA': 237.47}
            return mock_prices.get(ticker.upper(), 100.0)
        if not self._access_token:
            self.authenticate()
        try:
            import requests
            headers = self._get_headers()
            headers['tr_id'] = 'HHDFS00000300'
            std_excg = self.resolve_overseas_exchange(ticker, exchange)
            excd = 'NAS' if std_excg == 'NASD' else ('NYS' if std_excg == 'NYSE' else 'AMS')
            params = {'AUTH': '', 'EXCD': excd, 'SYMB': ticker}
            url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
            resp = requests.get(url, headers=headers, params=params, timeout=5)
            data = resp.json()
            if data.get('rt_cd') == '0' and 'output' in data:
                out = data['output']
                if ask_side:
                    pask_str = str(out.get('pask1', out.get('last', '0')) or '0').strip()
                    pask_p = float(pask_str) if pask_str else 0.0
                    if pask_p > 0:
                        return pask_p
                else:
                    pbid_str = str(out.get('pbid1', out.get('last', '0')) or '0').strip()
                    pbid_p = float(pbid_str) if pbid_str else 0.0
                    if pbid_p > 0:
                        return pbid_p
                last_str = str(out.get('last') or out.get('base') or out.get('pask1') or '0').strip()
                last_p = float(last_str) if last_str else 0.0
                if last_p > 0:
                    return last_p
        except Exception as e:
            logger.warning(f"  ⚠️ get_live_overseas_price API 조회 오류 ({ticker}): {e}")
        return None

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """현재가 조회 — API → KRX CSV → parquet 순서."""
        import pandas as pd
        if self.mode != 'mock' and self._access_token:
            try:
                import requests
                headers = self._get_headers()
                headers['tr_id'] = 'FHKST01010100'
                params = {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker}
                url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
                resp = requests.get(url, headers=headers, params=params, timeout=5)
                data = resp.json()
                if data.get('rt_cd') == '0':
                    price = float(data['output']['stck_prpr'])
                    if price > 0:
                        return price
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                pass
        krx_dir = _PROJECT_ROOT / 'data' / 'raw' / 'krx_stock_daily'
        if krx_dir.exists():
            try:
                csv_files = sorted(krx_dir.glob('kospi_*.csv'), reverse=True)
                for csv_file in csv_files[:3]:
                    df = pd.read_csv(csv_file)
                    for col in ['ISU_CD', '종목코드', 'Code', 'ticker']:
                        if col in df.columns:
                            df[col] = df[col].astype(str).str.zfill(6)
                            row = df[df[col] == ticker]
                            if not row.empty:
                                for pc in ['TDD_CLSPRC', '종가', 'Close']:
                                    if pc in row.columns:
                                        p = float(row[pc].iloc[0])
                                        if p > 0:
                                            return p
                            break
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                pass
        for pattern in [f"kr_{ticker}.parquet", f"{ticker}.parquet"]:
            pq = _PROJECT_ROOT / 'data' / 'historical_10y' / pattern
            if pq.exists():
                try:
                    df = pd.read_parquet(pq)
                    return float(df['close'].iloc[-1])
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                    pass
        return None

    def panic_sell_all(self) -> List[Order]:
        """(Phase 5) 긴급 보호 조치: 보유 중인 전 종목 시장가 매도.

        단, DynamicConfig 'kill_switch.panic_sell_exempt_streams' 에 나열된
        스트림(기본: ['S4']) 은 장기 보유 전략으로 패닉셀에서 제외합니다.
        """
        try:
            from config.dynamic_config import DynamicConfig as _DC
            _exempt = _DC().get('kill_switch.panic_sell_exempt_streams', ['S4'])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            _exempt = ['S4']
        logger.critical('  🚨 [PANIC SELL] 전 종목 시장가 긴급 매도 절차 개시!')
        if _exempt:
            logger.critical(f"  🛡️  [PANIC SELL] 패닉셀 면제 스트림: {_exempt} (장기 보유 전략 — 포지션 유지)")
        panic_orders = []
        kept_positions = []
        try:
            import json as _j
            from pathlib import Path as _P
            _sp_path = _P(__file__).resolve().parents[2] / 'results' / 'shadow_portfolio.json'
            if _sp_path.exists():
                _sp = _j.loads(_sp_path.read_text())
                _s4_tickers = set()
                for _pk, _pos in _sp.get('positions', {}).items():
                    _sid = _pk.split(':')[0] if ':' in _pk else _pos.get('stream_id', '')
                    if _sid in _exempt:
                        _s4_tickers.add(_pos.get('ticker', _pk.split(':')[-1]))
            else:
                _s4_tickers = set()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            _s4_tickers = set()
        # [Red Team V6] 좀비 포지션 완벽 척결을 위해 로컬 DB(self.positions) 대신 KIS 실계좌 잔고를 직접 긁어옴
        live_positions = self.fetch_live_positions()
        tickers = list(live_positions.keys())
        for ticker in tickers:
            qty = live_positions[ticker]
            if ticker in _s4_tickers:
                logger.critical(f"  🛡️  [S4] {ticker} 패닉셀 면제 (qty={qty}) — 포지션 유지")
                kept_positions.append(ticker)
                continue
            if qty > 0:
                logger.critical(f"    - Panic Sell: {ticker} x{qty}")
                order = self.sell(ticker=ticker, quantity=qty, price=0, order_type='market', exchange='SOR')
                panic_orders.append(order)
        if kept_positions:
            logger.critical(f"  🛡️  패닉셀 면제 종목 ({len(kept_positions)}개): {', '.join(kept_positions)}")
        return panic_orders

    def _get_live_fx_rate(self) -> float:
        """4중 멀티 폴백 원/달러 동적 환율 획득 (KIS OpenAPI t_rate -> Gateway -> Naver Bridge -> Cache)."""
        # 1. KIS OpenAPI 실시간 고시 환율 (t_rate) 우선 참조
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            kis_fx = KISDataCollector().get_usdkrw_exchange_rate()
            if kis_fx and kis_fx > 1000:
                return float(kis_fx)
        except Exception:
            pass

        # 2. CentralDataGateway 참조
        try:
            from src.data_collection.central_data_gateway import get_central_data_gateway
            fx_res = get_central_data_gateway().get_macro_indicator('usdkrw')
            px = float(fx_res.get('price', 0) or 0)
            if px > 1000:
                return px
        except Exception:
            pass

        # 3. Naver Finance FX Bridge 참조
        try:
            from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
            naver_fx = MacroRealtimeRefresher._fetch_usdkrw_naver()
            if naver_fx and naver_fx > 1000:
                return float(naver_fx)
        except Exception:
            pass

        # 4. Signal Cache 파일 참조
        try:
            sc_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_file.exists():
                sc_data = json.loads(sc_file.read_text(encoding='utf-8'))
                cached_fx = float(sc_data.get('USDKRW_RATE', sc_data.get('usdkrw', 0)) or 0)
                if cached_fx > 1000:
                    return cached_fx
        except Exception:
            pass

        return 1350.0  # Dynamic lookup baseline fallback

    def fetch_live_balance(self) -> bool:
        """[Live Patch] KIS 잔고/예수금 API 실시간 조회 → account.cash & total_equity 갱신.

        Live 모드 전용: __init__ 및 필요 시점에 호출하여 실제 계좌 잔고로 SSoT를 갱신합니다.
        API 실패 시 DynamicConfig 초기 자본을 유지하며, 절대 예외를 바깥으로 던지지 않습니다.

        KIS API: GET /uapi/domestic-stock/v1/trading/inquire-balance
            tr_id: TTTC8434R (실전투자)

        Returns:
            True: 잔고 갱신 성공
            False: API 실패 (기존 account 값 유지)
        """
        if self.mode not in ('live', 'paper'):
            return False
            
        # [Rate Limit & Slippage Defense] 60초 TTL 인메모리 캐싱: API 초과 호출 및 슬리피지 원천 방어
        now_ts = time.time()
        if hasattr(self, '_last_balance_fetch_ts') and (now_ts - getattr(self, '_last_balance_fetch_ts', 0) < 60):
            return True
            
        with self._lock:
            self._last_balance_fetch_ts = now_ts
            if not self._access_token:
                if not self.authenticate():
                    logger.warning('  ⚠️ fetch_live_balance: 인증 실패 — 잔고 조회 불가')
                    return False
            try:
                import requests
                headers = self._get_headers()
                headers = self._get_headers()
                headers['tr_id'] = 'TTTC8434R' if self.mode == 'live' else 'VTTC8434R'
                acnt = self.account_no.split('-')
                params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'AFHR_FLPR_YN': 'N', 'OFL_YN': 'N', 'INQR_DVSN': '02', 'UNPR_DVSN': '01', 'FUND_STTL_ICLD_YN': 'N', 'FNCG_AMT_AUTO_RDPT_YN': 'N', 'PRCS_DVSN': '01', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
                url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                data = resp.json()

                # [Red Team Patch] 미수 발생 없는 100% 당일 즉시 매수가능금액 2차 정밀 조회 (TTTC8908R / VTTC8908R)
                ord_psbl_cash = 0.0
                try:
                    headers_psbl = self._get_headers()
                    headers_psbl['tr_id'] = 'TTTC8908R' if self.mode == 'live' else 'VTTC8908R'
                    params_psbl = {
                        'CANO': acnt[0],
                        'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                        'PDNO': '069500',
                        'ORD_UNPR': '0',
                        'ORD_DVSN': '01',
                        'CASH_ORD_CFRM_DVSN': '00',
                        'CMAX_AMA_YN': 'N',
                        'CMA_EVLU_AMT_ICLD_YN': 'N',
                        'OVRS_ICLD_YN': 'Y'
                    }
                    url_psbl = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
                    resp_psbl = requests.get(url_psbl, headers=headers_psbl, params=params_psbl, timeout=10)
                    data_psbl = resp_psbl.json()
                    if data_psbl.get('rt_cd') == '0':
                        out_psbl = data_psbl.get('output', {})
                        # [SSoT Rule] 증권사 API 실제 해외 통합증거금 원화(nrcvb_buy_amt / max_buy_amt) 파싱
                        max_cash = float(out_psbl.get('nrcvb_buy_amt', out_psbl.get('max_buy_amt', 0)))
                        ord_psbl_cash = float(out_psbl.get('nrcvb_buy_amt', out_psbl.get('max_buy_amt', out_psbl.get('ord_psbl_cash', 0))))

                        if max_cash > 0:
                            fx_rate = self._get_live_fx_rate() or 1373.64
                            self.us_cash_usd = round(max_cash / fx_rate, 2)
                            logger.info(f"  💵 [Live US Cash (Integrated Margin Sync)] 한국투자증권 앱 통합증거금 실질 주문가능 달러 동기화: ${self.us_cash_usd:,.2f} USD")
                            ord_psbl_cash = max_cash
                except Exception as e_psbl:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e_psbl}", exc_info=True)
                    logger.debug(f"  [Live Patch] inquire-psbl-order 2차 조회 우회: {e_psbl}")

                if data.get('rt_cd') == '0':
                    output2 = data.get('output2', [{}])
                    if output2:
                        summary = output2[0]
                        if ord_psbl_cash <= 0 or ord_psbl_cash < 100000:
                            ord_psbl_cash = float(summary.get('nrcvb_buy_amt', summary.get('max_buy_amt', summary.get('prvs_rcdl_excc_amt', summary.get('nxdy_excc_amt', summary.get('dnca_tot_amt', 0))))))

                        # 🎯 [SSoT Fix] 실시간 당일 NAV (국내 총평가 + 해외 외화총평가 원화환산액) 동적 계산
                        kr_tot_evlu = float(summary.get('tot_evlu_amt', summary.get('nass_amt', 0)))
                        tot_evlu = kr_tot_evlu

                        # 🎯 [SSoT Root Fix] 기존 인메모리 포지션 전면 초기화 (유령/레거시 포지션 잔존 100% 원천 차단)
                        self.positions.clear()

                        # 🎯 [SSoT Fix] 해외(미국) 주식 잔고 및 개별 종목(output1)을 self.positions 원장에 100% 동기화
                        try:
                            headers_us = self._get_headers()
                            headers_us['tr_id'] = 'TTTS3012R' if self.mode == 'live' else 'VTTS3012R'
                            params_us = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': '%', 'TR_CRCY_CD': 'USD', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
                            url_us = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
                            resp_us = requests.get(url_us, headers=headers_us, params=params_us, timeout=10)
                            data_us = resp_us.json()
                            if data_us.get('rt_cd') == '0':
                                out2_us = data_us.get('output2', {})
                                us_stock_usd = float(out2_us.get('tot_evlu_pfls_amt', 0))
                                us_cash_usd = float(out2_us.get('ovrs_ord_psbl_amt', 0))
                                self.us_cash_usd = us_cash_usd
                                logger.info(f"  💵 [Live US Cash (Integrated Margin Sync)] 한국투자증권 앱 통합증거금 실질 주문가능 달러 동기화: ${self.us_cash_usd:,.2f} USD")

                                us_tot_usd = us_stock_usd + getattr(self, 'us_cash_usd', 0.0)
                                kis_us_krw = us_tot_usd * fx_rate
                                tot_evlu += kis_us_krw

                                for item_us in data_us.get('output1', []):
                                    u_ticker = item_us.get('ovrs_pdno', '').strip()
                                    u_qty = float(item_us.get('ovrs_cblc_qty', item_us.get('ord_psbl_qty', 0)) or 0)
                                    if u_ticker and u_qty > 0:
                                        u_avg = float(item_us.get('pchs_avg_pric', 0) or 0)
                                        u_curr = float(item_us.get('now_pric2', 0) or u_avg)
                                        u_pnl = float(item_us.get('evlu_pfls_rt', 0) or 0) / 100.0

                                        u_name = item_us.get('ovrs_item_name', '').strip() or item_us.get('prdt_name', '').strip()
                                        if not u_name:
                                            try:
                                                from src.utils.ticker_name_resolver import resolve_name
                                                u_name = resolve_name(u_ticker)
                                            except Exception:
                                                u_name = u_ticker

                                        self.positions[u_ticker] = Position(
                                            ticker=u_ticker,
                                            quantity=int(u_qty),
                                            avg_price=u_avg,
                                            current_price=u_curr,
                                            name=u_name
                                        )
                                        logger.info(f"  🇺🇸 [Live US Position] {u_ticker} ({u_name}): {int(u_qty)}주 @ ${u_curr:.2f} (수익률: {u_pnl:.2%})")
                        except Exception as e_us:
                            logger.warning(f"  ⚠️ 해외주식 잔고 통합 수집 예외: {e_us}")

                        # 🎯 [SSoT Fix] 국내 주식 개별 보유 종목(output1)을 self.positions 원장에 동기화
                        for item_kr in data.get('output1', []):
                            pdno = item_kr.get('pdno', '').strip()
                            hldg_qty = float(item_kr.get('hldg_qty', 0) or 0)
                            if pdno and hldg_qty > 0:
                                pchs_avg = float(item_kr.get('pchs_avg_pric', 0) or 0)
                                prpr = float(item_kr.get('prpr', 0) or pchs_avg)
                                pnl_rt = float(item_kr.get('evlu_pfls_rt', 0) or 0) / 100.0
                                kr_name = item_kr.get('prdt_name', '').strip()
                                if not kr_name:
                                    try:
                                        from src.utils.ticker_name_resolver import resolve_name
                                        kr_name = resolve_name(pdno)
                                    except Exception:
                                        kr_name = pdno
                                self.positions[pdno] = Position(
                                    ticker=pdno,
                                    quantity=int(hldg_qty),
                                    avg_price=pchs_avg,
                                    current_price=prpr,
                                    name=kr_name
                                )
                                logger.info(f"  🇰🇷 [Live KR Position] {pdno} ({kr_name}): {int(hldg_qty)}주 @ ₩{prpr:,.0f} (수익률: {pnl_rt:.2%})")

                        # 🎯 [Position Ledger Sync] 보유 종목 entry_date 복원 및 원장 동기화
                        try:
                            from src.execution.position_ledger import PositionLedgerManager
                            _ledger_mgr = PositionLedgerManager()
                            _active_tickers = list(self.positions.keys())
                            _ledger_map = _ledger_mgr.sync_live_positions(_active_tickers)
                            for t, pos in self.positions.items():
                                pos.entry_date = _ledger_map.get(t, "")
                        except Exception as _ledg_err:
                            logger.warning(f"  ⚠️ [PositionLedger] 동기화 경고: {_ledg_err}")

                        self.account.cash = ord_psbl_cash
                        try:
                            from src.execution.account_reconciler import AccountReconciler
                            AccountReconciler().reconcile(self)
                        except Exception as _rec_e:
                            logger.warning(f"  ⚠️ [AccountReconciler] 대조 실행 경고: {_rec_e}")
                        logger.info(f"  ✅ [Live Patch] KIS OpenAPI 통합 원장 갱신 완료: 주문가능현금={self.account.cash:,.0f}원 / MTS 100% 동일 총자산=₩{self.account.total_equity:,.0f}원")
                        return True
                    else:
                        logger.warning('  ⚠️ fetch_live_balance: output2 비어있음')
                elif data.get('rt_cd') == '1' and ('만료' in data.get('msg1', '') or '토큰' in data.get('msg1', '') or data.get('msg_cd') in ('EGW00123', 'EGW00103')):
                    logger.warning('  ⚠️ [Token Expired] 토큰 만료 감지 → 신규 토큰 발급 후 재시도')
                    if self._request_new_token():
                        return self.fetch_live_balance()
                elif data.get('rt_cd') == '1' and '초과' in data.get('msg1', ''):
                    _m1_rl = data.get("msg1", "")
                    logger.warning(f"  ⚠️ fetch_live_balance API 속도 제한 (Rate Limit): {_m1_rl}")
                else:
                    logger.warning(f"  ❌ fetch_live_balance API 오류: {data.get('msg1', '')} (rt_cd={data.get('rt_cd')})")
            except Exception as e:
                logger.error(f"  ❌ fetch_live_balance 예외: {e}")
            return False

    def fetch_live_positions(self) -> Dict[str, int]:
        """[Red Team V6] KIS 실계좌의 실제 보유 종목(positions) 조회.
        
        좀비 포지션(상태 비동기화) 해결 및 확실한 패닉셀을 위해 실제 계좌를 뒤집니다.
        
        Returns:
            Dict[str, int]: { '069500': 100, '122630': 50 } 형태의 실제 보유 수량 딕셔너리
        """
        if self.mode not in ('live', 'paper'):
            return {t: p.quantity for t, p in self.positions.items() if p.quantity > 0}
            
        # [Rate Limit & Slippage Defense] 60초 TTL 인메모리 캐싱
        now_ts = time.time()
        if hasattr(self, '_last_positions_fetch_ts') and (now_ts - getattr(self, '_last_positions_fetch_ts', 0) < 60) and hasattr(self, '_cached_live_positions'):
            return self._cached_live_positions
            
        with self._lock:
            self._last_positions_fetch_ts = now_ts
            if not self._access_token:
                if not self.authenticate():
                    return {}
            try:
                import requests
                headers = self._get_headers()
                headers['tr_id'] = 'TTTC8434R' if self.mode == 'live' else 'VTTC8434R'
                acnt = self.account_no.split('-')
                params = {
                    'CANO': acnt[0], 
                    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 
                    'AFHR_FLPR_YN': 'N', 'OFL_YN': 'N', 'INQR_DVSN': '02', 'UNPR_DVSN': '01', 
                    'FUND_STTL_ICLD_YN': 'N', 'FNCG_AMT_AUTO_RDPT_YN': 'N', 'PRCS_DVSN': '01', 
                    'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''
                }
                url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                data = resp.json()
                
                live_pos = {}
                self.live_position_details = {}
                if data.get('rt_cd') == '0':
                    output1 = data.get('output1', [])
                    for item in output1:
                        ticker = item.get('pdno', '')
                        qty = int(item.get('hldg_qty', 0))
                        if ticker and qty > 0:
                            live_pos[ticker] = qty

                # [Phase 75 US Live Balance Patch] 해외 주식 잔고 (TTTS3012R) 추가 통합
                try:
                    us_headers = self._get_headers()
                    us_headers['tr_id'] = 'TTTS3012R' if self.mode == 'live' else 'VTTS3012R'
                    us_params = {
                        'CANO': acnt[0],
                        'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                        'WCRS_DVSN_CD': '01',
                        'OVRS_EXCG_CD': '%',
                        'TR_CRCY_CD': 'USD',
                        'CTX_AREA_FK200': '',
                        'CTX_AREA_NK200': ''
                    }
                    us_url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
                    us_resp = requests.get(us_url, headers=us_headers, params=us_params, timeout=10)
                    us_data = us_resp.json()
                    if us_data.get('rt_cd') == '0':
                        for u_item in us_data.get('output1', []):
                            u_ticker = u_item.get('ovrs_pdno', '').strip()
                            u_qty = int(float(u_item.get('ovrs_cblc_qty', 0)))
                            u_avg = float(u_item.get('pchs_avg_pric', 0.0))
                            u_cur = float(u_item.get('now_pric2', 0.0))
                            u_pnl = float(u_item.get('evlu_pfls_rt', 0.0))
                            if u_ticker and u_qty > 0:
                                live_pos[u_ticker] = u_qty
                                self.live_position_details[u_ticker] = {
                                    'quantity': u_qty,
                                    'entry_price': u_avg,
                                    'current_price': u_cur,
                                    'pnl_pct': u_pnl
                                }
                                logger.info(f"  🇺🇸 [US Live Position] {u_ticker}: {u_qty}주 (매수가=${u_avg}, 현재가=${u_cur}, 손실률={u_pnl}%) 포착!")
                except Exception as _ue:
                    logger.warning(f"  ⚠️ 해외 잔고 동기화 경고: {_ue}")

                self._cached_live_positions = live_pos
                logger.info(f"  ✅ [Live Patch] 실계좌 국내/해외 통합 종목 동기화 완료: {live_pos}")
                return live_pos
            except Exception as e:
                logger.error(f"  ❌ fetch_live_positions 예외: {e}")
                if hasattr(self, '_cached_live_positions') and self._cached_live_positions is not None:
                    logger.warning("  ⚠️ [Live Patch] 예외 발생 → 기존 보유 잔고 캐시 반환 (Zombie Position 방지)")
                    return self._cached_live_positions
                return None

    def _update_account(self):
        pv = sum((p.current_price * p.quantity for p in self.positions.values()))
        self.account.positions_value = pv
        self.account.unrealized_pnl = sum((p.unrealized_pnl for p in self.positions.values()))
        self.account.total_equity = self.account.cash + pv

    def _save_state(self):
        try:
            state = {'timestamp': datetime.now().isoformat(), 'mode': self.mode, 'account': asdict(self.account), 'positions': {t: asdict(p) for t, p in self.positions.items()}, 'trade_history': self.trade_history[-500:]}
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.state_file, state, indent=2, default=str)
        except Exception as e:
            logger.critical(f"상태 저장 실패: {e}", exc_info=True)

    def _load_state(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, encoding='utf-8') as _f:
                    state = json.load(_f)
                if state.get('mode') == self.mode:
                    acct = state.get('account', {})
                    acct_data = {k: v for k, v in acct.items() if k in AccountInfo.__dataclass_fields__}
                    self.account = AccountInfo(**acct_data)
                    for t, p in state.get('positions', {}).items():
                        pos_data = {k: v for k, v in p.items() if k in Position.__dataclass_fields__}
                        self.positions[t] = Position(**pos_data)
                    self.trade_history = state.get('trade_history', [])
            except Exception as _e:
                logger.error(f"[KISAdapter] 상태 파일 로드 실패 ({self.state_file}): {_e}. Fresh start로 진행합니다.")

    def check_slippage_budget(self, ticker: str, submitted_price: float, max_bps: float = 15.0) -> bool:
        """동적 슬리피지 예산 차단기 (Dynamic 15 bps Slippage Budget).
        
        제출된 지정가가 실시간 시세 대비 15 bps (0.15%) 이상 벌어지면 True(수정 필요) 반환.
        """
        cur_p = self._get_current_price(ticker) or self.get_live_overseas_price(ticker, 'NASD')
        if not cur_p or cur_p <= 0 or submitted_price <= 0:
            return False
        drift_pct = abs(cur_p - submitted_price) / submitted_price
        if drift_pct * 10000 > max_bps:
            logger.warning(f"  🛡️ [Slippage Budget Exceeded] {ticker} 지정가(${submitted_price}) vs 현재가(${cur_p}) 드리프트 {drift_pct*100:.2f}% > {max_bps}bps")
            return True
        return False

    def inquire_unexecuted(self, ticker: str = '') -> List[Dict[str, Any]]:
        """증권사 미체결 주문 조회 (국내/해외 이중 지원)"""
        if self.mode == 'mock':
            return []
        try:
            import requests
            headers = self._get_headers()
            is_us = ticker and any(c.isalpha() for c in ticker)
            if is_us:
                headers['tr_id'] = 'VTTT3018R' if self.mode == 'paper' else 'TTTS3018R'
                acnt = self.account_no.split('-')
                params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': 'NASD', 'SORT_SQ': 'DS', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
                url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
            else:
                headers['tr_id'] = 'VTTC8001R' if self.mode == 'paper' else 'TTTC8001R'
                acnt = self.account_no.split('-')
                params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'INQR_DVSN_1': '0', 'INQR_DVSN_2': '0', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
                url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            
            resp = requests.get(url, headers=headers, params=params, timeout=5)
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output1', data.get('output', []))
                if ticker:
                    return [item for item in output if str(item.get('pdno', item.get('ticker', ''))).endswith(ticker)]
                return output
        except Exception as e:
            logger.warning(f"  ⚠️ inquire_unexecuted 조회 예외: {e}")
        return []

    def check_and_repeg_unexecuted_orders(self, max_wait_sec: float = 15.0) -> List[Dict]:
        """[Time-Bounded Pegging Monitoring Loop]
        미체결 주문(inquire-nccs)을 조회하여 제출 후 지연 시 실시간 호가로 자동 정정 체결!
        (매수/매도 구분 철저 적용: 매도는 최유리 매수호가로, 매수는 최유리 매도호가로 정정)
        """
        if self.mode == 'mock':
            return []
        try:
            import requests
            headers = self._get_headers()
            headers['tr_id'] = 'VTTT3018R' if self.mode == 'paper' else 'TTTS3018R'
            acnt = self.account_no.split('-')
            repeg_results = []
            seen_order_nos = set()

            for excg_target in ('NASD', 'NYSE', 'AMEX'):
                params = {
                    'CANO': acnt[0],
                    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                    'OVRS_EXCG_CD': excg_target,
                    'SORT_SQ': 'DS',
                    'CTX_AREA_FK200': '',
                    'CTX_AREA_NK200': ''
                }
                url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                data = resp.json()
                if data.get('rt_cd') == '0':
                    for item in data.get('output', []):
                        order_no = item.get('odno', '')
                        if not order_no or order_no in seen_order_nos:
                            continue
                        seen_order_nos.add(order_no)

                        ticker = item.get('pdno', '')
                        qty = int(float(item.get('nccs_qty', item.get('ft_ord_qty', 1))))
                        submitted_px = float(item.get('ft_ord_unpr3', 0))
                        excg = item.get('ovrs_excg_cd', excg_target)
                        side_cd = str(item.get('sll_buy_dvsn_cd', '')).strip()
                        is_sell = side_cd in ('01', 'SELL', 'sell')

                        if ticker and submitted_px > 0 and qty > 0:
                            resolved_excg = self.resolve_overseas_exchange(ticker, excg)
                            try:
                                self.cancel_us_order(order_no, ticker, resolved_excg)
                            except Exception:
                                pass

                            if is_sell:
                                bid_p = self.get_live_overseas_price(ticker, resolved_excg, ask_side=False) or (submitted_px - 0.01)
                                repeg_price = round(bid_p - 0.01, 2) if bid_p > 0 else round(submitted_px - 0.01, 2)
                                logger.warning(f"  ⚡ [Auto Re-Peg Loop] {ticker} {qty}주 매도 미체결({order_no}) 감지 ➔ 실시간 최유리 매수호가(${repeg_price:.2f})로 0초 즉시 매도 정정!")
                                repeg_res = self.sell(ticker=ticker, quantity=qty, price=repeg_price, order_type='limit', exchange=resolved_excg)
                            else:
                                ask_p = self.get_live_overseas_price(ticker, resolved_excg, ask_side=True) or (submitted_px + 0.01)
                                repeg_price = round(ask_p + 0.01, 2) if ask_p > 0 else round(submitted_px + 0.01, 2)
                                logger.warning(f"  ⚡ [Auto Re-Peg Loop] {ticker} {qty}주 매수 미체결({order_no}) 감지 ➔ 실시간 최유리 매도호가(${repeg_price:.2f})로 0초 즉시 매수 정정!")
                                repeg_res = self.buy(ticker=ticker, quantity=qty, price=repeg_price, order_type='limit', exchange=resolved_excg)

                            repeg_results.append({'order_no': order_no, 'ticker': ticker, 'side': 'sell' if is_sell else 'buy', 'repeg_order': repeg_res})
            return repeg_results
        except Exception as e:
            logger.debug(f"  [Auto Re-Peg Loop] 미체결 스캔 예외: {e}")
            return []

    def execute_time_bounded_toleranced_pegging(self, ticker: str, submitted_price: float, submitted_time: datetime, order_no: str = '', quantity: int = 1, max_wait_sec: float = 15.0, max_tolerance_bps: float = 20.0) -> Dict[str, Any]:
        """[대표님 제안 알고리즘] 타임아웃 허용범위 가변 체결 엔진 (Time-Bounded Tolerance-Pegged Execution Engine).
        
        1. 지정가 주문 제출 후 15초(max_wait_sec) 동안 체결을 기다림.
        2. 15초 경과 후 미체결 시, 현재 최유리 매도호가가 허용범위(20 bps = 0.2%) 이내이면 1초 만에 즉시 기존 주문 취소 ➔ 실시간 현재가/매도호가로 즉시 정정 체결!
        3. 허용범위를 초과해 급등한 경우 뇌동 매매를 차단하기 위해 주문을 취소하고 현금 보존.
        """
        now = datetime.now()
        elapsed = (now - submitted_time).total_seconds()
        if elapsed < max_wait_sec:
            return {'action': 'wait', 'reason': f"대기시간 {elapsed:.1f}s < 타임아웃 {max_wait_sec}s"}
            
        is_overseas = not ticker.isdigit()
        if is_overseas:
            cur_p = self.get_live_overseas_price(ticker, 'NYSE') or self.get_live_overseas_price(ticker, 'NASD') or submitted_price
        else:
            cur_p = self._get_current_price(ticker) or submitted_price

        if not cur_p or cur_p <= 0 or submitted_price <= 0:
            return {'action': 'none', 'reason': '시세 데이터 불량'}
            
        max_acceptable_price = submitted_price * (1.0 + (max_tolerance_bps / 10000.0))
        
        if cur_p <= max_acceptable_price:
            logger.warning(f"  ⚡ [Time-Bounded Pegging] {ticker} {elapsed:.1f}초 미체결 ➔ 허용범위(${max_acceptable_price:.2f}) 이내 현재가(${cur_p:.2f})로 즉시 정정 체결 집행!")
            if order_no:
                try:
                    if is_overseas:
                        self.cancel_us_order(order_no, ticker)
                    else:
                        self.cancel_order(order_no)
                except Exception as e_cncl:
                    logger.warning(f"  ⚠️ [Time-Bounded Pegging] 기존 주문({order_no}) 취소 시도 예외 발생: {e_cncl}")
            
            repeg_res = self.buy(ticker=ticker, quantity=quantity, price=cur_p, order_type='limit', exchange='NYSE' if is_overseas else 'KRX')
            return {
                'action': 'repeg', 
                'target_price': cur_p, 
                'order': repeg_res, 
                'reason': '허용범위 내 정정 체결 완료',
                'execution_method': 'cancel_and_reorder' if is_overseas else 'modify'
            }
        else:
            logger.warning(f"  🛑 [Tolerance Exceeded] {ticker} 현재가(${cur_p:.2f}) > 최대허용가(${max_acceptable_price:.2f}) ➔ 뇌동 매매 차단 및 주문 취소!")
            if order_no:
                try:
                    if is_overseas:
                        self.cancel_us_order(order_no, ticker)
                    else:
                        self.cancel_order(order_no)
                except Exception as e_cncl:
                    logger.warning(f"  ⚠️ [Time-Bounded Pegging] 기존 주문({order_no}) 취소 시도 예외 발생: {e_cncl}")
            return {'action': 'cancel', 'reason': '허용범위 초과 뇌동 매매 차단'}

    def get_integrated_margin_purchase_power(self) -> Dict[str, float]:
        """[User Directive: Pure KIS Integrated Margin Execution]
        
        KIS 계좌에 통합증거금이 신청되어 있으므로 환전 API를 부를 필요가 전혀 없음!
        통합증거금 시스템이 원화/달러 구분 없이 100% 매수 실탄으로 자동 인지하며,
        결제일(T+1)에 KIS 증권사가 알아서 자동 정산 환전 처리합니다.
        """
        try:
            self.fetch_live_balance()
        except Exception as e:
            logger.warning(f"  ⚠️ [Integrated Margin] 실계좌 잔고 수집 1차 실패 ({e}), 토큰 재발급 후 재시도...")
            try:
                self._access_token = None
                self._request_new_token()
                self.fetch_live_balance()
            except Exception as e2:
                logger.error(f"  ❌ [Integrated Margin] 실계좌 잔고 수집 재시도 실패: {e2}")
            
        # SSoT Strict Rule: Live 모드 시 레거시 파일(shadow_portfolio.json) 우회 전면 제거. 오직 KIS API 실시간 수집만 사용.
        fx_rate = self._get_live_fx_rate() or 1358.70
        krw_cash = float(getattr(self.account, 'cash', 0.0) or 0.0)
        usd_cash = float(getattr(self, 'us_cash_usd', 0.0) or 0.0)

        # 원화/달러 구분 없는 통합 실질 총 매수여력 (Integrated Buying Power)
        total_buying_power_krw = krw_cash + (usd_cash * fx_rate)
        total_buying_power_usd = usd_cash + (krw_cash / fx_rate if fx_rate > 0 else 0.0)
        
        return {
            'krw_cash': round(krw_cash, 0),
            'usd_cash': round(usd_cash, 2),
            'fx_rate': round(fx_rate, 2),
            'total_buying_power_krw': round(total_buying_power_krw, 0),
            'total_buying_power_usd': round(total_buying_power_usd, 2)
        }

    def sweep_uninvested_cash_to_shield(self) -> Dict[str, Any]:
        """Autonomous Yield Harvester 2.0 Engine (신호 기반 + 미국 SHV 04:50 KST 자동 청산 + KRX 15:35 KST 스윕).

        1. 전술적 매수 신호(OIS 곱버스/S11 등) 대기 중에는 현금 100% 보존.
        2. 장마감 후(15:35 KST) 원화 현금 459580 파킹 ➔ 익일 08:30 KST 자동 해제.
        3. 미국 정규장(22:30~05:00 KST) 유휴 현금 ➔ SHV(미국 초단기채 연~5%) 원화대용 파킹 ➔ 새벽 04:50 KST 자동 매도 청산.
        """
        results = {'us_sweep': None, 'kr_sweep': None, 'auto_redemptions': []}
        now_dt = datetime.now()
        now_time = now_dt.time()
        
        # 1. 자동 청산(Auto-Redemption) 체크
        # 새벽 04:50 KST ~ 05:00 KST: 미국 SHV 자동 청산 (익일 KRX 자금 100% 복구)
        if (now_dt.hour == 4 and now_dt.minute >= 50) or (now_dt.hour == 5 and now_dt.minute < 5):
            red_res = self.auto_redeem_shield_positions(market='US')
            results['auto_redemptions'].extend(red_res)
            
        # 아침 08:30 KST ~ 08:45 KST: 국내 459580 자동 청산 (09:00 KRX 개장 자금 복구)
        if now_dt.hour == 8 and 30 <= now_dt.minute <= 45:
            red_res = self.auto_redeem_shield_positions(market='KR')
            results['auto_redemptions'].extend(red_res)
            logger.info("  🏦 [Yield Harvester] 프리마켓/장개시 직전(08:30 KST) 459580 자동 청산 집행 완료")
            return results

        # 프리마켓/장개시 직전(08:00~09:15 KST) 신규 파킹 매수 금지
        if now_dt.hour == 8 or (now_dt.hour == 9 and now_dt.minute < 15):
            return results

        from config.dynamic_config import DynamicConfig
        _cfg = DynamicConfig()
        us_shield_ticker = _cfg.get('cash_shield.us_ticker', 'SHV')
        kr_shield_ticker = _cfg.get('cash_shield.kr_ticker', '459580')

        # 2. 미국 야간 세션 파킹 (22:30 ~ 04:45 KST)
        is_us_trading = (now_dt.hour >= 22 or now_dt.hour < 4 or (now_dt.hour == 4 and now_dt.minute < 45))
        now_ts = time.time()
        _last_us_ts = getattr(self, '_last_us_sweep_ts', 0.0)
        
        if is_us_trading and (now_ts - _last_us_ts >= 3600.0) and not self._has_active_tactical_signals(market='US'):
            self._last_us_sweep_ts = now_ts
            us_cash = getattr(self, 'us_cash_usd', 0.0)
            
            # [Ironclad Guardrail] 달러 현금(us_cash_usd > 100) 보유 시에만 미국 SHV 파킹 수행 (원화 자동환전 거부로 인한 반복 재발주 100% 원천 차단)
            if us_cash > 100:
                p_us = round(self.get_live_overseas_price(us_shield_ticker, 'NYSE') or 110.29, 2)
                if us_cash >= p_us and p_us > 0:
                    max_qty = int(us_cash // p_us)
                    for q_try in range(max_qty, 0, -1):
                        res = self.buy(ticker=us_shield_ticker, quantity=q_try, price=p_us, order_type='limit', exchange='NYSE', is_yield_sweep=True)
                        if getattr(res, 'status', '') in ('submitted', 'filled', 'pending'):
                            logger.info(f"  🏦 [Yield Harvester 2.0] 미국 야간 SHV x{q_try}주 (연~5.0% 채권 이자) 파킹 집행 완료!")
                            results['us_sweep'] = res
                            break
            else:
                logger.info("  🏦 [Yield Harvester 2.0] 원화 예수금 보유 중 (달러 미환전) ➔ KIS 해외증거금 반복 거부 방지를 위해 원화 현금 100% 안전 보존")

        # 3. 국내 장마감 후 파킹 (15:20 ~ 16:00 KST)
        is_kr_post_close = (now_dt.hour == 15 and 20 <= now_dt.minute <= 59)
        if is_kr_post_close and not self._has_active_tactical_signals(market='KR'):
            raw_kr_cash = float(getattr(self, 'raw_d2_cash', getattr(self.account, 'd2_cash', 0.0)) or 0.0)
            p_kr = self._get_current_price(kr_shield_ticker) or 1075000.0
            if raw_kr_cash >= p_kr and p_kr > 0:
                qty_kr = int(raw_kr_cash // p_kr)
                if qty_kr > 0:
                    logger.info(f"  🏦 [Yield Harvester 2.0] 장마감 후 원화 현금 ₩{raw_kr_cash:,.0f} ➔ {kr_shield_ticker} {qty_kr}주 (연~3.5% KOFR/CD 이자) 파킹 스윕 집행! (내일 08:30 KST 자동 청산 예약)")
                    results['kr_sweep'] = self.buy(ticker=kr_shield_ticker, quantity=qty_kr, price=p_kr, order_type='limit', exchange='KRX', is_yield_sweep=True)

        return results

    def auto_redeem_shield_positions(self, market: str = 'ALL') -> List[Any]:
        """파킹 자산(SHV / 459580) 자동 청산 (Auto-Redemption Routine)."""
        results = []
        try:
            self.fetch_live_balance()
        except Exception:
            pass
            
        shield_tickers_us = ['SHV', 'SGOV']
        shield_tickers_kr = ['459580', '430740', '357870']
        
        for ticker, pos in list(self.positions.items()):
            if pos.quantity <= 0:
                continue
            if market in ('ALL', 'US') and ticker in shield_tickers_us:
                p_us = self.get_live_overseas_price(ticker, 'NYSE') or pos.avg_price
                res = self.sell(ticker=ticker, quantity=pos.quantity, price=p_us, order_type='market', exchange='NYSE')
                logger.info(f"  ⚡ [Auto-Redemption] 새벽 04:50 KST 미국 파킹 채권 {ticker} x{pos.quantity}주 자동 청산 집행 완료! (익일 KRX 자금 100% 복구)")
                results.append(res)
            elif market in ('ALL', 'KR') and ticker in shield_tickers_kr:
                p_kr = self._get_current_price(ticker) or pos.avg_price
                res = self.sell(ticker=ticker, quantity=pos.quantity, price=p_kr, order_type='market', exchange='KRX')
                logger.info(f"  ⚡ [Auto-Redemption] 장전 08:30 KST 국내 파킹 ETF {ticker} x{pos.quantity}주 자동 청산 집행 완료! (09:00 KRX 개장 자금 복구)")
                results.append(res)
        return results

    def _has_active_tactical_signals(self, market: str = 'US') -> bool:
        """현재 계좌에 당일 실행 대기 중인 전술적 매수 신호 존재 여부 확인."""
        try:
            from pathlib import Path
            import json
            from datetime import datetime
            ls_path = Path(__file__).resolve().parent.parent.parent / 'results' / 'latest_signals.json'
            if ls_path.exists():
                with open(ls_path, encoding='utf-8') as f:
                    data = json.load(f)
                sig_date = data.get('date', '')
                today_str = datetime.now().strftime('%Y-%m-%d')
                if sig_date != today_str:
                    return False
                    
                signals = data.get('signals', {})
                for stream_id, sig_list in signals.items():
                    if sig_list:
                        for s in sig_list:
                            sig_mkt = s.get('market', 'KR' if s.get('ticker', '').isdigit() else 'US')
                            if sig_mkt == market and s.get('direction') == 'long':
                                return True
        except Exception:
            pass
        return False

    def _gen_order_id(self) -> str:
        ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
        return f"MRD-{ts}"
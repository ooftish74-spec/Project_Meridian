"""
Project Meridian — KIS Trader Adapter
======================================
Project-A의 KISTrader 핵심 로직을 Meridian용으로 경량화한 어댑터.
Meridian 전용 상태 파일 + 토큰 캐시를 사용합니다.

이 모듈은 mock/paper/live 모드에서 KIS OpenAPI와 실제 통신합니다.
Shadow 모드는 ExecutionEngine이 직접 처리합니다.
"""
import pandas as pd
import json
import logging
import time
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
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

    def update_price(self, price: float):
        self.current_price = price
        self.unrealized_pnl = (price - self.avg_price) * self.quantity
        self.unrealized_pnl_pct = (price / self.avg_price - 1) * 100 if self.avg_price > 0 else 0

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
                logger.error(f'  ❌ AccountInfo 초기화 오류: {e}')

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
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                initial_capital = 0
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = account_no
        self.base_url = self.BASE_URL.get(mode)
        self._comm = self.COMMISSION.get(mode, self.COMMISSION['mock'])
        self._slip = self.SLIPPAGE.get(mode, self.SLIPPAGE['mock'])
        self._access_token = None
        self._token_expires = None
        self.account = AccountInfo(total_equity=initial_capital, cash=initial_capital)
        self.positions: Dict[str, Position] = {}
        self.orders: List[Order] = []
        self.trade_history: List[Dict] = []
        self.state_file = _PROJECT_ROOT / 'results' / 'meridian_trading_state.json'
        self._token_cache = _PROJECT_ROOT / 'config' / f'.kis_token_meridian_{mode}.json'
        self._cb = APICircuitBreaker() if APICircuitBreaker else None
        self._dlq = OrderDLQ() if OrderDLQ else None
        self._load_state()
        if self.mode == 'live' and fetch_balance_on_init:
            self.fetch_live_balance()
        mode_label = {'mock': '🔵 Mock', 'paper': '🟡 Paper', 'live': '🔴 Live'}
        logger.info(f'  KISTraderAdapter: {mode_label.get(mode, mode)}')
        logger.info(f"    계좌: {account_no or 'N/A'}")
        logger.info(f'    자본: {self.account.cash:,.0f}원')

    def authenticate(self) -> bool:
        """API 인증."""
        with self._lock:
            if self.mode == 'mock':
                return True
            if not self.app_key or not self.app_secret:
                logger.error('  ❌ APP_KEY/APP_SECRET 미설정')
                return False
            if self._access_token and self._token_expires:
                if datetime.now() < self._token_expires - timedelta(hours=2):
                    return True
            if self._load_cached_token():
                return True
            return self._request_new_token()

    def _load_cached_token(self) -> bool:
        if not self._token_cache.exists():
            return False
        try:
            with open(self._token_cache, encoding='utf-8') as _f:
                data = json.load(_f)
            expires = datetime.fromisoformat(data['expires'])
            if datetime.now() < expires - timedelta(hours=2):
                self._access_token = data['access_token']
                self._token_expires = expires
                logger.info(f"  🔄 캐시 토큰 로드 (만료: {expires.strftime('%H:%M')})")
                return True
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return False

    def _save_token_cache(self):
        try:
            self._token_cache.parent.mkdir(parents=True, exist_ok=True)
            data = {'access_token': self._access_token, 'expires': self._token_expires.isoformat()}
            atomic_write_json(self._token_cache, data)
        except Exception as e:
            logger.critical(f'  토큰 캐시 저장 실패: {e}', exc_info=True)

    def _request_new_token(self) -> bool:
        import requests
        url = f'{self.base_url}/oauth2/tokenP'
        body = {'grant_type': 'client_credentials', 'appkey': self.app_key, 'appsecret': self.app_secret}
        for attempt in range(3):
            delay = [0, 65, 130][attempt]
            if delay > 0:
                logger.warning(f'  ⏳ 토큰 재시도 {attempt + 1}/3: {delay}초 대기...')
                time.sleep(delay)
            try:
                resp = requests.post(url, json=body, timeout=15)
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
                    continue
                else:
                    logger.error(f'  ❌ 인증 실패: {data}')
            except Exception as e:
                logger.error(f'  ❌ 인증 오류: {e}')
        logger.error('  🚨 KIS 토큰 갱신 최종 실패')
        return False

    def _get_headers(self) -> Dict:
        return {'Content-Type': 'application/json; charset=utf-8', 'authorization': f'Bearer {self._access_token}', 'appkey': self.app_key, 'appsecret': self.app_secret}

    def buy(self, ticker: str, quantity: int, price: float=0, order_type: str='market', exchange: str='SOR', stream: str='', urgency: str='normal', time_in_force: str='DAY') -> Order:
        """매수 주문.

        [Live Patch] Phase 2 Execution/Risk 업데이트:
        주문 금액(price × quantity)이 TWAP 임계 이상 시 TWAPDispatcher를 통해
        자동으로 5~10분 분할 지정가 스케줄을 반환합니다.
        (슬리피지 폭탄 방어)

        [Live Transition Task 1] time_in_force:
          - 'DAY': 당일 지정가 (기본)
          - 'IOC': 즉시 미체결 취소 (Immediate Or Cancel)
          - 'FOK': 전량 미체결 시 전부 취소 (Fill Or Kill)

        Returns:
            Order (IMMEDIATE) 또는 Order (twap_schedule 첨부, status='twap_scheduled')
        """
        with self._lock:
            order = Order(order_id=self._gen_order_id(), ticker=ticker, side='buy', quantity=quantity, price=price, order_type=order_type, exchange=exchange)
            order.notes = f'tif={time_in_force}'
            order_amount = (price or 0) * (quantity or 0)
            if time_in_force == 'DAY':
                try:
                    from src.execution.smart_router import TWAPDispatcher
                    _dispatcher = TWAPDispatcher()
                    if order_amount >= _dispatcher.threshold and quantity > 0:
                        _dispatch = _dispatcher.dispatch({'ticker': ticker, 'action': 'buy', 'quantity': quantity, 'price': price, 'stream': stream, 'urgency': urgency})
                        if _dispatch.get('twap_triggered'):
                            slices = _dispatch.get('slices', [])
                            logger.info(f'  🔀 [TWAP Routing] BUY {ticker} ₩{order_amount:,.0f} → {len(slices)}분할 @ {_dispatch.get("duration_min")}분')
                            order.status = 'twap_scheduled'
                            order.notes = _dispatch.get('reason', '')
                            order.twap_slices = [s.to_dict() for s in slices]
                            return order
                except Exception as _twap_err:
                    logger.critical(f'  TWAP 라우팅 불가 (즉시주문 폴백): {_twap_err}', exc_info=True)
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
            order.notes = f'tif={time_in_force}'
            order_amount = (price or 0) * (quantity or 0)
            if time_in_force == 'DAY':
                try:
                    from src.execution.smart_router import TWAPDispatcher
                    _dispatcher = TWAPDispatcher()
                    if order_amount >= _dispatcher.threshold and quantity > 0:
                        _dispatch = _dispatcher.dispatch({'ticker': ticker, 'action': 'sell', 'quantity': quantity, 'price': price, 'stream': stream, 'urgency': urgency})
                        if _dispatch.get('twap_triggered'):
                            slices = _dispatch.get('slices', [])
                            logger.info(f'  🔀 [TWAP Routing] SELL {ticker} ₩{order_amount:,.0f} → {len(slices)}분할 @ {_dispatch.get("duration_min")}분')
                            order.status = 'twap_scheduled'
                            order.notes = _dispatch.get('reason', '')
                            order.twap_slices = [s.to_dict() for s in slices]
                            return order
                except Exception as _twap_err:
                    logger.critical(f'  TWAP 라우팅 불가 (즉시주문 폴백): {_twap_err}', exc_info=True)
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
        logger.info(f'  ✅ {order.side.upper()} {order.ticker} x{order.quantity} @ {fill_price:,.0f}')
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
                
            order_val = order.quantity * order.price
            if order_val > max_amount and order.order_type != 'market':
                raise ValueError(f"Order amount {order_val} exceeds MAX_AMOUNT_KRW ({max_amount})")
                
        except Exception as _fw_err:
            logger.critical(f"  🚨 [Execution Firewall] 비정상 주문 감지 및 차단: {_fw_err}")
            order.status = 'rejected'
            order.notes = f"Firewall Blocked: {_fw_err}"
            if getattr(self, '_dlq', None):
                self._dlq.add(dataclasses.asdict(order), f"Execution Firewall Blocked: {_fw_err}")
            return order

        if self._cb and (not self._cb.can_execute()):
            logger.warning(f'  🛑 Circuit Breaker 차단: 주문 전송 보류 ({order.ticker})')
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
        tr_id = f'{prefix}TTC0802U' if order.side == 'buy' else f'{prefix}TTC0801U'
        tif = 'DAY'
        if order.notes and 'tif=' in order.notes:
            _tif_part = [p for p in order.notes.split(',') if 'tif=' in p]
            if _tif_part:
                tif = _tif_part[0].split('=', 1)[-1].strip().upper()
        _TIF_ORD_DVSN = {'DAY': '00' if order.order_type != 'market' else '01', 'MARKET': '01', 'IOC': '13', 'FOK': '14'}
        ord_dvsn = _TIF_ORD_DVSN.get(tif, '01' if order.order_type == 'market' else '00')
        if tif in ('IOC', 'FOK'):
            ord_price = '0'
        else:
            ord_price = '0' if ord_dvsn == '01' else str(int(order.price))
        acnt = self.account_no.split('-')
        headers = self._get_headers()
        headers['tr_id'] = tr_id
        body = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'PDNO': order.ticker, 'ORD_DVSN': ord_dvsn, 'ORD_QTY': str(order.quantity), 'ORD_UNPR': ord_price}
        if tif in ('IOC', 'FOK'):
            logger.info(f'  ⚡ [{tif}] 주문 전송: {order.side.upper()} {order.ticker} x{order.quantity} (ORD_DVSN={ord_dvsn})')
        url = f'{self.base_url}/uapi/domestic-stock/v1/trading/order-cash'
        try:
            from config.dynamic_config import DynamicConfig as _DC
            _rc = _DC()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
                    raise requests.exceptions.HTTPError(f'Server Error {resp.status_code}')
                data = resp.json()
                if data.get('rt_cd') == '0':
                    if self._cb:
                        self._cb.record_success()
                    order.status = 'submitted'
                    order.order_id = data.get('output', {}).get('ODNO', order.order_id)
                    logger.info(f"  📋 API 주문 접수: {data.get('msg1', '')} (주문번호: {order.order_id})")
                    self.orders.append(order)
                    self._save_state()
                    return order
                else:
                    msg_cd = data.get('msg_cd', '')
                    if msg_cd == 'EGW00103':
                        logger.warning('  ⚠️ 토큰 에러(EGW00103) 감지 -> Auth 재시도 필요')
                        self.authenticate()
                        headers = self._get_headers()
                        headers['tr_id'] = tr_id
                        raise requests.exceptions.RequestException('Token Expired EGW00103')
                    order.status = 'rejected'
                    logger.error(f'  ❌ API 주문 거부: {data}')
                    if self._dlq:
                        self._dlq.add(dataclasses.asdict(order), f"API 거부: {data.get('msg1')}")
                    self.orders.append(order)
                    self._save_state()
                    return order
            except requests.exceptions.RequestException as e:
                logger.warning(f'  ⚠️ API 전송 오류 ({attempt + 1}/{max_retries + 1}): {e}')
                if attempt < max_retries:
                    delay = retry_delays[attempt]
                    logger.info(f'  ⏳ {delay}초 대기 후 재전송 시도...')
                    time.sleep(delay)
                else:
                    order.status = 'rejected'
                    logger.error(f'  ❌ API 주문 최종 실패 (Max Retries 초과): {e}')
                    if self._cb:
                        self._cb.record_failure()
                        self._dlq.add(dataclasses.asdict(order), f'Max Retries 초과: {e}')
            return order

    def check_order_status(self, order_no: str) -> Dict:
        """미체결 주문 상태 조회.

        KIS API: /uapi/domestic-stock/v1/trading/inquire-nccs

        Returns:
            {
                'status': 'filled'/'partial'/'pending'/'canceled',
                'filled_qty': int,
                'remaining_qty': int,
                'order_price': float,
                'filled_price': float,
            }
        """
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
            tr_id = f'{prefix}TTC8001R'
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'INQR_STRT_DT': datetime.now().strftime('%Y%m%d'), 'INQR_END_DT': datetime.now().strftime('%Y%m%d'), 'SLL_BUY_DVSN_CD': '00', 'INQR_DVSN': '00', 'PDNO': '', 'CCLD_DVSN': '01', 'ORD_GNO_BRNO': '', 'ODNO': order_no, 'INQR_DVSN_3': '00', 'INQR_DVSN_1': '', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
            url = f'{self.base_url}/uapi/domestic-stock/v1/trading/inquire-nccs'
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
            logger.error(f'  미체결 조회 실패: {e}')
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
            tr_id = f'{prefix}TTC0803U'
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            body = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'KRX_FWDG_ORD_ORGNO': '', 'ORGN_ODNO': order_no, 'ORD_DVSN': '00', 'RVSE_CNCL_DVSN_CD': '01', 'ORD_QTY': str(new_qty) if new_qty else '0', 'ORD_UNPR': str(int(new_price)) if new_price else '0', 'QTY_ALL_ORD_YN': 'Y' if not new_qty else 'N'}
            url = f'{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl'
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                logger.info(f'  📝 주문 정정 성공: {order_no}')
                return {'success': True, 'message': data.get('msg1', '')}
            else:
                return {'success': False, 'message': data.get('msg1', '')}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
            tr_id = f'{prefix}TTC0803U'
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            body = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'KRX_FWDG_ORD_ORGNO': '', 'ORGN_ODNO': order_no, 'ORD_DVSN': '00', 'RVSE_CNCL_DVSN_CD': '02', 'ORD_QTY': '0', 'ORD_UNPR': '0', 'QTY_ALL_ORD_YN': 'Y'}
            url = f'{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl'
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                logger.info(f'  ❌ 주문 취소 성공: {order_no}')
                return {'success': True, 'message': data.get('msg1', '')}
            else:
                return {'success': False, 'message': data.get('msg1', '')}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
                logger.info(f'  ✅ 체결 완료: {order.ticker} x{order.filled_quantity}')
                return order
            elif status == 'partial':
                order.filled_quantity = result.get('filled_qty', 0)
                logger.debug(f'  ⏳ 부분 체결: {order.filled_quantity}/{order.quantity}')
            elif status in ('canceled', 'error'):
                order.status = status
                return order
            time.sleep(check_interval)
        logger.warning(f'  ⏰ 체결 타임아웃 ({timeout_sec}초): {order.ticker}')
        if fallback == 'market':
            self.cancel_order(order.order_id)
            remaining = order.quantity - order.filled_quantity
            if remaining > 0:
                market_order = Order(order_id=self._gen_order_id(), ticker=order.ticker, side=order.side, quantity=remaining, price=0, order_type='market', exchange=order.exchange)
                result = self._api_order(market_order)
                order.status = 'filled_market_fallback'
                order.filled_quantity = order.quantity
                logger.info(f'  🔄 시장가 전환: {remaining}주')
        else:
            self.cancel_order(order.order_id)
            order.status = 'canceled_timeout'
            logger.info(f'  ❌ 타임아웃 취소: {order.ticker}')
        return order

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """현재가 조회 — API → KRX CSV → parquet 순서."""
        import pandas as pd
        if self.mode != 'mock' and self._access_token:
            try:
                import requests
                headers = self._get_headers()
                headers['tr_id'] = 'FHKST01010100'
                params = {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': ticker}
                url = f'{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price'
                resp = requests.get(url, headers=headers, params=params, timeout=5)
                data = resp.json()
                if data.get('rt_cd') == '0':
                    price = float(data['output']['stck_prpr'])
                    if price > 0:
                        return price
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        for pattern in [f'kr_{ticker}.parquet', f'{ticker}.parquet']:
            pq = _PROJECT_ROOT / 'data' / 'historical_10y' / pattern
            if pq.exists():
                try:
                    df = pd.read_parquet(pq)
                    return float(df['close'].iloc[-1])
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _exempt = ['S4']
        logger.critical('  🚨 [PANIC SELL] 전 종목 시장가 긴급 매도 절차 개시!')
        if _exempt:
            logger.critical(f'  🛡️  [PANIC SELL] 패닉셀 면제 스트림: {_exempt} (장기 보유 전략 — 포지션 유지)')
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
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _s4_tickers = set()
        # [Red Team V6] 좀비 포지션 완벽 척결을 위해 로컬 DB(self.positions) 대신 KIS 실계좌 잔고를 직접 긁어옴
        live_positions = self.fetch_live_positions()
        tickers = list(live_positions.keys())
        for ticker in tickers:
            qty = live_positions[ticker]
            if ticker in _s4_tickers:
                logger.critical(f'  🛡️  [S4] {ticker} 패닉셀 면제 (qty={qty}) — 포지션 유지')
                kept_positions.append(ticker)
                continue
            if qty > 0:
                logger.critical(f'    - Panic Sell: {ticker} x{qty}')
                order = self.sell(ticker=ticker, quantity=qty, price=0, order_type='market', exchange='SOR')
                panic_orders.append(order)
        if kept_positions:
            logger.critical(f"  🛡️  패닉셀 면제 종목 ({len(kept_positions)}개): {', '.join(kept_positions)}")
        return panic_orders

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
        with self._lock:
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
                url = f'{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance'
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
                        'OVRS_ICLD_YN': 'N'
                    }
                    url_psbl = f'{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order'
                    resp_psbl = requests.get(url_psbl, headers=headers_psbl, params=params_psbl, timeout=10)
                    data_psbl = resp_psbl.json()
                    if data_psbl.get('rt_cd') == '0':
                        out_psbl = data_psbl.get('output', {})
                        nrcvb = float(out_psbl.get('nrcvb_buy_amt', out_psbl.get('max_buy_amt', 0)))
                        if nrcvb > 0:
                            ord_psbl_cash = nrcvb
                except Exception as e_psbl:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e_psbl}", exc_info=True)
                    logger.debug(f'  [Live Patch] inquire-psbl-order 2차 조회 우회: {e_psbl}')

                if data.get('rt_cd') == '0':
                    output2 = data.get('output2', [{}])
                    if output2:
                        summary = output2[0]
                        if ord_psbl_cash <= 0:
                            ord_psbl_cash = float(summary.get('prvs_rcdl_excc_amt', summary.get('prvs_rcdl_exn_amt', 0)))
                        if ord_psbl_cash <= 0:
                            ord_psbl_cash = float(summary.get('dnca_tot_amt', 0))

                        tot_evlu = float(summary.get('tot_evlu_amt', summary.get('nass_amt', 0)))

                        # [Red Team Patch] 08:00~08:50 장전 동시호가 평가액 0원 튀기(Glitch) 방어
                        if ord_psbl_cash > 0 or tot_evlu > 0:
                            self.account.cash = ord_psbl_cash
                            if tot_evlu > 0:
                                self.account.total_equity = tot_evlu
                            elif self.account.total_equity > 0:
                                logger.warning('  ⚠️ [Live Patch] 장전 평가액 0원 감지 → 기존 총자산 보존 방어')
                            else:
                                self.account.total_equity = ord_psbl_cash
                            logger.info(f'  ✅ [Live Patch] 실계좌 잔고 동적 갱신 완료: 주문가능현금={ord_psbl_cash:,.0f}원 / 총자산={self.account.total_equity:,.0f}원')
                            return True
                        else:
                            logger.warning('  ⚠️ fetch_live_balance: 잔고 데이터 0 — API 응답 확인 필요')
                    else:
                        logger.warning('  ⚠️ fetch_live_balance: output2 비어있음')
                elif data.get('rt_cd') == '1' and '초과' in data.get('msg1', ''):
                    logger.warning(f'  ⚠️ fetch_live_balance API 속도 제한 (Rate Limit): {data.get("msg1", "")}')
                else:
                    logger.error(f'  ❌ fetch_live_balance API 오류: {data.get("msg1", "")} (rt_cd={data.get("rt_cd")})')
            except Exception as e:
                logger.error(f'  ❌ fetch_live_balance 예외: {e}')
            return False

    def fetch_live_positions(self) -> Dict[str, int]:
        """[Red Team V6] KIS 실계좌의 실제 보유 종목(positions) 조회.
        
        좀비 포지션(상태 비동기화) 해결 및 확실한 패닉셀을 위해 실제 계좌를 뒤집니다.
        
        Returns:
            Dict[str, int]: { '069500': 100, '122630': 50 } 형태의 실제 보유 수량 딕셔너리
        """
        if self.mode not in ('live', 'paper'):
            return {t: p.quantity for t, p in self.positions.items() if p.quantity > 0}
            
        with self._lock:
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
                url = f'{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance'
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                data = resp.json()
                
                live_pos = {}
                if data.get('rt_cd') == '0':
                    output1 = data.get('output1', [])
                    for item in output1:
                        ticker = item.get('pdno', '')
                        qty = int(item.get('hldg_qty', 0))
                        if ticker and qty > 0:
                            live_pos[ticker] = qty
                    logger.info(f'  ✅ [Live Patch] 실계좌 종목 동기화 완료: {live_pos}')
                    return live_pos
                else:
                    logger.error(f'  ❌ fetch_live_positions API 오류: {data.get("msg1", "")}')
                    return {}
            except Exception as e:
                logger.error(f'  ❌ fetch_live_positions 예외: {e}')
                return {}

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
            logger.critical(f'상태 저장 실패: {e}', exc_info=True)

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
                        self.positions[t] = Position(**p)
                    self.trade_history = state.get('trade_history', [])
            except Exception as _e:
                logger.error(f'[KISAdapter] 상태 파일 로드 실패 ({self.state_file}): {_e}. Fresh start로 진행합니다.')

    def _gen_order_id(self) -> str:
        ts = datetime.now().strftime('%Y%m%d%H%M%S%f')
        return f'MRD-{ts}'
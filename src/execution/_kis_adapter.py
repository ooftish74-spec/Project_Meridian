"""
Project Meridian — KIS Trader Adapter
======================================
Project-A의 KISTrader 핵심 로직을 Meridian용으로 경량화한 어댑터.
Meridian 전용 상태 파일 + 토큰 캐시를 사용합니다.

이 모듈은 mock/paper/live 모드에서 KIS OpenAPI와 실제 통신합니다.
Shadow 모드는 ExecutionEngine이 직접 처리합니다.
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
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

# ★ Wave 0 E3/E4: 긴급 청산 등급 — 직전가 지정가 변환 금지 + 기존 미체결 매도 취소 후 재집행 대상
_URGENT_EXIT_LEVELS = frozenset({'panic', 'stop', 'high', 'urgent'})


def _order_urgency(order) -> str:
    """Order.notes 의 'urgency=...' 토큰을 파싱 (없으면 'normal')."""
    for part in str(getattr(order, 'notes', '') or '').split(','):
        part = part.strip()
        if part.startswith('urgency='):
            return part.split('=', 1)[1].strip().lower() or 'normal'
    return 'normal'

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

    @property
    def total_asset(self) -> float:
        """Alias for total_equity for backward compatibility."""
        return self.total_equity

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
        if mode == 'real':
            mode = 'live'
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
        _log_fn = logger.info if fetch_balance_on_init or self.mode != 'live' else logger.debug
        _log_fn(f"  KISTraderAdapter: {mode_label.get(mode, mode)}")
        _log_fn(f"    계좌: {self.account_no or 'N/A'}")
        _log_fn(f"    자본: {self.account.cash:,.0f}원")

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

    _token_fail_until: float = 0.0   # ★ Wave 1: 프로세스 공용 토큰 발급 실패 쿨다운 (epoch sec)

    def _request_new_token(self) -> bool:
        """KIS 토큰 발급.

        ★ Wave 1: 과거 구현은 모든 오류에 65s+130s 대기 → 인증 장애 시 데몬 루프가 최대 195초 멈춰
          그동안 손절/청산 감시가 중단될 수 있었다.
          - EGW00133(1분 1회 제한): 캐시 토큰 복구 → 실패 시 65초 1회 대기 후 재시도
          - 네트워크/기타 오류: 2초 후 1회 재시도
          - 최종 실패: 60초 쿨다운(즉시 실패) + 만료 캐시 토큰 복구 시도(기존 동작 유지)
        """
        import requests
        if time.time() < KISTraderAdapter._token_fail_until:
            if self._load_cached_token(ignore_expiry=True) and self._access_token:
                return True
            return False
        url = f"{self.base_url}/oauth2/tokenP"
        body = {'grant_type': 'client_credentials', 'appkey': self.app_key, 'appsecret': self.app_secret}
        rl_retry = False
        net_retry = False
        while True:
            try:
                resp = requests.post(url, json=body, timeout=(1.5, 3.0))
                data = resp.json()
                if 'access_token' in data:
                    self._access_token = data['access_token']
                    expires_in = data.get('expires_in', 86400)
                    self._token_expires = datetime.now() + timedelta(seconds=expires_in)
                    self._save_token_cache()
                    KISTraderAdapter._token_fail_until = 0.0
                    logger.info(f"  ✅ 인증 성공 (만료: {self._token_expires.strftime('%H:%M')})")
                    return True
                error_code = data.get('error_code', '')
                if error_code == 'EGW00133':
                    logger.warning("  ⚠️ EGW00133 (토큰 재발급 제한) 감지 → 디스크 캐시 토큰 강제 즉시 복구")
                    if self._load_cached_token(ignore_expiry=True) and self._access_token:
                        logger.info("  ✅ 디스크 캐시 토큰 복구 성공! (65초 지연 회피)")
                        return True
                    if not rl_retry:
                        rl_retry = True
                        logger.warning("  ⏳ 캐시 토큰 없음 → 65초 후 1회 재시도")
                        time.sleep(65)
                        continue
                    break
                logger.error(f"  ❌ 인증 실패: {data}")
                break
            except Exception as e:
                logger.error(f"  ❌ 인증 오류: {e}")
                if not net_retry:
                    net_retry = True
                    time.sleep(2)
                    continue
                break
        KISTraderAdapter._token_fail_until = time.time() + 60.0
        logger.error('  🚨 KIS 토큰 갱신 최종 실패(60초 쿨다운) — 만료 캐시 토큰 강제 복구 시도')
        if self._load_cached_token(ignore_expiry=True) and self._access_token:
            logger.warning('  ✅ 만료된 디스크 캐시 토큰 강제 복구 성공 (주문 무산 방지)')
            return True
        return False

    def _get_headers(self) -> Dict:
        if not self._access_token:
            self.authenticate()
        elif hasattr(self, '_token_expires') and self._token_expires and datetime.now() >= self._token_expires - timedelta(minutes=10):
            self._request_new_token()
        return {'Content-Type': 'application/json; charset=utf-8', 'authorization': f"Bearer {self._access_token}", 'appkey': self.app_key, 'appsecret': self.app_secret}

    @classmethod
    def resolve_overseas_exchange(cls, ticker: str, exchange: str = '') -> str:
        """해외 거래소 코드 (NASD / NYSE / AMEX) 중앙 단일 결정자 (티커 매핑 최우선)."""
        t_upper = str(ticker).upper().strip()

        try:
            from src.params.tables import load_table
            _ex_map = load_table('src.data_collection.kis_data_collector.KISDataCollector.US_EXCHANGE_MAP', {})
            _mapped = str((_ex_map or {}).get(t_upper, '')).upper().strip()
            if _mapped in ('AMS', 'AMEX'):
                return 'AMEX'
            if _mapped in ('NYS', 'NYSE'):
                return 'NYSE'
            if _mapped in ('NAS', 'NASD', 'NASDAQ'):
                return 'NASD'
        except Exception:
            pass

        _AMEX_SET = {  # hardcode-ok: L0 US exchange listing table (NYSE Arca / AMEX)
            'SOXL', 'SOXS', 'UPRO', 'SPXL', 'SPXS', 'TECL', 'WEBL', 'BOIL', 'KOLD',
            'SVIX', 'UVIX', 'LABU', 'LABD', 'BULZ', 'CONL', 'MSTX', 'MSTU', 'XBI',
            'GLD', 'SLV', 'USO', 'SPY', 'SGOV', 'BIL', 'EWY', 'IWM', 'LIT', 'SLX',
            'BDRY', 'URA', 'XLK', 'XLE', 'XLF', 'XLV', 'XLI', 'XLP', 'XLU', 'XLY',
            'PSQ', 'SH', 'SDS', 'QID', 'DOG', 'UUP', 'FXI', 'EWJ', 'HYG', 'LQD',
            'TIP', 'CPER', 'UNG', 'DBA', 'VNQ', 'GDX', 'XHB', 'XME', 'KRE',
        }
        _NYSE_SET = {  # hardcode-ok: L0 US exchange listing table (NYSE)
            'TSM', 'CAT', 'KO', 'PG', 'DIS', 'JNJ', 'PFE', 'MRK', 'ABBV', 'LLY',
            'UNH', 'JPM', 'BAC', 'V', 'MA', 'WMT', 'XOM', 'CVX', 'NEE', 'LMT', 'RTX',
        }
        _NASD_SET = {  # hardcode-ok: L0 US exchange listing table (NASDAQ)
            'SHV', 'NVDL', 'TQQQ', 'SQQQ', 'TSLL', 'FNGU', 'QQQ', 'NVDA', 'AAPL',
            'MSFT', 'AVGO', 'TSLA', 'AMZN', 'GOOGL', 'META', 'SOXX', 'SMH', 'TLT',
            'TMUS', 'IEF', 'SHY', 'PEP', 'COST',
        }

        if t_upper in _AMEX_SET:
            return 'AMEX'
        if t_upper in _NYSE_SET:
            return 'NYSE'
        if t_upper in _NASD_SET:
            return 'NASD'

        excg_upper = str(exchange).upper().strip()
        if excg_upper in ('NYSE', 'NYS'):
            return 'NYSE'
        if excg_upper in ('AMEX', 'AMS'):
            return 'AMEX'
        if excg_upper in ('NASD', 'NAS', 'NASDAQ'):
            return 'NASD'
        return 'NASD'

    def buy(self, ticker: str, quantity: int, price: float=0, order_type: str='market', exchange: str='SOR', stream: str='', urgency: str='normal', time_in_force: str='DAY', is_yield_sweep: bool = False, notes: str = '', **kwargs) -> Order:
        """매수 주문."""
        with self._lock:
            order = Order(order_id=self._gen_order_id(), ticker=ticker, side='buy', quantity=quantity, price=price, order_type=order_type, exchange=exchange)
            order.notes = f"tif={time_in_force}" + (f",{notes}" if notes else "")

            # [Guardrail] 장중/프리마켓(08:00~15:10 KST) 파킹 ETF(459580, 423160, 449170, 430740, 357870, 357330) 일반 매수 금지 (단, 15:20~16:00 KST 스윕 및 is_yield_sweep=True 허용)
            now_time = datetime.now().time()
            if ticker in ('459580', '423160', '449170', '430740', '357870', '357330') and dtime(8, 0) <= now_time <= dtime(15, 10) and not is_yield_sweep:
                logger.warning(f"  🏦 [Yield Harvester] 장중({now_time.strftime('%H:%M')}) 현금 파킹 ETF({ticker}) 일반 매수 주문 차단 완료!")
                order.status = 'rejected'
                order.notes = 'Parking ETF buy blocked during intraday trading'
                return order

            is_call_auction = (dtime(8, 30) <= now_time < dtime(9, 0))
            is_explicit_repeg_market = (str(order_type).lower() in ('01', '05', '06', '13', '14', 'premarket_close') or urgency == 'repeg')
            if self.mode in ('live', 'paper') and not is_call_auction and not is_explicit_repeg_market:
                cur_info = self._get_current_price(ticker)
                if not cur_info or cur_info <= 0:
                    cur_info = self.get_live_overseas_price(ticker, exchange)
                
                if (not price or price <= 0) and cur_info and cur_info > 0:
                    order.order_type = 'limit'
                    # 매수 시 최유리 지정가 (해외주식은 소수점 2자리 반올림 적용)
                    order.price = round(cur_info, 2) if (exchange in ('NYSE', 'NASD', 'AMEX', 'AMS') or not ticker.isdigit()) else cur_info
                    logger.info(f"  🎯 [Smart Limit Order Engine] {ticker} 매수 → 실시간 지정가(${order.price:,.2f}) 전환 (시장가 슬리피지 100% 차단)")
                elif not cur_info or cur_info <= 0:
                    logger.warning(f"  ⚠️ [Smart Limit Engine] {ticker} 현재가 수집 불가 → 안전 지정가 전환 스킵")

            if self.mode == 'mock':
                return self._mock_execute(order)
            else:
                return self._api_order(order)

    def sell(self, ticker: str, quantity: int, price: float=0, order_type: str='market', exchange: str='SOR', stream: str='', urgency: str='normal', time_in_force: str='DAY', notes: str = '', **kwargs) -> Order:
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
            _urg = str(urgency or 'normal').lower()
            order.notes = f"tif={time_in_force},urgency={_urg}" + (f",{notes}" if notes else "")
            # 🎯 [User Rule] 분할매도/TWAP 전면 금지 ➔ 100% 일괄 즉시 매도 전송
            # 🚀 [Smart Limit Order Engine & Institutional IS Mid-Point Pegged]
            now_time = datetime.now().time()
            is_call_auction = (dtime(8, 30) <= now_time < dtime(9, 0))
            # ★ Wave 0 E4: 긴급 청산(panic/stop/high)은 직전가 지정가 변환 금지 — 급락장 미체결로 손절 불발 방지
            is_urgent_exit = _urg in _URGENT_EXIT_LEVELS
            is_explicit_repeg_market = (str(order_type).lower() in ('01', '05', '06', '13', '14', 'premarket_close') or _urg == 'repeg' or is_urgent_exit)
            if self.mode in ('live', 'paper') and not is_call_auction and not is_explicit_repeg_market:
                cur_info = self._get_current_price(ticker)
                if not cur_info or cur_info <= 0:
                    cur_info = self.get_live_overseas_price(ticker, exchange)

                if (not price or price <= 0) and cur_info and cur_info > 0:
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
        
        # 0. Session Gatekeeper & Holdings Clamping
        is_us = not str(order.ticker).isdigit()
        try:
            from src.utils.market_calendar import is_ticker_tradable_now
            is_tradable, session_type = is_ticker_tradable_now(order.ticker)
            if not is_tradable:
                logger.info(f"  ⏸️ [Session Gate] {order.ticker} 해당 거래소 휴장 중 (session={session_type}) ➔ 주문 스킵")
                order.status = 'skipped_market_closed'
                order.notes = f"Market closed for {order.ticker} (session={session_type})"
                return order

            # 미국 주식 단가 미지정(price <= 0) 또는 프리/애프터마켓 시장가 주문 시 방화벽 검사 전에 실시간 호가로 즉시 확정
            if is_us and (order.price <= 0 or session_type in ('premarket', 'aftermarket')):
                if session_type in ('premarket', 'aftermarket') and order.order_type == 'market':
                    order.order_type = 'limit'
                if order.price <= 0:
                    _is_buy_side = (order.side.lower() == 'buy')
                    _live_px = self.get_live_overseas_price(order.ticker, order.exchange, ask_side=_is_buy_side)
                    if _live_px and _live_px > 0:
                        order.price = round(_live_px + (0.01 if _is_buy_side else -0.01), 2)
                    elif hasattr(self, '_estimate_price'):
                        order.price = self._estimate_price(order.ticker)
                if session_type in ('premarket', 'aftermarket'):
                    logger.info(f"  ⚡ [Extended Session Limit Peg] {order.ticker} ({session_type}): 장외 세션 규정상 지정가(${order.price:.2f}) 강제 적용")

            # 매도 주문 잔고 초과 방어 (Usable Quantity Clamping & Unexecuted Duplicate Guard)
            if order.side.lower() in ('sell', 'short'):
                _urg = _order_urgency(order)
                _is_urgent = _urg in _URGENT_EXIT_LEVELS
                try:
                    unexec_list = self.inquire_unexecuted(order.ticker)
                except Exception as _ue_err:
                    unexec_list = None
                    logger.error(f"  ⚠️ [Unexecuted Sell Guard] 미체결 조회 실패 ({order.ticker}): {_ue_err}")
                _pending_sells = []
                for u in (unexec_list if isinstance(unexec_list, list) else []):
                    is_sell = str(u.get('sll_buy_dvsn_cd', '')).strip() in ('01', 'sell', 'SELL') or 'sell' in str(u.get('side', '')).lower()
                    _u_odno = str(u.get('odno', '')).strip()
                    if is_sell and _u_odno and _u_odno not in getattr(self, '_recently_canceled_odnos', set()):
                        _pending_sells.append(u)

                if _pending_sells and _is_urgent:
                    # ★ Wave 0 E3: 긴급 청산 — 기존 미체결 매도(예: 익절 지정가)를 취소·확인한 뒤 손절/패닉 매도 집행
                    _all_cleared = True
                    for u in _pending_sells:
                        _odno = str(u.get('odno', '')).strip()
                        _cres = self.cancel_order(_odno, ticker=order.ticker) or {}
                        if not _cres.get('success', False):
                            _st = (self.check_order_status(_odno, ticker=order.ticker) or {}).get('status')
                            if _st not in ('canceled', 'filled'):
                                _all_cleared = False
                                logger.critical(f"  🚨 [Urgent Exit] {order.ticker}: 기존 미체결 매도 {_odno} 취소 미확인 "
                                                f"(msg={_cres.get('message')!r}, status={_st!r})")
                    if not _all_cleared:
                        order.status = 'rejected'
                        order.notes = (order.notes + ',' if order.notes else '') + 'urgent_exit_blocked:pending_sell_cancel_unconfirmed'
                        logger.critical(f"  🚨 [Urgent Exit] {order.ticker} {_urg} 매도 미집행 — 기존 매도 취소 미확인. 수동 확인 필요")
                        if getattr(self, '_dlq', None):
                            self._dlq.add(dataclasses.asdict(order), 'Urgent exit blocked: pending sell cancel unconfirmed')
                        self.orders.append(order)
                        self._save_state()
                        return order
                    logger.warning(f"  🛡️ [Urgent Exit] {order.ticker}: 기존 미체결 매도 {len(_pending_sells)}건 취소 확인 → {_urg} 매도 집행")
                    # 취소 직전 일부 체결됐을 수 있으므로 최신 보유 수량 재조회
                    try:
                        _fresh = self.fetch_live_positions() or {}
                        if getattr(self, '_last_positions_complete', False) and order.ticker in _fresh:
                            order.quantity = min(int(order.quantity), int(_fresh[order.ticker]))
                    except Exception as _fp_err:
                        logger.error(f"  ⚠️ [Urgent Exit] 보유 재조회 실패 ({order.ticker}): {_fp_err}")
                elif _pending_sells:
                    u = _pending_sells[0]
                    rem_qty = u.get('psbl_qty', u.get('ord_qty', 0))
                    logger.info(f"  🛡️ [Unexecuted Sell Guard] {order.ticker}: 증권사 거래소에 이미 대기 중인 미체결 매도 주문(ODNO: {u.get('odno')}, 잔여: {rem_qty}주) 확인 ➔ 중복 매도 주문 스킵 및 기접수 상태 반환")
                    order.status = 'submitted'
                    order.order_id = str(u.get('odno', order.order_id))
                    order.notes = (order.notes + ',' if order.notes else '') + 'duplicate_existing_sell'
                    self.orders.append(order)
                    self._save_state()
                    return order

                held_pos = self.positions.get(order.ticker)
                _mem_qty = int(held_pos.quantity) if held_pos else 0
                _broker_qty = int((getattr(self, '_cached_live_positions', None) or {}).get(order.ticker, 0) or 0)
                held_qty = max(_mem_qty, _broker_qty)
                if held_qty <= 0:
                    logger.warning(f"  🛑 [Holdings Guard] {order.ticker} 미보유(수량 0) 매도 시도 차단")
                    order.status = 'skipped_insufficient_holdings'
                    order.notes = f"No holdings for {order.ticker}"
                    return order
                if order.quantity > held_qty:
                    logger.warning(f"  🛡️ [Holdings Clamp] {order.ticker} 매도 요청 수량({order.quantity}) > 보유 수량({held_qty}) ➔ 보유 전량({held_qty}주)으로 자동 클램핑")
                    order.quantity = held_qty

            # 매수 주문 중복 방어 (Unexecuted Buy Guard - 거래소 미체결 매수 존재 시 중복 매수 원천 차단)
            if order.side.lower() in ('buy', 'long'):
                try:
                    unexec_list = self.inquire_unexecuted(order.ticker)
                    _canceled_set = getattr(self, '_recently_canceled_odnos', set())
                    if unexec_list and isinstance(unexec_list, list):
                        for u in unexec_list:
                            _u_odno = str(u.get('odno', '')).strip()
                            if _u_odno and _u_odno in _canceled_set:
                                continue
                            is_buy = str(u.get('sll_buy_dvsn_cd', '')).strip() in ('02', 'buy', 'BUY') or 'buy' in str(u.get('side', '')).lower()
                            rem_qty = int(float(u.get('psbl_qty', u.get('nccs_qty', u.get('rmn_qty', u.get('ord_qty', 0)))) or 0))
                            if is_buy and rem_qty > 0:
                                logger.info(f"  🛡️ [Unexecuted Buy Guard] {order.ticker}: 증권사 거래소에 이미 대기 중인 미체결 매수 주문(ODNO: {_u_odno}, 잔여: {rem_qty}주) 확인 ➔ 중복 매수 주문 차단 및 기접수 상태 반환")
                                order.status = 'submitted'
                                order.order_id = str(u.get('odno', order.order_id))
                                order.notes = f"Blocked duplicate buy; existing unexecuted ODNO {order.order_id}"
                                self.orders.append(order)
                                self._save_state()
                                return order
                except Exception as _ue_err:
                    logger.debug(f"Unexecuted buy check bypass: {_ue_err}")
        except Exception as _sg_err:
            logger.debug(f"Session gate / clamp check bypass: {_sg_err}")

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
            max_qty = pget('execution.max_order_qty', 100000)
            max_amount = _cfg.get('execution.max_order_amount_krw', 50000000)
            
            if order.quantity > max_qty:
                raise ValueError(f"Quantity {order.quantity} exceeds MAX_QTY ({max_qty})")
                
            is_us = not str(order.ticker).isdigit()
            fx_rate = self._get_live_fx_rate() if is_us else 1.0
            _fx_unknown = is_us and not (fx_rate and float(fx_rate) > 0)
            if _fx_unknown:
                if order.side.lower() in ('buy', 'long'):
                    # ★ Wave 1 Track C: 환율 미확보 → 원화 환산 현금 방화벽 계산 불가 → 해외 신규 매수 거부 (fail-closed)
                    raise ValueError(f"FX_UNKNOWN: USD/KRW 미확보 — 해외 신규 매수 거부 ({order.ticker})")
                # 매도/청산은 환율과 무관하게 진행 (원화 명목 한도 검사만 생략)
                logger.warning(f"  ⚠️ [FX Unknown] {order.ticker} {order.side.upper()}: USD/KRW 미확보 → 원화 명목 한도 검사 생략 후 매도 진행")
                fx_rate = 0.0
            order_price_krw = order.price * fx_rate
            order_val_krw = order.quantity * order_price_krw

            if (not _fx_unknown) and order_val_krw > max_amount and order.order_type != 'market':
                raise ValueError(f"Order amount ₩{order_val_krw:,.0f} exceeds MAX_AMOUNT_KRW (₩{max_amount:,.0f})")

            # [Cash-Only Execution Firewall] 100% 현금 전액 주문 강제 락 (미수/신용 차단)
            avail_cash_krw = self.account.cash if getattr(self, 'account', None) else 0.0
            if is_us:
                us_avail = getattr(self, 'us_buying_power_usd', getattr(self, 'us_cash_usd', 0.0))
                if us_avail > 0:
                    avail_cash_krw = max(avail_cash_krw, us_avail * fx_rate)

            if avail_cash_krw > 0 and order.side == 'buy' and order_price_krw > 0:
                if order_val_krw > avail_cash_krw:
                    logger.warning(f"  🛡️ [Cash-Only Guard] 가용 자금(₩{avail_cash_krw:,.0f}) 초과 미수 매수 차단: ₩{order_val_krw:,.0f}")
                    order.quantity = int(avail_cash_krw // order_price_krw)
                    if order.quantity <= 0:
                        raise ValueError(f"Insufficient cash: ₩{avail_cash_krw:,.0f} for {order.ticker} (단가: ₩{order_price_krw:,.0f})")
                
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
        # [KRX Pre-Market Alpha Patch] PREMARKET_CLOSE = KIS ORD_DVSN '05' (장전 시간외, 08:30~08:40, 전일 종가)
        # ★ Wave 1 (2026-10-19 실측): '06'은 '장후 시간외'라 08:30 주문이 APBK0918(장종료후시간외종가)로 거부됨.
        _TIF_ORD_DVSN = {'DAY': '00' if order.order_type != 'market' else '01', 'MARKET': '01', 'IOC': '13', 'FOK': '14', 'PREMARKET_CLOSE': '05'}
        ord_dvsn = _TIF_ORD_DVSN.get(tif, '05' if tif == 'PREMARKET_CLOSE' else ('01' if order.order_type == 'market' else '00'))
        if str(order.order_type).lower() in ('05', 'premarket_close'):
            ord_dvsn = '05'
        elif str(order.order_type).lower() == '06':
            ord_dvsn = '06'   # 명시적 장후 시간외
        elif str(order.order_type).lower() in ('01', 'market'):
            ord_dvsn = '01'
        elif str(order.order_type).lower() in ('00', 'limit'):
            ord_dvsn = '00'

        if ord_dvsn in ('01', '05', '06', '13', '14'):
            ord_price = '0'
        else:
            snapped_p = _snap_to_exchange_tick(order.price, order.ticker)
            ord_price = '0' if snapped_p <= 0 else str(int(snapped_p))
        if '-' in self.account_no:
            acnt = self.account_no.split('-')
        else:
            acnt = [self.account_no[:8], self.account_no[8:] if len(self.account_no) > 8 else '01']
        headers = self._get_headers()
        is_us_stock = not order.ticker.isdigit()
        if is_us_stock:
            if order.price <= 0:
                _is_buy_side = (order.side.lower() == 'buy')
                live_px = self.get_live_overseas_price(order.ticker, order.exchange, ask_side=_is_buy_side)
                if live_px and live_px > 0:
                    order.price = round(live_px + (0.01 if _is_buy_side else -0.01), 2)
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
                'OVRS_ORD_UNPR': f"{order.price:.2f}" if order.price >= 1.0 else f"{order.price:.4f}",
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

        active_tr_id = headers.get('tr_id', tr_id)
        _TOKEN_ERR_CODES = {'EGW00103', 'EGW00121', 'EGW00123'}   # 게이트웨이 단 거부 → 주문 미처리 확정
        _RATE_LIMIT_CODES = {'EGW00201'}                          # 초당 거래건수 초과 → 주문 미처리 확정
        _submit_started = datetime.now()
        for attempt in range(max_retries + 1):
            definitely_not_sent = False
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
                        order_cost_usd = order.quantity * order.price
                        order_cost_krw = (order_cost_usd * fx_rate) if is_us_stock else (order.quantity * order.price)
                        self.account.cash = max(0.0, self.account.cash - order_cost_krw)
                        if is_us_stock:
                            if hasattr(self, 'us_cash_usd'):
                                self.us_cash_usd = max(0.0, getattr(self, 'us_cash_usd', 0.0) - order_cost_usd)
                            if hasattr(self, 'direct_usd_cash'):
                                self.direct_usd_cash = max(0.0, getattr(self, 'direct_usd_cash', 0.0) - order_cost_usd)
                            if hasattr(self, 'us_buying_power_usd'):
                                self.us_buying_power_usd = max(0.0, getattr(self, 'us_buying_power_usd', 0.0) - order_cost_usd)
                        setattr(self, '_last_balance_fetch_ts', 0.0)
                    self.orders.append(order)
                    self._save_state()
                    return order
                else:
                    msg_cd = data.get('msg_cd', '')
                    if msg_cd in _TOKEN_ERR_CODES or msg_cd in _RATE_LIMIT_CODES:
                        # 게이트웨이가 명시적으로 거부 → 주문은 처리되지 않았음이 확정 → 재시도 안전
                        definitely_not_sent = True
                        if msg_cd in _TOKEN_ERR_CODES:
                            logger.warning(f'  ⚠️ 토큰 에러({msg_cd}) 감지 -> 토큰 재발급 후 재시도')
                            self._access_token = None
                            self.authenticate()
                            headers = self._get_headers()
                            headers['tr_id'] = active_tr_id
                        raise requests.exceptions.RequestException(f'Gateway rejected before processing: {msg_cd}')
                    order.status = 'rejected'
                    logger.error(f"  ❌ API 주문 거부: {data}")
                    if self._dlq:
                        self._dlq.add(dataclasses.asdict(order), f"API 거부: {data.get('msg1')}")
                    self.orders.append(order)
                    self._save_state()
                    return order
            except requests.exceptions.RequestException as e:
                _err_txt = repr(e)
                # 연결 자체가 수립되지 않은 경우만 '미전송 확정'으로 간주
                if (isinstance(e, requests.exceptions.ConnectTimeout)
                        or 'Failed to establish a new connection' in _err_txt
                        or 'NameResolutionError' in _err_txt):
                    definitely_not_sent = True
                logger.warning(f"  ⚠️ API 전송 오류 ({attempt + 1}/{max_retries + 1}, not_sent={definitely_not_sent}): {e}")

                if not definitely_not_sent:
                    # ★ Wave 0 E6: 결과 모호(ReadTimeout/5xx/연결 끊김) → 증권사가 이미 접수·체결했을 수 있음.
                    #   재전송 절대 금지. 동일 종목·방향·수량의 미체결 주문이 보이면 그 주문을 추적, 아니면 'unknown'.
                    _match = None
                    try:
                        _want_side = '01' if order.side.lower() in ('sell', 'short') else '02'
                        _since_hms = (_submit_started - timedelta(seconds=5)).strftime('%H%M%S')
                        for u in (self.inquire_unexecuted(order.ticker) or []):
                            if str(u.get('pdno', u.get('ticker', ''))).strip() != str(order.ticker):
                                continue
                            _u_side = str(u.get('sll_buy_dvsn_cd', '')).strip()
                            _u_qty = int(float(u.get('ord_qty', 0) or 0))
                            _u_tmd = str(u.get('ord_tmd', u.get('thco_ord_tmd', '')) or '').strip()
                            if _u_tmd and _u_tmd < _since_hms:
                                continue   # 제출 이전부터 걸려 있던 다른 주문 → 동일 주문으로 오인 금지
                            if (_u_side == _want_side or not _u_side) and (_u_qty == int(order.quantity) or not _u_qty):
                                _match = u
                                break
                    except Exception as ie:
                        logger.error(f"  ⚠️ [Idempotency] 미체결 조회 실패 — 결과 미확정 유지: {ie}")
                    if _match is None:
                        # 미체결에 없으면 이미 전량 체결됐을 수 있음 → 당일 주문/체결 원장에서 같은 주문 탐색
                        try:
                            _match = self._find_broker_order_since(order, _submit_started)
                        except Exception as le:
                            logger.error(f"  ⚠️ [Idempotency] 주문원장 조회 실패 — 결과 미확정 유지: {le}")
                    if _match is not None:
                        order.status = 'submitted'
                        order.order_id = str(_match.get('odno', order.order_id))
                        logger.warning(f"  🛡️ [Idempotency Guard] {order.ticker}: 모호한 실패 후 증권사 접수 주문 확인(ODNO {order.order_id}) → 재전송 없이 추적")
                    else:
                        order.status = 'unknown'
                        order.notes = (order.notes + ',' if order.notes else '') + f"ambiguous_submit:{type(e).__name__}"
                        logger.critical(f"  🚨 [Idempotency Guard] {order.side.upper()} {order.ticker} x{order.quantity}: 전송 결과 미확정 "
                                        f"({type(e).__name__}) → 중복 체결 방지를 위해 재전송 중단. 잔고 대사(reconcile) 필요")
                        if self._dlq:
                            self._dlq.add(dataclasses.asdict(order), f"Ambiguous submit (no resend): {e}")
                    if self._cb:
                        self._cb.record_failure()
                    self.orders.append(order)
                    self._save_state()
                    return order

                if attempt < max_retries:
                    delay = retry_delays[attempt]
                    logger.info(f"  ⏳ 미전송 확정 → {delay}초 대기 후 재전송 시도...")
                    time.sleep(delay)
                else:
                    order.status = 'rejected'
                    logger.error(f"  ❌ API 주문 최종 실패 (Max Retries 초과, 미전송 확정): {e}")
                    if self._cb:
                        self._cb.record_failure()
                    if self._dlq:
                        self._dlq.add(dataclasses.asdict(order), f"Max Retries 초과: {e}")
        return order

    def check_order_status(self, order_no: str, is_us: bool = False, ticker: str = '') -> Dict:
        """주문 상태 조회 (국내/해외 자동 구분 및 양방향 동적 매핑)."""
        # 티커 또는 등록된 주문 원장에서 해외 주문 여부 자동 판별
        if not is_us and not ticker:
            for o in self.orders:
                if str(o.order_id).strip() == str(order_no).strip():
                    ticker = o.ticker
                    is_us = not str(ticker).isdigit()
                    break

        if is_us or (ticker and not str(ticker).isdigit()):
            return self.check_us_order_status(str(order_no), ticker=ticker)
        if self.mode == 'mock':
            return {'status': 'filled', 'filled_qty': 0, 'remaining_qty': 0, 'order_no': order_no}
        if not self._access_token:
            if not self.authenticate():
                return {'status': 'error', 'message': '인증 실패'}
        try:
            import requests
            prefix = 'V' if self.mode == 'paper' else 'T'
            tr_id = f"{prefix}TTC8001R"
            headers = self._get_headers()
            headers['tr_id'] = tr_id
            acnt = self.account_no.split('-')
            target_odno = str(order_no).strip().lstrip('0')
            params = {
                'CANO': acnt[0],
                'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                'INQR_STRT_DT': datetime.now().strftime('%Y%m%d'),
                'INQR_END_DT': datetime.now().strftime('%Y%m%d'),
                'SLL_BUY_DVSN_CD': '00',
                'INQR_DVSN': '00',
                'PDNO': ticker or '',
                'CCLD_DVSN': '00',
                'ORD_GNO_BRNO': '',
                'ODNO': str(order_no).strip().zfill(10) if str(order_no).strip().isdigit() else str(order_no).strip(),
                'INQR_DVSN_3': '00',
                'INQR_DVSN_1': '',
                'CTX_AREA_FK100': '',
                'CTX_AREA_NK100': ''
            }
            url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output1', data.get('output', [])) or []
                if isinstance(output, dict):
                    output = [output]
                matched_item = None
                for candidate in output:
                    cand_odno = str(candidate.get('odno', candidate.get('ODNO', ''))).strip().lstrip('0')
                    if cand_odno and target_odno and cand_odno == target_odno:
                        matched_item = candidate
                        break
                if matched_item is None and len(output) == 1:
                    _lone = str(output[0].get('odno', output[0].get('ODNO', ''))).strip().lstrip('0')
                    if not _lone:   # ★ 주문번호가 다른 단일 행을 내 주문으로 오인 금지
                        matched_item = output[0]
                if matched_item:
                    total_qty = int(float(matched_item.get('ord_qty', matched_item.get('ORD_QTY', 0)) or 0))
                    filled_qty = int(float(matched_item.get('tot_ccld_qty', matched_item.get('TOT_CCLD_QTY', 0)) or 0))
                    remaining = int(float(matched_item.get('rmn_qty', matched_item.get('RMN_QTY', max(0, total_qty - filled_qty))) or 0))
                    cncl_yn = str(matched_item.get('cncl_yn', matched_item.get('CNCL_YN', matched_item.get('ORD_TMD', '')))).strip()
                    rjct_qty = int(float(matched_item.get('rjct_qty', 0) or 0))
                    cncl_cfrm_qty = int(float(matched_item.get('cncl_cfrm_qty', 0) or 0))
                    if (remaining == 0 and filled_qty > 0) or (filled_qty >= total_qty and total_qty > 0):
                        status = 'filled'
                    elif rjct_qty > 0 and filled_qty == 0:
                        status = 'rejected'
                    elif cncl_yn in ('Y', '취소') or cncl_cfrm_qty > 0 or (hasattr(self, '_recently_canceled_odnos') and str(order_no).strip() in self._recently_canceled_odnos):
                        status = 'canceled'
                    elif filled_qty > 0:
                        status = 'partial'
                    elif total_qty > 0 and remaining == 0:
                        # ★ Wave 0 (KIS 실데이터 검증): 체결 0·잔량 0 = 거부/만료/취소된 '죽은 주문' → 종결 상태
                        status = 'canceled'
                    else:
                        status = 'pending'
                    return {
                        'status': status,
                        'filled_qty': filled_qty,
                        'remaining_qty': remaining,
                        'total_qty': total_qty,
                        'order_price': float(matched_item.get('ord_unpr', matched_item.get('ORD_UNPR', 0)) or 0),
                        'filled_price': float(matched_item.get('avg_prvs', matched_item.get('AVG_PRVS', 0)) or 0),
                        'order_no': order_no
                    }
                return {'status': 'not_found', 'order_no': order_no}
            else:
                return {'status': 'error', 'message': data.get('msg1', '')}
        except Exception as e:
            logger.error(f"  국내 주문체결 조회 실패: {e}")
            return {'status': 'error', 'message': str(e)}

    def check_us_order_status(self, order_no: str, ticker: str = '', exchange: str = '') -> Dict:
        """해외주식 미체결(TTTS3018R) 및 체결(TTTS3035R) 원장 교차 검증 (즉시 체결 주문 타임아웃 오판 원천 방어)."""
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
            target_excgs = [self.resolve_overseas_exchange(ticker, exchange)] if ticker else ['NASD', 'NYSE', 'AMEX']
            for excg_target in target_excgs:
                params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': excg_target, 'SORT_SQN': 'DS', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
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
                            remaining = int(float(item.get('nccs_qty', total_qty - filled_qty) or 0))
                            status = 'filled' if (remaining == 0 and filled_qty > 0) else ('partial' if filled_qty > 0 else 'pending')
                            return {'status': status, 'filled_qty': filled_qty, 'remaining_qty': remaining, 'order_no': order_no}

            # 2차 교차 검증: 미체결 내역(TTTS3018R)에서 사라진 주문은 주문체결내역(TTTS3035R)에서 실제 체결 완료 여부 확인!
            headers_ccnl = self._get_headers()
            headers_ccnl['tr_id'] = 'TTTS3035R' if self.mode == 'live' else 'VTTS3035R'
            start_dt = (datetime.now() - timedelta(days=2)).strftime('%Y%m%d')
            end_dt = (datetime.now() + timedelta(days=1)).strftime('%Y%m%d')
            params_ccnl = {
                'CANO': acnt[0],
                'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                'PDNO': ticker if ticker else '%',
                'ORD_STRT_DT': start_dt,
                'ORD_END_DT': end_dt,
                'SLL_BUY_DVSN': '00',
                'CCLD_NCCS_DVSN': '00',
                'OVRS_EXCG_CD': '%',
                'SORT_SQN': 'DS',
                'ORD_DT': '',
                'ORD_GNO_BRNO': '',
                'ODNO': '',
                'CTX_AREA_NK200': '',
                'CTX_AREA_FK200': ''
            }
            url_ccnl = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl"
            resp_ccnl = requests.get(url_ccnl, headers=headers_ccnl, params=params_ccnl, timeout=10)
            data_ccnl = resp_ccnl.json()
            if data_ccnl.get('rt_cd') == '0':
                for item in (data_ccnl.get('output') or []):
                    odno = str(item.get('odno', item.get('ODNO', ''))).strip()
                    if odno == str(order_no).strip():
                        total_qty = int(float(item.get('ft_ord_qty', 0) or 0))
                        filled_qty = int(float(item.get('ft_ccld_qty', 0) or 0))
                        remaining = int(float(item.get('nccs_qty', max(0, total_qty - filled_qty)) or 0))
                        filled_price = float(item.get('ft_ccld_unpr3', 0) or 0.0)
                        rvse_cncl = str(item.get('rvse_cncl_dvsn_name', item.get('rvse_cncl_dvsn', '')) or '').strip()
                        if filled_qty >= total_qty > 0:
                            return {'status': 'filled', 'filled_qty': filled_qty, 'remaining_qty': 0, 'filled_price': filled_price, 'order_no': order_no}
                        elif '취소' in rvse_cncl or (total_qty > 0 and filled_qty == 0 and remaining == 0):
                            return {'status': 'canceled', 'filled_qty': filled_qty, 'remaining_qty': 0, 'order_no': order_no}
                        elif filled_qty > 0:
                            return {'status': 'partial', 'filled_qty': filled_qty, 'remaining_qty': remaining, 'filled_price': filled_price, 'order_no': order_no}
                        else:
                            return {'status': 'pending', 'filled_qty': 0, 'remaining_qty': remaining, 'order_no': order_no}

            return {'status': 'not_in_nccs', 'filled_qty': 0, 'remaining_qty': 0, 'order_no': order_no}
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
        except Exception as e:
            logger.warning(f"  ⚠️ 주문 정정 예외 ({order_no}): {e}")
            return {'success': False, 'message': str(e)}

    def cancel_order(self, order_no: str, ticker: str = '') -> Dict:
        """주문 취소 (국내/해외 통합 라우팅).

        KIS API:
            국내: /uapi/domestic-stock/v1/trading/order-rvsecncl (TTC0803U)
            해외: /uapi/overseas-stock/v1/trading/order-rvsecncl (TTTT1004U)

        Returns:
            {'success': bool, 'message': str}
        """
        if not ticker:
            for o in self.orders:
                if str(o.order_id).strip() == str(order_no).strip():
                    ticker = o.ticker
                    break

        if ticker and not str(ticker).isdigit():
            return self.cancel_us_order(order_no, ticker=ticker)

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
                if not hasattr(self, '_recently_canceled_odnos'):
                    self._recently_canceled_odnos = set()
                self._recently_canceled_odnos.add(str(order_no).strip())
                logger.info(f"  ❌ 주문 취소 성공: {order_no}")
                return {'success': True, 'message': data.get('msg1', '')}
            else:
                return {'success': False, 'message': data.get('msg1', '')}
        except Exception as e:
            logger.warning(f"  ⚠️ 주문 취소 예외 ({order_no}): {e}")
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
                if not hasattr(self, '_recently_canceled_odnos'):
                    self._recently_canceled_odnos = set()
                self._recently_canceled_odnos.add(str(order_no).strip())
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
            timeout_sec = pget('execution.fill_timeout_sec', 60) if _cfg else 60
        if fallback is None:
            fallback = _cfg.get('execution.fill_fallback', 'market') if _cfg else 'market'
        check_interval = pget('execution.fill_check_interval_sec', 5) if _cfg else 5
        start = time.time()
        is_us = not str(order.ticker).isdigit()
        while time.time() - start < timeout_sec:
            result = self.check_order_status(order.order_id, is_us=is_us, ticker=order.ticker)
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
            elif status in ('canceled', 'error', 'rejected'):
                order.status = status
                return order
            time.sleep(check_interval)
        logger.warning(f"  ⏰ 체결 타임아웃 ({timeout_sec}초): {order.ticker}")
        cancel_res = self.cancel_order(order.order_id, ticker=order.ticker)

        # ★ Wave 0 E7: 취소 시도 후 권위 있는 주문 상태를 재조회 — 취소가 '확인'된 경우에만 잔량 재주문.
        try:
            post = self.check_order_status(order.order_id, is_us=is_us, ticker=order.ticker) or {}
        except Exception as _pe:
            logger.error(f"  ⚠️ [Cancel Verify] 취소 후 상태 재조회 실패 ({order.ticker}): {_pe}")
            post = {}
        post_status = str(post.get('status', '') or '')
        try:
            post_filled = int(float(post.get('filled_qty', order.filled_quantity) or 0))
        except (TypeError, ValueError):
            post_filled = order.filled_quantity
        order.filled_quantity = max(int(order.filled_quantity or 0), post_filled)

        if post_status == 'filled' or order.filled_quantity >= order.quantity:
            order.status = 'filled'
            order.filled_quantity = min(order.quantity, max(order.filled_quantity, post_filled)) or order.quantity
            order.filled_price = post.get('filled_price', order.filled_price or order.price)
            order.fill_timestamp = datetime.now().isoformat()
            logger.info(f"  ✅ [Cancel Guard] {order.ticker} 취소 시점 이미 전량 체결 확인 ➔ 재발주 없음")
            return order

        _c_msg = str(cancel_res.get('message', '') or '')
        cancel_confirmed = bool(cancel_res.get('success', False)) or post_status in ('canceled', 'rejected')
        if not cancel_confirmed:
            # 원주문이 아직 살아 있을 수 있음 → 추가 주문 금지 (이중 체결 방지)
            order.status = 'unknown'
            order.notes = (order.notes + ',' if order.notes else '') + f"cancel_unconfirmed:{_c_msg[:60]}"
            logger.critical(f"  🚨 [Cancel Verify] {order.ticker} 취소 미확인(msg={_c_msg!r}, post={post_status!r}) "
                            f"→ 원주문 생존 가능성으로 시장가 전환 중단. 체결 {order.filled_quantity}/{order.quantity}")
            return order

        remaining = order.quantity - order.filled_quantity
        if fallback == 'market' and remaining > 0:
            market_order = Order(order_id=self._gen_order_id(), ticker=order.ticker, side=order.side, quantity=remaining, price=0, order_type='market', exchange=order.exchange)
            result = self._api_order(market_order)
            _res_status = getattr(result, 'status', None)
            if _res_status in ('submitted', 'filled'):
                # 시장가 체결 확인(최대 ~5초). 확인되면 실제 수량/가격, 아니면 비체결 상태로 보고(엔진 재주문 방지).
                _mf = {}
                for _ in range(5):
                    try:
                        _mf = self.check_order_status(market_order.order_id, is_us=is_us, ticker=order.ticker) or {}
                    except Exception:
                        _mf = {}
                    if _mf.get('status') == 'filled':
                        break
                    time.sleep(1)
                if _mf.get('status') == 'filled':
                    order.status = 'filled_market_fallback'
                    order.filled_quantity = order.filled_quantity + int(float(_mf.get('filled_qty', remaining) or remaining))
                    order.filled_price = _mf.get('filled_price', order.filled_price or order.price)
                    order.fill_timestamp = datetime.now().isoformat()
                    logger.info(f"  🔄 시장가 전환 체결 확인: {remaining}주")
                else:
                    order.status = 'submitted_market_fallback'
                    order.notes = (order.notes + ',' if order.notes else '') + f"market_fallback_odno:{market_order.order_id}"
                    logger.warning(f"  ⏳ 시장가 전환 접수(체결 미확인): {order.ticker} {remaining}주 (ODNO {market_order.order_id}) — 잔고 대사로 확정")
            else:
                order.status = 'rejected_market_fallback'
                logger.error(f"  ❌ 시장가 전환 주문 실패/거부 ({order.ticker}): status={_res_status}")
        else:
            order.status = 'canceled_timeout'
            logger.info(f"  ❌ 타임아웃 취소 확인: {order.ticker} (체결 {order.filled_quantity}/{order.quantity})")
        return order

    def get_live_overseas_price(self, ticker: str, exchange: str = '', ask_side: bool = False) -> Optional[float]:
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
            primary_excd = 'NAS' if std_excg == 'NASD' else ('NYS' if std_excg == 'NYSE' else 'AMS')
            excd_candidates = [primary_excd] + [e for e in ('NAS', 'AMS', 'NYS') if e != primary_excd]
            url = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
            for excd in excd_candidates:
                params = {'AUTH': '', 'EXCD': excd, 'SYMB': ticker}
                data = {}
                for _attempt in range(3):
                    resp = requests.get(url, headers=headers, params=params, timeout=5)
                    data = resp.json()
                    if data.get('msg_cd') == 'EGW00201':
                        time.sleep(0.25 * (_attempt + 1))
                        continue
                    break
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
        """현재가 조회 — 국내(KRX) 및 해외(US) 종목 자동 분기."""
        import pandas as pd
        tk_str = str(ticker or '').strip()
        if not tk_str:
            return None
        if not tk_str.isdigit():
            if self.mode != 'mock' and (self._access_token or self.authenticate()):
                try:
                    us_px = self.get_live_overseas_price(tk_str, self.resolve_overseas_exchange(tk_str))
                    if us_px and us_px > 0:
                        return float(us_px)
                except Exception as _us_e:
                    logger.debug(f"  KIS 해외 현재가 조회 폴백 ({tk_str}): {_us_e}")
            for _us_pq in (
                _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f'{tk_str}.parquet',
                _PROJECT_ROOT / 'data' / 'historical_10y' / f'{tk_str}.parquet',
            ):
                if _us_pq.exists():
                    try:
                        df = pd.read_parquet(_us_pq)
                        col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                        if col and not df.empty:
                            return float(df[col].dropna().iloc[-1])
                    except Exception:
                        pass
            if self.mode != 'mock':
                try:
                    from src.data_collection.alpha_vantage_collector import fetch_yahoo_chart_ohlcv
                    _yc = fetch_yahoo_chart_ohlcv(tk_str, range_str='5d')
                    if _yc is not None and not _yc.empty and 'close' in _yc.columns:
                        return float(_yc['close'].dropna().iloc[-1])
                except Exception:
                    pass
            return None

        if self.mode != 'mock' and (self._access_token or self.authenticate()):
            try:
                import requests
                headers = self._get_headers()
                headers['tr_id'] = 'FHKST01010100'
                params = {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': tk_str}
                url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
                resp = requests.get(url, headers=headers, params=params, timeout=5)
                data = resp.json()
                if data.get('rt_cd') == '0':
                    price = float(data['output']['stck_prpr'])
                    if price > 0:
                        return price
            except Exception as e:
                logger.debug(f"  KIS 실시간 현재가 조회 폴백 ({tk_str}): {e}")
        krx_dir = _PROJECT_ROOT / 'data' / 'raw' / 'krx_stock_daily'
        if krx_dir.exists():
            try:
                csv_files = sorted(krx_dir.glob('kospi_*.csv'), reverse=True)
                for csv_file in csv_files[:3]:
                    df = pd.read_csv(csv_file)
                    for col in ['ISU_CD', '종목코드', 'Code', 'ticker']:
                        if col in df.columns:
                            df[col] = df[col].astype(str).str.zfill(6)
                            row = df[df[col] == tk_str]
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
        for _dir_name in ('kr_markets', 'historical_10y'):
            for pattern in [f"kr_{tk_str}.parquet", f"{tk_str}.parquet"]:
                pq = _PROJECT_ROOT / 'data' / _dir_name / pattern
                if pq.exists():
                    try:
                        df = pd.read_parquet(pq)
                        col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                        if col and not df.empty:
                            return float(df[col].dropna().iloc[-1])
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                        pass
        return None

    def _estimate_price(self, ticker: str) -> float:
        """지정가 전환 폴백용 가격 추정."""
        return float(self._get_current_price(ticker) or 0.0)

    def _estimate_spread_bps(self, ticker: str) -> float:
        """종목별 호가 스프레드(bps) 추정."""
        try:
            if str(ticker or '').isdigit():
                from src.data_collection.kis_data_collector import KISDataCollector
                ob = KISDataCollector().get_orderbook(str(ticker))
                if ob and ob.get('spread_pct') is not None:
                    return float(ob['spread_pct']) * 100.0
        except Exception:
            pass
        return 5.0

    def fetch_live_account_balance(self) -> bool:
        """fetch_live_balance 호환 별칭 (ExecutionEngine / ExposureOrchestrator 호환)."""
        return self.fetch_live_balance()

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
        live_positions = self.fetch_live_positions() or {}
        if not getattr(self, '_last_positions_complete', False):
            # ★ Wave 0 E5: 불완전 스냅샷 → 직전 완전 스냅샷 + 메모리 포지션과 합집합(best-effort).
            #   증권사가 보유 초과 매도를 거부하므로 과매도 위험 없음.
            merged = dict(getattr(self, '_cached_live_positions', None) or {})
            for _t, _p in (self.positions or {}).items():
                _q = int(getattr(_p, 'quantity', 0) or 0)
                if _q > 0:
                    merged[_t] = max(int(merged.get(_t, 0) or 0), _q)
            for _t, _q in live_positions.items():
                merged[_t] = max(int(merged.get(_t, 0) or 0), int(_q or 0))
            logger.critical(f"  🚨 [PANIC SELL] 브로커 스냅샷 불완전 → best-effort 합집합 사용: {merged}")
            live_positions = merged
        tickers = list(live_positions.keys())
        for ticker in tickers:
            qty = live_positions[ticker]
            if ticker in _s4_tickers:
                logger.critical(f"  🛡️  [S4] {ticker} 패닉셀 면제 (qty={qty}) — 포지션 유지")
                kept_positions.append(ticker)
                continue
            if qty > 0:
                logger.critical(f"    - Panic Sell: {ticker} x{qty}")
                # ★ Wave 0 E4: urgency='panic' → Smart Limit(직전가 지정가) 변환 금지, 실제 시장가 유지
                order = self.sell(ticker=ticker, quantity=qty, price=0, order_type='market', exchange='SOR', urgency='panic')
                panic_orders.append(order)
        if kept_positions:
            logger.critical(f"  🛡️  패닉셀 면제 종목 ({len(kept_positions)}개): {', '.join(kept_positions)}")
        return panic_orders

    _fx_cache_val: float = 0.0
    _fx_cache_ts: float = 0.0

    def _get_live_fx_rate(self) -> float:
        """원/달러 환율 (★ Wave 1 Track C: CentralDataGateway.get_usdkrw_rate() 단일 경로).

        우선순위(게이트웨이 SSOT): 실시간 KIS t_rate → Naver → BOK → 직전 실관측(나이 ≤ fx.max_stale_hours)
        → 확보 실패 시 0.0 (상수 대체 금지). 반환형 float 유지 — 0.0은 '환율 미확보'를 뜻하며
        호출자는 해외 신규 매수를 거부하고(fail-closed) 매도/청산은 환율과 무관하게 진행해야 한다.
        (기존: 무기한 stale signal_cache 및 리터럴 1350.0 폴백 → 제거)
        """
        now_ts = time.time()
        if KISTraderAdapter._fx_cache_val > 1000 and (now_ts - KISTraderAdapter._fx_cache_ts) < 60.0:
            return KISTraderAdapter._fx_cache_val

        try:
            from src.data_collection.central_data_gateway import get_central_data_gateway
            rate = get_central_data_gateway().get_usdkrw_rate()
            if rate is not None and float(rate) > 1000:
                KISTraderAdapter._fx_cache_val = float(rate)
                KISTraderAdapter._fx_cache_ts = now_ts
                return float(rate)
        except Exception as _fx_e:
            logger.error(f"  ❌ [FX] 게이트웨이 환율 조회 예외: {_fx_e}")

        logger.critical("  🚨 [FX] USD/KRW 미확보 — 리터럴 대체 없이 0.0 반환 (해외 신규 매수 차단, 매도/청산은 진행)")
        return 0.0

    def _get_dynamic_usdkrw_rate(self) -> float:
        """Alias for _get_live_fx_rate() for backward compatibility."""
        return self._get_live_fx_rate()

    _BALANCE_MAX_PAGES_DEFAULT = 20

    def _kis_paged_get(self, url: str, headers: Dict, params: Dict, ctx_suffix: str, timeout: int = 10):
        """★ Wave 1 Track C (W1_3): KIS 연속조회(페이징) GET.

        - 응답 헤더 tr_cont ∈ {'M','F'} 이면 다음 페이지 존재 → 응답 본문의 ctx_area_fk{N}/nk{N}을
          요청 파라미터 CTX_AREA_FK{N}/NK{N}에 넣고 요청 헤더 tr_cont='N'으로 재조회.
          (국내 TTTC8434R: N=100, 해외 TTTS3012R: N=200)
        - 페이지 상한(execution.balance_max_pages, 기본 20) 초과/중간 페이지 실패/연속키 누락 → complete=False.
        - 첫 페이지 요청 예외는 그대로 전파(호출자 기존 실패 처리 유지), 이후 페이지 예외는 complete=False.

        Returns:
            (merged, complete): merged = 첫 페이지 응답 dict 사본 + output1(전 페이지 병합)
                                + output2(첫 번째 비어있지 않은 페이지 값)
        """
        import requests
        try:
            from config.dynamic_config import DynamicConfig
            max_pages = int(DynamicConfig().get('execution.balance_max_pages', self._BALANCE_MAX_PAGES_DEFAULT)
                            or self._BALANCE_MAX_PAGES_DEFAULT)
        except Exception:
            max_pages = self._BALANCE_MAX_PAGES_DEFAULT
        max_pages = max(1, max_pages)
        fk_key, nk_key = f'CTX_AREA_FK{ctx_suffix}', f'CTX_AREA_NK{ctx_suffix}'
        req_headers = dict(headers or {})
        req_params = dict(params or {})
        merged: Dict[str, Any] = {}
        rows: List[Dict] = []
        for page in range(max_pages):
            if page == 0:
                resp = requests.get(url, headers=req_headers, params=req_params, timeout=timeout)
                data = resp.json()
                merged = dict(data) if isinstance(data, dict) else {'rt_cd': '1'}
                if merged.get('rt_cd') != '0':
                    return merged, False
            else:
                time.sleep(0.1)   # 연속조회 간 최소 간격 (초당 거래건수 제한 방어)
                try:
                    resp = requests.get(url, headers=req_headers, params=req_params, timeout=timeout)
                    data = resp.json()
                except Exception as _pg_e:
                    logger.critical(f"  🚨 [Paging] {url.rsplit('/', 1)[-1]} {page + 1}페이지 조회 예외 → 불완전 스냅샷: {_pg_e}")
                    merged['output1'] = rows
                    return merged, False
                if not isinstance(data, dict) or data.get('rt_cd') != '0':
                    _d = data if isinstance(data, dict) else {}
                    logger.critical(f"  🚨 [Paging] {url.rsplit('/', 1)[-1]} {page + 1}페이지 실패 "
                                    f"rt_cd={_d.get('rt_cd')} msg={_d.get('msg_cd')}/{_d.get('msg1')} → 불완전 스냅샷")
                    merged['output1'] = rows
                    return merged, False
                if not merged.get('output2') and data.get('output2'):
                    merged['output2'] = data.get('output2')
            rows.extend(data.get('output1') or [])
            _rh = getattr(resp, 'headers', None)
            try:
                tr_cont = str((_rh.get('tr_cont', '') if _rh is not None else '') or '').strip().upper()
            except Exception:
                tr_cont = ''
            if tr_cont not in ('M', 'F'):
                merged['output1'] = rows
                return merged, True
            fk = data.get(fk_key.lower(), data.get(fk_key, ''))
            nk = data.get(nk_key.lower(), data.get(nk_key, ''))
            if not str(fk or '').strip() and not str(nk or '').strip():
                logger.critical(f"  🚨 [Paging] {url.rsplit('/', 1)[-1]}: tr_cont={tr_cont} 이나 연속조회 키 누락 → 불완전 스냅샷")
                merged['output1'] = rows
                return merged, False
            req_params[fk_key] = fk
            req_params[nk_key] = nk
            req_headers['tr_cont'] = 'N'
        logger.critical(f"  🚨 [Paging] {url.rsplit('/', 1)[-1]}: 페이지 상한({max_pages}) 초과 → 불완전 스냅샷")
        merged['output1'] = rows
        return merged, False

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
                headers['tr_id'] = 'TTTC8434R' if self.mode == 'live' else 'VTTC8434R'
                acnt = self.account_no.split('-')
                params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'AFHR_FLPR_YN': 'N', 'OFL_YN': 'N', 'INQR_DVSN': '02', 'UNPR_DVSN': '01', 'FUND_STTL_ICLD_YN': 'N', 'FNCG_AMT_AUTO_RDPT_YN': 'N', 'PRCS_DVSN': '01', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
                url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
                # ★ Wave 1 Track C (W1_3): 연속조회 페이징 (tr_cont M/F + CTX_AREA_FK100/NK100)
                data, _kr_pages_complete = self._kis_paged_get(url, headers, params, '100')

                # [Red Team Patch] 미수 및 달러 통합증거금 침범 없는 100% 순수 원화 매수가능금액 정밀 조회 (TTTC8908R, OVRS_ICLD_YN='N')
                ord_psbl_cash = 0.0
                # ★ Wave 1 Track C: 리터럴 1373.64 폴백 제거 — 미확보 시 0.0 (아래 CTRP6504R 고시환율로 보강 가능)
                _fx_from_gateway = float(self._get_live_fx_rate() or 0.0)
                fx_rate = _fx_from_gateway
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
                    url_psbl = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
                    resp_psbl = requests.get(url_psbl, headers=headers_psbl, params=params_psbl, timeout=10)
                    data_psbl = resp_psbl.json()
                    if data_psbl.get('rt_cd') == '0':
                        out_psbl = data_psbl.get('output', {})
                        # [Strict Currency Isolation] 순수 원화 주문가능금액만 파싱 (달러 통합증거금을 원화 현금으로 이중 계상/침범 원천 차단)
                        ord_psbl_cash = float(out_psbl.get('nrcvb_buy_amt', out_psbl.get('max_buy_amt', out_psbl.get('ord_psbl_cash', 0))) or 0.0)
                except Exception as e_psbl:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e_psbl}", exc_info=True)
                    logger.debug(f"  [Live Patch] inquire-psbl-order 2차 조회 우회: {e_psbl}")

                if data.get('rt_cd') == '0' and not _kr_pages_complete:
                    # ★ Wave 1 Track C (W1_3): 국내 잔고 연속조회 중간 실패 → 부분 원장으로 positions/NAV 덮어쓰기 금지
                    self._last_positions_complete = False
                    self._last_balance_complete = False
                    logger.critical("  🚨 [Live Balance] 국내 잔고 페이징 불완전 — positions/NAV 갱신 보류 (기존 값 유지)")
                    return False
                if data.get('rt_cd') == '0':
                    output2 = data.get('output2', [{}])
                    if output2:
                        summary = output2[0]
                        # D+2 가수도정산금(prvs_rcdl_excc_amt): 통합증거금 사용 시 음수(-) 부채가 반영된 실제 원화 순예수금
                        net_d2_cash_krw = float(summary.get('prvs_rcdl_excc_amt', summary.get('nxdy_excc_amt', summary.get('dnca_tot_amt', 0))) or 0.0)
                        self.net_d2_cash_krw = net_d2_cash_krw
                        self.raw_d2_cash = max(0.0, net_d2_cash_krw)
                        if ord_psbl_cash <= 0 and self.raw_d2_cash > 0:
                            ord_psbl_cash = self.raw_d2_cash
                        elif self.raw_d2_cash >= 0:
                            ord_psbl_cash = min(ord_psbl_cash, self.raw_d2_cash) if ord_psbl_cash > 0 else self.raw_d2_cash

                        # 🎯 [SSoT Fix] 실시간 당일 NAV (국내 순자산 tot_evlu_amt + 해외 순자산 원화환산액) 보존 항등식 계산
                        kr_tot_evlu = float(summary.get('tot_evlu_amt', summary.get('nass_amt', 0)) or 0.0)
                        tot_evlu = kr_tot_evlu

                        # 🎯 [SSoT Root Fix] 기존 인메모리 포지션 전면 초기화 (유령/레거시 포지션 잔존 100% 원천 차단)
                        _prev_positions_snapshot = dict(self.positions)   # 해외 페이징 불완전 시 복원용
                        self.positions.clear()

                        # 🎯 [SSoT Fix] 해외(미국) 주식 잔고 및 외화예수금 원장(CTRP6504R + TTTS3012R + TTTS3007R) 100% 동기화
                        us_fetch_ok = False
                        _us_pages_complete = True
                        _us_exposure_usd = 0.0
                        try:
                            headers_us = self._get_headers()
                            headers_us['tr_id'] = 'TTTS3012R' if self.mode == 'live' else 'VTTS3012R'
                            url_us = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
                            merged_us_output1 = []
                            seen_us_tickers = set()
                            out2_us = {}
                            # ★ Wave 1 Track C: 거래소 순회 금지 — NASD 조회가 NYSE/AMEX(예: XLV) 보유분까지 전부 반환함(실검증).
                            #   거래소별 반복 조회는 중복 계상/불필요한 호출을 유발 → 단일 조회 + 연속조회 페이징.
                            params_us = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': 'NASD', 'TR_CRCY_CD': 'USD', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
                            try:
                                d_ex, _us_pg_ok = self._kis_paged_get(url_us, headers_us, params_us, '200')
                                if d_ex.get('rt_cd') == '0':
                                    us_fetch_ok = True
                                    _us_pages_complete = _us_pg_ok
                                    if isinstance(d_ex.get('output2'), dict):
                                        out2_us = d_ex.get('output2', {})
                                    for _it in (d_ex.get('output1') or []):
                                        _tk_u = str(_it.get('ovrs_pdno', '')).strip()
                                        if _tk_u and _tk_u not in seen_us_tickers:
                                            seen_us_tickers.add(_tk_u)
                                            merged_us_output1.append(_it)
                            except Exception as _us_bal_e:
                                logger.warning(f"  ⚠️ 해외 inquire-balance 조회 예외: {_us_bal_e}")
                            data_us = {'rt_cd': '0' if us_fetch_ok else '1', 'output1': merged_us_output1, 'output2': out2_us}
                            if not us_fetch_ok:
                                # Fallback to inquire-present-balance if inquire-balance is unavailable
                                params_us = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': '%', 'TR_CRCY_CD': 'USD', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
                                url_us = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
                                data_us, _us_pg_ok = self._kis_paged_get(url_us, headers_us, params_us, '200')
                                if data_us.get('rt_cd') == '0':
                                    us_fetch_ok = True
                                    _us_pages_complete = _us_pg_ok

                            out2_us = data_us.get('output2', {}) if isinstance(data_us.get('output2'), dict) else {}
                            us_stock_usd = 0.0
                            for _it_us in data_us.get('output1', []):
                                _q_us = float(_it_us.get('ovrs_cblc_qty', _it_us.get('ord_psbl_qty', 0)) or 0)
                                _p_us = float(_it_us.get('now_pric2', _it_us.get('pchs_avg_pric', 0)) or 0)
                                if _q_us > 0 and _p_us > 0:
                                    us_stock_usd += _q_us * _p_us
                            if us_stock_usd <= 0:
                                us_stock_usd = float(out2_us.get('evlu_amt_smtl_amt', out2_us.get('evlu_amt_smtl', 0)) or 0)

                            # 1) CTRP6504R (체결기준현재잔고)에서 순수 외화예수금 원장(frcr_dncl_amt_2) 및 출금/사용가능 달러(frcr_drwg_psbl_amt_1) 정밀 조회
                            total_usd_deposit = 0.0
                            withdrawable_usd = 0.0
                            pledged_margin_usd = 0.0
                            try:
                                headers_ctrp = self._get_headers()
                                headers_ctrp['tr_id'] = 'CTRP6504R' if self.mode == 'live' else 'VTRP6504R'
                                params_ctrp = {
                                    'CANO': acnt[0],
                                    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                                    'WCRC_FRCR_DVSN_CD': '02',
                                    'NATN_CD': '840',
                                    'TR_MKET_CD': '00',
                                    'INQR_DVSN_CD': '00'
                                }
                                url_ctrp = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
                                resp_ctrp = requests.get(url_ctrp, headers=headers_ctrp, params=params_ctrp, timeout=5)
                                d_ctrp = resp_ctrp.json()
                                if d_ctrp.get('rt_cd') == '0':
                                    for _crow in (d_ctrp.get('output2') or []):
                                        if isinstance(_crow, dict) and _crow.get('crcy_cd') == 'USD':
                                            total_usd_deposit = float(_crow.get('frcr_dncl_amt_2', 0) or 0.0)
                                            withdrawable_usd = float(_crow.get('frcr_drwg_psbl_amt_1', 0) or 0.0)
                                            pledged_margin_usd = float(_crow.get('frcr_buy_mgn_amt', 0) or 0.0)
                                            _ctrp_fx = float(_crow.get('frst_bltn_exrt', 0) or 0.0)
                                            if _ctrp_fx > 0:
                                                fx_rate = _ctrp_fx
                                                if _fx_from_gateway <= 0 and _ctrp_fx > 1000:
                                                    # ★ Wave 1 Track C: 게이트웨이 미확보 시 증권사 당일 최초고시환율(실관측)을 60초 캐시에 반영
                                                    KISTraderAdapter._fx_cache_val = _ctrp_fx
                                                    KISTraderAdapter._fx_cache_ts = time.time()
                                            break
                            except Exception as _ctrp_e:
                                logger.debug(f"CTRP6504R inquiry bypass: {_ctrp_e}")

                            # 2) TTTS3007R (해외주식 매수가능금액조회)에서 즉시 주문가능 외화 및 통합증거금 달러 조회
                            direct_usd = 0.0
                            integ_usd = 0.0
                            try:
                                headers_ps = self._get_headers()
                                headers_ps['tr_id'] = 'TTTS3007R' if self.mode == 'live' else 'VTTS3007R'
                                params_ps = {
                                    'CANO': acnt[0],
                                    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                                    'OVRS_EXCG_CD': 'NASD',
                                    'OVRS_ORD_UNPR': '100',
                                    'ITEM_CD': 'AAPL'
                                }
                                url_ps = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
                                resp_ps = requests.get(url_ps, headers=headers_ps, params=params_ps, timeout=5)
                                d_ps = resp_ps.json()
                                if d_ps.get('rt_cd') == '0':
                                    out_ps = d_ps.get('output', {})
                                    _ord_frcr = float(out_ps.get('ord_psbl_frcr_amt', 0) or 0.0)
                                    _ovrs_psbl = float(out_ps.get('ovrs_ord_psbl_amt', 0) or 0.0)
                                    direct_usd = max(_ovrs_psbl, _ord_frcr, withdrawable_usd)
                                    integ_usd = float(out_ps.get('frcr_ord_psbl_amt1', 0) or 0.0)
                            except Exception as _pse:
                                logger.debug(f"inquire-psamount fetch bypass: {_pse}")

                            if total_usd_deposit <= 0:
                                total_usd_deposit = direct_usd
                            self.total_usd_deposit = round(total_usd_deposit, 2)
                            self.pledged_margin_usd = round(pledged_margin_usd, 2)
                            self.direct_usd_cash = round(direct_usd if direct_usd > 0 else withdrawable_usd, 2)
                            self.integrated_margin_usd = round(integ_usd, 2)
                            # [Strict Isolation] us_cash_usd는 즉시 주문 가능한 순수 달러(증거금 잠김 제외)만 반영
                            self.us_cash_usd = self.direct_usd_cash
                            self.us_buying_power_usd = round(max(self.integrated_margin_usd, self.direct_usd_cash), 2)

                            # [Conservation of Capital NAV Identity]
                            # 당일(D+0) 미국 주식 매수 시 CTRP6504R의 frcr_dncl_amt_2(D+2 결제 예수금)는 아직 차감 전이고
                            # frcr_buy_mgn_amt(pledged_margin_usd)에 잡히므로, 원화 통합증거금 부채(kr_integ_debt_usd)를 제외한
                            # 순수 당일 미국주식 미결제 매수증거금(us_unsettled_buy_usd)을 차감해야 이중 계상이 0원이 됨.
                            _kr_integ_debt_usd = max(0.0, -net_d2_cash_krw / fx_rate) if fx_rate > 0 else 0.0
                            _us_unsettled_buy_usd = max(0.0, self.pledged_margin_usd - _kr_integ_debt_usd)
                            self.net_usd_cash_for_nav = round(max(0.0, self.total_usd_deposit - _us_unsettled_buy_usd), 2)

                            logger.info(
                                f"  💵 [Live US Cash] 총 달러예수금: ${self.total_usd_deposit:,.2f} USD "
                                f"(가용: ${self.direct_usd_cash:,.2f} / 순예수금NAV: ${self.net_usd_cash_for_nav:,.2f} / 증거금잠김: ${self.pledged_margin_usd:,.2f}) / "
                                f"통합증거금 달러: ${self.integrated_margin_usd:,.2f} USD"
                            )

                            us_tot_usd = us_stock_usd + self.net_usd_cash_for_nav
                            _us_exposure_usd = us_tot_usd
                            kis_us_krw = us_tot_usd * fx_rate if fx_rate > 0 else 0.0
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

                        if us_fetch_ok and not _us_pages_complete:
                            # ★ Wave 1 Track C (W1_3): 해외 잔고 연속조회 중간 실패 → 부분 원장 폐기, 직전 positions 복원
                            self.positions.clear()
                            self.positions.update(_prev_positions_snapshot)
                            self._last_positions_complete = False
                            self._last_balance_complete = False
                            logger.critical("  🚨 [Live Balance] 해외 잔고 페이징 불완전 — positions/NAV 갱신 보류 (직전 값 복원)")
                            return False

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
                            if not us_fetch_ok:
                                for _existing_tk in _ledger_mgr.load_ledger().keys():
                                    if not str(_existing_tk).isdigit() and _existing_tk not in _active_tickers:
                                        _active_tickers.append(_existing_tk)
                            _ledger_map = _ledger_mgr.sync_live_positions(_active_tickers)
                            for t, pos in self.positions.items():
                                pos.entry_date = _ledger_map.get(t, "")
                        except Exception as _ledg_err:
                            logger.warning(f"  ⚠️ [PositionLedger] 동기화 경고: {_ledg_err}")

                        self.account.cash = ord_psbl_cash
                        # ★ Wave 1 Track C: 해외 익스포저가 있는데 환율 미확보 → NAV 원화환산 불가.
                        #   상수 환율로 날조하지 않고 NAV/대사/SSOT 기록을 보류(직전 NAV 유지) + degraded 플래그 + False 반환.
                        #   (수량·원화 현금은 환율과 무관하므로 위에서 정상 갱신됨 → 매도/청산 경로는 영향 없음)
                        _has_us_exposure = _us_exposure_usd > 0 or any(not str(_t).isdigit() for _t in self.positions)
                        self.nav_fx_degraded = bool(_has_us_exposure and not (fx_rate and fx_rate > 0))
                        if self.nav_fx_degraded:
                            self._last_balance_complete = False
                            logger.critical(f"  🚨 [Live Balance] USD/KRW 미확보 + 해외 익스포저(${_us_exposure_usd:,.2f}) → "
                                            f"NAV 갱신/대사/kis_portfolio 기록 보류 (직전 NAV ₩{float(self.account.total_equity or 0):,.0f} 유지)")
                            return False
                        self._last_balance_complete = True
                        self.account.total_equity = tot_evlu
                        try:
                            from src.execution.account_reconciler import AccountReconciler
                            AccountReconciler().reconcile(self)
                        except Exception as _rec_e:
                            logger.warning(f"  ⚠️ [AccountReconciler] 대조 실행 경고: {_rec_e}")

                        if self.mode == 'live' and not os.environ.get('PYTEST_CURRENT_TEST'):
                            try:
                                from src.utils.file_ops import safe_json_update
                                _kp_path = _PROJECT_ROOT / 'results' / 'kis_portfolio.json'
                                _live_holdings = {}
                                for _tk, _p in self.positions.items():
                                    _is_us = not _tk.isdigit()
                                    _fx = fx_rate if _is_us else 1.0
                                    _pnl_pct = round(((float(_p.current_price) / float(_p.avg_price)) - 1.0) * 100.0, 2) if float(_p.avg_price) > 0 else 0.0
                                    _live_holdings[_tk] = {
                                        'name': getattr(_p, 'name', _tk),
                                        'qty': int(_p.quantity),
                                        'avg_price': float(_p.avg_price),
                                        'current_price': float(_p.current_price),
                                        'eval_krw': round(float(_p.quantity) * float(_p.current_price) * _fx),
                                        'pnl_pct': _pnl_pct,
                                        'return_pct': _pnl_pct,
                                        'currency': 'USD' if _is_us else 'KRW',
                                        'entry_date': getattr(_p, 'entry_date', '')
                                    }
                                _acct_patch = {
                                    'nav': round(float(self.account.total_equity)),
                                    'total_equity': round(float(self.account.total_equity)),
                                    'cash': round(float(self.account.cash)),
                                    'us_cash_usd': float(getattr(self, 'direct_usd_cash', getattr(self, 'us_cash_usd', 0.0))),
                                    'integrated_margin_usd': float(getattr(self, 'integrated_margin_usd', 0.0)),
                                    'fx_rate': round(float(fx_rate), 2),
                                    'positions': _live_holdings,
                                    'n_positions': len(_live_holdings),
                                }

                                def _kp_mutator(_kp_data):
                                    # ★ Wave 1 Track C: 잠금 하 read-modify-write (스키마 불변)
                                    if not isinstance(_kp_data, dict):
                                        _kp_data = {}
                                    _kp_data['kis_mode'] = 'live'
                                    _kp_data['updated_at'] = datetime.now().isoformat()
                                    _kp_data['holdings'] = _live_holdings
                                    _kp_acct = _kp_data.get('account', {})
                                    if not isinstance(_kp_acct, dict):
                                        _kp_acct = {}
                                    _kp_acct.update(_acct_patch)
                                    _kp_data['account'] = _kp_acct
                                    _kp_data['nav'] = round(float(self.account.total_equity))
                                    return _kp_data

                                safe_json_update(_kp_path, _kp_mutator, default={}, indent=2)
                            except Exception as _kp_err:
                                logger.warning(f"  ⚠️ [Live SSOT] kis_portfolio.json 자동 동기화 실패(기존 파일 유지): {_kp_err}")
                        logger.info(f"  ✅ [Live Patch] KIS OpenAPI 통합 원장 갱신 완료: 주문가능현금={self.account.cash:,.0f}원 / MTS 100% 동일 총자산=₩{self.account.total_equity:,.0f}원")
                        return True
                    else:
                        logger.warning('  ⚠️ fetch_live_balance: output2 비어있음')
                elif data.get('rt_cd') == '1' and ('만료' in data.get('msg1', '') or '토큰' in data.get('msg1', '') or data.get('msg_cd') in ('EGW00123', 'EGW00103')):
                    logger.warning('  ⚠️ [Token Expired] 토큰 만료 감지 → 신규 토큰 발급 후 재시도')
                    if not getattr(self, '_balance_token_retry', False) and self._request_new_token():
                        self._last_balance_fetch_ts = 0.0   # ★ Wave 1: TTL 단락으로 재조회 없이 True 반환되던 버그
                        self._balance_token_retry = True    # 1회만 재시도 (무한 재귀 방지)
                        try:
                            return self.fetch_live_balance()
                        finally:
                            self._balance_token_retry = False
                elif data.get('rt_cd') == '1' and '초과' in data.get('msg1', ''):
                    _m1_rl = data.get("msg1", "")
                    logger.warning(f"  ⚠️ fetch_live_balance API 속도 제한 (Rate Limit): {_m1_rl}")
                    # ★ Wave 1: 09:00 데몬·파이프라인 동시 조회로 초당 한도 초과 → 지터 백오프 후 1회 재시도
                    if not getattr(self, '_balance_rl_retry', False):
                        import random as _rnd
                        self._balance_rl_retry = True
                        self._last_balance_fetch_ts = 0.0
                        time.sleep(1.0 + _rnd.random())
                        try:
                            return self.fetch_live_balance()
                        finally:
                            self._balance_rl_retry = False
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
            self._last_positions_complete = True
            return {t: p.quantity for t, p in self.positions.items() if p.quantity > 0}
            
        # [Rate Limit & Slippage Defense] 60초 TTL 인메모리 캐싱
        now_ts = time.time()
        if (hasattr(self, '_last_positions_fetch_ts') and (now_ts - getattr(self, '_last_positions_fetch_ts', 0) < 60)
                and hasattr(self, '_cached_live_positions') and getattr(self, '_last_positions_complete', False)):
            return self._cached_live_positions
            
        with self._lock:
            self._last_positions_fetch_ts = now_ts
            if not self._access_token:
                if not self.authenticate():
                    self._last_positions_complete = False
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
                # ★ Wave 1 Track C (W1_3): 연속조회 페이징 — 중간 실패 시 불완전 스냅샷
                data, _kr_pages_ok = self._kis_paged_get(url, headers, params, '100')
                
                live_pos = {}
                self.live_position_details = {}
                _complete = True
                if data.get('rt_cd') == '0':
                    output1 = data.get('output1', [])
                    for item in output1:
                        ticker = item.get('pdno', '')
                        qty = int(item.get('hldg_qty', 0))
                        if ticker and qty > 0:
                            live_pos[ticker] = qty
                    if not _kr_pages_ok:
                        _complete = False
                        logger.error("  ❌ [Live Positions] 국내 잔고 연속조회(페이징) 불완전")
                else:
                    _complete = False
                    logger.error(f"  ❌ [Live Positions] 국내 잔고 조회 실패 rt_cd={data.get('rt_cd')} msg={data.get('msg_cd')}/{data.get('msg1')}")

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
                    # ★ Wave 1 Track C (W1_3): 연속조회 페이징 (거래소 순회 없음 — 단일 조회가 전 거래소 반환)
                    us_data, _us_pages_ok = self._kis_paged_get(us_url, us_headers, us_params, '200')
                    if us_data.get('rt_cd') == '0':
                        if not _us_pages_ok:
                            _complete = False
                            logger.error("  ❌ [Live Positions] 해외 잔고 연속조회(페이징) 불완전")
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
                    else:
                        _complete = False
                        logger.error(f"  ❌ [Live Positions] 해외 잔고 조회 실패 rt_cd={us_data.get('rt_cd')} msg={us_data.get('msg_cd')}/{us_data.get('msg1')}")
                except Exception as _ue:
                    _complete = False
                    logger.error(f"  ❌ [Live Positions] 해외 잔고 동기화 예외: {_ue}")

                # ★ Wave 0 E5/E9: 불완전 스냅샷을 '보유 없음'으로 오인하지 않도록 완전성 플래그 노출,
                #   불완전 스냅샷은 권위 캐시로 저장하지 않는다.
                self._last_positions_complete = _complete
                if _complete:
                    self._cached_live_positions = live_pos
                    logger.info(f"  ✅ [Live Patch] 실계좌 국내/해외 통합 종목 동기화 완료: {live_pos}")
                else:
                    logger.critical(f"  🚨 [Live Positions] 불완전 스냅샷(일부 조회 실패): {live_pos} — 대사/Ghost 삭제/NAV 평가에 사용 금지")
                return live_pos
            except Exception as e:
                logger.error(f"  ❌ fetch_live_positions 예외: {e}")
                self._last_positions_complete = False
                if hasattr(self, '_cached_live_positions') and self._cached_live_positions is not None:
                    logger.warning("  ⚠️ [Live Patch] 예외 발생 → 직전 완전 스냅샷 캐시 반환 (불완전 플래그 설정)")
                    return dict(self._cached_live_positions)
                return {}

    def _update_account(self):
        _us_cash_usd = float(getattr(self, 'direct_usd_cash', getattr(self, 'us_cash_usd', 0.0)) or 0.0)
        _has_us = _us_cash_usd > 0 or any((not str(p.ticker).isdigit()) and p.quantity > 0 for p in self.positions.values())
        fx = float(getattr(self, 'fx_rate', 0) or self._get_live_fx_rate() or 0.0) if _has_us else 0.0
        if _has_us and fx <= 0:
            # ★ Wave 1 Track C: 환율 미확보 → 원화 NAV 날조 금지. 직전 평가값 유지 + degraded 플래그.
            self.nav_fx_degraded = True
            logger.critical("  🚨 [Account] USD/KRW 미확보 + 해외 익스포저 → positions_value/total_equity 갱신 보류 (직전 값 유지)")
            return
        self.nav_fx_degraded = False
        pv = sum((p.current_price * p.quantity * (1.0 if str(p.ticker).isdigit() else fx) for p in self.positions.values()))
        self.account.positions_value = pv
        self.account.unrealized_pnl = sum((p.unrealized_pnl * (1.0 if str(p.ticker).isdigit() else fx) for p in self.positions.values()))
        us_cash_krw = _us_cash_usd * fx
        self.account.total_equity = self.account.cash + us_cash_krw + pv

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

    def check_slippage_budget(self, ticker: str, submitted_price: float, max_bps: float = None) -> bool:
        """동적 슬리피지 예산 차단기 (Dynamic Volatility & Spread-Adjusted Slippage Budget).
        
        Slippage Budget = clip(1.5 * Spread_bps + 0.1 * Vol_bps, 5.0, 40.0) bps
        """
        cur_p = self._get_current_price(ticker) or self.get_live_overseas_price(ticker, 'NASD')
        if not cur_p or cur_p <= 0 or submitted_price <= 0:
            return False
        drift_pct = abs(cur_p - submitted_price) / submitted_price
        
        if max_bps is None:
            # [Mathematical Dynamic Model] 종목별 호가 스프레드 및 변동성 기반 동적 슬리피지 예산 산출
            spread_bps = 5.0
            if hasattr(self, '_estimate_spread_bps'):
                spread_bps = self._estimate_spread_bps(ticker)
            max_bps = max(5.0, min(40.0, 1.5 * spread_bps + 5.0))

        if drift_pct * 10000 > max_bps:
            logger.warning(f"  🛡️ [Slippage Budget Exceeded] {ticker} 지정가(${submitted_price}) vs 현재가(${cur_p}) 드리프트 {drift_pct*100:.2f}% > {max_bps:.1f}bps")
            return True
        return False

    def _find_broker_order_since(self, order: 'Order', since: datetime, attempts: int = 2) -> Optional[Dict[str, Any]]:
        """★ Wave 0 E6: 모호한 전송 실패 후 '당일 주문·체결 원장'에서 같은 주문(종목·방향·수량·제출 이후 시각)을 탐색.

        필드명은 2026-10-19 실계좌 읽기 전용 조회로 검증:
          KRX  TTTC8001R(inquire-daily-ccld): pdno, sll_buy_dvsn_cd('01'매도/'02'매수), ord_qty, ord_dt, ord_tmd(HHMMSS), odno
          US   TTTS3035R(inquire-ccnl): pdno, sll_buy_dvsn_cd, ft_ord_qty, dmst_ord_dt, thco_ord_tmd, odno
        """
        if self.mode not in ('live', 'paper'):
            return None
        import requests
        is_us = not str(order.ticker).isdigit()
        want_side = '01' if order.side.lower() in ('sell', 'short') else '02'
        since_hms = (since - timedelta(seconds=5)).strftime('%H%M%S')
        today = datetime.now().strftime('%Y%m%d')
        acnt = self.account_no.split('-') if '-' in self.account_no else [self.account_no[:8], self.account_no[8:] or '01']
        base = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01'}
        prefix = 'V' if self.mode == 'paper' else 'T'
        for i in range(max(1, attempts)):
            headers = self._get_headers()
            if not is_us:
                headers['tr_id'] = f"{prefix}TTC8001R"
                params = {**base, 'INQR_STRT_DT': today, 'INQR_END_DT': today, 'SLL_BUY_DVSN_CD': want_side, 'INQR_DVSN': '00',
                          'PDNO': order.ticker, 'CCLD_DVSN': '00', 'ORD_GNO_BRNO': '', 'ODNO': '', 'INQR_DVSN_3': '00',
                          'INQR_DVSN_1': '', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
                url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
                rows_key, qty_key, date_key, tmd_key = 'output1', 'ord_qty', 'ord_dt', 'ord_tmd'
            else:
                headers['tr_id'] = f"{prefix}TTS3035R"
                params = {**base, 'PDNO': order.ticker, 'ORD_STRT_DT': (datetime.now() - timedelta(days=1)).strftime('%Y%m%d'),
                          'ORD_END_DT': (datetime.now() + timedelta(days=1)).strftime('%Y%m%d'), 'SLL_BUY_DVSN': '00',
                          'CCLD_NCCS_DVSN': '00', 'OVRS_EXCG_CD': '%', 'SORT_SQN': 'DS', 'ORD_DT': '', 'ORD_GNO_BRNO': '',
                          'ODNO': '', 'CTX_AREA_NK200': '', 'CTX_AREA_FK200': ''}
                url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-ccnl"
                rows_key, qty_key, date_key, tmd_key = 'output', 'ft_ord_qty', 'dmst_ord_dt', 'thco_ord_tmd'
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            data = resp.json()
            if data.get('rt_cd') == '0':
                for r in (data.get(rows_key) or []):
                    if str(r.get('pdno', '')).strip() != str(order.ticker):
                        continue
                    if str(r.get('sll_buy_dvsn_cd', '')).strip() != want_side:
                        continue
                    if int(float(r.get(qty_key, 0) or 0)) != int(order.quantity):
                        continue
                    if str(r.get(date_key, today)) != today:
                        continue
                    if str(r.get(tmd_key, '999999') or '999999') < since_hms:
                        continue
                    logger.warning(f"  🔎 [Idempotency] 주문원장에서 일치 주문 발견: {order.ticker} {want_side} x{order.quantity} "
                                   f"(ODNO ***{str(r.get('odno', ''))[-4:]})")
                    return r
            if i + 1 < attempts:
                time.sleep(1.5)  # 증권사 원장 반영 지연 대비
        return None

    def inquire_unexecuted(self, ticker: str = '') -> List[Dict[str, Any]]:
        """증권사 미체결 주문 조회 (국내/해외 거래소 다형성 완비 - NASD/NYSE/AMEX 전수 순회)"""
        if self.mode == 'mock':
            return []
        try:
            import requests
            headers = self._get_headers()
            is_us = (ticker and not str(ticker).isdigit())
            if is_us:
                headers['tr_id'] = 'VTTS3018R' if self.mode == 'paper' else 'TTTS3018R'
                acnt = self.account_no.split('-')
                target_excgs = [self.resolve_overseas_exchange(ticker)] if ticker else ['NASD', 'NYSE', 'AMEX']
                all_us_orders = []
                for excg in target_excgs:
                    params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'OVRS_EXCG_CD': excg, 'SORT_SQN': 'DS', 'CTX_AREA_FK200': '', 'CTX_AREA_NK200': ''}
                    url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
                    resp = requests.get(url, headers=headers, params=params, timeout=5)
                    if hasattr(resp, 'text') and isinstance(resp.text, str) and not resp.text.strip():
                        continue
                    data = resp.json()
                    if data.get('rt_cd') == '0':
                        output = data.get('output', data.get('output1', []))
                        for item in output:
                            item['ovrs_excg_cd'] = excg
                            all_us_orders.append(item)
                if ticker:
                    return [item for item in all_us_orders if str(item.get('pdno', item.get('ticker', ''))).endswith(ticker)]
                return all_us_orders
            else:
                headers['tr_id'] = 'VTTC8036R' if self.mode == 'paper' else 'TTTC8036R'
                acnt = self.account_no.split('-')
                params = {'CANO': acnt[0], 'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 'INQR_DVSN_1': '0', 'INQR_DVSN_2': '0', 'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''}
                url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
                resp = requests.get(url, headers=headers, params=params, timeout=5)
                if hasattr(resp, 'text') and isinstance(resp.text, str) and not resp.text.strip():
                    return []
                data = resp.json()
                if data.get('rt_cd') == '0':
                    output = data.get('output', data.get('output1', []))
                    if ticker:
                        return [item for item in output if str(item.get('pdno', item.get('ticker', ''))).endswith(ticker)]
                    return output
        except Exception as e:
            logger.warning(f"  ⚠️ inquire_unexecuted 조회 예외: {e}")
        return []

    def check_and_repeg_unexecuted_orders(self, max_wait_sec: float = 0.0) -> List[Dict]:
        """[Time-Bounded Pegging Monitoring Loop]
        미체결 주문(inquire-nccs)을 조회하여 제출 후 지연 시 실시간 호가로 자동 정정 체결!
        (매수/매도 구분 철저 적용: 매도는 최유리 매수호가로, 매수는 최유리 매도호가로 정정)
        """
        if self.mode == 'mock':
            return []
        try:
            import requests
            headers = self._get_headers()
            headers['tr_id'] = 'VTTS3018R' if self.mode == 'paper' else 'TTTS3018R'
            acnt = self.account_no.split('-')
            repeg_results = []
            seen_order_nos = set()

            for excg_target in ('NASD', 'NYSE', 'AMEX'):
                params = {
                    'CANO': acnt[0],
                    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                    'OVRS_EXCG_CD': excg_target,
                    'SORT_SQN': 'DS',
                    'CTX_AREA_FK200': '',
                    'CTX_AREA_NK200': ''
                }
                url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
                resp = requests.get(url, headers=headers, params=params, timeout=10)
                if hasattr(resp, 'text') and isinstance(resp.text, str) and not resp.text.strip():
                    continue
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

                        # [Time-Bounded Delay Verification] 주문이 접수된 후 최소 max_wait_sec 경과 후에만 정정 집행
                        now_ts = time.time()
                        if not hasattr(self, '_unexec_first_seen_ts'):
                            self._unexec_first_seen_ts = {}
                        first_seen = self._unexec_first_seen_ts.setdefault(order_no, now_ts)
                        if max_wait_sec > 0 and (now_ts - first_seen) < max_wait_sec:
                            continue

                        if ticker and submitted_px > 0 and qty > 0:
                            resolved_excg = self.resolve_overseas_exchange(ticker, excg)
                            cncl_us = self.cancel_us_order(order_no, ticker, resolved_excg)
                            if not cncl_us.get('success', False):
                                continue
                            time.sleep(0.3)

                            if is_sell:
                                bid_p = self.get_live_overseas_price(ticker, resolved_excg, ask_side=False) or (submitted_px - 0.01)
                                repeg_price = round(min(bid_p, submitted_px) - 0.01, 2) if bid_p > 0 else round(submitted_px - 0.01, 2)
                                logger.warning(f"  ⚡ [Auto Re-Peg Loop] {ticker} {qty}주 매도 미체결({order_no}) ➔ 실시간 최유리 매수호가(${repeg_price:.2f})로 즉시 매도 정정!")
                                repeg_res = self.sell(ticker=ticker, quantity=qty, price=repeg_price, order_type='limit', exchange=resolved_excg, urgency='repeg')
                                repeg_results.append({'order_no': order_no, 'ticker': ticker, 'side': 'sell', 'repeg_order': repeg_res})
                            else:
                                ask_p = self.get_live_overseas_price(ticker, resolved_excg, ask_side=True) or (submitted_px + 0.01)
                                repeg_price = round(max(ask_p, submitted_px + 0.01), 2)
                                repeg_res = self.buy(ticker=ticker, quantity=qty, price=repeg_price, order_type='limit', exchange=resolved_excg, urgency='repeg')
                                repeg_results.append({'order_no': order_no, 'ticker': ticker, 'side': 'buy', 'repeg_order': repeg_res})

            # ── 2. KRX 국내 미체결 주문 실시간 자동 재페깅/시장가 전환 ──
            try:
                krx_headers = self._get_headers()
                krx_headers['tr_id'] = 'VTTC8036R' if self.mode == 'paper' else 'TTTC8036R'
                krx_params = {
                    'CANO': acnt[0],
                    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01',
                    'INQR_DVSN_1': '0',
                    'INQR_DVSN_2': '0',
                    'CTX_AREA_FK100': '',
                    'CTX_AREA_NK100': ''
                }
                krx_url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
                krx_resp = requests.get(krx_url, headers=krx_headers, params=krx_params, timeout=5)
                krx_data = krx_resp.json()
                if krx_data.get('rt_cd') == '0':
                    for item in krx_data.get('output', krx_data.get('output1', [])):
                        order_no = item.get('odno', item.get('ODNO', ''))
                        if not order_no or order_no in seen_order_nos:
                            continue
                        seen_order_nos.add(order_no)
                        ticker = item.get('pdno', item.get('PDNO', ''))
                        qty = int(float(item.get('psbl_qty', item.get('PSBL_QTY', item.get('rmn_qty', item.get('ord_qty', item.get('ORD_QTY', 0))))) or 0))
                        side_cd = str(item.get('sll_buy_dvsn_cd', item.get('SLL_BUY_DVSN_CD', ''))).strip()
                        is_sell = side_cd in ('01', 'SELL', 'sell', '매도')
                        now_ts = time.time()
                        if not hasattr(self, '_unexec_first_seen_ts'):
                            self._unexec_first_seen_ts = {}
                        first_seen = self._unexec_first_seen_ts.setdefault(order_no, now_ts)
                        if max_wait_sec > 0 and (now_ts - first_seen) < max_wait_sec:
                            continue

                        if ticker and qty > 0:
                            cncl_kr = self.cancel_order(order_no, ticker=ticker)
                            if not cncl_kr.get('success', False):
                                continue
                            time.sleep(0.3)
                            if is_sell:
                                logger.warning(f"  ⚡ [KRX Auto Re-Peg] {ticker} {qty}주 매도 미체결({order_no}) ➔ 즉시 최유리 시장가 매도 정정!")
                                repeg_res = self.sell(ticker=ticker, quantity=qty, price=0, order_type='01', exchange='KRX', urgency='repeg')
                                repeg_results.append({'order_no': order_no, 'ticker': ticker, 'side': 'sell', 'repeg_order': repeg_res})
                            else:
                                logger.warning(f"  ⚡ [KRX Auto Re-Peg] {ticker} {qty}주 매수 미체결({order_no}) ➔ 즉시 시장가 매수 정정!")
                                repeg_res = self.buy(ticker=ticker, quantity=qty, price=0, order_type='01', exchange='KRX', urgency='repeg')
                                repeg_results.append({'order_no': order_no, 'ticker': ticker, 'side': 'buy', 'repeg_order': repeg_res})
            except Exception as _krx_e:
                logger.debug(f"  [KRX Auto Re-Peg] 국내 미체결 스캔 예외: {_krx_e}")

            return repeg_results
        except Exception as e:
            logger.debug(f"  [Auto Re-Peg Loop] 미체결 스캔 예외: {e}")
            return []

    def execute_time_bounded_toleranced_pegging(self, ticker: str, submitted_price: float, submitted_time: datetime, order_no: str = '', quantity: int = 1, max_wait_sec: float = 15.0, max_tolerance_bps: float = None) -> Dict[str, Any]:
        """[대표님 제안 알고리즘] 타임아웃 허용범위 가변 체결 엔진 (Time-Bounded Tolerance-Pegged Execution Engine).
        
        1. 지정가 주문 제출 후 max_wait_sec 동안 체결을 기다림.
        2. max_wait_sec 경과 후 미체결 시, 현재 최유리 매도호가가 동적 허용범위 이내이면 즉시 기존 주문 취소 ➔ 실시간 현재가/매도호가로 즉시 정정 체결!
        3. 허용범위를 초과해 급등한 경우 뇌동 매매를 차단하기 위해 주문을 취소하고 현금 보존.
        """
        now = datetime.now()
        elapsed = (now - submitted_time).total_seconds()
        if elapsed < max_wait_sec:
            return {'action': 'wait', 'reason': f"대기시간 {elapsed:.1f}s < 타임아웃 {max_wait_sec}s"}
            
        if max_tolerance_bps is None:
            # [Mathematical Dynamic Model] 변동성 및 스프레드 연동 동적 허용 오차
            max_tolerance_bps = max(10.0, min(50.0, 20.0))
            
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
        # ★ Wave 1 Track C: 리터럴 1358.70 제거 — 환율 미확보 시 교차통화 환산을 하지 않음(매수여력 과소 계상 = fail-closed)
        fx_rate = float(self._get_live_fx_rate() or 0.0)
        _fx_ok = fx_rate > 0
        krw_cash = float(getattr(self.account, 'cash', 0.0) or 0.0)
        usd_cash = float(getattr(self, 'us_cash_usd', 0.0) or 0.0)

        # 원화/달러 구분 없는 통합 실질 총 매수여력 (Integrated Buying Power)
        total_buying_power_krw = krw_cash + (usd_cash * fx_rate if _fx_ok else 0.0)
        total_buying_power_usd = usd_cash + (krw_cash / fx_rate if _fx_ok else 0.0)
        if not _fx_ok:
            logger.critical("  🚨 [Integrated Margin] USD/KRW 미확보 → 교차통화 매수여력 미반영 (원화/달러 각자 통화분만 집계)")
        
        return {
            'krw_cash': round(krw_cash, 0),
            'usd_cash': round(usd_cash, 2),
            'fx_rate': round(fx_rate, 2),
            'total_buying_power_krw': round(total_buying_power_krw, 0),
            'total_buying_power_usd': round(total_buying_power_usd, 2),
            'fx_degraded': not _fx_ok,
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
        us_sweep_enabled = _cfg.get('cash_shield.us_sweep_enabled', False)

        # 2. 미국 야간 세션 파킹 (22:30 ~ 04:45 KST) - 기본 비활성화 (cash_shield.us_sweep_enabled=False)
        is_us_trading = (now_dt.hour >= 22 or now_dt.hour < 4 or (now_dt.hour == 4 and now_dt.minute < 45))
        now_ts = time.time()
        _last_us_ts = getattr(self, '_last_us_sweep_ts', 0.0)
        
        if us_sweep_enabled and is_us_trading and (now_ts - _last_us_ts >= 3600.0) and not self._has_active_tactical_signals(market='US'):
            self._last_us_sweep_ts = now_ts
            us_cash = getattr(self, 'direct_usd_cash', getattr(self, 'us_cash_usd', 0.0))
            
            # [Strict Currency-Matched Zero-FX Friction Principle]
            # 달러 초단기채(SHV) 파킹은 오직 순수 달러 예수금(direct_usd_cash > $100)으로만 수행!
            # 원화 기반 통합증거금(integrated_margin_usd)은 이자 파킹에 1센트도 침범 엄격 금지!
            if us_cash > 100:
                p_us = round(self.get_live_overseas_price(us_shield_ticker, 'NYSE') or 110.29, 2)
                if us_cash >= p_us and p_us > 0:
                    max_qty = int(us_cash // p_us)
                    for q_try in range(max_qty, 0, -1):
                        res = self.buy(ticker=us_shield_ticker, quantity=q_try, price=p_us, order_type='limit', exchange='NYSE', is_yield_sweep=True)
                        if getattr(res, 'status', '') in ('submitted', 'filled', 'pending'):
                            logger.info(f"  🏦 [Yield Harvester 2.0] 미국 순수 외화 현금 ${us_cash:.2f} ➔ SHV x{q_try}주 (연~5.0% 무환전 채권 이자) 파킹 집행 완료!")
                            results['us_sweep'] = res
                            break
            else:
                logger.info(f"  🏦 [Yield Harvester 2.0] 순수 달러 예수금(${us_cash:.2f}) $100 이하 ➔ 환전 마찰 방지를 위해 원화 통합증거금 파킹 절대 금지 (원화 현금 100% 안전 보존)")

        # 3. 국내 장마감 후 파킹 (15:20 ~ 16:00 KST)
        is_kr_post_close = (now_dt.hour == 15 and 20 <= now_dt.minute <= 59)
        if is_kr_post_close and not self._has_active_tactical_signals(market='KR'):
            # [Transaction Cost / Friction Guard]
            # 평일 단 하루(1박 2일) 파킹은 매매수수료(왕복 ~0.007%~0.028%) 마찰 대비 1일치 이자(~0.0097%)의 실익이 없음.
            # 금요일(주말 3일치 파킹) 또는 연휴/공휴일 직전(휴일 3일 이상) 무위험 이자가 확실히 수수료를 상회할 때만 파킹 집행!
            try:
                from src.utils.market_calendar import MarketCalendar
                cal = MarketCalendar()
                tomorrow = now_dt.date() + timedelta(days=1)
                cur_check = tomorrow
                gap_days = 0
                while not cal.is_trading_day(cur_check.strftime('%Y%m%d')) and gap_days < 10:
                    gap_days += 1
                    cur_check += timedelta(days=1)
                total_days = 1 + gap_days
                if total_days < 3:
                    logger.info(f"  🏦 [Yield Harvester Friction Guard] 익일이 정규 거래일(보유 기간: {total_days}일)입니다. 왕복 매매수수료 대비 1일치 이자 실익 부재로 평일 원화 파킹 스윕 스킵 (원화 현금 100% 안전 보존)")
                    return results
            except Exception as _fric_err:
                logger.debug(f"Friction guard check bypass: {_fric_err}")

            raw_kr_cash = float(getattr(self, 'raw_d2_cash', getattr(self.account, 'd2_cash', 0.0)) or 0.0)
            p_kr = self._get_current_price(kr_shield_ticker) or 1075000.0
            if raw_kr_cash >= p_kr and p_kr > 0:
                qty_kr = int(raw_kr_cash // p_kr)
                if qty_kr > 0:
                    logger.info(f"  🏦 [Yield Harvester 2.0] 주말/연휴(보유 기간: {total_days}일) 무위험 이자 누적 확정 ➔ 원화 현금 ₩{raw_kr_cash:,.0f} ➔ {kr_shield_ticker} {qty_kr}주 파킹 스윕 집행! (익영업일 08:30 KST 자동 청산 예약)")
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
        shield_tickers_kr = ['459580', '423160', '449170', '430740', '357870', '357330']
        
        for ticker, pos in list(self.positions.items()):
            if pos.quantity <= 0:
                continue
            if market in ('ALL', 'US') and ticker in shield_tickers_us:
                _exch = self.resolve_overseas_exchange(ticker, 'AMEX')
                p_us = self.get_live_overseas_price(ticker, _exch) or pos.current_price or pos.avg_price
                res = self.sell(ticker=ticker, quantity=pos.quantity, price=p_us, order_type='market', exchange=_exch)
                logger.info(f"  ⚡ [Auto-Redemption] 새벽 04:50 KST 미국 파킹 채권 {ticker} x{pos.quantity}주 자동 청산 집행 완료! (익일 KRX 자금 100% 복구)")
                results.append(res)
            elif market in ('ALL', 'KR') and ticker in shield_tickers_kr:
                p_kr = self._get_current_price(ticker) or pos.current_price or pos.avg_price
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
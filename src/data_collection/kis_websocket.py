"""
KIS WebSocket — 실시간 체결가/호가/체결통보 스트리밍
=====================================================

KIS OpenAPI WebSocket을 사용하여 실시간 데이터를 수신.
RealtimeDataBus와 통합하여 Circuit Breaker + Staleness 인프라 활용.

★ REST Polling(5초 간격) → WebSocket Push(수십ms)

채널:
  H0STCNT0: 실시간 체결가 (국내주식)
  H0STASP0: 실시간 호가 (국내주식)
  H0STCNR0: 체결 통보 (내 주문)

KIS WebSocket 프로토콜:
  1. wss://ops.koreainvestment.com:21000 (실전)
     wss://ops.koreainvestment.com:31000 (모의)
  2. 접속 후 approval_key로 인증
  3. JSON으로 종목 subscribe
  4. 데이터는 '|' 구분 텍스트 또는 JSON으로 수신

모든 파라미터 DynamicConfig 동적 로드.

Usage:
    from src.data_collection.kis_websocket import KISWebSocketClient
    ws = KISWebSocketClient()
    ws.subscribe_price(['005930', '000660'])
    ws.start()  # 백그라운드 수신 시작
"""
import pandas as pd
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    from config.dynamic_config import DynamicConfig
    from src.utils.time_utils import now_kst
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None

    def now_kst():
        from datetime import timezone, timedelta
        return datetime.now(tz=timezone(timedelta(hours=9)))

def _get(key: str, default):
    return _cfg.get(key, default) if _cfg else default
WS_URL = {'live': 'wss://ops.koreainvestment.com:21000', 'paper': 'wss://ops.koreainvestment.com:31000', 'mock': None}
TR_PRICE = 'H0STCNT0'
TR_ORDERBOOK = 'H0STASP0'
TR_NOTICE = 'H0STCNR0'

class KISWebSocketClient:
    """KIS WebSocket 실시간 스트리밍 클라이언트.

    설계:
      - 백그라운드 스레드에서 WebSocket 수신 루프
      - 콜백 기반 데이터 전달 (RealtimeDataBus 연동)
      - 자동 재연결 (지수 백오프)
      - 종목별 구독 관리 (최대 40개)
    """
    MAX_SUBSCRIPTIONS = 40

    def __init__(self, mode: str=None):
        """
        Args:
            mode: 'live', 'paper', 'mock' (None → DynamicConfig)
        """
        if mode is None:
            mode = _get('execution.current_mode', 'mock')
            if mode == 'shadow':
                mode = 'mock'
        self.mode = mode
        self._ws_url = WS_URL.get(mode)
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._approval_key: Optional[str] = None
        self._app_key = ''
        self._app_secret = ''
        self._load_credentials()
        self._subscribed_price: Set[str] = set()
        self._subscribed_orderbook: Set[str] = set()
        self._on_price: Optional[Callable] = None
        self._on_orderbook: Optional[Callable] = None
        self._on_notice: Optional[Callable] = None
        self._latest_prices: Dict[str, Dict] = {}
        self._latest_orderbooks: Dict[str, Dict] = {}
        self._latest_notices: List[Dict] = []
        self._tick_buffer: List[Dict] = []
        self._ob_buffer: List[Dict] = []
        self._flush_thread: Optional[threading.Thread] = None
        self._stats = {'messages_received': 0, 'price_updates': 0, 'orderbook_updates': 0, 'notices': 0, 'reconnects': 0, 'errors': 0, 'started_at': None}
        self._reconnect_base = _get('websocket.reconnect_base_sec', 1.0)
        self._reconnect_max = _get('websocket.reconnect_max_sec', 60.0)
        self._reconnect_attempt = 0

    def _load_credentials(self):
        """[Keychain] KIS API 키 로드."""
        from src.utils.credential_manager import CredentialManager
        cm = CredentialManager()
        prefix = 'KIS_PAPER' if self.mode == 'paper' else 'KIS'
        self._app_key = cm.read_from_keychain(f'{prefix}_APP_KEY') or ''
        self._app_secret = cm.read_from_keychain(f'{prefix}_APP_SECRET') or ''

    def _get_approval_key(self) -> Optional[str]:
        """WebSocket 접속용 Approval Key 발급.

        REST API access_token과 별도. WebSocket 전용 일회성 키.
        """
        if self.mode == 'mock':
            return 'mock_approval_key'
        if not self._app_key or not self._app_secret:
            logger.warning('  KIS APP_KEY/APP_SECRET 미설정 → WebSocket 불가')
            return None
        try:
            import requests
            base = 'https://openapi.koreainvestment.com:9443' if self.mode == 'live' else 'https://openapivts.koreainvestment.com:29443'
            url = f'{base}/oauth2/Approval'
            body = {'grant_type': 'client_credentials', 'appkey': self._app_key, 'secretkey': self._app_secret}
            resp = requests.post(url, json=body, timeout=10)
            data = resp.json()
            if 'approval_key' in data:
                self._approval_key = data['approval_key']
                logger.info('  ✅ WebSocket Approval Key 발급 완료')
                return self._approval_key
            else:
                logger.error(f'  ❌ Approval Key 발급 실패: {data}')
                return None
        except Exception as e:
            logger.error(f'  ❌ Approval Key 요청 오류: {e}', exc_info=True)
            return None

    def subscribe_price(self, tickers: List[str]):
        """실시간 체결가 구독 등록."""
        for ticker in tickers:
            if len(self._subscribed_price) >= self.MAX_SUBSCRIPTIONS:
                logger.warning(f'  WebSocket 구독 한도({self.MAX_SUBSCRIPTIONS}) 도달')
                break
            self._subscribed_price.add(ticker)
        if self._connected and self._ws:
            for ticker in tickers:
                self._send_subscribe(TR_PRICE, ticker)

    def subscribe_orderbook(self, tickers: List[str]):
        """실시간 호가 구독 등록."""
        for ticker in tickers:
            total = len(self._subscribed_price) + len(self._subscribed_orderbook)
            if total >= self.MAX_SUBSCRIPTIONS:
                logger.warning('  WebSocket 구독 한도 도달')
                break
            self._subscribed_orderbook.add(ticker)
        if self._connected and self._ws:
            for ticker in tickers:
                self._send_subscribe(TR_ORDERBOOK, ticker)

    def unsubscribe(self, ticker: str):
        """구독 해제."""
        self._subscribed_price.discard(ticker)
        self._subscribed_orderbook.discard(ticker)
        if self._connected and self._ws:
            self._send_unsubscribe(TR_PRICE, ticker)
            self._send_unsubscribe(TR_ORDERBOOK, ticker)

    def on_price(self, callback: Callable[[str, Dict], None]):
        """체결가 콜백: callback(ticker, {'price': float, 'volume': int, ...})"""
        self._on_price = callback

    def on_orderbook(self, callback: Callable[[str, Dict], None]):
        """호가 콜백: callback(ticker, {'bids': [...], 'asks': [...]})"""
        self._on_orderbook = callback

    def on_notice(self, callback: Callable[[Dict], None]):
        """체결통보 콜백: callback({'order_no': str, 'status': str, ...})"""
        self._on_notice = callback

    def start(self) -> bool:
        """WebSocket 수신 시작 (백그라운드 스레드)."""
        if self._running:
            logger.info('  WebSocket 이미 실행 중')
            return True
        if self.mode == 'mock':
            logger.info('  🔵 WebSocket Mock 모드 — 시뮬레이션 수신 시작')
            self._running = True
            self._thread = threading.Thread(target=self._mock_loop, daemon=True, name='kis-ws-mock')
            self._thread.start()
            self._stats['started_at'] = now_kst().isoformat()
            return True
        if not self._approval_key:
            self._approval_key = self._get_approval_key()
            if not self._approval_key:
                return False
        self._running = True
        self._thread = threading.Thread(target=self._ws_loop, daemon=True, name='kis-ws-live')
        self._thread.start()
        self._flush_thread = threading.Thread(target=self._flush_worker, daemon=True, name='kis-tick-harvester')
        self._flush_thread.start()
        self._stats['started_at'] = now_kst().isoformat()
        logger.info(f'  🟢 WebSocket 수신 및 Tick Harvester 시작 ({self.mode})')
        return True

    def stop(self):
        """WebSocket 수신 종료."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception as _e:
                logger.error(f'  WebSocket ws.close() 실패 (무시): {_e}', exc_info=True)
        self._connected = False
        logger.info('  🔴 WebSocket 수신 종료')

    @property
    def is_running(self) -> bool:
        return self._running and (self._thread is not None and self._thread.is_alive())

    @property
    def stats(self) -> Dict:
        return {**self._stats, 'is_running': self.is_running, 'connected': self._connected, 'subscribed_price': len(self._subscribed_price), 'subscribed_orderbook': len(self._subscribed_orderbook)}

    def get_latest_price(self, ticker: str) -> Optional[Dict]:
        """최근 수신된 체결가."""
        return self._latest_prices.get(ticker)

    def get_latest_orderbook(self, ticker: str) -> Optional[Dict]:
        """최근 수신된 호가."""
        return self._latest_orderbooks.get(ticker)

    def get_all_latest_prices(self) -> Dict[str, Dict]:
        """전체 최근 체결가."""
        return dict(self._latest_prices)

    def _ws_loop(self):
        """WebSocket 연결 + 수신 루프 (자동 재연결)."""
        while self._running:
            try:
                import websocket
                self._ws = websocket.WebSocketApp(self._ws_url, on_open=self._on_ws_open, on_message=self._on_ws_message, on_error=self._on_ws_error, on_close=self._on_ws_close)
                import ssl
                _ssl_verify = _get('websocket.ssl_verify', True)
                sslopt = {'cert_reqs': ssl.CERT_REQUIRED if _ssl_verify else ssl.CERT_NONE}
                self._ws.run_forever(sslopt=sslopt, ping_interval=30, ping_timeout=10)
            except ImportError as e:
                logger.error('  ❌ websocket-client 미설치. pip install websocket-client 필요')
                self._running = False
                return
            except Exception as e:
                self._stats['errors'] += 1
                logger.warning(f'  WebSocket 연결 오류: {e}', exc_info=True)
            if self._running:
                self._reconnect_attempt += 1
                self._stats['reconnects'] += 1
                delay = min(self._reconnect_max, self._reconnect_base * 2 ** self._reconnect_attempt)
                logger.info(f'  🔄 WebSocket 재연결 대기: {delay:.1f}초 (시도 #{self._reconnect_attempt})')
                time.sleep(delay)

    def _on_ws_open(self, ws):
        """WebSocket 연결 성공 → 구독 요청."""
        self._connected = True
        self._reconnect_attempt = 0
        logger.info('  ✅ WebSocket 연결 완료')
        for ticker in self._subscribed_price:
            self._send_subscribe(TR_PRICE, ticker)
        for ticker in self._subscribed_orderbook:
            self._send_subscribe(TR_ORDERBOOK, ticker)
        self._send_subscribe(TR_NOTICE, 'HPC01')

    def _on_ws_message(self, ws, message: str):
        """WebSocket 메시지 수신 처리."""
        self._stats['messages_received'] += 1
        try:
            if message.startswith('{'):
                data = json.loads(message)
                header = data.get('header', {})
                tr_id = header.get('tr_id', '')
                if header.get('tr_type') == '3':
                    ws.send(message)
                    return
                body = data.get('body', {})
                rt_cd = body.get('rt_cd', '')
                if rt_cd == '0':
                    logger.debug(f'  WS 구독 확인: {tr_id}')
                elif rt_cd:
                    logger.warning(f'  WS 구독 오류: {body.get('msg1', '')}')
            else:
                parts = message.split('|')
                if len(parts) < 4:
                    return
                tr_id = parts[1]
                data_str = parts[3]
                if tr_id == TR_PRICE:
                    self._handle_price_data(data_str)
                elif tr_id == TR_ORDERBOOK:
                    self._handle_orderbook_data(data_str)
                elif tr_id == TR_NOTICE:
                    self._handle_notice_data(data_str)
        except Exception as e:
            self._stats['errors'] += 1
            logger.error(f'  WS 메시지 처리 오류: {e}', exc_info=True)

    def _on_ws_error(self, ws, error):
        """WebSocket 에러."""
        self._stats['errors'] += 1
        logger.warning(f'  WebSocket 에러: {error}')

    def _on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket 연결 종료."""
        self._connected = False
        logger.info(f'  WebSocket 연결 종료 (code={close_status_code})')

    def _send_subscribe(self, tr_id: str, tr_key: str):
        """종목 구독 요청."""
        if not self._ws or not self._connected:
            return
        msg = json.dumps({'header': {'approval_key': self._approval_key, 'custtype': 'P', 'tr_type': '1', 'content-type': 'utf-8'}, 'body': {'input': {'tr_id': tr_id, 'tr_key': tr_key}}})
        try:
            self._ws.send(msg)
            logger.debug(f'  WS 구독 요청: {tr_id} / {tr_key}')
        except Exception as e:
            logger.warning(f'  WS 구독 전송 실패: {e}', exc_info=True)

    def _send_unsubscribe(self, tr_id: str, tr_key: str):
        """종목 구독 해제."""
        if not self._ws or not self._connected:
            return
        msg = json.dumps({'header': {'approval_key': self._approval_key, 'custtype': 'P', 'tr_type': '2', 'content-type': 'utf-8'}, 'body': {'input': {'tr_id': tr_id, 'tr_key': tr_key}}})
        try:
            self._ws.send(msg)
        except Exception as _e:
            logger.warning(f'  [WebSocket] 구독 메시지 전송 실패: {_e} → 세션 초기화', exc_info=True)
            self._connected = False
            self._stats['errors'] += 1

    def _handle_price_data(self, data_str: str):
        """실시간 체결가 파싱 (H0STCNT0).

        KIS 체결가 필드 ('^' 구분, 총 46개):
          [0]  종목코드    [1]  체결시간     [2]  현재가
          [3]  전일대비부호  [4]  전일대비    [5]  전일대비율
          [6]  가중평균가   [7]  시가        [8]  고가
          [9]  저가        [10] 매도호가1    [11] 매수호가1
          [12] 체결량      [13] 누적거래량   [14] 누적거래대금
          [15] 매도체결건수  [16] 매수체결건수 [17] 순매수체결건수
          [18] 체결강도     [19] 총매도수량   [20] 총매수수량
        """
        self._stats['price_updates'] += 1
        try:
            fields = data_str.split('^')
            if len(fields) < 21:
                return
            ticker = fields[0]
            now = now_kst()
            price_data = {'ticker': ticker, 'price': float(fields[2]), 'change_sign': fields[3], 'change': float(fields[4]), 'change_pct': float(fields[5]), 'vwap': float(fields[6]), 'open': float(fields[7]), 'high': float(fields[8]), 'low': float(fields[9]), 'ask1': float(fields[10]), 'bid1': float(fields[11]), 'volume': int(fields[12]), 'cum_volume': int(fields[13]), 'cum_amount': float(fields[14]), 'sell_count': int(fields[15]), 'buy_count': int(fields[16]), 'net_buy_count': int(fields[17]), 'strength': float(fields[18]), 'total_sell_qty': int(fields[19]), 'total_buy_qty': int(fields[20]), 'timestamp': now.isoformat(), 'ws_received_at': now.isoformat(), 'source': 'websocket'}
            self._latest_prices[ticker] = price_data
            self._tick_buffer.append(price_data)
            if self._on_price:
                self._on_price(ticker, price_data)
        except (ValueError, IndexError) as e:
            logger.warning(f'  체결가 파싱 실패: {e}', exc_info=True)

    def _handle_orderbook_data(self, data_str: str):
        """실시간 호가 파싱 (H0STASP0).

        KIS 호가 필드 ('^' 구분):
          [0]  종목코드
          [3]  매도호가1  [4]  매도호가2  ... [12] 매도호가10
          [13] 매수호가1  [14] 매수호가2  ... [22] 매수호가10
          [23] 매도잔량1  [24] 매도잔량2  ... [32] 매도잔량10
          [33] 매수잔량1  [34] 매수잔량2  ... [42] 매수잔량10
          [43] 총매도잔량  [44] 총매수잔량
        """
        self._stats['orderbook_updates'] += 1
        try:
            fields = data_str.split('^')
            if len(fields) < 45:
                return
            ticker = fields[0]
            now = now_kst()
            asks = []
            bids = []
            for i in range(10):
                asks.append({'price': float(fields[3 + i]), 'volume': int(fields[23 + i])})
                bids.append({'price': float(fields[13 + i]), 'volume': int(fields[33 + i])})
            total_ask = int(fields[43]) if len(fields) > 43 else 0
            total_bid = int(fields[44]) if len(fields) > 44 else 0
            orderbook_data = {'ticker': ticker, 'asks': asks, 'bids': bids, 'total_ask_volume': total_ask, 'total_bid_volume': total_bid, 'imbalance': round((total_bid - total_ask) / max(total_bid + total_ask, 1), 4), 'spread_pct': round((asks[0]['price'] - bids[0]['price']) / max(bids[0]['price'], 1) * 100, 4) if asks and bids and (bids[0]['price'] > 0) else 0, 'timestamp': now.isoformat(), 'source': 'websocket'}
            self._latest_orderbooks[ticker] = orderbook_data
            flat_ob = {'ticker': ticker, 'total_ask_volume': total_ask, 'total_bid_volume': total_bid, 'imbalance': orderbook_data['imbalance'], 'spread_pct': orderbook_data['spread_pct'], 'timestamp': now.isoformat()}
            for i in range(3):
                if i < len(asks):
                    flat_ob[f'ask{i + 1}_price'] = asks[i]['price']
                    flat_ob[f'ask{i + 1}_vol'] = asks[i]['volume']
                if i < len(bids):
                    flat_ob[f'bid{i + 1}_price'] = bids[i]['price']
                    flat_ob[f'bid{i + 1}_vol'] = bids[i]['volume']
            self._ob_buffer.append(flat_ob)
            if self._on_orderbook:
                self._on_orderbook(ticker, orderbook_data)
        except (ValueError, IndexError) as e:
            logger.warning(f'  호가 파싱 실패: {e}', exc_info=True)

    def _handle_notice_data(self, data_str: str):
        """체결 통보 파싱 (H0STCNR0).

        내 주문의 체결 상태를 실시간으로 수신.
        """
        self._stats['notices'] += 1
        try:
            fields = data_str.split('^')
            if len(fields) < 10:
                return
            notice = {'order_no': fields[1] if len(fields) > 1 else '', 'ticker': fields[2] if len(fields) > 2 else '', 'side': fields[4] if len(fields) > 4 else '', 'price': float(fields[5]) if len(fields) > 5 else 0, 'quantity': int(fields[6]) if len(fields) > 6 else 0, 'status': fields[8] if len(fields) > 8 else '', 'timestamp': now_kst().isoformat(), 'source': 'websocket'}
            self._latest_notices.append(notice)
            self._latest_notices = self._latest_notices[-100:]
            if self._on_notice:
                self._on_notice(notice)
            logger.info(f'  📋 체결통보: {notice['ticker']} {notice['side']} x{notice['quantity']} @ {notice['price']:,.0f}')
        except (ValueError, IndexError) as e:
            logger.warning(f'  체결통보 파싱 실패: {e}', exc_info=True)

    def _mock_loop(self):
        """Mock 모드 시뮬레이션 — 가격 데이터 생성."""
        import random
        logger.info('  🔵 WebSocket Mock 시뮬레이션 시작')
        self._connected = True
        base_prices = {}
        for ticker in self._subscribed_price:
            base_prices[ticker] = self._load_base_price(ticker)
        interval = _get('websocket.mock_interval_sec', 1.0)
        while self._running:
            for ticker in list(self._subscribed_price):
                base = base_prices.get(ticker, 50000)
                change_pct = random.gauss(0, 0.002)
                new_price = base * (1 + change_pct)
                base_prices[ticker] = new_price
                price_data = {'ticker': ticker, 'price': round(new_price), 'change_sign': '2' if change_pct >= 0 else '5', 'change': round(new_price * change_pct), 'change_pct': round(change_pct * 100, 2), 'vwap': round(new_price), 'open': round(base * 0.999), 'high': round(base * 1.005), 'low': round(base * 0.995), 'ask1': round(new_price * 1.001), 'bid1': round(new_price * 0.999), 'volume': random.randint(100, 5000), 'cum_volume': random.randint(100000, 5000000), 'cum_amount': 0, 'sell_count': 0, 'buy_count': 0, 'net_buy_count': 0, 'strength': round(random.uniform(90, 110), 1), 'total_sell_qty': 0, 'total_buy_qty': 0, 'timestamp': now_kst().isoformat(), 'ws_received_at': now_kst().isoformat(), 'source': 'mock_websocket'}
                self._latest_prices[ticker] = price_data
                self._stats['price_updates'] += 1
                self._stats['messages_received'] += 1
                if self._on_price:
                    self._on_price(ticker, price_data)
            time.sleep(interval)
        self._connected = False

    def _load_base_price(self, ticker: str) -> float:
        """parquet에서 최근 종가 로드."""
        try:
            import pandas as pd
            path = _PROJECT_ROOT / 'data' / 'historical_10y' / f'kr_{ticker}.parquet'
            if path.exists():
                df = pd.read_parquet(path)
                return float(df['close'].iloc[-1])
        except Exception as _e:
            logger.error(f'  WebSocket fallback 가격 조회 실패: {_e}', exc_info=True)
        return 50000.0

    def integrate_with_data_bus(self):
        """RealtimeDataBus와 자동 연동.

        WebSocket에서 수신한 데이터를 RealtimeDataBus의
        StalenessAwareCache에 자동 저장.
        """
        try:
            from src.data_collection.realtime_data_bus import RealtimeDataBus, DataPoint
        except ImportError as e:
            logger.error('  RealtimeDataBus 임포트 실패', exc_info=True)
            return
        bus = RealtimeDataBus.get_instance()

        def _on_price_to_bus(ticker: str, data: Dict):
            """체결가 → RealtimeDataBus cache."""
            cache = bus._caches.get('current_price')
            if cache:
                cache.set(ticker, {'price': data['price'], 'fetched_method': 'websocket'}, source='websocket')

        def _on_orderbook_to_bus(ticker: str, data: Dict):
            """호가 → RealtimeDataBus cache."""
            cache = bus._caches.get('orderbook')
            if cache:
                cache.set(ticker, {'bid_total': data['total_bid_volume'], 'ask_total': data['total_ask_volume'], 'imbalance': data['imbalance'], 'fetched_method': 'websocket'}, source='websocket')
        self.on_price(_on_price_to_bus)
        self.on_orderbook(_on_orderbook_to_bus)
        logger.info('  🔗 WebSocket → RealtimeDataBus 연동 완료')
_ws_instance: Optional[KISWebSocketClient] = None
_ws_lock = threading.Lock()

def get_websocket_client() -> KISWebSocketClient:
    """싱글톤 WebSocket 클라이언트."""
    global _ws_instance
    if _ws_instance is None:
        with _ws_lock:
            if _ws_instance is None:
                _ws_instance = KISWebSocketClient()
    return _ws_instance

def start_realtime_streaming(tickers: List[str]=None) -> KISWebSocketClient:
    """실시간 스트리밍 시작 편의 함수.

    Usage:
        ws = start_realtime_streaming(['005930', '000660'])
        # 자동으로 RealtimeDataBus 연동
    """
    ws = get_websocket_client()
    if tickers:
        ws.subscribe_price(tickers)
    ws.integrate_with_data_bus()
    ws.start()
    return ws
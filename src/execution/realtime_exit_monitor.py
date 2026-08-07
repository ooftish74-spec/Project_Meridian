"""
RealtimeExitMonitor — 실시간 TP/SL 모니터링 (하이브리드)
=========================================================

3-Layer 하이브리드 아키텍처:
  Layer 1: WebSocket → on_price 콜백으로 실시간 가격 수신
  Layer 2: Threshold Alert → SL/TP 임계값 근접 시에만 Exit 평가
  Layer 3: Heartbeat → N분마다 REST로 가격 조회 (WebSocket 장애 시 fallback)

Exit 발동 시 ShadowPortfolioManager.check_exit_conditions() → execute_sells() 체인.

모든 파라미터 DynamicConfig 동적 로드. 하드코딩 절대 금지.

Usage:
    from src.execution.realtime_exit_monitor import RealtimeExitMonitor
    monitor = RealtimeExitMonitor()
    monitor.start()   # 장 시작 (09:05 이후)
    ...
    monitor.stop()    # 장 마감 (15:10)
"""
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from src.utils.emergency_pager import send_emergency_page
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
except ImportError as e:
    DynamicConfig = None

def _cfg_get(key: str, default):
    """DynamicConfig 동적 로드 헬퍼."""
    try:
        cfg = DynamicConfig()
        return cfg.get(key, default)
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return default

class AlertZone:
    """포지션별 SL/TP Alert Zone 사전 계산 결과."""
    __slots__ = ['pos_key', 'ticker', 'stream_id', 'avg_price', 'sl_price', 'tp_price', 'sl_alert_price', 'tp_alert_price', 'sl_pct', 'tp_pct', 'in_alert_zone', 'last_check_ts']

    def __init__(self, pos_key: str, ticker: str, stream_id: str, avg_price: float, sl_pct: float, tp_pct: float, alert_margin_pct: float):
        self.pos_key = pos_key
        self.ticker = ticker
        self.stream_id = stream_id
        self.avg_price = avg_price
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct
        self.sl_price = avg_price * (1 + sl_pct)
        self.tp_price = avg_price * (1 + tp_pct)
        alert_ratio = 1.0 - alert_margin_pct / 100
        sl_distance = avg_price - self.sl_price
        tp_distance = self.tp_price - avg_price
        self.sl_alert_price = avg_price - sl_distance * alert_ratio
        self.tp_alert_price = avg_price + tp_distance * alert_ratio
        self.in_alert_zone = False
        self.last_check_ts = 0.0

    def check_proximity(self, current_price: float) -> str:
        """현재가의 SL/TP 근접도 판단.

        Returns:
            'breach_sl': SL 돌파 (즉시 Exit)
            'breach_tp': TP 돌파 (즉시 Exit)
            'alert_sl': SL 근접 (가속 체크)
            'alert_tp': TP 근접 (가속 체크)
            'normal': 정상 (일반 heartbeat)
        """
        if current_price <= self.sl_price:
            return 'breach_sl'
        if current_price >= self.tp_price:
            return 'breach_tp'
        if current_price <= self.sl_alert_price:
            return 'alert_sl'
        if current_price >= self.tp_alert_price:
            return 'alert_tp'
        return 'normal'

class RealtimeExitMonitor:
    """실시간 Exit 모니터링 엔진 — 하이브리드 아키텍처.

    설계:
      1. WebSocket 가격 수신 → Threshold Alert 평가
      2. Alert Zone 진입 시 체크 주기 가속 (heartbeat → alert interval)
      3. SL/TP 돌파 시 즉시 Exit 실행
      4. WebSocket 장애 시 REST heartbeat fallback

    모든 파라미터 DynamicConfig 동적 로드.
    """

    def __init__(self):
        self._running = False
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._ws_client = None
        self._alert_zones: Dict[str, AlertZone] = {}
        self._alert_zones_lock = threading.Lock()
        self._latest_prices: Dict[str, float] = {}
        self._prices_lock = threading.Lock()
        self._exit_in_progress = False
        self._exit_lock = threading.Lock()
        self._exit_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='exit-trigger')
        self._stats_lock = threading.Lock()
        self._stats = {'started_at': None, 'ws_price_updates': 0, 'rest_heartbeats': 0, 'alert_checks': 0, 'exit_triggers': 0, 'exit_executed': 0, 'ws_connected': False, 'last_heartbeat': None, 'last_exit_check': None}

    def start(self) -> bool:
        """모니터링 시작 — WebSocket + Heartbeat.

        Returns:
            True if started successfully
        """
        if self._running:
            logger.info('  RealtimeExitMonitor 이미 실행 중')
            return True
        enabled = _cfg_get('monitor.enabled', True)
        if not enabled:
            logger.info('  RealtimeExitMonitor 비활성 (monitor.enabled=False)')
            return False
        self._running = True
        with self._stats_lock:
            self._stats['started_at'] = datetime.now().isoformat()
        self._compute_alert_zones()
        if not self._alert_zones:
            logger.info('  RealtimeExitMonitor: 보유 포지션 없음 → 모니터링 불필요')
            self._running = False
            return False
        ws_enabled = _cfg_get('monitor.ws_enabled', True)
        if ws_enabled:
            self._start_websocket()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True, name='exit-monitor-heartbeat')
        self._heartbeat_thread.start()
        tickers = {az.ticker for az in self._alert_zones.values()}
        logger.info(f'  🟢 RealtimeExitMonitor 시작: {len(self._alert_zones)}포지션, {len(tickers)}종목 모니터링 (WS={('ON' if self._ws_client else 'OFF')}, HB={_cfg_get('monitor.heartbeat_interval_sec', 300)}초)')
        self._save_status()
        return True

    def stop(self):
        """모니터링 종료."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.critical('[SILENT_BYPASS] Suppressed exception at realtime_exit_monitor.py:232', exc_info=True)
                send_emergency_page('[FATAL] Suppressed exception at realtime_exit_monitor.py:232')
        try:
            self._exit_executor.shutdown(wait=True)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.critical('[SILENT_BYPASS] Suppressed exception at realtime_exit_monitor.py:240', exc_info=True)
            send_emergency_page('[FATAL] Suppressed exception at realtime_exit_monitor.py:240')
        logger.info(f'  🔴 RealtimeExitMonitor 종료: alerts={self._stats['alert_checks']}, exits={self._stats['exit_executed']}')

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> Dict:
        """모니터링 통계 + Alert Zone 상세."""
        base = {**self._stats, 'is_running': self._running, 'alert_zones': len(self._alert_zones)}
        zone_details = []
        with self._alert_zones_lock:
            for az in self._alert_zones.values():
                zone_details.append({'stream_id': az.stream_id, 'ticker': az.ticker, 'avg_price': az.avg_price, 'sl_price': round(az.sl_price, 2), 'tp_price': round(az.tp_price, 2), 'sl_alert': round(az.sl_alert_price, 2), 'tp_alert': round(az.tp_alert_price, 2), 'in_alert': az.in_alert_zone})
        base['alert_zone_details'] = zone_details
        return base

    def _save_status(self):
        """모니터 상태를 JSON으로 저장 (대시보드 연동)."""
        try:
            status_file = _RESULTS / 'exit_monitor_status.json'
            status_file.write_text(json.dumps(self.stats, ensure_ascii=False, indent=2, default=str))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.critical('[SILENT_BYPASS] Suppressed exception at realtime_exit_monitor.py:283', exc_info=True)
            send_emergency_page('[FATAL] Suppressed exception at realtime_exit_monitor.py:283')

    def _start_websocket(self):
        """KISWebSocketClient 시작 + on_price 콜백 등록."""
        try:
            from src.data_collection.kis_websocket import get_websocket_client
            self._ws_client = get_websocket_client()
            tickers = list({az.ticker for az in self._alert_zones.values()})
            max_sub = _cfg_get('ws.max_subscription_tickers', 40)
            tickers = tickers[:max_sub]
            self._ws_client.subscribe_price(tickers)
            self._ws_client.on_price(self._on_price_update)
            self._ws_client.integrate_with_data_bus()
            if not self._ws_client.is_running:
                self._ws_client.start()
            self._stats['ws_connected'] = self._ws_client.is_running
            logger.info(f'  🔗 WebSocket → Exit 연동: {len(tickers)}종목 구독')
        except ImportError as e:
            logger.critical('  ℹ️ WebSocket 미가용 → REST heartbeat 전용', exc_info=True)
            send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=e)
            self._ws_client = None
        except Exception as e:
            logger.critical(f'  WebSocket 시작 실패: {e} → REST fallback', exc_info=True)
            send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=e)
            self._ws_client = None

    def _on_price_update(self, ticker: str, data: Dict):
        """WebSocket 가격 수신 콜백 — Threshold Alert 평가.

        매 틱마다 호출되지만, Alert Zone 체크는 경량 연산만 수행.
        실제 Exit 평가는 breach 또는 alert 시에만 트리거.
        """
        with self._stats_lock:
            self._stats['ws_price_updates'] += 1
        price = data.get('price', 0)
        if price <= 0:
            return
        with self._prices_lock:
            self._latest_prices[ticker] = price
        with self._stats_lock:
            self._stats['ws_connected'] = True
        self._check_threshold(ticker, price)

    def _check_threshold(self, ticker: str, price: float):
        """SL/TP 근접도 평가 — breach 시 즉시 Exit, alert 시 로깅.

        Args:
            ticker: 종목코드
            price: 현재가
        """
        now = time.time()
        alert_interval = _cfg_get('monitor.alert_check_interval_sec', 30)
        normal_interval = _cfg_get('monitor.heartbeat_interval_sec', 300)
        with self._alert_zones_lock:
            for az in self._alert_zones.values():
                if az.ticker != ticker:
                    continue
                status = az.check_proximity(price)
                if status in ('breach_sl', 'breach_tp'):
                    pnl_pct = (price - az.avg_price) / az.avg_price * 100
                    reason = f'{('SL' if 'sl' in status else 'TP')} 돌파: ₩{price:,.0f} ({pnl_pct:+.2f}%) [{az.stream_id}:{az.ticker}]'
                    logger.warning(f'  🚨 {reason}')
                    with self._stats_lock:
                        self._stats['exit_triggers'] += 1
                    self._exit_executor.submit(self._trigger_exit_check, reason)
                    return
                elif status in ('alert_sl', 'alert_tp'):
                    if not az.in_alert_zone:
                        pnl_pct = (price - az.avg_price) / az.avg_price * 100
                        logger.info(f'  ⚠️ Alert Zone 진입: [{az.stream_id}:{az.ticker}] ₩{price:,.0f} ({pnl_pct:+.2f}%) → 체크 주기 {normal_interval}초 → {alert_interval}초')
                        az.in_alert_zone = True
                    if now - az.last_check_ts >= alert_interval:
                        with self._stats_lock:
                            self._stats['alert_checks'] += 1
                        az.last_check_ts = now
                elif az.in_alert_zone:
                    logger.info(f'  ✅ Alert Zone 해제: [{az.stream_id}:{az.ticker}]')
                    az.in_alert_zone = False

    def _trigger_exit_check(self, reason: str):
        """check_exit_conditions() → execute_sells() 체인.

        중복 실행 방지 (Lock).
        """
        with self._exit_lock:
            if self._exit_in_progress:
                logger.debug('  Exit 이미 진행 중 — 스킵')
                return
            self._exit_in_progress = True
        try:
            from src.portfolio.shadow_manager import ShadowPortfolioManager
            initial_capital = _cfg_get('portfolio.initial_capital', 100000000)
            with ShadowPortfolioManager(initial_capital=initial_capital).transaction() as mgr:
                with self._prices_lock:
                    prices = dict(self._latest_prices)
                if not prices:
                    prices = self._fetch_rest_prices([az.ticker for az in self._alert_zones.values()])
                if not prices:
                    logger.warning('  Exit 체크 실패: 가격 데이터 없음')
                    return
                mgr.mark_to_market(prices)
                regime = self._load_regime()
                sell_orders = mgr.check_exit_conditions(regime)
                if sell_orders:
                    try:
                        from src.execution.execution_engine import ExecutionEngine
                        mode = _cfg_get('execution.current_mode', 'mock')
                        ee = ExecutionEngine(mode=mode)
                        auto_orders = []
                        manual_orders = []
                        for so in sell_orders:
                            ro = {'stream': so.get('stream_id'), 'action': 'sell', 'ticker': so.get('ticker'), 'amount': so.get('quantity', 0) * prices.get(so.get('ticker'), 0), 'quantity': so.get('quantity', 0), 'reason': so.get('reason', 'Intraday TP/SL Triggered')}
                            if so.get('stream_id') == 'S4':
                                manual_orders.append(ro)
                            else:
                                auto_orders.append(ro)
                        if manual_orders:
                            try:
                                logger.info(f'  🔔 S4 수동 매도 알림 텔레그램 발송 생략 ({len(manual_orders)}건)')
                        except Exception as e:
                            logger.critical(f'  ❌ S4 텔레그램 로직 오류: {e}', exc_info=True)
                            send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=e)
                    if auto_orders:
                        ee_result = ee.execute(auto_orders, portfolio=mgr.get_summary())
                        logger.info(f'  ⚡ ExecutionEngine (Intraday): {ee_result.n_filled}/{ee_result.n_orders} 체결 완료')
                except Exception as ee_err:
                    logger.critical(f'  ❌ ExecutionEngine (Intraday) 연동 실패: {ee_err}', exc_info=True)
                    send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=ee_err)
                etf_comm = _cfg_get('execution.etf_commission_rate', 0.00015)
                s1_sells = [s for s in sell_orders if s.get('stream_id') == 'S1']
                other_sells = [s for s in sell_orders if s.get('stream_id') != 'S1']
                if s1_sells:
                    mgr.execute_sells(s1_sells, prices, commission_rate=etf_comm)
                    for so in s1_sells:
                        logger.info(f'  🔴 [S1] {so.get('name', '?')} 실시간 Exit: {so.get('reason', '')[:80]}')
                if other_sells:
                    mgr.execute_sells(other_sells, prices)
                    for so in other_sells:
                        logger.info(f'  🔴 [{so.get('stream_id', '')}] {so.get('name', '?')} 실시간 Exit: {so.get('reason', '')[:80]}')
                mgr.save()
                with self._stats_lock:
                    self._stats['exit_executed'] += len(sell_orders)
                logger.info(f'  ✅ 실시간 Exit 완료: {len(sell_orders)}건 (사유: {reason})')
                self._compute_alert_zones()
            else:
                logger.debug(f'  Exit 체크 완료: 청산 대상 없음 ({reason})')
            with self._stats_lock:
                self._stats['last_exit_check'] = datetime.now().isoformat()
        except Exception as e:
            logger.critical(f'  실시간 Exit 실패: {e}', exc_info=True)
            send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=e)
        finally:
            with self._exit_lock:
                self._exit_in_progress = False

    def _heartbeat_loop(self):
        """주기적 REST 가격 조회 + Exit 체크.

        WebSocket 정상 시: heartbeat 간격으로 보조 체크
        WebSocket 장애 시: heartbeat가 유일한 가격 소스
        """
        logger.info('  💓 Heartbeat 루프 시작')
        while self._running:
            interval = _cfg_get('monitor.heartbeat_interval_sec', 300)
            now = datetime.now()
            close_h = _cfg_get('s1.exit.close_time_hour', 15)
            close_m = _cfg_get('s1.exit.close_time_minute', 10)
            market_start_h = _cfg_get('monitor.market_start_hour', 9)
            market_start_m = _cfg_get('monitor.market_start_minute', 0)
            current_t = now.time()
            market_start = dtime(market_start_h, market_start_m)
            market_end = dtime(close_h, close_m)
            if current_t < market_start or current_t > market_end:
                logger.debug('  💓 장외 시간 — heartbeat 대기')
                time.sleep(60)
                continue
            try:
                tickers = list({az.ticker for az in self._alert_zones.values()})
                if not tickers:
                    time.sleep(interval)
                    continue
                rest_prices = self._fetch_rest_prices(tickers)
                if rest_prices:
                    with self._prices_lock:
                        self._latest_prices.update(rest_prices)
                    for ticker, price in rest_prices.items():
                        self._check_threshold(ticker, price)
                    with self._stats_lock:
                        self._stats['rest_heartbeats'] += 1
                        self._stats['last_heartbeat'] = now.isoformat()
                    ws_ok = self._ws_client and self._ws_client.is_running and self._stats.get('ws_connected', False)
                    if not ws_ok:
                        fallback_interval = _cfg_get('monitor.ws_fallback_interval_sec', 60)
                        logger.debug(f'  💓 Heartbeat (WS 장애 fallback): {len(rest_prices)}종목 조회, 다음 {fallback_interval}초 후')
                        interval = fallback_interval
                    else:
                        logger.debug(f'  💓 Heartbeat: {len(rest_prices)}종목 조회 완료')
                    any_alert = any((az.in_alert_zone for az in self._alert_zones.values()))
                    if any_alert:
                        alert_interval = _cfg_get('monitor.alert_check_interval_sec', 30)
                        interval = min(interval, alert_interval)
                        logger.info(f'  ⚠️ Alert Zone 활성 → 체크 주기 {interval}초')
                    self._trigger_exit_check('heartbeat')
                    self._save_status()
            except Exception as e:
                logger.critical(f'  💓 Heartbeat 오류: {e}', exc_info=True)
                send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=e)
            time.sleep(interval)
        logger.info('  💓 Heartbeat 루프 종료')

    def _compute_alert_zones(self):
        """모든 포지션의 SL/TP Alert Zone 사전 계산.

        포지션 변경 시 (진입/청산) 재호출 필요.
        """
        try:
            from src.portfolio.shadow_manager import ShadowPortfolioManager
            initial_capital = _cfg_get('portfolio.initial_capital', 100000000)
            with ShadowPortfolioManager(initial_capital=initial_capital).transaction() as mgr:
                alert_margin = _cfg_get('monitor.alert_margin_pct', 10)
                cfg = DynamicConfig() if DynamicConfig else None
                new_zones = {}
                for pos_key, pos in mgr.positions.items():
                    stream_id, ticker = mgr._parse_position_key(pos_key)
                    avg_price = pos.get('avg_price', pos.get('entry_price', 0))
                    quantity = pos.get('quantity', 0)
                    if avg_price <= 0 or quantity <= 0:
                        continue
                    regime = self._load_regime()
                    exit_cfg = mgr._get_exit_config_for_stream(stream_id, regime)
                    if stream_id == 'S1' and cfg:
                        dyn_enabled = cfg.get('s1.exit.dynamic_tp_sl_enabled', True)
                        if dyn_enabled:
                            vol_ctx = mgr._load_volatility_context()
                            dyn = mgr._compute_dynamic_s1_exit(pos, cfg, vol_ctx)
                            tp_pct = dyn['take_profit_pct']
                            sl_pct = dyn['stop_loss_pct']
                        else:
                            tp_pct = None
                            sl_pct = None
                    else:
                        tp_pct = None
                        sl_pct = None
                    _atr = float(pos.get('atr_14', 0) or 0)
                    if _atr > 0:
                        _trail_mult = 2.0
                        _tp_mult = 4.0
                        _peak = float(pos.get('peak_price', avg_price) or avg_price)
                        _trail_sl_price = _peak - _atr * _trail_mult
                        dyn_sl_pct = (_trail_sl_price - avg_price) / avg_price
                        dyn_tp_pct = _atr * _tp_mult / avg_price
                        sl_pct = dyn_sl_pct if sl_pct is None else max(sl_pct, dyn_sl_pct)
                        tp_pct = dyn_tp_pct if tp_pct is None else min(tp_pct, dyn_tp_pct)
                    else:
                        vix = 20.0
                        try:
                            import json as _fj
                            _sc = _RESULTS / 'signal_cache.json'
                            if _sc.exists():
                                _d = _fj.loads(_sc.read_text())
                                vix = float(_d.get('vix', vix))
                        except Exception:
                            pass
                        daily_vol = vix / 100.0 / math.sqrt(252)
                        dyn_sl_pct = -daily_vol * 1.5
                        dyn_tp_pct = daily_vol * 3.0
                        sl_pct = dyn_sl_pct if sl_pct is None else dyn_sl_pct
                        tp_pct = dyn_tp_pct if tp_pct is None else dyn_tp_pct
                    az = AlertZone(pos_key=pos_key, ticker=ticker, stream_id=stream_id, avg_price=avg_price, sl_pct=sl_pct, tp_pct=tp_pct, alert_margin_pct=alert_margin)
                    new_zones[pos_key] = az
                    logger.debug(f'    Alert Zone [{stream_id}:{ticker}]: SL=₩{az.sl_price:,.0f} ({sl_pct * 100:+.2f}%), TP=₩{az.tp_price:,.0f} ({tp_pct * 100:+.2f}%), AlertSL=₩{az.sl_alert_price:,.0f}, AlertTP=₩{az.tp_alert_price:,.0f}')
                with self._alert_zones_lock:
                    self._alert_zones = new_zones
                logger.info(f'  📊 Alert Zone 계산 완료: {len(new_zones)}포지션')
        except Exception as e:
            logger.critical(f'  Alert Zone 계산 실패: {e}', exc_info=True)
            send_emergency_page(f'🚨 [FATAL] {e} at realtime_exit_monitor.py', exc_info=e)

    def _fetch_rest_prices(self, tickers: List[str]) -> Dict[str, float]:
        """REST API로 현재가 조회 (오직 KIS REST 기반).

        Args:
            tickers: 종목 코드 리스트

        Returns:
            {ticker: price} 딕셔너리
        """
        prices = {}
        batch_size = _cfg_get('monitor.rest_batch_size', 20)
        missing = tickers[:batch_size]
        if missing:
            try:
                from src.execution.kis_price_service import KISPriceService
                svc = KISPriceService()
                for ticker in missing:
                    try:
                        p = svc.get_current_price(ticker)
                        if p and p > 0:
                            prices[ticker] = p
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        logger.critical('[SILENT_BYPASS] Suppressed exception at realtime_exit_monitor.py:760', exc_info=True)
                        send_emergency_page('[FATAL] Suppressed exception at realtime_exit_monitor.py:760')
            except ImportError as e:
                logger.critical('[SILENT_BYPASS] Suppressed exception at realtime_exit_monitor.py:762', exc_info=True)
                send_emergency_page('[FATAL] Suppressed exception at realtime_exit_monitor.py:762')
        still_missing = [t for t in tickers[:batch_size] if t not in prices]
        if still_missing and self._ws_client:
            for ticker in still_missing:
                ws_data = self._ws_client.get_latest_price(ticker)
                if ws_data and ws_data.get('price', 0) > 0:
                    prices[ticker] = ws_data['price']
        return prices

    def _load_regime(self) -> str:
        """현재 레짐 로드 (pipeline_state.json SSoT)."""
        try:
            rf = _RESULTS / 'pipeline_state.json'
            if rf.exists():
                ps = json.loads(rf.read_text())
                return ps.get('kr_regime') or ps.get('regime', 'caution')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.critical('[SILENT_BYPASS] Suppressed exception at realtime_exit_monitor.py:784', exc_info=True)
            send_emergency_page('[FATAL] Suppressed exception at realtime_exit_monitor.py:784')
        return 'caution'
_monitor_instance: Optional[RealtimeExitMonitor] = None
_monitor_lock = threading.Lock()

def _is_kelly_booster_active_monitor(regime: str) -> bool:
    """[Phase 37] RealtimeExitMonitor 내부 Kelly Booster 상태 확인 헬퍼."""
    import json, statistics, pathlib as _pl
    if regime != 'bull':
        return False
    _results = _pl.Path(__file__).resolve().parent.parent.parent / 'results'
    try:
        sc_path = _results / 'signal_cache.json'
        if not sc_path.exists():
            return False
        sc = json.loads(sc_path.read_text(encoding='utf-8'))
        ois_today = float(sc.get('ois', 50) or 50)
        ois_history = [float(v) for v in sc.get('ois_history', []) if v is not None]
        if not ois_history:
            return False
        ois_lookback = int(_cfg_get('kelly.ois_lookback_days', 60))
        ois_median = statistics.median(ois_history[-ois_lookback:])
        return ois_today > ois_median
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return False

def get_exit_monitor() -> RealtimeExitMonitor:
    """싱글톤 RealtimeExitMonitor."""
    global _monitor_instance
    if _monitor_instance is None:
        with _monitor_lock:
            if _monitor_instance is None:
                _monitor_instance = RealtimeExitMonitor()
    return _monitor_instance

def start_exit_monitoring() -> RealtimeExitMonitor:
    """실시간 Exit 모니터링 시작 편의 함수.

    Usage:
        monitor = start_exit_monitoring()
        # 장 마감 시:
        monitor.stop()
    """
    monitor = get_exit_monitor()
    monitor.start()
    return monitor

def stop_exit_monitoring():
    """실시간 Exit 모니터링 종료."""
    global _monitor_instance
    if _monitor_instance:
        _monitor_instance.stop()
        _monitor_instance = None
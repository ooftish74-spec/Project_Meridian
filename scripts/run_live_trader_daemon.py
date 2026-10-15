"""
Project Meridian: Live Streaming Intraday Execution Daemon
==========================================================

KRX 개장(08:30~15:30 KST) 및 US 개장(22:00~05:00 KST) 시간 동안
매분 초단위 실시간 틱 수급 및 괴리율 Z-Score를 감시하며,
시그널 발화 즉시 KIS OpenAPI 실계좌 매수/매도 TR을 1초 만에 자동 직송하는 전용 데몬 스케줄러.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / '.env')

os.environ['ENVIRONMENT'] = 'production'
from config.dynamic_config import DynamicConfig
from src.utils.market_calendar import MarketCalendar

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('LiveTraderDaemon')

def main():
    logger.info("🚀 [LiveTraderDaemon] 프로젝트 메리디안 실계좌 자동매매 데몬 가동 시작...")
    
    # 1. execution.current_mode = live 강제 설정
    cfg = DynamicConfig()
    cfg.set('execution.current_mode', 'live')
    logger.info("  ✅ [Live Mode Lock] execution.current_mode = 'live' 실계좌 직송 모드 확정!")

    cal = MarketCalendar()
    loop_count = 0

    while True:
        try:
            loop_count += 1
            if loop_count % 100 == 0:
                import gc
                gc.collect()
                try:
                    import resource
                    mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
                    max_mem = cfg.get('system.memory_guard_limit_mb', 800)
                    if mem_mb > max_mem:
                        logger.warning(f"🧹 [5-Year Memory Guard] 메모리 사용량 {mem_mb:.1f}MB > {max_mem}MB 감지 — systemd 자가 리사이클링 집행")
                        sys.exit(0)
                except Exception as _mem_e:
                    logger.debug(f"  메모리 가드 검사 예외: {_mem_e}")

            from src.utils.time_utils import now_kst
            now = now_kst()
            results_dir = _PROJECT_ROOT / 'results'
            should_halt = False
            for flag_name in ['SYSTEM_HALT.flag', 'KILL_SWITCH.flag']:
                flag_path = results_dir / flag_name
                if flag_path.exists():
                    try:
                        if flag_name == 'KILL_SWITCH.flag' and (time.time() - flag_path.stat().st_mtime > 12 * 3600):
                            logger.info(f"🧹 [5-Year Auto-Recovery] 12시간 경과 유령 플래그 {flag_name} 자동 소멸 해제")
                            flag_path.unlink(missing_ok=True)
                        else:
                            logger.warning(f"⛔ [Halt Active] {flag_name} 플래그 활성화 중. 수동 해제 전까지 데몬 일시 정지 (30초 대기).")
                            should_halt = True
                    except Exception as _fe:
                        logger.debug(f"  플래그 삭제 중 예외: {_fe}")
            if should_halt:
                time.sleep(30)
                continue

            today_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M:%S')

            # 주말(토=5, 일=6) 및 KRX/US 영업일 판별
            is_weekend = (now.weekday() in (5, 6))
            is_kr_trading = cal.is_trading_day(today_str)
            is_kr_session = is_kr_trading and ((now.hour == 8 and now.minute >= 30) or (9 <= now.hour < 15) or (now.hour == 15 and now.minute <= 30))

            # Lever 3: US 장 확장 관제 (주중 월~금만 가동, 주말 제외: 프리마켓 18~22시, 정규장 22~05시)
            is_us_session = (not is_weekend) and (now.hour >= 18 or now.hour < 6)

            from scripts.daily_pipeline import run_pipeline
            from src.regime.online_bayesian_filter import OnlineBayesianParticleFilter
            from src.allocation.yield_arbitrage_engine import MultiAssetYieldArbitrageEngine
            from src.analysis.algorithmic_footprint_tracker import AlgorithmicFootprintTracker
            from src.risk.whipsaw_defense_engine import AntiWhipsawDefenseEngine
            from src.execution.connection_health_guard import ConnectionHealthGuard

            if not hasattr(main, '_bayesian_filter'):
                main._bayesian_filter = OnlineBayesianParticleFilter()
                _bayesian_state_file = str(results_dir / 'bayesian_filter_state.json')
                main._bayesian_filter.load_state(_bayesian_state_file)
                main._yield_arb_engine = MultiAssetYieldArbitrageEngine()
                main._footprint_tracker = AlgorithmicFootprintTracker()
                main._whipsaw_defense = AntiWhipsawDefenseEngine()
                main._health_guard = ConnectionHealthGuard(heartbeat_timeout_sec=2.0)

            # Record heartbeat and check connection mode
            main._health_guard.record_heartbeat()
            conn_mode = main._health_guard.get_connection_mode()

            if not hasattr(main, '_daily_executed'):
                main._daily_executed = set()

            # ── KRX Call Auction & Market Open Time Triggers (1회성 Latch 적용) ──
            if is_kr_trading:
                if now.hour == 8 and now.minute == 30 and now.second < 20:
                    if ('call_auction_0830', today_str) not in main._daily_executed:
                        main._daily_executed.add(('call_auction_0830', today_str))
                        logger.info(f"  🔔 [{time_str}] [08:30 CallAuction Trigger] KRX 장전 동시호가 매도 파이프라인 1회 자율 집행!")
                        run_pipeline(phase="call_auction_0830")
                elif now.hour == 8 and 50 <= now.minute <= 54:
                    if ('call_auction_monitor', today_str, now.minute) not in main._daily_executed:
                        main._daily_executed.add(('call_auction_monitor', today_str, now.minute))
                        logger.info(f"  🔍 [{time_str}] [08:50~08:54 Monitor Trigger] 동시호가 예상체결가 모니터링 (분당 1회)!")
                        run_pipeline(phase="call_auction_monitor")
                elif now.hour == 9 and now.minute == 0 and now.second < 20:
                    if ('market_open_0900', today_str) not in main._daily_executed:
                        main._daily_executed.add(('market_open_0900', today_str))
                        logger.info(f"  🚀 [{time_str}] [09:00 MarketOpen Trigger] 정규장 개장 직후 KODEX 레버리지 슬리피지 락 지정가 스왑 1회 집행!")
                        run_pipeline(phase="market_open_0900")

            if is_kr_session:
                logger.info(f"  ⚡ [{time_str}] KRX 실시간 장중 호가/수급 관제 중...")
                # ★ [Phase 98] 09:00 1회성 체결 실패 방어: 미체결 신호 존재 시 5분 쿨다운 재진입
                try:
                    _ls_file = results_dir / 'latest_signals.json'
                    _tl_file = results_dir / 'trade_ledger.json'
                    if _ls_file.exists():
                        with open(_ls_file, encoding='utf-8') as f:
                            _ls_data = json.load(f)
                        _sigs = _ls_data.get('signals', {})
                        if _sigs and (not _tl_file.exists() or (now - datetime.fromtimestamp(_tl_file.stat().st_mtime)).total_seconds() > 300):
                            logger.info("  🎯 [Intraday Auto-Retry] 미체결 신호 존재 및 5분 경과 감지 ➔ 장중 자동 재진입 파이프라인 가동!")
                            run_pipeline(phase="market")
                except Exception as _retry_e:
                    logger.debug(f"  장중 재진입 예외: {_retry_e}")

                # 실시간 signal_cache에서 텔레메트리 연동 (하드코딩 더미 제거)
                _telemetry = {'vkospi': 18.5, 'ofi_z': 0.0, 'lead_lag': 0.0}
                _sc_file = results_dir / 'signal_cache.json'
                if _sc_file.exists():
                    try:
                        _sc_data = json.loads(_sc_file.read_text(encoding='utf-8'))
                        _telemetry['vkospi'] = float(_sc_data.get('vkospi', 18.5))
                        _telemetry['ofi_z'] = float(_sc_data.get('ofi_z', 0.0))
                        _telemetry['lead_lag'] = float(_sc_data.get('lead_lag', 0.0))
                    except Exception:
                        pass

                # 1분 단위 Online Bayesian Particle Filter 실시간 미분 업데이트
                _bayesian_res = main._bayesian_filter.update(_telemetry)
                if loop_count % 12 == 0:
                    main._bayesian_filter.save_state(str(results_dir / 'bayesian_filter_state.json'))
                logger.debug(f"  🧪 [Online Bayesian Filter] Dominant Regime: {_bayesian_res.get('dominant_regime')}")
                
                # 횡보 폭풍장 휩소 방어 엔진(Anti-Whipsaw Defense) 스캔
                _whipsaw_res = main._whipsaw_defense.evaluate_whipsaw_defense([])
                logger.debug(f"  🛡️ [Anti-Whipsaw] Active: {_whipsaw_res.get('is_whipsaw_active')}, KER: {_whipsaw_res.get('ker_value'):.3f}")

                # 기관 알고리즘 발자국(Algorithmic Footprint) 스캐닝
                _footprint_res = main._footprint_tracker.compute_footprint_score({})
                logger.debug(f"  🐾 [Footprint Tracker] Score: {_footprint_res.get('footprint_score'):.2f}, Herding: {_footprint_res.get('is_herding_active')}")

                # 무위험 Yield Arbitrage 파킹 스캔
                _yield_info = main._yield_arb_engine.scan_yield_opportunities({})

                # 🚀 [Intraday Dynamic Entry Scanner] 장중 시장 전수 수급 펄스 라이브 스캔
                try:
                    if not hasattr(main, '_entry_monitor'):
                        from src.execution.realtime_entry_monitor import RealtimeEntryMonitor
                        main._entry_monitor = RealtimeEntryMonitor(mode='live')

                    held_set = set()
                    _kp_file = results_dir / 'kis_portfolio.json'
                    if _kp_file.exists():
                        try:
                            _kp_data = json.loads(_kp_file.read_text(encoding='utf-8'))
                            held_set = set(_kp_data.get('holdings', {}).keys())
                        except Exception:
                            pass

                    _entry_sigs = main._entry_monitor.scan_intraday_entries(held_tickers=held_set)
                    if _entry_sigs:
                        logger.info(f"  ⚡ [Intraday Dynamic Entry] {len(_entry_sigs)}개 장중 돌파 수급 신호 감지: {[s['ticker'] for s in _entry_sigs]}")
                        # [Signal Propagation] 검출된 장중 돌파 신호를 latest_signals.json에 원자적 병합
                        try:
                            from src.utils.file_ops import atomic_write_json
                            _ls_path = results_dir / 'latest_signals.json'
                            _existing_ls = {}
                            if _ls_path.exists():
                                with open(_ls_path, 'r', encoding='utf-8') as _lf:
                                    _existing_ls = json.load(_lf)
                            _existing_sigs = _existing_ls.get('signals', {})
                            for _es in _entry_sigs:
                                _tk = _es['ticker']
                                _existing_sigs[_tk] = {
                                    'ticker': _tk,
                                    'action': 'BUY',
                                    'direction': _es.get('direction', 'long'),
                                    'confidence': _es.get('confidence', 0.85),
                                    'strategy': 'intraday_dynamic_breakout',
                                    'score': _es.get('score', 1.8),
                                    'timestamp': datetime.now().isoformat()
                                }
                            _existing_ls['signals'] = _existing_sigs
                            _existing_ls['updated_at'] = datetime.now().isoformat()
                            atomic_write_json(_ls_path, _existing_ls)

                            # 신규 돌파 신호 감지 시 1회 집행 (최소 120초 쿨다운 적용)
                            if not hasattr(main, '_last_entry_pipeline_ts'):
                                main._last_entry_pipeline_ts = 0.0
                            if time.time() - main._last_entry_pipeline_ts >= 120.0:
                                main._last_entry_pipeline_ts = time.time()
                                logger.info("  🚀 [Intraday Breakout Execute] 신규 돌파 신호 포착 즉시 집행 파이프라인 가동!")
                                run_pipeline(phase='market')
                        except Exception as _prop_err:
                            logger.debug(f"  장중 돌파 신호 병합 예외: {_prop_err}")
                except Exception as _entry_err:
                    logger.debug(f"Intraday entry scanner skip: {_entry_err}")

                # 🛡️ [Intraday Universal Exit Scanner & Live Pricing] 장중 포지션 전수 실시간 시세 조회 및 3대 손절/트레일링 익절 상시 감시 (30초 주기)
                if not hasattr(main, '_kis_adapter'):
                    from src.execution._kis_adapter import KISTraderAdapter
                    main._kis_adapter = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
                _adapter = main._kis_adapter

                # 1. 주기적(60초) 잔고 갱신으로 실제 주문 가능 현금 및 보유 현황 최신화
                _now_ts = time.time()
                if not hasattr(main, '_last_kr_bal_ts'):
                    main._last_kr_bal_ts = 0.0
                if _now_ts - main._last_kr_bal_ts >= 60.0:
                    try:
                        _adapter.fetch_live_balance()
                        main._last_kr_bal_ts = _now_ts
                    except Exception as _bal_err:
                        logger.debug(f"  [KRX Balance Refresh] 예외: {_bal_err}")

                if not hasattr(main, '_last_exit_scan_ts'):
                    main._last_exit_scan_ts = 0.0
                if time.time() - main._last_exit_scan_ts >= 30.0:
                    main._last_exit_scan_ts = time.time()
                    try:
                        from src.risk.universal_exit_engine import UniversalExitEngine
                        from src.execution.position_ledger import PositionLedgerManager
                        from src.execution.execution_engine import ExecutionEngine
                        ledger_mgr = PositionLedgerManager()
                        _kp_file = results_dir / 'kis_portfolio.json'
                        if _kp_file.exists():
                            _kp_data = json.loads(_kp_file.read_text(encoding='utf-8'))
                            _exit_positions = {}
                            for _t, _h in _kp_data.get('holdings', {}).items():
                                _cp = float(_h.get('current_price', 0.0))
                                _avg = float(_h.get('avg_price', 0.0))
                                _ret = float(_h.get('return_pct', 0.0))

                                # ★ [CRITICAL FIX] 디스크 캐시의 정적 가격이 아닌 KIS 브로커 실시간 현재가 직송 조회
                                try:
                                    if _t.isdigit():
                                        _live_p = _adapter._get_current_price(_t)
                                    else:
                                        _live_p = _adapter.get_live_overseas_price(_t, 'NASD') or _adapter.get_live_overseas_price(_t, 'NYSE')
                                    if _live_p and float(_live_p) > 0:
                                        _cp = float(_live_p)
                                        if _avg > 0:
                                            _ret = (_cp - _avg) / _avg
                                except Exception as _p_err:
                                    logger.debug(f"  {_t} 실시간 시세 조회 폴백: {_p_err}")

                                _hwm = ledger_mgr.update_hwm(_t, current_price=_cp, current_pnl_pct=_ret)
                                _exit_positions[f"LIVE_SSOT:{_t}"] = {
                                    'ticker': _t,
                                    'stream_id': 'LIVE_SSOT',
                                    'quantity': _h.get('qty', 0),
                                    'entry_price': _avg,
                                    'current_price': _cp,
                                    'unrealized_pnl_pct': _ret,
                                    'peak_pnl_pct': float(_hwm.get('peak_pnl_pct', _ret) or _ret),
                                    'peak_price': float(_hwm.get('peak_price', _cp) or _cp),
                                    'trail_activated': bool(_hwm.get('trail_activated', False)),
                                    'holding_minutes': 999.0,
                                    'market': _h.get('market', 'KR' if _t.isdigit() else 'US')
                                }
                            _exit_res = UniversalExitEngine().evaluate_all_positions(_exit_positions, regime='caution')
                            _raw_exit_orders = _exit_res.get('exit_orders', [])
                            _exit_orders = []
                            if not hasattr(main, '_exit_orders_in_flight'):
                                main._exit_orders_in_flight = {}

                            # Clean up completed or timed-out orders (> 300 sec)
                            _now_ts = time.time()
                            main._exit_orders_in_flight = {
                                _k: _ts for _k, _ts in main._exit_orders_in_flight.items()
                                if (_now_ts - _ts) < 300.0 and _k in _kp_data.get('holdings', {})
                            }

                            for _eo in _raw_exit_orders:
                                _tk = _eo.get('ticker')
                                if _tk in main._exit_orders_in_flight:
                                    logger.info(f"  ⏳ [Exit In-Flight Cooldown] {_tk} 매도 주문 이미 접수 진행 중 ({_now_ts - main._exit_orders_in_flight[_tk]:.0f}s 전) ➔ 중복 발주 차단")
                                    continue
                                _exit_orders.append(_eo)
                                main._exit_orders_in_flight[_tk] = _now_ts

                            if _exit_orders:
                                logger.warning(f"  🚨 [Intraday Exit Triggered] {len(_exit_orders)}건 손절/청산 신호 발화 ➔ 즉시 실계좌 시장가 매도 집행!")
                                _exec = ExecutionEngine(mode='live')
                                _exec.execute(_exit_orders)
                    except Exception as _exit_err:
                        logger.error(f"  ❌ [Intraday Exit Scanner] 예외 발생: {_exit_err}", exc_info=True)

                # 2. [KRX Auto Re-Peg] 국내 미체결 주문 포착 시 즉시 최유리 시장가 매도 정정 감시 (5초 쿨다운)
                if not hasattr(main, '_last_kr_repeg_time'):
                    main._last_kr_repeg_time = 0.0
                if time.time() - main._last_kr_repeg_time >= 5.0:
                    try:
                        _adapter.check_and_repeg_unexecuted_orders(max_wait_sec=10.0)
                        main._last_kr_repeg_time = time.time()
                    except Exception as _kr_rp_err:
                        logger.debug(f"  [KRX RePeg] 미체결 스캔 예외: {_kr_rp_err}")

                time.sleep(5)
            elif is_us_session:
                # ── 동적 서머타임(DST) 개장 시각 자동 산출 (EDT: 22:30 / EST: 23:30 KST) ──
                from src.utils.market_calendar import get_us_market_open_time
                us_open_h, us_open_m = get_us_market_open_time()
                reload_minute = us_open_m - 1 if us_open_m > 0 else 59
                reload_hour = us_open_h if us_open_m > 0 else us_open_h - 1

                # ── 1. [개장 10초 전] Token & Margin Hot-Reload Lock (당일 1회만 발급 보장) ──
                if now.hour == reload_hour and now.minute == reload_minute and now.second >= 50:
                    if ('us_token_reload', today_str) not in main._daily_executed:
                        main._daily_executed.add(('us_token_reload', today_str))
                        _token_success = False
                        for _retry in range(3):
                            try:
                                from src.execution._kis_adapter import KISTraderAdapter
                                _hot_adapter = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
                                _hot_adapter.authenticate()
                                logger.info(f"  🔒 [{time_str}] [Hot-Reload Lock] 개장 10초 전 KIS OAuth2 토큰 & 통합증거금 사전 갱신 성공! (시도 {_retry+1}/3)")
                                _token_success = True
                                break
                            except Exception as _hte:
                                logger.warning(f"  ⚠️ [Hot-Reload Lock] 토큰 핫리로드 실패 (시도 {_retry+1}/3): {_hte}")
                                time.sleep(1.0)
                        if not _token_success:
                            logger.error("🚨 [Hot-Reload Lock] KIS OpenAPI 토큰 핫리로드 3회 실패 — 개장 직송 격리 대기")

                is_us_premarket = (18 <= now.hour < us_open_h) or (now.hour == us_open_h and now.minute < us_open_m)
                if is_us_premarket:
                    logger.debug(f"  🇪🇺 [{time_str}] US 프리마켓 텔레메트리 관제 대기 중...")
                    time.sleep(15)
                else:
                    # ── 2. [개장 초 5분] 0.5초 Turbo Speed Mode (DST: 22:30~22:35 / Non-DST: 23:30~23:35) ──
                    is_turbo_mode = (now.hour == us_open_h and us_open_m <= now.minute <= us_open_m + 5)
                    sleep_interval = 0.5 if is_turbo_mode else 3.0
                    
                    if is_turbo_mode and loop_count % 20 == 0:
                        logger.info(f"  🚀 [{time_str}] [Turbo Mode] US 본장 개장 초 5분 초고속 호가 감시 가동 중 ({us_open_h}:{us_open_m:02d} KST)")

                    # 🚀 [Lightweight Realtime Telemetry, Trailing Stop & Re-Peg]
                    try:
                        if not hasattr(main, '_kis_adapter'):
                            from src.execution._kis_adapter import KISTraderAdapter
                            main._kis_adapter = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
                        _adapter = main._kis_adapter

                        # 1. 주기적(60초) 잔고 갱신으로 실제 주문 가능 달러 현금 추적
                        _now_ts = time.time()
                        if not hasattr(main, '_last_bal_ts'):
                            main._last_bal_ts = 0.0
                        if _now_ts - main._last_bal_ts >= 60.0:
                            _adapter.fetch_live_balance()
                            main._last_bal_ts = _now_ts

                        # 2. 실시간 미국 트레일링 스탑 감시 및 즉시 청산 집행 (10초 주기)
                        if not hasattr(main, '_last_us_exit_check'):
                            main._last_us_exit_check = 0.0
                        if _now_ts - main._last_us_exit_check >= 10.0:
                            main._last_us_exit_check = _now_ts
                            try:
                                from src.execution.us_night_engine import USNightExecutionEngine
                                us_engine = USNightExecutionEngine()
                                if us_engine.holdings:
                                    curr_prices = {}
                                    for h in us_engine.holdings:
                                        tk = h['ticker']
                                        p = _adapter.get_live_overseas_price(tk, 'NASD') or _adapter.get_live_overseas_price(tk, 'NYSE') or float(h.get('current_price', 0.0))
                                        if p > 0:
                                            curr_prices[tk] = p
                                    exit_orders = us_engine.update_trailing_stop(curr_prices)
                                    for eo in exit_orders:
                                        logger.info(f"  🚨 [US Trailing Stop] {eo['ticker']} 손절/익절 청산 주문 송출: {eo['quantity']}주 @ ${eo['price']:.2f}")
                                        _adapter.sell(
                                            ticker=eo['ticker'],
                                            quantity=eo['quantity'],
                                            price=eo['price'],
                                            order_type='limit',
                                            notes=eo.get('reason', 'US Night Trailing Stop')
                                        )
                            except Exception as _ex_err:
                                logger.debug(f"  US Trailing Stop 평가 예외: {_ex_err}")

                        # 3. [Time-Bounded Re-Peg Loop] 미체결 주문 포착 시 실시간 매도호가 정정 감시 (3.0초 쿨다운 적용)
                        if not hasattr(main, '_last_repeg_time'):
                            main._last_repeg_time = 0.0
                        if time.time() - main._last_repeg_time >= 3.0:
                            _adapter.check_and_repeg_unexecuted_orders()
                            main._last_repeg_time = time.time()
                    except Exception as _swee:
                        logger.debug(f"  [Auto-Sweep/RePeg] 이벤트 스윕 및 정정 예외: {_swee}")

                    time.sleep(sleep_interval)
            else:
                logger.debug(f"  💤 [{time_str}] 현재 장외/주말 휴장 시간 (관망 대기 중...)")
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("🛑 [LiveTraderDaemon] 사용자에 의해 데몬이 종료되었습니다.")
            break
        except Exception as e:
            logger.error(f"🚨 [LiveTraderDaemon] 데몬 루프 중 예외 발생: {e}", exc_info=True)
            time.sleep(10)

if __name__ == '__main__':
    main()

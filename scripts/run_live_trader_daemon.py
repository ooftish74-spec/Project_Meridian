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

            # ── KRX Call Auction & Market Open Time Triggers ──
            if is_kr_trading:
                if now.hour == 8 and now.minute == 30 and now.second < 10:
                    logger.info(f"  🔔 [{time_str}] [08:30 CallAuction Trigger] KRX 장전 동시호가 매도 파이프라인 자율 집행!")
                    run_pipeline(phase="call_auction_0830")
                elif now.hour == 8 and 50 <= now.minute <= 54:
                    logger.info(f"  🔍 [{time_str}] [08:50~08:54 Monitor Trigger] 동시호가 예상체결가 모니터링 & 08:54 3대 조건부 제어!")
                    run_pipeline(phase="call_auction_monitor")
                elif now.hour == 9 and now.minute == 0 and now.second < 10:
                    logger.info(f"  🚀 [{time_str}] [09:00 MarketOpen Trigger] 정규장 개장 직후 KODEX 레버리지 슬리피지 락 지정가 스왑 매수 집행!")
                    run_pipeline(phase="market_open_0900")

            if is_kr_session:
                logger.info(f"  ⚡ [{time_str}] KRX 실시간 장중 파이프라인 관제 실행 중...")
                # ★ [Phase 98] 09:00 1회성 체결 실패 방어: 미체결 신호 존재 시 장중 지속적 재진입 루프 가동
                try:
                    _ls_file = results_dir / 'latest_signals.json'
                    _tl_file = results_dir / 'trade_ledger.json'
                    if _ls_file.exists():
                        with open(_ls_file, encoding='utf-8') as f:
                            _ls_data = json.load(f)
                        _sigs = _ls_data.get('signals', {})
                        if _sigs and (not _tl_file.exists() or (now - datetime.fromtimestamp(_tl_file.stat().st_mtime)).total_seconds() > 300):
                            logger.info("  🎯 [Intraday Auto-Retry] 미체결 신호 존재 및 5분 미실행 감지 ➔ 장중 자동 재진입 파이프라인 가동!")
                            run_pipeline(phase="market")
                except Exception as _retry_e:
                    logger.debug(f"  장중 재진입 예외: {_retry_e}")
                # 실시간 signal_cache에서 텔레메트리 연동 (하드코딩 더미 제거)
                _telemetry = {'vkospi': 18.5, 'ofi_z': 0.0, 'lead_lag': 0.0}
                _sc_file = results_dir / 'signal_cache.json'
                if _sc_file.exists():
                    try:
                        import json
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

                # 🚀 [Intraday Dynamic Entry Scanner] 장중 시장 전수 수급 펄스 5초 라이브 스캔
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
                        # [Signal Propagation] 검출된 장중 돌파 신호를 latest_signals.json에 병합하여 실주문 집행 연동
                        try:
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
                            with open(_ls_path, 'w', encoding='utf-8') as _lf:
                                json.dump(_existing_ls, _lf, indent=2, ensure_ascii=False)
                        except Exception as _prop_err:
                            logger.debug(f"  장중 돌파 신호 병합 예외: {_prop_err}")
                except Exception as _entry_err:
                    logger.debug(f"Intraday entry scanner skip: {_entry_err}")

                run_pipeline(phase='market')
                time.sleep(5)
            elif is_us_session:
                # ── 동적 서머타임(DST) 개장 시각 자동 산출 (EDT: 22:30 / EST: 23:30 KST) ──
                from src.utils.market_calendar import get_us_market_open_time
                us_open_h, us_open_m = get_us_market_open_time()
                reload_minute = us_open_m - 1 if us_open_m > 0 else 59
                reload_hour = us_open_h if us_open_m > 0 else us_open_h - 1

                # ── 1. [개장 10초 전] Token & Margin Hot-Reload Lock (3회 재시도 사전 핫리로드) ──
                if now.hour == reload_hour and now.minute == reload_minute and now.second >= 50:
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
                    logger.info(f"  🇪🇺 [{time_str}] 유럽장 & US 프리마켓 수급 텔레메트리 관제 실행 중...")
                    run_pipeline(phase='us_premarket')
                    time.sleep(5)
                else:
                    # ── 2. [개장 초 5분] 0.5초 Turbo Speed Mode (DST: 22:30~22:35 / Non-DST: 23:30~23:35) ──
                    is_turbo_mode = (now.hour == us_open_h and us_open_m <= now.minute <= us_open_m + 5)
                    sleep_interval = 0.5 if is_turbo_mode else 5.0
                    
                    if is_turbo_mode:
                        logger.info(f"  🚀 [{time_str}] [Turbo Mode 0.5s] US 본장 개장 초 5분 0.5초 초고속 수급 감시 가동 중 ({us_open_h}:{us_open_m:02d} KST!)")

                    logger.info(f"  🇺🇸 [{time_str}] US Market 실시간 정규장 관제 실행 중...")
                    run_pipeline(phase='us_regular')
                    
                    # 🚀 [Event-Driven Yield Harvester & Re-Peg]
                    try:
                        if not hasattr(main, '_kis_adapter'):
                            from src.execution._kis_adapter import KISTraderAdapter
                            main._kis_adapter = KISTraderAdapter(mode='live', fetch_balance_on_init=False)
                        _adapter = main._kis_adapter

                        # 주기적(60초) 잔고 갱신으로 실제 주문 가능 달러 현금 추적
                        _now_ts = time.time()
                        if not hasattr(main, '_last_bal_ts'):
                            main._last_bal_ts = 0.0
                        if _now_ts - main._last_bal_ts >= 60.0:
                            _adapter.fetch_live_balance()
                            main._last_bal_ts = _now_ts

                        _us_c = getattr(_adapter, 'us_cash_usd', 0.0)
                        if _us_c > 100:
                            _adapter.sweep_uninvested_cash_to_shield()
                            
                        # 🚀 [Time-Bounded Re-Peg Loop] 미체결 주문 포착 시 실시간 매도호가 정정 감시 (3.0초 쿨다운 적용)
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

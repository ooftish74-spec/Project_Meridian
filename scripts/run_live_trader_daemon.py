"""
Project Meridian: Live Streaming Intraday Execution Daemon
==========================================================

KRX 개장(08:30~15:30 KST) 및 US 개장(22:00~05:00 KST) 시간 동안
매분 초단위 실시간 틱 수급 및 괴리율 Z-Score를 감시하며,
시그널 발화 즉시 KIS OpenAPI 실계좌 매수/매도 TR을 1초 만에 자동 직송하는 전용 데몬 스케줄러.
"""

def pget(name, fallback, ctx=None):  # Wave2 TrackF: lazy (scripts set sys.path after imports)
    try:
        from src.params import pget as _pget
    except Exception:
        return fallback
    return _pget(name, fallback, ctx)
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


def _latch_try_acquire(results_dir: Path, today_str: str, key: str) -> bool:
    """★ Wave 0 O3: 날짜 키 1회성 래치 (results/daemon_daily_latch.json 영속, 원자적 기록).

    Returns True 이면 이번 호출이 오늘 최초 실행 권한을 획득한 것.
    """
    from src.utils.file_ops import atomic_write_json
    latch_file = results_dir / 'daemon_daily_latch.json'
    try:
        state = json.loads(latch_file.read_text(encoding='utf-8')) if latch_file.exists() else {}
    except Exception:
        state = {}
    if state.get('date') != today_str:
        state = {'date': today_str, 'executed': []}
    if key in state.get('executed', []):
        return False
    state.setdefault('executed', []).append(key)
    state[f'{key}_at'] = datetime.now().isoformat()
    try:
        atomic_write_json(latch_file, state, indent=2)
    except Exception as e:
        logger.error(f"  ⚠️ [Latch] 래치 영속화 실패({key}) — 메모리 래치로만 진행: {e}")
        if not hasattr(_latch_try_acquire, '_mem'):
            _latch_try_acquire._mem = set()
        if (today_str, key) in _latch_try_acquire._mem:
            return False
        _latch_try_acquire._mem.add((today_str, key))
    return True


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
                    max_mem = pget('system.memory_guard_limit_mb', 800)
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
                    # ★ Wave 0: 12시간 경과 KILL_SWITCH.flag 자동 삭제(무인 매매 재개) 제거 — 해제는 사람만
                    logger.warning(f"⛔ [Halt Active] {flag_name} 플래그 활성화 중. 수동 해제 전까지 데몬 일시 정지 (30초 대기).")
                    should_halt = True
            if should_halt:
                time.sleep(30)
                continue

            today_str = now.strftime('%Y-%m-%d')
            time_str = now.strftime('%H:%M:%S')
            t_mins = now.hour * 60 + now.minute

            # 주말(토=5, 일=6) 및 KRX/US 영업일 판별
            from src.utils.market_calendar import get_us_market_open_time, is_kr_ticker
            is_kr_trading = cal.is_trading_day(today_str)
            is_kr_session = is_kr_trading and ((now.hour == 8 and now.minute >= 30) or (9 <= now.hour < 15) or (now.hour == 15 and now.minute <= 30))

            # Lever 3: US 장 확장 관제 (월~금 야간 18:00~24:00 KST 및 화~토 새벽 정규장 종료 시각까지: EDT 05:00 / EST 06:00 KST)
            _us_open_h_chk, _ = get_us_market_open_time()
            _us_close_h_chk = 5 if _us_open_h_chk == 22 else 6
            is_us_evening = (now.weekday() in (0, 1, 2, 3, 4)) and (now.hour >= 18)
            is_us_morning = (now.weekday() in (1, 2, 3, 4, 5)) and (now.hour < _us_close_h_chk)
            is_us_session = is_us_evening or is_us_morning

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

            if not hasattr(main, '_daily_executed'):
                main._daily_executed = set()

            # ── KRX Call Auction & Market Open Time Triggers (1회성 Latch: 5분 창 + 디스크 영속) ──
            # ★ Wave 0 O3: 기존 '분==30 AND 초<20'(20초 창) + 최대 30초 sleep → 약 1/3 확률 미발동, 재시작 시 래치 소실
            if is_kr_trading:
                _hm = t_mins
                if 8 * 60 + 30 <= _hm < 8 * 60 + 35:
                    if _latch_try_acquire(results_dir, today_str, 'call_auction_0830'):
                        logger.info(f"  🔔 [{time_str}] [08:30 CallAuction Trigger] KRX 장전 동시호가 매도 파이프라인 1회 자율 집행!")
                        run_pipeline(phase="call_auction_0830")
                elif now.hour == 8 and 50 <= now.minute <= 54:
                    if ('call_auction_monitor', today_str, now.minute) not in main._daily_executed:
                        main._daily_executed.add(('call_auction_monitor', today_str, now.minute))
                        logger.info(f"  🔍 [{time_str}] [08:50~08:54 Monitor Trigger] 동시호가 예상체결가 모니터링 (분당 1회)!")
                        run_pipeline(phase="call_auction_monitor")
                elif 9 * 60 <= _hm < 9 * 60 + 5:
                    if _latch_try_acquire(results_dir, today_str, 'market_open_0900'):
                        logger.info(f"  🚀 [{time_str}] [09:00 MarketOpen Trigger] 정규장 개장 직후 1회 집행")
                        run_pipeline(phase="market_open_0900")
                if 9 * 60 <= _hm < 9 * 60 + 30:
                    if _latch_try_acquire(results_dir, today_str, 'market_open_main_exec'):
                        logger.info(f"  🚀 [{time_str}] [KRX Market Phase Trigger] 정규장 메인 포트폴리오 최적화 및 알파 오케스트레이터 1회 집행!")
                        run_pipeline(phase="market")

            if is_kr_session:
                logger.info(f"  ⚡ [{time_str}] KRX 실시간 장중 호가/수급 관제 중...")
                # ★ [Phase 98] 09:00 1회성 체결 실패 방어: 09:05~09:15 사이 국내 보유종목 0개일 때만 당일 1회 재진입
                try:
                    if now.hour == 9 and 5 <= now.minute <= 15 and ('intraday_auto_retry', today_str) not in main._daily_executed:
                        _ls_file = results_dir / 'latest_signals.json'
                        _kp_check = results_dir / 'kis_portfolio.json'
                        _kr_held_cnt = 0
                        if _kp_check.exists():
                            _kp_c_data = json.loads(_kp_check.read_text(encoding='utf-8'))
                            _kr_held_cnt = sum(1 for _tk_c, _hv_c in (_kp_c_data.get('holdings') or {}).items() if is_kr_ticker(str(_tk_c)) and int(_hv_c.get('qty', 0) or 0) > 0)
                        if _ls_file.exists() and _kr_held_cnt == 0:
                            with open(_ls_file, encoding='utf-8') as f:
                                _ls_data = json.load(f)
                            _sigs = _ls_data.get('signals', {})
                            if _sigs:
                                main._daily_executed.add(('intraday_auto_retry', today_str))
                                logger.info("  🎯 [Intraday Auto-Retry] 09:00 시초가 미체결(보유 0종목) 감지 ➔ 당일 1회 자동 재진입 파이프라인 가동!")
                                run_pipeline(phase="market")
                except Exception as _retry_e:
                    logger.debug(f"  장중 재진입 예외: {_retry_e}")

                # 실시간 signal_cache에서 텔레메트리 연동 (하드코딩 더미 제거)
                _telemetry = {'vkospi': 18.5, 'ofi_z': 0.0, 'lead_lag': 0.0}
                _sc_data: dict = {}
                _sc_file = results_dir / 'signal_cache.json'
                if _sc_file.exists():
                    try:
                        _sc_data = json.loads(_sc_file.read_text(encoding='utf-8'))
                        _telemetry['vkospi'] = float(_sc_data.get('vkospi', 18.5))
                        _telemetry['ofi_z'] = float(_sc_data.get('ofi_z', 0.0))
                        _telemetry['lead_lag'] = float(_sc_data.get('lead_lag', 0.0))
                        _telemetry['foreign_flow_krw'] = float(_sc_data.get('foreign_net_buy', 0.0) or 0.0)
                    except Exception:
                        pass

                # 1분 단위 Online Bayesian Particle Filter 실시간 미분 업데이트
                _bayesian_res = main._bayesian_filter.update(_telemetry)
                if loop_count % 12 == 0:
                    main._bayesian_filter.save_state(str(results_dir / 'bayesian_filter_state.json'))
                logger.debug(f"  🧪 [Online Bayesian Filter] Dominant Regime: {_bayesian_res.get('dominant_regime')}")
                
                # 횡보 폭풍장 휩소 방어 엔진(Anti-Whipsaw Defense) 스캔 — 실제 지수/ETF 시계열 연동
                _bench_prices = _sc_data.get('kospi_close_history', [])
                if not _bench_prices:
                    try:
                        import pandas as _pd_w
                        _pq_069500 = _PROJECT_ROOT / 'data' / 'historical_10y' / 'kr_069500.parquet'
                        if _pq_069500.exists():
                            _bench_prices = _pd_w.read_parquet(_pq_069500)['close'].dropna().tail(25).astype(float).tolist()
                    except Exception:
                        _bench_prices = []
                _mkt_payload = {'signal_cache': _sc_data}
                _whipsaw_res = main._whipsaw_defense.evaluate_whipsaw_defense(_bench_prices, market_data=_mkt_payload)
                logger.debug(f"  🛡️ [Anti-Whipsaw] Active: {_whipsaw_res.get('is_whipsaw_active')}, KER: {_whipsaw_res.get('ker_value'):.3f}")

                # 기관 알고리즘 발자국(Algorithmic Footprint) 스캐닝
                _footprint_res = main._footprint_tracker.compute_footprint_score(_mkt_payload)
                logger.debug(f"  🐾 [Footprint Tracker] Score: {_footprint_res.get('footprint_score'):.2f}, Herding: {_footprint_res.get('is_herding_active')}")

                # 무위험 Yield Arbitrage 파킹 스캔
                _yield_info = main._yield_arb_engine.scan_yield_opportunities(_mkt_payload)

                # 🚀 [Intraday Dynamic Entry Scanner] 브라운 정보축적 주기 & 한계 순기대효용(Marginal Net EV) 게이트
                # 장마감 파이프라인(15:10 KST) 이후 동시호가 시간(15:10~15:30)에는 신규 진입 스캔을 차단하고 청산 감시만 유지
                _is_kr_entry_window = (now.hour == 9 and now.minute >= 5) or (10 <= now.hour <= 14) or (now.hour == 15 and now.minute < 10)
                try:
                    from src.utils.dynamic_quant_engine import get_quant_engine
                    _dqe_d = get_quant_engine()
                    _opt_scan_sec = _dqe_d.compute_optimal_min_holding_minutes('069500', vix_level=_telemetry.get('vkospi')) * 60.0
                    if not hasattr(main, '_last_entry_scan_ts'):
                        main._last_entry_scan_ts = time.time()
                    if _is_kr_entry_window and (time.time() - main._last_entry_scan_ts >= _opt_scan_sec):
                        main._last_entry_scan_ts = time.time()
                        if not hasattr(main, '_entry_monitor'):
                            from src.execution.realtime_entry_monitor import RealtimeEntryMonitor
                            main._entry_monitor = RealtimeEntryMonitor(mode='live')

                        held_set = set()
                        _kr_eval_sum = 0.0
                        _kr_cash_avail = 0.0
                        _kp_file = results_dir / 'kis_portfolio.json'
                        if _kp_file.exists():
                            try:
                                _kp_data = json.loads(_kp_file.read_text(encoding='utf-8'))
                                held_set = set(_kp_data.get('holdings', {}).keys())
                                _kr_cash_avail = float(_kp_data.get('cash_krw', _kp_data.get('cash', 0.0)) or 0.0)
                                _kr_eval_sum = sum(
                                    float(_v.get('qty', 0) or 0) * float(_v.get('current_price', _v.get('avg_price', 0.0)) or 0.0)
                                    for _k, _v in (_kp_data.get('holdings') or {}).items()
                                    if str(_k).isdigit()
                                )
                            except Exception:
                                pass
                        _exit_details = {}
                        try:
                            from src.execution.position_ledger import PositionLedgerManager
                            _exit_details = PositionLedgerManager().get_daily_exit_details(today_str)
                        except Exception:
                            pass

                        _raw_entry_sigs = main._entry_monitor.scan_intraday_entries(held_tickers=held_set)
                        _entry_sigs = []
                        for _es in _raw_entry_sigs:
                            _tk_e = str(_es.get('ticker', '')).upper()
                            if _tk_e in _exit_details:
                                _ex_i = _exit_details[_tk_e]
                                try:
                                    _ex_dt = datetime.fromisoformat(str(_ex_i.get('exited_at', ''))).replace(tzinfo=None)
                                    _now_naive = now.replace(tzinfo=None)
                                    _el_m = max(0.0, (_now_naive - _ex_dt).total_seconds() / 60.0)
                                except Exception:
                                    _el_m = 0.0
                                _hurdle = _dqe_d.compute_reentry_hurdle_pct(
                                    _tk_e, elapsed_minutes=_el_m, prev_realized_pnl_pct=float(_ex_i.get('realized_pnl_pct', 0.0) or 0.0)
                                )
                                _est_ev = abs(float(_es.get('chg_pct', 0.0) or 0.0)) * float(_es.get('confidence', 0.85) or 0.85)
                                if _est_ev < _hurdle:
                                    continue
                            _entry_sigs.append(_es)

                        if _entry_sigs:
                            logger.info(f"  ⚡ [Intraday Dynamic Entry] {len(_entry_sigs)}개 장중 돌파 수급 신호 감지: {[s['ticker'] for s in _entry_sigs]}")
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

                                # [Bayesian State-Divergence & FDR Multiple-Testing Trigger]
                                # 고정 비율(1.0) 비교 대신, 직전 최적화 시점 대비 레짐 사후확률 전변동 거리(D_TV) + 내재변동성 변위가
                                # 시장 무거래 반폭(B_market)을 초과하거나, 미보유 신규 신호 Z-Score가 다중가설검정 임계치(z_FDR)를 돌파할 때만 가동
                                _curr_opt_state = {
                                    'regime': str(_bayesian_res.get('dominant_regime', 'caution')),
                                    'regime_probs': _bayesian_res.get('posterior', _bayesian_res.get('probabilities', {})),
                                    'vkospi': float(_telemetry.get('vkospi', 18.5)),
                                }
                                if not hasattr(main, '_prev_opt_state'):
                                    main._prev_opt_state = dict(_curr_opt_state)
                                if not hasattr(main, '_last_entry_pipeline_ts'):
                                    main._last_entry_pipeline_ts = time.time() if held_set else 0.0
                                _max_sig_score = max(float(s.get('score', 0.0) or 0.0) for s in _entry_sigs)
                                _trig_res = _dqe_d.compute_state_divergence_trigger(
                                    main._prev_opt_state, _curr_opt_state, max_unheld_signal_z=_max_sig_score
                                )
                                if _trig_res.get('should_reoptimize', False) and (
                                    time.time() - main._last_entry_pipeline_ts >= _opt_scan_sec * 3.0
                                ):
                                    main._last_entry_pipeline_ts = time.time()
                                    main._prev_opt_state = dict(_curr_opt_state)
                                    logger.info(
                                        f"  🚀 [Intraday State-Divergence Execute] 상태 변위(D={_trig_res['state_divergence']:.3f}, "
                                        f"z_max={_trig_res['max_unheld_z']:.2f} >= z_FDR={_trig_res['fdr_z_threshold']:.2f}) 돌파 ➔ 파이프라인 가동!"
                                    )
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
                # ★ 장전 동시호가(08:30~09:00) 및 장마감 동시호가(15:20~15:30) 구간에서는 일반 체결이 불가능하므로
                # 정규 접속매매 시간(09:00~15:20, 540 <= t_mins <= 920)에만 실시간 손절/Time-Decay 시장가 청산을 집행하여 동시호가 무한 취소·재발주 루프 차단
                if (540 <= t_mins <= 920) and time.time() - main._last_exit_scan_ts >= 30.0:
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
                            from src.utils.dynamic_quant_engine import get_quant_engine
                            _PARKING_TICKERS = {'459580', '423160', '449170', '430740', '357870', '357330', '130730', 'SHV', 'SGOV', 'BIL'} | get_quant_engine().get_cash_equivalent_tickers()
                            for _t, _h in _kp_data.get('holdings', {}).items():
                                # KRX 장중에는 국내 종목만 손절/익절 감시 (미국 종목은 US 세션에서 감시, 파킹형 ETF 제외)
                                if not is_kr_ticker(str(_t)) or _t in _PARKING_TICKERS:
                                    continue
                                _cp = float(_h.get('current_price', 0.0))
                                _avg = float(_h.get('avg_price', 0.0))
                                _ret = float(_h.get('return_pct', _h.get('pnl_pct', 0.0)) or 0.0)

                                # ★ [CRITICAL FIX] 디스크 캐시의 정적 가격이 아닌 KIS 브로커 실시간 현재가 직송 조회
                                try:
                                    _live_p = _adapter._get_current_price(_t)
                                    if _live_p and float(_live_p) > 0:
                                        _cp = float(_live_p)
                                        if _avg > 0:
                                            # UniversalExitEngine 및 PositionLedgerManager는 퍼센트 단위(예: -5.2 = -5.2%) 사용!
                                            _ret = ((_cp - _avg) / _avg) * 100.0
                                except Exception as _p_err:
                                    logger.debug(f"  {_t} 실시간 시세 조회 폴백: {_p_err}")

                                _hwm = ledger_mgr.update_hwm(_t, current_price=_cp, current_pnl_pct=_ret)
                                _entry_date = _h.get('entry_date') or ledger_mgr.get_entry_date(_t) or today_str
                                try:
                                    from datetime import timedelta as _td
                                    _d_start = datetime.strptime(str(_entry_date)[:10], '%Y-%m-%d').date()
                                    _d_end = datetime.strptime(today_str, '%Y-%m-%d').date()
                                    _hold_days = 0
                                    _d_cur = _d_start + _td(days=1)
                                    while _d_cur <= _d_end:
                                        if cal.is_trading_day(_d_cur.strftime('%Y-%m-%d')):
                                            _hold_days += 1
                                        _d_cur += _td(days=1)
                                except Exception:
                                    _hold_days = 0
                                if _hold_days >= 1:
                                    _hold_mins = float(_hold_days) * 1440.0
                                else:
                                    _created_str = _hwm.get('created_at', '')
                                    try:
                                        _created_dt = datetime.fromisoformat(str(_created_str)).replace(tzinfo=None)
                                        _now_naive = now.replace(tzinfo=None)
                                        _hold_mins = max(0.0, (_now_naive - _created_dt).total_seconds() / 60.0)
                                    except Exception:
                                        _hold_mins = 0.0
                                _exit_positions[f"LIVE_SSOT:{_t}"] = {
                                    'ticker': _t,
                                    'stream_id': _h.get('stream_id', 'LIVE_SSOT'),
                                    'quantity': _h.get('qty', 0),
                                    'entry_price': _avg,
                                    'current_price': _cp,
                                    'unrealized_pnl_pct': _ret,
                                    'peak_pnl_pct': float(_hwm['peak_pnl_pct']) if _hwm.get('peak_pnl_pct') is not None else _ret,
                                    'peak_price': float(_hwm['peak_price']) if (_hwm.get('peak_price') or 0.0) > 0 else _cp,
                                    'lowest_price': float(_hwm['lowest_price']) if (_hwm.get('lowest_price') or 0.0) > 0 else _cp,
                                    'scaled_out': bool(_hwm.get('scaled_out', False)),
                                    'trail_activated': bool(_hwm.get('trail_activated', False)),
                                    'holding_days': float(_hold_days),
                                    'holding_minutes': _hold_mins,
                                    'entry_date': _entry_date,
                                    'market': 'KR'
                                }
                            _cur_regime = str(_kp_data.get('regime', 'caution') or 'caution')
                            _exit_res = UniversalExitEngine().evaluate_all_positions(_exit_positions, regime=_cur_regime)
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
                                _ex_pnl = float(_exit_positions.get(f"LIVE_SSOT:{_tk}", {}).get('unrealized_pnl_pct', 0.0) or 0.0)
                                _eo['pnl_pct'] = _ex_pnl
                                _exit_orders.append(_eo)

                            if _exit_orders:
                                logger.warning(f"  🚨 [Intraday Exit Triggered] {len(_exit_orders)}건 손절/청산 신호 발화 ➔ 즉시 실계좌 시장가 매도 집행!")
                                _exec = ExecutionEngine(mode='live')
                                _exec_res = _exec.execute(_exit_orders)
                                _filled_tks = {str(_f.get('ticker')) for _f in getattr(_exec_res, 'fills', [])}
                                _submitted_tks = {
                                    str(_o.get('ticker') if isinstance(_o, dict) else getattr(_o, 'ticker', ''))
                                    for _o in getattr(_exec_res, 'orders', [])
                                    if (str(_o.get('status', '') if isinstance(_o, dict) else getattr(_o, 'status', '')) in ('filled', 'submitted', 'submitted_market_fallback', 'filled_market_fallback'))
                                }
                                for _eo in _exit_orders:
                                    _tk = str(_eo.get('ticker'))
                                    if _tk in _filled_tks or _tk in _submitted_tks or getattr(_exec_res, 'n_filled', 0) > 0:
                                        main._exit_orders_in_flight[_tk] = _now_ts
                                        if _eo.get('exit_type') in ('scale_out_50pct', 'time_decay_exit'):
                                            ledger_mgr.mark_scaled_out(_tk)
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
                            _bal_ok = _adapter.fetch_live_balance()
                            main._last_bal_ts = _now_ts
                            if _bal_ok and hasattr(_adapter, 'positions'):
                                try:
                                    from src.execution.position_ledger import PositionLedgerManager
                                    PositionLedgerManager().purge_closed_positions(set(_adapter.positions.keys()))
                                except Exception:
                                    pass

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
                                    _held_us_tickers = set()
                                    for h in us_engine.holdings:
                                        tk = h['ticker']
                                        _held_us_tickers.add(tk)
                                        _exch = _adapter.resolve_overseas_exchange(tk)
                                        p = (
                                            _adapter.get_live_overseas_price(tk, _exch)
                                            or _adapter.get_live_overseas_price(tk, 'NASD')
                                            or _adapter.get_live_overseas_price(tk, 'NYSE')
                                            or _adapter.get_live_overseas_price(tk, 'AMEX')
                                            or float(h.get('current_price', 0.0))
                                        )
                                        if p > 0:
                                            curr_prices[tk] = p
                                    if not hasattr(main, '_exit_orders_in_flight'):
                                        main._exit_orders_in_flight = {}
                                    _now_us_ts = time.time()
                                    main._exit_orders_in_flight = {
                                        _k: _ts for _k, _ts in main._exit_orders_in_flight.items()
                                        if (_now_us_ts - _ts) < 300.0 and (_k in _held_us_tickers or is_kr_ticker(str(_k)))
                                    }
                                    exit_orders = us_engine.update_trailing_stop(curr_prices)
                                    for eo in exit_orders:
                                        _tk_us = eo['ticker']
                                        if _tk_us in main._exit_orders_in_flight:
                                            logger.info(f"  ⏳ [US Exit In-Flight Cooldown] {_tk_us} 매도 주문 이미 접수 진행 중 ({_now_us_ts - main._exit_orders_in_flight[_tk_us]:.0f}s 전) ➔ 중복 발주 차단")
                                            continue
                                        logger.info(f"  🚨 [US Trailing Stop] {_tk_us} 손절/익절 청산 주문 송출: {eo['quantity']}주 @ ${eo['price']:.2f}")
                                        _us_sell_fill = _adapter.sell(
                                            ticker=_tk_us,
                                            quantity=eo['quantity'],
                                            price=eo['price'],
                                            order_type='limit',
                                            notes=eo.get('reason', 'US Night Trailing Stop')
                                        )
                                        if getattr(_us_sell_fill, 'status', '') in ('filled', 'submitted', 'partially_filled', 'filled_market_fallback'):
                                            main._exit_orders_in_flight[_tk_us] = _now_us_ts
                                            try:
                                                from src.execution.position_ledger import PositionLedgerManager
                                                PositionLedgerManager().record_daily_exit(
                                                    str(_tk_us),
                                                    eo.get('reason', 'US Night Trailing Stop'),
                                                    today_str,
                                                    realized_pnl_pct=float(eo.get('pnl_pct', 0.0) or 0.0),
                                                )
                                            except Exception:
                                                pass
                            except Exception as _ex_err:
                                logger.debug(f"  US Trailing Stop 평가 예외: {_ex_err}")

                        # 3. [Time-Bounded Re-Peg Loop] 미체결 주문 포착 시 실시간 매도호가 정정 감시 (3.0초 쿨다운 + 최소 10초 거래소 접수 대기)
                        if not hasattr(main, '_last_repeg_time'):
                            main._last_repeg_time = 0.0
                        if time.time() - main._last_repeg_time >= 3.0:
                            _adapter.check_and_repeg_unexecuted_orders(max_wait_sec=10.0)
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

def _phase_collect_data():
    """06:00 모닝 수집 — 글로벌 + FRED + 크로스마켓 + 뉴스.

    ★ Pipeline Timing Optimization (2026-05-29)
    KR 종가/섹터/수급은 evening_data(17:00)로 이동.
    FRED 일간 + 크로스마켓은 US 장 마감 후인 06:00이 최적.

    수집 범위:
      1. 글로벌 시그널 (VIX, SOX, EWY, FXI 등 16개)
      2. US 매크로 FRED (DGS10, 스프레드 — US 장 마감 후 최신)
      3. 크로스마켓 (US-JP, Yield Curve — US 확정)
      4. 뉴스 감성 (야간 한국/글로벌 뉴스)
      5. ATR/VIX 사전 적재
    """
    logger.info("  🌅 모닝 수집 시작 (글로벌 + FRED + 크로스마켓 + 뉴스)")
    try:
        from src.data_collection.unified_collector import run_daily
        results = run_daily(mode='morning')
        logger.info(f"  ✅ 모닝 수집 완료: {results.get('elapsed_sec', '?')}초")
    except Exception as e:
        logger.warning(f"  모닝 수집 실패 (글로벌만 시도): {e}")
        try:
            from src.data_collection.unified_collector import collect_global_signals
            signals = collect_global_signals()
            logger.info(f"  ✅ 글로벌 시그널 fallback: {len(signals)}개")
        except Exception as e2:
            logger.error(f"  글로벌 시그널도 실패: {e2}")


def _phase_global_data():
    """글로벌 데이터만 수집 (KRX 휴장일)."""
    logger.info("  글로벌 시그널 수집 (VIX, 금리, 환율)")
    try:
        from src.data_collection.unified_collector import collect_global_signals
        signals = collect_global_signals()
        logger.info(f"  ✅ 글로벌 시그널: {len(signals)}개")
    except Exception as e:
        logger.warning(f"  글로벌 데이터 실패: {e}")


def _update_signal_cache(updates: dict):
    """signal_cache.json에 항목 추가/갱신.

    ★ NaN/None 방어 (2026-06-10):
      - float('nan'), None 값은 기존 유효한 값을 덮어쓰지 않음
      - 중첩 dict는 재귀 병합 (shallow update가 아닌 deep merge)
      - 기존에 값이 없었던 키에 NaN이 오면 해당 키를 건너뜀
    """
    import math as _m

    def _is_nan(v):
        """NaN/None/inf 판별."""
        if v is None:
            return True
        if isinstance(v, float) and (_m.isnan(v) or _m.isinf(v)):
            return True
        if isinstance(v, str) and v.lower() in ('nan', 'none', 'inf', '-inf'):
            return True
        return False

    def _deep_merge(base: dict, overlay: dict) -> dict:
        """NaN-safe deep merge. NaN 값은 base의 기존값을 유지.
        
        ★ 기존 base에 NaN이 저장된 경우, 새 유효값으로 교체.
        """
        for key, new_val in overlay.items():
            if isinstance(new_val, dict) and isinstance(base.get(key), dict):
                # 중첩 dict → 재귀 병합
                _deep_merge(base[key], new_val)
            elif _is_nan(new_val):
                # 새 값이 NaN → 기존값 유지 (단, 기존도 NaN이면 키 삭제)
                if key in base and _is_nan(base[key]):
                    del base[key]  # ★ 기존 NaN도 제거하여 기본값 적용 유도
            else:
                base[key] = new_val
        return base

    try:
        cache_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        existing = {}
        if cache_file.exists():
            existing = json.loads(cache_file.read_text())
        _deep_merge(existing, updates)
        existing['last_overnight_update'] = datetime.now().isoformat()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cache_file, existing, indent=2)
    except Exception as e:
        logger.warning(f"  signal_cache 업데이트 실패: {e}")



# ═══════════════════════════════════════════════════════
# A-Unique Phases (Project-A에서 이식)
# ═══════════════════════════════════════════════════════

def _phase_intraday():
    """장중 실시간 수집/모니터링 (09:30 KST).

    A의 phase_intraday 이식:
      - ★ MacroRealtimeRefresher: 글로벌 매크로 실시간 갱신 (3-Tier)
      - ★ RealtimeExitMonitor 하이브리드 모니터링 시작
        (WebSocket + Threshold Alert + REST Heartbeat)
      - 장중 pykrx 5분봉 수집
      - Exit Manager (TP/SL/Trailing) 평가
      - Intraday regime 갱신
    """
    logger.info("  📊 장중 모니터링")

    # 0-pre. ★ MacroRealtimeRefresher — 글로벌 매크로 실시간 갱신
    # 3-Tier: VIX/FX(5분), 원자재/채권(30분), FRED(1일)
    # 장 시간 외에는 내부에서 자동 스킵
    try:
        from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
        _refresher = MacroRealtimeRefresher()
        _refresh_result = _refresher.refresh(tier='auto')
        _t1 = _refresh_result.get('tier1', {}).get('n_updated', 0)
        _t2 = _refresh_result.get('tier2', {}).get('n_updated', 0)
        _kr = _refresh_result.get('kr_indices', {}).get('n_updated', 0)
        if _t1 + _t2 + _kr > 0:
            logger.info(f"  ✅ MacroRefresh: T1={_t1}, T2={_t2}, KR={_kr}")
        elif _refresh_result.get('skipped'):
            logger.debug(f"  ⏸ MacroRefresh: {_refresh_result['skipped']}")
    except ImportError:
        logger.debug("  macro_realtime_refresher import 실패")
    except Exception as e:
        logger.warning(f"  MacroRefresh 실패: {e}")

    # 0. ★ RealtimeExitMonitor 시작 (WebSocket + Heartbeat 통합)
    # 기존 수동 WebSocket 시작을 대체 — 모니터가 내부에서 관리
    try:
        from src.execution.realtime_exit_monitor import start_exit_monitoring

        monitor = start_exit_monitoring()
        if monitor.is_running:
            logger.info(
                f"  🟢 RealtimeExitMonitor 활성: "
                f"{monitor.stats.get('alert_zones', 0)}포지션 모니터링")
        else:
            logger.info("  ℹ️ RealtimeExitMonitor 비활성 → 기존 방식 fallback")
            # 기존 WebSocket fallback
            try:
                from src.data_collection.kis_websocket import start_realtime_streaming
                ws_tickers = set()
                _shadow_f = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
                if _shadow_f.exists():
                    _sp = json.loads(_shadow_f.read_text())
                    for pos in _sp.get('positions', {}).values():
                        tk = pos.get('ticker', '')
                        if tk:
                            ws_tickers.add(tk)
                _ws_core_etfs = cfg.get('ws.core_etf_tickers',
                                        ['069500', '122630', '252670', '114800'])
                ws_tickers.update(_ws_core_etfs)
                _ws_max = cfg.get('ws.max_subscription_tickers', 40)
                ws_tickers = list(ws_tickers)[:_ws_max]
                ws = start_realtime_streaming(ws_tickers)
                if ws.is_running:
                    logger.info(f"  🟢 WebSocket fallback: {len(ws_tickers)}종목")
                else:
                    logger.info("  ℹ️ WebSocket 미가용 → REST 전용")
            except Exception as e:
                logger.info(f"  ℹ️ WebSocket fallback 실패: {e}")
    except ImportError:
        logger.debug("  realtime_exit_monitor import 실패 — 기존 방식 유지")
        try:
            from src.data_collection.kis_websocket import start_realtime_streaming
            ws_tickers = set()
            _shadow_f = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if _shadow_f.exists():
                _sp = json.loads(_shadow_f.read_text())
                for pos in _sp.get('positions', {}).values():
                    tk = pos.get('ticker', '')
                    if tk:
                        ws_tickers.add(tk)
            _ws_core_etfs = cfg.get('ws.core_etf_tickers',
                                    ['069500', '122630', '252670', '114800'])
            ws_tickers.update(_ws_core_etfs)
            _ws_max = cfg.get('ws.max_subscription_tickers', 40)
            ws_tickers = list(ws_tickers)[:_ws_max]
            ws = start_realtime_streaming(ws_tickers)
            if ws.is_running:
                logger.info(f"  🟢 WebSocket 스트리밍: {len(ws_tickers)}종목 구독")
            else:
                logger.info("  ℹ️ WebSocket 미가용 → REST fallback")
        except ImportError:
            logger.debug("  kis_websocket import 실패 — REST 전용")
        except Exception as e:
            logger.info(f"  ℹ️ WebSocket 미가용: {e} → REST fallback")
    except Exception as e:
        logger.info(f"  RealtimeExitMonitor 시작 실패: {e} → 기존 방식")

    # 1. 장중 가격 수집
    try:
        from src.data_collection.pykrx_fetcher import get_5min_bars
        from config.universe import UNIVERSE
        tickers = UNIVERSE.get('kr_stocks', [])[:20]
        for ticker in tickers:
            try:
                bars = get_5min_bars(ticker)
                if bars is not None and len(bars) > 0:
                    out_dir = _PROJECT_ROOT / 'data' / 'minute_bars' / ticker
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_file = out_dir / f'{date.today().isoformat()}.csv'
                    bars.to_csv(out_file, index=False)
            except Exception:
                pass
        logger.info(f"  ✅ 장중 5분봉 수집: {len(tickers)}종목")
    except ImportError:
        logger.debug("  pykrx_fetcher import 실패")
    except Exception as e:
        logger.warning(f"  장중 수집 실패: {e}")

    # 2. 실시간 수급/밸류에이션
    try:
        from src.data_collection.realtime_collector import RealtimeCollector
        from src.data_collection.realtime_constants import KR_SECTOR_STOCKS
        rc = RealtimeCollector()
        _sectors = list(KR_SECTOR_STOCKS.keys())[:5]  # 장중이므로 상위 5개 섹터만
        _sd = rc.collect_kr_supply_demand(_sectors)
        logger.info(f"  ✅ 실시간 섹터 수급 수집: {len(_sd)}개 섹터")
    except ImportError:
        logger.debug("  realtime_collector import 실패")
    except Exception as e:
        logger.warning(f"  실시간 수급 실패: {e}")

    # 3. ★ 전체 스트림 Exit 조건 점검 (장중 SL/TP 실시간 체크)
    # S1은 당일 청산 전략이므로 필수, S2/S3/S4도 장중 SL 초과 시 즉시 대응
    try:
        from src.portfolio.shadow_manager import ShadowPortfolioManager
        _initial = cfg.get('portfolio.initial_capital')
        _mgr = ShadowPortfolioManager(initial_capital=_initial)

        # 모든 보유 종목 ticker 수집
        _all_exit_tickers = set()
        for pk in _mgr.positions:
            _, _tk = _mgr._parse_position_key(pk)
            _all_exit_tickers.add(_tk)

        if _all_exit_tickers:
            logger.info(f"  🔍 장중 Exit 체크: {len(_all_exit_tickers)}종목")

            # pykrx로 현재가 조회
            try:
                from pykrx import stock as _pykrx_stock
                _today_short = date.today().strftime('%Y%m%d')
                rt_prices = {}
                for _tk in _all_exit_tickers:
                    try:
                        _df = _pykrx_stock.get_market_ohlcv_by_date(
                            _today_short, _today_short, _tk)
                        if len(_df) > 0:
                            _price = _df.iloc[-1].get('종가', 0)
                            if _price > 0:
                                rt_prices[_tk] = float(_price)
                    except Exception:
                        pass

                if rt_prices:
                    # MTM 업데이트 + Exit 체크
                    _mgr.mark_to_market(rt_prices)

                    # 레짐 로드 — ★ pipeline_state.json SSoT
                    _regime = 'caution'
                    try:
                        _rf = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
                        if _rf.exists():
                            _ps = json.loads(_rf.read_text())
                            _regime = _ps.get('kr_regime') or _ps.get('regime', 'caution')
                    except Exception:
                        pass

                    sell_orders = _mgr.check_exit_conditions(_regime)

                    if sell_orders:
                        # S1 매도 (ETF 수수료)
                        s1_sells = [s for s in sell_orders
                                    if s.get('stream_id') == 'S1']
                        # S2/S3/S4 매도 (주식 수수료)
                        other_sells = [s for s in sell_orders
                                       if s.get('stream_id') != 'S1']

                        if s1_sells:
                            _etf_comm = cfg.get('execution.etf_commission_rate', 0.00015)
                            _mgr.execute_sells(s1_sells, rt_prices,
                                               commission_rate=_etf_comm)
                            logger.info(f"  🔴 S1 장중 긴급 청산: {len(s1_sells)}건")

                        if other_sells:
                            _mgr.execute_sells(other_sells, rt_prices)
                            for _so in other_sells:
                                logger.info(
                                    f"  🔴 [{_so.get('stream_id','')}] "
                                    f"{_so.get('name','?')} 장중 Exit: "
                                    f"{_so.get('reason','')[:60]}")

                        _mgr.save()
                        logger.info(f"  ✅ 장중 Exit: {len(sell_orders)}건 청산 완료")
                    else:
                        logger.info(f"  ✅ 장중 Exit: 청산 대상 없음")
                else:
                    logger.warning("  ⚠️ 장중 실시간 가격 조회 실패")
            except ImportError:
                logger.debug("  pykrx import 실패 — Exit 스킵")
        else:
            logger.info("  ⏸ 보유 포지션 없음 — Exit 체크 스킵")
    except Exception as e:
        # ★ avg_price KeyError 등 장중 Exit의 비치명적 실패는 info 레벨
        # (중요 Exit는 closing/aftermarket에서 재시도)
        logger.info(f"  장중 Exit 체크 스킵: {e}")

    # 4. ★ S1 장중 재진입 (Signal-Driven)
    # 재진입 시점까지 수집된 최신 신호 기반으로 동적 판단
    try:
        from config.dynamic_config import DynamicConfig
        _cfg_intra = DynamicConfig()
        _reentry_enabled = _cfg_intra.get('s1.intraday.reentry_enabled', True)

        if _reentry_enabled:
            from datetime import time as _time
            _now = datetime.now()
            _current_time = _now.time()

            # 세션 시간대 (DynamicConfig)
            _sessions = _cfg_intra.get('s1.intraday.sessions', [
                {'name': 'morning', 'start': '09:00', 'end': '10:00'},
                {'name': 'midday', 'start': '10:30', 'end': '13:00'},
                {'name': 'afternoon', 'start': '13:30', 'end': '14:30'},
            ])

            # 현재 세션 판정
            _active_session = None
            for _sess in _sessions:
                _s = _time.fromisoformat(_sess['start'])
                _e = _time.fromisoformat(_sess['end'])
                if _s <= _current_time <= _e:
                    _active_session = _sess['name']
                    break

            if _active_session:
                # 현재 S1 포지션 수 확인
                _max_entries = _cfg_intra.get('s1.max_daily_entries', 3)
                _s1_count = sum(1 for pk in _mgr.positions if pk.startswith('S1:'))

                if _s1_count < _max_entries:
                    logger.info(f"  🔄 S1 재진입 시도: 세션={_active_session}, "
                               f"현재 포지션={_s1_count}/{_max_entries}")

                    # 최신 market_data 구성
                    try:
                        from src.data.market_data_bridge import MarketDataBridge
                        _bridge_re = MarketDataBridge()
                        _sc_re = _bridge_re.build_signal_cache()
                        _ov_re = _bridge_re.build_overnight_intel()
                        _rh_re = _bridge_re.get_regime_history()
                        _market_data_re = {
                            'signal_cache': _sc_re,
                            'overnight_intel': _ov_re,
                            'vix_history': _rh_re.get('vix_history', []),
                            'kospi_returns': _rh_re.get('kospi_returns', []),
                        }

                        # 레짐 로드
                        _regime_re = 'caution'
                        _rf_re = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
                        if _rf_re.exists():
                            _ps_re = json.loads(_rf_re.read_text())
                            _regime_re = _ps_re.get('kr_regime') or _ps_re.get('regime', 'caution')

                        # S1 시그널 재생성
                        from src.streams.s1_edge.edge_stream import S1EdgeStream
                        _s1_re = S1EdgeStream()
                        _signals_re = _s1_re.generate_signals(_regime_re, _market_data_re)

                        if _signals_re:
                            # 세션별 사이즈 스케일링
                            _size_scales = _cfg_intra.get('s1.intraday.session_size_scale', {
                                'morning': 1.0, 'midday': 0.8, 'afternoon': 0.6,
                            })
                            _scale = _size_scales.get(_active_session, 0.8)

                            for _sig in _signals_re:
                                _sig['size'] = _sig.get('size', 0) * _scale
                                _sig['entry_session'] = _active_session
                                _sig['is_reentry'] = True

                            # 기존 포지션과 동일 ticker 제거 (독립 포지션이므로 direction만 확인)
                            _existing_tickers = {pk.split(':')[1] for pk in _mgr.positions
                                                if pk.startswith('S1:')}
                            _signals_re = [s for s in _signals_re
                                          if s.get('ticker') not in _existing_tickers]

                            if _signals_re:
                                logger.info(f"  ✅ S1 재진입 시그널: {len(_signals_re)}건 "
                                           f"(세션={_active_session}, scale={_scale:.1f})")

                                # 가상매수 실행
                                try:
                                    from scripts.run_virtual_trading import allocate_capital
                                    # ★ allocate_capital 시그니처:
                                    #   (all_signals, position_scale, capital, existing_tickers, mgr)
                                    _existing = list({
                                        pk.split(':')[1] for pk in _mgr.positions
                                        if ':' in pk
                                    })
                                    _s1_only_signals = {'S1': _signals_re}
                                    _buy_result = allocate_capital(
                                        _s1_only_signals,
                                        position_scale=1.0,
                                        capital=_mgr.cash,
                                        existing_tickers=_existing,
                                        mgr=_mgr,
                                    )
                                    if _buy_result:
                                        _mgr.save()
                                        logger.info(f"  ✅ S1 재진입 매수: {len(_buy_result)}건")
                                except Exception as _buy_err:
                                    logger.warning(f"  S1 재진입 매수 실패: {_buy_err}")
                            else:
                                logger.info(f"  ⏸ S1 재진입: 기존 포지션과 중복 → 스킵")
                        else:
                            logger.info(f"  ⏸ S1 재진입: 시그널 0건 (세션={_active_session})")
                    except Exception as _re_err:
                        logger.warning(f"  S1 재진입 데이터 구성 실패: {_re_err}")
                else:
                    logger.info(f"  ⏸ S1 재진입: 일일 최대 {_max_entries}회 도달 ({_s1_count}건)")
            else:
                logger.debug(f"  S1 재진입: 현재 세션 외 ({_current_time})")
    except Exception as e:
        logger.warning(f"  S1 재진입 실패: {e}")

    # 5. ★ 장중 SL 모니터링 — 절대 손절 기준 초과 포지션 즉시 청산
    # com.meridian.sl-monitor (11:00, 13:30)에서 intraday phase 재실행 시 동작
    # 기존 Exit 체크(섹션 3)는 스트림별 동적 SL을 사용하지만,
    # 이 섹션은 DynamicConfig 'portfolio.absolute_max_sl_pct' 기반 하드 플로어 역할
    try:
        _sl_check_enabled = cfg.get('portfolio.intraday_sl_check_enabled', True)
        if not _sl_check_enabled:
            logger.info("  ⏭️ 장중 SL 모니터링 비활성화")
        else:
            from src.portfolio.shadow_manager import ShadowPortfolioManager as _SLMgr
            _sl_capital = cfg.get('portfolio.initial_capital')
            _sl_mgr = _SLMgr(initial_capital=_sl_capital)

            if not _sl_mgr.positions:
                logger.info("  ⏸ 장중 SL 모니터링: 보유 포지션 없음")
            else:
                # 보유 종목 ticker 수집
                _sl_tickers = set()
                for _pk in _sl_mgr.positions:
                    _, _tk = _sl_mgr._parse_position_key(_pk)
                    _sl_tickers.add(_tk)

                # pykrx 현재가 조회
                _sl_prices = {}
                try:
                    from pykrx import stock as _pykrx_sl
                    _sl_today = date.today().strftime('%Y%m%d')
                    for _tk in _sl_tickers:
                        try:
                            _sl_df = _pykrx_sl.get_market_ohlcv_by_date(
                                _sl_today, _sl_today, _tk)
                            if len(_sl_df) > 0:
                                _sl_p = _sl_df.iloc[-1].get('종가', 0)
                                if _sl_p > 0:
                                    _sl_prices[_tk] = float(_sl_p)
                        except Exception:
                            pass
                except ImportError:
                    logger.debug("  pykrx import 실패 — SL 모니터링 스킵")

                if _sl_prices:
                    # MTM 업데이트
                    _sl_mgr.mark_to_market(_sl_prices)

                    # 레짐 로드
                    _sl_regime = 'caution'
                    try:
                        _sl_rf = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
                        if _sl_rf.exists():
                            _sl_ps = json.loads(_sl_rf.read_text())
                            _sl_regime = (_sl_ps.get('kr_regime')
                                          or _sl_ps.get('regime', 'caution'))
                    except Exception:
                        pass

                    # Exit 조건 체크 (스트림별 동적 SL/TP 포함)
                    _sl_sell_orders = _sl_mgr.check_exit_conditions(_sl_regime)

                    # ★ 절대 SL 기준 — DynamicConfig에서 로드
                    _max_sl = cfg.get('portfolio.absolute_max_sl_pct', -10.0)

                    # 절대 SL 위반 포지션 추가 수집
                    _emergency_sells = []
                    _existing_sell_keys = {s.get('pos_key') for s in _sl_sell_orders}
                    for _pk, _pos in _sl_mgr.positions.items():
                        if _pk in _existing_sell_keys:
                            continue
                        _avg = _pos.get('avg_price', _pos.get('entry_price', 0))
                        _cur = _pos.get('current_price', 0)
                        if _avg <= 0 or _cur <= 0:
                            continue
                        _pnl = (_cur - _avg) / _avg * 100  # % 단위
                        if _pnl < _max_sl:
                            _sid, _ttk = _sl_mgr._parse_position_key(_pk)
                            _emergency_sells.append({
                                'pos_key': _pk,
                                'ticker': _ttk,
                                'name': _pos.get('name', _ttk),
                                'quantity': _pos.get('quantity', 0),
                                'current_price': _cur,
                                'avg_price': _avg,
                                'pnl_pct': round(_pnl, 2),
                                'sell_type': 'absolute_sl',
                                'reason': (f"절대 SL 위반: {_pnl:+.1f}% "
                                           f"(한도 {_max_sl:.1f}%)"),
                                'stream_id': _sid,
                                'streams': _pos.get('streams', [_sid]),
                                'hold_days': 0,
                            })
                            logger.warning(
                                f"  🚨 [{_sid}] {_pos.get('name', _ttk)} "
                                f"절대 SL 위반: {_pnl:+.1f}% < {_max_sl:.1f}%")

                    # 전체 매도 대상 병합
                    _all_sl_sells = _sl_sell_orders + _emergency_sells

                    if _all_sl_sells:
                        _sl_mgr.execute_sells(_all_sl_sells, _sl_prices)
                        _sl_mgr.save()
                        logger.info(
                            f"  🔴 장중 SL 모니터링: {len(_all_sl_sells)}건 청산 "
                            f"(Exit={len(_sl_sell_orders)}, "
                            f"절대SL={len(_emergency_sells)})")
                    else:
                        logger.info("  ✅ 장중 SL 모니터링: 위반 포지션 없음")
                else:
                    logger.warning("  ⚠️ 장중 SL 모니터링: 현재가 조회 실패")
    except Exception as e:
        logger.info(f"  장중 SL 모니터링 스킵: {e}")

def _phase_krx_refresh():
    """KRX 확정 데이터 리프레시 (16:10 KST).

    KRX 장 마감(15:30) 후 pykrx 데이터 반영 time lag (~40분) 대응.
    16:10에 당일 확정 데이터만 수집.
    """
    logger.info("  🔄 KRX 확정 데이터 리프레시")
    try:
        from src.data_collection.unified_collector import run_daily
        results = run_daily(confirmed_only=True)
        logger.info(f"  ✅ KRX 확정 수집: {results.get('elapsed_sec', '?')}초")
    except TypeError:
        # confirmed_only 파라미터 미지원 시 일반 수집
        try:
            from src.data_collection.unified_collector import run_daily
            results = run_daily()
            logger.info(f"  ✅ KRX 수집 (full): {results.get('elapsed_sec', '?')}초")
        except Exception as e:
            logger.warning(f"  KRX 수집 실패: {e}")
    except Exception as e:
        logger.warning(f"  KRX 리프레시 실패: {e}")



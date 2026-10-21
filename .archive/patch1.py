def _phase_overnight():
    """야간 인텔리전스 수집 + OIS 계산.

    05:15 KST 실행 (미국장 마감 05:00 직후):
      1. 미국 선물 야간 변동 (ES=F, NQ=F, YM=F)
      2. EWY (SGX KOSPI200 프록시)
      3. VIX/DXY/US10Y/WTI/Gold 변동
      4. OIS(0~100) 계산 → signal_cache + threshold_adj
      5. US Market Regime 판정
      6. 이벤트 캘린더 체크 (FOMC/CPI 근접 여부)
      7. Market Shock 감지
    """
    logger.info("  🌙 Overnight Intelligence 수집")
    results = {}

    # ── 1. Overnight Macro 수집 ──
    try:
        from scripts.overnight_macro_collector import (
            collect_us_futures, collect_sgx_proxy,
            collect_macro_indicators, compute_overnight_score,
            compute_kospi_gap_estimate,
        )
        us_futures = collect_us_futures()
        sgx_proxy = collect_sgx_proxy()
        macro = collect_macro_indicators()
        overnight_score = compute_overnight_score(us_futures, sgx_proxy, macro)
        gap_estimate = compute_kospi_gap_estimate(overnight_score, sgx_proxy, us_futures)
        summary = {
            'date': date.today().isoformat(),
            'timestamp': datetime.now().isoformat(),
            'us_futures': us_futures,
            'sgx_proxy': sgx_proxy,
            'macro_indicators': macro,
            'overnight_score': overnight_score,
            'kospi_gap_estimate': gap_estimate,
        }

        # 결과 저장
        out_dir = _PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f'{date.today().isoformat()}.json'
        atomic_write_json(out_file, summary, indent=2)

        results['macro'] = {
            'us_futures': len(us_futures),
            'sgx_proxy': bool(sgx_proxy),
        }
        logger.info(f"  ✅ Overnight Macro: {len(us_futures)} 선물")

        # ★ signal_cache에 핵심 매크로 값 반영 (대시보드용)
        _macro_cache = {}
        for _mk, _sk in [('vix', 'vix'), ('dxy', 'dxy'), ('us10y', 'us10y'),
                          ('wti_oil', 'wti'), ('gold', 'gold'), ('usdkrw', 'usdkrw')]:
            _mi = macro.get(_mk, {})
            if isinstance(_mi, dict):
                _lc = _mi.get('last_close')
                _pc = _mi.get('prev_close')
                if _lc and isinstance(_lc, (int, float)):
                    _macro_cache[_sk] = _lc
                if _pc and isinstance(_pc, (int, float)):
                    _macro_cache[f'{_sk}_prev'] = _pc
        # US futures → S&P/NASDAQ 현재가
        for _fk, _sk in [('sp500_futures', 'sp500'), ('nasdaq_futures', 'nasdaq')]:
            _fi = us_futures.get(_fk, {})
            if isinstance(_fi, dict):
                _lc = _fi.get('last_close')
                if _lc and isinstance(_lc, (int, float)):
                    _macro_cache[_sk] = _lc
        if _macro_cache:
            _update_signal_cache(_macro_cache)
            logger.info(f"  💾 Macro → signal_cache: {list(_macro_cache.keys())}")
    except ImportError:
        logger.debug("  overnight_macro_collector import 실패")
    except Exception as e:
        logger.warning(f"  Overnight Macro 실패: {e}")

    # ── 2. OIS (Overnight Intelligence Score) 계산 ──
    try:
        from src.intelligence.overnight_intelligence import OvernightIntelligenceScore
        ois_engine = OvernightIntelligenceScore()
        ois_result = ois_engine.calculate(include_premarket=False)

        # signal_cache에 OIS 반영 (★ NaN 방어)
        import math as _m_ois2
        def _ois_safe(val, default=50.0):
            """OIS 값 NaN 방어."""
            if not isinstance(val, (int, float)) or _m_ois2.isnan(val):
                return default
            return val
        _update_signal_cache({
            'ois': _ois_safe(ois_result.get('ois', 50)),
            'ois_price': _ois_safe(ois_result.get('ois_price', 50)),
            'ois_sentiment': _ois_safe(ois_result.get('ois_sentiment', 50)),
            'ois_threshold_adj': ois_result.get('threshold_adj', 0),
        })

        # ★ overnight_intel을 signal_cache에 저장 (S1 Edge가 참조)
        try:
            from src.data.market_data_bridge import MarketDataBridge
            bridge = MarketDataBridge()
            overnight_intel = bridge.build_overnight_intel()
            _update_signal_cache({
                'overnight_intel': overnight_intel,
            })
            sp_chg = overnight_intel.get('sp500_change_pct', 0)
            nq_chg = overnight_intel.get('nasdaq_change_pct', 0)
            sox_chg = overnight_intel.get('sox_change_pct', 0)
            logger.info(f"  ✅ Overnight Intel → signal_cache: "
                        f"S&P={sp_chg:+.2f}%, NQ={nq_chg:+.2f}%, SOX={sox_chg:+.2f}%")
        except Exception as oi_err:
            logger.warning(f"  Overnight Intel 캐시 실패: {oi_err}")

        # ★ overnight_intel을 파일로도 저장 (OIS 소실 방지)
        try:
            _oi_dir = _PROJECT_ROOT / 'data' / 'raw' / 'overnight_intel'
            _oi_dir.mkdir(parents=True, exist_ok=True)
            _oi_file = _oi_dir / f'{date.today().isoformat()}.json'
            _oi_data = ois_result.copy() if ois_result else {}
            _oi_data.update(overnight_intel)
            atomic_write_json(_oi_file, _oi_data, indent=2)
            logger.info(f"  💾 overnight_intel → {_oi_file.name}")
        except Exception as _oi_err:
            logger.debug(f'overnight_intel 파일 저장 실패: {_oi_err}')

        results['ois'] = _ois_safe(ois_result.get('ois', 50))
        results['ois_price'] = _ois_safe(ois_result.get('ois_price', 50))
        results['ois_sentiment'] = _ois_safe(ois_result.get('ois_sentiment', 50))
        results['ois_label'] = ois_result.get('sentiment', 'neutral')
        results['ois_threshold_adj'] = ois_result.get('threshold_adj', 0)
        logger.info(f"  ✅ OIS: {ois_result['ois']:.1f}/100 "
                    f"→ {ois_result['sentiment']} "
                    f"(threshold {ois_result['threshold_adj']:+d})")
    except ImportError:
        logger.debug("  overnight_intelligence import 실패")
    except Exception as e:
        logger.warning(f"  OIS 계산 실패: {e}")

    # ── 3. US Market Regime 판정 ──
    try:
        from src.intelligence.us_market_regime import run_us_regime
        us_regime = run_us_regime()
        if us_regime:
            _update_signal_cache({
                'us_regime': us_regime.get('regime', 'neutral'),
                'us_regime_score': us_regime.get('score', 0),
                'us_regime_confidence': us_regime.get('confidence', 0),
            })
            results['us_regime'] = us_regime.get('regime', 'neutral')
            results['us_regime_confidence'] = us_regime.get('confidence', 0)
            results['us_regime_score'] = us_regime.get('score', 0)
            logger.info(f"  ✅ US Regime: {us_regime.get('regime')} "
                        f"(conf={us_regime.get('confidence', 0):.1%})")
    except ImportError:
        logger.debug("  us_market_regime import 실패")
    except Exception as e:
        logger.warning(f"  US Regime 실패: {e}")

    # ── 3.5 S1 Edge 필수 데이터 수집 (VKOSPI, SOX, stock_technicals) ──
    try:
        s1_cache_updates = {}

        # SOX 변동률: overnight_intel에서 추출 → 최상위 키로 노출
        oi = results.get('overnight_intel', {})
        if not oi:
            _sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if _sc_path.exists():
                _sc = json.loads(_sc_path.read_text())
                oi = _sc.get('overnight_intel', {})
        if oi:
            s1_cache_updates['sox_change'] = oi.get('sox_change_pct', 0)

        # VKOSPI + KOSPI200/KOSDAQ150: pykrx로 수집
        try:
            # ★ pykrx 지수 API 안전 래퍼 사용 (KeyError 방어 + yfinance fallback)
            # VKOSPI
            _vk_val = _safe_get_index_close('1004')
            if _vk_val is not None:
                s1_cache_updates['vkospi'] = _vk_val
                logger.info(f"  ✅ VKOSPI: {_vk_val:.2f}")

            # KOSPI200
            _k200_val = _safe_get_index_close('1028')
            if _k200_val is not None:
                s1_cache_updates['kospi200'] = _k200_val

            # KOSDAQ150
            _kq_val = _safe_get_index_close('2203')
            if _kq_val is not None:
                s1_cache_updates['kosdaq150'] = _kq_val
        except Exception as pyk_err:
            logger.warning(f"  지수 데이터 수집 실패: {pyk_err}")

        # stock_technicals (삼성전자/하이닉스 RSI, MACD, 거래량비)
        try:
            from pykrx import stock as _pykrx2
            from datetime import timedelta as _td3
            _end = datetime.now().strftime('%Y%m%d')
            _start = (datetime.now() - _td3(days=30)).strftime('%Y%m%d')

            stock_techs = {}
            for _ticker, _name in [('005930', '삼성전자'), ('000660', 'SK하이닉스')]:
                _ohlcv = _pykrx2.get_market_ohlcv_by_date(_start, _end, _ticker)
                if _ohlcv is not None and len(_ohlcv) >= 14:
                    closes = _ohlcv['종가'].values.astype(float)
                    volumes = _ohlcv['거래량'].values.astype(float)

                    # RSI-14
                    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                    gains = [max(0, d) for d in deltas[-14:]]
                    losses = [abs(min(0, d)) for d in deltas[-14:]]
                    avg_gain = sum(gains) / 14
                    avg_loss = sum(losses) / 14
                    rs = avg_gain / avg_loss if avg_loss > 0 else 100
                    rsi = 100 - (100 / (1 + rs))

                    # MACD (12-26-9)
                    def _ema(data, span):
                        mult = 2 / (span + 1)
                        e = data[0]
                        for v in data[1:]:
                            e = v * mult + e * (1 - mult)
                        return e
                    ema12 = _ema(list(closes[-26:]), 12)
                    ema26 = _ema(list(closes[-26:]), 26)
                    macd_hist = ema12 - ema26

                    # 거래량 비율 (최근 5일 평균 대비 당일)
                    vol_avg5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else volumes[-1]
                    vol_ratio = volumes[-1] / vol_avg5 if vol_avg5 > 0 else 1.0

                    stock_techs[_ticker] = {
                        'rsi_14': round(rsi, 2),
                        'macd_hist': round(macd_hist, 2),
                        'volume_ratio': round(vol_ratio, 2),
                        'name': _name,
                        'close': float(closes[-1]),
                    }
                    logger.info(f"  ✅ {_name} Tech: RSI={rsi:.1f}, MACD_H={macd_hist:.0f}, "
                                f"VolRatio={vol_ratio:.2f}")

            if stock_techs:
                s1_cache_updates['stock_technicals'] = stock_techs
        except Exception as tech_err:
            logger.debug(f"  stock_technicals 수집 실패: {tech_err}")

        if s1_cache_updates:
            _update_signal_cache(s1_cache_updates)
            logger.info(f"  ✅ S1 Edge 데이터: {len(s1_cache_updates)}개 항목 → signal_cache")
    except Exception as e:
        logger.warning(f"  S1 Edge 데이터 수집 실패: {e}")

    # ── 4. 이벤트 캘린더 체크 ──
    try:
        from src.intelligence.event_market_filter import EventCalendar
        cal = EventCalendar()
        # 오늘 + 향후 3일 이벤트
        from datetime import timedelta as _td
        upcoming = []
        for d in range(4):
            check_date = (datetime.now() + _td(days=d)).strftime('%Y-%m-%d')
            events = cal.get_events(check_date)
            for e in events:
                e['date'] = check_date
                upcoming.append(e)
        if upcoming:
            events_summary = [f"{e.get('name', e.get('event', '?'))}({e['date']})" for e in upcoming[:3]]
            _update_signal_cache({
                'upcoming_events': len(upcoming),
                'next_event': upcoming[0].get('name', upcoming[0].get('event', '')),
                'event_confidence_adj': upcoming[0].get('confidence_adj',
                    upcoming[0].get('confidence_reduction', 0)),
            })
            results['events'] = len(upcoming)
            logger.info(f"  ✅ 이벤트: {', '.join(events_summary)}")
        else:
            logger.info("  ✅ 이벤트: 3일 내 예정 없음")
    except ImportError:
        logger.debug("  event_market_filter import 실패")
    except Exception as e:
        logger.warning(f"  이벤트 캘린더 실패: {e}")

    # ── 4.5 뉴스 감성 분석 ──
    try:
        from src.intelligence.naver_news_sentiment import collect_news_sentiment
        news_result = collect_news_sentiment()
        results['news_sentiment'] = news_result.get('sentiment', 0)
        logger.info(f"  ✅ 뉴스 감성: {news_result.get('label', '?')} "
                    f"({news_result.get('sentiment', 0):+.3f}, "
                    f"{news_result.get('n_articles', 0)}건)")
    except ImportError:
        logger.debug("  naver_news_sentiment import 실패")
    except Exception as e:
        logger.warning(f"  뉴스 감성 수집 실패: {e}")

    # ── 5. Market Shock 감지 ──
    try:
        from src.intelligence.market_shock_detector import MarketShockDetector
        detector = MarketShockDetector()
        shock = detector.detect()
        if shock and shock.get('shocks'):
            severity = shock.get('severity', 'none')
            _update_signal_cache({
                'market_shock': severity,
                'shock_count': len(shock.get('shocks', [])),
            })
            results['shock'] = severity
            logger.info(f"  ⚠️ Market Shock: {severity} "
                        f"({len(shock['shocks'])}건)")
        else:
            _update_signal_cache({'market_shock': 'none'})
            logger.info("  ✅ Market Shock: 없음")
    except ImportError:
        logger.debug("  market_shock_detector import 실패")
    except Exception as e:
        logger.warning(f"  Shock 감지 실패: {e}")

    # ── 6. Cross-Asset 시그널 ──
    try:
        from src.intelligence.cross_asset_signals import CrossAssetSignalEngine
        engine = CrossAssetSignalEngine()
        signals = engine.generate_signals()
        if signals:
            _update_signal_cache({
                'cross_asset_direction': signals.get('direction', 'neutral'),
                'cross_asset_score': signals.get('score', 0),
            })
            results['cross_asset'] = signals.get('direction', 'neutral')
            logger.info(f"  ✅ Cross-Asset: {signals.get('direction')} "
                        f"(score={signals.get('score', 0):.2f})")
    except ImportError:
        logger.debug("  cross_asset_signals import 실패")
    except Exception as e:
        logger.warning(f"  Cross-Asset 실패: {e}")

    # 결과 저장
    results['timestamp'] = datetime.now().isoformat()
    overnight_file = _PROJECT_ROOT / 'results' / 'overnight_result.json'
    overnight_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(overnight_file, results, indent=2)

    # ── ★ pipeline_state.json 통합 저장 ──
    # Meridian 원칙: 분산 레짐 데이터를 단일 정규 파일로 통합
    # 소비자: exposure_orchestrator, medallion_orchestrator, train_ensemble,
    #         edge_stream, dashboard
    try:
        state_file = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
        prev_state = {}
        if state_file.exists():
            try:
                prev_state = json.loads(state_file.read_text())
            except Exception:
                pass

        current_us_regime = results.get('us_regime', 'caution')
        prev_us_regime = prev_state.get('us_regime', prev_state.get('regime'))

        # ★ 기존 kr_regime 보존 (regime_engine이 premarket에서 갱신)
        kr_regime = prev_state.get('kr_regime')
        kr_confidence = prev_state.get('kr_regime_confidence')
        kr_scores = prev_state.get('kr_regime_scores')

        pipeline_state = {
            # US Market Regime (야간 확정)
            'us_regime': current_us_regime,
            'us_prev_regime': prev_us_regime,
            'us_regime_confidence': results.get('us_regime_confidence', 0),
            'us_regime_score': results.get('us_regime_score', 0),
            'us_regime_source': 'us_market_regime',

            # KR Market Regime (premarket phase에서 regime_engine이 갱신)
            'kr_regime': kr_regime,
            'kr_prev_regime': prev_state.get('kr_prev_regime'),
            'kr_regime_confidence': kr_confidence,
            'kr_regime_scores': kr_scores,

            # OIS
            'ois': results.get('ois', 50),
            'ois_label': results.get('ois_label', 'neutral'),
            'ois_threshold_adj': results.get('ois_threshold_adj', 0),

            # 장중 레짐 (intraday phase에서 갱신)
            'intraday_regime': prev_state.get('intraday_regime'),

            # Operating Regime (regime_engine._compute_operating_regime이 갱신)
            'operating_regime': prev_state.get('operating_regime'),

            # 메타
            'updated_at': datetime.now().isoformat(),
            'updated_by': 'daily_pipeline.overnight',
        }

        # 하위 호환: 'regime' 키 유지 (기존 소비자용)
        pipeline_state['regime'] = current_us_regime

        atomic_write_json(state_file, pipeline_state, indent=2)

        # ★ signal_cache에도 kr_regime 반영 (대시보드 연결)
        _update_signal_cache({
            'kr_regime': kr_regime,
            'us_regime': current_us_regime,
        })

        logger.info(f"  ✅ pipeline_state: us={current_us_regime}"
                     f" kr={kr_regime}"
                     f" (prev_us={prev_us_regime})")
    except Exception as e:
        logger.warning(f"  pipeline_state 저장 실패: {e}")

    return results


# ═══════════════════════════════════════════════════════
# Phase 1: COLLECT (06:30 KST)
# ═══════════════════════════════════════════════════════

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


def _phase_collect_flow():
    """투자자 수급 수집 (16:30 KST)."""
    logger.info("  📈 투자자 수급 수집")
    try:
        from src.data_collection.investor_flow_collector import InvestorFlowCollector
        collector = InvestorFlowCollector()

        # ★ 포트폴리오 보유 종목 + 주요 지수 ETF 대상 배치 수집
        tickers = set()
        _shadow_f = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
        if _shadow_f.exists():
            _sp = json.loads(_shadow_f.read_text())
            for pos in _sp.get('positions', {}).values():
                tk = pos.get('ticker', '')
                if tk:
                    tickers.add(tk)
        # 주요 ETF 추가 (DynamicConfig)
        _core_flow_etfs = cfg.get('flow.core_etf_tickers', ['069500', '122630', '252670', '133690'])
        tickers.update(_core_flow_etfs)
        _max_batch = cfg.get('flow.max_batch_tickers', 20)
        tickers = list(tickers)[:_max_batch]

        results = collector.collect_batch_features(tickers)
        logger.info(f"  ✅ 수급 수집 완료: {len(results)}/{len(tickers)}종목")

        # ★ 수급 데이터 → signal_cache 반영 (KR Market Factor 연동)
        if results:
            # 대표 지수(KOSPI 069500) 수급을 기준으로 market-wide factor 추출
            # ★ collect_batch_features는 Dict[str, DataFrame] 반환
            _kospi_df = results.get('069500')
            _kv = {}
            if _kospi_df is not None and len(_kospi_df) > 0:
                _last = _kospi_df.iloc[-1]
                _kv = {k: float(v) if hasattr(v, '__float__') else v
                       for k, v in _last.to_dict().items()}

            # 종목별 수급도 by_ticker에 저장 (S2 ML feature용)
            _by_ticker = {}
            for _tk, _tk_df in results.items():
                if _tk_df is not None and len(_tk_df) > 0:
                    _tk_last = _tk_df.iloc[-1]
                    _by_ticker[_tk] = {k: float(v) if hasattr(v, '__float__') else v
                                       for k, v in _tk_last.to_dict().items()}

            flow_cache = {
                'investor_flow': {
                    'foreign_net_5d': _kv.get('foreign_net_5d', 0),
                    'foreign_net_20d': _kv.get('foreign_net_20d', 0),
                    'inst_net_5d': _kv.get('inst_net_5d', 0),
                    'inst_net_20d': _kv.get('inst_net_20d', 0),
                    'flow_momentum': _kv.get('flow_momentum', 0),
                    'foreign_streak': _kv.get('foreign_streak', 0),
                    'supply_demand_score': _kv.get('supply_demand_score', 0),
                    'collected_at': datetime.now().isoformat(),
                    'n_tickers': len(results),
                    'by_ticker': _by_ticker,
                },
            }
            _update_signal_cache(flow_cache)
            logger.info(f"  ✅ 수급 → signal_cache: "
                        f"foreign_5d={_kv.get('foreign_net_5d', 0):+.2f}, "
                        f"streak={_kv.get('foreign_streak', 0)}")
    except Exception as e:
        logger.warning(f"  수급 수집 실패: {e}")

    # ★ KR Market Factor 통합 (수급 + 대형주 모멘텀 → 스트림별 조정)
    try:
        from src.intelligence.kr_market_factor import KRMarketFactorEngine
        factor_engine = KRMarketFactorEngine()
        factor_result = factor_engine.compute()
        if factor_result:
            _update_signal_cache({
                'kr_factor_composite': factor_result.get('composite_score', 0),
                'kr_factor_foreign': factor_result.get('foreign_score', 0),
                'kr_factor_lcap': factor_result.get('large_cap_momentum', 0),
                'kr_stream_adjustments': factor_result.get('stream_adjustments', {}),
            })
            logger.info(f"  🇰🇷 KR Factor: composite={factor_result['composite_score']:+.3f}")
    except ImportError:
        logger.debug("  kr_market_factor import 실패")
    except Exception as e:
        logger.warning(f"  KR Factor 실패: {e}")



def _phase_evening_data():
    """이브닝 데이터 수집 (17:00 KST).

    ★ Pipeline Timing Optimization (2026-05-29)
    한국 장후 확정 데이터 + 아시아 지표 수집.
    FRED/크로스마켓은 모닝(06:00)으로 이동.

    수집 범위:
      1. KR ETF/개별종목 확정 종가 (pykrx)
      2. 섹터 배치 (상관관계 확정)
      3. 시장 브레드스 (VKOSPI, Put/Call)
      4. 외국인/기관 수급
      5. 이브닝 아시아 지표 (TAIEX, Nikkei, HangSeng)
      6. KR 기술적 지표 (삼성전자/하이닉스 RSI, MACD — 확정가 기준)
      7. 유니버스 갱신
    """
    logger.info("  🌆 이브닝 데이터 수집 (KR 확정 + 아시아)")

    # KR 확정 데이터 + 아시아 지표 수집
    try:
        from src.data_collection.unified_collector import run_daily
        results = run_daily(mode='evening')
        logger.info(f"  ✅ 이브닝 수집 완료: {results.get('elapsed_sec', '?')}초")
    except Exception as e:
        logger.warning(f"  이브닝 수집 실패: {e}")

    # KR 기술적 지표 (확정가 기준 RSI/MACD)
    try:
        from pykrx import stock as _pykrx
        from datetime import timedelta as _td3
        _end = datetime.now().strftime('%Y%m%d')
        _start = (datetime.now() - _td3(days=30)).strftime('%Y%m%d')

        s1_cache_updates = {}
        stock_techs = {}
        for _ticker, _name in [('005930', '삼성전자'), ('000660', 'SK하이닉스')]:
            _ohlcv = _pykrx.get_market_ohlcv_by_date(_start, _end, _ticker)
            if _ohlcv is not None and len(_ohlcv) >= 14:
                closes = _ohlcv['종가'].values.astype(float)
                volumes = _ohlcv['거래량'].values.astype(float)
                # RSI-14
                deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
                gains = [max(0, d) for d in deltas[-14:]]
                losses = [abs(min(0, d)) for d in deltas[-14:]]
                avg_gain = sum(gains) / 14
                avg_loss = sum(losses) / 14
                rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 50
                # Volume ratio
                vol_avg = float(volumes[-20:].mean()) if len(volumes) >= 20 else 1
                vol_ratio = float(volumes[-1] / vol_avg) if vol_avg > 0 else 1
                stock_techs[_ticker] = {
                    'rsi_14': round(rsi, 1), 'volume_ratio': round(vol_ratio, 2), 'name': _name,
                }
        if stock_techs:
            s1_cache_updates['stock_technicals'] = stock_techs

        # VKOSPI + KOSPI200 + KOSDAQ150 (안전 래퍼)
        _vk_val = _safe_get_index_close('1004')
        if _vk_val is not None:
            s1_cache_updates['vkospi'] = _vk_val
        _k200_val = _safe_get_index_close('1028')
        if _k200_val is not None:
            s1_cache_updates['kospi200'] = _k200_val
            s1_cache_updates['kospi_close'] = _k200_val
        _kq_val = _safe_get_index_close('2203')
        if _kq_val is not None:
            s1_cache_updates['kosdaq150'] = _kq_val
            s1_cache_updates['kosdaq_close'] = _kq_val

        if s1_cache_updates:
            _update_signal_cache(s1_cache_updates)
            logger.info(f"  ✅ KR 기술적 지표: {len(s1_cache_updates)}개 → signal_cache")
    except Exception as e:
        logger.debug(f"  KR 기술적 지표 실패: {e}")


def _phase_collect_dart():
    """DART 공시 수집 (19:00 KST)."""
    logger.info("  📄 DART 공시 수집")
    try:
        from src.data_collection.dart_daily_collector import DARTDailyCollector
        collector = DARTDailyCollector()
        result = collector.collect_incremental()
        logger.info(f"  ✅ DART 수집: {result}")
    except Exception as e:
        logger.warning(f"  DART 수집 실패: {e}")

    # DART 상세 (A 고유)
    try:
        from src.data_collection.dart_collector import run_dart_collection
        run_dart_collection()
        logger.info("  ✅ DART 상세 수집")
    except ImportError:
        logger.debug("  dart_collector (A 고유) import 실패")
    except Exception as e:
        logger.debug(f"  DART 상세 실패: {e}")

    # DART 수집 후 컨센서스도 수집 (같은 phase에서 실행)
    _phase_collect_consensus()


def _phase_collect_consensus():
    """애널리스트 컨센서스 수집 (DART 수집 후 실행).

    네이버 금융에서 포트폴리오 보유 종목의 목표가/투자의견/EPS수정 수집.
    data/analyst_consensus/{ticker}.json 에 저장.
    S4 Advisory confidence 보정에 사용됨.
    """
    if not cfg.get('s4.consensus_enrichment_enabled', True):
        logger.info("  ⏭️ 컨센서스 수집 비활성화 (s4.consensus_enrichment_enabled=False)")
        return

    logger.info("  📊 애널리스트 컨센서스 수집")
    try:
        from scripts.collect_analyst_consensus import collect_all
        result = collect_all(include_qvm_top=False)
        logger.info(
            f"  ✅ 컨센서스 수집: {result.get('collected', 0)}건 성공, "
            f"{result.get('skipped', 0)}건 데이터없음, "
            f"{result.get('failed', 0)}건 실패")
    except ImportError as e:
        logger.warning(f"  컨센서스 수집 import 실패: {e}")
    except Exception as e:
        logger.warning(f"  컨센서스 수집 실패: {e}")



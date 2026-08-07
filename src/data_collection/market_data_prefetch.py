import pandas as pd
'\n★ Market Data Pre-fetch — 거래 시간 전 데이터 사전 적재 [Bias-Fix Root Cause]\n===========================================================================\n근원적 해결:\n  - 거래 시간 중 on-demand yfinance 호출 → 실패 가능 (급락 시 부하)\n  - 대신 morning pipeline(06:00~08:30)에서 미리 로컬 캐시 생성\n  - 거래 로직은 캐시에서만 읽음 → 네트워크 실패와 완전 분리\n\n적재 데이터:\n  - ATR(14일) per ETF ticker  → results/atr_cache.json\n  - VIX 252일 이력            → data/cache/vix_daily.json\n\nAuthor: Project-A | Date: 2026-04-18\n'
import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from src.utils.time_utils import today_kst, now_kst
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ATR_CACHE = PROJECT_ROOT / 'results' / 'atr_cache.json'
VIX_CACHE = PROJECT_ROOT / 'data' / 'cache' / 'vix_daily.json'
_DEFAULT_ETF_TICKERS = ['069500', '122630', '252670', '114800', '229200', '233740', '251340', '102110', '278540', '091160', '091180', '139290', '308620', '143850', '133690']

def _get_etf_universe() -> List[str]:
    """
    [R-03] Universe 동적 로드 — universe_cache.json에서 ETF 타겟 확장.

    우선순위:
      1. data/universe_cache.json (type='etf' 또는 관련 타입 필터)
      2. results/l1_daytrader_state.json (날마다 다른 ETF만 사용 중일 때)
      3. _DEFAULT_ETF_TICKERS (모듈 기본값 fallback)

    신규 ETF가 universe에 추가되면 재배포 없이 자동으로 ATR pre-fetch 대상에 포함됨.

    Returns:
        종목코드 리스트 (중복 제거, _DEFAULT_ETF_TICKERS 병합)
    """
    tickers: set = set()
    cache_file = PROJECT_ROOT / 'data' / 'universe_cache.json'
    if cache_file.exists():
        try:
            raw = json.loads(cache_file.read_text(encoding='utf-8'))
            universe = raw.get('universe', raw if isinstance(raw, dict) else {})
            _etf_types = {'etf', 'bond_etf', 'index_etf', 'inverse_etf', 'leveraged_etf', 'ETF', 'KR_ETF'}
            for ticker, info in universe.items():
                if isinstance(info, dict):
                    asset_type = info.get('type', info.get('asset_type', ''))
                    if asset_type in _etf_types:
                        tickers.add(ticker.strip())
                elif isinstance(info, str) and 'etf' in info.lower():
                    tickers.add(ticker.strip())
            if tickers:
                logger.debug(f'  ETF 유니버스 로드 (universe_cache): {len(tickers)}종목')
                return list(tickers | set(_DEFAULT_ETF_TICKERS))
        except Exception as e:
            logger.error(f'  universe_cache 로드 실패: {e}', exc_info=True)
    state_file = PROJECT_ROOT / 'results' / 'l1_daytrader_state.json'
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding='utf-8'))
            t = state.get('ticker', '')
            if t and len(t) == 6:
                tickers.add(t)
        except Exception as e:
            logger.warning(f'  suppressed: {e}', exc_info=True)
    return list(set(_DEFAULT_ETF_TICKERS) | tickers)

def _atomic_write_json(path: Path, data: dict) -> None:
    """
    JSON을 임시 파일에 먼저 쓴 후 원자적으로 교체.
    쓰기 도중 충돌/OOM 시 기존 파일이 손상되지 않음.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
        try:
            os.unlink(tmp_path)
        except OSError:
            logger.error('[SILENT_BYPASS] Suppressed exception at market_data_prefetch.py:128', exc_info=True)
        raise

def prefetch_vix(window: int=756, force: bool=False) -> Dict:
    """
    VIX 최근 3년 일별 데이터를 data/cache/vix_daily.json에 저장.

    거래 로직은 이 파일에서만 읽으므로 장중 네트워크 실패 무관.

    Args:
        window: 수집할 최대 일수 (기본 756 = 3년)
        force:  오늘 이미 수집해도 강제 재수집

    Returns:
        {'ok': bool, 'n_rows': int, 'latest_vix': float, 'saved_at': str}
    """
    from src.utils.time_utils import today_kst
    if not force and VIX_CACHE.exists():
        try:
            cached = json.loads(VIX_CACHE.read_text())
            saved_date = cached.get('saved_date', '')
            if saved_date == today_kst().isoformat():
                n = len(cached.get('vix_values', []))
                logger.info(f'  VIX 캐시 유효 (오늘 {saved_date}, {n}일 이력) → 스킵')
                return {'ok': True, 'n_rows': n, 'latest_vix': cached.get('latest_vix', 0.0), 'saved_at': saved_date, 'source': 'cache'}
        except (json.JSONDecodeError, OSError):
            from src.utils.error_logger import log_error_rate_limited
            logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
    logger.info('  [Pre-fetch] VIX 데이터 수집 시작...')
    try:
        import yfinance as yf
        import pandas as _pd
        period = f'{min(window // 252 + 1, 3)}y'
        df = yf.download('^VIX', period=period, interval='1d', progress=False, auto_adjust=False)
        if df is None or df.empty or len(df) < 20:
            raise ValueError(f'VIX 데이터 부족: {(len(df) if df is not None else 0)}행')
        if isinstance(df.columns, _pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        close_col = 'Close' if 'Close' in df.columns else df.columns[0]
        series = df[close_col].dropna().tail(window)
        vix_records = [{'date': str(idx.date() if hasattr(idx, 'date') else idx)[:10], 'close': round(float(v), 4)} for idx, v in series.items() if v > 0]
        latest_vix = float(series.iloc[-1]) if len(series) > 0 else 0.0
        cache_data = {'saved_date': today_kst().isoformat(), 'saved_at': now_kst().isoformat(), 'source': 'yfinance', 'n_rows': len(vix_records), 'latest_vix': round(latest_vix, 4), 'vix_values': [r['close'] for r in vix_records], 'vix_records': vix_records}
        _atomic_write_json(VIX_CACHE, cache_data)
        logger.info(f'  ✅ VIX 캐시 저장: {len(vix_records)}일 (최신 {latest_vix:.2f}) → {VIX_CACHE}')
        return {'ok': True, 'n_rows': len(vix_records), 'latest_vix': latest_vix, 'saved_at': today_kst().isoformat(), 'source': 'yfinance'}
    except Exception as e:
        logger.error(f'  ❌ VIX pre-fetch 실패: {e}', exc_info=True)
        return _fallback_vix_from_overnight(error=str(e))

def _fallback_vix_from_overnight(error: str='') -> Dict:
    """overnight_intelligence_history에서 VIX 이력 추출 후 캐시 갱신."""
    hist_file = PROJECT_ROOT / 'results' / 'overnight_intelligence_history.json'
    if not hist_file.exists():
        try:
            cached = json.loads(VIX_CACHE.read_text())
            n = len(cached.get('vix_records', []))
            if n > 0:
                return {'ok': True, 'n_rows': n, 'latest_vix': cached.get('latest_vix', 0.0), 'saved_at': cached.get('saved_at', ''), 'source': 'cache_fallback'}
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at market_data_prefetch.py:230', exc_info=True)
        return {'ok': False, 'error': error, 'n_rows': 0}
    try:
        raw = hist_file.read_text()
        data = json.loads(raw)
        history = data.get('history', [])
        vix_list = []
        for rec in history:
            v = (rec.get('us_market', {}) or {}).get('vix', {})
            val = v.get('close') if isinstance(v, dict) else None
            d_str = rec.get('date', '')
            if val and float(val) > 0 and d_str:
                vix_list.append({'date': d_str[:10], 'close': round(float(val), 4)})
        if not vix_list:
            return {'ok': False, 'error': 'overnight history에 VIX 없음', 'n_rows': 0}
        cache_data = {'saved_date': today_kst().isoformat(), 'saved_at': now_kst().isoformat(), 'source': 'overnight_history_fallback', 'n_rows': len(vix_list), 'latest_vix': vix_list[-1]['close'], 'vix_values': [r['close'] for r in vix_list], 'vix_records': vix_list, 'original_error': error}
        _atomic_write_json(VIX_CACHE, cache_data)
        logger.info(f'  ⚠️ VIX fallback(overnight): {len(vix_list)}일 저장')
        return {'ok': True, 'n_rows': len(vix_list), 'latest_vix': vix_list[-1]['close'], 'source': 'overnight_fallback'}
    except Exception as e2:
        logger.error(f'  ❌ VIX fallback도 실패: {e2}', exc_info=True)
        return {'ok': False, 'error': f'{error} / {e2}', 'n_rows': 0}

def load_vix_from_cache() -> List[float]:
    """
    캐시에서 VIX 이력 로드. adaptive_thresholds에서만 이 함수로 읽음.
    캐시가 오늘 데이터가 아니면 WARNING (장 시작 전 prefetch 미실행 의심).
    """
    if not VIX_CACHE.exists():
        logger.warning('  VIX 캐시 없음 — morning pre-fetch 실행 여부 확인 필요')
        return []
    try:
        data = json.loads(VIX_CACHE.read_text())
        saved_date = data.get('saved_date', '')
        vix_values = data.get('vix_values', [])
        if saved_date != today_kst().isoformat():
            try:
                days_old = (today_kst() - date.fromisoformat(saved_date)).days
            except ValueError:
                days_old = 99
            if days_old > 3:
                logger.warning(f'  VIX 캐시 {days_old}일 경과 ({saved_date}) — morning pre-fetch가 실행되지 않은 것으로 의심됨')
            else:
                logger.debug(f'  VIX 캐시: {days_old}일 전 데이터 ({saved_date}) — 허용')
        return [float(v) for v in vix_values if v and float(v) > 0]
    except json.JSONDecodeError as e:
        logger.error(f'  VIX 캐시 JSON 손상: {e} — pre-fetch 재실행 필요', exc_info=True)
        return []
    except OSError as e:
        logger.error(f'  VIX 캐시 읽기 실패: {e}', exc_info=True)
        return []

def prefetch_atr(tickers: Optional[List[str]]=None, force: bool=False, max_age_hours: int=20) -> Dict:
    """
    유니버스의 모든 ETF 종목에 대해 ATR(14일)을 사전 계산하여 캐시 저장.

    거래 로직(_compute_tp_sl, _l3_tp_sl)은 캐시에서만 읽음.
    장중 yfinance 호출 완전 제거.

    Args:
        tickers:        대상 종목 리스트 (None이면 기본 ETF 목록)
        force:          오늘 이미 수집해도 강제 재수집
        max_age_hours:  캐시 최대 허용 만료 시간 (기본 20h → 하루 1회 보장)

    Returns:
        {'ok': bool, 'n_success': int, 'n_fail': int, 'tickers': [...]}
    """
    tickers = tickers or _get_etf_universe()
    if not force and ATR_CACHE.exists():
        try:
            cache = json.loads(ATR_CACHE.read_text())
            saved_at_str = cache.get('prefetch_saved_at', '')
            if saved_at_str:
                saved_at = datetime.fromisoformat(saved_at_str)
                hours_old = (now_kst() - saved_at).total_seconds() / 3600
                if hours_old < max_age_hours:
                    n = len([k for k in cache if not k.startswith('_')])
                    logger.info(f'  ATR 캐시 유효 ({hours_old:.1f}h 경과, {n}종목) → 스킵')
                    return {'ok': True, 'n_success': n, 'n_fail': 0, 'source': 'cache', 'age_hours': hours_old}
        except (json.JSONDecodeError, ValueError, OSError):
            logger.warning('[SILENT_BYPASS] Suppressed exception at market_data_prefetch.py:344', exc_info=True)
    logger.info(f'  [Pre-fetch] ATR 수집 시작: {len(tickers)}종목')
    try:
        import FinanceDataReader as fdr
    except ImportError as e:
        logger.error(f'  FinanceDataReader 미설치: {e}', exc_info=True)
        return {'ok': False, 'n_success': 0, 'n_fail': len(tickers), 'error': str(e)}
    existing_cache: Dict = {}
    if ATR_CACHE.exists():
        try:
            existing_cache = json.loads(ATR_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            existing_cache = {}
    success_list, fail_list = ([], [])
    new_entries: Dict = {}
    for ticker in tickers:
        try:
            start_date = (today_kst() - timedelta(days=45)).strftime('%Y-%m-%d')
            df = fdr.DataReader(ticker, start=start_date)
            if df is None or df.empty or len(df) < 15:
                raise ValueError(f'데이터 부족: {(len(df) if df is not None else 0)}행')
            close_col = 'Close' if 'Close' in df.columns else df.columns[0]
            prices = df[close_col].dropna().tolist()
            if len(prices) < 15:
                raise ValueError(f'유효 종가 부족: {len(prices)}개')
            rets = [abs(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
            atr = float(sum(rets[-14:]) / min(14, len(rets)))
            new_entries[ticker] = {'atr': round(atr, 6), 'saved_at': today_kst().isoformat(), 'n_days': len(prices)}
            success_list.append(ticker)
            logger.debug(f'    ✅ {ticker}: ATR={atr:.4f}')
        except Exception as e:
            fail_list.append(ticker)
            logger.warning(f'    ⚠️ {ticker} ATR 수집 실패: {type(e).__name__}: {e}', exc_info=True)
            if ticker in existing_cache:
                new_entries[ticker] = existing_cache[ticker]
                new_entries[ticker]['kept_from_cache'] = True
    new_entries['_meta'] = {'prefetch_saved_at': now_kst().isoformat(), 'n_success': len(success_list), 'n_fail': len(fail_list), 'failed_tickers': fail_list}
    for k, v in existing_cache.items():
        if k not in new_entries and (not k.startswith('_')):
            new_entries[k] = v
    _atomic_write_json(ATR_CACHE, new_entries)
    logger.info(f'  ✅ ATR pre-fetch 완료: {len(success_list)}/{len(tickers)}성공 ({len(fail_list)}실패) → {ATR_CACHE}')
    if fail_list:
        logger.warning(f'  실패 종목: {fail_list}')
    return {'ok': len(success_list) > 0, 'n_success': len(success_list), 'n_fail': len(fail_list), 'failed_tickers': fail_list, 'tickers': success_list}

def load_atr_from_cache(ticker: str, max_age_days: int=3) -> float:
    """
    캐시에서 ATR 로드.

    Args:
        ticker:       종목 코드 (KS suffix 없는 6자리)
        max_age_days: 최대 허용 캐시 경과일

    Returns:
        ATR 비율 (예: 0.012), 없거나 만료면 0.0
    """
    if not ATR_CACHE.exists():
        logger.warning(f'  ATR 캐시 없음 ({ticker}) — morning pre-fetch 미실행 가능성')
        return 0.0
    try:
        cache = json.loads(ATR_CACHE.read_text())
    except json.JSONDecodeError as e:
        logger.error(f'  ATR 캐시 JSON 손상: {e}', exc_info=True)
        return 0.0
    except OSError as e:
        logger.error(f'  ATR 캐시 읽기 실패: {e}', exc_info=True)
        return 0.0
    entry = cache.get(ticker, {})
    if not entry or 'atr' not in entry:
        logger.debug(f'  ATR 캐시에 {ticker} 없음')
        return 0.0
    try:
        saved = date.fromisoformat(entry.get('saved_at', '2000-01-01'))
        days_old = (today_kst() - saved).days
        if days_old > max_age_days:
            logger.warning(f'  ATR 캐시 만료: {ticker} ({days_old}일 경과, 저장={entry['saved_at']}) — morning pre-fetch 확인 필요')
            return 0.0
        return float(entry['atr'])
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f'  ATR 캐시 값 오류: {ticker}: {e}', exc_info=True)
        return 0.0

def run_prefetch(etf_tickers: Optional[List[str]]=None, force: bool=False) -> Dict:
    """
    morning pipeline Step에서 단일 호출로 모두 실행.

    usage (unified_daily_pipeline.py):
        from src.data_collection.market_data_prefetch import run_prefetch
        result = run_prefetch()

    Returns:
        {'vix': {...}, 'atr': {...}, 'ok': bool}
    """
    logger.info('=' * 60)
    logger.info('★ Market Data Pre-fetch (VIX + ATR)')
    logger.info('=' * 60)
    vix_result = prefetch_vix(force=force)
    atr_result = prefetch_atr(tickers=etf_tickers, force=force)
    overall_ok = vix_result.get('ok', False) and atr_result.get('ok', False)
    summary = {'ok': overall_ok, 'vix': vix_result, 'atr': atr_result, 'saved_at': now_kst().isoformat()}
    status = '✅' if overall_ok else '⚠️'
    logger.info(f'{status} Pre-fetch 완료: VIX={vix_result.get('n_rows', 0)}일 ATR={atr_result.get('n_success', 0)}/{len(etf_tickers or _get_etf_universe())}종목')
    return summary
if __name__ == '__main__':
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format='%(message)s')
    result = run_prefetch(force=True)
    logger.info(f'\n결과: VIX={result['vix'].get('n_rows')}일 | ATR 성공={result['atr'].get('n_success')}종목')

def validate_intraday_sources(test_ticker: str='069500') -> Dict:
    """
    [R-04] 장중 실시간 데이터 소스 안정성 검증.
    pre-market 페이즈(08:30~09:00)에서 호싈어 실제 장중 동작 시뮬레이션.

    필요성 배경:
    - pykrx는 KRX 공식 API가 아닌 크롤러 기반
    - 장중에 당일 데이터를 실시간 반환하는지 사전 검증 필요
    - 검증 실패 시 RealtimeDataBus만 pykrx CB를 사주에 OPEN으로 설정

    Returns:
        {
          'pykrx_intraday': bool,       # 장중 데이터 반환 여부
          'pykrx_data_fresh': bool,     # 당일 데이터인지 여부
          'kis_price': bool,            # KIS 현재가 조회 가능 여부
          'validation_at': str,
          'details': dict,
        }
    """
    result = {'pykrx_intraday': False, 'pykrx_data_fresh': False, 'kis_price': False, 'validation_at': now_kst().isoformat(), 'details': {}}
    today_str = now_kst().strftime('%Y%m%d')
    try:
        from pykrx import stock as pykrx_stock
        df = pykrx_stock.get_market_trading_volume_by_date(today_str, today_str, test_ticker)
        if df is not None and (not df.empty):
            result['pykrx_intraday'] = True
            if hasattr(df.index, 'strftime'):
                data_date = df.index[-1].strftime('%Y%m%d')
            else:
                data_date = str(df.index[-1])[:8].replace('-', '')
            result['pykrx_data_fresh'] = data_date == today_str
            result['details']['pykrx'] = {'data_date': data_date, 'today': today_str, 'is_fresh': result['pykrx_data_fresh'], 'shape': str(df.shape)}
            if not result['pykrx_data_fresh']:
                logger.warning(f'  ⚠️ [R-04] pykrx 장중 데이터 불일치: 데이터날={data_date}, 오늘={today_str} (KRX 30분 지연으로 당일 09:30 이후 첩신 예상)')
        else:
            result['details']['pykrx'] = {'error': 'empty_dataframe'}
            logger.warning(f'  ⚠️ [R-04] pykrx 장중 데이터 마음: 빈 DataFrame (09:30 이전 실행 or KRX 서버 지연)')
    except ImportError as e:
        result['details']['pykrx'] = {'error': 'pykrx_not_installed'}
        logger.error('  ⚠️ [R-04] pykrx 미설치 — 외국인수급 신호는 InvestorFlowCollector fallback 사용', exc_info=True)
    except Exception as e:
        result['details']['pykrx'] = {'error': str(e)[:100]}
        logger.warning(f'  ⚠️ [R-04] pykrx 장중 호숨 실패: {e}', exc_info=True)
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        kis = KISDataCollector()
        price = kis.get_current_price(test_ticker)
        if price and float(price) > 0:
            result['kis_price'] = True
            result['details']['kis'] = {'price': float(price)}
            logger.info(f'  ✅ [R-04] KIS 현재가 조회 성공: {test_ticker}={price:,}원')
    except Exception as e:
        result['details']['kis'] = {'error': str(e)[:100]}
        logger.warning(f'  ⚠️ [R-04] KIS API 현재가 호숨 실패: {e}', exc_info=True)
    pykrx_ok = result['pykrx_intraday']
    kis_ok = result['kis_price']
    logger.info(f'  [R-04] 장중 데이터 소스 검증: pykrx={('✅' if pykrx_ok else '❌')} (fresh={('✅' if result['pykrx_data_fresh'] else '⚠️')}), KIS={('✅' if kis_ok else '❌')}')
    validation_cache = PROJECT_ROOT / 'data' / 'cache' / 'source_validation.json'
    try:
        _atomic_write_json(validation_cache, result)
    except Exception as e:
        logger.warning(f'  suppressed: {e}', exc_info=True)
    return result
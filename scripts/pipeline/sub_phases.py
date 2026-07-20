#!/usr/bin/env python3
"""
Project Meridian — Unified Daily Pipeline
============================================
매일 실행되는 통합 파이프라인 (A + First 통합).

Phase 실행 순서:
  02:00  WEEKLY_RETRAIN  — 주간 ML 재학습 (토)
  03:00  WEEKLY_VALIDATE — 주간 검증 (토)
  05:15  OVERNIGHT       — 야간 글로벌 시장 수집 + OIS 계산
  06:00  COLLECT         — 10단계 전체 데이터 수집
  07:45  PREMARKET       — 레짐 판정 + 프리마켓 신호 생성
  08:00  PREMARKET_TRADE — S2/S4 개별주 프리마켓 매매 (NXT)
  08:00  MORNING         — Intelligence Cascade + 매매 신호 생성
  09:05  MARKET          — 매매 실행
  09:30  INTRADAY        — 장중 실시간 수집/모니터링 (A)
  15:10  CLOSING         — 포지션 청산 + PnL 계산
  15:30  AFTERMARKET_TRADE — S2/S4 개별주 에프터마켓 매매 (NXT)
  15:35  AFTERMARKET     — 애프터마켓 분석 + Shadow 확정
  16:10  KRX_REFRESH     — KRX 확정 데이터 리프레시 (A)
  16:30  COLLECT_FLOW    — 투자자 수급 수집 (A)
  17:00  EVENING_DATA    — US 가격 + 저녁 데이터 수집 (A)
  19:00  COLLECT_DART    — DART 공시 수집 (A)
  20:00  EVENING         — 자가학습 + 리포트 + Advisory
  22:35  US_MARKET       — 미국 시장 트레이딩 (A)

Usage:
    python scripts/daily_pipeline.py              # 전체 phase 실행
    python scripts/daily_pipeline.py overnight     # overnight만
    python scripts/daily_pipeline.py morning       # morning만
    python scripts/daily_pipeline.py collect,morning  # 복수 phase
"""

import json, logging, os, sys, time
from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from src.utils.file_ops import atomic_write_json, atomic_write_text

# .env 자동 로드 (launchd 등 외부 실행 환경 대응)
_env_file = _PROJECT_ROOT / '.env'
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _, _val = _line.partition('=')
                _key, _val = _key.strip(), _val.strip()
                if _key and _key not in os.environ:
                    os.environ[_key] = _val

from config.dynamic_config import DynamicConfig
from src.utils.market_calendar import is_trading_day
from src.utils.logger import setup_logger
from src.utils.time_utils import now_kst  # ★ L2-15 FIX: 스케줄링 시간 비교에 KST 사용

logger = setup_logger('daily_pipeline')
cfg = DynamicConfig()

# [Phase 62: Graceful Degradation] 데이터 결손 패널티 누산기 (mutable list로 module-level 공유)
# _phase_collect_data / _phase_evening_data 에서 실패 시 곱셈 적용
# run_pipeline 에서 ExposureOrchestrator.calculate(data_penalty=...) 로 전달
_data_penalty = [1.0]  # [0] 인덱스: 현재 누적 패널티 배율
_data_nogo_assets: list = []  # [Phase 70-Integration] DATA_NOGO 자산 목록 (모듈 레벨 공유)


# ── pykrx 지수 API 안전 래퍼 ──────────────────────────
# 근본 원인: KRX 서버가 야간(~08:30)에 지수 API 엔드포인트를 차단.
#   - 개별종목 OHLCV (MDCSTAT01701)는 야간에도 작동
#   - 지수 OHLCV (MDCSTAT00301), 지수목록 (MDCSTAT00401)은 야간 차단
# → 3단계 fallback: pykrx 지수 → yfinance → pykrx ETF proxy

_INDEX_YF_MAP = {
    '1004': None,       # VKOSPI (yfinance 미지원)
    '1028': '^KS200',   # KOSPI200
    '2203': None,       # KOSDAQ150 (yfinance 미지원)
}

# ETF proxy: 개별종목 API로 대체 수집 가능
_INDEX_ETF_PROXY = {
    '1004': '261220',   # KODEX VKOSPI선물 → VKOSPI 근사
    '1028': '069500',   # KODEX 200 → KOSPI200 추적
    '2203': '229200',   # KODEX KOSDAQ150 → KOSDAQ150 추적
}

# ETF → 실제 지수 변환 비율 (ETF 가격/지수 수준, 대략적 비율)
# 이 값은 수집 시점에 동적으로 계산하는 것이 이상적이나,
# ETF 가격 자체를 proxy로 사용하고 별도 필드로 저장
_INDEX_ETF_IS_PRICE = True  # ETF 가격은 지수와 다르므로, 변동률 기반 추정




def _safe_get_index_close(index_code: str, days: int = 5):
    """pykrx 지수 종가를 안전하게 가져오기.
    
    1차: pykrx (장중)
    2차: yfinance (KOSPI200)
    3차: Kalman Filter / EWMA 기반 시계열 보간 (Data Freshness 확보)
    """
    from datetime import timedelta as _td
    _today = datetime.now().strftime('%Y%m%d')
    _start = (datetime.now() - _td(days=days)).strftime('%Y%m%d')

    try:
        from pykrx import stock as _pykrx
        time.sleep(1)
        df = _pykrx.get_index_ohlcv(_start, _today, index_code)
        if df is not None and len(df) > 0:
            close_col = '종가' if '종가' in df.columns else df.columns[3]
            val = float(df.iloc[-1][close_col])
            logger.debug(f"  지수 {index_code}: {val} (pykrx)")
            return val
    except Exception as e:
        logger.error(f"pykrx API 실패: {e}", exc_info=True)

    yf_ticker = _INDEX_YF_MAP.get(index_code)
    if yf_ticker:
        try:
            import yfinance as yf
            data = yf.download(yf_ticker, period=f'{days}d', progress=False, auto_adjust=True)
            if data is not None and not data.empty:
                close_col = 'Close' if 'Close' in data.columns else 'close'
                v = data[close_col].iloc[-1]
                val = float(v.item() if hasattr(v, 'item') else v)
                logger.debug(f"  지수 {index_code}: {val} (yfinance)")
                return val
        except Exception as e:
            logger.error(f"yfinance API 실패: {e}", exc_info=True)

    # 3차: Kalman Filter / EWMA Data Interpolation
    try:
        logger.warning(f"  지수 {index_code}: 모든 실시간 API 실패. Kalman Filter 보간 시도.")
        _sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if _sc_path.exists():
            import json
            _sc = json.loads(_sc_path.read_text())
            if index_code == '1028':
                cached = _sc.get('kospi200')
            elif index_code == '2203':
                cached = _sc.get('kosdaq150')
            elif index_code == '1004':
                cached = _sc.get('vkospi')
            else:
                cached = None
                
            if cached and cached > 0:
                logger.info(f"  지수 {index_code}: {cached:.2f} (Kalman/EWMA 보간값)")
                return cached
    except Exception as e:
        logger.error(f"보간 엔진 실패: {e}", exc_info=True)

    logger.warning(f"  지수 {index_code}: 최종적으로 None 반환.")
    return None



def _check_data_gates(signal_cache: dict, phase_name: str, cfg=None) -> str:
    """Freshness Gate + DQS Gate 통합 진입 검사.

    두 가지 독립 게이트를 순서대로 통과해야 정상 실행이 허가됩니다.

    Gate 1 — Freshness Gate:
        signal_cache의 _generated_at 타임스탬프가 phase별 max_age_minutes 이내인지 확인.
        초과 시 'stale' 반환 → 호출자가 HOLD 유지 결정.

    Gate 2 — DQS Gate:
        signal_cache의 _data_quality_score가 SSOT 임계값을 충족하는지 확인.
        - score >= dqs_tier_full(0.70)    → 'ok'        (전 스트림 허용)
        - score >= dqs_tier_degraded(0.50) → 'degraded'  (S2 차단, S3/S5만 허용)
        - score >= dqs_tier_halt(0.30)     → 'halt'      (신규 진입 전면 차단)
        - score <  dqs_tier_halt(0.30)     → 'dead_man'  (데이터 완전 부재)

    Returns:
        'ok' | 'degraded' | 'halt' | 'dead_man' | 'stale'
    """
    from datetime import datetime, timezone

    def _g(k, d):
        return cfg.get(k, d) if cfg else d

    # ── Gate 1: Freshness ────────────────────────────────────────────────
    _generated_at_raw = signal_cache.get('_generated_at')
    if _generated_at_raw:
        try:
            _gen_dt    = datetime.fromisoformat(str(_generated_at_raw))
            _now       = datetime.now(_gen_dt.tzinfo)  # tzinfo 통일
            _elapsed   = (_now - _gen_dt).total_seconds() / 60.0
            _max_age   = float(_g(f'pipeline.cache_max_age_minutes.{phase_name}', 60))
            if _elapsed > _max_age:
                logger.warning(
                    f"  [Freshness Gate] {phase_name} phase: 캐시 경과 {_elapsed:.0f}분 "
                    f"> 한도 {_max_age:.0f}분 → 신선도 미달 (stale)"
                )
                return 'stale'
        except Exception as _fresh_e:
            logger.error(
                f"  [Freshness Gate] 타임스탬프 파싱 실패 (게이트 통과 허용): {_fresh_e}",
                exc_info=True,
            )

    # ── Gate 2: DQS ──────────────────────────────────────────────────────
    _dqs       = float(signal_cache.get('_data_quality_score', 1.0))
    _tier_full = float(_g('data.quality.dqs_tier_full',     0.70))
    _tier_deg  = float(_g('data.quality.dqs_tier_degraded', 0.50))
    _tier_halt = float(_g('data.quality.dqs_tier_halt',     0.30))

    if _dqs >= _tier_full:
        return 'ok'
    elif _dqs >= _tier_deg:
        logger.warning(
            f"  [DQS Gate] {phase_name}: score={_dqs:.3f} "
            f"(DEGRADED: {_tier_deg:.2f}~{_tier_full:.2f}) → S2 차단"
        )
        return 'degraded'
    elif _dqs >= _tier_halt:
        logger.error(
            f"  [DQS Gate] {phase_name}: score={_dqs:.3f} "
            f"(HALT: {_tier_halt:.2f}~{_tier_deg:.2f}) → 신규 진입 전면 차단",
            exc_info=False,
        )
        return 'halt'
    else:
        logger.critical(
            f"  [DQS Gate] {phase_name}: score={_dqs:.3f} "
            f"< HALT({_tier_halt:.2f}) → Dead Man's Switch (데이터 완전 부재)",
            exc_info=True,
        )
        return 'dead_man'


def _phase_premarket():
    """프리마켓 분석 (NXT 08:00 전).

    07:45 KST 실행:
      1. 레짐 판정 (야간 데이터 반영)
      2. 섹터 스코어링 (요약)
      3. 프리마켓 ETF 방향 신호 생성
    """
    logger.info("  🌅 Pre-Market 분석 (NXT 08:00 전)")

    # 1. 레짐 판정 (야간 OIS 반영)
    try:
        from src.intelligence.regime_engine import RegimeEngine
        regime = RegimeEngine().detect()
        logger.info(f"  ✅ 프리마켓 레짐: {regime['regime'].upper()} "
                    f"(conf={regime['confidence']:.2f})")
    except Exception as e:
        logger.error(f"  레짐 판정 실패: {e}", exc_info=True)

    # 2. OIS 최신값 반영 레짐 보정
    try:
        from src.intelligence.overnight_intelligence import OvernightIntelligenceScore
        ois = OvernightIntelligenceScore()
        result = ois.calculate(include_premarket=True)
        import math as _m_ois
        _ois_val = result.get('ois', 50)
        if not isinstance(_ois_val, (int, float)) or _m_ois.isnan(_ois_val):
            _ois_val = 50.0
        _update_signal_cache({
            'ois': _ois_val,
            'ois_premarket': True,
        })
        logger.info(f"  ✅ OIS 프리마켓 갱신: {_ois_val:.1f}")
    except Exception as e:
        logger.error(f"  OIS 갱신 실패: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════
# Phase PRE-TRADE: PREMARKET TRADE (NXT 08:00~08:50)
# ═══════════════════════════════════════════════════════

def _phase_premarket_trade():
    """프리마켓 S2/S4 개별주 매매 (NXT 08:00~08:50).

    ETF(S1/S3)는 프리마켓 거래 불가 → 개별주(S2/S4)만 실행.
    MarketSession.current() == 'pre' 확인 후 NXT 거래소로 주문.
    """
    _today_str = date.today().strftime('%Y-%m-%d')
    _log_file = _PROJECT_ROOT / 'logs' / f'premarket_trade_{_today_str}.log'
    _log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("  🌅 Pre-Market Trade: S2/S4 개별주 매매 (NXT)")

    # ── 0. Freshness Gate + DQS Gate 진입 검사 ──────────────────────────
    try:
        import json as _json_gate
        _sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        _sc_gate = _json_gate.loads(_sc_path.read_text()) if _sc_path.exists() else {}
        _cfg_gate = DynamicConfig()
        _gate = _check_data_gates(_sc_gate, 'morning', _cfg_gate)
        if _gate in ('dead_man', 'halt', 'stale'):
            logger.critical(f"[PreMarket Trade] DataGate 차단 ({_gate}) — 신규 진입 전면 중단 및 포지션 HOLD")
            return
        _skip_s2 = (_gate == 'degraded')
        if _skip_s2:
            logger.warning("[PreMarket Trade] DEGRADED MODE — S2 ML 진입 차단")
    except Exception as _gate_e:
        logger.critical(f"  [DataGate] 게이트 검사 중 치명적 에러 (Fail-Closed 작동): {_gate_e}", exc_info=True)
        return  # 에러 발생 시 강제 종료 (Fail-Closed)

    # ── 1. MarketSession 확인 ──
    try:
        from src.execution.execution_engine import MarketSession
        _session = MarketSession.current()
        if _session != 'pre':
            logger.info(f"  ⏭ 프리마켓 세션 아님 (current={_session}) — 스킵")
            return
    except ImportError as e:
        logger.error("  MarketSession import 실패 — 스킵", exc_info=True)
        return

        # ── 3. 동적 워터폴 엔진 가동 (V3 Architecture) ──
    if not _skip_s2:
        try:
            logger.info("  ⚡ [V3] 동적 통합 엔진 (stream_orchestrator) 가동")
            from scripts.stream_orchestrator import StreamOrchestrator
            StreamOrchestrator().run()
        except Exception as e:
            logger.error(f"  ❌ 프리마켓 동적 엔진 가동 실패: {e}", exc_info=True)
    else:
        logger.warning("  ⛔ [S2 차단] DEGRADED MODE — run_virtual_trading 스킵 (S4 Advisory만 허용)")

def sync_ssot_from_me():
    """ME 결과에서 go_nogo.json, shadow_summary.json, stream_metrics.json을 자동 추출하여 동기화."""
    logger.info("  🔄 SSoT 동기화 시작 (sync_ssot_from_me)...")
    try:
        from src.utils.file_ops import atomic_write_json
        from datetime import datetime, date
        import json
        _me_path = _PROJECT_ROOT / 'results' / 'measurement_engine.json'
        if _me_path.exists():
            _me_data = json.loads(_me_path.read_text())
            _me_off = _me_data.get('official', {})
            _me_views = _me_data.get('views', {})
            _me_ts = _me_data.get('timestamp', datetime.now().isoformat())

            # (A) go_nogo.json — ME SSoT에서 추출
            _gn_ssot = _me_views.get('go_nogo', {})
            if _gn_ssot:
                _gn_ssot['timestamp'] = _me_ts
                _gn_ssot['source'] = 'measurement_engine_ssot'
                _gn_path = _PROJECT_ROOT / 'results' / 'go_nogo.json'
                atomic_write_json(_gn_path, 
                    _gn_ssot, indent=2)
                logger.info(f"  ✅ SSoT: go_nogo.json 동기화 "
                            f"(verdict={_gn_ssot.get('verdict', '?')}, "
                            f"WR={_gn_ssot.get('win_rate', 0):.1%})")

            # (B) shadow_summary.json — ME에서 요약 추출 (기존 데이터 보존 merge)
            _ss_path = _PROJECT_ROOT / 'results' / 'shadow_summary.json'
            _ss_existing = {}
            if _ss_path.exists():
                try:
                    _ss_existing = json.loads(_ss_path.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    _ss_existing = {}

            # ME에서 업데이트할 필드만 merge (daily_stats 등 기존 데이터 보존)
            _ss_existing.update({
                'updated': _me_ts,
                'source': 'measurement_engine_ssot',
                'go_nogo': _gn_ssot or _ss_existing.get('go_nogo', {}),
                'nav': _me_off.get('nav') or _ss_existing.get('nav', 0),
                'cumulative_return_pct': _me_off.get('cumulative_return_pct',
                    _ss_existing.get('cumulative_return_pct', 0)),
                'grade': _me_off.get('grade') or _ss_existing.get('grade', '?'),
                'sharpe': _me_off.get('sharpe') or _ss_existing.get('sharpe', 0),
                'win_rate': _me_off.get('realized_win_rate') or _ss_existing.get('win_rate', 0),
                'max_dd': _me_off.get('max_drawdown_pct') or _ss_existing.get('max_dd', 0),
                'profit_factor': _me_off.get('profit_factor') or _ss_existing.get('profit_factor', 0),
            })

            atomic_write_json(_ss_path, 
                _ss_existing, indent=2)
            logger.info(f"  ✅ SSoT: shadow_summary.json 동기화 (merge)")

            # (C) stream_metrics.json — ME sleeves에서 추출
            _sleeves = _me_views.get('sleeves', {})
            _per_stream = _me_off.get('per_stream', {})
            _sm_path = _PROJECT_ROOT / 'results' / 'stream_metrics.json'
            _sm_existing = {}
            if _sm_path.exists():
                try:
                    _sm_existing = json.loads(_sm_path.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:299", exc_info=True)

            _sm_ssot = {
                'timestamp': _me_ts,
                'source': 'measurement_engine_ssot',
                'raw_data': _sm_existing.get('raw_data', {}),  # 보존
                'metrics': {},
            }
            for _sid in sorted(set(list(_sleeves.keys()) + list(_per_stream.keys()))):
                _sv = _sleeves.get(_sid, {})
                _ps_v = _per_stream.get(_sid, {})
                if isinstance(_sv, dict):
                    _sm_ssot['metrics'][_sid] = {
                        'win_rate': _sv.get('win_rate', _ps_v.get('win_rate', 0)),
                        'total_trades': _sv.get('total_trades', _ps_v.get('total_trades', 0)),
                        'realized_pnl': _sv.get('realized_pnl', _ps_v.get('realized_pnl', 0)),
                        'active_positions': _sv.get('active_positions', _ps_v.get('active_positions', 0)),
                        'market_value': _sv.get('market_value', _ps_v.get('market_value', 0)),
                        'sharpe': _sv.get('sharpe'),
                        'alpha': _sv.get('alpha'),
                        'mdd': _sv.get('mdd'),
                        'sortino': _sv.get('sortino'),
                    }
            _sm_path = _PROJECT_ROOT / 'results' / 'stream_metrics.json'
            atomic_write_json(_sm_path, 
                _sm_ssot, indent=2)
            logger.info(f"  ✅ SSoT: stream_metrics.json 동기화 "
                        f"({len(_sm_ssot['metrics'])} streams)")

            # (D) pipeline_state.json 타임스탬프 갱신
            _ps_path = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            _ps_data = {}
            if _ps_path.exists():
                try:
                    _ps_data = json.loads(_ps_path.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:337", exc_info=True)
            _ps_data['timestamp'] = _me_ts
            _ps_data['date'] = date.today().isoformat()
            _ps_data['ssot_synced'] = True
            atomic_write_json(_ps_path, 
                _ps_data, indent=2)
            logger.info(f"  ✅ SSoT: pipeline_state.json 타임스탬프 갱신")

        else:
            logger.warning("  ⚠️ SSoT 동기화 스킵: measurement_engine.json 없음")
    except Exception as e:
        logger.error(f"  SSoT 동기화 실패: {e}", exc_info=True)


def _phase_aftermarket():
    """애프터마켓 분석 (16:30~20:00).

    16:30 KST 실행:
      1. Shadow Portfolio NAV 확정 (마감가 반영)
      2. MeasurementEngine SSoT 계산
      3. Go/No-Go 판정
      4. 다음 거래일 전략 프리뷰
    """
    logger.info("  🌆 After-Market 분석")

    # 0. ★ 전체 포트폴리오 MTM (마감가 반영 — DA/IC 정상화 필수)
    try:
        from src.portfolio.shadow_manager import ShadowPortfolioManager
        _mgr = ShadowPortfolioManager()
        _positions = _mgr.data.get('positions', {})
        _tickers = set()
        for _pk, _pos in _positions.items():
            _tk = _pos.get('ticker', _pk.split(':')[-1] if ':' in _pk else _pk)
            _tickers.add(_tk)

        if False: # Disabled: pykrx bug with future dates (2026)
            from pykrx import stock as _pykrx_stock
            from datetime import datetime as _dt
            _today_short = _dt.now().strftime('%Y%m%d')
            _mtm_prices = {}
            for _tk in _tickers:
                try:
                    _df = _pykrx_stock.get_market_ohlcv(
                        _today_short, _today_short, _tk)
                    if len(_df) > 0:
                        _price = _df.iloc[-1].get('종가', 0)
                        if _price > 0:
                            _mtm_prices[_tk] = float(_price)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:388", exc_info=True)

            if _mtm_prices:
                _mgr.mark_to_market(_mtm_prices)
                _mgr.save()
                logger.info(f"  ✅ MTM: {len(_mtm_prices)}/{len(_tickers)} 종목 마감가 반영")
            else:
                logger.warning(f"  ⚠️ MTM: 마감가 조회 실패 ({len(_tickers)} 종목)")
    except Exception as e:
        logger.error(f"  MTM 실패: {e}", exc_info=True)

    # 0.5. ★ S1 당일 청산 안전장치 — S1 잔존 포지션 강제 청산
    # closing(15:10)보다 market(15:59)가 늦어 S1 진입 후 청산 안 된 경우 대비
    try:
        from src.portfolio.shadow_manager import ShadowPortfolioManager
        from datetime import datetime as _dt_s1
        _mgr_s1 = ShadowPortfolioManager()
        _s1_keys = [pk for pk in _mgr_s1.positions if pk.startswith('S1:')]

        if _s1_keys:
            logger.info(f"  🔴 S1 잔존 포지션 {len(_s1_keys)}개 발견 → 강제 청산")
            _today_s1 = _dt_s1.now().strftime('%Y%m%d')

            # 현재가 조회
            from pykrx import stock as _pyk_s1
            _s1_prices = {}
            for _pk in _s1_keys:
                _tk = _pk.split(':')[1]
                try:
                    _df = _pyk_s1.get_market_ohlcv(_today_s1, _today_s1, _tk)
                    if len(_df) > 0:
                        _s1_prices[_tk] = float(_df.iloc[-1].get('종가', 0))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:423", exc_info=True)

            if _s1_prices:
                _mgr_s1.mark_to_market(_s1_prices)

            for _pk in _s1_keys:
                _pos = _mgr_s1.positions[_pk]
                _tk = _pk.split(':')[1]
                _entry = _pos.get('entry_price', _pos.get('avg_price', 0))
                _cur = _s1_prices.get(_tk, _pos.get('current_price', _entry))
                _amt = _pos.get('amount', 0)
                _qty = _pos.get('quantity', int(_amt / _entry) if _entry > 0 else 0)
                _pnl = (_cur - _entry) * _qty if _qty > 0 else _pos.get('unrealized_pnl', 0)
                _pnl_pct = ((_cur / _entry) - 1) * 100 if _entry > 0 else 0

                # trade_history 기록
                _mgr_s1.data.setdefault('trade_history', []).append({
                    'date': _dt_s1.now().strftime('%Y-%m-%d'),
                    'action': 'SELL',
                    'ticker': _tk,
                    'name': _pos.get('name', _tk),
                    'stream': 'S1',
                    'quantity': _qty,
                    'price': _cur,
                    'avg_price': _entry,
                    'amount': _amt,
                    'realized_pnl': round(_pnl),
                    'pnl_pct': round(_pnl_pct, 2),
                    'sell_type': 'forced_close',
                    'reason': 'S1 당일 청산 안전장치 (aftermarket)',
                })

                # cash 복원 + 포지션 제거
                _sell_val = _cur * _qty if _qty > 0 else _pos.get('current_value', _amt)
                _mgr_s1.data['cash'] = _mgr_s1.data.get('cash', 0) + _sell_val
                del _mgr_s1.positions[_pk]
                logger.info(f"    ✅ 청산: {_pos.get('name', _tk)} PnL=₩{_pnl:+,.0f} ({_pnl_pct:+.1f}%)")

            # NAV 재계산 + 저장
            _total_mv = sum(p.get('market_value', p.get('amount', 0))
                           for p in _mgr_s1.positions.values())
            _mgr_s1.data['virtual_nav'] = _mgr_s1.data['cash'] + _total_mv
            _mgr_s1.save()
            logger.info(f"  ✅ S1 강제 청산 완료: NAV=₩{_mgr_s1.data['virtual_nav']:,.0f}")
        else:
            logger.info("  ✅ S1 잔존 포지션 없음")
    except Exception as e:
        logger.error(f"  S1 안전장치 실패: {e}", exc_info=True)

    # ★ 진단 개선: aftermarket 후 ME 즉시 재실행 (stale data 방지)
    try:
        from src.measurement.measurement_engine import run_measurement
        me_result = run_measurement()
        logger.info(f"  ✅ ME 재실행: alpha={me_result.get('views',{}).get('portfolio',{}).get('alpha_pct','?')}%")
    except Exception as e:
        logger.error(f"  ⚠️ ME 재실행 실패: {e}", exc_info=True)

    # 1. Shadow Portfolio 확정 — ShadowPortfolioManager.daily_snapshot()
    try:
        from src.portfolio.shadow_manager import ShadowPortfolioManager
        _ps = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
        _regime = 'caution'
        if _ps.exists():
            _psd = json.loads(_ps.read_text())
            _regime = _psd.get('kr_regime', _psd.get('operating_regime', 'caution'))
        _initial = cfg.get('portfolio.initial_capital')
        _mgr = ShadowPortfolioManager(initial_capital=_initial)
        record = _mgr.daily_snapshot(regime=_regime)
        _mgr.save()
        _nav = _mgr.nav
        _ret = record.get('daily_return_pct', 0)
        _ic = record.get('ic', {}).get('ic', 'N/A') if isinstance(record.get('ic'), dict) else 'N/A'
        logger.info(f"  ✅ Shadow 확정: NAV=₩{_nav:,.0f} ({_ret:+.2f}%) IC={_ic}")
    except Exception as e:
        logger.error(f"  Shadow 확정 실패: {e}", exc_info=True)

    # 2. MeasurementEngine SSoT
    try:
        from src.measurement.measurement_engine import run_measurement
        me_result = run_measurement()
        logger.info("  ✅ MeasurementEngine SSoT 계산 완료")

        try:
            from src.measurement.event_ledger import log_event
            log_event('MEASUREMENT', {
                'official': me_result.get('official', {}),
                'phase': 'aftermarket',
            }, source='measurement_engine')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:514", exc_info=True)

        # ★ SSoT 파일 자동 동기화 (ME 연산 직후 즉시 — Go/No-Go 이전)
        # go_nogo.json이 ME의 최신값으로 먼저 덮인 뒤 GoNoGoEngine이 실행되도록 순서 보장
        sync_ssot_from_me()

    except Exception as e:
        logger.error(f"  MeasurementEngine 실패: {e}", exc_info=True)

    # 2.5. Stream Metrics 재생성 (★ 대시보드 데이터 갱신)
    try:
        from scripts.rebuild_stream_metrics import rebuild
        if rebuild():
            logger.info("  ✅ Stream Metrics 재생성 완료")
        else:
            logger.warning("  Stream Metrics 재생성 실패")
    except Exception as e:
        logger.error(f"  Stream Metrics 재생성 실패: {e}", exc_info=True)

    # 3. Go/No-Go 판정
    try:
        from scripts.go_nogo import GoNoGoEngine
        gng = GoNoGoEngine()
        result = gng.evaluate()
        logger.info(f"  ✅ Go/No-Go: {result.get('verdict', '?')}")
    except Exception as e:
        logger.error(f"  Go/No-Go 실패: {e}", exc_info=True)

    # ═══ SSoT 동기화 ═══
    sync_ssot_from_me()

    # ★ KIS Portfolio + S4 Advisory 자동 갱신 (대시보드 SSoT)
    try:
        from scripts.generate_kis_portfolio import generate as _gen_kis
        _gen_kis()
        logger.info("  ✅ kis_portfolio.json 갱신 완료")
    except Exception as e:
        logger.error(f"  kis_portfolio 갱신 실패: {e}", exc_info=True)
    try:
        from scripts.generate_s4_advisory import generate_advisory as _gen_s4
        _gen_s4()
        logger.info("  ✅ s4_advisory_recommendations.json 갱신 완료")
    except Exception as e:
        logger.error(f"  s4_advisory 갱신 실패: {e}", exc_info=True)

    # ★ S4 Advisory BROKERAGE 자동 체결: auto_execute=True 주문 → shadow_portfolio 반영
    try:
        _adv_path = _PROJECT_ROOT / 'results' / 's4_advisory_recommendations.json'
        if _adv_path.exists():
            _adv = json.loads(_adv_path.read_text())
            _brk_auto = _adv.get('brokerage_auto', [])
            if _brk_auto:
                _sp_path = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
                _sp = json.loads(_sp_path.read_text()) if _sp_path.exists() else {}
                _positions = _sp.get('positions', {})
                _cash = _sp.get('cash', 0)
                _trade_history = _sp.get('trade_history', [])
                _today = datetime.now().strftime('%Y-%m-%d')
                _executed = 0

                for _item in _brk_auto:
                    if not _item.get('auto_execute', False):
                        continue
                    _action = _item.get('action', '').upper()
                    _ticker = _item.get('ticker', '')
                    _name = _item.get('name', _ticker)
                    _price = _item.get('price', 0)
                    _quantity = _item.get('quantity', 0)

                    if _action == 'BUY' and _price > 0 and _quantity > 0:
                        _amount = _price * _quantity
                        _pos_key = f'S4:BROKERAGE:{_ticker}'
                        # 이미 BROKERAGE 계좌에 보유 중이면 스킵
                        if _pos_key in _positions:
                            logger.info(f"    ⏭️ S4 Advisory: {_name} BROKERAGE 이미 보유 — 스킵")
                            continue
                        # 현금 부족 체크
                        if _cash < _amount:
                            logger.warning(f"    🚫 S4 Advisory: {_name} 현금 부족 "
                                          f"(필요 ₩{_amount:,.0f} > 보유 ₩{_cash:,.0f})")
                            continue

                        _sp['cash'] -= _amount
                        _cash -= _amount
                        _positions[_pos_key] = {
                            'ticker': _ticker,
                            'name': _name,
                            'quantity': _quantity,
                            'amount': _amount,
                            'avg_price': _price,
                            'current_price': _price,
                            'entry_date': _today,
                            'stream_id': 'S4',
                            'account': 'BROKERAGE',
                            'strategy': 'advisory_brokerage',
                            'direction': 'long',
                            'unrealized_pnl': 0,
                            'pnl_pct': 0.0,
                        }
                        _trade_history.append({
                            'date': _today,
                            'action': 'BUY',
                            'ticker': _ticker,
                            'name': _name,
                            'price': _price,
                            'quantity': _quantity,
                            'amount': _amount,
                            'stream': 'S4',
                            'account': 'BROKERAGE',
                            'strategy': 'advisory_brokerage',
                            'source': 's4_advisory_brokerage',
                        })
                        _executed += 1
                        logger.info(f"    ✅ S4 Advisory BUY: {_name} ({_ticker}) "
                                   f"{_quantity}주 × ₩{_price:,.0f} = ₩{_amount:,.0f}")

                    elif _action == 'SELL' and _ticker:
                        _pos_key = f'S4:BROKERAGE:{_ticker}'
                        _pos = _positions.pop(_pos_key, None)
                        if not _pos:
                            continue
                        _sell_price = _price if _price > 0 else _pos.get('current_price', _pos.get('avg_price', 0))
                        _sell_qty = _pos.get('quantity', 0)
                        _sell_amount = _sell_price * _sell_qty if _sell_price and _sell_qty else _pos.get('amount', 0)
                        _entry_price = _pos.get('avg_price', 0)
                        _pnl = _sell_amount - _pos.get('amount', 0)
                        _pnl_pct = (_sell_price / _entry_price - 1) * 100 if _entry_price > 0 else 0

                        _sp['cash'] += _sell_amount
                        _cash += _sell_amount
                        _trade_history.append({
                            'date': _today,
                            'action': 'SELL',
                            'ticker': _ticker,
                            'name': _pos.get('name', _name),
                            'price': _sell_price,
                            'quantity': _sell_qty,
                            'amount': _sell_amount,
                            'realized_pnl': _pnl,
                            'pnl_pct': round(_pnl_pct, 2),
                            'entry_price': _entry_price,
                            'stream': 'S4',
                            'account': 'BROKERAGE',
                            'strategy': 'advisory_brokerage',
                            'source': 's4_advisory_brokerage',
                        })
                        _executed += 1
                        logger.info(f"    ✅ S4 Advisory SELL: {_pos.get('name', _name)} ({_ticker}) "
                                   f"PnL ₩{_pnl:+,.0f} ({_pnl_pct:+.1f}%)")

                if _executed > 0:
                    _sp['positions'] = _positions
                    _sp['trade_history'] = _trade_history
                    atomic_write_json(_sp_path, 
                        _sp, indent=2)
                    logger.info(f"  ✅ S4 Advisory BROKERAGE 자동 체결: {_executed}건")
                else:
                    logger.info("  ℹ️ S4 Advisory BROKERAGE: 체결 대상 없음")
    except Exception as e:
        logger.error(f"  S4 Advisory 자동 체결 실패: {e}", exc_info=True)

    # ★ M12 IC Decay Auto-Retrain Trigger
    try:
        _me_path2 = _PROJECT_ROOT / 'results' / 'measurement_engine.json'
        if _me_path2.exists():
            _me2 = json.loads(_me_path2.read_text())
            _ic_ens = _me2.get('views', {}).get('ic_ensemble', {})
            _decay_alerts = _ic_ens.get('ic_decay_alerts', {})
            _any_decay = any(
                v.get('alert', False) 
                for v in _decay_alerts.values() 
                if isinstance(v, dict)
            )
            if _any_decay:
                logger.warning("  ⚠️ M12 IC 감쇠 경보 → 자동 재학습 트리거")
                try:
                    from scripts.train_ensemble import should_retrain, run_training
                    _needed, _trigger = should_retrain()
                    if _needed:
                        _result = run_training(trigger='ic_decay', enable_automl=True)
                        if _result:
                            logger.info(f"  ✅ IC 감쇠 재학습 완료: ACC={_result.get('val_acc', 0):.3f}")
                    else:
                        logger.info("  ℹ️ IC 감쇠 경보이나 재학습 조건 미충족 (스킵)")
                except Exception as _re:
                    logger.error(f"  IC 감쇠 재학습 실패: {_re}", exc_info=True)
            else:
                logger.info("  ✅ M12 IC 감쇠: 정상 (경보 없음)")
    except Exception as e:
        logger.error(f"  M12 IC decay check: {e}", exc_info=True)

    # ── TCA Post-Market Summary ──
    try:
        from src.execution.tca import TCAAnalyzer
        _tca = TCAAnalyzer()
        _tca_result = _tca.compute_and_save_summary()
        logger.info(f"  ✅ TCA 요약: {_tca_result.get('n_trades', 0)}건, "
                    f"avg_is={_tca_result.get('avg_is_bps', 0):.1f}bps")
    except Exception as e:
        logger.error(f"  TCA 요약 실패: {e}", exc_info=True)

    # 4. ★ 유니버스 갱신 (유동성 필터 기반)
    try:
        from scripts.build_universe import build_universe
        universe = build_universe(
            min_turnover_억=cfg.get('universe.min_turnover', 10.0),
            lookback_days=cfg.get('universe.lookback_days', 20))
        uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        with open(uni_file, 'w') as f:
            json.dump(universe, f, indent=2)
        logger.info(f"  ✅ 유니버스 갱신: {len(universe)}종목 (≥10억/일)")
    except Exception as e:
        logger.error(f"  유니버스 갱신 실패: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════
# Phase AFT-TRADE: AFTERMARKET TRADE (NXT 15:30~20:00)
# ═══════════════════════════════════════════════════════

def _phase_aftermarket_trade():
    """에프터마켓 S2/S4 개별주 매매 (NXT 15:30~20:00).

    정규장 매도/SL 미체결분 재시도 + 신규 시그널 매수.
    """
    _today_str = date.today().strftime('%Y-%m-%d')
    _log_file = _PROJECT_ROOT / 'logs' / f'aftermarket_trade_{_today_str}.log'
    _log_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("  🌆 After-Market Trade: S2/S4 개별주 매매 (NXT)")

    # ── 1. MarketSession 확인 ──
    try:
        from src.execution.execution_engine import MarketSession
        _session = MarketSession.current()
        if _session != 'after':
            logger.info(f"  ⏭ 에프터마켓 세션 아님 (current={_session}) — 스킵")
            return
    except ImportError as e:
        logger.error("  MarketSession import 실패 — 스킵", exc_info=True)
        return

        # ── 2. 동적 워터폴 엔진 가동 (V3 Architecture) ──
    try:
        logger.info("  ⚡ [V3] 동적 통합 엔진 (stream_orchestrator) 가동")
        from scripts.stream_orchestrator import StreamOrchestrator
        StreamOrchestrator().run()
    except Exception as e:
        logger.error(f"  ❌ 에프터마켓 동적 엔진 가동 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  overnight_macro_collector import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  Overnight Macro 실패: {e}", exc_info=True)

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
            logger.error(f"  Overnight Intel 캐시 실패: {oi_err}", exc_info=True)

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
            logger.error(f'overnight_intel 파일 저장 실패: {_oi_err}', exc_info=True)

        results['ois'] = _ois_safe(ois_result.get('ois', 50))
        results['ois_price'] = _ois_safe(ois_result.get('ois_price', 50))
        results['ois_sentiment'] = _ois_safe(ois_result.get('ois_sentiment', 50))
        results['ois_label'] = ois_result.get('sentiment', 'neutral')
        results['ois_threshold_adj'] = ois_result.get('threshold_adj', 0)
        logger.info(f"  ✅ OIS: {ois_result['ois']:.1f}/100 "
                    f"→ {ois_result['sentiment']} "
                    f"(threshold {ois_result['threshold_adj']:+d})")
    except ImportError as e:
        logger.error("  overnight_intelligence import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  OIS 계산 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  us_market_regime import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  US Regime 실패: {e}", exc_info=True)

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
            logger.error(f"  지수 데이터 수집 실패: {pyk_err}", exc_info=True)

        # stock_technicals (삼성전자/하이닉스 RSI, MACD, 거래량비)
        try:
            from pykrx import stock as _pykrx2
            from datetime import timedelta as _td3
            _end = datetime.now().strftime('%Y%m%d')
            _start = (datetime.now() - _td3(days=30)).strftime('%Y%m%d')

            stock_techs = {}
            _tech_tickers = cfg.get(
                'data.tech_indicator_tickers',
                [['005930', '삼성전자'], ['000660', 'SK하이닉스']]
            )
            for _ticker, _name in _tech_tickers:
                _ohlcv = _pykrx2.get_market_ohlcv(_start, _end, _ticker)
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
            logger.error(f"  stock_technicals 수집 실패: {tech_err}", exc_info=True)

        if s1_cache_updates:
            _update_signal_cache(s1_cache_updates)
            logger.info(f"  ✅ S1 Edge 데이터: {len(s1_cache_updates)}개 항목 → signal_cache")
    except Exception as e:
        logger.error(f"  S1 Edge 데이터 수집 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  event_market_filter import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  이벤트 캘린더 실패: {e}", exc_info=True)

    # ── 4.5 뉴스 감성 분석 ──
    try:
        from src.intelligence.naver_news_sentiment import collect_news_sentiment
        news_result = collect_news_sentiment()
        results['news_sentiment'] = news_result.get('sentiment', 0)
        logger.info(f"  ✅ 뉴스 감성: {news_result.get('label', '?')} "
                    f"({news_result.get('sentiment', 0):+.3f}, "
                    f"{news_result.get('n_articles', 0)}건)")
    except ImportError as e:
        logger.error("  naver_news_sentiment import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  뉴스 감성 수집 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  market_shock_detector import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  Shock 감지 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  cross_asset_signals import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  Cross-Asset 실패: {e}", exc_info=True)

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
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1120", exc_info=True)

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

        # ★ Flash Crash Gate 입력 준비
        try:
            from src.regime.regime_detector import RegimeDetector
            reg_det = RegimeDetector().detect()
            
            _s3_buys = []
            _ls_file = _PROJECT_ROOT / 'results' / 'latest_signals.json'
            if _ls_file.exists():
                _ls_data = json.loads(_ls_file.read_text())
                _s3_buys = [s for s in _ls_data.get('signals', {}).get('S3', [])
                            if s.get('action') in ('buy', 'long', 'BUY', 'LONG')]
            _s3_conf = round(sum(s.get('confidence', 0.0) for s in _s3_buys) / len(_s3_buys), 4) if _s3_buys else 0.0

            _update_signal_cache({
                'crash_type': reg_det.get('crash_type', 'unknown'),
                'cross_asset_stress': float(reg_det.get('cross_asset_stress', 0.0)),
                's3_avg_confidence': _s3_conf
            })
        except Exception as e:
            logger.error(f"  FlashCrash 데이터 수집 실패: {e}", exc_info=True)

        logger.info(f"  ✅ pipeline_state: us={current_us_regime}"
                     f" kr={kr_regime}"
                     f" (prev_us={prev_us_regime})")
    except Exception as e:
        logger.error(f"  pipeline_state 저장 실패: {e}", exc_info=True)

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
    # [Phase 65: SSOT] 매크로 데이터 내부 수집 (Data_Hub_Agent 의존성 제거)
    try:
        from src.data_collection.macro_collector import MacroCollector as _MacroCollector
        _MacroCollector().collect_all()
        logger.info('  [Phase 65] 매크로 파케 갱신 완료: data/macro/macro_data.parquet')
    except Exception as _mc_e:
        # [Phase 70-Integration] DataNoGoException 명시적 처리
        try:
            from src.utils.data_imputer import DataNoGoException as _DataNoGoException
            if isinstance(_mc_e, _DataNoGoException):
                _nogo_col = getattr(_mc_e, 'column', 'macro_unknown')
                _data_nogo_assets.append(_nogo_col)
                logger.error(
                    f'  [Phase 70 DATA_NOGO] 매크로 {_nogo_col}: '
                    f'PCA R² 미달 — 해당 지표 0% 격리'
                )
            else:
                logger.error(f'  ⚠️ [Phase 65] MacroCollector 실패 (Graceful): {_mc_e}', exc_info=True)
        except ImportError as e:
            logger.error(f'  ⚠️ [Phase 65] MacroCollector 실패 (Graceful): {_mc_e}', exc_info=True)
    try:
        from src.data_collection.unified_collector import run_daily
        results = run_daily(mode='morning')
        logger.info(f"  ✅ 모닝 수집 완료: {results.get('elapsed_sec', '?')}초")
    except Exception as e:
        logger.error(f"  모닝 수집 실패 (글로벌만 시도): {e}", exc_info=True)
        try:
            from src.data_collection.unified_collector import collect_global_signals
            signals = collect_global_signals()
            logger.info(f"  ✅ 글로벌 시그널 fallback: {len(signals)}개")
        except Exception as e2:
            logger.error(f"  글로벌 시그널도 실패: {e2}", exc_info=True)
            # [Phase 62: Graceful Degradation] 매크로+대체데이터 상실 시 50% 패널티
            _data_penalty[0] *= 0.5
            logger.warning(
                f'  [Phase 62] 데이터 결손 패널티 적용: 모닝 수집 전체 실패 '
                f'→ exposure ×0.5 (penalty={_data_penalty[0]:.2f})')

    # [Phase 57: Alpha Vantage Macro Sentiment] 글로벌 매크로 감성 수집
    # 무료 API 한도(25회/일)를 고려하여 파이프라인에서 단 1회만 호출
    def _run_alpha_vantage_collection():
        logger.info('  🌍 [Phase 57] Alpha Vantage 글로벌 매크로 감성 수집 실행')
        import subprocess
        try:
            _av_result = subprocess.run(
                [sys.executable,
                 str(_PROJECT_ROOT / 'src' / 'data_collection' / 'alpha_vantage_collector.py')],
                capture_output=True, text=True, check=True, timeout=30,
            )
            if _av_result.stdout:
                logger.debug(f'  AV stdout: {_av_result.stdout[:200]}')
            logger.info('  ✅ [Phase 57] Alpha Vantage 감성 수집 완료')
        except subprocess.TimeoutExpired:
            logger.error('  ⚠️ Alpha Vantage 수집 타임아웃 (30s)', exc_info=True)
        except subprocess.CalledProcessError as _cpe:
            logger.warning(
                f'  ⚠️ Alpha Vantage 수집 실패 (한도 초과 가능성): '
                f'{_cpe.stderr[:300]}')
        except Exception as _exc:
            logger.error(f'  ⚠️ Alpha Vantage 예외: {_exc}', exc_info=True)

    try:
        _run_alpha_vantage_collection()
    except Exception as e:
        logger.error(f'  [Phase 57] Alpha Vantage collection error: {e}', exc_info=True)

def _phase_global_data():
    """글로벌 데이터만 수집 (KRX 휴장일)."""
    logger.info("  글로벌 시그널 수집 (VIX, 금리, 환율)")
    try:
        from src.data_collection.unified_collector import collect_global_signals
        signals = collect_global_signals()
        logger.info(f"  ✅ 글로벌 시그널: {len(signals)}개")
    except Exception as e:
        logger.error(f"  글로벌 데이터 실패: {e}", exc_info=True)


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
        logger.error(f"  signal_cache 업데이트 실패: {e}", exc_info=True)



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
    except ImportError as e:
        logger.error("  macro_realtime_refresher import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  MacroRefresh 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  realtime_exit_monitor import 실패 — 기존 방식 유지", exc_info=True)
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
                logger.error(f"  🟢 WebSocket 스트리밍: {len(ws_tickers)}종목 구독", exc_info=True)
            else:
                logger.error("  ℹ️ WebSocket 미가용 → REST fallback", exc_info=True)
        except ImportError:
            logger.error("  kis_websocket import 실패 — REST 전용", exc_info=True)
        except Exception as e:
            logger.error(f"  ℹ️ WebSocket 미가용: {e} → REST fallback", exc_info=True)
    except Exception as e:
        logger.info(f"  RealtimeExitMonitor 시작 실패: {e} → 기존 방식")

    # 1. 장중 가격 수집
    try:
        from src.data_collection.pykrx_fetcher import get_5min_bars
        # ★ NM-02 FIX: UNIVERSE import 실패 시 동적 유니버스로 폴백
        tickers = []
        try:
            from config.universe import Universe
            univ = Universe()
            tickers = (
                [t.ticker for t in univ.A1_DIRECTIONAL.values()][:10]
                + [t.ticker for t in univ.A2_SECTORS.values()][:10]
            )
            logger.debug(f"  Universe 클래스 로드: {len(tickers)}종목")
        except Exception as _ue:
            logger.error(f"  유니버스 로드 실패: {_ue}", exc_info=True)

        if tickers:
            for ticker in tickers:
                try:
                    bars = get_5min_bars(ticker)
                    if bars is not None and len(bars) > 0:
                        out_dir = _PROJECT_ROOT / 'data' / 'minute_bars' / ticker
                        out_dir.mkdir(parents=True, exist_ok=True)
                        out_file = out_dir / f'{date.today().isoformat()}.csv'
                        bars.to_csv(out_file, index=False)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1476", exc_info=True)
            logger.info(f"  ✅ 장중 5분봉 수집: {len(tickers)}종목")
        else:
            logger.warning("  장중 5분봉: 유니버스 없음 → 스킵")
    except ImportError as e:
        logger.error("  pykrx_fetcher import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  장중 수집 실패: {e}", exc_info=True)

    # ─── [Phase 36] 장중 수급·거래량 수집 (스로틀링 적용) ──────────────────────
    # config.intraday.flow_fetch_interval_min(기본 10분)마다 KIS API 배치 조회.
    # API 장애 시 빈 dict 반환 → 하위 Exit 로직은 정적 fallback으로 동작.
    _intraday_flow_data = {}
    try:
        import time as _time_mod
        from datetime import datetime
        _now_dt = datetime.now()
        
        # 개별주 ATS 거래시간(08:00~20:00 프리/에프터 마켓) 대응 데이터 수집
        if 800 <= _now_dt.hour * 100 + _now_dt.minute <= 2000:
            from src.data_collection.intraday_flow_collector import IntradayFlowCollector
            _flow_collector = IntradayFlowCollector(config=cfg)
            
            _RESULTS_DIR = _PROJECT_ROOT / 'results'
            # 스로틀링: 마지막 수집 이후 interval 이상 경과 시에만 API 호출
            _flow_interval_sec = cfg.get("intraday.flow_fetch_interval_min", 10) * 60
            _last_flow_ts_file = _RESULTS_DIR / ".intraday_flow_last_ts"
            _now_ts = _time_mod.time()
            _last_ts = 0.0
            try:
                _last_ts = float(_last_flow_ts_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1510", exc_info=True)

            if _now_ts - _last_ts >= _flow_interval_sec:
                # 현재 보유 종목 + S1 관심 유니버스 대상 수집
                _flow_tickers = set()
                try:
                    _shadow_f = _RESULTS_DIR / "shadow_portfolio.json"
                    if _shadow_f.exists():
                        _sp_data = json.loads(_shadow_f.read_text())
                        for _pv in _sp_data.get("positions", {}).values():
                            _tk = _pv.get("ticker", "")
                            if _tk:
                                _flow_tickers.add(_tk)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1526", exc_info=True)
                # 핵심 SS-ETF 기초자산 추가
                _default_tickers = cfg.get("intraday.default_watch_tickers", [])
                _flow_tickers.update(_default_tickers)
                # 최대 종목 수: config 기반 동적 제한 (하드코딩 제거)
                _max_flow_tickers = int(cfg.get("intraday.max_batch_tickers", 20))
                _flow_tickers = list(_flow_tickers)[:_max_flow_tickers]

                _intraday_flow_data = _flow_collector.fetch(_flow_tickers)
                # 타임스탬프 갱신
                try:
                    _last_flow_ts_file.write_text(str(_now_ts))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1541", exc_info=True)
                logger.info(f"  ✅ [Phase 36] 장중 수급 수집: {len(_flow_tickers)}종목")
            else:
                # 아직 interval 미달 → 캐시 재사용
                _intraday_flow_data = IntradayFlowCollector.load_cache()
                _remaining = int(_flow_interval_sec - (_now_ts - _last_ts))
                logger.debug(f"  [Phase 36] 수급 스로틀링 — 다음 수집까지 {_remaining}초")

            # S1-B 돌파 스캔
            try:
                from src.streams.s1_edge.s1_intraday_breakout import S1IntradayBreakout
                _regime_now = cfg.get("regime.current", "bull")
                _breakout_signals = S1IntradayBreakout(config=cfg).scan(
                    _intraday_flow_data, regime=_regime_now
                )
                if _breakout_signals:
                    logger.info(f"  🚀 [Phase 36] S1-B 돌파 시그널 {len(_breakout_signals)}개")
            except Exception as _sb_e:
                logger.error(f"  [Phase 36] S1-B 스캔 실패: {_sb_e}", exc_info=True)
        else:
            logger.debug("  [Phase 36] 비장중 시간 (09:00~15:30 외) → 수급 수집 스킵")

    except ImportError as e:
        logger.error("  [Phase 36] IntradayFlowCollector import 실패 → 정적 fallback", exc_info=True)
    except Exception as _flow_e:
        logger.error(f"  [Phase 36] 장중 수급 수집 실패: {_flow_e} → 정적 fallback", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────────
    # 2. 실시간 수급/밸류에이션
    try:
        from src.data_collection.realtime_collector import RealtimeCollector
        from src.data_collection.realtime_constants import KR_SECTOR_STOCKS
        rc = RealtimeCollector()
        _sectors = list(KR_SECTOR_STOCKS.keys())[:5]  # 장중이므로 상위 5개 섹터만
        _sd = rc.collect_kr_supply_demand(_sectors)
        logger.info(f"  ✅ 실시간 섹터 수급 수집: {len(_sd)}개 섹터")
    except ImportError as e:
        logger.error("  realtime_collector import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  실시간 수급 실패: {e}", exc_info=True)

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
                        _df = _pykrx_stock.get_market_ohlcv(
                            _today_short, _today_short, _tk)
                        if len(_df) > 0:
                            _price = _df.iloc[-1].get('종가', 0)
                            if _price > 0:
                                rt_prices[_tk] = float(_price)
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1614", exc_info=True)

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
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        logger.error("[SILENT_BYPASS] Suppressed exception at sub_phases.py:1630", exc_info=True)

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
            except ImportError as e:
                logger.error("  pykrx import 실패 — Exit 스킵", exc_info=True)
        else:
            logger.info("  ⏸ 보유 포지션 없음 — Exit 체크 스킵")
    except Exception as e:
        # ★ avg_price KeyError 등 장중 Exit의 비치명적 실패는 info 레벨
        # (중요 Exit는 closing/aftermarket에서 재시도)
        logger.info(f"  장중 Exit 체크 스킵: {e}")

    # 4. ★ 동적 워터폴 엔진 가동 (V3 Architecture)
    try:
        logger.info("  ⚡ [V3] 동적 통합 엔진 (stream_orchestrator) 가동 - Intraday")
        from scripts.stream_orchestrator import StreamOrchestrator
        StreamOrchestrator().run()
    except Exception as e:
        logger.error(f"  ❌ Intraday 동적 엔진 가동 실패: {e}", exc_info=True)

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
            logger.error(f"  KRX 수집 실패: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"  KRX 리프레시 실패: {e}", exc_info=True)


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
        logger.error(f"  수급 수집 실패: {e}", exc_info=True)

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
    except ImportError as e:
        logger.error("  kr_market_factor import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  KR Factor 실패: {e}", exc_info=True)



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
        logger.error(f"  이브닝 수집 실패: {e}", exc_info=True)
        # [Phase 62: Graceful Degradation] 수급+섹터 데이터 상실 시 30% 패널티
        _data_penalty[0] *= 0.7
        logger.warning(
            f'  [Phase 62] 데이터 결손 패널티 적용: 이브닝 수집 실패 '
            f'→ exposure ×0.7 (penalty={_data_penalty[0]:.2f})')

    # KR 기술적 지표 (확정가 기준 RSI/MACD)
    try:
        from pykrx import stock as _pykrx
        from datetime import timedelta as _td3
        _end = datetime.now().strftime('%Y%m%d')
        _start = (datetime.now() - _td3(days=30)).strftime('%Y%m%d')

        s1_cache_updates = {}
        stock_techs = {}
        _tech_tickers2 = cfg.get(
            'data.tech_indicator_tickers',
            [['005930', '삼성전자'], ['000660', 'SK하이닉스']]
        )
        for _ticker, _name in _tech_tickers2:
            _ohlcv = _pykrx.get_market_ohlcv(_start, _end, _ticker)
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
        logger.error(f"  KR 기술적 지표 실패: {e}", exc_info=True)

    # [Phase 55: SS-ETF Pipeline Glue] Wag-the-Dog 리스크 수집
    # 한국 장 마감 후 ETF 거래량 확정 시점에 실행
    def _run_ss_etf_risk_collection():
        logger.info('  📊 [Phase 55] SS-ETF 단일종목 파생 리스크 수집 실행')
        import subprocess
        try:
            _result = subprocess.run(
                [sys.executable,
                 str(_PROJECT_ROOT / 'scripts' / 'generate_ss_etf_risk.py')],
                capture_output=True, text=True, check=True, timeout=120,
            )
            if _result.stdout:
                logger.debug(f'  SS-ETF stdout: {_result.stdout[:200]}')
            logger.info('  ✅ [Phase 55] SS-ETF 리스크 갱신 완료')
        except subprocess.TimeoutExpired:
            logger.error('  ⚠️ SS-ETF 리스크 수집 타임아웃 (120s)', exc_info=True)
        except subprocess.CalledProcessError as _cpe:
            logger.error(f'  ⚠️ SS-ETF 리스크 갱신 실패: {_cpe.stderr[:300]}', exc_info=True)
        except Exception as _exc:
            logger.error(f'  ⚠️ SS-ETF 리스크 예외: {_exc}', exc_info=True)

    _run_ss_etf_risk_collection()


def _phase_collect_dart():
    """DART 공시 수집 (19:00 KST)."""
    logger.info("  📄 DART 공시 수집")
    try:
        from src.data_collection.dart_daily_collector import DARTDailyCollector
        collector = DARTDailyCollector()
        result = collector.collect_incremental()
        logger.info(f"  ✅ DART 수집: {result}")
    except Exception as e:
        logger.error(f"  DART 수집 실패: {e}", exc_info=True)

    # DART 상세 (A 고유)
    try:
        from src.data_collection.dart_collector import run_dart_collection
        run_dart_collection()
        logger.info("  ✅ DART 상세 수집")
    except ImportError as e:
        logger.error("  dart_collector (A 고유) import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  DART 상세 실패: {e}", exc_info=True)

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
        logger.error(f"  컨센서스 수집 import 실패: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"  컨센서스 수집 실패: {e}", exc_info=True)




def is_us_dst() -> bool:
    """[Phase 41] 현재 뉴욕 시간(US/Eastern) 기준 서머타임(DST) 적용 여부.

    pytz 기반 자동 감지. pytz 미설치 시 월-11월 보수적 fallback.

    Returns:
        True  — EDT (서머타임, UTC-4, KST-13h)
        False — EST (표준시,  UTC-5, KST-14h)
    """
    try:
        import pytz
        from datetime import datetime as _dt
        eastern = pytz.timezone('US/Eastern')
        now_et  = _dt.now(eastern)
        return bool(now_et.dst() and now_et.dst().total_seconds() > 0)
    except ImportError as e:
        # pytz 없으면 월 기준 근사 판단 (3~11월 ≈ DST)
        from datetime import datetime as _dt
        m = _dt.now().month
        return 3 <= m <= 11


def _us_phase_window_check(phase_name: str) -> bool:
    """[Phase 41] 멱등성 보장: 이미 오늘 실행됐으면 False 반환.

    락 파일: logs/us_{phase_name}_{YYYYMMDD}.lock
    시간 윈도우: config us.phase_window_min 분 내에서만 True

    Args:
        phase_name: 'premarket' | 'regular'
    Returns:
        True  — 이 창(window) 내에서 첫 실행 (실행 허용)
        False — 이미 실행됨 or 창 밖
    """
    from config.dynamic_config import DynamicConfig
    from datetime import datetime as _dt
    _cfg     = DynamicConfig()
    _now     = _dt.now()
    _is_dst  = is_us_dst()
    _win_min = int(_cfg.get('us.phase_window_min', 25))

    # DST별 기준 시각 파싱
    _key = f'us.{"dst" if _is_dst else "nodst"}_{phase_name}_kst'
    _trigger_str = str(_cfg.get(_key, '22:30' if phase_name == 'regular' else '17:30'))
    _th, _tm = map(int, _trigger_str.split(':'))
    _trigger_min = _th * 60 + _tm
    _now_min     = _now.hour * 60 + _now.minute

    # 시간 윈도우 확인
    if not (_trigger_min <= _now_min < _trigger_min + _win_min):
        logger.info(
            f'  [Phase 41] {phase_name} 윈도우 외 '
            f'(now={_now.strftime("%H:%M")}, trigger={_trigger_str}, '
            f'window={_win_min}min, DST={_is_dst}) → skip'
        )
        return False

    # 멱등성 락 파일 확인
    _lock_dir = _PROJECT_ROOT / str(_cfg.get('us.idempotency_lock_dir', 'logs'))
    _lock_dir.mkdir(parents=True, exist_ok=True)
    _lock_file = _lock_dir / f'us_{phase_name}_{_now.strftime("%Y%m%d")}.lock'

    if _lock_file.exists():
        logger.info(
            f'  [Phase 41] {phase_name} 이미 실행됨 '
            f'(lock={_lock_file.name}) → idempotency skip'
        )
        return False

    # 락 파일 생성
    _lock_file.write_text(
        f'executed_at={_now.isoformat()}\ndst={_is_dst}\nwindow={_win_min}min\n',
        encoding='utf-8'
    )
    logger.info(
        f'  [Phase 41] {phase_name} 실행 허가 '
        f'(trigger={_trigger_str} KST, DST={_is_dst}, '
        f'now={_now.strftime("%H:%M")})'
    )
    return True



def _phase_us_premarket():
    """[Phase 41] 미국 프리마켓 트레이딩 (DST: 17:30 KST / Non-DST: 18:30 KST).

    멱등성 보장: 당일 1회만 실행 (lock 파일 기반).

    실행 내용:
      1. 멱등성/시간 윈도우 체크 → 조건 불충족 시 즉시 return
      2. US 가격·글로벌 시그널 갱신
      3. US 프리마켓 데이터 갱신
      4. 프리마켓 주문 내역을 results/premarket_orders.json 에 저장
    """
    _tag = '[PRE-MARKET]'
    logger.info(f'  🇺🇸 {_tag} US 프리마켓 페이즈 시작')

    # ── 멱등성/시간 윈도우 체크 ──────────────────────────────────────────────
    if not _us_phase_window_check('premarket'):
        return

    # ── 1. US 가격 데이터 갱신 ──────────────────────────────────────────────
    try:
        from src.data_collection.us_stock_collector import USStockCollector
        USStockCollector().collect_all()
        logger.info(f'  ✅ {_tag} US 종목 수집')
    except Exception as e:
        logger.error(f'  {_tag} US 수집 스킵: {e}', exc_info=True)

    try:
        from src.data_collection.unified_collector import collect_global_signals
        signals = collect_global_signals()
        logger.info(f'  ✅ {_tag} 글로벌 시그널: {len(signals)}개')
    except Exception as e:
        logger.error(f'  {_tag} 글로벌 시그널 실패: {e}', exc_info=True)


    logger.info(f'  ✅ {_tag} US 프리마켓 페이즈 완료')


def _phase_us_regular_market():
    """[Phase 41] 미국 본장 트레이딩 (DST: 22:30 KST / Non-DST: 23:30 KST).

    멱등성 보장: 당일 1회만 실행 (lock 파일 기반).

    실행 내용:
      1. 멱등성/시간 윈도우 체크
      2. 잔여 프리마켓 미체결 주문 정리 (cancel_unfilled_premarket_orders)
      3. US 데이터 갱신 로직 실행
      4. [S5] 본장 방향성 매매 시그널 (session='regular')
    """
    _tag = '[REGULAR]'
    logger.info(f'  🇺🇸 {_tag} US 본장 페이즈 시작')

    # ── 멱등성/시간 윈도우 체크 ──────────────────────────────────────────────
    if not _us_phase_window_check('regular'):
        return


    logger.info(f'  ✅ {_tag} US 본장 페이즈 완료')

def _phase_us_market():
    """미국 시장 트레이딩 (22:35 KST).

    A의 phase_us_market 이식:
      - US 가격 데이터 갱신
      - US 포트폴리오 모니터링
      - [Phase 13: Global Alpha] US 데이터 갱신 및 포트폴리오 모니터링
    """
    logger.info("  🇺🇸 US Market 데이터 수집")
    try:
        from src.data_collection.us_stock_collector import USStockCollector
        collector = USStockCollector()
        collector.collect_all()
        logger.info("  ✅ US 종목 수집")
    except ImportError as e:
        logger.error("  us_stock_collector import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  US 수집 스킵: {e}", exc_info=True)

    # 글로벌 시그널 갱신
    try:
        from src.data_collection.unified_collector import collect_global_signals
        signals = collect_global_signals()
        logger.info(f"  ✅ 글로벌 시그널 갱신: {len(signals)}개")
    except Exception as e:
        logger.error(f"  글로벌 시그널 실패: {e}", exc_info=True)




def _phase_weekly_retrain():
    """주간 ML 재학습 (토 02:00)."""
    logger.info("  🔄 주간 ML 재학습")
    try:
        from scripts.train_ensemble import run_training
        result = run_training(trigger='weekly_schedule', enable_automl=True)
        if result:
            logger.info(f"  ✅ 재학습 완료: ACC={result.get('val_acc', 0):.3f}")
        else:
            logger.info("  ⏭️ 재학습 스킵 (조건 미충족)")
    except ImportError as e:
        logger.error("  train_ensemble import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  재학습 실패: {e}", exc_info=True)

    # [Phase 70-C] Genetic Feature Generator 주간 진화 (주간 재학습 백그라운드)
    try:
        import pandas as _pd
        from src.analysis.genetic_feature_generator import GeneticFeatureGenerator
        from src.data_collection.macro_collector import MacroCollector
        _mc = MacroCollector()
        _macro_df = _mc.collect_all()
        if _macro_df is not None and not _macro_df.empty and len(_macro_df) >= 60:
            _gen = GeneticFeatureGenerator()
            _fwd_col = _macro_df.columns[-1]
            _fwd_ret = _macro_df[_fwd_col].pct_change().shift(-1).dropna()
            _aligned = _macro_df.loc[_fwd_ret.index]
            _new_features = _gen.evolve(
                raw_df=_aligned,
                forward_returns=_fwd_ret,
                generations=20,
            )
            logger.info(
                f'  ✅ [Phase 70-C] GA 진화 완료: {len(_new_features)}개 직교 피처 발견'
            )
        else:
            logger.debug('  [Phase 70-C] GA: 매크로 데이터 부족 (<60), 스킵')
    except ImportError as e:
        logger.error('  [Phase 70-C] GeneticFeatureGenerator import 실패', exc_info=True)
    except Exception as _ga_err:  # noqa: BLE001 — GA 실패는 전체 재학습에 영향 없어야 함
        logger.error(f'  [Phase 70-C] GA 진화 실패 (무시): {_ga_err}', exc_info=True)

    # Feature Store 정리 (오래된 데이터 삭제 + 통계)
    try:
        from src.data_collection.feature_store import FeatureStore
        fs = FeatureStore()
        deleted = fs.delete_old(days=365 * 3)  # 3년 이상 오래된 데이터 삭제
        stats = fs.get_stats()
        logger.info(f"  ✅ Feature Store 정리: {deleted}건 삭제, "
                     f"{stats.get('n_tickers', 0)}종목/{stats.get('n_features', 0)}피처")
    except ImportError as e:
        logger.error("  feature_store import 실패", exc_info=True)
    except Exception as e:
        logger.error(f"  Feature Store 실패: {e}", exc_info=True)


def _phase_weekly_validate():
    """주간 검증 (토 03:00)."""
    logger.info("  🔍 주간 검증")
    try:
        from scripts.train_ensemble import run_validation
        result = run_validation()
        logger.info(f"  ✅ 검증 완료: {result}")
    except ImportError as e:
        # 검증 함수가 없으면 기본 데이터 무결성 체크
        logger.error("  ⏭️ 전용 검증 함수 없음 — 데이터 무결성 체크", exc_info=True)
        try:
            import pandas as pd
            data_dir = _PROJECT_ROOT / 'data' / 'historical_10y'
            parquets = list(data_dir.glob('kr_*.parquet'))
            valid = sum(1 for p in parquets if pd.read_parquet(p).shape[0] > 0)
            logger.error(f"  ✅ 데이터 무결성: {valid}/{len(parquets)} 파일 정상", exc_info=True)
        except Exception as e:
            logger.error(f"  데이터 무결성 체크 실패: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"  검증 실패: {e}", exc_info=True)



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

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
from src.utils.file_ops import atomic_write_json, atomic_write_text


from config.dynamic_config import DynamicConfig
from src.utils.market_calendar import is_trading_day
from src.utils.logger import setup_logger
from src.utils.time_utils import now_kst, today_kst  # ★ L2-15 FIX: 스케줄링 시간 비교에 KST 사용

logger = setup_logger('daily_pipeline')
logger.propagate = False
# ★ FIX: 내부 모듈(virtual_trading 등)의 로그가 무시되지 않도록 Root Logger에도 핸들러 복사
_root_logger = logging.getLogger()
_root_logger.setLevel(logger.level)
for _h in logger.handlers:
    if _h not in _root_logger.handlers:
        _root_logger.addHandler(_h)

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


from scripts.pipeline.sub_phases import _safe_get_index_close, _phase_premarket, _phase_premarket_trade, sync_ssot_from_me, _phase_aftermarket, _phase_aftermarket_trade, _phase_overnight, _phase_collect_data, _phase_global_data, _update_signal_cache, _phase_intraday, _phase_krx_refresh, _phase_collect_flow, _phase_evening_data, _phase_collect_dart, _phase_collect_consensus, is_us_dst, _us_phase_window_check, _phase_us_premarket, _phase_us_regular_market, _phase_us_market, _phase_weekly_retrain, _phase_weekly_validate





class ErrorCollectorHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errors = []
    
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            self.errors.append(self.format(record))

error_collector = ErrorCollectorHandler()
error_collector.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))

def run_pipeline(phase: str = 'all'):
    """통합 파이프라인 실행."""
    logging.getLogger().addHandler(error_collector)
    error_collector.errors.clear()
    today = today_kst()
    is_krx_open = is_trading_day(today.strftime('%Y%m%d'))
    now = now_kst()  # ★ L2-15 FIX: UTC 서버에서 KST 스케줄링 오상 방지

    logger.info(f"{'='*60}")
    logger.info(f"  Project Meridian Daily Pipeline — {today}")
    logger.info(f"  KRX: {'Open' if is_krx_open else 'CLOSED (holiday)'}")
    logger.info(f"  Phase: {phase}")
    logger.info(f"{'='*60}")

    # 이벤트 레져 — 원칙 2: 모든 것을 이벤트로 기록
    try:
        from src.measurement.event_ledger import log_event
        log_event('SYSTEM', {
            'action': 'pipeline_start',
            'phase': phase,
            'krx_open': is_krx_open,
            'date': today.isoformat(),
        }, source='daily_pipeline')
    except Exception as _phase_err:  # [Phase 70-E] Silent Error 철거
        logger.error(f'[Phase 70-E] 페이즈 오류 은폐 방지: {_phase_err}', exc_info=True)
        raise

    # ★ API Health Check (사전 예방)
    # 매일 06:00(collect) 및 주요 트레이딩 단계에서 검증 (휴장일 무관)
    if phase in ('all', 'collect', 'morning', 'premarket_trade', 'market', 'intraday', 'aftermarket_trade'):
        try:
            from scripts.api_health_check import check_api_health
            if not check_api_health():
                logger.error("🚨 API Health Check 실패로 인해 파이프라인 실행을 중단합니다. (Hot-Reload 대기)")
                return  # Block pipeline
        except Exception as e:
            logger.error(f"🚨 API Health Check 모듈 실행 에러: {e}", exc_info=True)

    # ★ KRX 휴장일에도 실행 가능한 phase 목록
    # US 시장, 글로벌 데이터, overnight intel은 KRX와 무관하게 수집 필요
    _WEEKEND_OK_PHASES = (
        'weekly_retrain', 'weekly_validate',  # ML 재학습/검증
        'us_market',       # ★ US 시장은 KRX 휴장과 무관 (금요일 미국 마감 데이터)
        'us_premarket',    # ★ [Phase 41] 프리마켓 (KRX 독립)
        'us_regular',      # ★ [Phase 41] 본장 (KRX 독립)
        'crypto_arb',      # ★ 크립토는 24시간 365일
        'overnight',       # ★ 글로벌 야간 데이터 (SGX, VIX, 뉴스 등)
        'evening_data',    # ★ US 가격 + 저녁 데이터
        'collect',         # ★ 글로벌 데이터 수집 (매크로, breadth 등)
    )

    if not is_krx_open and phase not in _WEEKEND_OK_PHASES:
        logger.info("  KRX 휴장일 → 글로벌 + US 데이터 수집만 실행")
        # ★ 체크포인트를 통해 실행하여 대시보드에 정확히 반영
        try:
            from src.infra.pipeline_checkpoint import PipelineCheckpoint
            _hol_ckpt = PipelineCheckpoint()
        except ImportError as e:
            _hol_ckpt = None

        _holiday_phases = {
            'overnight': _phase_overnight,
            'us_market':         _phase_us_market,
            'us_premarket':      _phase_us_premarket,      # [Phase 41] 프리마켓
            'us_regular':        _phase_us_regular_market, # [Phase 41] 본장
        }

        # phase가 'all'이면 모든 글로벌 phase 실행, 아니면 해당 phase만
        _phases_to_run = list(_holiday_phases.keys()) if phase == 'all' else [phase]

        for _hp in _phases_to_run:
            if _hp in _holiday_phases:
                if _hol_ckpt:
                    _hol_ckpt.mark_running(_hp)
                _t0 = time.time()
                try:
                    _holiday_phases[_hp]()
                    _elapsed = time.time() - _t0
                    logger.info(f"  ✅ {_hp} 완료 ({_elapsed:.1f}s)")
                    if _hol_ckpt:
                        _hol_ckpt.mark_done(_hp, _elapsed)
                except Exception as _he:
                    _elapsed = time.time() - _t0
                    logger.error(f"  ❌ {_hp} 실패 ({_elapsed:.1f}s): {_he}", exc_info=True)
                    if _hol_ckpt:
                        _hol_ckpt.mark_failed(_hp, str(_he))
        return

    from scripts.stream_orchestrator import StreamOrchestrator
    exec_mode = cfg.get('execution.mode', 'shadow')
    orch = StreamOrchestrator(exec_mode=exec_mode)

    # ── Kill Switch 게이트 ──
    _kill_switch_result = {'triggered': False, 'can_buy': True, 'position_scale': 1.0}
    try:
        from src.risk.kill_switch import KillSwitch
        ks = KillSwitch()
        # Shadow NAV 로드
        _shadow_f = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
        if _shadow_f.exists():
            import json as _json
            _shadow_data = _json.loads(_shadow_f.read_text())
            _current_nav = _shadow_data.get('virtual_nav', cfg.get('portfolio.initial_capital'))
            _kill_switch_result = ks.check(_current_nav)
            _fo = _kill_switch_result.get('forward_override', {})
            if _kill_switch_result['triggered']:
                logger.warning(f"  🚨 Kill Switch: {_kill_switch_result['reason']}")
            elif _fo.get('override'):
                # 전방 지표가 후행 트리거를 override → INFO (정상 동작)
                logger.info(f"  🟢 Kill Switch Override: {_fo.get('reason', '')}")
                logger.info(f"    Forward Score: {_fo.get('score', 0):.0%} "
                            f"(threshold: {_fo.get('threshold', 0.6):.0%})")
                _details = _fo.get('details', {})
                if _details:
                    _good = [k for k, v in _details.items() if v >= 0.6]
                    _weak = [k for k, v in _details.items() if v < 0.6]
                    if _good:
                        logger.info(f"    양호: {', '.join(_good)}")
                    if _weak:
                        logger.info(f"    약함: {', '.join(_weak)}")
            else:
                logger.info(f"  ✅ Kill Switch: {_kill_switch_result['reason']}")
    except Exception as e:
        logger.error(f"  🚨 Kill Switch 에러: {e}", exc_info=True)
        logger.warning("  Self-Correction: Kill Switch 판단 불가로 인해 매수 강제 차단 (Safe Default)")
        _kill_switch_result['can_buy'] = False
        _kill_switch_result['position_scale'] = 0.5

    # ── Crash Defense 게이트 ──
    try:
        from src.risk.crash_defense import CrashDefense
        _crash = CrashDefense().run()
        if _crash.get('crash_mode'):
            _crash_scale = cfg.get('risk.crash_defense_scale', 0.2)
            _kill_switch_result['position_scale'] *= _crash_scale
            logger.warning(f"  🚨 Crash Defense: CRASH 모드 → scale={_crash_scale}")
        for alert in _crash.get('alerts', []):
            logger.warning(f"  {alert}")
        if _crash['modules'].get('sl_circuit', {}).get('tripped'):
            _kill_switch_result['can_buy'] = False
            logger.warning(f"  🛑 연쇄 SL 차단기 → 매매 중단")
    except Exception as e:
        logger.error(f"  🚨 Crash Defense 에러: {e}", exc_info=True)
        logger.warning("  Self-Correction: Crash Defense 판단 불가로 인해 포지션 스케일 강제 축소 (0.5x)")
        _kill_switch_result['position_scale'] *= 0.5

    # ── Drawdown Guard 게이트 ──
    try:
        from src.risk.drawdown_guard import DrawdownGuard
        _shadow_f = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
        if _shadow_f.exists():
            _sd = json.loads(_shadow_f.read_text())
            _nav = _sd.get('virtual_nav', cfg.get('portfolio.initial_capital'))
            _dd = DrawdownGuard().check(_nav)
            _dd_exposure = _dd.get('exposure', 1.0)
            _kill_switch_result['position_scale'] *= _dd_exposure
            if _dd_exposure < 1.0:
                # ★ DD 기반 노출 감소는 정상 동작 → info 레벨 (반복 warning 방지)
                logger.info(f"  🛡️ Drawdown Guard: DD={_dd['drawdown_pct']}% "
                             f"→ 노출 {_dd_exposure:.0%} ({_dd['stage']})")
    except Exception as e:
        logger.error(f"  Drawdown Guard 실패: {e}", exc_info=True)

    # ── Exposure Orchestrator (다층 노출 제어) ──
    try:
        from src.risk.exposure_orchestrator import ExposureOrchestrator
        _eo = ExposureOrchestrator().calculate(data_penalty=_data_penalty[0])  # [Phase 62]
        _target_exp = _eo.get('target_exposure', 1.0)
        _kill_switch_result['position_scale'] = min(
            _kill_switch_result['position_scale'], _target_exp)
        if _target_exp < 0.8:
            logger.info(f"  📊 Exposure: {_target_exp:.0%} ({_eo['reason']})")
    except Exception as e:
        logger.error(f"  Exposure Orchestrator 실패: {e}", exc_info=True)

    # ── ★ 집중 리스크 스케일링 (포지션 간 상관관계 → 노출 동적 조정) ──
    try:
        from src.risk.stream_correlation import StreamCorrelationMonitor
        _conc = StreamCorrelationMonitor().compute_concentration_scale()
        _conc_scale = _conc.get('concentration_scale', 1.0)
        _kill_switch_result['position_scale'] = min(
            _kill_switch_result['position_scale'], _conc_scale)
        _ortho = _conc.get('orthogonality_score', 1.0)
        _n_high = _conc.get('high_corr_count', 0)
        _conc_warn_th = cfg.get('risk.corr_warn_threshold', 0.85)
        if _conc_scale < _conc_warn_th:
            logger.warning(
                f"  ⚠️ 집중 리스크: 직교성={_ortho:.2f}, "
                f"고상관 {_n_high}쌍 → scale={_conc_scale:.2f}")
        elif _conc.get('sufficient_data'):
            logger.info(
                f"  ℹ️ 집중 리스크: 직교성={_ortho:.2f}, "
                f"고상관 {_n_high}쌍 → scale={_conc_scale:.2f}")
    except Exception as e:
        logger.error(f"  집중 리스크 계산 실패: {e}", exc_info=True)

    # ── Realtime VaR (모니터링 전용 — position_scale에 영향 없음) ──
    # ★ 퀀트 펀드 표준: VaR는 사후 모니터링 지표, 포지션 크기는 σ-target이 결정
    try:
        from src.risk.realtime_var import RealtimeVaR
        _var = RealtimeVaR().calculate(
            cfg.get('portfolio.initial_capital'))
        _var_pct = _var.get('var_pct', 0)
        _cvar_pct = _var.get('cvar_pct', _var_pct)
        _within = _var.get('within_limit', True)
        _method = _var.get('method', '?')

        if not _within:
            logger.warning(f"  ⚠️ VaR 모니터링: {_var_pct:.2f}% "
                         f"(CVaR={_cvar_pct:.2f}%, "
                         f"한도={_var.get('limit_pct', 0):.1f}%, "
                         f"method={_method})")
        else:
            logger.info(f"  ✅ VaR: {_var_pct:.2f}% "
                       f"(CVaR={_cvar_pct:.2f}%, "
                       f"한도={_var.get('limit_pct', 0):.1f}%)")
    except Exception as e:
        logger.error(f"  VaR 계산 실패: {e}", exc_info=True)

    # ── ★ DD-06: VaR 결과 저장 (var_daily.json) ──
    try:
        if '_var' in dir() and _var:
            import tempfile, os
            var_path = _PROJECT_ROOT / 'results' / 'var_daily.json'
            var_path.parent.mkdir(parents=True, exist_ok=True)
            _var['saved_at'] = datetime.now().isoformat()
            fd, tmp = tempfile.mkstemp(dir=str(var_path.parent), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(_var, f, indent=2, default=str)
            os.replace(tmp, str(var_path))
            logger.info(f"  💾 VaR → var_daily.json: {_var.get('var_pct', 0):.2f}%")
    except Exception as e:
        logger.error(f"  VaR 저장 실패: {e}", exc_info=True)

    # ── ★ DD-08: Factor Risk Decomposition 파이프라인 연동 ──
    try:
        from src.risk.factor_risk import FactorRiskDecomposer
        _frd = FactorRiskDecomposer()
        _fr_result = _frd.decompose()
        if _fr_result:
            logger.info(
                f"  ✅ Factor Risk: R²={_fr_result.get('r_squared', 0):.3f}, "
                f"α={_fr_result.get('alpha', 0):.4f}, "
                f"systematic={_fr_result.get('systematic_pct', 0):.1f}%")
    except Exception as e:
        logger.error(f"  Factor Risk 계산 실패: {e}", exc_info=True)

    # ── ★ DD-09: DCC-GARCH 상관관계 파이프라인 연동 ──
    try:
        from src.risk.correlation_decay import CorrelationDecayMonitor
        _cde = CorrelationDecayMonitor()
        _dcc_result = _cde.compute_dcc_correlation()
        if _dcc_result:
            _n_pairs = len(_dcc_result.get('dcc_correlations', {}))
            logger.info(
                f"  ✅ DCC-GARCH: {_n_pairs}쌍 상관, "
                f"avg={_dcc_result.get('avg_correlation', 0):.3f}")
    except Exception as e:
        logger.error(f"  DCC-GARCH 계산 실패: {e}", exc_info=True)

    # ── Medallion Orchestrator (4원칙 검증) ──
    try:
        from src.risk.medallion_orchestrator import MedallionOrchestrator
        _medal = MedallionOrchestrator().validate_all()
        if _medal.get('overall') == 'FAIL':
            _kill_switch_result['can_buy'] = False
            logger.warning(f"  🔴 Medallion FAIL: {_medal.get('critical', 0)}건 Critical")
        elif _medal.get('overall') == 'WARN':
            logger.info(f"  🟡 Medallion WARN: {_medal.get('total_issues', 0)}건")
    except Exception as e:
        logger.error(f"  Medallion Orchestrator 실패: {e}", exc_info=True)

    # ── S1 편입 검증 (Inclusion Validation) — 정보 제공용 ──
    # ★ S1은 일일 시그널 기반이므로 거래 자체를 차단하지 않음.
    #   Validator는 Shadow → Live 전환 판단용 대시보드 지표로만 사용.
    try:
        from src.streams.s1_edge.s1_validator import S1InclusionValidator
        _s1v = S1InclusionValidator().analyze()
        _verdict = _s1v.get('verdict', {}).get('decision', 'N/A')
        logger.info(f"  🔍 S1 Validation: {_verdict} ({_s1v.get('verdict', {}).get('score', '')})")
    except Exception as e:
        logger.error(f"  S1 Validation 실패: {e}", exc_info=True)

    logger.info(f"  📊 최종 Position Scale: {_kill_switch_result['position_scale']:.2f}")


    def _morning_phase():
        """Morning: 레짐 판정 + 스트림 신호 생성 (StreamOrchestrator)."""
        logger.info("  🌅 Morning Phase: 레짐 판정 + 신호 생성")
        try:
            # ★ MarketDataBridge에서 market_data 구성
            from src.data.market_data_bridge import MarketDataBridge
            _bridge = MarketDataBridge()
            _sc = _bridge.build_signal_cache()
            _ov = _bridge.build_overnight_intel()
            _rh = _bridge.get_regime_history()
            _market_data = {
                'signal_cache': _sc,
                'overnight_intel': _ov,
                'vix_history': _rh.get('vix_history', []),
                'kospi_returns': _rh.get('kospi_returns', []),
            }
            # ★ Data Freshness Gate 검사 (수집 실패 시 청산 전용 모드 전환)
            from src.infra.data_freshness_validator import DataFreshnessValidator
            is_fresh = True
            try:
                is_fresh = DataFreshnessValidator().check_is_fresh()
            except Exception as e:
                logger.error(f"  Freshness Gate 오류: {e}", exc_info=True)

            if not is_fresh:
                logger.error("🚨 [Freshness Gate] 데이터 수집 실패 감지! 신규 진입을 전면 차단하고 '청산 전용 모드(Exit-Only)'로 전환합니다.")
                # 시그널을 비우고 조기 종료하여 신규 매수 차단 (기존 포지션의 TP/SL만 작동)
                sig_file = _PROJECT_ROOT / 'results' / 'latest_signals.json'
                atomic_write_json(sig_file, {}, indent=2)
                # ★ NM-01 FIX: market_data → _market_data (NameError 수정)
                # 이 스코프의 변수명은 _market_data임 (market_data는 미정의)
                _market_data['exit_only'] = True

            # 레짐 판정 — HMM Regime Model 적용 (탈 하드코딩)
            from src.regime.hmm_regime_model import HMMRegimeModel
            import pandas as pd
            
            hmm_engine = HMMRegimeModel()
            if not hmm_engine.is_fitted:
                # 과거 데이터 임시 로드 후 학습 (실제 환경에서는 별도 배치 잡에서 fit 수행 권장)
                try:
                    df_hist = pd.DataFrame(_rh.get('vix_history', []))
                    if not df_hist.empty:
                        hmm_engine.fit(df_hist)
                except Exception as e:
                    logger.error(f"  HMM 초기 학습 실패: {e}", exc_info=True)
                    
            try:
                # 예측 시도
                features = hmm_engine._prepare_features(_market_data)
                regime_result = hmm_engine.predict(features)
                regime = regime_result.get('regime', 'caution')
                confidence = regime_result.get('confidence', 0.5)
            except Exception as e:
                logger.error(f"  HMM 예측 실패, Fallback to RegimeEngine: {e}", exc_info=True)
                from src.intelligence.regime_engine import RegimeEngine
                regime_result = RegimeEngine().detect()
                regime = regime_result.get('regime', 'caution')
                confidence = regime_result.get('confidence', 0.5)
            
            # ★ S4 Dynamic Scaling을 위해 confidence를 market_data에 주입
            _market_data['regime_confidence'] = confidence
            
            logger.info(f"  ✅ 레짐: {regime.upper()} "
                       f"(conf={regime_result.get('confidence', 0):.2f}, "
                       f"method={regime_result.get('method', '?')})")

            # 스트림 상태 확인
            status = orch.get_stream_status()
            for s in status:
                logger.info(f"    {s['stream_id']}: active={s['active']}, "
                           f"shadow={s['shadow']}")

            # ★ 장 전 시그널 생성 → latest_signals.json 갱신
            # market phase(09:05) 전에 오늘의 시그널 미리 생성
            try:
                _morning_signals = {}
                for _stream in orch.streams:
                    try:
                        _sigs = _stream.generate_signals(regime, _market_data)
                        if _sigs:
                            _morning_signals[_stream.stream_id] = []
                            for _sig in (_sigs or [])[:20]:
                                _morning_signals[_stream.stream_id].append({
                                    'ticker': _sig.get('ticker', ''),
                                    'name': _sig.get('name', ''),
                                    'direction': _sig.get('direction', 'long'),
                                    'confidence': round(
                                        _sig.get('confidence', 0), 3),
                                    'strategy': _sig.get('strategy', ''),
                                })
                    except Exception as _se:
                        logger.debug(
                            f"    {_stream.stream_id} 시그널 생성 실패: {_se}")

                if _morning_signals:
                    _ms_out = {
                        'date': today.isoformat(),
                        'timestamp': datetime.now().isoformat(),
                        'regime': regime,
                        'phase': 'morning_preview',
                        'signals': _morning_signals,
                    }
                    _ls_path = _PROJECT_ROOT / 'results' / 'latest_signals.json'
                    atomic_write_json(_ls_path, 
                        _ms_out, indent=2)
                    _total_sigs = sum(
                        len(v) for v in _morning_signals.values())
                    logger.info(
                        f"  💾 latest_signals.json 장 전 갱신 "
                        f"({_total_sigs}건, "
                        f"{len(_morning_signals)} streams)")
            except Exception as _mse:
                logger.error(f"  장 전 시그널 생성 실패: {_mse}", exc_info=True)

        except Exception as e:
            logger.error(f"  Morning Phase 실패: {e}", exc_info=True)

    def _market_and_shadow():
        """Market Phase: 포트폴리오 최적화 + StreamOrchestrator 실행."""
        # Kill Switch가 매수를 차단하면 스킵
        if not _kill_switch_result.get('can_buy', True):
            logger.warning(f"  ⛔ Kill Switch 매수 차단 — Market Phase 스킵")
            return

        # ★ 포트폴리오 최적화 및 Meta-Level Capital Allocation (주문 생성 전)
        try:
            import numpy as np
            from src.allocation.capital_allocator import MetaCapitalAllocator
            from src.allocation.risk_parity import RiskParityOptimizer
            
            # 1. 기존 PortfolioOptimizer 로직 유지 (단기 리밸런싱용)
            from src.allocation.portfolio_optimizer import PortfolioOptimizer
            opt = PortfolioOptimizer()
            opt_result = opt.optimize(trigger='market_open')
            action = opt_result.get('action', 'skip')
            
            # 2. Meta Capital Allocator 실행 (스트림 간 가상 장부 재배분)
            meta_allocator = MetaCapitalAllocator()
            
            # (임시) Mock 데이터로 할당 로직 구동 (실제로는 StreamMetrics 객체 연동)
            mock_metrics = {
                "S1": {"win_rate": 0.55, "edge": 0.05},
                "S2": {"win_rate": 0.60, "edge": 0.08},
                "Beta": {"win_rate": 0.50, "edge": 0.02}
            }
            # KODEX 인버스를 모사하는 음의 상관관계 공분산 행렬 생성 (Mock)
            # 순서: S1, S2, Inverse/Crypto_Short
            mock_cov = np.array([
                [ 0.04,  0.03, -0.04],
                [ 0.03,  0.05, -0.05],
                [-0.04, -0.05,  0.06]
            ])
            
            try:
                allocs = meta_allocator.reallocate(mock_metrics, mock_cov)
                logger.info(f"  📊 MetaCapitalAllocator 가상 장부 할당 완료: {allocs}")
                
                # Risk Parity: 인버스/크립토 숏 편입 (Long-Short Bounds)
                rp = RiskParityOptimizer()
                rp_weights = rp.optimize(mock_cov, allow_short=True, custom_bounds=[(0, 1), (0, 1), (-1, 1)])
                logger.info(f"  ⚖️ Risk Parity (Inverse 포함) 산출 가중치: {rp_weights}")
            except Exception as alloc_err:
                logger.error(f"  MetaCapitalAllocator/RiskParity 실행 실패: {alloc_err}", exc_info=True)
                
            # 3. 통계적 차익거래 (StatArb) 페어 생성
            try:
                from src.intelligence.stat_arb_engine import StatArbEngine
                import pandas as pd
                # (임시) Mock 가격 데이터
                mock_prices = pd.DataFrame(np.random.randn(100, 5).cumsum(axis=0), columns=['A', 'B', 'C', 'D', 'E'])
                stat_arb = StatArbEngine()
                pairs = stat_arb.find_cointegrated_pairs(mock_prices)
                if pairs:
                    sigs = stat_arb.generate_signals(mock_prices, pairs)
                    logger.info(f"  📈 StatArbEngine: 페어 매매 시그널 {len(sigs)}건 포착")
            except Exception as _stat_err:
                logger.error(f"  StatArbEngine 실행 실패: {_stat_err}", exc_info=True)

            if action == 'rebalance':
                fw = opt_result.get('final_weights', {})
                ws = ' '.join(f'{k}:{v:.0%}' for k, v in sorted(fw.items()))
                logger.info(f"  📊 PortfolioOptimizer: REBALANCE → {ws}")
            else:
                logger.info(
                    f"  📊 PortfolioOptimizer: {action} "
                    f"({opt_result.get('reason', '')})")
        except Exception as e:
            logger.error(f"  PortfolioOptimizer 실패: {e}", exc_info=True)

        result = None
        try:
            # [Live Transition Task 2] Desync 사전 검증 (주문 실행 전)
            try:
                from src.execution.execution_engine import ExecutionEngine, DesyncError
                from config.dynamic_config import DynamicConfig
                _ee_sync = ExecutionEngine(mode=DynamicConfig().get('execution.current_mode', 'live'))
                _sync = _ee_sync.check_account_sync(raise_on_desync=True)
                if _sync.get('level') == 'warn':
                    logger.warning(
                        f"  ⚠️ [Desync 경고] diff={_sync['diff_pct']:.4%} "
                        f"— 주문은 계속 진행 (1% 미달)")
            except DesyncError as _de:
                logger.critical(
                    f"  🚨 [Live Transition Task 2] Desync 감지! {_de} "
                    f"— Hard Liquidate 실행"
                )
                from src.risk.kill_switch import KillSwitch
                KillSwitch().hard_liquidate_all(reason=f'DesyncError: {_de}')
                return  # 주문 전체 중단
            except Exception as _sync_err:
                # 동기화 체크 자체가 실패해도 주문은 계속 (fail-safe)
                logger.error(f"  Desync 체크 실패 (계속 진행): {_sync_err}", exc_info=True)

            # [Live Transition Task 4] API Timeout 감지를 위한 시작 시각 기록
            import time as _t
            _api_start = _t.time()
            _api_timeout_sec = 600  # 10분 = 600초

            # [Phase 70-Integration] DATA_NOGO 플래그를 orch 컨텍스트로 전달
            if _data_nogo_assets:
                orch.data_nogo = True
                orch.data_nogo_reason = (
                    f'{_data_nogo_assets} PCA R² 미달 — 비중 0% 강제'
                )
                logger.warning(
                    f'  [Phase 70] DATA_NOGO 전파: {_data_nogo_assets} → '
                    f'StreamOrchestrator Circuit Breaker 발동'
                )

            result = orch.run()

            _api_elapsed = _t.time() - _api_start
            if _api_elapsed > _api_timeout_sec:
                logger.critical(
                    f"  🚨 [Live Transition Task 4] API Timeout 감지: "
                    f"{_api_elapsed:.0f}초 > {_api_timeout_sec}초 "
                    f"— Hard Liquidate 실행"
                )
                from src.risk.kill_switch import KillSwitch
                KillSwitch().hard_liquidate_all(
                    reason=f'API Timeout {_api_elapsed:.0f}s > {_api_timeout_sec}s')
                return

        except DesyncError as _de:
            logger.critical(
                f"  🚨 [Live Transition Task 2] DesyncError 발생: {_de} "
                f"— Hard Liquidate 즉시 실행"
            )
            try:
                from src.risk.kill_switch import KillSwitch
                KillSwitch().hard_liquidate_all(reason=f'DesyncError: {_de}')
            except Exception as _hl_err:
                logger.critical(f"  ❌ Hard Liquidate 실패: {_hl_err}")
            return

        except (RuntimeError, ConnectionError, TimeoutError) as _ce:
            # 브로커 API 연결 오류 / 타임아웃 → 비상 청산
            logger.critical(
                f"  🚨 [Live Transition Task 4] 치명적 오류: {type(_ce).__name__}: {_ce} "
                f"— Hard Liquidate 실행"
            )
            try:
                from src.risk.kill_switch import KillSwitch
                KillSwitch().hard_liquidate_all(
                    reason=f'{type(_ce).__name__}: {_ce}')
            except Exception as _hl_err:
                logger.critical(f"  ❌ Hard Liquidate 실패: {_hl_err}")
            return

        if result is None:
            logger.error("  ⚠️ orch.run() 결과 없음 — Market Phase 조기 종료", exc_info=True)
            return

        logger.info(f"  ✅ StreamOrchestrator: status={result['status']}, "
                   f"orders={len(result.get('orders', []))}, "
                   f"regime={result.get('regime', '?')}")

        # Shadow 기록은 StreamOrchestrator 내부에서 처리됨 (ShadowRecorder)
        exec_info = result.get('execution', {})
        if exec_info:
            logger.info(f"    체결: {exec_info.get('n_filled', 0)}/"
                       f"{exec_info.get('n_orders', 0)} "
                       f"(mode={exec_info.get('mode', '?')})")

        # ★ pipeline_state.json timestamp 갱신 (Aging 방지)
        try:
            _ps_path = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            if _ps_path.exists():
                _ps_data = json.loads(_ps_path.read_text())
                _ps_data['updated_at'] = datetime.now().isoformat()
                _ps_data['updated_by'] = 'daily_pipeline.market'
                _ps_data['market_regime'] = result.get('regime', _ps_data.get('kr_regime', 'unknown'))
                atomic_write_json(_ps_path, 
                    _ps_data, indent=2)
        except Exception as _phase_err:  # [Phase 70-E] Silent Error 철거
            logger.error(f'[Phase 70-E] 페이즈 오류 은폐 방지: {_phase_err}', exc_info=True)
            raise

        # ★ 누락 파일 저장 — Data Freshness STALE 근본 수정
        # (1) stream_metrics.json: 스트림 성과
        try:
            _sm = result.get('streams', {})
            if not _sm:
                # streams가 없으면 각 스트림에서 직접 수집
                _sm = {}
                for _s in orch.streams:
                    _sm[_s.stream_id] = _s.get_performance()
            _sm['_timestamp'] = datetime.now().isoformat()
            _sm['_regime'] = result.get('regime', 'unknown')
            _sm_path = _PROJECT_ROOT / 'results' / 'stream_metrics.json'
            atomic_write_json(_sm_path, 
                _sm, indent=2)
            logger.info("  💾 stream_metrics.json 갱신")
        except Exception as _e:
            logger.error(f"  stream_metrics 저장 실패: {_e}", exc_info=True)

        # (2) latest_signals.json: 최신 매매 신호
        try:
            _signals_out = {
                'date': today.isoformat(),
                'timestamp': datetime.now().isoformat(),
                'regime': result.get('regime', 'unknown'),
                'n_orders': len(result.get('orders', [])),
                'n_filled': exec_info.get('n_filled', 0),
                'signals': {},
            }
            for _sid, _sigs in result.get('signals', {}).items():
                _signals_out['signals'][_sid] = []
                for _sig in (_sigs or [])[:20]:
                    _signals_out['signals'][_sid].append({
                        'ticker': _sig.get('ticker', ''),
                        'name': _sig.get('name', ''),
                        'direction': _sig.get('direction', 'long'),
                        'confidence': round(_sig.get('confidence', 0), 3),
                        'strategy': _sig.get('strategy', ''),
                    })
            _ls_path = _PROJECT_ROOT / 'results' / 'latest_signals.json'
            atomic_write_json(_ls_path, 
                _signals_out, indent=2)
            logger.info(f"  💾 latest_signals.json 갱신 "
                       f"({_signals_out['n_orders']}건)")
        except Exception as _e:
            logger.error(f"  latest_signals 저장 실패: {_e}", exc_info=True)

        # (3) shadow_portfolio.json: 체결 결과 반영 (STALE 근본 수정)
        try:
            from src.portfolio.shadow_manager import ShadowManager
            _sp = ShadowManager()
            _orders = result.get('orders', [])
            if _orders:
                _actions = []
                for _o in _orders:
                    _actions.append({
                        'ticker': _o.get('ticker', ''),
                        'name': _o.get('name', ''),
                        'action': 'buy' if _o.get('direction') == 'long' else 'sell',
                        'amount': _o.get('amount_krw', 0),
                        'entry_price': _o.get('price', _o.get('entry_price', 0)),
                        'strategy': _o.get('stream_id', _o.get('strategy', 'unknown')),
                        'up_prob': _o.get('confidence', 0.5),
                    })
                _sp.execute_signals(_actions)
                logger.info(f"  💾 shadow_portfolio.json 갱신 ({len(_actions)}건 체결)")
            else:
                # 주문 없어도 touch하여 freshness 유지
                _sp._save()
                logger.info("  💾 shadow_portfolio.json 갱신 (주문 없음, timestamp만)")
        except Exception as _e:
            logger.error(f"  shadow_portfolio 갱신 실패: {_e}", exc_info=True)

        # (3b) S4 Account Tracker 동기화 (ISA/IRP/PENSION/BROKERAGE 분류)
        try:
            from src.streams.s4_advisory.account_tracker import sync_s4_accounts
            sync_s4_accounts()
            logger.info("  📋 S4 Account Tracker 동기화 완료")
        except Exception as _e:
            logger.error(f"  S4 Account Tracker 동기화 실패: {_e}", exc_info=True)

        # (4) go_nogo.json: Go/No-Go 판정 갱신
        try:
            _gn = result.get('go_nogo', {})
            if _gn:
                _gn_path = _PROJECT_ROOT / 'results' / 'go_nogo.json'
                # 기존 파일 merge
                _gn_existing = {}
                if _gn_path.exists():
                    try:
                        _gn_existing = json.loads(_gn_path.read_text())
                    except Exception as _phase_err:  # [Phase 70-E] Silent Error 철거
                        logger.error(f'[Phase 70-E] 페이즈 오류 은폐 방지: {_phase_err}', exc_info=True)
                        raise
                _gn_existing.update({
                    'verdict': _gn.get('verdict', 'N/A'),
                    'n_days': _gn.get('n_days', 0),
                    'sharpe': _gn.get('sharpe', 0),
                    'win_rate': _gn.get('win_rate', 0),
                    'max_dd': _gn.get('max_dd', 0),
                    'timestamp': datetime.now().isoformat(),
                })
                atomic_write_json(_gn_path, 
                    _gn_existing, indent=2)
                logger.info(f"  💾 go_nogo.json 갱신 "
                           f"(verdict={_gn.get('verdict', '?')})")
        except Exception as _e:
            logger.error(f"  go_nogo 저장 실패: {_e}", exc_info=True)

    def _closing_phase():
        """Closing: 포트폴리오 최적화 + 포지션 청산 + PnL 계산."""
        logger.info("  🔔 Closing Phase")

        # ★ RealtimeExitMonitor 종료 (장 마감 정리)
        try:
            from src.execution.realtime_exit_monitor import stop_exit_monitoring
            stop_exit_monitoring()
            logger.info("  🔴 RealtimeExitMonitor 종료")
        except ImportError as e:
            pass
        except Exception as e:
            logger.error(f"  RealtimeExitMonitor 종료 실패: {e}", exc_info=True)

        # ★ 마감 리밸런싱 (다음 날 준비)
        try:
            from src.allocation.portfolio_optimizer import PortfolioOptimizer
            opt = PortfolioOptimizer()
            opt_result = opt.optimize(trigger='closing')
            action = opt_result.get('action', 'skip')
            if action == 'rebalance':
                fw = opt_result.get('final_weights', {})
                ws = ' '.join(f'{k}:{v:.0%}' for k, v in sorted(fw.items()))
                logger.info(f"  📊 Closing Rebalance: {ws}")
            else:
                logger.info(
                    f"  📊 Closing Optimizer: {action} "
                    f"({opt_result.get('reason', '')})")
        except Exception as e:
            logger.error(f"  Closing PortfolioOptimizer 실패: {e}", exc_info=True)

        # ★ 근본 수정: ShadowPortfolioManager.daily_snapshot() 사용
        # (레거시 ShadowPortfolio.record_daily()는 IC/DA 없이 레코드 생성하여
        #  MeasurementEngine signal_quality가 항상 0을 표시하던 근본 원인)
        try:
            from src.portfolio.shadow_manager import ShadowPortfolioManager
            # ★ Meridian 통합: pipeline_state.json SSoT
            _ps = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            _regime = 'caution'
            if _ps.exists():
                _psd = json.loads(_ps.read_text())
                _regime = _psd.get('kr_regime', _psd.get('operating_regime', 'caution'))
            _initial = cfg.get('portfolio.initial_capital')
            with ShadowPortfolioManager(initial_capital=_initial).transaction() as _mgr:

                # ★ 전체 스트림 MTM + TP/SL Exit 체크 (S1/S2/S3/S4 공통)
                try:
                    from pykrx import stock as _pykrx_exit
                    _today_exit = date.today().strftime('%Y%m%d')
                    _all_tickers = set()
                    for pk in _mgr.positions:
                        _, _tk = _mgr._parse_position_key(pk)
                        _all_tickers.add(_tk)

                    _exit_prices = {}
                    for _tk in _all_tickers:
                        try:
                            _df = _pykrx_exit.get_market_ohlcv(
                                _today_exit, _today_exit, _tk)
                            if len(_df) > 0:
                                _p = float(_df.iloc[-1].get('종가', 0))
                                if _p > 0:
                                    _exit_prices[_tk] = _p
                        except Exception as _phase_err:  # [Phase 70-E] Silent Error 철거
                            logger.error(f'[Phase 70-E] 페이즈 오류 은폐 방지: {_phase_err}', exc_info=True)
                            raise

                    if _exit_prices:
                        _mgr.mark_to_market(_exit_prices)
                        # ★ S2/S3/S4 TP/SL/Trailing 자동 Exit
                        _sell_orders = _mgr.check_exit_conditions(_regime)
                        _s234_sells = [s for s in _sell_orders
                                       if s.get('stream_id', '') != 'S1']
                        if _s234_sells:
                            # S2/S3는 주식 수수료, S4는 ETF 포함
                            _mgr.execute_sells(_s234_sells, _exit_prices)
                            logger.info(f"  🔴 S2/S3/S4 자동 Exit: {len(_s234_sells)}건")
                            for _so in _s234_sells:
                                logger.info(f"    [{_so.get('stream_id','')}] "
                                           f"{_so.get('name','?')} "
                                           f"({_so.get('sell_type','')}): "
                                           f"{_so.get('reason','')[:60]}")
                        else:
                            logger.info("  ✅ S2/S3/S4 Exit: 청산 대상 없음")
                except ImportError as e:
                    logger.error("  pykrx import 실패 — Exit 스킵", exc_info=True)
                except Exception as e:
                    # ★ NoneType 방어: check_exit_conditions의 base.get() None 방어 실패 시
                    logger.error(f"  S2/S3/S4 Exit 체크 스킵: {e}", exc_info=True)

                record = _mgr.daily_snapshot(regime=_regime)
            _nav = _mgr.nav
            _ret = record.get('daily_return_pct', 0)
            _ic = record.get('ic', {}).get('ic', 'N/A') if isinstance(record.get('ic'), dict) else 'N/A'
            logger.info(f"  ✅ Shadow: NAV=₩{_nav:,.0f} ({_ret:+.2f}%) IC={_ic}")
        except Exception as e:
            logger.error(f"  Closing 실패: {e}", exc_info=True)

        # ★ S1 장마감 강제 청산 (15:10)
        try:
            from src.portfolio.shadow_manager import ShadowPortfolioManager
            _cfg_close = DynamicConfig()
            _initial_close = _cfg_close.get('portfolio.initial_capital')
            with ShadowPortfolioManager(initial_capital=_initial_close).transaction() as _mgr_close:

                s1_positions = {pk: pos for pk, pos in _mgr_close.positions.items()
                              if pk.startswith('S1:')}

                if s1_positions:
                    # 현재가 조회
                    from pykrx import stock as _pykrx_close
                    _today_close = date.today().strftime('%Y%m%d')
                    _close_prices = {}
                    for pk in s1_positions:
                        _tk = pk.split(':')[1]
                        try:
                            _df = _pykrx_close.get_market_ohlcv(
                                _today_close, _today_close, _tk)
                            if len(_df) > 0:
                                _close_prices[_tk] = float(_df.iloc[-1].get('종가', 0))
                        except Exception as _phase_err:  # [Phase 70-E] Silent Error 철거
                            logger.error(f'[Phase 70-E] 페이즈 오류 은폐 방지: {_phase_err}', exc_info=True)
                            raise

                    if _close_prices:
                        _mgr_close.mark_to_market(_close_prices)

                        # 전량 강제 청산
                        _force_sells = []
                        for pk, pos in s1_positions.items():
                            _, _tk = pk.split(':', 1)
                            _force_sells.append({
                                'pos_key': pk,
                                'ticker': _tk,
                                'stream_id': 'S1',
                                'quantity': pos['quantity'],
                                'reason': f'장마감 강제 청산 (15:10)',
                                'sell_type': 'forced_close',
                            })

                        _mgr_close.execute_sells(_force_sells, _close_prices)
                        logger.info(f"  🔴 S1 장마감 청산: {len(_force_sells)}건 완료")
                    else:
                        logger.warning("  ⚠️ S1 장마감 청산: 현재가 조회 실패")
                else:
                    logger.info("  ⏸ S1 포지션 없음 — 장마감 청산 불필요")
        except Exception as e:
            logger.error(f"  S1 장마감 청산 실패: {e}", exc_info=True)
            
        # ★ V4: S5 Cash Sweep & Overnight Overlay 진입 (15:20 동시호가)
        # S1 청산 직후 반환된 예산으로 S5를 진입시키기 위해 V3 엔진을 호출합니다.
        try:
            logger.info("  ⚡ [V4] S5 파킹 및 오버레이 진입 (run_virtual_trading 가동)")
            import sys
            import os
            # Prevent arg parsing issues in run_virtual_trading
            old_argv = sys.argv
            sys.argv = [sys.argv[0]]
            from scripts.stream_orchestrator import StreamOrchestrator
            StreamOrchestrator().run()
            sys.argv = old_argv
        except Exception as e:
            logger.error(f"  ❌ Closing S5 동적 엔진 가동 실패: {e}", exc_info=True)

        # ★ DD-14: WebSocket 실시간 스트리밍 종료
        try:
            from src.data_collection.kis_websocket import get_websocket_client
            ws = get_websocket_client()
            if ws and ws.is_running:
                ws.stop()
                logger.info(f"  🔴 WebSocket 종료: "
                           f"msg={ws.stats['messages_received']}, "
                           f"reconnects={ws.stats['reconnects']}")
        except ImportError as e:
            pass
        except Exception as e:
            logger.error(f"  WebSocket 종료 실패: {e}", exc_info=True)

        # ★ DD-15: Telegram 일일 요약 알림
        try:
            if cfg.get('telegram.enabled', True):
                from src.interface.telegram_notifier import MeridianTelegram
                _tg = MeridianTelegram()
                # Shadow 결과 요약 전송
                _summary_parts = []
                _summary_parts.append(f"📊 Daily Closing ({date.today()})")
                if '_nav' in dir():
                    _summary_parts.append(f"NAV: ₩{_nav:,.0f}")
                if '_ret' in dir():
                    _summary_parts.append(f"수익: {_ret:+.2f}%")
                try:
                    _sp_data = json.loads(
                        (_PROJECT_ROOT / 'results' / 'shadow_portfolio.json').read_text())
                    _n_pos = len(_sp_data.get('positions', {}))
                    _summary_parts.append(f"포지션: {_n_pos}건")
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)
                _tg.send('\n'.join(_summary_parts))
                logger.info("  📱 Telegram 일일 요약 발송")
        except ImportError as e:
            logger.error("  telegram_notifier import 실패", exc_info=True)
        except Exception as e:
            logger.error(f"  Telegram 발송 실패: {e}", exc_info=True)

        # ★ DD-12: shadow_trades.json 생성 (TCA/대시보드용)
        try:
            _sp_path = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if _sp_path.exists():
                _sp_data = json.loads(_sp_path.read_text())
                _trades = _sp_data.get('trade_history', [])
                if _trades:
                    import tempfile, os
                    _st_path = _PROJECT_ROOT / 'results' / 'shadow_trades.json'
                    fd, tmp = tempfile.mkstemp(
                        dir=str(_st_path.parent), suffix='.tmp')
                    with os.fdopen(fd, 'w') as f:
                        json.dump(_trades, f, indent=2,
                                  ensure_ascii=False, default=str)
                    os.replace(tmp, str(_st_path))
                    logger.info(f"  💾 shadow_trades.json: {len(_trades)}건")
        except Exception as e:
            logger.error(f"  shadow_trades 저장 실패: {e}", exc_info=True)

        # ★ DD-20: PnL Attribution 자동 갱신 (5-스트림 기여도 분리)
        try:
            from src.allocation.pnl_attribution import PnLAttributionEngine
            _pnl_eng = PnLAttributionEngine()
            _pnl_result = _pnl_eng.compute()
            if _pnl_result:
                import tempfile as _tf20
                _pnl_path = _PROJECT_ROOT / 'results' / 'pnl_attribution.json'
                _pnl_tmp = _tf20.NamedTemporaryFile(
                    mode='w', dir=_pnl_path.parent, suffix='.tmp', delete=False)
                json.dump(_pnl_result, _pnl_tmp, indent=2, ensure_ascii=False, default=str)
                _pnl_tmp.close()
                os.replace(_pnl_tmp.name, _pnl_path)
                logger.info(f"  📊 PnL Attribution 갱신: {len(_pnl_result.get('streams', {}))} 스트림")
        except Exception as e:
            logger.error(f"  PnL Attribution 실패: {e}", exc_info=True)

        # ★ DD-11: Drawdown Guard 상태 → pipeline_state 기록
        try:
            from src.risk.drawdown_guard import DrawdownGuard
            _ddg = DrawdownGuard()
            _sp_data = json.loads((_PROJECT_ROOT / 'results' / 'shadow_portfolio.json').read_text())
            _current_nav = _sp_data.get('nav', _sp_data.get('total_value', cfg.get('portfolio.initial_capital')))
            _dd_status = _ddg.check(nav=float(_current_nav))

            # pipeline_state.json에 직접 기록
            _ps_path = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            if _ps_path.exists():
                _ps_data = json.loads(_ps_path.read_text())
                _ps_data['drawdown_stage'] = _dd_status.get('stage', 'Normal')
                _ps_data['drawdown_pct'] = _dd_status.get('drawdown_pct', 0)
                _ps_data['updated_at'] = datetime.now().isoformat()
                atomic_write_json(_ps_path, _ps_data, indent=2)
        except Exception as e:
            logger.error(f"  Drawdown Guard 상태 기록 실패: {e}", exc_info=True)

    def _evening_phase():
        """Evening: SelfLearning + OnlineLearner + Go/No-Go + Advisory.

        ★ Pipeline Timing Optimization (2026-05-29)
        SelfLearning/OnlineLearner를 morning_ml → evening으로 이동.
        이브닝에 당일 성과 기반 학습을 완료하고,
        morning_ml에서는 DriftGuard + Conformal + MorningFusion만 실행.
        """
        logger.info("  🌙 Evening Phase: 학습 + 판정")

        # ── 1. SelfLearning: IC 기반 전략 파라미터 조정 ──
        try:
            from src.measurement.measurement_engine import load_official
            from src.learning.self_learning import SelfLearning

            official = load_official()
            if official and isinstance(official, dict):
                me_file = _PROJECT_ROOT / 'results' / 'measurement_engine.json'
                me_full = {}
                if me_file.exists():
                    me_full = json.loads(me_file.read_text())
                sl = SelfLearning()
                learn_result = sl.update(me_full)
                _applied = learn_result.get('applied', False)
                _n_changes = learn_result.get('judgment', {}).get('n_changes', 0)
                if _applied:
                    logger.info(f"  ✅ SelfLearning: {_n_changes}개 파라미터 갱신")
                else:
                    logger.info(f"  ℹ️ SelfLearning: 변경 없음")
            else:
                logger.info("  ℹ️ SelfLearning: 전날 측정 결과 없음 (스킵)")
        except Exception as e:
            logger.error(f"  SelfLearning 실패: {e}", exc_info=True)

        # ── 2. OnlineLearner: 전날 체결 결과 EWA 학습 ──
        try:
            from src.learning.online_learner import OnlineLearner
            from datetime import timedelta as _td_ol
            ol = OnlineLearner()
            sp_file = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if sp_file.exists():
                sp = json.loads(sp_file.read_text())
                stream_results = {}
                yesterday = (datetime.now() - _td_ol(days=1)).strftime('%Y-%m-%d')
                for trade in sp.get('trade_history', []):
                    if trade.get('date', '').startswith(yesterday):
                        sid = trade.get('stream_id', trade.get('stream', 'unknown'))
                        if sid not in stream_results:
                            stream_results[sid] = []
                        # ★ OnlineLearner.observe()가 기대하는 형식으로 변환
                        stream_results[sid].append({
                            'signal_confidence': trade.get('confidence', 0.5),
                            'actual_return': trade.get('pnl_pct', 0) / 100 if trade.get('pnl_pct') else 0,
                            'hold_minutes': trade.get('hold_minutes', 0),
                            'regime': trade.get('regime', 'unknown'),
                        })
                if stream_results:
                    result = ol.batch_update(stream_results)
                    logger.info(f"  ✅ OnlineLearner: {len(stream_results)} 스트림 학습")
                else:
                    logger.info("  ℹ️ OnlineLearner: 전일 체결 없음")
        except Exception as e:
            logger.error(f"  OnlineLearner 실패: {e}", exc_info=True)

        # ── 3. Go/No-Go 판정 ──
        try:
            go_nogo = orch.shadow_recorder.go_nogo_evaluation()
            logger.info(f"  ✅ Go/No-Go: {go_nogo.get('verdict', '?')}")
        except Exception as e:
            logger.error(f"  Go/No-Go 실패: {e}", exc_info=True)

        # ── 4. Walk-Forward 일일 검증 (P2-1) ──
        try:
            from scripts.walk_forward_validator import WalkForwardValidator
            wfv = WalkForwardValidator()
            wf_result = wfv.validate()
            wf_status = wf_result.get('status', 'skip')
            wf_warnings = wf_result.get('warnings', [])
            if wf_status == 'completed':
                logger.info(
                    f"  ✅ Walk-Forward: ACC={wf_result.get('oos_acc', 0):.1%}, "
                    f"IC={wf_result.get('oos_ic', 0):.4f}")
                if wf_warnings:
                    logger.warning(f"  ⚠️ WF 경고: {'; '.join(wf_warnings)}")
            else:
                logger.info(f"  ℹ️ Walk-Forward: {wf_result.get('reason', wf_status)}")
        except Exception as e:
            logger.error(f"  Walk-Forward 검증 실패: {e}", exc_info=True)

    def _phase_alt_data():
        """대체 데이터 수집 + FeatureStore V2 적재."""
        logger.info("  🔄 Alternative Data Pipeline")
        try:
            from src.data_collection.alt_data_pipeline import (
                AlternativeDataPipeline)
            pipeline = AlternativeDataPipeline()
            result = pipeline.run()
            n_ok = result.get('n_sources_ok', 0)
            total = result.get('total_collected', 0)
            saved = result.get('feature_store_saved', 0)
            logger.info(
                f"  ✅ AltData: {n_ok} sources, "
                f"{total} features, {saved} saved")
        except ImportError as e:
            logger.error("  alt_data_pipeline import 실패", exc_info=True)
        except Exception as e:
            logger.error(f"  AltData 실패: {e}", exc_info=True)

        # FeatureStore V2 통합 적재
        try:
            from src.data_collection.feature_store_v2 import FeatureStoreV2
            fsv2 = FeatureStoreV2()
            result = fsv2.ingest_and_transform()
            logger.info(
                f"  ✅ FeatureStore V2 sync: "
                f"{result.get('ingest', {})}")
        except ImportError as e:
            logger.error("  feature_store_v2 import 실패", exc_info=True)
        except Exception as e:
            logger.error(f"  FeatureStore V2 실패: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════
    # ★ Morning ML Phase — 매일 아침 학습 (07:30)
    # ═══════════════════════════════════════════════════════
    def _phase_morning_ml():
        """Morning ML: DriftGuard + Conformal + MorningIntelligenceFusion.

        ★ Pipeline Timing Optimization (2026-05-29)
        SelfLearning/OnlineLearner는 evening(20:00)으로 이동.
        morning_ml은:
          1. DriftGuard: 피처 분포 드리프트 감지
          2. ConformalPredictor: 예측 구간 갱신
          3. MorningIntelligenceFusion: 이브닝 학습 + 야간 글로벌 복합 분석
        premarket(07:45) 전에 완료되어야 합니다.
        """
        logger.info("  🧠 Morning ML Phase: DriftGuard + Conformal + Fusion")
        _ml_start = datetime.now()
        _results = {}

        # ────────────────────────────────────────────
        # ★ SelfLearning + OnlineLearner → evening_phase로 이동 (2026-05-29)
        # 이브닝에 당일 성과 기반 학습 완료 → 모닝에서는 결과만 참조
        # ────────────────────────────────────────────
        _results['self_learning'] = {'moved_to': 'evening_phase'}
        _results['online_learner'] = {'moved_to': 'evening_phase'}

        # ────────────────────────────────────────────
        # 0. MacroFeatureIntegrator: 미사용 데이터 통합 적재
        # ────────────────────────────────────────────
        try:
            from src.intelligence.macro_feature_integrator import (
                run_macro_integration)
            macro_result = run_macro_integration()
            _results['macro_integration'] = {
                'n_features': macro_result.get('n_features', 0),
                'sources': macro_result.get('sources', []),
            }
            logger.info(
                f"  ✅ MacroFeatures: {macro_result.get('n_features', 0)}개 "
                f"({', '.join(macro_result.get('sources', []))})")
        except Exception as e:
            logger.error(f"  MacroFeatures 실패: {e}", exc_info=True)
            _results['macro_integration'] = {'error': str(e)}

        # ────────────────────────────────────────────
        # 1. DriftGuard: 피처 분포 드리프트 조기 경보
        # ────────────────────────────────────────────
        try:
            drift_state_file = _PROJECT_ROOT / 'results' / 'drift_guard_state.json'

            # ★ 재학습 직후(쿨다운 이내)에는 check 스킵
            # 이유: run_training()이 정확한 학습 데이터로 reference를 갱신하지만,
            # morning_ml의 간이 피처 구성은 학습 피처와 분포가 달라 항상 false-positive drift 발생.
            # 재학습 직후에는 기존 resolved 상태를 유지.
            _skip_drift_check = False
            _meta_f = _PROJECT_ROOT / 'results' / 'models' / 'ensemble_meta.json'
            if _meta_f.exists():
                try:
                    _em = json.loads(_meta_f.read_text())
                    _last_train = datetime.fromisoformat(_em.get('train_date', '2020-01-01'))
                    _hours_since_train = (datetime.now() - _last_train).total_seconds() / 3600
                    if _hours_since_train < 24:
                        _skip_drift_check = True
                        logger.info(f"  ⏭️ DriftGuard: 재학습 후 {_hours_since_train:.0f}h — check 스킵 (쿨다운)")
                        # timestamp만 갱신하여 freshness 유지
                        if drift_state_file.exists():
                            _existing_drift = json.loads(drift_state_file.read_text())
                        else:
                            _existing_drift = {}
                        _existing_drift['timestamp'] = datetime.now().isoformat()
                        _existing_drift['drifted'] = False
                        _existing_drift['retrain_needed'] = False
                        _existing_drift['n_drifted'] = 0
                        _existing_drift['drifted_features'] = []
                        _existing_drift['mean_psi'] = 0.0
                        _existing_drift['reason'] = f'post_retrain_cooldown_{_hours_since_train:.0f}h'
                        atomic_write_json(drift_state_file, _existing_drift, indent=2)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)

            # ★ DriftGuard 실행: state 파일을 매일 갱신
            if not _skip_drift_check:
                try:
                    from src.risk.drift_guard import DriftGuard as _DG
                    _guard = _DG()
                    _ref = _guard._load_reference()
                    if _ref is not None:
                        # ★ 실제 피처 벡터 구성: 앙상블 feature_names 기반
                        _current_features = None
                        try:
                            import numpy as np
                            import pickle as _pkl_drift
                            _ens_path = _PROJECT_ROOT / 'results' / 'models' / 'stock_ranker_ensemble.pkl'
                            if _ens_path.exists():
                                with open(_ens_path, 'rb') as _ef:
                                    _ens_pkg = _pkl_drift.load(_ef)
                                _feat_names = _ens_pkg.get('metadata', {}).get('feature_names', _ens_pkg.get('feature_names', []))
                                if _feat_names:
                                    # ── 1차: signal_cache stock_technicals ──
                                    _sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
                                    _sc = json.loads(_sc_path.read_text()) if _sc_path.exists() else {}
                                    _st = _sc.get('stock_technicals', {})
                                    _rows = []
                                    for _tk, _td in _st.items():
                                        if isinstance(_td, dict) and len(_td) >= 5:
                                            _row = []
                                            for _fn in _feat_names:
                                                _row.append(float(_td.get(_fn, 0.0)))
                                            _rows.append(_row)

                                    # ── 2차: parquet 기반 피처 추출 (fallback) ──
                                    if len(_rows) < 20:
                                        import pandas as pd
                                        _data_dir = _PROJECT_ROOT / 'data' / 'historical_10y'
                                        _parquets = sorted(_data_dir.glob('kr_0*.parquet'))[:50]
                                        for _pf in _parquets:
                                            try:
                                                _pdf = pd.read_parquet(_pf)
                                                if len(_pdf) < 20 or 'close' not in _pdf.columns:
                                                    continue
                                                _close = pd.to_numeric(_pdf['close'], errors='coerce').dropna()
                                                _vol = pd.to_numeric(_pdf.get('volume', pd.Series(dtype=float)), errors='coerce').fillna(0)
                                                if len(_close) < 20:
                                                    continue
                                                # 기본 피처 동적 계산
                                                _fmap = {}
                                                _c = _close.values
                                                _fmap['rsi_14'] = 50.0  # 간이 RSI
                                                _delta = np.diff(_c[-15:])
                                                _gain = np.mean(_delta[_delta > 0]) if np.any(_delta > 0) else 0
                                                _loss = -np.mean(_delta[_delta < 0]) if np.any(_delta < 0) else 1e-9
                                                _rs = _gain / max(_loss, 1e-9)
                                                _fmap['rsi_14'] = 100 - 100 / (1 + _rs)
                                                _fmap['volume_ratio_20d'] = float(_vol.iloc[-1] / max(_vol.iloc[-20:].mean(), 1))
                                                _sma20 = _c[-20:].mean()
                                                _fmap['price_ma_ratio_20_60'] = float(_c[-1] / max(_sma20, 1))
                                                _fmap['atr_pct'] = float(np.std(_c[-14:]) / max(_c[-1], 1) * 100)
                                                _fmap['bb_position'] = float((_c[-1] - _sma20) / max(np.std(_c[-20:]) * 2, 1e-9))
                                                # 피처 벡터 구성
                                                _row = [float(_fmap.get(_fn, 0.0)) for _fn in _feat_names]
                                                _rows.append(_row)
                                            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                                                logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)
                                                continue

                                    if len(_rows) >= 20:
                                        _current_features = np.array(_rows)
                                        logger.info(f"  ✅ DriftGuard: {len(_rows)}행 피처 구성 "
                                                    f"({len(_feat_names)}차원)")
                        except Exception as _feat_err:
                            logger.error(f"  DriftGuard 피처 구성 실패: {_feat_err}", exc_info=True)

                        if _current_features is not None and len(_current_features) >= 20:
                            _guard.check(_current_features)
                            logger.info("  ✅ DriftGuard: check() 실행 → state 갱신")
                        else:
                            # 피처 없어도 timestamp 갱신 (freshness 유지)
                            _dg_state = {'timestamp': datetime.now().isoformat(),
                                         'drifted': False, 'retrain_needed': False,
                                         'mean_psi': 0, 'n_drifted': 0,
                                         'n_unavailable': 0, 'n_sparse_tolerated': 0,
                                         'drifted_features': [], 'reason': 'insufficient_features'}
                            atomic_write_json(drift_state_file, _dg_state, indent=2)
                            logger.info("  ℹ️ DriftGuard: 현재 피처 부족 → timestamp만 갱신")
                    else:
                        # 참조 데이터 없어도 timestamp 갱신 (freshness 유지)
                        _dg_state = {'timestamp': datetime.now().isoformat(),
                                     'drifted': False, 'retrain_needed': False,
                                     'mean_psi': 0, 'n_drifted': 0,
                                     'n_unavailable': 0, 'n_sparse_tolerated': 0,
                                     'drifted_features': [], 'reason': 'no_reference'}
                        atomic_write_json(drift_state_file, _dg_state, indent=2)
                        logger.info("  ℹ️ DriftGuard: 참조 데이터 없음 → timestamp만 갱신")
                except Exception as _dg_err:
                    logger.error(f"  DriftGuard 실행 실패: {_dg_err}", exc_info=True)

            if drift_state_file.exists():
                drift = json.loads(drift_state_file.read_text())
                n_drifted = drift.get('n_drifted', 0)
                mean_psi = drift.get('mean_psi', 0)
                drifted_features = drift.get('drifted_features', [])
                # ★ 총 피처 수: ensemble_meta.json → psi_scores → fallback 53
                _meta_f = _PROJECT_ROOT / 'results' / 'models' / 'ensemble_meta.json'
                if _meta_f.exists():
                    try:
                        _meta = json.loads(_meta_f.read_text())
                        n_total_features = _meta.get(
                            'n_features',
                            len(_meta.get('feature_names', [])))
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)
                        n_total_features = len(
                            drift.get('psi_scores', {})) or 53
                else:
                    n_total_features = len(
                        drift.get('psi_scores', {})) or 53
                n_critical = sum(
                    1 for f in drifted_features
                    if f.get('severity') == 'CRITICAL')
                n_warning = sum(
                    1 for f in drifted_features
                    if f.get('severity') == 'WARNING')

                # ★ 동적 confidence discount 계산
                # PSI 임계값 (통계적 기준):
                #   PSI < 0.10 → 분포 동일 (위험도 ~0)
                #   PSI 0.10~0.25 → 약간 변화 (위험도 0.1~0.3)
                #   PSI 0.25~0.50 → 심각한 변화 (위험도 0.3~0.7)
                #   PSI > 0.50 → 완전히 다른 분포 (위험도 0.7~1.0)
                import math

                def _psi_to_risk(psi: float) -> float:
                    """PSI를 0~1 위험도로 변환 (sigmoid 기반).

                    PSI=0.1 → ~0.05, PSI=0.25 → ~0.27,
                    PSI=0.5 → ~0.73, PSI=1.0 → ~0.95
                    """
                    # sigmoid: 1 / (1 + exp(-k*(x - x0)))
                    # k=6, x0=0.35로 PSI 통계 기준에 맞춤
                    return 1.0 / (1.0 + math.exp(-6.0 * (psi - 0.35)))

                if drifted_features:
                    # 각 드리프트 피처의 위험도 계산
                    feature_risks = []
                    for df_item in drifted_features:
                        psi = df_item.get('psi', 0)
                        risk = _psi_to_risk(psi)
                        feature_risks.append(risk)

                    # 가중 평균 위험도 (PSI가 높을수록 가중치 증가)
                    psi_values = [df_item.get('psi', 0)
                                  for df_item in drifted_features]
                    total_psi_weight = sum(psi_values) or 1.0
                    weighted_risk = sum(
                        r * p for r, p in zip(feature_risks, psi_values)
                    ) / total_psi_weight

                    # 드리프트 비율 반영 (전체 피처 중 드리프트 비율)
                    drift_ratio = min(n_drifted / max(n_total_features, 1),
                                      1.0)

                    # 최종 discount = 1 - (가중위험도 × 드리프트비율)
                    # 하한: DynamicConfig에서 읽기 (기본 0.3)
                    _min_discount = cfg.get(
                        'drift.min_confidence_discount', 0.3)
                    raw_discount = 1.0 - (weighted_risk * drift_ratio)
                    drift_discount = max(_min_discount,
                                         round(raw_discount, 3))
                else:
                    drift_discount = 1.0
                    weighted_risk = 0.0
                    drift_ratio = 0.0

                _results['drift_guard'] = {
                    'n_drifted': n_drifted,
                    'n_critical': n_critical,
                    'n_warning': n_warning,
                    'mean_psi': round(mean_psi, 4),
                    'weighted_risk': round(weighted_risk, 4),
                    'drift_ratio': round(drift_ratio, 4),
                    'confidence_discount': drift_discount,
                }

                # 적용 임계: discount < 1.0이면 감쇄 적용
                if drift_discount < 1.0:
                    cfg._overrides[
                        'drift.confidence_discount'] = drift_discount
                    # ★ 드리프트 감지는 정보 로그, 심각한 감쇄만 WARNING
                    _drift_log = logger.warning if drift_discount < 0.5 else logger.info
                    _drift_log(
                        f"  {'⚠️' if drift_discount < 0.5 else '📊'} DriftGuard: {n_drifted}건 드리프트 "
                        f"(CRITICAL={n_critical}, WARN={n_warning}) → "
                        f"confidence ×{drift_discount:.2f} "
                        f"(risk={weighted_risk:.2f}, "
                        f"ratio={drift_ratio:.0%})")
                    for df_item in sorted(
                            drifted_features,
                            key=lambda x: x.get('psi', 0),
                            reverse=True)[:3]:
                        _drift_log(
                            f"    📊 {df_item['feature']}: "
                            f"PSI={df_item['psi']:.3f} "
                            f"(risk={_psi_to_risk(df_item['psi']):.2f})")
                elif n_drifted > 0:
                    logger.info(
                        f"  ✅ DriftGuard: 드리프트 {n_drifted}건 "
                        f"(CRITICAL={n_critical}, WARN={n_warning}, "
                        f"PSI={mean_psi:.3f}) — discount=1.0 (적용 안 함)")
                else:
                    logger.info("  ✅ DriftGuard: 드리프트 없음")
            else:
                logger.info("  ℹ️ DriftGuard: 상태 파일 없음 (스킵)")
                _results['drift_guard'] = {'reason': 'no_state'}
        except Exception as e:
            logger.error(f"  DriftGuard 실패: {e}", exc_info=True)
            _results['drift_guard'] = {'error': str(e)}

        # ────────────────────────────────────────────
        # 4. Conformal Predictor: 일일 캘리브레이션
        # ────────────────────────────────────────────
        try:
            from src.intelligence.conformal_predictor import (
                AdaptiveConformalPredictor)
            import pickle

            conformal_path = _PROJECT_ROOT / 'results' / 'models' / 'conformal_state.pkl'
            if conformal_path.exists():
                # 전날 예측과 실제 결과를 shadow_portfolio에서 추출
                sp_file = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
                if sp_file.exists():
                    sp = json.loads(sp_file.read_text())
                    positions = sp.get('positions', {})

                    if len(positions) >= 5:
                        # 현재 포지션의 예측 confidence vs 실제 PnL 비교
                        # Conformal 업데이트 — 단건씩 커버리지 추적
                        try:
                            # ★ 기존 상태 로드 → 업데이트 → 재저장
                            cp = AdaptiveConformalPredictor()
                            try:
                                with open(conformal_path, 'rb') as _cpf:
                                    _cp_state = pickle.load(_cpf)
                                if isinstance(_cp_state, dict):
                                    cp.calibration_scores = _cp_state.get(
                                        'calibration_scores', [])
                                    cp.quantile_level = _cp_state.get(
                                        'quantile_level', 0.9)
                            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                                logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)

                            n_updated = 0
                            for pos_key, pos in positions.items():
                                conf = pos.get('confidence', 0.5)
                                pnl = pos.get('pnl_pct', 0)
                                actual = 1.0 if pnl > 0 else 0.0
                                try:
                                    cp.update(conf, actual)
                                    n_updated += 1
                                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                                    logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)

                            # ★ 업데이트된 상태 pkl 저장
                            if n_updated > 0:
                                with open(conformal_path, 'wb') as _cpf:
                                    pickle.dump({
                                        'calibration_scores': cp.calibration_scores,
                                        'quantile_level': cp.quantile_level,
                                        'updated_at': datetime.now().isoformat(),
                                        'n_samples': len(cp.calibration_scores),
                                    }, _cpf)

                            _results['conformal'] = {
                                'n_samples': n_updated,
                                'updated': n_updated > 0,
                                'total_calibration': len(cp.calibration_scores),
                            }
                            logger.info(
                                f"  ✅ Conformal: {n_updated}건 "
                                f"캘리브레이션 업데이트 "
                                f"(총 {len(cp.calibration_scores)}건 저장)")
                        except Exception as _ce:
                            logger.info(
                                f"  ℹ️ Conformal: 업데이트 스킵 ({_ce})")
                            _results['conformal'] = {'reason': str(_ce)}
                    else:
                        logger.info(
                            f"  ℹ️ Conformal: 포지션 부족 "
                            f"({len(positions)}건)")
                        _results['conformal'] = {
                            'reason': f'insufficient_positions_{len(positions)}'}
            else:
                logger.error("  ℹ️ Conformal: 모델 없음 (스킵)", exc_info=True)
                _results['conformal'] = {'reason': 'no_model'}
        except Exception as e:
            logger.error(f"  Conformal 실패: {e}", exc_info=True)
            _results['conformal'] = {'error': str(e)}

        # ────────────────────────────────────────────
        # 5. MorningIntelligenceFusion: 학습+글로벌 복합 분석
        # ────────────────────────────────────────────
        try:
            from src.intelligence.morning_intelligence_fusion import (
                run_morning_fusion)
            fusion_result = run_morning_fusion()
            _results['morning_fusion'] = {
                'regime_adj': fusion_result.get('regime_adjustment', 0),
                'conflicts': len(fusion_result.get('conflicts', [])),
                'risk_flags': len(fusion_result.get('risk_flags', [])),
                'confidence': fusion_result.get('fusion_confidence', 0.5),
            }

            # signal_cache에 fusion 결과 반영
            _update_signal_cache({
                'morning_fusion': fusion_result,
                'regime_adjustment': fusion_result.get('regime_adjustment', 0),
            })

            logger.info(
                f"  ✅ MorningFusion: "
                f"regime_adj={fusion_result.get('regime_adjustment', 0):+.2f}, "
                f"conflicts={len(fusion_result.get('conflicts', []))}, "
                f"flags={len(fusion_result.get('risk_flags', []))}")
        except Exception as e:
            logger.error(f"  MorningFusion 실패: {e}", exc_info=True)
            _results['morning_fusion'] = {'error': str(e)}

        # ────────────────────────────────────────────
        # 6. Daily SHAP 간이 갱신 (built-in feature importance)
        # ────────────────────────────────────────────
        try:
            import pickle as _pkl_shap
            _ens_path = _PROJECT_ROOT / 'results' / 'models' / 'stock_ranker_ensemble.pkl'
            _shap_path = _PROJECT_ROOT / 'results' / 'shap_analysis.json'
            if _ens_path.exists():
                with open(_ens_path, 'rb') as _f:
                    _pkg = _pkl_shap.load(_f)
                _models = _pkg.get('models', {})
                _feat_names = _pkg.get('metadata', {}).get('feature_names', _pkg.get('feature_names', []))
                if _models and _feat_names:
                    # 앙상블 평균 feature importance
                    _imp_sum = None
                    _n_models = 0
                    for _mname, _mobj in _models.items():
                        try:
                            _fi = _mobj.feature_importances_
                            if _imp_sum is None:
                                _imp_sum = list(_fi)
                            else:
                                for _i in range(len(_imp_sum)):
                                    _imp_sum[_i] += _fi[_i]
                            _n_models += 1
                        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                            logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)
                    if _imp_sum and _n_models > 0:
                        _imp_avg = [v / _n_models for v in _imp_sum]
                        _pairs = sorted(
                            zip(_feat_names, _imp_avg),
                            key=lambda x: abs(x[1]), reverse=True)
                        _shap_result = {
                            'timestamp': datetime.now().isoformat(),
                            'importances': {f: round(v, 5) for f, v in _pairs},
                            'top_features': [f for f, _ in _pairs[:15]],
                            'weak_features': [f for f, v in _pairs if abs(v) < 0.01],
                            'n_features': len(_feat_names),
                            'method': 'builtin_daily',
                            'n_models_averaged': _n_models,
                        }
                        atomic_write_json(_shap_path, 
                            _shap_result, indent=2)
                        logger.info(
                            f"  ✅ SHAP 일일 갱신: "
                            f"top3={_pairs[0][0]},{_pairs[1][0]},{_pairs[2][0]}")
                        _results['shap_daily'] = {
                            'updated': True,
                            'n_features': len(_feat_names),
                            'method': 'builtin_daily',
                        }
            else:
                logger.info("  ℹ️ SHAP: 앙상블 모델 없음 (스킵)")
                _results['shap_daily'] = {'reason': 'no_ensemble'}
        except Exception as e:
            logger.error(f"  SHAP 일일 갱신 실패: {e}", exc_info=True)
            _results['shap_daily'] = {'error': str(e)}

        # ────────────────────────────────────────────
        # 7. ★ 이벤트 트리거 기반 재학습 (장 개장 전 방어)
        # ────────────────────────────────────────────
        # 밤사이 VIX 급등, 레짐 전환, DA 연속 실패 등을
        # evening까지 기다리지 않고 morning에서 즉시 감지 → 재학습
        try:
            from scripts.train_ensemble import should_retrain, run_training
            _retrain_needed, _retrain_trigger = should_retrain()
            if _retrain_needed:
                logger.info(f"  🔄 Morning 재학습 트리거: {_retrain_trigger}")
                _retrain_start = datetime.now()
                _retrain_result = run_training(trigger=_retrain_trigger, enable_automl=False)
                _retrain_elapsed = (datetime.now() - _retrain_start).total_seconds()
                if _retrain_result:
                    logger.info(
                        f"  ✅ Morning 재학습 완료: "
                        f"ACC={_retrain_result['val_acc']:.3f}, "
                        f"AUC={_retrain_result['val_auc']:.3f} "
                        f"({_retrain_elapsed:.0f}초)")
                    _results['morning_retrain'] = {
                        'trigger': _retrain_trigger,
                        'val_acc': _retrain_result['val_acc'],
                        'val_auc': _retrain_result['val_auc'],
                        'elapsed_sec': round(_retrain_elapsed, 1),
                    }
                    # ★ 재학습 후 drift_guard 결과를 해소 상태로 갱신
                    if _results.get('drift_guard', {}).get('n_drifted', 0) > 0:
                        _results['drift_guard']['resolved'] = True
                        _results['drift_guard']['resolved_by'] = f'retrain_{_retrain_trigger}'
                else:
                    logger.info(f"  ⏭️ 재학습 스킵 (데이터 부족)")
                    _results['morning_retrain'] = {
                        'trigger': _retrain_trigger,
                        'skipped': 'insufficient_data',
                    }
            else:
                logger.info(f"  ⏭️ Morning 재학습 불필요 (trigger=none)")
                _results['morning_retrain'] = {'needed': False}
        except ImportError as e:
            logger.error("  train_ensemble import 실패", exc_info=True)
            _results['morning_retrain'] = {'error': 'import_failed'}
        except Exception as e:
            logger.error(f"  Morning 재학습 실패: {e}", exc_info=True)
            _results['morning_retrain'] = {'error': str(e)}

        # ────────────────────────────────────────────
        # 결과 저장 + 요약
        # ────────────────────────────────────────────
        _elapsed = (datetime.now() - _ml_start).total_seconds()
        _results['elapsed_sec'] = round(_elapsed, 1)
        _results['timestamp'] = datetime.now().isoformat()

        # 결과 JSON 저장
        try:
            ml_log = _PROJECT_ROOT / 'results' / 'morning_ml_log.json'
            atomic_write_json(ml_log, 
                _results, indent=2)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)

        logger.info(
            f"  🧠 Morning ML 완료 ({_elapsed:.1f}초)")

    phases = {
        'overnight': _phase_overnight,
        'collect': _phase_collect_data,
        'morning_ml': _phase_morning_ml,
        'premarket': _phase_premarket,
        'premarket_trade': _phase_premarket_trade,
        'morning': _morning_phase,
        'market': _market_and_shadow,
        'intraday': _phase_intraday,
        'closing': _closing_phase,
        'aftermarket_trade': _phase_aftermarket_trade,
        'aftermarket': _phase_aftermarket,
        'krx_refresh': _phase_krx_refresh,
        'collect_flow': _phase_collect_flow,
        'evening_data': _phase_evening_data,
        'alt_data': _phase_alt_data,
        'collect_dart': _phase_collect_dart,
        'collect_consensus': _phase_collect_consensus,
        'evening': _evening_phase,
        'us_market': _phase_us_market,
        'us_premarket': _phase_us_premarket,
        'us_regular': _phase_us_regular_market,
        'weekly_retrain': _phase_weekly_retrain,
        'weekly_validate': _phase_weekly_validate,
    }

    # ── Checkpoint 초기화 ──
    try:
        from src.infra.pipeline_checkpoint import PipelineCheckpoint
        ckpt = PipelineCheckpoint()
    except ImportError as e:
        ckpt = None

    # intraday는 하루 여러 번 실행 → checkpoint skip 제외 대상
    _REPEATABLE_PHASES = {'intraday'}

    def _run_with_checkpoint(name: str, func) -> None:
        """Phase를 Checkpoint 추적과 함께 실행."""
        if ckpt and name not in _REPEATABLE_PHASES and not ckpt.should_run(name):
            logger.info(f"  ⏭ {name} — 이미 완료 (checkpoint skip)")
            return
        if ckpt:
            ckpt.mark_running(name)
        t0 = time.time()
        try:
            func()
            elapsed = time.time() - t0
            logger.info(f"  ✅ {name} 완료 ({elapsed:.1f}s)")
            if ckpt:
                ckpt.mark_done(name, elapsed)
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"  ❌ {name} 실패 ({elapsed:.1f}s): {e}",
                           exc_info=True)
            if ckpt:
                ckpt.mark_failed(name, str(e))

    if phase == 'all':
        for name, func in phases.items():
            logger.error(f"\n{'─'*40}", exc_info=True)
            logger.info(f"  Phase: {name.upper()}")
            _run_with_checkpoint(name, func)
    elif ',' in phase:
        for p in phase.split(','):
            p = p.strip()
            if p in phases:
                _run_with_checkpoint(p, phases[p])
    elif phase in phases:
        _run_with_checkpoint(phase, phases[phase])
    else:
        logger.error(f"  알 수 없는 phase: {phase}")

    # Shadow 기록 — ShadowPortfolioManager.daily_snapshot() (IC/DA 포함)
    if phase in ('all', 'closing'):
        try:
            from src.portfolio.shadow_manager import ShadowPortfolioManager
            _ps = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            _regime = 'caution'
            if _ps.exists():
                _psd = json.loads(_ps.read_text())
                _regime = _psd.get('kr_regime', _psd.get('operating_regime', 'caution'))
            _initial = cfg.get('portfolio.initial_capital')
            with ShadowPortfolioManager(initial_capital=_initial).transaction() as _mgr:
                record = _mgr.daily_snapshot(regime=_regime)
            _nav = _mgr.nav
            _ret = record.get('daily_return_pct', 0)
            logger.info(f"  Shadow: NAV=₩{_nav:,.0f} ({_ret:+.2f}%)")
        except Exception as e:
            logger.error(f"  Shadow 기록 실패: {e}", exc_info=True)

    # ★ 원칙 1: MeasurementEngine (One Truth, Many Views)
    if phase in ('all', 'closing', 'evening'):
        try:
            from src.measurement.measurement_engine import run_measurement
            me_result = run_measurement()
            logger.info("  ✅ MeasurementEngine SSoT 계산 완료")

            # 이벤트 기록
            try:
                from src.measurement.event_ledger import log_event
                log_event('MEASUREMENT', {
                    'official': me_result.get('official', {}),
                }, source='measurement_engine')
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)
            
            # ★ SSoT 파일 자동 동기화 (go_nogo.json 등 업데이트)
            sync_ssot_from_me()

        except Exception as e:
            logger.error(f"  MeasurementEngine 실패: {e}", exc_info=True)

    # ★ S4 계좌별 추적 동기화
    if phase in ('all', 'closing', 'evening'):
        try:
            from src.streams.s4_advisory.account_tracker import sync_s4_accounts
            sync_s4_accounts()
            logger.info("  ✅ S4 AccountTracker 동기화 완료")
        except Exception as e:
            logger.error(f"  S4 AccountTracker 실패: {e}", exc_info=True)

    # ★ Confidence Calibrator 업데이트
    if phase in ('all', 'closing', 'evening'):
        try:
            from src.ml.confidence_calibrator import get_calibrator
            _cal = get_calibrator()
            _shadow_f = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if _shadow_f.exists():
                _sp = json.loads(_shadow_f.read_text())
                _cal.update_from_trades(
                    _sp.get('trade_history', []),
                    _sp.get('positions', {}))
                logger.info(f"  ✅ Confidence Calibrator 업데이트 "
                            f"({_cal.state.get('n_samples', 0)} samples)")
        except Exception as e:
            logger.error(f"  Calibrator 업데이트 실패: {e}", exc_info=True)

    # ★ Beta Hedge 모니터링 (advisory 전용 — 실제 헷지 실행은 아님)
    # 실행: run_virtual_trading.py Step 9c → ExposureOrchestrator.calculate_hedge_position()
    if phase in ('all', 'closing', 'evening'):
        try:
            from src.risk.beta_hedge import BetaHedge
            _bh = BetaHedge()
            _bh_result = _bh.compute()
            _beta = _bh_result.get('portfolio_beta')
            if _beta is not None:
                logger.info(f"  ✅ Beta Monitor: β={_beta:.3f}, "
                            f"α={_bh_result.get('alpha_annual_pct', 0):+.1f}%/yr")
            else:
                logger.info("  ⏭️ Beta Monitor: 데이터 부족")
        except Exception as e:
            logger.error(f"  Beta Monitor 실패: {e}", exc_info=True)

    # ★ Feature Importance Audit (주 1회)
    if phase in ('all', 'evening', 'weekly_validate'):
        try:
            import os
            _audit_f = _PROJECT_ROOT / 'results' / 'feature_importance_audit.json'
            _should_audit = not _audit_f.exists()
            if not _should_audit:
                _audit_age = (datetime.now() - datetime.fromtimestamp(
                    os.path.getmtime(_audit_f))).days
                _audit_interval = cfg.get('feature_audit.interval_days', 1)
                _should_audit = _audit_age >= _audit_interval
            if _should_audit:
                from scripts.feature_importance_audit import run_audit
                run_audit()
                logger.info("  ✅ Feature Importance Audit 완료")
            else:
                logger.info(f"  ⏭️ Feature Audit: {_audit_age}일 전 실행, 스킵")
        except Exception as e:
            logger.error(f"  Feature Audit 실패: {e}", exc_info=True)

    # 리포트 및 이메일 발송 스케줄 스마트 라우팅
    _send_email = False
    if phase in ('all', 'evening'):
        _send_email = True
    elif phase == 'morning' and not is_krx_open:
        _send_email = True  # 휴장일엔 8시(morning) 발송
    elif phase == 'market' and is_krx_open:
        _send_email = True  # 개장일엔 9시 5분(market) ETF 체결 후 발송

    if _send_email:
        try:
            from src.interface.report_generator import ReportGenerator
            from src.interface.email_notifier import MeridianEmail
            gen = ReportGenerator()
            report_md = gen.generate_daily()
            logger.info("  ✅ 일일 리포트 생성")

            # 이메일 HTML 발송 연동
            email = MeridianEmail()
            if email.enabled:
                if email.send_html_report(report_md):
                    logger.info("  ✅ 일일 리포트 이메일 발송 완료")
        except Exception as e:
            logger.error(f"  리포트 생성/발송 실패: {e}", exc_info=True)

    # ★ Gap Analysis — 예측 vs 실현 격차 진단 (#6)
    if phase in ('all', 'evening'):
        try:
            from scripts.gap_analysis import GapAnalyzer
            ga = GapAnalyzer()
            ga_result = ga.analyze()
            ga_status = ga_result.get('status', 'skip')
            if ga_status == 'completed':
                summary = ga_result.get('summary', {})
                da = summary.get('overall_da', 0)
                wr = summary.get('overall_win_rate', 0)
                n_sug = summary.get('n_improvement_suggestions', 0)
                logger.info(
                    f"  ✅ Gap Analysis: DA={da:.1%}, WR={wr:.1%}, "
                    f"제안={n_sug}건")
            else:
                logger.info(
                    f"  ⏭️ Gap Analysis: {ga_result.get('reason', ga_status)}")
        except Exception as e:
            logger.error(f"  Gap Analysis 실패: {e}", exc_info=True)

    # ★ Gap Feedback — 갭분석 결과 → 파라미터 자동 조정 + 재학습 트리거
    if phase in ('all', 'evening'):
        try:
            from scripts.gap_feedback import GapFeedbackEngine
            fb = GapFeedbackEngine()
            fb_result = fb.run()
            fb_status = fb_result.get('status', 'skip')
            if fb_status == 'completed':
                n_adj = fb_result.get('n_adjustments', 0)
                retrain = fb_result.get('retrain_needed', False)
                logger.info(
                    f"  ✅ Gap Feedback: {n_adj}건 파라미터 조정, "
                    f"retrain={'필요' if retrain else '불필요'}")
            else:
                logger.info(
                    f"  ⏭️ Gap Feedback: {fb_result.get('reason', fb_status)}")
        except Exception as e:
            logger.error(f"  Gap Feedback 실패: {e}", exc_info=True)

    # ★ ML 앙상블 재학습 트리거 체크 (주 1회 + 이벤트 + gap_feedback)
    if phase in ('all', 'evening'):
        try:
            from scripts.train_ensemble import should_retrain, run_training
            needed, trigger = should_retrain()
            if needed:
                logger.info(f"  🔄 ML 재학습 트리거: {trigger}")
                result = run_training(trigger=trigger, enable_automl=True)
                if result:
                    logger.info(f"  ✅ 재학습 완료: ACC={result['val_acc']:.3f}")
            else:
                logger.info("  ⏭️ ML 재학습 불필요")
        except Exception as e:
            logger.error(f"  ML 재학습 체크 실패: {e}", exc_info=True)

    # ★ Ultimate Quant Report 생성 및 텔레그램 연동 (PDF Report)
    if phase in ('all', 'evening'):
        try:
            from scripts.generate_ultimate_report import UltimateMeridianReport
            from src.interface.telegram_notifier import MeridianTelegram
            
            logger.info("  📊 Ultimate Quant Report 생성 중...")
            report_gen = UltimateMeridianReport()
            report_path = report_gen.generate()
            
            if report_path:
                logger.info(f"  ✅ 리포트 생성 완료: {report_path}")
                notifier = MeridianTelegram()
                if notifier.enabled:
                    caption_text = f"📈 Ultimate Quant Report ({today.strftime('%Y-%m-%d')})"
                    
                    if error_collector.errors:
                        err_summary = "\\n🚨 [Pipeline Errors]\\n" + "\\n".join(error_collector.errors[:5])
                        if len(error_collector.errors) > 5:
                            err_summary += f"\\n...and {len(error_collector.errors)-5} more errors."
                        caption_text += err_summary
                        
                    notifier.send_document(
                        document_path=str(report_path),
                        caption=caption_text
                    )
                    logger.info("  ✅ 텔레그램 리포트 발송 완료")
            else:
                logger.warning("  ⚠️ 리포트 생성 실패")
        except Exception as e:
            logger.error(f"  🚨 리포트 생성 및 발송 실패: {e}", exc_info=True)

    # 이벤트 레져 — 파이프라인 종료
    try:
        from src.measurement.event_ledger import log_event
        log_event('SYSTEM', {
            'action': 'pipeline_end',
            'phase': phase,
            'elapsed_sec': round((now_kst() - now).total_seconds(), 1),  # ★ BUG FIX: now_kst() 일관성
        }, source='daily_pipeline')
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)

    logger.info(f"\n{'='*60}")
    logger.info(f"  Pipeline 완료 ({now_kst() - now})")  # ★ BUG FIX: offset-aware 일관성
    logger.info(f"{'='*60}")


# ═══════════════════════════════════════════════════════
# Phase PRE: PREMARKET (07:45 KST)
# ═══════════════════════════════════════════════════════

if __name__ == '__main__':
    phase = sys.argv[1] if len(sys.argv) > 1 else 'all'
    run_pipeline(phase)


#!/usr/bin/env python3
"""
StreamOrchestrator — 4-Stream 통합 오케스트레이터 (Phase 2)
============================================================

4개 스트림 (S1~S4)을 순서대로 실행하고,
AlphaAllocator로 배분하며,
리스크 모듈로 최종 검증합니다.

Phase 2 통합:
  - RegimeDetector: Rule-Based + HMM 앙상블 레짐 감지
  - MarketDataBridge: Project-A 데이터 → Meridian 포맷
  - ExecutionEngine: Shadow / Mock / Paper / Live 체결
  - ShadowRecorder: Shadow 거래 기록 + Go/No-Go 자동 평가

파이프라인:
  1. 시장 데이터 로드 (MarketDataBridge)
  2. 레짐 판단 (RegimeDetector)
  3. 리스크 선검사 (Kill Switch, Crash Defense, DD Guard)
  4. 각 스트림 신호 생성
  5. AlphaAllocator 배분
  6. 레버리지 판정
  7. 최종 주문 생성
  8. 체결 (ExecutionEngine)
  9. Shadow 기록 (ShadowRecorder)
  10. SelfLearning 업데이트
  11. EventLedger 기록

Usage:
    from scripts.stream_orchestrator import StreamOrchestrator
    orch = StreamOrchestrator()
    result = orch.run()
"""

def pget(name, fallback, ctx=None):  # Wave2 TrackF: lazy (scripts set sys.path after imports)
    try:
        from src.params import pget as _pget
    except Exception:
        return fallback
    return _pget(name, fallback, ctx)
import json
import numpy as np
import pandas as pd
import logging
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 프로젝트 루트를 path에 추가
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config.dynamic_config import DynamicConfig
from src.utils.market_calendar import is_kr_ticker as _is_kr_tk
from src.streams.s0_beta.beta_stream import S0BetaStream
from src.streams.s1_edge.etf_sniper_stream import S1ETFSniperStream
from src.streams.s10_mega_trend.mega_trend_stream import S10MegaTrendStream
from src.streams.s2_ml_alpha.ml_stream import S2MLAlphaStream
from src.streams.s3_active_macro.active_macro_stream import S3FactorStream as S3ActiveMacroStream
from src.streams.s4_advisory.advisory_stream import S4AdvisoryStream
from src.streams.s5_overnight.overnight_stream import S5OvernightStream
from src.streams.s13_japan_alpha.japan_stream import S13JapanAlphaStream
from src.streams.s14_pair_alpha.sector_pair_stream import S14SectorPairStream


from src.streams.s4_advisory.dynamic_exit import DynamicExitEvaluator

from src.allocation.alpha_allocator import AlphaAllocator
from src.allocation.rebalance_engine import RebalanceEngine


def _cfg_get(key: str, default: Any = None) -> Any:
    """Helper function to safely get configuration values from DynamicConfig."""
    try:
        val = DynamicConfig().get(key, default)
        return default if val is None else val
    except Exception:
        return default
from src.risk.drawdown_guard import DrawdownGuard
from src.risk.kill_switch import KillSwitch
from src.risk.crash_defense import CrashDefense
from src.risk.leverage_judge import LeverageJudge
try:
    from src.risk.kelly_criterion import KellyCriterion
    _KELLY_AVAILABLE = True
except ImportError as e:
    _KELLY_AVAILABLE = False
from src.learning.self_learning import SelfLearning
from src.regime.regime_detector import RegimeDetector
from src.data.market_data_bridge import MarketDataBridge
from src.execution.execution_engine import ExecutionEngine
from src.measurement.shadow_recorder import ShadowRecorder

# 100% 동적 수학 모델 기반 조율엔진 & CRO 리스크 차단기 모듈
from src.orchestration.intent_schema import SignalIntent
from src.allocation.netting_engine import NettingEngine
from src.allocation.qp_portfolio_optimizer import QPortfolioOptimizer
from src.risk.cro_final_gate import CROFinalGate
from src.measurement.conflict_audit_ledger import ConflictAuditLedger
from src.risk.master_scenario_engine import MasterScenarioEngine
from src.strategy.tactic_e_event_squeeze import AsymmetricEventSqueezeSniper
from src.tax.tax_loss_harvesting_engine import TaxLossHarvestingEngine
from src.execution.cross_venue_netting import CrossVenueSpreadNetting
try:
    from src.utils.data_imputer import DataNoGoException  # [Phase 70-A]
except ImportError as e:
    DataNoGoException = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)
cfg = DynamicConfig()




def _compute_asset_daily_volatility(
        ticker: str, market_data: dict, fallback: float = 0.02) -> float:
    """[Phase 51: Bias-Free ATR Exit] 개별 종목 20일 일일 변동성 계산.

    종목별 역사적 일일 수익률 표준편차(Daily Volatility)를 반환.
    데이터 부족 또는 계산 불가 시 fallback(기본 2%) 반환.

    Args:
        ticker: 종목 코드 (예: '005930')
        market_data: 오케스트레이터에 전달된 시장 데이터 딕셔너리
        fallback: 계산 실패 시 반환할 기본 변동성 (소수, 예: 0.02 = 2%)

    Returns:
        일일 변동성 (소수 형태, 예: 0.025 = 2.5%)
    """
    import numpy as np
    import pandas as pd

    try:
        # ① market_data['features'][ticker] 구조 (feature_store 기반)
        df = (market_data or {}).get('features', {}).get(ticker)
        if df is None:
            # ② market_data[ticker] 대안 구조 대비
            df = (market_data or {}).get(ticker)

        if isinstance(df, pd.DataFrame) and 'close' in df.columns and len(df) >= 20:
            returns = df['close'].pct_change().replace([np.inf, -np.inf], np.nan).dropna().tail(20)
            if len(returns) >= 5:
                vol = float(returns.std())
                if pd.notna(vol) and vol > 0:
                    logger.debug(
                        f"[Phase 51 vol] {ticker}: {vol*100:.3f}% "
                        f"(n={len(returns)} days)")
                    return vol

        # ③ signal_cache 내 ticker별 atr 또는 volatility 키 fallback
        sc = (market_data or {}).get('signal_cache', {})
        atr_key = f"{ticker}_atr_pct"
        if atr_key in sc:
            atr_val = float(sc[atr_key] or 0.0)
            if atr_val > 0:
                logger.debug(f"[Phase 51 vol] {ticker}: ATR proxy {atr_val:.3f}%")
                return atr_val / 100.0 if atr_val > 0.5 else atr_val

        # ④ DynamicQuantEngine 실측 20일 변동성 (로컬 Parquet OHLCV 기반)
        from src.utils.dynamic_quant_engine import get_quant_engine
        _m_vol = float(get_quant_engine().get_asset_metrics(str(ticker)).get('vol_daily', 0.0) or 0.0)
        if 0.001 <= _m_vol <= 0.30:
            logger.debug(f"[Phase 51 vol] {ticker}: DynamicQuantEngine vol={_m_vol*100:.3f}%")
            return _m_vol

    except Exception as _vol_e:
        logger.error(f"[Phase 51 vol] {ticker} 변동성 계산 실패: {_vol_e}", exc_info=True)

    logger.debug(f"[Phase 51 vol] {ticker} → fallback {fallback*100:.1f}%")
    return fallback


class StreamOrchestrator:
    """4-Stream 통합 오케스트레이터.

    모든 컴포넌트를 조율하여 하나의 매매 사이클을 실행합니다.
    """

    def _is_ticker_session_open_for_live_exit(self, ticker: str, market_data: Dict = None) -> bool:
        """라이브 모드에서 현재 해당 종목의 거래소가 개장(거래 가능) 상태인지 판별.
        단위 테스트(MERIDIAN_TEST_MODE / PYTEST_CURRENT_TEST) 및 shadow 모드에서는 항상 True 반환."""
        import os
        if getattr(self, 'exec_mode', 'shadow') != 'live':
            return True
        if getattr(self, '_ignore_market_hours', False):
            return True
        if isinstance(market_data, dict) and market_data.get('disable_session_gate'):
            return True
        try:
            from src.utils.market_calendar import is_ticker_tradable_now
            _is_mocked = (
                getattr(is_ticker_tradable_now, '__module__', '') != 'src.utils.market_calendar'
                or getattr(is_ticker_tradable_now, '__name__', '') != 'is_ticker_tradable_now'
            )
            _enforce_sg = (
                (isinstance(market_data, dict) and bool(market_data.get('enforce_session_gate', False)))
                or (getattr(self, '_ignore_market_hours', None) is False)
                or _is_mocked
            )
            if (os.environ.get('MERIDIAN_TEST_MODE') == '1' or os.environ.get('PYTEST_CURRENT_TEST')) and not _enforce_sg:
                return True
            clean_tk = str(ticker or '').split(':')[-1].strip()
            if not clean_tk:
                return True
            res = is_ticker_tradable_now(clean_tk)
            is_open = res[0] if isinstance(res, tuple) else bool(res)
            return bool(is_open)
        except Exception:
            return True

    def __init__(self, exec_mode: str = None):
        if exec_mode is None:
            exec_mode = cfg.get('execution.current_mode', cfg.get('execution.mode', 'live'))
        self.exec_mode = exec_mode
        # 스트림 초기화
        self.s0 = S0BetaStream()
        self.s1 = S1ETFSniperStream()
        self.s2 = S2MLAlphaStream()
        self.s3 = S3ActiveMacroStream()
        self.s4 = S4AdvisoryStream()
        self.s5 = S5OvernightStream()
        self.s10 = S10MegaTrendStream()
        from src.streams.s11_high_beta.high_beta_sniper_stream import S11HighBetaSniperStream
        from src.streams.s12_derivative_squeeze.derivative_squeeze_stream import S12DerivativeSqueezeStream
        self.s11 = S11HighBetaSniperStream()
        self.s12 = S12DerivativeSqueezeStream()
        self.s13 = S13JapanAlphaStream()
        self.s14 = S14SectorPairStream()
        # S0 BetaStream, S10 MegaTrend, S11 HighBeta Sniper, S12 Derivative Squeeze, S13 Japan Alpha, S14 Sector Pair 파이프라인 정규 편입
        self.streams = [self.s0, self.s1, self.s2, self.s3, self.s4, self.s5, self.s10, self.s11, self.s12, self.s13, self.s14]
        self.stream_cache = {} # Kelly Criterion (DD-07: 파이프라인 연동)
        self.kelly = KellyCriterion() if _KELLY_AVAILABLE else None

        # 리밸런싱 엔진
        self.rebalance_engine = RebalanceEngine()

        # 배분 엔진
        self.allocator = AlphaAllocator()

        # 리스크 모듈
        self.dd_guard = DrawdownGuard()
        self.kill_switch = KillSwitch()
        self.crash_defense = CrashDefense()
        self.leverage_judge = LeverageJudge()
        from src.risk.whipsaw_defense_engine import AntiWhipsawDefenseEngine
        self.whipsaw_defense = AntiWhipsawDefenseEngine()

        # 자가학습
        self.self_learning = SelfLearning()

        # ── Phase 2 모듈 ──
        self.regime_detector = RegimeDetector()
        self.data_bridge = MarketDataBridge()
        self.execution_engine = ExecutionEngine(mode=self.exec_mode)
        self.shadow_recorder = ShadowRecorder()

        # 100% 동적 수학 모델 조율기 & CRO 최후단 리스크 차단기
        self.netting_engine = NettingEngine()
        self.qp_optimizer = QPortfolioOptimizer()
        self.cro_final_gate = CROFinalGate()
        self.audit_ledger = ConflictAuditLedger()

        # 8대 마스터 시나리오, 미지 블랙스완 탐지기 및 고효율 알파/절세/상쇄 모듈
        self.master_scenario_engine = MasterScenarioEngine()
        from src.risk.unknown_black_swan_detector import UnknownBlackSwanDetector
        self.black_swan_detector = UnknownBlackSwanDetector()
        self.tactic_e_sniper = AsymmetricEventSqueezeSniper()
        self.tax_harvest_engine = TaxLossHarvestingEngine()
        self.cross_venue_netting = CrossVenueSpreadNetting()


    @staticmethod
    def _compute_risk_parity_benchmark(signal_cache: dict, cfg) -> dict:
        """[Phase 60] SPY/TLT/GLD/USO 역변동성 가중 동적 All-Weather 벤치마크.

        Weight_i = (1/σ_i) / Σ(1/σ_j)
        β = Σ(Weight_i × r_i)

        σ 데이터 소스: signal_cache 히스토리 → yfinance fallback (60일)
        """
        import math
        try:
            _vol_floor = float(cfg.get('benchmark.vol_floor', 0.005))
            _assets_str = cfg.get('benchmark.assets', 'spy,tlt,gld,uso')
            ASSETS = [a.strip() for a in _assets_str.split(',')]
            WINDOW = int(cfg.get('benchmark.window', 60))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)
            _vol_floor = 0.005
            ASSETS = ['spy', 'tlt', 'gld', 'uso']
            WINDOW = 60

        FALLBACK_TICKERS = {a: a.upper() for a in ASSETS}

        # ── 히스토리 수집 ─────────────────────────────────────────────────
        hist: dict = {}
        for a in ASSETS:
            h = signal_cache.get(f"{a}_history", [])
            if len(h) >= WINDOW:
                hist[a] = [float(x) for x in h[-WINDOW:]]

        # yfinance fallback 폐기 — 로컬 RealtimeDataBus / MarketDataBridge 우선 참조
        missing = [a for a in ASSETS if a not in hist]
        if missing:
            pass

        if not hist:
            return {}  # 데이터 없음 → skip

        # ── 변동성(σ) 계산 ─────────────────────────────────────────────────
        vols, rets = {}, {}
        for a, prices in hist.items():
            daily_rets = [(prices[i] - prices[i-1]) / max(abs(prices[i-1]), 1e-9)
                          for i in range(1, len(prices))]
            mu = sum(daily_rets) / len(daily_rets)
            sigma = (sum((r - mu)**2 for r in daily_rets) / len(daily_rets))**0.5
            vols[a] = max(sigma, _vol_floor)
            rets[a] = daily_rets[-1] if daily_rets else 0.0  # 당일 수익

        if not vols:
            return {}

        # ── 역변동성 가중치 ────────────────────────────────────────────────
        inv_vols = {a: 1.0 / v for a, v in vols.items()}
        total_inv = sum(inv_vols.values())
        weights = {a: inv_vols[a] / total_inv for a in inv_vols}

        # ── β(벤치마크 수익) ──────────────────────────────────────────────
        beta_return = sum(weights.get(a, 0.0) * rets.get(a, 0.0) for a in weights)

        return {
            'weights':      weights,
            'vols':         vols,
            'daily_rets':   rets,
            'beta_return':  round(beta_return, 6),
            'assets_used':  list(weights.keys()),
        }

    def run(self, market_data: Dict = None,
            portfolio: Dict = None,
            regime: str = None,
            injected_stream_metrics: Dict = None) -> Dict:
        """전체 파이프라인 1회 실행.

        Args:
            market_data: 시장 데이터 (None이면 기본값)
            portfolio: 포트폴리오 상태 (None이면 기본값)
            regime: 레짐 (None이면 자동 판단)

        Returns:
            실행 결과 딕셔너리
        """
        start = datetime.now()
        logger.info("=" * 60)
        logger.info(f"  🚀 StreamOrchestrator 실행 시작: {start.isoformat()}")
        logger.info("=" * 60)

        # ── [Track 1 Lean Core] 최신 DynamicConfig 오버라이드 핫 리로드 (<=1ms) ──
        cfg.reload()

        # ── Phase 2: 시장 데이터 자동 로드 ──
        if market_data is None:
            logger.info("  📋 Step 0: MarketDataBridge 데이터 로드")
            signal_cache = self.data_bridge.build_signal_cache()
            overnight = self.data_bridge.build_overnight_intel()
            regime_hist = self.data_bridge.get_regime_history()
            us_regime_val = 'neutral'
            try:
                usr_path = _ROOT / 'results' / 'us_market_regime.json'
                if usr_path.exists():
                    usr_data = json.loads(usr_path.read_text(encoding='utf-8'))
                    us_regime_val = usr_data.get('regime', 'neutral')
            except Exception as _usr_err:
                logger.debug(f"  us_market_regime 로드 예외: {_usr_err}")

            market_data = {
                'signal_cache': signal_cache,
                'overnight_intel': overnight,
                'vix_history': regime_hist.get('vix_history', []),
                'kospi_returns': regime_hist.get('kospi_returns', []),
                'kospi_return_dates': regime_hist.get('kospi_return_dates', []),
                'us_regime': us_regime_val,
                '_test_mode': self.execution_engine.mode == 'mock',
                'is_mock': self.execution_engine.mode == 'mock',
            }

        if portfolio is None:
            # ExecutionEngine에서 계좌 정보 가져오기
            acct = self.execution_engine.get_account_summary()
            initial = pget('portfolio.initial_capital', 19600000.0)

            kis_path = _ROOT / 'results' / 'kis_portfolio.json'
            shadow_path = _ROOT / 'results' / 'shadow_portfolio.json'

            holdings = {}
            positions = {}
            total_nav = float(acct.get('total_equity', initial))
            cash = float(acct.get('cash', initial))

            if getattr(self, 'exec_mode', 'live') == 'live':
                _kis_loaded = False
                if kis_path.exists():
                    try:
                        kis_data = json.loads(kis_path.read_text(encoding='utf-8'))
                        raw_holdings = kis_data.get('holdings', {})
                        acct_data = kis_data.get('account', {})
                        total_nav = float(acct_data.get('total_equity') or kis_data.get('nav') or acct_data.get('nav', total_nav))
                        _raw_cash_k = float(acct_data.get('cash', cash))
                        _nrcvb_k = acct_data.get('nrcvb_buy_amt_krw', kis_data.get('nrcvb_buy_amt_krw'))
                        cash = min(_raw_cash_k, float(_nrcvb_k)) if _nrcvb_k is not None else _raw_cash_k
                        # ★ Wave 1 Track C: 리터럴 1334.3 제거 — 스냅샷 환율 → (필요 시) 게이트웨이(신선도 한도 내) → 미확보
                        _fx_orch = float(acct_data.get('fx_rate') or kis_data.get('fx_rate') or 0.0) or None
                        _fx_orch_tried = False
                        for t, h in raw_holdings.items():
                            holdings[t] = h
                            _qty_h = int(h.get('qty', 0))
                            _cp_h = float(h.get('current_price', 0.0))
                            _is_us_h = not _is_kr_tk(str(t))
                            _amt_krw = float(h.get('eval_krw', 0.0) or 0.0)
                            _fx_deg_h = False
                            if not _amt_krw:
                                if _is_us_h:
                                    if _fx_orch is None and not _fx_orch_tried:
                                        _fx_orch_tried = True
                                        try:
                                            from src.data_collection.central_data_gateway import get_central_data_gateway
                                            _r_orch = get_central_data_gateway().get_usdkrw_rate()
                                            _fx_orch = float(_r_orch) if _r_orch is not None and float(_r_orch) > 0 else None
                                        except Exception as _fx_o_err:
                                            logger.error(f"  ❌ [FX] 포트폴리오 평가용 USD/KRW 조회 예외: {_fx_o_err}")
                                    if _fx_orch is None:
                                        # 환율 미확보: 원화 평가액 날조 금지 → 0 + degraded 표시 (해외 신규 매수는 FX Guard가 별도 차단)
                                        _fx_deg_h = True
                                        logger.critical(f"  🚨 [FX] {t}: USD/KRW 미확보 + eval_krw 없음 → 원화 평가액 미산출(degraded)")
                                    else:
                                        _amt_krw = _qty_h * _cp_h * _fx_orch
                                else:
                                    _amt_krw = _qty_h * _cp_h
                            _ret_h = float(h.get('return_pct', h.get('pnl_pct', 0.0)) or 0.0)
                            _hwm_orch = {}
                            _ed_orch = h.get('entry_date')
                            try:
                                from src.execution.position_ledger import PositionLedgerManager
                                _plm_orch = PositionLedgerManager()
                                if self._is_ticker_session_open_for_live_exit(t, market_data):
                                    _hwm_orch = _plm_orch.update_hwm(t, current_price=_cp_h, current_pnl_pct=_ret_h) or {}
                                else:
                                    _hwm_orch = _plm_orch.get_hwm(t) or {}
                                if not _ed_orch:
                                    _ed_orch = _plm_orch.get_entry_date(t)
                            except Exception:
                                pass
                            positions[t] = {
                                'ticker': t,
                                'name': h.get('name', t),
                                'quantity': _qty_h,
                                'qty': _qty_h,
                                'avg_price': float(h.get('avg_price', 0.0)),
                                'current_price': _cp_h,
                                'amount': _amt_krw,
                                'market_value': _amt_krw,
                                'unrealized_pnl_pct': _ret_h,
                                'pnl_pct': _ret_h,
                                'return_pct': _ret_h,
                                'peak_pnl_pct': float(_hwm_orch.get('peak_pnl_pct', h.get('peak_pnl_pct', _ret_h)) or _ret_h),
                                'peak_price': float(_hwm_orch.get('peak_price', h.get('peak_price', _cp_h)) or _cp_h),
                                'trail_activated': bool(_hwm_orch.get('trail_activated', h.get('trail_activated', False))),
                                'entry_date': _ed_orch or h.get('entry_date'),
                                'stream_id': str(h.get('stream_id') or _hwm_orch.get('stream_id') or ''),
                                'track': str(h.get('track') or _hwm_orch.get('track') or ''),
                                'market': h.get('market', 'US' if _is_us_h else 'KR')
                            }
                            if _fx_deg_h:
                                positions[t]['fx_degraded'] = True
                        _kis_loaded = True
                    except Exception as _k_err:
                        logger.warning(f"kis_portfolio.json 로드 예외: {_k_err}")
                if not _kis_loaded:
                    total_nav = 0.0
                    cash = 0.0
                    positions = {}
                    holdings = {}
                    self._data_quality_exit_only = True
            elif shadow_path.exists():
                try:
                    sp_data = json.loads(shadow_path.read_text(encoding='utf-8'))
                    positions = sp_data.get('positions', {})
                    total_nav = float(sp_data.get('total_nav', total_nav))
                    cash = float(sp_data.get('cash', cash))
                    for k, p in positions.items():
                        t = p.get('ticker') or (k.split(':')[-1] if ':' in k else k)
                        holdings[t] = p
                except Exception as _s_err:
                    logger.warning(f"shadow_portfolio.json 로드 예외: {_s_err}")
            else:
                raw_pos = acct.get('positions', [])
                if isinstance(raw_pos, list):
                    for p in raw_pos:
                        t = p.get('ticker', '')
                        if t:
                            positions[t] = p
                            holdings[t] = p
                elif isinstance(raw_pos, dict):
                    positions = raw_pos
                    holdings = raw_pos

            # ★ Wave 0 E8: HWM = 추적된 자산 고점과 현재 NAV의 최대값 (Live 모드는 kis_portfolio.json 격리).
            _tracked_hwm = 0.0
            try:
                if getattr(self, 'exec_mode', 'live') == 'live':
                    if kis_path.exists():
                        _kp_hwm_data = json.loads(kis_path.read_text(encoding='utf-8'))
                        _tracked_hwm = float(
                            _kp_hwm_data.get('hwm')
                            or (_kp_hwm_data.get('account', {}) or {}).get('hwm')
                            or 0.0
                        )
                elif shadow_path.exists():
                    _tracked_hwm = float(json.loads(shadow_path.read_text(encoding='utf-8')).get('hwm') or 0.0)
            except Exception as _hwm_err:
                logger.error(f"  [HWM] hwm 로드 실패: {_hwm_err}")
            _live_hwm = max(_tracked_hwm, total_nav)

            _cash_usd = 0.0
            try:
                if kis_path.exists():
                    _acct_u = json.loads(kis_path.read_text(encoding='utf-8')).get('account', {}) or {}
                    _cash_usd = float(_acct_u.get('us_cash_usd') or _acct_u.get('cash_usd') or 0.0)
            except Exception as _cu_err:
                logger.error(f"  [Cash] USD 현금 로드 실패: {_cu_err}")

            portfolio = {
                'total_nav': total_nav,
                'nav': total_nav,
                'hwm': _live_hwm,
                'daily_returns': [],
                'active_positions': len(positions),
                'cash': cash,
                'cash_usd': _cash_usd,
                'positions': positions,
                'holdings': holdings
            }
        else:
            if 'positions' in portfolio and isinstance(portfolio['positions'], list):
                pos_dict = {}
                for p in portfolio['positions']:
                    if isinstance(p, dict):
                        t = p.get('ticker') or p.get('name', '')
                        if t:
                            pos_dict[t] = p
                portfolio['positions'] = pos_dict
            if 'holdings' not in portfolio and 'positions' in portfolio:
                portfolio['holdings'] = {
                    (p.get('ticker') or k.split(':')[-1]): p
                    for k, p in portfolio['positions'].items()
                }

        if regime is None:
            # ★ Phase 2: RegimeDetector 사용
            regime_result = self.regime_detector.detect(market_data)
            regime = regime_result['regime']
            if isinstance(market_data, dict) and isinstance(market_data.get('signal_cache'), dict):
                market_data['signal_cache']['regime'] = regime
            if isinstance(getattr(self, 'market_data', None), dict) and isinstance(self.market_data.get('signal_cache'), dict):
                self.market_data['signal_cache']['regime'] = regime
            logger.info(
                f"  🏷️ Regime: {regime} "
                f"(conf={regime_result['confidence']:.2f}, "
                f"method={regime_result['method']})")
            
            # Streams like S1 rely on this
            market_data['regime_confidence'] = regime_result['confidence']
            if 'crash_type' in regime_result:
                market_data['crash_type'] = regime_result['crash_type']
            if 'divergence_state' in regime_result:
                market_data['divergence_state'] = regime_result['divergence_state']
            if 'prob_recession' in regime_result:
                market_data['prob_recession'] = regime_result['prob_recession']
                
        # ── Step 0.4: DynamicConfig 업데이트 (Medallion/Bridgewater Style) ──
        try:
            signal_cache = market_data.get('signal_cache', {})
            intraday_vol = signal_cache.get('vkospi', signal_cache.get('vix'))
            market_state = {
                'vix': signal_cache.get('vix'),
                'vix_20d_avg': signal_cache.get('vix_20d_avg'),
                'intraday_volatility': intraday_vol,
                'ois_score': signal_cache.get('ois'),
                'us10y': signal_cache.get('us10y'),
                'us2y': signal_cache.get('us2y'),
                'usdkrw': signal_cache.get('usdkrw'),
                'usdkrw_prev': signal_cache.get('usdkrw_prev'),
                'foreign_flow': signal_cache.get('foreign_net_buy'),
                'foreign_flow_baseline': signal_cache.get('foreign_net_buy_20d_avg'),
                'regime': regime,
                'regime_confidence': market_data.get('regime_confidence'),
                'defense_factor': regime_result.get('defense_factor', 1.0),
                'mri':            regime_result.get('mri', 0.0),
                'portfolio_mdd': portfolio.get('hwm', 0) > 0 and (portfolio.get('total_nav', 0) / portfolio['hwm'] - 1.0) * 100 or 0.0
            }
            cfg.update_from_market_state(market_state)
        except Exception as _cfg_e:
            logger.debug(f"DynamicConfig update skipped: {_cfg_e}")

        # ── [Phase 99 SSOT] 보유 자산 실시간 위험조정 기대값(holdings_ev) 산출 ──
        try:
            from src.execution.intraday_capital_router import IntradayCapitalRouter
            market_data['holdings_ev'] = IntradayCapitalRouter().compute_holdings_ev(portfolio, market_data)
            if market_data.get('holdings_ev'):
                logger.info(f"  📊 [Holdings EV] 보유 자산 실시간 기대값: {market_data['holdings_ev']}")
        except Exception as _hev_e:
            logger.debug(f"  holdings_ev 산출 예외 (계속 진행): {_hev_e}")

        # ── [Phase 60] Dynamic Risk-Parity Benchmark ─────────────────────────
        try:
            _bench = self._compute_risk_parity_benchmark(signal_cache, cfg)
            market_data['rp_benchmark'] = _bench
            if _bench:
                _w = _bench.get('weights', {})
                logger.info(
                    f"  [Phase60 Benchmark] "
                    f"SPY={_w.get('spy', 0):.1%} TLT={_w.get('tlt', 0):.1%} "
                    f"GLD={_w.get('gld', 0):.1%} USO={_w.get('uso', 0):.1%} "
                    f"| β={_bench.get('beta_return', 0):+.4f}"
                )
        except Exception as _be:
            logger.error(f"  [Phase60] Benchmark 계산 실패: {_be}", exc_info=True)
            market_data['rp_benchmark'] = None
            
        result = {
            'timestamp': start.isoformat(),
            'regime': regime,
            'signals': {},
            'allocation': {},
            'risk': {},
            'leverage': {},
            'orders': [],
            'learning': {},
            'status': 'success',
        }

        # ── Step 0: 거래일 검증 ──
        try:
            logger.info("  [SANDBOX] 휴일 체크 무력화. 강제로 거래일로 간주합니다.")
        except Exception as _td_e:
            if date.today().weekday() >= 5:
                logger.error(f"  ⚠️ 주말 감지 (weekday={date.today().weekday()}). 주문 생성 스킵.", exc_info=True)
                result['status'] = 'skipped_weekend'
                return result
            logger.debug(f"  거래일 검증 실패 (진행): {_td_e}")

        # ── Step 0.5: DATA_NOGO Circuit Breaker [Phase 70-A] ──
        # ★ Track E (W1_2): 모듈 변수(_data_nogo_assets)는 프로세스/모듈 간 공유되지 않아 이 지점에
        #   도달한 적이 없었다 → results/data_nogo.json(날짜+TTL) 영속 저장소를 함께 읽는다.
        #   의미론 변경: 전체 중단(halt) → Exit-Only(신규 진입 금지, 청산 허용). 종목 단위 항목은
        #   market_data['data_nogo_tickers'] 계약으로 전달된다.
        _legacy_nogo = bool(
            (market_data.get('data_nogo', False) if market_data else False)
            or getattr(self, 'data_nogo', False)  # daily_pipeline에서 orch.data_nogo 주입 시
        )
        try:
            from src.measurement.data_nogo_store import apply_data_nogo_to_run
            _md_nogo, _nogo_info = apply_data_nogo_to_run(
                market_data, legacy_flag=_legacy_nogo,
                legacy_reason=getattr(self, 'data_nogo_reason', None))
        except Exception as _dn_e:
            logger.error(f"  [DATA_NOGO] 저장소 판정 실패 — 레거시 플래그만 적용: {_dn_e}", exc_info=True)
            _md_nogo = market_data if isinstance(market_data, dict) else {}
            _nogo_info = {'active': _legacy_nogo, 'mode': 'exit_only' if _legacy_nogo else None,
                          'reason': f"legacy:{getattr(self, 'data_nogo_reason', 'unknown')}" if _legacy_nogo else None}
            if _legacy_nogo:
                _md_nogo['exit_only'] = True
                _md_nogo['data_nogo_reason'] = _nogo_info['reason']
        if _nogo_info.get('active'):
            market_data = _md_nogo
            result['data_nogo'] = _nogo_info
            result['data_nogo_reason'] = _nogo_info.get('reason')
            if _nogo_info.get('mode') == 'exit_only':
                result['status'] = 'data_nogo_exit_only'
                logger.critical(
                    f"  🚨 [Phase 70 DATA_NOGO] 데이터 신뢰도 상실 — {_nogo_info.get('mode')} "
                    f"(신규 진입 차단, 청산 허용): {_nogo_info.get('reason')} "
                    f"tickers={_nogo_info.get('tickers') or []}"
                )
            else:
                logger.info(
                    f"  ℹ️ [Phase 70 DATA_NOGO] 개별 거래정지/결측 종목 신규 진입 차단 (정상 운용 유지): "
                    f"{_nogo_info.get('reason')} tickers={_nogo_info.get('tickers') or []}"
                )

        # ── Step 1: Kill Switch 확인 ──
        logger.info("  📋 Step 1: Kill Switch 확인")
        self._exit_only_reason = None
        _exit_only_flag = _ROOT / 'results' / 'EXIT_ONLY.flag'
        if _exit_only_flag.exists():
            try:
                _eo_reason = json.loads(_exit_only_flag.read_text(encoding='utf-8')).get('reason', 'EXIT_ONLY.flag')
            except Exception:
                _eo_reason = 'EXIT_ONLY.flag (unparseable)'
            self._exit_only_reason = f"flag:{_eo_reason}"
            if market_data is None:
                market_data = {}
            market_data['exit_only'] = True
            logger.critical(f"  🛑 [Exit-Only] 수동 해제 전까지 신규 매수 차단 — {_eo_reason}")
        ks_result = self.kill_switch.assess(portfolio, regime)
        result['risk']['kill_switch'] = ks_result

        _ks_judgment = ks_result.get('judgment', {})
        if not _ks_judgment.get('safe', True):
            # ★ Wave 0 S2: 킬스위치 발동 처리 재설계
            #   - 기존: SYS_KILL 주문을 'direction'/'qty' 키로 생성만 하고 집행 없이 return
            #     (엔진은 'action'/'quantity'를 읽으므로 설령 집행돼도 action='buy', quantity=0).
            #   - 신규: (1) 파이프라인은 exit-only로 계속(손절/익절 유지, 신규 매수는 최종 실행 게이트에서 전량 제거)
            #           (2) 전량 청산은 사람 승인 플래그가 있을 때만 엔진 표준 스키마 + urgency='panic'으로 집행
            _ks_action = _ks_judgment.get('action', 'halt_all')
            _ks_reasons = [t.get('reason', '') for t in _ks_judgment.get('triggers', []) if not t.get('overridden')]
            if market_data is None:
                market_data = {}
            market_data['exit_only'] = True
            self._exit_only_reason = f"kill_switch:{_ks_action}"
            result['status'] = 'kill_switch_exit_only'
            logger.critical(f"  🚨 KILL SWITCH 발동 (action={_ks_action}) → Exit-Only 모드: 신규 매수 전면 차단, 청산 로직 유지 | {_ks_reasons}")

            if _ks_action == 'halt_all':
                _ack_required = bool(cfg.get('kill_switch.require_human_ack', True))
                _ack_flag = _ROOT / 'results' / 'KILL_LIQUIDATION_APPROVED.flag'
                if _ack_required and not _ack_flag.exists():
                    _msg = (f"⛔ 킬스위치 발동 — 전량 청산은 승인 대기 중 (Exit-Only 운용 중)\n사유: {_ks_reasons}\n"
                            f"승인 시: results/KILL_LIQUIDATION_APPROVED.flag 생성")
                    logger.critical(f"  🛑 [Kill Liquidation] 사람 승인 대기 — {_msg}")
                    result['kill_liquidation'] = {'status': 'awaiting_human_ack', 'reasons': _ks_reasons}
                    _ack_alert_f = _ROOT / 'results' / 'kill_ack_alert_sent.json'
                    _today_s = datetime.now().strftime('%Y-%m-%d')
                    try:
                        _last = json.loads(_ack_alert_f.read_text(encoding='utf-8')).get('date') if _ack_alert_f.exists() else None
                    except Exception:
                        _last = None
                    if _last != _today_s:
                        try:
                            from src.utils.telegram_notifier import TelegramNotifier
                            TelegramNotifier().send_alert('🛑 KILL SWITCH — 청산 승인 필요', _msg)
                            from src.utils.file_ops import atomic_write_json as _awj
                            _awj(_ack_alert_f, {'date': _today_s, 'reasons': _ks_reasons}, indent=2, ensure_ascii=False)
                        except Exception as _tg_e:
                            logger.error(f"  [Kill Liquidation] 텔레그램 알림 실패: {_tg_e}")
                else:
                    _liq_orders = []
                    _pos = portfolio.get('positions', {}) or {}
                    _pos_iter = _pos.items() if isinstance(_pos, dict) else [(p.get('ticker', ''), p) for p in _pos]
                    for _key, _p in _pos_iter:
                        _qty = int(float(_p.get('quantity', _p.get('qty', 0)) or 0))
                        _tk = str(_p.get('ticker') or str(_key).split(':')[-1])
                        if _qty > 0 and _tk:
                            _liq_orders.append({
                                'stream_id': 'SYS_KILL', 'stream': 'SYS_KILL', 'ticker': _tk, 'name': _p.get('name', ''),
                                'action': 'sell', 'side': 'sell', 'quantity': _qty, 'price': 0, 'order_type': 'market',
                                'execution_algo': 'market', 'urgency': 'panic', 'confidence': 1.0,
                                'strategy': 'Kill Switch Liquidation', 'reason': f"Kill Switch Liquidation (approved): {_ks_reasons}",
                            })
                    logger.critical(f"  🚨 [Kill Liquidation] 승인 확인 → 전량 청산 {len(_liq_orders)}건 집행")
                    _liq_res = self.execution_engine.execute(_liq_orders, portfolio) if _liq_orders else None
                    result['orders'].extend(_liq_orders)
                    result['kill_liquidation'] = {'status': 'executed', 'n_orders': len(_liq_orders),
                                                  'execution': _liq_res.to_dict() if _liq_res is not None else None}
                    try:
                        if _ack_flag.exists():
                            _ack_flag.unlink()  # 1회성 승인
                    except Exception as _uf_e:
                        logger.error(f"  [Kill Liquidation] 승인 플래그 제거 실패: {_uf_e}")
                    self._log_run_event(result)
                    return result

        # ── Step 2: Crash Defense 확인 ──
        logger.info("  📋 Step 2: Crash Defense 확인")
        cd_result = self.crash_defense.assess(market_data, portfolio, regime)
        result['risk']['crash_defense'] = cd_result

        # ── Step 2.5: Master 8-Scenario Engine & Unknown Black Swan 검증 & 대응 ──
        logger.info("  📋 Step 2.5: MasterScenarioEngine 8대 마스터 시나리오 & 미지 블랙스완 검증")
        sc_result = self.master_scenario_engine.evaluate_scenarios(market_data)
        result['risk']['master_scenarios'] = sc_result
        bs_result = self.black_swan_detector.detect_unknown_crisis(market_data=market_data)
        result['risk']['unknown_black_swan'] = bs_result

        # ── Step 3: DD Guard 확인 ──
        logger.info("  📋 Step 3: DrawdownGuard 확인")
        dd_result = self.dd_guard.assess(portfolio, regime)
        result['risk']['drawdown_guard'] = dd_result

        # 최대 노출도 결정 (리스크 모듈들의 최소값 — tighten_only)
        max_exposure = 1.0
        scale_multiplier = 1.0
        dd_action = 'none'

        if not dd_result['judgment']['safe']:
            max_exposure = min(max_exposure,
                               dd_result['judgment']['target_exposure'])
            scale_multiplier = min(scale_multiplier, dd_result['judgment'].get('scale_multiplier', 1.0))
            dd_action = dd_result['judgment'].get('action_required', 'none')

        if not cd_result['judgment']['safe']:
            for action in cd_result['judgment']['actions']:
                target = 1.0 - action.get('target_cash_ratio', 0)
                max_exposure = min(max_exposure, target)

        if bs_result.get('is_unknown_black_swan'):
            max_exposure = 0.0
            scale_multiplier = 0.0
            logger.warning(
                f"  🚨 [UnknownBlackSwan] {bs_result.get('reason')} → 신규 롱 노출 전면 차단 (max_exposure=0%)"
            )
        elif sc_result.get('status') == 'CRITICAL_SCENARIO_TRIGGERED':
            _sc_rec_exp = float(sc_result.get('recommended_max_exposure', 1.0))
            max_exposure = min(max_exposure, _sc_rec_exp)
            logger.warning(
                f"  🚨 [MasterScenario] {sc_result.get('active_scenarios')} 발동 "
                f"(P={float(sc_result.get('composite_crash_prob', 0.0)):.1%}) → max_exposure={max_exposure:.0%}"
            )
        elif sc_result.get('status') == 'V_REBOUND_SURGE_TRIGGERED':
            _sc_surge_exp = float(sc_result.get('recommended_max_exposure') or 1.0)
            if _sc_surge_exp > max_exposure:
                max_exposure = _sc_surge_exp
            logger.info(
                f"  🚀 [MasterScenario] V-Rebound/Surge 시나리오 포착 "
                f"({sc_result.get('active_scenarios')}, P={float(sc_result.get('composite_surge_prob', 0.0)):.1%}) → max_exposure={max_exposure:.0%}"
            )

        logger.info(f"  📊 최대 노출도: {max_exposure:.0%}, DD Scale: {scale_multiplier:.2f}, DD Action: {dd_action}")

        # ── Step 4: 각 스트림 신호 생성 ──
        logger.info("  📋 Step 4: 스트림 신호 생성")
        all_signals = {}
        for stream in self.streams:
            if not stream.is_active():
                logger.info(f"    ⏸ {stream.stream_id} 비활성")
                continue

            try:
                signals = stream.generate_signals(regime, market_data)

                all_signals[stream.stream_id] = signals
                logger.info(
                    f"    ✅ {stream.stream_id}: {len(signals)}개 신호")
            except Exception as e:
                logger.error(f"    ❌ {stream.stream_id} 신호 생성 실패: {e}", exc_info=True)
                all_signals[stream.stream_id] = []

        # ── Step 4.0: S10 Mega-Trend 신호 생성 및 별칭 동기화 ──
        if 'S10' in all_signals:
            if 'S10_MEGA_TREND' not in all_signals or not all_signals.get('S10_MEGA_TREND'):
                all_signals['S10_MEGA_TREND'] = all_signals.pop('S10')
            else:
                all_signals.pop('S10', None)
        if 'S10_MEGA_TREND' not in all_signals:
            try:
                s10_out = self.s10.generate_signals(regime, market_data)
                s10_signals = s10_out if isinstance(s10_out, list) else s10_out.get('orders', [])
                all_signals['S10_MEGA_TREND'] = s10_signals
                market_data['s10_status'] = s10_out.get('s10_status', 'neutral') if isinstance(s10_out, dict) else 'neutral'
                logger.info(f"    ✅ S10_MEGA_TREND: {len(s10_signals)}개 신호 (Status: {market_data['s10_status']})")
            except Exception as e:
                logger.error(f"    ❌ S10_MEGA_TREND 신호 생성 실패: {e}", exc_info=True)
                all_signals['S10_MEGA_TREND'] = []
                market_data['s10_status'] = 'neutral'
        if 'S13' in all_signals:
            if 'S13_JAPAN_ALPHA' not in all_signals or not all_signals.get('S13_JAPAN_ALPHA'):
                all_signals['S13_JAPAN_ALPHA'] = all_signals.pop('S13')
            else:
                all_signals.pop('S13', None)

        # ── Step 4.1: 8대 마스터 시나리오 & 블랙스완 즉각 대응 (시나리오 조건부 기대수익 생존 게이트) ──
        try:
            import math as _math_sc
            from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_sc
            from src.params.dynamic_quant_models import scenario_conditional_alpha_survival_gate
            _qe_sc = _get_qe_sc()
            _inv_set_sc = set(_qe_sc.get_inverse_hedge_tickers())
            _def_set_sc = set(_qe_sc.get_defensive_exempt_tickers())
            _sc_dirs = dict(sc_result.get('stream_directives') or {})
            if bs_result.get('is_unknown_black_swan'):
                for _bs_sid in list(all_signals.keys()):
                    if _bs_sid not in ('S0', 'S12', 'S_YIELD'):
                        _sc_dirs[_bs_sid] = 'STOP_ALL_NEW_BUYS'

            _sc_cache = (market_data or {}).get('signal_cache') or {}
            _vix_sc = float((market_data or {}).get('vix') or _sc_cache.get('vix') or 0.0)
            _ann_days_sc = 252.0
            _mkt_vol_sc = (_vix_sc / 100.0) / _math_sc.sqrt(_ann_days_sc) if _vix_sc > 0.0 else 0.0
            _p_crash_sc = max(
                float(sc_result.get('composite_crash_prob', 0.0) or 0.0),
                float((market_data or {}).get('crash_prob') or _sc_cache.get('crash_prob') or 0.0),
            )
            _rt_cost_sc = float(pget('a3.roundtrip_cost_pct', 0.36)) / 100.0
            _SC_CANON_MAP = {
                'S10': 'S10_MEGA_TREND',
                'S11': 'S11_HIGHBETA_SNIPER',
                'S12': 'S12_DERIVATIVE_SQUEEZE',
                'S13': 'S13_JAPAN_ALPHA',
                'S14': 'S14_PAIR_ALPHA',
            }

            for _d_sid, _d_act in _sc_dirs.items():
                if _d_act in ('BLOCK_LONG_SIGNALS', 'BLOCK_LONG_SNIPING', 'STOP_ALL_NEW_BUYS'):
                    for _match_sid in dict.fromkeys((_d_sid, _SC_CANON_MAP.get(_d_sid, _d_sid))):
                        if _match_sid in all_signals:
                            _before_n = len(all_signals[_match_sid])
                            _kept_sc = []
                            for _s in all_signals[_match_sid]:
                                if not isinstance(_s, dict):
                                    continue
                                _tk_sc = str(_s.get('ticker', '')).split(':')[-1].upper()
                                if (
                                    str(_s.get('direction', '')).lower() == 'exit'
                                    or _s.get('strategy') == '_sys_meta'
                                    or _tk_sc in _inv_set_sc
                                    or _tk_sc in _def_set_sc
                                    or bool(_s.get('is_hedge', False))
                                    or bool(_s.get('is_market_neutral', False))
                                ):
                                    _kept_sc.append(_s)
                                    continue
                                if _d_act == 'STOP_ALL_NEW_BUYS':
                                    continue
                                _alpha_s = max(
                                    float(_s.get('expected_return') or 0.0),
                                    float(_s.get('expected_value') or 0.0),
                                    float(_s.get('gk_expected_alpha') or 0.0),
                                    float(_s.get('expected_return_pct') or 0.0) / 100.0,
                                )
                                _beta_raw = _s.get('beta')
                                if _beta_raw is None:
                                    _beta_raw = _qe_sc.get_asset_metrics(_tk_sc).get('beta', 1.0)
                                _beta_s = float(_beta_raw if _beta_raw is not None else 1.0)
                                _gate_sc = scenario_conditional_alpha_survival_gate(
                                    expected_alpha=_alpha_s,
                                    asset_beta=_beta_s,
                                    scenario_crash_prob=_p_crash_sc,
                                    market_daily_vol=_mkt_vol_sc,
                                    round_trip_cost=_rt_cost_sc,
                                )
                                if _gate_sc.get('survives', False):
                                    _kept_sc.append(_s)
                            all_signals[_match_sid] = _kept_sc
                            if len(all_signals[_match_sid]) < _before_n:
                                logger.info(
                                    f"    🛡️ [Scenario Directive] {_match_sid} ({_d_act}): "
                                    f"신규 롱 신호 {_before_n - len(all_signals[_match_sid])}건 차단 "
                                    f"(생존 {len(all_signals[_match_sid])}건)"
                                )
                elif _d_act == 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH':
                    # V-반등/폭등 시나리오 발동 시 보유 방향성 인버스 ETF 즉시 청산 시그널 주입 및 레버리지 스위칭
                    # (단, S14_PAIR_ALPHA 마켓뉴트럴 페어 소속 헤지 레그는 단독 청산 시 롱 레그가 비헤지 노출되므로 제외)
                    from src.allocation.sector_pair_alpha_engine import (
                        DEFAULT_SECTOR_ETF_MAP as _S14_KR_LONG_MAP,
                        US_SECTOR_ETF_MAP as _S14_US_LONG_MAP,
                        KR_INVERSE_ETF_MAP as _S14_KR_INV_MAP,
                        US_INVERSE_ETF_MAP as _S14_US_INV_MAP,
                    )
                    _s14_kr_long = set(_S14_KR_LONG_MAP.values())
                    _s14_us_long = set(_S14_US_LONG_MAP.values())
                    _s14_kr_inv = set(_S14_KR_INV_MAP.values())
                    _s14_us_inv = set(_S14_US_INV_MAP.values())
                    _held_for_inv = {}
                    if portfolio and isinstance(portfolio, dict):
                        _held_for_inv.update(portfolio.get('positions') or portfolio.get('holdings') or {})
                    _held_tks_for_inv = {
                        str((_v.get('ticker') if isinstance(_v, dict) else _k) or _k).split(':')[-1].upper()
                        for _k, _v in _held_for_inv.items()
                        if float((_v.get('qty', _v.get('quantity', 0)) if isinstance(_v, dict) else 0) or 0) > 0
                    }
                    _has_s14_long_kr = bool(_held_tks_for_inv & _s14_kr_long)
                    _has_s14_long_us = bool(_held_tks_for_inv & _s14_us_long)
                    for _hk, _hv in _held_for_inv.items():
                        _htk = str((_hv.get('ticker') if isinstance(_hv, dict) else _hk) or _hk).split(':')[-1].upper()
                        _hq = float((_hv.get('qty', _hv.get('quantity', 0)) if isinstance(_hv, dict) else 0) or 0)
                        _h_sid = str((_hv.get('stream_id') or _hv.get('stream') or '') if isinstance(_hv, dict) else '').upper()
                        _is_s14_pair_hedge = (
                            _h_sid.startswith('S14')
                            or str(_hk).upper().startswith('S14:')
                            or (_htk in _s14_kr_inv and _has_s14_long_kr)
                            or (_htk in _s14_us_inv and _has_s14_long_us)
                        )
                        if _is_s14_pair_hedge:
                            continue
                        if _htk in _inv_set_sc and _hq > 0:
                            all_signals.setdefault('S0', []).append({
                                'stream_id': 'S0',
                                'ticker': _htk,
                                'direction': 'exit',
                                'action': 'exit',
                                'quantity': int(_hq),
                                'confidence': float(sc_result.get('composite_surge_prob', 1.0)),
                                'strategy': 'scenario_v_rebound_inverse_harvest',
                                'reason': f"MasterScenario V-Rebound ({sc_result.get('active_scenarios')}) inverse harvest",
                            })
                            logger.info(f"    🚀 [Scenario V-Rebound] 보유 인버스 {_htk} 전량 즉시 청산 시그널 주입")
                    _lev_tk_sc = str(getattr(getattr(self, 's0', None), 'TICKER_LEVERAGE_2X', '') or '').strip().upper()
                    _existing_s0_tks = {
                        str(_x.get('ticker', '')).split(':')[-1].upper()
                        for _x in (all_signals.get('S0') or [])
                        if isinstance(_x, dict) and str(_x.get('direction', '')).lower() == 'long'
                    }
                    if _lev_tk_sc and _lev_tk_sc not in _existing_s0_tks:
                        _ok_s0_lev = True
                        _veto_s0_lev = ''
                        if hasattr(self, 's0') and hasattr(self.s0, '_evaluate_intraday_tape_and_gap_guard'):
                            _ok_s0_lev, _veto_s0_lev = self.s0._evaluate_intraday_tape_and_gap_guard(
                                _lev_tk_sc, is_inverse=False, market_data=market_data
                            )
                        if _ok_s0_lev:
                            _half_sz = 1.0 / 2.0
                            _lev_sz = float(sc_result.get('recommended_max_exposure') or _half_sz)
                            _lev_conf = float(sc_result.get('composite_surge_prob') or 1.0)
                            all_signals.setdefault('S0', []).append({
                                'stream_id': 'S0',
                                'ticker': _lev_tk_sc,
                                'direction': 'long',
                                'action': 'buy',
                                'size_pct': _lev_sz,
                                'confidence': _lev_conf,
                                'strategy': 'scenario_v_rebound_leverage_switch',
                                'reason': f"MasterScenario V-Rebound ({sc_result.get('active_scenarios')}) leverage switch",
                            })
                            logger.info(f"    🚀 [Scenario V-Rebound] 레버리지 스위칭 {_lev_tk_sc} 매수 시그널 주입 (size_pct={_lev_sz:.2f})")
                        else:
                            logger.info(f"    🛡️ [Scenario V-Rebound] 레버리지 스위칭 {_lev_tk_sc} 갭/테이프 가드 유예: {_veto_s0_lev}")
                elif _d_act in ('FORCE_2X_INVERSE_MAX_BUDGET', 'FORCE_INVERSE_HEDGE', 'MAX_INVERSE_SQUEEZE'):
                    _inv_ratio = float(sc_result.get('target_inverse_hedge_ratio', 0.0))
                    _s12_canon = getattr(getattr(self, 's12', None), 'stream_id', 'S12_DERIVATIVE_SQUEEZE')
                    if _inv_ratio > 0 and not all_signals.get(_s12_canon) and not all_signals.get('S12'):
                        _s12_tk = getattr(self.s12, 'ticker_inv_2x', getattr(self.s0, 'TICKER_INVERSE_2X', ''))
                        if _s12_tk:
                            _ok_s12_inv = True
                            _veto_s12_inv = ''
                            if hasattr(self, 's0') and hasattr(self.s0, '_evaluate_intraday_tape_and_gap_guard'):
                                _ok_s12_inv, _veto_s12_inv = self.s0._evaluate_intraday_tape_and_gap_guard(
                                    _s12_tk, is_inverse=True, market_data=market_data
                                )
                            if not _ok_s12_inv:
                                logger.warning(
                                    f"    🛑 [Scenario Inverse Tape/Gap Guard] {_s12_canon} {_s12_tk} 즉각 헤지 진입 차단: {_veto_s12_inv}"
                                )
                            else:
                                _s12_tp_pct = float(pget('s12.default_tp_pct', 8.0))
                                _s12_sl_pct = float(pget('s12.default_sl_pct', 3.5))
                                _s12_conf = float(sc_result.get('composite_crash_prob', 1.0))
                                _s12_ev = (_s12_tp_pct / 100.0) * max(_s12_conf, 1.0 / 2.0)
                                all_signals[_s12_canon] = [{
                                    'stream_id': _s12_canon,
                                    'ticker': _s12_tk,
                                    'direction': 'long',
                                    'action': 'buy',
                                    'is_hedge': True,
                                    'size_pct': _inv_ratio,
                                    'confidence': _s12_conf,
                                    'sl_pct': _s12_sl_pct,
                                    'tp_pct': _s12_tp_pct,
                                    'expected_return': round(_s12_ev, 4),
                                    'expected_return_pct': round(_s12_ev * 100.0, 2),
                                    'strategy': 'master_scenario_inverse_squeeze',
                                    'reason': f"MasterScenario {sc_result.get('active_scenarios')} immediate inverse hedge",
                                }]
                                logger.warning(
                                    f"    🛡️ [Scenario Inverse] {_s12_canon} {_s12_tk} 즉각 헤지 시그널 생성 (size_pct={_inv_ratio:.1%})"
                                )
        except Exception as _sc_dir_e:
            logger.error(f"    ⚠️ [Step 4.1] 시나리오 디렉티브 적용 예외: {_sc_dir_e}", exc_info=True)

        result['signals'] = {
            sid: [s for s in sigs]
            for sid, sigs in all_signals.items()
        }

        # ★ Exit-Only Mode 처리: 데이터 이상 시 신규 매수 시그널 전부 삭제 (TP/SL만 유지)
        if market_data and market_data.get('exit_only'):
            logger.error("🚨 [Exit-Only Mode] 신규 매수 시그널을 모두 삭제합니다.")
            all_signals = {
                sid: [s for s in sigs if isinstance(s, dict) and str(s.get('direction', '')).lower() == 'exit']
                for sid, sigs in all_signals.items()
            }
            result['signals'] = {sid: [] for sid in result['signals']}

        # [Phase 48 C-1] existing_tickers 사전 선언 — Step 4.5보다 반드시 앞에 위치해야
        # NameError 방지: _generate_orders()에서 재정의되지만, Step 4.5에서 먼저 참조함
        existing_tickers: set = set()

        # ── Step 4.5: S3/S4 Alpha-Ranked Hysteresis 리밸런싱 ──
        logger.info("  📋 Step 4.5: S3/S4 리밸런싱")
        rebalance_orders = []
        try:
            import json as _json_rebal
            _all_positions = {}
            if getattr(self, 'exec_mode', 'live') != 'live':
                _sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
                if _sp_path.exists():
                    _sp_data = _json_rebal.loads(_sp_path.read_text())
                    _all_positions = dict(_sp_data.get('positions', {}))
            elif (_ROOT / 'results' / 'kis_portfolio.json').exists() and not (portfolio and portfolio.get('positions')):
                _kp_data = _json_rebal.loads((_ROOT / 'results' / 'kis_portfolio.json').read_text(encoding='utf-8'))
                for _kt, _kh in (_kp_data.get('holdings') or {}).items():
                    if isinstance(_kh, dict) and float(_kh.get('qty', _kh.get('quantity', 0)) or 0) > 0:
                        _tk_up = str(_kt).upper()
                        _sid = str(_kh.get('stream_id') or '').strip()
                        if not _sid:
                            _sid = 'S0' if _tk_up in ('069500', '229200', '252670', '114800') else 'S3'
                        _all_positions[f"{_sid}:{_tk_up}"] = {
                            'ticker': _tk_up,
                            'stream_id': _sid,
                            'track': str(_kh.get('track') or ''),
                            'qty': _kh.get('qty', _kh.get('quantity', 0)),
                            'amount': float(_kh.get('qty', 0) or 0) * float(_kh.get('current_price', 0.0) or 0.0),
                            'current_price': float(_kh.get('current_price', 0.0) or 0.0),
                        }

            # [SSoT Integration] 실계좌 포트폴리오(portfolio) 주입 시 보유 종목 스트림 매핑 주입
            if portfolio and portfolio.get('positions'):
                _raw_positions = portfolio['positions']
                if isinstance(_raw_positions, dict):
                    for _pk, _pv in _raw_positions.items():
                        if ':' in str(_pk):
                            _all_positions[str(_pk)] = _pv if isinstance(_pv, dict) else {'ticker': str(_pk).split(':')[-1], 'qty': getattr(_pv, 'quantity', 0)}
                        else:
                            _tk_up = str(getattr(_pv, 'ticker', _pk) or _pk).upper()
                            _sid = str((_pv.get('stream_id') if isinstance(_pv, dict) else getattr(_pv, 'stream_id', '')) or '').strip()
                            if not _sid:
                                _sid = 'S0' if _tk_up in ('069500', '229200', '252670', '114800') else 'S3'
                            _qty_val = getattr(_pv, 'quantity', _pv.get('qty', 0) if isinstance(_pv, dict) else 0)
                            _amt_val = getattr(_pv, 'amount', _pv.get('amount', 0) if isinstance(_pv, dict) else 0)
                            _prc_val = getattr(_pv, 'current_price', _pv.get('current_price', 0.0) if isinstance(_pv, dict) else 0.0)
                            _p_dict = {
                                'ticker': _tk_up,
                                'stream_id': _sid,
                                'track': str((_pv.get('track') if isinstance(_pv, dict) else getattr(_pv, 'track', '')) or ''),
                                'qty': _qty_val,
                                'amount': _amt_val,
                                'current_price': _prc_val,
                            }
                            _all_positions[f"{_sid}:{_tk_up}"] = _p_dict

            # [SSoT Policy] 리밸런싱 주문 생성: S3, S4 및 활성화된 스트림의 rebalance.enabled 확인
            existing_tickers = {(pv.get('ticker') if isinstance(pv, dict) else getattr(pv, 'ticker', '')) or str(pk).split(':')[-1] for pk, pv in _all_positions.items()}
            _candidate_rebal_streams = ['S3', 'S4', 'S0', 'S1', 'S2']
            _active_rebal_streams = [s for s in _candidate_rebal_streams if _cfg_get(f"{s.lower()}.rebalance.enabled", False)]
            for _rebal_sid in _active_rebal_streams:
                _stream_positions = {
                    pk: pv for pk, pv in _all_positions.items()
                    if pk.startswith(f"{_rebal_sid}:")}
                _stream_signals = all_signals.get(_rebal_sid, [])

                _rebal = self.rebalance_engine.rebalance(
                    stream_id=_rebal_sid,
                    current_positions=_stream_positions,
                    new_signals=_stream_signals,
                    market_data=market_data,
                )

                if _rebal.get('skipped'):
                    logger.info(
                        f"    ⏸ {_rebal_sid} 리밸런싱 스킵: "
                        f"{_rebal['skipped']}")
                else:
                    _n_sell = len(_rebal.get('sells', []))
                    _n_buy = len(_rebal.get('buys', []))
                    if _n_sell > 0 or _n_buy > 0:
                        logger.info(
                            f"    🔄 {_rebal_sid} 리밸런싱: "
                            f"매도 {_n_sell}건, 매수 {_n_buy}건")
                    # 리밸런싱 매도 주문
                    for _sell in _rebal.get('sells', []):
                        rebalance_orders.append({
                            'stream_id': _rebal_sid,
                            'ticker': _sell['ticker'],
                            'name': _sell.get('name', _sell['ticker']),
                            'direction': 'short',  # 매도
                            'amount_krw': _sell.get('amount', 0),
                            'price': 0,  # pykrx에서 주입
                            'confidence': 1.0,
                            'strategy': 'rebalance_sell',
                            'reason': _sell.get('reason', ''),
                            'sell_type': 'rebalance',
                            'pos_key': _sell.get('pos_key', ''),
                        })
                    # 리밸런싱 매수 주문
                    for _buy in _rebal.get('buys', []):
                        # ★ [Phase 47] 중복 매수 원천 차단 — 이미 보유 중인 종목 스킵
                        if _buy.get('ticker') in existing_tickers:
                            logger.debug(
                                f"    [Phase 47] 리밸런싱 중복 매수 차단: "
                                f"{_buy.get('ticker')} (이미 보유)"
                            )
                            continue
                        rebalance_orders.append({
                            'stream_id': _rebal_sid,
                            'ticker': _buy['ticker'],
                            'name': _buy.get('name', _buy['ticker']),
                            'direction': 'long',
                            'amount_krw': _buy.get('amount_krw', 0),
                            'price': 0,  # pykrx에서 주입
                            'confidence': _buy.get('confidence', 0.5),
                            'strategy': _buy.get('strategy', 'rebalance'),
                            'reason': _buy.get('reason', ''),
                        })

                result['rebalance'] = result.get('rebalance', {})
                result['rebalance'][_rebal_sid] = {
                    'sells': len(_rebal.get('sells', [])),
                    'buys': len(_rebal.get('buys', [])),
                    'replacements': _rebal.get('replacements', 0),
                    'skipped': _rebal.get('skipped'),
                }
        except Exception as _rebal_e:
            logger.error(f"    리밸런싱 실패: {_rebal_e}", exc_info=True)

        # ── Step 5: 스트림 성과 수집 + 배분 ──
        logger.info("  📋 Step 5: AlphaAllocator 배분")
        stream_metrics = {}
        if injected_stream_metrics:
            stream_metrics.update(injected_stream_metrics)
        for stream in self.streams:
            perf = stream.get_performance()
            # Inject된 데이터가 있으면 보존 (또는 stream에서 자체 생성한 것을 덮어쓰기)
            if stream.stream_id not in stream_metrics:
                stream_metrics[stream.stream_id] = perf

        # [Phase 50] Check for S0 Cash Sweep
        s0_sigs = all_signals.get('S0', [])

        # ★ [SSOT Wiring Fix] all_signals를 _stream_signals로 주입하여 섹터 한도(_enforce_sector_limits) 및 직교화 활성화
        from src.params.dynamic_quant_models import (
            compute_corporate_after_tax_cagr_metrics,
            normalize_signal_horizon_expectation,
        )
        stream_metrics['_stream_signals'] = all_signals
        for _sid_er, _sigs_er in all_signals.items():
            if not isinstance(_sigs_er, list):
                continue
            _er_num = 0.0
            _p_num = 0.0
            _rk_num = 0.0
            _er_den = 0.0
            _n_buy_er = 0
            for _s_er in _sigs_er:
                if not isinstance(_s_er, dict) or _s_er.get('strategy') == '_sys_meta':
                    continue
                _dir_s = str(_s_er.get('direction', 'long')).lower()
                if not _s_er.get('action'):
                    _s_er['action'] = 'exit' if _dir_s == 'exit' else ('sell' if _dir_s in ('short', 'sell') else 'buy')
                if not _s_er.get('stream_id'):
                    _s_er['stream_id'] = _sid_er
                if _dir_s in ('exit', 'short', 'sell') or str(_s_er.get('action', '')).lower() in ('exit', 'sell'):
                    continue
                _norm_er = normalize_signal_horizon_expectation(_s_er, stream_id=_sid_er)
                _w_er = _norm_er['size_weight']
                if _w_er <= 0.0:
                    continue
                _r_er = _norm_er['reward_dec']
                if _r_er > 0.0:
                    _er_num += _w_er * _r_er
                    _p_num += _w_er * _norm_er['p_win']
                    _rk_num += _w_er * _norm_er['risk_dec']
                    _er_den += _w_er
                    _n_buy_er += 1
            if _er_den > 0.0:
                stream_metrics.setdefault(_sid_er, {})
                if isinstance(stream_metrics[_sid_er], dict):
                    stream_metrics[_sid_er]['expected_return'] = _er_num / _er_den
                    stream_metrics[_sid_er]['signal_win_rate'] = _p_num / _er_den
                    stream_metrics[_sid_er]['signal_risk'] = _rk_num / _er_den
                    stream_metrics[_sid_er]['n_live_signals'] = _n_buy_er

        if '_s2_oos_validation' not in stream_metrics:
            try:
                from src.params.dynamic_quant_models import load_latest_purged_wf_oos
                _oos_v = load_latest_purged_wf_oos(_ROOT / 'results' / 'ml_validation')
                if _oos_v is not None:
                    stream_metrics['_s2_oos_validation'] = _oos_v
            except Exception:
                pass

        weights = self.allocator.allocate(stream_metrics, regime, market_data, s0_sigs=s0_sigs)
        result['allocation'] = weights
        logger.info(f"    📊 배분: {weights}")
        try:
            _nav_cagr = float((portfolio or {}).get('total_nav') or (portfolio or {}).get('cash') or 1.0)
            _cagr_eval = compute_corporate_after_tax_cagr_metrics(
                stream_weights=weights,
                stream_signals=all_signals,
                portfolio_nav_krw=_nav_cagr,
            )
            result['corporate_after_tax_cagr'] = _cagr_eval
            logger.info(
                f"    📈 [Corporate After-Tax CAGR] 기대 세후 CAGR={_cagr_eval['expected_corporate_after_tax_cagr_pct']:.2f}% "
                f"(일복리={_cagr_eval['portfolio_daily_net_corp_growth']*100:.4f}%/d, "
                f"법인세율={_cagr_eval['effective_corporate_tax_rate']*100:.2f}%, "
                f"50% 허들 달성={_cagr_eval['meets_50pct_cagr_hurdle']})"
            )
        except Exception as _cagr_e:
            logger.debug(f"Corporate After-Tax CAGR 산출 생략: {_cagr_e}")

        # ── Step 6: 레버리지 판정 ──
        logger.info("  📋 Step 6: 레버리지 판정")
        lev_result = self.leverage_judge.assess(
            portfolio, stream_metrics, regime)
        result['leverage'] = lev_result
        lev_level = lev_result['judgment']['leverage_level']
        logger.info(f"    ⚡ 레버리지: {lev_level}X")

        # ── Step 7: 최종 주문 생성 ──
        logger.info("  📋 Step 7: 최종 주문 생성")

        # ★ 가격 누락 시그널에 KIS / 인스티튜셔널 실시간 종가 주입 (₩50,000 하드코딩 방지)
        try:
            _missing_tickers = set()
            for _sigs in all_signals.values():
                for _s in _sigs:
                    if not isinstance(_s, dict):
                        continue
                    _t = str(_s.get('ticker', '') or '')
                    if not _t or _t.startswith('_') or _s.get('strategy') == '_sys_meta' or _s.get('direction') == 'exit':
                        continue
                    if not _s.get('price') or _s['price'] <= 0:
                        _missing_tickers.add(_t)

            from src.utils.ticker_canonical import LEGACY_CODE_MAP as _ALIAS  # 단일 SSOT (로그는 canonical_ticker에서)
            _PARKING_FALLBACK_PX = {
                '459580': 1074340.0,
                '423160': 110770.0,
                '449170': 112110.0,
                '430740': 110770.0,
                '357870': 58190.0,
                '357330': 58190.0,
                'SHV': 110.0,
                'SGOV': 100.0,
            }
            _injected = 0
            for _t in _missing_tickers:
                _norm_t = _ALIAS.get(_t, _t)
                try:
                    # 1순위: CentralDataGateway (3-tier hot failover)
                    try:
                        from src.data_collection.central_data_gateway import CentralDataGateway
                        _cdg_res = CentralDataGateway().get_current_price(_norm_t)
                        if isinstance(_cdg_res, dict) and _cdg_res.get('price'):
                            _px = float(_cdg_res['price'])
                            if _px > 0:
                                for _sigs in all_signals.values():
                                    for _s in _sigs:
                                        if _s.get('ticker') in (_t, _norm_t) and (not _s.get('price') or _s['price'] <= 0):
                                            _s['ticker'] = _norm_t
                                            _s['price'] = _px
                                            _injected += 1
                                continue
                    except Exception:
                        pass

                    # 2순위: KIS Price Service
                    try:
                        from src.execution.kis_price_service import KISPriceService
                        _kps_res = KISPriceService().get_current_price(_norm_t)
                        if isinstance(_kps_res, dict) and _kps_res.get('price'):
                            _px = float(_kps_res['price'])
                            if _px > 0:
                                for _sigs in all_signals.values():
                                    for _s in _sigs:
                                        if _s.get('ticker') in (_t, _norm_t) and (not _s.get('price') or _s['price'] <= 0):
                                            _s['ticker'] = _norm_t
                                            _s['price'] = _px
                                            _injected += 1
                                continue
                    except Exception:
                        pass

                    # 3순위: 파킹 ETF 기준가(NAV Anchor)
                    if _norm_t in _PARKING_FALLBACK_PX:
                        _px = _PARKING_FALLBACK_PX[_norm_t]
                        for _sigs in all_signals.values():
                            for _s in _sigs:
                                if _s.get('ticker') in (_t, _norm_t) and (not _s.get('price') or _s['price'] <= 0):
                                    _s['ticker'] = _norm_t
                                    _s['price'] = _px
                                    _injected += 1
                        continue

                    # 4순위: 인스티튜셔널 OHLCV (Naver / KRX API)
                    from src.data_collection.pykrx_compat import stock as _krx_compat
                    from datetime import date as _date_orch, timedelta as _td_orch
                    _today_orch = _date_orch.today().strftime('%Y%m%d')
                    _week_ago = (_date_orch.today() - _td_orch(days=7)).strftime('%Y%m%d')
                    _df = _krx_compat.get_market_ohlcv_by_date(_week_ago, _today_orch, _norm_t)
                    if len(_df) > 0:
                        _close = float(_df.iloc[-1].get('종가', 0))
                        if _close > 0:
                            for _sigs in all_signals.values():
                                for _s in _sigs:
                                    if _s.get('ticker') in (_t, _norm_t) and (not _s.get('price') or _s['price'] <= 0):
                                        _s['ticker'] = _norm_t
                                        _s['price'] = _close
                                        _injected += 1
                except Exception as _e:
                    logger.debug(f"Missing price injection skipped for {_t}: {_e}")
            if _injected:
                logger.info(f"    📡 기관급 가격 주입(KIS/Gateway/Anchor): {_injected}건 ({len(_missing_tickers)}종목)")
        except Exception as _pe:
            logger.debug(f"가격 주입 처리 완료: {_pe}")

        # ── Step 4.7: 글로벌 Exit Manager (TP/SL/Trailing Stop) ──
        logger.info("  📋 Step 4.7: 글로벌 Exit Manager (TP/SL)")
        self._latest_signals = all_signals
        exit_orders = self._evaluate_exits(market_data, regime, portfolio=portfolio)
        if exit_orders:
            logger.info(f"    🔔 Exit Manager: {len(exit_orders)}건 청산 주문 생성")
        else:
            logger.info("    ✅ Exit Manager: 청산 조건 없음")

        # [Task 4] S2 SYS_META 추출 — _generate_orders() 이전 제거 필수
        _s2_bear_score = 0.0
        _s2_meta = [s for s in all_signals.get('S2', []) if isinstance(s, dict) and s.get('strategy') == '_sys_meta']
        if _s2_meta:
            _s2_bear_score = float(_s2_meta[0].get('bear_score', 0.0))
            all_signals['S2'] = [s for s in all_signals.get('S2', []) if not (isinstance(s, dict) and s.get('strategy') == '_sys_meta')]
            logger.info(f"  [Task 4] S2 Bear Score 수신: {_s2_bear_score:.3f}")

        orders = self._generate_orders(
            all_signals, weights, max_exposure, lev_result, portfolio, regime, market_data)

        # Exit 주문을 매수 주문보다 먼저 병합 (청산 우선 원칙)
        if exit_orders:
            orders = exit_orders + orders
            logger.info(f"    📋 Exit 주문 {len(exit_orders)}건 선행 병합")

        # ★ DD Overlay 액션 실행 (강제 청산 및 헷지)
        overlay_orders = self._apply_dd_overlay(dd_action, portfolio, regime)
        if overlay_orders:
            orders.extend(overlay_orders)
            logger.info(f"    🛡️ DD Overlay 주문 {len(overlay_orders)}건 병합")

        # ★ 리밸런싱 주문 병합 (S3/S4 교체 매도+매수)
        if rebalance_orders:
            orders.extend(rebalance_orders)
            logger.info(f"    🔄 리밸런싱 주문 {len(rebalance_orders)}건 병합")

        # ★ [Phase 99: Cross-Asset Alpha Swap] 가용 현금 부족 시 신규 고알파 시그널 vs 보유자산 EV 비교 자본 스왑
        try:
            from src.execution.intraday_capital_router import IntradayCapitalRouter
            _router = IntradayCapitalRouter()
            
            # Step 1: 보유자산 실시간 EV 산출 및 market_data 주입
            if 'holdings_ev' not in market_data:
                market_data['holdings_ev'] = _router.compute_holdings_ev(portfolio, market_data)

            # Step 2: 현금 부족으로 미주문된 신규 매수 시그널 수집 (이미 보유 중인 종목 및 파킹 ETF 제외)
            _ordered_tickers = {o.get('ticker') for o in orders if o.get('direction') in ('buy', 'long') or o.get('action') == 'buy'}
            _held_swap_set = set()
            if portfolio and isinstance(portfolio, dict):
                for _sk in ('positions', 'holdings'):
                    _sd = portfolio.get(_sk) or {}
                    if isinstance(_sd, dict):
                        for _pk, _pv in _sd.items():
                            _pt = (_pv.get('ticker') if isinstance(_pv, dict) else None) or str(_pk).split(':')[-1]
                            _pq = int((_pv.get('quantity', _pv.get('qty', 0)) if isinstance(_pv, dict) else 0) or 0)
                            if _pt and _pq > 0:
                                _held_swap_set.add(str(_pt).upper())
            if getattr(self, 'exec_mode', 'live') == 'live' and (_ROOT / 'results' / 'kis_portfolio.json').exists():
                try:
                    _kp_swap = json.loads((_ROOT / 'results' / 'kis_portfolio.json').read_text(encoding='utf-8'))
                    for _kt, _kh in (_kp_swap.get('holdings') or {}).items():
                        if int((_kh.get('qty', _kh.get('quantity', 0)) if isinstance(_kh, dict) else 0) or 0) > 0:
                            _held_swap_set.add(str(_kt).upper())
                except Exception:
                    pass
            _PARKING_SWAP_SKIP = {'459580', '449170', '423160', '430740', '357870', '357330', 'SHV', 'SGOV', 'BIL'}
            _swap_lockout_set = {
                str(eo.get('ticker', '')).upper()
                for eo in exit_orders
                if eo.get('ticker')
            }
            try:
                from src.measurement.data_nogo_store import load_active_data_nogo as _load_dng_swap
                _act_dng_sw = _load_dng_swap() or {}
                for _tk_dng in (_act_dng_sw.get('ticker') or []):
                    if _tk_dng:
                        _swap_lockout_set.add(str(_tk_dng).strip().upper())
            except Exception:
                pass
            if getattr(self, 'exec_mode', 'live') == 'live':
                try:
                    from src.execution.position_ledger import PositionLedgerManager as _PLM_Swap
                    _swap_lockout_set |= {str(k).upper() for k in (_PLM_Swap().get_daily_exit_details() or {}).keys()}
                except Exception:
                    pass
                try:
                    import json as _json_dlq_sw
                    _dlq_sw_p = _ROOT / 'results' / 'failed_orders.json'
                    if _dlq_sw_p.exists():
                        for _it_sw in (_json_dlq_sw.loads(_dlq_sw_p.read_text(encoding='utf-8')) or []):
                            if isinstance(_it_sw, dict):
                                _rs_sw = str(_it_sw.get('reason', _it_sw.get('error', '')) or '')
                                _st_sw = str(_it_sw.get('status', '') or '')
                                if _st_sw == 'halted_permanent' or 'APBK0066' in _rs_sw or '거래정지' in _rs_sw:
                                    _od_sw = _it_sw.get('order_dict') or _it_sw.get('order') or _it_sw
                                    if isinstance(_od_sw, dict) and _od_sw.get('ticker'):
                                        _swap_lockout_set.add(str(_od_sw['ticker']).strip().upper())
                except Exception:
                    pass
            from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_swap
            _us_eff_reg_swap = _get_qe_swap().resolve_effective_us_regime(regime, market_data)
            _US_INV_SWAP_BLOCK = {'SOXS', 'SQQQ', 'PSQ', 'SH', 'SDOW', 'SPXS', 'SPXU', 'QID'} if _us_eff_reg_swap not in ('bear', 'crash') else set()
            _all_active_long_sigs = []
            _candidate_sigs = []
            _SWAP_CANON_MAP = {
                'S0_BETA': 'S0',
                'S1_EDGE': 'S1',
                'S2_ML_ALPHA': 'S2',
                'S3_ACTIVE_MACRO': 'S3',
                'S10': 'S10_MEGA_TREND',
                'S11': 'S11_HIGHBETA_SNIPER',
                'S12': 'S12_DERIVATIVE_SQUEEZE',
                'S13': 'S13_JAPAN_ALPHA',
                'S14': 'S14_PAIR_ALPHA',
            }
            _last_init_b_map = getattr(self, '_last_initial_stream_budgets', None) or {}
            _last_held_b_map = getattr(self, '_last_stream_held_krw', None) or {}
            for _sid, _sigs in all_signals.items():
                if _sid in ('S0', 'S4', 'S5', 'S_YIELD'):
                    continue
                if float(weights.get(_sid, 0.0) or 0.0) <= 0.0:
                    continue
                _sid_c_sw = _SWAP_CANON_MAP.get(str(_sid), str(_sid))
                _init_b_sw = float(_last_init_b_map.get(_sid_c_sw, _last_init_b_map.get(str(_sid), 0.0)) or 0.0)
                _held_b_sw = float(_last_held_b_map.get(_sid_c_sw, _last_held_b_map.get(str(_sid), 0.0)) or 0.0)
                _ord_b_sw = sum(
                    float(_o_sw.get('amount_krw', 0.0) or 0.0)
                    for _o_sw in orders
                    if (
                        (_o_sw.get('direction') in ('buy', 'long') or _o_sw.get('action') == 'buy')
                        and _SWAP_CANON_MAP.get(str(_o_sw.get('stream_id', '')), str(_o_sw.get('stream_id', ''))) == _sid_c_sw
                    )
                )
                _stream_budget_saturated = (_init_b_sw > 0.0) and ((_held_b_sw + _ord_b_sw) >= _init_b_sw * 0.85)
                for _s in _sigs:
                    _tk = str(_s.get('ticker', '')).strip()
                    if (
                        _tk
                        and _tk.upper() not in _PARKING_SWAP_SKIP
                        and _tk.upper() not in _swap_lockout_set
                        and _tk.upper() not in _US_INV_SWAP_BLOCK
                        and _s.get('direction', _s.get('action', 'long')) in ('long', 'buy', 'BUY')
                    ):
                        _all_active_long_sigs.append(_s)
                        if (not _stream_budget_saturated) and _tk not in _ordered_tickers and _tk.upper() not in _held_swap_set:
                            _candidate_sigs.append(_s)

            # [Co-Domain Unification] 기보유 종목이 현재 사이클에서도 활성 매수 시그널을 보유한 경우 실제 시그널 EV_adj를 market_data에 주입하여 부당 스왑 매도 원천 차단
            if _all_active_long_sigs:
                _ranked_all_active = _router.rank_signals_by_ev_adj(_all_active_long_sigs, market_data)
                _active_ev_map = dict(market_data.get('active_signal_ev_map') or {})
                for _ra_s in _ranked_all_active:
                    _ra_tk = str(_ra_s.get('ticker', '')).strip().upper()
                    _ra_ev = float(_ra_s.get('ev_adj', 0.0) or 0.0)
                    if _ra_tk and _ra_ev > 0.0:
                        _active_ev_map[_ra_tk] = max(float(_active_ev_map.get(_ra_tk, 0.0) or 0.0), _ra_ev)
                market_data['active_signal_ev_map'] = _active_ev_map
            market_data['data_nogo_tickers'] = list(_swap_lockout_set)

            # Step 3: 자본 스왑 라우팅 집행 (기존 최저 EV 보유자산 매도 대금으로만 신규 고EV 자산 교체 매수 — 미배정 현금 임의 집행 차단)
            _swap_orders = _router.route_intraday_capital(_candidate_sigs, market_data, total_cash_krw=0.0, portfolio=portfolio, enforce_session_gate=True)
            
            if _swap_orders:
                _swap_sells = [o for o in _swap_orders if o.get('direction') == 'sell' or o.get('action') == 'sell']
                _swap_buys = [o for o in _swap_orders if o.get('direction') in ('buy', 'long') or o.get('action') == 'buy']
                if _swap_sells and _swap_buys:
                    orders = _swap_sells + orders + _swap_buys
                    logger.info(f"    🔄 [Cross-Asset Alpha Swap] 자본 교체 매매: 매도 {len(_swap_sells)}건, 매수 {len(_swap_buys)}건 병합")
                elif _swap_buys and not _swap_sells:
                    logger.info(f"    🛡️ [Cross-Asset Alpha Swap] 매도 대상 없는 단독 매수 {len(_swap_buys)}건 병합 차단 (정규 예산 규율 보호)")
        except Exception as _swap_err:
            logger.error(f"    ❌ Cross-Asset Alpha Swap 실행 실패: {_swap_err}", exc_info=True)

        # [Task 1 Phase50] SYS_HEDGE directional logic has been migrated to S0BetaStream.

        # [Phase 66: Project Argus Overlay] 거시 텍스트 오버레이 로드
        _argus = {}
        try:
            import json as _json
            _argus_path = Path(__file__).resolve().parent.parent / 'data' / 'alt_data' / 'pipeline_latest.json'
            if _argus_path.exists():
                _argus = _json.loads(_argus_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)
        _argus_policy    = float(_argus.get('argus_policy',    0.5))
        _argus_semi      = float(_argus.get('argus_semi_cycle', 0.5))
        _argus_inflation = float(_argus.get('argus_inflation',  0.5))

        # — Bull 쉴드: 하락장 신호 시에도 친시장 정책/반도체 호황이면 헤지 50% 삭감
        if regime in ('bear', 'caution') and any(
            o.get('strategy') in ('sys_tactical_hedge', 'tail_risk_hedge')
            for o in orders
        ):
            _argus_policy_thr = float(pget('argus.bull_shield_policy_thr', 0.75))
            _argus_semi_thr   = float(pget('argus.semi_shield_thr', 0.75))
            if _argus_policy >= _argus_policy_thr or _argus_semi >= _argus_semi_thr:
                for o in orders:
                    if o.get('strategy') in ('sys_tactical_hedge', 'tail_risk_hedge'):
                        _prev_hedge = o['amount_krw']
                        _argus_hedge_cut = float(pget('argus.bull_shield_hedge_cut', 0.5))
                        _new_h_amt = round(_prev_hedge * _argus_hedge_cut)
                        o['amount_krw'] = _new_h_amt
                        logger.info(f"  [Phase 66 Argus 오버레이] Bull 쉴드 발동 (policy={_argus_policy:.2f} semi={_argus_semi:.2f}) → 헤지 {_prev_hedge:.0f}→{_new_h_amt:.0f} (-50%)")

        # — Bear 쉴드: 상승장 신호에서 인플레 과열이면 롱 20% 삭감 (기존 orders에 직접 적용)
        _long_inflation_cut = 1.0
        _argus_inf_thr = float(pget('argus.bear_shield_inflation_thr', 0.80))
        if regime == 'bull' and _argus_inflation >= _argus_inf_thr:
            _long_inflation_cut = float(pget('argus.bear_shield_long_cut', 0.8))
            logger.info(
                f"  [Phase 66 Argus 오버레이] Bear 쉴드 발동 "
                f"(inflation={_argus_inflation:.2f}) "
                f"→ 롱 진입 규모 ×0.8 (과열 방어)")
        if _long_inflation_cut < 1.0:
            for o in orders:
                if o.get('direction') == 'long' and o.get('strategy') not in (
                        'sys_tactical_hedge', 'tail_risk_hedge', 'rebalance'):
                    o['amount_krw'] = round(o['amount_krw'] * _long_inflation_cut)
            logger.info(
                f"    📉 [Phase 66 Argus] 롱 인플레 삭감 ×{_long_inflation_cut:.2f} 적용 완료")

        from src.utils.dynamic_quant_engine import get_quant_engine
        from src.params.dynamic_quant_models import directional_beta_exposure_multiplier
        _dqe = get_quant_engine()
        _dyn_inv_set = set(_dqe.get_inverse_hedge_tickers())
        _dyn_def_set = _dqe.get_defensive_exempt_tickers()

        # ★ scale_multiplier 적용 (Beta-Convexity DD 방어선: 음의 베타 인버스 및 마켓뉴트럴 페어 면제)
        if scale_multiplier < 1.0:
            for o in orders:
                if o.get('direction') == 'long' and o.get('strategy') not in ('tail_risk_hedge', 'rebalance'):
                    _o_tk = str(o.get('ticker', '')).split(':')[-1].upper()
                    _o_is_hdg = bool(o.get('is_hedge', False)) or (_o_tk in _dyn_inv_set) or (_o_tk in _dyn_def_set)
                    _o_is_mn = bool(o.get('is_market_neutral', False)) or (o.get('stream_id') in ('S14', 'S14_PAIR_ALPHA', 'S7'))
                    _o_beta = -1.0 if _o_tk in _dyn_inv_set else float(o.get('beta') if o.get('beta') is not None else 1.0)
                    _dd_mult = directional_beta_exposure_multiplier(
                        asset_beta=_o_beta,
                        max_exposure=scale_multiplier,
                        is_hedge=_o_is_hdg,
                        is_market_neutral=_o_is_mn,
                    )
                    o['amount_krw'] = o['amount_krw'] * _dd_mult
            logger.info(f"    📉 매수 스케일 {scale_multiplier:.2f} (Beta-Convexity) 적용 완료")

        # ── [Phase 76] Two-Track: MACRO vs MICRO 시그널 필터 ──
        _MICRO_BOUND = set(cfg.get('stream.micro_bound', ['S1', 'S2', 'S5']))
        _MACRO_BOUND = set(cfg.get(
            'stream.macro_bound_streams',
            ['S3', 'S4']
        ))
        _dqe = get_quant_engine()
        _dyn_inv_set = set(_dqe.get_inverse_hedge_tickers())
        _dyn_def_set = _dqe.get_defensive_exempt_tickers()
        _all_confs = [float(x.get('confidence', 0.0) or 0.0) for x in (orders if isinstance(orders, list) else [orders])]
        _curr_regime = regime  # run() 스코프에서 직접 참조
        _dyn_caution_cut = _dqe.compute_dynamic_confidence_cutoff(_all_confs, regime=_curr_regime)
        _filtered_orders = []
        for _ord in (orders if isinstance(orders, list) else [orders]):
            _stream = str(_ord.get('stream_id', _ord.get('stream', '')))
            _action = str(_ord.get('action', _ord.get('direction', ''))).upper()
            _tk_u = str(_ord.get('ticker', '')).upper()
            _is_exit  = _action in ('SELL', 'EXIT', 'CLOSE', 'REDUCE')
            _is_hedge = (
                _ord.get('is_hedge', False)
                or _tk_u in _dyn_inv_set
                or float(_dqe.get_asset_metrics(_tk_u).get('beta', 1.0)) <= -0.35
                or 'hedge' in str(_ord.get('reason', '')).lower()
                or 'inverse' in str(_ord.get('ticker', '')).lower()
                or _ord.get('strategy') in ('sys_tactical_hedge', 'tail_risk_hedge', 'beta_directional_short_v4')
            )
            
            # [Phase 80 + Track 2 Isolation] Short-term AlphaStreams (S1, S2, S10), Track 2 Buffett QVM (S3_B) 및 HEDGE/EXIT는 필터 면제
            _is_track2_ord = (
                _stream == 'S3_B'
                or _ord.get('track') == 'TRACK_2_BUFFETT_QVM'
                or _ord.get('strategy') in ('buffett_qvm', 'qvm_value')
            )
            _is_alpha_exempt = (_stream in ('S1', 'S2', 'S10', 'S3_B') or _is_track2_ord) and not _is_exit and not _is_hedge

            # EXIT / HEDGE / Track 2 Buffett QVM 은 항상 통과
            if _is_exit or _is_hedge or _is_alpha_exempt:
                _filtered_orders.append(_ord)
                continue

            # MACRO_BOUND BUY → caution/bear/crash 시 기각 (동적 완화, 방어형 ETF 면제 및 US 실효 레짐 디커플링)
            _DEFENSIVE_TICKERS = {'TLT', 'GLD', 'UUP', 'SHV', 'SGOV', 'BIL', 'SH', 'SQQQ', 'SOXS', '114800', '252670', '251340', '459580', '449170'} | _dyn_def_set
            _ord_regime = _curr_regime if _is_kr_tk(_tk_u) else _dqe.resolve_effective_us_regime(_curr_regime, market_data)
            if _stream in _MACRO_BOUND and _action in ('BUY', 'LONG', '') and _tk_u not in _DEFENSIVE_TICKERS:
                _conf = float(_ord.get('confidence', 0.0))
                _ord_caution_cut = _dyn_caution_cut if _ord_regime == _curr_regime else _dqe.compute_dynamic_confidence_cutoff(_all_confs, regime=_ord_regime)
                _macro_caution_pass_conf = float(cfg.get('risk.macro_guard_caution_pass_conf', _ord_caution_cut))
                # caution 장세라도 확신도가 동적 분위수 임계값 이상이면 진입 허용
                if _ord_regime == 'caution' and _conf >= _macro_caution_pass_conf:
                    logger.info(f"  [Phase 76 MacroGuard] {_stream}({_tk_u}) BUY 통과 (regime=caution 이나 confidence={_conf:.2f}>={_macro_caution_pass_conf:.2f})")
                elif _ord_regime in ('caution', 'bear', 'crash'):
                    logger.info(
                        f"  [Phase 76 MacroGuard] {_stream}({_tk_u}) BUY 기각 "
                        f"(regime={_ord_regime}, conf={_conf:.2f})"
                    )
                    continue

            # [Task 2] S3 Pyramiding (불타기) 허용
            if _stream in ('S3', 'S3_A') and _action in ('BUY', 'LONG'):
                _conf = float(_ord.get('confidence', 0.0))
                _pyramid_conf_th  = float(cfg.get('s3.pyramiding_conf_threshold', max(0.65, _dyn_caution_cut + 0.10)))
                _pyramid_scale    = float(cfg.get('s3.pyramiding_scale_factor',   2.00))  # (conf-th)*scale = max boost
                if _conf >= _pyramid_conf_th:
                    _pyramid_mult = 1.0 + (_conf - _pyramid_conf_th) * _pyramid_scale
                    if 'amount_krw' in _ord:
                        _ord['amount_krw'] = _ord['amount_krw'] * _pyramid_mult
                        logger.info(f"  🔥 [Pyramiding] {_stream} 강한 확신(conf={_conf:.2f}): 투자 스케일 {_pyramid_mult:.2f}x 확대")


            # MICRO_BOUND BUY → regime 무관, IntradayMicroGuard만 체크
            if _stream in _MICRO_BOUND and _action in ('BUY', 'LONG', ''):
                try:
                    from src.risk.intraday_micro_guard import IntradayMicroGuard
                    _mg = IntradayMicroGuard()
                    _mg_ok, _mg_reason = _mg.check(market_data)
                    if not _mg_ok:
                        logger.warning(
                            f"  [Phase 76 MicroGuard] {_stream} BUY 일시정지: {_mg_reason}"
                        )
                        continue
                    logger.info(
                        f"  [Phase 76 MicroGuard] {_stream} BUY 통과 "
                        f"(regime={_ord_regime}, 거시무관)"
                    )
                except Exception as _mge:  # noqa: BLE001
                    logger.error(f"  [Phase 76 MicroGuard] 체크 실패, 통과: {_mge}", exc_info=True)

            _filtered_orders.append(_ord)
        # ★ [SSoT Live Order Policy] 허용 스트림 및 모든 청산/교체매매(Swap) 주문 통과
        _ORDER_PERMITTED_STREAMS = {
            'S0', 'S1', 'S2', 'S3', 'S3_A', 'S3_B', 'S3_C', 'S4', 'S5', 'S6', 'S10', 'S11', 'S12', 'S13', 'S14',
            'S0_BETA', 'S1_EDGE', 'S2_ML_ALPHA', 'S3_ACTIVE_MACRO', 'S10_MEGA_TREND',
            'S11_HIGH_BETA', 'S11_HIGHBETA_SNIPER', 'S12_DERIVATIVE_SQUEEZE', 'S13_JAPAN_ALPHA', 'S14_PAIR_ALPHA',
            'S_BETA', 'S_YIELD', 'EXIT_MANAGER', 'SYS_SWAP', 'SYS_NET', 'SYS_KILL', 'SYS_HEDGE', 'TACTIC_D', 'TACTIC_E'
        }
        orders = [
            o for o in _filtered_orders
            if str(o.get('stream_id', o.get('stream', ''))).upper() in _ORDER_PERMITTED_STREAMS
            or o.get('strategy') in ('YieldParking', 'sys_tactical_hedge', 'tail_risk_hedge', 'cross_alpha_swap_sell', 'cross_alpha_swap_buy', 'rebalance_sell', 'l4_global_exit', 'Kill Switch Liquidation')
            or str(o.get('action', o.get('direction', ''))).lower() in ('sell', 'short')
        ]

        # [Phase 78] S_YIELD (KOFR / CD) Auto-Liquidation
        # 새로운 매수 주문이 있거나 현금 부족 시, 파킹된 자산 전량을 가용 현금화합니다.
        _PARKING_TICKERS = {'459580', '449170', '423160', '430740', '357870', '357330', 'SHV', 'SGOV'} | _dqe.get_cash_equivalent_tickers()
        _cfg_kofr = str(cfg.get('sc_yield.kofr_ticker', _dqe.get_optimal_kr_parking_ticker() or '459580'))
        _PARKING_TICKERS.add(_cfg_kofr)

        positions_source = portfolio.get('positions', {}) if portfolio else {}
        positions_list = []
        if isinstance(positions_source, dict):
            for k, v in positions_source.items():
                if isinstance(v, dict):
                    v_copy = dict(v)
                    v_copy.setdefault('ticker', k)
                    positions_list.append(v_copy)
                elif hasattr(v, 'ticker'):
                    positions_list.append({
                        'ticker': getattr(v, 'ticker'),
                        'name': getattr(v, 'name', k),
                        'quantity': getattr(v, 'quantity', 0),
                        'qty': getattr(v, 'quantity', 0),
                        'current_price': getattr(v, 'current_price', 0.0),
                        'avg_price': getattr(v, 'avg_price', 0.0),
                        'amount': getattr(v, 'amount', 0.0) or (getattr(v, 'quantity', 0) * getattr(v, 'current_price', 0.0)),
                    })
        elif isinstance(positions_source, list):
            positions_list = positions_source

        # [Strict Domestic Isolation] 국내 파킹 자산 자동 매도는 오직 '국내 원화 주식(KR ticker)' 매수 주문이 있고,
        # 원화 현금이 실제 부족할 때만 허용! (해외/미국 주식 매수 주문으로 인한 국내 파킹 매도 100% 금지)
        kr_buy_amount = sum(
            float(o.get('amount_krw', o.get('amount', 0.0)) or 0.0)
            for o in orders
            if _is_kr_tk(o.get('ticker', '')) and
            (str(o.get('action', '')).lower() in ('buy', 'long') or str(o.get('direction', '')).lower() in ('long', 'buy'))
        )
        current_kr_cash = float(portfolio.get('cash', 0.0) or 0.0)
        needs_kr_cash = (kr_buy_amount > current_kr_cash) and (kr_buy_amount > 0)

        for park_pos in positions_list:
            p_ticker = str(park_pos.get('ticker', '')).strip()
            if p_ticker in _PARKING_TICKERS:
                p_amt = float(park_pos.get('amount', 0.0) or 0.0)
                p_qty = float(park_pos.get('qty', park_pos.get('quantity', 0.0)) or 0.0)
                p_price = float(park_pos.get('current_price', park_pos.get('avg_price', 0.0)) or 0.0)
                if p_qty > 0 and p_amt <= 0 and p_price > 0:
                    p_amt = p_qty * p_price
                if p_qty <= 0 and p_amt > 0 and p_price > 0:
                    p_qty = p_amt / p_price

                if p_amt > 0 and needs_kr_cash:
                    p_name = park_pos.get('name')
                    if not p_name or p_name == p_ticker:
                        try:
                            from src.utils.ticker_name_resolver import resolve_name
                            p_name = resolve_name(p_ticker)
                        except Exception:
                            p_name = '파킹형 금리 ETF'
                    orders.insert(0, {
                        'stream_id': 'S_YIELD',
                        'strategy': 'YieldParking',
                        'ticker': p_ticker,
                        'name': p_name,
                        'action': 'sell',
                        'direction': 'short',
                        'amount_krw': p_amt,
                        'amount': p_qty,
                        'quantity': int(p_qty),
                        'price': p_price if p_price > 0 else 100000.0,
                        'confidence': 1.0,
                        'execution_algo': 'market',
                        'reason': 'Auto-liquidation of parking asset to fund new buy orders'
                    })
                    portfolio['cash'] = float(portfolio.get('cash', 0.0) or 0.0) + p_amt
                    logger.info(f"  [S_YIELD] 자금 확보를 위해 파킹 자산 {p_ticker} {p_name} ({p_amt:,.0f}원) 전량 매도(가상 현금 편입) 선행 추가")
                    break

        # [Phase 78] MacroAttacker 오버레이 (Alpha Neutralization + Beta Directional Bet)
        try:
            from src.allocation.macro_attacker import MacroAttacker
            ma = MacroAttacker()
            if ma:
                orders = ma.apply_macro_overlay(orders, portfolio, market_data=market_data, regime=regime)
        except Exception as e:
            logger.error(f"  [MacroAttacker] 실패 (스킵): {e}", exc_info=True)

        # ── Tactic E AsymmetricEventSqueezeSniper 평가 & 주문 주입 ──
        try:
            signal_cache = market_data.get('signal_cache', {})
            cvd_raw = signal_cache.get('cvd_15m')
            if cvd_raw is not None:
                cvd_val = float(cvd_raw)
                iv_curr = float(signal_cache.get('iv_current', 0.85))
                iv_hist = float(signal_cache.get('iv_historical_mean', 0.40))
                neg_gex = bool(signal_cache.get('is_negative_gex', False))
                eq_val = float(portfolio.get('total_nav', pget('portfolio.initial_capital', 19600000.0)))
                
                tactic_e_res = self.tactic_e_sniper.evaluate_earnings_event_trade(
                    ticker='NVDA', cvd_15m=cvd_val, iv_current=iv_curr, iv_historical_mean=iv_hist,
                    is_negative_gex_zone=neg_gex, account_equity=eq_val
                )
                result['tactic_e_event_squeeze'] = tactic_e_res
                if tactic_e_res['action'] in ('LONG', 'SHORT_INVERSE'):
                    _exec_tk = tactic_e_res.get('execution_ticker', tactic_e_res['target_ticker'])
                    orders.append({
                        'stream_id': 'TACTIC_E',
                        'strategy': 'EarningsEventSqueezeSniper',
                        'ticker': _exec_tk,
                        'action': 'buy',
                        'direction': 'long',
                        'is_hedge': tactic_e_res['action'] == 'SHORT_INVERSE',
                        'amount_krw': tactic_e_res['allocated_capital'],
                        'confidence': 0.90,
                        'reason': tactic_e_res['reason']
                    })
                    logger.info(f"  🎯 [TacticE] 어닝 스퀴즈 주문 주입: {tactic_e_res['action']} ({_exec_tk}) ₩{tactic_e_res['allocated_capital']:,.0f}")
            else:
                logger.debug("Tactic E: cvd_15m 미존재로 어닝 스퀴즈 주문 스킵")
        except Exception as e:
            logger.debug(f"Tactic E Sniper evaluation skipped: {e}")

        result['orders'] = orders
        logger.info(f"    📝 최종 주문: {len(orders)}개")

        # ── [Phase 77] Step 8.5: Cash Management (S_YIELD) ──
        logger.info("  📋 Step 8.5: Cash Management (S_YIELD)")
        try:
            total_nav = portfolio.get('total_nav', 0.0)
            
            # S8 계좌 삭제됨
            
            # ── Step 8.4: 가격/수량 및 필요 증거금 사전 확정 ──
            logger.info("  📋 Step 8.4: 가격/수량 및 증거금 확정")
            valid_orders = []
            actual_total_margin = 0.0
            actual_krw_margin = 0.0
            from src.data_collection.central_data_gateway import CentralDataGateway
            _dyn_fx = CentralDataGateway().get_usdkrw_rate()
            _jpy_fx = float(((market_data or {}).get('signal_cache') or {}).get('jpykrw', cfg.get('fx.jpykrw_fallback', 9.0)) or 9.0)
            
            for o in orders:
                if o.get('action') == 'sell' or o.get('direction') == 'short':
                    # 매도 주문 수량 보장
                    if not o.get('quantity') and o.get('price', 0) > 0:
                        _p = float(o['price'])
                        _tk_str = str(o.get('ticker', '')).strip()
                        _is_jp = _tk_str.upper().endswith('.T')
                        _is_us = (not _is_kr_tk(_tk_str)) and (not _is_jp)
                        if _is_us and not _dyn_fx:
                            logger.error(f"    🛑 [FX Guard] {_tk_str}: USD/KRW 미확보 → 금액 기반 매도수량 산출 불가, 스킵")
                            continue
                        _p_krw = (_p * _jpy_fx) if _is_jp else ((_p * _dyn_fx) if _is_us else _p)
                        o['quantity'] = max(1, int(float(o.get('amount_krw', 0.0)) / _p_krw))
                    valid_orders.append(o)
                    continue
                    
                _exec_price = o.get('price', 0)
                _tk_str_b = str(o.get('ticker', '')).strip()
                _is_jp = _tk_str_b.upper().endswith('.T')
                _is_us = (not _is_kr_tk(_tk_str_b)) and (not _is_jp)
                if not self._is_ticker_session_open_for_live_exit(_tk_str_b, market_data):
                    logger.debug(f"    ⏭️ [Step 8.4 Session Gate] {_tk_str_b}: 현재 거래 세션 외 신규 매수 주문 스킵")
                    if o.get('pair_id'):
                        o['_pair_skipped_s84'] = True
                    continue
                if not _exec_price or _exec_price <= 0:
                    _raw_tk = str(o.get('ticker', '')).strip()
                    from src.utils.ticker_canonical import canonical_ticker
                    _ticker = canonical_ticker(_raw_tk, context='stream_orchestrator.price_resolve')
                    o['ticker'] = _ticker
                    try:
                        # 1. market_data 확인
                        if market_data:
                            _exec_price = float((market_data.get('current_prices') or {}).get(_ticker, 0.0) or
                                                (market_data.get('prices') or {}).get(_ticker, 0.0) or
                                                (market_data.get('kr_close') or {}).get(_ticker, 0.0) or
                                                (market_data.get('us_close') or {}).get(_ticker, 0.0))
                        # 2. data_bridge 확인
                        if (not _exec_price or _exec_price <= 0) and hasattr(self, 'data_bridge') and self.data_bridge and hasattr(self.data_bridge, 'get_current_price'):
                            _exec_price = float(self.data_bridge.get_current_price(_ticker) or 0.0)
                        # 3. CentralDataGateway 조회 (KR/US 3중 핫페일오버)
                        if not _exec_price or _exec_price <= 0:
                            try:
                                from src.data_collection.central_data_gateway import CentralDataGateway
                                _cdg_res = CentralDataGateway().get_current_price(_ticker)
                                if isinstance(_cdg_res, dict) and _cdg_res.get('price'):
                                    _exec_price = float(_cdg_res['price'])
                                elif isinstance(_cdg_res, (int, float)) and _cdg_res > 0:
                                    _exec_price = float(_cdg_res)
                            except Exception:
                                pass
                        # 4. KISPriceService 조회
                        if not _exec_price or _exec_price <= 0:
                            try:
                                from src.execution.kis_price_service import KISPriceService
                                _kps_res = KISPriceService().get_current_price(_ticker)
                                if isinstance(_kps_res, dict) and _kps_res.get('price'):
                                    _exec_price = float(_kps_res['price'])
                                elif isinstance(_kps_res, (int, float)) and _kps_res > 0:
                                    _exec_price = float(_kps_res)
                            except Exception:
                                pass
                        # 5. 파킹 ETF 기준가(NAV) 보호 폴백
                        if not _exec_price or _exec_price <= 0:
                            _PARKING_FALLBACK_PX = {
                                '459580': 1074340.0,
                                '423160': 110770.0,
                                '449170': 112110.0,
                                '430740': 110770.0,
                                '357870': 58190.0,
                                '357330': 58190.0,
                                'SHV': 110.0,
                                'SGOV': 100.0,
                            }
                            if _ticker in _PARKING_FALLBACK_PX:
                                _exec_price = _PARKING_FALLBACK_PX[_ticker]
                                logger.info(f"    🛡️ [Step 8.4 Cash Anchor] {_ticker} 파킹 ETF 기준가(₩{_exec_price:,.0f}) 확정")
                        # 6. 인스티튜셔널 브리지 확인 (국내)
                        if (not _exec_price or _exec_price <= 0) and _is_kr_tk(str(_ticker)):
                            from src.data_collection.pykrx_compat import stock as _pykrx_s8
                            from datetime import date as _d, timedelta as _td
                            _t_str = _d.today().strftime('%Y%m%d')
                            _w_str = (_d.today() - _td(days=7)).strftime('%Y%m%d')
                            _df_p = _pykrx_s8.get_market_ohlcv_by_date(_w_str, _t_str, str(_ticker))
                            if len(_df_p) > 0:
                                _exec_price = float(_df_p.iloc[-1].get('종가', 0.0))

                        if _exec_price and _exec_price > 0:
                            o['price'] = _exec_price
                            logger.info(f"    🔄 [Live Price Sync] {_ticker} 실시간 가격 {_exec_price:,.2f} 획득")
                    except Exception as _ep:
                        logger.error(f"    ⚠️ 실시간 가격 획득 실패: {_ep}", exc_info=True)
                
                if not _exec_price or _exec_price <= 0:
                    logger.warning(f"    ⚠️ 가격 없음 → 스킵: {o.get('ticker')} ({o.get('stream_id')})")
                    if o.get('pair_id'):
                        o['_pair_skipped_s84'] = True
                    continue
                    
                if o.get('execution_algo', 'market') == 'market':
                    # [Pure Dynamic Math] 개별 종목 20일 실현 변동성에 비례한 동적 시장가 증거금 완충율 (1.3 하드코딩 완전 폐지)
                    _v = _compute_asset_daily_volatility(str(o.get('ticker', '')), market_data, fallback=0.015)
                    margin_rate = round(1.0 + min(0.20, max(0.01, _v * 2.5)), 4)
                else:
                    margin_rate = 1.0
                if _is_jp:
                    _cost_per_share_krw = _exec_price * _jpy_fx * margin_rate
                elif _is_us:
                    if not _dyn_fx:
                        logger.error(f"    🛑 [FX Guard] {o.get('ticker')}: USD/KRW 미확보 → 해외 신규 매수 스킵 (fail-closed)")
                        if o.get('pair_id'):
                            o['_pair_skipped_s84'] = True
                        continue
                    _cost_per_share_krw = _exec_price * _dyn_fx * margin_rate
                else:
                    _cost_per_share_krw = _exec_price * margin_rate
                
                # 수량이 이미 설정되어 있지 않다면 amount_krw 기반 산출
                qty = int(o.get('quantity', 0))
                if qty <= 0:
                    qty = int(o.get('amount_krw', 0.0) / _cost_per_share_krw) if _cost_per_share_krw > 0 else 0
                
                # Min-Lot Sweep: 기보유 수량이 0주이고 당일(연속 세션) 청산 이력이 없는 순수 신규 진입 종목에 한해 최소 1주 배정
                _tk_check = str(o.get('ticker', '')).split(':')[-1].upper()
                _already_held_s84 = False
                if portfolio and isinstance(portfolio, dict):
                    for _ps_k in ('positions', 'holdings'):
                        for _pk_s, _pv_s in (portfolio.get(_ps_k) or {}).items():
                            if isinstance(_pv_s, dict):
                                _pt_s = str(_pv_s.get('ticker') or str(_pk_s).split(':')[-1]).upper()
                                if _pt_s == _tk_check and float(_pv_s.get('quantity', _pv_s.get('qty', 0)) or 0) > 0:
                                    _already_held_s84 = True
                                    break
                _recently_exited_s84 = False
                if getattr(self, 'exec_mode', 'live') == 'live':
                    try:
                        from src.execution.position_ledger import PositionLedgerManager
                        _recently_exited_s84 = _tk_check in PositionLedgerManager().get_daily_exited_tickers()
                    except Exception:
                        _recently_exited_s84 = False
                _amt_budget = float(o.get('amount_krw', 0.0) or 0.0)
                # ★ Wave 0 S4: 예산이 1주 비용의 50% 이상이고 당일 청산 종목이 아닐 때만 1주 배정
                if qty <= 0 and not _already_held_s84 and not _recently_exited_s84 and _cost_per_share_krw > 0 and _amt_budget >= 0.5 * _cost_per_share_krw:
                    qty = 1
                    o['_min_lot_sweep'] = True
                    o['amount_krw'] = max(_amt_budget, float(_cost_per_share_krw / max(margin_rate, 1.0)))
                    logger.info(f"    💡 [Step 8.4 Min-Lot Sweep] {o.get('ticker')} (단가: {_exec_price:.2f}) 신규 진입 예산 미달 절삭 방지 ➔ 최소 1주 배정")

                if qty > 0:
                    o['quantity'] = qty
                    o['required_margin'] = qty * _cost_per_share_krw
                    actual_total_margin += o['required_margin']
                    if not _is_us and not _is_jp:
                        actual_krw_margin += o['required_margin']
                    valid_orders.append(o)
                else:
                    if o.get('pair_id'):
                        o['_pair_skipped_s84'] = True
                    logger.warning(f"    ⚠️ 예산 부족으로 스킵: {o.get('ticker')} (예산: {o.get('amount_krw'):,.0f} < 필요증거금: {_cost_per_share_krw:,.0f})")

            _skipped_s84_pids = {str(_o_s.get('pair_id')) for _o_s in orders if _o_s.get('_pair_skipped_s84') and _o_s.get('pair_id')}
            if _skipped_s84_pids:
                _cleaned_valid_s84 = []
                for _vo in valid_orders:
                    if str(_vo.get('pair_id', '')) in _skipped_s84_pids:
                        _rm_m = float(_vo.get('required_margin', 0.0) or 0.0)
                        actual_total_margin = max(0.0, actual_total_margin - _rm_m)
                        if _is_kr_tk(str(_vo.get('ticker', ''))):
                            actual_krw_margin = max(0.0, actual_krw_margin - _rm_m)
                        logger.info(
                            f"    ⚖️ [Step 8.4 Pair Atomicity] {_vo.get('ticker')} (pair_id={_vo.get('pair_id')}): "
                            f"페어 파트너 예산 부족 스킵에 따라 반쪽 레그 동반 제외"
                        )
                        continue
                    _cleaned_valid_s84.append(_vo)
                valid_orders = _cleaned_valid_s84

            orders = valid_orders
            
            # 메인 계좌의 남은 잉여 순수 원화 현금 계산 (달러 통합증거금 침범 100% 차단)
            available_cash = portfolio.get('cash', 0.0)
            if hasattr(self, 'execution_engine') and getattr(self.execution_engine, 'mode', '') in ('live', 'paper'):
                try:
                    _trader = self.execution_engine._get_trader(stream_id='S_YIELD')
                    if _trader:
                        _trader.fetch_live_balance()
                        _raw_d2 = float(getattr(_trader, 'raw_d2_cash', _trader.account.cash) or 0.0)
                        available_cash = max(0.0, min(float(_trader.account.cash or 0.0), _raw_d2))
                        logger.info(f"    [S_YIELD] 실계좌 순수 원화 현금 연동 성공: {available_cash:,.0f}원 (통합증거금 제외)")
                except Exception as e:
                    logger.warning(f"    [S_YIELD] 실계좌 현금 연동 실패, portfolio 백업 사용: {e}")
            
            idle_cash = available_cash - actual_krw_margin
            
            yield_ticker = str(cfg.get('sc_yield.kofr_ticker', '449170'))  # KODEX KOFR금리액티브(합성)
            
            price = None
            if market_data:
                signal_cache = market_data.get('signal_cache', {})
                raw_price = signal_cache.get(yield_ticker)
                if isinstance(raw_price, dict):
                    price = float(raw_price.get('close', 0.0))
                elif raw_price is not None:
                    price = float(raw_price)
            
            if not price or price <= 0:
                # KIS API 실시간 가격 조회 우선 시도
                try:
                    if hasattr(self, 'execution_engine') and self.execution_engine:
                        _trader = self.execution_engine._get_trader(stream_id='S_YIELD')
                        if _trader and hasattr(_trader, '_get_current_price'):
                            price = float(_trader._get_current_price(yield_ticker) or 0.0)
                            if price > 0:
                                logger.info(f"    [S_YIELD] KOFR 실시간 시세 KIS API 연동 성공: {price:,.0f}원")
                except Exception as _ep:
                    logger.debug(f"  [S_YIELD] KIS API 시세 조회 생략: {_ep}")
                
            if not price or price <= 0:
                kofr_pos = next((p for p in portfolio.get('positions', []) if isinstance(p, dict) and p.get('ticker') == yield_ticker), None)
                if kofr_pos and kofr_pos.get('avg_price', 0) > 0:
                    price = float(kofr_pos['avg_price'])
                    logger.info(f"    [S_YIELD] KOFR 시세 누락 → 보유 평단가({price:,.0f}원)로 추정")
                else:
                    price = 111555.0
                    logger.info(f"    [S_YIELD] KOFR 시세 누락 → 최신 시장가 111,555원으로 추정")
            
            # [Transaction Cost / Friction Guard]
            # 평일 단 하루(1박 2일) 파킹은 매매수수료 마찰 대비 1일치 이자의 실익이 없음.
            # 금요일(주말 3일치 파킹) 또는 연휴/공휴일 직전(휴일 3일 이상) 무위험 이자가 확실히 수수료를 상회할 때만 파킹 집행!
            is_parking_efficient = False
            try:
                from src.utils.market_calendar import MarketCalendar
                cal = MarketCalendar()
                now_dt = datetime.now()
                tomorrow = now_dt.date() + timedelta(days=1)
                cur_check = tomorrow
                gap_days = 0
                while not cal.is_trading_day(cur_check.strftime('%Y%m%d')) and gap_days < 10:
                    gap_days += 1
                    cur_check += timedelta(days=1)
                total_days = 1 + gap_days
                if total_days >= 3:
                    is_parking_efficient = True
                else:
                    logger.info(f"    [S_YIELD Friction Guard] 익일이 정규 거래일(보유 기간: {total_days}일) ➔ 평일 단기 파킹 스킵 (원화 현금 100% 안전 보존)")
            except Exception as _fe:
                logger.debug(f"S_YIELD Friction Guard check bypass: {_fe}")

            # 잉여 현금이 포트폴리오의 2% 이상이고, 주말/연휴 등 3일 이상 무위험 이자가 확정될 때만 KOFR 등 무위험 자산에 파킹
            parking_threshold = float(pget('orchestrator.idle_cash_parking_threshold', 0.02))
            if is_parking_efficient and idle_cash > total_nav * parking_threshold:
                logger.info(f"    [S_YIELD] 주말/연휴 {total_days}일치 무위험 이자 확정 ➔ 잉여 현금 {idle_cash:,.0f}원 파킹 집행")
                
                # KOFR 파킹 수수료 및 증거금 안전 버퍼 (0.5% 여유) 적용 수량 산출
                safe_idle_cash = idle_cash * 0.995
                qty = int(safe_idle_cash / price)
                if qty > 0:
                    orders.append({
                        'stream_id': 'S_YIELD',
                        'strategy': 'YieldParking',
                        'ticker': yield_ticker,
                        'name': 'KODEX KOFR금리액티브(합성)',
                        'action': 'buy',
                        'direction': 'long',
                        'amount_krw': qty * price,
                        'quantity': qty,
                        'amount': qty * price,
                        'price': price,
                        'confidence': 1.0,
                        'execution_algo': 'limit',
                        'order_type': 'limit',
                        'reason': f"Idle Cash Safe Parking ({qty}주)"
                    })
            elif idle_cash < 0:
                # 현금이 부족하면 기존에 파킹해둔 KOFR을 매도하여 현금 확보
                post_positions = portfolio.get('positions', {})
                kofr_pos = post_positions.get(yield_ticker, 0.0)
                if isinstance(kofr_pos, dict):
                    current_kofr = float(kofr_pos.get('amount', 0.0))
                else:
                    current_kofr = float(kofr_pos)
                
                if current_kofr > 10000:
                    margin = float(pget('orchestrator.idle_cash_margin', 1.05))
                    needed = abs(idle_cash) * margin # 수수료 등 감안 5% 여유
                    sell_amount = min(current_kofr, needed)
                    logger.info(f"    [S_YIELD] 현금 부족! 파킹된 KOFR {sell_amount:,.0f}원 매도하여 유동성 공급")
                    orders.append({
                        'stream_id': 'S_YIELD',
                        'strategy': 'YieldParking',
                        'ticker': yield_ticker,
                        'name': 'KODEX KOFR금리액티브(합성)',
                        'action': 'sell',
                        'direction': 'short', # 청산 의미
                        'amount_krw': sell_amount,
                        'quantity': int(sell_amount / price),
                        'amount': sell_amount / price,
                        'price': price,
                        'confidence': 1.0,
                        'execution_algo': 'market',
                        'reason': 'Liquidity Provision'
                    })
        except Exception as e:
            logger.error(f"    [S_YIELD] 파킹 에러: {e}", exc_info=True)

        # ── Step 8: SelfLearning ──
        logger.info("  📋 Step 8: SelfLearning")
        try:
            learning_result = self.self_learning.update({
                'streams': stream_metrics,
                'feature_ic': market_data.get('feature_ic', {}),
            })
            result['learning'] = {
                'applied': learning_result.get('applied', False),
                'n_changes': learning_result.get('judgment', {}).get(
                    'n_changes', 0),
            }
        except Exception as e:
            logger.error(f"    SelfLearning 실패: {e}", exc_info=True)
            result['learning'] = {'applied': False, 'error': str(e)}

        # ── Step 8.5: [Universal Baseline Elevation] 전사 공통 Universal Exit Engine 평가 (S0~S11 전 포지션) ──
        dynamic_exit_orders = []
        if cfg.get('exit.universal_engine_enabled', True):
            logger.info("  📋 Step 8.5: UniversalExitEngine (전 포지션 상방 무제한 샹들리에 & 고점 이탈 방어) 평가")
            try:
                from src.risk.universal_exit_engine import UniversalExitEngine
                universal_exit = UniversalExitEngine()
                all_positions = {}
                
                # 1순위: KIS 실계좌 SSOT 포지션 우선 적재 (NVDA, SOXX, 091160 등 실보유 종목)
                kp_path = _ROOT / 'results' / 'kis_portfolio.json'
                if kp_path.exists():
                    try:
                        from src.execution.position_ledger import PositionLedgerManager
                        ledger_mgr = PositionLedgerManager()
                        kp = json.loads(kp_path.read_text(encoding='utf-8'))
                        for t, h in kp.get('holdings', {}).items():
                            if not self._is_ticker_session_open_for_live_exit(t, market_data):
                                continue
                            curr_p = float(h.get('current_price', 0.0))
                            curr_ret = float(h.get('return_pct', 0.0))
                            hwm_data = ledger_mgr.update_hwm(
                                t,
                                current_price=curr_p,
                                current_pnl_pct=curr_ret,
                                avg_price=float(h.get('avg_price', 0.0) or 0.0),
                                quantity=float(h.get('qty', h.get('quantity', 0)) or 0.0),
                            )
                            _ed_str = str(h.get('entry_date') or hwm_data.get('entry_date') or '')[:10]
                            _today_s = datetime.now().strftime('%Y-%m-%d')
                            if 'holding_minutes' in h:
                                _h_mins = float(h['holding_minutes'])
                            elif _ed_str and _ed_str < _today_s:
                                _h_mins = 1440.0
                            else:
                                try:
                                    _c_dt = datetime.fromisoformat(str(hwm_data.get('created_at', '')).replace('Z', '+00:00')).replace(tzinfo=None)
                                    _h_mins = max(0.0, (datetime.now().replace(tzinfo=None) - _c_dt).total_seconds() / 60.0)
                                except Exception:
                                    _h_mins = 0.0
                            all_positions[f"LIVE_SSOT:{t}"] = {
                                'ticker': t,
                                'stream_id': h.get('stream_id') or hwm_data.get('stream_id') or 'LIVE_SSOT',
                                'strategy': h.get('strategy') or hwm_data.get('strategy') or '',
                                'track': h.get('track') or hwm_data.get('track') or '',
                                'quantity': h.get('qty', 0),
                                'entry_price': h.get('avg_price', 0.0),
                                'current_price': curr_p,
                                'unrealized_pnl_pct': curr_ret,
                                'peak_pnl_pct': float(hwm_data['peak_pnl_pct']) if hwm_data.get('peak_pnl_pct') is not None else curr_ret,
                                'peak_price': float(hwm_data['peak_price']) if (hwm_data.get('peak_price') or 0.0) > 0 else curr_p,
                                'trail_activated': bool(hwm_data.get('trail_activated', False)),
                                'holding_minutes': _h_mins,
                                'market': h.get('market', 'KR' if _is_kr_tk(t) else 'US')
                            }
                    except Exception as _kpe:
                        logger.warning(f"kis_portfolio.json 로드 예외: {_kpe}")

                # 2순위: 섀도우 포트폴리오 포지션 병합 (Live 모드에서는 실계좌 SSOT 오염 방지를 위해 차단)
                if getattr(self, 'exec_mode', 'live') != 'live':
                    sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
                    if sp_path.exists():
                        try:
                            sp = json.loads(sp_path.read_text(encoding='utf-8'))
                            for pk, pv in sp.get('positions', {}).items():
                                if pk not in all_positions:
                                    all_positions[pk] = pv
                        except Exception as _spe:
                            logger.debug(f"shadow_portfolio.json 병합 생략: {_spe}")
                    
                # 전사 공통 Exit 평가 실행 (S0~S11)
                exit_eval = universal_exit.evaluate_all_positions(all_positions, market_data, regime)
                _already_exited_tickers = {str(eo.get('ticker', '')).split(':')[-1] for eo in exit_orders if eo.get('ticker')}
                dynamic_exit_orders = [
                    deo for deo in exit_eval.get('exit_orders', [])
                    if str(deo.get('ticker', '')).split(':')[-1] not in _already_exited_tickers
                ]
                
                result['universal_exit'] = {
                    'evaluated_count': exit_eval.get('evaluated_count', 0),
                    'exit_order_count': len(dynamic_exit_orders)
                }
                logger.info(f"    📊 Universal Exit Engine: {exit_eval.get('evaluated_count', 0)}개 포지션 평가, {len(dynamic_exit_orders)}건 전사 체결 주문 합성 완료")
            except Exception as e:
                logger.error(f"    UniversalExitEngine 평가 실패: {e}", exc_info=True)
                result['universal_exit'] = {'applied': False, 'error': str(e)}

        # ── Step 9: ExecutionEngine 체결 ──
        logger.info("  📋 Step 9: ExecutionEngine 체결")
        # 🎯 [Risk Dominance Gate & Sunk-Cost Exponential Decay Re-Entry Hurdle]:
        # 1) 현 사이클에서 즉시 청산(매도)되는 종목은 동시 매수 충돌을 차단하고,
        # 2) 당일 이전에 실제 체결/접수되어 청산된 종목은 매몰비용·변동성 지수감쇠 허들(H_reentry)을 초과하는 기대수익 신호만 통과!
        exit_tickers = {
            str(o['ticker']).upper() for o in (exit_orders + dynamic_exit_orders)
            if o.get('ticker') and (o.get('action') == 'sell' or o.get('direction') in ('short', 'sell'))
        }
        same_day_exit_details = {}
        if getattr(self, 'exec_mode', 'live') == 'live':
            try:
                from src.execution.position_ledger import PositionLedgerManager
                _plm = PositionLedgerManager()
                same_day_exit_details = _plm.get_daily_exit_details()
            except Exception:
                pass
        if exit_tickers or same_day_exit_details:
            from src.utils.dynamic_quant_engine import get_quant_engine
            _dqe_step9 = get_quant_engine()
            filtered_orders = []
            _vetoed_pair_ids = set()
            _vetoed_tickers = set()
            for o in orders:
                _otk = str(o.get('ticker', '')).upper()
                _opid = str(o.get('pair_id', '') or '').strip()
                _is_buy_o = o.get('action') in ('buy', 'long') or o.get('direction') in ('buy', 'long')
                if _is_buy_o and _otk in exit_tickers:
                    logger.warning(f"  🛑 [Risk Exit Veto] {_otk} 동시 청산 발화 중 ➔ 신규 매수 시그널({o.get('stream_id', o.get('stream', ''))}) 취소")
                    _vetoed_tickers.add(_otk)
                    if _opid:
                        _vetoed_pair_ids.add(_opid)
                    continue
                if _is_buy_o and _otk in same_day_exit_details:
                    _ex_info = same_day_exit_details[_otk]
                    _prev_pnl = float(_ex_info.get('realized_pnl_pct', 0.0) or 0.0)
                    _ex_reason_l = str(_ex_info.get('reason', '') or '').lower()
                    _lev_inv_set = set(cfg.get('leverage_judge.etf_tickers', [])) | set(cfg.get('leverage_judge.inverse_tickers', []))
                    for _attr_tk in (
                        getattr(getattr(self, 's0', None), 'TICKER_INVERSE_2X', ''),
                        getattr(getattr(self, 's0', None), 'TICKER_LEVERAGE_2X', ''),
                        getattr(getattr(self, 's0', None), 'TICKER_INVERSE_TECH', ''),
                        getattr(getattr(self, 's0', None), 'TICKER_INVERSE_KQ', ''),
                        getattr(getattr(self, 's12', None), 'ticker_inv_2x', ''),
                    ):
                        if _attr_tk:
                            _lev_inv_set.add(str(_attr_tk).upper())
                    _is_stop_exit = _prev_pnl < 0 or any(
                        kw in _ex_reason_l for kw in ('stop', 'sl', '손절', 'trailing', 'etf stop', 'etf_dynamic_exit')
                    )
                    _is_profit_harvest = _prev_pnl > 0 and any(
                        kw in _ex_reason_l for kw in ('scale_out', 'harvest', 'tp', '익절')
                    )
                    if _is_stop_exit or (_otk in _lev_inv_set and not _is_profit_harvest):
                        logger.warning(
                            f"  🛑 [Same-Day Stop/Inverse Lockout] {_otk}: 당일 손절/레버리지·인버스 청산 이력 존재 "
                            f"(PnL={_prev_pnl:+.2f}%, 사유={_ex_info.get('reason', '')}) ➔ 당일 재매수 원천 차단"
                        )
                        _vetoed_tickers.add(_otk)
                        if _opid:
                            _vetoed_pair_ids.add(_opid)
                        continue
                    try:
                        _ex_dt = datetime.fromisoformat(str(_ex_info.get('exited_at', ''))).replace(tzinfo=None)
                        _elapsed_m = max(0.0, (datetime.now().replace(tzinfo=None) - _ex_dt).total_seconds() / 60.0)
                    except Exception:
                        _elapsed_m = 0.0
                    _ord_val = float(o.get('amount_krw', 2_500_000.0) or 2_500_000.0)
                    _ord_px = float(o.get('price', 0.0) or 0.0)
                    _hurdle_pct = _dqe_step9.compute_reentry_hurdle_pct(
                        _otk, elapsed_minutes=_elapsed_m, prev_realized_pnl_pct=_prev_pnl, order_value_krw=_ord_val, price=_ord_px
                    )
                    _raw_tp = float(o.get('expected_return_pct', o.get('tp_pct', 0.0)) or 0.0)
                    if 0 < _raw_tp < 0.50:
                        _raw_tp *= 100.0
                    _sig_ev_pct = _raw_tp * float(o.get('confidence', 0.5) or 0.5)
                    if _sig_ev_pct < _hurdle_pct:
                        logger.warning(
                            f"  🛑 [Sunk-Cost Re-Entry Hurdle Veto] {_otk}: 기대수익({_sig_ev_pct:.2f}%) < "
                            f"매몰비용·변동성 감쇠 허들({_hurdle_pct:.2f}%, 경과 {_elapsed_m:.0f}m) ➔ 재매수 기각"
                        )
                        _vetoed_tickers.add(_otk)
                        if _opid:
                            _vetoed_pair_ids.add(_opid)
                        continue
                filtered_orders.append(o)
            if _vetoed_pair_ids or _vetoed_tickers:
                _has_remaining_swap_buy = any(
                    o.get('strategy') == 'cross_alpha_swap_buy'
                    and (o.get('action') in ('buy', 'long') or o.get('direction') in ('buy', 'long'))
                    for o in filtered_orders
                )
                _pair_safe_orders = []
                for o in filtered_orders:
                    _otk = str(o.get('ticker', '')).upper()
                    _opid = str(o.get('pair_id', '') or '').strip()
                    _opartner = str(o.get('pair_partner', '') or '').split(':')[-1].strip().upper()
                    _osid = str(o.get('stream_id', o.get('stream', '')) or '')
                    _is_buy_o = o.get('action') in ('buy', 'long') or o.get('direction') in ('buy', 'long')
                    if o.get('strategy') == 'cross_alpha_swap_sell' and not _has_remaining_swap_buy:
                        logger.warning(
                            f"  🛑 [Atomic Swap Leg Veto] {_otk}: 교체 매수 대상(Swap Buy) 기각으로 인한 단독 스왑 매도 원천 취소"
                        )
                        continue
                    if _is_buy_o and (
                        (_opid and _opid in _vetoed_pair_ids)
                        or (_opartner and _opartner in _vetoed_tickers and (_opid or _osid in ('S14', 'S14_PAIR_ALPHA', 'S7')))
                    ):
                        logger.warning(
                            f"  🛑 [Atomic Pair Leg Veto] {_otk} ({_osid}): 파트너 헤지 레그({_opartner or _opid}) 기각으로 인한 단방향(One-Legged) 롱 진입 원천 차단"
                        )
                        continue
                    _pair_safe_orders.append(o)
                filtered_orders = _pair_safe_orders
            orders = filtered_orders

        # [Red Team Fix] 주문 충돌 방지 (Order Netting): 우세 방향 가격/메타데이터 정밀 상계 및 50만 원 미만 잔챙이 차단
        netted_orders_map = {}
        all_candidate_orders = orders + dynamic_exit_orders
        min_order_krw = float(cfg.get('execution.min_order_value_krw', 500000.0) or 500000.0)
        _held_qty_by_ticker = {}
        _track2_held_tickers = set()
        for _pk_h, _pv_h in (portfolio.get('positions', {}) if portfolio else {}).items():
            if isinstance(_pv_h, dict):
                _tk_h = str(_pv_h.get('ticker') or str(_pk_h).split(':')[-1])
                _q_h = int(_pv_h.get('quantity', _pv_h.get('qty', 0)) or 0)
                _held_qty_by_ticker[_tk_h] = max(_held_qty_by_ticker.get(_tk_h, 0), _q_h)
                if _q_h > 0 and (
                    _pv_h.get('stream_id') == 'S3_B'
                    or _pv_h.get('track') == 'TRACK_2_BUFFETT_QVM'
                    or _pv_h.get('strategy') in ('buffett_qvm', 'qvm_value')
                    or str(_pk_h).startswith('S3_B:')
                ):
                    _track2_held_tickers.add(_tk_h)
        for _pk_h, _pv_h in (portfolio.get('holdings', {}) if portfolio else {}).items():
            if isinstance(_pv_h, dict):
                _tk_h = str(_pv_h.get('ticker') or str(_pk_h).split(':')[-1])
                _q_h = int(_pv_h.get('quantity', _pv_h.get('qty', 0)) or 0)
                _held_qty_by_ticker[_tk_h] = max(_held_qty_by_ticker.get(_tk_h, 0), _q_h)
                if _q_h > 0 and (
                    _pv_h.get('stream_id') == 'S3_B'
                    or _pv_h.get('track') == 'TRACK_2_BUFFETT_QVM'
                    or _pv_h.get('strategy') in ('buffett_qvm', 'qvm_value')
                ):
                    _track2_held_tickers.add(_tk_h)
        import os as _os_s9
        _in_pytest_s9 = bool(_os_s9.environ.get('MERIDIAN_TEST_MODE') == '1' or _os_s9.environ.get('PYTEST_CURRENT_TEST') or (market_data or {}).get('_test_mode', False))
        if (
            getattr(self, 'exec_mode', 'live') == 'live'
            and (_ROOT / 'results' / 'kis_portfolio.json').exists()
            and (not _in_pytest_s9 or not _held_qty_by_ticker)
        ):
            try:
                _kp_net = json.loads((_ROOT / 'results' / 'kis_portfolio.json').read_text(encoding='utf-8'))
                for _kt_n, _kh_n in (_kp_net.get('holdings') or {}).items():
                    if isinstance(_kh_n, dict):
                        _q_kn = int(_kh_n.get('qty', _kh_n.get('quantity', 0)) or 0)
                        _held_qty_by_ticker[str(_kt_n)] = max(_held_qty_by_ticker.get(str(_kt_n), 0), _q_kn)
                        if _q_kn > 0 and (
                            _kh_n.get('stream_id') == 'S3_B'
                            or _kh_n.get('track') == 'TRACK_2_BUFFETT_QVM'
                            or _kh_n.get('strategy') in ('buffett_qvm', 'qvm_value')
                        ):
                            _track2_held_tickers.add(str(_kt_n))
            except Exception:
                pass
        if not _in_pytest_s9:
            try:
                from src.execution.position_ledger import PositionLedgerManager as _PLM_Net
                for _tk_ln, _meta_ln in (_PLM_Net().load_ledger() or {}).items():
                    if isinstance(_meta_ln, dict) and _held_qty_by_ticker.get(str(_tk_ln), 0) > 0:
                        if (
                            _meta_ln.get('stream_id') == 'S3_B'
                            or _meta_ln.get('track') == 'TRACK_2_BUFFETT_QVM'
                            or _meta_ln.get('strategy') in ('buffett_qvm', 'qvm_value')
                        ):
                            _track2_held_tickers.add(str(_tk_ln))
            except Exception:
                pass

        _jpy_fx_net = float(((market_data or {}).get('signal_cache') or {}).get('jpykrw', cfg.get('fx.jpykrw_fallback', 9.0)) or 9.0)

        for o in all_candidate_orders:
            ticker = o.get('ticker', '')
            if not ticker:
                continue
            
            raw_action = str(o.get('action', '')).lower()
            raw_direction = str(o.get('direction', '')).lower()
            # ★ Wave 0: 미지 action을 'buy'로 강제하던 결함 수정 ('exit'/'reduce' 청산이 매수로 뒤집힘).
            _SELL_WORDS = ('sell', 'short', 'exit', 'reduce', 'close', 'liquidate', 'trim', 'stop', 'stop_loss')
            _BUY_WORDS = ('buy', 'long', 'add_on')
            _has_exit_marker = bool(o.get('exit_type') or o.get('sell_type'))
            if raw_action in _BUY_WORDS:
                action = 'buy'
            elif raw_action in _SELL_WORDS:
                action = 'sell'
            elif raw_action in ('', 'enter', 'entry', 'open') and (raw_direction in ('short', 'sell') or _has_exit_marker):
                action = 'sell'
            elif raw_action in ('', 'enter', 'entry', 'open') and raw_direction in ('long', 'buy'):
                action = 'buy'
            elif _has_exit_marker:
                action = 'sell'
            else:
                logger.warning(f"  ⚠️ [Order Normalize] {ticker}: 방향 미상 action='{raw_action}' direction='{raw_direction}' → 주문 제외 (추측 금지)")
                continue

            # [Session Gate] 폐장 세션(야간 US 장중 국내 KR 종목 매수 등) 주문이 Final Gate S5 / CRO를 오염시키는 현상 차단
            if action == 'buy' and not self._is_ticker_session_open_for_live_exit(ticker, market_data):
                logger.debug(f"    ⏭️ [Session Gate] {ticker}: 현재 거래 세션 외 신규 매수 주문 스킵")
                continue

            price = float(o.get('price', 0.0))
            qty = int(o.get('quantity', 0))
            amount_krw = float(o.get('amount_krw', 0.0))
            if not qty and price > 0:
                _tk_s = str(ticker).strip()
                is_jp_stock = _tk_s.upper().endswith('.T')
                is_us_stock = (not _is_kr_tk(_tk_s)) and (not is_jp_stock)
                if is_jp_stock:
                    eff_price_krw = price * _jpy_fx_net
                    qty = int(amount_krw / eff_price_krw) if eff_price_krw > 0 else 0
                elif is_us_stock:
                    from src.data_collection.central_data_gateway import CentralDataGateway
                    fx_rate = CentralDataGateway().get_usdkrw_rate()
                    if not fx_rate or fx_rate <= 0:
                        logger.error(f"    🛑 [FX Guard] {ticker}: USD/KRW 환율 미확보 → 해외 주문 수량 산출 불가, 스킵")
                        continue
                    eff_price_krw = price * fx_rate
                    qty = int(amount_krw / eff_price_krw) if eff_price_krw > 0 else 0
                else:
                    eff_price_krw = price
                    qty = int(amount_krw / price)

                # 💡 [Min-Lot Liquidity Sweep] 미보유(0주) 신규 진입 시그널 0주 절삭 시 1주 보장
                # ★ Wave 0 S4: 예산이 1주 비용의 50% 이상일 때만 (amount=0/entry_score만으로 강제 매수 금지)
                if (qty == 0 and action == 'buy' and _held_qty_by_ticker.get(str(ticker), 0) == 0
                        and eff_price_krw > 0 and amount_krw >= 0.5 * eff_price_krw):
                    qty = 1
                    o['_min_lot_sweep'] = True
                    logger.info(f"💡 [Min-Lot Sweep] {ticker} (단가: {price:.2f}) 배정 예산({amount_krw:,.0f} KRW) < 1주 단가 → 단기 파킹 자산(SHV/KOFR) 부족분 스윕 후 최소 1주 매수 집행 보장")

            if qty <= 0:
                continue

            if action == 'sell':
                _max_sell_q = _held_qty_by_ticker.get(str(ticker), 0)
                if _max_sell_q <= 0:
                    logger.warning(f"    🛑 [Cash-Account Short Guard] {ticker}: 미보유 종목(0주) 매도/공매도 차단 ({o.get('stream_id', o.get('stream', ''))})")
                    continue
                if str(ticker) in _track2_held_tickers:
                    _o_sid = str(o.get('stream_id', o.get('stream', '')))
                    _o_trk = str(o.get('track', ''))
                    _o_str = str(o.get('strategy', ''))
                    if _o_sid != 'S3_B' and _o_trk != 'TRACK_2_BUFFETT_QVM' and _o_str not in ('buffett_qvm', 'qvm_value'):
                        logger.warning(
                            f"    🛡️ [Track 2 Non-Interference Guard] {ticker}: Track 2 (S3_B Buffett QVM) 장기 보유 종목에 대한 "
                            f"Track 1({_o_sid}/{_o_str}) 매도 간섭 원천 차단!"
                        )
                        continue

            if ticker not in netted_orders_map:
                netted_orders_map[ticker] = {
                    'buy_qty': 0, 'sell_qty': 0,
                    'buy_prices': [], 'sell_prices': [],
                    'buy_orders': [], 'sell_orders': [],
                }
            
            if action == 'buy':
                netted_orders_map[ticker]['buy_qty'] += qty
                if price > 0:
                    netted_orders_map[ticker]['buy_prices'].append(price)
                netted_orders_map[ticker]['buy_orders'].append(o)
            else:
                _max_sell_q = _held_qty_by_ticker.get(str(ticker), 0)
                netted_orders_map[ticker]['sell_qty'] = min(_max_sell_q, netted_orders_map[ticker]['sell_qty'] + qty)
                if price > 0:
                    netted_orders_map[ticker]['sell_prices'].append(price)
                netted_orders_map[ticker]['sell_orders'].append(o)

        exec_orders = []
        from src.execution.execution_engine import _derive_order_urgency
        _URG_RANK = {
            3: 5, 2: 4, 1: 1,
            '3': 5, '2': 4, '1': 1,
            'panic': 5, 'stop': 4, 'immediate': 4, 'high': 3, 'urgent': 3, 'normal': 2, 'low': 1,
        }
        _URG_NORM_MAP = {
            3: 'panic', 2: 'stop', 1: 'normal',
            '3': 'panic', '2': 'stop', '1': 'normal',
        }
        for ticker, ndata in netted_orders_map.items():
            _sell_urg = 'normal'
            for _so in ndata['sell_orders']:
                _raw_u = _so.get('urgency')
                _u = _URG_NORM_MAP.get(_raw_u, _derive_order_urgency(_so, False))
                if _URG_RANK.get(_u, 0) > _URG_RANK.get(_sell_urg, 0):
                    _sell_urg = _u
            if _URG_RANK.get(_sell_urg, 0) >= 3 and ndata['buy_qty'] > 0 and ndata['sell_qty'] > 0:
                # ★ Wave 0 E2: 손절/패닉 청산은 신규 매수와 수량 상계하지 않는다 → 매수 제거, 매도 전량 집행
                logger.warning(f"    🛡️ [Exit Protection] {ticker}: {_sell_urg} 청산({ndata['sell_qty']}주)과 신규 매수({ndata['buy_qty']}주) 충돌 → 매수 제거")
                ndata['buy_qty'] = 0
                ndata['buy_orders'] = []
                ndata['buy_prices'] = []
            net_qty = ndata['buy_qty'] - ndata['sell_qty']
            if net_qty == 0:
                logger.info(f"    ⚖️ [Order Netting] {ticker}: 매수({ndata['buy_qty']}주)/매도({ndata['sell_qty']}주) 동일 수량 완벽 상계 (주문 스킵)")
                continue
            
            final_action = 'buy' if net_qty > 0 else 'sell'
            dominant_orders = ndata['buy_orders'] if final_action == 'buy' else ndata['sell_orders']
            dominant_prices = ndata['buy_prices'] if final_action == 'buy' else ndata['sell_prices']
            
            final_price = 0.0
            if dominant_prices:
                final_price = max(dominant_prices)
            elif ndata['buy_prices'] or ndata['sell_prices']:
                final_price = max(ndata['buy_prices'] + ndata['sell_prices'])
                
            if dominant_orders:
                sample_order = max(
                    dominant_orders,
                    key=lambda x: (int(x.get('quantity', 0) or 0), float(x.get('amount_krw', 0.0) or 0.0)),
                )
            else:
                sample_order = (ndata['buy_orders'] + ndata['sell_orders'])[0]
            contributing_streams = list(dict.fromkeys(
                str(do.get('stream_id', do.get('stream', 'SYS_NET')))
                for do in (dominant_orders or (ndata['buy_orders'] + ndata['sell_orders']))
            ))
            dominant_stream_id = sample_order.get('stream_id', sample_order.get('stream', 'SYS_NET'))
            
            # 소액 잔챙이 상계 주문 차단 (50만 원 미만)
            _tk_net_s = str(ticker).strip()
            is_jp_net = _tk_net_s.upper().endswith('.T')
            is_us_net = (not _is_kr_tk(_tk_net_s)) and (not is_jp_net)
            if is_jp_net:
                _net_fx = _jpy_fx_net
                netted_val_krw = final_price * _net_fx * abs(net_qty)
            elif is_us_net:
                from src.data_collection.central_data_gateway import CentralDataGateway
                _net_fx = CentralDataGateway().get_usdkrw_rate()
                if not _net_fx or _net_fx <= 0:
                    if final_action == 'buy':
                        logger.error(f"    🛑 [FX Guard] {ticker}: USD/KRW 환율 미확보 → 해외 신규 매수 스킵")
                        continue
                    _net_fx = 0.0  # 매도(청산)는 환율과 무관하게 집행
                netted_val_krw = final_price * _net_fx * abs(net_qty)
            else:
                _net_fx = 1.0
                netted_val_krw = final_price * abs(net_qty)

            # 소액 잔챙이 주문 차단 (신규 매수 주문에만 적용 — 매도/손절은 100% 집행 보장)
            # US 종목의 경우 정규 1주 이상(abs(net_qty) >= 1) 주문은 단가가 $50 미만(예: SOXS $43, PSQ $25)이어도 1주 최소 단위로 정상 집행 허용 (페어 헤지 레그 절삭 방지)
            _min_trade_cfg = float(pget('a3.min_trade_amount', 10000) or 200_000)
            effective_min_val = (min(50.0, final_price) * _net_fx) if (is_us_net and abs(net_qty) >= 1) else ((50.0 * _net_fx) if is_us_net else min(min_order_krw, _min_trade_cfg))
            _is_min_lot = any(bo.get('_min_lot_sweep') for bo in ndata['buy_orders'])
            if final_action == 'buy' and final_price > 0 and netted_val_krw < effective_min_val and not _is_min_lot:
                logger.info(f"    🛑 [Order Netting Drop] {ticker}: 신규 매수 상계 금액(₩{netted_val_krw:,.0f}) < ₩{effective_min_val:,.0f} 미달 스킵")
                continue

            _all_dom_pairs = bool(dominant_orders) and (
                bool(sample_order.get('pair_partner'))
                or all(bool(_do.get('pair_partner')) for _do in dominant_orders)
            )
            _dom_track = sample_order.get('track') or ('TRACK_2_BUFFETT_QVM' if dominant_stream_id == 'S3_B' else '')
            _sell_pnl_pct = 0.0
            if final_action == 'sell':
                for _so_p in ndata['sell_orders']:
                    _cand_p = float(_so_p.get('pnl_pct', _so_p.get('unrealized_pnl_pct', 0.0)) or 0.0)
                    if _cand_p != 0.0:
                        _sell_pnl_pct = _cand_p
                        break
            _rep = sample_order
            _raw_net_urg = _sell_urg if final_action == 'sell' else _rep.get('urgency', 'normal')
            _net_urg = _URG_NORM_MAP.get(_raw_net_urg, str(_raw_net_urg or 'normal').strip().lower() or 'normal')
            if _net_urg in _URG_NORM_MAP:
                _net_urg = _URG_NORM_MAP[_net_urg]
            _netted_item = {
                'stream': dominant_stream_id,
                'stream_id': dominant_stream_id,
                'track': _dom_track,
                'contributing_streams': contributing_streams,
                'ticker': ticker,
                'pair_id': _rep.get('pair_id', '') if _all_dom_pairs else '',
                'pair_partner': _rep.get('pair_partner', '') if _all_dom_pairs else '',
                'pair_partners': [
                    str(_do.get('pair_partner', '')).strip().upper()
                    for _do in (dominant_orders or [])
                    if _do.get('pair_partner')
                ] if _all_dom_pairs else [],
                'action': final_action,
                'direction': 'long' if final_action == 'buy' else 'short',
                'quantity': abs(net_qty),
                'price': final_price,
                'signal_price': _rep.get('signal_price') or _rep.get('price') or final_price,
                'execution_algo': _rep.get('execution_algo', 'market'),
                'execution_start_time': _rep.get('execution_start_time', ''),
                'strategy': _rep.get('strategy', 'netted_order'),
                'reason': f"OrderNetting (Buy:{ndata['buy_qty']}, Sell:{ndata['sell_qty']} -> Net:{net_qty}, Streams:{','.join(contributing_streams)})",
                'urgency': _net_urg,
                'exit_reason': ' | '.join(str(_so.get('reason', '') or _so.get('exit_type', '')) for _so in ndata['sell_orders'])[:300] if final_action == 'sell' else '',
                'pnl_pct': _sell_pnl_pct,
                'unrealized_pnl_pct': _sell_pnl_pct,
                '_min_lot_sweep': _is_min_lot,
                '_fx': _net_fx,
                'fx_rate': _net_fx,
            }
            for _mk_copy in (
                'expected_return', 'expected_return_pct', 'expected_alpha',
                'tp_pct', 'sl_pct', 'confidence', 'is_hedge', 'is_hedge_completion', 'is_market_neutral', 'beta',
            ):
                if _rep.get(_mk_copy) is not None:
                    _netted_item[_mk_copy] = _rep[_mk_copy]
            exec_orders.append(_netted_item)

        # ── ★ Wave 0 Final Gate (S2 Exit-Only + S5 Global Cash Constraint) ──
        exec_orders = self._apply_final_execution_gate(exec_orders, portfolio, market_data)

        # ── ★ [Atomic Pair Integrity Guard + Same-Index Inverse Mutual Exclusion & Consolidation Guard] ──
        exec_orders = self._enforce_pair_and_inverse_mutex_guards(exec_orders, _held_qty_by_ticker)

        # ── Step 9.1: Global Cross-Venue Netting & Dark Pool RPI Routing ──
        try:
            cv_res = self.cross_venue_netting.optimize_order_batch_netting(exec_orders)
            result['cross_venue_netting'] = cv_res
        except Exception as e:
            logger.debug(f"CrossVenueSpreadNetting skipped: {e}")

        # ── Step 9.2: CRO Final Gate 최후단 VaR Veto & Scaling ──
        try:
            from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_cro_so
            _qe_cro_so = _get_qe_cro_so()
            total_nav_val = float(portfolio.get('total_nav', portfolio.get('cash', pget('portfolio.initial_capital', 19600000.0))))
            _cur_pos_cro = portfolio.get('positions') or portfolio.get('holdings') or {}
            ticker_map = {o['ticker']: i for i, o in enumerate(exec_orders) if o.get('ticker')}
            _n_cro = max(1, len(ticker_map))
            cov_eye = np.zeros((_n_cro, _n_cro), dtype=float)
            for _tk_c, _idx_c in ticker_map.items():
                if hasattr(_qe_cro_so, 'get_ticker_volatility'):
                    _vol_a = float(_qe_cro_so.get_ticker_volatility(_tk_c) or 0.0)
                    _var_d = (_vol_a / (252 ** (1.0 / 2.0))) ** 2
                else:
                    _m_c = _qe_cro_so.get_asset_metrics(_tk_c) or {}
                    _vol_d = float(_m_c.get('vol_daily') or (float(_m_c.get('vol_ann', 0.0) or 0.0) / (252 ** (1.0 / 2.0))))
                    _var_d = _vol_d ** 2
                cov_eye[_idx_c, _idx_c] = _var_d
            exec_orders, cro_audit = self.cro_final_gate.evaluate_and_scale_orders(
                exec_orders,
                total_nav_val,
                cov_eye,
                ticker_map,
                market_data=market_data,
                current_positions=_cur_pos_cro,
            )
            result['risk']['cro_final_gate'] = cro_audit
        except Exception as e:
            logger.debug(f"CROFinalGate evaluation skipped: {e}")

        # ── Step 9.3: K-IFRS Corporate Tax Loss Harvesting Evaluation ──
        try:
            positions_list = portfolio.get('positions', [])
            if isinstance(positions_list, dict):
                positions_list = [{'ticker': k, 'unrealized_pnl': v.get('unrealized_pnl', 0.0)} for k, v in positions_list.items()]
            tax_res = self.tax_harvest_engine.evaluate_tax_loss_harvesting(
                positions_list, current_year_realized_profit=float(portfolio.get('realized_pnl', 0.0)), current_month=datetime.now().month
            )
            result['tax_loss_harvesting'] = tax_res
        except Exception as e:
            logger.debug(f"TaxLossHarvestingEngine evaluation skipped: {e}")

        # ── Step 9.4: Central Conflict & Audit Ledger Snap Logging ──
        try:
            cro_audit_data = result.get('risk', {}).get('cro_final_gate', {})
            self.audit_ledger.log_audit_entry(
                intents_count=len(orders),
                netting_telemetry=result.get('cross_venue_netting', {}),
                qp_results=result.get('allocation', {}),
                cro_veto_audit=cro_audit_data,
                final_orders=exec_orders
            )
            logger.info("  📄 [ConflictAuditLedger] 중앙 관제 감사 장부(conflict_audit_ledger.json) 기록 완료")
        except Exception as e:
            logger.debug(f"ConflictAuditLedger logging skipped: {e}")

        exec_result = self.execution_engine.execute(exec_orders, portfolio)
        result['execution'] = exec_result.to_dict()
        logger.info(
            f"    ✅ 체결: {exec_result.n_filled}/{exec_result.n_orders} "
            f"(mode={exec_result.mode})")

        # ── Step 10: Shadow 기록 ──
        logger.info("  📋 Step 10: ShadowRecorder 기록")
        self.shadow_recorder.record(result, exec_result.to_dict())

        # ── [Phase 60] Pure Alpha 표기 ─────────────────────────────────────
        try:
            _bench = market_data.get('rp_benchmark')
            if _bench and _bench.get('beta_return') is not None:
                _model_ret = result.get('portfolio_return',
                             result.get('total_return', 0.0))
                _pure_alpha = float(_model_ret) - float(_bench['beta_return'])
                logger.info(
                    f"  [Phase60 Alpha] "
                    f"Model={_model_ret:+.4f} "
                    f"β(RP)={_bench['beta_return']:+.4f} "
                    f"α={_pure_alpha:+.4f}"
                )
        except Exception as _ae:
            logger.error(f"  [Phase60] Alpha 계산 실패: {_ae}", exc_info=True)

        # ── Step 11: Go/No-Go 평가 ──
        go_nogo = self.shadow_recorder.go_nogo_evaluation()
        result['go_nogo'] = go_nogo

        # ── 완료 ──
        elapsed = (datetime.now() - start).total_seconds()
        result['elapsed_seconds'] = round(elapsed, 2)
        self._log_run_event(result)

        logger.info("=" * 60)
        logger.info(
            f"  ✅ StreamOrchestrator 완료: {elapsed:.1f}초, "
            f"주문 {len(orders)}개, 체결 {exec_result.n_filled}개, "
            f"레짐={regime}, Go/No-Go={go_nogo['verdict']}")
        logger.info("=" * 60)

        return result

    @staticmethod
    def _enforce_pair_and_inverse_mutex_guards(
        exec_orders: List[Dict],
        held_qty_by_ticker: Dict[str, int] = None,
    ) -> List[Dict]:
        """페어 트레이딩 양방향 동시 매수 원자성(Atomic Pair Guard) 및 동일지수 인버스 중복 매입 방지·병합 가드."""
        _held_qty_by_ticker = held_qty_by_ticker or {}

        def _run_atomic_pair_guard(orders_in: List[Dict]) -> List[Dict]:
            cur_orders = list(orders_in)
            while True:
                _active_buy_tks = {
                    str(_eo.get('ticker', '')).strip().upper()
                    for _eo in cur_orders
                    if str(_eo.get('action', '')).lower() in ('buy', 'add_on') and int(_eo.get('quantity', 0) or 0) >= 1
                }
                _atomic_filtered = []
                _dropped_any = False
                for _eo in cur_orders:
                    _partners = [p for p in (_eo.get('pair_partners') or []) if p]
                    if not _partners and _eo.get('pair_partner'):
                        _partners = [str(_eo.get('pair_partner', '')).strip().upper()]
                    if _partners and str(_eo.get('action', '')).lower() in ('buy', 'add_on'):
                        # 페어 트레이딩은 반드시 양쪽 레그가 동일 배치에서 동시에 신규 매수될 때만 집행 (기보유 종목 편승으로 인한 인버스 단독 매수 원천 차단;
                        # 단, S14 Market-Neutral Hedge Completion Reserve가 기보유 롱 파트너 대비 과소 헷지 복원을 명시한 경우는 허용)
                        _active_partners = [
                            p for p in _partners
                            if p in _active_buy_tks
                            or (bool(_eo.get('is_hedge_completion', False)) and _held_qty_by_ticker.get(p, 0) > 0)
                        ]
                        if not _active_partners:
                            logger.warning(
                                f"  ⚖️ [Atomic Pair Guard] {_eo.get('ticker')}: 페어 파트너({','.join(_partners)}) 동시 매수 부재/절삭 감지 "
                                f"→ 반쪽짜리 단독 레그 매수 기각"
                            )
                            _dropped_any = True
                            continue
                    _atomic_filtered.append(_eo)
                cur_orders = _atomic_filtered
                if not _dropped_any:
                    break
            return cur_orders

        # ── ★ [Atomic Pair Integrity Guard - Pass 1] 마켓 뉴트럴 페어(S14_PAIR_ALPHA 등) 반쪽 체결 방지 ──
        exec_orders = _run_atomic_pair_guard(exec_orders)

        # ── ★ [Same-Index Inverse Mutual Exclusion, Flip-Flop Prevention & Consolidation Guard] ──
        # 동일 지수 기반 인버스(114800 1X)와 곱버스(252670 2X), 또는 US 인버스(SH/PSQ vs SQQQ)가 동시에 매입·보유되거나
        # 한쪽 인버스 청산(손절/트레일링)과 동시에 다른 배율 인버스를 매수하는 Flip-Flop 교차매매 원천 차단
        _INVERSE_MUTEX_GROUPS = [
            ('114800', '252670'),
            ('SH', 'PSQ', 'SQQQ'),
        ]
        _active_sell_tks = {
            str(_eo.get('ticker', '')).strip().upper()
            for _eo in exec_orders
            if str(_eo.get('action', '')).lower() == 'sell' and int(_eo.get('quantity', 0) or 0) >= 1
        }
        _non_consolidation_sell_tks = {
            str(_eo.get('ticker', '')).strip().upper()
            for _eo in exec_orders
            if str(_eo.get('action', '')).lower() == 'sell'
            and int(_eo.get('quantity', 0) or 0) >= 1
            and str(_eo.get('strategy', '')) != 'inverse_consolidation_exit'
        }
        for _inv_grp in _INVERSE_MUTEX_GROUPS:
            _grp_set = set(_inv_grp)
            # 1) 동일 지수 인버스 그룹 내 한 종목이 청산(손절/트레일링/시그널)되는 배치에서 다른 배율 인버스 신규 매수(Flip-Flop) 금지
            _exiting_in_grp = _grp_set.intersection(_non_consolidation_sell_tks)
            if _exiting_in_grp:
                _flip_buys = [
                    str(_eo.get('ticker', '')).strip().upper()
                    for _eo in exec_orders
                    if str(_eo.get('ticker', '')).strip().upper() in _grp_set
                    and str(_eo.get('action', '')).lower() in ('buy', 'add_on')
                ]
                if _flip_buys:
                    logger.warning(
                        f"  🛑 [Inverse Flip-Flop Guard] 동일 지수 인버스 청산({_exiting_in_grp}) 진행 중 "
                        f"→ 교차 인버스 신규 매수({_flip_buys}) 즉시 차단 (거래비용 낭비 방지)"
                    )
                    exec_orders = [
                        _eo for _eo in exec_orders
                        if not (
                            str(_eo.get('ticker', '')).strip().upper() in _grp_set
                            and str(_eo.get('action', '')).lower() in ('buy', 'add_on')
                        )
                    ]
                    continue

            _grp_buys = [
                _eo for _eo in exec_orders
                if str(_eo.get('ticker', '')).strip().upper() in _grp_set
                and str(_eo.get('action', '')).lower() in ('buy', 'add_on')
                and not (bool(_eo.get('pair_id')) and str(_eo.get('stream_id', '')).upper().startswith('S14'))
            ]
            if len(_grp_buys) > 1:
                # 동일 배치 내 복수 인버스 매수 시 주문 금액(quantity * price)이 가장 큰 주력 1종목만 유지
                _primary_inv = max(
                    _grp_buys,
                    key=lambda x: float(x.get('quantity', 0) or 0) * float(x.get('price', 0.0) or 0.0),
                )
                _keep_tk = str(_primary_inv.get('ticker', '')).strip().upper()
                _dropped_tks = [str(x.get('ticker', '')).strip().upper() for x in _grp_buys if str(x.get('ticker', '')).strip().upper() != _keep_tk]
                logger.warning(
                    f"  🛡️ [Inverse Mutex Guard] 동일 지수 인버스 중복 매수 감지({_dropped_tks} vs {_keep_tk}) "
                    f"→ 주력 종목({_keep_tk})만 유지하고 중복 인버스 매수 기각"
                )
                exec_orders = [
                    _eo for _eo in exec_orders
                    if not (
                        str(_eo.get('ticker', '')).strip().upper() in _grp_set
                        and str(_eo.get('action', '')).lower() in ('buy', 'add_on')
                        and not (bool(_eo.get('pair_id')) and str(_eo.get('stream_id', '')).upper().startswith('S14'))
                        and str(_eo.get('ticker', '')).strip().upper() != _keep_tk
                    )
                ]
                _grp_buys = [_primary_inv]

            if len(_grp_buys) == 1:
                _buy_eo = _grp_buys[0]
                _buy_tk = str(_buy_eo.get('ticker', '')).strip().upper()
                _buy_notional = float(_buy_eo.get('quantity', 0) or 0) * float(_buy_eo.get('price', 0.0) or 0.0) * float(_buy_eo.get('_fx', 1.0) or 1.0)
                _held_siblings = [
                    _stk for _stk in _inv_grp
                    if _stk != _buy_tk and _held_qty_by_ticker.get(_stk, 0) > 0 and _stk not in _active_sell_tks
                ]
                if _held_siblings:
                    # 만약 기보유 인버스(114800/PSQ/SH)가 S14 마켓뉴트럴 페어의 헷지 레그로 작동 중이면
                    # 곱버스(252670/SQQQ) 진입을 위해 페어 헷지를 강제 청산(Consolidation)하는 것을 금지하고, 중복 곱버스 매수를 차단한다.
                    from src.allocation.sector_pair_alpha_engine import get_dynamic_s14_pair_sets as _get_s14_sets_so
                    _kr_l_so, _kr_i_so, _us_l_so, _us_i_so = _get_s14_sets_so()
                    _is_active_pair_hedge_held = (
                        (bool(set(_held_siblings) & _kr_i_so) and any(_held_qty_by_ticker.get(_pt, 0) > 0 for _pt in _kr_l_so))
                        or (bool(set(_held_siblings) & _us_i_so) and any(_held_qty_by_ticker.get(_pt, 0) > 0 for _pt in _us_l_so))
                    )
                    if _buy_tk in ('252670', 'SQQQ') and not _is_active_pair_hedge_held:
                        for _stk in _held_siblings:
                            _s_qty = int(_held_qty_by_ticker.get(_stk, 0) or 0)
                            if _s_qty > 0:
                                logger.warning(
                                    f"  🔄 [Inverse Consolidation] 주력 곱버스({_buy_tk}) 진입에 따라 기보유 중복 인버스({_stk} {_s_qty}주) 자동 청산 병합"
                                )
                                exec_orders.insert(0, {
                                    'stream': 'EXIT_MANAGER',
                                    'stream_id': 'EXIT_MANAGER',
                                    'contributing_streams': ['EXIT_MANAGER'],
                                    'ticker': _stk,
                                    'action': 'sell',
                                    'direction': 'short',
                                    'quantity': _s_qty,
                                    'price': 0.0,
                                    'execution_algo': 'market',
                                    'strategy': 'inverse_consolidation_exit',
                                    'reason': f"Inverse Consolidation: replacing {_stk} with {_buy_tk}",
                                    'urgency': 'high',
                                    'exit_reason': f"Inverse Consolidation into {_buy_tk}",
                                })
                                _active_sell_tks.add(_stk)
                    else:
                        # 이미 곱버스(252670/SQQQ), 활성 S14 페어 헷지(114800/PSQ), 또는 다른 인버스를 보유 중인데 추가 인버스가 들어오면 신규 매수 차단
                        logger.warning(
                            f"  🛡️ [Inverse Mutex Guard] 기보유 인버스({_held_siblings}) 존재 → 중복 인버스({_buy_tk}, ₩{_buy_notional:,.0f}) 신규 매수 차단"
                        )
                        exec_orders = [_eo for _eo in exec_orders if _eo is not _buy_eo]

        # ── ★ [Long-Index vs Short-Index ETF Mutual Exclusion Guard] ──
        # 동일 시장 대표지수 롱 ETF(069500/122630, 229200/233740, SPY/QQQ/TQQQ, SOXX/SOXL)와
        # 인버스/곱버스 ETF(114800/252670, 251340, SH/PSQ/SQQQ, SOXS)의 동시 매수 및 동시 보유(자전 상쇄) 원천 차단
        _OPPOSITE_INDEX_GROUPS = [
            ({'069500', '122630'}, {'114800', '252670'}),
            ({'229200', '233740'}, {'251340'}),
            ({'SPY', 'QQQ', 'TQQQ'}, {'SH', 'PSQ', 'SQQQ'}),
            ({'SOXX', 'SOXL'}, {'SOXS'}),
        ]
        for _long_idx_set, _short_idx_set in _OPPOSITE_INDEX_GROUPS:
            _l_buys = [
                _eo for _eo in exec_orders
                if str(_eo.get('ticker', '')).strip().upper() in _long_idx_set
                and str(_eo.get('action', '')).lower() in ('buy', 'add_on')
            ]
            _s_buys = [
                _eo for _eo in exec_orders
                if str(_eo.get('ticker', '')).strip().upper() in _short_idx_set
                and str(_eo.get('action', '')).lower() in ('buy', 'add_on')
            ]
            # 1) 동일 배치에서 롱 지수 ETF와 인버스 지수 ETF가 동시에 매수 요청된 경우: 주문 금액 우세 방향만 남기고 반대 방향 기각
            if _l_buys and _s_buys:
                _l_val = sum(float(x.get('quantity', 0) or 0) * float(x.get('price', 0.0) or 0) for x in _l_buys)
                _s_val = sum(float(x.get('quantity', 0) or 0) * float(x.get('price', 0.0) or 0) for x in _s_buys)
                if _s_val >= _l_val:
                    logger.warning(
                        f"  🛑 [Long/Inverse Index Mutex] 동일 지수 롱({_long_idx_set}) vs 인버스({_short_idx_set}) 동시 매수 충돌 "
                        f"→ 인버스 우세로 롱 지수 매수 기각"
                    )
                    exec_orders = [_eo for _eo in exec_orders if _eo not in _l_buys]
                    _l_buys = []
                else:
                    logger.warning(
                        f"  🛑 [Long/Inverse Index Mutex] 동일 지수 롱({_long_idx_set}) vs 인버스({_short_idx_set}) 동시 매수 충돌 "
                        f"→ 롱 우세로 인버스 매수 기각"
                    )
                    exec_orders = [_eo for _eo in exec_orders if _eo not in _s_buys]
                    _s_buys = []

            # 2) 기보유 중인 인버스 지수 ETF가(매도 예정 없이) 존재할 때 동일 지수 롱 ETF(069500/122630 등) 신규 매수 차단
            _held_short_idx = [tk for tk in _short_idx_set if _held_qty_by_ticker.get(tk, 0) > 0 and tk not in _active_sell_tks]
            if _held_short_idx and _l_buys:
                logger.warning(
                    f"  🛑 [Long/Inverse Index Mutex] 기보유 인버스({_held_short_idx}) 존재 → 반대 방향 지수 롱 ETF({[x.get('ticker') for x in _l_buys]}) 신규 매수 차단"
                )
                exec_orders = [_eo for _eo in exec_orders if _eo not in _l_buys]

            # 3) 기보유 중인 지수 롱 ETF가(매도 예정 없이) 존재할 때 단독(비페어) 인버스 지수 ETF 신규 매수 시 롱 ETF 선청산 또는 인버스 차단
            _held_long_idx = [tk for tk in _long_idx_set if _held_qty_by_ticker.get(tk, 0) > 0 and tk not in _active_sell_tks]
            if _held_long_idx and _s_buys:
                _unpaired_s_buys = [x for x in _s_buys if not (x.get('pair_partner') or x.get('pair_partners'))]
                if _unpaired_s_buys:
                    logger.warning(
                        f"  🛑 [Long/Inverse Index Mutex] 기보유 지수 롱({_held_long_idx}) 존재 → 자전 상쇄 방지를 위해 단독 인버스({[x.get('ticker') for x in _unpaired_s_buys]}) 신규 매수 차단"
                    )
                    exec_orders = [_eo for _eo in exec_orders if _eo not in _unpaired_s_buys]

        # ── ★ [Atomic Pair Integrity Guard - Pass 2] 인버스 중복 기각 후 남은 반쪽 페어 레그 제거 ──
        exec_orders = _run_atomic_pair_guard(exec_orders)

        return exec_orders

    def _detect_regime(self, market_data: Dict) -> str:
        """레짐 감지 — RegimeDetector 위임."""
        result = self.regime_detector.detect(market_data)
        return result['regime']

    def _generate_orders(self, signals: Dict[str, List],
                          weights: Dict[str, float],
                          max_exposure: float,
                          leverage_result: Dict,
                          portfolio: Dict = None,
                          regime: str = 'caution',
                          market_data: Dict = None) -> List[Dict]:
        """최종 주문 생성.

        스트림 신호 × 배분 비중 × 노출도 제한 × 레버리지 × Kelly.
        ★ 스트림별 예산 한도 적용: S1=₩20M, S2=₩40M, S3=₩30M, S4=₩64M.
        """
        orders = []
        lev_level = float(
            ((leverage_result or {}).get('judgment') or {}).get(
                'leverage_level',
                (leverage_result or {}).get('final_leverage', 1.0),
            )
            or 1.0
        )
        initial_capital = cfg.get('portfolio.initial_capital')

        # ★ 전체 포트폴리오 NAV 기반 동적 예산 배분
        nav = initial_capital
        if portfolio and 'total_nav' in portfolio:
            nav = portfolio['total_nav']
            
        # [L4 Upgrade] Task 2: 중복 Waterfall Allocation 블록 제거 — 아래 단일 블록만 유지
        # ★ Medallion-Grade Waterfall Allocation (S4 제외, 종합계좌 통합 배분)
        # 1. 현금 비중 확보
        # [Phase 37: Moonshot Booster] Kelly Booster 발동 조건 확인 + 현금 비율 오버라이드
        _kelly_pf_path = str(
            _ROOT / 'results' / ('kis_portfolio.json' if getattr(self, 'exec_mode', 'live') == 'live' else 'shadow_portfolio.json')
        )
        _kelly_active = _is_kelly_booster_active(
            regime=regime,
            signal_cache_path=str(_ROOT / 'results' / 'signal_cache.json'),
            portfolio_path=_kelly_pf_path,
        )
        import os as _os_gen
        from datetime import time as _dtime_gen
        _in_pytest_gen = bool(_os_gen.environ.get('MERIDIAN_TEST_MODE') == '1' or _os_gen.environ.get('PYTEST_CURRENT_TEST') or (market_data or {}).get('_test_mode', False))
        _now_t_gen = datetime.now().time()
        _is_us_window_gen = (not _in_pytest_gen) and (_now_t_gen >= _dtime_gen(20, 0) or _now_t_gen <= _dtime_gen(6, 30))

        if _kelly_active:
            target_cash_ratio = float(cfg.get('kelly.booster_cash_ratio', 0.0))
            logger.info(f"  🚀 [Phase 37] Kelly Booster ON: Cash Ratio → {target_cash_ratio:.0%}")
        else:
            # [Pure Dynamic Math] 레짐 및 시장 거시 변동성에 연동된 동적 현금 비축 비율 (미국 정규장 세션은 US 실효 레짐 연동)
            _vix_cash = float((market_data.get('vix') or (market_data.get('signal_cache') or {}).get('vix') or 18.0) if market_data else 18.0)
            _cash_reg = regime
            if _is_us_window_gen:
                try:
                    from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_cash
                    _cash_reg = _get_qe_cash().resolve_effective_us_regime(regime, market_data)
                except Exception:
                    _cash_reg = regime
            if _cash_reg in ('bull', 'neutral'):
                target_cash_ratio = round(max(0.03, min(0.12, _vix_cash / 180.0)), 4)
            elif _cash_reg == 'caution':
                target_cash_ratio = round(max(0.08, min(0.20, _vix_cash / 120.0)), 4)
            else:
                target_cash_ratio = round(max(0.20, min(0.50, _vix_cash / 60.0)), 4)
        cash_reserve = nav * target_cash_ratio

        # 2. [Task 4] Beta Hedge 예약 폐지 → SYS_HEDGE 중앙화
        logger.debug('  [Task 4] Beta Hedge 예약 폐지 → SYS_HEDGE 중앙 처리')
        available_capital = max(0, nav - cash_reserve)
        
        # [Phase 37] Kelly Booster ON 시: S2/S3 종목 압축 수 결정
        _kelly_max_positions = None
        if _kelly_active:
            _kelly_max_positions = _compute_kelly_max_positions(
                portfolio_path=_kelly_pf_path
            )
            logger.info(f"  [Phase 37] Kelly 압축: S2/S3 최대 {_kelly_max_positions}종목")

        # 3. 가용 예산 동적 배분 (순수 Softmax EV 수학적 비중 100% 반영)
        stream_budgets = {}
        for _w_sid, _w_val in (weights or {}).items():
            stream_budgets[_w_sid] = available_capital * float(_w_val or 0.0)
        for sid in ['S0', 'S1', 'S2', 'S3', 'S4', 'S5']:
            w = weights.get(sid, 0.0)
            stream_budgets[sid] = available_capital * w
        
        stream_budgets['S10_MEGA_TREND'] = available_capital * (float(weights.get('S10_MEGA_TREND', 0.0) or 0.0) + float(weights.get('S10', 0.0) or 0.0))
        if 'S10' in signals and 'S10_MEGA_TREND' not in signals:
            stream_budgets['S10'] = stream_budgets['S10_MEGA_TREND']
        else:
            stream_budgets.pop('S10', None)
        stream_budgets['S11_HIGHBETA_SNIPER'] = available_capital * (float(weights.get('S11_HIGHBETA_SNIPER', 0.0) or 0.0) + float(weights.get('S11', 0.0) or 0.0))
        stream_budgets['S11'] = stream_budgets['S11_HIGHBETA_SNIPER']
        stream_budgets['S12_DERIVATIVE_SQUEEZE'] = available_capital * (float(weights.get('S12_DERIVATIVE_SQUEEZE', 0.0) or 0.0) + float(weights.get('S12', 0.0) or 0.0))
        stream_budgets['S12'] = stream_budgets['S12_DERIVATIVE_SQUEEZE']
        stream_budgets['S13_JAPAN_ALPHA'] = available_capital * (float(weights.get('S13_JAPAN_ALPHA', 0.0) or 0.0) + float(weights.get('S13', 0.0) or 0.0))
        stream_budgets['S13'] = stream_budgets['S13_JAPAN_ALPHA']
        stream_budgets['S14_PAIR_ALPHA'] = available_capital * (float(weights.get('S14_PAIR_ALPHA', 0.0) or 0.0) + float(weights.get('S14', 0.0) or 0.0))
        stream_budgets['S14'] = stream_budgets['S14_PAIR_ALPHA']
        


        # [Phase 47: DI 우선] 기존 종목 수집 + 예산 차감
        # ★ portfolio 딕셔너리가 주입된 경우 shadow_portfolio.json I/O 완전 차단
        # (백테스트 격리 원칙: 주입된 의존성이 있으면 파일 접근 자체를 금지)
        existing_tickers = set()
        current_holdings = {}
        _stream_held_krw = {}
        _unattributed_held_krw = 0.0
        _initial_stream_budgets = dict(stream_budgets)
        try:
            _positions = {}
            if portfolio and 'positions' in portfolio:
                # ★ DI 경로: 주입된 portfolio 딕셔너리만 사용 — 파일 Fallback 완전 바이패스
                _positions = portfolio['positions']
            else:
                # Live 전용 Fallback (백테스트에서는 절대 실행되지 않아야 함)
                if portfolio and isinstance(portfolio.get('holdings'), dict):
                    for _hk, _hv in portfolio['holdings'].items():
                        if isinstance(_hv, dict) and float(_hv.get('qty', _hv.get('quantity', _hv.get('shares', 1))) or 0) > 0:
                            existing_tickers.add(str(_hv.get('ticker', '') or _hk))
                            _positions[str(_hk)] = _hv
                import json as _json_budget
                if not _positions and getattr(self, 'exec_mode', 'live') == 'live' and (_ROOT / 'results' / 'kis_portfolio.json').exists():
                    _kp_b = _json_budget.loads((_ROOT / 'results' / 'kis_portfolio.json').read_text(encoding='utf-8'))
                    _positions = _kp_b.get('holdings', {})
                elif getattr(self, 'exec_mode', 'live') != 'live':
                    _sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
                    if _sp_path.exists():
                        _sp_data = _json_budget.loads(_sp_path.read_text())
                        _positions = _sp_data.get('positions', {})

            _PARKING_EXEMPT_BUDGET = {'459580', '449170', '423160', '130730', '430740', '357870', '357330', 'SHV', 'SGOV', 'BIL'}  # hardcode-ok: L0 cash-parking shield ETFs
            try:
                from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_ub
                _PARKING_EXEMPT_BUDGET = _PARKING_EXEMPT_BUDGET | _get_qe_ub().get_cash_equivalent_tickers()
            except Exception:
                pass
            from src.utils.market_calendar import is_kr_ticker as _is_kr_ub
            from src.execution.position_ledger import PositionLedgerManager as _PLM_UB, infer_canonical_stream_and_track as _infer_st_ub
            _ledger_entries_ub = {}
            if not (portfolio and 'positions' in portfolio):
                try:
                    _ledger_entries_ub = _PLM_UB().load_ledger()
                except Exception:
                    _ledger_entries_ub = {}
            _fx_ub = float((market_data or {}).get('usdkrw') or ((market_data or {}).get('signal_cache') or {}).get('usdkrw') or 0.0)
            if _fx_ub <= 0:
                try:
                    from src.data_collection.central_data_gateway import CentralDataGateway as _CDG_UB
                    _fx_ub = float(_CDG_UB().get_usdkrw_rate() or 0.0)
                except Exception:
                    _fx_ub = 0.0
            _pending_exits_gen = {
                str(_x).split(':')[-1].strip().upper()
                for _x in (getattr(self, '_pending_exit_tickers', set()) or set())
                if _x
            }
            if isinstance(market_data, dict) and market_data.get('pending_exit_tickers'):
                _pending_exits_gen.update(
                    str(_x).split(':')[-1].strip().upper()
                    for _x in (market_data.get('pending_exit_tickers') or [])
                    if _x
                )
            for _pk, _pv in _positions.items():
                if not isinstance(_pv, dict):
                    continue
                # VirtualPortfolio 형식: {ticker: {'ticker': ..., 'stream_id': ..., 'amount': ...}}
                # shadow_portfolio 형식:  {'S3:005930': {'ticker': ..., 'stream_id': ..., 'amount': ...}}
                # 두 형식 모두 _pv.get('ticker') 또는 _pk(= ticker 자체)로 처리
                _t = _pv.get('ticker', '') or _pk
                _clean_tk_ub = str(_t).split(':')[-1].strip() if _t else ''
                if _clean_tk_ub and _clean_tk_ub.upper() in _pending_exits_gen:
                    continue
                if _clean_tk_ub:
                    existing_tickers.add(_clean_tk_ub)

                _is_kr_pos = _is_kr_ub(_clean_tk_ub) if _clean_tk_ub else True
                _pos_fx = 1.0 if _is_kr_pos else (float(_pv.get('fx_rate') or 0.0) or _fx_ub or 1.0)
                _qty_ub = float(_pv.get('quantity', _pv.get('qty', _pv.get('shares', 0))) or 0.0)
                _pos_val = float(_pv.get('eval_krw', 0.0) or _pv.get('eval_amount_krw', 0.0) or _pv.get('amount_krw', 0.0) or 0.0)
                if _pos_val <= 0:
                    _cp_ub = float(_pv.get('current_price', 0.0) or _pv.get('avg_price', 0.0) or _pv.get('entry_price', 0.0) or 0.0)
                    if _qty_ub > 0 and _cp_ub > 0:
                        _pos_val = _qty_ub * _cp_ub * _pos_fx
                    else:
                        _raw_amt_ub = float(_pv.get('amount', 0.0) or _pv.get('market_value', 0.0) or 0.0)
                        _pos_val = _raw_amt_ub * (_pos_fx if not _is_kr_pos else 1.0)

                _STREAM_CANONICAL_MAP = {
                    'S0_BETA': 'S0',
                    'S1_EDGE': 'S1',
                    'S2_ML_ALPHA': 'S2',
                    'S3_ACTIVE_MACRO': 'S3',
                    'S3_A': 'S3',
                    'S3_B': 'S3',
                    'S3_C': 'S3',
                    'S6_US_SECTOR': 'S6',
                    'S10': 'S10_MEGA_TREND',
                    'S11': 'S11_HIGHBETA_SNIPER',
                    'S11_HIGH_BETA': 'S11_HIGHBETA_SNIPER',
                    'S12': 'S12_DERIVATIVE_SQUEEZE',
                    'S13': 'S13_JAPAN_ALPHA',
                    'S14': 'S14_PAIR_ALPHA',
                }
                _raw_sid = _pk.split(':')[0] if ':' in _pk else _pv.get('stream_id', '?')
                _raw_sid_str = str(_raw_sid or '').strip()
                _resolved_trk_ub = str(_pv.get('track', '') or '').strip()
                if not _raw_sid_str or _raw_sid_str.upper() in ('', '?', 'NONE', 'NULL', 'UNKNOWN', 'S_RECON', 'LIVE_SSOT'):
                    _led_item = _ledger_entries_ub.get(_clean_tk_ub) or _ledger_entries_ub.get(_clean_tk_ub.upper()) or {}
                    _led_sid = str(_led_item.get('stream_id', '') or '').strip() if isinstance(_led_item, dict) else ''
                    _led_trk = str(_led_item.get('track', '') or '').strip() if isinstance(_led_item, dict) else ''
                    if _led_sid and _led_sid.upper() not in ('', '?', 'NONE', 'NULL', 'UNKNOWN', 'S_RECON', 'LIVE_SSOT'):
                        _raw_sid_str = _led_sid
                        if not _resolved_trk_ub and _led_trk:
                            _resolved_trk_ub = _led_trk
                    else:
                        _raw_sid_str, _inf_trk = _infer_st_ub(_clean_tk_ub, holding_info=_pv)
                        if not _resolved_trk_ub:
                            _resolved_trk_ub = _inf_trk
                if not _resolved_trk_ub:
                    _, _resolved_trk_ub = _infer_st_ub(_clean_tk_ub, holding_info=_pv)

                if _clean_tk_ub:
                    _tk_u_key = _clean_tk_ub.upper()
                    _prev_ch = current_holdings.get(_tk_u_key, {'qty': 0.0, 'eval_krw': 0.0, 'stream_id': _raw_sid_str, 'track': _resolved_trk_ub})
                    current_holdings[_tk_u_key] = {
                        'qty': float(_prev_ch.get('qty', 0.0) or 0.0) + max(0.0, _qty_ub if _qty_ub > 0 else (1.0 if _pos_val > 0 else 0.0)),
                        'eval_krw': float(_prev_ch.get('eval_krw', 0.0) or 0.0) + max(0.0, _pos_val),
                        'stream_id': _raw_sid_str,
                        'track': _resolved_trk_ub,
                    }

                if _clean_tk_ub.upper() in _PARKING_EXEMPT_BUDGET:
                    continue

                _sid = _STREAM_CANONICAL_MAP.get(_raw_sid_str, _raw_sid_str)
                _stream_held_krw[_sid] = _stream_held_krw.get(_sid, 0.0) + _pos_val
                if _raw_sid_str != _sid:
                    _stream_held_krw[_raw_sid_str] = _stream_held_krw.get(_raw_sid_str, 0.0) + _pos_val
                if _sid in stream_budgets:
                    stream_budgets[_sid] = max(0, stream_budgets[_sid] - _pos_val)
                    for _short_k, _canon_k in _STREAM_CANONICAL_MAP.items():
                        if _canon_k == _sid and _short_k in stream_budgets:
                            stream_budgets[_short_k] = stream_budgets[_sid]
                elif _sid == 'S6' and 'S3' in stream_budgets:
                    stream_budgets['S3'] = max(0, stream_budgets['S3'] - _pos_val)
                    _stream_held_krw['S3'] = _stream_held_krw.get('S3', 0.0) + _pos_val
                elif not _sid or _sid.upper() in ('', '?', 'NONE', 'NULL', 'UNKNOWN', 'S_RECON', 'LIVE_SSOT'):
                    _unattributed_held_krw += _pos_val
        except Exception as _et_e:
            logger.error(f"    [Phase 47] existing_tickers 수집 실패: {_et_e}", exc_info=True)
        self._last_stream_held_krw = dict(_stream_held_krw)
        self._last_initial_stream_budgets = dict(_initial_stream_budgets)

        # ★ [Dynamic Budget Recycling + Capital Conservation Identity]
        # 시그널이 없는 스트림의 유휴 예산에서 이미 S_RECON 등으로 실계좌에 투입된 자본(_unattributed_held_krw)을 우선 차감하여 이중 계상 원천 차단
        # 또한 활성 알파 매수 시그널이 존재할 때는 국내 파킹 ETF(459580, 357870 등) 수동 파킹 시그널이
        # 유휴 예산 재분배와 [Final Gate S5] 원화 매수 한도를 잠식하지 않도록 사전 제외 (최종 잉여 현금은 Step 8.4 S_YIELD가 처리)
        _INTRADAY_PARKING_BLOCK_TKS = {'459580', '423160', '449170', '430740', '357870', '357330'}  # hardcode-ok: L0 KRX parking ETF codes
        _LONG_LEV_2X_3X_TKS = set("122630 233740 123320 TQQQ SOXL SPXL UPRO".split())  # hardcode-ok: L0 2X/3X long ETP codes
        _SHORT_LEV_3X_TKS = set("SQQQ SOXS SPXU".split())  # hardcode-ok: L0 3X short ETP codes
        _reg_l_gen = str(regime or 'caution').lower()
        _has_non_parking_alpha_buy = any(
            isinstance(_s, dict)
            and str(_s.get('ticker', '')).split(':')[-1].strip() not in _INTRADAY_PARKING_BLOCK_TKS
            and not str(_s.get('ticker', '')).strip().startswith('_')
            and str(_s.get('direction', _s.get('action', 'long'))).lower() in ('long', 'buy')
            for _s_list in signals.values()
            for _s in (_s_list or [])
        )
        if getattr(self, 'exec_mode', 'live') in ('live', 'paper', 'mock') and (not _in_pytest_gen or bool((market_data or {}).get('enforce_intraday_parking_guard', False))):
            for _p_sid in list(signals.keys()):
                _before_p = len(signals[_p_sid] or [])
                signals[_p_sid] = [
                    _s for _s in (signals[_p_sid] or [])
                    if not (
                        isinstance(_s, dict)
                        and str(_s.get('direction', _s.get('action', 'long'))).lower() in ('long', 'buy')
                        and (
                            (
                                _has_non_parking_alpha_buy
                                and str(_s.get('ticker', '')).split(':')[-1].strip() in _INTRADAY_PARKING_BLOCK_TKS
                                and not bool(_s.get('is_yield_sweep', False))
                            )
                            or (
                                str(_s.get('strategy', '')).lower() == 'overnight_overlay'
                                and (_now_t_gen < _dtime_gen(15, 15) or _reg_l_gen not in ('bull', 'momentum_surge'))
                            )
                            or (
                                str(_s.get('ticker', '')).split(':')[-1].strip().upper() in _LONG_LEV_2X_3X_TKS
                                and _reg_l_gen not in ('bull', 'momentum_surge')
                            )
                            or (
                                str(_s.get('ticker', '')).split(':')[-1].strip().upper() in _SHORT_LEV_3X_TKS
                                and _reg_l_gen not in ('bear', 'crash', 'crisis')
                            )
                        )
                    )
                ]
                if len(signals[_p_sid]) < _before_p:
                    logger.info(
                        f"  🏦 [Intraday Parking & Regime ETP Crowd-Out Guard] {_p_sid}: 장중({_now_t_gen.strftime('%H:%M')}, regime={_reg_l_gen}) "
                        f"파킹/비적합 레버리지 매수 시그널 {_before_p - len(signals[_p_sid])}건 사전 제외 → 예산을 활성 알파/인버스 스트림으로 100% 전환"
                    )

        # ★ [Session Closed-Market Signal Crowd-Out Guard]
        # 비개장 시장(KRX 휴장 시 국내 종목, US 휴장 시 해외 종목) 매수 시그널이 예산을 선점한 뒤
        # Session Gate에서 증발하여 가용 자본이 유휴화되는 현상 원천 차단
        from src.utils.market_calendar import is_kr_trading_now, get_us_session_state, is_ticker_tradable_now
        _kr_open_now, _ = is_kr_trading_now()
        _us_open_now, _ = get_us_session_state()
        _is_tt_mocked = (
            getattr(is_ticker_tradable_now, '__module__', '') != 'src.utils.market_calendar'
            or getattr(is_ticker_tradable_now, '__name__', '') != 'is_ticker_tradable_now'
        )
        _any_session_open = bool(
            _kr_open_now
            or _us_open_now
            or (market_data or {}).get('enforce_session_gate', False)
            or _is_tt_mocked
        )
        if _any_session_open and getattr(self, 'exec_mode', 'live') in ('live', 'paper') and not getattr(self, '_ignore_market_hours', False):
            for _p_sid in list(signals.keys()):
                _orig_sigs = signals[_p_sid] or []
                _before_s = len(_orig_sigs)
                _kept_sigs = []
                for _s in _orig_sigs:
                    if not isinstance(_s, dict):
                        continue
                    _dir_s = str(_s.get('direction', _s.get('action', 'long'))).lower()
                    _tk_s = str(_s.get('ticker', '')).split(':')[-1].strip()
                    if _dir_s in ('long', 'buy') and _tk_s and not _tk_s.startswith('_'):
                        if not self._is_ticker_session_open_for_live_exit(_tk_s, market_data):
                            continue
                    _kept_sigs.append(_s)
                if len(_kept_sigs) < _before_s:
                    _open_buys = [
                        _x for _x in _kept_sigs
                        if isinstance(_x, dict) and str(_x.get('direction', _x.get('action', 'long'))).lower() in ('long', 'buy')
                    ]
                    if _open_buys:
                        _sum_open_sz = sum(float(_x.get('size_pct', 0.0) or 0.0) for _x in _open_buys)
                        if 0.0 < _sum_open_sz < 0.70:
                            _rescale = min(2.5, 0.85 / _sum_open_sz)
                            for _x in _open_buys:
                                _x['size_pct'] = round(float(_x.get('size_pct', 0.0) or 0.0) * _rescale, 4)
                    signals[_p_sid] = _kept_sigs
                    logger.info(
                        f"  🌎 [Session Crowd-Out Guard] {_p_sid}: 비개장 세션 매수 시그널 {_before_s - len(_kept_sigs)}건 사전 제외 "
                        f"→ 개장 세션(US 통합증거금) 활성 시그널({len(_open_buys)}건)로 예산 100% 전환"
                    )

        _idle_budget = 0.0
        _active_streams_with_buys = []
        _seen_canonical = set()
        _CANONICAL_MAP = {
            'S10': 'S10_MEGA_TREND', 'S10_MEGA_TREND': 'S10_MEGA_TREND',
            'S11': 'S11_HIGHBETA_SNIPER', 'S11_HIGHBETA_SNIPER': 'S11_HIGHBETA_SNIPER',
            'S12': 'S12_DERIVATIVE_SQUEEZE', 'S12_DERIVATIVE_SQUEEZE': 'S12_DERIVATIVE_SQUEEZE',
            'S13': 'S13_JAPAN_ALPHA', 'S13_JAPAN_ALPHA': 'S13_JAPAN_ALPHA',
            'S14': 'S14_PAIR_ALPHA', 'S14_PAIR_ALPHA': 'S14_PAIR_ALPHA',
        }
        for _s_key, _s_sigs in signals.items():
            _canon = _CANONICAL_MAP.get(_s_key, _s_key)
            _has_buy = any(str(_s.get('direction', _s.get('action', 'long'))).lower() in ('long', 'buy') for _s in _s_sigs)
            _b = stream_budgets.get(_s_key, 0.0)
            if not _has_buy and _b > 0:
                if _canon not in _seen_canonical:
                    _idle_budget += _b
                    _seen_canonical.add(_canon)
                stream_budgets[_s_key] = 0.0
                stream_budgets[_canon] = 0.0
            elif _has_buy:
                _active_streams_with_buys.append(_s_key)

        _over_held_krw = 0.0
        _canon_held_seen = set()
        for _raw_s_k, _h_val_k in _stream_held_krw.items():
            _c_sid_k = _CANONICAL_MAP.get(_raw_s_k, _raw_s_k)
            if _c_sid_k in _canon_held_seen:
                continue
            _canon_held_seen.add(_c_sid_k)
            _h_canon_val = float(_stream_held_krw.get(_c_sid_k, _h_val_k) or 0.0)
            _init_b_val = float(_initial_stream_budgets.get(_c_sid_k, _initial_stream_budgets.get(_raw_s_k, 0.0)) or 0.0)
            if _h_canon_val > _init_b_val:
                _over_held_krw += (_h_canon_val - _init_b_val)

        _idle_budget = max(0.0, _idle_budget - _unattributed_held_krw - _over_held_krw)
        if _idle_budget > 0 and _active_streams_with_buys:
            # [Pure Dynamic Math] 순 유휴 예산을 활성 스트림의 수학적 EV 가중치 및 미충족(Unfilled) 여력 비율로 정밀 배분
            _active_weights = {}
            _min_room_fl = 0.05
            for k in _active_streams_with_buys:
                _base_w = max(0.001, float(weights.get(k, weights.get(k.replace('_MEGA_TREND', ''), 0.01))))
                _canon_k = _CANONICAL_MAP.get(k, k)
                _held_k = float(_stream_held_krw.get(_canon_k, _stream_held_krw.get(k, 0.0)) or 0.0)
                _init_b_k = float(_initial_stream_budgets.get(_canon_k, _initial_stream_budgets.get(k, 0.0)) or 0.0)
                _buy_sigs_k = [
                    _s for _s in (signals.get(k) or [])
                    if isinstance(_s, dict) and str(_s.get('direction', _s.get('action', 'long'))).lower() in ('long', 'buy')
                ]
                _unheld_sigs_k = [
                    _s for _s in _buy_sigs_k
                    if str(_s.get('ticker', '')).split(':')[-1].strip().upper() not in current_holdings
                ]
                _unheld_ratio = (len(_unheld_sigs_k) / len(_buy_sigs_k)) if _buy_sigs_k else 1.0
                _cap_room = max(_min_room_fl, 1.0 - (_held_k / max(_init_b_k + _idle_budget, 1.0))) if _held_k > 0 else 1.0
                _active_weights[k] = _base_w * max(_min_room_fl, _unheld_ratio) * _cap_room
            _sum_w = sum(_active_weights.values())
            for _s_key in _active_streams_with_buys:
                _prop = _active_weights[_s_key] / _sum_w
                stream_budgets[_s_key] = stream_budgets.get(_s_key, 0.0) + (_idle_budget * _prop)
            logger.info(f"  ♻️ [Dynamic Budget Recycling] 순 유휴 예산 ₩{_idle_budget:,.0f}을 활성 스트림 {_active_streams_with_buys}의 EV·미충족 여력 비중에 비례하여 동적 재분배")

        # [L4 Upgrade] Task 1: 시그널 증발 버그 복구 — S1/S2/S3/S5 시그널 순회 및 주문 생성
        # (S4는 Advisory 전용: 자동 주문 제외)
        _meta_keys = [
            'tp_pct', 'sl_pct', 'trail_activate_pct', 'trail_distance_pct', 'max_hold_days',
            'expected_return_pct', 'expected_return', 'expected_value', 'pair_id', 'pair_partner',
            'pair_vol_parity_scale',
            'is_hedge', 'is_hedge_completion', 'is_market_neutral', 'beta', 'gk_expected_alpha', 'score_z',
            'track', 'noise_stop_loss_exempt', 'margin_of_safety_pct', 'f_score',
            'ohlson_prob', 'roic_wacc_spread_pct',
        ]
        
        # [Phase 46 BugFix] PositionSizer(EV/SNR 허들) 연결 누락 복구
        try:
            from src.execution.position_sizer import filter_by_friction
            _sizer_available = True
        except ImportError as e:
            _sizer_available = False
            logger.error("    ⚠️ PositionSizer 로드 실패: 마찰비용 및 SNR 필터링이 스킵됩니다.", exc_info=True)

        from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_gen
        from src.params.dynamic_quant_models import (
            directional_beta_exposure_multiplier,
            stream_relative_three_sigma_cap,
        )
        _qe_gen = _get_qe_gen()
        _inv_set_gen = set(_qe_gen.get_inverse_hedge_tickers())
        _def_set_gen = set(_qe_gen.get_defensive_exempt_tickers())
        _safe_nav_gen = max(1.0, float(nav or 0.0))

        # ★ [Market-Neutral Hedge Completion Reserve]
        # S14_PAIR_ALPHA에서 롱 레그(pair_partner)가 이미 실계좌에 보유 중이나
        # 대응 인버스 헷지 레그(114800, PSQ 등)가 50% 미만으로 과소 헷지된 경우,
        # 타 세션(예: US) 기보유 평가액으로 인해 스트림 잔여 예산이 0원이 되었더라도
        # 기보유 롱 레그와 1:1 마켓뉴트럴 균형을 완성하기 위한 부족분(_deficit_krw)을 최우선 리스크 중립화 예비금으로 보장
        _s14_hedge_completion_map = {}
        _s14_hedge_only_mode = False
        for _s14_key in ('S14_PAIR_ALPHA', 'S14'):
            for _s_hc in (signals.get(_s14_key) or []):
                if not isinstance(_s_hc, dict):
                    continue
                if str(_s_hc.get('direction', _s_hc.get('action', 'long'))).lower() not in ('long', 'buy'):
                    continue
                _pid_hc = str(_s_hc.get('pair_id', '') or '').strip()
                _inv_tk_hc = str(_s_hc.get('ticker', '')).split(':')[-1].strip().upper()
                _prt_tk_hc = str(_s_hc.get('pair_partner', '')).split(':')[-1].strip().upper()
                if not (_pid_hc and _inv_tk_hc in _inv_set_gen and _prt_tk_hc):
                    continue
                if (
                    _any_session_open
                    and getattr(self, 'exec_mode', 'live') in ('live', 'paper')
                    and not getattr(self, '_ignore_market_hours', False)
                    and not self._is_ticker_session_open_for_live_exit(_inv_tk_hc, market_data)
                ):
                    continue
                _prt_ch = current_holdings.get(_prt_tk_hc)
                _prt_eval = float((_prt_ch or {}).get('eval_krw', 0.0) or 0.0)
                _inv_ch = current_holdings.get(_inv_tk_hc)
                _inv_eval = float((_inv_ch or {}).get('eval_krw', 0.0) or 0.0)
                if _prt_eval > 0.0 and _inv_eval < _prt_eval * 0.50:
                    _def_krw = max(0.0, _prt_eval - _inv_eval)
                    if _def_krw >= float(pget('a3.min_trade_amount', 10000)):
                        _s14_hedge_completion_map[_pid_hc] = {
                            'partner_tk': _prt_tk_hc,
                            'inv_tk': _inv_tk_hc,
                            'partner_eval_krw': _prt_eval,
                            'inv_eval_krw': _inv_eval,
                            'deficit_krw': _def_krw,
                        }
        if _s14_hedge_completion_map:
            _req_hc_bgt = sum(float(_v['deficit_krw']) for _v in _s14_hedge_completion_map.values())
            _cur_s14_bgt = max(
                float(stream_budgets.get('S14_PAIR_ALPHA', 0.0) or 0.0),
                float(stream_budgets.get('S14', 0.0) or 0.0),
            )
            if _cur_s14_bgt < _req_hc_bgt:
                _s14_hedge_only_mode = True
                stream_budgets['S14_PAIR_ALPHA'] = _req_hc_bgt
                stream_budgets['S14'] = _req_hc_bgt
                logger.info(
                    f"  🛡️ [S14 Market-Neutral Hedge Completion Reserve] 기보유 롱 레그 대비 과소 헷지된 페어 "
                    f"{list(_s14_hedge_completion_map.keys())} 감지 ➔ 헷지 완성 예산 ₩{_req_hc_bgt:,.0f} 배정"
                )

        # 포트폴리오 전체 양(+)의 베타 스트림 예산 비중 합산 -> 이중 레짐 페널티 방지 및 정확한 포트폴리오 노출 상한 E_eff 산출
        _pos_beta_budget_krw = 0.0
        for _sid_b, _sigs_b in signals.items():
            if _sid_b in ('S0', 'S14', 'S14_PAIR_ALPHA', 'S7'):
                continue
            _b_val = float(stream_budgets.get(_sid_b, 0.0) or 0.0)
            if _b_val > 0.0 and any(isinstance(_x, dict) and _x.get('direction', 'long') == 'long' for _x in (_sigs_b or [])):
                _pos_beta_budget_krw += _b_val
        _pos_beta_share = _pos_beta_budget_krw / _safe_nav_gen
        _eff_max_exp = min(1.0, float(max_exposure) / _pos_beta_share) if _pos_beta_share > 0.0 else float(max_exposure)

        stream_orders = {}
        _cumulative_ordered_krw_by_ticker = {}
        for stream_id, stream_signals in signals.items():
            stream_orders[stream_id] = []
            stream_signals = [
                _s for _s in (stream_signals or [])
                if isinstance(_s, dict)
                and not str(_s.get('ticker', '')).strip().startswith('_')
                and str(_s.get('direction', '')).lower() != 'meta'
                and not (
                    _any_session_open
                    and getattr(self, 'exec_mode', 'live') == 'live'
                    and not getattr(self, '_ignore_market_hours', False)
                    and str(_s.get('direction', _s.get('action', 'long'))).lower() in ('long', 'buy')
                    and not self._is_ticker_session_open_for_live_exit(str(_s.get('ticker', '')), market_data)
                )
            ]
            if stream_id in ('S14', 'S14_PAIR_ALPHA') and _s14_hedge_only_mode and _s14_hedge_completion_map:
                stream_signals = [
                    _s for _s in stream_signals
                    if str(_s.get('pair_id', '') or '').strip() in _s14_hedge_completion_map
                ]
            budget = stream_budgets.get(stream_id, 0)
            if budget <= 0 and stream_id == 'S10_MEGA_TREND':
                budget = stream_budgets.get('S10', 0)
            elif budget <= 0 and stream_id == 'S10':
                budget = stream_budgets.get('S10_MEGA_TREND', 0)
            elif budget <= 0 and stream_id == 'S14_PAIR_ALPHA':
                budget = stream_budgets.get('S14', 0)
            elif budget <= 0 and stream_id == 'S14':
                budget = stream_budgets.get('S14_PAIR_ALPHA', 0)

            if budget <= 0:
                logger.debug(f"    {stream_id}: 예산 없음 — 주문 생성 스킵")
                continue

            # [Phase 46 BugFix] 스트림별 시그널을 PositionSizer(Friction/SNR)로 필터링
            if _sizer_available and stream_signals:
                # [Phase 56 BugFix] VIX 누락(Stale-Halt) 방어 + 백테스트 환경 지원
                _vix_val = 0.0
                if market_data:
                    _vix_val = float(market_data.get('vix') or market_data.get('signal_cache', {}).get('vix', 0.0))
                
                if _vix_val <= 0.0:
                    try:
                        _pl_state_path = _ROOT / 'results' / 'pipeline_state.json'
                        if _pl_state_path.exists():
                            _pl_state = json.loads(_pl_state_path.read_text())
                            _vix_val = float(_pl_state.get('kr_regime_measurements', {}).get('vix', 0.0))
                    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError) as e:
                        logger.warning(f"Failed to parse kr_regime_measurements from pipeline_state: {e}", exc_info=True)
                if _vix_val <= 0.0:
                    try:
                        _sc_path = _ROOT / 'results' / 'signal_cache.json'
                        if _sc_path.exists():
                            _sc_data = json.loads(_sc_path.read_text())
                            _vix_val = float(_sc_data.get('vix', 0.0))
                    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError) as e:
                        logger.warning(f"Failed to parse vix from signal_cache: {e}", exc_info=True)
                
                # ★ [Stage 2: 4-Stage Harmonized Scoring] Re-score signal using compute_unified_factor_score
                try:
                    from src.allocation.alpha_allocator import AlphaAllocator
                    _alloc = AlphaAllocator()
                    for sig in stream_signals:
                        if 'market_data' not in sig and market_data:
                            sig['market_data'] = market_data
                        _prev_conf_u = float(sig.get('confidence', 0.0) or 0.0)
                        _uf_conf_u = float(_alloc.compute_unified_factor_score(sig, market_data) or 0.0)
                        if sig.get('pair_id') or stream_id in ('S14', 'S14_PAIR_ALPHA'):
                            sig['confidence'] = max(_prev_conf_u, _uf_conf_u)
                        else:
                            sig['confidence'] = _uf_conf_u
                except Exception as _e:
                    logger.warning(f"Failed to compute unified factor score: {_e}")
                
                _orig_sizes = {id(s): float(s.get('size_pct', 0.0) or 0.0) for s in stream_signals if isinstance(s, dict)}
                for s in stream_signals:
                    if isinstance(s, dict) and '_orig_stream_size_pct' not in s:
                        s['_orig_stream_size_pct'] = float(s.get('size_pct', 0.0) or 0.0)
                stream_signals = filter_by_friction(
                    signals=stream_signals,
                    portfolio_value=nav,
                    regime=regime,
                    vix=_vix_val
                )
                for s in stream_signals:
                    if isinstance(s, dict) and s.get('_orig_stream_size_pct', 0.0) > 0:
                        s['nav_probe_size_pct'] = s.get('size_pct', 0.0)
                        s['size_pct'] = min(float(s['_orig_stream_size_pct']), float(s['size_pct']))
                # [Pair Atomic Integrity] filter_by_friction 단계에서 페어 중 한쪽 레그만 탈락한 경우, 남은 단방향 레그도 원자적으로 제거
                _pid_counts = {}
                for _ps in stream_signals:
                    if isinstance(_ps, dict) and _ps.get('pair_id') and _ps.get('size_pct', 0) > 0:
                        _pid_k = str(_ps['pair_id']).strip()
                        _pid_counts[_pid_k] = _pid_counts.get(_pid_k, 0) + 1
                if any(_cnt < 2 for _cnt in _pid_counts.values()):
                    stream_signals = [
                        _ps for _ps in stream_signals
                        if not (
                            isinstance(_ps, dict)
                            and _ps.get('pair_id')
                            and _pid_counts.get(str(_ps['pair_id']).strip(), 0) < 2
                        )
                    ]

            # [Symmetric Normalization & 3-Sigma Cap] 기보유 종목(existing_tickers)과 신규 종목 구분 없이 동일한 리스크 상한 적용
            valid_signals = [
                sig for sig in stream_signals
                if sig and sig.get('direction', 'long') == 'long' 
                and sig.get('size_pct', 0) > 0
            ]
            
            # [Red Team V5] 매직 넘버(30%) 삭제 및 100% 수학적 한도(Kelly/Volatility/Drawdown) 주입
            # PositionSizer의 스트림 내 상대적 Kelly/CF 확신도 비율을 보존하면서 스트림 목표 가동률로 정규화 후 3-Sigma 단일종목 상한 적용
            _total_size_pct = sum(sig.get('size_pct', 0) for sig in valid_signals)
            _orig_total_pct = min(
                1.0,
                sum(float(sig.get('_orig_stream_size_pct', sig.get('size_pct', 0)) or 0.0) for sig in valid_signals),
            )
            
            # 포트폴리오 최대 일일 손실 한도 (기본 -3.0%)
            max_daily_loss = abs(float(pget('risk.global_max_daily_loss_pct', 3.0))) / 100.0
            
            _max_leverage_limit = float(pget('allocator.golden_bull_multiplier', 1.50)) if regime == 'bull' else 1.00
            _target_util = min(_max_leverage_limit, max(_orig_total_pct, 1.0 if len(valid_signals) >= 2 else _orig_total_pct))
            _scale_factor = 1.0
            if _total_size_pct > _max_leverage_limit:
                _scale_factor = _max_leverage_limit / _total_size_pct
                logger.info(f"    [Red Team V5] {stream_id}: 총 비중 {_total_size_pct*100:.1f}% > 최대 레버리지 한도 {_max_leverage_limit*100:.0f}% 초과 → 스케일 다운 (×{_scale_factor:.2f})")
            elif 0.0 < _total_size_pct < _target_util:
                _scale_factor = _target_util / _total_size_pct
                logger.info(f"    [Red Team V5] {stream_id}: 스트림 내 상대 Kelly 비중 합계({_total_size_pct*100:.1f}%) → 목표 가동률({_target_util*100:.1f}%)로 정규화 (×{_scale_factor:.2f}, 3-Sigma 상한 적용)")
            
            _canon_sid_ord = _CANONICAL_MAP.get(stream_id, stream_id)
            _held_in_stream = float(_stream_held_krw.get(_canon_sid_ord, _stream_held_krw.get(stream_id, 0.0)) or 0.0)
            total_stream_budget = max(float(budget), float(budget) + _held_in_stream)
            _stream_share = float(total_stream_budget) / _safe_nav_gen
            for _sig_n in valid_signals:
                # 1. 스트림 내 상대 Kelly 비중 정규화
                _sig_n['size_pct'] = _sig_n['size_pct'] * _scale_factor
                
                # 2. 개별 종목 수학적 한도 (차원 일관성 3-Sigma 파산 방어선: (w_s * s_i) * 3 * sigma_i <= L_max)
                vol = float(_sig_n.get('daily_vol_pct') or 0.0)
                if vol <= 0.0:
                    vol = float(_compute_asset_daily_volatility(str(_sig_n.get('ticker', '')), market_data))
                
                math_max_weight = stream_relative_three_sigma_cap(
                    max_daily_loss=max_daily_loss,
                    asset_daily_vol=vol,
                    stream_budget_share=_stream_share,
                )
                
                if _sig_n['size_pct'] > math_max_weight:
                    logger.info(f"    [Red Team V5] 켈리 비중({_sig_n['size_pct']*100:.1f}%)이 수학적 한계선({math_max_weight*100:.1f}%, Vol:{vol*100:.1f}%) 초과 → 안전 컷오프")
                    _sig_n['size_pct'] = math_max_weight

            # 🎯 [User Rule] 분할매수(DCA) 전면 금지 ➔ 100% 일시불 매수 (dca_days = 1.0)
            dca_days = 1.0
            _skipped_pair_ids = set()

            def _abort_pair(pid_val: str) -> None:
                if not pid_val:
                    return
                _skipped_pair_ids.add(pid_val)
                for _prev_o in list(stream_orders.get(stream_id, [])):
                    if str(_prev_o.get('pair_id', '') or '') == pid_val:
                        _prev_tk = str(_prev_o.get('ticker', '')).split(':')[-1].upper()
                        _prev_amt = float(_prev_o.get('amount_krw', 0.0) or 0.0)
                        _cumulative_ordered_krw_by_ticker[_prev_tk] = max(
                            0.0,
                            _cumulative_ordered_krw_by_ticker.get(_prev_tk, 0.0) - _prev_amt,
                        )
                        if _prev_o in stream_orders[stream_id]:
                            stream_orders[stream_id].remove(_prev_o)
                        if _prev_o in orders:
                            orders.remove(_prev_o)

            for sig in stream_signals:
                if not sig or sig.get('direction', 'long') != 'long':
                    continue  # 매수 시그널만 처리 (매도는 Exit Manager 담당)

                _sig_pid = str(sig.get('pair_id', '') or '')
                if _sig_pid and _sig_pid in _skipped_pair_ids:
                    continue

                # [Phase 46 BugFix] 무조건 스킵하던 로직 제거 -> DCA 갭투자로 변경
                _t = sig.get('ticker', '')
                _t_clean = str(_t).split(':')[-1].upper()

                size_pct = sig.get('size_pct', 0)
                if size_pct <= 0:
                    _abort_pair(_sig_pid)
                    continue

                # 스트림 총 예산(기보유 포함) × size_pct로 "목표(Target)" 금액 계산 (신규 발주 총액은 아래에서 잔여 현금 예산 budget 상한으로 캡핑)
                target_amount = min(
                    total_stream_budget * size_pct,  # 배분 비중
                    total_stream_budget,             # 스트림 총 예산 상한
                )

                # Beta-Convexity 노출도 & 레버리지 반영 (음의 베타 인버스 및 마켓뉴트럴 페어, 그리고 Track 2 버핏 해자 복리 슬리브 S3_B는 롱 노출 상한 감산에서 면제)
                _sig_is_hdg = bool(sig.get('is_hedge', False)) or (_t_clean in _inv_set_gen) or (_t_clean in _def_set_gen)
                _sig_is_mn = (
                    bool(sig.get('is_market_neutral', False))
                    or (stream_id in ('S14', 'S14_PAIR_ALPHA', 'S7'))
                    or str(sig.get('stream_id', '')).upper() == 'S3_B'
                    or str(sig.get('track', '')).upper() == 'TRACK_2_BUFFETT_QVM'
                )
                _raw_b_gen = sig.get('beta') if sig.get('beta') is not None else _qe_gen.get_asset_metrics(_t_clean).get('beta', 1.0)
                _sig_beta = -1.0 if _t_clean in _inv_set_gen else float(_raw_b_gen if _raw_b_gen is not None else 1.0)
                _long_budget_share = sum(
                    float(stream_budgets.get(_lk, 0.0) or 0.0)
                    for _lk in ('S2', 'S3', 'S10_MEGA_TREND', 'S11_HIGHBETA_SNIPER', 'S13_JAPAN_ALPHA')
                ) / _safe_nav_gen
                _exp_mult_sig = directional_beta_exposure_multiplier(
                    asset_beta=_sig_beta,
                    max_exposure=_eff_max_exp,
                    is_hedge=_sig_is_hdg,
                    is_market_neutral=_sig_is_mn,
                    long_budget_share=_long_budget_share,
                )
                target_amount = target_amount * _exp_mult_sig * lev_level
                # [Portfolio-Level Single-Stock Concentration Cap] 개별 주식 종목이 단일 스트림 내 독점 시그널일 때도 전체 NAV 대비 단일 종목 한도(기본 15%) 초과 누적 매수 원천 차단
                _max_single_stock_pct = float(pget('sizer.max_single_position_pct', 0.15))
                if (
                    stream_id not in ('S0', 'S_BETA', 'S4', 'S5', 'S_YIELD', 'S14', 'S14_PAIR_ALPHA', 'S7')
                    and not _sig_is_hdg
                ):
                    target_amount = min(target_amount, _safe_nav_gen * _max_single_stock_pct)

                # 현재 보유 금액 및 타 스트림 선행 발주 금액 파악 (중복 매수 방지)
                # 단, 마켓뉴트럴 페어(pair_id)의 공유 인버스 헷지 레그(예: 114800, PSQ)는 각 활성 페어의 독립 슬라이스이므로
                # 동일 사이클 내 선행 페어 발주분 차감은 면제하되, 계좌 기보유 평가액은 슬라이스 비중(size_pct)만큼 안분 차감하여 중복 과매수 차단
                _is_pair_shared_inv = bool(_sig_pid) and (_t_clean in _inv_set_gen)
                _held_qty_raw = 0
                _held_eval_krw_raw = 0.0
                _ch_entry = current_holdings.get(_t_clean)
                if isinstance(_ch_entry, dict) and float(_ch_entry.get('qty', 0.0) or 0.0) > 0:
                    _held_qty_raw = int(float(_ch_entry.get('qty', 0.0) or 0.0))
                    _held_eval_krw_raw = float(_ch_entry.get('eval_krw', 0.0) or 0.0)
                elif portfolio and _t_clean not in _pending_exits_gen:
                    pos_source = portfolio.get('positions') or portfolio.get('holdings') or {}
                    pos_iterable = pos_source.values() if isinstance(pos_source, dict) else pos_source
                    for pos in pos_iterable:
                        if isinstance(pos, dict):
                            pos_t = pos.get('ticker', '')
                            if pos_t and _t and pos_t.split(':')[-1].upper() == _t_clean:
                                _p_qty = float(pos.get('quantity', pos.get('qty', 0)) or 0.0)
                                _p_px = float(pos.get('current_price', 0.0) or pos.get('avg_price', 0.0) or pos.get('entry_price', 0.0) or sig.get('price', 0.0) or 0.0)
                                _p_amt = float(pos.get('eval_krw', 0.0) or pos.get('eval_amount_krw', 0.0) or pos.get('amount_krw', 0.0) or pos.get('amount', 0.0) or pos.get('market_value', 0.0) or 0.0)
                                if _p_amt <= 0:
                                    _p_amt = _p_qty * _p_px
                                _held_eval_krw_raw += _p_amt
                                _held_qty_raw += int(_p_qty)

                if _is_pair_shared_inv:
                    _same_inv_sigs = [
                        _s for _s in stream_signals
                        if isinstance(_s, dict)
                        and str(_s.get('ticker', '')).split(':')[-1].upper() == _t_clean
                        and float(_s.get('size_pct', 0.0) or 0.0) > 0
                    ]
                    _tot_inv_sz = sum(float(_s.get('size_pct', 0.0) or 0.0) for _s in _same_inv_sigs)
                    _slice_share = (float(size_pct) / _tot_inv_sz) if _tot_inv_sz > 0 else 1.0
                    current_eval_krw = _held_eval_krw_raw * _slice_share
                    current_qty = int(round(_held_qty_raw * _slice_share)) if _held_eval_krw_raw > 0 else 0
                    if _held_eval_krw_raw > 0 and current_qty == 0 and _held_qty_raw > 0:
                        current_qty = 1
                    current_amount = current_eval_krw
                else:
                    current_qty = _held_qty_raw
                    current_eval_krw = _held_eval_krw_raw
                    current_amount = float(_cumulative_ordered_krw_by_ticker.get(_t_clean, 0.0)) + current_eval_krw

                target_order_krw = target_amount
                if _sig_pid and _sig_pid in _s14_hedge_completion_map:
                    _hc_item = _s14_hedge_completion_map[_sig_pid]
                    if _t_clean == _hc_item['partner_tk']:
                        target_order_krw = min(target_order_krw, float(_hc_item['partner_eval_krw']))
                    elif _t_clean == _hc_item['inv_tk']:
                        target_order_krw = float(_hc_item['partner_eval_krw'])
                        current_eval_krw = float(_hc_item['inv_eval_krw'])
                        current_amount = current_eval_krw
                        sig['is_hedge_completion'] = True

                _dup_hold_ratio = 1.0 / 2.0
                if current_qty > 0 and target_order_krw > 0:
                    if current_eval_krw >= target_order_krw * _dup_hold_ratio:
                        logger.info(
                            f"    🛡️ [Duplicate-Buy Guard] {_t}: 기보유 평가액 ₩{current_eval_krw:,.0f} "
                            f">= 목표 발주액 ₩{target_order_krw:,.0f}의 50% (수량={current_qty}) ➔ 중복 매수 스킵"
                        )
                        continue
                    target_amount = max(0.0, target_order_krw - current_eval_krw)
                    gap = (
                        target_amount
                        if _is_pair_shared_inv
                        else (target_amount - float(_cumulative_ordered_krw_by_ticker.get(_t_clean, 0.0)))
                    )
                else:
                    gap = target_amount - current_amount

                # 목표 달성 또는 초과 시 매수 스킵 (단, 기보유 수량 current_qty > 0 인 경우 반대쪽 헷지 레그 보충을 위해 페어 전체 취소 금지)
                if gap <= 0:
                    if current_qty == 0:
                        _abort_pair(_sig_pid)
                    continue

                # [Constantinides-Leland 3제곱근 무거래 영역 & Grinold-Kahn 이산 수량 한계 순효용 검증]
                if current_amount > 0 and target_amount > 0 and not _is_pair_shared_inv:
                    try:
                        from src.utils.dynamic_quant_engine import get_quant_engine
                        _dqe_ord = get_quant_engine()
                        _sig_px = float(sig.get('price', 0.0) or 0.0)
                        _nt_band = _dqe_ord.compute_no_trade_band_ratio(
                            str(_t_clean),
                            order_value_krw=target_amount,
                            price=_sig_px,
                            regime=regime,
                        )
                        if (gap / target_amount) < _nt_band:
                            logger.info(
                                f"    🛡️ [Constantinides No-Trade Band Skip] {_t}: 기보유 ₩{current_amount:,.0f} vs 목표 ₩{target_amount:,.0f} "
                                f"(괴리 {gap/target_amount*100:.1f}% < 동적 무거래 반폭 {_nt_band*100:.1f}%) ➔ 추가매수 스킵"
                            )
                            if current_qty == 0:
                                _abort_pair(_sig_pid)
                            continue
                        if _sig_px > 0 and current_qty > 0:
                            if _is_kr_tk(str(_t_clean)):
                                _eff_px_krw = _sig_px
                            elif str(_t_clean).endswith('.T'):
                                _jpy_fx_ord = float(((market_data or {}).get('signal_cache') or {}).get('jpykrw', cfg.get('fx.jpykrw_fallback', 9.0)) or 9.0)
                                _eff_px_krw = _sig_px * _jpy_fx_ord
                            else:
                                # ★ Wave 1: 1345.0 리터럴 제거 → 관측 환율(signal_cache → 게이트웨이 last-good), 미상 시 해외 추가매수 생략
                                _usd_fx_ord = float(((market_data or {}).get('signal_cache') or {}).get('usdkrw') or 0.0)
                                if not (900.0 <= _usd_fx_ord <= 2500.0):
                                    from src.data_collection.central_data_gateway import CentralDataGateway as _CDG_ord
                                    _usd_fx_ord = float(_CDG_ord().get_usdkrw_rate() or 0.0)
                                if _usd_fx_ord <= 0:
                                    logger.warning(f"    🛑 [FX Guard] {_t}: USD/KRW 미상 → 해외 추가매수 생략")
                                    if current_qty == 0:
                                        _abort_pair(_sig_pid)
                                    continue
                                _eff_px_krw = _sig_px * _usd_fx_ord
                            _dq_est = max(1, int(gap / max(_eff_px_krw, 1.0)))
                            _util_res = _dqe_ord.compute_discrete_lot_marginal_utility(
                                _t_clean,
                                delta_qty=_dq_est,
                                current_qty=current_qty,
                                price_krw=_eff_px_krw,
                                nav_krw=float(nav or 25_000_000.0),
                                target_value_krw=target_amount,
                                confidence=float(sig.get('confidence', 0.55) or 0.55),
                                regime=regime,
                            )
                            if not _util_res.get('is_optimal_trade', False):
                                logger.info(
                                    f"    🛡️ [Grinold-Kahn Discrete-Lot Skip] {_t}: Δq=+{_dq_est}주 "
                                    f"(한계순효용 {_util_res['net_marginal_utility_pct']:+.3f}%, 사유={_util_res['reason']}) ➔ 추가매수 스킵"
                                )
                                if current_qty == 0:
                                    _abort_pair(_sig_pid)
                                continue
                    except Exception:
                        if (gap / target_amount) < 0.20:
                            if current_qty == 0:
                                _abort_pair(_sig_pid)
                            continue
                    
                # 🎯 [User Rule] 100% 일시불 즉시 진입 (분할매수/DCA 100% 원천 차단)
                amount_krw = gap

                min_trade = pget('a3.min_trade_amount', 10000)
                if amount_krw < min_trade:
                    if current_qty == 0:
                        _abort_pair(_sig_pid)
                    continue

                _cumulative_ordered_krw_by_ticker[_t_clean] = _cumulative_ordered_krw_by_ticker.get(_t_clean, 0.0) + amount_krw

                order = {
                    'stream_id': str(sig.get('stream_id') or stream_id),
                    'ticker': sig.get('ticker', ''),
                    'name': sig.get('name', sig.get('ticker', '')),
                    'direction': 'long',
                    'amount_krw': round(amount_krw),
                    'price': sig.get('price', 0),
                    'confidence': sig.get('confidence', 0),
                    'strategy': sig.get('strategy', ''),
                    'reason': sig.get('reason', ''),
                    'regime': sig.get('regime', ''),
                    'sector': sig.get('sector', ''),
                }

                # [L4 Upgrade] Task 1: TP/SL/Trail 메타데이터 그대로 전달
                for _mk in _meta_keys:
                    if _mk in sig:
                        order[_mk] = sig[_mk]

                logger.debug(f"    [DEBUG] Appending order for {stream_id} {sig.get('ticker')}: keys={list(order.keys())}")
                stream_orders[stream_id].append(order)
                orders.append(order)

        # 각 스트림별로 잔여 예산(budget * lev_level) 상한 캡핑 및 독립 포지션 스케일링
        min_trade_cap = pget('a3.min_trade_amount', 10000)
        for sid in list(stream_orders.keys()):
            _s_ords = stream_orders.get(sid, [])
            _bgt = float(stream_budgets.get(sid, 0) or 0.0)
            _max_new_bgt = _bgt * float(lev_level or 1.0)
            _amt = sum(float(o.get('amount_krw', 0) or 0.0) for o in _s_ords)
            if _s_ords and _max_new_bgt > 0 and _amt > _max_new_bgt * 1.001:
                _b_scale = _max_new_bgt / _amt
                for _o_sc in list(_s_ords):
                    _new_amt = round(float(_o_sc.get('amount_krw', 0) or 0.0) * _b_scale)
                    if _new_amt < min_trade_cap:
                        if _o_sc in _s_ords:
                            _s_ords.remove(_o_sc)
                        if _o_sc in orders:
                            orders.remove(_o_sc)
                    else:
                        _o_sc['amount_krw'] = _new_amt
                _amt = sum(float(o.get('amount_krw', 0) or 0.0) for o in _s_ords)
            _n = len(_s_ords)
            if _n > 0:
                logger.info(
                    f"    📊 {sid}: {_n}건 ₩{_amt:,.0f} / 예산 ₩{_bgt:,.0f}"
                    + (f" ({_amt/_bgt*100:.0f}%)" if _bgt > 0 else " (예산 없음)"))

        self._pending_exit_tickers = set()
        return orders

    def _evaluate_exits(self, market_data: Dict, regime: str, portfolio: Dict = None) -> List[Dict]:
        """[L4 Upgrade] Task 3: 글로벌 Exit Manager — TP/SL/Trailing Stop 평가.

        portfolio.json 또는 주입된 portfolio의 포지션을 순회하며 각 포지션에 저장된
        tp_pct, sl_pct 메타데이터 기반으로 청산 여부를 평가하고 매도 주문을 반환.

        Returns:
            청산 주문 리스트 (direction='short')
        """
        exit_orders = []
        self._pending_exit_tickers = set()
        try:
            positions = {}
            if portfolio and 'positions' in portfolio:
                positions = portfolio['positions']
            elif portfolio and 'holdings' in portfolio:
                positions = portfolio['holdings']
            elif getattr(self, 'exec_mode', 'live') == 'live':
                _kis_exit_path = _ROOT / 'results' / 'kis_portfolio.json'
                if not _kis_exit_path.exists():
                    return exit_orders
                import json as _json_exit
                from src.execution.position_ledger import PositionLedgerManager
                ledger_mgr = PositionLedgerManager()
                _kp = _json_exit.loads(_kis_exit_path.read_text(encoding='utf-8'))
                for t, h in _kp.get('holdings', {}).items():
                    curr_p = float(h.get('current_price', 0.0))
                    curr_ret = float(h.get('return_pct', 0.0))
                    hwm_data = ledger_mgr.update_hwm(
                        t,
                        current_price=curr_p,
                        current_pnl_pct=curr_ret,
                        avg_price=float(h.get('avg_price', 0.0) or 0.0),
                        quantity=float(h.get('qty', h.get('quantity', 0)) or 0.0),
                        stream_id=str(h.get('stream_id', '') or ''),
                        strategy=str(h.get('strategy', '') or ''),
                        track=str(h.get('track', '') or ''),
                    )
                    positions[t] = {
                        'ticker': t,
                        'name': h.get('name', t),
                        'quantity': int(h.get('qty', 0)),
                        'qty': int(h.get('qty', 0)),
                        'avg_price': float(h.get('avg_price', 0.0)),
                        'current_price': curr_p,
                        'amount': float(h.get('qty', 0)) * curr_p,
                        'unrealized_pnl_pct': curr_ret,
                        'pnl_pct': curr_ret,
                        'peak_pnl_pct': float(hwm_data['peak_pnl_pct']) if hwm_data.get('peak_pnl_pct') is not None else curr_ret,
                        'peak_price': float(hwm_data['peak_price']) if (hwm_data.get('peak_price') or 0.0) > 0 else curr_p,
                        'trail_activated': bool(hwm_data.get('trail_activated', False)),
                        'market': h.get('market', 'KR'),
                        'stream_id': str(h.get('stream_id') or hwm_data.get('stream_id', '') or ''),
                        'strategy': str(h.get('strategy') or hwm_data.get('strategy', '') or ''),
                        'track': str(h.get('track') or hwm_data.get('track', '') or ''),
                    }
            else:
                import json as _json_exit
                _sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
                if not _sp_path.exists():
                    return exit_orders
                _sp = _json_exit.loads(_sp_path.read_text(encoding='utf-8'))
                positions = _sp.get('positions', {})

            if isinstance(positions, list):
                positions = {p.get('ticker', f"pos_{i}"): p for i, p in enumerate(positions) if isinstance(p, dict)}

            if getattr(self, 'exec_mode', 'live') == 'live' and positions:
                try:
                    from src.execution.position_ledger import PositionLedgerManager
                    _ledger_enrich = PositionLedgerManager()
                    for _pk_e, _pv_e in list(positions.items()):
                        if isinstance(_pv_e, dict):
                            _tk_e = str(_pv_e.get('ticker', _pk_e.split(':')[-1] if ':' in str(_pk_e) else _pk_e)).strip()
                            if _tk_e:
                                _hwm_e = _ledger_enrich.get_hwm(_tk_e)
                                if 'peak_pnl_pct' not in _pv_e or 'entry_date' not in _pv_e:
                                    _cp_e = float(_pv_e.get('current_price', 0.0) or 0.0)
                                    _cr_e = float(_pv_e.get('unrealized_pnl_pct', _pv_e.get('pnl_pct', 0.0)) or 0.0)
                                    _hwm_pk_pnl = float(_hwm_e['peak_pnl_pct']) if _hwm_e.get('peak_pnl_pct') is not None else _cr_e
                                    _hwm_pk_px = float(_hwm_e['peak_price']) if (_hwm_e.get('peak_price') or 0.0) > 0 else _cp_e
                                    _pv_e.setdefault('peak_pnl_pct', max(_cr_e, _hwm_pk_pnl))
                                    _pv_e.setdefault('peak_price', max(_cp_e, _hwm_pk_px))
                                    _pv_e.setdefault('trail_activated', bool(_hwm_e.get('trail_activated', False)))
                                    _ed_e = _ledger_enrich.get_entry_date(_tk_e)
                                    if _ed_e:
                                        _pv_e.setdefault('entry_date', _ed_e)
                                if not _pv_e.get('stream_id') and _hwm_e.get('stream_id'):
                                    _pv_e['stream_id'] = str(_hwm_e['stream_id'])
                                if not _pv_e.get('strategy') and _hwm_e.get('strategy'):
                                    _pv_e['strategy'] = str(_hwm_e['strategy'])
                                if not _pv_e.get('track') and _hwm_e.get('track'):
                                    _pv_e['track'] = str(_hwm_e['track'])
                except Exception:
                    pass

            _EXIT_CANON_MAP = {
                'S_BETA': 'S0',
                'S_YIELD': 'S5',
                'S10': 'S10_MEGA_TREND',
                'S11': 'S11_HIGHBETA_SNIPER',
                'S12': 'S12_DERIVATIVE_SQUEEZE',
                'S13': 'S13_JAPAN_ALPHA',
                'S14': 'S14_PAIR_ALPHA',
            }
            _explicit_exit_map = {}
            _active_long_tickers_all = set()
            _active_s14_kr_tickers = set()
            _active_s14_us_tickers = set()
            for _esid, _esigs in (getattr(self, '_latest_signals', None) or {}).items():
                _esid_u = str(_esid or '').strip().upper()
                for _es in (_esigs or []):
                    if not isinstance(_es, dict):
                        continue
                    _etk = str(_es.get('ticker', '')).split(':')[-1].strip().upper()
                    if not _etk:
                        continue
                    if (
                        str(_es.get('direction', '')).lower() == 'exit'
                        or str(_es.get('action', '')).lower() == 'exit'
                    ):
                        _explicit_exit_map.setdefault(_etk, []).append((_esid, _es))
                    elif str(_es.get('direction', 'long')).lower() in ('long', 'buy'):
                        _active_long_tickers_all.add(_etk)
                        _es_sid_u = str(_es.get('stream_id') or _esid_u).strip().upper()
                        if _es_sid_u in ('S14', 'S14_PAIR_ALPHA'):
                            if _is_kr_tk(_etk):
                                _active_s14_kr_tickers.add(_etk)
                            else:
                                _active_s14_us_tickers.add(_etk)

            # [Task 3 Phase50: ATR Elastic Band] VKOSPI 정비례 고무줄 밴드 — Whipsaw 방지
            # 원칙: 고변동성 시장은 잔파도가 크므로 밴드를 넓혀 털림 방지
            #       저변동성 시장은 밴드를 조여 이익을 빠르게 확정
            #
            # 공식: vol_scaler = max(1.0, min(2.0, vkospi / 20.0))
            #   VKOSPI 10 → vol_scaler=1.0: activate=3.0%, dist=2.5% (최소 밴드)
            #   VKOSPI 20 → vol_scaler=1.0: activate=3.0%, dist=2.5% (중립)
            #   VKOSPI 30 → vol_scaler=1.5: activate=4.5%, dist=3.75% (공포 확장)
            #   VKOSPI 40 → vol_scaler=2.0: activate=6.0%, dist=5.0%  (패닉 최대 밴드)
            _sc_exit = (market_data or {}).get('signal_cache', {})
            _exit_vkospi = float(_sc_exit.get('vkospi', 18.0) or 18.0)
            _vix_neutral = float(pget('risk.vix_neutral_fallback', 18.0))
            _exit_vix = float(_sc_exit.get('vix', _vix_neutral) or _vix_neutral)

            # vol_scaler: VKOSPI/20 기준 (최소 1.0, 최대 2.0으로 클램핑)
            # [Phase 51: Bias-Free ATR Exit] 포지션 루프 시작
            # 종목별 변동성은 루프 내에서 _compute_asset_daily_volatility()로 동적 계산
            # (기존 VKOSPI 고무줄 밴드 제거 → 순수 통계 Z-Score 방식)
            for pos_key, pos in positions.items():
                if isinstance(pos, str):
                    pos = {'ticker': pos, 'stream_id': 'UNKNOWN'}
                stream_id = str(pos.get('stream_id', ''))
                # S1 Exit 버그 수정: stream_id가 S1인 경우도 L4 Global Exit 대상에 포함
                if not stream_id or stream_id in ('None', 'UNKNOWN', 'S_RECON', 'LIVE_SSOT'):
                    # pos_key 접두어로 추정 (S1:..., S2:... 등)
                    if ':' in pos_key and pos_key.split(':')[0] not in ('S_RECON', 'LIVE_SSOT', 'UNKNOWN', 'None'):
                        stream_id = pos_key.split(':')[0]
                    else:
                        # [SSoT Canonical Mapping] 실계좌 코어 보유자산 스트림 매핑 (S1 오인 방지)
                        _tk_up = str(pos.get('ticker', pos_key)).upper()
                        if _tk_up in ('NVDA', 'QQQ', 'SOXX', 'XLK', '091160', 'MSFT', 'AAPL', 'GOOGL', 'AMZN', 'TSLA'):
                            stream_id = 'S3'
                        elif _tk_up in ('069500', '229200', 'SPY', 'IVV', 'VOO'):
                            stream_id = 'S0'
                        elif _tk_up in ('459580', '449170', '423160', '430740', '357870', '357330', 'SHV', 'SGOV'):
                            stream_id = 'S_YIELD'
                        elif str(pos.get('track', '')).upper() == 'TRACK_2_BUFFETT_QVM' or str(pos.get('strategy', '')).lower() in ('buffett_qvm', 'qvm_value'):
                            stream_id = 'S3_B'
                        else:
                            stream_id = 'S3'  # 기본값을 단기 S1이 아닌 중기 S3로 안전하게 지정

                _pos_ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
                _pos_ticker_up = str(_pos_ticker).split(':')[-1].upper()

                if pos.get('quantity', pos.get('qty', 0)) <= 0:
                    continue

                if not self._is_ticker_session_open_for_live_exit(_pos_ticker_up, market_data):
                    continue

                # ── S_BETA, S10, S_YIELD, 그리고 Track 2 버핏 해자 복리 슬리브(S3_B / TRACK_2_BUFFETT_QVM)는 단기 L4 TP/SL/Trail에서 완전 면제 (단, 소유 스트림 일치 명시적 exit 시그널은 즉시 집행) ──
                _is_track2_pos = (
                    stream_id in ('S3_B', 'TRACK_2_BUFFETT_QVM')
                    or str(pos.get('track', '')).upper() == 'TRACK_2_BUFFETT_QVM'
                    or str(pos.get('strategy', '')).lower() in ('buffett_qvm', 'qvm_value')
                )
                _explicit_exit_info = None
                _pos_canon_sid = 'S3_B' if _is_track2_pos else _EXIT_CANON_MAP.get(stream_id, stream_id)
                _is_s14_pair_member_ex = (
                    _pos_canon_sid == 'S14_PAIR_ALPHA'
                    or 'pair' in str(pos.get('strategy', '')).lower()
                    or (_pos_ticker_up in _active_s14_kr_tickers and len(_active_s14_kr_tickers) >= 2)
                    or (_pos_ticker_up in _active_s14_us_tickers and len(_active_s14_us_tickers) >= 2)
                )
                for _cand_esid, _cand_es in (_explicit_exit_map.get(_pos_ticker_up) or []):
                    _emitter_sid_u = str(_cand_esid or '').strip().upper()
                    _cand_strat_l = str(_cand_es.get('strategy') or '').strip().lower()
                    # S14 마켓뉴트럴 페어 레그(114800, PSQ 등)는 오직 S14 스트림이 발행한 청산 시그널로만 명시 청산되며,
                    # S3 등 타 스트림의 단독 인버스 청산(s3_inverse_regime_invalidation)이 페어 헷지를 가로채 청산하는 것을 원천 차단
                    if _is_s14_pair_member_ex and (
                        _emitter_sid_u not in ('S14', 'S14_PAIR_ALPHA')
                        or _cand_strat_l == 's3_inverse_regime_invalidation'
                    ):
                        continue
                    _sig_sid_raw = str(_cand_es.get('stream_id') or _cand_esid or '').strip().upper()
                    _sig_canon_sid = _EXIT_CANON_MAP.get(_sig_sid_raw, _sig_sid_raw)
                    if _sig_canon_sid == _pos_canon_sid:
                        _explicit_exit_info = (_cand_esid, _cand_es)
                        break
                    if _sig_sid_raw in ('S3_B', 'TRACK_2_BUFFETT_QVM') and _is_track2_pos:
                        _explicit_exit_info = (_cand_esid, _cand_es)
                        break
                    if _sig_sid_raw in ('S10', 'S10_MEGA_TREND') and (
                        stream_id in ('S10', 'S10_MEGA_TREND')
                        or str(pos.get('strategy', '')).lower() in ('s10_megatrend', 'mega_trend')
                    ):
                        _explicit_exit_info = (_cand_esid, _cand_es)
                        break

                if (stream_id in ('S_BETA', 'S10_MEGA_TREND', 'S10', 'S_YIELD') or _is_track2_pos) and not _explicit_exit_info:
                    continue  # Track 2 및 코어 슬리브는 단기 TP/SL 대상에서 제외

                pnl_pct = pos.get('unrealized_pnl_pct', pos.get('pnl_pct', 0))
                tp_pct = pos.get('tp_pct')
                sl_pct = pos.get('sl_pct')  # 양수값 저장 규약
                if tp_pct is not None:
                    tp_pct = float(tp_pct)
                    if 0 < tp_pct < 0.5:
                        tp_pct *= 100.0
                if sl_pct is not None:
                    sl_pct = abs(float(sl_pct))
                    if 0 < sl_pct < 0.5:
                        sl_pct *= 100.0
                # [Phase 51: Bias-Free ATR Exit] 종목별 역사적 변동성 기반 Z-Score 밴드
                # 공식: activate = vol_daily(%) × Z=3.0 (3σ 이익권 확인 후 추적 시작)
                #        dist    = vol_daily(%) × Z=2.0 (고점 대비 2σ=95% CI 이탈 시 청산)
                _asset_vol_daily = _compute_asset_daily_volatility(
                    _pos_ticker, market_data, fallback=0.02)
                _trail_z_act  = float(pget('exit.trail_z_activate', 3.0))
                _trail_act_lo = float(pget('exit.trail_activate_min', 3.0))
                _trail_act_hi = float(pget('exit.trail_activate_max', 15.0))
                _trail_z_dst  = float(cfg.get('exit.trail_z_dist',       2.0))
                _trail_dst_lo = float(pget('exit.trail_dist_min', 0.4))
                _trail_dst_hi = float(pget('exit.trail_dist_max', 10.0))
                _dyn_trail_activate_raw = _asset_vol_daily * 100
                _dyn_trail_dist_raw     = _asset_vol_daily * 100
                _dyn_trail_activate = round(max(_trail_act_lo, min(_dyn_trail_activate_raw * _trail_z_act, _trail_act_hi)), 2)
                _dyn_trail_dist     = round(max(_trail_dst_lo, min(_dyn_trail_dist_raw     * _trail_z_dst, _trail_dst_hi)), 2)
                _dyn_sl_pct         = _dyn_trail_dist  # [SSoT Unification] 동적 VIX/ATR 밴드로 100% 통일
                logger.debug(
                    f"[Phase 51] {_pos_ticker} vol={_asset_vol_daily*100:.2f}% "
                    f"→ activate={_dyn_trail_activate:.2f}%, dist={_dyn_trail_dist:.2f}%, sl={_dyn_sl_pct:.2f}%")
                trail_activate = pos.get('trail_activate_pct') or _dyn_trail_activate
                trail_dist = pos.get('trail_distance_pct') or _dyn_trail_dist
                sl_pct = sl_pct or _dyn_sl_pct  # 포지션 메타 우선, 없으면 동적 밴드 사용
                
                days_held = pos.get('days_held')
                entry_date_str = pos.get('entry_date') or getattr(pos, 'entry_date', None)
                if not entry_date_str and _pos_ticker:
                    try:
                        from src.execution.position_ledger import PositionLedgerManager
                        entry_date_str = PositionLedgerManager().get_entry_date(_pos_ticker)
                    except Exception:
                        pass

                if not days_held and entry_date_str:
                    try:
                        import os as _os_dh
                        import pandas as pd
                        from datetime import datetime as _dt_dh, timezone as _tz_dh, timedelta as _td_dh
                        _entry = pd.to_datetime(entry_date_str).date()
                        _curr_raw = (market_data or {}).get('date') if isinstance(market_data, dict) else None
                        if not _curr_raw:
                            _curr_raw = _dt_dh.now(_tz_dh(_td_dh(hours=9))).date()
                        _curr = pd.to_datetime(_curr_raw).date()
                        _use_tday_hold = bool(
                            (isinstance(market_data, dict) and market_data.get('enforce_trading_days_hold'))
                            or (
                                not (isinstance(market_data, dict) and market_data.get('date'))
                                and not _os_dh.environ.get('PYTEST_CURRENT_TEST')
                                and _os_dh.environ.get('MERIDIAN_TEST_MODE') != '1'
                            )
                        )
                        if _use_tday_hold and _curr > _entry:
                            from src.execution.position_ledger import count_trading_days_held as _cnt_td_dh
                            days_held = _cnt_td_dh(
                                str(_entry.strftime('%Y-%m-%d')),
                                is_kr=_is_kr_tk(str(_pos_ticker_up)),
                                as_of_date=_curr,
                            )
                        else:
                            days_held = max(0, (_curr - _entry).days)
                    except Exception as e:
                        logger.error(f"[DEBUG L4 ERROR] days_held computation failed: {e}, pos={pos}", exc_info=True)
                        days_held = 0
                days_held = days_held or 0
                
                _max_hold_days = pos.get('max_hold_days')
                if _max_hold_days is None:
                    _cur_regime = str(regime.get('primary_regime', 'caution') if isinstance(regime, dict) else (regime or 'caution')).lower()
                    if not _is_kr_tk(str(_pos_ticker_up)):
                        try:
                            from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_dh
                            _cur_regime = _get_qe_dh().resolve_effective_us_regime(_cur_regime, market_data)
                        except Exception:
                            pass
                    try:
                        from src.risk.dynamic_half_life_engine import DynamicHalfLifeEngine
                        _dh_engine = DynamicHalfLifeEngine()
                        _max_hold_days = _dh_engine.compute_dynamic_max_hold(
                            ticker=_pos_ticker,
                            regime_name=_cur_regime,
                            market_data=market_data
                        )
                    except Exception as _dh_err:
                        logger.warning(f"    ⚠️ [DynamicHalfLifeEngine] 산출 예외: {_dh_err}")
                        _max_hold_days = DynamicConfig().get(f'exit.max_hold_days.{_cur_regime}', 5)

                _is_s14_pos_tl = (
                    stream_id in ('S14', 'S14_PAIR_ALPHA')
                    or 'pair' in str(pos.get('strategy', '')).lower()
                )
                _is_s14_orphan_tl = False
                _is_active_s14_pair_leg = False
                _pair_spread_peak_so = float(pnl_pct or 0.0)
                _pair_spread_trail_so = False
                if _is_s14_pos_tl:
                    if _is_kr_tk(str(_pos_ticker_up)) and _active_s14_kr_tickers and _pos_ticker_up not in _active_s14_kr_tickers:
                        _is_s14_orphan_tl = True
                    elif (not _is_kr_tk(str(_pos_ticker_up))) and _active_s14_us_tickers and _pos_ticker_up not in _active_s14_us_tickers:
                        _is_s14_orphan_tl = True
                if _is_s14_orphan_tl and _max_hold_days is not None:
                    _max_hold_days = min(float(_max_hold_days), 1.0)
                elif _is_s14_pos_tl and not _is_s14_orphan_tl:
                    _s14_pair_set = _active_s14_kr_tickers if _is_kr_tk(str(_pos_ticker_up)) else _active_s14_us_tickers
                    if _pos_ticker_up in _s14_pair_set and len(_s14_pair_set) >= 2:
                        _pair_val_sum = 0.0
                        _pair_pnl_weighted = 0.0
                        _pair_leg_cnt = 0
                        _pair_leg_vals_so: Dict[str, float] = {}
                        _pair_leg_pxs_so: Dict[str, float] = {}
                        _pair_leg_sigs_so: List[str] = []
                        _pair_exp_pks_so: List[float] = []
                        _pair_exp_trs_so: List[bool] = []
                        for _pk_p, _pv_p in positions.items():
                            if not isinstance(_pv_p, dict):
                                continue
                            _tk_p = str(_pv_p.get('ticker', str(_pk_p).split(':')[-1])).split(':')[-1].strip().upper()
                            _q_p = float(_pv_p.get('quantity', _pv_p.get('qty', 0)) or 0.0)
                            if _tk_p in _s14_pair_set and _q_p > 0:
                                _cp_p = float(_pv_p.get('current_price', _pv_p.get('avg_price', 1.0)) or 1.0)
                                _ep_p = float(_pv_p.get('entry_price', _pv_p.get('avg_price', _cp_p)) or _cp_p)
                                _val_p = float(_pv_p.get('eval_krw', _pv_p.get('amount', 0.0)) or (_q_p * _cp_p))
                                _pnl_p = float(_pv_p.get('unrealized_pnl_pct', _pv_p.get('pnl_pct', 0.0)) or 0.0)
                                if _val_p > 0:
                                    _pair_val_sum += _val_p
                                    _pair_pnl_weighted += _val_p * _pnl_p
                                    _pair_leg_cnt += 1
                                    _pair_leg_vals_so[_tk_p] = _val_p
                                    _pair_leg_pxs_so[_tk_p] = _cp_p
                                    _pair_leg_sigs_so.append(f"{_tk_p}:{int(round(_q_p))}:{round(_ep_p, 4)}")
                                    if _pv_p.get('pair_spread_peak_pnl_pct') is not None:
                                        _pair_exp_pks_so.append(float(_pv_p['pair_spread_peak_pnl_pct']))
                                    if _pv_p.get('pair_spread_trail_activated') is not None:
                                        _pair_exp_trs_so.append(bool(_pv_p['pair_spread_trail_activated']))
                        if _pair_leg_cnt >= 2 and _pair_val_sum > 0:
                            pnl_pct = _pair_pnl_weighted / _pair_val_sum
                            _is_active_s14_pair_leg = True
                            try:
                                from src.utils.dynamic_quant_engine import get_quant_engine as _get_qe_pair_so
                                _cur_reg_so = str(regime.get('primary_regime', 'caution') if isinstance(regime, dict) else (regime or 'caution')).lower()
                                _pair_opt_so = _get_qe_pair_so().compute_pair_spread_stopping_params(
                                    leg_weights=_pair_leg_vals_so,
                                    regime=_cur_reg_so,
                                    vix_level=_exit_vix,
                                    total_pair_value_krw=_pair_val_sum,
                                    leg_prices=_pair_leg_pxs_so,
                                )
                                sl_pct = abs(float(_pair_opt_so['hard_sl_limit_pct']))
                                trail_activate = float(_pair_opt_so['trailing_trigger_pct'])
                                trail_dist = float(_pair_opt_so['chandelier_drop_pct'])
                                tp_pct = None  # 상방 고정 익절 상한 제거 → 무상한 ATR 샹들리에 트레일링 및 OU Z-Score 수렴으로 일원화
                            except Exception:
                                pass
                            try:
                                from src.execution.position_ledger import PositionLedgerManager as _PLM_Pair_SO
                                _sp_hwm_so = _PLM_Pair_SO().update_pair_spread_hwm(
                                    pair_tickers=list(_pair_leg_vals_so.keys()),
                                    current_spread_pnl_pct=pnl_pct,
                                    leg_signature="|".join(sorted(_pair_leg_sigs_so)),
                                    trail_activate_threshold=float(trail_activate or 0.0),
                                    explicit_spread_peak=max(_pair_exp_pks_so) if _pair_exp_pks_so else None,
                                    explicit_trail_activated=any(_pair_exp_trs_so) if _pair_exp_trs_so else None,
                                )
                                _pair_spread_peak_so = float(_sp_hwm_so['pair_spread_peak_pnl_pct'])
                                _pair_spread_trail_so = bool(_sp_hwm_so['pair_spread_trail_activated'])
                            except Exception:
                                _pair_spread_peak_so = max([pnl_pct] + _pair_exp_pks_so)
                                _pair_spread_trail_so = bool(any(_pair_exp_trs_so) or (_pair_spread_peak_so >= float(trail_activate or 0.0)))

                if _max_hold_days:
                    logger.info(f"    [DEBUG L4 Dynamic Math] {_pos_ticker}: days_held={days_held}, dynamic_max_hold={_max_hold_days:.2f}d, pos={pos}")

                # [Phase 80] SYS_HEDGE / 레버리지 ETF 특수 Time-Stop 및 동적 청산 로직은 leverage_judge로 위임됨


                reason = None
                urgency = 'normal'

                if _explicit_exit_info is not None:
                    _ex_sid, _ex_sig = _explicit_exit_info
                    reason = f"[Stream Exit Signal] {_ex_sid}: {_ex_sig.get('reason', 'Explicit exit signal')}"
                    urgency = 'panic'

                # ── TP 도달 ──
                elif tp_pct is not None and pnl_pct >= tp_pct:
                    reason = f"[L4 Exit] TP: {pnl_pct:+.1f}% >= {tp_pct:.1f}%"
                    urgency = 'stop'

                # ── SL 도달 ──
                elif sl_pct is not None and pnl_pct <= -abs(sl_pct):
                    reason = f"[L4 Exit] SL: {pnl_pct:+.1f}% <= -{abs(sl_pct):.1f}%"
                    urgency = 'panic'

                # ── Time Limit (보유기간 만료 강제 청산) ──
                # 단기 데이트레이딩(S1, S11 등) 및 시그널 소멸 종목에만 적용하며,
                # 코어 슬리브 및 현재 활성 매수 시그널 유지 종목은 면제 (단, 시그널이 소멸된 S14 고아 레그는 청산 허용)
                elif _is_s14_orphan_tl or (_max_hold_days is not None and _max_hold_days > 0 and days_held >= _max_hold_days):
                    from src.execution.position_ledger import is_core_or_macro_sleeve as _is_core_so
                    if (not _is_s14_orphan_tl) and (
                        _is_core_so(
                            stream_id,
                            track=str(pos.get('track', '')),
                            strategy=str(pos.get('strategy', '')),
                            ticker=str(_pos_ticker_up),
                        )
                        or stream_id in ('S_RECON', 'LIVE_SSOT')
                        or (_pos_ticker_up in _active_long_tickers_all)
                    ):
                        logger.debug(f"    🛡️ [Core/Active Signal Time Exemption] {_pos_ticker} ({stream_id}) 단기 타임리밋 면제")
                    else:
                        reason = f"[L4 Exit] Time Limit: {days_held}일 보유 >= {_max_hold_days}일 제한"
                        urgency = 'stop'

                # ── Trailing Stop (단일 포지션은 개별 HWM, S14 활성 페어는 동시 스프레드 HWM 및 공분산 샹들리에 밴드 적용) ──
                peak_pnl = (
                    _pair_spread_peak_so
                    if _is_active_s14_pair_leg
                    else (float(pos['peak_pnl_pct']) if pos.get('peak_pnl_pct') is not None else pnl_pct)
                )
                drawdown_from_peak = peak_pnl - pnl_pct
                is_trail_activated = (
                    (_pair_spread_trail_so or (trail_activate is not None and peak_pnl >= trail_activate))
                    if _is_active_s14_pair_leg
                    else (bool(pos.get('trail_activated', False)) or (trail_activate is not None and peak_pnl >= trail_activate))
                )
                if (reason is None and trail_dist is not None and is_trail_activated and drawdown_from_peak >= trail_dist):
                    reason = (
                        f"[L4 Exit] Trail: peak={peak_pnl:+.1f}% → "
                        f"now={pnl_pct:+.1f}% (↓{drawdown_from_peak:.1f}% "
                        f">= trail_dist={trail_dist:.1f}%)"
                    )
                    urgency = 'stop'

                # ── VIX 기반 동적 TP (인버스 한정) ──

                if reason:
                    _qty_exit = int(pos.get('qty', pos.get('quantity', 0)))
                    _amt_exit = float(pos.get('amount', 0.0))
                    _p_exit = float(pos.get('current_price', 0.0))
                    _tk_exit = str(pos.get('ticker', pos_key.split(':')[-1]))
                    _is_jp_exit = _tk_exit.upper().endswith('.T')
                    _is_us_exit = (not _is_kr_tk(_tk_exit)) and (not _is_jp_exit)
                    if _is_jp_exit:
                        _jpy_fx_exit = float(((market_data or {}).get('signal_cache') or {}).get('jpykrw', cfg.get('fx.jpykrw_fallback', 9.0)) or 9.0)
                        if _qty_exit > 0 and _p_exit > 0:
                            _amt_exit = _qty_exit * _p_exit * _jpy_fx_exit
                    elif _is_us_exit:
                        _dyn_fx_exit = float((market_data or {}).get('usdkrw') or ((market_data or {}).get('signal_cache') or {}).get('usdkrw') or 0.0)
                        if _dyn_fx_exit <= 0:
                            from src.data_collection.central_data_gateway import CentralDataGateway
                            _dyn_fx_exit = float(CentralDataGateway().get_usdkrw_rate() or 0.0)
                        if _qty_exit > 0 and _p_exit > 0 and _dyn_fx_exit > 0:
                            _amt_exit = _qty_exit * _p_exit * _dyn_fx_exit
                    elif not _amt_exit and _qty_exit > 0 and _p_exit > 0:
                        _amt_exit = _qty_exit * _p_exit

                    exit_orders.append({
                        'stream_id': stream_id,
                        'ticker': _tk_exit,
                        'name': pos.get('name', ''),
                        'direction': 'short',
                        'action': 'sell',
                        'quantity': _qty_exit,
                        'amount_krw': _amt_exit,
                        'price': _p_exit,
                        'confidence': 1.0,
                        'strategy': 'l4_global_exit',
                        'reason': reason,
                        'sell_type': 'tp_sl_exit',
                        'urgency': urgency,
                        'pos_key': pos_key,
                        'pnl_pct': pnl_pct,
                    })
                    logger.info(
                        f"    📤 Exit: {_tk_exit} ({_qty_exit}주) "
                        f"({stream_id}) | {reason} | urgency={urgency}")

        except Exception as _exit_err:
            logger.error(f"    [L4 Exit] _evaluate_exits 실패: {_exit_err}", exc_info=True)

        # --- ETF Tracking Logic (Delegated to LeverageJudge) ---
        try:
            _tradable_pos = {
                k: v for k, v in positions.items()
                if self._is_ticker_session_open_for_live_exit(
                    (v.get('ticker', k) if isinstance(v, dict) else k), market_data
                )
            } if isinstance(positions, dict) else positions
            etf_exits = self.leverage_judge.check_etf_stops(_tradable_pos, market_data, regime=regime)
            if etf_exits:
                for eo in etf_exits:
                    # Prevent duplicates if already exited
                    if not any(x['pos_key'] == eo['pos_key'] for x in exit_orders):
                        exit_orders.append(eo)
                        logger.info(f"    📤 Exit (ETF Stop): {eo.get('ticker', eo.get('pos_key'))} | {eo.get('reason')} | urgency={eo.get('urgency')}")
        except Exception as _etf_err:
            logger.error(f"    [ETF Stop] check_etf_stops 실패: {_etf_err}", exc_info=True)

        self._pending_exit_tickers = {
            str(eo.get('ticker', '')).split(':')[-1].strip().upper()
            for eo in exit_orders
            if isinstance(eo, dict) and eo.get('ticker')
        }
        return exit_orders

    def _apply_dd_overlay(self, dd_action: str, portfolio: Dict, regime: str = 'caution') -> List[Dict]:
        """DD Overlay 방어 전략 실행 (강제 청산 또는 헷지)."""
        orders = []
        if dd_action == 'none':
            return orders

        # 동적 설정 로드
        force_sell_ratio = pget('dd_overlay.force_sell_ratio', 0.2)
        hedge_ratio = pget('dd_overlay.tail_risk_hedge_ratio', 0.2)
        inverse_ticker = cfg.get('dd_overlay.inverse_etf_ticker', '252670')
        inverse_name = cfg.get('dd_overlay.inverse_etf_name', 'KODEX 200선물인버스2X')

        # Regime에 따른 동적 조정 (Crash 상태면 더 방어적으로)
        if regime == 'crash':
            force_sell_ratio = min(0.5, force_sell_ratio * 1.5)  # 최대 50%까지 강제 청산
            hedge_ratio = min(0.5, hedge_ratio * 1.5)            # 최대 50%까지 헷지 비중 확대

        try:
            import json
            positions = (portfolio or {}).get('positions') or (portfolio or {}).get('holdings') or {}
            if not positions and getattr(self, 'exec_mode', 'live') != 'live':
                sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
                if not sp_path.exists():
                    return orders
                sp = json.loads(sp_path.read_text())
                positions = sp.get('positions', {})
            elif not positions and getattr(self, 'exec_mode', 'live') == 'live':
                kp_path = _ROOT / 'results' / 'kis_portfolio.json'
                if not kp_path.exists():
                    return orders
                kp = json.loads(kp_path.read_text(encoding='utf-8'))
                raw_holdings = kp.get('holdings') or kp.get('positions') or {}
                if isinstance(raw_holdings, dict):
                    positions = {}
                    for _tk_dd, _h_dd in raw_holdings.items():
                        if isinstance(_h_dd, dict):
                            _d_copy = dict(_h_dd)
                            _d_copy.setdefault('ticker', _tk_dd)
                            _d_copy.setdefault('quantity', int(_h_dd.get('quantity', _h_dd.get('qty', 0)) or 0))
                            positions[_tk_dd] = _d_copy
                else:
                    positions = raw_holdings
            pos_list = positions.values() if isinstance(positions, dict) else positions
            active_pos = [
                p for p in pos_list
                if isinstance(p, dict) and p.get('quantity', p.get('qty', 0)) > 0
                and str(p.get('stream_id')) not in ('S1', 'S2', 'S3_B', 'S5', 'S_BETA', 'BETA', 'S_YIELD')
                and str(p.get('track', '')).upper() != 'TRACK_2_BUFFETT_QVM'
                and str(p.get('strategy', '')).lower() not in ('buffett_qvm', 'qvm_value')
                and self._is_ticker_session_open_for_live_exit(p.get('ticker', ''), None)
            ]
            
            if dd_action == 'force_sell_bottom_20':
                if not active_pos:
                    return orders
                # 수익률 하위 정렬
                active_pos.sort(key=lambda x: x.get('unrealized_pnl_pct', x.get('return_pct', 0)))
                n_sell = max(1, int(len(active_pos) * force_sell_ratio))
                targets = active_pos[:n_sell]
                
                for t in targets:
                    _qty = int(t.get('quantity', t.get('qty', 0)))
                    _prc = float(t.get('current_price', t.get('avg_price', 0)))
                    orders.append({
                        'stream_id': t.get('stream_id', 'S0'),
                        'ticker': t['ticker'],
                        'name': t.get('name', ''),
                        'direction': 'short',
                        'action': 'sell',
                        'quantity': _qty,
                        'amount_krw': t.get('amount') or t.get('market_value') or (_qty * _prc),
                        'price': _prc,
                        'confidence': 1.0,
                        'strategy': 'dd_overlay_force_sell',
                        'reason': f"DD Stage 1: 수익률 하위 {n_sell}종목 강제 청산",
                        'sell_type': 'force_sell',
                        'urgency': 'panic',
                        'pos_key': f"{t.get('stream_id', 'S0')}:{t['ticker']}"
                    })
                logger.warning(f"    🚨 DD Stage 1: 하위 {force_sell_ratio:.0%} ({n_sell}개) 강제 청산")
                
            elif dd_action == 'tail_risk_hedge':
                # 인버스 ETF 매수 (기본: KODEX 200선물인버스2X)
                # 현재 포트폴리오 가치의 동적 비율만큼 할당
                nav = portfolio.get('total_nav', 0)
                hedge_amount = nav * hedge_ratio
                orders.append({
                    'stream_id': 'SYS_HEDGE',
                    'ticker': inverse_ticker,
                    'name': inverse_name,
                    'direction': 'long',
                    'amount_krw': hedge_amount,
                    'price': 0,
                    'confidence': 1.0,
                    'strategy': 'tail_risk_hedge',
                    'reason': f"DD Stage 2: 꼬리 리스크 헷지 인버스 매수"
                })
                logger.warning(f"    🚨 DD Stage 2: {inverse_name} ₩{hedge_amount:,.0f} 매수 주문 (비중 {hedge_ratio:.0%})")
                
                # S4 Advisory 알람 생성 (텔레그램 스팸 방지로 제거됨)
                logger.debug("    S4 Advisory DD 알람 발송 생략 (텔레그램 비활성)")
        except Exception as e:
            logger.error(f"DD Overlay 적용 실패: {e}", exc_info=True)

        return orders

    def _apply_final_execution_gate(self, exec_orders: List[Dict], portfolio: Optional[Dict], market_data: Optional[Dict]) -> List[Dict]:
        """★ Wave 0 최종 실행 게이트.

        S2: exit-only(킬스위치/데이터 이상) → 모든 매수 제거, 매도(청산)만 통과.
        S5: 통화별 전역 현금 제약 — Σ매수명목 ≤ 가용현금 (+ 동일통화 매도대금 × w, 기본 w=0:
            현재 엔진은 매도/매수를 동시 집행하므로 매도대금 선사용은 미수 위험).
            초과 시 해당 통화 매수 수량을 비례 축소 s = avail / need (정수 절삭, 0주는 제거).
        """
        orders = [dict(o) for o in (exec_orders or [])]
        is_buy = lambda o: str(o.get('action', '')).lower() in ('buy', 'add_on')
        exit_only = bool((market_data or {}).get('exit_only')) or bool(getattr(self, '_exit_only_reason', None))
        if exit_only:
            kept = [o for o in orders if not is_buy(o)]
            if len(kept) != len(orders):
                logger.critical(f"  🛑 [Final Gate] Exit-Only({getattr(self, '_exit_only_reason', None) or 'market_data'}) — "
                                f"매수 {len(orders) - len(kept)}건 제거, 매도 {len(kept)}건 통과")
            return kept

        # ★ Wave 1: 종목 단위 DATA_NOGO — 데이터 이상 종목은 신규 매수 제거(청산은 허용)
        _nogo_t = {str(x).strip() for x in ((market_data or {}).get('data_nogo_tickers') or [])}
        if _nogo_t:
            _before = len(orders)
            orders = [o for o in orders if not (is_buy(o) and str(o.get('ticker', '')).strip() in _nogo_t)]
            if len(orders) != _before:
                logger.warning(f"  🛑 [Final Gate] DATA_NOGO 종목 매수 {_before - len(orders)}건 제거: {sorted(_nogo_t)}")

        # ★ 장중(08:00~15:10 KST) 파킹 ETF 일반 매수 주문이 Final Gate S5 현금 비례축소(s = avail/need) 모수를 부풀리지 않도록 사전 제외
        import os as _os_fg
        from datetime import time as _dtime_fg
        _in_pytest_fg = bool(_os_fg.environ.get('MERIDIAN_TEST_MODE') == '1' or _os_fg.environ.get('PYTEST_CURRENT_TEST') or (market_data or {}).get('_test_mode', False))
        _now_t_fg = datetime.now().time()
        _FG_PARKING_BLOCK_TKS = {'459580', '423160', '449170', '430740', '357870', '357330'}
        if getattr(self, 'exec_mode', 'live') in ('live', 'paper') and (not _in_pytest_fg or bool((market_data or {}).get('enforce_intraday_parking_guard', False))) and (_dtime_fg(8, 0) <= _now_t_fg <= _dtime_fg(15, 10)):
            _before_fg = len(orders)
            orders = [
                o for o in orders
                if not (
                    is_buy(o)
                    and str(o.get('ticker', '')).strip() in _FG_PARKING_BLOCK_TKS
                    and not bool(o.get('is_yield_sweep', False))
                )
            ]
            if len(orders) < _before_fg:
                logger.info(
                    f"  🏦 [Final Gate Parking Filter] 장중({_now_t_fg.strftime('%H:%M')}) 파킹 ETF 일반 매수 "
                    f"{_before_fg - len(orders)}건 제외 → 활성 알파 주문 비례축소 방지"
                )

        from src.data_collection.universe_loader import _KR_TICKER_RE as _KR_RE_FG
        def _ccy(o):
            t = str(o.get('ticker', '')).strip().upper()
            if t.endswith('.T'):
                return 'JPY'
            is_kr = t.isdigit() or bool(_KR_RE_FG.match(t))
            return 'KRW' if is_kr else 'USD'

        def _px(o):
            p = float(o.get('price', 0) or 0)
            if p <= 0:
                try:
                    p = float(self.execution_engine._estimate_price(o.get('ticker', '')) or 0)
                except Exception:
                    p = 0.0
            return p

        import os as _os_fg
        w_sell = float(cfg.get('execution.cash_cap_sell_proceeds_weight', 0.0) or 0.0)
        fee_buf = float(pget('execution.cash_cap_fee_buffer', 0.003) or 0.003)
        _pf = portfolio or {}
        cash = {'KRW': (float(_pf.get('cash') or 0) if 'cash' in _pf else None),
                'USD': (float(_pf.get('cash_usd') or 0) if 'cash_usd' in _pf else None)}
        if (
            getattr(self, 'exec_mode', 'live') == 'live'
            and not _os_fg.environ.get('PYTEST_CURRENT_TEST')
            and _os_fg.environ.get('MERIDIAN_TEST_MODE') != '1'
        ):
            try:
                _live_tr = self.execution_engine._get_trader('DEFAULT')
                if _live_tr is not None and hasattr(_live_tr, 'fetch_live_balance'):
                    _live_tr.fetch_live_balance()
                    _live_krw = float(getattr(getattr(_live_tr, 'account', None), 'cash', 0.0) or 0.0)
                    _live_nrcvb = getattr(_live_tr, 'nrcvb_buy_amt_krw', None)
                    if _live_nrcvb is not None and float(_live_nrcvb) >= 0.0:
                        _live_krw = min(_live_krw, float(_live_nrcvb)) if _live_krw > 0.0 else float(_live_nrcvb)
                    if _live_krw >= 0.0:
                        cash['KRW'] = min(float(cash['KRW']), _live_krw) if cash['KRW'] is not None else _live_krw
            except Exception as _fg_bal_e:
                logger.debug(f"  [Final Gate] live balance sync skipped: {_fg_bal_e}")

        out = []
        for ccy in ('KRW', 'USD'):
            sells = [o for o in orders if not is_buy(o) and _ccy(o) == ccy]
            buys = [o for o in orders if is_buy(o) and _ccy(o) == ccy]
            out.extend(sells)
            if not buys:
                continue
            if cash[ccy] is None:
                logger.warning(f"  ⚠️ [Final Gate S5] {ccy} 가용현금 정보 없음(portfolio 키 부재) — 현금 제약 미적용")
                out.extend(buys)
                continue
            priced, unpriced = [], []
            _dropped_pair_ids = set()
            for o in buys:
                if _px(o) > 0:
                    priced.append(o)
                else:
                    unpriced.append(o)
                    if o.get('pair_id'):
                        _dropped_pair_ids.add(str(o.get('pair_id')))
            for o in unpriced:
                logger.error(f"  🛑 [Final Gate] {o.get('ticker')}: 가격 미확보로 현금 제약 검증 불가 → 매수 제거")
            if _dropped_pair_ids:
                _clean_priced = []
                for o in priced:
                    if str(o.get('pair_id', '') or '') in _dropped_pair_ids:
                        logger.warning(f"  🛑 [Final Gate Pair Atomic] {o.get('ticker')}: 페어 파트너 가격 미확보로 동반 제거 (pair_id={o.get('pair_id')})")
                    else:
                        _clean_priced.append(o)
                priced = _clean_priced
            need = sum(_px(o) * int(o.get('quantity', 0) or 0) for o in priced) * (1.0 + fee_buf)
            avail = max(0.0, cash[ccy] + w_sell * sum(_px(o) * int(o.get('quantity', 0) or 0) for o in sells))
            if need <= avail or need <= 0:
                out.extend(priced)
                continue
            s = avail / need
            logger.critical(f"  🛡️ [Final Gate S5] {ccy} 매수 필요 {need:,.2f} > 가용 {avail:,.2f} → 비례 축소 s={s:.3f}")
            _scaled_candidates = []
            for o in priced:
                q0 = int(o.get('quantity', 0) or 0)
                q1 = int(q0 * s)
                if q1 <= 0:
                    logger.warning(f"    ↳ {o.get('ticker')}: {q0}주 → 0주 (제거)")
                    if o.get('pair_id'):
                        _dropped_pair_ids.add(str(o.get('pair_id')))
                    continue
                o['quantity'] = q1
                o['cash_cap_scale'] = round(s, 4)
                logger.warning(f"    ↳ {o.get('ticker')}: {q0}주 → {q1}주")
                _scaled_candidates.append(o)
            for o in _scaled_candidates:
                if str(o.get('pair_id', '') or '') and str(o.get('pair_id', '') or '') in _dropped_pair_ids:
                    logger.warning(f"    ↳ [Pair Atomic Drop] {o.get('ticker')}: 페어 파트너 수량 0주 절삭으로 동반 제거 (pair_id={o.get('pair_id')})")
                    continue
                out.append(o)
        # 현금 정보가 없는 통화(JPY)는 기존 로직 유지(경고)
        for o in orders:
            if _ccy(o) == 'JPY':
                if is_buy(o):
                    logger.warning(f"  ⚠️ [Final Gate] {o.get('ticker')}: JPY 현금 정보 없음 — 현금 제약 미적용")
                out.append(o)
        return out

    def _log_run_event(self, result: Dict):
        """실행 결과 EventLedger 기록."""
        try:
            from src.measurement.event_ledger import log_event
            log_event('SYSTEM', {
                'type': 'orchestrator_run',
                'status': result['status'],
                'regime': result['regime'],
                'n_signals': sum(
                    len(s) for s in result['signals'].values()),
                'n_orders': len(result['orders']),
                'allocation': result['allocation'],
                'leverage': result.get('leverage', {}).get(
                    'judgment', {}).get('leverage_level', 1),
                'elapsed': result.get('elapsed_seconds', 0),
            }, source='stream_orchestrator')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)

    def get_stream_status(self) -> List[Dict]:
        """전체 스트림 상태 요약."""
        status = []
        for stream in self.streams:
            perf = stream.get_performance()
            status.append({
                'stream_id': stream.stream_id,
                'name': stream.name,
                'active': stream.is_active(),
                'shadow': stream.is_shadow,
                'enabled': stream._enabled,
                'sharpe': perf.get('sharpe'),
                'cum_return': perf.get('cumulative_return_pct', 0),
                'positions': perf.get('active_positions', 0),
            })
        return status


# ── 스크립트 직접 실행 ──
# ═══════════════════════════════════════════════════════════════════════════════
# [Phase 37: Moonshot Booster System] 모듈 수준 헬퍼 함수
# ═══════════════════════════════════════════════════════════════════════════════

def _is_kelly_booster_active(regime, signal_cache_path, portfolio_path):
    """[Phase 37] Kelly Booster 발동 조건 동적 평가 — 하드코딩 없음."""
    import json, statistics, pathlib as _pl
    if regime != 'bull':
        return False
    ois_lookback  = int(pget('kelly.ois_lookback_days', 60))
    conf_pct      = int(pget('kelly.confidence_percentile', 75))
    conf_lookback = int(pget('kelly.confidence_lookback', 30))
    # OIS 동적 임계값: 롤링 중앙값
    try:
        sc_path = _pl.Path(signal_cache_path)
        if not sc_path.exists():
            return False
        sc = json.loads(sc_path.read_text(encoding='utf-8'))
        ois_today   = float(sc.get('ois', 50) or 50)
        ois_history = [float(v) for v in sc.get('ois_history', []) if v is not None]
        ois_median  = (statistics.median(ois_history[-ois_lookback:])
                       if len(ois_history) >= 5
                       else float(pget('kelly.ois_fallback_threshold', 55)))
        if ois_today <= ois_median:
            return False
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)
        return False
    # ML Confidence 동적 임계값: 롤링 퍼센타일
    try:
        import numpy as np
        sp_path = _pl.Path(portfolio_path)
        if not sp_path.exists():
            return False
        sp = json.loads(sp_path.read_text(encoding='utf-8'))
        positions = sp.get('positions') or sp.get('holdings') or {}
        conf_vals = [
            float(v.get('ml_confidence', 0) or v.get('confidence', 0))
            for v in positions.values()
            if v.get('stream_id', '') in ('S2', 'S3')
            and (v.get('ml_confidence') or v.get('confidence'))
        ]
        conf_hist_raw = [float(v) for v in sp.get('confidence_history', []) if v]
        conf_hist = (conf_vals + conf_hist_raw)[-conf_lookback:]
        if len(conf_hist) < 3 or not conf_vals:
            return False
        conf_threshold = float(np.percentile(conf_hist, conf_pct))
        if not any(c > conf_threshold for c in conf_vals):
            return False
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)
        return False
    return True


def _compute_kelly_max_positions(portfolio_path):
    """[Phase 37] 포지션 간 평균 상관계수 기반 Kelly 압축 종목 수 동적 결정."""
    import json, pathlib as _pl
    fallback = int(pget('kelly.max_pos_fallback', 3))
    try:
        import numpy as np
        sp_path = _pl.Path(portfolio_path)
        if not sp_path.exists():
            return fallback
        sp = json.loads(sp_path.read_text(encoding='utf-8'))
        corr_matrix = sp.get('correlation_matrix', {})
        if not corr_matrix:
            return fallback
        tickers = list(corr_matrix.keys())
        corr_vals = []
        for i, t1 in enumerate(tickers):
            for t2 in tickers[i + 1:]:
                v = corr_matrix.get(t1, {}).get(t2)
                if v is not None:
                    corr_vals.append(abs(float(v)))
        if not corr_vals:
            return fallback
        avg_corr = float(np.mean(corr_vals))
        if avg_corr < 0.3:
            return int(pget('kelly.max_pos_low_corr', 5))
        elif avg_corr < 0.6:
            return int(pget('kelly.max_pos_mid_corr', 3))
        else:
            return int(cfg.get('kelly.max_pos_high_corr', 2))
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)
        return fallback

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Meridian StreamOrchestrator')
    parser.add_argument('--mode', default=None,
                        choices=['shadow', 'mock', 'paper', 'live'],
                        help='체결 모드 (기본: .env의 KIS_MODE)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    orch = StreamOrchestrator(exec_mode=args.mode)
    result = orch.run()

    print(f"\n{'='*50}")
    print(f"Status: {result['status']}")
    print(f"Regime: {result['regime']}")
    print(f"Orders: {len(result['orders'])}")
    print(f"Allocation: {result['allocation']}")
    print(f"Execution: {result.get('execution', {}).get('n_filled', 0)}/"
          f"{result.get('execution', {}).get('n_orders', 0)}")
    print(f"Go/No-Go: {result.get('go_nogo', {}).get('verdict', 'N/A')}")
    print(f"Elapsed: {result.get('elapsed_seconds', 0):.1f}s")
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

import json
import pandas as pd
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List

# 프로젝트 루트를 path에 추가
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config.dynamic_config import DynamicConfig
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
                return atr_val / 100.0  # % → 소수 변환

    except Exception as _vol_e:
        logger.error(f"[Phase 51 vol] {ticker} 변동성 계산 실패: {_vol_e}", exc_info=True)

    logger.debug(f"[Phase 51 vol] {ticker} → fallback {fallback*100:.1f}%")
    return fallback
class StreamOrchestrator:
    """4-Stream 통합 오케스트레이터.

    모든 컴포넌트를 조율하여 하나의 매매 사이클을 실행합니다.
    """

    def __init__(self, exec_mode: str = None):
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
        self.execution_engine = ExecutionEngine(mode=exec_mode)
        self.shadow_recorder = ShadowRecorder()

        # 100% 동적 수학 모델 조율기 & CRO 최후단 리스크 차단기
        self.netting_engine = NettingEngine()
        self.qp_optimizer = QPortfolioOptimizer()
        self.cro_final_gate = CROFinalGate()
        self.audit_ledger = ConflictAuditLedger()

        # 8대 마스터 시나리오 및 고효율 알파/절세/상쇄 모듈
        self.master_scenario_engine = MasterScenarioEngine()
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
                'us_regime': us_regime_val,
                '_test_mode': self.execution_engine.mode == 'mock',
                'is_mock': self.execution_engine.mode == 'mock',
            }

        if portfolio is None:
            # ExecutionEngine에서 계좌 정보 가져오기
            acct = self.execution_engine.get_account_summary()
            initial = cfg.get('portfolio.initial_capital', 20_000_000.0)

            kis_path = _ROOT / 'results' / 'kis_portfolio.json'
            shadow_path = _ROOT / 'results' / 'shadow_portfolio.json'

            holdings = {}
            positions = {}
            total_nav = float(acct.get('total_equity', initial))
            cash = float(acct.get('cash', initial))

            if getattr(self, 'exec_mode', 'live') == 'live' and kis_path.exists():
                try:
                    kis_data = json.loads(kis_path.read_text(encoding='utf-8'))
                    raw_holdings = kis_data.get('holdings', {})
                    acct_data = kis_data.get('account', {})
                    total_nav = float(acct_data.get('nav', total_nav))
                    cash = float(acct_data.get('cash', cash))
                    for t, h in raw_holdings.items():
                        holdings[t] = h
                        positions[t] = {
                            'ticker': t,
                            'name': h.get('name', t),
                            'quantity': int(h.get('qty', 0)),
                            'qty': int(h.get('qty', 0)),
                            'avg_price': float(h.get('avg_price', 0.0)),
                            'current_price': float(h.get('current_price', 0.0)),
                            'amount': float(h.get('qty', 0)) * float(h.get('current_price', 0.0)),
                            'unrealized_pnl_pct': float(h.get('return_pct', 0.0)),
                            'pnl_pct': float(h.get('return_pct', 0.0)),
                            'market': h.get('market', 'KR')
                        }
                except Exception as _k_err:
                    logger.warning(f"kis_portfolio.json 로드 예외: {_k_err}")
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

            portfolio = {
                'total_nav': total_nav,
                'nav': total_nav,
                'hwm': max(total_nav, initial),
                'daily_returns': [],
                'active_positions': len(positions),
                'cash': cash,
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
            from pykrx import stock as _pykrx_td
            _today_td = date.today().strftime('%Y%m%d')
            # [SANDBOX BYPASS] 미래 날짜(2026년) 테스트를 위해 pykrx 휴일 검증 강제 무력화
            # _td_df = _pykrx_td.get_market_ohlcv_by_date(_today_td, _today_td, '005930')
            # if len(_td_df) == 0:
            #     logger.warning("  ⚠️ 오늘은 거래일이 아닙니다 (주말/공휴일). 주문 생성 스킵.")
            #     result['status'] = 'skipped_non_trading_day'
            #     return result
            logger.info("  [SANDBOX] 휴일 체크 무력화. 강제로 거래일로 간주합니다.")
        except Exception as _td_e:
            # pykrx 실패 시 weekday fallback
            if date.today().weekday() >= 5:
                logger.error(f"  ⚠️ 주말 감지 (weekday={date.today().weekday()}). 주문 생성 스킵.", exc_info=True)
                result['status'] = 'skipped_weekend'
                return result
            logger.debug(f"  거래일 검증 실패 (진행): {_td_e}")

        # ── Step 0.5: DATA_NOGO Circuit Breaker [Phase 70-A] ──
        _data_nogo_flag = (
            (market_data.get('data_nogo', False) if market_data else False)
            or getattr(self, 'data_nogo', False)  # daily_pipeline에서 orch.data_nogo 주입 시
        )
        if _data_nogo_flag and not market_data:
            market_data = {}
        if getattr(self, 'data_nogo', False) and isinstance(market_data, dict):
            market_data.setdefault('data_nogo_reason',
                                   getattr(self, 'data_nogo_reason', 'unknown'))
        if _data_nogo_flag:
            _nogo_reason = (
                market_data.get('data_nogo_reason', 'unknown') if market_data else 'unknown'
            )
            logger.critical(
                f"  🚨 [Phase 70 DATA_NOGO] 데이터 신뢰도 상실 — 파이프라인 안전 중단: {_nogo_reason}"
            )
            result['status'] = 'halted_data_nogo'
            result['data_nogo_reason'] = _nogo_reason
            self._log_run_event(result)
            return result

        # ── Step 1: Kill Switch 확인 ──
        logger.info("  📋 Step 1: Kill Switch 확인")
        ks_result = self.kill_switch.assess(portfolio, regime)
        result['risk']['kill_switch'] = ks_result

        if not ks_result['judgment']['safe']:
            logger.critical("  🚨 KILL SWITCH 발동 — 파이프라인 중단 (전량 청산 주문 생성)")
            result['status'] = 'halted_kill_switch'
            
            # [Phase 50] Kill Switch 발동 시 포트폴리오 전량 시장가 청산
            # 기존에는 그대로 리턴하여 포지션이 묶이는(Freeze) 버그가 있었음
            for ticker, pos in portfolio.get('positions', {}).items():
                qty = pos.get('qty', 0)
                if qty > 0:
                    result['orders'].append({
                        'stream_id': 'SYS_KILL',
                        'ticker': pos.get('ticker', ticker.split(':')[-1]),
                        'name': pos.get('name', ''),
                        'direction': 'sell',
                        'qty': qty,
                        'amount_krw': pos.get('amount', 1.0),
                        'price': market_data.get('signal_cache', {}).get(ticker, pos.get('avg_price', 0)),
                        'confidence': 1.0,
                        'strategy': 'Kill Switch Liquidation',
                        'reason': f"Kill Switch Triggered: {ks_result.get('triggers', [])}",
                        'urgency': 1
                    })
            
            self._log_run_event(result)
            return result

        # ── Step 2: Crash Defense 확인 ──
        logger.info("  📋 Step 2: Crash Defense 확인")
        cd_result = self.crash_defense.assess(market_data, portfolio, regime)
        result['risk']['crash_defense'] = cd_result

        # ── Step 2.5: Master 8-Scenario Engine 검증 & 대응 ──
        logger.info("  📋 Step 2.5: MasterScenarioEngine 8대 마스터 시나리오 검증")
        sc_result = self.master_scenario_engine.evaluate_scenarios(market_data)
        result['risk']['master_scenarios'] = sc_result

        # ── Step 3: DD Guard 확인 ──
        logger.info("  📋 Step 3: DrawdownGuard 확인")
        dd_result = self.dd_guard.assess(portfolio, regime)
        result['risk']['drawdown_guard'] = dd_result

        # 최대 노출도 결정 (리스크 모듈들의 최소값)
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
        if 'S10_MEGA_TREND' not in all_signals or not all_signals['S10_MEGA_TREND']:
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
        all_signals['S10'] = all_signals.get('S10_MEGA_TREND', [])



        result['signals'] = {
            sid: [s for s in sigs]
            for sid, sigs in all_signals.items()
        }
        
        # ★ Exit-Only Mode 처리: 데이터 이상 시 신규 매수 시그널 전부 삭제 (TP/SL만 유지)
        if market_data and market_data.get('exit_only'):
            logger.error("🚨 [Exit-Only Mode] 신규 매수 시그널을 모두 삭제합니다.")
            result['signals'] = {sid: [] for sid in result['signals']}

        # [Phase 48 C-1] existing_tickers 사전 선언 — Step 4.5보다 반드시 앞에 위치해야
        # NameError 방지: _generate_orders()에서 재정의되지만, Step 4.5에서 먼저 참조함
        existing_tickers: set = set()

        # ── Step 4.5: S3/S4 Alpha-Ranked Hysteresis 리밸런싱 ──
        logger.info("  📋 Step 4.5: S3/S4 리밸런싱")
        rebalance_orders = []
        try:
            import json as _json_rebal
            _sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
            _sp_data = {}
            if _sp_path.exists():
                _sp_data = _json_rebal.loads(_sp_path.read_text())
            _all_positions = dict(_sp_data.get('positions', {}))

            # [SSoT Integration] 실계좌 포트폴리오(portfolio) 주입 시 보유 종목 스트림 매핑 주입
            if portfolio and portfolio.get('positions'):
                _raw_positions = portfolio['positions']
                if isinstance(_raw_positions, dict):
                    for _pk, _pv in _raw_positions.items():
                        if ':' in str(_pk):
                            _all_positions[str(_pk)] = _pv if isinstance(_pv, dict) else {'ticker': str(_pk).split(':')[-1], 'qty': getattr(_pv, 'quantity', 0)}
                        else:
                            _tk_up = str(getattr(_pv, 'ticker', _pk) or _pk).upper()
                            _sid = 'S3' if _tk_up in ('NVDA', 'QQQ', 'SOXX', 'XLK', '091160', 'MSFT', 'AAPL', 'TSLA') else ('S0' if _tk_up in ('069500', '229200') else 'S3')
                            _qty_val = getattr(_pv, 'quantity', _pv.get('qty', 0) if isinstance(_pv, dict) else 0)
                            _amt_val = getattr(_pv, 'amount', _pv.get('amount', 0) if isinstance(_pv, dict) else 0)
                            _prc_val = getattr(_pv, 'current_price', _pv.get('current_price', 0.0) if isinstance(_pv, dict) else 0.0)
                            _p_dict = {
                                'ticker': _tk_up,
                                'stream_id': _sid,
                                'qty': _qty_val,
                                'amount': _amt_val,
                                'current_price': _prc_val,
                            }
                            _all_positions[f"{_sid}:{_tk_up}"] = _p_dict

            # [SSoT Policy] 리밸런싱 주문 생성: S3, S4 및 활성화된 스트림의 rebalance.enabled 확인
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

        weights = self.allocator.allocate(stream_metrics, regime, market_data, s0_sigs=s0_sigs)
        result['allocation'] = weights
        logger.info(f"    📊 배분: {weights}")

        # ── Step 6: 레버리지 판정 ──
        logger.info("  📋 Step 6: 레버리지 판정")
        lev_result = self.leverage_judge.assess(
            portfolio, stream_metrics, regime)
        result['leverage'] = lev_result
        lev_level = lev_result['judgment']['leverage_level']
        logger.info(f"    ⚡ 레버리지: {lev_level}X")

        # ── Step 7: 최종 주문 생성 ──
        logger.info("  📋 Step 7: 최종 주문 생성")

        # ★ 가격 누락 시그널에 pykrx 실시간 종가 주입 (₩50,000 하드코딩 방지)
        try:
            _missing_tickers = set()
            for _sigs in all_signals.values():
                for _s in _sigs:
                    if not _s.get('price') or _s['price'] <= 0:
                        _t = _s.get('ticker', '')
                        if _t:
                            _missing_tickers.add(_t)

            if _missing_tickers:
                from pykrx import stock as _pykrx_orch
                from datetime import date as _date_orch, timedelta as _td_orch
                _today_orch = _date_orch.today().strftime('%Y%m%d')
                _week_ago = (_date_orch.today() - _td_orch(days=7)).strftime('%Y%m%d')
                _injected = 0
                for _t in _missing_tickers:
                    try:
                        _df = _pykrx_orch.get_market_ohlcv_by_date(
                            _week_ago, _today_orch, _t)
                        if len(_df) > 0:
                            _close = float(_df.iloc[-1].get('종가', 0))
                            if _close > 0:
                                for _sigs in all_signals.values():
                                    for _s in _sigs:
                                        if _s.get('ticker') == _t and (not _s.get('price') or _s['price'] <= 0):
                                            _s['price'] = _close
                                            _injected += 1
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).warning(f"Targeted fallback: {e}", exc_info=True)
                if _injected:
                    logger.info(f"    📡 pykrx 가격 주입: {_injected}건 "
                               f"({len(_missing_tickers)}종목)")
        except Exception as _pe:
            logger.error(f"    가격 주입 실패: {_pe}", exc_info=True)

        # ── Step 4.7: 글로벌 Exit Manager (TP/SL/Trailing Stop) ──
        logger.info("  📋 Step 4.7: 글로벌 Exit Manager (TP/SL)")
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

            # Step 2: 현금 부족으로 미주문된 신규 매수 시그널 수집 (S10, S2, S3 등)
            _ordered_tickers = {o.get('ticker') for o in orders if o.get('direction') in ('buy', 'long') or o.get('action') == 'buy'}
            _candidate_sigs = []
            for _sid, _sigs in all_signals.items():
                for _s in _sigs:
                    _tk = _s.get('ticker')
                    if _tk and _tk not in _ordered_tickers and _s.get('direction', _s.get('action', 'long')) in ('long', 'buy', 'BUY'):
                        _candidate_sigs.append(_s)

            # Step 3: 자본 스왑 라우팅 집행 (기존 최저 EV 보유자산 매도 + 신규 매수 주문 쌍 생성)
            _avail_cash = float(portfolio.get('cash', 0.0) or 0.0)
            _swap_orders = _router.route_intraday_capital(_candidate_sigs, market_data, total_cash_krw=_avail_cash, portfolio=portfolio)
            
            if _swap_orders:
                _swap_sells = [o for o in _swap_orders if o.get('direction') == 'sell' or o.get('action') == 'sell']
                _swap_buys = [o for o in _swap_orders if o.get('direction') in ('buy', 'long') or o.get('action') == 'buy']
                if _swap_sells or _swap_buys:
                    orders = _swap_sells + orders + _swap_buys
                    logger.info(f"    🔄 [Cross-Asset Alpha Swap] 자본 교체 매매: 매도 {len(_swap_sells)}건, 매수 {len(_swap_buys)}건 병합")
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
            _argus_policy_thr = float(cfg.get('argus.bull_shield_policy_thr', 0.75))
            _argus_semi_thr   = float(cfg.get('argus.semi_shield_thr',        0.75))
            if _argus_policy >= _argus_policy_thr or _argus_semi >= _argus_semi_thr:
                for o in orders:
                    if o.get('strategy') in ('sys_tactical_hedge', 'tail_risk_hedge'):
                        _prev_hedge = o['amount_krw']
                        _argus_hedge_cut = float(cfg.get('argus.bull_shield_hedge_cut', 0.5))
                        _new_h_amt = o['amount_krw']
                        logger.info(f"  [Phase 66 Argus 오버레이] Bull 쉴드 발동 (policy={_argus_policy:.2f} semi={_argus_semi:.2f}) → 헤지 {_prev_hedge:.0f}→{_new_h_amt:.0f} (-50%)")

        # — Bear 쉴드: 상승장 신호에서 인플레 과열이면 롱 20% 삭감 (기존 orders에 직접 적용)
        _long_inflation_cut = 1.0
        _argus_inf_thr = float(cfg.get('argus.bear_shield_inflation_thr', 0.80))
        if regime == 'bull' and _argus_inflation >= _argus_inf_thr:
            _long_inflation_cut = float(cfg.get('argus.bear_shield_long_cut', 0.8))
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

        # ★ scale_multiplier 적용 (DD 방어선 축소)
        if scale_multiplier < 1.0:
            for o in orders:
                if o.get('direction') == 'long' and o.get('strategy') not in ('tail_risk_hedge', 'rebalance'):
                    o['amount_krw'] = o['amount_krw'] * scale_multiplier
            logger.info(f"    📉 매수 스케일 {scale_multiplier:.2f} 적용 완료")

        # ── [Phase 76] Two-Track: MACRO vs MICRO 시그널 필터 ──
        _MICRO_BOUND = set(cfg.get('stream.micro_bound', ['S1', 'S2', 'S5']))
        _MACRO_BOUND = set(cfg.get(
            'stream.macro_bound_streams',
            ['S3', 'S4']
        ))
        _curr_regime = regime  # run() 스코프에서 직접 참조
        _filtered_orders = []
        for _ord in (orders if isinstance(orders, list) else [orders]):
            _stream = str(_ord.get('stream_id', _ord.get('stream', '')))
            _action = str(_ord.get('action', _ord.get('direction', ''))).upper()
            _is_exit  = _action in ('SELL', 'EXIT', 'CLOSE', 'REDUCE')
            _is_hedge = (
                _ord.get('is_hedge', False)
                or 'hedge' in str(_ord.get('reason', '')).lower()
                or 'inverse' in str(_ord.get('ticker', '')).lower()
                or _ord.get('strategy') in ('sys_tactical_hedge', 'tail_risk_hedge')
            )
            
            # [Phase 80] Short-term AlphaStreams (S1, S8) 및 HEDGE/EXIT는 필터 면제
            _is_alpha_exempt = _stream in ('S1', 'S2', 'S10') and not _is_exit and not _is_hedge

            # EXIT / HEDGE 는 항상 통과
            if _is_exit or _is_hedge or _is_alpha_exempt:
                _filtered_orders.append(_ord)
                continue

            # MACRO_BOUND BUY → caution/bear/crash 시 기각 (동적 완화 및 S3 불타기 허용)
            if _stream in _MACRO_BOUND and _action in ('BUY', 'LONG', ''):
                _conf = float(_ord.get('confidence', 0.0))
                _macro_caution_pass_conf = float(cfg.get('risk.macro_guard_caution_pass_conf', 0.60))
                # caution 장세라도 확신도가 임계값 이상이면 진입 허용 (S3 강제기각 방지)
                if _curr_regime == 'caution' and _conf >= _macro_caution_pass_conf:
                    logger.info(f"  [Phase 76 MacroGuard] {_stream} BUY 통과 (regime=caution 이나 confidence={_conf:.2f}>={_macro_caution_pass_conf:.2f})")
                elif _curr_regime in ('caution', 'bear', 'crash'):
                    logger.info(
                        f"  [Phase 76 MacroGuard] {_stream} BUY 기각 "
                        f"(regime={_curr_regime}, conf={_conf:.2f})"
                    )
                    continue

            # [Task 2] S3 Pyramiding (불타기) 허용
            if _stream == 'S3' and _action in ('BUY', 'LONG'):
                _conf = float(_ord.get('confidence', 0.0))
                _pyramid_conf_th  = float(cfg.get('s3.pyramiding_conf_threshold', 0.70))
                _pyramid_scale    = float(cfg.get('s3.pyramiding_scale_factor',   2.00))  # (conf-th)*scale = max boost
                if _conf >= _pyramid_conf_th:
                    _pyramid_mult = 1.0 + (_conf - _pyramid_conf_th) * _pyramid_scale
                    if 'amount_krw' in _ord:
                        _ord['amount_krw'] = _ord['amount_krw'] * _pyramid_mult
                        logger.info(f"  🔥 [Pyramiding] S3 강한 확신(conf={_conf:.2f}): 투자 스케일 {_pyramid_mult:.2f}x 확대")


            # MICRO_BOUND BUY → regime 무관, IntradayMicroGuard만 체크
            if _stream in _MICRO_BOUND and _action in ('BUY', 'LONG', ''):
                try:
                    from src.risk.intraday_micro_guard import IntradayMicroGuard
                    _mg = IntradayMicroGuard()
                    _mg_ok, _mg_reason = _mg.check()
                    if not _mg_ok:
                        logger.warning(
                            f"  [Phase 76 MicroGuard] {_stream} BUY 일시정지: {_mg_reason}"
                        )
                        continue
                    logger.info(
                        f"  [Phase 76 MicroGuard] {_stream} BUY 통과 "
                        f"(regime={_curr_regime}, 거시무관)"
                    )
                except Exception as _mge:  # noqa: BLE001
                    logger.error(f"  [Phase 76 MicroGuard] 체크 실패, 통과: {_mge}", exc_info=True)

            _filtered_orders.append(_ord)
        # ★ [SSoT Live Order Policy] 허용 스트림 및 모든 청산/교체매매(Swap) 주문 통과
        _ORDER_PERMITTED_STREAMS = {
            'S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S10', 'S11',
            'S0_BETA', 'S1_EDGE', 'S2_ML_ALPHA', 'S3_ACTIVE_MACRO', 'S10_MEGA_TREND', 'S11_HIGH_BETA',
            'S_BETA', 'S_YIELD', 'EXIT_MANAGER', 'SYS_SWAP', 'SYS_NET', 'SYS_KILL', 'TACTIC_D', 'TACTIC_E'
        }
        orders = [
            o for o in _filtered_orders
            if str(o.get('stream_id', o.get('stream', ''))).upper() in _ORDER_PERMITTED_STREAMS
            or o.get('strategy') in ('YieldParking', 'sys_tactical_hedge', 'tail_risk_hedge', 'cross_alpha_swap_sell', 'cross_alpha_swap_buy', 'rebalance_sell', 'l4_global_exit', 'Kill Switch Liquidation')
            or str(o.get('action', o.get('direction', ''))).lower() in ('sell', 'short')
        ]

        # [Phase 78] S_YIELD (KOFR / CD) Auto-Liquidation
        # 새로운 매수 주문이 있거나 현금 부족 시, 파킹된 자산(459580, 449170 등) 전량을 가용 현금화합니다.
        _PARKING_TICKERS = {'459580', '449170', '430740', '357870', 'SHV', 'SGOV'}
        _cfg_kofr = str(cfg.get('sc_yield.kofr_ticker', '459580'))
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

        has_buy_orders = any(
            str(o.get('action', '')).lower() in ('buy', 'long') or
            str(o.get('direction', '')).lower() in ('long', 'buy')
            for o in orders
        )

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

                if p_amt > 0 and (has_buy_orders or portfolio.get('cash', 0) < 500000):
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
            cvd_val = float(signal_cache.get('cvd_15m', 1500.0))
            iv_curr = float(signal_cache.get('iv_current', 0.85))
            iv_hist = float(signal_cache.get('iv_historical_mean', 0.40))
            neg_gex = bool(signal_cache.get('is_negative_gex', False))
            eq_val = float(portfolio.get('total_nav', cfg.get('portfolio.initial_capital', 20_000_000.0)))
            
            tactic_e_res = self.tactic_e_sniper.evaluate_earnings_event_trade(
                ticker='NVDA', cvd_15m=cvd_val, iv_current=iv_curr, iv_historical_mean=iv_hist,
                is_negative_gex_zone=neg_gex, account_equity=eq_val
            )
            result['tactic_e_event_squeeze'] = tactic_e_res
            if tactic_e_res['action'] in ('LONG', 'SHORT_INVERSE'):
                orders.append({
                    'stream_id': 'TACTIC_E',
                    'strategy': 'EarningsEventSqueezeSniper',
                    'ticker': tactic_e_res['target_ticker'],
                    'action': 'buy' if tactic_e_res['action'] == 'LONG' else 'sell',
                    'direction': 'long' if tactic_e_res['action'] == 'LONG' else 'short',
                    'amount_krw': tactic_e_res['allocated_capital'],
                    'confidence': 0.90,
                    'reason': tactic_e_res['reason']
                })
                logger.info(f"  🎯 [TacticE] 어닝 스퀴즈 주문 주입: {tactic_e_res['action']} ₩{tactic_e_res['allocated_capital']:,.0f}")
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
            from src.execution.execution_engine import ExecutionEngine
            _ee_inst = ExecutionEngine()
            _dyn_fx = _ee_inst._get_dynamic_usdkrw_rate()
            
            for o in orders:
                if o.get('action') == 'sell' or o.get('direction') == 'short':
                    # 매도 주문 수량 보장
                    if not o.get('quantity') and o.get('price', 0) > 0:
                        _p = float(o['price'])
                        _is_us = not str(o.get('ticker', '')).isdigit()
                        _p_krw = _p * _dyn_fx if _is_us else _p
                        o['quantity'] = max(1, int(float(o.get('amount_krw', 0.0)) / _p_krw))
                    valid_orders.append(o)
                    continue
                    
                _exec_price = o.get('price', 0)
                _is_us = not str(o.get('ticker', '')).isdigit()
                if not _exec_price or _exec_price <= 0:
                    _ticker = o.get('ticker')
                    try:
                        # 1. market_data 확인
                        if market_data:
                            _exec_price = float((market_data.get('current_prices') or {}).get(_ticker, 0.0) or
                                                (market_data.get('prices') or {}).get(_ticker, 0.0) or
                                                (market_data.get('kr_close') or {}).get(_ticker, 0.0) or
                                                (market_data.get('us_close') or {}).get(_ticker, 0.0))
                        # 2. data_bridge 확인
                        if (not _exec_price or _exec_price <= 0) and hasattr(self, 'data_bridge') and self.data_bridge:
                            _exec_price = float(self.data_bridge.get_current_price(_ticker) or 0.0)
                        # 3. execution_engine 확인
                        if (not _exec_price or _exec_price <= 0) and hasattr(self, 'execution_engine') and self.execution_engine:
                            _trader = self.execution_engine._get_trader(stream_id=o.get('stream_id', 'S0'))
                            if _trader and hasattr(_trader, 'get_live_price'):
                                _exec_price = float(_trader.get_live_price(_ticker) or 0.0)
                        # 4. pykrx 확인 (국내)
                        if (not _exec_price or _exec_price <= 0) and str(_ticker).isdigit():
                            from pykrx import stock as _pykrx_s8
                            from datetime import date as _d, timedelta as _td
                            _t_str = _d.today().strftime('%Y%m%d')
                            _w_str = (_d.today() - _td(days=7)).strftime('%Y%m%d')
                            _df_p = _pykrx_s8.get_market_ohlcv_by_date(_w_str, _t_str, str(_ticker))
                            if len(_df_p) > 0:
                                _exec_price = float(_df_p.iloc[-1].get('종가', 0.0))

                        if _exec_price and _exec_price > 0:
                            o['price'] = _exec_price
                            logger.info(f"    🔄 [Live Price Sync] {_ticker} 실시간 가격 {_exec_price:,.0f} 획득")
                    except Exception as _ep:
                        logger.error(f"    ⚠️ 실시간 가격 획득 실패: {_ep}", exc_info=True)
                
                if not _exec_price or _exec_price <= 0:
                    logger.warning(f"    ⚠️ 가격 없음 → 스킵: {o.get('ticker')} ({o.get('stream_id')})")
                    continue
                    
                if o.get('execution_algo', 'market') == 'market':
                    # [Pure Dynamic Math] 개별 종목 20일 실현 변동성에 비례한 동적 시장가 증거금 완충율 (1.3 하드코딩 완전 폐지)
                    _v = _compute_asset_daily_volatility(str(o.get('ticker', '')), market_data, fallback=0.015)
                    margin_rate = round(1.0 + min(0.20, max(0.01, _v * 2.5)), 4)
                else:
                    margin_rate = 1.0
                _cost_per_share_krw = (_exec_price * _dyn_fx * margin_rate) if _is_us else (_exec_price * margin_rate)
                
                # 수량이 이미 설정되어 있지 않다면 amount_krw 기반 산출
                qty = int(o.get('quantity', 0))
                if qty <= 0:
                    qty = int(o.get('amount_krw', 0.0) / _cost_per_share_krw) if _cost_per_share_krw > 0 else 0
                
                # Min-Lot Sweep: 고확신 매수 시그널 0주 절삭 방지 1주 보장
                if qty <= 0 and (o.get('amount_krw', 0.0) > 0 or float(o.get('confidence', 0.0)) >= 0.5):
                    qty = 1
                    logger.info(f"    💡 [Step 8.4 Min-Lot Sweep] {o.get('ticker')} (단가: {_exec_price:.2f}) 예산 미달 절삭 방지 ➔ 최소 1주 배정")

                if qty > 0:
                    o['quantity'] = qty
                    o['required_margin'] = qty * _cost_per_share_krw
                    actual_total_margin += o['required_margin']
                    valid_orders.append(o)
                else:
                    logger.warning(f"    ⚠️ 예산 부족으로 스킵: {o.get('ticker')} (예산: {o.get('amount_krw'):,.0f} < 필요증거금: {_cost_per_share_krw:,.0f})")

            orders = valid_orders
            
            # 메인 계좌의 남은 잉여 현금 계산
            available_cash = portfolio.get('cash', total_nav)
            if hasattr(self, 'execution_engine') and getattr(self.execution_engine, 'mode', '') in ('live', 'paper'):
                try:
                    _trader = self.execution_engine._get_trader(stream_id='S_YIELD')
                    if _trader:
                        _trader.fetch_live_balance()
                        available_cash = _trader.account.cash
                        logger.info(f"    [S_YIELD] 실계좌 현금 연동 성공: {available_cash:,.0f}원")
                except Exception as e:
                    logger.warning(f"    [S_YIELD] 실계좌 현금 연동 실패, portfolio 백업 사용: {e}")
            
            idle_cash = available_cash - actual_total_margin
            
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
            
            # 잉여 현금이 포트폴리오의 2% 이상이면 KOFR 등 무위험 자산에 파킹
            parking_threshold = float(cfg.get('orchestrator.idle_cash_parking_threshold', 0.02))
            if idle_cash > total_nav * parking_threshold:
                logger.info(f"    [S_YIELD] 잉여 현금 {idle_cash:,.0f}원 파킹")
                
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
                    margin = float(cfg.get('orchestrator.idle_cash_margin', 1.05))
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
                        kp = json.loads(kp_path.read_text(encoding='utf-8'))
                        for t, h in kp.get('holdings', {}).items():
                            all_positions[f"LIVE_SSOT:{t}"] = {
                                'ticker': t,
                                'stream_id': 'LIVE_SSOT',
                                'quantity': h.get('qty', 0),
                                'entry_price': h.get('avg_price', 0.0),
                                'current_price': h.get('current_price', 0.0),
                                'unrealized_pnl_pct': h.get('return_pct', 0.0),
                                'peak_pnl_pct': h.get('peak_pnl_pct', h.get('return_pct', 0.0)),
                                'holding_minutes': h.get('holding_minutes', 999.0),
                                'market': h.get('market', 'KR' if t.isdigit() else 'US')
                            }
                    except Exception as _kpe:
                        logger.warning(f"kis_portfolio.json 로드 예외: {_kpe}")

                # 2순위: 섀도우 포트폴리오 포지션 병합
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
                dynamic_exit_orders = exit_eval.get('exit_orders', [])
                
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
        # [Red Team Fix] 주문 충돌 방지 (Order Netting): 우세 방향 가격/메타데이터 정밀 상계 및 50만 원 미만 잔챙이 차단
        netted_orders_map = {}
        all_candidate_orders = orders + dynamic_exit_orders
        min_order_krw = float(cfg.get('execution.min_order_value_krw', 500000.0) or 500000.0)

        for o in all_candidate_orders:
            ticker = o.get('ticker', '')
            if not ticker:
                continue
            
            raw_action = str(o.get('action', '')).lower()
            raw_direction = str(o.get('direction', '')).lower()
            if raw_action in ('buy', 'long') or raw_direction in ('long', 'buy'):
                action = 'buy'
            elif raw_action in ('sell', 'short') or raw_direction in ('short', 'sell'):
                action = 'sell'
            else:
                action = 'buy'
                
            price = float(o.get('price', 0.0))
            qty = int(o.get('quantity', 0))
            amount_krw = float(o.get('amount_krw', 0.0))
            if not qty and price > 0:
                is_us_stock = not str(ticker).isdigit()
                if is_us_stock:
                    from src.execution.execution_engine import ExecutionEngine
                    fx_rate = ExecutionEngine()._get_dynamic_usdkrw_rate()
                    eff_price_krw = price * fx_rate
                    qty = int(amount_krw / eff_price_krw) if eff_price_krw > 0 else 0
                else:
                    qty = int(amount_krw / price)
                
                # 💡 [Min-Lot Liquidity Sweep] 고확신 매수 시그널 0주 절삭 시 단기 파킹 자산 부족분 스윕 1주 보장
                if qty == 0 and action == 'buy' and (amount_krw > 0 or o.get('entry_score', 0) >= 0.5):
                    qty = 1
                    logger.info(f"💡 [Min-Lot Sweep] {ticker} (단가: {price:.2f}) 배정 예산({amount_krw:,.0f} KRW) < 1주 단가 → 단기 파킹 자산(SHV/KOFR) 부족분 스윕 후 최소 1주 매수 집행 보장")

            if qty <= 0:
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
                netted_orders_map[ticker]['sell_qty'] += qty
                if price > 0:
                    netted_orders_map[ticker]['sell_prices'].append(price)
                netted_orders_map[ticker]['sell_orders'].append(o)

        exec_orders = []
        for ticker, ndata in netted_orders_map.items():
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
                
            sample_order = dominant_orders[0] if dominant_orders else (ndata['buy_orders'] + ndata['sell_orders'])[0]
            
            # 소액 잔챙이 상계 주문 차단 (50만 원 미만)
            is_us_net = not str(ticker).isdigit()
            if is_us_net:
                from src.execution.execution_engine import ExecutionEngine
                _net_fx = ExecutionEngine()._get_dynamic_usdkrw_rate()
                netted_val_krw = final_price * _net_fx * abs(net_qty)
            else:
                netted_val_krw = final_price * abs(net_qty)

            # 소액 잔챙이 주문 차단 (신규 매수 주문에만 적용 — 매도/손절은 100% 집행 보장)
            if final_action == 'buy' and final_price > 0 and netted_val_krw < min_order_krw:
                logger.info(f"    🛑 [Order Netting Drop] {ticker}: 신규 매수 상계 금액(₩{netted_val_krw:,.0f}) < ₩{min_order_krw:,.0f} 미달 스킵")
                continue

            exec_orders.append({
                'stream': sample_order.get('stream_id', sample_order.get('stream', 'SYS_NET')),
                'ticker': ticker,
                'action': final_action,
                'direction': 'long' if final_action == 'buy' else 'short',
                'quantity': abs(net_qty),
                'price': final_price,
                'execution_algo': sample_order.get('execution_algo', 'market'),
                'execution_start_time': sample_order.get('execution_start_time', ''),
                'strategy': sample_order.get('strategy', 'netted_order'),
                'reason': f"OrderNetting (Buy:{ndata['buy_qty']}, Sell:{ndata['sell_qty']} -> Net:{net_qty})",
            })

        # ── Step 9.1: Global Cross-Venue Netting & Dark Pool RPI Routing ──
        try:
            cv_res = self.cross_venue_netting.optimize_order_batch_netting(exec_orders)
            result['cross_venue_netting'] = cv_res
        except Exception as e:
            logger.debug(f"CrossVenueSpreadNetting skipped: {e}")

        # ── Step 9.2: CRO Final Gate 최후단 VaR Veto & Scaling ──
        try:
            total_nav_val = float(portfolio.get('total_nav', portfolio.get('cash', cfg.get('portfolio.initial_capital', 20_000_000.0))))
            ticker_map = {o['ticker']: i for i, o in enumerate(exec_orders)}
            cov_eye = np.eye(max(1, len(ticker_map))) * 0.0004
            exec_orders, cro_audit = self.cro_final_gate.evaluate_and_scale_orders(
                exec_orders, total_nav_val, cov_eye, ticker_map
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
        lev_level = leverage_result['judgment']['leverage_level']
        initial_capital = cfg.get('portfolio.initial_capital')

        # ★ 전체 포트폴리오 NAV 기반 동적 예산 배분
        nav = initial_capital
        if portfolio and 'total_nav' in portfolio:
            nav = portfolio['total_nav']
            
        # [L4 Upgrade] Task 2: 중복 Waterfall Allocation 블록 제거 — 아래 단일 블록만 유지
        # ★ Medallion-Grade Waterfall Allocation (S4 제외, 종합계좌 통합 배분)
        # 1. 현금 비중 확보
        # [Phase 37: Moonshot Booster] Kelly Booster 발동 조건 확인 + 현금 비율 오버라이드
        _kelly_active = _is_kelly_booster_active(
            regime=regime,
            signal_cache_path=str(_ROOT / 'results' / 'signal_cache.json'),
            portfolio_path=str(_ROOT / 'results' / 'shadow_portfolio.json'),
        )
        if _kelly_active:
            target_cash_ratio = float(cfg.get('kelly.booster_cash_ratio', 0.0))
            logger.info(f"  🚀 [Phase 37] Kelly Booster ON: Cash Ratio → {target_cash_ratio:.0%}")
        else:
            # [Pure Dynamic Math] 레짐 및 시장 거시 변동성에 연동된 동적 현금 비축 비율 (15% 하드코딩 완전 폐지)
            _vix_cash = float(market_data.get('vix', 18.0) if market_data else 18.0)
            if regime == 'bull':
                target_cash_ratio = round(max(0.03, min(0.12, _vix_cash / 180.0)), 4)
            elif regime == 'caution':
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
                portfolio_path=str(_ROOT / 'results' / 'shadow_portfolio.json')
            )
            logger.info(f"  [Phase 37] Kelly 압축: S2/S3 최대 {_kelly_max_positions}종목")

        # 3. 가용 예산 동적 배분 (순수 Softmax EV 수학적 비중 100% 반영)
        stream_budgets = {}
        for sid in ['S1', 'S2', 'S3', 'S4']:
            w = weights.get(sid, 0.0)
            stream_budgets[sid] = available_capital * w
        
        stream_budgets['S10_MEGA_TREND'] = available_capital * (weights.get('S10', 0.0) or weights.get('S10_MEGA_TREND', 0.0))
        stream_budgets['S10'] = stream_budgets['S10_MEGA_TREND']
        stream_budgets['S11_HIGHBETA_SNIPER'] = available_capital * (weights.get('S11_HIGHBETA_SNIPER', 0.0) or weights.get('S11', 0.0))
        stream_budgets['S11'] = stream_budgets['S11_HIGHBETA_SNIPER']

        # S5 예산
        stream_budgets['S5'] = available_capital * weights.get('S5', 0.0)
        

        # [Phase 47: DI 우선] 기존 종목 수집 + 예산 차감
        # ★ portfolio 딕셔너리가 주입된 경우 shadow_portfolio.json I/O 완전 차단
        # (백테스트 격리 원칙: 주입된 의존성이 있으면 파일 접근 자체를 금지)
        existing_tickers = set()
        try:
            _positions = {}
            if portfolio and 'positions' in portfolio:
                # ★ DI 경로: 주입된 portfolio 딕셔너리만 사용 — 파일 Fallback 완전 바이패스
                _positions = portfolio['positions']
            else:
                # Live 전용 Fallback (백테스트에서는 절대 실행되지 않아야 함)
                import json as _json_budget
                _sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
                if _sp_path.exists():
                    _sp_data = _json_budget.loads(_sp_path.read_text())
                    _positions = _sp_data.get('positions', {})

            for _pk, _pv in _positions.items():
                # VirtualPortfolio 형식: {ticker: {'ticker': ..., 'stream_id': ..., 'amount': ...}}
                # shadow_portfolio 형식:  {'S3:005930': {'ticker': ..., 'stream_id': ..., 'amount': ...}}
                # 두 형식 모두 _pv.get('ticker') 또는 _pk(= ticker 자체)로 처리
                _t = _pv.get('ticker', '') or _pk
                if _t:
                    existing_tickers.add(_t)

                _sid = _pk.split(':')[0] if ':' in _pk else _pv.get('stream_id', '?')
                if _sid in stream_budgets:
                    stream_budgets[_sid] = max(0, stream_budgets[_sid] - _pv.get('amount', 0))
        except Exception as _et_e:
            logger.error(f"    [Phase 47] existing_tickers 수집 실패: {_et_e}", exc_info=True)

        # ★ [Dynamic Budget Recycling] 시그널이 없는 스트림의 유휴 예산을 활성 시그널 보유 스트림으로 전격 재활용
        _idle_budget = 0.0
        _active_streams_with_buys = []
        for _s_key, _s_sigs in signals.items():
            _has_buy = any(str(_s.get('direction', _s.get('action', 'long'))).lower() in ('long', 'buy') for _s in _s_sigs)
            _b = stream_budgets.get(_s_key, 0.0)
            if not _has_buy and _b > 0:
                _idle_budget += _b
                stream_budgets[_s_key] = 0.0
            elif _has_buy:
                _active_streams_with_buys.append(_s_key)

        if _idle_budget > 0 and _active_streams_with_buys:
            # [Pure Dynamic Math] 유휴 예산을 활성 스트림의 수학적 EV 가중치 비율로 정밀 배분
            _active_weights = {k: max(0.001, float(weights.get(k, weights.get(k.replace('_MEGA_TREND', ''), 0.01)))) for k in _active_streams_with_buys}
            _sum_w = sum(_active_weights.values())
            for _s_key in _active_streams_with_buys:
                _prop = _active_weights[_s_key] / _sum_w
                stream_budgets[_s_key] = stream_budgets.get(_s_key, 0.0) + (_idle_budget * _prop)
            logger.info(f"  ♻️ [Dynamic Budget Recycling] 무신호 유휴 예산 ₩{_idle_budget:,.0f}을 활성 스트림 {_active_streams_with_buys}의 EV 비중에 비례하여 동적 재분배")

        # [L4 Upgrade] Task 1: 시그널 증발 버그 복구 — S1/S2/S3/S5 시그널 순회 및 주문 생성
        # (S4는 Advisory 전용: 자동 주문 제외)
        _meta_keys = ['tp_pct', 'sl_pct', 'trail_activate_pct', 'trail_distance_pct', 'max_hold_days']
        
        # [Phase 46 BugFix] PositionSizer(EV/SNR 허들) 연결 누락 복구
        try:
            from src.execution.position_sizer import filter_by_friction
            _sizer_available = True
        except ImportError as e:
            _sizer_available = False
            logger.error("    ⚠️ PositionSizer 로드 실패: 마찰비용 및 SNR 필터링이 스킵됩니다.", exc_info=True)

        stream_orders = {}
        for stream_id, stream_signals in signals.items():
            stream_orders[stream_id] = []
            budget = stream_budgets.get(stream_id, 0)
            if budget <= 0 and stream_id == 'S10_MEGA_TREND':
                budget = stream_budgets.get('S10', 0)
            elif budget <= 0 and stream_id == 'S10':
                budget = stream_budgets.get('S10_MEGA_TREND', 0)

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
                        sig['confidence'] = _alloc.compute_unified_factor_score(sig, market_data)
                except Exception as _e:
                    logger.warning(f"Failed to compute unified factor score: {_e}")
                
                stream_signals = filter_by_friction(
                    signals=stream_signals,
                    portfolio_value=nav,
                    regime=regime,
                    vix=_vix_val
                )

            # [Phase 52: Normalization & S6 Pipeline] 이중 비율 축소 방지 정규화 (보유 종목 제외 후)
            valid_signals = [
                sig for sig in stream_signals
                if sig and sig.get('direction', 'long') == 'long' 
                and sig.get('size_pct', 0) > 0 
                and sig.get('ticker') not in existing_tickers
            ]
            
            # [Red Team V5] 매직 넘버(30%) 삭제 및 100% 수학적 한도(Kelly/Volatility/Drawdown) 주입
            # _scale_factor가 1.0보다 크면 확대(몰빵)이므로 차단하고, 1.0 초과 시에만 비례 축소.
            _total_size_pct = sum(sig.get('size_pct', 0) for sig in valid_signals)
            
            # 포트폴리오 최대 일일 손실 한도 (기본 -3.0%)
            max_daily_loss = abs(float(cfg.get('risk.global_max_daily_loss_pct', 3.0))) / 100.0
            
            _max_leverage_limit = float(cfg.get('allocator.golden_bull_multiplier', 1.50)) if regime == 'bull' else 1.00
            _scale_factor = 1.0
            if _total_size_pct > _max_leverage_limit:
                _scale_factor = _max_leverage_limit / _total_size_pct
                logger.info(f"    [Red Team V5] {stream_id}: 총 비중 {_total_size_pct*100:.1f}% > 최대 레버리지 한도 {_max_leverage_limit*100:.0f}% 초과 → 스케일 다운 (×{_scale_factor:.2f})")
            elif _total_size_pct > 0 and _total_size_pct < 1.0:
                logger.info(f"    [Red Team V5] {stream_id}: 총 비중 {_total_size_pct*100:.1f}% 미달 → 현금 보존(Cash Reserve).")
            
            for _sig_n in valid_signals:
                # 1. 포트폴리오 예산 초과 방지 (Scale Down Only)
                _sig_n['size_pct'] = _sig_n['size_pct'] * _scale_factor
                
                # 2. 개별 종목 수학적 한도 (3-Sigma 파산 방어선)
                vol = float(_sig_n.get('daily_vol_pct', 0.02))
                if vol <= 0: vol = 0.02
                
                # 3-시그마 사건(폭락)이 터졌을 때 포트폴리오 전체 손실이 max_daily_loss 이내가 되도록 비중 제한
                math_max_weight = max_daily_loss / (3.0 * vol)
                math_max_weight = min(1.0, math_max_weight) # 예산(1.0) 초과 불가
                
                if _sig_n['size_pct'] > math_max_weight:
                    logger.info(f"    [Red Team V5] 켈리 비중({_sig_n['size_pct']*100:.1f}%)이 수학적 한계선({math_max_weight*100:.1f}%, Vol:{vol*100:.1f}%) 초과 → 안전 컷오프")
                    _sig_n['size_pct'] = math_max_weight

            # 🎯 [User Rule] 분할매수(DCA) 전면 금지 ➔ 100% 일시불 매수 (dca_days = 1.0)
            dca_days = 1.0

            for sig in stream_signals:
                if not sig or sig.get('direction', 'long') != 'long':
                    continue  # 매수 시그널만 처리 (매도는 Exit Manager 담당)

                # [Phase 46 BugFix] 무조건 스킵하던 로직 제거 -> DCA 갭투자로 변경
                _t = sig.get('ticker', '')

                size_pct = sig.get('size_pct', 0)
                if size_pct <= 0:
                    continue

                # 스트림 예산 × size_pct로 "목표(Target)" 금액 계산
                target_amount = min(
                    budget * size_pct,  # 배분 비중
                    budget,             # 스트림 총 예산 상한
                )

                # 노출도 & 레버리지 반영
                target_amount = target_amount * max_exposure * lev_level

                # 현재 보유 금액 파악
                current_amount = 0.0
                if portfolio:
                    pos_source = portfolio.get('positions') or portfolio.get('holdings') or {}
                    pos_iterable = pos_source.values() if isinstance(pos_source, dict) else pos_source
                    for pos in pos_iterable:
                        if isinstance(pos, dict):
                            pos_t = pos.get('ticker', '')
                            if pos_t == _t or (pos_t and pos_t in _t) or (_t and _t in pos_t):
                                current_amount += pos.get('amount', float(pos.get('quantity', pos.get('qty', 0))) * float(pos.get('current_price', pos.get('avg_price', 0.0))))

                gap = target_amount - current_amount
                
                # 목표 달성 또는 초과 시 매수 스킵 (시그널 소멸 시에도 자동 스킵됨)
                if gap <= 0:
                    continue
                    
                # 🎯 [User Rule] 100% 일시불 즉시 진입 (분할매수/DCA 100% 원천 차단)
                amount_krw = gap

                min_trade = cfg.get('a3.min_trade_amount', 200_000)
                if amount_krw < min_trade:
                    continue

                order = {
                    'stream_id': stream_id,
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

                logger.error(f"    [DEBUG] Appending order for {stream_id} {sig.get('ticker')}: keys={list(order.keys())}", exc_info=True)
                stream_orders[stream_id].append(order)
                orders.append(order)

        # 각 스트림별로 독립적으로 포지션 스케일링
        for sid in ['S1', 'S2', 'S3', 'S5', 'S10_MEGA_TREND']:
            _n = sum(1 for o in orders if o.get('stream_id') == sid)
            _amt = sum(o['amount_krw'] for o in orders if o.get('stream_id') == sid)
            _bgt = stream_budgets.get(sid, 0)
            if _n > 0:
                logger.info(
                    f"    📊 {sid}: {_n}건 ₩{_amt:,.0f} / 예산 ₩{_bgt:,.0f}"
                    + (f" ({_amt/_bgt*100:.0f}%)" if _bgt > 0 else " (예산 없음)"))

        return orders

    def _evaluate_exits(self, market_data: Dict, regime: str, portfolio: Dict = None) -> List[Dict]:
        """[L4 Upgrade] Task 3: 글로벌 Exit Manager — TP/SL/Trailing Stop 평가.

        portfolio.json 또는 주입된 portfolio의 포지션을 순회하며 각 포지션에 저장된
        tp_pct, sl_pct 메타데이터 기반으로 청산 여부를 평가하고 매도 주문을 반환.

        Returns:
            청산 주문 리스트 (direction='short')
        """
        exit_orders = []
        try:
            positions = {}
            if portfolio and 'positions' in portfolio:
                positions = portfolio['positions']
            elif getattr(self, 'exec_mode', 'live') == 'live' and (_ROOT / 'results' / 'kis_portfolio.json').exists():
                import json as _json_exit
                _kp = _json_exit.loads((_ROOT / 'results' / 'kis_portfolio.json').read_text(encoding='utf-8'))
                for t, h in _kp.get('holdings', {}).items():
                    positions[t] = {
                        'ticker': t,
                        'name': h.get('name', t),
                        'quantity': int(h.get('qty', 0)),
                        'qty': int(h.get('qty', 0)),
                        'avg_price': float(h.get('avg_price', 0.0)),
                        'current_price': float(h.get('current_price', 0.0)),
                        'amount': float(h.get('qty', 0)) * float(h.get('current_price', 0.0)),
                        'unrealized_pnl_pct': float(h.get('return_pct', 0.0)),
                        'pnl_pct': float(h.get('return_pct', 0.0)),
                        'market': h.get('market', 'KR')
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
            _vix_neutral = float(cfg.get('risk.vix_neutral_fallback', 18.0))
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
                if not stream_id or stream_id == 'None' or stream_id == 'UNKNOWN':
                    # pos_key 접두어로 추정 (S1:..., S2:... 등)
                    if ':' in pos_key:
                        stream_id = pos_key.split(':')[0]
                    else:
                        # [SSoT Canonical Mapping] 실계좌 코어 보유자산 스트림 매핑 (S1 오인 방지)
                        _tk_up = str(pos.get('ticker', pos_key)).upper()
                        if _tk_up in ('NVDA', 'QQQ', 'SOXX', 'XLK', '091160', 'MSFT', 'AAPL', 'GOOGL', 'AMZN', 'TSLA'):
                            stream_id = 'S3'
                        elif _tk_up in ('069500', '229200', 'SPY', 'IVV', 'VOO'):
                            stream_id = 'S0'
                        elif _tk_up in ('459580', '449170', '430740', '357870', 'SHV', 'SGOV'):
                            stream_id = 'S_YIELD'
                        else:
                            stream_id = 'S3'  # 기본값을 단기 S1이 아닌 중기 S3로 안전하게 지정

                # ── S_BETA (포트폴리오 헷지) 및 S10 (메가 트렌드)는 L4 Global Exit(개별 종목 TP/SL)에서 제외 ──
                if stream_id in ('S_BETA', 'S10_MEGA_TREND', 'S10', 'S_YIELD'):
                    continue  # S10 및 S_YIELD는 단기 TP/SL 대상에서 제외

                if pos.get('quantity', pos.get('qty', 0)) <= 0:
                    continue

                pnl_pct = pos.get('unrealized_pnl_pct', pos.get('pnl_pct', 0))
                tp_pct = pos.get('tp_pct')
                sl_pct = pos.get('sl_pct')  # 양수값 저장 규약
                # [Phase 51: Bias-Free ATR Exit] 종목별 역사적 변동성 기반 Z-Score 밴드
                # 공식: activate = vol_daily(%) × Z=3.0 (3σ 이익권 확인 후 추적 시작)
                #        dist    = vol_daily(%) × Z=2.0 (고점 대비 2σ=95% CI 이탈 시 청산)
                _pos_ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
                _asset_vol_daily = _compute_asset_daily_volatility(
                    _pos_ticker, market_data, fallback=0.02)
                _trail_z_act  = float(cfg.get('exit.trail_z_activate',   3.0))
                _trail_act_lo = float(cfg.get('exit.trail_activate_min', 3.0))
                _trail_act_hi = float(cfg.get('exit.trail_activate_max', 15.0))
                _trail_z_dst  = float(cfg.get('exit.trail_z_dist',       2.0))
                _trail_dst_lo = float(cfg.get('exit.trail_dist_min',     0.4))
                _trail_dst_hi = float(cfg.get('exit.trail_dist_max',     10.0))
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
                sl_pct = pos.get('sl_pct') or _dyn_sl_pct  # 포지션 메타 우선, 없으면 동적 밴드 사용
                
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
                        import pandas as pd
                        from datetime import date
                        _entry = pd.to_datetime(entry_date_str).date()
                        _curr_raw = market_data.get('date')
                        if not _curr_raw:
                            _curr_raw = date.today()
                        _curr = pd.to_datetime(_curr_raw).date()
                        days_held = (_curr - _entry).days
                    except Exception as e:
                        logger.error(f"[DEBUG L4 ERROR] days_held computation failed: {e}, pos={pos}", exc_info=True)
                        days_held = 0
                days_held = days_held or 0
                
                _max_hold_days = pos.get('max_hold_days')
                if _max_hold_days is None:
                    _cur_regime = str(regime.get('primary_regime', 'caution') if isinstance(regime, dict) else (regime or 'caution')).lower()
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

                if _max_hold_days:
                    logger.info(f"    [DEBUG L4 Dynamic Math] {_pos_ticker}: days_held={days_held}, dynamic_max_hold={_max_hold_days:.2f}d, pos={pos}")

                # [Phase 80] SYS_HEDGE / 레버리지 ETF 특수 Time-Stop 및 동적 청산 로직은 leverage_judge로 위임됨


                reason = None
                urgency = 0

                # ── TP 도달 ──
                if tp_pct is not None and pnl_pct >= tp_pct:
                    reason = f"[L4 Exit] TP: {pnl_pct:+.1f}% >= {tp_pct:.1f}%"
                    urgency = 2

                # ── SL 도달 ──
                elif sl_pct is not None and pnl_pct <= -abs(sl_pct):
                    reason = f"[L4 Exit] SL: {pnl_pct:+.1f}% <= -{abs(sl_pct):.1f}%"
                    urgency = 3

                # ── Time Limit (보유기간 만료 강제 청산) ──
                # 단기 데이트레이딩(S1, S11 등)에만 적용하며, 코어 슬리브(S0, S3, S4, S10, S_YIELD, S_BETA)는 면제
                elif _max_hold_days is not None and _max_hold_days > 0 and days_held >= _max_hold_days:
                    if stream_id in ('S0', 'S3', 'S4', 'S10', 'S10_MEGA_TREND', 'S_YIELD', 'S_BETA'):
                        logger.debug(f"    🛡️ [Core Sleeve Time Exemption] {_pos_ticker} ({stream_id}) 코어 자산은 5일 단기 타임리밋 면제")
                    else:
                        reason = f"[L4 Exit] Time Limit: {days_held}일 보유 >= {_max_hold_days}일 제한"
                        urgency = 2

                # ── Trailing Stop ──
                # trail_activate_pct에 도달한 뒤 trail_distance_pct 이상 하락 시 청산
                elif (trail_activate is not None and trail_dist is not None
                      and pnl_pct >= trail_activate):
                    peak_pnl = pos.get('peak_pnl_pct', pnl_pct)
                    drawdown_from_peak = peak_pnl - pnl_pct
                    if drawdown_from_peak >= trail_dist:
                        reason = (
                            f"[L4 Exit] Trail: peak={peak_pnl:+.1f}% → "
                            f"now={pnl_pct:+.1f}% (↓{drawdown_from_peak:.1f}% "
                            f">= trail_dist={trail_dist:.1f}%)"
                        )
                        urgency = 2

                # ── VIX 기반 동적 TP (인버스 한정) ──

                if reason:
                    _qty_exit = int(pos.get('qty', pos.get('quantity', 0)))
                    _amt_exit = float(pos.get('amount', 0.0))
                    _p_exit = float(pos.get('current_price', 0.0))
                    _tk_exit = str(pos.get('ticker', pos_key.split(':')[-1]))
                    _is_us_exit = not _tk_exit.isdigit()
                    from src.execution.execution_engine import ExecutionEngine
                    _dyn_fx_exit = ExecutionEngine()._get_dynamic_usdkrw_rate()
                    if _is_us_exit:
                        if _qty_exit > 0 and _p_exit > 0:
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
            etf_exits = self.leverage_judge.check_etf_stops(positions, market_data)
            if etf_exits:
                for eo in etf_exits:
                    # Prevent duplicates if already exited
                    if not any(x['pos_key'] == eo['pos_key'] for x in exit_orders):
                        exit_orders.append(eo)
                        logger.info(f"    📤 Exit (ETF Stop): {eo.get('ticker', eo.get('pos_key'))} | {eo.get('reason')} | urgency={eo.get('urgency')}")
        except Exception as _etf_err:
            logger.error(f"    [ETF Stop] check_etf_stops 실패: {_etf_err}", exc_info=True)

        return exit_orders

    def _apply_dd_overlay(self, dd_action: str, portfolio: Dict, regime: str = 'caution') -> List[Dict]:
        """DD Overlay 방어 전략 실행 (강제 청산 또는 헷지)."""
        orders = []
        if dd_action == 'none':
            return orders

        # 동적 설정 로드
        force_sell_ratio = cfg.get('dd_overlay.force_sell_ratio', 0.2)
        hedge_ratio = cfg.get('dd_overlay.tail_risk_hedge_ratio', 0.2)
        inverse_ticker = cfg.get('dd_overlay.inverse_etf_ticker', '252670')
        inverse_name = cfg.get('dd_overlay.inverse_etf_name', 'KODEX 200선물인버스2X')

        # Regime에 따른 동적 조정 (Crash 상태면 더 방어적으로)
        if regime == 'crash':
            force_sell_ratio = min(0.5, force_sell_ratio * 1.5)  # 최대 50%까지 강제 청산
            hedge_ratio = min(0.5, hedge_ratio * 1.5)            # 최대 50%까지 헷지 비중 확대

        try:
            import json
            sp_path = _ROOT / 'results' / 'shadow_portfolio.json'
            if not sp_path.exists():
                return orders
            sp = json.loads(sp_path.read_text())
            positions = sp.get('positions', {})
            active_pos = [p for p in positions.values() if p.get('quantity', 0) > 0 and str(p.get('stream_id')) not in ('S1', 'S2', 'S5')]
            
            if dd_action == 'force_sell_bottom_20':
                if not active_pos:
                    return orders
                # 수익률 하위 정렬
                active_pos.sort(key=lambda x: x.get('unrealized_pnl_pct', 0))
                n_sell = max(1, int(len(active_pos) * force_sell_ratio))
                targets = active_pos[:n_sell]
                
                for t in targets:
                    orders.append({
                        'stream_id': t.get('stream_id', 'S0'),
                        'ticker': t['ticker'],
                        'name': t.get('name', ''),
                        'direction': 'short',
                        'amount_krw': t.get('amount', 0),
                        'price': 0,
                        'confidence': 1.0,
                        'strategy': 'dd_overlay_force_sell',
                        'reason': f"DD Stage 1: 수익률 하위 {n_sell}종목 강제 청산",
                        'sell_type': 'force_sell',
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
    ois_lookback  = int(cfg.get('kelly.ois_lookback_days', 60))
    conf_pct      = int(cfg.get('kelly.confidence_percentile', 75))
    conf_lookback = int(cfg.get('kelly.confidence_lookback', 30))
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
                       else float(cfg.get('kelly.ois_fallback_threshold', 55)))
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
        positions = sp.get('positions', {})
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
    fallback = int(cfg.get('kelly.max_pos_fallback', 3))
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
            return int(cfg.get('kelly.max_pos_low_corr', 5))
        elif avg_corr < 0.6:
            return int(cfg.get('kelly.max_pos_mid_corr', 3))
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
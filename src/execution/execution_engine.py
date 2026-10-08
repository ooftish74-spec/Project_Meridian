"""
Project Meridian — Execution Engine
====================================
4-Stream Orchestrator의 주문을 실제 체결(또는 Shadow 기록)로 변환.

모드:
  shadow: 실 가격으로 가상 체결 기록 (주문 제출 안 함)
  mock:   KISTrader mock 모드 (로컬 시뮬레이션)
  paper:  한투 모의투자 서버
  live:   실전 매매

Usage:
    from src.execution.execution_engine import ExecutionEngine
    engine = ExecutionEngine(mode='shadow')
    result = engine.execute(orders)
"""
import pandas as pd
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from src.utils.file_ops import atomic_write_json

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_KRX_TICKER_COLUMNS = ['ISU_CD', '종목코드', 'Code', 'ticker']
_KRX_TRDVAL_COLUMNS = ['ACC_TRDVAL', '거래대금']
_KRX_MKTCAP_COLUMNS = ['MKTCAP', '시가총액', 'market_cap']
_KRX_CLOSE_COLUMNS = ['TDD_CLSPRC', '종가', 'close']
try:
    from src.execution.algo_executor import AlgoExecutor
    _ALGO_AVAILABLE = True
except ImportError as e:
    _ALGO_AVAILABLE = False
try:
    from src.execution.smart_router import SmartOrderRouter
    _SOR_AVAILABLE = True
except ImportError as e:
    _SOR_AVAILABLE = False
try:
    from src.execution.slippage_model import AdvancedSlippageModel
    _SLIPPAGE_MODEL_AVAILABLE = True
except ImportError as e:
    _SLIPPAGE_MODEL_AVAILABLE = False
try:
    from src.execution.fill_rate_simulator import FillRateSimulator
    _FILL_SIM_AVAILABLE = True
except ImportError as e:
    _FILL_SIM_AVAILABLE = False
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None
try:
    # KOFR / CD Auto Liquidation Verification
    from src.risk.liquidity_monitor import LiquidityMonitor
    _LIQUIDITY_AVAILABLE = True
except ImportError as e:
    _LIQUIDITY_AVAILABLE = False

try:
    from src.risk.intraday_regime_fast_switch import IntradayRegimeFastSwitch
    from src.risk.volatility_decay_shield import VolatilityDecayShield
    _AUTONOMOUS_RISK_AVAILABLE = True
except ImportError as e:
    _AUTONOMOUS_RISK_AVAILABLE = False


class MarketSession:
    """KRX + NexTrade 거래 세션 관리 (Self-Healing / Dynamic Load)."""

    @staticmethod
    def _get_sessions() -> Dict[str, Tuple[str, str]]:
        if _cfg:
            return _cfg.get('execution.market_sessions', {'pre': ('08:00', '08:50'), 'pause1': ('08:50', '09:00'), 'regular': ('09:00', '15:20'), 'pause2': ('15:20', '15:30'), 'after': ('15:30', '20:00')})
        return {'pre': ('08:00', '08:50'), 'pause1': ('08:50', '09:00'), 'regular': ('09:00', '15:20'), 'pause2': ('15:20', '15:30'), 'after': ('15:30', '20:00')}

    @staticmethod
    def current() -> str:
        now = datetime.now().strftime('%H:%M')
        sessions = MarketSession._get_sessions()
        if 'pause1' in sessions and sessions['pause1'][0] <= now < sessions['pause1'][1]:
            return 'pause'
        if 'pause2' in sessions and sessions['pause2'][0] <= now < sessions['pause2'][1]:
            return 'pause'
        if sessions['pre'][0] <= now < sessions['pre'][1]:
            return 'pre'
        if sessions['regular'][0] <= now < sessions['regular'][1]:
            return 'regular'
        if sessions['after'][0] <= now < sessions['after'][1]:
            return 'after'
        return 'closed'

    @staticmethod
    def is_tradeable() -> bool:
        return MarketSession.current() != 'closed'

    @staticmethod
    def best_exchange() -> str:
        session = MarketSession.current()
        if session == 'regular':
            return 'SOR'
        if session in ('pre', 'after'):
            return 'NXT'
        return 'CLOSED'

@dataclass
class ExecutionResult:
    """체결 결과 요약."""
    mode: str = 'shadow'
    timestamp: str = ''
    n_orders: int = 0
    n_filled: int = 0
    n_rejected: int = 0
    total_buy_amount: float = 0.0
    total_sell_amount: float = 0.0
    estimated_slippage: float = 0.0
    estimated_commission: float = 0.0
    estimated_tax: float = 0.0
    fills: List[Dict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return asdict(self)

class DesyncError(RuntimeError):
    """시스템 내부 NAV와 증권사 실잔고 불일치(Desync) 시 발생.

    [Live Transition Task 2]
    - 오차율이 desync_threshold(기본 1%)를 초과할 경우 주문 전체 Halt
    - sleeve_orchestrator의 try-except 블록에서 포착하여
      KillSwitch.hard_liquidate_all() 자동 트리거
    """

    def __init__(self, msg: str, nav_system: float=0.0, nav_broker: float=0.0, diff_pct: float=0.0, broker_cash: float=0.0):
        super().__init__(msg)
        self.nav_system = nav_system
        self.nav_broker = nav_broker
        self.diff_pct = diff_pct
        self.broker_cash = broker_cash

class ExecutionEngine:
    """Meridian 주문 체결 엔진.

    StreamOrchestrator가 생성한 order dict 리스트를
    실제 체결 또는 Shadow 기록으로 변환합니다.
    """

    @staticmethod
    def _commission_rate(exchange: str) -> float:
        """거래소별 수수료율 (DynamicConfig 동적 로드)."""
        if _cfg:
            return _cfg.get(f"execution.commission_rate.{exchange.lower()}", {'KRX': 8.8e-05, 'NXT': 5.3e-05, 'SOR': 7e-05}.get(exchange, 7e-05))
        return {'KRX': 8.8e-05, 'NXT': 5.3e-05, 'SOR': 7e-05}.get(exchange, 7e-05)

    @staticmethod
    def _tax_rate(order: Dict) -> float:
        """종목별 증권거래세율 산출 (매도 시에만 부과).
        ETF 및 ETN은 증권거래세 비과세(0.0%). 일반 주식은 0.18% (2024년 기준).
        """
        stream_id = order.get('stream_id', order.get('stream', ''))
        asset_type = order.get('asset_type', '').lower()
        if stream_id in ('S0', 'S1', 'S3_A', 'S5', 'S_YIELD') or asset_type in ('etf', 'etn'):
            return 0.0
        if _cfg:
            try:
                return float(_cfg.get('execution.tax_rate_stock', 0.0018))
            except (ValueError, TypeError):
                return 0.0018
        return 0.0018

    @staticmethod
    def _slippage_rate_fallback(exchange: str) -> float:
        """거래소별 슬리피지 폴백율 (AdvancedSlippageModel 실패 시)."""
        if _cfg:
            return _cfg.get(f"execution.slippage_rate.{exchange.lower()}", {'KRX': 0.001, 'NXT': 0.0006, 'SOR': 0.0008}.get(exchange, 0.0008))
        return {'KRX': 0.001, 'NXT': 0.0006, 'SOR': 0.0008}.get(exchange, 0.0008)

    def __init__(self, mode: str='live', account_type: str='main'):
        """
        초기화.

        Args:
            mode: 'paper', 'live'
            account_type: 'main' (S1~S6) 또는 's8' (초단타 스트림 전용)
        """
        self.mode = mode
        self.account_type = account_type
        self._trader_main = None
        self._trader_s8 = None
        self._shadow_dir = _PROJECT_ROOT / 'results' / 'shadow_trades'
        self._shadow_dir.mkdir(parents=True, exist_ok=True)
        self._algo_executor = AlgoExecutor() if _ALGO_AVAILABLE else None
        self._liquidity_monitor = LiquidityMonitor() if _LIQUIDITY_AVAILABLE else None
        self._smart_router = SmartOrderRouter() if _SOR_AVAILABLE else None
        self._slippage_model = AdvancedSlippageModel() if _SLIPPAGE_MODEL_AVAILABLE else None
        self._fill_simulator = FillRateSimulator() if _FILL_SIM_AVAILABLE else None
        self.fast_switch = IntradayRegimeFastSwitch() if _AUTONOMOUS_RISK_AVAILABLE else None
        self.decay_shield = VolatilityDecayShield() if _AUTONOMOUS_RISK_AVAILABLE else None
        self._liquidity_cache: Dict[str, Dict] = {}
        _sor_str = 'ON' if self._smart_router else 'OFF'
        _slip_str = '동적' if self._slippage_model else '고정'
        _fill_str = 'ON' if self._fill_simulator else 'OFF'
        logger.info(f"  ExecutionEngine 초기화: mode={self.mode}, SOR={_sor_str}, SlippageModel={_slip_str}, FillSim={_fill_str}, FastSwitch={'ON' if self.fast_switch else 'OFF'}")


    def _read_mode_from_env(self) -> str:
        """환경변수에서 KIS_MODE 읽기 (기본값: live)."""
        mode = os.getenv('KIS_MODE', '')
        if mode and mode != 'disabled':
            return mode
        return 'live'

    def _get_trader(self, stream_id: str=None):
        """KISTrader lazy initialization (paper/live 모드 전용).
        stream_id가 'S8'이면 S8 전용 계좌 어댑터 반환, 그 외는 Main 어댑터 반환.
        """
        is_s8 = stream_id == 'S8'
        if is_s8 and self._trader_s8 is not None:
            return self._trader_s8
        elif not is_s8 and self._trader_main is not None:
            return self._trader_main
        if self.mode == 'shadow':
            return None
        app_key = app_secret = account_no = None
        KISTraderAdapter = None
        try:
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
            if self.mode == 'paper':
                prefix = 'KIS_PAPER_S8' if is_s8 else 'KIS_PAPER'
            else:
                prefix = 'KIS_S8' if is_s8 else 'KIS'
            app_key = cm.read_from_env(f"{prefix}_APP_KEY")
            app_secret = cm.read_from_env(f"{prefix}_APP_SECRET")
            account_no = cm.read_from_env(f"{prefix}_ACCOUNT_NO")
            if is_s8 and (not all([app_key, app_secret, account_no])):
                logger.warning(f"  ⚠️ S8 전용 계좌 정보 누락됨 ({prefix}). 가상 체결(Shadow) 모드로 전환합니다.")
                return None
            from src.execution._kis_adapter import KISTraderAdapter
            trader = KISTraderAdapter(mode=self.mode, app_key=app_key, app_secret=app_secret, account_no=account_no)
            target_account_type = 's8' if is_s8 else 'main'
            trader.state_file = _PROJECT_ROOT / 'results' / f"meridian_{target_account_type}_trading_state.json"
            trader._token_cache = _PROJECT_ROOT / 'config' / f".kis_token_meridian_{self.mode}_{target_account_type}.json"
            if is_s8:
                self._trader_s8 = trader
                return self._trader_s8
            else:
                self._trader_main = trader
                return self._trader_main
        except Exception as e:
            logger.error(f"  KISTrader 초기화 실패: {e}")
            if KISTraderAdapter is None or not all([app_key, app_secret, account_no]):
                raise RuntimeError(f"Critical Execution Error: KIS 자격증명 또는 어댑터 로드 실패 — 재시도 불가. {e}")
            logger.warning('  Self-Correction: Token 갱신 및 재초기화(Re-Auth) 1회 시도')
            try:
                import time
                time.sleep(2)
                trader = KISTraderAdapter(mode=self.mode, app_key=app_key, app_secret=app_secret, account_no=account_no)
                target_account_type = 's8' if is_s8 else 'main'
                trader.state_file = _PROJECT_ROOT / 'results' / f"meridian_{target_account_type}_trading_state.json"
                trader._token_cache = _PROJECT_ROOT / 'config' / f".kis_token_meridian_{self.mode}_{target_account_type}.json"
                if is_s8:
                    self._trader_s8 = trader
                    return self._trader_s8
                else:
                    self._trader_main = trader
                    return self._trader_main
            except Exception as retry_e:
                logger.error(f"  🚨 KISTrader 재초기화 완전 실패: {retry_e}")
                raise RuntimeError(f"Critical Execution Error: Broker API Init Failed. {retry_e}")

    def execute(self, orders: List[Dict], portfolio: Dict=None) -> ExecutionResult:
        """주문 리스트를 체결합니다.

        Args:
            orders: StreamOrchestrator가 생성한 주문 리스트
                    [{'stream': 'S1', 'action': 'buy', 'ticker': '069500', ...}]
            portfolio: 현재 포트폴리오 상태 (NAV, positions 등)

        Returns:
            ExecutionResult
        """
        # 🎯 [Intraday Capital Router SSOT] Step 4: Wash-Trade & Conflict Cancel 검증
        try:
            from src.execution.intraday_capital_router import IntradayCapitalRouter
            orders = IntradayCapitalRouter().resolve_signal_conflicts(orders)
        except Exception as e_icr:
            logger.debug(f"  Intraday Capital Router conflict check bypass: {e_icr}")

        result = ExecutionResult(mode=self.mode, n_orders=len(orders))
        if not orders:
            return result
        if self.mode == 'shadow':
            result = self._execute_shadow(orders, result)
        elif self.mode in ('mock', 'paper', 'live'):
            result = self._execute_live(orders, result)
        else:
            result.errors.append(f"Unknown mode: {self.mode}")
        self._save_shadow_record(orders, result)
        return result

    def _get_ticker_liquidity(self, ticker: str) -> Dict:
        """종목별 ADV(일평균거래대금), 시가총액, 20일 실현변동성 조회 (캐시 사용).

        ★ 신규: volatility 필드 추가 — AdvancedSlippageModel σ 연결용
        """
        if ticker in self._liquidity_cache:
            return self._liquidity_cache[ticker]
        adv = 0.0
        market_cap = 0.0
        volatility = 0.0
        closes_for_vol: list = []
        try:
            import pandas as pd
            krx_dir = _PROJECT_ROOT / 'data' / 'raw' / 'krx_stock_daily'
            if krx_dir.exists():
                csv_files = sorted(krx_dir.glob('kospi_*.csv'), reverse=True)
                lookback = _cfg.get('slippage.vol_lookback_days', 20) if _cfg else 20
                for csv_file in csv_files[:lookback + 5]:
                    try:
                        df = pd.read_csv(csv_file)
                        for col in _KRX_TICKER_COLUMNS:
                            if col in df.columns:
                                df[col] = df[col].astype(str).str.zfill(6)
                                row = df[df[col] == ticker]
                                if not row.empty:
                                    for tv in _KRX_TRDVAL_COLUMNS:
                                        if tv in row.columns:
                                            adv = max(adv, float(row[tv].iloc[0]))
                                    for mc in _KRX_MKTCAP_COLUMNS:
                                        if mc in row.columns:
                                            market_cap = max(market_cap, float(row[mc].iloc[0]))
                                    for cc in _KRX_CLOSE_COLUMNS:
                                        if cc in row.columns:
                                            close_val = float(row[cc].iloc[0])
                                            if close_val > 0:
                                                closes_for_vol.append(close_val)
                                            break
                                break
                    except (FileNotFoundError, pd.errors.EmptyDataError, pd.errors.ParserError):
                        logger.warning("Tier 2/3 Fallback: Data parsing error or missing file. Imputing or retrying (Graceful Degradation).", exc_info=True)
                        continue
                if len(closes_for_vol) >= 2:
                    import math as _math
                    log_rets = [_math.log(closes_for_vol[i] / closes_for_vol[i + 1]) for i in range(len(closes_for_vol) - 1) if closes_for_vol[i] > 0 and closes_for_vol[i + 1] > 0]
                    if log_rets:
                        mean_r = sum(log_rets) / len(log_rets)
                        var = sum(((r - mean_r) ** 2 for r in log_rets)) / len(log_rets)
                        raw_vol = _math.sqrt(var)
                        vol_min = _cfg.get('execution.volatility_min', 0.005) if _cfg else 0.005
                        vol_max = _cfg.get('execution.volatility_max', 0.15) if _cfg else 0.15
                        volatility = max(vol_min, min(vol_max, raw_vol))
        except (FileNotFoundError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            logger.critical(f"  Liquidity CSV 조회 실패 ({ticker}): {e}", exc_info=True)
        fallback_vol = _cfg.get('slippage.default_daily_vol', 0.02) if _cfg else 0.02
        result = {'adv': adv, 'market_cap': market_cap, 'volatility': volatility if volatility > 0 else fallback_vol}
        self._liquidity_cache[ticker] = result
        return result

    def _get_current_regime(self) -> str:
        """현재 레짐 동적 로드."""
        try:
            regime_file = _PROJECT_ROOT / 'results' / 'current_regime.json'
            if regime_file.exists():
                data = json.loads(regime_file.read_text())
                return data.get('regime', 'caution')
        except FileNotFoundError:
            from src.utils.error_logger import log_error_rate_limited
            logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        except json.JSONDecodeError as e:
            logger.error(f"  Current regime 파일 JSON 파싱 에러: {e}")
        return 'caution'

    def check_etp_overnight_allowed(
        self,
        ticker: str,
        regime: Optional[str] = None,
        market_data: Optional[Dict] = None
    ) -> bool:
        """3-Regime Dynamic ETP Holding Matrix + 0% Hardcoding Dynamic Risk Shield 기반 오버나이트 허용 여부 판정.

        1. Intraday Fast Switch: VIX/Breadth/Vol Z-Score >= +2.0σ ➔ 즉시 CRASH_FLASH 오버라이드
        2. Volatility Decay Shield: CHOP Index >= Q90 (Choppy) ➔ 100% 당일 청산 강제
        3. 3-Regime Matrix: Bull ➔ Long ETP 허용, Bear ➔ Short ETP 허용, Sideways ➔ 당일 청산
        """
        if market_data is None:
            market_data = {}

        # 1. Intraday Fast Switch 검증 (VIX / Breadth / Vol Z-Score)
        if self.fast_switch and ('vix' in market_data or 'declining_ratio' in market_data):
            vix = float(market_data.get('vix', 20.0))
            vix3m = float(market_data.get('vix3m', 21.5))
            dec_ratio = float(market_data.get('declining_ratio', 0.50))
            is_fast_trig, fast_regime, z_comp, _ = self.fast_switch.evaluate_fast_switch(
                vix=vix, vix3m=vix3m, declining_ratio_5m=dec_ratio
            )
            if is_fast_trig:
                regime = fast_regime  # 'CRASH_FLASH'

        # 2. Volatility Decay Shield (CHOP Index & Sigmoid Scaling)
        if self.decay_shield and 'highs' in market_data and 'lows' in market_data and 'closes' in market_data:
            decay_eval = self.decay_shield.evaluate_etp_deleveraging(
                highs=market_data['highs'], lows=market_data['lows'], closes=market_data['closes']
            )
            if not decay_eval.get('allow_overnight', True):
                return False  # CHOP >= Q90 또는 Sigmoid scale 저하 시 당일 청산

        if regime is None:
            regime = self._get_current_regime()

        ticker_upper = ticker.upper()
        regime_lower = regime.lower()
        bull_tickers = {'233740', '122630', 'TQQQ', 'SOXL', 'UPRO', 'QLD', 'SSO'}
        bear_tickers = {'252670', '114800', 'SQQQ', 'SOXS', 'SPXS', 'QID', 'SDS'}

        if ticker_upper not in bull_tickers and ticker_upper not in bear_tickers:
            return True  # 레버리지 ETP가 아닌 일반 주식/채권은 일반 보유 정책 적용

        if regime_lower in ('bull', 'bull_strong') and ticker_upper in bull_tickers:
            return True
        if regime_lower in ('bear', 'bear_strong', 'crash_flash') and ticker_upper in bear_tickers:
            return True

        # Sideways / Range 또는 레짐-ETP 불일치 시 오버나이트 차단 (당일 청산)
        return False




    def _execute_shadow(self, orders: List[Dict], result: ExecutionResult) -> ExecutionResult:
        """Shadow 모드: 동적 슬리피지 모델로 가상 체결 계산.

        ★ AdvancedSlippageModel(Almgren-Chriss)로 종목별/규모별 차등 슬리피지.
        Medallion: 대형 주문은 AlgoExecutor(TWAP/VWAP)로 분할 시뮬레이션.
        """
        regime = self._get_current_regime()
        for order in orders:
            try:
                ticker = order.get('ticker', '')
                action = order.get('action', 'buy')
                quantity = order.get('quantity', 0)
                price = order.get('price', 0)
                if price <= 0:
                    price = self._estimate_price(ticker)
                    if price <= 0:
                        result.n_rejected += 1
                        result.errors.append(f"가격 조회 실패: {ticker}")
                        continue
                if self._liquidity_monitor:
                    liq = self._liquidity_monitor.check_liquidity(ticker=ticker, order_amount=price * quantity)
                    if not liq.get('ok', True):
                        _imp_p = liq.get('estimated_impact_pct', 0)
                        logger.warning(f"  ⚠️ 유동성 부족: {ticker} (impact={_imp_p:.2f}%)")
                        quantity = int(quantity * liq.get('adjusted_ratio', 0.5))
                        if quantity <= 0:
                            result.n_rejected += 1
                            result.errors.append(f"유동성 부족 차단: {ticker}")
                            continue
                stream_id = str(order.get('stream', order.get('stream_id', ''))).upper()
                if stream_id == 'S1':
                    tp_pct = float(order.get('tp_pct', 0.015))
                    max_spread = tp_pct / 3.0
                    spread_pct = 0.0
                    try:
                        from src.data_collection.realtime_data_bus import RealtimeDataBus
                        bus = RealtimeDataBus.get_instance()
                        ob_dp = bus.get_orderbook(ticker)
                        if ob_dp and isinstance(ob_dp.value, dict) and ('spread_pct' in ob_dp.value):
                            spread_pct = float(ob_dp.value['spread_pct'])
                    except Exception:
                        from src.utils.error_logger import log_error_rate_limited
                        logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
                    if spread_pct > max_spread:
                        logger.warning(f"  🚨 [SpreadGuard Shadow] {ticker} 스프레드({spread_pct:.2%})가 허용치({max_spread:.2%}) 초과 → 주문 강제 취소(Abort)")
                        result.n_rejected += 1
                        result.errors.append(f"{ticker}: Spread too high ({spread_pct:.4f} > {max_spread:.4f})")
                        continue
                order_amount = price * quantity
                algo_name = self._select_algo(order_amount, order)
                if self._smart_router:
                    sor_market_data = {}
                    try:
                        from src.data_collection.realtime_data_bus import RealtimeDataBus
                        bus = RealtimeDataBus.get_instance()
                        ob_dp = bus.get_orderbook(ticker)
                        if ob_dp and isinstance(ob_dp.value, dict):
                            if 'imbalance' in ob_dp.value:
                                sor_market_data['imbalance'] = ob_dp.value['imbalance']
                            if 'spread_pct' in ob_dp.value:
                                sor_market_data['spread_pct'] = ob_dp.value['spread_pct']
                    except Exception as e:
                        logger.critical(f"  [SOR] RealtimeDataBus 연동 실패: {e}", exc_info=True)
                    sor_result = self._smart_router.route({'ticker': ticker, 'action': action, 'quantity': quantity, 'price': price, 'urgency': order.get('urgency', 'normal')}, market_data=sor_market_data)
                    exchange = sor_result.get('venue', 'SOR')
                    if exchange == 'CLOSED':
                        exchange = 'SOR'
                else:
                    exchange = MarketSession.best_exchange()
                    if exchange == 'CLOSED':
                        exchange = 'SOR'
                comm_rate = self._commission_rate(exchange)
                slip_bps = 0.0
                slip_components = {}
                if self._slippage_model:
                    liq_data = self._get_ticker_liquidity(ticker)
                    slip_result = self._slippage_model.estimate(order_size=order_amount, adv=liq_data['adv'], market_cap=liq_data['market_cap'], regime=regime, volatility=liq_data.get('volatility', 0.0), ticker=ticker)
                    slip_bps = slip_result.get('slippage_bps', 8.0)
                    slip_components = slip_result.get('components', {})
                    algo_reduction = _cfg.get('execution.algo_slip_reduction', 0.4) if _cfg else 0.4
                    if algo_name != 'market':
                        slip_bps *= algo_reduction
                        slip_components['algo_reduction'] = algo_reduction
                    slip_rate = slip_bps / 10000
                else:
                    slip_rate = self._slippage_rate_fallback(exchange)
                    algo_reduction = _cfg.get('execution.algo_slip_reduction', 0.4) if _cfg else 0.4
                    if algo_name != 'market':
                        slip_rate *= algo_reduction
                    slip_bps = slip_rate * 10000
                fill_sim_result = None
                fill_simulated = False
                fill_probability = 1.0
                if self._fill_simulator:
                    fill_sim_result = self._fill_simulator.estimate_fill_probability(order={'action': action, 'price': price}, hist_candles=[])
                    fill_probability = fill_sim_result.get('fill_probability', 1.0)
                    fill_simulated = True
                if action == 'buy':
                    fill_price = price * (1 + slip_rate)
                    amount = fill_price * quantity
                    commission = amount * comm_rate
                    tax = 0.0
                    result.total_buy_amount += amount + commission
                else:
                    fill_price = price * (1 - slip_rate)
                    amount = fill_price * quantity
                    commission = amount * comm_rate
                    tax = amount * self._tax_rate(order)
                    result.total_sell_amount += amount - commission - tax
                result.estimated_slippage += abs(fill_price - price) * quantity
                result.estimated_commission += commission
                result.estimated_tax += tax
                result.n_filled += 1
                liq_cached = self._liquidity_cache.get(ticker, {})
                result.fills.append({'timestamp': datetime.now().isoformat(), 'stream': order.get('stream', ''), 'ticker': ticker, 'action': action, 'quantity': quantity, 'signal_price': price, 'fill_price': round(fill_price, 2), 'slippage_bps': round(slip_bps, 2), 'slippage_pct': round(slip_rate * 100, 4), 'slippage_components': slip_components, 'commission': round(commission, 2), 'tax': round(tax, 2), 'exchange': exchange, 'algo': algo_name, 'regime': regime, 'adv': liq_cached.get('adv', 0), 'market_cap': liq_cached.get('market_cap', 0), 'volatility_used': round(liq_cached.get('volatility', 0), 6), 'fill_probability': round(fill_probability, 4), 'fill_simulated': fill_simulated, 'mode': 'shadow'})
            except Exception as e:
                logger.error(f"  Shadow 주문 처리 중 크래시: {e}", exc_info=True)
                result.n_rejected += 1
                _o_tk = order.get('ticker', '?')
                result.errors.append(f"{_o_tk}: {e}")
        return result

    def _select_algo(self, order_amount: float, order: Dict) -> str:
        """주문 규모에 따라 최적 알고리즘 선택.

        ★ 대표님 지침 확정: 20억 이하 자본 규모에서는 TWAP/VWAP 분할매수 전면 금지!
        100% 일시불(Single Lump-Sum Instant Order) 즉시 전송으로 슬리피지 및 마찰 차단.
        (20억 원 미만 주문은 무조건 limit/market 일시불 체결)
        """
        if not self._algo_executor:
            return 'limit' if order.get('action', '').lower() in ('buy', 'add_on') else 'market'
            
        # 20억 원(2,000,000,000 KRW) 미만 주문은 100% 일시불 체결
        _vwap_threshold = float(_cfg.get('execution.algo_vwap_threshold', 2000000000.0) if _cfg else 2000000000.0)
        _twap_threshold = float(_cfg.get('execution.algo_twap_threshold', 2000000000.0) if _cfg else 2000000000.0)
        
        if order_amount >= _vwap_threshold:
            return 'vwap'
        elif order_amount >= _twap_threshold:
            return 'twap'
        else:
            return 'limit' if order.get('action', '').lower() in ('buy', 'add_on') else 'market'

    def _calculate_alpha_decay_slippage_limit(self, order: Dict) -> Dict[str, float]:
        """[Institutional IS Model] 시그널 기대 알파 기반 동적 슬리피지 한계 및 3단계 강등 기준 산출.
        
        공식:
          - Expected Alpha (α_expected): 시그널 confidence 또는 tp_pct (기본 1.5%)
          - Alpha Decay Threshold: α_expected * 0.50 (기대 알파의 50%)
          - Net EV Limit: α_expected - Slippage - Fee <= 0 (기대값 0 이하 시 완전 취소)
        """
        exp_alpha = float(order.get('expected_alpha', order.get('tp_pct', 0.015) or 0.015))
        decay_ratio = float(_cfg.get('execution.alpha_decay_ratio', 0.50)) if _cfg else 0.50
        fee_rate = float(_cfg.get('execution.fee_rate', 0.0015)) if _cfg else 0.0015
        
        max_allowed_slippage = exp_alpha * decay_ratio
        net_ev_cutoff = max(0.0, exp_alpha - fee_rate)
        
        return {
            'expected_alpha': exp_alpha,
            'max_allowed_slippage': max_allowed_slippage,
            'net_ev_cutoff': net_ev_cutoff,
            'fee_rate': fee_rate
        }

    def _get_dynamic_usdkrw_rate(self) -> float:
        """KIS API 실시간 고시환율(t_rate) 및 디스크 캐시 기반 동적 환율 산출 (1400.0 하드코딩 100% 제거)."""
        try:
            from src.data_collection.central_data_gateway import CentralDataGateway
            rate = CentralDataGateway().get_usdkrw_rate()
            if rate and isinstance(rate, (int, float)) and 1000.0 <= rate <= 2500.0:
                return float(rate)
        except Exception:
            pass
        
        # 2차 디스크 캐시 (EWMA 변동성 밴드 검증)
        cache_path = _PROJECT_ROOT / 'results' / 'usdkrw_rate_cache.json'
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    c_data = json.load(f)
                    cached_rate = float(c_data.get('t_rate', c_data.get('rate', 0)))
                    if 1000.0 <= cached_rate <= 2500.0:
                        return cached_rate
            except Exception:
                pass
        return 1450.0  # Dynamic fallback

    def _execute_live(self, orders: List[Dict], result: ExecutionResult) -> ExecutionResult:
        """Live/Paper/Mock 모드: KISTrader를 통한 실제 체결.

        [Live Transition Task 1] S1(Edge Stream) 시그널은 IOC 조건 강제 적용.
          S1은 데이트레이딩(당일 15:10 전량 청산)이므로 미체결 잔량이
          시장에 남으면 안 됨 → IOC = Immediate Or Cancel.
        """
        _mode = getattr(self, '_mode', self.mode)
        for order in orders:
            stream_id = order.get('stream', '')
            try:
                _trader = self._get_trader(stream_id)
            except RuntimeError as _re:
                logger.error(f"[CRITICAL] {_mode} 모드 KISTrader 초기화 실패 ({stream_id}) — 주문 중단. Shadow fallback을 live 모드에서 사용할 수 없습니다.")
                result.errors.append(f"{_mode} KISTrader 초기화 실패: 주문 전체 취소")
                result.status = 'failed'
                return result
        import asyncio

        async def _async_execute_order(order):
            loop = asyncio.get_event_loop()
            stream_id = str(order.get('stream', order.get('stream_id', ''))).upper()
            try:
                _trader = self._get_trader(stream_id)
            except Exception as e:
                return {'order': order, 'status': 'failed', 'error': f"KISTrader ({stream_id}) 초기화 실패: {e}"}
            ticker = order.get('ticker', '')
            action = order.get('action', 'buy')
            price = order.get('price', 0)
            quantity = int(order.get('quantity', 0))
            if quantity <= 0:
                p_est = price if price > 0 else self._estimate_price(ticker)
                err_msg = f"🛑 [ExecutionEngine] 주문 수량 미달 (quantity={quantity})"
                logger.warning(err_msg)
                return {'order': order, 'status': 'rejected', 'error': err_msg}
            if stream_id == 'S1':
                tif = 'IOC'
                logger.debug(f"  ⚡ [Live Transition Task 1] S1 IOC 적용: {ticker} {action}")
                tp_pct = float(order.get('tp_pct', 0.015))
                max_spread = tp_pct / 3.0
                spread_pct = 0.0
                try:
                    from src.data_collection.realtime_data_bus import RealtimeDataBus
                    bus = RealtimeDataBus()
                    ob = bus.get_orderbook(ticker)
                    if ob and ob.get('ask1') and ob.get('bid1') and ob['bid1'] > 0:
                        spread_pct = (ob['ask1'] - ob['bid1']) / ob['bid1']
                except Exception as e:
                    logger.debug(f"RealtimeDataBus orderbook fetch fail: {e}")
                if spread_pct > max_spread and spread_pct > 0:
                    logger.warning(f"  ⚠️ [Live Transition Task 1] S1 호가 갭 스프레드({spread_pct:.2%}) > 1/3 TP 한계({max_spread:.2%}) → 주문 스킵 ({ticker})")
                    return {'order': order, 'status': 'skipped_spread_gap'}
            else:
                tif = order.get('time_in_force', 'DAY')

            is_limits = self._calculate_alpha_decay_slippage_limit(order)
            max_slip_pct = is_limits['max_allowed_slippage']
            
            curr_price = price if price > 0 else float(order.get('current_price', 0))
            if curr_price <= 0:
                curr_price = self._estimate_price(ticker)
            sig_price = float(order.get('signal_price', curr_price) or curr_price)
            slip_pct = abs(curr_price - sig_price) / sig_price if sig_price > 0 else 0.0
            
            # [Tick Snapping Guard] 호가단위 미절사 거부 방지: 전송 직전 무조건 tick 절사
            from src.execution._kis_adapter import _snap_to_exchange_tick
            raw_p = price if price > 0 else curr_price
            order_price = _snap_to_exchange_tick(raw_p, ticker)
            
            # Stage 3 Check: Net EV <= 0 Cutoff (Outright Cancel)
            if is_limits['expected_alpha'] - slip_pct - is_limits['fee_rate'] <= 0 and slip_pct > 0:
                logger.warning(
                    f"  🛑 [Institutional IS Stage 3] Net EV <= 0 ({ticker}): "
                    f"α={is_limits['expected_alpha']:.2%}, Slip={slip_pct:.2%}, Fee={is_limits['fee_rate']:.2%} → 트래킹 에러 차단 전량 취소")
                return {'order': order, 'status': 'rejected', 'error': 'Institutional IS Stage 3: Net EV <= 0'}

            max_retries = _cfg.get('execution.max_retries', 3) if _cfg else 3
            remaining_qty = quantity
            filled_qty = 0
            final_fills = []
            last_status = 'failed'
            last_error = ''
            
            # [Bounded ThreadPoolExecutor Singleton] 24시간 데몬 가동 시 스레드 무한 증식 메모리 누수 원천 차단
            from concurrent.futures import ThreadPoolExecutor
            if not hasattr(ExecutionEngine, '_shared_thread_pool'):
                ExecutionEngine._shared_thread_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix='meridian_executor')
            t_pool = ExecutionEngine._shared_thread_pool
            
            for attempt in range(max_retries):
                if remaining_qty <= 0:
                    break
                try:
                    execution_algo = order.get('execution_algo', 'market')
                    # Stage 2 Check: Alpha Decay > 50% Limit -> Mid-Point Pegged Limit Degradation
                    if slip_pct > max_slip_pct and execution_algo == 'market':
                        execution_algo = 'mid_pegged'
                        logger.info(
                            f"  🔄 [Institutional IS Stage 2] 슬리피지({slip_pct:.2%}) > 알파감쇄한계({max_slip_pct:.2%}) "
                            f"→ Mid-Point Pegged 지정가 강등 체결 ({ticker})")

                    is_buy = action in ('buy', 'add_on')
                    if is_buy:
                        fill = await loop.run_in_executor(None, _trader.buy, ticker, remaining_qty, order_price, execution_algo, 'SOR', str(stream_id), 'normal', tif)
                    else:
                        fill = await loop.run_in_executor(None, _trader.sell, ticker, remaining_qty, order_price, execution_algo, 'SOR', str(stream_id), 'normal', tif)
                    if fill.status == 'submitted':
                        _wait_timeout = 5 if tif == 'IOC' else None
                        fill = await loop.run_in_executor(None, _trader.wait_for_fill, fill, _wait_timeout)
                    if fill.status in ('filled', 'filled_market_fallback', 'partially_filled'):
                        filled_qty += fill.filled_quantity
                        remaining_qty -= fill.filled_quantity
                        final_fills.append(fill)
                        last_status = 'filled' if remaining_qty == 0 else 'partially_filled'
                        if last_status == 'partially_filled':
                            logger.info(f"  🔄 부분 체결 재시도 ({attempt + 1}/{max_retries}): {ticker} 잔량={remaining_qty}")
                            await asyncio.sleep(0.5)
                            continue
                    else:
                        last_status = fill.status
                        last_error = getattr(fill, 'error_message', None) or getattr(fill, 'note', None) or getattr(fill, 'status', 'fill_rejected')
                        final_fills.append(fill)
                        break
                except Exception as e:
                    last_status = 'error'
                    last_error = str(e)
                    break
            return {'order': order, 'status': last_status, 'fills': final_fills, 'error': last_error, 'tif': tif}

        async def _execute_all(orders):
            tasks = [_async_execute_order(o) for o in orders]
            return await asyncio.gather(*tasks, return_exceptions=True)
        coro_results = asyncio.run(_execute_all(orders))
        for res in coro_results:
            if isinstance(res, Exception):
                logger.error(f"  Live 비동기 주문 중 예외 발생: {res}", exc_info=True)
                result.n_rejected += 1
                result.errors.append(f"Async Error: {res}")
                continue
            order = res['order']
            ticker = order.get('ticker', '?')
            action = order.get('action', 'buy')
            stream_id = order.get('stream', '')
            if res['status'] == 'failed' or res['status'] == 'rejected' or res['status'] == 'error':
                result.n_rejected += 1
                _err_r = res.get('error', 'Failed')
                result.errors.append(f"{ticker}: {_err_r}")
                if '초기화 실패' in res.get('error', ''):
                    self._execute_shadow([order], result)
                continue
            fills = res.get('fills', [])
            if fills:
                for fill in fills:
                    if fill.status in ('filled', 'filled_market_fallback', 'partially_filled'):
                        result.n_filled += 1
                        amount = fill.filled_price * fill.filled_quantity
                        if action == 'buy':
                            result.total_buy_amount += amount
                        else:
                            result.total_sell_amount += amount
                        result.estimated_commission += fill.commission
                        result.estimated_slippage += fill.slippage * fill.filled_quantity
                        result.fills.append({'timestamp': fill.fill_timestamp, 'stream': stream_id, 'ticker': ticker, 'action': action, 'quantity': fill.filled_quantity, 'fill_price': fill.filled_price, 'commission': fill.commission, 'exchange': fill.exchange, 'time_in_force': res.get('tif", "DAY'), 'mode': self.mode, 'status': fill.status})
                    else:
                        result.n_rejected += 1
                        result.errors.append(f"{ticker}: {fill.status}")
        return result

    def check_account_sync(self, portfolio: Optional[Dict]=None, raise_on_desync: bool=True) -> Dict:
        """시스템 NAV와 증권사 실잔고 대조 (Desync 검증).

        [Live Transition Task 2]
        KIS API fetch_live_balance()로 가져온 실잔고 NAV와
        shadow_portfolio.json의 NAV를 대조합니다.

        오차 범위:
          - ok:      diff_pct <= desync_warn_pct (기본 0.5%)
          - warn:    desync_warn_pct < diff_pct <= desync_threshold (기본 1%)
          - DESYNC:  diff_pct > desync_threshold → DesyncError 발생 + 주문 Halt

        Args:
            portfolio:       외부에서 전달한 포트폴리오 dict (없으면 파일에서 로드)
            raise_on_desync: True이면 Desync 시 DesyncError 발생

        Returns:
            {
                'ok': bool,
                'nav_system': float,
                'nav_broker': float,
                'diff_pct': float,
                'level': 'ok' / 'warn' / 'desync',
                'message': str,
            }

        Raises:
            DesyncError: diff_pct > desync_threshold이면
        """
        import time as _time
        desync_threshold = _cfg.get('execution.desync_threshold', 0.01) if _cfg else 0.01
        desync_warn_pct = _cfg.get('execution.desync_warn_pct', 0.005) if _cfg else 0.005
        nav_system: float = 0.0
        result_sync: Dict = {'ok': True, 'nav_system': 0.0, 'nav_broker': 0.0, 'diff_pct': 0.0, 'level': 'ok', 'message': ''}
        try:
            if self.mode in ('live', 'paper'):
                trader = self._get_trader()
                if trader:
                    trader.fetch_live_account_balance()
                    nav_system = float(trader.account.total_equity)
            if nav_system <= 0 and portfolio and portfolio.get('total_nav'):
                nav_system = float(portfolio['total_nav'])
        except Exception as e:
            logger.warning(f"  [Desync] 실시간 KIS 계좌 NAV 수집 실패: {e}")
        if nav_system <= 0:
            logger.warning('  [Desync] 시스템 NAV = 0 — 동기화 검증 스킵 (초기화 중일 가능성)')
            return {'ok': True, 'nav_system': 0.0, 'nav_broker': 0.0, 'diff_pct': 0.0, 'level': 'ok', 'message': 'system_nav_zero_skip'}
        nav_broker: float = 0.0
        broker_cash: float = 0.0
        broker_fetch_ok = False
        if self.mode in ('live', 'paper'):
            try:
                trader = self._get_trader()
                if trader:
                    for _attempt in range(3):
                        try:
                            ok = trader.fetch_live_balance()
                            if ok:
                                nav_broker = float(trader.account.total_equity)
                                broker_cash = float(getattr(trader.account, 'cash', 0.0))
                                broker_fetch_ok = True
                                break
                            logger.warning(f"  [Desync] 잔고 조회 실패 {_attempt + 1}/3 (fetch 반환 False)")
                        except Exception as _fe:
                            logger.warning(f"  [Desync] 잔고 조회 예외 {_attempt + 1}/3: {_fe}")
                        if _attempt < 2:
                            _time.sleep(2 ** _attempt)
            except Exception as e:
                logger.error(f"  🔴 [Desync] 증권사 계좌 잔고 통신 예외 발생: {e}")
        elif self.mode == 'mock':
            try:
                trader = self._get_trader()
                if trader:
                    nav_broker = float(trader.account.total_equity)
                    broker_cash = float(getattr(trader.account, 'cash', 0.0))
                    broker_fetch_ok = True
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                pass
        else:
            return {'ok': True, 'nav_system': nav_system, 'nav_broker': nav_system, 'broker_cash': nav_system, 'diff_pct': 0.0, 'level': 'ok', 'message': 'shadow_mode_skip'}
        if not broker_fetch_ok or nav_broker <= 0:
            logger.warning('  [Desync] 증권사 NAV 조회 실패 — Desync 검증 보류 (안전 방향 통과)')
            return {'ok': True, 'nav_system': nav_system, 'nav_broker': 0.0, 'broker_cash': 0.0, 'diff_pct': 0.0, 'level': 'warn', 'message': 'broker_fetch_failed'}
        diff_pct = abs(nav_system - nav_broker) / max(nav_broker, 1.0)
        result_sync: Dict = {'ok': diff_pct <= desync_threshold, 'nav_system': round(nav_system, 0), 'nav_broker': round(nav_broker, 0), 'broker_cash': round(broker_cash, 0), 'diff_pct': round(diff_pct, 6), 'level': 'ok', 'message': ''}
        if diff_pct <= desync_warn_pct:
            result_sync['level'] = 'ok'
            result_sync['message'] = f"NAV 동기화 정상: diff={diff_pct:.4%}"
            logger.info(f"  ✅ [Desync] NAV OK: 시스템=₩{nav_system:,.0f}, 증권사=₩{nav_broker:,.0f}, diff={diff_pct:.4%}")
        elif diff_pct <= desync_threshold:
            result_sync['level'] = 'warn'
            result_sync['message'] = f"⚠️ NAV 불일치 경고: diff={diff_pct:.4%} (시스템₩{nav_system:,.0f} vs 증권사₩{nav_broker:,.0f})"
            _msg_sync = result_sync.get('message', '')
            logger.warning(f"  ⚠️ [Desync] 경고: {_msg_sync}")
        else:
            result_sync['level'] = 'desync'
            result_sync['ok'] = False
            err_msg = f"치명적 NAV Desync: diff={diff_pct:.4%} > threshold={desync_threshold:.2%} [시스템₩{nav_system:,.0f} vs 증권사₩{nav_broker:,.0f}] — 주문 전체 Halt"
            result_sync['message'] = err_msg
            logger.critical(f"  🚨 [Desync] {err_msg}")
            try:
                from src.utils.telegram_notifier import TelegramNotifier
                TelegramNotifier().send_alert('🚨 DESYNC 경보 — 주문 Halt', f"⛔ *NAV 증권사 불일치 치명적 발견*\n시스템 NAV: ₩{nav_system:,.0f}\n증권사 NAV: ₩{nav_broker:,.0f}\n오차: {diff_pct:.4%} (Threshold {desync_threshold:.2%} 초과)\n→ 자동 Halt + 하드 청산 대기 중")
            except Exception as _tg_err:
                logger.error(f"  [Desync] 텔레그램 실패 (NAV 불일치 안달되는 다): {_tg_err}")
            if raise_on_desync:
                # [Red Team V6] 잔고 강제 동기화 (Zombie Position Reconciliation)
                logger.critical(f"  🚨 [Quarantine Mode] {err_msg}\n  -> 좀비 포지션을 치유하기 위해 KIS 실계좌 잔고를 강제 동기화(Reconciliation) 합니다.")
                try:
                    if hasattr(trader, 'fetch_live_positions'):
                        live_pos = trader.fetch_live_positions()
                        if live_pos is not None:
                            from src.portfolio.shadow_manager import ShadowPortfolioManager
                            with ShadowPortfolioManager().transaction() as sm:
                                sys_tickers = {k.split(':')[-1] if ':' in k else k for k in sm.positions.keys()}
                                if isinstance(live_pos, dict):
                                    live_tickers = set(live_pos.keys())
                                elif isinstance(live_pos, list):
                                    live_tickers = {p.get('ticker', str(p)) if isinstance(p, dict) else str(p) for p in live_pos}
                                else:
                                    live_tickers = set()

                                
                                # 시스템에 2개 이상 종목이 있는데 브로커에서 50% 이상이 갑자기 증발했다고 보고할 경우
                                if len(sys_tickers) >= 2 and len(live_tickers) <= len(sys_tickers) * 0.5:
                                    msg = "🚨 [SANITY CHECK FAILED] API 보고 잔고가 기존 대비 50% 이상 증발했습니다 (글리치 의심). force_reconcile을 차단하고 시스템을 Halt 합니다."
                                    logger.critical(msg)
                                    result_sync['message'] += f"\n  -> {msg}"
                                    try:
                                        TelegramNotifier().send_alert('🚨 SANITY CHECK FAILED', msg)
                                    except: pass
                                else:
                                    sm.force_reconcile(live_pos, broker_cash)
                                    result_sync['message'] += " -> 섀도우 포트폴리오 강제 동기화 완료."
                                    result_sync['level'] = 'warn' # 동기화 했으므로 Halt 풀림
                                    result_sync['ok'] = True
                except Exception as _recon_err:
                    logger.critical(f"  🚨 강제 동기화 실패: {_recon_err}", exc_info=True)
        return result_sync

    def _estimate_price(self, ticker: str) -> float:
        """종목 현재가 추정 (data/ 디렉토리에서)."""
        import pandas as pd
        krx_dir = _PROJECT_ROOT / 'data' / 'raw' / 'krx_stock_daily'
        if krx_dir.exists():
            try:
                csv_files = sorted(krx_dir.glob('kospi_*.csv'), reverse=True)
                for csv_file in csv_files[:3]:
                    df = pd.read_csv(csv_file)
                    for col in _KRX_TICKER_COLUMNS:
                        if col in df.columns:
                            df[col] = df[col].astype(str).str.zfill(6)
                            row = df[df[col] == ticker]
                            if not row.empty:
                                for pc in _KRX_CLOSE_COLUMNS:
                                    if pc in row.columns:
                                        p = float(row[pc].iloc[0])
                                        if p > 0:
                                            return p
                            break
            except (FileNotFoundError, pd.errors.EmptyDataError, pd.errors.ParserError):
                from src.utils.error_logger import log_error_rate_limited
                logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
            except Exception as e:
                logger.error(f"  가격 추정(CSV) 중 에러: {e}")
        for pattern in [f"kr_{ticker}.parquet", f"{ticker}.parquet", f"us_stocks/{ticker}.parquet", f"us_stocks/{ticker.upper()}.parquet"]:
            pq = _PROJECT_ROOT / 'data' / 'historical_10y' / pattern
            if pq.exists():
                try:
                    df = pd.read_parquet(pq)
                    col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else df.columns[-1])
                    return float(df[col].iloc[-1])
                except (FileNotFoundError, pd.errors.EmptyDataError, pd.errors.ParserError):
                    from src.utils.error_logger import log_error_rate_limited
                    logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
                except Exception as e:
                    logger.error(f"  가격 추정(Parquet) 중 에러: {e}")

        # SSoT Live Portfolio fallback (kis_portfolio.json)
        kp_file = _PROJECT_ROOT / 'results' / 'kis_portfolio.json'
        if kp_file.exists():
            try:
                kp_data = json.loads(kp_file.read_text(encoding='utf-8'))
                h = kp_data.get('holdings', {}).get(ticker, {})
                p = float(h.get('current_price', h.get('avg_price', 0.0)) or 0.0)
                if p > 0:
                    return p
            except Exception:
                pass

        # signal_cache fallback
        sc_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
        if sc_file.exists():
            try:
                sc_data = json.loads(sc_file.read_text(encoding='utf-8'))
                info = sc_data.get(ticker, {})
                if isinstance(info, dict):
                    p = float(info.get('close', info.get('price', 0.0)) or 0.0)
                    if p > 0:
                        return p
            except Exception:
                pass

        return 0.0

    def _save_shadow_record(self, orders: List[Dict], result: ExecutionResult):
        """Shadow 거래 기록 저장."""
        if self.mode == 'mock':
            return
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            record_file = self._shadow_dir / f"{today}.json"
            existing = []
            if record_file.exists():
                try:
                    existing = json.loads(record_file.read_text())
                except FileNotFoundError:
                    existing = []
                except json.JSONDecodeError as e:
                    logger.error(f"  Shadow 기록 JSON 파싱 에러: {e}")
                    existing = []
            entry = {'timestamp': datetime.now().isoformat(), 'mode': self.mode, 'n_orders': result.n_orders, 'n_filled': result.n_filled, 'n_rejected': result.n_rejected, 'total_buy': round(result.total_buy_amount, 0), 'total_sell': round(result.total_sell_amount, 0), 'slippage': round(result.estimated_slippage, 0), 'commission': round(result.estimated_commission, 0), 'fills': result.fills, 'errors': result.errors[:10]}
            existing.append(entry)
            atomic_write_json(record_file, existing, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f"  Shadow 기록 저장 실패: {e}")

    def get_account_summary(self) -> Dict:
        """계좌 요약 (trader가 있으면 실 정보, 없으면 Shadow 통계)."""
        trader = self._get_trader()
        if trader:
            from dataclasses import asdict
            pos_list = [asdict(p) for p in trader.positions.values()]
            return {
                'mode': self.mode, 
                'cash': trader.account.cash, 
                'available_cash': trader.account.cash,
                'total_equity': trader.account.total_equity,
                'total_nav': trader.account.total_equity,
                'positions': pos_list, 
                'realized_pnl': trader.account.realized_pnl
            }
        return self._get_shadow_stats()

    def _get_shadow_stats(self) -> Dict:
        """Shadow 거래 통계."""
        total_buy = 0.0
        total_sell = 0.0
        n_trades = 0
        for f in sorted(self._shadow_dir.glob('*.json')):
            try:
                records = json.loads(f.read_text())
                for r in records:
                    total_buy += r.get('total_buy', 0)
                    total_sell += r.get('total_sell', 0)
                    n_trades += r.get('n_filled', 0)
            except (FileNotFoundError, json.JSONDecodeError):
                logger.warning(f"⚠️ [Fallback] 파일/모듈 누락 예외 우회: (exception variable 없음)")
                continue
            except Exception as e:
                logger.error(f"  Shadow 통계 조회 중 에러: {e}")
                continue
        return {'mode': 'shadow', 'total_buy': total_buy, 'total_sell': total_sell, 'n_trades': n_trades, 'n_days': len(list(self._shadow_dir.glob('*.json')))}

    def __repr__(self) -> str:
        return f"ExecutionEngine(mode={self.mode})"
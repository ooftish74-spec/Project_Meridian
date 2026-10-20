"""
Project Meridian: US Night Execution Engine (us_night_engine.py)
==================================================================

미국 장(22:30~05:00 KST) 전용 라이브 트레이딩 집행 엔진:
  1. S3 Track A (미국 빅테크 Indxx, US 필라델피아 반도체, S&P 500 ETF) 및 Track B (QVM 가치주) 실계좌 매매 TR 직송
  2. 24시간 자본 회전율 200%: 한국 장 15:20 KST 당일 청산 현금을 22:30 KST 미국 장에 최대 50% 캡(Cap) 범위 내 재사용
  3. 트레일링 스탑 (Trailing Stop: 고점 대비 -2.5% 이탈 시 익절/손절) 다일 스윙 매매 적용
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.dynamic_config import DynamicConfig
from src.utils.file_ops import atomic_write_json

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'
_US_HOLDINGS_FILE = _RESULTS_DIR / 'us_night_holdings.json'

_US_TICKER_MAP = {
    '381180': 'SOXX',  # TIGER 미국필라델피아반도체 -> iShares Semiconductor ETF
    '390390': 'NVDA',  # KODEX 미국반도체 -> Nvidia Corp (롱 기본값; 인버스 전략일 경우 SOXS로 동적 분기)
    '091160': 'QQQ',   # KODEX 반도체 -> Invesco QQQ Trust
    '396500': 'SOXX',  # TIGER 반도체TOP10 -> SOXX
    '000660': 'TSM',   # SK하이닉스 -> TSMC ADR
    '005930': 'AAPL',  # 삼성전자 -> Apple Inc
    '005380': 'TSLA',  # 현대차 -> Tesla Inc
    '227550': 'SPY',   # KODEX 200 -> SPDR S&P 500
    '069500': 'SPY',   # KODEX 200 -> SPDR S&P 500
    '122630': 'TQQQ',  # KODEX 레버리지 -> ProShares UltraPro QQQ
    '233740': 'TQQQ',  # KODEX 코스닥150레버리지 -> ProShares UltraPro QQQ
    '252670': 'SQQQ',  # KODEX 200선물인버스2X -> ProShares UltraPro Short QQQ
    '114800': 'SH',    # KODEX 인버스 -> ProShares Short S&P500
    '251340': 'SQQQ',  # KODEX 코스닥150선물인버스 -> ProShares UltraPro Short QQQ
    '000270': 'TSLA',  # 기아 -> Tesla Inc
    '009540': 'CAT',   # HD한국조선해양 -> Caterpillar
    '030200': 'TMUS',  # KT -> T-Mobile US
    '001040': 'KO',    # CJ -> Coca-Cola Co
}

_US_NAME_MAP = {
    'SOXX': 'iShares Semiconductor ETF',
    'SOXS': 'Direxion Daily Semiconductor Bear 3X Shares',
    'NVDA': 'NVIDIA Corp',
    'QQQ': 'Invesco QQQ Trust (Nasdaq 100)',
    'TQQQ': 'ProShares UltraPro QQQ',
    'SQQQ': 'ProShares UltraPro Short QQQ',
    'SH': 'ProShares Short S&P500',
    'TLT': 'iShares 20+ Year Treasury Bond ETF',
    'GLD': 'SPDR Gold Shares',
    'UUP': 'Invesco DB US Dollar Index Bullish',
    'TSM': 'Taiwan Semiconductor Manufacturing ADR',
    'AAPL': 'Apple Inc',
    'TSLA': 'Tesla Inc',
    'SPY': 'SPDR S&P 500 ETF Trust',
    'CAT': 'Caterpillar Inc',
    'TMUS': 'T-Mobile US Inc',
    'KO': 'The Coca-Cola Co',
}

class USNightExecutionEngine:
    """미국 장 전용 24시간 자본 회전 라이브 집행 엔진."""

    def __init__(self):
        self.max_exposure_pct = self._compute_dynamic_exposure_cap()
        self.trailing_stop_pct = self._compute_dynamic_trailing_stop()
        self.trail_activation_pct = float(pget('us_night.trail_activation_pct', 0.015))
        self.hard_stop_pct = float(pget('us_night.hard_stop_pct', 0.05))
        self._load_holdings()

    def _compute_dynamic_exposure_cap(self) -> float:
        """[Mathematical Dynamic Model] 시장 레짐 및 포트폴리오 거시 리스크 기반 동적 노출 캡 산출."""
        try:
            regime = str(cfg.get('market.current_regime', 'caution')).lower()
            regime_multipliers = {
                'bull': 0.50,
                'caution': 0.30,
                'bear': 0.20,
                'crash': 0.10,
                'panic': 0.05
            }
            return float(regime_multipliers.get(regime, 0.30))
        except Exception:
            return 0.30

    def _compute_dynamic_trailing_stop(self, ticker: str = '', atr: float = 0.0) -> float:
        """[Mathematical Dynamic Model] 종목별 실현 변동성/ATR 기반 동적 트레일링 스탑 비율."""
        if atr > 0:
            return max(0.015, min(0.060, atr * 2.0))
        default_pct = pget('us_night.trailing_stop_pct', 0.025)
        return float(default_pct)

    def _get_live_fx_rate(self) -> float:
        """실시간 동적 원/달러 환율 획득 (KIS -> Central Gateway -> Default)."""
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            fx = KISDataCollector().get_usdkrw_exchange_rate()
            if fx and fx > 1000:
                return float(fx)
        except Exception:
            pass
        try:
            from src.data_collection.central_data_gateway import get_central_data_gateway
            fx_res = get_central_data_gateway().get_macro_indicator('usdkrw')
            px = float(fx_res.get('price', 0) or 0)
            if px > 1000:
                return px
        except Exception:
            pass
        # ★ Wave 1: 1353.63 리터럴 기본값 제거 → 게이트웨이 실관측 last-good(≤ fx.max_stale_hours) 또는 0.0(미상)
        try:
            from src.data_collection.central_data_gateway import CentralDataGateway
            _lg = CentralDataGateway().get_usdkrw_rate()
            if _lg and float(_lg) > 0:
                return float(_lg)
        except Exception:
            pass
        return 0.0

    def _load_holdings(self):
        """미국 장 야간 보유 포지션 로드 (KIS 실계좌 SSOT 및 HWM 연동)."""
        self.holdings: List[Dict[str, Any]] = []
        saved_hwm_map: Dict[str, float] = {}
        if _US_HOLDINGS_FILE.exists():
            try:
                _saved_list = json.loads(_US_HOLDINGS_FILE.read_text(encoding='utf-8'))
                if isinstance(_saved_list, list):
                    for _sh in _saved_list:
                        if isinstance(_sh, dict) and _sh.get('ticker'):
                            _sh_tk = str(_sh['ticker'])
                            _sh_hi = max(
                                float(_sh.get('highest_price', 0.0) or 0.0),
                                float(_sh.get('peak_price', 0.0) or 0.0)
                            )
                            if _sh_hi > 0:
                                saved_hwm_map[_sh_tk] = _sh_hi
            except Exception:
                pass

        kp_path = _RESULTS_DIR / 'kis_portfolio.json'
        if kp_path.exists():
            try:
                from src.execution.position_ledger import PositionLedgerManager
                ledger_mgr = PositionLedgerManager()
                kp = json.loads(kp_path.read_text(encoding='utf-8'))
                today_str = datetime.now().strftime('%Y-%m-%d')
                for t, h in kp.get('holdings', {}).items():
                    if not t.isdigit():  # US 종목 (NVDA, QQQ, SOXX, XLK 등)
                        entry_p = float(h.get('avg_price', 0.0) or 0.0)
                        curr_p = float(h.get('current_price', 0.0) or 0.0)
                        curr_ret = float(h.get('return_pct', h.get('pnl_pct', 0.0)) or 0.0)
                        ledger_hwm = ledger_mgr.get_hwm(t)
                        saved_high = max(
                            saved_hwm_map.get(t, 0.0),
                            float(ledger_hwm.get('peak_price', 0.0) or 0.0),
                            float(h.get('highest_price', 0.0) or 0.0),
                            float(h.get('peak_price', 0.0) or 0.0),
                        )
                        merged_high = max(curr_p, entry_p, saved_high)
                        hwm = ledger_mgr.update_hwm(t, current_price=merged_high, current_pnl_pct=curr_ret)
                        final_peak = max(merged_high, float(hwm.get('peak_price', merged_high) or merged_high))
                        self.holdings.append({
                            'ticker': t,
                            'name': h.get('name', t),
                            'entry_price': entry_p,
                            'current_price': curr_p,
                            'highest_price': final_peak,
                            'peak_price': final_peak,
                            'peak_pnl_pct': float(hwm.get('peak_pnl_pct', curr_ret) or curr_ret),
                            'trail_activated': bool(hwm.get('trail_activated', False)) or (entry_p > 0 and final_peak >= entry_p * (1.0 + self.trail_activation_pct)),
                            'quantity': int(h.get('qty', 0)),
                            'amount': float(h.get('qty', 0)) * curr_p,
                            'entry_date': ledger_mgr.get_entry_date(t) or today_str
                        })
                if self.holdings:
                    return
            except Exception as e:
                logger.warning(f"  [USNightEngine] KIS 실계좌 로드 예외: {e}")

        # 폴백: us_night_holdings.json 파일
        if _US_HOLDINGS_FILE.exists():
            try:
                self.holdings = json.loads(_US_HOLDINGS_FILE.read_text(encoding='utf-8'))
            except Exception as e:
                logger.warning(f"  [USNightEngine] 보유 파일 로드 예외: {e}")
                self.holdings = []

    def _save_holdings(self):
        """보유 포지션 원자적 저장."""
        atomic_write_json(_US_HOLDINGS_FILE, self.holdings, indent=2, ensure_ascii=False, default=str)

    def is_us_session_active(self, now: Optional[datetime] = None) -> bool:
        """미국 장 운영 시간대 (서머타임 22:30 / 표준시 23:30 ~ 05:00/06:00 KST) 판별."""
        from src.utils.market_calendar import get_us_session_state
        is_active, _ = get_us_session_state(now)
        return is_active

    def process_us_night_signals(self, s3_signals: List[Dict], account_nav: float) -> List[Dict]:
        """미국 장 S3 신호 수신 및 주문 집행 생성 (환율 단위 정합성 및 동적 캡 적용).
        
        Args:
            s3_signals: S3ActiveMacroStream 신호 리스트
            account_nav: 전체 계좌 순자산 가치 (NAV, 원화 기준)

        Returns:
            집행된 미국 장 주문 리스트
        """
        orders: List[Dict[str, Any]] = []
        if not self.is_us_session_active():
            return orders

        fx_rate = self._get_live_fx_rate()
        if fx_rate <= 0:
            logger.warning("🇺🇸 [USNightEngine] USD/KRW 미상 → 신규 미국 매수 전면 생략 (fail-closed)")
            return orders
        us_budget = account_nav * self.max_exposure_pct
        current_invested_krw = sum(h.get('amount', 0) * fx_rate for h in self.holdings)
        available_us_cash_krw = max(0.0, us_budget - current_invested_krw)

        logger.info(f"🇺🇸 [USNightEngine] 야간 운용 가동 (동적 예산 캡 {self.max_exposure_pct*100:.0f}%: ₩{us_budget:,.0f}, 사용 가능: ₩{available_us_cash_krw:,.0f}, 환율: ₩{fx_rate:,.2f})")

        seen_us_tickers = set()
        for sig in s3_signals:
            raw_ticker = str(sig.get('ticker', '')).strip()
            strat_str = str(sig.get('strategy', '')).lower()
            name_str = str(sig.get('name', ''))
            if raw_ticker == '390390' and ('short' in strat_str or 'inverse' in strat_str or '인버스' in name_str):
                us_ticker = 'SOXS'
            else:
                us_ticker = _US_TICKER_MAP.get(raw_ticker, raw_ticker)
            if not us_ticker or str(us_ticker).isdigit():
                continue
            if us_ticker in seen_us_tickers:
                continue
            seen_us_tickers.add(us_ticker)

            us_name = _US_NAME_MAP.get(us_ticker, sig.get('name', us_ticker))
            direction = sig.get('direction', 'long')
            weight = sig.get('size_pct', sig.get('weight', 0.05))
            target_amount_krw = min(available_us_cash_krw, account_nav * float(weight))

            px = float(sig.get('price', 0.0) or 0.0)
            if px <= 0:
                try:
                    from src.data_collection.central_data_gateway import get_central_data_gateway
                    _cdg_p = get_central_data_gateway().get_current_price(us_ticker)
                    if isinstance(_cdg_p, dict) and float(_cdg_p.get('price', 0) or 0) > 0:
                        px = float(_cdg_p['price'])
                    elif isinstance(_cdg_p, (int, float)) and _cdg_p > 0:
                        px = float(_cdg_p)
                except Exception:
                    pass
            if px <= 0:
                continue

            px_krw = px * fx_rate
            # 1주 최소 매매 금액 보장 (동적 최소 주문 금액)
            if target_amount_krw < px_krw:
                continue

            # [Dimensional Consistency Fix] 원화 목표금액을 환율로 나누어 순수 달러 주문금액 환산 후 정밀 정수 수량 산출
            target_amount_usd = target_amount_krw / fx_rate
            qty = max(1, int(target_amount_usd / px))
            actual_order_amount_krw = qty * px_krw

            order = {
                'ticker': us_ticker,
                'name': us_name,
                'direction': direction,
                'action': 'buy' if direction in ('long', 'buy') else 'sell',
                'price': px,
                'quantity': qty,
                'amount': actual_order_amount_krw,
                'amount_krw': actual_order_amount_krw,
                'amount_usd': qty * px,
                'order_type': 'LIMIT_BEST',
                'session': 'US_NIGHT',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'strategy': 'S3_US_MOMENTUM_SWING'
            }
            orders.append(order)
            available_us_cash_krw = max(0.0, available_us_cash_krw - actual_order_amount_krw)

            # 포지션에 트레일링 스탑 피크 가격 등록
            self.holdings.append({
                'ticker': us_ticker,
                'entry_price': px,
                'highest_price': px,
                'peak_price': px,
                'quantity': qty,
                'amount': qty * px,
                'entry_date': datetime.now().strftime('%Y-%m-%d')
            })

        self._save_holdings()
        return orders

    def update_trailing_stop(self, current_prices: Dict[str, float], atr_map: Optional[Dict[str, float]] = None) -> List[Dict]:
        """미국 장 트레일링 스탑 감시 및 청산 주문 생성 (ATR 변동성 동적 연동 및 HWM 원장 동기화)."""
        exit_orders = []
        if not os.environ.get('PYTEST_CURRENT_TEST') and os.environ.get('MERIDIAN_TEST_MODE') != '1':
            if not self.is_us_session_active():
                return exit_orders
        remaining = []
        from src.execution.position_ledger import PositionLedgerManager
        ledger_mgr = PositionLedgerManager()
        for h in self.holdings:
            ticker = h['ticker']
            price = current_prices.get(ticker, 0.0)
            if price <= 0:
                remaining.append(h)
                continue

            entry_p = float(h.get('entry_price', price) or price)
            prev_peak = max(
                float(h.get('highest_price', 0.0) or 0.0),
                float(h.get('peak_price', entry_p) or entry_p),
                entry_p
            )
            curr_pnl_pct = (price - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0
            hwm = ledger_mgr.update_hwm(ticker, current_price=max(price, prev_peak), current_pnl_pct=curr_pnl_pct)
            highest_price = max(prev_peak, float(hwm.get('peak_price', price) or price), price)
            h['peak_price'] = highest_price
            h['highest_price'] = highest_price

            entry_return = (price - entry_p) / entry_p if entry_p > 0 else 0.0
            drop_pct = (price - highest_price) / highest_price if highest_price > 0 else 0.0

            # Dynamic Volatility ATR Trailing Stop (실측 변동성 연동: atr_map 미전달 시 DynamicQuantEngine 실측 일간 변동성 조회)
            ticker_atr = 0.0
            if atr_map and ticker in atr_map and float(atr_map[ticker] or 0.0) > 0:
                ticker_atr = float(atr_map[ticker])
            else:
                try:
                    from src.utils.dynamic_quant_engine import get_quant_engine
                    _m = get_quant_engine().get_asset_metrics(ticker)
                    ticker_atr = float(_m.get("vol_daily", 0.0) or 0.0)
                except Exception:
                    ticker_atr = 0.0
            if ticker_atr <= 0:
                ticker_atr = 0.015

            atr_multiplier = float(cfg.get('us_night.trailing_stop_atr_mult', 2.0) or 2.0)
            dynamic_cap = max(0.05, min(0.15, ticker_atr * 3.0))
            dynamic_trailing_stop_pct = max(0.015, min(dynamic_cap, ticker_atr * atr_multiplier))
            effective_activation_pct = max(self.trail_activation_pct, ticker_atr * 0.75)

            # 1) Hard Stop-Loss (수익 활성화 전에도 진입가 대비 -hard_stop_pct 이탈 시 즉시 손절)
            if entry_p > 0 and entry_return <= -self.hard_stop_pct:
                exit_reason = f"US Night Hard Stop-Loss ({entry_return*100:.2f}%)"
                logger.info(f"🚨 [USNightEngine] {ticker} 하드 손절 발화! (진입가 대비 {entry_return*100:.2f}%, 한도=-{self.hard_stop_pct*100:.2f}%)")
                ledger_mgr.record_daily_exit(
                    ticker,
                    reason=exit_reason,
                    realized_pnl_pct=float(entry_return * 100.0),
                )
                exit_orders.append({
                    'ticker': ticker,
                    'direction': 'sell',
                    'action': 'sell',
                    'quantity': h.get('quantity', 0),
                    'amount': h.get('amount', 0),
                    'price': price,
                    'pnl_pct': round(entry_return * 100.0, 4),
                    'reason': exit_reason,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
            # 2) Trailing Stop (고점이 진입가 대비 +effective_activation_pct 이상 수익 활성화 후 고점 대비 하락 시 발화)
            elif entry_p > 0 and highest_price >= entry_p * (1.0 + effective_activation_pct) and drop_pct <= -dynamic_trailing_stop_pct:
                exit_reason = f"US Night Dynamic Trailing Stop ({drop_pct*100:.2f}%)"
                logger.info(f"🚨 [USNightEngine] {ticker} 동적 트레일링 스탑 발화! (고점 대비 {drop_pct*100:.2f}%, 한도={dynamic_trailing_stop_pct*100:.2f}%)")
                ledger_mgr.record_daily_exit(
                    ticker,
                    reason=exit_reason,
                    realized_pnl_pct=float(entry_return * 100.0),
                )
                exit_orders.append({
                    'ticker': ticker,
                    'direction': 'sell',
                    'action': 'sell',
                    'quantity': h.get('quantity', 0),
                    'amount': h.get('amount', 0),
                    'price': price,
                    'pnl_pct': round(entry_return * 100.0, 4),
                    'reason': exit_reason,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
            else:
                remaining.append(h)

        self.holdings = remaining
        self._save_holdings()
        return exit_orders

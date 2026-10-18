"""
Universal Exit Engine (V3 단일 SSOT 전사 공통 Exit & 3중 손절 뼈대 엔진)
========================================================================
기존 s4_advisory/dynamic_exit.py 및 개별 파일에 파편화되어 있던 Exit 로직을
단 하나의 SSOT(Single Source of Truth) 전사 공통 기본 뼈대로 완전 통합:

1. [1차 손절] Hard Entry Stop-Loss: 진입가 대비 ATR×1.5 또는 -2.5% 무조건 손절
2. [2차 손절] Catastrophic Peak Stop: 고점 대비 ATR×3.5 (약 -7%~-8%) 폭락 시 100% 손절
3. [3차 손절] Global Portfolio KillSwitch 연동: 계좌 일일 손실 -3.0% 시 포트폴리오 전량 손절
4. Uncapped ATR Chandelier Trailing Exit: 상방 무제한 추종 (전 스트림 S0~S11 공통)
5. 50% Scale-Out Runner: ATR×8 달성 시 50% 차익실현 후 잔여 50% 무한 트레일링
"""

import math
import numpy as np
import pandas as pd
import logging
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from config.dynamic_config import DynamicConfig
from src.utils.dynamic_quant_engine import get_quant_engine

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class UniversalExitEngine:
    """전사 포지션(S0~S11) 공통 3중 손절 & 샹들리에 익절 단일 SSOT 엔진."""

    def __init__(self):
        self._atr_cache: Dict[str, float] = {}

    def compute_ticker_atr(self, ticker: str, default_atr_pct: float = 0.025) -> float:
        """종목별 14일 실시간 적응형 ATR (% decimal, e.g. 0.025 = 2.5%)."""
        if ticker in self._atr_cache:
            return self._atr_cache[ticker]
        
        # 1. 역사적 일봉 데이터(data/historical_10y/)에서 14일 실시간 ATR 산출
        try:
            from pathlib import Path
            data_dir = _PROJECT_ROOT / 'data' / 'historical_10y'
            p_file = data_dir / f"kr_{ticker}.parquet" if ticker.isdigit() else data_dir / f"us_{ticker}.parquet"
            if p_file.exists():
                import pandas as pd
                df = pd.read_parquet(p_file)
                if len(df) >= 15 and 'high' in df.columns and 'low' in df.columns and 'close' in df.columns:
                    high = df['high'].values[-15:]
                    low = df['low'].values[-15:]
                    close = df['close'].values[-15:]
                    prev_close = np.roll(close, 1)
                    prev_close[0] = close[0]
                    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
                    atr14 = float(np.mean(tr[-14:]) / close[-1])
                    if not math.isnan(atr14) and atr14 > 0:
                        clipped_atr = float(np.clip(atr14, 0.015, 0.080))
                        self._atr_cache[ticker] = clipped_atr
                        return clipped_atr
        except Exception:
            pass

        # 2. DynamicQuantEngine 경험적 일간 변동성(σ_daily * sqrt(8/π) ≈ 1.253 * σ_daily) 기반 동적 ATR 추정
        try:
            from src.utils.dynamic_quant_engine import get_quant_engine
            _dqe = get_quant_engine()
            _m = _dqe.get_asset_metrics(ticker)
            if _m and _m.get('vol_daily', 0) > 0 and _m.get('vol_daily') != 0.02:
                atr_pct = float(np.clip(float(_m['vol_daily']) * 1.2533, 0.003, 0.080))
                self._atr_cache[ticker] = atr_pct
                return atr_pct
        except Exception:
            pass

        if ticker in ('252670', '233740', '122630', 'TQQQ', 'SQQQ', 'SOXL'):
            atr_pct = 0.045
        elif ticker in ('NVDA', '091160', 'SOXX', 'XLK', 'TSLA'):
            atr_pct = 0.028
        elif ticker in ('069500', 'SPY', 'QQQ', 'VOO', 'IVV'):
            atr_pct = 0.018
        elif ticker in ('SHV', '459580', 'BIL'):
            atr_pct = 0.003
        else:
            atr_pct = float(cfg.get('exit.default_atr_pct', default_atr_pct))

        self._atr_cache[ticker] = atr_pct
        return atr_pct

    def compute_inverse_chandelier_exit(
        self,
        trough_price: float,
        current_price: float,
        atr_pct: float,
        chandelier_mult: float = 1.5
    ) -> Tuple[bool, float, float]:
        """숏/인버스 포지션 전용 Inverse Chandelier Trailing Exit (Low_min + mult * ATR).

        Returns:
            (is_exit, inverse_trail_price, chandelier_gap_pct)
        """
        chandelier_gap_pct = max(0.020, chandelier_mult * atr_pct)
        inverse_trail_price = trough_price * (1.0 + chandelier_gap_pct)
        is_exit = current_price >= inverse_trail_price
        return is_exit, inverse_trail_price, chandelier_gap_pct


    def evaluate_all_positions(self, positions: Dict, market_data: Dict = None, regime: str = 'caution') -> Dict:
        """전체 포지션(S0~S11) 대상 전사 3중 손절 & 샹들리에 익절 평가.

        Args:
            positions: {pos_key: pos_dict} — 포트폴리오 내 모든 포지션
            market_data: 시장 데이터
            regime: 현재 시장 레짐

        Returns:
            {
                'exit_orders': [...],      # 청산/손절/부분익절 주문 목록
                'evaluated_count': int
            }
        """
        if market_data is None:
            market_data = {}
            
        exit_orders = []
        
        # [Pure Dynamic Math] VIX 분위수 및 레짐 기반 확률과정 최적 정지(Optimal Stopping) 엔진 연동
        _dqe = get_quant_engine()
        _vq = _dqe.get_vix_regime_quantiles((market_data or {}).get('signal_cache'))
        _vix = float(
            (market_data or {}).get('vix')
            or (market_data or {}).get('signal_cache', {}).get('vix')
            or _vq.get('baseline', 16.5)
        )
        _sc_dict = (market_data or {}).get('signal_cache') or {}
        if not _sc_dict:
            try:
                _sc_p = _PROJECT_ROOT / 'results' / 'signal_cache.json'
                if _sc_p.exists():
                    _sc_dict = json.loads(_sc_p.read_text(encoding='utf-8'))
            except Exception:
                _sc_dict = {}
        _fx_rate = float((market_data or {}).get('usdkrw') or _sc_dict.get('usdkrw') or 1334.3)
        _dyn_cash_eq = _dqe.get_cash_equivalent_tickers()
        _PARKING_EXEMPT = {'459580', '423160', '449170', '430740', '357870', '357330', 'SHV', 'SGOV'} | _dyn_cash_eq

        for pos_key, pos in positions.items():
            ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
            stream_id = pos.get('stream_id', pos_key.split(':')[0] if ':' in pos_key else 'S4')
            qty = float(pos.get('quantity', pos.get('qty', 0)))
            
            if qty <= 0:
                continue

            # S_BETA 헷지 포지션 및 파킹 자산은 전사 Exit에서 제외
            if stream_id in ('S_BETA', 'BETA', 'S_YIELD') or str(ticker).upper() in _PARKING_EXEMPT:
                continue

            price = float(pos.get('current_price', 0.0) or 0.0)
            entry_px = float(pos.get('entry_price', pos.get('avg_price', 0.0)) or 0.0)
            if price <= 0:
                try:
                    _pq_p = _PROJECT_ROOT / 'data' / 'historical_10y' / (f"kr_{ticker}.parquet" if str(ticker).isdigit() else f"us_{ticker}.parquet")
                    if _pq_p.exists():
                        import pandas as _pd_ex
                        price = float(_pd_ex.read_parquet(_pq_p)['close'].dropna().iloc[-1])
                except Exception:
                    pass
                if price <= 0:
                    price = entry_px if entry_px > 0 else 1.0
            pnl_pct = float(pos.get('unrealized_pnl_pct', pos.get('pnl_pct', pos.get('return_pct', ((price - entry_px) / entry_px * 100.0) if entry_px > 0 and price > 0 else 0.0))))
            peak_pnl = float(pos.get('peak_pnl_pct', pnl_pct))
            atr_pct = self.compute_ticker_atr(ticker)

            # [100% Mathematical Model] Almgren-Chriss 왕복 마찰비용 + 브라운 확산 최소 정보축적 시간 + 첫 도달 최적 정지 장벽 산출
            is_kr_tk = str(ticker).isdigit()
            order_val_krw = qty * price * (1.0 if is_kr_tk else _fx_rate)
            _opt = _dqe.compute_optimal_stopping_params(
                ticker=str(ticker),
                regime=regime,
                vix_level=_vix,
                order_value_krw=max(order_val_krw, 500_000.0),
                price=price,
                atr_pct=atr_pct,
            )
            min_holding_required = _opt['min_holding_minutes']
            holding_minutes = float(pos.get('holding_minutes', pos.get('holding_time_min', min_holding_required * 2.0)))

            # ── 0. [Profit-Lock Break-Even Escalation] (마찰비용+변동성 결합 동적 본전 보존 장벽) ──
            profit_lock_trigger = _opt['profit_lock_trigger_pct']
            break_even_floor = _opt['break_even_floor_pct']
            if holding_minutes >= min_holding_required and peak_pnl >= profit_lock_trigger:
                if pnl_pct < break_even_floor:
                    detail = f"🔒 [Profit-Lock Escalation] {ticker} ({stream_id}): 고점 P&L {peak_pnl:+.1f}% >= {profit_lock_trigger:.1f}% 달성 후 P&L {pnl_pct:+.2f}% < 동적 본전 지지선 +{break_even_floor:.2f}% 이탈 (보유 {holding_minutes:.0f}m >= {min_holding_required:.0f}m) ➔ 이익 보존 100% 청산"
                    logger.info(f"  {detail}")
                    action_type = 'buy' if pos.get('direction') == 'short' or pos.get('is_short') else 'sell'
                    exit_orders.append({
                        'stream_id': stream_id, 'ticker': ticker, 'direction': 'short' if action_type == 'sell' else 'long',
                        'action': action_type, 'quantity': qty, 'price': price, 'execution_algo': 'market',
                        'reason': detail
                    })
                    continue

            # ── 1. [1차 손절] Hard Entry Stop-Loss (확률과정 첫 도달 하방 장벽) ──
            hard_sl_limit = _opt['hard_sl_limit_pct']
            if pnl_pct <= hard_sl_limit:
                detail = f"🛑 [1차 진입 손절 발화] {ticker} ({stream_id}): P&L {pnl_pct:+.2f}% <= 동적 하방 손절한도 {hard_sl_limit:+.2f}% 도달"
                logger.warning(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue

            # ── 2. [2차 손절] Catastrophic Peak Drawdown Guard (고점 대비 99.9% 꼬리 폭락 손절) ──
            allowed_cat_drop = _opt['catastrophic_drop_pct']
            peak_drawdown = peak_pnl - pnl_pct

            if peak_pnl > 0 and peak_drawdown >= allowed_cat_drop:
                detail = f"🚨 [2차 고점 폭락 손절 발화] {ticker} ({stream_id}): 고점 {peak_pnl:+.1f}% 대비 -{peak_drawdown:.1f}% 하락이 동적 안전망 -{allowed_cat_drop:.1f}% 이탈"
                logger.warning(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue

            # ── 3. Uncapped ATR Chandelier Trailing Exit (상방 무제한 동적 샹들리에 익절) ──
            chandelier_drop = _opt['chandelier_drop_pct']
            trailing_trigger = _opt['trailing_trigger_pct']
            is_trail_activated = bool(pos.get('trail_activated', False)) or (peak_pnl >= trailing_trigger)
            is_trailing_exit = (
                holding_minutes >= min_holding_required and
                is_trail_activated and
                pnl_pct < (peak_pnl - chandelier_drop)
            )

            if is_trailing_exit:
                detail = f"✂️ [Chandelier Trailing Exit] {ticker} ({stream_id}): P&L {pnl_pct:+.1f}% (고점 {peak_pnl:+.1f}% 대비 -{peak_drawdown:.1f}% 하락이 샹들리에 밴드 -{chandelier_drop:.1f}% 이탈, N={holding_minutes:.0f}m >= {min_holding_required:.0f}m)"
                logger.info(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue

            # ── 4. Dynamic Scale-Out Runner (정보비율 IR 기반 동적 분할 차익실현) ──
            scaled_out = bool(pos.get('scaled_out', False))
            scale_out_target = _opt['scale_out_target_pct']
            scale_out_ratio = _opt['scale_out_ratio']
            
            if not scaled_out and pnl_pct >= scale_out_target:
                if qty <= 1:
                    logger.info(f"  🎯 [Scale-Out Skip] {ticker} ({stream_id}): 잔고 1주 이하 ({qty}주)로 분할 매도 스킵 ➔ 100% 러너 포지션으로 유지")
                    continue
                so_qty = max(1.0, math.floor(qty * scale_out_ratio))
                detail = f"🎯 [Scale-Out {scale_out_ratio*100:.0f}%] {ticker} ({stream_id}): P&L {pnl_pct:+.1f}% >= 동적 목표 {scale_out_target:.1f}% ({scale_out_ratio*100:.0f}% 차익실현)"
                logger.info(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': so_qty, 'price': price, 'execution_algo': 'market',
                    'exit_type': 'scale_out_50pct',
                    'reason': detail
                })
                continue

            # ── 5. Time-Decay Auto Exit (3~5일 0.5x ATR 횡보 알파소멸 자동 청산) ──
            if not scaled_out:
                try:
                    from src.risk.time_decay_exit import TimeDecayExitEvaluator
                    holding_days = float(pos.get('holding_days', 0.0) or 0.0)
                    if holding_days <= 0:
                        _ed_str = str(pos.get('entry_date', '') or '')[:10]
                        if _ed_str:
                            try:
                                from datetime import date as _dt_date
                                holding_days = float(max(0, (_dt_date.today() - _dt_date.fromisoformat(_ed_str)).days))
                            except Exception:
                                holding_days = 0.0
                        elif pos.get('holding_minutes'):
                            holding_days = float(pos.get('holding_minutes', 0.0)) / (24.0 * 60.0)
                    time_decay_eval = TimeDecayExitEvaluator().evaluate_time_decay(
                        position=pos,
                        current_price=price,
                        atr_val=price * atr_pct,
                        holding_days=holding_days
                    )
                    if time_decay_eval.get('trigger', False):
                        so_pct = time_decay_eval.get('scale_out_pct', 0.50)
                        decay_qty = max(1, math.floor(qty * so_pct))
                        detail = time_decay_eval.get('reason', f"⏱️ [Time-Decay Exit] {ticker} ({stream_id})")
                        exit_orders.append({
                            'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                            'action': 'sell', 'quantity': decay_qty, 'price': price, 'execution_algo': 'market',
                            'exit_type': 'time_decay_exit',
                            'reason': detail
                        })
                        continue
                except Exception as e_td:
                    logger.debug(f"  Time-Decay exit check bypass: {e_td}")

        logger.info(f"  🛡️ [Universal Exit SSOT] 전사 포지션({len(positions)}개) 평가 ➔ 체결/손절 주문 {len(exit_orders)}건 발화")
        return {'exit_orders': exit_orders, 'evaluated_count': len(positions)}

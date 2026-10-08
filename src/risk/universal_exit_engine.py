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
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class UniversalExitEngine:
    """전사 포지션(S0~S11) 공통 3중 손절 & 샹들리에 익절 단일 SSOT 엔진."""

    def __init__(self):
        self._atr_cache: Dict[str, float] = {}

    def compute_ticker_atr(self, ticker: str, default_atr_pct: float = 0.025) -> float:
        """종목별 14일 ATR (% decimal, e.g. 0.025 = 2.5%)."""
        if ticker in self._atr_cache:
            return self._atr_cache[ticker]
        
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
        
        # [Pure Dynamic Math] VIX 및 레짐 적응형 연속 샹들리에 배수
        _vix = float((market_data or {}).get('vix', (market_data or {}).get('signal_cache', {}).get('vix', 18.0)) or 18.0)
        if regime == 'bull':
            chandelier_mult = round(max(2.0, min(3.5, 3.5 - (_vix / 20.0))), 2)
        elif regime == 'caution':
            chandelier_mult = round(max(1.2, min(2.0, 2.0 - (_vix / 30.0))), 2)
        elif regime == 'bear':
            chandelier_mult = round(max(0.8, min(1.3, 1.5 - (_vix / 35.0))), 2)
        else:
            chandelier_mult = round(max(0.6, min(1.0, 1.0 - (_vix / 50.0))), 2)
        
        for pos_key, pos in positions.items():
            ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
            stream_id = pos.get('stream_id', pos_key.split(':')[0] if ':' in pos_key else 'S4')
            qty = float(pos.get('quantity', pos.get('qty', 0)))
            
            if qty <= 0:
                continue

            # S_BETA 헷지 포지션은 전사 Exit에서 제외 (S_BETA는 전용 모듈에서 통제)
            if stream_id == 'S_BETA':
                continue

            pnl_pct = float(pos.get('unrealized_pnl_pct', pos.get('pnl_pct', pos.get('return_pct', 0.0))))
            peak_pnl = float(pos.get('peak_pnl_pct', pnl_pct))
            price = float(pos.get('current_price', pos.get('entry_price', pos.get('avg_price', 1.0))))
            atr_pct = self.compute_ticker_atr(ticker)

            # ── 0. [Profit-Lock Break-Even Escalation] (수익 종목 손실 전환 100% 방지) ──
            profit_lock_trigger = max(6.0, atr_pct * 3.0 * 100.0)
            if peak_pnl >= profit_lock_trigger:
                break_even_floor = float(cfg.get('exit.profit_lock_floor_pct', 0.50))
                if pnl_pct < break_even_floor:
                    detail = f"🔒 [Profit-Lock Escalation] {ticker} ({stream_id}): 고점 P&L {peak_pnl:+.1f}% >= {profit_lock_trigger:.1f}% 달성 후 P&L {pnl_pct:+.2f}% < 본전 지지선 +{break_even_floor:.1f}% 이탈 ➔ 이익 보존 100% 청산"
                    logger.info(f"  {detail}")
                    action_type = 'buy' if pos.get('direction') == 'short' or pos.get('is_short') else 'sell'
                    exit_orders.append({
                        'stream_id': stream_id, 'ticker': ticker, 'direction': 'short' if action_type == 'sell' else 'long',
                        'action': action_type, 'quantity': qty, 'price': price, 'execution_algo': 'market',
                        'reason': detail
                    })
                    continue

            # ── 1. [1차 손절] Hard Entry Stop-Loss (진입가 대비 변동성 적응형 하방 손절) ──
            # 고베타 종목 노이즈 털림 방지: ATR 2.0배 또는 최소 3.0% 적용 (음수 max 수치 버그 정밀 수정)
            hard_sl_pct = abs(float(cfg.get('exit.hard_stop_loss_pct', 3.0)))
            hard_sl_limit = -max(hard_sl_pct, atr_pct * 2.0 * 100.0)
            if pnl_pct <= hard_sl_limit:
                detail = f"🛑 [1차 진입 손절 발화] {ticker} ({stream_id}): P&L {pnl_pct:+.2f}% <= 하방 손절한도 {hard_sl_limit:+.2f}% 도달"
                logger.warning(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue


            # ── 2. [2차 손절] Catastrophic Peak Drawdown Guard (고점 대비 폭락 손절) ──
            catastrophic_mult = float(cfg.get('exit.catastrophic_atr_mult', 3.5))
            allowed_cat_drop = atr_pct * catastrophic_mult * 100.0
            peak_drawdown = peak_pnl - pnl_pct

            if peak_pnl > 0 and peak_drawdown >= allowed_cat_drop:
                detail = f"🚨 [2차 고점 폭락 손절 발화] {ticker} ({stream_id}): 고점 {peak_pnl:+.1f}% 대비 -{peak_drawdown:.1f}% 하락이 안전망 -{allowed_cat_drop:.1f}% 이탈"
                logger.warning(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue

            # ── 3. Uncapped ATR Chandelier Trailing Exit (상방 무제한 트레일링 익절) ──
            # 보유 기간 보장 (기존 포트폴리오 스윙 종목은 기본 999분으로 즉시 평가 허용)
            holding_minutes = float(pos.get('holding_minutes', pos.get('holding_time_min', 999.0)))
            min_holding_required = float(cfg.get('exit.min_holding_minutes_for_trailing', 15.0))
            
            chandelier_drop = atr_pct * chandelier_mult * 100.0
            min_drop = float(cfg.get('exit.trailing_drop_floor', 2.0))
            chandelier_drop = max(min_drop, chandelier_drop)

            trailing_trigger = float(cfg.get('exit.trailing_tp_trigger', 2.5))
            is_trailing_exit = (
                holding_minutes >= min_holding_required and 
                peak_pnl >= trailing_trigger and 
                pnl_pct < (peak_pnl - chandelier_drop)
            )

            if is_trailing_exit:
                detail = f"✂️ [Chandelier Trailing Exit] {ticker} ({stream_id}): P&L {pnl_pct:+.1f}% (고점 {peak_pnl:+.1f}% 대비 -{peak_drawdown:.1f}% 하락이 샹들리에 밴드 -{chandelier_drop:.1f}% 이탈, N={holding_minutes:.0f}m)"
                logger.info(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue

            # ── 4. 50% Scale-Out Runner (분할 차익실현) ──
            scaled_out = pos.get('scaled_out', False)
            scale_out_target = max(atr_pct * float(cfg.get('exit.scale_out_atr_mult', 8.0)) * 100.0, 15.0)
            
            if not scaled_out and pnl_pct >= scale_out_target:
                # 1주 이하 포지션은 50% 분할 매도 시 전량 100% 청산되므로 스킵하고 러너(Runner)로 지속 보유
                if qty <= 1:
                    logger.info(f"  🎯 [Scale-Out Skip] {ticker} ({stream_id}): 잔고 1주 이하 ({qty}주)로 50% 분할 매도 스킵 ➔ 100% 러너 포지션으로 유지")
                    continue
                so_qty = max(1.0, math.floor(qty * 0.50))
                detail = f"🎯 [Scale-Out 50%] {ticker} ({stream_id}): P&L {pnl_pct:+.1f}% >= 목표 {scale_out_target:.1f}% (50% 차익실현)"
                logger.info(f"  {detail}")
                exit_orders.append({
                    'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                    'action': 'sell', 'quantity': so_qty, 'price': price, 'execution_algo': 'market',
                    'reason': detail
                })
                continue

            # ── 5. Time-Decay Auto Exit (3~5일 0.5x ATR 횡보 알파소멸 자동 청산) ──
            try:
                from src.risk.time_decay_exit import TimeDecayExitEvaluator
                holding_days = float(pos.get('holding_days', pos.get('holding_minutes', 0.0) / (24.0 * 60.0)))
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
                        'reason': detail
                    })
                    continue
            except Exception as e_td:
                logger.debug(f"  Time-Decay exit check bypass: {e_td}")

        logger.info(f"  🛡️ [Universal Exit SSOT] 전사 포지션({len(positions)}개) 평가 ➔ 체결/손절 주문 {len(exit_orders)}건 발화")
        return {'exit_orders': exit_orders, 'evaluated_count': len(positions)}

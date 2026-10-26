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

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import math
import numpy as np
import pandas as pd
import logging
from typing import Any, Dict, List, Optional, Tuple
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
            from src.utils.market_calendar import is_kr_ticker as _is_kr_atr
            p_file = (
                (_PROJECT_ROOT / 'data' / 'kr_markets' / f"kr_{ticker}.parquet")
                if _is_kr_atr(str(ticker))
                else (_PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f"{ticker}.parquet")
            )
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
                        clipped_atr = float(np.clip(atr14, pget('exit.atr_clip_min', 0.015), pget('exit.atr_clip_max', 0.08)))
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
                atr_pct = float(np.clip(float(_m['vol_daily']) * 1.2533, pget('exit.vol_atr_clip_min', 0.003), pget('exit.atr_clip_max', 0.08)))
                self._atr_cache[ticker] = atr_pct
                return atr_pct
            _tk_u = str(ticker).upper()
            if _tk_u in _dqe.get_cash_equivalent_tickers():
                atr_pct = float(pget('exit.vol_atr_clip_min', 0.003))
                self._atr_cache[ticker] = atr_pct
                return atr_pct
            if _tk_u in set(_dqe.get_inverse_hedge_tickers()):
                atr_pct = float(cfg.get('exit.default_atr_pct', default_atr_pct)) * 2.0
                self._atr_cache[ticker] = atr_pct
                return atr_pct
        except Exception:
            pass

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
        chandelier_gap_pct = max(pget('exit.inverse_chandelier_gap_floor', 0.02), chandelier_mult * atr_pct)
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
        _sc_dict = (market_data or {}).get('signal_cache') or {}
        if not _sc_dict:
            try:
                _sc_p = _PROJECT_ROOT / 'results' / 'signal_cache.json'
                if _sc_p.exists():
                    _sc_dict = json.loads(_sc_p.read_text(encoding='utf-8'))
            except Exception:
                _sc_dict = {}
        _vq = _dqe.get_vix_regime_quantiles(_sc_dict)
        _vix_raw_uee = (market_data or {}).get('vix') or _sc_dict.get('vix')
        if isinstance(_vix_raw_uee, dict):
            _vix_raw_uee = _vix_raw_uee.get('current', _vix_raw_uee.get('close'))
        _vix = float(_vix_raw_uee or _vq.get('baseline', 16.5))
        # ★ Wave 1 Track C: 리터럴 1334.3 제거. 환율은 청산 판단 자체가 아니라 Almgren-Chriss 주문규모 입력에만 쓰임 →
        #   미확보 시 해외 종목은 기존 하한(500,000원) 주문규모로 평가(충격비용 최소 → 장벽 가장 타이트 = 청산 우선).
        #   청산 경로 지연 방지를 위해 네트워크 조회는 추가하지 않음.
        _fx_raw = (market_data or {}).get('usdkrw') or _sc_dict.get('usdkrw')
        try:
            _fx_rate = float(_fx_raw) if _fx_raw is not None and float(_fx_raw) > 0 else None
        except (TypeError, ValueError):
            _fx_rate = None
        _dyn_cash_eq = _dqe.get_cash_equivalent_tickers()
        _PARKING_EXEMPT = {'459580', '423160', '449170', '430740', '357870', '357330', 'SHV', 'SGOV'} | _dyn_cash_eq
        from src.utils.market_calendar import is_kr_ticker as _is_kr_uee
        try:
            from src.execution.position_ledger import PositionLedgerManager as _PLM_Uee
            _plm_uee = _PLM_Uee()
        except Exception:
            _plm_uee = None

        for pos_key, pos in positions.items():
            ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
            _l_meta = _plm_uee.get_position_meta(str(ticker)) if _plm_uee is not None else {}
            stream_id = pos.get('stream_id') or pos.get('stream') or _l_meta.get('stream_id') or (pos_key.split(':')[0] if ':' in pos_key else 'S4')
            pos_track = pos.get('track') or _l_meta.get('track', '')
            pos_strategy = pos.get('strategy') or _l_meta.get('strategy', '')
            qty = float(pos.get('quantity', pos.get('qty', 0)))
            
            if qty <= 0:
                continue

            # S_BETA 헷지 포지션 및 파킹 자산은 전사 Exit에서 제외
            if stream_id in ('S_BETA', 'BETA', 'S_YIELD') or str(ticker).upper() in _PARKING_EXEMPT:
                continue

            price = float(pos.get('current_price', 0.0) or pos.get('price', 0.0) or 0.0)
            entry_px = float(pos.get('entry_price', pos.get('avg_price', 0.0)) or 0.0)
            is_kr_tk = _is_kr_uee(str(ticker))
            if price <= 0:
                try:
                    _pq_p = (
                        (_PROJECT_ROOT / 'data' / 'kr_markets' / f"kr_{ticker}.parquet")
                        if is_kr_tk
                        else (_PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f"{ticker}.parquet")
                    )
                    if _pq_p.exists():
                        import pandas as _pd_ex
                        price = float(_pd_ex.read_parquet(_pq_p)['close'].dropna().iloc[-1])
                except Exception:
                    pass
                if price <= 0:
                    _raw_p = pos.get('unrealized_pnl_pct')
                    if _raw_p is None:
                        _raw_p = pos.get('pnl_pct')
                    if _raw_p is None:
                        _raw_p = pos.get('return_pct')
                    if entry_px > 0 and _raw_p is not None:
                        price = entry_px * (1.0 + (float(_raw_p) / 100))
                    else:
                        logger.warning(f"  ⚠️ [UniversalExitEngine] {ticker} ({stream_id}): 유효 가격 미확보(price<=0) → 평가 스킵")
                        continue

            # ── [Track 2 Isolation] Buffett-Moat QVM (S3_B) 전용 4대 매도 헌법 라우팅 ──
            # 단기 노이즈 손절(-2.5%~-5%), 본전 보존(Profit-Lock), 샹들리에 추적손절, 3~5일 시간감쇠 청산을 100% 차단하고
            # 오직 해자 훼손(Exit 1), 극단적 과열(Exit 2), 구조적 대세 추세 붕괴(Exit 3), 기회비용 교체(Exit 4)만 평가.
            is_track2_buffett = (
                stream_id == 'S3_B'
                or pos_track == 'TRACK_2_BUFFETT_QVM'
                or pos_strategy in ('buffett_qvm', 'qvm_value')
                or bool(pos.get('noise_stop_loss_exempt', False))
            )
            if is_track2_buffett:
                _t2_order = self._evaluate_buffett_track2_exit(
                    ticker=str(ticker),
                    pos=pos,
                    price=price,
                    entry_px=entry_px,
                    qty=qty,
                    ledger_meta=_l_meta,
                    market_data=market_data,
                )
                if _t2_order is not None:
                    exit_orders.append(_t2_order)
                continue

            pnl_pct = float(pos.get('unrealized_pnl_pct', pos.get('pnl_pct', pos.get('return_pct', ((price - entry_px) / entry_px * 100.0) if entry_px > 0 and price > 0 else 0.0))))
            peak_pnl = float(pos.get('peak_pnl_pct', pnl_pct))
            atr_pct = self.compute_ticker_atr(ticker)

            # [100% Mathematical Model] Almgren-Chriss 왕복 마찰비용 + 브라운 확산 최소 정보축적 시간 + 첫 도달 최적 정지 장벽 산출
            # compute_ticker_atr는 True Range(ATR = sqrt(8/π) * σ_d ≈ 1.2533 * σ_d)를 반환하므로,
            # 가우시안 일간 표준편차(σ_d)를 입력받는 compute_optimal_stopping_params 호출 시 sqrt(8/π)로 정규화하여 중복 팽창 방지
            order_val_krw = qty * price * (1.0 if is_kr_tk else (_fx_rate or 0.0))
            _sigma_from_atr = atr_pct / math.sqrt(8.0 / math.pi)
            _opt = _dqe.compute_optimal_stopping_params(
                ticker=str(ticker),
                regime=regime,
                vix_level=_vix,
                order_value_krw=max(order_val_krw, 500_000.0),
                price=price,
                atr_pct=_sigma_from_atr,
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
                        'urgency': 'high',
                        'pnl_pct': round(float(pnl_pct), 4),
                        'unrealized_pnl_pct': round(float(pnl_pct), 4),
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
                    'exit_type': 'hard_stop_loss',
                    'urgency': 'stop',
                    'pnl_pct': round(float(pnl_pct), 4),
                    'unrealized_pnl_pct': round(float(pnl_pct), 4),
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
                    'exit_type': 'catastrophic_peak_stop',
                    'urgency': 'stop',
                    'pnl_pct': round(float(pnl_pct), 4),
                    'unrealized_pnl_pct': round(float(pnl_pct), 4),
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
                    'exit_type': 'chandelier_trailing',
                    'urgency': 'high',
                    'pnl_pct': round(float(pnl_pct), 4),
                    'unrealized_pnl_pct': round(float(pnl_pct), 4),
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
                    'urgency': 'high',
                    'pnl_pct': round(float(pnl_pct), 4),
                    'unrealized_pnl_pct': round(float(pnl_pct), 4),
                    'reason': detail
                })
                continue

            # ── 5. Time-Decay Auto Exit (3~5일 0.5x ATR 횡보 알파소멸 자동 청산) ──
            if not scaled_out:
                try:
                    from src.execution.position_ledger import (
                        count_trading_days_held as _cnt_td_uee,
                        is_core_or_macro_sleeve as _is_core_uee,
                    )
                    from src.risk.time_decay_exit import TimeDecayExitEvaluator
                    _explicit_hd = pos.get('holding_days')
                    holding_days = float(_explicit_hd or 0.0)
                    if holding_days <= 0:
                        _ed_str = str(pos.get('entry_date') or _l_meta.get('entry_date') or '')[:10]
                        if _ed_str:
                            holding_days = float(_cnt_td_uee(_ed_str, is_kr=is_kr_tk))
                        elif pos.get('holding_minutes'):
                            holding_days = float(pos.get('holding_minutes', 0.0)) / (24.0 * 60.0)
                    if _explicit_hd is None and _is_core_uee(stream_id, track=pos_track, strategy=pos_strategy, ticker=str(ticker)):
                        holding_days = 0.0
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
                        _td_ex_type = str(time_decay_eval.get('exit_type') or 'time_decay_exit')
                        exit_orders.append({
                            'stream_id': stream_id, 'ticker': ticker, 'direction': 'short',
                            'action': 'sell', 'quantity': decay_qty, 'price': price, 'execution_algo': 'market',
                            'exit_type': _td_ex_type,
                            'urgency': 'stop' if _td_ex_type in ('hard_sl', 'volatility_exhaustion') else 'high',
                            'pnl_pct': round(float(pnl_pct), 4),
                            'unrealized_pnl_pct': round(float(pnl_pct), 4),
                            'reason': detail
                        })
                        continue
                except Exception as e_td:
                    logger.debug(f"  Time-Decay exit check bypass: {e_td}")

        logger.info(f"  🛡️ [Universal Exit SSOT] 전사 포지션({len(positions)}개) 평가 ➔ 체결/손절 주문 {len(exit_orders)}건 발화")
        return {'exit_orders': exit_orders, 'evaluated_count': len(positions)}

    def _evaluate_buffett_track2_exit(
        self,
        ticker: str,
        pos: Dict[str, Any],
        price: float,
        entry_px: float,
        qty: float,
        ledger_meta: Optional[Dict[str, Any]] = None,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Track 2 (S3_B / TRACK_2_BUFFETT_QVM) 전용 4대 매도 헌법 평가기.

        Track 1의 단기 가격 손절(-2.5%~-5%), Profit-Lock 본전 청산, 단기 샹들리에 추적손절,
        3~5일 Time-Decay 반토막 청산을 100% 배제하고 오직 Buffett 4대 매도 법칙만 적용합니다.
        """
        try:
            from src.streams.s3_active_macro.qvm_strategic_sleeve import StrategicQVMCompoundingSleeve
            _l_meta = ledger_meta or {}
            _hwm = float(pos.get('highest_price', _l_meta.get('highest_price', 0.0)) or 0.0)
            _zero = 0.0
            if _hwm <= _zero:
                _hwm = max(price, entry_px)
            else:
                _hwm = max(_hwm, price)
            _pnl_pct = float(
                pos.get(
                    'unrealized_pnl_pct',
                    pos.get(
                        'pnl_pct',
                        pos.get('return_pct', ((price - entry_px) / entry_px * 100.0) if entry_px > _zero and price > _zero else 0.0),
                    ),
                )
            )
            pos_merged = dict(pos)
            pos_merged.update({
                'ticker': ticker,
                'stream_id': 'S3_B',
                'track': 'TRACK_2_BUFFETT_QVM',
                'current_price': price,
                'entry_price': entry_px,
                'highest_price': _hwm,
                'pnl_pct': _pnl_pct,
            })

            stock_metrics = dict(pos.get('stock_metrics') if isinstance(pos.get('stock_metrics'), dict) else pos)
            stock_metrics.setdefault('current_price', price)
            if float(stock_metrics.get('ma200', 0.0) or 0.0) <= _zero:
                try:
                    import numpy as _np_t2
                    import pandas as _pd_t2
                    from src.utils.market_calendar import is_kr_ticker as _is_kr_atr
                    _kr_pq = (
                        (_PROJECT_ROOT / 'data' / 'kr_markets' / f'kr_{ticker}.parquet')
                        if _is_kr_atr(str(ticker))
                        else (_PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f'{ticker}.parquet')
                    )
                    if _kr_pq.exists():
                        _df_p = _pd_t2.read_parquet(_kr_pq)
                        _c_col = 'close' if 'close' in _df_p.columns else ('Close' if 'Close' in _df_p.columns else None)
                        if _c_col is not None:
                            _cs = _df_p[_c_col].dropna().astype(float)
                            _min_c = 20
                            if len(_cs) >= _min_c:
                                stock_metrics.setdefault('ma20', float(_cs.tail(20).mean()))
                                stock_metrics.setdefault('ma60', float(_cs.tail(min(60, len(_cs))).mean()))
                                stock_metrics.setdefault('ma200', float(_cs.tail(min(200, len(_cs))).mean()))
                                _rets = _cs.pct_change().dropna().tail(20)
                                _min_r = 5
                                if len(_rets) >= _min_r:
                                    stock_metrics.setdefault('daily_vol_20d', float(_rets.std()))
                                _hwm_60 = float(_cs.tail(min(60, len(_cs))).max())
                                if _hwm_60 > _hwm:
                                    pos_merged['highest_price'] = _hwm_60
                except Exception:
                    pass

            sleeve = StrategicQVMCompoundingSleeve()
            res = sleeve.evaluate_buffett_exit_laws(
                stock_data=stock_metrics,
                holding_info=pos_merged,
                top_unheld_candidate=pos.get('best_candidate'),
                is_sleeve_full=bool(pos.get('is_sleeve_full', False)),
            )
            if res and (res.get('exit_law') or res.get('should_exit', False)):
                _ex_type = str(res.get('exit_law') or res.get('exit_type') or 'BUFFETT_EXIT')
                _reason = str(res.get('reason') or f'🏰 [Buffett Exit] {ticker} ({_ex_type})')
                logger.warning(f"  🏰 [Track 2 Buffett Exit 발화] {ticker} (S3_B): {_reason}")
                return {
                    'stream_id': 'S3_B',
                    'track': 'TRACK_2_BUFFETT_QVM',
                    'ticker': ticker,
                    'direction': 'short',
                    'action': 'sell',
                    'quantity': qty,
                    'price': price,
                    'execution_algo': 'market',
                    'exit_type': _ex_type,
                    'urgency': 'stop' if _ex_type in ('hard_sl', 'volatility_exhaustion') else 'high',
                    'pnl_pct': round(float(_pnl_pct), 4),
                    'unrealized_pnl_pct': round(float(_pnl_pct), 4),
                    'reason': _reason,
                }
        except Exception as _t2_err:
            logger.debug(f"  [Track 2 Buffett Exit] 평가 스킵 ({ticker}): {_t2_err}")
        return None


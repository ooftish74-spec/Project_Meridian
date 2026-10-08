"""Intraday Capital Router & Signal Mediation Engine (Project Meridian V3.5)

Implements 4-step intraday signal routing & capital mediation:
1. Step 1: EV_adj Real-time Risk-Adjusted Ranking.
2. Step 2: Time-Horizon Cascading & Dynamic Cash Swap (S1 Tactic D -> S11 High-Beta).
3. Step 3: Market Depth Capacity Sizing (Max 15% Depth 1-3 Orderbook Cap).
4. Step 4: Conflict Cancel & Wash-Trade Defense (Long vs Short Conflict -> Cancel -> S0 KOFR Cash Defense).
"""

import math
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class IntradayCapitalRouter:
    """Intraday Capital Router & Signal Mediation Engine."""

    # Inverse ETF correlation map for Conflict Cancel
    INVERSE_PAIR_MAP = {
        '069500': '114800',  # KODEX 200 vs KODEX 인버스
        '114800': '069500',
        '122630': '252670',  # KODEX 레버리지 vs KODEX 200선물인버스2X
        '252670': '122630',
        '091160': '390390',  # KODEX 반도체 vs KODEX 반도체인버스
        '390390': '091160',
        '233740': '251340',  # KOSDAQ 150 레버리지 vs KOSDAQ 150선물인버스2X
        '251340': '233740',
        'SOXX': 'SOXS',
        'SOXS': 'SOXX',
        'QQQ': 'PSQ',
        'PSQ': 'QQQ',
    }


    def __init__(self, max_depth_pct: float = 0.15, default_timeout_min: int = 3):
        self.max_depth_pct = max_depth_pct
        self.default_timeout_min = default_timeout_min

    @staticmethod
    def compute_ev_adj(win_rate: float, target_return_pct: float, stop_loss_pct: float, intraday_atr_pct: float) -> float:
        """[Step 1] 변동성 조정 기대값(EV_adj) 실시간 연산.
        
        EV_adj = [ WinRate * Target Return - (1 - WinRate) * Stop Loss ] / Intraday Volatility (ATR)
        """
        win_rate = max(0.01, min(0.99, win_rate))
        target_return_pct = max(0.01, abs(target_return_pct))
        stop_loss_pct = max(0.01, abs(stop_loss_pct))
        atr_pct = max(0.05, abs(intraday_atr_pct))

        net_ev = (win_rate * target_return_pct) - ((1.0 - win_rate) * stop_loss_pct)
        ev_adj = net_ev / atr_pct
        return round(ev_adj, 4)

    def rank_signals_by_ev_adj(self, signals: List[Dict[str, Any]], market_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """[Step 1] EV_adj 실시간 랭킹 산출."""
        ranked = []
        for sig in signals:
            win_rate = float(sig.get('win_rate', sig.get('confidence', 0.55)))
            tp = float(sig.get('tp_pct', 2.0))
            sl = float(sig.get('sl_pct', 1.0))
            ticker = sig.get('ticker', '')
            atr_pct = float(market_data.get('ticker_atr_pct', {}).get(ticker, market_data.get('atr_pct', 1.5)))

            ev_adj = self.compute_ev_adj(win_rate, tp, sl, atr_pct)
            sig_copy = dict(sig)
            sig_copy['ev_adj'] = ev_adj
            ranked.append(sig_copy)

        # EV_adj 내림차순 정렬
        ranked.sort(key=lambda x: x.get('ev_adj', 0.0), reverse=True)
        return ranked

    def check_market_depth_capacity(self, ticker: str, target_qty: int, orderbook_depth_1_3_qty: int) -> int:
        """[Step 3] 호가창 깊이(Market Depth Capacity) 기반 체결 수량 동적 제한.
        
        Max Order Capacity: 실시간 호가창 1~3단 매수/매도 잔량의 최대 15% 이하로 제한.
        """
        if target_qty <= 0:
            return 0
        if orderbook_depth_1_3_qty <= 0:
            # 호가 정보 누락 시 안전 기본값 적용 (target_qty 보존)
            return target_qty

        max_allowed_qty = math.floor(orderbook_depth_1_3_qty * self.max_depth_pct)
        if max_allowed_qty <= 0:
            max_allowed_qty = 1

        capped_qty = min(target_qty, max_allowed_qty)
        if capped_qty < target_qty:
            logger.info(f"  🛡️ [Market Depth Cap] {ticker}: 목표 {target_qty}주 -> 호가창 1~3단({orderbook_depth_1_3_qty}주) 15% 제약({capped_qty}주) 체결 분산")
        return capped_qty

    def resolve_signal_conflicts(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """[Step 4] 상충 신호 무효화 (Conflict Cancel & Wash-Trade Defense).
        
        S1 롱 vs S11 숏 또는 동일 자산/역방향 ETF 대립 신호 발생 시 두 신호 모두 강제 취소.
        """
        if not signals:
            return []

        cancelled_indices = set()
        n = len(signals)

        for i in range(n):
            for j in range(i + 1, n):
                s1 = signals[i]
                s2 = signals[j]

                t1 = str(s1.get('ticker', '')).strip()
                t2 = str(s2.get('ticker', '')).strip()
                side1 = str(s1.get('side', s1.get('action', 'buy'))).lower()
                side2 = str(s2.get('side', s2.get('action', 'buy'))).lower()

                # 조건 1: 동일 종목 상충 (Long vs Short)
                same_ticker_conflict = (t1 == t2) and (side1 != side2)

                # 조건 2: 역방향 ETF 상충 (예: KODEX 200 롱 vs KODEX 인버스 롱)
                inverse_conflict = (self.INVERSE_PAIR_MAP.get(t1) == t2) and (side1 == 'buy' and side2 == 'buy')

                if same_ticker_conflict or inverse_conflict:
                    logger.warning(
                        f"  🚨 [Conflict Cancel] 상충 신호 감지: ({t1}:{side1}) vs ({t2}:{side2}) "
                        f"-> Wash Trade 방지를 위해 두 신호 모두 100% 강제 취소 (S0 KOFR 현금 보존)"
                    )
                    cancelled_indices.add(i)
                    cancelled_indices.add(j)

        valid_signals = [sig for idx, sig in enumerate(signals) if idx not in cancelled_indices]
        return valid_signals

    def cascading_cash_swap(self, freed_cash_krw: float, pending_s11_signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """[Step 2] 시계열 연쇄 및 동적 자금 스왑 (Time-Horizon Cascading & Dynamic Cash Swap).
        
        S1 Tactic D 청산/스케일아웃으로 해제된 현금을 S11 High-Beta 포지션으로 동적 스왑.
        """
        if freed_cash_krw <= 0 or not pending_s11_signals:
            return []

        logger.info(f"  🔄 [Dynamic Cash Swap] S1 Tactic D 해제 현금(₩{freed_cash_krw:,.0f}) -> S11 High-Beta 스왑 재배치 집행")
        allocated_signals = []
        rem_cash = freed_cash_krw

        from src.execution.execution_engine import ExecutionEngine
        ee = ExecutionEngine()

        for sig in pending_s11_signals:
            price = float(sig.get('price', 0.0))
            ticker = sig.get('ticker', '')
            if price <= 0:
                continue
            target_amount = min(rem_cash, float(sig.get('target_amount', rem_cash)))
            is_us = not str(ticker).isdigit()
            eff_price_krw = price * ee._get_dynamic_usdkrw_rate() if is_us else price
            qty = math.floor(target_amount / eff_price_krw)
            alloc_val = qty * eff_price_krw
            if qty >= 1:
                sig_copy = dict(sig)
                sig_copy['quantity'] = qty
                sig_copy['allocated_cash'] = alloc_val
                allocated_signals.append(sig_copy)
                rem_cash -= alloc_val


        return allocated_signals

    def compute_holdings_ev(self, portfolio: Dict[str, Any], market_data: Dict[str, Any] = None) -> Dict[str, float]:
        """[Dynamic Opportunity Cost] 보유 포지션별 실시간 위험조정 기대값(EV_adj) 산출.
        
        보유 자산의 당일 모멘텀, 이평선 이격, 수급, 손익률을 종합하여
        '보유 지속 시의 기대수익률'을 정량화하고, 신규 진입 시그널과의 스왑 비교 벤치마크로 사용.
        """
        if not portfolio:
            return {}
        
        market_data = market_data or {}
        signal_cache = market_data.get('signal_cache', {})
        stock_technicals = signal_cache.get('stock_technicals', {})
        kospi_close = float(signal_cache.get('kospi', 0.0) or 0.0)
        kospi_ma20 = float(signal_cache.get('kospi_ma20', kospi_close) or kospi_close)
        
        # 1. 포지션 딕셔너리 정규화
        raw_holdings = portfolio.get('holdings') or portfolio.get('positions') or {}
        holdings_ev = {}

        for key, pos in raw_holdings.items():
            ticker = pos.get('ticker') or (key.split(':')[-1] if ':' in key else key)
            if not ticker:
                continue
            
            # 현금성 자산(KOFR, CD금리, 단기채)은 스왑 편출 우선순위 최하위 또는 별도 안전자산으로 보호
            if ticker in ('357330', '430740', 'SGOV'):
                holdings_ev[ticker] = 0.50 # 안전자산 기본 중립값
                continue

            current_p = float(pos.get('current_price') or pos.get('price') or 0.0)
            avg_p = float(pos.get('avg_price', current_p) or current_p)
            return_pct = float(pos.get('return_pct', pos.get('pnl_pct', 0.0)) or 0.0)

            # 승률 기본값
            win_rate = 0.50

            # (1) 기술적 지표 반영
            tech = stock_technicals.get(ticker, {})
            rsi = float(tech.get('rsi_14', 50.0) or 50.0)
            macd_hist = float(tech.get('macd_hist', 0.0) or 0.0)

            # RSI 편차 기여도 (-0.15 ~ +0.15)
            win_rate += (rsi - 50.0) / 100.0 * 0.30

            # MACD 방향성 기여도
            if macd_hist > 0:
                win_rate += 0.04
            elif macd_hist < 0:
                win_rate -= 0.06

            # (2) 지수 및 이평선 모멘텀
            if ticker == '069500' and kospi_ma20 > 0:
                # KODEX 200: 코스피 지수가 20일선 아래면 승률 하향
                if kospi_close < kospi_ma20:
                    win_rate -= 0.08
                else:
                    win_rate += 0.04
            elif not str(ticker).isdigit():
                # US 종목: 나스닥/S&P 500 모멘텀
                qqq_close = float(signal_cache.get('qqq_close', signal_cache.get('nasdaq', 0.0)) or 0.0)
                qqq_ma20 = float(signal_cache.get('qqq_ma20', signal_cache.get('nasdaq_ma20', qqq_close)) or qqq_close)
                if qqq_ma20 > 0 and qqq_close > 0:
                    if qqq_close < qqq_ma20:
                        win_rate -= 0.06
                    else:
                        win_rate += 0.04

            # (3) 당일/최근 손익 모멘텀 반영
            if return_pct < -1.5:
                win_rate -= 0.05
            elif return_pct > 3.0:
                win_rate += 0.05

            win_rate = max(0.15, min(0.85, win_rate))

            # (4) 변동성(ATR%) 및 목표/손절비율
            atr_pct = float(market_data.get('ticker_atr_pct', {}).get(ticker, 1.8))
            tp_pct = max(3.0, atr_pct * 2.5)
            sl_pct = max(1.5, atr_pct * 1.5)

            ev_adj = self.compute_ev_adj(win_rate, tp_pct, sl_pct, atr_pct)
            holdings_ev[ticker] = round(ev_adj, 4)

        return holdings_ev

    def route_intraday_capital(
        self,
        signals: List[Dict[str, Any]],
        market_data: Dict[str, Any],
        total_cash_krw: float,
        portfolio: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Intraday Capital Router 통합 제어 및 교체 매매(Swap) 집행.
        
        - OFI Z > 3.0: S1 Tactic D 80% 우선 배정 (3분 타임아웃)
        - Global Lead-Lag > 2%: S11 High-Beta 70% 우선 배정
        - 동일 방향 합의: 1.2x 시너지 배수 연산
        - 상충 신호: Conflict Cancel -> KOFR 100% 수비
        - 자본 한도 초과 시: Cross-Asset Alpha Swap (기존 보유 EV vs 신규 EV 비교 후 자동 스왑 매도/매수 쌍 생성)
        """
        if not signals:
            return []

        # 1. Step 4: 상충 신호 무효화
        filtered_signals = self.resolve_signal_conflicts(signals)
        if not filtered_signals:
            return []

        # 2. Step 1: EV_adj 실시간 랭킹 산출
        ranked_signals = self.rank_signals_by_ev_adj(filtered_signals, market_data)

        # 3. 장중 시장 국면 파라미터 수집
        ofi_z = float(market_data.get('ofi_z_score', market_data.get('OFI_Z', 0.0)))
        lead_lag = float(market_data.get('lead_lag_score', market_data.get('LEAD_LAG', 0.0)))

        # 방향성 동시 합의 (Same Direction Synergy Check)
        sides = {s.get('side', 'buy') for s in ranked_signals}
        has_synergy = len(sides) == 1 and len(ranked_signals) >= 2
        synergy_mult = 1.20 if has_synergy else 1.0

        # holdings_ev 보장
        holdings_ev = market_data.get('holdings_ev')
        if not holdings_ev and portfolio:
            holdings_ev = self.compute_holdings_ev(portfolio, market_data)
            market_data['holdings_ev'] = holdings_ev

        routed_signals = []
        from src.execution.execution_engine import ExecutionEngine
        ee = ExecutionEngine()
        for sig in ranked_signals:
            stream = str(sig.get('stream', sig.get('stream_id', ''))).upper()
            tactic = str(sig.get('tactic', sig.get('strategy', ''))).upper()
            ticker = sig.get('ticker', '')

            # [Pure Dynamic Math] 연속 로지스틱 시그모이드 자본 할당 비율
            sig_ev_score = float(sig.get('ev_adj', 0.5))
            factor_drive = 0.0
            if 'TACTIC_D' in tactic or 'S1' in stream:
                factor_drive = max(-2.0, min(2.0, ofi_z / 2.0))
                sig['timeout_min'] = self.default_timeout_min
            elif 'S11' in stream:
                factor_drive = max(-2.0, min(2.0, lead_lag / 2.0))

            alloc_ratio = round(1.0 / (1.0 + math.exp(-(sig_ev_score - 0.5 + factor_drive))), 4)
            alloc_ratio = max(0.15, min(0.85, alloc_ratio))

            alloc_ratio *= synergy_mult
            alloc_cash = min(total_cash_krw, total_cash_krw * alloc_ratio)
            price = float(sig.get('price', 0.0) or 0.0)
            if price <= 1.0:
                # 1. market_data 실시간 가격 우선 조회
                if market_data:
                    price = float((market_data.get('current_prices') or {}).get(ticker, 0.0) or
                                  (market_data.get('prices') or {}).get(ticker, 0.0) or
                                  (market_data.get('kr_close') or {}).get(ticker, 0.0) or
                                  (market_data.get('us_close') or {}).get(ticker, 0.0))
                # 2. portfolio 보유자산 가격 확인
                if price <= 1.0 and portfolio:
                    p_dict = portfolio.get('positions') or portfolio.get('holdings') or {}
                    if isinstance(p_dict, dict) and ticker in p_dict:
                        p_info = p_dict[ticker]
                        price = float(p_info.get('current_price', p_info.get('price', 0.0)))
                # 3. pykrx 폴백
                if price <= 1.0 and str(ticker).isdigit():
                    try:
                        from pykrx import stock as _pykrx
                        from datetime import date as _d, timedelta as _td
                        _t_str = _d.today().strftime('%Y%m%d')
                        _w_str = (_d.today() - _td(days=7)).strftime('%Y%m%d')
                        _df_p = _pykrx.get_market_ohlcv_by_date(_w_str, _t_str, str(ticker))
                        if len(_df_p) > 0:
                            price = float(_df_p.iloc[-1].get('종가', 0.0))
                    except Exception:
                        pass

            if price <= 0:
                logger.warning(f"  ⚠️ [Router] {ticker} 유효 가격 미확인으로 스킵")
                continue

            sig['price'] = price
            is_us = not str(ticker).isdigit()
            eff_price_krw = price * ee._get_dynamic_usdkrw_rate() if is_us else price
            raw_qty = math.floor(alloc_cash / eff_price_krw) if eff_price_krw > 0 else 0

            # 4. Step 3: Market Depth Capacity 15% 동적 제한
            depth_qty = int(market_data.get('orderbook_depth_1_3', {}).get(ticker, 0))
            capped_qty = self.check_market_depth_capacity(ticker, raw_qty, depth_qty)

            if capped_qty >= 1:
                sig['quantity'] = capped_qty
                sig['action'] = 'buy'
                sig['direction'] = 'long'
                sig['amount_krw'] = capped_qty * eff_price_krw
                sig['allocated_cash'] = capped_qty * eff_price_krw
                routed_signals.append(sig)
            else:
                sig_ev = float(sig.get('ev_adj', 0.5))
                if holdings_ev:
                    is_us_candidate = not str(ticker).isdigit()
                    eligible_holdings = {}
                    holdings_dict = portfolio.get('holdings') or portfolio.get('positions') or {} if portfolio else {}

                    for h_ticker, h_ev in holdings_ev.items():
                        h_is_us = not str(h_ticker).isdigit()
                        if h_is_us == is_us_candidate and h_ticker not in ('357330', '430740', '459580', '449170', 'SHV', 'SGOV'):
                            eligible_holdings[h_ticker] = h_ev

                    if eligible_holdings:
                        lowest_ticker = min(eligible_holdings, key=eligible_holdings.get)
                        lowest_ev = eligible_holdings[lowest_ticker]
                        
                        # [Pure Dynamic Math] 실제 마찰비용 및 변동성에 기반한 스왑 허들
                        atr_val = float(market_data.get('ticker_atr_pct', {}).get(ticker, market_data.get('atr_pct', 1.5)))
                        friction_pct = 0.23 if not is_us_candidate else 0.08
                        swap_premium = round(friction_pct / max(0.5, atr_val), 4)

                        if sig_ev > (lowest_ev + swap_premium):
                            lowest_pos = holdings_dict.get(lowest_ticker) or {}
                            lowest_qty = int(lowest_pos.get('qty', lowest_pos.get('quantity', 0)))
                            lowest_price = float(lowest_pos.get('current_price', lowest_pos.get('price', 0.0)))
                            lowest_name = lowest_pos.get('name', lowest_ticker)
                            lowest_is_us = not str(lowest_ticker).isdigit()
                            lowest_price_krw = lowest_price * ee._get_dynamic_usdkrw_rate() if lowest_is_us else lowest_price

                            if lowest_qty > 0 and lowest_price_krw > 0 and eff_price_krw > 0:
                                # [Pure Dynamic Math] 신규 매수에 필요한 정확한 수량만큼만 정밀 매도 (임의 35% 강제 청산 폐지)
                                min_buy_amount = eff_price_krw * 1.02
                                sell_qty_needed = max(1, math.ceil(min_buy_amount / lowest_price_krw))
                                sell_qty = min(lowest_qty, sell_qty_needed)
                                swap_freed_krw = sell_qty * lowest_price_krw
                                buy_qty = math.floor(swap_freed_krw / eff_price_krw)

                                if buy_qty >= 1:
                                    logger.info(
                                        f"  🔄 [Cross-Asset Alpha Swap Trigger] 신규 {ticker} (EV_adj={sig_ev:.2f}) > "
                                        f"기존 {lowest_ticker} (EV_adj={lowest_ev:.2f}) + 교체비용({swap_premium:.1f}%) "
                                        f"➔ {lowest_ticker} {sell_qty}주 매도(₩{swap_freed_krw:,.0f}) 후 {ticker} {buy_qty}주 자금 스왑 발화!"
                                    )
                                    # 1. 기존 저효율 자산 매도 주문 생성 (매도 선행)
                                    sell_order = {
                                        'stream_id': 'SYS_SWAP',
                                        'ticker': lowest_ticker,
                                        'name': lowest_name,
                                        'direction': 'sell',
                                        'action': 'sell',
                                        'quantity': sell_qty,
                                        'amount_krw': swap_freed_krw,
                                        'price': lowest_price,
                                        'confidence': 1.0,
                                        'strategy': 'cross_alpha_swap_sell',
                                        'reason': f"자본 스왑 매도: {lowest_ticker}(EV={lowest_ev:.2f}) -> {ticker}(EV={sig_ev:.2f})",
                                        'target_buy_ticker': ticker
                                    }
                                    routed_signals.append(sell_order)

                                    # 2. 신규 고알파 자산 매수 주문 생성
                                    buy_order = dict(sig)
                                    buy_order['stream_id'] = sig.get('stream_id') or sig.get('stream') or 'S10_MEGA_TREND'
                                    buy_order['direction'] = 'buy'
                                    buy_order['action'] = 'buy'
                                    buy_order['quantity'] = buy_qty
                                    buy_order['amount_krw'] = buy_qty * eff_price_krw
                                    buy_order['allocated_cash'] = buy_qty * eff_price_krw
                                    buy_order['reallocate_from'] = lowest_ticker
                                    buy_order['strategy'] = 'cross_alpha_swap_buy'
                                    buy_order['reason'] = f"자본 스왑 매수: {ticker}(EV={sig_ev:.2f}) <- {lowest_ticker}(EV={lowest_ev:.2f})"
                                    routed_signals.append(buy_order)

                                    # 동일 사이클 내 중복 편출 방지를 위해 lowest_ticker 임시 EV 상향
                                    eligible_holdings[lowest_ticker] = 99.0
                                    holdings_ev[lowest_ticker] = 99.0
                        else:
                            logger.info(
                                f"  🛡️ [Alpha Premium Unmet] 신규 {ticker} (EV_adj={sig_ev:.2f}) <= "
                                f"기존 {lowest_ticker} (EV_adj={lowest_ev:.2f}) + 교체비용({swap_premium:.1f}%) ➔ 기존 포지션 유지 (교체 매매 스킵)"
                            )
                else:
                    logger.info(
                        f"  ℹ️ [Capital Limit] 신규 {ticker}: 단가 ₩{eff_price_krw:,.0f} > 가용현금 ₩{total_cash_krw:,.0f} "
                        f"& 기존 자산 손절/청산 조건 미도달 ➔ 기존 포트폴리오 유지"
                    )

        return routed_signals

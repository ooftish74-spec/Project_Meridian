"""Intraday Capital Router & Signal Mediation Engine (Project Meridian V3.5)

Implements 4-step intraday signal routing & capital mediation:
1. Step 1: EV_adj Real-time Risk-Adjusted Ranking.
2. Step 2: Time-Horizon Cascading & Dynamic Cash Swap (S1 Tactic D -> S11 High-Beta).
3. Step 3: Market Depth Capacity Sizing (Max 15% Depth 1-3 Orderbook Cap).
4. Step 4: Conflict Cancel & Wash-Trade Defense (Long vs Short Conflict -> Cancel -> S0 KOFR Cash Defense).
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import math
import logging
from typing import Dict, List, Any, Optional
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


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
        """[Step 1] EV_adj 실시간 랭킹 산출 (무하드코딩 동적 장벽 연동)."""
        from src.utils.dynamic_quant_engine import get_quant_engine
        _dqe = get_quant_engine()
        ranked = []
        for sig in signals:
            ticker = str(sig.get('ticker', '')).strip()
            _barriers = _dqe.compute_dynamic_exit_barriers(ticker) if ticker else {}
            win_rate = float(sig.get('win_rate', sig.get('confidence', 0.55)))
            _sentinel_barrier = 50
            _raw_tp = float(sig.get('tp_pct') or 0.0)
            _raw_sl = abs(float(sig.get('sl_pct') or 0.0))
            tp = _raw_tp if (0.0 < _raw_tp < _sentinel_barrier) else float(_barriers.get('take_profit_pct', 2.0))
            sl = _raw_sl if (0.0 < _raw_sl < _sentinel_barrier) else abs(float(_barriers.get('stop_loss_pct', 1.0)))
            atr_pct = float(
                market_data.get('ticker_atr_pct', {}).get(
                    ticker, market_data.get('atr_pct', _barriers.get('sigma_daily_pct', 1.5))
                )
            )

            ev_adj = self.compute_ev_adj(win_rate, tp, sl, atr_pct)
            sig_copy = dict(sig)
            sig_copy.setdefault('tp_pct', round(tp, 4))
            sig_copy.setdefault('sl_pct', round(sl, 4))
            sig_copy.setdefault('expected_return_pct', round(tp, 4))
            sig_copy['ev_adj'] = ev_adj
            ranked.append(sig_copy)

        # EV_adj 내림차순 정렬
        ranked.sort(key=lambda x: x.get('ev_adj', 0.0), reverse=True)
        return ranked

    def check_market_depth_capacity(self, ticker: str, target_qty: int, orderbook_depth_1_3_qty: int, spread_bps: float = None) -> int:
        """[Step 3] 호가창 깊이(Market Depth Capacity) 기반 체결 수량 동적 제한.
        
        [Mathematical Dynamic Model]
        Dynamic Depth Cap Pct = clip(0.20 - 0.005 * spread_bps, 0.05, 0.25)
        호가 스프레드가 좁고 유동성이 깊을수록 20~25% 허용, 스프레드가 넓고 호가가 얇을수록 5~10%로 동적 보수화.
        """
        if target_qty <= 0:
            return 0
        if orderbook_depth_1_3_qty <= 0:
            return target_qty

        dynamic_cap_pct = self.max_depth_pct
        if spread_bps is not None and spread_bps > 0:
            dynamic_cap_pct = max(0.05, min(0.25, 0.20 - (spread_bps * 0.005)))

        max_allowed_qty = math.floor(orderbook_depth_1_3_qty * dynamic_cap_pct)
        if max_allowed_qty <= 0:
            max_allowed_qty = 1

        capped_qty = min(target_qty, max_allowed_qty)
        if capped_qty < target_qty:
            logger.info(f"  🛡️ [Market Depth Cap] {ticker}: 목표 {target_qty}주 -> 호가창 1~3단({orderbook_depth_1_3_qty}주) 동적 제약({dynamic_cap_pct*100:.1f}%, {capped_qty}주) 체결 분산")
        return capped_qty

    def resolve_signal_conflicts(self, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """[Step 4] 스마트 상충 신호 상쇄 (Smart Conflict Netting & Wash-Trade Defense).
        
        동일 종목 반대 매매(롱 vs 숏) 또는 상관계수 -1.0 역방향 ETF 페어(예: KODEX 200 vs KODEX 인버스)
        신호 충돌 발생 시:
        1. 확신도/요청자본 기반 순노출(Net Exposure) 연산
        2. 양측 노출도가 균등(차이 5% 미만 또는 미지정 기본값)할 경우: 100% 상호 상쇄 취소 (Wash-Trade 원천 방지)
        3. 한쪽 확신도가 우세할 경우: 열세 신호 소멸 및 우위 신호 순노출 차액 주문으로 축소 집행 (수수료/슬리피지 대폭 절감)
        """
        if not signals:
            return []

        from src.utils.dynamic_quant_engine import get_quant_engine
        _dqe = get_quant_engine()

        # 작업용 복사본 생성 (+ 상장폐지 구코드 → 후속코드 정규화: 단일 SSOT, 로그 기록)
        from src.utils.ticker_canonical import canonical_ticker
        working_signals = [dict(s) for s in signals]
        for _s in working_signals:
            if 'ticker' in _s:
                _s['ticker'] = canonical_ticker(_s.get('ticker'), context='intraday_capital_router')
        cancelled_indices = set()
        n = len(working_signals)

        for i in range(n):
            if i in cancelled_indices:
                continue
            for j in range(i + 1, n):
                if j in cancelled_indices:
                    continue

                s1 = working_signals[i]
                s2 = working_signals[j]

                # ★ Wave 0 D3: 주문 dict의 ticker를 다른 종목으로 치환하던 _ALIAS 제거 (오종목 주문 위험)
                t1 = str(s1.get('ticker', '')).strip()
                t2 = str(s2.get('ticker', '')).strip()

                side1 = str(s1.get('side', s1.get('action', 'buy'))).lower()
                side2 = str(s2.get('side', s2.get('action', 'buy'))).lower()

                # 조건 1: 동일 종목 상충 (Long vs Short)
                same_ticker_conflict = (t1 == t2) and (side1 != side2)

                # ★ Wave 0 E2: 매도(청산·손절·리밸런싱 축소)는 기존 보유분 감소이므로 절대 상쇄/축소하지 않는다.
                #   동일 종목 buy↔sell 충돌 시 매수만 취소하고 매도는 원형 그대로 보존.
                if same_ticker_conflict and ('sell' in (side1, side2)):
                    buy_idx = i if side1 != 'sell' else j
                    logger.warning(
                        f"  🛡️ [Exit Protection] {t1}: 동일 종목 매수/매도 충돌 → 매도(청산) 보존, 매수 신호 취소 "
                        f"(sell_reason={(s1 if side1 == 'sell' else s2).get('reason', '')!s:.60})"
                    )
                    cancelled_indices.add(buy_idx)
                    if buy_idx == i:
                        break
                    continue

                # 조건 2: 역방향 ETF 상충 (정적 매핑 + 동일 주식군 내 경험적 베타 부호 반대 β1 * β2 <= -0.75 & 주가지수 상관곱 ρ1 * ρ2 <= -0.55)
                # 비주식 안전자산(금/채권/원자재 등 safe_havens/cash_equivalents 또는 상관 약한 자산)은 주식 인버스와 다중자산 헤지이므로 제외
                from src.utils.market_calendar import is_kr_ticker
                _m1 = _dqe.get_asset_metrics(t1)
                _m2 = _dqe.get_asset_metrics(t2)
                _non_eq = _dqe.get_cash_equivalent_tickers() | set((_dqe._classifications or {}).get('safe_havens', set()))
                _same_mkt = (is_kr_ticker(t1) == is_kr_ticker(t2)) and (t1 not in _non_eq) and (t2 not in _non_eq)
                _beta_conflict = (
                    _same_mkt
                    and (_m1.get('beta', 1.0) * _m2.get('beta', 1.0) <= -0.75)
                    and (_m1.get('corr', 0.0) * _m2.get('corr', 0.0) <= -0.55)
                )
                inverse_conflict = ((self.INVERSE_PAIR_MAP.get(t1) == t2) or _beta_conflict) and (side1 == 'buy' and side2 == 'buy')

                if same_ticker_conflict or inverse_conflict:
                    # 마켓-뉴트럴 페어 전략(S14) 및 Track 2 장기 해자 복리 슬리브(S3_B / TRACK_2_BUFFETT_QVM) 주문은
                    # Track 1 단기 방향성/인버스 헤지 주문과 역방향 상충 상쇄(Smart Netting)하지 않음
                    _strat1 = str(s1.get('strategy', '')).lower()
                    _strat2 = str(s2.get('strategy', '')).lower()
                    _sid1 = str(s1.get('stream_id', s1.get('stream', ''))).upper()
                    _sid2 = str(s2.get('stream_id', s2.get('stream', ''))).upper()
                    _is_pair1 = (
                        ('pair' in _strat1)
                        or bool(s1.get('pair_id'))
                        or bool(s1.get('pair_partner'))
                        or _sid1 in ('S14', 'S14_PAIR_ALPHA', 'S3_B')
                        or s1.get('track') == 'TRACK_2_BUFFETT_QVM'
                        or _strat1 in ('buffett_qvm', 'qvm_value')
                    )
                    _is_pair2 = (
                        ('pair' in _strat2)
                        or bool(s2.get('pair_id'))
                        or bool(s2.get('pair_partner'))
                        or _sid2 in ('S14', 'S14_PAIR_ALPHA', 'S3_B')
                        or s2.get('track') == 'TRACK_2_BUFFETT_QVM'
                        or _strat2 in ('buffett_qvm', 'qvm_value')
                    )
                    if inverse_conflict and (_is_pair1 or _is_pair2):
                        continue

                    # 확신도 및 요청 자본에 기반한 유효 노출(Exposure) 연산
                    has_amt1 = 'amount_krw' in s1 or 'target_amount' in s1 or 'size_pct' in s1 or 'weight' in s1
                    has_amt2 = 'amount_krw' in s2 or 'target_amount' in s2 or 'size_pct' in s2 or 'weight' in s2
                    amt1 = float(s1.get('amount_krw', s1.get('target_amount', s1.get('size_pct', s1.get('weight', 1.0)))) or 1.0)
                    amt2 = float(s2.get('amount_krw', s2.get('target_amount', s2.get('size_pct', s2.get('weight', 1.0)))) or 1.0)
                    conf1 = float(s1.get('confidence', s1.get('win_rate', s1.get('ev_adj', 0.5))) or 0.5)
                    conf2 = float(s2.get('confidence', s2.get('win_rate', s2.get('ev_adj', 0.5))) or 0.5)

                    exp1 = amt1 * conf1
                    exp2 = amt2 * conf2
                    max_exp = max(exp1, exp2, 1e-9)
                    rel_diff = abs(exp1 - exp2) / max_exp

                    # 금액이 지정되지 않았고 확신도가 동일하거나, 노출 차이가 5% 이내인 경우 -> 100% 완전 상쇄
                    if (not has_amt1 and not has_amt2 and abs(conf1 - conf2) < 0.01) or rel_diff < 0.05:
                        logger.warning(
                            f"  🚨 [Conflict Cancel] 상충 신호 균등 상쇄: ({t1}:{side1}, E={exp1:.2f}) vs ({t2}:{side2}, E={exp2:.2f}) "
                            f"-> Wash Trade 방지 및 무의미한 양방향 비용 회피를 위해 두 신호 모두 100% 상쇄 소멸"
                        )
                        cancelled_indices.add(i)
                        cancelled_indices.add(j)
                        break
                    elif exp1 > exp2:
                        # s1 우세 -> s2 소멸, s1은 순노출(net exposure)로 축소 집행
                        net_ratio = max(0.01, min(1.0, (exp1 - exp2) / exp1))
                        saved_vol = 2.0 * exp2
                        if 'target_amount' in s1:
                            s1['target_amount'] = float(s1['target_amount']) * net_ratio
                        if 'amount_krw' in s1:
                            s1['amount_krw'] = float(s1['amount_krw']) * net_ratio
                        if 'amount_usd' in s1:
                            s1['amount_usd'] = float(s1['amount_usd']) * net_ratio
                        if 'quantity' in s1:
                            new_q1 = int(round(float(s1['quantity']) * net_ratio))
                            if new_q1 <= 0:
                                logger.info(
                                    f"  🛑 [Smart Netting Cancel] ({t1}:{side1}, E={exp1:,.0f}) vs ({t2}:{side2}, E={exp2:,.0f}) "
                                    f"-> 상쇄 후 순노출({exp1 - exp2:,.0f}, 비율 {net_ratio*100:.1f}%)이 1주 미만(0주)이므로 양측 모두 상쇄 취소"
                                )
                                cancelled_indices.add(i)
                                cancelled_indices.add(j)
                                break
                            s1['quantity'] = new_q1
                        s1['netted_with'] = t2
                        s1['saved_turnover_krw'] = saved_vol
                        logger.info(
                            f"  🔄 [Smart Netting] 상충 신호 순노출 상쇄: ({t1}:{side1}, E={exp1:,.0f}) vs ({t2}:{side2}, E={exp2:,.0f}) "
                            f"-> 우위 종목 {t1} 순노출({exp1 - exp2:,.0f}, 비율 {net_ratio*100:.1f}%) 채택 집행, {t2} 상쇄 소멸 (절감 거래대금: ₩{saved_vol:,.0f})"
                        )
                        cancelled_indices.add(j)
                    else:
                        # s2 우세 -> s1 소멸, s2는 순노출(net exposure)로 축소 집행
                        net_ratio = max(0.01, min(1.0, (exp2 - exp1) / exp2))
                        saved_vol = 2.0 * exp1
                        if 'target_amount' in s2:
                            s2['target_amount'] = float(s2['target_amount']) * net_ratio
                        if 'amount_krw' in s2:
                            s2['amount_krw'] = float(s2['amount_krw']) * net_ratio
                        if 'amount_usd' in s2:
                            s2['amount_usd'] = float(s2['amount_usd']) * net_ratio
                        if 'quantity' in s2:
                            new_q2 = int(round(float(s2['quantity']) * net_ratio))
                            if new_q2 <= 0:
                                logger.info(
                                    f"  🛑 [Smart Netting Cancel] ({t2}:{side2}, E={exp2:,.0f}) vs ({t1}:{side1}, E={exp1:,.0f}) "
                                    f"-> 상쇄 후 순노출({exp2 - exp1:,.0f}, 비율 {net_ratio*100:.1f}%)이 1주 미만(0주)이므로 양측 모두 상쇄 취소"
                                )
                                cancelled_indices.add(i)
                                cancelled_indices.add(j)
                                break
                            s2['quantity'] = new_q2
                        s2['netted_with'] = t1
                        s2['saved_turnover_krw'] = saved_vol
                        logger.info(
                            f"  🔄 [Smart Netting] 상충 신호 순노출 상쇄: ({t2}:{side2}, E={exp2:,.0f}) vs ({t1}:{side1}, E={exp1:,.0f}) "
                            f"-> 우위 종목 {t2} 순노출({exp2 - exp1:,.0f}, 비율 {net_ratio*100:.1f}%) 채택 집행, {t1} 상쇄 소멸 (절감 거래대금: ₩{saved_vol:,.0f})"
                        )
                        cancelled_indices.add(i)
                        break

        valid_signals = [sig for idx, sig in enumerate(working_signals) if idx not in cancelled_indices]
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

        from src.data_collection.central_data_gateway import CentralDataGateway
        from src.utils.market_calendar import is_kr_ticker
        dyn_fx = CentralDataGateway().get_usdkrw_rate()

        for sig in pending_s11_signals:
            price = float(sig.get('price', 0.0))
            ticker = sig.get('ticker', '')
            if price <= 0:
                continue
            target_amount = min(rem_cash, float(sig.get('target_amount', rem_cash)))
            is_us = not is_kr_ticker(ticker)
            if is_us and not dyn_fx:
                logger.error(f"  🛑 [FX Guard] {ticker}: USD/KRW 미확보 → 해외 배분 스킵")
                continue
            eff_price_krw = price * dyn_fx if is_us else price
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
        
        from src.utils.market_calendar import is_kr_ticker
        market_data = market_data or {}
        signal_cache = market_data.get('signal_cache', {})
        stock_technicals = signal_cache.get('stock_technicals', {})
        kospi_close = float(signal_cache.get('kospi', 0.0) or 0.0)
        kospi_ma20 = float(signal_cache.get('kospi_ma20', kospi_close) or kospi_close)
        
        # 1. 포지션 딕셔너리 정규화
        raw_holdings = portfolio.get('holdings') or portfolio.get('positions') or {}
        holdings_ev = {}

        try:
            from src.execution.position_ledger import PositionLedgerManager as _PLM_Ev
            _plm_ev = _PLM_Ev()
        except Exception:
            _plm_ev = None

        for key, pos in raw_holdings.items():
            ticker = pos.get('ticker') or (key.split(':')[-1] if ':' in key else key)
            if not ticker:
                continue

            # [Track 2 & Pair Isolation] Buffett-Moat QVM (S3_B / TRACK_2_BUFFETT_QVM) 장기 복리 종목 및
            # S14 마켓뉴트럴 페어 레그는 단기 전술 스트림(Track 1)의 Cross-Asset Alpha Swap 자금원에서 100% 제외
            _l_meta_ev = _plm_ev.get_position_meta(str(ticker)) if _plm_ev is not None else {}
            _sid_ev = pos.get('stream_id') or pos.get('stream') or _l_meta_ev.get('stream_id', '')
            _trk_ev = pos.get('track') or _l_meta_ev.get('track', '')
            _str_ev = pos.get('strategy') or _l_meta_ev.get('strategy', '')
            if (
                _sid_ev in ('S3_B', 'S14', 'S14_PAIR_ALPHA', 'S7')
                or _trk_ev == 'TRACK_2_BUFFETT_QVM'
                or _str_ev in ('buffett_qvm', 'qvm_value', 'kr_sector_pair_alpha', 'sector_pair_alpha')
                or bool(pos.get('pair_id') or _l_meta_ev.get('pair_id'))
            ):
                continue

            # 현금성 자산(KOFR, CD금리, 단기채)은 스왑 편출 우선순위 최하위 또는 별도 안전자산으로 보호
            if ticker in ('459580', '423160', '449170', '430740', '357870', '357330', 'SHV', 'SGOV'):
                holdings_ev[ticker] = 0.50 # 안전자산 기본 중립값
                continue

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
            elif not is_kr_ticker(ticker):
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

            # (4) 변동성(ATR%) 및 동적 출구 장벽 연동 (통일된 스케일)
            from src.utils.dynamic_quant_engine import get_quant_engine
            _dqe = get_quant_engine()
            _barriers = _dqe.compute_dynamic_exit_barriers(ticker) if ticker else {}
            atr_pct = float(market_data.get('ticker_atr_pct', {}).get(ticker, _barriers.get('sigma_daily_pct', 1.8)))
            tp_atr_mult = float(pget('exit.tp_atr_multiplier', 3.5))
            sl_atr_mult = float(pget('exit.sl_atr_multiplier', 2.0))
            tp_min_pct = float(pget('exit.tp_min_pct', 3.0))
            sl_min_pct = float(pget('exit.sl_min_pct', 1.5))
            tp_pct = float(_barriers.get('take_profit_pct', max(tp_min_pct, atr_pct * tp_atr_mult)))
            sl_pct = abs(float(_barriers.get('stop_loss_pct', max(sl_min_pct, atr_pct * sl_atr_mult))))

            ev_adj = self.compute_ev_adj(win_rate, tp_pct, sl_pct, atr_pct)
            holdings_ev[ticker] = round(ev_adj, 4)

        return holdings_ev

    def route_intraday_capital(
        self,
        signals: List[Dict[str, Any]],
        market_data: Dict[str, Any],
        total_cash_krw: float,
        portfolio: Optional[Dict[str, Any]] = None,
        enforce_session_gate: bool = False
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

        from src.utils.market_calendar import is_kr_ticker

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
        rem_cash_krw = float(total_cash_krw)
        _existing_held = set()
        if portfolio and isinstance(portfolio, dict):
            for _src_k in ('holdings', 'positions'):
                _src_d = portfolio.get(_src_k) or {}
                if isinstance(_src_d, dict):
                    for _hk, _hv in _src_d.items():
                        _ht = (_hv.get('ticker') if isinstance(_hv, dict) else None) or str(_hk).split(':')[-1]
                        _hq = int((_hv.get('qty', _hv.get('quantity', 0)) if isinstance(_hv, dict) else 0) or 0)
                        if _ht and _hq > 0:
                            _existing_held.add(str(_ht).upper())

        from src.data_collection.central_data_gateway import CentralDataGateway
        dyn_fx = CentralDataGateway().get_usdkrw_rate()
        for sig in ranked_signals:
            stream = str(sig.get('stream', sig.get('stream_id', ''))).upper()
            tactic = str(sig.get('tactic', sig.get('strategy', ''))).upper()
            _raw_tk = str(sig.get('ticker', '')).strip()
            from src.utils.ticker_canonical import canonical_ticker
            ticker = canonical_ticker(_raw_tk, context='intraday_capital_router.rank')

            try:
                from src.measurement.data_nogo_store import is_data_nogo as _is_dng_rt
                if _is_dng_rt(str(ticker), kind='ticker'):
                    logger.info(f"  🛑 [Router Data-NoGo Guard] {ticker}: 거래정지/Data-NoGo 등록 종목 ➔ 라우팅/스왑 차단")
                    continue
            except Exception:
                pass
            if isinstance(market_data.get('data_nogo_tickers'), (list, tuple, set)) and str(ticker).upper() in {str(x).upper() for x in market_data.get('data_nogo_tickers', [])}:
                logger.info(f"  🛑 [Router Data-NoGo Guard] {ticker}: 거래정지/Data-NoGo 목록 종목 ➔ 라우팅/스왑 차단")
                continue

            if (enforce_session_gate or market_data.get('enforce_session_gate', False)) and str(ticker).upper() in _existing_held:
                logger.info(f"  🛡️ [Router Position Guard] {ticker}: 이미 보유 중인 종목 ➔ 중복 추가 매수 스킵")
                continue

            # [Cross-Market Inverse Bridge] KRX 휴장 + US 개장 시간대에 발생한 KRX 인버스/곱버스 신호를 US 인버스 ETF(SQQQ/SOXS/SH)로 자동 전환
            if (enforce_session_gate or market_data.get('enforce_session_gate', False)) and is_kr_ticker(ticker):
                from src.utils.market_calendar import is_ticker_tradable_now
                _kr_ok, _ = is_ticker_tradable_now(ticker)
                if not _kr_ok:
                    _us_ok, _ = is_ticker_tradable_now('SQQQ')
                    if _us_ok:
                        _KR_TO_US_INVERSE_MAP = {
                            '252670': ('SQQQ', 'ProShares UltraPro Short QQQ'),
                            '251340': ('SQQQ', 'ProShares UltraPro Short QQQ'),
                            '114800': ('SH', 'ProShares Short S&P500'),
                        }
                        if ticker == '390390' and ('SHORT' in tactic or 'INVERSE' in tactic or '인버스' in str(sig.get('name', ''))):
                            _mapped_us = ('SOXS', 'Direxion Daily Semiconductor Bear 3X Shares')
                        else:
                            _mapped_us = _KR_TO_US_INVERSE_MAP.get(ticker)
                        if _mapped_us:
                            try:
                                from src.execution.position_ledger import PositionLedgerManager
                                _exited_map = PositionLedgerManager().get_daily_exit_details()
                                if _mapped_us[0].upper() in _exited_map:
                                    logger.info(
                                        f"  🛑 [Cross-Market Inverse Bridge Veto] {ticker} ➔ {_mapped_us[0]}: "
                                        f"당일 {_mapped_us[0]} 이미 청산됨 ({_exited_map[_mapped_us[0].upper()].get('reason')}) — 브릿지 재진입 차단"
                                    )
                                    continue
                            except Exception:
                                pass
                            logger.info(f"  🌐 [Cross-Market Inverse Bridge] KRX 휴장 & US 개장 감지: {ticker} ➔ {_mapped_us[0]} ({_mapped_us[1]}) 자동 전환")
                            ticker = _mapped_us[0]
                            sig['name'] = _mapped_us[1]
                            sig['market'] = 'US'
                            sig['price'] = 0.0  # US 실시간 현재가 재조회 강제

            sig['ticker'] = ticker

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
            if enforce_session_gate or market_data.get('enforce_session_gate', False):
                _sig_sz = float(sig.get('size_pct', 0.15) or 0.15)
                alloc_ratio = min(alloc_ratio, max(0.05, min(0.20, _sig_sz)))

            alloc_ratio *= synergy_mult
            alloc_cash = min(rem_cash_krw, total_cash_krw * alloc_ratio)
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
                        price = float(p_info.get('current_price', p_info.get('price', 0.0)) or 0.0)
                # 3. CentralDataGateway 조회 (KR/US 3중 핫페일오버)
                if price <= 1.0:
                    try:
                        from src.data_collection.central_data_gateway import CentralDataGateway
                        _cdg_res = CentralDataGateway().get_current_price(ticker)
                        if isinstance(_cdg_res, dict) and _cdg_res.get('price'):
                            price = float(_cdg_res['price'])
                        elif isinstance(_cdg_res, (int, float)) and _cdg_res > 0:
                            price = float(_cdg_res)
                    except Exception:
                        pass
                # 4. KISPriceService 조회
                if price <= 1.0:
                    try:
                        from src.execution.kis_price_service import KISPriceService
                        _kps_res = KISPriceService().get_current_price(ticker)
                        if isinstance(_kps_res, dict) and _kps_res.get('price'):
                            price = float(_kps_res['price'])
                        elif isinstance(_kps_res, (int, float)) and _kps_res > 0:
                            price = float(_kps_res)
                    except Exception:
                        pass
                # 5. 파킹 ETF 기준가(NAV) 보호 폴백
                if price <= 1.0:
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
                    if ticker in _PARKING_FALLBACK_PX:
                        price = _PARKING_FALLBACK_PX[ticker]
                        logger.info(f"  🛡️ [Router Cash Anchor] {ticker} 파킹 ETF 기준가(₩{price:,.0f}) 확정")
                # 6. 인스티튜셔널 브리지 확인 (국내)
                if price <= 1.0 and is_kr_ticker(ticker):
                    try:
                        from src.data_collection.pykrx_compat import stock as _pykrx_s8
                        from datetime import date as _d, timedelta as _td
                        _t_str = _d.today().strftime('%Y%m%d')
                        _w_str = (_d.today() - _td(days=7)).strftime('%Y%m%d')
                        _df_p = _pykrx_s8.get_market_ohlcv_by_date(_w_str, _t_str, str(ticker))
                        if len(_df_p) > 0:
                            price = float(_df_p.iloc[-1].get('종가', 0.0))
                    except Exception:
                        pass

            if price <= 0:
                logger.warning(f"  ⚠️ [Router] {ticker} 유효 가격 미확인으로 스킵")
                continue

            # [Session Gatekeeper] 해당 종목의 거래소가 현재 거래 가능한 상태인지 사전 필터링 (라이브 오케스트레이터 호출 시)
            if enforce_session_gate or market_data.get('enforce_session_gate', False):
                from src.utils.market_calendar import is_ticker_tradable_now
                is_tradable, session_type = is_ticker_tradable_now(ticker)
                if not is_tradable:
                    logger.info(f"  ⏸️ [Router Session Gate] {ticker} 거래소 휴장 중(session={session_type}) ➔ 주문 생성 스킵")
                    continue

            sig['price'] = price
            is_us = not is_kr_ticker(ticker)
            if is_us and not dyn_fx:
                logger.error(f"  🛑 [FX Guard] {ticker}: USD/KRW 미확보 → 해외 라우팅 스킵 (fail-closed)")
                continue
            eff_price_krw = price * dyn_fx if is_us else price
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
                rem_cash_krw = max(0.0, rem_cash_krw - capped_qty * eff_price_krw)
                routed_signals.append(sig)
            else:
                sig_ev = float(sig.get('ev_adj', 0.5))
                if holdings_ev:
                    is_us_candidate = not is_kr_ticker(ticker)
                    eligible_holdings = {}
                    holdings_dict = portfolio.get('holdings') or portfolio.get('positions') or {} if portfolio else {}

                    # [Co-domain Unification] 현재 사이클 신규 매수 시그널에 동일 보유 종목이 포함되어 있다면 그 높은 기대값을 상속
                    active_sig_ev_map = {
                        str(k).strip().upper(): float(v or 0.0)
                        for k, v in (market_data.get('active_signal_ev_map') or {}).items()
                        if float(v or 0.0) > 0.0
                    }
                    for s in ranked_signals:
                        _s_dir = str(s.get('direction', s.get('action', s.get('side', 'buy')))).lower()
                        _s_ev = float(s.get('ev_adj', 0.0) or 0.0)
                        _s_tk = str(s.get('ticker', '')).strip().upper()
                        if _s_tk and _s_dir in ('buy', 'long') and _s_ev > 0.0:
                            active_sig_ev_map[_s_tk] = max(active_sig_ev_map.get(_s_tk, 0.0), _s_ev)

                    for h_ticker, h_ev in holdings_ev.items():
                        h_pos_info = holdings_dict.get(h_ticker) if isinstance(holdings_dict, dict) else {}
                        if isinstance(h_pos_info, dict) and (
                            h_pos_info.get('stream_id') in ('S3_B', 'S14', 'S14_PAIR_ALPHA', 'S7')
                            or h_pos_info.get('track') == 'TRACK_2_BUFFETT_QVM'
                            or h_pos_info.get('strategy') in ('buffett_qvm', 'qvm_value', 'kr_sector_pair_alpha', 'sector_pair_alpha')
                            or bool(h_pos_info.get('pair_id'))
                        ):
                            continue
                        h_is_us = not is_kr_ticker(h_ticker)
                        if h_is_us == is_us_candidate and h_ticker not in ('459580', '423160', '449170', '430740', '357870', '357330', 'SHV', 'SGOV'):
                            if enforce_session_gate or market_data.get('enforce_session_gate', False):
                                h_tradable, _ = is_ticker_tradable_now(h_ticker)
                                if not h_tradable:
                                    continue
                            h_tk_up = str(h_ticker).strip().upper()
                            if h_tk_up in active_sig_ev_map:
                                # 현재 사이클에 활성 매수 시그널이 존재하는 보유 종목은 자본 스왑 매도 대상에서 원천 제외
                                continue
                            h_ret_pct = float(
                                (h_pos_info.get('return_pct', h_pos_info.get('unrealized_pnl_pct', 0.0)) if isinstance(h_pos_info, dict) else 0.0)
                                or 0.0
                            )
                            if h_ret_pct > (1.0 / 2.0):
                                # 수익 구간(+0.5% 초과) 진행 중인 우위 보유 종목은 조기 스왑 매도에서 보호 (Cut losers, let winners run)
                                continue
                            eff_h_ev = max(float(h_ev), active_sig_ev_map.get(h_tk_up, 0.0))
                            eligible_holdings[h_ticker] = eff_h_ev

                    if eligible_holdings:
                        lowest_ticker = min(eligible_holdings, key=eligible_holdings.get)
                        lowest_ev = eligible_holdings[lowest_ticker]
                        lowest_pos = holdings_dict.get(lowest_ticker) or {}
                        lowest_qty = int(lowest_pos.get('qty', lowest_pos.get('quantity', 0)))
                        lowest_price = float(lowest_pos.get('current_price', lowest_pos.get('price', 0.0)))
                        lowest_name = lowest_pos.get('name', lowest_ticker)
                        lowest_is_us = not is_kr_ticker(lowest_ticker)
                        lowest_price_krw = (lowest_price * dyn_fx if dyn_fx else 0.0) if lowest_is_us else lowest_price
                        
                        # [Pure Dynamic Math] 실제 마찰비용 및 변동성에 기반한 기본 스왑 허들
                        atr_val = float(market_data.get('ticker_atr_pct', {}).get(ticker, market_data.get('atr_pct', 1.5)))
                        friction_kr = float(pget('cost.friction_kr_pct', 0.23))
                        friction_us = float(pget('cost.friction_us_pct', 0.08))
                        friction_pct = friction_kr if not is_us_candidate else friction_us
                        base_swap_premium = round(friction_pct / max(0.5, atr_val), 4)

                        # [Tier-1 Continuous Amortized Cost Penalty Function (Boyd / Almgren-Chriss)]
                        elapsed_mins = float(lowest_pos.get('holding_minutes', lowest_pos.get('holding_time_min', 0.0)) or 0.0)
                        if elapsed_mins <= 0.0:
                            try:
                                from src.execution.position_ledger import PositionLedgerManager
                                from datetime import datetime
                                _plm = PositionLedgerManager()
                                _hwm = _plm.get_hwm(lowest_ticker)
                                _created_at_str = _hwm.get('created_at')
                                if _created_at_str:
                                    _dt_entry = datetime.fromisoformat(str(_created_at_str).replace('Z', '+00:00'))
                                    _now_dt = datetime.now(_dt_entry.tzinfo) if _dt_entry.tzinfo is not None else datetime.now()
                                    _diff_sec = (_now_dt - _dt_entry).total_seconds()
                                    if _diff_sec >= 0:
                                        elapsed_mins = _diff_sec / 60.0
                            except Exception as _t_err:
                                logger.debug(f"Holding elapsed time parse fallback for {lowest_ticker}: {_t_err}")

                        if elapsed_mins <= 0.0:
                            _entry_date = lowest_pos.get('entry_date')
                            _today_str = datetime.now().strftime("%Y-%m-%d")
                            if _entry_date and str(_entry_date)[:10] == _today_str:
                                elapsed_mins = 30.0
                            elif _entry_date and str(_entry_date)[:10] < _today_str:
                                elapsed_mins = 1440.0

                        from src.utils.dynamic_quant_engine import get_quant_engine
                        _dqe = get_quant_engine()
                        _opt_params = _dqe.compute_optimal_stopping_params(
                            ticker=lowest_ticker,
                            regime=market_data.get('regime', 'caution'),
                            price=lowest_price if lowest_price > 0 else 100.0,
                            atr_pct=atr_val
                        )
                        _default_tau = 30.0
                        _min_tau = 15.0
                        _max_tau = 180.0
                        tau_amort = float(_opt_params.get('min_holding_minutes') or _default_tau)
                        tau_amort = max(_min_tau, min(_max_tau, tau_amort * 2.0))

                        _def_kappa = 3.0
                        kappa_turnover = float(cfg.get('swap.kappa_turnover') or _def_kappa)
                        amort_mult = 1.0 + kappa_turnover * math.exp(-max(0.0, elapsed_mins) / max(1.0, tau_amort))
                        amort_friction = base_swap_premium * amort_mult

                        # [Empirical Statistical Significance Hurdle >= 0.25 * sigma_ev]
                        cand_evs = [float(s.get('ev_adj', 0.0)) for s in ranked_signals if float(s.get('ev_adj', 0.0)) > 0]
                        if len(cand_evs) >= 2:
                            _ev_mean = sum(cand_evs) / len(cand_evs)
                            _ev_std = math.sqrt(sum((x - _ev_mean) ** 2 for x in cand_evs) / len(cand_evs))
                        else:
                            _ev_std = 0.50

                        _def_sig_w = 0.25
                        stat_sig_weight = float(cfg.get('swap.stat_sig_hurdle') or _def_sig_w)
                        _min_sig_hurdle = 0.10
                        sigma_hurdle = max(_min_sig_hurdle, stat_sig_weight * _ev_std)
                        total_swap_hurdle = amort_friction + sigma_hurdle

                        if sig_ev > (lowest_ev + total_swap_hurdle):
                            if lowest_qty > 0 and lowest_price_krw > 0 and eff_price_krw > 0:
                                # [Pure Dynamic Math] 신규 매수에 필요한 정확한 수량만큼만 정밀 매도 (임의 35% 강제 청산 폐지)
                                min_buy_amount = eff_price_krw * 1.02
                                sell_qty_needed = max(1, math.ceil(min_buy_amount / lowest_price_krw))
                                sell_qty = min(lowest_qty, sell_qty_needed)
                                _rem_stub_krw = (lowest_qty - sell_qty) * lowest_price_krw
                                _stub_floor_krw = (100.0 + 100.0) * 1000.0
                                if 0 < _rem_stub_krw < _stub_floor_krw:
                                    sell_qty = lowest_qty
                                swap_freed_krw = sell_qty * lowest_price_krw
                                buy_qty = math.floor(swap_freed_krw / eff_price_krw)

                                if buy_qty >= 1:
                                    logger.info(
                                        f"  🔄 [Cross-Asset Alpha Swap Trigger] 신규 {ticker} (EV_adj={sig_ev:.2f}) > "
                                        f"기존 {lowest_ticker} (EV_adj={lowest_ev:.2f}) + 동적교체허들({total_swap_hurdle:.2f}, "
                                        f"감가비용={amort_friction:.2f}(x{amort_mult:.2f}, {elapsed_mins:.0f}m) + 유의성={sigma_hurdle:.2f}) "
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
                                        'reason': f"자본 스왑 매도: {lowest_ticker}(EV={lowest_ev:.2f}) -> {ticker}(EV={sig_ev:.2f}) [Hurdle={total_swap_hurdle:.2f}]",
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
                                    buy_order['reason'] = f"자본 스왑 매수: {ticker}(EV={sig_ev:.2f}) <- {lowest_ticker}(EV={lowest_ev:.2f}) [Hurdle={total_swap_hurdle:.2f}]"
                                    routed_signals.append(buy_order)

                                    # 동일 사이클 내 중복 편출 방지를 위해 lowest_ticker 임시 EV 상향
                                    eligible_holdings[lowest_ticker] = 99.0
                                    holdings_ev[lowest_ticker] = 99.0
                        else:
                            logger.info(
                                f"  🛡️ [Alpha Premium Unmet] 신규 {ticker} (EV_adj={sig_ev:.2f}) <= "
                                f"기존 {lowest_ticker} (EV_adj={lowest_ev:.2f}) + 동적교체허들({total_swap_hurdle:.2f}, "
                                f"감가비용={amort_friction:.2f}(x{amort_mult:.2f}, {elapsed_mins:.0f}m) + 유의성={sigma_hurdle:.2f}) ➔ 기존 포지션 유지 (교체 매매 스킵)"
                            )
                else:
                    logger.info(
                        f"  ℹ️ [Capital Limit] 신규 {ticker}: 단가 ₩{eff_price_krw:,.0f} > 가용현금 ₩{total_cash_krw:,.0f} "
                        f"& 기존 자산 손절/청산 조건 미도달 ➔ 기존 포트폴리오 유지"
                    )

        return routed_signals

"""
Smart Order Router — 최적 거래소 라우팅 엔진
================================================

Medallion Upgrade Phase 2-C-2.

기능:
  1. 다차원 의사결정: 시간대 + 스프레드 + 주문크기 + 유동성 + 수수료
  2. KRX / NexTrade / SOR 최적 자동 선택
  3. 거래소별 비용 시뮬레이션 → 최저 비용 경로 선택
  4. 주문 분할 권고 (대형 주문 → 멀티-벤뉴)

거래소 특성:
  - KRX: 정규장(09:00~15:20), 유동성 최대, 수수료 중간
  - NXT: 시간외(08:00~08:50, 15:30~20:00), 수수료 최저, 유동성 제한
  - SOR: KRX+NXT 자동 선택 (증권사), 수수료 중간

모든 파라미터 DynamicConfig 동적 로드.
"""
import json
import logging
import math
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class SmartOrderRouter:
    """다차원 최적 거래소 라우팅."""

    def __init__(self):
        self._tca_impact_modifier = 1.0
        self._load_tca_feedback()

    def _load_tca_feedback(self):
        """[Phase 86] TCA 피드백 루프: 어제자 실제 체결 오차율을 불러옵니다."""
        import json
        from pathlib import Path
        try:
            feedback_path = Path(__file__).resolve().parent.parent.parent / 'results' / 'tca_feedback.json'
            if feedback_path.exists():
                with open(feedback_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._tca_impact_modifier = data.get('recommended_impact_modifier', 1.0)
                    logger.info(f'  [SOR] TCA 피드백 적용: impact_modifier = {self._tca_impact_modifier:.3f}')
        except Exception as e:
            logger.error(f'  [SOR] TCA 피드백 로드 실패: {e}')

    def route(self, order: Dict, market_data: Dict=None) -> Dict:
        """주문에 대한 최적 거래소 선택.

        Args:
            order: {
                'ticker': str,
                'action': 'buy' | 'sell',
                'quantity': int,
                'price': float,         # 현재가/희망가
                'urgency': 'low' | 'normal' | 'high',
            }
            market_data: {
                'spread_krx_bps': float,  # KRX 스프레드 (bps)
                'spread_nxt_bps': float,  # NXT 스프레드 (bps)
                'adv_krx': int,           # KRX 일평균거래량
                'adv_nxt': int,           # NXT 일평균거래량
                'vix': float,             # 변동성 지표
            }

        Returns:
            {
                'venue': 'KRX' | 'NXT' | 'SOR',
                'reason': str,
                'estimated_cost_bps': float,
                'split_recommendation': Optional[List],
                'scores': Dict,
            }
        """
        if market_data is None:
            market_data = {}
        session = self._current_session()
        order_amount = order.get('price', 0) * order.get('quantity', 0)
        urgency = order.get('urgency', 'normal')
        available_venues = self._available_venues(session)
        if not available_venues:
            return {'venue': 'CLOSED', 'reason': '거래 불가 시간대', 'estimated_cost_bps': 0, 'split_recommendation': None, 'scores': {}}
        if len(available_venues) == 1:
            venue = available_venues[0]
            cost = self._estimate_cost(venue, order_amount, market_data)
            return {'venue': venue, 'reason': f'{session} 세션 → {venue} 전용', 'estimated_cost_bps': round(cost, 2), 'split_recommendation': None, 'scores': {venue: 1.0}}
        venue_costs = {}
        venue_scores = {}
        for venue in available_venues:
            cost = self._estimate_cost(venue, order_amount, market_data)
            venue_costs[venue] = cost
        for venue in available_venues:
            score = self._compute_venue_score(venue, order_amount, urgency, market_data, venue_costs[venue])
            venue_scores[venue] = score
        best_venue = max(venue_scores, key=venue_scores.get)
        best_cost = venue_costs[best_venue]
        split = self._split_recommendation(best_venue, order_amount, market_data, available_venues)
        reason = self._build_reason(best_venue, session, order_amount, venue_scores, venue_costs)
        return {'venue': best_venue, 'reason': reason, 'estimated_cost_bps': round(best_cost, 2), 'split_recommendation': split, 'scores': {v: round(s, 4) for v, s in venue_scores.items()}}

    def _current_session(self) -> str:
        """현재 거래 세션 판별."""
        now_t = datetime.now().time().replace(second=0, microsecond=0)

        def _to_time(s, fallback):
            try:
                parts = str(s).split(':')
                return dtime(int(parts[0]), int(parts[1]))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return fallback
        pre_start_t = _to_time(cfg.get('router.session_pre_start', '08:00'), dtime(8, 0))
        pre_end_t = _to_time(cfg.get('router.session_pre_end', '08:50'), dtime(8, 50))
        reg_start_t = _to_time(cfg.get('router.session_regular_start', '09:00'), dtime(9, 0))
        reg_end_t = _to_time(cfg.get('router.session_regular_end', '15:20'), dtime(15, 20))
        after_start_t = _to_time(cfg.get('router.session_after_start', '15:30'), dtime(15, 30))
        after_end_t = _to_time(cfg.get('router.session_after_end', '20:00'), dtime(20, 0))
        if pre_start_t <= now_t < pre_end_t:
            return 'pre'
        elif reg_start_t <= now_t < reg_end_t:
            return 'regular'
        elif after_start_t <= now_t < after_end_t:
            return 'after'
        return 'closed'

    def _available_venues(self, session: str) -> List[str]:
        """세션별 거래 가능 거래소."""
        _DEFAULT_VENUE_MAP = {'regular': ['KRX', 'NXT', 'SOR'], 'pre': ['NXT'], 'after': ['NXT'], 'closed': []}
        venue_map = cfg.get('router.venue_map', _DEFAULT_VENUE_MAP)
        if not isinstance(venue_map, dict):
            venue_map = _DEFAULT_VENUE_MAP
        return venue_map.get(session, [])

    def _estimate_cost(self, venue: str, order_amount: float, market_data: Dict) -> float:
        """거래소별 총 비용 추정 (bps).

        총 비용 = 수수료 + 스프레드 비용 + 시장 충격

        Args:
            venue: 거래소
            order_amount: 주문 금액
            market_data: 시장 데이터

        Returns:
            추정 총 비용 (bps)
        """
        commission_bps = cfg.get(f'router.commission_bps.{venue.lower()}', {'KRX': 0.88, 'NXT': 0.53, 'SOR': 0.7}.get(venue, 0.7))
        spread_key = f'spread_{venue.lower()}_bps'
        spread_bps = market_data.get(spread_key, cfg.get(f'router.default_spread_bps.{venue.lower()}', {'KRX': 3.0, 'NXT': 5.0, 'SOR': 3.5}.get(venue, 3.5)))
        half_spread = spread_bps / 2.0
        adv_key = f'adv_{venue.lower()}'
        adv = market_data.get(adv_key, 0)
        if adv > 0:
            participation = order_amount / adv
        else:
            default_adv = cfg.get(f'router.default_adv.{venue.lower()}', {'KRX': 5000000000.0, 'NXT': 1000000000.0, 'SOR': 5000000000.0}.get(venue, 5000000000.0))
            participation = order_amount / default_adv
        impact_coeff = cfg.get('router.impact_coefficient', 10.0)
        dynamic_impact_coeff = impact_coeff * getattr(self, '_tca_impact_modifier', 1.0)
        impact_bps = dynamic_impact_coeff * math.sqrt(max(0, participation))
        return commission_bps + half_spread + impact_bps

    def _compute_venue_score(self, venue: str, order_amount: float, urgency: str, market_data: Dict, estimated_cost: float) -> float:
        """거래소 종합 점수 계산.

        점수 = w_cost × 비용점수 + w_liq × 유동성점수
               + w_speed × 속도점수 + w_reliability × 안정성점수

        Args:
            venue: 거래소
            order_amount: 주문 금액
            urgency: 긴급도
            market_data: 시장 데이터
            estimated_cost: 추정 비용

        Returns:
            종합 점수 (0~1, 높을수록 좋음)
        """
        if urgency == 'high':
            w_cost = cfg.get('router.weight_cost_high', 0.2)
            w_liq = cfg.get('router.weight_liquidity_high', 0.3)
            w_speed = cfg.get('router.weight_speed_high', 0.35)
            w_rel = cfg.get('router.weight_reliability_high', 0.15)
        elif urgency == 'low':
            w_cost = cfg.get('router.weight_cost_low', 0.5)
            w_liq = cfg.get('router.weight_liquidity_low', 0.2)
            w_speed = cfg.get('router.weight_speed_low', 0.1)
            w_rel = cfg.get('router.weight_reliability_low', 0.2)
        else:
            w_cost = cfg.get('router.weight_cost_normal', 0.35)
            w_liq = cfg.get('router.weight_liquidity_normal', 0.25)
            w_speed = cfg.get('router.weight_speed_normal', 0.2)
            w_rel = cfg.get('router.weight_reliability_normal', 0.2)
        max_cost = cfg.get('router.max_cost_bps', 20)
        cost_score = max(0, 1 - estimated_cost / max_cost)
        liq_scores = cfg.get('router.liquidity_scores', {'KRX': 0.95, 'NXT': 0.5, 'SOR': 0.9})
        liq_score = liq_scores.get(venue, 0.5) if isinstance(liq_scores, dict) else 0.5
        large_threshold = cfg.get('router.large_order_threshold', 50000000)
        if order_amount > large_threshold:
            liq_score *= cfg.get('router.large_order_liq_boost', 1.2)
            liq_score = min(1.0, liq_score)
        speed_scores = cfg.get('router.speed_scores', {'KRX': 0.9, 'NXT': 0.85, 'SOR': 0.95})
        speed_score = speed_scores.get(venue, 0.85) if isinstance(speed_scores, dict) else 0.85
        vix = market_data.get('vix', 0)
        vix_threshold = cfg.get('router.vix_instability_threshold', 30)
        rel_scores = cfg.get('router.reliability_scores', {'KRX': 0.98, 'NXT': 0.8, 'SOR': 0.92})
        rel_score = rel_scores.get(venue, 0.85) if isinstance(rel_scores, dict) else 0.85
        if vix > vix_threshold and venue == 'NXT':
            penalty = cfg.get('router.nxt_vix_penalty', 0.15)
            rel_score = max(0.3, rel_score - penalty)
        total = w_cost * cost_score + w_liq * liq_score + w_speed * speed_score + w_rel * rel_score
        return total

    def _split_recommendation(self, primary_venue: str, order_amount: float, market_data: Dict, available_venues: List[str]) -> Optional[List[Dict]]:
        """대형 주문 분할 권고.

        ADV의 일정 비율 이상이면 멀티-벤뉴 분할 권고.
        """
        split_threshold = cfg.get('router.split_threshold_amount', 100000000)
        if order_amount < split_threshold:
            return None
        if len(available_venues) < 2:
            return None
        venue_costs = {}
        for venue in available_venues:
            cost = self._estimate_cost(venue, order_amount, market_data)
            venue_costs[venue] = max(cost, 0.01)
        total_inv_cost = sum((1.0 / c for c in venue_costs.values()))
        splits = []
        for venue in available_venues:
            weight = 1.0 / venue_costs[venue] / total_inv_cost
            split_amount = order_amount * weight
            splits.append({'venue': venue, 'weight_pct': round(weight * 100, 1), 'amount': round(split_amount, 0), 'estimated_cost_bps': round(venue_costs[venue], 2)})
        splits.sort(key=lambda x: x['weight_pct'], reverse=True)
        return splits

    def _build_reason(self, venue: str, session: str, order_amount: float, scores: Dict, costs: Dict) -> str:
        """선택 이유 생성."""
        parts = [f'{session}세션']
        if order_amount > cfg.get('router.large_order_threshold', 50000000):
            parts.append(f'대형주문(₩{order_amount:,.0f})')
        sorted_venues = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        if len(sorted_venues) >= 2:
            margin = sorted_venues[0][1] - sorted_venues[1][1]
            runner_up = sorted_venues[1][0]
            cost_saving = costs.get(runner_up, 0) - costs.get(venue, 0)
            if cost_saving > 0:
                parts.append(f'vs {runner_up} -{cost_saving:.1f}bps')
        return f'{venue} 선택 ({', '.join(parts)})'

    def route_batch(self, orders: List[Dict], market_data: Dict=None) -> List[Dict]:
        """복수 주문 일괄 라우팅.

        Args:
            orders: 주문 리스트
            market_data: 공유 시장 데이터

        Returns:
            각 주문별 라우팅 결과
        """
        results = []
        for order in orders:
            result = self.route(order, market_data)
            result['ticker'] = order.get('ticker', '')
            results.append(result)
        return results

    def get_dynamic_limit(self, action: str, base_price: float, oim: float, spread_pct: float) -> float:
        """OIM 수치를 기반으로 최적 지정가(Dynamic Limit) 산출.
        
        OIM > 1.5 : 매수 압력 강함 (매수벽 두꺼움)
        OIM < 0.7 : 매도 압력 강함 (매도벽 두꺼움)
        
        Args:
            action: 'buy' 또는 'sell'
            base_price: 현재가 (또는 기준가)
            oim: Orderbook Imbalance (총 매수잔량 / (총매수잔량 + 총매도잔량)의 비율 혹은 순수 비율)
                 - 참고: 현재 kis_websocket은 (bid - ask) / max(bid+ask, 1) 로 산출 중 (범위 -1.0 ~ 1.0)
                 - OIM 환산: 1.0이면 매수 압력 극대화, -1.0이면 매도 압력 극대화.
                 - 여기서 OIM = (bid - ask) / (bid + ask) 로 가정.
            spread_pct: 스프레드 비율 (%)
        
        Returns:
            최적 지정가 (float)
        """

        def _tick_size(price: float) -> int:
            if price < 2000:
                return 1
            if price < 5000:
                return 5
            if price < 20000:
                return 10
            if price < 50000:
                return 50
            if price < 200000:
                return 100
            if price < 500000:
                return 500
            return 1000
        tick = _tick_size(base_price)
        buy_thresh = 0.5
        sell_thresh = -0.3
        try:
            import json
            from pathlib import Path
            thresholds_path = Path(__file__).resolve().parent.parent.parent / 'results' / 'oim_thresholds.json'
            if thresholds_path.exists():
                with open(thresholds_path, 'r', encoding='utf-8') as f:
                    th = json.load(f)
                    default_th = th.get('DEFAULT', {})
                    buy_thresh = default_th.get('buy_threshold', 0.5)
                    sell_thresh = default_th.get('sell_threshold', -0.3)
        except Exception as e:
            logger.critical(f'  [SOR] 동적 임계값 로드 실패, 기본값 사용: {e}', exc_info=True)
        if action == 'buy':
            if oim > buy_thresh:
                return base_price + tick
            elif oim < sell_thresh:
                return base_price - tick * 2
            else:
                return base_price - tick
        elif action == 'sell':
            if oim < sell_thresh:
                return base_price - tick
            elif oim > buy_thresh:
                return base_price + tick * 2
            else:
                return base_price + tick
        return base_price

    def route_with_algo(self, order: Dict, market_data: Optional[Dict]=None) -> Dict:
        """주문 라우팅 + TWAP 자동 연결 (Phase 2 통합 인터페이스).

        [Live Patch] Phase 2 Execution/Risk 업데이트:
        주문 금액(price × quantity)이 TWAP 임계(기본 1,000만 원) 이상이면
        TWAPDispatcher를 통해 자동으로 5~10분 분할주문 스케줄을 생성합니다.
        금액 미달 시 지정가 즉시 체결(IMMEDIATE)로 처리.

        Args:
            order: {'ticker', 'action', 'quantity', 'price', 'stream', 'urgency'}
            market_data: SmartOrderRouter.route() 키와 동일

        Returns:
            {
                'routing'      : SmartOrderRouter.route() 결과,
                'algo'         : 'TWAP' | 'IMMEDIATE',
                'slices'       : List[OrderSlice] (빈 리스트이면 IMMEDIATE),
                'twap_triggered': bool,
            }
        """
        if market_data is None:
            market_data = {}
        original_price = order.get('price', 0)
        action = order.get('action', 'buy')
        if 'imbalance' in market_data:
            oim = market_data['imbalance']
            spread_pct = market_data.get('spread_pct', 0.0)
            dynamic_price = self.get_dynamic_limit(action, original_price, oim, spread_pct)
            order['price'] = dynamic_price
            order['dynamic_limit_applied'] = True
            order['oim'] = oim
            logger.info(f'  [SOR] Dynamic Limit: {action.upper()} {original_price:,.0f} -> {dynamic_price:,.0f} (OIM: {oim:.2f})')
        routing = self.route(order, market_data)
        dispatcher = TWAPDispatcher()
        dispatch = dispatcher.dispatch(order)
        return {'routing': routing, 'algo': dispatch.get('algo', 'IMMEDIATE'), 'slices': dispatch.get('slices', []), 'twap_triggered': dispatch.get('twap_triggered', False)}

class TWAPDispatcher:
    """대형 주문 TWAP 자동 라우팅 (Anti-Slippage).

    [Live Patch] Phase 2 Execution/Risk 업데이트:

    주문 금액 TWAP_THRESHOLD(기본 1,000만 원) 이상 시
    AlgoExecutor.twap_schedule()을 통해 5~10분 분할 지정가 주문을 생성.

    목적:
      - 시장가 대량 매수로 인한 슬리피지 폭탄 방지
      - 'One Shot 시장가' 대신 지정가 분할 주문으로 복수 체결 기회
      - 기관식 Stealth Execution: 대량 주문의 시장 시그널 미노출

    DynamicConfig 키:
      execution.twap_threshold_krw    — TWAP 적용 최소 주문 금액, 기본 10_000_000
      execution.twap_duration_min     — TWAP 실행 기간(분), 기본 7
      execution.twap_min_slice_amount — 슬라이스 최소 금액, 기본 500_000
      execution.twap_max_slices       — 최대 분할 수, 기본 10
    """

    def __init__(self):
        self.threshold: float = float(cfg.get('execution.twap_threshold_krw') or 10000000)
        self.duration_min: int = int(cfg.get('execution.twap_duration_min') or 7)
        self.min_slice_amount: float = float(cfg.get('execution.twap_min_slice_amount') or 500000)
        self.max_slices: int = int(cfg.get('execution.twap_max_slices') or 10)

    def dispatch(self, order: Dict, adv: float=0.0) -> Dict:
        """[Live Patch] Phase 2 Execution/Risk 업데이트

        주문 금액에 따라 TWAP vs IMMEDIATE 자동 선택.

        Args:
            order: {'ticker', 'action', 'quantity', 'price', 'stream', 'urgency'}
            adv  : 일평균거래량 (주). 제공 시 VWAP 폴백 항목.

        Returns:
            {
                'algo'          : 'TWAP' | 'IMMEDIATE',
                'slices'        : List[OrderSlice],
                'twap_triggered': bool,
                'order_amount'  : float,
                'threshold'     : float,
                'n_slices'      : int,
                'duration_min'  : int,
                'reason'        : str,
            }
        """
        price = float(order.get('price', 0.0) or 0.0)
        qty = int(order.get('quantity', 0) or 0)
        order_amount = price * qty
        ticker = order.get('ticker', '')
        action = order.get('action', 'buy')
        price_limit: Optional[float] = order.get('price_limit') or price
        if order_amount < self.threshold or qty <= 0:
            logger.debug(f'  통합라우터: {ticker} {action} ₩{order_amount:,.0f} < TWAP 임계 ₩{self.threshold:,.0f} → IMMEDIATE')
            return {'algo': 'IMMEDIATE', 'slices': [], 'twap_triggered': False, 'order_amount': order_amount, 'threshold': self.threshold, 'n_slices': 0, 'duration_min': 0, 'reason': f'₩{order_amount:,.0f} < TWAP 임계: IMMEDIATE'}
        try:
            from src.execution.algo_executor import AlgoExecutor
            algo = AlgoExecutor()
            slices = algo.twap_schedule(order=order, duration_minutes=self.duration_min)
            for sl in slices:
                if sl.price_limit is None:
                    sl.price_limit = price_limit
            n = len(slices)
            logger.info(f'  통합라우터: {ticker} {action} ₩{order_amount:,.0f} ≥ TWAP 임계 ₩{self.threshold:,.0f} → TWAP {n}분할 @ {self.duration_min}분')
            return {'algo': 'TWAP', 'slices': slices, 'twap_triggered': True, 'order_amount': order_amount, 'threshold': self.threshold, 'n_slices': n, 'duration_min': self.duration_min, 'reason': f'₩{order_amount:,.0f} ≥ TWAP 임계: {n}분할 @ {self.duration_min}분 지정가로 실행'}
        except Exception as exc:
            logger.error(f'  ❌ TWAPDispatcher: AlgoExecutor 연동 실패 → IMMEDIATE fallback: {exc}')
            return {'algo': 'IMMEDIATE', 'slices': [], 'twap_triggered': False, 'order_amount': order_amount, 'threshold': self.threshold, 'n_slices': 0, 'duration_min': 0, 'reason': f'AlgoExecutor 오류: IMMEDIATE fallback ({exc})'}
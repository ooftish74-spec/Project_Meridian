"""
Algorithmic Execution — TWAP/VWAP/POV 주문 분할 실행
======================================================

Medallion-grade execution: 시장 충격 최소화를 위한 알고리즘 실행.

Strategy:
  - TWAP: 균등 시간 분할 (기본)
  - VWAP: 예상 거래량 프로파일 기반 분할
  - POV:  Percentage of Volume — 실시간 시장량 추종 (Stealth Execution)
  - Adaptive: 시장 상황에 따라 TWAP/VWAP/POV 자동 선택

POV 특징:
  - 실시간 시장 거래량의 설정% 이하로만 참여
  - 기관이 자신의 주문 의도(Signaling)를 숨길 때 사용
  - 유동성이 낮은 시간대에 자동 슬로우다운

Usage:
    from src.execution.algo_executor import AlgoExecutor
    algo = AlgoExecutor()
    slices = algo.twap_schedule(order, duration_minutes=30)
    slices = algo.pov_schedule(order, adv=200000)  # POV
"""
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from src.utils.emergency_pager import send_emergency_page
logger = logging.getLogger(__name__)
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

@dataclass
class OrderSlice:
    """분할 주문 단위."""
    slice_id: int
    ticker: str
    action: str
    quantity: int
    scheduled_time: str
    price_limit: Optional[float] = None
    algo: str = 'TWAP'
    urgency: str = 'normal'
    status: str = 'pending'

    def to_dict(self) -> Dict:
        return asdict(self)

class AlgoExecutor:
    """TWAP/VWAP 알고리즘 실행기.

    Strategy:
    - TWAP: 균등 시간 분할 (기본)
    - VWAP: 예상 거래량 프로파일 기반 분할
    - Adaptive: 시장 상황에 따라 TWAP/VWAP 자동 선택
    """
    VOLUME_PROFILE = [0.15, 0.1, 0.08, 0.07, 0.06, 0.06, 0.06, 0.07, 0.08, 0.08, 0.09, 0.1]

    def __init__(self):
        self.max_slices = _cfg.get('execution.max_slices', 10) if _cfg else 10
        self.participation_rate = _cfg.get('execution.participation_rate', 0.1) if _cfg else 0.1
        self.pov_rate = _cfg.get('execution.pov_rate', 0.05) if _cfg else 0.05
        self.pov_max_duration_min = _cfg.get('execution.pov_max_duration_min', 120) if _cfg else 120
        self.pov_adv_threshold = _cfg.get('execution.pov_adv_threshold', 0.05) if _cfg else 0.05
        
        # [V2 Dynamic Slicing Parameters]
        self.impact_threshold_pct = _cfg.get('execution.impact_threshold_pct', 0.005) if _cfg else 0.005
        self.slice_chunk_pct = _cfg.get('execution.slice_chunk_pct', 0.002) if _cfg else 0.002
        self.absolute_min_slice = _cfg.get('execution.absolute_min_slice', 5000000) if _cfg else 5000000
        self.fallback_min_amount = float(_cfg.get('portfolio.initial_capital', 1000000.0)) if _cfg else 1000000.0

    def twap_schedule(self, order: Dict, duration_minutes: int=30, n_slices: Optional[int]=None, adv: float=0) -> List[OrderSlice]:
        """TWAP 분할 스케줄 생성.

        Args:
            order: {'ticker', 'action', 'quantity', 'price', 'stream'}
            duration_minutes: 실행 기간 (분)
            n_slices: 분할 수 (None이면 자동 계산)
            adv: Average Daily Volume (주) - 동적 슬라이싱에 사용
        """
        ticker = order.get('ticker', '')
        total_qty = order.get('quantity', 0)
        price = order.get('price', 0)
        action = order.get('action', 'buy')
        if total_qty <= 0:
            return []
            
        # 장 마감 방어 로직 (End of Day Limit)
        now = datetime.now()
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if market_close > now:
            minutes_to_close = (market_close - now).total_seconds() / 60.0
            if 0 < minutes_to_close < duration_minutes:
                duration_minutes = max(1, int(minutes_to_close))
                logger.info(f"  [TWAP Edge Case] 장 마감 임박으로 실행 기간을 {duration_minutes}분으로 축소")
            
        # [Task 1] AWS 고도화 Option B: 1분 단위 분할 (Micro-TWAP)
        # 사용자가 1분 단위(Option B)를 선택했으므로, duration_minutes와 n_slices를 일치시킵니다.
        # 단, 동적 슬라이싱 로직을 통과한 이후에 적용 (아래에서 n_slices 덮어쓰기 방지)
        
        if n_slices is None:
            adtv = price * adv if adv > 0 else 0
            total_amount = total_qty * price
            
            # 1. Zero Division / No Data Fallback
            if adtv <= 0:
                n_slices = 5 if total_amount >= self.fallback_min_amount else 1
            else:
                # 2. Commission Floor & ADTV Impact Threshold
                if total_amount < self.absolute_min_slice or total_amount < adtv * self.impact_threshold_pct:
                    n_slices = 1
                else:
                    # 3. Dynamic Chunking
                    n_slices = math.ceil(total_amount / max(1.0, (adtv * self.slice_chunk_pct)))
            
            # 4. 황제주 소수점 매수 불가 방어 (Price > Slice Value)
            while n_slices > 1 and (total_amount / n_slices) < price:
                n_slices -= 1
                
            n_slices = max(1, min(self.max_slices, int(n_slices)))
            
            # 장 마감 시간 초과 방지
            if n_slices > duration_minutes:
                n_slices = max(1, duration_minutes)
            
        base_qty = total_qty // n_slices
        remainder = total_qty % n_slices
        interval = timedelta(minutes=duration_minutes / n_slices)
        now = datetime.now()
        slices = []
        for i in range(n_slices):
            qty = base_qty + (1 if i < remainder else 0)
            scheduled = now + interval * i
            slices.append(OrderSlice(slice_id=i + 1, ticker=ticker, action=action, quantity=qty, scheduled_time=scheduled.isoformat(), algo='TWAP'))
        logger.info(f'  TWAP: {ticker} {action} {total_qty}주 → {n_slices}분할, {duration_minutes}분 간격')
        return slices

    def vwap_schedule(self, order: Dict, adv: float=0) -> List[OrderSlice]:
        """VWAP 분할 스케줄 생성.

        Args:
            order: {'ticker', 'action', 'quantity', 'price'}
            adv: Average Daily Volume (주)
        """
        ticker = order.get('ticker', '')
        total_qty = order.get('quantity', 0)
        action = order.get('action', 'buy')
        if total_qty <= 0:
            return []
        if adv > 0:
            max_qty = int(adv * self.participation_rate)
            if total_qty > max_qty:
                logger.warning(f'  VWAP: {ticker} 주문량({total_qty}) > 참여한도({max_qty}), 제한 적용')
                total_qty = max_qty
        now = datetime.now()
        current_hour = now.hour
        current_min = now.minute
        start_idx = max(0, (current_hour - 9) * 2 + (1 if current_min >= 30 else 0))
        remaining_profile = self.VOLUME_PROFILE[start_idx:]
        if not remaining_profile:
            return self.twap_schedule(order, duration_minutes=30, adv=adv)
        total_weight = sum(remaining_profile)
        if total_weight <= 0:
            return self.twap_schedule(order, duration_minutes=30, adv=adv)
        slices = []
        cumulative = 0
        base_time = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
        for i, weight in enumerate(remaining_profile):
            qty = max(1, int(total_qty * weight / total_weight))
            cumulative += qty
            if cumulative > total_qty:
                qty -= cumulative - total_qty
                cumulative = total_qty
            if qty <= 0:
                continue
            scheduled = base_time + timedelta(minutes=30 * i)
            slices.append(OrderSlice(slice_id=len(slices) + 1, ticker=ticker, action=action, quantity=qty, scheduled_time=scheduled.isoformat(), algo='VWAP'))
        logger.info(f'  VWAP: {ticker} {action} {total_qty}주 → {len(slices)}분할 (거래량 가중)')
        return slices

    def pov_schedule(self, order: Dict, adv: float=0) -> List[OrderSlice]:
        """POV (Percentage of Volume) 분할 스케줄 생성.

        기관의 Stealth Execution 전략:
        - 실시간 시장 거래량의 pov_rate(%) 이하로만 참여
        - 시간당 예상 거래량 × pov_rate = 슬라이스 크기
        - 초과 시 다음 슬롯으로 이월 (Signaling 은닉)

        Args:
            order: {'ticker', 'action', 'quantity', 'price', 'urgency'}
            adv:   Average Daily Volume (주) — 하루치 예상 거래량
        """
        ticker = order.get('ticker', '')
        total_qty = order.get('quantity', 0)
        action = order.get('action', 'buy')
        if total_qty <= 0:
            return []
        if adv <= 0:
            logger.warning(f'  POV: {ticker} ADV 없음 → TWAP 폴백')
            return self.twap_schedule(order, duration_minutes=self.pov_max_duration_min, adv=adv)
        alpha_decay = float(order.get('alpha_decay', 0.0))
        target_pov_rate = self.pov_rate
        if alpha_decay > 0:
            target_pov_rate = min(0.3, self.pov_rate + alpha_decay * 0.25)
        now = datetime.now()
        current_hour = now.hour
        current_min = now.minute
        start_idx = max(0, (current_hour - 9) * 2 + (1 if current_min >= 30 else 0))
        remaining_profile = self.VOLUME_PROFILE[start_idx:]
        if not remaining_profile:
            logger.warning(f'  POV: {ticker} 장 마감 시간 → TWAP 폴백')
            return self.twap_schedule(order, duration_minutes=30, adv=adv)
        total_weight = sum(remaining_profile)
        slices = []
        cumulative = 0
        base_time = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
        max_duration_slots = self.pov_max_duration_min // 30
        for i, weight in enumerate(remaining_profile):
            if i >= max_duration_slots:
                break
            slot_market_vol = adv * (weight / total_weight) if total_weight > 0 else 0
            max_slot_qty = max(1, int(slot_market_vol * target_pov_rate))
            qty = min(max_slot_qty, total_qty - cumulative)
            if qty <= 0:
                break
            cumulative += qty
            scheduled = base_time + timedelta(minutes=30 * i)
            slices.append(OrderSlice(slice_id=len(slices) + 1, ticker=ticker, action=action, quantity=qty, scheduled_time=scheduled.isoformat(), algo='POV', urgency='low'))
            if cumulative >= total_qty:
                break
        unfilled = total_qty - cumulative
        if unfilled > 0 and slices:
            logger.warning(f'  POV: {ticker} 잔여 {unfilled}주 미처리 → VWAP 전환')
            vwap_order = dict(order)
            vwap_order['quantity'] = unfilled
            vwap_slices = self.vwap_schedule(vwap_order, adv)
            slices.extend(vwap_slices)
        logger.info(f'  POV: {ticker} {action} {total_qty}주 → {len(slices)}분할 (참여율={target_pov_rate:.1%}, ADV={adv:,.0f}주)')
        return slices

    def select_algo(self, order: Dict, adv: float=0) -> str:
        """주문 특성에 따라 최적 알고리즘 선택.

        선택 우선순위:
          1. TCA 피드백: 과거 슬리피지 높은 종목 → VWAP
          2. 소액 주문 → IMMEDIATE
          3. 긴급(high) + 큰 주문 → TWAP
          4. ADV 대비 주문 크고 긴급도 낮음 → POV (Stealth)
          5. ADV 대비 주문 큼 → VWAP
          6. 기본 → TWAP
        """
        total_qty = order.get('quantity', 0)
        price = order.get('price', 0)
        amount = total_qty * price
        urgency = order.get('urgency', 'normal')
        ticker = order.get('ticker', '')
        try:
            from src.execution.tca import TCAAnalyzer
            tca = TCAAnalyzer()
            impact_score = tca.get_market_impact_score(ticker)
            tca_threshold = _cfg.get('execution.tca_adaptive_threshold_bps', 10.0) if _cfg else 10.0
            if impact_score > tca_threshold:
                logger.warning(f'  TCA 피드백: {ticker} 과거 슬리피지({impact_score}bps) 초과. VWAP으로 우회합니다.')
                return 'VWAP'
        except Exception as e:
            logger.critical(f'TCA 연동 오류: {e}', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at algo_executor.py:337', exc_info=e)
        if amount < self.min_slice_amount:
            return 'IMMEDIATE'
        if urgency == 'high':
            return 'TWAP'
        if adv > 0 and total_qty > adv * 0.01:
            return 'VWAP'
        return 'TWAP'

    def schedule(self, order: Dict, adv: float=0) -> List[OrderSlice]:
        """자동 알고리즘 선택 후 스케줄 생성."""
        algo = self.select_algo(order, adv)
        if algo == 'IMMEDIATE':
            return [OrderSlice(slice_id=1, ticker=order.get('ticker', ''), action=order.get('action', 'buy'), quantity=order.get('quantity', 0), scheduled_time=datetime.now().isoformat(), algo='IMMEDIATE')]
        elif algo == 'POV':
            return self.pov_schedule(order, adv)
        elif algo == 'VWAP':
            return self.vwap_schedule(order, adv)
        else:
            return self.twap_schedule(order, adv=adv)

    def estimate_market_impact(self, order: Dict, adv: float=0, volatility: float=0.0) -> float:
        """시장 충격 비용 추정 (bps).

        Almgren-Chriss Square-root market impact:
          impact_bps = η × σ × √(Q / ADV) × 10000

        Args:
            order:      주문 딕셔너리
            adv:        Average Daily Volume (주)
            volatility: 일간 변동성 σ (0이면 DynamicConfig 폴백)
        """
        total_qty = order.get('quantity', 0)
        if adv <= 0 or total_qty <= 0:
            return _cfg.get('slippage.default_impact_bps', 10.0) if _cfg else 10.0
        sigma = volatility if volatility > 0 else _cfg.get('slippage.default_daily_vol', 0.02) if _cfg else 0.02
        eta = _cfg.get('slippage.impact_coefficient', 10.0) if _cfg else 10.0
        delta = _cfg.get('slippage.impact_exponent', 0.5) if _cfg else 0.5
        participation = total_qty / adv
        impact_bps = eta * sigma * math.pow(participation, delta) * 10000
        max_bps = _cfg.get('slippage.max_total_bps', 50.0) if _cfg else 50.0
        return round(min(impact_bps, max_bps), 2)
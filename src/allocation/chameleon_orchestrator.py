"""
ChameleonOrchestrator — V2 엔진 (극초동적 카멜레온 전략)
=========================================================

[Phase 80: Hyper-Fluid Chameleon Architecture]

기존 기관용 분산투자(alpha_allocator)를 대체하여 자금 규모 1.5억~20억 구간에
사용되는 이원화 라우팅 엔진입니다.

핵심 원칙:
  1. 고정 비중(Static Weight) 방식 폐기 -> Binary Routing (100% 단일 스트림 할당)
  2. 절대 우위 계층(Hierarchy) 하드코딩:
     - State 3 (CRASH): 거시경제 블랙스완 (S0: Inverse 100%)
     - State 4 (SHADOW): 미시 다이버전스 (S0: Inverse 100%)
     - State 1 (BULL): 상승/투기장 (S10: Bull/Mania 100%)
     - State 2 (CAUTION): 변동성/횡보장 (S1-C: Edge Sniper 100%)
  3. Hysteresis(이력현상) 밴드: 단순 시간 제한 쿨다운 대신 진입/이탈 스코어를 다르게 적용
  4. Global NAV -3% Hard Stop & V-Recovery Exception 내장

Usage:
    from src.allocation.chameleon_orchestrator import ChameleonOrchestrator
    orch = ChameleonOrchestrator()
    allocations = orch.run_routing(market_data, portfolio_state)
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

try:
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
except Exception as e:
    logger.error(f"Failed to load DynamicConfig (Silent Error Prevented): {e}")
    cfg = None

class ChameleonOrchestrator:
    """극초동적 100% Binary 라우팅 오케스트레이터."""
    
    def __init__(self):
        self._current_state = "STATE_2_CAUTION"  # Default Safe State
        self._bull_peak_bull_score = 0.0  # 상승장 모멘텀 고점 추적용 신규 변수
        self._whipsaw_cooloff_counter = 0  # 휩소 방지용 쿨오프 타이머
        
        # Hysteresis 밴드 설정 (DynamicConfig 연동 우선)
        self._bull_enter_threshold = self._cfg_get('chameleon.bull_enter', 80.0)
        self._bull_exit_threshold = self._cfg_get('chameleon.bull_exit', 50.0)
        self._crash_enter_threshold = self._cfg_get('chameleon.crash_enter', 85.0)
        self._crash_exit_threshold = self._cfg_get('chameleon.crash_exit', 40.0)
        self._shadow_enter_threshold = self._cfg_get('chameleon.shadow_enter', 75.0)
        self._shadow_exit_threshold = self._cfg_get('chameleon.shadow_exit', 35.0)

    def _cfg_get(self, key: str, default: Any) -> Any:
        return cfg.get(key, default) if cfg else default

    def run_routing(self, market_data: Dict[str, Any], portfolio_state: Dict[str, Any]) -> Dict[str, float]:
        """현재 시장 데이터를 분석하여 4가지 State 중 하나에 100% 비중 할당."""
        
        # 1. Global NAV Hard Stop & V-Recovery Exception
        nav_drawdown = portfolio_state.get('nav_drawdown', 0.0)
        v_recovery_prob = market_data.get('v_recovery_prob', 0.0)
        
        hard_stop_limit     = float(self._cfg_get('chameleon.hard_stop_limit', -3.0))
        v_recovery_relaxed  = float(self._cfg_get('chameleon.v_recovery_stop_limit', -5.0))
        _v_recovery_thresh = float(self._cfg_get('chameleon.v_recovery_threshold', 0.65))
        if v_recovery_prob >= _v_recovery_thresh:
            hard_stop_limit = v_recovery_relaxed  # V-Recovery Exception (Relaxation)
            logger.info(f"  🛡️ V-Recovery 확률 {_v_recovery_thresh*100:.0f}% 이상! NAV 하드 스탑 임계치 {v_recovery_relaxed}%로 완화 (Whipsaw 방어)")

        safe_asset = "S_SAFE" if bool(self._cfg_get('chameleon.use_safe_haven', False)) else "CASH"

        if nav_drawdown <= hard_stop_limit:
            logger.critical(f"  🚨 GLOBAL NAV HARD STOP 발동! (Drawdown: {nav_drawdown:.2f}%) -> 전량 대피 ({safe_asset})!")
            return {safe_asset: 1.0}

        # [V3 Maximalist] Soft Shutdown 및 Whipsaw Cool-off 완전 철폐 (하드코딩 삭제)
        risk_multiplier = 1.0

        # 2. State Trigger Evaluation
        crash_score  = market_data.get('crash_score', 0.0)      # VIX, VKOSPI 급등
        shadow_score = market_data.get('shadow_score', 0.0)     # Wag-the-Dog 다이버전스
        bull_score   = market_data.get('bull_score', 0.0)       # 거래대금, 상승종목비율

        # [GEX Negative Gamma Threshold Logic]
        signal = market_data.get('signal_cache', {})
        if isinstance(signal, dict):
            gex = signal.get('macro_gex')
            if gex is not None:
                gex = float(gex)
                gex_crash_threshold = float(self._cfg_get('chameleon.gex_crash_threshold', -1e9)) # -1 Billion
                if gex < gex_crash_threshold:
                    logger.critical(f"  🚨 [Negative Gamma] GEX가 극단적 마이너스({gex:,.0f}) 진입! CrashRadar 강제 경고 발동")
                    crash_score = max(crash_score, self._crash_enter_threshold + 5.0)

        # [Phase 95 Decoupling] 기존 15:20 강제 CRASH 오버라이드는 철회됨.
        # 매크로 오케스트레이터(Chameleon)는 오직 거시 점수에만 집중하고,
        # 15:20 종가의 마이크로 갭 대응은 철저히 분리된 S5 엔진이 전담하여 충돌을 막음.

        # 3. Hysteresis Band — crash_exit은 VIX-adaptive 동적값 우선 사용
        # StreamOrchestrator Step 1.5에서 계산된 dynamic_crash_exit이 있으면 우선 적용
        # Enter 임계값은 항상 정적값 유지 (보수적 진입 원칙)
        _effective_crash_exit = float(market_data.get('dynamic_crash_exit', self._crash_exit_threshold))
        logger.debug(
            f"  [Chameleon] crash_score={crash_score:.1f} "
            f"enter={self._crash_enter_threshold} exit={_effective_crash_exit:.1f} "
            f"(static_exit={self._crash_exit_threshold})"
        )

        is_crash = self._evaluate_hysteresis(self._current_state == "STATE_3_CRASH", crash_score, self._crash_enter_threshold, _effective_crash_exit)
        is_shadow = self._evaluate_hysteresis(self._current_state == "STATE_4_SHADOW", shadow_score, self._shadow_enter_threshold, self._shadow_exit_threshold)
        
        # [Red Team 1] Market Breadth 수학적 진입 장벽
        # bull_score가 50 아래로 무너지면(상승 종목 감소), 제곱의 속도로 허들이 높아짐
        _effective_bull_enter = self._bull_enter_threshold
        _breadth_penalty = 40.0 * (1.0 - min(1.0, bull_score / 50.0)) ** 2
        _effective_bull_enter += _breadth_penalty

        if _breadth_penalty > 0:
            logger.info(f"  🛡️ [Market Breadth] 내부 체력 약화 감지 (bull_score={bull_score:.1f}) -> Bull 진입 조건 상향 ({self._bull_enter_threshold} -> {_effective_bull_enter:.1f})")
        elif crash_score >= 60.0:
            _effective_bull_enter += 15.0
            logger.info(f"  🛡️ [Dynamic Hysteresis] 거시적 불안(crash={crash_score:.1f}) 감지 -> Bull 진입 조건 상향 ({self._bull_enter_threshold} -> {_effective_bull_enter})")
            
        is_bull = self._evaluate_hysteresis(self._current_state == "STATE_1_BULL", bull_score, _effective_bull_enter, self._bull_exit_threshold)

        # [V3 Maximalist] 트랩도어(Trap-Door) 룰: 가짜 V자 반등(Dead-Cat) 붕괴 방어
        trap_door_limit = float(market_data.get('v_recovery_low', -9999.0))
        current_kospi = float(market_data.get('kospi_close', 9999.0))
        if trap_door_limit > 0 and current_kospi < trap_door_limit:
            logger.critical(f"  🚨 [Trap-Door] V-Recovery 가짜 바닥 붕괴! (현재가 {current_kospi} < 방어선 {trap_door_limit}). 강제 CRASH 스위칭!")
            is_crash = True
            is_bull = False

        # 4. Absolute Hierarchy Routing
        # 서열: CRASH(3) > SHADOW(4) > BULL(1) > CAUTION(2)
        new_state = "STATE_2_CAUTION"
        allocations = {"S1": 1.0}  # Default State 2

        if market_data.get('crash_low_conf', False):
            new_state = "STATE_5_UNCERTAIN"
            allocations = {safe_asset: 1.0}
            logger.info(f"  🛡️ [VIX Smoothing] Crash 신호지만 신뢰도가 낮아 100% 대피({safe_asset})합니다.")
        elif is_crash:
            new_state = "STATE_3_CRASH"
            
            # [Phase 96: Alpha Preemption] CRASH 레짐에서도 VIX 폭등/GEX 악화 시 현금을 뱉어내 S1에게 사냥 권한 부여
            _s0_weight = 1.0
            if bool(self._cfg_get('chameleon.use_volatility_targeting', True)):
                _vix_proxy = float(market_data.get('signal_cache', {}).get('vix', 30.0)) if isinstance(market_data.get('signal_cache'), dict) else 30.0
                _gex_val = float(market_data.get('signal_cache', {}).get('macro_gex', 0.0)) if isinstance(market_data.get('signal_cache'), dict) and market_data.get('signal_cache', {}).get('macro_gex') is not None else 0.0
                
                # 찐 GEX 비중을 최고 우선순위로 격상: Negative Gamma가 심할수록 볼 타겟팅(현금비중 확대) 강하게 적용
                _gex_threshold = float(self._cfg_get('chameleon.gex_scavenger_threshold', -2e9))
                if _gex_val < _gex_threshold:
                    _vol_scale = max(0.0, 1.0 - ((_gex_threshold - _gex_val) / 3e9) ** 2)
                    _s0_weight = round(1.0 * _vol_scale, 2)
                    logger.info(f"  📉 [Vol Target] 찐 GEX({_gex_val:,.0f}) 극단적 마이너스! 인버스 비중 감쇄 (Scale: {_vol_scale:.2f}) -> 현금 스캐빈저 출동 대기")
                elif _vix_proxy >= 35.0: # 극단적 공포 (GEX 데이터가 없거나 임계치 도달 안했을때의 Fallback)
                    _vol_scale = max(0.0, 1.0 - ((_vix_proxy - 35.0) / 15.0) ** 2)
                    _s0_weight = round(1.0 * _vol_scale, 2)
                    logger.info(f"  📉 [Vol Target] CRASH VIX({_vix_proxy:.1f}) 폭등! 인버스 비중 감쇄 (Scale: {_vol_scale:.2f}) -> 현금 스캐빈저 출동 대기")

            _cash_weight = round(1.0 - _s0_weight, 2)
            allocations = {"S0": _s0_weight}
            if _cash_weight > 0.0:
                allocations[safe_asset] = _cash_weight
                allocations["S1_BUDGET_CAP"] = _cash_weight # S1 스캐빈저 예산 부여
        elif is_shadow:
            new_state = "STATE_4_SHADOW"
            allocations = {"S0": 1.0} # Inverse 100%
        elif is_bull:
            new_state = "STATE_1_BULL"
            # [Dual Kelly Competition] 야간 갭락 기댓값(Track B Edge) 산출
            _vix_raw = float(market_data.get('signal_cache', {}).get('vix', 15.0)) if isinstance(market_data.get('signal_cache'), dict) else 15.0
            _vix_ema = float(market_data.get('signal_cache', {}).get('vix_ema', _vix_raw)) if isinstance(market_data.get('signal_cache'), dict) else _vix_raw
            
            # [Red Team Point 1] Time-Lag Blindspot 방어: 미국 NQ 선물 충격 합성
            try:
                from src.data.market_data_bridge import MarketDataBridge
                _nq_data = MarketDataBridge().get_nq_futures_change()
                _nq_chg = float(_nq_data.get('chg_1d_pct', 0.0)) if _nq_data else 0.0
                if _nq_chg < -1.0:
                    _iv_shock = abs(_nq_chg) * 10.0 - 10.0 # -1% -> 0, -2% -> 10, -3% -> 20
                    _vix_raw = max(_vix_raw, _vix_raw + max(0.0, _iv_shock))
                    logger.warning(f"  🚨 [Red Team] 미국 NQ 선물 급락 감지({_nq_chg:.2f}%). 야간 VIX 합성 충격 적용: {_vix_raw:.1f}")
            except Exception as _nq_err:
                logger.error(f"  [Red Team] NQ 선물 합성 실패: {_nq_err}")

            # VIX 모멘텀(Skew 대용치) 계산: 오늘 VIX가 EMA보다 높게 튀어 오르면 야간 리스크 프리미엄 급증
            _skew_momentum = max(0.0, _vix_raw - _vix_ema)
            
            # 수학적 매핑: EMA가 임계값 이상이면 서서히 Edge가 발생하고, 모멘텀이 터지면 급증.
            _edge_threshold = float(self._cfg_get('chameleon.dual_kelly.vix_edge_threshold', 13.0))
            _base_multiplier = float(self._cfg_get('chameleon.dual_kelly.base_edge_multiplier', 0.015))
            _skew_weight = float(self._cfg_get('chameleon.dual_kelly.skew_momentum_weight', 0.05))
            _max_cap = float(self._cfg_get('chameleon.dual_kelly.track_b_max_cap', 0.35))
            
            _track_b_edge_score = max(0.0, (_vix_ema - _edge_threshold) * _base_multiplier) + (_skew_momentum * _skew_weight)
            _track_b_budget_cap = min(_max_cap, _track_b_edge_score)
            
            # [Macro Calendar Hard-Filter] 오늘 Tier-1 매크로 이벤트가 있다면 Track B 예산 강제 회수 (Fat Tail Risk 회피)
            try:
                from src.intelligence.event_calendar import EventCalendar
                from datetime import date
                _today_events = EventCalendar().get_events(target_date=date.today())
                _tier1_events = []
                _current_time = datetime.now().time()
                for e in _today_events:
                    if e.get('tier') == 1:
                        # BOJ 등 주요 금리 발표는 보통 15:20(Track B 진입시간) 이전에 종료됨
                        if "BOJ" in e.get('id', '') and _current_time.hour >= 14:
                            logger.info(f"  ✅ [Macro Filter] {e.get('name')} 이벤트 종료 추정 (14시 경과). 스나이퍼 전략 허용.")
                            continue
                        _tier1_events.append(e)
                if _tier1_events:
                    _event_names = ", ".join([e.get('name', 'Unknown') for e in _tier1_events])
                    logger.warning(f"  🚨 [Macro Filter] 오늘 Tier-1 매크로 이벤트({_event_names}) 예정. 스나이퍼 전략(Track B) 예산을 0으로 잠급니다.")
                    _track_b_budget_cap = 0.0
            except Exception as _cal_err:
                logger.error(f"  [Macro Filter] 캘린더 조회 실패 (무시하고 진행): {_cal_err}")
            
            if _track_b_budget_cap > 0.0:
                logger.info(f"  🎯 [Dual Kelly] 옵션 스큐(VIX {_vix_raw:.1f} vs EMA {_vix_ema:.1f}) 감지. Track B 예산 선점: {_track_b_budget_cap*100:.1f}%")

            # Track A 남은 파이 내에서 할당
            _available_for_track_a = 1.0 - _track_b_budget_cap
            
            # [Maximalist] 내부 현금화(Cash Drag) 로직 철폐 -> 기반 비중 100%
            _s10_base_weight = 1.0
            
            # [Bayesian Kelly] DynamicConfig의 자율 수학 모델 결과 직결 (Pass-through)
            _vol_scale = float(self._cfg_get('sizer.kelly_fraction', 1.0))
            if _vol_scale < 1.0:
                logger.info(f"  📉 [Autonomous Kelly] DynamicConfig 수학 모델에 의한 레버리지 동적 감쇄 적용 (Scale: {_vol_scale:.2f})")
                    
            _s10_weight = round(_s10_base_weight * _vol_scale * _available_for_track_a, 2)
            _cash_weight = round(1.0 - _s10_weight, 2) # Track B 예산 포함 전체 남은 현금
            
            allocations = {"S10": _s10_weight}
            if _cash_weight > 0.0:
                allocations[safe_asset] = _cash_weight
                allocations["S1_BUDGET_CAP"] = _cash_weight # S1 스캐빈저(Track B) 예산 부여
        
        if not is_bull or new_state != "STATE_1_BULL":
            self._bull_peak_bull_score = 0.0

        if new_state != self._current_state:
            logger.info(f"  🔄 [Chameleon] State 스위칭 발생: {self._current_state} -> {new_state}")
            self._current_state = new_state

        # [V3 Maximalist] Soft Shutdown 코드 블럭 완전 삭제

        return allocations

    def _evaluate_hysteresis(self, currently_active: bool, score: float, enter_threshold: float, exit_threshold: float) -> bool:
        """이력현상 밴드를 이용한 상태 유지/이탈 판독 (Flickering 방지)."""
        if currently_active:
            # 이미 활성 상태라면, 이탈 임계치 밑으로 떨어져야만 False 반환
            return score >= exit_threshold
        else:
            # 비활성 상태라면, 진입 임계치를 돌파해야만 True 반환
            return score >= enter_threshold

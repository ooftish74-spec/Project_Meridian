"""
Meridian — Smart Wallet: Volatility-Scaled Merton-Kelly Capital Allocator
===========================================================================
하드코딩 계단식 비율(90/10, 70/30 등)을 전면 폐기하고,
HMM 붕괴 확률(P_c) + VIX 페널티로 매 분마다 현금 비중을 연속 곡선으로 조절한다.

★ 핵심 수식 (Volatility-Scaled Merton-Kelly):
  Vol_Penalty = max(1.0, current_vix / ema_vix)
  f_long      = Base_Long × max(0.0, 1.0 - P_c - 0.5 × P_b) / Vol_Penalty
  target_cash = max(min_cash_ratio, 1.0 - f_long)

★ Graceful Degradation:
  - HMM/신호 실패 시 default_cash_ratio(50%)로 보수적 Fallback
  - 부동소수점 오차 방지: max(0.0, ...) Clamping 명시 적용

기존 MetaCapitalAllocator는 보존하되,
SmartWalletAllocator를 새로운 진입점으로 추가.
"""
import json
import logging
import numpy as np
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SIGNAL_CACHE = _PROJECT_ROOT / 'results' / 'signal_cache.json'

class SmartWalletAllocator:
    """HMM + VIX 기반 연속 방정식 자본 할당기.

    절대 `if regime == 'crash': ...` 같은 하드코딩 분기문 금지.
    f_long 수식 하나로 모든 레짐을 연속적으로 처리.
    """

    def __init__(self):
        self._cfg = DynamicConfig()

    def _get(self, key: str, default: Any=None) -> Any:
        return self._cfg.get(key, default)

    def compute_vol_penalty(self, signal_cache: Optional[Dict]=None) -> float:
        """변동성 페널티 산출.

        수식: Vol_Penalty = max(1.0, current_vix / ema_vix)

        ★ 설계 의도:
          - 변동성이 평상시(current_vix ≤ ema_vix)에는 페널티 1.0 유지 (중립)
          - 공포 급등 시(current_vix > ema_vix)에만 페널티 > 1.0 부여
          → 롱 비중을 기계적으로 추가로 깎아 방어적 전환

        Args:
            signal_cache: signal_cache.json 딕셔너리. None이면 파일에서 읽음.

        Returns:
            Vol_Penalty (float, 최소 1.0)
        """
        try:
            if signal_cache is None:
                signal_cache = self._load_signal_cache()
            vol_spot_key = self._get('smart_wallet.vol_spot_key', 'vkospi')
            vol_ema_key = self._get('smart_wallet.vol_ema_key', 'vkospi_ema')
            ema_fallback = float(self._get('smart_wallet.vol_ema_fallback', 15.0))
            current_vix = float(signal_cache.get(vol_spot_key, 0) or 0)
            ema_vix = float(signal_cache.get(vol_ema_key, 0) or 0)
            if current_vix <= 0:
                return 1.0
            if ema_vix <= 0:
                ema_vix = ema_fallback
            penalty = max(1.0, current_vix / ema_vix)
            logger.debug(f'  [SmartWallet] Vol_Penalty={penalty:.4f} ({vol_spot_key}={current_vix:.2f} / ema={ema_vix:.2f})')
            return round(penalty, 6)
        except Exception as e:
            logger.critical(f'  [SmartWallet] Vol_Penalty 계산 실패 → 1.0: {e}', exc_info=True)
            return 1.0

    def compute_f_long(self, p_crash: float, p_bear: float, vol_penalty: float) -> float:
        """동적 롱 비중 f_long 산출.

        수식:
          f_long = Base_Long × max(0.0, 1.0 - P_c - 0.5 × P_b) / Vol_Penalty

        ★ Clamping (부동소수점 오차 방지):
          분자 `1.0 - P_c - 0.5×P_b`는 이론상 [0,1] 범위지만,
          Python 부동소수점 연산의 미세 음수를 차단하기 위해 max(0.0, ...)으로 감쌈.
          → 절대 숏 포지션 유발 불가.

        Args:
            p_crash:     HMM 폭락 확률 (0~1)
            p_bear:      HMM 하락 확률 (0~1)
            vol_penalty: compute_vol_penalty() 결과 (≥ 1.0)

        Returns:
            f_long ∈ [0.0, base_long_ratio]
        """
        base_long = float(self._get('smart_wallet.base_long_ratio', 0.9))
        bear_half = float(self._get('smart_wallet.bear_half_weight', 0.5))
        numerator = max(0.0, 1.0 - p_crash - bear_half * p_bear)
        denom = max(1.0, vol_penalty)
        f_long = base_long * numerator / denom
        f_long = max(0.0, min(base_long, f_long))
        return round(f_long, 6)

    def compute_target_cash(self, p_crash: float, p_bear: float, vol_penalty: float) -> float:
        """최종 S5 현금 비중 산출.

        수식: target_cash = max(min_cash_ratio, 1.0 - f_long)

        Returns:
            target_cash_ratio ∈ [min_cash_ratio, 1.0]
        """
        min_cash = float(self._get('smart_wallet.min_cash_ratio', 0.1))
        f_long = self.compute_f_long(p_crash, p_bear, vol_penalty)
        target_cash = max(min_cash, 1.0 - f_long)
        return round(target_cash, 6)

    def allocate(self, market_data: Optional[Dict]=None, regime_probs: Optional[Dict[str, float]]=None, signal_cache: Optional[Dict]=None) -> Dict[str, Any]:
        """Smart Wallet 메인 할당 함수.

        Volatility-Scaled Merton-Kelly 방정식을 통해
        현금 비중과 현물 롱 비중을 연속적으로 산출한다.

        Args:
            market_data:   RegimeDetector.detect()에 전달할 시장 데이터.
                           None이면 Regime 계산을 건너뜀(probe 모드).
            regime_probs:  사전 계산된 {'normal', 'bear', 'crash'} 확률.
                           None이면 RegimeDetector로 직접 계산.
            signal_cache:  signal_cache.json 내용. None이면 파일에서 읽음.

        Returns:
            {
                'target_cash_ratio':  float,    # 현금 비중 [0,1]
                'target_long_ratio':  float,    # 현물 롱 비중 [0,1]
                'f_long':             float,    # 순수 f_long (클램핑 전)
                'vol_penalty':        float,    # 변동성 페널티
                'p_crash':            float,
                'p_bear':             float,
                'p_normal':           float,
                'regime_source':      str,      # 'detector'/'direct'/'fallback'
                'fallback_reason':    str,      # Fallback 발생 이유 (없으면 '')
            }
        """
        default_cash = float(self._get('smart_wallet.default_cash_ratio', 0.5))
        fallback_result = {'target_cash_ratio': default_cash, 'target_long_ratio': round(1.0 - default_cash, 6), 'f_long': round(1.0 - default_cash, 6), 'vol_penalty': 1.0, 'p_crash': 0.0, 'p_bear': 0.0, 'p_normal': 1.0, 'regime_source': 'fallback', 'fallback_reason': ''}
        try:
            if regime_probs is not None:
                probs = regime_probs
                regime_source = 'direct'
            elif market_data is not None:
                probs = self._get_regime_probs_from_detector(market_data)
                regime_source = 'detector'
            else:
                probs = self._get_regime_probs_from_cache(signal_cache)
                regime_source = 'cache'
        except Exception as e:
            logger.warning(f"Regime calculation failed, returning fallback: {e}")
            fallback_result['fallback_reason'] = str(e)
            return fallback_result
        p_crash = float(probs.get('crash', 0.0))
        p_bear = float(probs.get('bear', 0.0))
        p_normal = float(probs.get('normal', 1.0 - p_crash - p_bear))
        vol_penalty = self.compute_vol_penalty(signal_cache)
        f_long = self.compute_f_long(p_crash, p_bear, vol_penalty)
        target_cash = self.compute_target_cash(p_crash, p_bear, vol_penalty)
        target_long = round(1.0 - target_cash, 6)
        result = {'target_cash_ratio': target_cash, 'target_long_ratio': target_long, 'f_long': f_long, 'vol_penalty': vol_penalty, 'p_crash': round(p_crash, 4), 'p_bear': round(p_bear, 4), 'p_normal': round(p_normal, 4), 'regime_source': regime_source, 'fallback_reason': ''}
        logger.info(f'  💰 [SmartWallet] cash={target_cash:.1%}, long={target_long:.1%} | P_c={p_crash:.3f}, P_b={p_bear:.3f}, Vol×={vol_penalty:.3f} (src={regime_source})')
        return result


    def _get_regime_probs_from_detector(self, market_data: Dict) -> Dict[str, float]:
        """RegimeDetector를 통해 실시간 확률 계산."""
        try:
            from src.regime.regime_detector import RegimeDetector
            detector = RegimeDetector()
            return detector.get_regime_probabilities(market_data)
        except Exception as e:
            logger.warning(f'  [SmartWallet] RegimeDetector 실패 → 보수적 Fallback: {e}')
            return {'normal': float(self._get('smart_wallet.fallback_normal', 0.3)), 'bear': float(self._get('smart_wallet.fallback_bear', 0.3)), 'crash': float(self._get('smart_wallet.fallback_crash', 0.4))}

    def _get_regime_probs_from_cache(self, signal_cache: Optional[Dict]=None) -> Dict[str, float]:
        """signal_cache.json에서 기존 확률 읽기 (probe 모드)."""
        fb_normal = float(self._get('smart_wallet.cache_fallback_normal', 0.5))
        fb_bear = float(self._get('smart_wallet.cache_fallback_bear', 0.3))
        fb_crash = float(self._get('smart_wallet.cache_fallback_crash', 0.2))
        fallback = {'normal': fb_normal, 'bear': fb_bear, 'crash': fb_crash}
        cache = signal_cache or self._load_signal_cache()
        stored = cache.get('regime_probabilities', {})
        if stored and 'crash' in stored:
            return {'normal': float(stored.get('normal', stored.get('bull', fb_normal))), 'bear': float(stored.get('bear', fb_bear)), 'crash': float(stored.get('crash', fb_crash))}

        return fallback

    def _load_signal_cache(self) -> Dict:
        """signal_cache.json 로드."""
        if _SIGNAL_CACHE.exists():
            return json.loads(_SIGNAL_CACHE.read_text())

        return {}
try:
    import numpy as _np
    from src.allocation.virtual_account_manager import VirtualAccountManager as _VAM
    _VAM_AVAILABLE = True
except ImportError as e:
    _VAM_AVAILABLE = False

class MetaCapitalAllocator:
    """기존 Edge + Kelly 기반 할당기 (하위 호환 보존).

    신규 코드는 SmartWalletAllocator를 사용하라.
    """

    def __init__(self, total_capital: float=200000000.0):
        if _VAM_AVAILABLE:
            self.virtual_manager = _VAM(total_master_capital=total_capital)
        else:
            self.virtual_manager = None
            self._total_capital = total_capital
        self.cfg = DynamicConfig()

    def _get_regime_multiplier(self, stream_id: str) -> float:
        try:
            if _SIGNAL_CACHE.exists():
                cache = json.loads(_SIGNAL_CACHE.read_text())
                probs = cache.get('regime_probabilities', {})
                bull_prob = probs.get('bull', 0.5)
                bear_prob = probs.get('bear', 0.2)
                crash_prob = probs.get('crash', 0.1)
                bull_mult = self.cfg.get(f'allocation.multiplier.bull.{stream_id}', 1.0)
                bear_mult = self.cfg.get(f'allocation.multiplier.bear.{stream_id}', 0.5)
                crash_mult = self.cfg.get(f'allocation.multiplier.crash.{stream_id}', 0.0)
                return bull_prob * bull_mult + bear_prob * bear_mult + crash_prob * crash_mult
        except Exception as e:
            logger.critical(f'Failed to read regime state for multiplier: {e}', exc_info=True)
        return self.cfg.get(f'allocation.multiplier.default.{stream_id}', 1.0)

    def calculate_allocations(self, stream_metrics: Dict[str, Dict[str, float]], covariance_matrix: _np.ndarray, tax_reserve_buffer: float=0.0) -> Dict[str, float]:
        """스트림별 Edge + Half-Kelly 기반 배분액 계산.
        
        Args:
            stream_metrics: 각 스트림의 성과 지표
            covariance_matrix: 공분산 행렬
            tax_reserve_buffer: ETF(S0, S5) 배당소득세(15.4%) 원천징수 대비용 현금 락업(Lock-up) 금액.
                                이 금액은 베팅 사이즈 계산 시 총 예수금에서 차감되어 과배팅(Cash Drag)을 방지함.
        """
        logger.info(f'Meta-Level Capital Allocator: 엣지 기반 자본 배분 계산 시작 (Tax Reserve: {tax_reserve_buffer:,.0f} KRW)')
        kelly_fractions: Dict[str, float] = {}
        for stream_id, metrics in stream_metrics.items():
            edge = metrics.get('edge', 0.0)
            if edge <= 0:
                kelly_fractions[stream_id] = 0.0
            else:
                kelly = edge * 0.5
                kelly_fractions[stream_id] = min(max(kelly, 0.0), 0.3)
        raw_total_capital = self.virtual_manager.total_master_capital if self.virtual_manager else self._total_capital
        available_capital = max(0.0, raw_total_capital - tax_reserve_buffer)
        target_allocations: Dict[str, float] = {}
        for stream_id, k_frac in kelly_fractions.items():
            regime_mult = self._get_regime_multiplier(stream_id)
            target_allocations[stream_id] = available_capital * k_frac * regime_mult
        return target_allocations

    def reallocate(self, stream_metrics: Dict[str, Dict[str, float]], covariance_matrix: _np.ndarray, tax_reserve_buffer: float=0.0) -> Dict[str, float]:
        """산출된 배분액을 가상 장부에 반영."""
        allocations = self.calculate_allocations(stream_metrics, covariance_matrix, tax_reserve_buffer)
        if self.virtual_manager:
            self.virtual_manager.allocate_capital(allocations)
        return allocations
"""
AdaptiveThreshold — S1 진입 임계값 동적 조정 엔진
=====================================================

기존 하드코딩된 임계값의 문제:
  - gap_min_us_change_pct = 0.5%  → 40%+ 거래일 무신호
  - ois_long_threshold = 0.65     → OIS 정규분포 기준 상위 16%만 통과
  - ois_short_threshold = 0.35    → 하위 16%만 통과
  → 전체 거래일의 68%가 "중립"으로 미거래

해결: 3가지 수학적 메커니즘으로 동적 조정

1. Rolling Percentile (분위수 기반)
   - 과거 N일간 신호 분포의 P_lo ~ P_hi 구간을 "중립"으로 정의
   - 시장 특성 변화에 자동 적응

2. Volatility Scaling (변동성 연동)
   - VIX ↑ → 밴드 확대 (노이즈 필터링 강화)
   - VIX ↓ → 밴드 축소 (약한 신호도 포착)
   - 공식: threshold *= (vix / vix_baseline) ^ elasticity

3. Regime Asymmetry (레짐별 비대칭)
   - Bull: long 진입 완화, short 진입 강화 (추세 순응)
   - Bear: short 진입 완화, long 진입 강화 (추세 순응)
   - Crash: 인버스 진입 완화, 레버리지 진입 강화

Usage:
    from src.streams.s1_edge.adaptive_threshold import AdaptiveThreshold
    at = AdaptiveThreshold()
    thresholds = at.compute(vix=16.5, regime='caution', signal_history=ois_history)

Academic References:
    - Bollerslev (1986): GARCH — 조건부 변동성
    - Mandelbrot (1963): Fat tails — 정규분포 가정의 한계
    - Ang & Chen (2002): Asymmetric correlations — 비대칭 상관
"""
import json
import logging
import math
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_RESULTS = _PROJECT_ROOT / 'results'
_DATA_DIR = _PROJECT_ROOT / 'data' / 'historical_10y'
_STATE_FILE = _RESULTS / 'adaptive_threshold_state.json'

class AdaptiveThreshold:
    """S1 진입 임계값 동적 조정 엔진.

    모든 임계값은 수학적으로 계산되며, 하드코딩 값은 fallback으로만 사용.

    Attributes:
        _vix_baseline: VIX 기준값 (장기 평균 ~18)
        _vix_elasticity: VIX 변화에 대한 밴드 탄력성 (0~1)
        _percentile_window: Rolling 분위수 계산 윈도우 (일)
        _signal_history: OIS/US변동률 히스토리 (in-memory ring buffer)
    """
    PERCENTILE_WINDOW = 60
    SENTIMENT_WINDOW = 252
    LONG_PERCENTILE = 55
    SHORT_PERCENTILE = 45
    PCR_EXTREME_PERCENTILE = 95
    VIX_ASSURANCE_PERCENTILE = 90
    SENTIMENT_RISK_PERCENTILE = 20
    SENTIMENT_EXTREME_PERCENTILE = 5
    GAP_PERCENTILE = 20
    OIS_LONG_FLOOR = 0.5
    OIS_LONG_CEILING = 0.75
    OIS_SHORT_FLOOR = 0.25
    OIS_SHORT_CEILING = 0.5
    GAP_CEILING = 1.5
    MIN_NEUTRAL_BAND = 0.04
    PCR_FLOOR = 1.2
    PCR_CEILING = 2.0
    VIX_ASSURANCE_FLOOR = 20.0
    VIX_ASSURANCE_CEILING = 35.0
    SENTIMENT_RISK_FLOOR = -0.4
    SENTIMENT_RISK_CEILING = -0.1
    SENTIMENT_EXTREME_FLOOR = -0.5
    SENTIMENT_EXTREME_CEILING = -0.2

    def __init__(self):
        self._ois_history: List[float] = []
        self._us_change_history: List[float] = []
        self._vix_history: List[float] = []
        self._pcr_history: List[float] = []
        self._sentiment_history: List[float] = []
        self._last_computed: Optional[Dict] = None
        self._load_state()

    def compute(self, vix: float=18.0, regime: str='caution', ois_current: float=50.0, us_change_current: float=0.0, pcr_current: float=1.0, sentiment_current: float=0.0, regime_strength: float=0.5) -> Dict:
        """모든 S1 임계값을 동적 계산.

        Args:
            vix: 현재 VIX 값
            regime: 현재 레짐 ('bull', 'caution', 'bear', 'crash')
            ois_current: 현재 OIS 값 (0~100)
            us_change_current: 현재 US 시장 변동률 (%)

        Returns:
            {
                'gap_min_us_change_pct': float,   # Gap 진입 최소 변동률
                'ois_long_threshold': float,       # OIS long 진입 임계값 (0~1)
                'ois_short_threshold': float,      # OIS short 진입 임계값 (0~1)
                'single_stock_min_score': float,   # 단일종목 레버리지 최소 스코어
                'single_stock_max_inv_score': float,  # 인버스 최대 스코어

                # 메타 정보
                'method': str,
                'vix_scale_factor': float,
                'regime_adjustments': dict,
                'confidence': str,  # 'high' / 'medium' / 'low'
            }
        """
        self._update_history(ois_current, us_change_current, vix, pcr_current, sentiment_current)
        vix_scale = self._compute_vix_scale(vix)
        ois_thresholds = self._compute_ois_thresholds(vix_scale)
        gap_threshold = self._compute_gap_threshold(vix_scale)
        pcr_extreme_th = self._compute_pcr_threshold(vix_scale)
        vix_assurance_th = self._compute_vix_assurance_threshold()
        sentiment_thresholds = self._compute_sentiment_thresholds()
        regime_cfg = _cfg.get(f'adaptive_threshold.regime_asymmetry.{regime}', {})
        if not regime_cfg:
            regime_cfg = _cfg.get('adaptive_threshold.regime_asymmetry.caution', {})
        base_long_shift = float(regime_cfg.get('base_long_shift', 0.0))
        base_short_shift = float(regime_cfg.get('base_short_shift', 0.0))
        tanh_scale = math.tanh(regime_strength * math.pi)
        ois_long = ois_thresholds['long'] + base_long_shift * tanh_scale
        ois_short = ois_thresholds['short'] + base_short_shift * tanh_scale
        ois_long = max(self.OIS_LONG_FLOOR, min(self.OIS_LONG_CEILING, ois_long))
        ois_short = max(self.OIS_SHORT_FLOOR, min(self.OIS_SHORT_CEILING, ois_short))
        min_friction = _cfg.get('adaptive_threshold.min_friction_cost', 0.1)
        gap_threshold = max(min_friction, min(self.GAP_CEILING, gap_threshold))
        band_width = ois_long - ois_short
        if band_width < self.MIN_NEUTRAL_BAND:
            deficit = (self.MIN_NEUTRAL_BAND - band_width) / 2.0
            ois_long = min(self.OIS_LONG_CEILING, ois_long + deficit)
            ois_short = max(self.OIS_SHORT_FLOOR, ois_short - deficit)
        kelly_shrinkage = float(_cfg.get('adaptive_threshold.single_stock.kelly_shrinkage', 0.5))
        beta = float(_cfg.get('adaptive_threshold.single_stock.beta_floor', 0.8))
        idio_vol = vix_scale * (1.0 - 1.0 / (1.0 + beta))
        single_min = max(0.52, min(0.68, 0.5 + kelly_shrinkage * idio_vol))
        single_max_inv = max(0.25, min(0.45, 0.5 - kelly_shrinkage * idio_vol))
        regime_adj = {'base_long_shift': base_long_shift, 'base_short_shift': base_short_shift, 'tanh_scale': round(tanh_scale, 4), 'long_shift': round(base_long_shift * tanh_scale, 4), 'short_shift': round(base_short_shift * tanh_scale, 4)}
        n_ois = len(self._ois_history)
        if n_ois >= self.PERCENTILE_WINDOW:
            confidence = 'high'
        elif n_ois >= 20:
            confidence = 'medium'
        else:
            confidence = 'low'
        result = {'gap_min_us_change_pct': round(gap_threshold, 3), 'ois_long_threshold': round(ois_long, 3), 'ois_short_threshold': round(ois_short, 3), 'single_stock_min_score': round(single_min, 3), 'single_stock_max_inv_score': round(single_max_inv, 3), 'pcr_extreme_threshold': round(pcr_extreme_th, 3), 'vix_assurance_threshold': round(vix_assurance_th, 3), 'sentiment_risk_threshold': round(sentiment_thresholds['risk'], 3), 'sentiment_extreme_threshold': round(sentiment_thresholds['extreme'], 3), 'method': 'adaptive_v1', 'vix_scale_factor': round(vix_scale, 3), 'regime': regime, 'regime_adjustments': regime_adj, 'confidence': confidence, 'history_depth': n_ois, 'neutral_band_width': round(ois_long - ois_short, 3), 'timestamp': datetime.now().isoformat()}
        self._last_computed = result
        self._save_state()
        logger.info(f'  🎯 AdaptiveThreshold: gap={gap_threshold:.3f}%, OIS=[{ois_short:.3f}, {ois_long:.3f}], VIX_scale={vix_scale:.2f}, conf={confidence}')
        return result

    def get_last(self) -> Optional[Dict]:
        """마지막 계산 결과."""
        return self._last_computed

    def _compute_vix_scale(self, vix: float) -> float:
        """Model 1: Gamma-Aware Asymmetric Log VIX Scaling.

        공식: scale = 1.0 + max(0, log(vix / vix_baseline)) * elasticity

        특성:
          - VIX < vix_baseline: max(0, negative) = 0 → scale = 1.0 (하방 자동 클램핑)
          - VIX = vix_baseline(18): scale = 1.0 (기준)
          - VIX = 25: scale = 1.0 + log(25/18)*0.5 ≈ 1.168
          - VIX = 36 (2×baseline): scale = 1.0 + log(2)*0.5 ≈ 1.347
          → 옵션 MM 감마 플립선 이하에서 중립 유지, 이상에서만 로그 비례 확대

        Config:
          adaptive_threshold.vix_baseline        (default 18.0)
          adaptive_threshold.vix_log_elasticity  (default 0.5)
        """
        if vix <= 0:
            return 1.0
        vix_baseline = float(_cfg.get('adaptive_threshold.vix_baseline', 18.0))
        elasticity = float(_cfg.get('adaptive_threshold.vix_log_elasticity', 0.5))
        log_ratio = math.log(vix / vix_baseline)
        raw_scale = 1.0 + max(0.0, log_ratio) * elasticity
        return max(1.0, raw_scale)

    def _compute_ois_thresholds(self, vix_scale: float) -> Dict[str, float]:
        """OIS 진입 임계값 계산.

        데이터 충분 시: Rolling 분위수 기반
        데이터 부족 시: Z-score 기반 (정규분포 가정)

        기본 논리:
          - OIS가 상위 P_long 이상이면 long
          - OIS가 하위 P_short 이하이면 short
          - VIX 스케일링으로 밴드 폭 조정
        """
        n = len(self._ois_history)
        if n >= 20:
            window = self._ois_history[-self.PERCENTILE_WINDOW:]
            sorted_vals = sorted(window)
            n_w = len(sorted_vals)
            long_idx = int(n_w * self.LONG_PERCENTILE / 100)
            short_idx = int(n_w * self.SHORT_PERCENTILE / 100)
            long_idx = min(long_idx, n_w - 1)
            short_idx = min(short_idx, n_w - 1)
            raw_long = sorted_vals[long_idx] / 100.0
            raw_short = sorted_vals[short_idx] / 100.0
        else:
            raw_long = 0.5 + 0.44 * 0.15
            raw_short = 0.5 - 0.44 * 0.15
        center = 0.5
        long_band = (raw_long - center) * vix_scale
        short_band = (center - raw_short) * vix_scale
        return {'long': center + long_band, 'short': center - short_band, 'method': 'rolling_percentile' if n >= 20 else 'z_score'}

    def _compute_gap_threshold(self, vix_scale: float) -> float:
        """Model 2: Friction-Bound Volatility Gap Threshold.

        최소 마찰 비용(Slippage + Commission)을 하한선으로 두는 모델:
          gap_threshold = max(min_friction_cost, atr_20 * z_target)

        - min_friction_cost: 체결 비용 이하에서는 진입 의미 없음 (수익 불가)
        - atr_20: |US 변동률| rolling 20일 실현 변동성 (ATR 근사)
        - z_target: GAP_PERCENTILE 기반 분위수 (일반 시장 노이즈 필터)

        데이터 부족 시 fallback:
          VIX ≈ σ_annual% → σ_daily = VIX / √252, threshold = 0.5 × σ_daily

        Config:
          adaptive_threshold.min_friction_cost  (default 0.10)
        """
        n = len(self._us_change_history)
        min_friction = float(_cfg.get('adaptive_threshold.min_friction_cost', 0.1))
        if n >= 20:
            window = self._us_change_history[-self.PERCENTILE_WINDOW:]
            abs_changes = sorted((abs(x) for x in window))
            idx = min(int(len(abs_changes) * self.GAP_PERCENTILE / 100), len(abs_changes) - 1)
            atr_20 = abs_changes[idx]
        else:
            vix_latest = self._vix_history[-1] if self._vix_history else 18.0
            sigma_daily = vix_latest / 252 ** 0.5
            atr_20 = 0.5 * sigma_daily
        return max(min_friction, atr_20 * vix_scale)

    def _compute_pcr_threshold(self, vix_scale: float) -> float:
        """PCR 패닉 임계값 동적 계산 (과거 60일 P95)."""
        n = len(self._pcr_history)
        if n >= 20:
            window = self._pcr_history[-self.PERCENTILE_WINDOW:]
            sorted_vals = sorted(window)
            idx = int(len(sorted_vals) * self.PCR_EXTREME_PERCENTILE / 100)
            idx = min(idx, len(sorted_vals) - 1)
            raw = sorted_vals[idx]
        else:
            raw = 1.5
        adjusted = raw * ((vix_scale - 1) * 0.3 + 1)
        return max(self.PCR_FLOOR, min(self.PCR_CEILING, adjusted))

    def _compute_vix_assurance_threshold(self) -> float:
        """VIX 동반 폭등(Assurance) 임계값 동적 계산 (과거 60일 P90)."""
        n = len(self._vix_history)
        if n >= 20:
            window = self._vix_history[-self.PERCENTILE_WINDOW:]
            sorted_vals = sorted(window)
            idx = int(len(sorted_vals) * self.VIX_ASSURANCE_PERCENTILE / 100)
            idx = min(idx, len(sorted_vals) - 1)
            raw = sorted_vals[idx]
        else:
            raw = 25.0
        return max(self.VIX_ASSURANCE_FLOOR, min(self.VIX_ASSURANCE_CEILING, raw))

    def _compute_sentiment_thresholds(self) -> Dict[str, float]:
        """매크로 센티먼트 임계값 계산 (과거 90일 분위수)."""
        n = len(self._sentiment_history)
        if n >= 30:
            window = self._sentiment_history[-self.SENTIMENT_WINDOW:]
            sorted_vals = sorted(window)
            risk_idx = int(len(sorted_vals) * self.SENTIMENT_RISK_PERCENTILE / 100)
            ext_idx = int(len(sorted_vals) * self.SENTIMENT_EXTREME_PERCENTILE / 100)
            raw_risk = sorted_vals[min(risk_idx, len(sorted_vals) - 1)]
            raw_ext = sorted_vals[min(ext_idx, len(sorted_vals) - 1)]
        else:
            raw_risk = -0.15
            raw_ext = -0.3
        return {'risk': max(self.SENTIMENT_RISK_FLOOR, min(self.SENTIMENT_RISK_CEILING, raw_risk)), 'extreme': max(self.SENTIMENT_EXTREME_FLOOR, min(self.SENTIMENT_EXTREME_CEILING, raw_ext))}

    def _update_history(self, ois: float, us_change: float, vix: float, pcr: float, sentiment: float):
        """신호 히스토리 업데이트 (ring buffer).

        유효하지 않은 입력은 직전 값 또는 중립 기본값으로 대체하여 항상 동일 길이를 유지.
        """
        max_len = self.PERCENTILE_WINDOW * 2
        max_sentiment_len = self.SENTIMENT_WINDOW * 2
        if ois is None or not 0 <= ois <= 100:
            ois = self._ois_history[-1] if self._ois_history else 50.0
        if us_change is None:
            us_change = self._us_change_history[-1] if self._us_change_history else 0.0
        if vix is None or vix <= 0:
            vix = self._vix_history[-1] if self._vix_history else self.VIX_BASELINE
        if pcr is None or pcr <= 0:
            pcr = self._pcr_history[-1] if self._pcr_history else 1.0
        if sentiment is None:
            sentiment = self._sentiment_history[-1] if self._sentiment_history else 0.0
        self._ois_history.append(ois)
        self._us_change_history.append(us_change)
        self._vix_history.append(vix)
        self._pcr_history.append(pcr)
        self._sentiment_history.append(sentiment)
        if len(self._ois_history) > max_len:
            self._ois_history = self._ois_history[-max_len:]
        if len(self._us_change_history) > max_len:
            self._us_change_history = self._us_change_history[-max_len:]
        if len(self._vix_history) > max_len:
            self._vix_history = self._vix_history[-max_len:]
        if len(self._pcr_history) > max_len:
            self._pcr_history = self._pcr_history[-max_len:]
        if len(self._sentiment_history) > max_sentiment_len:
            self._sentiment_history = self._sentiment_history[-max_sentiment_len:]

    def bootstrap_from_historical(self):
        """10년 히스토리에서 초기 분포를 부트스트랩.

        최근 60일치 US 변동률과 VIX를 로드하여
        첫 계산부터 confidence='high'를 달성.
        """
        try:
            import pandas as pd
            import numpy as np
            _SIG_DIR = _DATA_DIR.parent / 'signals'
            for sp_path in [_SIG_DIR / 'signal_sp500.parquet', _DATA_DIR / 'us_sp500.parquet', _DATA_DIR / 'cross_sp500.parquet']:
                if sp_path.exists():
                    df = pd.read_parquet(sp_path).reset_index()
                    df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
                    if hasattr(df.columns, 'levels'):
                        df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df.columns]
                    if 'close' in df.columns:
                        close = pd.to_numeric(df['close'], errors='coerce').dropna()
                        returns = close.pct_change().dropna() * 100
                        recent = returns.tail(self.PERCENTILE_WINDOW).tolist()
                        self._us_change_history = recent
                        logger.info(f'  📊 Bootstrap US returns: {len(recent)}일 ({sp_path.name})')
                        break
            for vix_path in [_SIG_DIR / 'signal_vix.parquet', _DATA_DIR / 'us_vix.parquet', _DATA_DIR / 'cross_vix.parquet']:
                if vix_path.exists():
                    df = pd.read_parquet(vix_path).reset_index()
                    df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
                    if hasattr(df.columns, 'levels'):
                        df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df.columns]
                    if 'close' in df.columns:
                        vix = pd.to_numeric(df['close'], errors='coerce').dropna()
                        recent = vix.tail(self.PERCENTILE_WINDOW).tolist()
                        self._vix_history = recent
                        logger.info(f'  📊 Bootstrap VIX: {len(recent)}일 ({vix_path.name})')
                        break
            if self._us_change_history:
                arr = self._us_change_history
                mu = sum(arr) / len(arr)
                var = sum(((x - mu) ** 2 for x in arr)) / len(arr)
                std = var ** 0.5 if var > 0 else 1.0
                self._ois_history = [max(0, min(100, 50 + x / std * 15)) for x in arr]
                logger.info(f'  📊 Bootstrap OIS (synthetic): {len(self._ois_history)}일')
        except Exception as e:
            logger.warning(f'  Bootstrap 실패: {e}')

    def _save_state(self):
        """상태 영속화."""
        try:
            state = {'ois_history': self._ois_history[-120:], 'us_change_history': self._us_change_history[-120:], 'vix_history': self._vix_history[-120:], 'pcr_history': self._pcr_history[-120:], 'sentiment_history': self._sentiment_history[-504:], 'last_computed': self._last_computed, 'updated': datetime.now().isoformat()}
            atomic_write_json(_STATE_FILE, state, indent=2)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  State save failed: {e}')

    def _load_state(self):
        """저장된 상태 복원."""
        if not _STATE_FILE.exists():
            return
        try:
            state = json.loads(_STATE_FILE.read_text())
            self._ois_history = state.get('ois_history', [])
            self._us_change_history = state.get('us_change_history', [])
            self._vix_history = state.get('vix_history', [])
            self._pcr_history = state.get('pcr_history', [])
            self._sentiment_history = state.get('sentiment_history', [])
            self._last_computed = state.get('last_computed')
            logger.debug(f'  State restored: {len(self._ois_history)} OIS, {len(self._pcr_history)} PCR, {len(self._sentiment_history)} Sentiment')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  State load failed: {e}')
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    at = AdaptiveThreshold()
    at.bootstrap_from_historical()
    logger.info('\n' + '=' * 60)
    logger.info('AdaptiveThreshold — 시나리오별 임계값')
    logger.info('=' * 60)
    scenarios = [('Low VIX + Bull', 12.0, 'bull'), ('Normal VIX + Caution', 18.0, 'caution'), ('High VIX + Bear', 28.0, 'bear'), ('Spike VIX + Crash', 40.0, 'crash')]
    for name, vix, regime in scenarios:
        result = at.compute(vix=vix, regime=regime, ois_current=50, us_change_current=0.5)
        logger.info(f'\n── {name} (VIX={vix}, {regime}) ──')
        logger.info(f'  Gap threshold:  {result['gap_min_us_change_pct']:.3f}%  (fixed: 0.500%)')
        logger.info(f'  OIS long:       {result['ois_long_threshold']:.3f}  (fixed: 0.650)')
        logger.info(f'  OIS short:      {result['ois_short_threshold']:.3f}  (fixed: 0.350)')
        logger.info(f'  Neutral band:   {result['neutral_band_width']:.3f}  (fixed: 0.300)')
        logger.info(f'  Single min:     {result['single_stock_min_score']:.3f}  (fixed: 0.600)')
        logger.info(f'  VIX scale:      {result['vix_scale_factor']:.3f}')
        logger.info(f'  Confidence:     {result['confidence']}')
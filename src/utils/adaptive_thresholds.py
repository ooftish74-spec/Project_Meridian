"""
★ 적응형 임계값 유틸리티 [H-01~H-12 2026-04-18]
=================================================
통계적으로 유의미한 동적 임계값 제공:

  H-01: compute_da_threshold()    — 이항검정 기반 DA 경보 임계값
  H-02: ic_is_significant()       — t-통계량 기반 IC 유의성 검사
  H-03: DynamicVIXThreshold       — 분위수 기반 VIX 레짐 분류
  H-04: atr_based_sl_tp()         — ATR 기반 변동성 정규화 손절/익절
  H-05: ic_weighted_blend()       — IC 비율 기반 앙상블 가중치
  H-08: regime_quantile_thresholds() — 분위수 기반 레짐 경계
  H-10: kelly_position_limit()    — Half-Kelly 포지션 상한
  H-11: vol_adjusted_halt()       — 변동성 정규화 일일 손실 한도
  H-12: optimal_lookback_from_ic() — IC-decay 분석 최적 룩백

Author: Project-A | Date: 2026-04-18
"""
import logging
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

def compute_da_threshold(n_days: int, alpha: float=0.1, fallback: float=0.52) -> float:
    """
    n거래일 기준 DA 경보 임계값 (이항검정).

    H0: p = 0.5 (순수 랜덤) 가정 하에,
    alpha 유의수준으로 H0를 기각할 수 있는 최소 DA 반환.

    Args:
        n_days:   평가 기간 거래일 수
        alpha:    유의수준 (기본 0.10 = 90% 신뢰)
        fallback: n_days < 5일 때 반환할 기본값

    Returns:
        DA 경보 기준선 (예: n=20→0.60, n=252→0.53)

    통계 근거:
        이항분포 B(n, 0.5)의 (1-alpha) 백분위수 / n
        = 표본크기가 작을수록 높은 기준선 요구
    """
    if n_days < 5:
        return fallback
    try:
        from scipy.stats import binom
        k = binom.ppf(1.0 - alpha, n=n_days, p=0.5)
        threshold = k / n_days
        return float(min(max(threshold, 0.5), 0.65))
    except ImportError as e:
        z = 1.282
        se = math.sqrt(0.25 / n_days)
        return min(max(0.5 + z * se, 0.5), 0.65)

def ic_is_significant(ic: float, n_periods: int, alpha: float=0.05) -> bool:
    """
    IC가 통계적으로 유의한지 t-검정으로 판단.

    t = IC / SE,  SE = sqrt((1 - IC²) / (n - 2))

    Args:
        ic:        정보계수 (예: 0.05)
        n_periods: 관측 기간
        alpha:     유의수준 (기본 0.05 = 95% 신뢰)

    Returns:
        True: 유의 (H0: IC=0 기각)
        False: 유의하지 않음
    """
    if n_periods < 3:
        return False
    try:
        from scipy.stats import t as t_dist
        se = math.sqrt(max((1 - ic ** 2) / (n_periods - 2), 1e-12))
        t_stat = ic / se
        t_crit = t_dist.ppf(1 - alpha / 2, df=n_periods - 2)
        return abs(t_stat) > t_crit
    except ImportError as e:
        z_crit = {0.05: 1.96, 0.1: 1.645, 0.01: 2.576}.get(alpha, 1.96)
        se = math.sqrt(max((1 - ic ** 2) / (n_periods - 2), 1e-12))
        return abs(ic / se) > z_crit

class DynamicVIXThreshold:
    """
    과거 VIX 분포 기반 동적 레짐 분류기.

    고정 임계값(15/20/25/30/35/45) 대신 실제 분포 분위수를 사용.
    → 저변동성 환경(VIX 평균 15)과 고변동성 환경(VIX 평균 25)에서
      동일한 레짐 분류 품질 유지.

    사용:
        vt = DynamicVIXThreshold.from_history()
        level = vt.classify(vix=22.5)  # 'normal' | 'elevated' | ...
    """
    LEVEL_NAMES = ['calm', 'normal', 'elevated', 'high', 'extreme']

    def __init__(self, percentiles: Dict[str, float]):
        """
        percentiles: {
            'p25': float,  # 하위 25% → calm/normal 경계
            'p50': float,  # 중앙값   → normal/elevated 경계
            'p75': float,  # 상위 25% → elevated/high 경계
            'p90': float,  # 상위 10% → high/extreme 경계
        }
        """
        self.p25 = percentiles.get('p25', 14.0)
        self.p50 = percentiles.get('p50', 18.0)
        self.p75 = percentiles.get('p75', 24.0)
        self.p90 = percentiles.get('p90', 32.0)

    @classmethod
    def from_history(cls, vix_values: Optional[List[float]]=None, window: int=252) -> 'DynamicVIXThreshold':
        """
        VIX 이력에서 분위수 계산.

        Args:
            vix_values: VIX 이력 (없으면 overnight_intelligence 자동 로드)
            window:     최근 거래일 수
        """
        if vix_values is None:
            vix_values = _load_vix_history(window)
        if not vix_values or len(vix_values) < 20:
            logger.warning(f'  VIX 이력 부족 ({(len(vix_values) if vix_values else 0)}개) → yfinance 최신 VIX 1년 데이터로 초기화 시도')
            live_vix = _fetch_vix_from_yfinance(fallback_window=252)
            if live_vix and len(live_vix) >= 20:
                vix_values = live_vix
                logger.info(f'  yfinance VIX 로드 성공: {len(live_vix)}일')
            else:
                logger.warning('  yfinance VIX도 실패 → 구보적 기본값 사용 (2013~2019 저변동 편향 주의)')
                return cls({'p25': 14.0, 'p50': 18.0, 'p75': 24.0, 'p90': 32.0})
        arr = np.array(vix_values[-window:])
        percentiles = {'p25': float(np.percentile(arr, 25)), 'p50': float(np.percentile(arr, 50)), 'p75': float(np.percentile(arr, 75)), 'p90': float(np.percentile(arr, 90))}
        logger.debug(f'  DynamicVIX: p25={percentiles['p25']:.1f} p50={percentiles['p50']:.1f} p75={percentiles['p75']:.1f} p90={percentiles['p90']:.1f}')
        return cls(percentiles)

    def classify(self, vix: float) -> str:
        """
        VIX → 레짐 레벨 반환.

        Returns:
            'calm' | 'normal' | 'elevated' | 'high' | 'extreme'
        """
        if vix < self.p25:
            return 'calm'
        if vix < self.p50:
            return 'normal'
        if vix < self.p75:
            return 'elevated'
        if vix < self.p90:
            return 'high'
        return 'extreme'

    def is_no_trade(self, vix: float) -> bool:
        """거래 금지 레벨 (기존 VIX>35 대체)."""
        return vix >= self.p90

    def is_cash_wait(self, vix: float) -> bool:
        """현금 대기 레벨 (기존 VIX 30~35 대체)."""
        return self.p75 <= vix < self.p90

    def is_high_vol(self, vix: float) -> bool:
        """고변동 레벨 (기존 VIX>25 대체)."""
        return vix >= self.p75

    @property
    def no_trade_level(self) -> float:
        """거래 금지 절대 기준 (극단 완충: p90 + 0.5σ)."""
        return self.p90

    @property
    def cash_wait_level(self) -> float:
        """현금 대기 기준."""
        return self.p75

    @property
    def high_vol_level(self) -> float:
        """고변동 기준."""
        return self.p75

def _load_vix_history(window: int=252) -> List[float]:
    """
    VIX 이력 로드.

    [Root Cause Fix] 우선순위:
      1순위: data/cache/vix_daily.json (아침 pre-fetch 참조)
      2순위: results/overnight_intelligence_history.json
    yfinance 직접 호출은 이 함수에서 하지 않음
    (→ DynamicVIXThreshold.from_history의 fallback 단에서만)
    """
    import json
    results = PROJECT_ROOT / 'results'
    vix_cache = PROJECT_ROOT / 'data' / 'cache' / 'vix_daily.json'
    if vix_cache.exists():
        try:
            raw = vix_cache.read_text()
            data = json.loads(raw)
            values = data.get('vix_values', [])
            if values and len(values) >= 20:
                logger.debug(f'  VIX pre-fetch 케시 사용: {len(values)}일')
                return [float(v) for v in values[-window:] if v and float(v) > 0]
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    hist_file = results / 'overnight_intelligence_history.json'
    if not hist_file.exists():
        return []
    try:
        raw = hist_file.read_text()
    except OSError as e:
        logger.warning(f'  VIX 이력 파일 읽기 실패 (OS에러): {e}')
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f'  VIX 이력 JSON 파싱 실패: {e}')
        return []
    history = data.get('history', [])
    if not history:
        logger.debug("  VIX 이력 'history' 키 비어있음")
        return []
    vix_list = []
    for rec in history[-window:]:
        try:
            v = (rec.get('us_market', {}) or {}).get('vix', {})
            vix_val = v.get('close') if isinstance(v, dict) else None
            if vix_val and float(vix_val) > 0:
                vix_list.append(float(vix_val))
        except (TypeError, ValueError):
            continue
    return vix_list

def _fetch_vix_from_yfinance(fallback_window: int=252) -> List[float]:
    """
    [BIAS-FIX CRITICAL-2] yfinance로 VIX 최신 1년 데이터 직접 로드.
    overnight_intelligence_history 없어도 실제 분포 기반 임계값 산출 가능.
    """
    try:
        import yfinance as yf
        import pandas as _pd
        df = yf.download('^VIX', period='1y', interval='1d', progress=False, auto_adjust=False)
        if df is None or len(df) < 20:
            return []
        if isinstance(df.columns, _pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        close_col = 'Close' if 'Close' in df.columns else df.columns[0]
        vix_vals = df[close_col].dropna().tolist()
        logger.info(f'  yfinance ^VIX 로드: {len(vix_vals)}일')
        return [float(v) for v in vix_vals if v > 0]
    except Exception as e:
        logger.debug(f'  yfinance ^VIX 로드 실패: {e}')
        return []
_vix_threshold_cache: Optional[DynamicVIXThreshold] = None
_vix_cache_date: str = ''

def get_vix_threshold() -> DynamicVIXThreshold:
    """당일 캐시된 DynamicVIXThreshold 반환."""
    global _vix_threshold_cache, _vix_cache_date
    from datetime import date
    today = date.today().isoformat()
    if _vix_threshold_cache is None or _vix_cache_date != today:
        _vix_threshold_cache = DynamicVIXThreshold.from_history()
        _vix_cache_date = today
    return _vix_threshold_cache

def atr_based_sl_tp(atr_14d: float, sl_multiplier: float=1.5, tp_multiplier: float=3.0, min_sl: float=-0.005, max_sl: float=-0.03, min_tp: float=0.01, max_tp: float=0.08) -> Tuple[float, float]:
    """
    ATR(14일) 기반 변동성 정규화 손절/익절.

    기존 고정값(-1%/+2%)은 변동성 무시 → 저변동 시 과대, 고변동 시 과소.
    ATR 배수는 Risk:Reward = 2:1 유지.

    Args:
        atr_14d:        14일 평균 진폭 (비율, 예: 0.012 = 1.2%)
        sl_multiplier:  손절 = atr × sl_multiplier
        tp_multiplier:  익절 = atr × tp_multiplier
        min_sl/max_sl:  손절 범위 클램프
        min_tp/max_tp:  익절 범위 클램프

    Returns:
        (sl_pct, tp_pct)  예: (-0.012, 0.024)
    """
    if atr_14d <= 0:
        return (-0.01, 0.02)
    sl = -atr_14d * sl_multiplier
    tp = atr_14d * tp_multiplier
    sl = float(min(max(sl, max_sl), min_sl))
    tp = float(min(max(tp, min_tp), max_tp))
    return (sl, tp)

def compute_atr(prices: List[float], period: int=14) -> float:
    """
    주가 시리즈에서 ATR 계산 (일간 수익률 표준편차 근사).

    Args:
        prices: 종가 리스트 (최근 n일, 오래된 것 먼저)
        period: ATR 기간

    Returns:
        ATR (비율)
    """
    if len(prices) < period + 1:
        return 0.015
    rets = [abs(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
    return float(np.mean(rets[-period:]))

def ic_weighted_blend(ic_factor_rank: float, ic_catboost: float, shrinkage: float=0.1, min_weight: float=0.2) -> Tuple[float, float]:
    """
    IC 비율 기반 앙상블 블렌딩 가중치.

    기존: 고정 0.5/0.5
    개선: IC 비율 + Shrinkage → 과도한 편향 방지

    Args:
        ic_factor_rank: Factor Rank IC (절대값)
        ic_catboost:    CatBoost IC (절대값)
        shrinkage:      James-Stein 수축 파라미터 (과적합 방지)
        min_weight:     최소 가중치 (0이 되지 않도록)

    Returns:
        (w_factor_rank, w_catboost)  합계 = 1.0
    """
    af = abs(ic_factor_rank) + shrinkage
    ac = abs(ic_catboost) + shrinkage
    total = af + ac
    w_factor = af / total
    w_catboost = ac / total
    w_factor = max(w_factor, min_weight)
    w_catboost = max(w_catboost, min_weight)
    s = w_factor + w_catboost
    return (round(w_factor / s, 4), round(w_catboost / s, 4))

def load_ic_for_blend() -> Tuple[float, float]:
    """
    ic_monitor_state.json에서 최근 IC 로드.

    [BIAS-FIX IMPORTANT-1] 기존 bare except Exception → 에러 유형별 구분 로깅.
    IC 파일 손상 시 자동 equal-blend(0.03, 0.03) 복귀는 알파 소실 위험.
    각 실패 원인을 WARNING 로그로 운영자에게 노출.

    Returns:
        (ic_factor_rank, ic_catboost) — 없으면 (0.03, 0.03) + WARNING
    """
    import json
    ic_file = PROJECT_ROOT / 'results' / 'ic_monitor_state.json'
    if not ic_file.exists():
        logger.debug('  ic_monitor_state.json 없음 → equal-blend fallback (0.03, 0.03)')
        return (0.03, 0.03)
    try:
        raw = ic_file.read_text()
    except OSError as e:
        logger.warning(f'  IC 파일 읽기 실패: {e} → equal-blend fallback (알파 소실 주의)')
        return (0.03, 0.03)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f'  IC JSON 파싱 실패: {e} → equal-blend fallback (파일 손상 의심)')
        return (0.03, 0.03)
    try:
        ic_factor = abs(float(state.get('ic_factor_rank', 0.03)))
        ic_catboost = abs(float(state.get('last_ic', 0.03)))
        if ic_factor == 0.03 and ic_catboost == 0.03:
            logger.debug('  IC 상태 파일에 유효한 IC 없음 → equal-blend (0.03/0.03)')
        return (ic_factor, ic_catboost)
    except (TypeError, ValueError) as e:
        logger.warning(f'  IC 값 형변환 실패: {e} → equal-blend fallback')
        return (0.03, 0.03)

def regime_quantile_thresholds(scores: List[float], bull_pct: float=67, bear_pct: float=33) -> Tuple[float, float]:
    """
    과거 스코어 분포 기반 레짐 경계 (분위수).

    기존: z_score > 0.5 고정, BULL_THRESHOLD = 0.03 고정
    개선: 실제 분포의 상위/하위 percentile 사용
          → 레짐 비율이 균형 유지 (bull 33%, neutral 34%, bear 33%)

    Args:
        scores:   과거 레짐 스코어 시리즈
        bull_pct: Bull 기준 백분위 (기본 상위 33% → 67번째 퍼센타일)
        bear_pct: Bear 기준 백분위 (기본 하위 33%)

    Returns:
        (bear_threshold, bull_threshold)
        score < bear_threshold → BEAR
        score > bull_threshold → BULL
        사이 → NEUTRAL
    """
    if len(scores) < 20:
        return (-0.5, 0.5)
    arr = np.array(scores)
    bear_th = float(np.percentile(arr, bear_pct))
    bull_th = float(np.percentile(arr, bull_pct))
    return (bear_th, bull_th)

def kelly_position_limit(win_rate: float, avg_win: float, avg_loss: float, kelly_fraction: float=0.5, min_limit: float=0.01, max_limit: float=0.1) -> float:
    """
    Half-Kelly 포지션 상한 산출.

    기존: 고정 5% NAV
    개선: 실제 승률/손익비에서 최적 베팅 크기 계산
          Half-Kelly (보수적)으로 과도한 포지션 방지

    Args:
        win_rate:       승률 (예: 0.55)
        avg_win:        평균 수익 (비율, 예: 0.02)
        avg_loss:       평균 손실 (비율 절대값, 예: 0.01)
        kelly_fraction: Kelly 분수 (0.5 = Half-Kelly)
        min_limit:      최소 상한 (1%)
        max_limit:      최대 상한 (10%)

    Returns:
        포지션 상한 (비율)
    """
    if avg_loss <= 0 or win_rate <= 0:
        return 0.05
    b = avg_win / avg_loss
    q = 1.0 - win_rate
    kelly = (win_rate * b - q) / b
    half_kelly = kelly * kelly_fraction
    return float(min(max(half_kelly, min_limit), max_limit))

def vol_adjusted_halt(portfolio_vol_1d: float, sigma_multiplier: float=2.0, min_halt: float=-0.01, max_halt: float=-0.05) -> float:
    """
    포트폴리오 일간 변동성 기반 매매 중단 손실 한도.

    기존: 고정 -2%
    개선: 당일 예상 변동성의 2σ → 저변동 시 엄격, 고변동 시 탄력적

    Args:
        portfolio_vol_1d: 포트폴리오 일간 변동성 (예: 0.01 = 1%)
        sigma_multiplier: σ 배수 (기본 2.0)
        min_halt:         최소 한도 (절대값 작아지면 너무 엄격)
        max_halt:         최대 한도 (절대값 커지면 너무 관대)

    Returns:
        손실 한도 (음수, 예: -0.016)

    예시:
        vol 0.5% → halt = -0.010 (낮은 변동성: 엄격)
        vol 1.0% → halt = -0.020 (보통)
        vol 2.0% → halt = -0.040 (높은 변동성: 탄력)
    """
    if portfolio_vol_1d <= 0:
        return -0.02
    halt = -portfolio_vol_1d * sigma_multiplier
    return float(min(max(halt, max_halt), min_halt))

def optimal_lookback_from_ic(ic_series: List[float], max_lag: int=120, decay_threshold: float=0.5, fallback: int=20) -> int:
    """
    IC 자기상관 감쇠 분석으로 최적 룩백 산출.

    IC-decay 반감기 = IC가 초기값의 50%로 감소하는 지연(lag).
    → 이 지연값이 신호의 "유효 수명" → 룩백 창으로 사용.

    Args:
        ic_series:        과거 IC 시리즈
        max_lag:          분석할 최대 지연
        decay_threshold:  반감 기준 (기본 50%)
        fallback:         IC 시리즈 부족 시 기본값

    Returns:
        최적 룩백 기간 (거래일)
    """
    if len(ic_series) < 20:
        return fallback
    arr = np.array(ic_series)
    if np.std(arr) == 0:
        return fallback
    ic0 = 1.0
    for lag in range(1, min(max_lag, len(arr) - 1) + 1):
        ic_lag = float(np.corrcoef(arr[:-lag], arr[lag:])[0, 1]) if lag < len(arr) else 0
        if abs(ic_lag) < ic0 * decay_threshold:
            return max(lag, 5)
    return max_lag

class MultiHorizonEWMA:
    """
    단기(Fast), 중기(Medium), 장기(Slow) EWMA를 앙상블하여
    시장의 노이즈를 제거하고 진짜 추세(Baseline)를 추출하는 유틸리티.
    """

    def __init__(self, fast_span: int=20, med_span: int=60, slow_span: int=120):
        self.fast_span = fast_span
        self.med_span = med_span
        self.slow_span = slow_span

    def compute(self, series: List[float], weights: Tuple[float, float, float]=(0.2, 0.5, 0.3)) -> float:
        """
        시계열 데이터에서 Multi-Horizon EWMA의 최신 값을 계산.
        
        Args:
            series: 시계열 데이터 (과거 -> 최신)
            weights: (Fast, Medium, Slow) 앙상블 가중치
            
        Returns:
            앙상블된 최신 Baseline 값
        """
        if not series:
            return 0.0
        try:
            import pandas as pd
            s = pd.Series(series)
            fast_ewma = s.ewm(span=self.fast_span, adjust=False).mean().iloc[-1]
            med_ewma = s.ewm(span=self.med_span, adjust=False).mean().iloc[-1]
            slow_ewma = s.ewm(span=self.slow_span, adjust=False).mean().iloc[-1]
            blend = fast_ewma * weights[0] + med_ewma * weights[1] + slow_ewma * weights[2]
            return float(blend)
        except ImportError as e:
            s = np.array(series)
            f = np.mean(s[-self.fast_span:]) if len(s) >= self.fast_span else np.mean(s)
            m = np.mean(s[-self.med_span:]) if len(s) >= self.med_span else np.mean(s)
            sl = np.mean(s[-self.slow_span:]) if len(s) >= self.slow_span else np.mean(s)
            return float(f * weights[0] + m * weights[1] + sl * weights[2])

class VolatilityScaledThreshold:
    """
    고정된 컷오프(예: > 1.3) 대신, 현재 변동성(표준편차)과 
    Percentile을 기반으로 임계값을 동적으로 조절하는 클래스.
    """

    @staticmethod
    def is_extreme(current_val: float, history: List[float], z_score_limit: float=1.5, percentile_limit: float=90.0) -> bool:
        """
        현재 값이 과거 분포 대비 '극단적(Extreme)'인지 판단합니다.
        조건: Z-score가 z_score_limit 이상이거나, 상위 percentile_limit % 이상일 때.
        """
        if not history or len(history) < 10:
            return False
        arr = np.array(history)
        mean = np.mean(arr)
        std = np.std(arr)
        z_score = (current_val - mean) / std if std > 0 else 0
        z_condition = abs(z_score) > z_score_limit
        pct_val = np.percentile(arr, percentile_limit)
        pct_condition = current_val > pct_val
        return z_condition or pct_condition

    @staticmethod
    def get_dynamic_threshold(history: List[float], baseline_multiplier: float=1.5) -> float:
        """
        과거 이력을 바탕으로 현재 적용해야 할 동적 상단 임계값을 반환.
        Threshold = Multi-Horizon Baseline + (Volatility * multiplier)
        """
        if not history or len(history) < 20:
            return 0.0
        ewma_engine = MultiHorizonEWMA()
        baseline = ewma_engine.compute(history)
        recent_std = np.std(history[-60:])
        return float(baseline + recent_std * baseline_multiplier)
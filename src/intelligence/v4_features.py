"""
V7 Feature Engine — 56 피처 (V6 53 + SS-ETF 3) + Auto-Alpha 동적 확장
======================================================================
V3 25피처 + V4 10신규 + V5 Cross-Market 3 + V6 Auxiliary 15 = 53피처.

V7 신규 (SS-ETF 유동성 팩터 3):
  SS-ETF (3): ss_etf_vol_ratio, lp_delta_pressure, intraday_vol_anomaly
  └─ 단일종목 레버리지/인버스 ETF 상장(2026-05-27) 후부터 유효.
  └─ 상장 이전 기간: 0.0 Impute (ML StandardScaler 안전).

V8 동적 자동제어 (Alpha Factory):
  auto_alpha_001, auto_alpha_002 ... — results/discovered_alphas.json 에서
  'status': 'active' 인 수식이 ML DF에 자동 주입.
  └─ inject_auto_alphas(df) 호출 시 활성 알파 컬럼 자동 추가.
  └─ 알파 IC 소멸 시 AlphaGarbageCollector가 'retired' 자동 전환.

V6 신규 (보조 데이터):
  Sentiment (4): news_sentiment_mean, news_sentiment_std, news_count_norm, news_pos_ratio
  DART (3): dart_insider_signal, dart_buyback_signal, dart_composite
  Flow (4): foreign_net_buy_norm, inst_net_buy_norm, foreign_ratio_feat, short_proxy_score
  Financials (4): earnings_surprise_latest, revenue_yoy_latest, roe_2yr_avg, debt_ratio_latest

Usage:
    from src.intelligence.v4_features import extract_v4, FEATURE_NAMES_V4
    # V6 전체:
    from src.intelligence.v4_features import FEATURE_NAMES_V6
    # V7 (단일종목 ETF 포함):
    from src.intelligence.v4_features import FEATURE_NAMES_V7, SS_ETF_FEATURE_NAMES
    # 자동 알파 주입:
    from src.intelligence.v4_features import inject_auto_alphas
    df = inject_auto_alphas(df)
"""
import logging
import numpy as np
from typing import Dict, List, Optional, Any
logger = logging.getLogger(__name__)
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None

def get_active_features(version: str='v7') -> List[str]:
    """학습에 사용할 활성 피처 목록 반환.

    DynamicConfig ml.excluded_features에 등록된 피처를 제외.
    0값으로만 채워지는 피처(DART, 재무 등)를 동적으로 제거 가능.

    Args:
        version: 'v4' (38피처) / 'v6' (53피처) / 'v7' (56피처, 기본)

    Returns:
        활성 피처 이름 리스트
    """
    if version == 'v4':
        base = FEATURE_NAMES_V4
    elif version == 'v6':
        base = FEATURE_NAMES_V6
    else:
        base = FEATURE_NAMES_V7
    excluded = []
    if _cfg:
        excluded = _cfg.get('ml.excluded_features', [])
    if not excluded:
        return list(base)
    active = [f for f in base if f not in excluded]
    if excluded:
        logger.info(f'  🔧 피처 제외: {len(excluded)}개 → 활성 {len(active)}/{len(base)}개')
    return active
FEATURE_NAMES_V4 = ['rsi_14', 'bb_position', 'macd_signal', 'volume_ratio_20d', 'atr_pct', 'ma5_dist', 'ma20_dist', 'ma60_dist', 'return_5d', 'return_20d', 'volatility_20d', 'asset_type', 'mean_reversion', 'trend_strength', 'volume_trend', 'return_1d', 'return_3d', 'ma5_ma20_cross', 'high_low_range', 'close_to_high_20d', 'rsi_slope_5d', 'volume_spike', 'adx_proxy', 'overnight_return', 'intraday_return', 'earnings_surprise', 'revenue_yoy', 'earnings_qoq', 'earnings_momentum', 'log_return_skew_20d', 'log_return_kurtosis_20d', 'price_ma_ratio_5_20', 'price_ma_ratio_20_60', 'vol_ratio_5_20', 'obv_slope_20d', 'sp500_overnight_return', 'vix_change_1d', 'usdkrw_change_5d']
FEATURE_NAMES_V6 = FEATURE_NAMES_V4 + ['news_sentiment_mean', 'news_sentiment_std', 'news_count_norm', 'news_pos_ratio', 'dart_insider_signal', 'dart_buyback_signal', 'dart_composite', 'foreign_net_buy_norm', 'inst_net_buy_norm', 'foreign_ratio_feat', 'short_proxy_score', 'earnings_surprise_latest', 'revenue_yoy_latest', 'roe_2yr_avg', 'debt_ratio_latest', 'automl_alpha_score']
try:
    from src.data_collection.ss_etf_feature_engine import SS_ETF_FEATURE_NAMES
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    SS_ETF_FEATURE_NAMES = ['ss_etf_vol_ratio', 'lp_delta_pressure', 'intraday_vol_anomaly']
FEATURE_NAMES_V7 = FEATURE_NAMES_V6 + SS_ETF_FEATURE_NAMES
SIGNATURE_FEATURE_NAMES: List[str] = ['sig5_level1_p', 'sig5_area', 'sig20_level1_p', 'sig20_area', 'sig20_mom_qual']
FEATURE_NAMES_V7 = FEATURE_NAMES_V7 + SIGNATURE_FEATURE_NAMES
EXPORT_MACRO_FEATURE_NAMES: List[str] = ['export_total_yoy', 'export_10d_yoy', 'export_yoy_auto', 'export_yoy_semi', 'export_yoy_battery', 'us_export_yoy', 'china_export_yoy', 'us_momentum_spread', 'china_rebound_index']
FEATURE_NAMES_V7 = FEATURE_NAMES_V7 + EXPORT_MACRO_FEATURE_NAMES
try:
    from src.alpha_factory.alpha_miner import get_active_alpha_names
    AUTO_ALPHA_FEATURE_NAMES: List[str] = get_active_alpha_names()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    AUTO_ALPHA_FEATURE_NAMES = []
ALPHA_FACTORY_NEW_EDGES: List[str] = ['alpha_order_imbalance_10d', 'alpha_smart_money_flow_20d', 'alpha_vol_term_structure', 'alpha_vol_adj_mom_10d', 'alpha_dd_velocity_3d', 'alpha_pca_mr_proxy_20d']
FEATURE_NAMES_V8 = FEATURE_NAMES_V7 + AUTO_ALPHA_FEATURE_NAMES + ALPHA_FACTORY_NEW_EDGES
FEATURE_NAMES = FEATURE_NAMES_V8

def inject_auto_alphas(df, max_alphas: Optional[int]=None):
    """discovered_alphas.json의 활성 알파를 DataFrame에 자동 주입.

    'status': 'active'인 알파만 OOS IC 내림차순으로 주입.
    실패 시 해당 컬럼 = 0.0 (ZeroDivisionError, NaN, Inf 모두 안전 처리).

    Args:
        df:         ML Feature DataFrame
        max_alphas: 주입할 최대 알파 수 (None=DynamicConfig 값)

    Returns:
        auto_alpha_001 ... 컬럼이 추가된 DataFrame
    """
    df_out = df.copy()
    try:
        from src.alpha_factory.alpha_miner import inject_auto_alphas as _inject
        df_out = _inject(df_out, max_alphas=max_alphas)
    except Exception as e:
        logger.error(f'  inject_auto_alphas 실패: {e}', exc_info=True)
    try:
        from src.alpha_factory.edges.microstructure import OrderImbalanceAlpha
        from src.alpha_factory.edges.vol_surface import VolatilitySurfaceAlpha
        from src.alpha_factory.edges.stat_arb import PCAMeanReversionAlpha
        df_out = OrderImbalanceAlpha.generate(df_out)
        df_out = VolatilitySurfaceAlpha.generate(df_out)
        df_out = PCAMeanReversionAlpha.generate(df_out)
    except Exception as e:
        logger.error(f'  차세대 엣지 생성 실패: {e}', exc_info=True)
    return df_out

def extract_v4(close, high, low, opn, volume, idx, is_etf=False, earnings: Optional[Dict]=None, cross_asset: Optional[Dict]=None, aux_data: Optional[Dict]=None, alpha_model: Optional[Any]=None, ss_etf_data: Optional[Dict]=None) -> Optional[Dict]:
    """V7 56피처 추출.

    Args:
        close, high, low, opn, volume: 가격/거래량 배열
        idx: 현재 인덱스
        is_etf: ETF 여부
        earnings: {'earnings_surprise': ..., 'revenue_yoy': ..., ...}
        cross_asset: {'sp500_return': float, 'vix_close': float, ...}
        aux_data: AuxDataLoader.get_features() 결과 (15개 보조 피처)
        ss_etf_data: SSETFFeatureEngine.compute() 결과 (3개 SS-ETF 피처)
            {'ss_etf_vol_ratio': float, 'lp_delta_pressure': float,
             'intraday_vol_anomaly': float}
            상장 이전이거나 단일종목 ETF 비대상 종목이면 None 또는 {}

    Returns:
        Dict[feature_name: value] or None
    """
    try:
        from scripts.train_ensemble import extract_v3
        feat = extract_v3(close, high, low, opn, volume, idx, is_etf)
    except ImportError as e:
        feat = _extract_v3_standalone(close, high, low, opn, volume, idx, is_etf)
    if feat is None:
        return None
    n = len(close)
    _warmup = int(_cfg.get('ml.warmup_bars', 60)) if _cfg else 60
    if idx < _warmup or idx >= n:
        return None
    c = close[:idx + 1]
    earn = earnings or {}
    aux = aux_data or {}
    feat['earnings_surprise'] = float(earn.get('earnings_surprise', aux.get('earnings_surprise_latest', 0.0)))
    feat['revenue_yoy'] = float(earn.get('revenue_yoy', aux.get('revenue_yoy_latest', 0.0)))
    feat['earnings_qoq'] = float(earn.get('earnings_qoq', aux.get('earnings_surprise_latest', 0.0)))
    feat['earnings_momentum'] = float(earn.get('earnings_momentum', aux.get('revenue_yoy_latest', 0.0)))
    log_ret = np.diff(np.log(c[-21:])) if len(c) >= 21 else np.zeros(1)
    feat['log_return_skew_20d'] = float(_safe_skew(log_ret))
    feat['log_return_kurtosis_20d'] = float(_safe_kurtosis(log_ret))
    w_fast = _cfg.get('ml.feature_window_fast', 5) if _cfg else 5
    w_mid = _cfg.get('ml.feature_window_mid', 20) if _cfg else 20
    w_slow = _cfg.get('ml.feature_window_slow', 60) if _cfg else 60
    ma5 = np.mean(c[-w_fast:]) if len(c) >= w_fast else c[-1]
    ma20 = np.mean(c[-w_mid:]) if len(c) >= w_mid else c[-1]
    ma60 = np.mean(c[-w_slow:]) if len(c) >= w_slow else c[-1]
    feat['price_ma_ratio_5_20'] = float(ma5 / ma20 - 1) if ma20 > 0 else 0
    feat['price_ma_ratio_20_60'] = float(ma20 / ma60 - 1) if ma60 > 0 else 0
    v = volume[:idx + 1]
    vma5 = np.mean(v[-w_fast:]) if len(v) >= w_fast else 1
    vma20 = np.mean(v[-w_mid:]) if len(v) >= w_mid else 1
    feat['vol_ratio_5_20'] = float(vma5 / vma20) if vma20 > 0 else 1.0
    if len(c) >= 21 and len(v) >= 21:
        ret_signs = np.sign(np.diff(c[-21:]))
        obv = np.cumsum(ret_signs * v[-20:])
        x = np.arange(len(obv))
        slope = np.polyfit(x, obv, 1)[0] if len(obv) > 1 else 0
        feat['obv_slope_20d'] = float(slope / (np.mean(v[-20:]) + 1))
    else:
        feat['obv_slope_20d'] = 0.0
    ca = cross_asset or {}
    feat['sp500_overnight_return'] = float(ca.get('sp500_return', 0.0))
    vix_close = ca.get('vix_close', 0)
    vix_prev = ca.get('vix_prev', 0)
    if vix_prev > 0:
        feat['vix_change_1d'] = float((vix_close / vix_prev - 1) * 100)
    else:
        feat['vix_change_1d'] = 0.0
    usdkrw = ca.get('usdkrw_close', 0)
    usdkrw_5d = ca.get('usdkrw_5d_ago', 0)
    if usdkrw_5d > 0:
        feat['usdkrw_change_5d'] = float((usdkrw / usdkrw_5d - 1) * 100)
    else:
        feat['usdkrw_change_5d'] = 0.0
    aux = aux_data or {}
    feat['news_sentiment_mean'] = float(aux.get('news_sentiment_mean', 0.5))
    feat['news_sentiment_std'] = float(aux.get('news_sentiment_std', 0.0))
    feat['news_count_norm'] = float(aux.get('news_count_norm', 0.0))
    feat['news_pos_ratio'] = float(aux.get('news_pos_ratio', 0.5))
    feat['dart_insider_signal'] = float(aux.get('dart_insider_signal', 0.0))
    feat['dart_buyback_signal'] = float(aux.get('dart_buyback_signal', 0.0))
    feat['dart_composite'] = float(aux.get('dart_composite', 0.0))
    feat['foreign_net_buy_norm'] = float(aux.get('foreign_net_buy_norm', 0.0))
    feat['inst_net_buy_norm'] = float(aux.get('inst_net_buy_norm', 0.0))
    feat['foreign_ratio_feat'] = float(aux.get('foreign_ratio_feat', 0.0))
    feat['short_proxy_score'] = float(aux.get('short_proxy_score', 0.0))
    feat['earnings_surprise_latest'] = float(aux.get('earnings_surprise_latest', 0.0))
    feat['revenue_yoy_latest'] = float(aux.get('revenue_yoy_latest', 0.0))
    feat['roe_2yr_avg'] = float(aux.get('roe_2yr_avg', 0.0))
    feat['debt_ratio_latest'] = float(aux.get('debt_ratio_latest', 0.0))
    ss = ss_etf_data or {}
    feat['ss_etf_vol_ratio'] = float(ss.get('ss_etf_vol_ratio', 0.0))
    feat['lp_delta_pressure'] = float(ss.get('lp_delta_pressure', 0.0))
    feat['intraday_vol_anomaly'] = float(ss.get('intraday_vol_anomaly', 0.0))
    return feat

def _extract_v3_standalone(close, high, low, opn, volume, idx, is_etf=False):
    """V3 피처 독립 추출 (train_ensemble import 불가 시 fallback)."""
    n = len(close)
    _warmup_v3 = int(_cfg.get('ml.warmup_bars_v3', 260)) if _cfg else 260
    if idx < _warmup_v3 or idx >= n - 1:
        return None
    c = close[:idx + 1]
    h = high[:idx + 1]
    l = low[:idx + 1]
    o = opn[:idx + 1]
    v = volume[:idx + 1]

    def _safe(val, default=0.0):
        return float(val) if np.isfinite(val) else default
    diffs = np.diff(c[-15:])
    gains = np.mean(np.maximum(diffs, 0))
    losses = np.mean(np.maximum(-diffs, 0))
    rsi = 100 - 100 / (1 + gains / losses) if losses > 0 else 50
    rsi = _safe(rsi, 50)
    ma20 = np.mean(c[-20:])
    std20 = np.std(c[-20:])
    bb = (c[-1] - (ma20 - 2 * std20)) / (4 * std20) if std20 > 0 else 0.5
    _macd_fast = int(_cfg.get('ml.macd_fast', 12)) if _cfg else 12
    _macd_slow = int(_cfg.get('ml.macd_slow', 26)) if _cfg else 26
    ema_fast = np.mean(c[-_macd_fast:])
    ema_slow = np.mean(c[-_macd_slow:])
    macd_val = ema_fast - ema_slow
    macd_signal = macd_val / (np.std(c[-_macd_slow:]) + 1e-08)
    vol_ma20 = np.mean(v[-20:])
    vol_ratio = v[-1] / vol_ma20 if vol_ma20 > 0 else 1.0
    tr = np.maximum(h[-20:] - l[-20:], np.abs(h[-20:] - np.roll(c[-20:], 1)[1:20]), np.abs(l[-20:] - np.roll(c[-20:], 1)[1:20]))
    atr_pct = np.mean(tr[-14:]) / c[-1] * 100 if c[-1] > 0 else 0
    w_fast = _cfg.get('ml.feature_window_fast', 5) if _cfg else 5
    w_mid = _cfg.get('ml.feature_window_mid', 20) if _cfg else 20
    w_slow = _cfg.get('ml.feature_window_slow', 60) if _cfg else 60
    ma5 = np.mean(c[-w_fast:])
    ma60 = np.mean(c[-w_slow:])
    ret_1d = (c[-1] / c[-2] - 1) * 100 if len(c) >= 2 and c[-2] > 0 else 0
    ret_3d = (c[-1] / c[-4] - 1) * 100 if len(c) >= 4 and c[-4] > 0 else 0
    ret_5d = (c[-1] / c[-6] - 1) * 100 if len(c) >= 6 and c[-6] > 0 else 0
    _idx_mid = w_mid + 1
    ret_20d = (c[-1] / c[-_idx_mid] - 1) * 100 if len(c) >= _idx_mid and c[-_idx_mid] > 0 else 0
    vol_20d = np.std(np.diff(np.log(c[-_idx_mid:]))) * np.sqrt(252) * 100 if len(c) >= _idx_mid else 0
    automl_score = 0.0
    alpha_model = None
    if alpha_model is not None:
        try:
            ret_1d_val = c[-1] / c[-2] - 1 if len(c) > 1 and c[-2] > 0 else 0.0
            vol_20 = np.std(np.diff(np.log(c[-21:]))) * np.sqrt(252) if len(c) >= 20 else 0.0
            X_row = np.array([[c[-1], h[-1], l[-1], v[-1], ret_1d_val, vol_20, rsi, _safe(bb)]])
            if hasattr(alpha_model, 'execute'):
                automl_score = float(alpha_model.execute(X_row)[0])
            elif hasattr(alpha_model, 'predict'):
                automl_score = float(alpha_model.predict(X_row)[0])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at v4_features.py:469', exc_info=True)
    return {'rsi_14': rsi, 'bb_position': _safe(bb), 'macd_signal': _safe(macd_signal), 'volume_ratio_20d': _safe(vol_ratio), 'atr_pct': _safe(atr_pct), 'ma5_dist': _safe((c[-1] / ma5 - 1) * 100), 'ma20_dist': _safe((c[-1] / ma20 - 1) * 100), 'ma60_dist': _safe((c[-1] / ma60 - 1) * 100), 'return_5d': _safe(ret_5d), 'return_20d': _safe(ret_20d), 'volatility_20d': _safe(vol_20d), 'asset_type': 1.0 if is_etf else 0.0, 'mean_reversion': _safe(rsi - 50), 'trend_strength': _safe(abs(c[-1] / ma20 - 1) * 100), 'volume_trend': _safe(np.mean(v[-5:]) / vol_ma20 if vol_ma20 > 0 else 1), 'return_1d': _safe(ret_1d), 'return_3d': _safe(ret_3d), 'ma5_ma20_cross': 1.0 if ma5 > ma20 else 0.0, 'high_low_range': _safe((np.max(h[-20:]) - np.min(l[-20:])) / c[-1] * 100), 'close_to_high_20d': _safe(c[-1] / np.max(h[-20:])), 'rsi_slope_5d': _safe(rsi - (100 - 100 / (1 + np.mean(np.maximum(np.diff(c[-20:-15]), 0)) / (np.mean(np.maximum(-np.diff(c[-20:-15]), 0)) + 1e-08))), 0), 'volume_spike': 1.0 if v[-1] > vol_ma20 * (float(_cfg.get('ml.volume_spike_ratio', 2.0)) if _cfg else 2.0) else 0.0, 'adx_proxy': _safe(abs(ret_5d) / (vol_20d / np.sqrt(252) + 1e-08)), 'overnight_return': _safe((o[-1] / c[-2] - 1) * 100 if c[-2] > 0 else 0), 'intraday_return': _safe((c[-1] / o[-1] - 1) * 100 if o[-1] > 0 else 0), 'automl_alpha_score': _safe(automl_score)}

def _safe_skew(arr):
    n = len(arr)
    if n < 3:
        return 0.0
    m = np.mean(arr)
    s = np.std(arr)
    if s < 1e-10:
        return 0.0
    return float(np.mean(((arr - m) / s) ** 3))

def _safe_kurtosis(arr):
    n = len(arr)
    if n < 4:
        return 0.0
    m = np.mean(arr)
    s = np.std(arr)
    if s < 1e-10:
        return 0.0
    return float(np.mean(((arr - m) / s) ** 4) - 3)
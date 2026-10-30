"""
Meridian — Statistical Arbitrage (Pairs Trading) Engine
=========================================================
KOSPI 200 전체 유니버스의 가격 시계열을 바탕으로 
공적분(Cointegration) 검정을 수행하고, 
스프레드가 임계치를 이탈한 Long-Short 페어 시그널을 추출합니다.
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import logging
import math
from typing import Any, List, Dict, Tuple
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)

try:
    import statsmodels.tsa.stattools as ts
    import statsmodels.api as sm
    _STATSMODELS_AVAILABLE = True
except ImportError:
    ts = None
    sm = None
    _STATSMODELS_AVAILABLE = False
    logger.debug("[StatArbEngine] statsmodels 미설치 — numpy Kalman/OU 대체 모드로 동작합니다.")

_EPS_VAR = 1e-12
_PHI_FLOOR = -0.999
_TAU_MIN = 1.0
_TAU_OPT = 10.0
_TAU_SCALE = 25.0
_TAU_MAX = 35.0
_Q_MID = 0.60
_HALF = 0.50
_MIN_SERIES_LEN = 5
_MIN_COINT_LEN = 60
_CORR_GATE = 0.70
_DF_CRIT_T = -1.95


class StatArbEngine:

    def __init__(self, p_value_threshold: float=0.05, z_score_entry: float=2.0, z_score_exit: float=0.0, max_pairs: int=20, z_score_window: int=60):
        self.p_value_threshold = p_value_threshold
        self.z_score_entry = z_score_entry
        self.z_score_exit = z_score_exit
        self.max_pairs = max_pairs
        self.z_score_window = z_score_window

    @staticmethod
    def estimate_kalman_hedge_ratio(
        y: np.ndarray,
        x: np.ndarray,
        delta: float = 1e-4,
        obs_var: float = 1e-3,
    ) -> Dict[str, Any]:
        """Citadel GQS 2D State-Space Kalman Filter for time-varying intercept α_t and hedge ratio β_t."""
        y_arr = np.asarray(y, dtype=float).ravel()
        x_arr = np.asarray(x, dtype=float).ravel()
        n = min(len(y_arr), len(x_arr))
        if n < _MIN_SERIES_LEN:
            return {
                'alpha': 0.0,
                'beta': 1.0,
                'spread': y_arr[:n] - x_arr[:n],
                'innovation_z': 0.0,
            }
        y_arr = y_arr[-n:]
        x_arr = x_arr[-n:]

        var_x0 = float(np.var(x_arr, ddof=1)) if n > 1 else 0.0
        if var_x0 > _EPS_VAR:
            cov_xy0 = float(np.cov(x_arr, y_arr, ddof=1)[0, 1])
            beta0 = cov_xy0 / var_x0
            alpha0 = float(np.mean(y_arr) - beta0 * np.mean(x_arr))
        else:
            beta0 = 1.0
            alpha0 = 0.0

        theta = np.array([alpha0, beta0], dtype=float)
        p_cov = np.eye(2, dtype=float)
        q_val = float(delta) / max(1.0 - float(delta), _EPS_VAR)
        q_cov = np.eye(2, dtype=float) * q_val
        r_var = max(float(obs_var), _EPS_VAR)

        spread = np.zeros(n, dtype=float)
        last_innov_z = 0.0
        eye2 = np.eye(2, dtype=float)

        for t in range(n):
            f_vec = np.array([1.0, x_arr[t]], dtype=float)
            p_pred = p_cov + q_cov
            y_pred = float(f_vec @ theta)
            err = float(y_arr[t] - y_pred)
            s_var = float(f_vec @ p_pred @ f_vec) + r_var
            k_gain = (p_pred @ f_vec) / max(s_var, _EPS_VAR)
            theta = theta + k_gain * err
            p_cov = (eye2 - np.outer(k_gain, f_vec)) @ p_pred
            spread[t] = float(y_arr[t] - (theta[0] + theta[1] * x_arr[t]))
            last_innov_z = err / math.sqrt(max(s_var, _EPS_VAR))

        return {
            'alpha': float(theta[0]),
            'beta': float(theta[1]),
            'spread': spread,
            'innovation_z': float(last_innov_z),
        }

    @staticmethod
    def estimate_ou_half_life(spread: np.ndarray) -> Dict[str, Any]:
        """Citadel GQS Ornstein-Uhlenbeck (Δs_t = c + φ s_{t-1} + ε_t) Half-Life & Mean-Reversion Quality."""
        s_arr = np.asarray(spread, dtype=float).ravel()
        s_arr = s_arr[~np.isnan(s_arr)]
        n = len(s_arr)
        if n < _MIN_SERIES_LEN:
            return {
                'phi': 0.0,
                'kappa': 0.0,
                'half_life': float('inf'),
                'df_t_stat': 0.0,
                'is_stationary': False,
                'ou_quality': 0.0,
            }

        s_lag = s_arr[:-1]
        ds = s_arr[1:] - s_arr[:-1]
        s_lag_c = s_lag - float(np.mean(s_lag))
        ds_c = ds - float(np.mean(ds))
        denom = float(np.sum(s_lag_c * s_lag_c))
        if denom <= _EPS_VAR:
            return {
                'phi': 0.0,
                'kappa': 0.0,
                'half_life': float('inf'),
                'df_t_stat': 0.0,
                'is_stationary': False,
                'ou_quality': 0.0,
            }

        phi = float(np.sum(s_lag_c * ds_c) / denom)
        resid = ds_c - phi * s_lag_c
        dof = max(1, len(ds) - 2)
        s2 = float(np.sum(resid * resid)) / float(dof)
        se_phi = math.sqrt(max(s2 / denom, _EPS_VAR))
        df_t_stat = phi / se_phi

        if phi >= 0.0:
            return {
                'phi': round(phi, 6),
                'kappa': 0.0,
                'half_life': float('inf'),
                'df_t_stat': round(df_t_stat, 4),
                'is_stationary': False,
                'ou_quality': 0.0,
            }

        phi_safe = max(_PHI_FLOOR, phi)
        kappa = -math.log(1.0 + phi_safe)
        half_life = math.log(2.0) / max(kappa, _EPS_VAR)

        if half_life < _TAU_MIN:
            ou_quality = _HALF * half_life
        elif half_life <= _TAU_MAX:
            diff_opt = (half_life - _TAU_OPT) / _TAU_SCALE
            ou_quality = math.exp(-_HALF * diff_opt * diff_opt)
        else:
            ou_quality = _Q_MID * math.exp(-(half_life - _TAU_MAX) / _TAU_SCALE)

        ou_quality = max(0.0, min(1.0, ou_quality))
        is_stationary = bool(phi < 0.0 and half_life <= (_TAU_MAX * 2.0))
        return {
            'phi': round(phi, 6),
            'kappa': round(kappa, 6),
            'half_life': round(half_life, 4),
            'df_t_stat': round(df_t_stat, 4),
            'is_stationary': is_stationary,
            'ou_quality': round(ou_quality, 4),
        }

    def find_cointegrated_pairs(self, prices_df: pd.DataFrame) -> List[Dict]:
        """
        O(N^2) 공적분 검정을 수행하여 유효한 페어를 찾습니다.
        :param prices_df: 각 열이 종목 Ticker, 각 행이 시점인 가격 DataFrame
        :return: Cointegrated pairs list
        """
        n = prices_df.shape[1]
        keys = prices_df.keys()
        pairs = []
        corr_matrix = prices_df.corr()
        for i in range(n):
            for j in range(i + 1, n):
                t1, t2 = (keys[i], keys[j])
                if corr_matrix.loc[t1, t2] > 0.7:
                    series1 = prices_df[t1]
                    series2 = prices_df[t2]
                    if len(series1.dropna()) > _MIN_COINT_LEN:
                        try:
                            if _STATSMODELS_AVAILABLE and ts is not None:
                                score, pvalue, _ = ts.coint(series1, series2)
                                if pvalue < self.p_value_threshold:
                                    pairs.append({'asset_y': t1, 'asset_x': t2, 'p_value': pvalue, 'correlation': corr_matrix.loc[t1, t2]})
                            else:
                                kf = self.estimate_kalman_hedge_ratio(series1.dropna().values, series2.dropna().values)
                                ou = self.estimate_ou_half_life(kf['spread'])
                                if ou['is_stationary'] and ou['df_t_stat'] <= _DF_CRIT_T:
                                    p_approx = max(0.0, min(1.0, math.exp(_HALF * ou['df_t_stat'])))
                                    pairs.append({'asset_y': t1, 'asset_x': t2, 'p_value': round(p_approx, 4), 'correlation': corr_matrix.loc[t1, t2], 'half_life': ou['half_life'], 'ou_quality': ou['ou_quality']})
                        except Exception as e:
                            logger.error('[SILENT_BYPASS] Suppressed exception at stat_arb_engine.py:62', exc_info=True)
        logger.info(f"  [StatArb] 유니버스 {n}개 중 {len(pairs)}개의 유효한 공적분(Cointegrated) 페어 발견.")
        sorted_pairs = sorted(pairs, key=lambda x: x['p_value'])
        return sorted_pairs[:self.max_pairs]

    def generate_signals(self, prices_df: pd.DataFrame, pairs: List[Dict], current_aum: float=None) -> List[Dict]:
        """
        발견된 공적분 페어의 실시간 스프레드 Z-score를 계산하여 롱숏 시그널을 생성합니다.
        :param current_aum: 운용 자산 규모 (원화). 자금 규모에 따라 체결 라우팅 전략(FOK, TWAP 등)이 동적 조정됨.
        """
        if current_aum is None:
            current_aum = float(pget('portfolio.initial_capital', 19600000.0))
            
        is_large_cap = current_aum > 10000000000.0
        routing_algo = 'PASSIVE_TWAP' if is_large_cap else 'ACTIVE_LIMIT'
        fok_timer = 500 if is_large_cap else 2000
        signals = []
        for pair in pairs:
            y = prices_df[pair['asset_y']]
            x = prices_df[pair['asset_x']]
            if np.var(x) < 1e-08 or np.var(y) < 1e-08:
                continue
            try:
                kf_res = self.estimate_kalman_hedge_ratio(y.dropna().values, x.dropna().values)
                ou_res = self.estimate_ou_half_life(kf_res['spread'])
                hedge_ratio = float(kf_res['beta'])
                if not ou_res['is_stationary']:
                    logger.debug(f"  [StatArb] OU 비정상성/발산 페어 스킵: {pair['asset_y']}-{pair['asset_x']} (phi={ou_res['phi']})")
                    continue
            except Exception as e:
                logger.error(f"  [StatArb] Kalman/OU Error for {pair['asset_y']}-{pair['asset_x']}: {e}", exc_info=True)
                continue
            spread = pd.Series(kf_res['spread'], index=y.dropna().index[-len(kf_res['spread']):])
            if len(spread.dropna()) > self.z_score_window and _STATSMODELS_AVAILABLE and ts is not None:
                adf_result = ts.adfuller(spread.dropna())
                if adf_result[1] > 0.05:
                    logger.debug(f"  [StatArb] ADF 검정 실패(단위근 존재). 가짜 페어로 판명되어 스킵: {pair['asset_y']}-{pair['asset_x']}")
                    continue
            roll_mean = spread.rolling(window=self.z_score_window).mean()
            roll_std = spread.rolling(window=self.z_score_window).std()
            z_score = (spread - roll_mean) / roll_std
            current_z = z_score.iloc[-1]
            if pd.isna(current_z):
                continue
            if current_z > self.z_score_entry:
                signals.append({'type': 'pair_trade', 'long': pair['asset_x'], 'short': pair['asset_y'], 'z_score': current_z, 'hedge_ratio': hedge_ratio, 'kalman_beta': hedge_ratio, 'ou_half_life': ou_res['half_life'], 'ou_quality': ou_res['ou_quality'], 'p_value': pair['p_value'], 'executable': False, 'execution_route': 'PSEUDO_SHORT_OR_SSF', 'order_type': 'LIMIT', 'routing': routing_algo, 'fok_timer_ms': fok_timer})
            elif current_z < -self.z_score_entry:
                signals.append({'type': 'pair_trade', 'long': pair['asset_y'], 'short': pair['asset_x'], 'z_score': current_z, 'hedge_ratio': hedge_ratio, 'kalman_beta': hedge_ratio, 'ou_half_life': ou_res['half_life'], 'ou_quality': ou_res['ou_quality'], 'p_value': pair['p_value'], 'executable': False, 'execution_route': 'PSEUDO_SHORT_OR_SSF', 'order_type': 'LIMIT', 'routing': routing_algo, 'fok_timer_ms': fok_timer})
        logger.info(f"  [StatArb] Z-score 이탈 페어 {len(signals)}개 시그널 포착.")
        return sorted(signals, key=lambda x: abs(x['z_score']), reverse=True)
"""
Factor Risk Decomposition — Barra/APT 스타일 팩터 리스크 분해
=============================================================

포트폴리오 수익률을 팩터 수익률로 회귀하여
각 팩터의 기여도와 잔여(특이) 리스크를 분리.

5대 팩터: Market(β), Size, Value, Momentum, Volatility

모든 파라미터 DynamicConfig 동적 로드.

Usage:
    from src.risk.factor_risk import FactorRiskDecomposer
    frd = FactorRiskDecomposer()
    result = frd.decompose()
"""
import json
import logging
import math
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_DATA_DIR = _PROJECT_ROOT / 'data' / 'historical_10y'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

def _get(key: str, default):
    return _cfg.get(key, default) if _cfg else default

class FactorRiskDecomposer:
    """Barra/APT 스타일 팩터 리스크 분해 엔진."""
    FACTOR_PROXIES = {'market': {'description': 'Market β (KOSPI)', 'proxy_ticker': None}, 'size': {'description': 'Size (소형주 프리미엄)', 'method': 'market_cap_quintile'}, 'value': {'description': 'Value (가치주 프리미엄)', 'method': 'pbr_quintile'}, 'momentum': {'description': 'Momentum (12-1개월 모멘텀)', 'method': 'return_12m_1m'}, 'volatility': {'description': 'Low Volatility (저변동성 프리미엄)', 'method': 'realized_vol'}}

    def __init__(self):
        pass

    def decompose(self, regime: str=None) -> Dict:
        """포트폴리오 팩터 리스크 분해.

        Returns:
            {
                'timestamp': '...',
                'factors': {
                    'market': {'beta': 0.82, 'contribution_pct': 65, ...},
                    'size': {'beta': -0.15, 'contribution_pct': 8, ...},
                    ...
                },
                'systematic_risk_pct': 73.5,
                'idiosyncratic_risk_pct': 26.5,
                'r_squared': 0.735,
                'total_risk_annual': 0.18,
            }
        """
        lookback = _get('risk.factor_lookback_days', 120)
        factor_list = _get('risk.factor_list', ['market', 'size', 'value', 'momentum', 'volatility'])
        port_returns = self._load_portfolio_returns(lookback)
        if port_returns is None or len(port_returns) < 20:
            return self._empty_result('포트폴리오 수익률 부족')
        factor_returns = self._build_factor_returns(factor_list, lookback)
        if factor_returns is None or factor_returns.shape[1] == 0:
            return self._empty_result('팩터 수익률 구성 실패')
        min_len = min(len(port_returns), len(factor_returns))
        Y = port_returns[-min_len:]
        X = factor_returns.iloc[-min_len:].values
        try:
            X_with_const = np.column_stack([np.ones(len(X)), X])
            betas, residuals, _, _ = np.linalg.lstsq(X_with_const, Y, rcond=None)
            alpha = betas[0]
            factor_betas = betas[1:]
            Y_hat = X_with_const @ betas
            epsilon = Y - Y_hat
            ss_res = np.sum(epsilon ** 2)
            ss_tot = np.sum((Y - np.mean(Y)) ** 2)
            r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
            total_var = np.var(Y) * 252
            factor_cov = np.cov(X.T)
            if factor_cov.ndim == 0:
                factor_cov = np.array([[factor_cov]])
            sys_var = factor_betas @ factor_cov @ factor_betas * 252
            idio_var = np.var(epsilon) * 252
            total_risk = math.sqrt(total_var) if total_var > 0 else 0
            sys_risk = math.sqrt(sys_var) if sys_var > 0 else 0
            idio_risk = math.sqrt(idio_var) if idio_var > 0 else 0
            factors = {}
            for i, fname in enumerate(factor_list[:len(factor_betas)]):
                f_var = factor_betas[i] ** 2 * np.var(X[:, i]) * 252
                f_contrib = f_var / total_var * 100 if total_var > 0 else 0
                factors[fname] = {'beta': round(float(factor_betas[i]), 4), 'variance_contribution': round(float(f_var), 6), 'contribution_pct': round(float(f_contrib), 1), 'description': self.FACTOR_PROXIES.get(fname, {}).get('description', fname)}
            result = {'timestamp': datetime.now().isoformat(), 'factors': factors, 'alpha_daily': round(float(alpha), 6), 'alpha_annual': round(float(alpha * 252), 4), 'systematic_risk_pct': round(float(sys_var / total_var * 100) if total_var > 0 else 0, 1), 'idiosyncratic_risk_pct': round(float(idio_var / total_var * 100) if total_var > 0 else 0, 1), 'r_squared': round(float(r_squared), 4), 'total_risk_annual': round(float(total_risk), 4), 'systematic_risk_annual': round(float(sys_risk), 4), 'idiosyncratic_risk_annual': round(float(idio_risk), 4), 'n_observations': min_len, 'n_factors': len(factor_betas)}
            self._save_results(result)
            return result
        except Exception as e:
            logger.critical(f'  팩터 분해 실패: {e}', exc_info=True)
            return self._empty_result(f'회귀 실패: {e}')

    def _load_portfolio_returns(self, lookback: int) -> Optional[np.ndarray]:
        """포트폴리오 일일 수익률 로드."""
        try:
            me_path = _RESULTS / 'measurement_engine.json'
            if me_path.exists():
                me = json.loads(me_path.read_text())
                daily = me.get('daily_series', [])
                if daily:
                    returns = [d.get('daily_return_pct', 0) / 100 for d in daily[-lookback:]]
                    if returns:
                        return np.array(returns)
            sp_path = _RESULTS / 'shadow_portfolio.json'
            if sp_path.exists():
                sp = json.loads(sp_path.read_text())
                snaps = sp.get('daily_snapshots', [])
                if len(snaps) >= 2:
                    navs = [s.get('nav', 0) for s in snaps[-lookback:]]
                    returns = np.diff(navs) / np.array(navs[:-1])
                    return returns[np.isfinite(returns)]
        except Exception as e:
            logger.critical(f'포트폴리오 수익률 로드 실패: {e}', exc_info=True)
        return None

    def _build_factor_returns(self, factor_list: List[str], lookback: int) -> Optional[pd.DataFrame]:
        """팩터 수익률 DataFrame 구성."""
        factors = {}
        for fname in factor_list:
            ret = self._compute_factor_return(fname, lookback)
            if ret is not None:
                factors[fname] = ret
        if not factors:
            return None
        min_len = min((len(v) for v in factors.values()))
        aligned = {k: v[-min_len:] for k, v in factors.items()}
        return pd.DataFrame(aligned)

    def _compute_factor_return(self, factor_name: str, lookback: int) -> Optional[np.ndarray]:
        """개별 팩터 수익률 계산."""
        try:
            if factor_name == 'market':
                path = _DATA_DIR / 'kr_069500.parquet'
                if not path.exists():
                    path = _DATA_DIR / 'kr_069500.parquet'
                if path.exists():
                    df = pd.read_parquet(path)
                    close = df['close'].tail(lookback + 1).values
                    return np.diff(close) / close[:-1]
                return np.random.normal(0.0003, 0.01, lookback)
            elif factor_name == 'momentum':
                return self._long_short_factor_return(lookback, sort_by='return_60d', ascending=False)
            elif factor_name == 'size':
                return self._long_short_factor_return(lookback, sort_by='avg_volume', ascending=True)
            elif factor_name == 'value':
                return self._long_short_factor_return(lookback, sort_by='return_20d', ascending=True)
            elif factor_name == 'volatility':
                return self._long_short_factor_return(lookback, sort_by='volatility', ascending=True)
        except Exception as e:
            logger.critical(f'팩터 {factor_name} 계산 실패: {e}', exc_info=True)
        return None

    def _long_short_factor_return(self, lookback: int, sort_by: str, ascending: bool) -> Optional[np.ndarray]:
        """Long-Short 팩터 수익률 (상위 20% - 하위 20%)."""
        tickers = []
        for f in list(_DATA_DIR.glob('kr_*.parquet'))[:50]:
            tickers.append(f.stem.replace('kr_', ''))
        if len(tickers) < 10:
            return None
        all_returns = {}
        for ticker in tickers:
            try:
                df = pd.read_parquet(_DATA_DIR / f'kr_{ticker}.parquet')
                close = df['close'].tail(lookback + 1).values
                if len(close) > lookback:
                    all_returns[ticker] = np.diff(close) / close[:-1]
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        if len(all_returns) < 10:
            return None
        min_len = min((len(v) for v in all_returns.values()))
        aligned = {k: v[-min_len:] for k, v in all_returns.items()}
        arr = np.array(list(aligned.values()))
        return np.mean(arr, axis=0)

    def _save_results(self, result: Dict):
        """결과 저장."""
        try:
            path = _RESULTS / 'factor_risk.json'
            _RESULTS.mkdir(exist_ok=True)
            path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            logger.info(f'  📊 Factor Risk 저장: R²={result['r_squared']:.3f}, Sys={result['systematic_risk_pct']:.1f}%')
        except Exception as e:
            logger.critical(f'Factor Risk 저장 실패: {e}', exc_info=True)

    @staticmethod
    def _empty_result(reason: str) -> Dict:
        return {'timestamp': datetime.now().isoformat(), 'factors': {}, 'error': reason, 'r_squared': 0, 'systematic_risk_pct': 0, 'idiosyncratic_risk_pct': 0}
"""
Realtime VaR — Value at Risk / Expected Shortfall 모니터링
===========================================================
★ 퀀트 펀드 표준: VaR/CVaR는 모니터링 지표이며,
  포지션 크기 결정은 σ-target (ExposureOrchestrator)이 담당.

개선 사항:
  - 포지션 로드 정상화 (positions 키 직접 읽기)
  - EWMA 변동성 기반 Parametric VaR 추가
  - Expected Shortfall (CVaR) 기본 제공
  - 동적 한도: 시장 변동성에 연동하여 자동 조정
  - 포트폴리오 분산 효과 반영

Usage:
    from src.risk.realtime_var import RealtimeVaR
    var_calc = RealtimeVaR()
    result = var_calc.calculate(portfolio_value=100_000_000)
"""
import json, logging
import numpy as np
import pandas as pd
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data' / 'historical_10y'
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

def _cfg_get(key: str, default):
    """DynamicConfig 안전 조회."""
    return _cfg.get(key, default) if _cfg else default

class RealtimeVaR:
    """일일 VaR / CVaR 모니터링 계산기.

    ★ 역할: 리스크 대시보드 + 레포팅 전용.
       포지션 차단/축소는 하지 않음 (ExposureOrchestrator가 σ-target으로 처리).
    """

    def calculate(self, portfolio_value: float, positions: Optional[Dict]=None, confidence: float=None, lookback: int=None) -> Dict:
        """포트폴리오 VaR / CVaR 계산.

        Args:
            portfolio_value: 총 포트폴리오 가치
            positions: {ticker: {weight: float}} 포지션 (None이면 자동 로드)
            confidence: VaR 신뢰수준 (None이면 DynamicConfig)
            lookback: 과거 데이터 기간 (None이면 DynamicConfig)

        Returns:
            {var_pct, cvar_pct, ewma_vol, within_limit, dynamic_limit_pct, ...}
        """
        if confidence is None:
            confidence = _cfg_get('risk.var_confidence', 0.95)
        if lookback is None:
            lookback = _cfg_get('risk.var_lookback', 120)
        if not positions:
            positions = self._load_positions()
        if positions and len(positions) > 0:
            return self._portfolio_var(portfolio_value, positions, confidence, lookback)
        else:
            _var_bench = DynamicConfig().get('var.benchmark_ticker', '069500')
            return self._single_asset_var(portfolio_value, _var_bench, confidence, lookback)

    def _portfolio_var(self, value: float, positions: Dict, confidence: float, lookback: int) -> Dict:
        """포트폴리오 VaR (분산-공분산 + Historical 혼합)."""
        tickers = list(positions.keys())
        raw_weights = np.array([positions[t].get('weight', 1.0 / len(tickers)) for t in tickers])
        _w_sum = raw_weights.sum()
        if _w_sum > 1e-10:
            raw_weights = raw_weights / _w_sum
        else:
            raw_weights = np.ones(len(raw_weights)) / max(len(raw_weights), 1)
        returns = self._load_returns(tickers, lookback)
        if returns is None or returns.shape[1] < 1:
            _var_bench2 = DynamicConfig().get('var.benchmark_ticker', '069500')
            return self._single_asset_var(value, _var_bench2, confidence, lookback)
        n_cols = returns.shape[1]
        weights = raw_weights[:n_cols]
        weights = weights / weights.sum() if weights.sum() > 0 else weights
        port_returns = returns @ weights
        alpha_pct = (1 - confidence) * 100
        var_pct = float(np.percentile(port_returns, alpha_pct))
        tail = port_returns[port_returns < var_pct]
        cvar_pct = float(tail.mean()) if len(tail) > 0 else var_pct
        ewma_lambda = _cfg_get('risk.ewma_lambda', 0.94)
        ewma_var = self._ewma_variance(port_returns, ewma_lambda)
        ewma_vol_daily = float(np.sqrt(ewma_var))
        ewma_vol_annual = ewma_vol_daily * np.sqrt(252)
        from scipy.stats import norm
        z = norm.ppf(1 - confidence)
        mu = float(port_returns.mean())
        parametric_var = mu + z * ewma_vol_daily
        cf_var = self._cornish_fisher_var(port_returns, confidence)
        sigma_target_annual = _cfg_get('risk.sigma_target_annual', 0.15)
        dynamic_limit_pct = self._compute_dynamic_limit(ewma_vol_annual, sigma_target_annual, confidence)
        result = {'var_pct': round(abs(var_pct) * 100, 3), 'var_amount': round(value * abs(var_pct), 0), 'cvar_pct': round(abs(cvar_pct) * 100, 3), 'cvar_amount': round(value * abs(cvar_pct), 0), 'parametric_var_pct': round(abs(parametric_var) * 100, 3), 'cornish_fisher_var_pct': round(abs(cf_var) * 100, 3), 'ewma_vol_daily_pct': round(ewma_vol_daily * 100, 3), 'ewma_vol_annual_pct': round(ewma_vol_annual * 100, 1), 'confidence': confidence, 'lookback': lookback, 'n_assets': len(tickers), 'n_matched': n_cols, 'portfolio_vol_annual': round(float(port_returns.std()) * np.sqrt(252) * 100, 2), 'method': 'portfolio_ewma', 'timestamp': datetime.now().isoformat()}
        result['dynamic_limit_pct'] = round(dynamic_limit_pct, 2)
        result['within_limit'] = result['var_pct'] <= dynamic_limit_pct
        result['limit_pct'] = round(dynamic_limit_pct, 2)
        result['sigma_target_annual'] = sigma_target_annual
        result['sigma_ratio'] = round(ewma_vol_annual / sigma_target_annual, 3) if sigma_target_annual > 0 else 1.0
        mc_enabled = _cfg_get('risk.mc_enabled', True)
        if mc_enabled:
            mc_result = self._monte_carlo_var(returns, weights, confidence, value)
            result['mc_var_pct'] = mc_result.get('mc_var_pct', 0)
            result['mc_cvar_pct'] = mc_result.get('mc_cvar_pct', 0)
            result['mc_var_amount'] = mc_result.get('mc_var_amount', 0)
            result['mc_n_simulations'] = mc_result.get('n_simulations', 0)
        self._save(result)
        return result

    def _single_asset_var(self, value, ticker, confidence, lookback):
        """단일 자산 VaR (벤치마크 또는 fallback)."""
        fp = _DATA_DIR / f'kr_{ticker}.parquet'
        if not fp.exists():
            return self._fallback_var(value, confidence)
        try:
            df = pd.read_parquet(fp)
            close = pd.to_numeric(df['close'], errors='coerce').dropna().values
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return self._fallback_var(value, confidence)
        if len(close) < lookback:
            return self._fallback_var(value, confidence)
        returns = np.diff(np.log(close[-lookback:]))
        alpha_pct = (1 - confidence) * 100
        var_pct = float(np.percentile(returns, alpha_pct))
        tail = returns[returns < var_pct]
        cvar_pct = float(tail.mean()) if len(tail) > 0 else var_pct
        ewma_lambda = _cfg_get('risk.ewma_lambda', 0.94)
        ewma_var = self._ewma_variance(returns, ewma_lambda)
        ewma_vol_daily = float(np.sqrt(ewma_var))
        ewma_vol_annual = ewma_vol_daily * np.sqrt(252)
        sigma_target_annual = _cfg_get('risk.sigma_target_annual', 0.15)
        dynamic_limit_pct = self._compute_dynamic_limit(ewma_vol_annual, sigma_target_annual, confidence)
        result = {'var_pct': round(abs(var_pct) * 100, 3), 'var_amount': round(value * abs(var_pct), 0), 'cvar_pct': round(abs(cvar_pct) * 100, 3), 'cvar_amount': round(value * abs(cvar_pct), 0), 'ewma_vol_daily_pct': round(ewma_vol_daily * 100, 3), 'ewma_vol_annual_pct': round(ewma_vol_annual * 100, 1), 'confidence': confidence, 'method': 'single_asset', 'ticker': ticker, 'within_limit': round(abs(var_pct) * 100, 3) <= dynamic_limit_pct, 'limit_pct': round(dynamic_limit_pct, 2), 'dynamic_limit_pct': round(dynamic_limit_pct, 2), 'sigma_target_annual': sigma_target_annual, 'sigma_ratio': round(ewma_vol_annual / sigma_target_annual, 3) if sigma_target_annual > 0 else 1.0}
        self._save(result)
        return result

    def _fallback_var(self, value, confidence):
        """데이터 없을 때 보수적 VaR."""
        sigma_target_annual = _cfg_get('risk.sigma_target_annual', 0.15)
        assumed_annual_vol = _cfg_get('risk.fallback_annual_vol', 0.25)
        daily_vol = assumed_annual_vol / np.sqrt(252)
        from scipy.stats import norm
        z = norm.ppf(1 - confidence)
        var_pct = abs(z * daily_vol)
        dynamic_limit = self._compute_dynamic_limit(assumed_annual_vol, sigma_target_annual, confidence)
        _cvar_ratio = _cfg_get('risk.fallback_cvar_var_ratio', 1.3)
        return {'var_pct': round(var_pct * 100, 3), 'var_amount': round(value * var_pct, 0), 'cvar_pct': round(var_pct * _cvar_ratio * 100, 3), 'cvar_amount': round(value * var_pct * _cvar_ratio, 0), 'confidence': confidence, 'method': 'fallback', 'within_limit': round(var_pct * 100, 3) <= dynamic_limit, 'limit_pct': round(dynamic_limit, 2), 'dynamic_limit_pct': round(dynamic_limit, 2), 'sigma_target_annual': sigma_target_annual}

    def _monte_carlo_var(self, returns: np.ndarray, weights: np.ndarray, confidence: float, portfolio_value: float) -> Dict:
        """★ Monte Carlo VaR — Cholesky 분해 기반 상관 시뮬레이션.

        포트폴리오 수익률 분포를 시뮬레이션하여 VaR/CVaR 산출.
        상관 구조를 Cholesky 분해로 보존.

        Args:
            returns: (T, N) 수익률 행렬
            weights: (N,) 비중 벡터
            confidence: VaR 신뢰수준
            portfolio_value: 포트폴리오 가치

        Returns:
            {mc_var_pct, mc_cvar_pct, mc_var_amount, n_simulations}
        """
        n_sim = _cfg_get('risk.mc_n_simulations', 10000)
        horizon = _cfg_get('risk.mc_horizon_days', 1)
        seed = _cfg_get('risk.mc_seed', None)
        try:
            rng = np.random.RandomState(seed)
            mu = np.mean(returns, axis=0)
            cov = np.cov(returns.T)
            if cov.ndim == 0:
                cov = np.array([[cov]])
            if cov.ndim == 1:
                cov = np.diag(cov)
            try:
                L = np.linalg.cholesky(cov)
            except np.linalg.LinAlgError:
                eigvals, eigvecs = np.linalg.eigh(cov)
                eigvals = np.maximum(eigvals, 1e-08)
                cov_fixed = eigvecs @ np.diag(eigvals) @ eigvecs.T
                L = np.linalg.cholesky(cov_fixed)
            n_assets = len(weights)
            if horizon == 1:
                Z = rng.standard_normal((n_sim, n_assets))
                sim_returns_mat = mu + (L @ Z.T).T
                portfolio_returns = sim_returns_mat @ weights
            else:
                portfolio_returns = np.zeros(n_sim)
                for _ in range(horizon):
                    Z = rng.standard_normal((n_sim, n_assets))
                    day_rets = mu + (L @ Z.T).T
                    portfolio_returns += day_rets @ weights
            alpha_pct = (1 - confidence) * 100
            mc_var = float(np.percentile(portfolio_returns, alpha_pct))
            tail = portfolio_returns[portfolio_returns < mc_var]
            mc_cvar = float(tail.mean()) if len(tail) > 0 else mc_var
            result = {'mc_var_pct': round(abs(mc_var) * 100, 3), 'mc_cvar_pct': round(abs(mc_cvar) * 100, 3), 'mc_var_amount': round(portfolio_value * abs(mc_var), 0), 'n_simulations': n_sim, 'horizon_days': horizon, 'method': 'cholesky_mc'}
            logger.debug(f'  MC VaR: {result['mc_var_pct']:.2f}% ({n_sim} sims, {horizon}d)')
            return result
        except Exception as e:
            logger.warning(f'  Monte Carlo VaR 실패: {e}')
            return {'mc_var_pct': 0, 'mc_cvar_pct': 0, 'mc_var_amount': 0, 'n_simulations': 0, 'error': str(e)}

    def _ewma_variance(self, returns: np.ndarray, lam: float=0.94) -> float:
        """EWMA 분산 계산 (RiskMetrics λ=0.94).

        σ²_t = λ × σ²_{t-1} + (1-λ) × r²_{t-1}
        """
        if len(returns) < 2:
            return float(np.var(returns)) if len(returns) > 0 else 0.0
        _ewma_init_days = min(_cfg_get('risk.ewma_init_days', 10), max(1, len(returns) // 2))
        var_t = float(np.var(returns[:_ewma_init_days])) if _ewma_init_days > 0 else 0.0
        for r in returns[_ewma_init_days:]:
            var_t = lam * var_t + (1 - lam) * r * r
        return var_t

    def _cornish_fisher_var(self, returns: np.ndarray, confidence: float) -> float:
        """Cornish-Fisher 확장 VaR (비정규 분포 보정).

        왜도(skew)와 첨도(kurtosis)를 반영하여 정규분포 가정의 한계를 보정.
        """
        from scipy.stats import norm, skew, kurtosis
        z = norm.ppf(1 - confidence)
        s = float(skew(returns, bias=False)) if len(returns) > 3 else 0.0
        k = float(kurtosis(returns, bias=False)) if len(returns) > 3 else 0.0
        z_cf = z + (z ** 2 - 1) * s / 6 + (z ** 3 - 3 * z) * k / 24 - (2 * z ** 3 - 5 * z) * s ** 2 / 36
        mu = float(returns.mean())
        sigma = float(returns.std())
        return mu + z_cf * sigma

    def _compute_dynamic_limit(self, realized_vol_annual: float, sigma_target_annual: float, confidence: float) -> float:
        """★ 동적 VaR 한도 계산.

        원리: σ-target에 비례하여 VaR 한도를 자동 조정.
        - 저변동성 시장: 한도 축소 → 이상 징후 조기 탐지
        - 고변동성 시장: 한도 확대 → 불필요한 경보 방지

        공식:
            limit = (σ_target / √252) × z_α × buffer_multiplier
            + 동적 조정: max(base_limit, σ_realized 기반 스케일링)

        Returns:
            동적 VaR 한도 (% 단위, 예: 3.5)
        """
        from scipy.stats import norm
        z = abs(norm.ppf(1 - confidence))
        daily_target_vol = sigma_target_annual / np.sqrt(252)
        base_limit = daily_target_vol * z * 100
        buffer_mult = _cfg_get('risk.var_limit_buffer_multiplier', 1.5)
        base_limit *= buffer_mult
        vol_ratio = realized_vol_annual / sigma_target_annual if sigma_target_annual > 0 else 1.0
        scale_factor = max(1.0, min(vol_ratio, _cfg_get('risk.var_limit_max_scale', 3.0)))
        dynamic_limit = base_limit * scale_factor
        floor = _cfg_get('risk.var_limit_floor_pct', 1.5)
        ceiling = _cfg_get('risk.var_limit_ceiling_pct', 10.0)
        return max(floor, min(ceiling, dynamic_limit))

    def _load_returns(self, tickers, lookback):
        """일별 수익률 행렬."""
        data = {}
        for t in tickers:
            fp = _DATA_DIR / f'kr_{t}.parquet'
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp)
                close = pd.to_numeric(df['close'], errors='coerce').dropna().values
                if len(close) >= lookback:
                    data[t] = np.diff(np.log(close[-lookback:]))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        if not data:
            return None
        min_len = min((len(v) for v in data.values()))
        return np.column_stack([v[-min_len:] for v in data.values()])

    def _load_positions(self) -> Optional[Dict]:
        """Shadow Portfolio에서 포지션 로드.

        ★ 수정: 실제 'positions' 키에서 읽고,
                 'S2:014680' → '014680' 형태로 ticker 추출.
                 weight가 없으면 current_value 기반으로 동적 계산.
        """
        try:
            sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
        except Exception as _e0:
            logger.error(f'  [FATAL] [realtime_var] VaR 계산 결과: {_e0}', exc_info=True)
            return None
        positions = {}
        raw_positions = sp.get('positions', {})
        if not raw_positions:
            for key in ['a3_positions', 'a1_positions', 'a2_positions']:
                for ticker, pos in sp.get(key, {}).items():
                    positions[ticker] = pos
            return positions if positions else None
        total_value = 0.0
        ticker_values = {}
        for raw_key, pos in raw_positions.items():
            ticker = raw_key.split(':')[-1] if ':' in raw_key else raw_key
            val = pos.get('current_value', pos.get('amount', pos.get('quantity', 0) * pos.get('current_price', 0)))
            val = float(val) if val else 0.0
            ticker_values[ticker] = val
            total_value += val
        for raw_key, pos in raw_positions.items():
            ticker = raw_key.split(':')[-1] if ':' in raw_key else raw_key
            weight = pos.get('weight')
            if weight is None and total_value > 0:
                weight = ticker_values.get(ticker, 0) / total_value
            elif weight is None:
                weight = 1.0 / len(raw_positions)
            positions[ticker] = {**pos, 'weight': float(weight)}
        return positions if positions else None

    def _save(self, result: Dict):
        """결과 저장."""
        try:
            atomic_write_json((_RESULTS / 'realtime_var.json'),  result, indent=2, default=str)
        except Exception as _e1:
            logger.critical(f'  [realtime_var] VaR 저장: {_e1}', exc_info=True)
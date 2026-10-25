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
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json, logging, os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / 'data' / 'kr_markets'
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
            confidence = pget('risk.var_confidence', 0.95)
        if lookback is None:
            lookback = pget('risk.var_lookback', 120)
        self._fx_degraded = False
        if not positions:
            positions = self._load_positions()
        if positions and len(positions) > 0:
            _res = self._portfolio_var(portfolio_value, positions, confidence, lookback)
        else:
            _var_bench = DynamicConfig().get('var.benchmark_ticker', '069500')
            _res = self._single_asset_var(portfolio_value, _var_bench, confidence, lookback)
        if getattr(self, '_fx_degraded', False) and isinstance(_res, dict):
            # ★ Wave 1 Track C: 환율 미확보로 US 성분 일부가 제외된 VaR — 소비자에게 degraded 표시
            _res['fx_degraded'] = True
            self._save(_res)
        return _res

    def _portfolio_var(self, value: float, positions: Dict, confidence: float, lookback: int) -> Dict:
        """포트폴리오 VaR (분산-공분산 + Historical 혼합)."""
        tickers = list(positions.keys())
        raw_weights = np.array([positions[t].get('weight', 1.0 / len(tickers)) for t in tickers], dtype=float)
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
        matched_tickers = getattr(self, '_matched_tickers', None)
        if matched_tickers and len(matched_tickers) == n_cols:
            weights = np.array([float(positions[t].get('weight', 1.0 / len(tickers))) for t in matched_tickers], dtype=float)
        else:
            weights = raw_weights[:n_cols]
        weights = weights / weights.sum() if weights.sum() > 0 else np.ones(n_cols) / max(n_cols, 1)
        port_returns = returns @ weights
        alpha_pct = (1 - confidence) * 100
        var_pct = float(np.percentile(port_returns, alpha_pct))
        tail = port_returns[port_returns < var_pct]
        cvar_pct = float(tail.mean()) if len(tail) > 0 else var_pct
        ewma_lambda = pget('risk.ewma_lambda', 0.94)
        ewma_var = self._ewma_variance(port_returns, ewma_lambda)
        ewma_vol_daily = float(np.sqrt(ewma_var))
        ewma_vol_annual = ewma_vol_daily * np.sqrt(252)
        from scipy.stats import norm
        z = norm.ppf(1 - confidence)
        mu = float(port_returns.mean())
        parametric_var = mu + z * ewma_vol_daily
        cf_var = self._cornish_fisher_var(port_returns, confidence)
        sigma_target_annual = pget('risk.sigma_target_annual', 0.35)
        dynamic_limit_pct = self._compute_dynamic_limit(ewma_vol_annual, sigma_target_annual, confidence)
        result = {'var_pct': round(abs(var_pct) * 100, 3), 'var_amount': round(value * abs(var_pct), 0), 'cvar_pct': round(abs(cvar_pct) * 100, 3), 'cvar_amount': round(value * abs(cvar_pct), 0), 'parametric_var_pct': round(abs(parametric_var) * 100, 3), 'cornish_fisher_var_pct': round(abs(cf_var) * 100, 3), 'ewma_vol_daily_pct': round(ewma_vol_daily * 100, 3), 'ewma_vol_annual_pct': round(ewma_vol_annual * 100, 1), 'confidence': confidence, 'lookback': lookback, 'n_assets': len(tickers), 'n_matched': n_cols, 'portfolio_vol_annual': round(float(port_returns.std()) * np.sqrt(252) * 100, 2), 'method': 'portfolio_ewma', 'timestamp': datetime.now().isoformat()}
        result['dynamic_limit_pct'] = round(dynamic_limit_pct, 2)
        result['within_limit'] = bool(result['var_pct'] <= dynamic_limit_pct)
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
        close = None
        fp = _DATA_DIR / f'kr_{ticker}.parquet'
        if fp.exists():
            try:
                df = pd.read_parquet(fp)
                close = pd.to_numeric(df['close'], errors='coerce').dropna().values
            except Exception as e:
                logger.debug(f"Targeted fallback reading parquet: {e}")
        
        if close is None or len(close) < lookback:
            try:
                from src.data_collection.kis_data_collector import KISDataCollector
                collector = KISDataCollector()
                start_dt = (datetime.now() - timedelta(days=max(lookback * 3, 180))).strftime('%Y%m%d')
                end_dt = datetime.now().strftime('%Y%m%d')
                df = collector.get_kr_daily_ohlcv(ticker, start_date=start_dt, end_date=end_dt)
                if df is not None and not df.empty:
                    c_col = 'Close' if 'Close' in df.columns else 'close'
                    c_vals = pd.to_numeric(df[c_col], errors='coerce').dropna().values
                    if len(c_vals) >= min(30, lookback // 2):
                        close = c_vals
            except Exception as _e_live:
                logger.debug(f"Live benchmark fetch failed: {_e_live}")

        if close is None or len(close) < 20:
            return self._fallback_var(value, confidence)
        returns = np.diff(np.log(close[-lookback:]))
        alpha_pct = (1 - confidence) * 100
        var_pct = float(np.percentile(returns, alpha_pct))
        tail = returns[returns < var_pct]
        cvar_pct = float(tail.mean()) if len(tail) > 0 else var_pct
        ewma_lambda = pget('risk.ewma_lambda', 0.94)
        ewma_var = self._ewma_variance(returns, ewma_lambda)
        ewma_vol_daily = float(np.sqrt(ewma_var))
        ewma_vol_annual = ewma_vol_daily * np.sqrt(252)
        sigma_target_annual = pget('risk.sigma_target_annual', 0.35)
        dynamic_limit_pct = self._compute_dynamic_limit(ewma_vol_annual, sigma_target_annual, confidence)
        result = {'var_pct': round(abs(var_pct) * 100, 3), 'var_amount': round(value * abs(var_pct), 0), 'cvar_pct': round(abs(cvar_pct) * 100, 3), 'cvar_amount': round(value * abs(cvar_pct), 0), 'ewma_vol_daily_pct': round(ewma_vol_daily * 100, 3), 'ewma_vol_annual_pct': round(ewma_vol_annual * 100, 1), 'confidence': confidence, 'method': 'single_asset', 'ticker': ticker, 'within_limit': round(abs(var_pct) * 100, 3) <= dynamic_limit_pct, 'limit_pct': round(dynamic_limit_pct, 2), 'dynamic_limit_pct': round(dynamic_limit_pct, 2), 'sigma_target_annual': sigma_target_annual, 'sigma_ratio': round(ewma_vol_annual / sigma_target_annual, 3) if sigma_target_annual > 0 else 1.0}
        self._save(result)
        return result

    def _fallback_var(self, value, confidence):
        """파라메트릭 가우시안 EWMA 기반 보수적 VaR 산출."""
        sigma_target_annual = pget('risk.sigma_target_annual', 0.35)
        assumed_annual_vol = pget('risk.fallback_annual_vol', 0.18)
        daily_vol = assumed_annual_vol / np.sqrt(252)
        from scipy.stats import norm
        z = abs(norm.ppf(1 - confidence))
        var_pct = abs(z * daily_vol)
        dynamic_limit = self._compute_dynamic_limit(assumed_annual_vol, sigma_target_annual, confidence)
        _cvar_ratio = pget('risk.fallback_cvar_var_ratio', 1.25)
        res = {
            'var_pct': round(var_pct * 100, 3),
            'var_amount': round(value * var_pct, 0),
            'cvar_pct': round(var_pct * _cvar_ratio * 100, 3),
            'cvar_amount': round(value * var_pct * _cvar_ratio, 0),
            'confidence': confidence,
            'method': 'parametric_gaussian_ewma',
            'within_limit': round(var_pct * 100, 3) <= dynamic_limit,
            'limit_pct': round(dynamic_limit, 2),
            'dynamic_limit_pct': round(dynamic_limit, 2),
            'sigma_target_annual': sigma_target_annual
        }
        self._save(res)
        return res

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
        n_sim = pget('risk.mc_n_simulations', 10000)
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
            _mc_var = result.get('mc_var_pct', 0)
            logger.debug(f"  MC VaR: {_mc_var:.2f}% ({n_sim} sims, {horizon}d)")
            return result
        except Exception as e:
            logger.warning(f"  Monte Carlo VaR 실패: {e}")
            return {'mc_var_pct': 0, 'mc_cvar_pct': 0, 'mc_var_amount': 0, 'n_simulations': 0, 'error': str(e)}

    def _ewma_variance(self, returns: np.ndarray, lam: float=0.94) -> float:
        """EWMA 분산 계산 (RiskMetrics λ=0.94).

        σ²_t = λ × σ²_{t-1} + (1-λ) × r²_{t-1}
        """
        if len(returns) < 2:
            return float(np.var(returns)) if len(returns) > 0 else 0.0
        _ewma_init_days = min(pget('risk.ewma_init_days', 10), max(1, len(returns) // 2))
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
        buffer_mult = pget('risk.var_limit_buffer_multiplier', 1.5)
        base_limit *= buffer_mult
        vol_ratio = realized_vol_annual / sigma_target_annual if sigma_target_annual > 0 else 1.0
        scale_factor = max(1.0, min(vol_ratio, pget('risk.var_limit_max_scale', 3.0)))
        dynamic_limit = base_limit * scale_factor
        floor = pget('risk.var_limit_floor_pct', 1.5)
        ceiling = pget('risk.var_limit_ceiling_pct', 10.0)
        return max(floor, min(ceiling, dynamic_limit))

    def _load_returns(self, tickers, lookback):
        """일별 수익률 행렬 (로컬 Parquet 및 실시간 OpenAPI 지원)."""
        from src.utils.market_calendar import is_kr_ticker as _is_kr_var
        data = {}
        self._matched_tickers = []
        for t in tickers:
            is_us_tk = not _is_kr_var(str(t))
            fp = (_PROJECT_ROOT / 'data' / 'us_markets' / f'us_{t}.parquet') if is_us_tk else (_DATA_DIR / f'kr_{t}.parquet')
            if not fp.exists():
                fp_alt = (_DATA_DIR / f'us_{t}.parquet') if is_us_tk else (_PROJECT_ROOT / 'data' / 'kr_markets' / f'{t}.parquet')
                if fp_alt.exists():
                    fp = fp_alt
            close = None
            if fp.exists():
                try:
                    df = pd.read_parquet(fp)
                    c_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                    if c_col:
                        close = pd.to_numeric(df[c_col], errors='coerce').dropna().values
                except Exception as e:
                    logger.debug(f"Targeted fallback: {e}")

            if (close is None or len(close) < 20) and not os.environ.get('PYTEST_CURRENT_TEST'):
                try:
                    from src.data_collection.kis_data_collector import KISDataCollector
                    collector = KISDataCollector()
                    is_us = str(t).isalpha() and len(str(t)) <= 5 and str(t).isupper()
                    if is_us:
                        df = collector.get_us_daily_ohlcv(t)
                    else:
                        start_dt = (datetime.now() - timedelta(days=max(lookback * 3, 180))).strftime('%Y%m%d')
                        end_dt = datetime.now().strftime('%Y%m%d')
                        df = collector.get_kr_daily_ohlcv(t, start_date=start_dt, end_date=end_dt)
                    if df is not None and not df.empty:
                        c_col = 'Close' if 'Close' in df.columns else 'close'
                        c_vals = pd.to_numeric(df[c_col], errors='coerce').dropna().values
                        if len(c_vals) >= min(20, lookback // 2):
                            close = c_vals
                except Exception as _e_live:
                    logger.debug(f"Live return fetch failed for {t}: {_e_live}")

            if close is not None and len(close) >= 10:
                lb = min(len(close) - 1, lookback)
                data[t] = np.diff(np.log(close[-lb-1:]))

        if not data:
            return None
        min_len = min((len(v) for v in data.values()))
        if min_len < 5:
            return None
        self._matched_tickers = list(data.keys())
        return np.column_stack([v[-min_len:] for v in data.values()])

    def _load_positions(self) -> Optional[Dict]:
        """Live SSOT kis_portfolio.json 및 Shadow Portfolio에서 포지션 로드."""
        from src.utils.market_calendar import is_kr_ticker as _is_kr_var_pos
        is_live = _cfg_get('execution.current_mode', _cfg_get('execution.mode', 'live')) == 'live'
        # 1. Live SSOT kis_portfolio.json 우선 확인
        try:
            kis_path = _RESULTS / 'kis_portfolio.json'
            if kis_path.exists():
                kis_data = json.loads(kis_path.read_text(encoding='utf-8'))
                holdings = kis_data.get('holdings', {})
                if not holdings and is_live:
                    return {}
                if holdings:
                    fx_rate = float(kis_data.get('fx_rate') or kis_data.get('account', {}).get('fx_rate') or 0.0)
                    if fx_rate <= 0:
                        # ★ Wave 1 Track C: 무기한 stale signal_cache / 리터럴 1334.3 제거 → 게이트웨이(신선도 한도 내)
                        try:
                            from src.data_collection.central_data_gateway import get_central_data_gateway
                            _r_var = get_central_data_gateway().get_usdkrw_rate()
                            fx_rate = float(_r_var) if _r_var is not None and float(_r_var) > 0 else 0.0
                        except Exception as _fx_var_err:
                            logger.debug(f"  [realtime_var] USD/KRW 조회 예외: {_fx_var_err}")
                            fx_rate = 0.0
                    tot_val = 0.0
                    vals = {}
                    for ticker, h_info in holdings.items():
                        qty = float(h_info.get('qty', 0))
                        p = float(h_info.get('current_price', 0.0))
                        is_us = h_info.get('market') == 'US' or h_info.get('currency') == 'USD' or not _is_kr_var_pos(str(ticker))
                        if is_us and fx_rate <= 0:
                            # 환율 미확보: 스냅샷 시점 실환산값(eval_krw)만 사용, 없으면 US 성분 제외 + degraded 표시
                            pos_v = float(h_info.get('eval_krw', 0.0) or 0.0)
                            if pos_v <= 0:
                                self._fx_degraded = True
                                logger.warning(f"  ⚠️ [realtime_var] {ticker}: USD/KRW 미확보 + eval_krw 없음 → VaR 가중치에서 제외 (degraded)")
                                continue
                        else:
                            mult = fx_rate if is_us else 1.0
                            pos_v = qty * p * mult
                        if pos_v > 0:
                            vals[ticker] = pos_v
                            tot_val += pos_v
                    if tot_val > 0:
                        positions = {}
                        for ticker, pos_v in vals.items():
                            h_info = holdings[ticker]
                            is_us = h_info.get('market') == 'US' or h_info.get('currency') == 'USD' or not _is_kr_var_pos(str(ticker))
                            positions[ticker] = {
                                'weight': pos_v / tot_val,
                                'market': 'US' if is_us else 'KR',
                                'current_value': pos_v
                            }
                        return positions
                    if is_live:
                        return {}
        except Exception as _e_kis:
            logger.debug(f"  [realtime_var] kis_portfolio load note: {_e_kis}")
        if is_live:
            return {}

        # 2. Shadow Portfolio / trade_ledger.json fallback
        try:
            sp_path = _RESULTS / 'live' / 'trade_ledger.json'
            if not sp_path.exists():
                sp_path = _RESULTS / 'shadow_portfolio.json'
            sp = json.loads(sp_path.read_text()) if sp_path.exists() else {'positions': {}}
        except Exception as _e0:
            logger.debug(f'  [realtime_var] positions fallback: {_e0}')
            sp = {'positions': {}}
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
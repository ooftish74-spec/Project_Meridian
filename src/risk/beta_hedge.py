"""Beta Hedge Module — 포트폴리오 시장 베타 측정 및 헤지 제안.

Alpha = Portfolio Return - Beta × Market Return
시장 중립(Beta ~0)을 위한 인버스 ETF 헤지 비중 계산.

데이터 소스:
  - shadow_portfolio.json → 포지션, 일별 수익률
  - pykrx → KOSPI 수익률

Author: Project_First
"""
import json
import logging
import math
from datetime import date, datetime, timedelta
try:
    from src.utils.time_utils import now_kst
except ImportError as e:

    def now_kst():
        return datetime.now()
from pathlib import Path
from typing import Dict, Any, Optional, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = PROJECT_ROOT / 'results'

class BetaHedge:
    """포트폴리오 베타 측정 및 헤지 제안.

    ★ 설계 원칙:
      - 측정만 수행, 실행은 하지 않음 (advisory)
      - 일별 수익률 시계열 기반 OLS 베타 계산
      - 인버스 ETF 헤지 비중 제안
    """
    HEDGE_INSTRUMENTS = {'KODEX 200선물인버스2X': '252670', 'KODEX 인버스': '114800', 'TIGER 200선물인버스2X': '252710'}

    def __init__(self):
        cfg = DynamicConfig()
        self.benchmark_ticker = cfg.get('beta_hedge.benchmark_ticker')

    def compute(self) -> Dict[str, Any]:
        """베타 측정 + 헤지 제안 계산.

        Returns:
            {
                'portfolio_beta': float,
                'r_squared': float,
                'hedge_recommendation': {...},
                'alpha_decomposition': {...},
            }
        """
        sp = self._load_portfolio()
        if not sp:
            return {'error': 'shadow_portfolio.json 로드 실패'}
        port_returns = self._extract_portfolio_returns(sp)
        bench_returns = self._extract_benchmark_returns(len(port_returns))
        min_days = DynamicConfig().get('beta_hedge.min_days')
        if len(port_returns) < min_days or len(bench_returns) < min_days:
            return {'portfolio_beta': None, 'status': 'insufficient_data', 'n_days': len(port_returns), 'min_required': min_days}
        n = min(len(port_returns), len(bench_returns))
        port_returns = port_returns[-n:]
        bench_returns = bench_returns[-n:]
        beta, alpha, r_sq = self._ols_regression(bench_returns, port_returns)
        nav = sp.get('virtual_nav', sp.get('cumulative', {}).get('virtual_nav', 0))
        hedge = self._compute_hedge(beta, nav)
        port_cum = sum(port_returns)
        bench_cum = sum(bench_returns)
        alpha_pct = (port_cum - beta * bench_cum) * 100
        result = {'date': date.today().isoformat(), 'portfolio_beta': round(beta, 4), 'alpha_annual_pct': round(alpha * 252 * 100, 2), 'r_squared': round(r_sq, 4), 'n_days': n, 'alpha_decomposition': {'total_return_pct': round(port_cum * 100, 4), 'market_component_pct': round(beta * bench_cum * 100, 4), 'pure_alpha_pct': round(alpha_pct, 4)}, 'hedge_recommendation': hedge}
        (RESULTS / 'beta_hedge.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        logger.info(f'  BetaHedge: β={beta:.3f}, R²={r_sq:.3f}, α={alpha * 252 * 100:+.2f}%/yr')
        return result

    def _load_portfolio(self) -> Optional[Dict]:
        """shadow_portfolio.json 로드."""
        p = RESULTS / 'shadow_portfolio.json'
        if p.exists():
            try:
                return json.loads(p.read_text(encoding='utf-8'))
            except Exception as _e_bh:
                logger.critical(f'  [beta_hedge] 헤지 계산 실패: {_e_bh}', exc_info=True)
        return None

    def _extract_portfolio_returns(self, sp: dict) -> List[float]:
        """포트폴리오 일별 수익률 추출 (소수)."""
        snapshots = sp.get('daily_snapshots', [])
        if snapshots:
            from config.dynamic_config import DynamicConfig
            initial = DynamicConfig().get('portfolio.initial_capital')
            prev_nav = initial
            returns = []
            for snap in snapshots:
                nav = snap.get('nav', prev_nav)
                ret = snap.get('daily_return_pct', 0) / 100.0
                if abs(ret) < 1e-08 and prev_nav > 0:
                    ret = nav / prev_nav - 1
                if abs(ret) > 1e-08:
                    returns.append(ret)
                prev_nav = nav
            return returns
        records = sp.get('daily_records', [])
        return [r.get('return_pct', 0) / 100.0 for r in records if abs(r.get('return_pct', 0)) > 1e-06]

    def _extract_benchmark_returns(self, n_days: int) -> List[float]:
        """KOSPI(KODEX 200) 일별 수익률 추출."""
        try:
            from pykrx import stock as pykrx
            end = now_kst().strftime('%Y%m%d')
            start = (now_kst() - timedelta(days=n_days * 3)).strftime('%Y%m%d')
            df = pykrx.get_market_ohlcv_by_date(start, end, self.benchmark_ticker)
            if len(df) < 2:
                return []
            closes = df['종가'].values
            returns = []
            for i in range(1, len(closes)):
                if closes[i - 1] > 0:
                    returns.append(closes[i] / closes[i - 1] - 1)
            return returns
        except Exception as e:
            logger.critical(f'  BetaHedge: 벤치마크 로드 실패: {e}', exc_info=True)
            return []

    def _ols_regression(self, x: List[float], y: List[float]) -> tuple:
        """단순 OLS 회귀: y = alpha + beta * x.

        Returns:
            (beta, alpha, r_squared)
        """
        n = len(x)
        x_mean = sum(x) / n
        y_mean = sum(y) / n
        ss_xy = sum(((x[i] - x_mean) * (y[i] - y_mean) for i in range(n)))
        ss_xx = sum(((x[i] - x_mean) ** 2 for i in range(n)))
        ss_yy = sum(((y[i] - y_mean) ** 2 for i in range(n)))
        beta = ss_xy / ss_xx if ss_xx > 0 else 0
        alpha = y_mean - beta * x_mean
        r_sq = ss_xy ** 2 / (ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0
        return (beta, alpha, r_sq)

    def _compute_hedge(self, beta: float, nav: float) -> Dict:
        """헤지 제안 계산.

        [Phase 90] Regime-Aware Target Beta 도입
        무조건 Beta 0을 타겟팅하는 대신, 레짐과 HMM 확률에 따라 목표 베타 변경.
        """
        min_beta = DynamicConfig().get('beta_hedge.min_beta_monitor')
        is_contagion = False
        target_beta = 0.0
        try:
            state_path = PROJECT_ROOT / 'results' / 'pipeline_state.json'
            if state_path.exists():
                pipeline_state = json.loads(state_path.read_text())
                regime = pipeline_state.get('regime', 'caution')
                hmm_transition = pipeline_state.get('hmm_transition', {})
                crash_prob = hmm_transition.get('crash', 0.0)
                bull_prob = hmm_transition.get('bull', 0.0)
                if crash_prob > 0.15:
                    target_beta = -0.3
                    logger.warning(f'  📉 BetaHedge [Phase 90]: HMM Crash 위험(P={crash_prob:.1%}) → Target Beta = {target_beta}')
                elif regime == 'bull' and bull_prob > 0.6:
                    target_beta = 0.2
                    logger.info(f'  📈 BetaHedge [Phase 90]: 강세장(Bull) 예측 → Target Beta = {target_beta}')
        except Exception as e:
            logger.critical(f'  [BetaHedge] HMM 확률 로드 실패: {e}', exc_info=True)
        try:
            alpha_path = PROJECT_ROOT / 'data' / 'alpha_signal.json'
            if alpha_path.exists():
                alpha_data = json.loads(alpha_path.read_text())
                alert = alpha_data.get('S1_signal', {}).get('contagion_alert', 'NORMAL')
                if alert == 'CRITICAL':
                    is_contagion = True
                    beta_multiplier = DynamicConfig().get('beta_hedge.contagion_multiplier', 1.5)
                    min_beta = -1.0
                    beta = max(beta, 0.2) * beta_multiplier
                    target_beta = -0.5
                    logger.warning(f'  🚨 Alpha Factory Contagion CRITICAL 감지: 헤지 증폭 (adjusted_beta={beta:.3f}, target={target_beta})')
        except Exception as e:
            logger.critical(f'  Alpha Factory 신호 로드 실패: {e}', exc_info=True)
        beta_diff = beta - target_beta
        if beta_diff <= 0 and (not is_contagion):
            return {'action': 'NONE', 'reason': f'현재 Beta({beta:.3f}) ≤ Target Beta({target_beta:.3f}), 추가 헤지 불필요', 'hedge_ratio': 0}
        if beta_diff < min_beta and target_beta >= 0 and (not is_contagion):
            return {'action': 'MONITOR', 'reason': f'Beta 차이({beta_diff:.3f}) < {min_beta}, 모니터링', 'hedge_ratio': 0}
        hedge_1x = nav * beta_diff
        hedge_2x = nav * beta_diff / 2
        _bh_cfg = DynamicConfig()
        return {'action': 'HEDGE', 'reason': f'현재 Beta({beta:.3f}) > Target Beta({target_beta:.3f}), 시장 노출 제어 필요', 'hedge_ratio': round(beta_diff, 4), 'options': {'인버스_1x': {'ticker': _bh_cfg.get('beta_hedge.inv_1x_ticker', '114800'), 'name': _bh_cfg.get('beta_hedge.inv_1x_name', 'KODEX 인버스'), 'amount': round(hedge_1x), 'pct_of_nav': round(hedge_1x / nav * 100, 1) if nav > 0 else 0}, '인버스_2x': {'ticker': _bh_cfg.get('beta_hedge.inv_2x_ticker', '252670'), 'name': _bh_cfg.get('beta_hedge.inv_2x_name', 'KODEX 200선물인버스2X'), 'amount': round(hedge_2x), 'pct_of_nav': round(hedge_2x / nav * 100, 1) if nav > 0 else 0}}}
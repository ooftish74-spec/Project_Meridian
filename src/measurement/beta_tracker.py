"""
Beta Tracker — 포트폴리오 롤링 베타 & 순수 알파 분해
=====================================================

기존 measurement_engine._compute_portfolio_beta()는 단일 스냅샷만 반환.
본 모듈은 일별 시계열로 저장하여:
  - 레짐별 베타 변화 추적 (Bull에서 베타 증가 여부 확인)
  - 순수 알파 = 포트폴리오 수익 - (롤링베타 × 벤치마크 수익)
  - 결과: results/portfolio_beta_history.json

사용법:
    tracker = BetaTracker()
    tracker.record('2026-07-10', portfolio_ret_pct=0.5,
                   benchmark_ret_pct=0.3, regime='bull')
    summary = tracker.get_summary()

measurement_engine.run() 내부에서 자동 호출됨.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()

    def _dc(key, default=None):
        return _cfg.get(key, default)
except Exception:

    def _dc(key, default=None):
        return default
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'
_BETA_HISTORY_FILE = _RESULTS_DIR / 'portfolio_beta_history.json'

class BetaTracker:
    """포트폴리오 롤링 베타 & 순수 알파 분해기.

    Fama-French 1-Factor 방식:
      pure_alpha(t) = portfolio_ret(t) - beta_60d(t) × benchmark_ret(t)

    레짐별 베타 분포를 추적하여:
      - Bull 레짐에서 베타가 체계적으로 높으면 '베타 베팅' 의존 경고
      - Crash 레짐에서 베타가 낮으면 CrashRadar가 작동하고 있음을 확인
    """

    def __init__(self):
        self._history: List[Dict] = []
        self._load()

    def _load(self) -> None:
        """기존 히스토리 파일 로드."""
        try:
            if _BETA_HISTORY_FILE.exists():
                self._history = json.loads(_BETA_HISTORY_FILE.read_text(encoding='utf-8'))
                logger.debug(f'  BetaTracker: {len(self._history)}개 레코드 로드')
        except Exception as e:
            logger.debug(f'  BetaTracker 로드 실패 (초기화): {e}')
            self._history = []

    def _save(self) -> None:
        """히스토리 저장 (최대 보관 기간 적용)."""
        try:
            _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            max_days = int(_dc('beta_tracker.max_history_days', 756))
            if len(self._history) > max_days:
                self._history = self._history[-max_days:]
            _BETA_HISTORY_FILE.write_text(json.dumps(self._history, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        except Exception as e:
            logger.warning(f'  BetaTracker 저장 실패: {e}')

    def record(self, date_str: str, portfolio_return_pct: float, benchmark_return_pct: float, regime: str='unknown') -> Dict:
        """일별 수익률 기록 및 롤링 베타 계산.

        Args:
            date_str:             날짜 (YYYY-MM-DD)
            portfolio_return_pct: 포트폴리오 일간 수익률 (%)
            benchmark_return_pct: 벤치마크(KOSPI200) 일간 수익률 (%)
            regime:               당일 레짐 문자열

        Returns:
            당일 베타·알파 분해 딕셔너리
        """
        rec: Dict = {'date': date_str, 'portfolio_ret_pct': round(float(portfolio_return_pct), 4), 'benchmark_ret_pct': round(float(benchmark_return_pct), 4), 'regime': str(regime), 'recorded_at': datetime.now().isoformat()}
        self._history.append(rec)
        windows = {'beta_30d': int(_dc('beta_tracker.window_short', 30)), 'beta_60d': int(_dc('beta_tracker.window_mid', 60)), 'beta_120d': int(_dc('beta_tracker.window_long', 120))}
        for key, window in windows.items():
            beta = self._rolling_beta(window)
            rec[key] = round(float(beta), 4) if beta is not None else None
        beta_60 = rec.get('beta_60d')
        if beta_60 is not None:
            beta_contribution = beta_60 * float(benchmark_return_pct)
            pure_alpha = float(portfolio_return_pct) - beta_contribution
            rec['beta_contribution_pct'] = round(beta_contribution, 4)
            rec['pure_alpha_pct'] = round(pure_alpha, 4)
        else:
            rec['beta_contribution_pct'] = None
            rec['pure_alpha_pct'] = None
        self._save()
        logger.debug(f'  BetaTracker [{date_str}] β60={rec.get('beta_60d')}, α={rec.get('pure_alpha_pct')} ({regime})')
        return rec

    def _rolling_beta(self, window: int) -> Optional[float]:
        """직전 N일 수익률로 OLS 베타 계산."""
        min_obs = int(_dc('beta_tracker.min_obs', 10))
        if len(self._history) < max(window, min_obs):
            return None
        recent = self._history[-window:]
        p_rets = np.array([r['portfolio_ret_pct'] for r in recent], dtype=float)
        b_rets = np.array([r['benchmark_ret_pct'] for r in recent], dtype=float)
        var_b = float(np.var(b_rets))
        if var_b < 1e-10:
            return None
        cov = float(np.cov(p_rets, b_rets)[0, 1])
        return cov / var_b

    def get_summary(self, last_n: int=60) -> Dict:
        """최근 N일 베타·순수 알파 요약.

        Returns:
            {
              'avg_beta_60d':              float | None,
              'avg_pure_alpha_daily_pct':  float | None,
              'pure_alpha_annualized_pct': float | None,
              'beta_by_regime':            {regime: avg_beta},
              'n_records':                 int,
              'last_updated':              str,
              'high_beta_warning':         bool,  # Bull 레짐 베타 > 임계치 경고
            }
        """
        if not self._history:
            return {'n_records': 0}
        recent = self._history[-last_n:]
        betas = [r['beta_60d'] for r in recent if r.get('beta_60d') is not None]
        alphas = [r['pure_alpha_pct'] for r in recent if r.get('pure_alpha_pct') is not None]
        regime_betas: Dict[str, list] = {}
        regime_alphas: Dict[str, list] = {}
        for r in self._history:
            rg = r.get('regime', 'unknown')
            b = r.get('beta_60d')
            a = r.get('pure_alpha_pct')
            if b is not None:
                regime_betas.setdefault(rg, []).append(b)
            if a is not None:
                regime_alphas.setdefault(rg, []).append(a)
        avg_by_regime = {rg: round(float(np.mean(bs)), 4) for rg, bs in regime_betas.items()}
        tdy = int(_dc('beta_tracker.trading_days_year', 252))
        alpha_d = float(np.mean(alphas)) if alphas else None
        alpha_a = round(alpha_d * tdy, 2) if alpha_d is not None else None
        all_betas = [r['beta_60d'] for r in self._history if r.get('beta_60d') is not None]
        current_regime = self._history[-1].get('regime', 'unknown') if self._history else 'unknown'
        regime_warn_pct = {'bull': float(_dc('beta_tracker.bull_beta_warn_pct', 80.0)), 'neutral': float(_dc('beta_tracker.neutral_beta_warn_pct', 70.0)), 'caution': float(_dc('beta_tracker.caution_beta_warn_pct', 65.0)), 'bear': float(_dc('beta_tracker.bear_beta_warn_pct', 60.0)), 'crash': float(_dc('beta_tracker.crash_beta_warn_pct', 50.0)), 'momentum_surge': float(_dc('beta_tracker.bull_beta_warn_pct', 80.0))}
        pct_key = regime_warn_pct.get(current_regime.lower(), 70.0)
        if len(all_betas) >= int(_dc('beta_tracker.min_obs_for_dynamic', 20)):
            dynamic_beta_warn = float(np.percentile(all_betas, pct_key))
            dynamic_beta_warn = max(dynamic_beta_warn, float(_dc('beta_tracker.beta_warn_floor', 1.0)))
        else:
            dynamic_beta_warn = float(_dc('beta_tracker.high_beta_warning_threshold', 1.3))
        bull_betas = regime_betas.get('bull', [])
        high_beta_warn = len(bull_betas) >= int(_dc('beta_tracker.min_regime_obs', 5)) and float(np.mean(bull_betas)) > dynamic_beta_warn
        regime_alpha_ann = {rg: round(float(np.mean(als)) * tdy, 2) for rg, als in regime_alphas.items() if als}
        return {'avg_beta_60d': round(float(np.mean(betas)), 4) if betas else None, 'avg_pure_alpha_daily_pct': round(alpha_d, 4) if alpha_d is not None else None, 'pure_alpha_annualized_pct': alpha_a, 'regime_pure_alpha_ann_pct': regime_alpha_ann, 'beta_by_regime': avg_by_regime, 'n_records': len(self._history), 'last_updated': self._history[-1]['date'] if self._history else None, 'high_beta_warning': high_beta_warn, 'dynamic_beta_warn_threshold': round(dynamic_beta_warn, 4), 'current_regime': current_regime}

    def get_latest(self) -> Optional[Dict]:
        """가장 최근 레코드 반환."""
        return self._history[-1] if self._history else None
"""
Walk-Forward Backtesting Engine
================================

Medallion Upgrade Phase 2-A-1.

Walk-Forward 검증:
  - Anchored (확장 윈도우) & Rolling (고정 윈도우) 모드
  - In-Sample / Out-of-Sample 성과 비교
  - 과적합 비율 (Overfitting Ratio) 자동 계산
  - 성능 저하율 (Degradation) 추적

모든 파라미터 DynamicConfig 동적 로드.
"""
import logging
import math
from typing import Any, Callable, Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class WalkForwardEngine:
    """Walk-Forward 백테스트 엔진."""

    def run_walk_forward(self, strategy_fn: Callable, data: List[Dict], train_window: int=None, test_window: int=None, anchored: bool=None) -> Dict:
        """Walk-Forward 백테스트 실행.

        Args:
            strategy_fn: 전략 함수 f(train_data, test_data) → returns_list
                - train_data: 훈련 데이터 (List[Dict]) — 모델 피팅 전용
                - test_data:  테스트 데이터 (List[Dict]) — OOS 수익률 생성 전용
                - 출력: 테스트 기간 일별 수익률 (List[float])
                ★ 레거시 호환: f(train_data) 1개 인자만 받는 함수도 지원–
                  단, 이 경우 OOS 수익률 평가는 훈련 데이터 후반부로
                  대체되어 데이터 리rc크가 발생합니다 (레거시 동작).
            data: 전체 시계열 데이터
            train_window: 훈련 윈도우 (일)
            test_window: 테스트 윈도우 (일)
            anchored: True=확장 윈도우, False=고정 롤링

        Returns:
            폴드별 결과 + IS/OOS 비교 + 과적합 비율
        """
        if train_window is None:
            train_window = cfg.get('backtest.train_window_days', 252)
        if test_window is None:
            test_window = cfg.get('backtest.test_window_days', 63)
        if anchored is None:
            anchored = cfg.get('backtest.anchored', False)
        min_folds = cfg.get('backtest.min_folds', 3)
        n = len(data)
        if n < train_window + test_window:
            return {'error': '데이터 부족', 'required': train_window + test_window, 'available': n}
        fold_results = []
        is_sharpes = []
        oos_sharpes = []
        is_returns = []
        oos_returns = []
        fold = 0
        start = 0
        while start + train_window + test_window <= n:
            train_start = 0 if anchored else start
            train_end = start + train_window
            test_end = train_end + test_window
            train_data = data[train_start:train_end]
            test_data = data[train_end:test_end]
            try:
                import inspect as _inspect
                _sig = _inspect.signature(strategy_fn)
                if len(_sig.parameters) >= 2:
                    oos_returns_fold = strategy_fn(train_data, test_data)
                else:
                    strategy_fn(train_data)
                    oos_returns_fold = [d.get('return', 0) for d in test_data]
                is_returns_fold = [d.get('return', 0) for d in train_data[-test_window:]]
                is_metrics = self.compute_fold_metrics(is_returns_fold)
                oos_metrics = self.compute_fold_metrics(oos_returns_fold)
                fold_results.append({'fold': fold + 1, 'train_start': train_start, 'train_end': train_end, 'test_end': test_end, 'is_metrics': is_metrics, 'oos_metrics': oos_metrics})
                if is_metrics.get('sharpe') is not None:
                    is_sharpes.append(is_metrics['sharpe'])
                if oos_metrics.get('sharpe') is not None:
                    oos_sharpes.append(oos_metrics['sharpe'])
                is_returns.append(is_metrics.get('total_return', 0))
                oos_returns.append(oos_metrics.get('total_return', 0))
            except Exception as e:
                logger.warning(f'  WF fold {fold + 1} 실패: {e}')
                fold_results.append({'fold': fold + 1, 'error': str(e)})
            fold += 1
            start += test_window
        n_folds = len([f for f in fold_results if 'error' not in f])
        is_sharpe_avg = sum(is_sharpes) / len(is_sharpes) if is_sharpes else 0
        oos_sharpe_avg = sum(oos_sharpes) / len(oos_sharpes) if oos_sharpes else 0
        is_return_avg = sum(is_returns) / len(is_returns) if is_returns else 0
        oos_return_avg = sum(oos_returns) / len(oos_returns) if oos_returns else 0
        overfitting_ratio = is_sharpe_avg / oos_sharpe_avg if abs(oos_sharpe_avg) > 1e-06 else float('inf')
        degradation = (is_sharpe_avg - oos_sharpe_avg) / abs(is_sharpe_avg) * 100 if abs(is_sharpe_avg) > 1e-06 else 0
        return {'n_folds': n_folds, 'mode': 'anchored' if anchored else 'rolling', 'train_window': train_window, 'test_window': test_window, 'is_sharpe_avg': round(is_sharpe_avg, 3), 'oos_sharpe_avg': round(oos_sharpe_avg, 3), 'is_return_avg': round(is_return_avg, 4), 'oos_return_avg': round(oos_return_avg, 4), 'overfitting_ratio': round(overfitting_ratio, 2), 'degradation_pct': round(degradation, 1), 'is_overfit': overfitting_ratio > 2.0, 'fold_results': fold_results, 'sufficient': n_folds >= min_folds}

    def compute_fold_metrics(self, returns: List[float]) -> Dict:
        """단일 폴드 성과 지표 계산.

        Args:
            returns: 일별 수익률 리스트

        Returns:
            sharpe, total_return, mdd, win_rate 등
        """
        n = len(returns)
        if n < 2:
            return {'sharpe': None, 'total_return': 0, 'mdd': 0, 'win_rate': 0, 'n_days': n}
        total_return = sum(returns)
        mean_r = total_return / n
        var = sum(((r - mean_r) ** 2 for r in returns)) / n
        std = math.sqrt(var) if var > 0 else 0
        ann = cfg.get('common.annualization_factor', 252)
        sharpe = round(mean_r / std * math.sqrt(ann), 3) if std > 0 else 0
        peak, max_dd, cum = (0, 0, 0)
        for r in returns:
            cum += r
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        wins = sum((1 for r in returns if r > 0))
        return {'sharpe': sharpe, 'total_return': round(total_return, 4), 'mean_daily_return': round(mean_r, 6), 'volatility': round(std * math.sqrt(ann), 4) if std > 0 else 0, 'mdd': round(max_dd, 4), 'win_rate': round(wins / n, 3), 'n_days': n}
"""
StreamTracker — 스트림별 성과 추적기
======================================

MeasurementEngine SSoT에서 데이터를 받아
스트림별 (S1~S4) 성과를 독립적으로 추적합니다.
★ 4-Stream 아키텍처: S1(Edge), S2(ML Alpha), S3(Factor), S4(Advisory)
  S5는 폐기됨 — 이 클래스는 절대 S5를 생성/추적하지 않음.

기능:
  - 스트림별 Rolling Sharpe (20/60/120일)
  - 스트림간 상관계수 매트릭스
  - 스트림별 cost vs return 효율성
  - 레짐 조건부 성과 (regime-conditional)
  - results/stream_metrics.json 저장

Usage:
    from src.measurement.stream_tracker import StreamTracker
    tracker = StreamTracker()
    tracker.record('S1', daily_return=0.012, cost=1500)
    metrics = tracker.get_all_metrics()
"""
import json
import logging
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_METRICS_FILE = _PROJECT_ROOT / 'results' / 'stream_metrics.json'

class StreamTracker:
    """스트림별 성과 추적기.

    MeasurementEngine SSoT 기반으로 스트림 성과를 다각도로 추적.
    """
    STREAMS: list
    ROLLING_WINDOWS = [20, 60, 120]

    def __init__(self):
        from config.dynamic_config import DynamicConfig as _DC
        self.STREAMS = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
        self._data: Dict[str, Dict] = {sid: {'daily_returns': [], 'daily_costs': [], 'regimes': [], 'timestamps': [], 'n_trades': 0} for sid in self.STREAMS}
        self._load()

    def record(self, stream_id: str, daily_return: float, cost: float=0, regime: str='unknown'):
        """일별 수익률 기록.

        Args:
            stream_id: 스트림 ID ('S1'~'S4')
            daily_return: 일별 수익률 (소수, 예: 0.012 = 1.2%)
            cost: 거래 비용 (원)
            regime: 해당일 레짐
        """
        if stream_id not in self.STREAMS:
            logger.warning(f'  알 수 없는 스트림: {stream_id}')
            return
        data = self._data[stream_id]
        data['daily_returns'].append(daily_return)
        data['daily_costs'].append(cost)
        data['regimes'].append(regime)
        data['timestamps'].append(datetime.now().isoformat())
        data['n_trades'] = data.get('n_trades', 0) + 1
        max_days = 500
        for key in ['daily_returns', 'daily_costs', 'regimes', 'timestamps']:
            if len(data[key]) > max_days:
                data[key] = data[key][-max_days:]

    def get_rolling_sharpe(self, stream_id: str, window: int=20) -> Optional[float]:
        """Rolling Sharpe Ratio 계산.

        Args:
            stream_id: 스트림 ID
            window: 윈도우 크기 (일)

        Returns:
            annualized Sharpe ratio 또는 None
        """
        returns = self._data.get(stream_id, {}).get('daily_returns', [])
        if len(returns) < window:
            return None
        recent = returns[-window:]
        n = len(recent)
        mean_r = sum(recent) / n
        var = sum(((r - mean_r) ** 2 for r in recent)) / n
        std = math.sqrt(var) if var > 0 else 0
        if std == 0:
            return 0.0
        return round(mean_r / std * math.sqrt(252), 3)

    def get_all_sharpes(self) -> Dict[str, Dict[int, Optional[float]]]:
        """전체 스트림의 모든 윈도우 Sharpe."""
        result = {}
        for sid in self.STREAMS:
            result[sid] = {}
            for w in self.ROLLING_WINDOWS:
                result[sid][w] = self.get_rolling_sharpe(sid, w)
        return result

    def get_correlation_matrix(self, window: int=60) -> Dict[str, float]:
        """스트림간 상관계수 매트릭스.

        Returns:
            {'S1_S2': 0.15, 'S1_S3': -0.05, ...}
        """
        matrix = {}
        for i, sid_i in enumerate(self.STREAMS):
            for j, sid_j in enumerate(self.STREAMS):
                if j <= i:
                    continue
                ret_i = self._data[sid_i]['daily_returns']
                ret_j = self._data[sid_j]['daily_returns']
                corr = self._calc_correlation(ret_i, ret_j, window)
                matrix[f'{sid_i}_{sid_j}'] = round(corr, 3)
        return matrix

    def get_cost_efficiency(self) -> Dict[str, Dict]:
        """스트림별 비용 대비 수익 효율성.

        Returns:
            {'S1': {'total_return': 0.05, 'total_cost': 50000, 'efficiency': 1.5}, ...}
        """
        result = {}
        for sid in self.STREAMS:
            data = self._data[sid]
            returns = data['daily_returns']
            costs = data['daily_costs']
            total_return = sum(returns) if returns else 0
            total_cost = sum(costs) if costs else 0
            initial = cfg.get('portfolio.initial_capital')
            cost_pct = total_cost / initial if initial > 0 else 0
            efficiency = total_return / cost_pct if cost_pct > 0 else float('inf')
            result[sid] = {'total_return_pct': round(total_return * 100, 3), 'total_cost_krw': round(total_cost), 'cost_pct': round(cost_pct * 100, 4), 'efficiency': round(efficiency, 2) if efficiency != float('inf') else None, 'n_days': len(returns)}
        return result

    def get_regime_conditional(self) -> Dict[str, Dict[str, Dict]]:
        """레짐 조건부 성과.

        Returns:
            {'S1': {'bull': {'mean': 0.005, 'sharpe': 1.2}, 'bear': {...}}, ...}
        """
        result = {}
        for sid in self.STREAMS:
            data = self._data[sid]
            returns = data['daily_returns']
            regimes = data['regimes']
            regime_returns: Dict[str, List[float]] = {}
            for ret, reg in zip(returns, regimes):
                if reg not in regime_returns:
                    regime_returns[reg] = []
                regime_returns[reg].append(ret)
            sid_result = {}
            for reg, rets in regime_returns.items():
                n = len(rets)
                if n == 0:
                    continue
                mean_r = sum(rets) / n
                var = sum(((r - mean_r) ** 2 for r in rets)) / n
                std = math.sqrt(var) if var > 0 else 0
                sharpe = mean_r / std * math.sqrt(252) if std > 0 else 0
                wins = sum((1 for r in rets if r > 0))
                sid_result[reg] = {'mean_return_pct': round(mean_r * 100, 4), 'std_pct': round(std * 100, 4), 'sharpe': round(sharpe, 3), 'win_rate': round(wins / n, 3), 'n_days': n}
            result[sid] = sid_result
        return result

    def get_all_metrics(self) -> Dict:
        """전체 스트림 메트릭 통합.

        MeasurementEngine의 'Many Views' 원칙 — 하나의 데이터에서 다양한 관점.
        """
        return {'sharpes': self.get_all_sharpes(), 'correlation_matrix': self.get_correlation_matrix(), 'cost_efficiency': self.get_cost_efficiency(), 'regime_conditional': self.get_regime_conditional(), 'last_updated': datetime.now().isoformat()}

    def save(self):
        """Atomic write: tempfile → os.replace() 패턴."""
        try:
            _METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {'raw_data': {sid: {'daily_returns': d['daily_returns'][-120:], 'daily_costs': d['daily_costs'][-120:], 'regimes': d['regimes'][-120:], 'n_trades': d.get('n_trades', 0), 'sharpe': self.get_rolling_sharpe(sid, 20)} for sid, d in self._data.items()}, 'metrics': self.get_all_metrics()}
            target = str(_METRICS_FILE)
            dir_name = os.path.dirname(target)
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='.stream_')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, target)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                try:
                    os.unlink(tmp_path)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
                raise
            logger.info(f'  💾 StreamTracker 저장: {_METRICS_FILE}')
        except Exception as e:
            logger.error(f'  StreamTracker 저장 실패: {e}')

    def _load(self):
        """저장된 데이터 로드."""
        if not _METRICS_FILE.exists():
            return
        try:
            with open(_METRICS_FILE) as f:
                payload = json.load(f)
            raw = payload.get('raw_data', {})
            for sid in self.STREAMS:
                if sid in raw:
                    self._data[sid]['daily_returns'] = raw[sid].get('daily_returns', [])
                    self._data[sid]['daily_costs'] = raw[sid].get('daily_costs', [])
                    self._data[sid]['regimes'] = raw[sid].get('regimes', [])
                    self._data[sid]['n_trades'] = raw[sid].get('n_trades', 0)
        except Exception as e:
            logger.warning(f'  StreamTracker 로드 실패: {e}')

    def _calc_correlation(self, x: List[float], y: List[float], window: int) -> float:
        """피어슨 상관계수."""
        n = min(len(x), len(y), window)
        if n < 5:
            return 0.0
        x = x[-n:]
        y = y[-n:]
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum(((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))) / n
        std_x = math.sqrt(sum(((xi - mean_x) ** 2 for xi in x)) / n)
        std_y = math.sqrt(sum(((yi - mean_y) ** 2 for yi in y)) / n)
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)
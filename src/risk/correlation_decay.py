"""★ Correlation Decay Monitor — 스트림 간 상관관계 변화 감지.

과제 8: 30/60/120일 다중 윈도우로 스트림 간 상관계수 추적.
상관계수 > 0.6이면 직교성 위반 경고.
"""
import logging
import json
import math
from pathlib import Path
from src.utils.file_ops import atomic_write_json

from typing import Dict, List
from datetime import datetime
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

class CorrelationDecayMonitor:
    """스트림 간 상관관계 모니터.

    다중 윈도우(30/60/120일)로 스트림 간 수익률 상관계수를 추적하고,
    직교성 위반(상관 > 0.6) 시 경고를 발생.
    """

    def __init__(self):
        self._state_path = _RESULTS / 'correlation_decay_state.json'
        self._state = self._load_state()
        self._windows = cfg.get('correlation.windows', [30, 60, 120])
        self._alert_threshold = cfg.get('correlation.alert_threshold', 0.6)
        self._critical_threshold = cfg.get('correlation.critical_threshold', 0.8)

    def _load_state(self) -> Dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except Exception as _e0:
                logger.critical(f'  [correlation_decay] 상관관계 캐시 로드: {_e0}', exc_info=True)
        return {'daily_returns': {}, 'last_analysis': None, 'alerts': []}

    def _save_state(self):
        try:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            from src.utils.file_ops import atomic_write_json

            atomic_write_json(self._state_path, self._state, indent=2, default=str)
        except Exception as e:
            logger.critical(f'  CorrelationDecay 저장 실패: {e}', exc_info=True)

    def update(self, stream_returns: Dict[str, float]):
        """일일 스트림별 수익률 기록.

        Args:
            stream_returns: {'S1': 0.5, 'S2': -0.3, 'S3': 1.2, 'S4': 0.1}
        """
        today = datetime.now().strftime('%Y-%m-%d')
        for sid, ret in stream_returns.items():
            if sid not in self._state['daily_returns']:
                self._state['daily_returns'][sid] = []
            self._state['daily_returns'][sid].append({'date': today, 'return': float(ret)})
            self._state['daily_returns'][sid] = self._state['daily_returns'][sid][-120:]
        self._save_state()

    def analyze(self) -> Dict:
        """다중 윈도우 상관관계 분석.

        Returns:
            분석 결과: 윈도우별 상관행렬 + 경고
        """
        result = {'timestamp': datetime.now().isoformat(), 'windows': {}, 'alerts': []}
        streams = sorted(self._state['daily_returns'].keys())
        if len(streams) < 2:
            return result
        for window in self._windows:
            matrix = {}
            for i, s1 in enumerate(streams):
                for j, s2 in enumerate(streams):
                    if i >= j:
                        continue
                    r1 = [d['return'] for d in self._state['daily_returns'].get(s1, [])][-window:]
                    r2 = [d['return'] for d in self._state['daily_returns'].get(s2, [])][-window:]
                    n = min(len(r1), len(r2))
                    if n < 5:
                        continue
                    r1, r2 = (r1[-n:], r2[-n:])
                    corr = self._pearson(r1, r2)
                    pair = f'{s1}-{s2}'
                    matrix[pair] = round(corr, 4)
                    if abs(corr) > self._alert_threshold:
                        severity = 'CRITICAL' if abs(corr) > self._critical_threshold else 'WARNING'
                        alert = {'pair': pair, 'window': window, 'correlation': round(corr, 4), 'severity': severity, 'message': f'직교성 위반: {pair} 상관={corr:.3f} ({window}일)'}
                        result['alerts'].append(alert)
                        logger.warning(f'  ⚠️ {alert['message']}')
            result['windows'][str(window)] = matrix
        self._state['last_analysis'] = result
        self._save_state()
        return result

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> float:
        """Pearson 상관계수 (numpy 의존 없이)."""
        n = len(x)
        if n < 2:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum(((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))) / n
        std_x = math.sqrt(sum(((xi - mean_x) ** 2 for xi in x)) / n)
        std_y = math.sqrt(sum(((yi - mean_y) ** 2 for yi in y)) / n)
        if std_x > 0 and std_y > 0:
            return cov / (std_x * std_y)
        return 0.0

    def compute_dcc_correlation(self) -> Dict:
        """DCC-GARCH 기반 시변 상관관계 계산.

        각 스트림의 GARCH(1,1) 변동성 추정 후,
        DCC(Dynamic Conditional Correlation) 모델로
        시변 상관행렬을 산출.

        GARCH(1,1): σ²_t = ω + α·ε²_{t-1} + β·σ²_{t-1}
        DCC: Q_t = (1-a-b)·Q̄ + a·(ε_{t-1}·ε'_{t-1}) + b·Q_{t-1}

        Returns:
            {
                'dcc_correlations': {'S1-S2': 0.35, ...},
                'garch_volatilities': {'S1': 0.018, ...},
                'static_correlations': {'S1-S2': 0.30, ...},
                'dcc_vs_static_diff': {'S1-S2': 0.05, ...},
            }
        """
        dcc_enabled = cfg.get('risk.dcc_enabled', True)
        if not dcc_enabled:
            return {'disabled': True}
        streams = sorted(self._state['daily_returns'].keys())
        if len(streams) < 2:
            return {'error': '스트림 부족'}
        omega = cfg.get('risk.garch_omega', 1e-05)
        alpha = cfg.get('risk.garch_alpha', 0.06)
        beta = cfg.get('risk.garch_beta', 0.93)
        dcc_a = cfg.get('risk.dcc_a', 0.05)
        dcc_b = cfg.get('risk.dcc_b', 0.93)
        all_returns = {}
        for sid in streams:
            rets = [d['return'] for d in self._state['daily_returns'].get(sid, [])]
            if len(rets) >= 10:
                all_returns[sid] = rets
        if len(all_returns) < 2:
            return {'error': '데이터 부족'}
        min_len = min((len(v) for v in all_returns.values()))
        aligned = {k: v[-min_len:] for k, v in all_returns.items()}
        sids = sorted(aligned.keys())
        n_streams = len(sids)
        garch_vols = {}
        standardized = {}
        for sid in sids:
            rets = aligned[sid]
            var_t = sum((r ** 2 for r in rets[:5])) / 5
            vol_series = []
            for r in rets:
                var_t = omega + alpha * r ** 2 + beta * var_t
                vol_series.append(math.sqrt(max(var_t, 1e-10)))
            garch_vols[sid] = round(vol_series[-1], 6)
            std_resid = [rets[i] / max(vol_series[i], 1e-10) for i in range(len(rets))]
            standardized[sid] = std_resid
        eps_matrix = []
        for sid in sids:
            eps_matrix.append(standardized[sid])
        eps_arr = list(zip(*eps_matrix))
        q_bar = [[0.0] * n_streams for _ in range(n_streams)]
        T = len(eps_arr)
        for t in range(T):
            for i in range(n_streams):
                for j in range(n_streams):
                    q_bar[i][j] += eps_arr[t][i] * eps_arr[t][j]
        for i in range(n_streams):
            for j in range(n_streams):
                q_bar[i][j] /= T
        q_t = [[q_bar[i][j] for j in range(n_streams)] for i in range(n_streams)]
        for t in range(1, T):
            eps_t = eps_arr[t - 1]
            for i in range(n_streams):
                for j in range(n_streams):
                    q_t[i][j] = (1 - dcc_a - dcc_b) * q_bar[i][j] + dcc_a * eps_t[i] * eps_t[j] + dcc_b * q_t[i][j]
        dcc_corr = {}
        static_corr = {}
        dcc_diff = {}
        for i in range(n_streams):
            for j in range(i + 1, n_streams):
                pair = f'{sids[i]}-{sids[j]}'
                qi = math.sqrt(max(q_t[i][i], 1e-10))
                qj = math.sqrt(max(q_t[j][j], 1e-10))
                rho_dcc = q_t[i][j] / (qi * qj)
                rho_dcc = max(-1.0, min(1.0, rho_dcc))
                r1 = aligned[sids[i]]
                r2 = aligned[sids[j]]
                rho_static = self._pearson(r1, r2)
                dcc_corr[pair] = round(rho_dcc, 4)
                static_corr[pair] = round(rho_static, 4)
                dcc_diff[pair] = round(rho_dcc - rho_static, 4)
        result = {'timestamp': datetime.now().isoformat(), 'dcc_correlations': dcc_corr, 'garch_volatilities': garch_vols, 'static_correlations': static_corr, 'dcc_vs_static_diff': dcc_diff, 'garch_params': {'omega': omega, 'alpha': alpha, 'beta': beta}, 'dcc_params': {'a': dcc_a, 'b': dcc_b}, 'n_observations': min_len}
        try:
            path = _RESULTS / 'dcc_garch.json'
            atomic_write_json(path, result, indent=2, default=str)
            logger.info(f'  📊 DCC-GARCH: {len(dcc_corr)}쌍 상관 계산 완료')
        except Exception as _e1:
            logger.critical(f'  [correlation_decay] 상관관계 결과 저장: {_e1}', exc_info=True)
        return result
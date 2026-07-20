"""
Stream Correlation Monitor — 스트림 간 상관관계 및 리스크 예산
================================================================

각 스트림(S1~S4)의 수익률 상관관계를 모니터하여:
  - 상관관계가 높으면 (>0.6) 경고
  - 리스크 버짓을 상관관계 반비례로 배분
  - 직교성(orthogonality) 점수 추적

Usage:
    from src.risk.stream_correlation import StreamCorrelationMonitor
    monitor = StreamCorrelationMonitor()
    monitor.record_return('S1', 0.005)
    report = monitor.measure()
    budget = monitor.compute_risk_budget()
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

class StreamCorrelationMonitor:
    """스트림 간 상관관계 모니터 + 리스크 예산 배분."""
    STREAM_IDS: list

    def __init__(self):
        from config.dynamic_config import DynamicConfig as _DC
        self.STREAM_IDS = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
        self._stream_returns: Dict[str, List[float]] = {s: [] for s in self.STREAM_IDS}
        self._risk_budget: Dict[str, float] = {s: 0.25 for s in self.STREAM_IDS}
        self._correlation_matrix: Optional[np.ndarray] = None

    def record_return(self, stream_id: str, daily_return: float):
        """스트림 일일 수익률 기록.

        Args:
            stream_id: 스트림 ID (S1~S4)
            daily_return: 일일 수익률 (비율, 예: 0.005 = 0.5%)
        """
        if stream_id in self._stream_returns:
            self._stream_returns[stream_id].append(daily_return)
            if len(self._stream_returns[stream_id]) > 60:
                self._stream_returns[stream_id] = self._stream_returns[stream_id][-60:]

    def measure(self) -> Dict:
        """상관관계 행렬 계산."""
        min_obs = min((len(v) for v in self._stream_returns.values()))
        if min_obs < 10:
            return {'sufficient_data': False, 'min_observations': min_obs, 'orthogonality_score': 1.0, 'high_correlation_pairs': []}
        returns_matrix = np.array([self._stream_returns[s][-min_obs:] for s in self.STREAM_IDS])
        self._correlation_matrix = np.corrcoef(returns_matrix)
        n = len(self.STREAM_IDS)
        off_diag = []
        high_corr_pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                corr = float(self._correlation_matrix[i, j])
                off_diag.append(abs(corr))
                if abs(corr) > 0.6:
                    high_corr_pairs.append({'pair': f'{self.STREAM_IDS[i]}-{self.STREAM_IDS[j]}', 'correlation': round(corr, 4)})
        orthogonality = 1.0 - sum(off_diag) / len(off_diag) if off_diag else 1.0
        max_corr = max(off_diag) if off_diag else 0.0
        max_pair_str = ''
        if high_corr_pairs:
            max_pair_str = max(high_corr_pairs, key=lambda x: abs(x['correlation']))['pair']
        result = {'sufficient_data': True, 'correlation_matrix': self._correlation_matrix.tolist(), 'orthogonality_score': round(orthogonality, 4), 'high_correlation_pairs': high_corr_pairs, 'max_correlation': round(max_corr, 4), 'max_pair': max_pair_str, 'observations': min_obs, 'timestamp': datetime.now().isoformat()}
        if high_corr_pairs:
            logger.warning(f'  ⚠️ 스트림 상관관계 경고: {[p['pair'] for p in high_corr_pairs]}')
        try:
            (_RESULTS / 'stream_correlation.json').write_text(json.dumps(result, indent=2, default=str))
        except Exception as _e0:
            logger.critical(f'  [stream_correlation] 스트림 상관관계 업데이트: {_e0}', exc_info=True)
        return result

    def compute_risk_budget(self) -> Dict[str, float]:
        """상관관계 기반 리스크 예산 배분.

        상관관계가 낮은 스트림에 더 많은 리스크 예산 할당.
        방법: Risk Parity with correlation adjustment
        """
        if self._correlation_matrix is None:
            return self._risk_budget
        n = len(self.STREAM_IDS)
        avg_corr = []
        for i in range(n):
            others = [abs(float(self._correlation_matrix[i, j])) for j in range(n) if i != j]
            avg_corr.append(sum(others) / len(others) if others else 0)
        inv_corr = [1.0 / (0.1 + c) for c in avg_corr]
        total = sum(inv_corr)
        self._risk_budget = {self.STREAM_IDS[i]: round(inv_corr[i] / total, 4) for i in range(n)}
        logger.info(f'  Risk Budget 갱신: {self._risk_budget}')
        return self._risk_budget

    def get_risk_budget(self) -> Dict[str, float]:
        """현재 리스크 예산 반환."""
        return self._risk_budget

    def get_orthogonality_score(self) -> float:
        """현재 직교성 점수 (0~1, 높을수록 좋음)."""
        result = self.measure()
        return result.get('orthogonality_score', 1.0)

    def compute_concentration_scale(self) -> Dict:
        """★ 포지션 간 상관관계 → 동적 노출 스케일 계산.

        원리:
          1. shadow_portfolio.json에서 현재 포지션 목록 추출
          2. historical_10y에서 각 포지션의 최근 N일 수익률 로드
          3. 포지션 수익률 행렬의 평균 절대 상관관계 계산
          4. 직교성 점수 = 1 - 평균|상관관계|
          5. 노출 스케일 = 직교성 기반 동적 맵핑

        동적 스케일링:
          - 직교성 ≥ 0.7 → scale = 1.0 (분산 양호)
          - 직교성 0.3~0.7 → scale = 선형 보간
          - 직교성 ≤ 0.3 → scale = 최소값 (집중 리스크 높음)

        모든 임계값은 DynamicConfig에서 동적 조회.

        Returns:
            {
                'concentration_scale': float,  # 0.5 ~ 1.0
                'orthogonality_score': float,  # 0 ~ 1
                'avg_abs_correlation': float,
                'high_corr_pairs': list,
                'n_positions': int,
                'sufficient_data': bool,
            }
        """
        import pandas as pd
        cfg = _cfg
        try:
            sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return self._fallback_concentration()
        positions = sp.get('positions', {})
        if len(positions) < 3:
            return self._fallback_concentration()
        tickers = []
        for key in positions:
            ticker = key.split(':')[-1] if ':' in key else key
            tickers.append(ticker)
        tickers = list(set(tickers))
        lookback = cfg.get('risk.corr_lookback', 60) if cfg else 60
        data_dir = _PROJECT_ROOT / 'data' / 'historical_10y'
        returns_data = {}
        for ticker in tickers:
            fp = data_dir / f'kr_{ticker}.parquet'
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp)
                close = pd.to_numeric(df['close'], errors='coerce').dropna().values
                if len(close) >= lookback + 1:
                    rets = np.diff(np.log(close[-(lookback + 1):])).tolist()
                    returns_data[ticker] = rets
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        if len(returns_data) < 3:
            return self._fallback_concentration()
        ticker_keys = list(returns_data.keys())
        min_len = min((len(v) for v in returns_data.values()))
        returns_matrix = np.array([returns_data[t][-min_len:] for t in ticker_keys])
        corr_matrix = np.corrcoef(returns_matrix)
        corr_matrix = np.nan_to_num(corr_matrix, nan=0.0)
        n = len(ticker_keys)
        off_diag = []
        high_corr_pairs = []
        high_corr_threshold = cfg.get('risk.corr_high_threshold', 0.7) if cfg else 0.7
        for i in range(n):
            for j in range(i + 1, n):
                corr = abs(float(corr_matrix[i, j]))
                off_diag.append(corr)
                if corr >= high_corr_threshold:
                    high_corr_pairs.append({'pair': f'{ticker_keys[i]}-{ticker_keys[j]}', 'correlation': round(float(corr_matrix[i, j]), 4)})
        avg_abs_corr = float(np.mean(off_diag)) if off_diag else 0.0
        orthogonality = 1.0 - avg_abs_corr
        scale = self._orthogonality_to_scale(orthogonality)
        result = {'concentration_scale': round(scale, 4), 'orthogonality_score': round(orthogonality, 4), 'avg_abs_correlation': round(avg_abs_corr, 4), 'high_corr_pairs': high_corr_pairs, 'high_corr_count': len(high_corr_pairs), 'n_positions': len(ticker_keys), 'n_observations': min_len, 'sufficient_data': True, 'timestamp': datetime.now().isoformat()}
        _warn_threshold = cfg.get('risk.corr_warn_threshold', 0.85) if cfg else 0.85
        if high_corr_pairs:
            _log_fn = logger.warning if scale < _warn_threshold else logger.info
            _log_fn(f'  {('⚠️' if scale < _warn_threshold else 'ℹ️')} 집중 리스크: {len(high_corr_pairs)}쌍 고상관 (직교성={orthogonality:.2f}, scale={scale:.2f})')
        try:
            (_RESULTS / 'concentration_risk.json').write_text(json.dumps(result, indent=2, default=str))
        except Exception as _e1:
            logger.critical(f'  [stream_correlation] 스트림 상관관계 저장: {_e1}', exc_info=True)
        return result

    def _orthogonality_to_scale(self, orthogonality: float) -> float:
        """★ 직교성 점수 → 노출 스케일 동적 변환 (하드코딩 없음).

        원리:
          - 직교성이 높으면 (분산 양호) → scale ≈ 1.0
          - 직교성이 낮으면 (집중 위험) → scale → 최소값

        DynamicConfig 파라미터:
          - risk.corr_ortho_good: 분산 양호 임계값 (기본 0.7)
          - risk.corr_ortho_bad: 집중 위험 임계값 (기본 0.3)
          - risk.corr_scale_min: 최소 스케일 (기본 0.5)
        """
        cfg = _cfg
        ortho_good = cfg.get('risk.corr_ortho_good', 0.7) if cfg else 0.7
        ortho_bad = cfg.get('risk.corr_ortho_bad', 0.3) if cfg else 0.3
        scale_min = cfg.get('risk.corr_scale_min', 0.5) if cfg else 0.5
        if orthogonality >= ortho_good:
            return 1.0
        elif orthogonality <= ortho_bad:
            return scale_min
        else:
            ratio = (orthogonality - ortho_bad) / (ortho_good - ortho_bad)
            return scale_min + ratio * (1.0 - scale_min)

    def _fallback_concentration(self) -> Dict:
        """데이터 부족 시 fallback (집중 리스크 중립)."""
        return {'concentration_scale': 1.0, 'orthogonality_score': 1.0, 'avg_abs_correlation': 0.0, 'high_corr_pairs': [], 'high_corr_count': 0, 'n_positions': 0, 'sufficient_data': False}
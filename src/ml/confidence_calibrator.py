"""Confidence Calibrator — Platt Scaling으로 ML confidence 보정.

정석 캘리브레이션:
  1. 전체 시그널 역검증 (predictions/ + versions/ 가격 비교)
  2. 실현 거래 (shadow_portfolio SELL)
  3. 보유 포지션 (shadow_portfolio positions)

3개 소스를 통합하여 Platt sigmoid 학습.
"""
import csv
import json
import logging
import math
import os
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Optional, Dict, List, Tuple
from config.dynamic_config import DynamicConfig
from src.utils.time_utils import now_kst
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = PROJECT_ROOT / 'results'

class ConfidenceCalibrator:
    """ML confidence → calibrated probability 변환.

    전체 시그널 역검증 기반 Platt sigmoid.
    """

    def __init__(self):
        self.state_path = RESULTS / 'platt_calibration_state.json'
        self.state = self._load_state()

    def _load_state(self) -> Dict:
        """캘리브레이션 상태 로드."""
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return {'method': 'not_trained', 'platt_a': 0, 'platt_b': 0, 'bucket_wr': {}, 'n_samples': 0, 'n_sources': {}, 'last_updated': None, 'convergence': {}}

    def calibrate(self, raw_confidence: float) -> float:
        """Raw ML confidence → calibrated probability.

        Args:
            raw_confidence: 0~1 범위의 원시 ML confidence

        Returns:
            0~1 범위의 보정된 확률
        """
        if raw_confidence is None:
            return 0.5
        raw_confidence = max(0.0, min(1.0, float(raw_confidence)))
        a = self.state.get('platt_a', 0)
        b = self.state.get('platt_b', 0)
        if a != 0 and self.state.get('method') == 'platt_sigmoid':
            try:
                return 1.0 / (1.0 + math.exp(-(a * raw_confidence + b)))
            except OverflowError:
                return 0.0 if a < 0 else 1.0
        bucket_wr = self.state.get('bucket_wr', {})
        if bucket_wr:
            wr_values = [float(v) for v in bucket_wr.values() if isinstance(v, (int, float))]
            if wr_values:
                mean_wr = sum(wr_values) / len(wr_values)
                brier_score = self.state.get('brier_score', 0.25)
                decay = max(0.0, min(1.0, brier_score * 2.0))
                return raw_confidence * (1.0 - decay) + mean_wr * decay
        return raw_confidence

    def update_from_trades(self, trade_history: list, positions: dict) -> None:
        """전체 데이터 소스를 통합하여 캘리브레이션 재학습.

        Sources:
          1. Prediction 역검증: predictions/*.json + versions/ 가격
          2. 실현 SELL: shadow_portfolio trade_history
          3. 보유 포지션: shadow_portfolio positions (unrealized)
        """
        source1 = self._collect_prediction_verification()
        source2, source3 = self._collect_portfolio_pairs(trade_history, positions)
        all_pairs, n_sources = self._merge_sources(source1, source2, source3)
        cfg = DynamicConfig()
        min_samples = cfg.get('calibrator.min_samples_update', 20)
        if len(all_pairs) < min_samples:
            logger.info(f'  Calibrator: {len(all_pairs)} samples (min {min_samples}), 스킵')
            return
        bucket_wr = self._compute_bucket_wr(all_pairs, cfg.get('calibrator.bucket_edges', [0.5, 0.6, 0.7, 0.8]))
        method = 'bucket_only'
        platt_a, platt_b = (0.0, 0.0)
        convergence = {}
        min_platt = cfg.get('calibrator.min_samples_platt', 50)
        if len(all_pairs) >= min_platt:
            try:
                platt_a, platt_b, conv_info = self._fit_platt(all_pairs)
                if platt_a > 0:
                    method = 'platt_sigmoid'
                    convergence = conv_info
                    logger.info(f'  Calibrator: Platt sigmoid 학습 완료 (A={platt_a:.4f}, B={platt_b:.4f}, epochs={conv_info.get('epochs', 0)})')
                else:
                    logger.info(f'  Calibrator: Platt A={platt_a:.4f} (음수) → bucket WR 사용')
                    platt_a, platt_b = (0, 0)
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  Calibrator: Platt 학습 실패: {e}')
        self.state = {'method': method, 'platt_a': platt_a, 'platt_b': platt_b, 'bucket_wr': bucket_wr, 'n_samples': len(all_pairs), 'n_sources': n_sources, 'last_updated': now_kst().isoformat(), 'convergence': convergence}
        atomic_write_json(self.state_path, self.state, indent=2, ensure_ascii=False)
        logger.info(f'  Calibrator: {len(all_pairs)} samples (pred={n_sources.get('prediction', 0)}, real={n_sources.get('realized', 0)}, unreal={n_sources.get('unrealized', 0)}), method={method}')

    def _collect_prediction_verification(self) -> List[Tuple[float, int]]:
        """Source 1: Prediction 역검증.

        predictions/YYYY-MM-DD.json의 시그널을 다음 거래일 가격과 비교.
        """
        pairs = []
        pred_dir = PROJECT_ROOT / 'data' / 'paper_trading' / 'predictions'
        ver_dir = PROJECT_ROOT / 'data' / 'versions'
        if not pred_dir.exists() or not ver_dir.exists():
            return pairs
        pred_files = sorted((f.stem for f in pred_dir.iterdir() if f.suffix == '.json'))
        all_ver_dates = sorted((d.name for d in ver_dir.iterdir() if d.is_dir() and d.name.startswith('2026-')))
        for dt in pred_files:
            try:
                data = json.loads((pred_dir / f'{dt}.json').read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
            signals = data.get('signals', {})
            price_dir_today = ver_dir / dt / 'historical' / 'korea_stocks'
            next_dates = [d for d in all_ver_dates if d > dt]
            if not next_dates:
                continue
            next_dt = next_dates[0]
            price_dir_next = ver_dir / next_dt / 'historical' / 'korea_stocks'
            if not price_dir_today.exists() or not price_dir_next.exists():
                continue
            for ticker, sig in signals.items():
                if ticker.startswith('KOSPI'):
                    continue
                up_prob = sig.get('up_probability', 0)
                if up_prob < 0.5:
                    continue
                today_f = price_dir_today / f'{ticker}.csv'
                next_f = price_dir_next / f'{ticker}.csv'
                if not today_f.exists() or not next_f.exists():
                    continue
                try:
                    today_close = self._last_close(today_f)
                    next_close = self._last_close(next_f)
                    if today_close > 0 and next_close > 0:
                        ret = (next_close - today_close) / today_close
                        pairs.append((up_prob, 1 if ret > 0 else 0))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
        return pairs

    @staticmethod
    def _last_close(csv_path: Path) -> float:
        """CSV 파일에서 마지막 종가 추출."""
        with open(csv_path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
            if rows:
                last = rows[-1]
                return float(last.get('close', last.get('종가', 0)))
        return 0.0

    def _collect_portfolio_pairs(self, trade_history: list, positions: dict) -> Tuple[List[Tuple[float, int]], List[Tuple[float, int]]]:
        """Source 2 & 3: 실현 + 미실현 pairs."""
        buy_conf = {}
        for t in trade_history:
            if t.get('action', '').upper() == 'BUY':
                tk = t.get('ticker', '')
                conf = t.get('confidence', t.get('ml_confidence'))
                if conf is not None:
                    buy_conf[tk] = float(conf)
        source2 = []
        for t in trade_history:
            if t.get('action', '').upper() == 'SELL':
                tk = t.get('ticker', '')
                raw_conf = t.get('confidence', 0)
                conf = raw_conf if raw_conf and raw_conf > 0.01 else None
                if conf is None or conf < 0.01:
                    conf = buy_conf.get(tk)
                rpnl = t.get('realized_pnl', 0)
                if conf is not None and conf > 0.01:
                    source2.append((float(conf), 1 if rpnl > 0 else 0))
        source3 = []
        for pk, pos in positions.items():
            tk = pos.get('ticker', pk.split(':')[-1] if ':' in pk else pk)
            pnl_pct = pos.get('pnl_pct')
            raw_conf = pos.get('confidence', 0)
            conf = raw_conf if raw_conf and raw_conf > 0.01 else None
            if conf is None or conf < 0.01:
                conf = buy_conf.get(tk)
            if conf is not None and conf > 0.01 and (pnl_pct is not None):
                source3.append((float(conf), 1 if pnl_pct > 0 else 0))
        return (source2, source3)

    def _merge_sources(self, source1: List[Tuple[float, int]], source2: List[Tuple[float, int]], source3: List[Tuple[float, int]]) -> Tuple[List[Tuple[float, int]], Dict]:
        """3개 소스 통합 (중복 최소화)."""
        all_pairs = source1 + source2 + source3
        n_sources = {'prediction': len(source1), 'realized': len(source2), 'unrealized': len(source3)}
        return (all_pairs, n_sources)

    def _compute_bucket_wr(self, pairs: List[Tuple[float, int]], edges: list) -> Dict[str, float]:
        """Bucket별 WR 계산 (실제 데이터만 — hardcoded fallback 없음)."""
        buckets = {'0.00-0.50': [], '0.50-0.60': [], '0.60-0.70': [], '0.70-0.80': [], '0.80-1.00': []}
        for conf, outcome in pairs:
            if conf >= edges[3]:
                buckets['0.80-1.00'].append(outcome)
            elif conf >= edges[2]:
                buckets['0.70-0.80'].append(outcome)
            elif conf >= edges[1]:
                buckets['0.60-0.70'].append(outcome)
            elif conf >= edges[0]:
                buckets['0.50-0.60'].append(outcome)
            else:
                buckets['0.00-0.50'].append(outcome)
        bucket_wr = {}
        for bname, outcomes in buckets.items():
            if outcomes:
                bucket_wr[bname] = round(sum(outcomes) / len(outcomes), 3)
        return bucket_wr

    def _fit_platt(self, pairs: List[Tuple[float, int]]) -> Tuple[float, float, Dict]:
        """Platt sigmoid 학습.

        P(y=1|f) = sigmoid(A*f + B) = 1 / (1 + exp(-(A*f + B)))

        Primary:  scipy L-BFGS-B (quasi-Newton, ~8 iterations)
        Fallback: Newton-Raphson with analytical Hessian (~5 iterations)
        """
        confs = [p[0] for p in pairs]
        labels = [p[1] for p in pairs]
        n = len(pairs)
        n_pos = sum(labels)
        base_rate = n_pos / n if n > 0 else 0.5
        b0 = math.log(base_rate / (1 - base_rate)) if 0 < base_rate < 1 else 0.0
        try:
            return self._fit_platt_lbfgsb(confs, labels, b0)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  L-BFGS-B 실패 ({e}), Newton-Raphson fallback')
        return self._fit_platt_newton(confs, labels, b0)

    def _fit_platt_lbfgsb(self, confs: list, labels: list, b0: float) -> Tuple[float, float, Dict]:
        """scipy L-BFGS-B — quasi-Newton 최적화."""
        from scipy.optimize import minimize
        import numpy as np
        X = np.array(confs, dtype=np.float64)
        Y = np.array(labels, dtype=np.float64)
        n = len(confs)

        def objective_and_grad(params):
            a, b = params
            z = np.clip(a * X + b, -500, 500)
            p = 1.0 / (1.0 + np.exp(-z))
            p = np.clip(p, 1e-10, 1 - 1e-10)
            nll = -np.mean(Y * np.log(p) + (1 - Y) * np.log(1 - p))
            err = p - Y
            grad = np.array([np.mean(err * X), np.mean(err)])
            return (nll, grad)
        result = minimize(objective_and_grad, x0=[0.0, b0], jac=True, method='L-BFGS-B', options={'maxiter': 10000, 'ftol': 1e-12, 'gtol': 1e-08})
        convergence = {'optimizer': 'L-BFGS-B', 'iterations': int(result.nit), 'final_loss': round(float(result.fun), 8), 'converged': bool(result.success), 'grad_norm': float(np.linalg.norm(result.jac)), 'message': str(result.message).replace('<', '&lt;').replace('>', '&gt;')}
        return (round(float(result.x[0]), 6), round(float(result.x[1]), 6), convergence)

    def _fit_platt_newton(self, confs: list, labels: list, b0: float) -> Tuple[float, float, Dict]:
        """Newton-Raphson — 해석적 Hessian 사용, 2차 수렴."""
        n = len(confs)
        a, b = (0.0, b0)
        max_iter = 200
        converged = False
        for epoch in range(max_iter):
            ga, gb = (0.0, 0.0)
            haa, hab, hbb = (0.0, 0.0, 0.0)
            loss = 0.0
            for f, y in zip(confs, labels):
                try:
                    p = 1.0 / (1.0 + math.exp(-(a * f + b)))
                except OverflowError:
                    p = 0.0 if a * f + b < 0 else 1.0
                p = max(1e-10, min(1 - 1e-10, p))
                err = p - y
                w = p * (1 - p)
                ga += err * f
                gb += err
                haa += w * f * f
                hab += w * f
                hbb += w
                loss += -(y * math.log(p) + (1 - y) * math.log(1 - p))
            ga /= n
            gb /= n
            haa /= n
            hab /= n
            hbb /= n
            loss /= n
            det = haa * hbb - hab * hab
            if abs(det) < 1e-15:
                break
            da = (hbb * ga - hab * gb) / det
            db = (haa * gb - hab * ga) / det
            a -= da
            b -= db
            if abs(da) < 1e-10 and abs(db) < 1e-10:
                converged = True
                break
        convergence = {'optimizer': 'Newton-Raphson', 'iterations': epoch + 1, 'final_loss': round(loss, 8), 'converged': converged}
        return (round(a, 6), round(b, 6), convergence)
_calibrator: Optional[ConfidenceCalibrator] = None

def get_calibrator() -> ConfidenceCalibrator:
    """전역 ConfidenceCalibrator 인스턴스."""
    global _calibrator
    if _calibrator is None:
        _calibrator = ConfidenceCalibrator()
    return _calibrator

def calibrate_confidence(raw_confidence: float) -> float:
    """Raw ML confidence → calibrated probability (편의 함수)."""
    return get_calibrator().calibrate(raw_confidence)
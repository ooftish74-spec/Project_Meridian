"""
Kelly Criterion — 최적 포지션 사이징
=====================================

Medallion/Two Sigma 수준 포지션 사이징:
  - Half-Kelly 기본 (과적합 리스크 완화)
  - 스트림별 독립 Kelly 비율 계산
  - 레짐 조건부 Kelly 조정
  - 실시간 measurement_engine 연동

공식:
  f* = (p × b - q) / b
  - p = 승률 (win_rate)
  - q = 1 - p (패율)
  - b = 평균 이익 / 평균 손실 (profit-loss ratio)
  Half-Kelly = f* × fraction (기본 0.5)

모든 파라미터는 DynamicConfig에서 동적 로드.

Usage:
    from src.risk.kelly_criterion import KellyCriterion
    kc = KellyCriterion()
    result = kc.calculate_all()
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import logging
import math
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_KELLY_EDGE_BOOST_COEF = 0.50
_KELLY_MIN_PENALTY = 0.10
_KELLY_STATUTORY_CEIL = 0.25
_KELLY_VIX_REF = 15.0
_KELLY_VIX_TAU = 20.0
_KELLY_DD_FLOOR_DEFAULT = -5.0
_KELLY_DD_MAX_CUT = 0.85
_KELLY_DD_EPS = 1e-6
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None

def _get(key: str, default):
    """DynamicConfig safe getter."""
    if _cfg:
        return _cfg.get(key, default)
    return default

class KellyCriterion:
    """최적 포지션 사이징 엔진.

    스트림별 독립적으로 Kelly 비율을 계산하고,
    레짐/변동성/드로다운 볼록성(Drawdown Convexity) 조건에 따라 동적 조정합니다.
    """
    STREAMS: list

    def __init__(self):
        from config.dynamic_config import DynamicConfig as _DC
        self.STREAMS = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10', 'S11', 'S12', 'S13', 'S14_PAIR_ALPHA']))
        self._measurement_data = None
        self._stream_metrics = None
        self._signal_cache = None

    @staticmethod
    def compute_drawdown_convexity_factor(
        current_dd_pct: float,
        floor_dd_pct: float = _KELLY_DD_FLOOR_DEFAULT,
    ) -> float:
        """Citadel / Millennium 드로다운 볼록성 감쇠 계수(Drawdown Convexity Dampener, ψ(d_t)) 산출.

        ψ(d_t, d_floor) = 1.0 - min(ψ_max_cut, (max(0, -DD_t) / max(|d_floor|, ε))^2)
        """
        dd_mag = max(0.0, -float(current_dd_pct))
        floor_mag = max(_KELLY_DD_EPS, abs(float(floor_dd_pct)))
        ratio_sq = (dd_mag / floor_mag) ** 2
        return round(1.0 - min(_KELLY_DD_MAX_CUT, ratio_sq), 6)

    @staticmethod
    def kelly_fraction(win_rate: float, profit_loss_ratio: float) -> float:
        """순수 Kelly 비율 계산.

        f* = (p × b - q) / b
        where p = win_rate, b = avg_win/avg_loss, q = 1-p

        Args:
            win_rate: 승률 (0~1)
            profit_loss_ratio: 평균 이익/평균 손실 비율 (>0)

        Returns:
            Kelly 비율 (음수 가능 — 이 경우 투자하지 않음)
        """
        if win_rate > 1.0:
            win_rate = win_rate / 100.0
        if win_rate >= 1.0:
            logger.warning(f"win_rate={win_rate:.4f} ≥ 1.0: 비현실적 입력 가능성 높음 — 데이터 확인 권장")
            win_rate = min(win_rate, 0.99)
        if profit_loss_ratio <= 0 or win_rate <= 0 or win_rate > 1:
            return 0.0
        p = win_rate
        q = 1.0 - p
        b = profit_loss_ratio
        f_star = (p * b - q) / b
        return f_star

    def calculate_stream_kelly(
        self,
        stream_id: str,
        regime: str = 'caution',
        current_dd_pct: Optional[float] = None,
    ) -> Dict:
        """개별 스트림의 Kelly 비율 계산.

        Args:
            stream_id: 스트림 ID (S1~S4)
            regime: 현재 레짐
            current_dd_pct: 현재 포트폴리오 드로다운 (%, 선택사항 — 전달 시 볼록성 감쇠 ψ(d_t) 적용)

        Returns:
            {
                'stream_id': 'S2',
                'full_kelly': 0.12,
                'adjusted_kelly': 0.06,  # Half-Kelly × 레짐 조정
                'max_position_pct': 0.06,
                'win_rate': 0.55,
                'profit_loss_ratio': 1.8,
                'n_trades': 30,
                'sufficient_data': True,
                'regime_adj': 0.9,
            }
        """
        stats = self._get_stream_stats(stream_id)
        win_rate = stats.get('win_rate', 0)
        avg_win = stats.get('avg_win', 0)
        avg_loss = stats.get('avg_loss', 0)
        n_trades = stats.get('n_trades', 0)
        min_trades = pget('risk.kelly_min_trades', 10)
        sufficient_data = n_trades >= min_trades
        if not sufficient_data or avg_loss == 0 or win_rate <= 0:
            default_pct = _get(f'risk.kelly_default.{stream_id}', pget('risk.kelly_default_pct', 0.05))
            return {'stream_id': stream_id, 'full_kelly': 0.0, 'adjusted_kelly': default_pct, 'max_position_pct': default_pct, 'win_rate': win_rate, 'profit_loss_ratio': 0.0, 'n_trades': n_trades, 'sufficient_data': False, 'regime_adj': 1.0, 'note': f"데이터 부족 ({n_trades}/{min_trades}건) → 기본값 {default_pct * 100:.0f}%"}
        pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        full_kelly = self.kelly_fraction(win_rate, pl_ratio)
        fraction = pget('risk.kelly_fraction', 0.5)
        fractional_kelly = full_kelly * fraction
        
        # Bayesian Uncertainty Penalty (VIX 연속 지수 감쇠 모델: exp(-max(0, V_t - V_ref) / tau_V))
        bayesian_penalty = 1.0
        if self._signal_cache is None:
            self._load_data()
        if self._signal_cache:
            vix_data = self._signal_cache.get('VIX') if 'VIX' in self._signal_cache else self._signal_cache.get('vix', {})
            current_vix = float(vix_data.get('close', vix_data.get('value', 0.0)) or 0.0) if isinstance(vix_data, dict) else float(vix_data or 0.0)
            if current_vix > 0.0:
                excess_vix = max(0.0, current_vix - _KELLY_VIX_REF)
                bayesian_penalty = max(_KELLY_MIN_PENALTY, math.exp(-excess_vix / _KELLY_VIX_TAU))
                logger.debug(f"    - [Bayesian Kelly] VIX={current_vix:.2f} -> Exp Penalty Factor: {bayesian_penalty:.4f}")

        _regime_defaults = {'bull': 1.0, 'caution': 0.8, 'bear': 0.5, 'crash': 0.3}
        _regime_safe = regime if regime in _regime_defaults else 'caution'
        regime_adj = _get(f'risk.kelly_regime_adj.{_regime_safe}', _regime_defaults[_regime_safe])

        dd_floor = float(pget('kill_switch.absolute_mdd_kill_pct', _KELLY_DD_FLOOR_DEFAULT))
        dd_convexity = (
            self.compute_drawdown_convexity_factor(float(current_dd_pct), dd_floor)
            if current_dd_pct is not None
            else 1.0
        )

        # 최종 조정된 켈리 = fractional_kelly * regime_adj * bayesian_penalty * dd_convexity
        adjusted_kelly = fractional_kelly * regime_adj * bayesian_penalty * dd_convexity
        
        if full_kelly <= 0:
            return {
                'stream_id': stream_id, 'full_kelly': 0.0, 'fractional_kelly': 0.0,
                'adjusted_kelly': 0.0, 'max_position_pct': 0.0, 'win_rate': round(win_rate, 4),
                'profit_loss_ratio': round(pl_ratio, 4), 'avg_win_pct': round(avg_win * 100, 2),
                'avg_loss_pct': round(avg_loss * 100, 2), 'n_trades': n_trades,
                'sufficient_data': sufficient_data, 'regime': regime, 'regime_adj': regime_adj,
                'dd_convexity_factor': round(dd_convexity, 6), 'fraction': fraction
            }
        base_max_pos = float(pget('risk.kelly_max_position_pct', 0.15))
        min_pos = float(pget('risk.kelly_min_position_pct', 0.01))
        sample_conf = math.sqrt(float(n_trades) / float(n_trades + max(1, int(min_trades))))
        edge_boost = 1.0 + _KELLY_EDGE_BOOST_COEF * sample_conf * math.tanh(max(0.0, float(full_kelly)))
        raw_dyn_max = base_max_pos * float(regime_adj) * math.sqrt(max(_KELLY_MIN_PENALTY, float(bayesian_penalty))) * edge_boost * dd_convexity
        max_pos = max(min_pos, min(_KELLY_STATUTORY_CEIL, raw_dyn_max))
        final_kelly = max(min_pos, min(max_pos, adjusted_kelly))
        return {
            'stream_id': stream_id,
            'full_kelly': round(full_kelly, 6),
            'fractional_kelly': round(fractional_kelly, 6),
            'adjusted_kelly': round(adjusted_kelly, 6),
            'dynamic_max_position_pct': round(max_pos, 6),
            'max_position_pct': round(final_kelly, 6),
            'win_rate': round(win_rate, 4),
            'profit_loss_ratio': round(pl_ratio, 4),
            'avg_win_pct': round(avg_win * 100, 2),
            'avg_loss_pct': round(avg_loss * 100, 2),
            'n_trades': n_trades,
            'sufficient_data': sufficient_data,
            'regime': regime,
            'regime_adj': regime_adj,
            'dd_convexity_factor': round(dd_convexity, 6),
            'fraction': fraction,
        }

    def calculate_all(self, regime: str='caution', current_dd_pct: Optional[float]=None) -> Dict:
        """모든 스트림의 Kelly 비율 일괄 계산.

        Returns:
            {
                'timestamp': '...',
                'regime': 'caution',
                'streams': {
                    'S1': {...}, 'S2': {...}, ...
                },
                'portfolio_kelly': 0.08,  # 가중 평균
            }
        """
        self._load_data()
        results = {}
        total_weight = 0
        weighted_kelly = 0
        for stream_id in self.STREAMS:
            result = self.calculate_stream_kelly(stream_id, regime, current_dd_pct=current_dd_pct)
            results[stream_id] = result
            if result['sufficient_data'] and result['max_position_pct'] > 0:
                w = min(result['n_trades'], 100) / 100
                weighted_kelly += result['max_position_pct'] * w
                total_weight += w
        portfolio_kelly = weighted_kelly / total_weight if total_weight > 0 else 0
        output = {'timestamp': datetime.now().isoformat(), 'regime': regime, 'fraction': pget('risk.kelly_fraction', 0.5), 'streams': results, 'portfolio_kelly': round(portfolio_kelly, 6)}
        self._save_results(output)
        return output

    def get_position_size(self, stream_id: str, capital: float, regime: str='caution', current_dd_pct: Optional[float]=None) -> float:
        """특정 스트림의 최적 포지션 금액 반환.

        Args:
            stream_id: 스트림 ID
            capital: 가용 자본금
            regime: 레짐

        Returns:
            최적 포지션 금액 (원)
        """
        result = self.calculate_stream_kelly(stream_id, regime, current_dd_pct=current_dd_pct)
        return capital * result['max_position_pct']

    def _load_data(self):
        """measurement_engine.json + stream_metrics.json 로드."""
        try:
            me_path = _RESULTS / 'measurement_engine.json'
            if me_path.exists():
                self._measurement_data = json.loads(me_path.read_text())
        except Exception as e:
            logger.critical(f'measurement_engine 로드 실패: {e}', exc_info=True)
        try:
            sm_path = _RESULTS / 'stream_metrics.json'
            if sm_path.exists():
                self._stream_metrics = json.loads(sm_path.read_text())
        except Exception as e:
            logger.critical(f'stream_metrics 로드 실패: {e}', exc_info=True)
        try:
            sc_path = _RESULTS / 'signal_cache.json'
            if sc_path.exists():
                self._signal_cache = json.loads(sc_path.read_text())
        except Exception as e:
            logger.warning(f"signal_cache 로드 실패 (Bayesian Penalty 스킵): {e}")

    def _get_stream_stats(self, stream_id: str) -> Dict:
        """스트림 통계 추출."""
        if self._measurement_data is None and self._stream_metrics is None:
            self._load_data()
        stats = {'win_rate': 0, 'avg_win': 0, 'avg_loss': 0, 'n_trades': 0}
        if self._stream_metrics:
            metrics = self._stream_metrics.get('metrics', {})
            raw_s = (self._stream_metrics.get('raw_data') or {}).get(stream_id, {}) or {}
            perf_s = (
                metrics.get('stream_performance', {}).get(stream_id, {})
                or metrics.get('stream_summary', {}).get(stream_id, {})
                or self._stream_metrics.get('stream_performance', {}).get(stream_id, {})
                or self._stream_metrics.get('stream_summary', {}).get(stream_id, {})
                or {}
            )
            stream_data = {**raw_s, **perf_s} if (raw_s or perf_s) else {}
            if stream_data:
                raw_wr = float(stream_data.get('win_rate', 0) or 0)
                stats['win_rate'] = raw_wr / 100.0 if raw_wr > 1.0 else raw_wr
                stats['n_trades'] = int(
                    stream_data.get('total_trades')
                    or stream_data.get('trade_count')
                    or (int(stream_data.get('n_buys', 0) or 0) + int(stream_data.get('n_sells', 0) or 0))
                    or stream_data.get('n_days', 0)
                    or 0
                )
                avg_win_pct = stream_data.get('avg_win_pct', stream_data.get('avg_win', 0))
                avg_loss_pct = stream_data.get('avg_loss_pct', stream_data.get('avg_loss', 0))
                if avg_win_pct:
                    stats['avg_win'] = abs(avg_win_pct) / 100.0
                if avg_loss_pct:
                    stats['avg_loss'] = abs(avg_loss_pct) / 100.0
                d_rets = stream_data.get('daily_returns')
                if isinstance(d_rets, list):
                    stats['daily_returns'] = list(d_rets)
                if isinstance(d_rets, list) and d_rets and (stats['avg_win'] == 0 or stats['avg_loss'] == 0):
                    pos_r = [float(r) for r in d_rets if float(r or 0.0) > 0.0]
                    neg_r = [abs(float(r)) for r in d_rets if float(r or 0.0) < 0.0]
                    if stats['win_rate'] == 0:
                        stats['win_rate'] = len(pos_r) / len(d_rets)
                    if stats['avg_win'] == 0 and pos_r:
                        stats['avg_win'] = sum(pos_r) / len(pos_r)
                    if stats['avg_loss'] == 0 and neg_r:
                        stats['avg_loss'] = sum(neg_r) / len(neg_r)
        if self._measurement_data:
            per_stream_map = ((self._measurement_data.get('official') or {}).get('per_stream') or {})
            sleeves_map = ((self._measurement_data.get('views') or {}).get('sleeves') or {})
            quant = per_stream_map.get(stream_id) or sleeves_map.get(stream_id) or {}
            if stats['n_trades'] == 0 and quant:
                raw_wr = float(quant.get('win_rate', quant.get('realized_win_rate', 0)) or 0)
                stats['win_rate'] = raw_wr / 100.0 if raw_wr > 1.0 else raw_wr
                stats['n_trades'] = int(quant.get('total_trades', quant.get('realized_trades', 0)) or 0)
            if stats['avg_win'] == 0 and quant:
                avg_w = quant.get('avg_win_pct', quant.get('avg_win', 0))
                avg_l = quant.get('avg_loss_pct', quant.get('avg_loss', 0))
                if avg_w:
                    stats['avg_win'] = abs(float(avg_w)) / 100.0 if abs(float(avg_w)) > 1.0 else abs(float(avg_w))
                if avg_l:
                    stats['avg_loss'] = abs(float(avg_l)) / 100.0 if abs(float(avg_l)) > 1.0 else abs(float(avg_l))
        return stats

    def _save_results(self, output: Dict):
        """결과 저장."""
        try:
            path = _RESULTS / 'kelly_criterion.json'
            _RESULTS.mkdir(exist_ok=True)
            atomic_write_json(path, output, indent=2, ensure_ascii=False, default=str)
            logger.info(f"  📊 Kelly Criterion 저장: portfolio={output['portfolio_kelly'] * 100:.1f}%")
        except Exception as e:
            logger.critical(f'Kelly 결과 저장 실패: {e}', exc_info=True)
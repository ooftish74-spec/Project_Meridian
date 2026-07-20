"""
SignalQualityGate — IC 기반 진입 필터
======================================

시그널 품질이 저하되었을 때 진입을 차단하는 IC-aware 필터.
각 스트림별 Rolling Spearman IC (confidence vs realized pnl_pct)를 추적하고,
IC가 유의하게 음수이면 해당 스트림 진입을 차단 또는 스로틀링합니다.

핵심 수학:
  - Spearman IC = rank_corr(confidence, pnl_pct) over rolling window
  - Adjusted Confidence = raw_conf × (1 + rolling_ic × ic_weight)
  - Signal Decay: effective_conf = entry_conf × exp(-hold_days / ic_halflife)

Top Quant 원칙:
  1. 모든 임계값은 DynamicConfig에서 로드 (하드코딩 Zero)
  2. 엣지 케이스 완전 처리 (NaN, 빈 데이터, 부족 샘플)
  3. 상태 영속화 (results/signal_quality_state.json)

Usage:
    from src.intelligence.signal_quality_gate import SignalQualityGate
    gate = SignalQualityGate()
    gate.update(portfolio_data)
    result = gate.check_entry('S2', '005930', 0.72)
"""
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
from scipy import stats
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class SignalQualityGate:
    """IC 기반 시그널 품질 게이트.

    각 스트림의 최근 N건 거래에서 Spearman IC를 계산하고,
    IC가 음수면 confidence를 감소시키거나 진입을 차단합니다.

    Attributes:
        _rolling_ic: 스트림별 rolling IC 값
        _samples: 스트림별 유효 샘플 수
        _trade_buffer: 스트림별 최근 거래 (confidence, pnl_pct) 버퍼
    """

    def __init__(self) -> None:
        """DynamicConfig 로드 및 상태 초기화."""
        self._cfg = DynamicConfig()
        self._rolling_ic: Dict[str, float] = {}
        self._samples: Dict[str, int] = {}
        self._trade_buffer: Dict[str, List[Dict[str, float]]] = {}
        self._state_file = _PROJECT_ROOT / 'results' / 'signal_quality_state.json'
        self._load_state()
        logger.info('SignalQualityGate 초기화 완료 | streams=%s', list(self._rolling_ic.keys()))

    def update(self, portfolio_data: dict) -> None:
        """포트폴리오 trade_history에서 스트림별 Rolling IC 업데이트.

        각 스트림의 최근 거래에서 Spearman IC를 계산합니다:
            IC = spearman_rank_corr(confidence[], pnl_pct[])

        Args:
            portfolio_data: 포트폴리오 데이터
                - trade_history: List[Dict] with keys: stream, confidence, pnl_pct
        """
        trade_history = portfolio_data.get('trade_history', [])
        if not trade_history:
            logger.debug('SignalQualityGate.update: trade_history 비어있음')
            return
        ic_window = self._cfg.get('signal_gate.ic_window', 20)
        stream_trades: Dict[str, List[Dict]] = {}
        for trade in trade_history:
            stream = trade.get('stream', 'unknown')
            conf = trade.get('confidence')
            pnl = trade.get('pnl_pct')
            if conf is not None and pnl is not None:
                try:
                    conf_val = float(conf)
                    pnl_val = float(pnl)
                    if not (math.isnan(conf_val) or math.isnan(pnl_val)):
                        stream_trades.setdefault(stream, []).append({'confidence': conf_val, 'pnl_pct': pnl_val})
                except (ValueError, TypeError):
                    continue
        for stream_id, trades in stream_trades.items():
            recent = trades[-ic_window:]
            self._trade_buffer[stream_id] = recent
            n_samples = len(recent)
            self._samples[stream_id] = n_samples
            min_samples = self._cfg.get('signal_gate.min_samples', 10)
            if n_samples < max(3, min_samples // 2):
                logger.debug('SignalQualityGate: %s 샘플 부족 (%d건), IC 계산 스킵', stream_id, n_samples)
                self._rolling_ic[stream_id] = 0.0
                continue
            confidences = np.array([t['confidence'] for t in recent])
            pnl_pcts = np.array([t['pnl_pct'] for t in recent])
            if np.std(confidences) < 1e-10 or np.std(pnl_pcts) < 1e-10:
                logger.debug('SignalQualityGate: %s 분산 부족, IC=0.0 설정', stream_id)
                self._rolling_ic[stream_id] = 0.0
                continue
            try:
                ic_val, p_value = stats.spearmanr(confidences, pnl_pcts)
                if math.isnan(ic_val):
                    ic_val = 0.0
                self._rolling_ic[stream_id] = round(float(ic_val), 6)
                logger.info('SignalQualityGate: %s IC=%.4f (p=%.4f, n=%d)', stream_id, ic_val, p_value, n_samples)
            except Exception as e:
                logger.warning('SignalQualityGate: %s IC 계산 오류: %s', stream_id, e, exc_info=True)
                self._rolling_ic[stream_id] = 0.0
        self._save_state()

    def compute_orthogonality_score(self, ticker: str, vol_ratio: float=1.0, orderbook_imbalance: float=0.0, regime: str='bull') -> float:
        """[Phase 74] 직교성 훼손 계수 (웹더독 Veto).

        시총액 최상위 종목(Samsung/Hynix)에 수급 왜곡 발생 시
        S3 매수 신호를 직교성 훼손 계수로 감쇠.

        Returns: 0.0(완전 기각) ~ 1.0(정상)
        """
        cfg = self._cfg
        _vol_thr = float(cfg.get('veto.vol_ratio_threshold', 5.0))
        _ob_thr = float(cfg.get('veto.orderbook_imbalance_threshold', 0.7))
        _top_caps = {str(t) for t in cfg.get('veto.top_cap_tickers', ['005930', '000660', '035420', '051910'])}
        if ticker not in _top_caps:
            return 1.0
        score = 1.0
        if vol_ratio > _vol_thr:
            _excess = vol_ratio / _vol_thr
            _decay = min(0.9, (_excess - 1.0) * 0.25)
            score *= 1.0 - _decay
            logger.debug(f'  [Phase 74 Veto] {ticker}: vol_ratio={vol_ratio:.1f}x (-{_decay:.0%})')
        if orderbook_imbalance < -_ob_thr:
            _od = min(0.8, abs(orderbook_imbalance) - _ob_thr)
            score *= 1.0 - _od
        if regime in ('whipsaw', 'crash'):
            score *= float(cfg.get('veto.crash_regime_multiplier', 0.5))
        final = round(max(0.0, min(1.0, score)), 4)
        if final < 0.5:
            logger.warning(f'  [Phase 74 Veto] {ticker}: score={final:.3f} vol={vol_ratio:.1f}x OB={orderbook_imbalance:.2f} regime={regime}')
        return final

    def check_entry(self, stream_id: str, ticker: str, confidence: float, vol_ratio: float=1.0, orderbook_imbalance: float=0.0, regime: str='bull') -> dict:
        """IC 기반 진입 허용 여부 및 조정된 confidence 반환.

        판정 로직:
          1. rolling_ic < ic_block_threshold AND n >= min_samples → 차단
          2. rolling_ic < 0 → confidence 감소 (ic_penalty_factor 적용)
          3. adjusted_confidence = raw_conf × (1 + rolling_ic × ic_weight)

        Args:
            stream_id: 스트림 ID (S1, S2, S3, S4)
            ticker: 종목 코드
            confidence: 원래 confidence 값

        Returns:
            {
                'allowed': bool,
                'adjusted_confidence': float,
                'reason': str,
                'rolling_ic': float,
                'samples': int,
            }
        """
        rolling_ic = self._rolling_ic.get(stream_id, 0.0)
        n_samples = self._samples.get(stream_id, 0)
        ic_block_threshold = self._cfg.get('signal_gate.ic_block_threshold', -0.05)
        min_samples = self._cfg.get('signal_gate.min_samples', 10)
        ic_weight = self._cfg.get('signal_gate.ic_weight', 0.5)
        _veto = self.compute_orthogonality_score(ticker, vol_ratio, orderbook_imbalance, regime)
        if _veto < 1.0:
            confidence = confidence * _veto
            logger.info(f'  [Phase 74 Veto] {stream_id}/{ticker}: 직교성계수={_veto:.3f} conf조정')
        adjustment_factor = 1.0 + rolling_ic * ic_weight
        adjustment_factor = max(adjustment_factor, 0.1)
        adjusted_confidence = confidence * adjustment_factor
        if rolling_ic < ic_block_threshold and n_samples >= min_samples:
            reason = f'IC 차단: {stream_id} IC={rolling_ic:.4f} < threshold={ic_block_threshold} (n={n_samples})'
            logger.warning('SignalQualityGate BLOCKED: %s %s | %s', stream_id, ticker, reason)
            return {'allowed': False, 'adjusted_confidence': adjusted_confidence, 'reason': reason, 'rolling_ic': rolling_ic, 'samples': n_samples, 'orthogonality_score': round(_veto, 4)}
        if rolling_ic < 0 and n_samples >= min_samples:
            reason = f'IC 패널티: {stream_id} IC={rolling_ic:.4f} | conf {confidence:.4f} → {adjusted_confidence:.4f}'
            logger.info('SignalQualityGate PENALTY: %s %s | %s', stream_id, ticker, reason)
            return {'allowed': True, 'adjusted_confidence': adjusted_confidence, 'reason': reason, 'rolling_ic': rolling_ic, 'samples': n_samples, 'orthogonality_score': round(_veto, 4)}
        if n_samples < min_samples:
            reason = f'샘플 부족 ({n_samples} < {min_samples}), confidence 무변경'
            adjusted_confidence = confidence
        else:
            reason = f'IC 양호: {stream_id} IC={rolling_ic:.4f}, boost 적용'
        logger.debug('SignalQualityGate PASS: %s %s | IC=%.4f, conf=%.4f→%.4f', stream_id, ticker, rolling_ic, confidence, adjusted_confidence)
        return {'allowed': True, 'adjusted_confidence': adjusted_confidence, 'reason': reason, 'rolling_ic': rolling_ic, 'samples': n_samples, 'orthogonality_score': round(_veto, 4)}

    def get_signal_decay(self, entry_confidence: float, hold_days: int) -> float:
        """시그널 신선도 감쇠 계산.

        지수 감쇠 모델:
            decay_factor = exp(-hold_days / ic_halflife)
            effective_conf = entry_confidence × decay_factor

        ic_halflife: 반감기 (일). 1.4일이면 ~1.4일 보유 시 confidence 절반.

        Args:
            entry_confidence: 진입 시점 confidence
            hold_days: 보유 일수

        Returns:
            effective_confidence (float)
            - decay_exit_threshold 미만이면 청산 권고 의미
        """
        ic_halflife = self._cfg.get('signal_gate.ic_halflife', 1.4)
        decay_exit_threshold = self._cfg.get('signal_gate.decay_exit_threshold', 0.45)
        if hold_days < 0:
            logger.warning('get_signal_decay: hold_days=%d < 0, 0으로 처리', hold_days)
            hold_days = 0
        if ic_halflife <= 0:
            logger.warning('ic_halflife=%.2f ≤ 0, 감쇠 비활성', ic_halflife)
            return entry_confidence
        decay_factor = math.exp(-hold_days / ic_halflife)
        effective_conf = entry_confidence * decay_factor
        if effective_conf < decay_exit_threshold:
            logger.info('Signal Decay EXIT 권고: entry_conf=%.3f, hold=%dd, effective=%.3f < threshold=%.3f', entry_confidence, hold_days, effective_conf, decay_exit_threshold)
        return round(effective_conf, 6)

    def get_stream_status(self) -> dict:
        """스트림별 IC 상태, 활성/스로틀/차단 판정 반환.

        Returns:
            {
                'S2': {
                    'rolling_ic': 0.12,
                    'status': 'active',  # active / throttled / blocked
                    'samples': 25,
                },
                ...
            }
        """
        ic_block_threshold = self._cfg.get('signal_gate.ic_block_threshold', -0.05)
        min_samples = self._cfg.get('signal_gate.min_samples', 10)
        result: Dict[str, Dict[str, Any]] = {}
        for stream_id in set(list(self._rolling_ic.keys()) + list(self._samples.keys())):
            ic_val = self._rolling_ic.get(stream_id, 0.0)
            n_samples = self._samples.get(stream_id, 0)
            if n_samples < min_samples:
                status = 'active'
            elif ic_val < ic_block_threshold:
                status = 'blocked'
            elif ic_val < 0:
                status = 'throttled'
            else:
                status = 'active'
            result[stream_id] = {'rolling_ic': ic_val, 'status': status, 'samples': n_samples}
        return result

    def _save_state(self) -> None:
        """상태를 JSON 파일로 영속화."""
        state = {'rolling_ic': self._rolling_ic, 'samples': self._samples, 'updated_at': datetime.now().isoformat()}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            logger.debug('SignalQualityGate 상태 저장: %s', self._state_file)
        except Exception as e:
            logger.error('SignalQualityGate 상태 저장 실패: %s', e, exc_info=True)

    def _load_state(self) -> None:
        """JSON 파일에서 상태 복원."""
        if not self._state_file.exists():
            logger.info('SignalQualityGate 상태 파일 없음, 초기 상태로 시작')
            return
        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            self._rolling_ic = state.get('rolling_ic', {})
            self._samples = state.get('samples', {})
            logger.info('SignalQualityGate 상태 복원: streams=%s, updated=%s', list(self._rolling_ic.keys()), state.get('updated_at', 'unknown'))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning('SignalQualityGate 상태 파일 파싱 오류: %s', e, exc_info=True)
        except Exception as e:
            logger.error('SignalQualityGate 상태 로드 실패: %s', e, exc_info=True)
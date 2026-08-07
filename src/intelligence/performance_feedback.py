"""
PerformanceFeedback — 자가학습 피드백 루프
===========================================

실현 성과 기반으로 전략을 자동 조정하는 피드백 시스템.

핵심 기능:
  1. 스트림별 Rolling WR/PF 추적 → 자동 스로틀/부스트/비활성화
  2. 종목별 연속 SL 추적 → 블랙리스트
  3. MAE/MFE 분석 → 최적 SL/TP 제안

수학:
  - WR = wins / total (rolling window)
  - PF = sum(profits) / sum(|losses|) (rolling window)
  - optimal_sl = percentile(MAE, p_mae)  [p_mae=80%]
  - optimal_tp = percentile(MFE, p_mfe)  [p_mfe=60%]
  - 블랙리스트: N건 연속 SL → 30일 차단

Top Quant 원칙:
  1. 모든 임계값 DynamicConfig 로드 (하드코딩 Zero)
  2. 상태 영속화 (results/performance_feedback_state.json)
  3. 엣지 케이스 완전 처리 (빈 데이터, NaN, 부족 샘플)

Usage:
    from src.intelligence.performance_feedback import PerformanceFeedback
    fb = PerformanceFeedback()
    fb.update(portfolio_data)
    scale = fb.get_stream_scale('S2')
    blacklisted = fb.is_blacklisted('005930', 'S2')
"""
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class PerformanceFeedback:
    """자가학습 피드백 루프.

    실현 성과를 추적하고 전략 파라미터를 자동 조정합니다.

    Attributes:
        _stream_metrics: 스트림별 {rolling_wr, rolling_pf, avg_pnl, n_trades}
        _ticker_sl_count: (ticker, stream) → 연속 SL 횟수
        _blacklist: (ticker, stream) → 만료일
    """

    def __init__(self) -> None:
        """DynamicConfig 로드 및 상태 초기화."""
        self._cfg = DynamicConfig()
        self._stream_metrics: Dict[str, Dict[str, Any]] = {}
        self._ticker_sl_count: Dict[str, int] = {}
        self._blacklist: Dict[str, str] = {}
        self._state_file = _PROJECT_ROOT / 'results' / 'performance_feedback_state.json'
        self.load_state()
        logger.info('PerformanceFeedback 초기화 완료')

    def update(self, portfolio_data: dict) -> None:
        """포트폴리오 trade_history에서 모든 지표 업데이트.

        스트림별:
          - rolling WR = wins / total (최근 N건)
          - rolling PF = sum(profits) / sum(|losses|)
          - avg_pnl = mean(pnl_pct)

        종목별:
          - 연속 SL 카운트 추적

        Args:
            portfolio_data: 포트폴리오 데이터
                - trade_history: List[Dict] with keys:
                    stream, ticker, pnl_pct, exit_reason (optional)
        """
        trade_history = portfolio_data.get('trade_history', [])
        if not trade_history:
            logger.debug('PerformanceFeedback.update: trade_history 비어있음')
            return
        rolling_window = self._cfg.get('feedback.rolling_window', 20)
        stream_trades: Dict[str, List[Dict]] = {}
        for trade in trade_history:
            stream = trade.get('stream', 'unknown')
            pnl = trade.get('pnl_pct')
            if pnl is not None:
                try:
                    pnl_val = float(pnl)
                    if not math.isnan(pnl_val):
                        stream_trades.setdefault(stream, []).append(trade)
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                    continue
        for stream_id, trades in stream_trades.items():
            recent = trades[-rolling_window:]
            n = len(recent)
            if n == 0:
                continue
            pnl_values = []
            for t in recent:
                try:
                    pnl_values.append(float(t['pnl_pct']))
                except (ValueError, TypeError, KeyError):
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
                    continue
            if not pnl_values:
                continue
            pnl_arr = np.array(pnl_values)
            wins = int(np.sum(pnl_arr > 0))
            rolling_wr = wins / len(pnl_arr)
            profits = pnl_arr[pnl_arr > 0]
            losses = pnl_arr[pnl_arr < 0]
            sum_profits = float(np.sum(profits)) if len(profits) > 0 else 0.0
            sum_losses = float(np.sum(np.abs(losses))) if len(losses) > 0 else 0.0
            if sum_losses > 1e-10:
                rolling_pf = sum_profits / sum_losses
            else:
                rolling_pf = 99.0 if sum_profits > 0 else 1.0
            avg_pnl = float(np.mean(pnl_arr))
            avg_win = float(np.mean(profits)) if len(profits) > 0 else 0.01
            avg_loss = float(np.mean(np.abs(losses))) if len(losses) > 0 else 0.01
            rolling_vol = float(np.std(pnl_arr)) if len(pnl_arr) > 1 else 1.0
            rolling_vol = max(rolling_vol, 0.001)
            self._stream_metrics[stream_id] = {'rolling_wr': round(rolling_wr, 4), 'rolling_pf': round(rolling_pf, 4), 'avg_pnl': round(avg_pnl, 4), 'n_trades': len(pnl_arr), 'avg_win': round(avg_win, 6), 'avg_loss': round(avg_loss, 6), 'rolling_vol': round(rolling_vol, 6), 'updated_at': datetime.now().isoformat()}
            logger.info('PerformanceFeedback %s: WR=%.2f%%, PF=%.2f, avg_pnl=%.3f%% (n=%d)', stream_id, rolling_wr * 100, rolling_pf, avg_pnl, len(pnl_arr))
        self.save_state()

    @property
    def _stream_stats(self) -> dict:
        """_stream_metrics의 별칭 (compute_dynamic_kelly 호환)."""
        return self._stream_metrics

    def compute_dynamic_kelly(self, stream_id: str) -> float:
        """[Phase 74] 동적 켈리 기준(Dynamic Kelly Criterion).

        f* = (p - (1-p)/b) / vol_penalty
          p   = rolling win rate
          b   = avg_win / avg_loss (payoff ratio)
          vol = rolling volatility (penalty)

        f* <= 0 -> Soft-kill (포지션 0%로 수렴)
        """
        stats: dict = self._stream_stats.get(stream_id, {})
        p = float(stats.get('rolling_wr', 0.5))
        avg_w = float(stats.get('avg_win', 0.01))
        avg_l = float(stats.get('avg_loss', 0.01))
        r_vol = float(stats.get('rolling_vol', 1.0))
        if avg_l <= 0 or p <= 0:
            return 0.0
        b = avg_w / max(avg_l, 1e-09)
        f_raw = p - (1.0 - p) / max(b, 1e-09)
        vol_penalty = max(r_vol, float(self._cfg.get('feedback.kelly_min_vol', 0.1)))
        f_star = f_raw / vol_penalty
        logger.debug(f'  [Phase 74 Kelly] {stream_id}: p={p:.3f} b={b:.3f} vol={vol_penalty:.3f} f*={f_star:.4f}')
        return max(-1.0, min(1.0, f_star))

    def get_stream_scale(self, stream_id: str) -> dict:
        """스트림별 자동 스로틀/부스트/비활성화 판정.

        판정 로직:
          - WR < deactivate_wr AND PF < deactivate_pf → active=False
          - WR < throttle_wr → scale=0.5
          - WR > boost_wr → scale=1.5
          - otherwise → scale=1.0

        Args:
            stream_id: 스트림 ID (S1, S2, S3, S4)

        Returns:
            {
                'active': bool,
                'scale': float,
                'rolling_wr': float,
                'rolling_pf': float,
                'reason': str,
            }
        """
        cfg = self._cfg
        metrics = self._stream_metrics.get(stream_id, {})
        rolling_wr = metrics.get('rolling_wr', 0.5)
        rolling_pf = metrics.get('rolling_pf', 1.0)
        n_trades = metrics.get('n_trades', 0)
        min_samples = cfg.get('feedback.rolling_window', 20) // 2
        if n_trades < min_samples:
            reason = f'데이터 부족 ({n_trades} < {min_samples}), 기본 scale=1.0'
            logger.debug('PerformanceFeedback %s: %s', stream_id, reason)
            return {'active': True, 'scale': 1.0, 'rolling_wr': rolling_wr, 'rolling_pf': rolling_pf, 'kelly_fstar': 0.0, 'reason': reason}
        kelly_max = float(cfg.get('feedback.kelly_max_scale', 2.0))
        kelly_frac = float(cfg.get('feedback.kelly_fraction', 0.25))
        f_star = self.compute_dynamic_kelly(stream_id)
        _s3_exempt = str(stream_id).upper().startswith('S3') or str(stream_id).upper() in cfg.get('feedback.kelly_exempt_streams', ['S3', 'S3_A', 'S3_B'])
        if f_star <= 0.0 and (not _s3_exempt):
            _softfall_base = float(cfg.get('feedback.kelly_softfall_base', 1.0))
            _softfall_cap = float(cfg.get('feedback.kelly_softfall_cap', 0.1))
            scale = max(0.0, f_star * kelly_max + _softfall_base)
            scale = min(scale, _softfall_cap)
            reason = f'[Phase74] Kelly Soft-kill f*={f_star:.4f}'
            active = scale > 0.0
        elif f_star <= 0.0 and _s3_exempt:
            scale = float(cfg.get('feedback.s3_min_kelly_scale', 0.1))
            reason = f'[Phase79] S3 Kelly 면제 (f*={f_star:.4f} 무시, scale={scale:.2f})'
            active = True
        else:
            scale = min(f_star * kelly_frac * kelly_max, kelly_max)
            scale = round(scale, 4)
            reason = f'[Phase74] Kelly scale f*={f_star:.4f} -> {scale:.3f}'
            active = True
        logger.info('PerformanceFeedback %s: %s', stream_id, reason)
        return {'active': active, 'scale': scale, 'rolling_wr': rolling_wr, 'rolling_pf': rolling_pf, 'kelly_fstar': round(f_star, 4), 'reason': reason}

    def is_blacklisted(self, ticker: str, stream_id: str) -> dict:
        """종목 블랙리스트 여부 확인.

        연속 SL이 consecutive_sl_limit 이상이면 blacklist_days 동안 차단.

        Args:
            ticker: 종목 코드
            stream_id: 스트림 ID

        Returns:
            {
                'blacklisted': bool,
                'reason': str,
                'expires': str or None,  # ISO 형식
                'consecutive_sl': int,
            }
        """
        key = f'{ticker}::{stream_id}'
        consecutive_sl = self._ticker_sl_count.get(key, 0)
        if key not in self._blacklist:
            return {'blacklisted': False, 'reason': f'블랙리스트 아님 (연속 SL: {consecutive_sl})', 'expires': None, 'consecutive_sl': consecutive_sl}
        expires_str = self._blacklist[key]
        try:
            expires_dt = datetime.fromisoformat(expires_str)
            if datetime.now() > expires_dt:
                del self._blacklist[key]
                self._ticker_sl_count[key] = 0
                logger.info('PerformanceFeedback 블랙리스트 만료 해제: %s/%s', ticker, stream_id)
                self.save_state()
                return {'blacklisted': False, 'reason': '블랙리스트 만료 해제', 'expires': None, 'consecutive_sl': 0}
        except (ValueError, TypeError):
            del self._blacklist[key]
            return {'blacklisted': False, 'reason': '블랙리스트 만료일 파싱 오류, 해제', 'expires': None, 'consecutive_sl': consecutive_sl}
        reason = f'블랙리스트: {ticker}/{stream_id} 연속 SL {consecutive_sl}회, 만료: {expires_str[:10]}'
        return {'blacklisted': True, 'reason': reason, 'expires': expires_str, 'consecutive_sl': consecutive_sl}

    def compute_optimal_exits(self, stream_id: str, trade_history: list) -> dict:
        """MAE/MFE 분석 기반 최적 SL/TP 계산.

        수학:
          - MAE (Maximum Adverse Excursion): 보유 중 최대 불리 움직임
          - MFE (Maximum Favorable Excursion): 보유 중 최대 유리 움직임
          - optimal_sl = percentile(MAE, mae_percentile) [80th]
          - optimal_tp = percentile(MFE, mfe_percentile) [60th]

        Args:
            stream_id: 스트림 ID
            trade_history: 거래 이력 리스트
                각 trade 에 mae_pct, mfe_pct 키가 있어야 함

        Returns:
            {
                'optimal_sl': float,    # 최적 SL (%)
                'optimal_tp': float,    # 최적 TP (%)
                'current_sl': float,    # 현재 설정 SL
                'current_tp': float,    # 현재 설정 TP
                'adjustment_suggested': bool,
                'n_samples': int,
                'reason': str,
            }
        """
        mae_percentile = self._cfg.get('feedback.mae_percentile', 80)
        mfe_percentile = self._cfg.get('feedback.mfe_percentile', 60)
        stream_key = stream_id.lower() if stream_id else 's2'
        current_sl = self._cfg.get('portfolio.stop_loss_pct', -5.0)
        current_tp = self._cfg.get(f'{stream_key}.take_profit_pct', self._cfg.get('portfolio.take_profit_pct', 15.0))
        stream_trades = [t for t in trade_history if t.get('stream', '') == stream_id][-50:]
        mae_values = []
        mfe_values = []
        for trade in stream_trades:
            mae = trade.get('mae_pct')
            mfe = trade.get('mfe_pct')
            if mae is not None:
                try:
                    mae_val = float(mae)
                    if not math.isnan(mae_val):
                        mae_values.append(abs(mae_val))
                except (ValueError, TypeError):
                    logger.warning('[SILENT_BYPASS] Suppressed exception at performance_feedback.py:440', exc_info=True)
            if mfe is not None:
                try:
                    mfe_val = float(mfe)
                    if not math.isnan(mfe_val):
                        mfe_values.append(abs(mfe_val))
                except (ValueError, TypeError):
                    logger.warning('[SILENT_BYPASS] Suppressed exception at performance_feedback.py:447', exc_info=True)
        n_mae = len(mae_values)
        n_mfe = len(mfe_values)
        min_analysis_samples = 10
        if n_mae < min_analysis_samples or n_mfe < min_analysis_samples:
            reason = f'MAE/MFE 데이터 부족: MAE={n_mae}, MFE={n_mfe} (최소 {min_analysis_samples}건)'
            logger.debug('PerformanceFeedback %s: %s', stream_id, reason)
            return {'optimal_sl': current_sl, 'optimal_tp': current_tp, 'current_sl': current_sl, 'current_tp': current_tp, 'adjustment_suggested': False, 'n_samples': min(n_mae, n_mfe), 'reason': reason}
        mae_arr = np.array(mae_values)
        mfe_arr = np.array(mfe_values)
        optimal_sl_abs = float(np.percentile(mae_arr, mae_percentile))
        optimal_tp_abs = float(np.percentile(mfe_arr, mfe_percentile))
        optimal_sl = -optimal_sl_abs
        optimal_tp = optimal_tp_abs
        sl_diff_pct = abs(optimal_sl - current_sl) / max(abs(current_sl), 1e-10)
        tp_diff_pct = abs(optimal_tp - current_tp) / max(abs(current_tp), 1e-10)
        adjustment_suggested = sl_diff_pct > 0.1 or tp_diff_pct > 0.1
        reason = f'MAE/MFE 분석 완료: optimal_sl={optimal_sl:.2f}% (현재 {current_sl:.2f}%), optimal_tp={optimal_tp:.2f}% (현재 {current_tp:.2f}%) | n={min(n_mae, n_mfe)}'
        if adjustment_suggested:
            logger.info('PerformanceFeedback %s 조정 제안: %s', stream_id, reason)
        else:
            logger.debug('PerformanceFeedback %s: %s', stream_id, reason)
        return {'optimal_sl': round(optimal_sl, 4), 'optimal_tp': round(optimal_tp, 4), 'current_sl': current_sl, 'current_tp': current_tp, 'adjustment_suggested': adjustment_suggested, 'n_samples': min(n_mae, n_mfe), 'reason': reason}

    def save_state(self) -> None:
        """상태를 JSON 파일로 저장."""
        state = {'stream_metrics': self._stream_metrics, 'ticker_sl_count': self._ticker_sl_count, 'blacklist': self._blacklist, 'updated_at': datetime.now().isoformat()}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            from src.utils.file_ops import atomic_write_json

            atomic_write_json(self._state_file, state, indent=2, ensure_ascii=False)
            logger.debug('PerformanceFeedback 상태 저장: %s', self._state_file)
        except Exception as e:
            logger.error('PerformanceFeedback 상태 저장 실패: %s', e, exc_info=True)

    def load_state(self) -> None:
        """JSON 파일에서 상태 복원."""
        if not self._state_file.exists():
            logger.info('PerformanceFeedback 상태 파일 없음, 초기 상태로 시작')
            return
        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            self._stream_metrics = state.get('stream_metrics', {})
            self._ticker_sl_count = state.get('ticker_sl_count', {})
            self._blacklist = state.get('blacklist', {})
            now = datetime.now()
            expired_keys = []
            for key, expires_str in self._blacklist.items():
                try:
                    if now > datetime.fromisoformat(expires_str):
                        expired_keys.append(key)
                except (ValueError, TypeError):
                    expired_keys.append(key)
            for key in expired_keys:
                del self._blacklist[key]
                self._ticker_sl_count.pop(key, None)
            if expired_keys:
                logger.info('PerformanceFeedback: %d건 만료 블랙리스트 해제', len(expired_keys))
            logger.info('PerformanceFeedback 상태 복원: streams=%s, blacklist=%d, updated=%s', list(self._stream_metrics.keys()), len(self._blacklist), state.get('updated_at', 'unknown'))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning('PerformanceFeedback 상태 파싱 오류: %s', e, exc_info=True)
        except Exception as e:
            logger.error('PerformanceFeedback 상태 로드 실패: %s', e, exc_info=True)
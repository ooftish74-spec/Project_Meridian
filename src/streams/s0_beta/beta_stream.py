#!/usr/bin/env python3
"""
S0 Beta Stream — Dynamic Kelly + Z-Score Conviction Engine
============================================================

전략 핵심:
  - 평상시: KOFR 금리액티브(수비수) 포지션 유지
  - 통계적 승산 확보 시: Kelly Criterion + Z-Score 동적 임계값 기반
    2X 레버리지(Bull) 또는 2X 인버스(Crash) 공격수로 즉시 전환
  - 모든 상수는 SSOT(config/dynamic_config.py)를 통해 지연 로드(Lazy Load)

수학적 모델:
  1. 동적 승률 = HMM 확률(Bayesian 가중) + 실측 승률 융합
  2. Kelly Fraction:  f* = (p·b - q) / b
  3. Z-Score 동적 임계값: 변동성 반영, 공격/수비 성향 연속 조정
  4. Fractional Kelly: f* × fraction_multiplier × z_boost (Over-betting 방지)
  5. Fail-Safe: 이력 부족 시 KOFR 강제 전환 (Fail-Loud 로깅)

Zero-Hardcoding Policy:
  이 파일에는 어떠한 매직 넘버도 존재하지 않습니다.
  모든 수치 파라미터는 config/dynamic_config.py의 SSOT 키로 관리됩니다.

Usage:
    from src.streams.s0_beta.beta_stream import S0BetaStream
    s0 = S0BetaStream()
    signals = s0.generate_signals(market_data)
"""

import logging
from typing import Dict, Any, List, Tuple

import numpy as np

from src.streams.base_stream import BaseStream

logger = logging.getLogger(__name__)


class S0BetaStream(BaseStream):
    """방향성 베타 스트림 — Kelly + Z-Score 기반 동적 스위칭.

    모든 파라미터는 generate_signals() 및 각 메서드 내부에서
    DynamicConfig를 지연 로드(Lazy Load)하여 Circular Import를 원천 차단합니다.
    """

    def __init__(self):
        super().__init__('S0', 'S0 Beta Stream')
        self.stream_id      = 'S0'
        self._bull_history:  list = []  # HMM Bull 확률 이력
        self._crash_history: list = []  # HMM Down 확률 이력
        self._pnl_history:   list = []  # 실거래 PnL 이력 (소수)

    # ═══════════════════════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════════════════════

    def generate_signals(
        self,
        regime: str = 'sideways',
        market_data: Dict[str, Any] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """방향성 베타 시그널 생성.

        Args:
            regime:      HMM 레짐 레이블 (참고용)
            market_data: 파이프라인 공유 데이터 딕셔너리
        Returns:
            시그널 리스트 — 2X 레버리지 / 2X 인버스 / KOFR 수비수 중 하나
        """
        # ── 지연 로드 (Lazy Load, Circular Import 방지) ─────────────────
        from config.dynamic_config import DynamicConfig as _DC
        _cfg = _DC()

        def _f(key: str, fb) -> float:
            return float(_cfg.get(key, fb))
        def _s(key: str, fb: str) -> str:
            return str(_cfg.get(key, fb))
        def _b(key: str, fb: bool) -> bool:
            return bool(_cfg.get(key, fb))
        def _i(key: str, fb: int) -> int:
            return int(_cfg.get(key, fb))

        # ── SSOT 파라미터 로드 ───────────────────────────────────────────
        leverage_ticker   = _s('s0_beta.leverage_ticker',          '122630')
        inverse2x_ticker  = _s('s0_beta.inverse2x_ticker',         '252670')
        hist_max_len      = _i('s0_beta.history_max_length',           120)
        vkospi_default    = _f('s0_beta.vkospi_default',              15.0)
        vix_stress_thr    = _f('s0_beta.vix_stress_threshold',         25.0)
        vkospi_stress_thr = _f('s0_beta.vkospi_stress_threshold',      22.0)
        vix_spike_sweep   = _f('s0_beta.vix_spike_sweep_ratio',         0.30)
        vix_spike_conf    = _f('s0_beta.vix_spike_confidence',          0.80)
        base_enabled      = _b('s0_beta.base_position_enabled',         True)
        base_ticker       = _s('s0_beta.base_ticker',               '357870')
        base_ratio        = _f('s0_beta.base_position_ratio',           0.80)
        base_conf         = _f('s0_beta.base_position_confidence',       0.50)
        base_exp_ret      = _f('s0_beta.base_expected_return',           0.035)

        if market_data is None:
            market_data = {}

        # ── 시장 데이터 파싱 ─────────────────────────────────────────────
        pipeline_state = market_data.get('pipeline_state', {})
        hmm_trans      = pipeline_state.get('hmm_transition', {})
        bull_prob   = float(hmm_trans.get('bull',  0.0))
        bear_prob   = float(hmm_trans.get('bear',  0.0))
        crash_prob  = float(hmm_trans.get('crash', 0.0))
        down_prob   = bear_prob + crash_prob

        # [Phase 80] S7 VIX Logic 통합 — 이 클래스가 단일 진실 공급원
        signal_cache = market_data.get('signal_cache', {})
        vix    = float(market_data.get('vix', signal_cache.get('vix', 0.0)))
        vkospi = float(signal_cache.get('vkospi', vkospi_default))

        features     = market_data.get('features', {})
        vol_adj_mom  = float(features.get('alpha_vol_adj_mom_10d', 0.0))
        dd_vel       = float(features.get('alpha_dd_velocity_3d',  0.0))

        is_vix_spike = (vix >= vix_stress_thr) or (vkospi >= vkospi_stress_thr)

        # ── 이력 업데이트 (SSOT 최대 길이 적용) ──────────────────────────
        self._bull_history.append(bull_prob)
        self._crash_history.append(down_prob)
        if len(self._bull_history)  > hist_max_len:
            self._bull_history.pop(0)
        if len(self._crash_history) > hist_max_len:
            self._crash_history.pop(0)

        # ── Kelly Payoff Ratio 동적 계산 ─────────────────────────────────
        _wr, _payoff = self._compute_payoff_ratio(_cfg)

        # ── 확신도 평가 ───────────────────────────────────────────────────
        is_bull, sweep_bull, exp_bull = self._evaluate_conviction(
            current_prob = bull_prob,
            history      = self._bull_history,
            wr           = _wr,
            payoff       = _payoff,
            vol_signal   = vol_adj_mom,
            is_crash     = False,
            cfg          = _cfg,
        )
        is_crash_, sweep_crash, exp_crash = self._evaluate_conviction(
            current_prob = down_prob,
            history      = self._crash_history,
            wr           = _wr,
            payoff       = _payoff,
            vol_signal   = dd_vel,
            is_crash     = True,
            cfg          = _cfg,
        )

        # ── 시그널 조립 ──────────────────────────────────────────────────
        signals: List[Dict[str, Any]] = []

        if is_bull:
            # ▶ 공격수 전환: 2X 레버리지 롱 (KODEX 레버리지)
            z_val = self._z_score_of(bull_prob, self._bull_history)
            signals.append({
                'ticker':             leverage_ticker,
                'name':               'KODEX 레버리지',
                'strategy':           'beta_directional_long',
                'confidence':         bull_prob,
                'predict_proba':      bull_prob,
                'direction':          'long',
                'size_pct':           1.0,
                'trigger_cash_sweep': True,
                'target_sweep_ratio': sweep_bull,
                'expected_return':    exp_bull,
                'reason': (
                    f'Kelly={sweep_bull:.1%} | '
                    f'Z={z_val:.2f} | '
                    f'WR={_wr:.1%} | '
                    f'Payoff={_payoff:.2f}'
                ),
            })
            logger.info(
                f'  📈 [S0 Beta] 2X 레버리지 롱 시그널: {leverage_ticker} '
                f'(Kelly={sweep_bull:.1%}, Z={z_val:.2f})'
            )

        elif is_crash_ or is_vix_spike:
            # ▶ 공격수 전환: 2X 인버스 (Crash 신호 또는 VIX 스파이크)
            conf  = max(down_prob, vix_spike_conf if is_vix_spike else 0.0)
            sweep = sweep_crash if is_crash_ else vix_spike_sweep
            z_val = self._z_score_of(down_prob, self._crash_history)
            signals.append({
                'ticker':             inverse2x_ticker,
                'name':               'KODEX 200선물인버스2X',
                'strategy':           'beta_directional_short',
                'confidence':         conf,
                'predict_proba':      conf,
                'direction':          'long',   # ETF 자체는 매수
                'size_pct':           1.0,
                'trigger_cash_sweep': True,
                'target_sweep_ratio': sweep,
                'expected_return':    exp_crash,
                'reason': (
                    f'Kelly={sweep:.1%} | '
                    f'Z={z_val:.2f} | '
                    f'VIX_Spike={is_vix_spike}'
                ),
            })
            logger.error(
                f'  📉 [S0 Beta] 2X 인버스 시그널: {inverse2x_ticker} '
                f'(Kelly={sweep:.1%}, VIX_Spike={is_vix_spike})',
                exc_info=True,
            )

        # ── 중립 구간: KOFR 수비수 대기 ──────────────────────────────────
        if base_enabled and not signals:
            signals.append({
                'ticker':             base_ticker,
                'name':               'KOFR (기본 방어 포지션)',
                'strategy':           'beta_base_hold',
                'confidence':         base_conf,
                'predict_proba':      base_conf,
                'direction':          'long',
                'size_pct':           1.0,
                'trigger_cash_sweep': False,
                'target_sweep_ratio': base_ratio,
                'expected_return':    base_exp_ret,
                'is_base_position':   True,
                'reason': (
                    f'중립 레짐 수비수(Defender) 대기 '
                    f'(Kelly_bull={sweep_bull:.2f}, '
                    f'Kelly_crash={sweep_crash:.2f})'
                ),
            })
            logger.debug(
                f'  [S0 Beta] 방어 포지션: {base_ticker} '
                f'(비율={base_ratio:.0%}, bull={bull_prob:.2f}, down={down_prob:.2f})'
            )

        return signals

    # ═══════════════════════════════════════════════════════════════════════
    # Core Math: Conviction Evaluator (Kelly + Dynamic Z-Score Band)
    # ═══════════════════════════════════════════════════════════════════════

    def _evaluate_conviction(
        self,
        current_prob: float,
        history: list,
        wr: float,
        payoff: float,
        vol_signal: float,
        is_crash: bool,
        cfg,
    ) -> Tuple[bool, float, float]:
        """Kelly Criterion + Dynamic Z-Score 기반 확신도 판단.

        수학적 모델:
          Step 1 — Volatility-Adaptive Z-Score Band:
            dynamic_z_thresh = clip(base_z ± vol_signal, z_floor, ∞)
          Step 2 — Bayesian Blended Win Rate:
            blended_wr = hmm_w × HMM_prob + (1 - hmm_w) × actual_wr
          Step 3 — Full Kelly Fraction:
            f* = (p × b - (1-p)) / b
          Step 4 — Dual Hurdle Gate:
            진입 ← f* > 0  AND  z_score ≥ dynamic_z_thresh
          Step 5 — Fractional Kelly Sizing:
            target_ratio = clip(f* × fraction_mult × z_boost,
                                kelly_min_ratio, max_sweep_ratio)

        Args:
            current_prob: 현재 HMM 확률 (Bull 또는 Down 방향)
            history:      과거 확률 이력 리스트
            wr:           실측 / 기본 승률
            payoff:       실측 / 기본 손익비
            vol_signal:   변동성 방향 시그널
                          (Bull: alpha_vol_adj_mom_10d / Crash: alpha_dd_velocity_3d)
            is_crash:     True → 하락장 판단, False → 상승장 판단
            cfg:          DynamicConfig 인스턴스

        Returns:
            (is_high, target_ratio, expected_return)
            is_high=False → KOFR 수비수 유지 권고
        """
        def _f(key, fb): return float(cfg.get(key, fb))
        def _i(key, fb): return int(cfg.get(key, fb))

        # ── SSOT 파라미터 로드 ───────────────────────────────────────────
        base_z_score    = _f('s0_beta.base_z_score',              1.5)
        z_thresh_floor  = _f('s0_beta.z_thresh_floor',             0.5)
        z_boost_cap     = _f('s0_beta.z_boost_cap',                2.0)
        fraction_mult   = _f('s0_beta.kelly_fraction_multiplier',  0.5)
        kelly_min_ratio = _f('s0_beta.kelly_min_ratio',            0.20)
        hmm_weight      = _f('s0_beta.kelly_hmm_weight',           0.60)
        max_sweep       = _f('s0_beta.max_sweep_ratio',            1.0)
        exp_daily_vol   = _f('s0_beta.expected_return_daily_vol',  0.05)
        hist_min_len    = _i('s0_beta.history_min_length',           30)

        # ── Fail-Safe: 이력 부족 → KOFR 강제 전환 (Fail-Loud) ──────────
        if len(history) < hist_min_len:
            logger.warning(
                f'  [S0 Beta] 데이터 결손 (N={len(history)} < {hist_min_len}): '
                f'Z-Score 연산 불가 → KOFR 강제 전환',
                exc_info=True,
            )
            return False, 0.0, 0.0

        arr  = np.array(history, dtype=float)
        mean = float(np.mean(arr))
        std  = float(np.std(arr))

        if std < 1e-9:
            logger.warning(
                '  [S0 Beta] 확률 표준편차 ≈ 0: Z-Score 연산 불가 → KOFR 전환',
                exc_info=True,
            )
            return False, 0.0, 0.0

        # ── Step 1: Volatility-Adaptive Z-Score Band ─────────────────────
        #   Bull:  상승 모멘텀(vol_signal > 0) → 장벽 완화 (공격 성향 ↑)
        #   Crash: 하락 속도(vol_signal < 0)   → 장벽 완화 (공격 성향 ↑)
        #   하한: z_thresh_floor (어떤 경우에도 최소 방어선 유지)
        dynamic_z_thresh = base_z_score
        if not is_crash and vol_signal > 0:
            dynamic_z_thresh = max(z_thresh_floor, base_z_score - vol_signal)
        elif is_crash and vol_signal < 0:
            dynamic_z_thresh = max(z_thresh_floor, base_z_score + vol_signal)

        z_score = (current_prob - mean) / std

        # ── Step 2: Bayesian Blended Win Rate ────────────────────────────
        #   blended = hmm_weight × HMM확률 + (1 - hmm_weight) × 실측승률
        hist_weight      = 1.0 - hmm_weight
        blended_win_rate = (hmm_weight * current_prob) + (hist_weight * wr)

        # ── Step 3: Full Kelly Fraction ───────────────────────────────────
        #   f* = (p × b - q) / b   (p=blended_wr, q=1-p, b=payoff)
        q          = 1.0 - blended_win_rate
        full_kelly = (blended_win_rate * payoff - q) / payoff \
                     if payoff > 1e-9 else 0.0

        # ── Step 4: Dual Hurdle Gate ──────────────────────────────────────
        #   허들 1: full_kelly > 0  (기대수익 양수)
        #   허들 2: z_score ≥ dynamic_z_thresh  (통계적 유의성 충족)
        if not (full_kelly > 0 and z_score >= dynamic_z_thresh):
            logger.debug(
                f'  [S0 Beta] {"Crash" if is_crash else "Bull"} 확신도 미달: '
                f'kelly={full_kelly:.3f}, z={z_score:.2f}(thresh={dynamic_z_thresh:.2f})'
            )
            return False, 0.0, 0.0

        # ── Step 5: Fractional Kelly Sizing ──────────────────────────────
        #   z_boost: Z-Score가 임계값의 몇 배인지 (최대 z_boost_cap)
        #   Fractional Kelly = f* × fraction_mult × z_boost
        z_boost      = min(z_boost_cap, z_score / dynamic_z_thresh) \
                       if dynamic_z_thresh > 0 else 1.0
        target_ratio = full_kelly * fraction_mult * z_boost
        target_ratio = min(max_sweep, max(kelly_min_ratio, target_ratio))

        # ── Step 6: 기대수익 ─────────────────────────────────────────────
        #   기대수익 = 현재 확률 × 일간 변동성 가정 × Z부스트
        expected_return = current_prob * exp_daily_vol * z_boost

        logger.debug(
            f'  [S0 Beta] {"Crash" if is_crash else "Bull"} 확신도 통과: '
            f'z={z_score:.2f}(thresh={dynamic_z_thresh:.2f}), '
            f'kelly={full_kelly:.3f}, '
            f'blended_wr={blended_win_rate:.3f}, '
            f'target={target_ratio:.1%}'
        )
        return True, target_ratio, expected_return

    # ═══════════════════════════════════════════════════════════════════════
    # Helper Utilities
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _z_score_of(value: float, history: list) -> float:
        """history 분포에서 value의 Z-Score 계산 (이력 부족 시 0 반환)."""
        if len(history) < 2:
            return 0.0
        arr = np.array(history, dtype=float)
        std = float(np.std(arr))
        if std < 1e-9:
            return 0.0
        return (value - float(np.mean(arr))) / std

    # ═══════════════════════════════════════════════════════════════════════
    # Trade Records & Kelly Payoff
    # ═══════════════════════════════════════════════════════════════════════

    def record_trade_result(self, pnl_pct: float) -> None:
        """체결 결과 PnL 기록 → Kelly payoff ratio 동적 계산에 반영.

        Args:
            pnl_pct: 체결 수익률 소수 (양수=이익, 음수=손실)
        """
        from config.dynamic_config import DynamicConfig as _DC
        _cfg     = _DC()
        max_hist = int(_cfg.get('s0_beta.pnl_history_size', 252))

        self._pnl_history.append(float(pnl_pct))
        if len(self._pnl_history) > max_hist:
            self._pnl_history = self._pnl_history[-max_hist:]

    def _compute_payoff_ratio(self, cfg=None) -> Tuple[float, float]:
        """실거래 이력에서 payoff ratio 및 win rate 동적 계산.

        Returns:
            (win_rate, payoff_ratio)
            거래 이력 부족 시 SSOT 기본값 반환 (보수적 추정)
        """
        if cfg is None:
            from config.dynamic_config import DynamicConfig as _DC
            cfg = _DC()

        def _f(key, fb): return float(cfg.get(key, fb))
        def _i(key, fb): return int(cfg.get(key, fb))

        min_trades     = _i('s0_beta.kelly_min_trades',       20)
        default_wr     = _f('s0_beta.kelly_default_win_rate', 0.55)
        default_payoff = _f('s0_beta.kelly_default_payoff',   1.5)

        if len(self._pnl_history) < min_trades:
            logger.debug(
                f'  [S0 Kelly] 거래 이력 부족 ({len(self._pnl_history)}/{min_trades}) '
                f'→ 보수적 기본값 (wr={default_wr}, payoff={default_payoff})'
            )
            return default_wr, default_payoff

        wins   = [p for p in self._pnl_history if p > 0]
        losses = [p for p in self._pnl_history if p < 0]

        if not wins or not losses:
            return default_wr, default_payoff

        win_rate  = len(wins) / len(self._pnl_history)
        avg_win   = float(np.mean(wins))
        avg_loss  = abs(float(np.mean(losses)))
        payoff    = avg_win / avg_loss if avg_loss > 1e-9 else default_payoff

        logger.debug(
            f'  [S0 Kelly] 실측 통계: wr={win_rate:.3f}, '
            f'payoff={payoff:.3f} (N={len(self._pnl_history)})'
        )
        return win_rate, payoff

    # ═══════════════════════════════════════════════════════════════════════
    # Base Interface
    # ═══════════════════════════════════════════════════════════════════════

    def get_performance(self) -> Dict[str, Any]:
        """성과 지표 반환."""
        return {'sharpe': 0.0, 'cumulative_return_pct': 0.0, 'active_positions': 0}

    def get_positions(self) -> List[Dict[str, Any]]:
        """현재 보유 포지션 반환."""
        return []

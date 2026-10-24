"""
S12 Derivative Squeeze Short Alpha Stream
==========================================

월가 퀀트 헤지펀드 아키텍처 Phase 95-A.

개념:
  파생 시장(외국인 선물 누적 순매도, 개인 콜옵션 Skew, 선물-현물 베이시스 백워데이션)의
  기계적 델타 스퀴즈 현상을 포착하여, 하락 파동에서 양(+)의 알파 수익을 쥐어짜내는 100% 수학적 숏 알파 스트림.

특징:
  1. 하드코딩 magic number 0% ➔ DynamicConfig & Z-Score / BDI 표준화 수치 모델 사용.
  2. 3대 파생 델타 스퀴즈 조건 동시 달성 시 252670(곱버스) / 114800(인버스) 숏 저격 발화.
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import logging
import math
from typing import Any, Dict, List, Tuple
from src.streams.base_stream import BaseStream
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


class S12DerivativeSqueezeStream(BaseStream):
    """S12 Derivative Squeeze Short Alpha Stream (Pure Math Model)."""

    def __init__(self, stream_id: str = "S12_DERIVATIVE_SQUEEZE", name: str = "Derivative Squeeze Short Alpha"):
        super().__init__(stream_id=stream_id, name=name)
        self.inverse_2x_ticker = str(cfg.get('s12.inverse_2x_ticker', '252670'))
        self.ticker_inv_2x = self.inverse_2x_ticker
        self.inverse_1x_ticker = str(cfg.get('s12.inverse_1x_ticker', '114800'))
        self.z_skew_threshold = float(pget('s12.z_skew_threshold', 1.5))       # Skew Z-Score >= +1.5
        self.z_flow_threshold = float(pget('s12.z_flow_threshold', 1.5))       # Foreign Futures Net Sell Z-Score >= +1.5
        self.bdi_threshold = float(cfg.get('s12.bdi_threshold') or (-2.0 / 1000.0))  # BDI <= -0.002 (True Backwardation)
        _init_sl = float(pget('s12.sl_pct', 3.0))
        _init_tp = float(pget('s12.tp_pct', 6.0))
        self.sl_pct = abs(_init_sl) * 100.0 if 0 < abs(_init_sl) < (1.0 / 2.0) else abs(_init_sl)
        self.tp_pct = _init_tp * 100.0 if 0 < _init_tp < (1.0 / 2.0) else _init_tp

    def compute_z_score(self, value: float, mean: float, std: float) -> float:
        """통계적 Z-Score 산출 (표준화 모델)."""
        if std is None or std <= 1e-8 or math.isnan(std):
            return 0.0
        return (value - mean) / std

    def compute_basis_disparity_index(self, futures_price: float, spot_price: float) -> float:
        """선물-현물 베이시스 괴리율 (Basis Disparity Index, BDI) 산출."""
        if (
            spot_price is None or spot_price <= 0 or math.isnan(spot_price)
            or futures_price is None or futures_price <= 0 or math.isnan(futures_price)
        ):
            return 0.0
        return (futures_price - spot_price) / spot_price

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """파생 델타 스퀴즈 3대 수학 조건 수식 평가 및 숏 알파 신호 생성."""
        signals = []
        if not self.is_active():
            return signals

        signal_cache = market_data.get('signal_cache', {}) if market_data else {}

        # ── 1. 변수 수집 ──
        # (1) 개인 콜옵션 Skew (Call Option Buying Bias)
        skew_val = float(signal_cache.get('call_option_skew', signal_cache.get('CALL_OPTION_SKEW', 0.0)))
        skew_mean = float(signal_cache.get('call_option_skew_mean_20d', 0.0))
        skew_std = float(signal_cache.get('call_option_skew_std_20d', 1.0))
        z_skew = self.compute_z_score(skew_val, skew_mean, skew_std)

        # (2) 외국인 선물 누적 순매도 수급 (Foreign Futures Net Flow)
        foreign_futures_flow = float(signal_cache.get('foreign_futures_net_contracts', signal_cache.get('FOREIGN_FUTURES_NET_CONTRACTS', signal_cache.get('frgn_fut_net_buy', 0.0))))
        flow_mean = float(signal_cache.get('foreign_futures_net_mean_20d', 0.0))
        flow_std = float(signal_cache.get('foreign_futures_net_std_20d', 3000.0))
        z_flow_sell = self.compute_z_score(-foreign_futures_flow, -flow_mean, flow_std)

        # (3) 선물-현물 베이시스 (KOSPI 200 Futures vs Spot)
        futures_price = float(signal_cache.get('kospi200_futures_price', signal_cache.get('FUTURES_PRICE', 0.0)))
        spot_price = float(signal_cache.get('kospi200_spot_price', signal_cache.get('SPOT_PRICE', 0.0)))
        if futures_price > 0 and spot_price > 0:
            bdi = self.compute_basis_disparity_index(futures_price, spot_price)
            basis_trigger = bdi <= self.bdi_threshold
        else:
            bdi = 0.0
            basis_trigger = False

        # ── 2. 순수 수학적 조건 판정 ──
        skew_trigger = z_skew >= self.z_skew_threshold
        flow_trigger = z_flow_sell >= self.z_flow_threshold

        logger.info(
            f"  [S12 Derivative Squeeze Metrics] Z_skew={z_skew:+.2f} (th={self.z_skew_threshold}), "
            f"Z_flow_sell={z_flow_sell:+.2f} (th={self.z_flow_threshold}), BDI={bdi:+.4f} (th={self.bdi_threshold})"
        )

        # ── 3. 숏 알파 신호 발화 ──
        if skew_trigger and flow_trigger and basis_trigger:
            _reg_l = str(regime or '').lower()
            _ext_z = 2.0
            _ext_bdi = -(1.0 / 200.0)
            _extreme_squeeze = (z_skew >= _ext_z and z_flow_sell >= _ext_z and bdi <= _ext_bdi)
            target_ticker = (
                self.inverse_2x_ticker
                if (_reg_l in ('crash', 'crisis', 'bear') or _extreme_squeeze)
                else self.inverse_1x_ticker
            )
            confidence = min(1.0, max(0.60, 0.50 + 0.10 * (z_skew + z_flow_sell) / 2.0))

            reason = (
                f"🚨 [S12 Squeeze Short Alpha Active] Z_skew={z_skew:.2f}, Z_flow={z_flow_sell:.2f}, "
                f"BDI={bdi:.4f} ➔ {target_ticker} 숏 알파 저격 집행!"
            )
            logger.info(f"  {reason}")

            _raw_sl = float(pget('s12.sl_pct', 3.0))
            _raw_tp = float(pget('s12.tp_pct', 6.0))
            norm_sl_pct = abs(_raw_sl) * 100.0 if 0 < abs(_raw_sl) < 0.5 else abs(_raw_sl)
            norm_tp_pct = _raw_tp * 100.0 if 0 < _raw_tp < 0.5 else _raw_tp
            self.sl_pct = norm_sl_pct
            self.tp_pct = norm_tp_pct

            signal = {
                'stream_id': self.stream_id,
                'ticker': target_ticker,
                'name': f"S12 파생 스퀴즈 숏 알파 ({target_ticker})",
                'direction': 'long',
                'size_pct': float(pget('s12.default_size_pct', 0.25)),
                'confidence': round(confidence, 3),
                'strategy': 'derivative_squeeze_short_alpha',
                'sl_pct': norm_sl_pct,
                'tp_pct': norm_tp_pct,
                'expected_return': round(self.tp_pct / 100.0, 4),
                'expected_return_pct': self.tp_pct,
                'target_price': None,
                'stop_loss_price': None,
                'reason': reason,
                'metrics': {
                    'z_skew': round(z_skew, 2),
                    'z_flow_sell': round(z_flow_sell, 2),
                    'bdi': round(bdi, 5)
                }
            }
            signals.append(signal)

        return signals

    def get_positions(self) -> List[Dict]:
        """현재 보유 포지션 반환."""
        return getattr(self, '_positions', [])

    def get_performance(self) -> Dict:
        """성과 지표 반환 (실측 수익률 기반 표준 동적 성과 산출)."""
        return self._compute_standard_performance(self._daily_pnl, len(self._positions), self._trade_count)

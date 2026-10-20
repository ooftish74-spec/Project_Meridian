"""
Alpha 4: S3 Track C Cross-Asset Macro Sleeve (src/streams/s3_active_macro/cross_asset_sleeve.py)
========================================================================================

미국 상장 대체자산(금, 원유, 구리, 국채, 달러)의 상대 모멘텀 스피레드 및 Flight-to-Quality 신호를 수확하는 엔진.

대체자산 유니버스:
  - Gold: GLD (SPDR Gold Shares) / IAU
  - Crude Oil: USO (United States Oil Fund)
  - Copper: CPER (United States Copper Index Fund)
  - Treasury: TLT (iShares 20+ Year Treasury) / IEF
  - US Dollar: UUP (Invesco DB US Dollar Index Bullish)

수학적 모델:
  1. Cross-Asset Momentum Spread:
     Spread_k = Return_k(N-days) - Return_SPY(N-days)
  2. Conviction Signal:
     LONG if (Spread_k >= dynamic_threshold OR (regime in ['bear','crash'] AND Return_k > 0))

Zero-Hardcoding Policy:
  모든 임계치 및 유니버스 설정은 DynamicConfig ('s3.track_c.*')에서 지연 로드됩니다.
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import logging
from typing import Dict, List, Any
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S3CrossAssetSleeve:
    """S3 Track C: 대체자산/원자재/FX 교차 모멘텀 슬리브 엔진."""

    def __init__(self):
        pass

    def generate_track_c_signals(self, regime: str, market_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """대체자산 유니버스 대상 Track C 알파 4 신호 생성.

        Args:
            regime: HMM 레짐 레이블 ('bull', 'caution', 'bear', 'crash')
            market_data: 파이프라인 공유 데이터 딕셔너리

        Returns:
            대체자산 롱 신호 리스트 (미국 전용)
        """
        # Zero-Hardcoding 파라미터 지연 로드 + 수리적 동적 보정
        from src.utils.dynamic_quant_engine import get_quant_engine
        _dqe = get_quant_engine()
        spread_threshold = float(pget('s3.track_c.spread_threshold', 0.005))
        min_confidence = float(pget('s3.track_c.min_confidence', 0.75))

        cross_universe = dict(cfg.get('s3.track_c.universe', {
            'GLD': {'name': 'SPDR Gold Shares', 'category': 'gold'},
            'USO': {'name': 'United States Oil Fund', 'category': 'oil'},
            'CPER': {'name': 'United States Copper Index Fund', 'category': 'copper'},
            'TLT': {'name': 'iShares 20+ Year Treasury Bond', 'category': 'treasury'},
            'UUP': {'name': 'Invesco DB US Dollar Index Bullish', 'category': 'dollar'}
        }))
        if regime in ('bear', 'crash'):
            for _inv_tk in _dqe.get_inverse_hedge_tickers('US')[:4]:
                cross_universe.setdefault(_inv_tk, {'name': _inv_tk, 'category': 'inverse_hedge'})
            cross_universe.setdefault('SQQQ', {'name': 'ProShares UltraPro Short QQQ', 'category': 'inverse_hedge'})
            cross_universe.setdefault('SOXS', {'name': 'Direxion Daily Semiconductor Bear 3X', 'category': 'inverse_hedge'})

        def _get_live_5d_return(tk: str) -> float:
            try:
                from pathlib import Path
                import pandas as pd
                pq_path = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'us_stocks' / 'prices' / f'{tk}.parquet'
                if pq_path.exists():
                    df = pd.read_parquet(pq_path)
                    close_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                    if close_col and len(df) >= 6:
                        s = df[close_col].dropna()
                        if len(s) >= 6 and float(s.iloc[-6]) > 0:
                            return float(s.iloc[-1]) / float(s.iloc[-6]) - 1.0
            except Exception:
                pass
            return 0.0

        def _get_live_1d_return(tk: str) -> float:
            key_1d = f"{tk.lower()}_return_1d"
            if key_1d in market_data:
                return float(market_data.get(key_1d, 0.0) or 0.0)
            try:
                from pathlib import Path
                import pandas as pd
                pq_path = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'us_stocks' / 'prices' / f'{tk}.parquet'
                if pq_path.exists():
                    df = pd.read_parquet(pq_path)
                    close_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                    if close_col and len(df) >= 2:
                        s = df[close_col].dropna()
                        if len(s) >= 2 and float(s.iloc[-2]) > 0:
                            return float(s.iloc[-1]) / float(s.iloc[-2]) - 1.0
            except Exception:
                pass
            return 0.0

        has_explicit_returns = ('spy_return_5d' in market_data) or ('sp500_return_5d' in market_data) or ('asset_returns' in market_data)
        signals = []
        if has_explicit_returns:
            spy_return = float(market_data.get('spy_return_5d', market_data.get('sp500_return_5d', 0.0)) or 0.0)
        else:
            spy_return = _get_live_5d_return('SPY')

        for ticker, meta in cross_universe.items():
            is_inv_hedge = (meta.get('category') == 'inverse_hedge')
            if is_inv_hedge and regime not in ('bear', 'crash'):
                continue

            key_ret = f"{ticker.lower()}_return_5d"
            if key_ret in market_data or (isinstance(market_data.get('asset_returns'), dict) and ticker in market_data['asset_returns']):
                asset_ret = float(market_data.get(key_ret, market_data.get('asset_returns', {}).get(ticker, 0.0)) or 0.0)
            elif not has_explicit_returns:
                asset_ret = _get_live_5d_return(ticker)
            else:
                asset_ret = 0.0
            spread = asset_ret - spy_return

            if is_inv_hedge:
                ret_1d = _get_live_1d_return(ticker)
                if ret_1d < 0.0:
                    logger.info(f"  🛑 [S3 Track C Inverse Guard] {ticker}: 5일 수익률({asset_ret:+.2%}) 양수이나 당일/1일 수익률({ret_1d:+.2%}) 음수 전환 — 역방향 추격 매수 차단")
                    continue

            is_flight_to_quality = (regime in ['bear', 'crash'] if is_inv_hedge else regime in ['bear', 'crash', 'caution']) and (asset_ret > 0.0)
            is_momentum_outperformance = spread >= spread_threshold and (not is_inv_hedge or asset_ret > 0.0)

            if is_flight_to_quality or is_momentum_outperformance:
                confidence = min(1.0, min_confidence + abs(spread) * 2.0)
                reason = (
                    f"🛡️ [Alpha 4: Track C Cross-Asset] {meta['name']} ({ticker}) 롱 진입 | "
                    f"Ret5d={asset_ret:+.2%} vs SPY={spy_return:+.2%} (Spread={spread:+.2%}, Regime={regime})"
                )
                logger.info(f"  {reason}")

                try:
                    _barriers = _dqe.compute_dynamic_exit_barriers(ticker)
                    _tp_pct = float(_barriers.get('take_profit_pct', max(2.0, abs(spread) * 100.0)))
                    _sl_pct = abs(float(_barriers.get('stop_loss_pct', 1.5)))
                except Exception:
                    _tp_pct = round(max(2.0, abs(spread) * 100.0), 2)
                    _sl_pct = 1.5

                sig = {
                    'stream_id': 'S3_C',
                    'ticker': ticker,
                    'name': meta['name'],
                    'direction': 'long',
                    'confidence': round(confidence, 3),
                    'size_pct': round(min(0.20, confidence * 0.15), 4),
                    'tp_pct': round(_tp_pct, 2),
                    'sl_pct': round(_sl_pct, 2),
                    'expected_return_pct': round(_tp_pct, 2),
                    'strategy': 's3_cross_asset_momentum',
                    'category': meta['category'],
                    'market': 'US',
                    'tax_drag_pct': 0.00, # 통합증거금 가상환전 0원
                    'reason': reason
                }
                signals.append(sig)

        return signals

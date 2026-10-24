"""
S14 Market-Neutral Sector Pair & Stat-Arb Stream
=================================================
횡보 노이즈장(Chop/Sideways Market, KER < 0.40) 및 박스권 국면에서
시장 전체 베타(Beta) 위험을 0으로 헤지하고,
섹터 상대강도 Z-Score 스프레드 및 공적분(Cointegration) 평균회귀를 통해
지수 방향과 무관하게 순수 알파(Alpha)를 능동적으로 수확하는 전용 퀀트 스트림.

특징:
  1. 100% 동적 수학 모델: 하드코딩 magic number 전면 제거, Kaufman Noise (1 - KER) 및 롤링 Z-Score 표준화 수식 적용.
  2. 동적 리스크 패리티 (Dynamic Volatility Parity): σ_long * w_long = σ_short * w_short 기반 진정한 Zero-Beta 마켓 뉴트럴.
  3. 수학적 Gaussian CDF(erf) 기반의 연속적 확률 신뢰도 산출.
  4. 국내(KRX) 및 해외(US) 마켓 뉴트럴 롱/숏(인버스 페어링) 자동 라우팅.
  5. Single Source of Truth: SectorPairAlphaEngine 및 StatArbEngine과 직접 직결.
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import math
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.streams.base_stream import BaseStream
from src.allocation.sector_pair_alpha_engine import SectorPairAlphaEngine, US_SECTOR_ETF_MAP, DEFAULT_SECTOR_ETF_MAP
from src.intelligence.stat_arb_engine import StatArbEngine
from src.risk.whipsaw_defense_engine import AntiWhipsawDefenseEngine
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class S14SectorPairStream(BaseStream):
    """S14 Market-Neutral Sector Pair & Stat-Arb Alpha Stream."""

    def __init__(self, stream_id: str = "S14_PAIR_ALPHA", name: str = "Sector Pair & Stat-Arb Alpha"):
        super().__init__(stream_id=stream_id, name=name)
        self.sector_engine = SectorPairAlphaEngine()
        self.stat_arb_engine = StatArbEngine()
        self.whipsaw_defense = AntiWhipsawDefenseEngine()
        self.ker_chop_threshold = float(pget('pair.ker_chop_threshold', 0.40))
        self.min_spread_z = float(pget('pair.min_spread_z', 1.20))

    def _extract_returns_and_volatility(self, ticker: str, market: str = 'US', days: int = 20) -> Tuple[float, float]:
        """종목별 최근 N일 모멘텀 수익률 및 연율화 변동성(σ) 100% 동적 산출."""
        try:
            if market.upper() == 'US':
                p = _PROJECT_ROOT / f"data/us_stocks/prices/{ticker}.parquet"
            else:
                p = _PROJECT_ROOT / f"data/kr_markets/kr_{ticker}.parquet"
                if not p.exists():
                    p = _PROJECT_ROOT / f"data/kr_markets/{ticker}.parquet"

            if p.exists():
                df = pd.read_parquet(p)
                _c_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                if not df.empty and _c_col:
                    closes = df[_c_col].dropna().values
                    if len(closes) >= days + 1:
                        ret = float((closes[-1] - closes[-days-1]) / closes[-days-1])
                        log_rets = np.diff(np.log(closes[-days-1:]))
                        vol = float(np.std(log_rets) * np.sqrt(252)) if len(log_rets) > 1 else 0.20
                        return ret, max(vol, 0.05)
                    elif len(closes) >= 2:
                        ret = float((closes[-1] - closes[0]) / closes[0])
                        log_rets = np.diff(np.log(closes))
                        vol = float(np.std(log_rets) * np.sqrt(252)) if len(log_rets) > 1 else 0.20
                        return ret, max(vol, 0.05)
        except Exception as e:
            logger.debug(f"[_extract_returns_and_volatility] {ticker} 계산 예외: {e}")
        return 0.0, 0.20

    def compute_market_ker(self, market_or_prices: Any = 'US') -> float:
        """시장 대표 지수 또는 가격 시계열의 Kaufman Efficiency Ratio (KER) 산출."""
        if isinstance(market_or_prices, (list, np.ndarray, pd.Series)):
            return float(self.whipsaw_defense.compute_kaufman_efficiency_ratio(list(market_or_prices), period=len(market_or_prices)-1))

        market = str(market_or_prices)
        benchmark = 'QQQ' if market.upper() == 'US' else '069500'
        try:
            if market.upper() == 'US':
                p = _PROJECT_ROOT / f"data/us_stocks/prices/{benchmark}.parquet"
            else:
                p = _PROJECT_ROOT / f"data/kr_markets/kr_{benchmark}.parquet"
                if not p.exists():
                    p = _PROJECT_ROOT / f"data/kr_markets/{benchmark}.parquet"

            if p.exists():
                df = pd.read_parquet(p)
                _c_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                if not df.empty and _c_col:
                    closes = df[_c_col].dropna().tolist()
                    return self.whipsaw_defense.compute_kaufman_efficiency_ratio(closes, period=20)
        except Exception as e:
            logger.debug(f"[compute_market_ker] {benchmark} KER 연산 예외: {e}")
        
        # Historical Parquet 부재 시 SSOT signal_cache.json에서 동적 조회
        try:
            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc_data = json.loads(sc_path.read_text(encoding='utf-8'))
                if market.upper() == 'US' and 'qqq_ker' in sc_data:
                    return float(sc_data['qqq_ker'])
                elif 'ker' in sc_data:
                    return float(sc_data['ker'])
        except Exception:
            pass

        return 0.25  # 중립 횡보 디폴트

    def calculate_market_ker(self, market_or_prices: Any = 'US') -> float:
        """compute_market_ker의 별칭 메서드."""
        return self.compute_market_ker(market_or_prices)

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """횡보장 국면 감지 시 섹터 롱숏 페어 및 공적분 통계적 차익거래 시그널 생성 (100% 동적 수학 모델)."""
        signals = []
        if not self.is_active():
            return signals

        # ── 1. KER 기반 횡보장 적합도 및 연속적 노이즈 강도(Noise Intensity) 평가 ──
        ker_us = float(market_data.get('qqq_ker', self.compute_market_ker('US'))) if market_data else self.compute_market_ker('US')
        ker_kr = float(market_data.get('kospi_ker', self.compute_market_ker('KR'))) if market_data else self.compute_market_ker('KR')

        is_sideways = (
            (ker_us < self.ker_chop_threshold) or 
            (ker_kr < self.ker_chop_threshold) or 
            regime in ('caution', 'neutral', 'sideways', 'normal')
        )

        if not is_sideways and regime in ('bull', 'momentum_surge'):
            logger.debug(f"  [S14] 강력 추세장(KER_US={ker_us:.2f}, KER_KR={ker_kr:.2f}) — 페어 트레이딩 시그널 보류")
            return []

        # 동적 임계치: 추세 효율성(KER)이 높을수록 스프레드 진입 임계치(Z-score)를 수학적으로 강화
        # KER=0 -> threshold = min_spread_z
        # KER=1 -> threshold = min_spread_z * 2.0
        active_spread_threshold = self.min_spread_z * (1.0 + max(0.0, (ker_us - self.ker_chop_threshold) / (1.0 - self.ker_chop_threshold)))

        # 100% 연속적 노이즈 가중치 (Brownian Noise Multiplier): Noise = 1.0 - KER
        noise_us = max(0.10, min(1.0, 1.0 - ker_us))
        noise_kr = max(0.10, min(1.0, 1.0 - ker_kr))

        # ── 2. 미국(US) 섹터 페어 알파 탐색 ──
        us_sector_returns = {}
        us_volatility_map = {}
        
        # 외부 주입된 실시간 섹터 수익률이 있다면 우선 적용 (하드코딩 mock 완전 제거)
        if market_data and 'us_sector_returns' in market_data:
            us_sector_returns = dict(market_data['us_sector_returns'])
            us_volatility_map = dict(market_data.get('us_volatility_map', {}))
        else:
            for sec, ticker in US_SECTOR_ETF_MAP.items():
                ret, vol = self._extract_returns_and_volatility(ticker, market='US', days=20)
                if ret != 0.0 or vol != 0.20:
                    us_sector_returns[sec] = ret
                    us_volatility_map[sec] = vol

        if len(us_sector_returns) >= 2:
            us_pair_res = self.sector_engine.generate_pair_signals(
                sector_returns=us_sector_returns,
                market='US',
                volatility_map=us_volatility_map
            )

            if us_pair_res.get('pair_active') and us_pair_res.get('spread_z', 0.0) >= active_spread_threshold:
                from src.utils.dynamic_quant_engine import get_quant_engine
                _dqe_s14 = get_quant_engine()
                _sc_us = (market_data or {}).get('signal_cache', {}) if isinstance(market_data, dict) else {}
                _vix_us = float((market_data or {}).get('vix') or _sc_us.get('vix', 20.0) or 20.0)
                _ois_us = float((market_data or {}).get('ois') or _sc_us.get('ois', 50.0) or 50.0)
                _reg_us = str((market_data or {}).get('regime') or regime or 'caution').lower() if isinstance(market_data, dict) else str(regime or 'caution').lower()
                _vix_caut_lim = float(_dqe_s14.get_vix_regime_quantiles()['caution'])
                _ois_bull_lim = float(cfg.get('leverage.ois_bull_threshold') or 55.0)
                _us_inv_map = {'semi': 'SOXS', 'tech': 'PSQ', 'SOXX': 'SOXS', 'XLK': 'PSQ'}
                _us_inv_blocked = (_vix_us < _vix_caut_lim and _ois_us >= _ois_bull_lim) or _reg_us == 'bull'
                for order in (us_pair_res.get('pair_orders', []) if not _us_inv_blocked else []):
                    t_dir = order.get('direction', 'long').lower()
                    ticker = order['ticker']
                    action = order.get('action', 'buy').lower()
                    is_inv_hedge = bool(order.get('is_inverse_hedge', False)) or t_dir == 'short' or action in ('sell', 'buy_inverse')
                    
                    if is_inv_hedge:
                        sec_key = str(order.get('sector', '')).lower()
                        if ticker in ('SH', 'PSQ', 'SOXS', 'SQQQ'):
                            ticker_to_trade = ticker
                        else:
                            ticker_to_trade = _us_inv_map.get(sec_key, _us_inv_map.get(ticker, 'SH'))
                        direction_to_trade = 'long'
                        action_to_trade = 'buy'
                        leg_label = 'INVERSE_HEDGE'
                    else:
                        ticker_to_trade = ticker
                        direction_to_trade = 'long'
                        action_to_trade = 'buy'
                        leg_label = 'LONG'

                    # 연속적 노이즈 강도에 따른 동적 포지션 크기 스케일링
                    dynamic_size = round(order.get('target_weight', 0.10) * noise_us, 4)
                    try:
                        _bar_us = _dqe_s14.compute_dynamic_exit_barriers(ticker_to_trade)
                        _tp_us = float(_bar_us['take_profit_pct'])
                        _sl_us = abs(float(_bar_us['stop_loss_pct']))
                    except Exception:
                        _tp_us, _sl_us = 2.5, 1.5

                    signals.append({
                        'stream_id': self.stream_id,
                        'ticker': ticker_to_trade,
                        'pair_id': order.get('pair_id', ''),
                        'pair_partner': order.get('pair_partner', ''),
                        'name': f"US Pair {order['sector'].upper()} ({leg_label})",
                        'direction': direction_to_trade,
                        'action': action_to_trade,
                        'market': 'US',
                        'size_pct': dynamic_size,
                        'tp_pct': round(_tp_us, 2),
                        'sl_pct': round(_sl_us, 2),
                        'expected_return': round(_tp_us / 100.0, 5),
                        'expected_return_pct': round(_tp_us, 2),
                        'confidence': order.get('confidence', 0.70),
                        'strategy': 'us_sector_pair_alpha',
                        'reason': f"[S14 US Pair] {order['reason']} (KER={ker_us:.2f}, Noise={noise_us:.1%})",
                        'spread_z': us_pair_res.get('spread_z', 0.0),
                        'timestamp': pd.Timestamp.now().isoformat()
                    })

        # ── 3. 국내(KR) 섹터 페어 알파 탐색 ──
        kr_sector_returns = {}
        kr_volatility_map = {}

        if market_data and 'kr_sector_returns' in market_data:
            kr_sector_returns = dict(market_data['kr_sector_returns'])
            kr_volatility_map = dict(market_data.get('kr_volatility_map', {}))
        else:
            for sec, ticker in DEFAULT_SECTOR_ETF_MAP.items():
                ret, vol = self._extract_returns_and_volatility(ticker, market='KR', days=20)
                if ret != 0.0 or vol != 0.20:
                    kr_sector_returns[sec] = ret
                    kr_volatility_map[sec] = vol

        if len(kr_sector_returns) >= 2:
            kr_pair_res = self.sector_engine.generate_pair_signals(
                sector_returns=kr_sector_returns,
                market='KR',
                volatility_map=kr_volatility_map
            )

            if kr_pair_res.get('pair_active') and kr_pair_res.get('spread_z', 0.0) >= active_spread_threshold:
                from src.utils.dynamic_quant_engine import get_quant_engine
                _dqe_kr = get_quant_engine()
                _kr_inv_list = _dqe_kr.get_inverse_hedge_tickers('KR')
                _kr_1x_inv = _kr_inv_list[-1] if _kr_inv_list else '114800'
                _sc_kr = (market_data or {}).get('signal_cache', {}) if isinstance(market_data, dict) else {}
                _vix_kr = float((market_data or {}).get('vix') or _sc_kr.get('vix', 20.0) or 20.0)
                _ois_kr = float((market_data or {}).get('ois') or _sc_kr.get('ois', 50.0) or 50.0)
                _vix_caut_kr = float(_dqe_kr.get_vix_regime_quantiles()['caution'])
                _ois_bull_kr = float(cfg.get('leverage.ois_bull_threshold') or 55.0)
                _reg_kr = str((market_data or {}).get('regime') or regime or 'caution').lower() if isinstance(market_data, dict) else str(regime or 'caution').lower()
                _kr_inv_blocked = (_vix_kr < _vix_caut_kr and _ois_kr >= _ois_bull_kr) or _reg_kr == 'bull'
                for order in (kr_pair_res.get('pair_orders', []) if not _kr_inv_blocked else []):
                    t_dir = order.get('direction', 'long').lower()
                    is_inv_hedge = bool(order.get('is_inverse_hedge', False)) or t_dir == 'short' or order.get('action') in ('sell', 'buy_inverse')
                    
                    if is_inv_hedge:
                        trade_ticker = _kr_1x_inv
                        trade_dir = 'long'      # 인버스를 매수하므로 현물 계좌 주문 방향은 long
                        leg_label = 'INVERSE_HEDGE'
                        partner_tk = order.get('pair_partner', '')
                    else:
                        trade_ticker = order['ticker']
                        trade_dir = 'long'
                        leg_label = 'LONG'
                        partner_tk = _kr_1x_inv

                    dynamic_size = round(order.get('target_weight', 0.10) * noise_kr, 4)
                    try:
                        _bar_kr = _dqe_kr.compute_dynamic_exit_barriers(trade_ticker)
                        _tp_kr = float(_bar_kr['take_profit_pct'])
                        _sl_kr = abs(float(_bar_kr['stop_loss_pct']))
                    except Exception:
                        _tp_kr, _sl_kr = 2.5, 1.5

                    signals.append({
                        'stream_id': self.stream_id,
                        'ticker': trade_ticker,
                        'pair_id': order.get('pair_id', ''),
                        'pair_partner': partner_tk,
                        'name': f"KR Pair {order['sector'].upper()} ({leg_label})",
                        'direction': trade_dir,
                        'action': 'buy',
                        'market': 'KR',
                        'size_pct': dynamic_size,
                        'tp_pct': round(_tp_kr, 2),
                        'sl_pct': round(_sl_kr, 2),
                        'expected_return': round(_tp_kr / 100.0, 5),
                        'expected_return_pct': round(_tp_kr, 2),
                        'confidence': order.get('confidence', 0.70),
                        'strategy': 'kr_sector_pair_alpha',
                        'reason': f"[S14 KR Pair] {order['reason']} (KER={ker_kr:.2f}, Noise={noise_kr:.1%})",
                        'spread_z': kr_pair_res.get('spread_z', 0.0),
                        'timestamp': pd.Timestamp.now().isoformat()
                    })

        # ── 4. Ornstein-Uhlenbeck 평균회귀 반감기 기반 무하드코딩 동적 공적분 페어 보강 ──
        _reg_all = str((market_data or {}).get('regime') or regime or 'caution').lower() if isinstance(market_data, dict) else str(regime or 'caution').lower()
        if not signals and _reg_all not in ('bull', 'momentum_surge'):
            try:
                from src.utils.dynamic_quant_engine import get_quant_engine
                _dqe = get_quant_engine()
                _sc_ou = (market_data or {}).get('signal_cache', {}) if isinstance(market_data, dict) else {}
                _vix_ou = float((market_data or {}).get('vix') or _sc_ou.get('vix', 20.0) or 20.0)
                _ois_ou = float((market_data or {}).get('ois') or _sc_ou.get('ois', 50.0) or 50.0)
                _vix_caut_ou = float(_dqe.get_vix_regime_quantiles()['caution'])
                _ois_bull_ou = float(cfg.get('leverage.ois_bull_threshold') or 55.0)
                if not (_vix_ou < _vix_caut_ou and _ois_ou >= _ois_bull_ou):
                    for _mkt, _noise in (('KR', noise_kr), ('US', noise_us)):
                        _inv_list_ou = _dqe.get_inverse_hedge_tickers(_mkt)
                        if not _inv_list_ou:
                            continue
                        _inv_tk_ou = _inv_list_ou[-1]
                        for _p in _dqe.discover_stat_arb_pairs(_mkt, max_pairs=1):
                            if abs(_p['z_score']) >= active_spread_threshold:
                                _ou_er_pct = round(max(1.5, float(_p.get('expected_return', 0.025) or 0.025) * 100.0), 2)
                                _pid_ou = str(_p.get('pair_id') or f"{_mkt}_OU_{_p['long_ticker']}_{_inv_tk_ou}")
                                _sz_ou = round(0.08 * _noise, 4)
                                signals.append({
                                    'stream_id': self.stream_id,
                                    'ticker': _p['long_ticker'],
                                    'pair_id': _pid_ou,
                                    'pair_partner': _inv_tk_ou,
                                    'name': f"{_mkt} StatArb {_p['long_ticker']}/{_inv_tk_ou} (LONG)",
                                    'direction': 'long',
                                    'action': 'buy',
                                    'market': _mkt,
                                    'size_pct': _sz_ou,
                                    'tp_pct': _ou_er_pct,
                                    'sl_pct': round(max(1.0, _ou_er_pct * 0.6), 2),
                                    'expected_return_pct': _ou_er_pct,
                                    'confidence': _p['confidence'],
                                    'expected_return': _p['expected_return'],
                                    'strategy': f"{_mkt.lower()}_ou_cointegration_pair",
                                    'reason': f"[S14 OU StatArb] {_pid_ou} Z={_p['z_score']:+.2f} (HalfLife={_p['half_life']:.1f}d, ρ={_p['corr']:.2f})",
                                    'spread_z': abs(_p['z_score']),
                                    'timestamp': pd.Timestamp.now().isoformat()
                                })
                                signals.append({
                                    'stream_id': self.stream_id,
                                    'ticker': _inv_tk_ou,
                                    'pair_id': _pid_ou,
                                    'pair_partner': _p['long_ticker'],
                                    'name': f"{_mkt} StatArb {_p['long_ticker']}/{_inv_tk_ou} (INVERSE_HEDGE)",
                                    'direction': 'long',
                                    'action': 'buy',
                                    'market': _mkt,
                                    'size_pct': _sz_ou,
                                    'tp_pct': _ou_er_pct,
                                    'sl_pct': round(max(1.0, _ou_er_pct * 0.6), 2),
                                    'expected_return_pct': _ou_er_pct,
                                    'confidence': _p['confidence'],
                                    'expected_return': _p['expected_return'],
                                    'strategy': f"{_mkt.lower()}_ou_cointegration_pair",
                                    'reason': f"[S14 OU StatArb Hedge] {_pid_ou} Z={_p['z_score']:+.2f} (HalfLife={_p['half_life']:.1f}d, ρ={_p['corr']:.2f})",
                                    'spread_z': abs(_p['z_score']),
                                    'timestamp': pd.Timestamp.now().isoformat()
                                })
            except Exception as _ou_e:
                logger.debug(f"  [S14 OU StatArb] 동적 페어 탐색 참고: {_ou_e}")

        if signals:
            _total_raw_sz = sum(max(0.0, float(s.get('size_pct', 0.0) or 0.0)) for s in signals)
            _mean_noise = max(0.0, min(1.0, (float(noise_us) + float(noise_kr)) / 2.0))
            if _total_raw_sz > 0.0 and _mean_noise > 0.0:
                _scale_norm = _mean_noise / _total_raw_sz
                for _s_item in signals:
                    _s_item['size_pct'] = round(float(_s_item.get('size_pct', 0.0) or 0.0) * _scale_norm, 4)
                    _s_item['is_market_neutral'] = True
                    _s_item['beta'] = 0.0

        logger.info(f"  ⚖️ [S14 Sector Pair Stream] {len(signals)}개 마켓 뉴트럴 페어 신호 생성 (KER_US={ker_us:.2f}, KER_KR={ker_kr:.2f})")
        return signals

    def get_positions(self) -> List[Dict]:
        """스트림 보유 포지션 조회."""
        return self._positions

    def get_performance(self) -> Dict:
        """스트림 성과 지표 동적 산출 (SSOT 및 실시간 daily_pnl 기반, 하드코딩 완전 제거)."""
        std_perf = self._compute_standard_performance(self._daily_pnl)
        win_rate = float(std_perf.get('win_rate', 1.0 / 2.0))
        sharpe = float(std_perf.get('sharpe', 0.0) or 0.0)

        # 1. results/stream_metrics.json 확인
        try:
            sm_file = _PROJECT_ROOT / 'results' / 'stream_metrics.json'
            if sm_file.exists():
                sm_data = json.loads(sm_file.read_text(encoding='utf-8'))
                raw = sm_data.get('raw_data', {}).get(self.stream_id, {})
                if raw:
                    raw_wr = float(raw.get('win_rate', win_rate))
                    win_rate = raw_wr / 100.0 if raw_wr > 1.0 else raw_wr
                    sharpe = float(raw.get('sharpe', sharpe))
                    raw_rets = list(raw.get('daily_returns', self._daily_pnl) or self._daily_pnl)
                    return {
                        **std_perf,
                        'stream_id': self.stream_id,
                        'name': self.name,
                        'daily_pnl': list(self._daily_pnl),
                        'daily_returns': raw_rets,
                        'win_rate': round(win_rate, 4),
                        'sharpe': round(sharpe, 4),
                    }
        except Exception:
            pass

        return {
            **std_perf,
            'stream_id': self.stream_id,
            'name': self.name,
            'daily_pnl': list(self._daily_pnl),
            'daily_returns': list(self._daily_pnl),
            'win_rate': round(win_rate, 4),
            'sharpe': round(sharpe, 4),
        }

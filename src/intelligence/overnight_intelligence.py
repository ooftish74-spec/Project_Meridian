"""
Overnight Intelligence Score (OIS)
===================================
야간 정보 흐름을 단일 점수로 통합 (기관급 harmonization).

시간순 정보 체인:
  [15:40-20:00] NXT 애프터마켓    → 15%
  [21:30-06:00] 미국 시장 S&P/NQ → 20%
  [야간]        SGX KOSPI200 선물 → 25%
  [야간]        VIX / Fear&Greed  → 10%
  [08:00-09:00] NXT 프리마켓      → 30%

출력:
  OIS 0~100 (50=중립, >70=강세, <30=약세)
  threshold_adj: buy_threshold 단일 보정값

기관 참조:
  - Bridgewater: 매크로 70% + 가격 30%
  - Two Sigma: Time-decay 가중
  - Citadel: 가격 60% + 포지셔닝 30% + 뉴스 10%
  → Project-A: 가격 70%(NXT+SGX) + 매크로 30%(VIX+US)

Author: Project-A
Date: 2026-03-11
"""
import pandas as pd
import json
import logging
import math
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    from src.utils.data_health_monitor import dhm
except ImportError as e:
    dhm = None
import sys
sys.path.insert(0, str(_PROJECT_ROOT))
from config.dynamic_config import DynamicConfig
_cfg = DynamicConfig()
_COL_SP500 = _cfg.get('ois.csv.col_sp500', 1)
_COL_NASDAQ = _cfg.get('ois.csv.col_nasdaq', 2)
_COL_VIX = _cfg.get('ois.csv.col_vix', 8)
_COL_FG = _cfg.get('ois.csv.col_fear_greed', 9)

class OvernightIntelligenceScore:
    """야간 정보를 단일 OIS(0~100)로 통합."""
    _DEFAULT_WEIGHTS = {'nxt_premarket': 0.25, 'kospi_intraday': 0.15, 'sgx_futures': 0.2, 'us_market': 0.2, 'nxt_aftermarket': 0.1, 'vix_fear': 0.1}

    @property
    def WEIGHTS(self):
        """동적 가중치: cfg에서 오버라이드 가능."""
        return {k: _cfg.get(f'ois.weight.{k}', v) for k, v in self._DEFAULT_WEIGHTS.items()}

    def __init__(self):
        self.data_dir = _PROJECT_ROOT / 'data' / 'raw'
        self.nxt_dir = self.data_dir / 'nxt_sentiment'
        self.sent_dir = self.data_dir / 'sentiment'
        self.results_dir = _PROJECT_ROOT / 'results'

    def calculate(self, include_premarket: bool=True) -> Dict:
        """
        OIS 통합 점수 계산.
        
        Args:
            include_premarket: True면 NXT 프리마켓 포함 (09:15 매매 전),
                              False면 프리마켓 제외 (15:35 결산 시)
        Returns:
            {
                'ois': float (0-100),
                'sentiment': str,
                'threshold_adj': int (-5 ~ +7),
                'components': {name: {score, weight, raw_data}},
                'timestamp': str,
            }
        """
        components = {}
        if include_premarket:
            components['nxt_premarket'] = self._score_nxt_premarket()
        components['kospi_intraday'] = self._score_kospi_intraday()
        components['sgx_futures'] = self._score_sgx_futures()
        components['us_market'] = self._score_us_market()
        components['nxt_aftermarket'] = self._score_nxt_aftermarket()
        components['vix_fear'] = self._score_vix_fear()
        weights = dict(self.WEIGHTS)
        if not include_premarket:
            removed = weights.pop('nxt_premarket', 0.25)
            remaining_total = sum(weights.values())
            if remaining_total > 0:
                for k in list(weights.keys()):
                    weights[k] += removed * (weights[k] / remaining_total)
        no_data_weight = 0.0
        has_data_keys = []
        for name, comp in components.items():
            if comp.get('detail') == 'no data':
                no_data_weight += weights.get(name, 0)
            else:
                has_data_keys.append(name)
        if no_data_weight > 0 and has_data_keys:
            data_total = sum((weights.get(k, 0) for k in has_data_keys))
            if data_total > 0:
                for k in has_data_keys:
                    weights[k] += no_data_weight * (weights[k] / data_total)
                for name, comp in components.items():
                    if comp.get('detail') == 'no data':
                        weights[name] = 0.0
        total_weight = 0
        weighted_sum = 0
        for name, comp in components.items():
            w = weights.get(name, 0)
            _score = comp['score']
            if not isinstance(_score, (int, float)) or math.isnan(_score):
                _score = 50.0
                comp['score'] = 50.0
                logger.warning(f"  ⚠️ OIS component '{name}' score=NaN → 50.0 대체")
            weighted_sum += _score * w
            total_weight += w
        ois = weighted_sum / total_weight if total_weight > 0 else 50.0
        if math.isnan(ois):
            logger.warning('  ⚠️ OIS 최종값 NaN → 50.0 fallback')
            ois = 50.0
        ois = float(np.clip(ois, 0, 100))
        if ois >= 70:
            sentiment = 'strong_bullish'
            threshold_adj = -5
        elif ois >= 60:
            sentiment = 'bullish'
            threshold_adj = -3
        elif ois >= 55:
            sentiment = 'slightly_bullish'
            threshold_adj = -1
        elif ois >= 45:
            sentiment = 'neutral'
            threshold_adj = 0
        elif ois >= 40:
            sentiment = 'slightly_bearish'
            threshold_adj = 2
        elif ois >= 30:
            sentiment = 'bearish'
            threshold_adj = 5
        else:
            sentiment = 'strong_bearish'
            threshold_adj = 7
        PRICE_COMPONENTS = {'nxt_premarket', 'sgx_futures', 'us_market', 'nxt_aftermarket', 'kospi_intraday'}
        SENT_COMPONENTS = {'vix_fear'}
        _price_w_sum = _price_w_total = 0.0
        _sent_w_sum = _sent_w_total = 0.0
        for _name, _comp in components.items():
            _w = weights.get(_name, 0)
            if _name in PRICE_COMPONENTS:
                _price_w_sum += _comp['score'] * _w
                _price_w_total += _w
            elif _name in SENT_COMPONENTS:
                _sent_w_sum += _comp['score'] * _w
                _sent_w_total += _w
        ois_price = float(np.clip(_price_w_sum / _price_w_total, 0, 100)) if _price_w_total > 0 else ois
        ois_sentiment = float(np.clip(_sent_w_sum / _sent_w_total, 0, 100)) if _sent_w_total > 0 else ois
        if math.isnan(ois_price):
            ois_price = ois
        if math.isnan(ois_sentiment):
            ois_sentiment = ois
        result = {'ois': round(ois, 1), 'ois_price': round(ois_price, 1), 'ois_sentiment': round(ois_sentiment, 1), 'sentiment': sentiment, 'threshold_adj': threshold_adj, 'components': components, 'timestamp': datetime.now().isoformat(), 'include_premarket': include_premarket}
        ois_dir = self.data_dir / 'overnight_intelligence'
        ois_dir.mkdir(parents=True, exist_ok=True)
        suffix = 'full' if include_premarket else 'closing'
        ois_file = ois_dir / f'{datetime.now().strftime('%Y-%m-%d_%H%M')}_{suffix}.json'
        try:
            from src.utils.file_ops import atomic_write_json

            atomic_write_json(ois_file, result, indent=2, default=str)
        except Exception as _e:
            logger.warning('skip: %s', _e, exc_info=True)
        logger.info(f'  🌙 OIS: {ois:.1f}/100 → {sentiment} (threshold {threshold_adj:+d})')
        for name, comp in components.items():
            w = weights.get(name, 0)
            logger.info(f'    {name:18s}: {comp['score']:5.1f} × {w:.0%} = {comp['score'] * w:5.1f}  {comp.get('detail', '')}')
        return result

    def _score_kospi_intraday(self) -> Dict:
        """★ KOSPI/KOSDAQ 장중 모멘텀 → 점수.

        signal_cache의 한국 시장 데이터를 기반으로:
        1. KOSPI 변동률 (전일 대비)
        2. KOSPI vs MA20 위치
        3. VKOSPI 레벨
        """
        try:
            sc_path = self.results_dir / 'signal_cache.json'
            if not sc_path.exists():
                return {'score': 50.0, 'detail': 'no data'}
            sc = json.loads(sc_path.read_text())
            kospi = sc.get('kospi_close', sc.get('kospi', 0))
            kospi_prev = sc.get('kospi_prev_close', 0)
            kospi_ma20 = sc.get('kospi_ma20', 0)
            vkospi = sc.get('vkospi', 0)
            _min_kospi = _cfg.get('ois.kospi.min_valid', 1000)
            if not kospi or kospi < _min_kospi:
                return {'score': 50.0, 'detail': 'no data'}
            scores = []
            details = []
            _chg_scale = _cfg.get('ois.kospi.change_pct_scale', 1.0)
            _chg_floor = _cfg.get('ois.kospi.change_score_floor', 5)
            _chg_cap = _cfg.get('ois.kospi.change_score_cap', 95)
            if kospi_prev and kospi_prev > _min_kospi:
                chg_pct = (kospi / kospi_prev - 1) * 100
                chg_score = 50 + chg_pct * (50 / _chg_scale)
                chg_score = float(np.clip(chg_score, _chg_floor, _chg_cap))
                scores.append(chg_score)
                details.append(f'KOSPI {chg_pct:+.2f}%')
            elif kospi_ma20 and kospi_ma20 > _min_kospi:
                _ma_scale = _cfg.get('ois.kospi.ma_ratio_scale', 2.0)
                ma_ratio = (kospi / kospi_ma20 - 1) * 100
                ma_score = 50 + ma_ratio * (50 / _ma_scale)
                ma_score = float(np.clip(ma_score, 10, 90))
                scores.append(ma_score)
                details.append(f'KOSPI/MA20 {ma_ratio:+.1f}%')
            _ma_above_score = _cfg.get('ois.kospi.ma_above_score', 55)
            _ma_below_score = _cfg.get('ois.kospi.ma_below_score', 45)
            if kospi_ma20 and kospi_ma20 > _min_kospi:
                above_ma = kospi > kospi_ma20
                ma_bonus = _ma_above_score if above_ma else _ma_below_score
                scores.append(ma_bonus)
            if vkospi and vkospi > 0:
                _vk_thresholds = [(_cfg.get('ois.vkospi.calm_max', 15), _cfg.get('ois.vkospi.calm_score', 72)), (_cfg.get('ois.vkospi.low_max', 18), _cfg.get('ois.vkospi.low_score', 62)), (_cfg.get('ois.vkospi.mid_max', 22), _cfg.get('ois.vkospi.mid_score', 50)), (_cfg.get('ois.vkospi.high_max', 28), _cfg.get('ois.vkospi.high_score', 38))]
                _vk_extreme_score = _cfg.get('ois.vkospi.extreme_score', 25)
                vk_score = _vk_extreme_score
                for thresh, score_val in _vk_thresholds:
                    if vkospi < thresh:
                        vk_score = score_val
                        break
                scores.append(vk_score)
                details.append(f'VK={vkospi:.0f}')
            if scores:
                avg = sum(scores) / len(scores)
                return {'score': round(avg, 1), 'detail': ', '.join(details) if details else f'KOSPI {kospi:,.0f}', 'raw': {'kospi': kospi, 'kospi_prev': kospi_prev, 'kospi_ma20': kospi_ma20, 'vkospi': vkospi}}
        except Exception as _e:
            if dhm:
                dhm.record('ois_kospi_intraday', _e, 'warning', context={'component': 'kospi_intraday'})
            else:
                logger.warning('kospi_intraday skip: %s', _e)
        return {'score': 50.0, 'detail': 'no data'}

    def _score_nxt_premarket(self) -> Dict:
        """NXT 프리마켓 (08:00~09:00) 가격 동향 → 점수."""
        return self._score_nxt_data(session='premarket', max_age_hours=3)

    def _score_nxt_aftermarket(self) -> Dict:
        """NXT 애프터마켓 (전일) 가격 동향 → 점수."""
        return self._score_nxt_data(session='aftermarket', max_age_hours=24)

    def _score_nxt_data(self, session: str, max_age_hours: int) -> Dict:
        """NXT 센서 데이터 → 0~100 점수."""
        try:
            if not self.nxt_dir.exists():
                return {'score': 50.0, 'detail': 'no data'}
            nxt_files = sorted(self.nxt_dir.glob('*.json'), reverse=True)
            cutoff = datetime.now() - timedelta(hours=max_age_hours)
            for nf in nxt_files[:10]:
                try:
                    from src.utils.file_ops import atomic_write_json

                    with open(nf, 'r', encoding='utf-8') as _f:
                        data = json.load(_f)
                    ts = datetime.fromisoformat(data.get('timestamp', '2000-01-01'))
                    if ts < cutoff:
                        continue
                    avg_chg = data.get('avg_change_pct', 0)
                    n = data.get('n_stocks', 0)
                    if n == 0:
                        continue
                    score = 50 + avg_chg * (50 / 3)
                    score = float(np.clip(score, 0, 100))
                    return {'score': round(score, 1), 'detail': f'{avg_chg:+.2f}% ({n}종목)', 'raw': {'avg_change': avg_chg, 'n_stocks': n}}
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                    continue
        except Exception as _e:
            logger.warning('skip: %s', _e, exc_info=True)
        return {'score': 50.0, 'detail': 'no data'}

    def _score_sgx_futures(self) -> Dict:
        """SGX KOSPI200 선물 프록시 → 점수.
        
        우선순위:
          0. ★ overnight_macro_collector 데이터 (가장 신선)
          1. 야간선물 직접 데이터 (krx_futures_overnight.json)
          2. KIS API: EWY 실시간 시세 (실전 키, 시세 전용)
          3. EWY parquet 파일 (yfinance)
          4. realtime_sentiment.csv 폴백
        """
        try:
            today_str = datetime.now().strftime('%Y-%m-%d')
            macro_file = self.data_dir / 'overnight_macro' / f'{today_str}.json'
            if macro_file.exists():
                with open(macro_file) as _f:
                    macro_data = json.load(_f)
                sgx = macro_data.get('sgx_proxy', {})
                if 'sgx_proxy_score' in sgx:
                    ewy_chg = sgx.get('ewy', {}).get('change_pct', 0)
                    return {'score': float(np.clip(sgx['sgx_proxy_score'], 0, 100)), 'detail': f'EWY(Macro) {ewy_chg:+.2f}%', 'raw': sgx, 'source': 'overnight_macro'}
            kf_file = self.sent_dir / 'krx_futures_overnight.json'
            if kf_file.exists():
                try:
                    with open(kf_file) as _f:
                        data = json.load(_f)
                except (Exception,):
                    data = {}
                chg = data.get('change_pct', data.get('overnight_gap', 0))
                if isinstance(chg, (int, float)):
                    score = 50 + chg * (50 / 2)
                    return {'score': float(np.clip(score, 0, 100)), 'detail': f'SGX {chg:+.2f}%', 'raw': data}
            ewy_data = self._fetch_ewy_from_kis()
            if ewy_data and ewy_data.get('rate'):
                chg = float(ewy_data['rate'])
                score = 50 + chg * (50 / 3)
                return {'score': float(np.clip(score, 0, 100)), 'detail': f'EWY(KIS) {chg:+.2f}%', 'raw': ewy_data}
            import pandas as pd
            for ewy_file in [_PROJECT_ROOT / 'data' / 'signals' / 'signal_ewy.parquet', _PROJECT_ROOT / 'data' / 'historical_10y' / 'us_stocks' / 'EWY.parquet']:
                if ewy_file.exists():
                    df = pd.read_parquet(ewy_file)
                    df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
                    if hasattr(df.columns, 'levels'):
                        df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df.columns]
                    if 'close' in df.columns and len(df) >= 2:
                        last_close = float(df['close'].iloc[-1])
                        prev_close = float(df['close'].iloc[-2])
                        chg = (last_close / prev_close - 1) * 100
                        score = 50 + chg * (50 / 3)
                        return {'score': float(np.clip(score, 0, 100)), 'detail': f'EWY {chg:+.2f}%', 'raw': {'ewy_close': last_close, 'ewy_prev': prev_close}}
            sent_file = self.sent_dir / 'realtime_sentiment.csv'
            if sent_file.exists():
                import csv
                rows = list(csv.reader(open(sent_file)))
                if len(rows) >= 2:
                    last = rows[-1]
                    try:
                        sp500 = float(last[1]) if len(last) > 1 else 0
                        nasdaq = float(last[2]) if len(last) > 2 else 0
                        avg = (sp500 + nasdaq) / 2 if sp500 and nasdaq else 0
                        score = 50 + avg * (50 / 2)
                        return {'score': float(np.clip(score, 0, 100)), 'detail': f'US proxy {avg:+.2f}%'}
                    except (ValueError, IndexError):
                        logger.warning('[SILENT_BYPASS] Suppressed exception at overnight_intelligence.py:481', exc_info=True)
        except Exception as _e:
            if dhm:
                dhm.record('ois_sgx_futures', _e, 'warning', context={'component': 'sgx_futures'})
            else:
                logger.warning('skip: %s', _e)
        return {'score': 50.0, 'detail': 'no data'}

    def _fetch_ewy_from_kis(self) -> Optional[Dict]:
        """KIS 실전 API로 EWY 현재가 조회 (시세 전용, 주문 아님).
        
        SGX KOSPI200 선물 직접 조회가 불가하므로,
        EWY (iShares MSCI South Korea ETF)를 프록시로 사용.
        EWY vs SGX KOSPI200 상관계수 ≈ 0.95+
        """
        try:
            import requests
            import os
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
            app_key = cm.read_from_env('KIS_APP_KEY')
            app_secret = cm.read_from_env('KIS_APP_SECRET')
            if not app_key or not app_secret:
                return None
            base_url = 'https://openapi.koreainvestment.com:9443'
            token_file = _PROJECT_ROOT / 'results' / '.kis_live_token.json'
            token = None
            if token_file.exists():
                try:
                    with open(token_file) as _f:
                        tc = json.load(_f)
                    exp = datetime.fromisoformat(tc.get('expires', '2000-01-01'))
                    if datetime.now() < exp:
                        token = tc['token']
                except Exception as _e:
                    logger.warning('skip: %s', _e, exc_info=True)
            if not token:
                resp = requests.post(f'{base_url}/oauth2/tokenP', json={'grant_type': 'client_credentials', 'appkey': app_key, 'appsecret': app_secret}, timeout=10)
                data = resp.json()
                token = data.get('access_token', '')
                if token:
                    from datetime import timedelta as td
                    token_file.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(token_file, {'token': token, 'expires': (datetime.now() + td(hours=23)).isoformat()})
            if not token:
                return None
            headers = {'Content-Type': 'application/json; charset=utf-8', 'authorization': f'Bearer {token}', 'appkey': app_key, 'appsecret': app_secret, 'tr_id': 'HHDFS00000300'}
            resp = requests.get(f'{base_url}/uapi/overseas-price/v1/quotations/price', headers=headers, params={'AUTH': '', 'EXCD': 'AMS', 'SYMB': 'EWY'}, timeout=5)
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output', {})
                last_price = output.get('last', '')
                rate = output.get('rate', '')
                diff = output.get('diff', '')
                if last_price and rate:
                    cache = {'timestamp': datetime.now().isoformat(), 'ewy_price': last_price, 'rate': rate, 'diff': diff, 'source': 'KIS_API_live'}
                    cache_file = self.sent_dir / 'ewy_overnight.json'
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    from src.utils.file_ops import atomic_write_json

                    atomic_write_json(cache_file, cache, indent=2)
                    return cache
        except Exception as e:
            logger.error(f'EWY KIS 조회 실패: {e}', exc_info=True)
        cache_file = self.sent_dir / 'ewy_overnight.json'
        if cache_file.exists():
            try:
                with open(cache_file) as _f:
                    cached = json.load(_f)
                ts = datetime.fromisoformat(cached.get('timestamp', '2000-01-01'))
                if (datetime.now() - ts).total_seconds() < 86400:
                    return cached
            except Exception as _e:
                logger.warning('skip: %s', _e, exc_info=True)
        return None

    def _score_us_market(self) -> Dict:
        """미국 시장 (S&P500, NASDAQ) → 점수."""
        try:
            sent_file = self.sent_dir / 'realtime_sentiment.csv'
            if sent_file.exists():
                import csv
                rows = list(csv.reader(open(sent_file)))
                if len(rows) >= 2:
                    last = rows[-1]
                    prev = rows[-2] if len(rows) >= 3 else last
                    changes = []
                    for col in [1, 2]:
                        try:
                            now_val = float(last[col])
                            prev_val = float(prev[col])
                            if prev_val > 0:
                                chg = (now_val / prev_val - 1) * 100
                                changes.append(chg)
                        except (ValueError, IndexError):
                            logger.warning('[SILENT_BYPASS] Suppressed exception at overnight_intelligence.py:617', exc_info=True)
                    if changes:
                        avg = sum(changes) / len(changes)
                        score = 50 + avg * (50 / 3)
                        return {'score': float(np.clip(score, 0, 100)), 'detail': f'S&P/NQ avg {avg:+.2f}%', 'raw': {'changes': changes}}
            import pandas as pd
            _SIG_MAP = {'SPY': 'signal_sp500.parquet', 'QQQ': 'signal_nasdaq.parquet'}
            for sym in ['SPY', 'QQQ']:
                for pq in [_PROJECT_ROOT / 'data' / 'signals' / _SIG_MAP.get(sym, ''), _PROJECT_ROOT / 'data' / 'historical_10y' / 'us_stocks' / f'{sym}.parquet']:
                    if pq.exists():
                        df = pd.read_parquet(pq)
                        df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
                        if hasattr(df.columns, 'levels'):
                            df.columns = ['_'.join(c).strip('_') if isinstance(c, tuple) else c for c in df.columns]
                        if 'close' in df.columns and len(df) >= 2:
                            chg = (float(df['close'].iloc[-1]) / float(df['close'].iloc[-2]) - 1) * 100
                            rets = df['close'].pct_change().dropna() * 100
                            std = float(rets.std()) if len(rets) > 5 else 1.5
                            if pd.isna(std) or std == 0:
                                std = 1.5
                            z = chg / std
                            score = 100.0 * (0.5 * (1 + math.erf(z / math.sqrt(2))))
                            return {'score': float(np.clip(score, 0, 100)), 'detail': f'{sym} {chg:+.2f}%'}
        except Exception as _e:
            if dhm:
                dhm.record('ois_us_market', _e, 'warning', context={'component': 'us_market'})
            else:
                logger.warning('skip: %s', _e)
        return {'score': 50.0, 'detail': 'no data'}

    def _score_vix_fear(self) -> Dict:
        """VIX + Fear & Greed → 점수 (역방향: VIX↑ = 약세)."""
        try:
            sent_file = self.sent_dir / 'realtime_sentiment.csv'
            if sent_file.exists():
                import csv
                rows = list(csv.reader(open(sent_file)))
                if len(rows) >= 2:
                    last = rows[-1]
                    prev = rows[-2] if len(rows) >= 3 else last
                    vix = None
                    fear_greed = None
                    try:
                        vix_now = float(last[8]) if len(last) > 8 else None
                        vix_prev = float(prev[8]) if len(prev) > 8 else None
                        if vix_now and vix_prev and (vix_prev > 0):
                            vix_chg = (vix_now / vix_prev - 1) * 100
                            vix_score = 100.0 / (1.0 + math.exp(0.15 * (vix_now - 22.5)))
                            vix_score = max(20.0, min(80.0, vix_score))
                            vix_score -= min(15, max(-10, vix_chg * 2))
                            vix = float(np.clip(vix_score, 0, 100))
                    except (ValueError, IndexError):
                        logger.warning('[SILENT_BYPASS] Suppressed exception at overnight_intelligence.py:687', exc_info=True)
                    try:
                        fg = float(last[9]) if len(last) > 9 else None
                        if fg is not None:
                            fear_greed = fg
                    except (ValueError, IndexError):
                        logger.warning('[SILENT_BYPASS] Suppressed exception at overnight_intelligence.py:695', exc_info=True)
                    scores = [s for s in [vix, fear_greed] if s is not None]
                    if scores:
                        avg = sum(scores) / len(scores)
                        detail_parts = []
                        if vix is not None:
                            detail_parts.append(f'VIX={vix:.0f}')
                        if fear_greed is not None:
                            detail_parts.append(f'F&G={fear_greed:.0f}')
                        return {'score': round(avg, 1), 'detail': ', '.join(detail_parts)}
        except Exception as _e:
            if dhm:
                dhm.record('ois_vix_fear', _e, 'warning', context={'component': 'vix_fear'})
            else:
                logger.warning('skip: %s', _e)
        return {'score': 50.0, 'detail': 'no data'}

    def get_latest_ois(self) -> Optional[Dict]:
        """가장 최근 저장된 OIS 로드."""
        ois_dir = self.data_dir / 'overnight_intelligence'
        if not ois_dir.exists():
            return None
        files = sorted(ois_dir.glob('*.json'), reverse=True)
        if files:
            with open(files[0]) as _f:
                return json.load(_f)
        return None
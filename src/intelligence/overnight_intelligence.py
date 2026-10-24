"""
Project Meridian — Overnight Intelligence Score (OIS) Engine
==============================================================
야간 정보 (US 증시, EWY, 야간선물, VIX, 센티멘트)를 0~100 OIS 점수로 실시간 산출.
"""

import json
import logging
import math
import sys
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
from src.utils.file_ops import atomic_write_json

logger = logging.getLogger(__name__)
_cfg = DynamicConfig()

class OvernightIntelligenceScore:
    """야간 정보를 단일 OIS(0~100)로 통합."""
    _DEFAULT_WEIGHTS = {
        'nxt_premarket': 0.15,
        'kospi_intraday': 0.15,
        'krx_night_futures': 0.35,
        'us_market': 0.25,
        'vix_fear': 0.10
    }

    @property
    def WEIGHTS(self):
        w = {}
        for k, v in self._DEFAULT_WEIGHTS.items():
            cfg_v = _cfg.get(f"ois.weight.{k}")
            if cfg_v is None and k == 'krx_night_futures':
                cfg_v = _cfg.get("ois.weight.sgx_futures")
            w[k] = cfg_v if cfg_v is not None else v
        return w

    def __init__(self):
        self.data_dir = _PROJECT_ROOT / 'data' / 'raw'
        self.nxt_dir = self.data_dir / 'nxt_sentiment'
        self.sent_dir = self.data_dir / 'sentiment'
        self.results_dir = _PROJECT_ROOT / 'results'

    def calculate(self, include_premarket: bool = True) -> Dict:
        """OIS 통합 점수 계산."""
        components = {}
        if include_premarket:
            components['nxt_premarket'] = self._score_nxt_premarket()
        components['kospi_intraday'] = self._score_kospi_intraday()
        components['krx_night_futures'] = self._score_krx_night_futures()
        components['us_market'] = self._score_us_market()
        components['vix_fear'] = self._score_vix_fear()

        weights = dict(self.WEIGHTS)
        if not include_premarket:
            removed = weights.pop('nxt_premarket', 0.15)
            remaining_total = sum(weights.values())
            if remaining_total > 0:
                for k in list(weights.keys()):
                    weights[k] += removed * (weights[k] / remaining_total)

        # Distribute weights for components with valid data
        no_data_weight = 0.0
        has_data_keys = []
        for name, comp in components.items():
            if comp.get('detail') == 'no data' or comp.get('score') is None:
                no_data_weight += weights.get(name, 0)
            else:
                has_data_keys.append(name)

        if no_data_weight > 0 and has_data_keys:
            data_total = sum((weights.get(k, 0) for k in has_data_keys))
            if data_total > 0:
                for k in has_data_keys:
                    weights[k] += no_data_weight * (weights[k] / data_total)
                for name, comp in components.items():
                    if comp.get('detail') == 'no data' or comp.get('score') is None:
                        weights[name] = 0.0

        total_weight = 0.0
        weighted_sum = 0.0
        for name, comp in components.items():
            w = weights.get(name, 0)
            _score = comp.get('score')
            if isinstance(_score, (int, float)) and not math.isnan(_score):
                weighted_sum += _score * w
                total_weight += w

        ois = weighted_sum / total_weight if total_weight > 0 else 50.0
        ois = float(np.clip(ois, 0.0, 100.0))

        # Sentiment Threshold
        sentiment = 'strong_bullish' if ois >= 70 else 'bullish' if ois >= 55 else 'neutral' if ois >= 45 else 'bearish' if ois >= 30 else 'strong_bearish'
        threshold_adj = -5 if ois >= 70 else -3 if ois >= 60 else 0 if ois >= 45 else +3 if ois >= 35 else +5

        # Backward-compatibility alias
        components['sgx_futures'] = components.get('krx_night_futures', {})

        result = {
            'ois': round(ois, 1),
            'ois_price': round(ois, 1),
            'ois_sentiment': round(ois, 1),
            'sentiment': sentiment,
            'threshold_adj': threshold_adj,
            'components': components,
            'timestamp': datetime.now().isoformat(),
            'include_premarket': include_premarket
        }
        return result

    def _score_kospi_intraday(self) -> Dict:
        """KOSPI 장중 모멘텀 점수 (09:00 KST 정규장 개장 전에는 전일 종가 잔상 배제)."""
        try:
            from datetime import time as _dt_time
            now_time = datetime.now().time()
            if now_time < _dt_time(9, 0):
                return {'score': None, 'detail': 'before KRX regular open (09:00 KST)'}
            sc_path = self.results_dir / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text())
                kospi_chg = sc.get('kospi_change_1d', sc.get('kospi_change', 0.0))
                if isinstance(kospi_chg, (int, float)) and kospi_chg != 0.0:
                    score = float(np.clip(50.0 + kospi_chg * (50.0 / 2.0), 10.0, 90.0))
                    return {'score': round(score, 1), 'detail': f"KOSPI {kospi_chg:+.2f}%", 'status': 'OK'}
        except Exception as _e:
            logger.warning('kospi_intraday skip: %s', _e)
        return {'score': None, 'detail': 'no data'}

    def _score_nxt_premarket(self) -> Dict:
        """장전 동시호가/프리마켓(08:00~09:00 KST) 호가 잔량 및 프리마켓 체결가(삼성전자/SK하이닉스/KODEX200) 모멘텀 점수."""
        from datetime import time as _dt_time
        now_time = datetime.now().time()
        if now_time < _dt_time(8, 0):
            return {'score': None, 'detail': 'outside premarket window (08:00-09:00 KST)'}
        try:
            sc_path = self.results_dir / 'signal_cache.json'
            sc = json.loads(sc_path.read_text()) if sc_path.exists() else {}
            pa = sc.get('premarket_auction', {})
            pa_ts = pa.get('timestamp', pa.get('updated_at', ''))
            gap = pa.get('disparity_pct', sc.get('krx_premarket_gap_pct'))
            if gap is not None and isinstance(gap, (int, float)):
                if not pa_ts or str(pa_ts)[:10] == datetime.now().strftime('%Y-%m-%d'):
                    score = float(np.clip(50.0 + float(gap) * (50.0 / 2.0), 10.0, 90.0))
                    return {'score': round(score, 1), 'detail': f"Premarket Gap {gap:+.2f}%", 'status': 'OK'}

            if _dt_time(8, 0) <= now_time <= _dt_time(15, 30):
                from src.data_collection.kis_data_collector import KISDataCollector
                kis = KISDataCollector()
                techs = sc.get('stock_technicals', {})
                gaps = []
                for tk in ('005930', '000660', '069500'):
                    prev_c = float((techs.get(tk) or {}).get('close') or 0.0)
                    if prev_c > 0:
                        live_q = kis.get_current_price(tk)
                        live_p = float((live_q or {}).get('price') or (live_q or {}).get('antc_price') or 0.0) if isinstance(live_q, dict) else float(live_q or 0.0)
                        if live_p > 0 and abs(live_p - prev_c) / prev_c < 0.30:
                            gaps.append((live_p - prev_c) / prev_c * 100.0)
                if gaps:
                    avg_gap = float(sum(gaps) / len(gaps))
                    score = float(np.clip(50.0 + avg_gap * (50.0 / 2.0), 10.0, 90.0))
                    return {'score': round(score, 1), 'detail': f"NXT Premarket/Leader Avg {avg_gap:+.2f}%", 'gap_pct': round(avg_gap, 3), 'status': 'OK'}
            ois_ts = str((sc.get('ois_details') or {}).get('timestamp', ''))
            prev_nxt = (sc.get('ois_details') or {}).get('components', {}).get('nxt_premarket', {})
            if ois_ts[:10] == datetime.now().strftime('%Y-%m-%d') and isinstance(prev_nxt, dict) and isinstance(prev_nxt.get('score'), (int, float)):
                return prev_nxt
        except Exception as _e:
            logger.warning('nxt_premarket score skip: %s', _e)
            
        if _dt_time(8, 0) <= now_time <= _dt_time(9, 0):
            return {'score': None, 'detail': 'premarket auction pending (08:00-09:00 KST)'}
        return {'score': None, 'detail': 'outside premarket window (08:00-09:00 KST)'}

    def _score_krx_night_futures(self) -> Dict:
        """KRX 야간선물 (KOSPI 200 야간선물 F202612) → 점수."""
        try:
            # 1. krx_futures_overnight.json 또는 night_futures.json
            macro_dir = _PROJECT_ROOT / 'data' / 'macro'
            for kf_file in [macro_dir / 'night_futures.json', self.results_dir / 'krx_futures_overnight.json']:
                if kf_file.exists():
                    try:
                        with open(kf_file, 'r', encoding='utf-8') as _f:
                            data = json.load(_f)
                        
                        # [Freshness Shield] 18시간 이상 지난 과거 캐시 파일 방어 차단
                        ts_str = data.get('timestamp', '')
                        if ts_str:
                            try:
                                ts_dt = datetime.fromisoformat(ts_str)
                                if (datetime.now() - ts_dt).total_seconds() > 64800: # 18시간 경과
                                    logger.warning(f"  ⚠️ 야간선물 데이터 Stale 감지 ({kf_file.name}, {ts_str}) ➔ 과거 데이터 사용 거부")
                                    continue
                            except Exception as _ts_e:
                                logger.debug(f"Timestamp parse fallback ({kf_file.name}): {_ts_e}")

                        chg = data.get('change_pct', data.get('overnight_gap'))
                        # 세션 종가 마감 후 status가 CLOSED이더라도 valid numeric change_pct가 존재하면 최종 마감 등락률로 사용
                        if chg is not None and isinstance(chg, (int, float)) and not math.isnan(chg):
                            score = 50.0 + float(chg) * (50.0 / 2.0)
                            front_code = data.get('front_code', 'F202612')
                            return {'score': round(float(np.clip(score, 5.0, 95.0)), 1), 'detail': f"KRX NightFut({front_code}) {chg:+.2f}%", 'raw': data, 'status': 'OK'}
                        if data.get('status') == 'CLOSED':
                            return {'score': None, 'detail': 'Market Closed (Weekend/Holiday)'}
                    except Exception as _fe:
                        logger.warning(f"Failed parsing {kf_file.name}: {_fe}")

            # 2. signal_cache.json night_futures_change_1d
            sc_path = self.results_dir / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text())
                nf_chg = sc.get('night_futures_change_1d', sc.get('ewy_change_1d', 0.0))
                if isinstance(nf_chg, (int, float)) and nf_chg != 0.0:
                    score = 50.0 + nf_chg * (50.0 / 2.0)
                    return {'score': float(np.clip(score, 5.0, 95.0)), 'detail': f"KRX NightFut(Cache) {nf_chg:+.2f}%", 'status': 'OK'}

        except Exception as _e:
            logger.error(f"Error evaluating KRX night futures: {_e}")
        return {'score': None, 'detail': 'no data'}

    def _score_sgx_futures(self) -> Dict:
        """하위 호환성 별칭 메서드 (KRX 야간선물 위임)."""
        return self._score_krx_night_futures()

    def _score_us_market(self) -> Dict:
        """미국 시장 (S&P500, NASDAQ, SOX) → 점수 (signal_cache.json 1순위 최우선)."""
        try:
            # 1. 최우선: signal_cache.json 실시간 1일 등락률 (sp500_change_1d, nasdaq_change_1d, sox_change_1d)
            sc_path = self.results_dir / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text(encoding='utf-8'))
                # 대표 3대 지수 우선 추출 (중복 키 평탄화)
                target_keys = ['sox_change_1d', 'nasdaq_change_1d', 'sp500_change_1d']
                found_rets = []
                for k in target_keys:
                    v = sc.get(k)
                    if v is not None:
                        try:
                            fv = float(v)
                            if not math.isnan(fv):
                                found_rets.append(fv)
                        except (ValueError, TypeError) as _ve_ret:
                            logger.debug(f"Invalid US index return value ({k}={v}): {_ve_ret}")
                if found_rets:
                    avg_ret = sum(found_rets) / len(found_rets)
                    score = float(np.clip(50.0 + avg_ret * (50.0 / 2.5), 5.0, 95.0))
                    return {'score': round(score, 1), 'detail': f"US Indices avg {avg_ret:+.2f}%", 'status': 'OK'}

            # 2. 2순위: overnight_macro/*.json 24시간 이내 신규 파일만 인정
            macro_dir = _PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
            if macro_dir.exists():
                files = sorted(macro_dir.glob('*.json'))
                if files:
                    latest = files[-1]
                    mtime = latest.stat().st_mtime
                    if (time.time() - mtime) <= 86400: # 24시간 이내 신선한 파일만
                        try:
                            data = json.loads(latest.read_text(encoding='utf-8'))
                            us_fut = data.get('us_futures', {})
                            rets = []
                            for k, v in us_fut.items():
                                chg = v.get('change_pct')
                                if isinstance(chg, (int, float)) and not math.isnan(chg):
                                    rets.append(chg)
                            if rets:
                                avg_ret = sum(rets) / len(rets)
                                score = float(np.clip(50.0 + avg_ret * (50.0 / 2.5), 5.0, 95.0))
                                return {'score': round(score, 1), 'detail': f"US Futures avg {avg_ret:+.2f}%", 'status': 'OK'}
                        except Exception as _fe:
                            logger.warning(f"Failed parsing {latest.name}: {_fe}")
        except Exception as _e:
            logger.warning(f"us_market score skip: {_e}")
        return {'score': 50.0, 'detail': 'no data'}

    def _score_vix_fear(self) -> Dict:
        """VIX + Fear & Greed → 점수 (역방향: VIX 낮을수록 강세 점수 높음)."""
        try:
            sc_path = self.results_dir / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text())
                vix = sc.get('vix')
                if isinstance(vix, (int, float)) and vix > 0:
                    vix_score = 50.0 + (20.0 - vix) * 3.33
                    vix_score = float(np.clip(vix_score, 10.0, 90.0))
                    return {'score': round(vix_score, 1), 'detail': f"VIX={vix:.1f}", 'status': 'OK'}
        except Exception as _ve:
            logger.warning(f"vix_fear score skip: {_ve}")
        return {'score': 50.0, 'detail': 'no data'}

    def _load_active_portfolio(self) -> Dict[str, Any]:
        """라이브 모드 시 kis_portfolio.json을 최우선 조회하고 shadow_portfolio.json으로 폴백."""
        is_live = str(_cfg.get('execution.current_mode', 'live')).lower() == 'live'
        pf_files = (
            [self.results_dir / 'kis_portfolio.json', self.results_dir / 'shadow_portfolio.json']
            if is_live
            else [self.results_dir / 'shadow_portfolio.json', self.results_dir / 'kis_portfolio.json']
        )
        for pf_path in pf_files:
            if pf_path.exists():
                try:
                    return json.loads(pf_path.read_text(encoding='utf-8'))
                except Exception as _pe:
                    logger.debug(f"Portfolio load fallback ({pf_path.name}): {_pe}")
        return {}
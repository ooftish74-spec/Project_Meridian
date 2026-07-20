"""
Market Shock Detector — 시장 급변 실시간 감지 + 방향성 조정
=============================================================
문제: 2026-03-04 대폭 하락(-14%)에서 Direction Accuracy 0%
원인: HMM/GMM 레짐은 과거 시계열 기반 → 당일 급변에 반응 불가

해결: 예측 실행 직전에 미국 시장/선물/VIX 등 실시간 신호를 검사하여
      "충격 이벤트"를 감지하고, 예측 방향과 크기를 즉시 보정

통합:
  파이프라인 Phase 0.9.1 → Regime Detection 후 실행
  overnight_predictor → bias 보정에 shock 신호 반영
"""
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    import sys as _sys
    _sys.path.insert(0, str(PROJECT_ROOT))
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None
_cfg_get = (lambda k, d=None: _cfg.get(k, d)) if _cfg else lambda k, d=None: d

class MarketShockDetector:
    """
    실시간 시장 충격 감지기.

    3단계 충격 수준:
      - NONE: 정상 시장 (충격 없음)
      - MODERATE: 주의 (미국 -2%~ -4%, VIX 25~35)
      - SEVERE: 경보 (미국 -4%+, VIX 35+, 야간 선물 급락)
    """

    @property
    def THRESHOLDS(self) -> Dict:
        return {'us_sp500_moderate': _cfg_get('shock.threshold.us_sp500_moderate', -0.02), 'us_sp500_severe': _cfg_get('shock.threshold.us_sp500_severe', -0.04), 'us_nasdaq_moderate': _cfg_get('shock.threshold.us_nasdaq_moderate', -0.025), 'us_nasdaq_severe': _cfg_get('shock.threshold.us_nasdaq_severe', -0.05), 'vix_moderate': _cfg_get('shock.threshold.vix_moderate', 25.0), 'vix_severe': _cfg_get('shock.threshold.vix_severe', 35.0), 'futures_moderate': _cfg_get('shock.threshold.futures_moderate', -0.015), 'futures_severe': _cfg_get('shock.threshold.futures_severe', -0.03), 'usdkrw_moderate': _cfg_get('shock.threshold.usdkrw_moderate', 0.01), 'usdkrw_severe': _cfg_get('shock.threshold.usdkrw_severe', 0.02)}
    YF_TIMEOUT = 10

    def __init__(self):
        self.shock_level = 'NONE'
        self.shock_signals = []
        self.shock_score = 0.0
        self.direction_bias = 0.0
        self.confidence_penalty = 1.0

    @staticmethod
    def _safe_yf_download(ticker: str, period: str='5d', timeout: int=10) -> Optional[pd.DataFrame]:
        """
        이중화 데이터 조회 (resilient_download → yfinance → FMP → 캐시).

        timeout 초과 시 None 반환 — 무한 블로킹 방지.
        컬럼명은 소문자로 정규화하여 반환.
        """
        try:
            from src.utils.resilient_market_data import resilient_download
            result = resilient_download(ticker, period=period)
            if result is not None and len(result) > 0:
                result.columns = [c.lower() if isinstance(c, str) else c for c in result.columns]
                return result
        except Exception as e:
            logger.warning(f'  resilient_download skip: {ticker} — {e}', exc_info=True)
        return None

    def detect(self, target_date: str=None) -> Dict:
        """
        시장 충격 감지 (예측 실행 직전 호출).

        Returns:
            {
                'shock_level': 'NONE'|'MODERATE'|'SEVERE',
                'shock_score': float (0~100),
                'direction_bias': float (-1~+1),
                'confidence_penalty': float (0~1),
                'signals': list[str],
                'recommendation': str,
            }
        """
        self.shock_signals = []
        scores = []
        us_signals = self._check_us_market()
        scores.extend(us_signals)
        vix_signals = self._check_vix()
        scores.extend(vix_signals)
        futures_signals = self._check_futures()
        scores.extend(futures_signals)
        fx_signals = self._check_fx()
        scores.extend(fx_signals)
        pattern_signals = self._check_gap_pattern()
        scores.extend(pattern_signals)
        if scores:
            self.shock_score = min(100, sum(scores))
        else:
            self.shock_score = 0.0
        if self.shock_score >= 60:
            self.shock_level = 'SEVERE'
            self.direction_bias = -0.6
            self.confidence_penalty = 0.4
        elif self.shock_score >= 30:
            self.shock_level = 'MODERATE'
            self.direction_bias = -0.3
            self.confidence_penalty = 0.65
        else:
            self.shock_level = 'NONE'
            self.direction_bias = 0.0
            self.confidence_penalty = 1.0
        recs = {'NONE': '정상 시장 — 기본 예측 유지', 'MODERATE': '⚠️ 주의 — 양수 예측 30% 감쇠, CI 1.5x 확대', 'SEVERE': '🔴 경보 — 양수 예측 60% 감쇠, CI 2.5x 확대, 방어적 전략'}
        result = {'shock_level': self.shock_level, 'shock_score': round(self.shock_score, 1), 'direction_bias': round(self.direction_bias, 3), 'confidence_penalty': round(self.confidence_penalty, 2), 'signals': self.shock_signals, 'recommendation': recs[self.shock_level]}
        try:
            shock_dir = PROJECT_ROOT / 'data' / 'feedback'
            shock_dir.mkdir(parents=True, exist_ok=True)
            date_str = target_date or datetime.now().strftime('%Y-%m-%d')
            with open(shock_dir / f'shock_{date_str}.json', 'w') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return result

    def _check_us_market(self) -> list:
        """미국 시장 마감 데이터 확인."""
        scores = []
        try:
            sent_dir = PROJECT_ROOT / 'data' / 'raw' / 'realtime_sentiment'
            today_str = datetime.now().strftime('%Y-%m-%d')
            sent_path = sent_dir / f'{today_str}.json'
            if not sent_path.exists():
                from datetime import timedelta
                yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
                sent_path = sent_dir / f'{yesterday}.json'
            if sent_path.exists():
                try:
                    sent = json.load(open(sent_path))
                except (Exception,):
                    sent = {}
                sp500_ret = sent.get('sp500_return', sent.get('us_market', {}).get('sp500_return', 0))
                if sp500_ret:
                    sp500_ret = float(sp500_ret)
                    if sp500_ret < self.THRESHOLDS['us_sp500_severe']:
                        scores.append(40)
                        self.shock_signals.append(f'S&P500 {sp500_ret * 100:+.1f}% (SEVERE)')
                    elif sp500_ret < self.THRESHOLDS['us_sp500_moderate']:
                        scores.append(20)
                        self.shock_signals.append(f'S&P500 {sp500_ret * 100:+.1f}% (MODERATE)')
                nasdaq_ret = sent.get('nasdaq_return', sent.get('us_market', {}).get('nasdaq_return', 0))
                if nasdaq_ret:
                    nasdaq_ret = float(nasdaq_ret)
                    if nasdaq_ret < self.THRESHOLDS['us_nasdaq_severe']:
                        scores.append(35)
                        self.shock_signals.append(f'NASDAQ {nasdaq_ret * 100:+.1f}% (SEVERE)')
                    elif nasdaq_ret < self.THRESHOLDS['us_nasdaq_moderate']:
                        scores.append(15)
                        self.shock_signals.append(f'NASDAQ {nasdaq_ret * 100:+.1f}% (MODERATE)')
            if not scores:
                sp = self._safe_yf_download('^GSPC', timeout=self.YF_TIMEOUT)
                if sp is not None and len(sp) >= 2:
                    last_ret = float(sp['close'].iloc[-1] / sp['close'].iloc[-2] - 1)
                    if last_ret < self.THRESHOLDS['us_sp500_severe']:
                        scores.append(40)
                        self.shock_signals.append(f'S&P500(yf) {last_ret * 100:+.1f}% (SEVERE)')
                    elif last_ret < self.THRESHOLDS['us_sp500_moderate']:
                        scores.append(20)
                        self.shock_signals.append(f'S&P500(yf) {last_ret * 100:+.1f}% (MODERATE)')
        except Exception as e:
            logger.error(f'  US 시장 체크 실패: {e}', exc_info=True)
        return scores

    def _check_vix(self) -> list:
        """VIX 수준 확인."""
        scores = []
        try:
            sent_dir = PROJECT_ROOT / 'data' / 'raw' / 'realtime_sentiment'
            for date_offset in range(3):
                from datetime import timedelta
                d = (datetime.now() - timedelta(days=date_offset)).strftime('%Y-%m-%d')
                p = sent_dir / f'{d}.json'
                if p.exists():
                    s = json.load(open(p))
                    vix = s.get('vix_term', {}).get('vix', 0)
                    if vix:
                        vix = float(vix)
                        if vix > self.THRESHOLDS['vix_severe']:
                            scores.append(30)
                            self.shock_signals.append(f'VIX {vix:.1f} (SEVERE)')
                        elif vix > self.THRESHOLDS['vix_moderate']:
                            scores.append(15)
                            self.shock_signals.append(f'VIX {vix:.1f} (MODERATE)')
                    break
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        if not scores:
            vdf = self._safe_yf_download('^VIX', timeout=self.YF_TIMEOUT)
            if vdf is not None and len(vdf) > 0:
                vix_now = float(vdf['close'].iloc[-1])
                if vix_now > self.THRESHOLDS['vix_severe']:
                    scores.append(30)
                    self.shock_signals.append(f'VIX(yf) {vix_now:.1f} (SEVERE)')
                elif vix_now > self.THRESHOLDS['vix_moderate']:
                    scores.append(15)
                    self.shock_signals.append(f'VIX(yf) {vix_now:.1f} (MODERATE)')
        return scores

    def _check_futures(self) -> list:
        """KOSPI 야간 선물 확인."""
        scores = []
        try:
            sent_dir = PROJECT_ROOT / 'data' / 'raw' / 'realtime_sentiment'
            for date_offset in range(3):
                from datetime import timedelta
                d = (datetime.now() - timedelta(days=date_offset)).strftime('%Y-%m-%d')
                p = sent_dir / f'{d}.json'
                if p.exists():
                    s = json.load(open(p))
                    kf = s.get('kospi_futures', {})
                    night_ret = kf.get('night_return', kf.get('change_pct', 0))
                    if night_ret:
                        night_ret = float(night_ret) / 100 if abs(float(night_ret)) > 1 else float(night_ret)
                        if night_ret < self.THRESHOLDS['futures_severe']:
                            scores.append(25)
                            self.shock_signals.append(f'야간선물 {night_ret * 100:+.1f}% (SEVERE)')
                        elif night_ret < self.THRESHOLDS['futures_moderate']:
                            scores.append(12)
                            self.shock_signals.append(f'야간선물 {night_ret * 100:+.1f}% (MODERATE)')
                    break
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return scores

    def _check_fx(self) -> list:
        """USD/KRW 환율 급변 확인 (안 3: 로컬 데이터 우선)."""
        scores = []
        try:
            sc_path = PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text())
                usdkrw_chg = sc.get('usdkrw_change_1m', 0)
                if usdkrw_chg:
                    fx_ret = float(usdkrw_chg) / 100 / 21
                    if fx_ret > self.THRESHOLDS['usdkrw_severe']:
                        scores.append(20)
                        self.shock_signals.append(f'USD/KRW {fx_ret * 100:+.1f}% (원화 급락)')
                    elif fx_ret > self.THRESHOLDS['usdkrw_moderate']:
                        scores.append(10)
                        self.shock_signals.append(f'USD/KRW {fx_ret * 100:+.1f}% (원화 약세)')
                    return scores
        except Exception as _e:
            logger.error(f'  FX 로컬 체크 스킵: {_e}', exc_info=True)
        fx = self._safe_yf_download('KRW=X', timeout=self.YF_TIMEOUT)
        if fx is not None and len(fx) >= 2:
            fx_ret = float(fx['close'].iloc[-1] / fx['close'].iloc[-2] - 1)
            if fx_ret > self.THRESHOLDS['usdkrw_severe']:
                scores.append(20)
                self.shock_signals.append(f'USD/KRW {fx_ret * 100:+.1f}% (원화 급락)')
            elif fx_ret > self.THRESHOLDS['usdkrw_moderate']:
                scores.append(10)
                self.shock_signals.append(f'USD/KRW {fx_ret * 100:+.1f}% (원화 약세)')
        return scores

    def _check_gap_pattern(self) -> list:
        """최근 Gap History에서 연속 방향 오류 패턴 감지."""
        scores = []
        try:
            hist_path = PROJECT_ROOT / 'data' / 'feedback' / 'gap_history.json'
            if hist_path.exists():
                history = json.load(open(hist_path))
                if len(history) >= 2:
                    recent_dirs = [h.get('direction', 50) for h in history[-3:]]
                    if all((d < 50 for d in recent_dirs)):
                        scores.append(15)
                        self.shock_signals.append(f'연속 방향 오류 {len(recent_dirs)}일 (평균 {np.mean(recent_dirs):.0f}%)')
                    recent_gaps = [abs(h.get('avg_gap', 0)) for h in history[-3:]]
                    if all((g > 10 for g in recent_gaps)):
                        scores.append(10)
                        self.shock_signals.append(f'연속 대형 갭 (평균 {np.mean(recent_gaps):.1f}%)')
        except Exception as _e:
            logger.warning(f'  suppressed: {_e}', exc_info=True)
        return scores

    def apply_to_prediction(self, predicted_return: float, current_price: float) -> Dict:
        """
        충격 수준에 따라 예측 수정.

        Returns:
            {'adjusted_return': float, 'adjusted': bool, 'reason': str}
        """
        if self.shock_level == 'NONE':
            return {'adjusted_return': predicted_return, 'adjusted': False, 'reason': 'no shock'}
        original = predicted_return
        adjusted = predicted_return
        if self.shock_level == 'SEVERE':
            if predicted_return > 0:
                adjusted = predicted_return * 0.4
            else:
                adjusted = predicted_return * 1.3
        elif self.shock_level == 'MODERATE':
            if predicted_return > 0:
                adjusted = predicted_return * 0.7
            else:
                adjusted = predicted_return * 1.15
        return {'adjusted_return': round(adjusted, 6), 'adjusted': True, 'reason': f'{self.shock_level}: {len(self.shock_signals)} signals, score={self.shock_score:.0f}', 'original_return': original}
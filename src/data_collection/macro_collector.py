"""[Phase 69: SSOT] 내부 매크로 데이터 수집기.

Data_Hub_Agent 외부 의존성 완전 제거 후
`data/macro/macro_data.parquet`를 Project_Meridian 내부에서
직접 생성하는 SSOT(Single Source of Truth) 수집기.

수집 지표 (4개 시 컨텐):
  high_yield_spread   : HYG-IEF yield proxy (또는 FRED BAMLH0A0HYM2)
  copper_gold_ratio   : 동/금 선물 비율 (HG=F / GC=F)
  cboe_skew           : CBOE SKEW Index (^SKEW) — [Phase 69] ^PCCE 404 대체
  gscpi               : NY Fed 글로벌 공급망 압력지수 (FRED GSCPI)

사용 승인 설계:
  Q1. GSCPI: FRED 키 없으면 Forward-fill (Graceful Degradation)
  Q2. HY Spread: yfinance HYG-IEF 스프레드 프록시, FRED 또는 API 키 있으면 우선
  Q3. 추론 흐름 별도 포함 안 함 — 수집 + parquet 갱신만
"""
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
logger = logging.getLogger('macro_collector')
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
_MACRO_DIR = ROOT / 'data' / 'macro'
_PARQUET = _MACRO_DIR / 'macro_data.parquet'
_CACHE_JSON = _MACRO_DIR / 'macro_cache.json'
_LOOKBACK: int = 252

class MacroCollector:
    """[Phase 65] Project_Meridian 내부 매크로 데이터 수집기."""

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg
        from src.utils.credential_manager import CredentialManager
        self.fred_key = CredentialManager().read_from_env('FRED_API_KEY') or ''
        _MACRO_DIR.mkdir(parents=True, exist_ok=True)
        _get = (lambda k, d: cfg.get(k, d)) if cfg and hasattr(cfg, 'get') else lambda k, d: d
        self._lookback: int = int(_get('data.macro_lookback_days', 252))
        self._ffill_limit: int = int(_get('data.ffill_limit', 3))
        self._hy_rolling: int = int(_get('data.hy_spread_rolling_window', 20))
        try:
            from src.utils.vendor_multiplexer import VendorMultiplexer
            self._vmx = VendorMultiplexer(cfg)
        except ImportError as e:
            self._vmx = None

    def collect_all(self) -> pd.DataFrame:
        """4개 매크로 지표를 수집하여 parquet 저장 후 DataFrame 반환.

        콜럼: date, high_yield_spread, copper_gold_ratio, cboe_skew, gscpi
        """
        logger.info('[Phase 65] 매크로 데이터 수집 시작')
        try:
            import yfinance as yf
        except ImportError as e:
            raise RuntimeError('[Phase 65] yfinance 미설치 — pip install yfinance')
        end = datetime.today()
        _buf = float(self._cfg.get('data.macro_lookback_buffer', 1.5)) if self._cfg and hasattr(self._cfg, 'get') else 1.5
        start = end - timedelta(days=int(self._lookback * _buf))
        frames = {}
        frames['high_yield_spread'] = self._fetch_hy_spread(yf, start, end)
        frames['copper_gold_ratio'] = self._fetch_copper_gold(yf, start, end)
        frames['cboe_skew'] = self._fetch_cboe_skew(yf, start, end)
        frames['gscpi'] = self._fetch_gscpi(start, end)
        df = pd.DataFrame(frames)
        df.index.name = 'date'
        _proxy_for_impute = df.copy()
        try:
            from src.utils.data_imputer import OrthogonalDataImputer, DataNoGoException
            _r2 = float(self._cfg.get('imputer.r2_threshold', 0.9) if self._cfg and hasattr(self._cfg, 'get') else 0.9)
            _imputer = OrthogonalDataImputer(r2_threshold=_r2)
            for _col in list(df.columns):
                if not df[_col].isna().any():
                    continue
                _proxies = _proxy_for_impute.drop(columns=[_col]).dropna(how='any')
                if _proxies.empty or len(_proxies) < 10:
                    continue
                try:
                    df[_col] = df[_col].fillna(_imputer.impute(_col, df[_col], _proxies, df.index))
                    logger.info(f'  [Phase 70] {_col}: PCA 직교 합성 성공')
                except DataNoGoException as _dne:
                    logger.error(f'  [Phase 70 DATA_NOGO] {_col}: R²={_dne.r2:.3f} < {_dne.threshold:.2f} — 해당 컬럼 제외')
                    raise
        except (ImportError, Exception) as _imp_e:
            if 'DataNoGoException' in type(_imp_e).__name__:
                raise
            logger.error(f'  [Phase 70] Imputer 에러: {_imp_e}', exc_info=True)
            raise
        df = df.dropna(how='all')
        df.to_parquet(_PARQUET, compression='snappy')
        logger.info(f'[Phase 65] 매크로 파케 저장: {_PARQUET} ({len(df)}행 x {len(df.columns)}콸럼)')
        _CACHE_JSON.write_text(json.dumps({'collected_at': datetime.now().isoformat(), 'rows': len(df), 'cols': list(df.columns)}, ensure_ascii=False, indent=2), encoding='utf-8')
        return df

    def _fetch_hy_spread(self, yf, start, end) -> pd.Series:
        """HY 크레딧 스프레드 프록시.

        우선: FRED BAMLH0A0HYM2 (% 단위)
        Fallback: (HYG 롤마1 30d 표준편차 * sqrt(252)) 세미-프록시
        """
        if self.fred_key:
            try:
                fred_s = self._fred_series('BAMLH0A0HYM2', start, end, label='HY_Spread(FRED)')
                _fred_min = int(self._cfg.get('data.fred_min_series_length', 20)) if self._cfg and hasattr(self._cfg, 'get') else 20
                if fred_s is not None and len(fred_s) > _fred_min:
                    return fred_s.rename('high_yield_spread')
            except Exception as _e:
                logger.error(f'  FRED HY Spread 실패: {_e}', exc_info=True)
        logger.info('  HY Spread: VendorMultiplexer HYG/IEF 프록시')
        _ss, _es = (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if self._vmx is None:
            raise RuntimeError('[Phase 70] VendorMultiplexer가 주입되지 않았습니다.')
        try:
            hyg = self._vmx.fetch('HYG', _ss, _es)
            ief = self._vmx.fetch('IEF', _ss, _es)
            hyg_ret = hyg.pct_change().rolling(self._hy_rolling).std() * np.sqrt(252) * 100
            ief_ret = ief.pct_change().rolling(self._hy_rolling).std() * np.sqrt(252) * 100
            spread = (hyg_ret - ief_ret).dropna()
            return spread.rename('high_yield_spread')
        except Exception as _e:
            logger.warning(f'  HY Spread 수집 실패 (VMX 에러): {_e} — PCA 대기', exc_info=True)
            idx = pd.date_range(start, end, freq='B')
            return pd.Series(float('nan'), index=idx, name='high_yield_spread')

    def _fetch_copper_gold(self, yf, start, end) -> pd.Series:
        """[Phase 70-Integration] VendorMultiplexer 경유 Copper/Gold 비율."""
        _ss, _es = (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if self._vmx is None:
            raise RuntimeError('[Phase 70] VendorMultiplexer가 주입되지 않았습니다.')
        try:
            _copper = self._vmx.fetch('HG=F', _ss, _es)
            _gold = self._vmx.fetch('GC=F', _ss, _es)
            ratio = (_copper / _gold).replace([float('inf'), float('-inf')], float('nan')).dropna()
            return ratio[ratio > 0].rename('copper_gold_ratio')
        except Exception as _e:
            logger.warning(f'  Copper/Gold 수집 실패 (VMX 에러): {_e} — PCA 대기', exc_info=True)
            idx = pd.date_range(start, end, freq='B')
            return pd.Series(float('nan'), index=idx, name='copper_gold_ratio')

    def _fetch_cboe_skew(self, yf, start, end) -> pd.Series:
        """[Phase 70-Integration] VendorMultiplexer 경유 ^SKEW 수집."""
        _ss, _es = (start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if self._vmx is None:
            raise RuntimeError('[Phase 70] VendorMultiplexer가 주입되지 않았습니다.')
        try:
            return self._vmx.fetch('^SKEW', _ss, _es).rename('cboe_skew')
        except Exception as _e:
            logger.warning(f'  CBOE SKEW 수집 실패 (VMX 에러): {_e} — PCA 대기', exc_info=True)
            idx = pd.date_range(start, end, freq='B')
            return pd.Series(float('nan'), index=idx, name='cboe_skew')

    def _fetch_gscpi(self, start, end) -> pd.Series:
        """NY Fed GSCPI (FRED 'GSCPI').

        FRED API 실패 시 NaN 반환하여 PCA Imputer가 합성하도록 유도.
        """
        if self.fred_key:
            try:
                gscpi = self._fred_series('GSCPI', start, end, label='GSCPI(FRED)')
                _gscpi_min = int(self._cfg.get('data.gscpi_min_obs', 6)) if self._cfg and hasattr(self._cfg, 'get') else 6
                if gscpi is not None and len(gscpi) > _gscpi_min:
                    idx = pd.date_range(start, end, freq='B')
                    return gscpi.reindex(idx).ffill().rename('gscpi')
            except Exception as _e:
                logger.warning(f'  GSCPI 수집 실패 (FRED 에러): {_e} — PCA 대기', exc_info=True)
        logger.warning('  GSCPI: 수집 실패. PCA Imputer 대기.')
        idx = pd.date_range(start, end, freq='B')
        return pd.Series(float('nan'), index=idx, name='gscpi')

    def _fred_series(self, series_id: str, start, end, label: str='') -> Optional[pd.Series]:
        """FRED API를 통해 시계열 데이터 수집."""
        import requests
        url = 'https://api.stlouisfed.org/fred/series/observations'
        params = {'series_id': series_id, 'api_key': self.fred_key, 'file_type': 'json', 'observation_start': start.strftime('%Y-%m-%d'), 'observation_end': end.strftime('%Y-%m-%d')}
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        obs = data.get('observations', [])
        if not obs:
            return None
        s = pd.Series({o['date']: float(o['value']) for o in obs if o['value'] != '.'}, name=series_id)
        s.index = pd.to_datetime(s.index)
        logger.info(f'  {label}: {len(s)}개 관측치 수집 완료')
        return s
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    import sys
    print('[Phase 65] MacroCollector 단독 테스트')
    df = MacroCollector().collect_all()
    print(df.tail(5).to_string())
    print(f'\n콜럼: {list(df.columns)}')
    print(f'NaN 비율:\n{df.isna().mean().round(4).to_string()}')
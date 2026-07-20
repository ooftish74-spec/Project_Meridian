"""
Cross-Asset Signal Engine — V5 개선사항 #11 구현
==================================================
다자산 다지역 시그널 통합.

해외 ETF/지수 → 한국 시장 리드-래그 시그널:
  - SPY/QQQ: 미국 기술주 → 한국 IT/반도체
  - TLT: 미국 금리 → 한국 성장주/금융주
  - EWY: 한국 ETF → 해외 자금 흐름 추적
  - VIX: 글로벌 공포지수 → 레짐 보조
  - DXY/USD-KRW: 달러 → 수출주 영향

Author: Project-A
Date: 2026-05-01
"""
import json
import logging
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS = ROOT / 'results'
DATA = ROOT / 'data'
GLOBAL_ASSETS = {'SPY': {'name': 'S&P 500', 'kr_impact': ['KOSPI', 'tech', 'large_cap']}, 'QQQ': {'name': 'NASDAQ-100', 'kr_impact': ['IT', 'semiconductor']}, 'SOXX': {'name': 'Semiconductor', 'kr_impact': ['semiconductor']}, 'TLT': {'name': 'US 20Y Treasury', 'kr_impact': ['growth', 'utilities']}, 'HYG': {'name': 'High Yield Bond', 'kr_impact': ['risk_sentiment']}, 'EWY': {'name': 'Korea ETF', 'kr_impact': ['KOSPI', 'foreign_flow']}, 'GLD': {'name': 'Gold', 'kr_impact': ['safe_haven']}, 'USO': {'name': 'Oil', 'kr_impact': ['energy', 'chemical']}, 'EWJ': {'name': 'Japan ETF', 'kr_impact': ['asia_sentiment']}, 'FXI': {'name': 'China Large Cap', 'kr_impact': ['china_exposure']}}

class CrossAssetSignalEngine:
    """
    다자산 리드-래그 시그널 엔진.

    미국 시장이 한국보다 약 14시간 먼저 마감하므로,
    미국 마감 데이터 → 한국 개장 시그널로 활용.
    """

    def __init__(self):
        self._cache: Dict = {}

    def generate_signals(self) -> Dict:
        """
        최신 글로벌 데이터에서 한국 시장 시그널 생성.

        Returns:
            {
                'composite_signal': -1.0~+1.0,
                'asset_signals': {asset: {signal, weight, data}},
                'regime_adjustment': str,
                'timestamp': str,
            }
        """
        global_data = self._load_global_data()
        asset_signals = {}
        for asset, info in GLOBAL_ASSETS.items():
            sig = self._compute_asset_signal(asset, global_data.get(asset, {}))
            if sig is not None:
                asset_signals[asset] = {'signal': round(sig, 4), 'name': info['name'], 'kr_impact': info['kr_impact']}
        weights = {'SPY': 0.2, 'QQQ': 0.15, 'SOXX': 0.1, 'EWY': 0.15, 'TLT': 0.1, 'GLD': 0.05, 'USO': 0.05, 'EWJ': 0.05, 'FXI': 0.1, 'HYG': 0.05}
        composite = 0.0
        total_w = 0.0
        for asset, data in asset_signals.items():
            w = weights.get(asset, 0.05)
            composite += data['signal'] * w
            total_w += w
        if total_w > 0:
            composite /= total_w
        macro_adj = self._compute_macro_adjustment()
        if macro_adj != 0:
            composite = composite * 0.85 + macro_adj * 0.15
            composite = float(np.clip(composite, -1.0, 1.0))
        regime_adj = 'neutral'
        try:
            from config.dynamic_config import DynamicConfig
            z_ext = DynamicConfig().get('adaptive.z_score_extreme', 1.5)
            hist_file = RESULTS / 'cross_asset_signals_history.json'
            comp_hist = []
            if hist_file.exists():
                try:
                    hist_data = json.loads(hist_file.read_text())
                    comp_hist = [h.get('composite_signal', 0) for h in hist_data[-60:]]
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at cross_asset_signals.py:128', exc_info=True)
            if len(comp_hist) >= 20:
                from src.utils.adaptive_thresholds import VolatilityScaledThreshold
                if VolatilityScaledThreshold.is_extreme(composite, comp_hist, z_score_limit=z_ext, percentile_limit=90.0):
                    regime_adj = 'risk_on'
                elif VolatilityScaledThreshold.is_extreme(-composite, [-c for c in comp_hist], z_score_limit=z_ext, percentile_limit=90.0):
                    regime_adj = 'risk_off'
            else:
                spy_vol = global_data.get('SPY', {}).get('change_pct', 0.0)
                dyn_thresh = max(0.3, abs(spy_vol) * 1.5)
                if composite > dyn_thresh:
                    regime_adj = 'risk_on'
                elif composite < -dyn_thresh:
                    regime_adj = 'risk_off'
        except Exception as e:
            logger.error(f'동적 레짐 조정 실패: {e}', exc_info=True)
            if composite > 0.3:
                regime_adj = 'risk_on'
            elif composite < -0.3:
                regime_adj = 'risk_off'
        result = {'composite_signal': round(composite, 4), 'asset_signals': asset_signals, 'regime_adjustment': regime_adj, 'macro_adjustment': round(macro_adj, 4), 'n_assets_available': len(asset_signals), 'timestamp': datetime.now().isoformat()}
        sig_file = RESULTS / 'cross_asset_signals.json'
        try:
            with open(sig_file, 'w') as f:
                json.dump(result, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f'  cross_asset_signals.json 저장 실패 (비치명적): {e}', exc_info=True)
        return result

    def get_sector_signals(self) -> Dict[str, float]:
        """
        글로벌 데이터에서 한국 섹터별 시그널 추출.

        Returns:
            {sector: signal_strength}
        """
        signals = self.generate_signals()
        sector_agg = {}
        for asset, data in signals.get('asset_signals', {}).items():
            sig = data.get('signal', 0)
            for sector in data.get('kr_impact', []):
                sector_agg.setdefault(sector, []).append(sig)
        return {sector: round(float(np.mean(sigs)), 4) for sector, sigs in sector_agg.items()}

    def _load_global_data(self) -> Dict:
        """글로벌 자산 데이터 로드 (4개 소스 폴백).

        Source 우선순위:
          1. overnight_intelligence.json (가장 상세)
          2. data/cross_market/latest.json
          3. data/cross_market/us_market_data.json
          4. ★ signal_cache.json + us_market_regime.json (항상 존재)
        """
        result = {}
        oi_file = RESULTS / 'overnight_intelligence.json'
        if oi_file.exists():
            try:
                oi = json.loads(oi_file.read_text())
                markets = oi.get('markets', {})
                for asset in GLOBAL_ASSETS:
                    if asset in markets:
                        result[asset] = markets[asset]
                    elif asset.lower() in markets:
                        result[asset] = markets[asset.lower()]
            except Exception as _e:
                logger.warning(f'  Unexpected: {_e}', exc_info=True)
        cm_file = DATA / 'cross_market' / 'latest.json'
        if cm_file.exists():
            try:
                cm = json.loads(cm_file.read_text())
                for asset in GLOBAL_ASSETS:
                    if asset not in result and asset in cm:
                        result[asset] = cm[asset]
            except Exception as _e:
                logger.warning(f'  Unexpected: {_e}', exc_info=True)
        usa_file = DATA / 'cross_market' / 'us_market_data.json'
        if usa_file.exists():
            try:
                usa = json.loads(usa_file.read_text())
                etfs = usa.get('etf_data', {})
                for asset in GLOBAL_ASSETS:
                    if asset not in result and asset in etfs:
                        result[asset] = etfs[asset]
            except Exception as _e:
                logger.warning(f'  Unexpected: {_e}', exc_info=True)
        if len(result) < 3:
            result = self._fallback_from_pipeline(result)
        return result

    def _fallback_from_pipeline(self, existing: Dict) -> Dict:
        """signal_cache + us_market_regime에서 cross-asset 데이터 구성."""
        result = dict(existing)
        sc_file = RESULTS / 'signal_cache.json'
        sc = {}
        if sc_file.exists():
            try:
                sc = json.loads(sc_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at cross_asset_signals.py:258', exc_info=True)
        usr_file = RESULTS / 'us_market_regime.json'
        usr = {}
        if usr_file.exists():
            try:
                usr = json.loads(usr_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at cross_asset_signals.py:269', exc_info=True)
        oi = sc.get('overnight_intel', {})
        if 'SPY' not in result:
            sp_chg = oi.get('sp500_change_pct', 0)
            if sp_chg == 0 and sc.get('sp500_change_1m'):
                sp_chg = float(sc['sp500_change_1m']) / 21
            if sp_chg != 0:
                result['SPY'] = {'change_pct': sp_chg, 'close': sc.get('sp500', 0)}
        if 'QQQ' not in result:
            nq_chg = oi.get('nasdaq_change_pct', 0)
            if nq_chg == 0 and sc.get('nasdaq_change_1m'):
                nq_chg = float(sc['nasdaq_change_1m']) / 21
            if nq_chg != 0:
                result['QQQ'] = {'change_pct': nq_chg, 'close': sc.get('nasdaq', 0)}
        if 'SOXX' not in result:
            sox_chg = oi.get('sox_change_pct', sc.get('sox_change', 0))
            if sox_chg != 0:
                result['SOXX'] = {'change_pct': float(sox_chg)}
        if 'GLD' not in result and sc.get('gold_us_change_1m') is not None:
            gold_chg = float(sc['gold_us_change_1m']) / 21
            result['GLD'] = {'change_pct': gold_chg, 'close': sc.get('gold_us', 0)}
        if 'USO' not in result and sc.get('wti_change_1m') is not None:
            wti_chg = float(sc['wti_change_1m']) / 21
            result['USO'] = {'change_pct': wti_chg, 'close': sc.get('wti', 0)}
        if 'TLT' not in result and sc.get('us10y_change_1m') is not None:
            tlt_chg = float(sc['us10y_change_1m']) / 21
            result['TLT'] = {'change_pct': tlt_chg, 'close': sc.get('us10y', 0)}
        if 'HYG' not in result and sc.get('vix'):
            vix = float(sc['vix'])
            vix_1m = float(sc.get('vix_change_1m', 0))
            hyg_chg = -vix_1m / 21 * 0.3
            result['HYG'] = {'change_pct': hyg_chg, 'vix': vix}
        if 'EWY' not in result:
            ewy_ret = oi.get('ewy_return', 0)
            if ewy_ret == 0:
                usdkrw_chg = float(sc.get('usdkrw_change_1m', 0)) / 21
                if usdkrw_chg != 0:
                    ewy_ret = -usdkrw_chg * 0.5
            if ewy_ret != 0:
                result['EWY'] = {'change_pct': float(ewy_ret)}
        if 'FXI' not in result and sc.get('dxy_change_1m') is not None:
            fxi_chg = -float(sc['dxy_change_1m']) / 21 * 0.5
            result['FXI'] = {'change_pct': fxi_chg, 'dxy': sc.get('dxy', 0)}
        if len(result) > len(existing):
            logger.info(f'  ★ Cross-Asset fallback: {len(result)}개 자산 (signal_cache + us_market_regime)')
        return result

    def _compute_macro_adjustment(self) -> float:
        """macro_features에서 종합 보정값 계산.

        Returns:
            -1.0 ~ +1.0 (양수=강세, 음수=약세)
        """
        sc_file = RESULTS / 'signal_cache.json'
        if not sc_file.exists():
            return 0.0
        try:
            sc = json.loads(sc_file.read_text())
            macro = sc.get('macro_features', {})
            if not macro:
                return 0.0
            signals = []
            weights = []
            hy = macro.get('fred_hy_spread')
            if hy is not None:
                from config.dynamic_config import DynamicConfig as _DC
                _cfg = _DC()
                hy_raw = float(hy)
                hy_mean = _cfg.get('cross_asset.hy_spread_long_term_mean', 5.0)
                hy_std = _cfg.get('cross_asset.hy_spread_long_term_std', 2.0)
                hy_zscore = (hy_raw - hy_mean) / max(hy_std, 0.01)
                hy_signal = float(np.clip(-hy_zscore / 2.0, -1.0, 1.0))
                signals.append(hy_signal)
                weights.append(0.25)
            yc = macro.get('cross_yield_signal')
            if yc is not None:
                signals.append(float(yc))
                weights.append(0.2)
            ism = macro.get('cross_ism_signal')
            if ism is not None:
                signals.append(float(ism))
                weights.append(0.2)
            caixin = macro.get('cross_caixin_signal')
            if caixin is not None:
                signals.append(float(caixin))
                weights.append(0.15)
            news = macro.get('news_llm_sentiment', macro.get('news_naver_sentiment'))
            if news is not None:
                signals.append(float(news))
                weights.append(0.1)
            cs = macro.get('fred_consumer_sentiment')
            if cs is not None:
                signals.append(float(cs))
                weights.append(0.1)
            if not weights:
                return 0.0
            total_w = sum(weights)
            weighted = sum((s * w for s, w in zip(signals, weights)))
            return float(np.clip(weighted / total_w, -1.0, 1.0))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return 0.0

    def _compute_asset_signal(self, asset: str, data: Dict) -> Optional[float]:
        """개별 자산 데이터 → -1.0~+1.0 시그널."""
        if not data:
            return None
        change_pct = data.get('change_pct', data.get('pct_change', data.get('daily_return', None)))
        if change_pct is not None:
            if isinstance(change_pct, str):
                try:
                    change_pct = float(change_pct.replace('%', ''))
                except ValueError:
                    return None
            signal = np.clip(change_pct / 3.0, -1.0, 1.0)
            if asset in ('TLT', 'GLD'):
                signal = -signal
            return float(signal)
        return None
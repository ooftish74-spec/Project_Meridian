"""
Macro Feature Integrator — 미사용 수집 데이터 통합
======================================================
수집되고 있지만 학습/신호에 활용되지 않던 17개 데이터를
signal_cache에 정규화 피처로 적재하여 모든 소비 모듈이 참조 가능하게 합니다.

통합 대상:
  FRED 6개: unemployment_rate, fed_funds_rate, gdp_growth,
            consumer_sentiment, hy_credit_spread, ig_credit_spread
  크로스마켓 4개: us_jp_spread, yield_curve, ism_pmi, caixin_pmi
  뉴스 감성 2개: llm_sentiment, naver_news_sentiment
  섹터 배치 4개: sector_correlation, supply_chain, per_band, us_kr_beta
  대체 데이터 1개: short_selling

출력:
  signal_cache.json에 'macro_features' 키로 적재
  → RegimeEngine, MorningFusion, S3 Factor, CrossAsset 등에서 참조

Author: Project Meridian
Date: 2026-05-29
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_DATA = _PROJECT_ROOT / 'data'

class MacroFeatureIntegrator:
    """미사용 수집 데이터를 정규화 피처로 통합."""

    def __init__(self):
        self._signal_cache = self._load_json(_RESULTS / 'signal_cache.json')

    def integrate_all(self) -> Dict[str, Any]:
        """모든 미사용 데이터를 통합하여 signal_cache에 적재.

        Returns:
            {'macro_features': {...}, 'n_features': int, 'sources': [...]}
        """
        features = {}
        sources = []
        fred_features = self._integrate_fred()
        if fred_features:
            features.update(fred_features)
            sources.append('FRED')
        cross_features = self._integrate_cross_market()
        if cross_features:
            features.update(cross_features)
            sources.append('CrossMarket')
        news_features = self._integrate_news_sentiment()
        if news_features:
            features.update(news_features)
            sources.append('NewsSentiment')
        sector_features = self._integrate_sector_batch()
        if sector_features:
            features.update(sector_features)
            sources.append('SectorBatch')
        short_features = self._integrate_short_selling()
        if short_features:
            features.update(short_features)
            sources.append('ShortSelling')
        result = {'macro_features': features, 'n_features': len(features), 'sources': sources, 'timestamp': datetime.now().isoformat()}
        self._save_to_signal_cache(features)
        logger.info(f'  ✅ MacroFeatureIntegrator: {len(features)}개 피처, sources={sources}')
        return result

    def _integrate_fred(self) -> Dict:
        """FRED 수집 데이터 → 정규화 피처."""
        features = {}
        sc = self._signal_cache
        unemp = sc.get('unemployment_rate')
        if unemp is not None:
            unemp = float(unemp)
            features['fred_unemployment'] = round(np.clip((4.0 - unemp) / 2.0, -1, 1), 3)
        ffr = sc.get('federal_funds_rate')
        if ffr is not None:
            ffr = float(ffr)
            features['fred_fed_rate'] = round(np.clip((4.5 - ffr) / 2.0, -1, 1), 3)
        gdp = sc.get('gdp_growth')
        if gdp is not None:
            gdp = float(gdp)
            features['fred_gdp_growth'] = round(np.clip(gdp / 5.0, -1, 1), 3)
        cs = sc.get('consumer_sentiment')
        if cs is not None:
            cs = float(cs)
            features['fred_consumer_sentiment'] = round(np.clip((cs - 65) / 30, -1, 1), 3)
        hy = sc.get('hy_credit_spread')
        if hy is not None:
            hy = float(hy)
            features['fred_hy_spread'] = round(np.clip((4.0 - hy) / 3.0, -1, 1), 3)
            features['credit_stress'] = 1 if hy > 5.0 else 0
        ig = sc.get('ig_credit_spread')
        if ig is not None:
            ig = float(ig)
            features['fred_ig_spread'] = round(np.clip((1.5 - ig) / 1.5, -1, 1), 3)
        return features

    def _integrate_cross_market(self) -> Dict:
        """크로스마켓 CSV → 정규화 피처.

        수정 2026-05-29:
          - 경로: data/cross_market/ → data/raw/cross_market/
          - CSV 컬럼명 실제 파일과 일치하도록 매핑 수정
        """
        features = {}
        cm_dir = _DATA / 'raw' / 'cross_market'
        usjp_file = cm_dir / 'us_jp_spread.csv'
        if usjp_file.exists():
            try:
                df = pd.read_csv(usjp_file)
                if len(df) >= 2:
                    spread = float(df.iloc[-1].get('US_JP_Spread', df.iloc[-1].get('spread', 0)))
                    prev = float(df.iloc[-2].get('US_JP_Spread', df.iloc[-2].get('spread', 0)))
                    features['cross_usjp_spread'] = round(spread, 3)
                    features['cross_usjp_change'] = round(spread - prev, 3)
                    features['cross_usjp_signal'] = round(np.clip((2.0 - spread) / 2.0, -1, 1), 3)
            except Exception as e:
                logger.error(f'  US-JP 스프레드 로드 실패: {e}', exc_info=True)
        yc_file = cm_dir / 'us_yield_curve.csv'
        if yc_file.exists():
            try:
                df = pd.read_csv(yc_file)
                if len(df) >= 1:
                    row = df.iloc[-1]
                    spread_10_2 = float(row.get('Yield_Curve_2Y10Y', row.get('10Y_2Y', row.get('spread_10y_2y', 0))))
                    features['cross_yield_curve'] = round(spread_10_2, 3)
                    inverted = row.get('Curve_Inverted', None)
                    features['yield_curve_inverted'] = int(inverted) if inverted is not None else 1 if spread_10_2 < 0 else 0
                    features['cross_yield_signal'] = round(np.clip(spread_10_2 / 1.0, -1, 1), 3)
            except Exception as e:
                logger.error(f'  Yield Curve 로드 실패: {e}', exc_info=True)
        pmi_file = cm_dir / 'us_ism_pmi.csv'
        if pmi_file.exists():
            try:
                df = pd.read_csv(pmi_file)
                if len(df) >= 1:
                    row = df.iloc[-1]
                    pmi = float(row.get('PMI', row.get('ISM_PMI', 0)))
                    if pmi == 0:
                        prod = row.get('US_Ind_Production', None)
                        if prod is not None and len(df) >= 2:
                            prev_prod = df.iloc[-2].get('US_Ind_Production', prod)
                            if prev_prod and prev_prod > 0:
                                pmi_proxy = 50 + (float(prod) / float(prev_prod) - 1) * 500
                                pmi = round(float(np.clip(pmi_proxy, 30, 70)), 1)
                    if pmi > 0:
                        features['cross_ism_pmi'] = round(pmi, 1)
                        features['cross_ism_signal'] = round(np.clip((pmi - 50) / 10, -1, 1), 3)
            except Exception as e:
                logger.error(f'  ISM PMI 로드 실패: {e}', exc_info=True)
        cpmi_file = cm_dir / 'china_pmi.csv'
        if cpmi_file.exists():
            try:
                df = pd.read_csv(cpmi_file)
                if len(df) >= 1:
                    row = df.iloc[-1]
                    cpmi = float(row.get('PMI', row.get('Caixin_PMI', 0)))
                    if cpmi == 0:
                        proxy = row.get('US_Mfg_Emp_proxy', None)
                        if proxy is not None and len(df) >= 2:
                            prev_proxy = df.iloc[-2].get('US_Mfg_Emp_proxy', proxy)
                            if prev_proxy and prev_proxy > 0:
                                cpmi = round(50 + (float(proxy) / float(prev_proxy) - 1) * 500, 1)
                                cpmi = float(np.clip(cpmi, 30, 70))
                    if cpmi > 0:
                        features['cross_caixin_pmi'] = round(cpmi, 1)
                        features['cross_caixin_signal'] = round(np.clip((cpmi - 50) / 5, -1, 1), 3)
            except Exception as e:
                logger.error(f'  Caixin PMI 로드 실패: {e}', exc_info=True)
        return features

    def _integrate_news_sentiment(self) -> Dict:
        """뉴스 감성 수집 결과 → 정규화 피처."""
        features = {}
        llm_file = _RESULTS / 'llm_sentiment_results.json'
        if llm_file.exists():
            try:
                llm = json.loads(llm_file.read_text())
                overall = llm.get('overall_sentiment', llm.get('composite_score', 0))
                if isinstance(overall, (int, float)):
                    features['news_llm_sentiment'] = round(float(np.clip(overall, -1, 1)), 3)
                sector_sent = llm.get('sector_sentiment', {})
                for sector, score in sector_sent.items():
                    if isinstance(score, (int, float)):
                        key = f'news_sector_{sector}'
                        features[key] = round(float(np.clip(score, -1, 1)), 3)
                contrarian = llm.get('contrarian_signal', 0)
                if contrarian:
                    features['news_contrarian'] = round(float(contrarian), 3)
            except Exception as e:
                logger.error(f'  LLM 감성 로드 실패: {e}', exc_info=True)
        sent_dir = _DATA / 'sentiment'
        if sent_dir.exists():
            try:
                scores = []
                for ticker_dir in sent_dir.iterdir():
                    if not ticker_dir.is_dir():
                        continue
                    daily_file = ticker_dir / 'daily_signal.csv'
                    if daily_file.exists():
                        try:
                            df = pd.read_csv(daily_file)
                            if len(df) >= 1:
                                score = df.iloc[-1].get('sentiment_score', df.iloc[-1].get('score', None))
                                if score is not None:
                                    scores.append(float(score))
                        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                            import logging
                            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                            logger.warning('[SILENT_BYPASS] Suppressed exception at macro_feature_integrator.py:322', exc_info=True)
                if scores:
                    avg = np.mean(scores)
                    features['news_naver_sentiment'] = round(float(np.clip(avg / 50 - 1, -1, 1)), 3)
                    features['news_naver_n_tickers'] = len(scores)
            except Exception as e:
                logger.error(f'  네이버 뉴스 감성 로드 실패: {e}', exc_info=True)
        rt_file = _DATA / 'raw' / 'realtime_sentiment'
        if rt_file.exists():
            try:
                files = sorted(rt_file.glob('*.json'), reverse=True)
                if files:
                    rt = json.loads(files[0].read_text())
                    market_sent = rt.get('market_sentiment', rt.get('overall', 0))
                    if isinstance(market_sent, (int, float)):
                        features['news_realtime_sentiment'] = round(float(np.clip(market_sent / 50 - 1, -1, 1)), 3)
            except Exception as e:
                logger.error(f'  실시간 감성 로드 실패: {e}', exc_info=True)
        return features

    def _integrate_sector_batch(self) -> Dict:
        """섹터 배치 수집 결과 → 정규화 피처."""
        features = {}
        sb_dir = _DATA / 'sector_analysis'
        corr_file = sb_dir / 'sector_correlation.json'
        if corr_file.exists():
            try:
                corr = json.loads(corr_file.read_text())
                if isinstance(corr, dict):
                    all_corrs = []
                    for k, v in corr.items():
                        if isinstance(v, dict):
                            all_corrs.extend([float(c) for c in v.values() if isinstance(c, (int, float))])
                    if all_corrs:
                        avg_corr = np.mean(all_corrs)
                        features['sector_avg_correlation'] = round(avg_corr, 3)
                        features['sector_diversification'] = round(1 - avg_corr, 3)
            except Exception as e:
                logger.error(f'  섹터 상관관계 로드 실패: {e}', exc_info=True)
        per_file = sb_dir / 'per_band.json'
        if per_file.exists():
            try:
                per = json.loads(per_file.read_text())
                if isinstance(per, dict):
                    for sector, data in per.items():
                        if isinstance(data, dict):
                            current_per = data.get('current', 0)
                            avg_per = data.get('average', data.get('mean', 0))
                            if current_per and avg_per:
                                position = (current_per - avg_per * 0.7) / (avg_per * 0.6) if avg_per else 0.5
                                features[f'sector_per_{sector}'] = round(float(np.clip(position, 0, 1)), 3)
            except Exception as e:
                logger.error(f'  PER 밴드 로드 실패: {e}', exc_info=True)
        beta_file = sb_dir / 'us_kr_beta.json'
        if beta_file.exists():
            try:
                beta = json.loads(beta_file.read_text())
                if isinstance(beta, dict):
                    for sector, val in beta.items():
                        if isinstance(val, (int, float)):
                            features[f'sector_uskr_beta_{sector}'] = round(float(val), 3)
                    vals = [v for v in beta.values() if isinstance(v, (int, float))]
                    if vals:
                        features['sector_avg_uskr_beta'] = round(float(np.mean(vals)), 3)
            except Exception as e:
                logger.error(f'  US-KR 베타 로드 실패: {e}', exc_info=True)
        sc_file = sb_dir / 'supply_chain.json'
        if sc_file.exists():
            try:
                supply = json.loads(sc_file.read_text())
                if isinstance(supply, dict):
                    risk = supply.get('overall_risk', supply.get('risk_score', 0))
                    if isinstance(risk, (int, float)):
                        features['sector_supply_chain_risk'] = round(float(np.clip(risk / 100, 0, 1)), 3)
            except Exception as e:
                logger.error(f'  공급망 데이터 로드 실패: {e}', exc_info=True)
        return features

    def _integrate_short_selling(self) -> Dict:
        """공매도 수집 결과 → 정규화 피처."""
        features = {}
        short_dir = _DATA / 'raw' / 'short_selling'
        if short_dir.exists():
            try:
                files = sorted(short_dir.glob('*.json'), reverse=True)
                if files:
                    data = json.loads(files[0].read_text())
                    market_ratio = data.get('market_short_ratio', data.get('overall_ratio', 0))
                    if isinstance(market_ratio, (int, float)):
                        features['short_market_ratio'] = round(float(market_ratio), 3)
                        features['short_signal'] = round(float(np.clip((5 - market_ratio) / 5, -1, 1)), 3)
                    top_shorts = data.get('top_shorts', [])
                    if top_shorts:
                        features['short_n_high'] = len([s for s in top_shorts if s.get('ratio', 0) > 10])
            except Exception as e:
                logger.error(f'  공매도 데이터 로드 실패: {e}', exc_info=True)
        return features

    def _load_json(self, path: Path) -> Dict:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at macro_feature_integrator.py:479', exc_info=True)
        return {}

    def _save_to_signal_cache(self, features: Dict):
        """signal_cache.json에 macro_features 적재."""
        try:
            sc_file = _RESULTS / 'signal_cache.json'
            existing = {}
            if sc_file.exists():
                try:
                    existing = json.loads(sc_file.read_text())
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.warning('[SILENT_BYPASS] Suppressed exception at macro_feature_integrator.py:493', exc_info=True)
            existing['macro_features'] = features
            existing['macro_features_ts'] = datetime.now().isoformat()
            sc_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f'  signal_cache 적재 실패: {e}', exc_info=True)

def run_macro_integration() -> Dict:
    """모듈 레벨 실행 함수."""
    integrator = MacroFeatureIntegrator()
    return integrator.integrate_all()
'\nProject Meridian — Macro Feature Integrator\n'
"""
[Module 2] Realtime Collector
==============================
분석 시점에 실시간 수집하는 빠르게 변하는 데이터.
Module 2 분석기(sector_health_v3)가 import해서 사용.

리팩토링(2026-05-27): 1035줄→~310줄
    - 상수/매핑 → realtime_constants.py
    - 뉴스 감성 분석 → realtime_news_sentiment.py (NewsSentimentMixin)
    - KRX 수급/밸류에이션/스코어 → 이 파일에 유지

수집 대상:
  1. 뉴스 감성 (NewsAPI — 분석 시점 기준 최근 7일)
  2. KRX 투자자 수급 (외국인/기관 순매수 — 90일 + 최근 5일)
  3. 밸류에이션 스냅샷 (Forward PE, PEG, EV/EBITDA 등)
"""
import json, logging, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
import numpy as np
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / 'data' / 'raw'
from .realtime_constants import _load_api_key, NEWS_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, KR_SECTOR_STOCKS, US_SECTOR_STOCKS, NEWS_KEYWORDS_EN, NEWS_KEYWORDS_KR, KR_POSITIVE_WORDS, KR_NEGATIVE_WORDS, KR_POSITIVE_PHRASES, KR_NEGATIVE_PHRASES, KR_RSS_FEEDS, NEWS_KEYWORDS
from .realtime_news_sentiment import NewsSentimentMixin

class RealtimeCollector(NewsSentimentMixin):
    """Module 2 실시간 수집기 — 분석 시점에 호출

    뉴스 감성 분석 메서드는 NewsSentimentMixin에서 상속.
    이 클래스에는 KRX 수급, 밸류에이션, 스코어 산출만 유지.
    """

    def collect_all(self, sectors: Optional[list]=None) -> Dict:
        """모든 실시간 데이터 수집"""
        sectors = sectors or list(KR_SECTOR_STOCKS.keys())
        logger.info('=' * 60)
        logger.info('📌 [Module 2] Realtime Data Collection')
        logger.info('=' * 60)
        news = self.collect_news_dual(sectors)
        supply_demand = self.collect_kr_supply_demand(sectors)
        valuation = self.collect_valuation_snapshot(sectors)
        val_scores = self.compute_valuation_scores(valuation, sectors)
        return {'news': news, 'supply_demand': supply_demand, 'valuation': valuation, 'valuation_scores': val_scores, 'collected_at': datetime.now().isoformat()}

    def collect_kr_supply_demand(self, sectors: list) -> Dict:
        """KRX 외국인/기관 순매수"""
        from src.data_collection.pykrx_compat import stock as pykrx_stock
        logger.info('\n📌 KRX 투자자 수급 수집 (90일)')
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=90)).strftime('%Y%m%d')
        results = {}
        for sector in sectors:
            stocks = KR_SECTOR_STOCKS.get(sector, {})
            if not stocks:
                results[sector] = {'score': 50, 'foreign_net': 0, 'inst_net': 0, 'signal': 'N/A'}
                continue
            total_foreign = 0
            total_inst = 0
            stock_details = []
            for code, name in stocks.items():
                try:
                    df = pykrx_stock.get_market_trading_value_by_date(start_date, end_date, code)
                    if df.empty:
                        continue
                    foreign = float(df['외국인합계'].sum()) if '외국인합계' in df.columns else 0
                    inst = float(df['기관합계'].sum()) if '기관합계' in df.columns else 0
                    recent_f = float(df['외국인합계'].tail(5).sum()) if '외국인합계' in df.columns else 0
                    recent_i = float(df['기관합계'].tail(5).sum()) if '기관합계' in df.columns else 0
                    total_foreign += foreign
                    total_inst += inst
                    stock_details.append({'code': code, 'name': name, 'foreign_90d': foreign, 'inst_90d': inst, 'foreign_5d': recent_f, 'inst_5d': recent_i})
                    time.sleep(0.3)
                except Exception as e:
                    logger.warning(f'  ⚠️ {code}: {e}', exc_info=True)
                    time.sleep(0.5)
            results[sector] = {'foreign_net': total_foreign, 'inst_net': total_inst, 'signal': '순매수' if total_foreign + total_inst > 0 else '순매도', 'stocks': stock_details}
            logger.info(f'  {sector:18s} 외인:{total_foreign / 100000000.0:+,.0f}억 기관:{total_inst / 100000000.0:+,.0f}억')
        combined_values = {}
        for sector, data in results.items():
            f = data.get('foreign_net', 0)
            i = data.get('inst_net', 0)
            combined_values[sector] = f + i
        all_sectors = list(combined_values.keys())
        all_values = [combined_values[s] for s in all_sectors]
        if len(all_values) > 1:
            for sector in all_sectors:
                cv = combined_values[sector]
                below = sum((1 for v in all_values if v < cv))
                equal = sum((1 for v in all_values if v == cv))
                pct = (below + 0.5 * equal) / len(all_values) * 100
                score = max(5, min(95, pct))
                results[sector]['score'] = round(score, 1)
                results[sector]['percentile_rank'] = round(pct, 1)
        else:
            for sector in all_sectors:
                results[sector]['score'] = 50.0
                results[sector]['percentile_rank'] = 50.0
        logger.info('\n  📊 수급 Percentile Rank:')
        ranked = sorted(results.items(), key=lambda x: x[1].get('score', 50), reverse=True)
        for sector, data in ranked:
            if data.get('signal') != 'N/A':
                logger.info(f'    {sector:18s} {data['signal']} Score:{data['score']:5.1f} (Pct:{data.get('percentile_rank', 50):.0f}%)')
        out_dir = DATA_DIR / 'sector_supply_demand'
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / 'all_sectors_supply_demand.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        return results

    def collect_valuation_snapshot(self, sectors: list) -> Dict:
        """미국 종목 멀티팩터 밸류에이션 + KR PER 현재값"""
        import yfinance as yf
        from src.data_collection.pykrx_compat import stock as pykrx_stock
        logger.info('\n📌 밸류에이션 스냅샷 수집')
        results = {}
        for sector in sectors:
            tickers = US_SECTOR_STOCKS.get(sector, [])
            if not tickers:
                continue
            stocks = []
            for ticker in tickers:
                try:
                    t = yf.Ticker(ticker)
                    info = t.info
                    entry = {'ticker': ticker, 'forward_pe': info.get('forwardPE') or 0, 'peg_ratio': info.get('pegRatio') or 0, 'price_to_book': info.get('priceToBook') or 0, 'ev_to_ebitda': info.get('enterpriseToEbitda') or 0, 'ev_to_revenue': info.get('enterpriseToRevenue') or 0, 'dividend_yield': info.get('dividendYield') or 0, 'revenue_growth': info.get('revenueGrowth') or 0, 'profit_margins': info.get('profitMargins') or 0, 'roe': info.get('returnOnEquity') or 0, 'market_cap': info.get('marketCap') or 0, 'free_cashflow': info.get('freeCashflow') or 0}
                    if entry['market_cap'] and entry['free_cashflow']:
                        entry['fcf_yield'] = entry['free_cashflow'] / entry['market_cap']
                    else:
                        entry['fcf_yield'] = 0
                    stocks.append(entry)
                    time.sleep(0.2)
                except Exception as e:
                    logger.warning(f'  ⚠️ {ticker}: {e}', exc_info=True)
            if stocks:
                results[sector] = {'us_stocks': stocks}
                fpes = [s['forward_pe'] for s in stocks if s['forward_pe'] and s['forward_pe'] > 0]
                logger.info(f'  {sector:18s} US {len(stocks)}종목 FwdPE={np.median(fpes):.1f}' if fpes else f'  {sector:18s} US {len(stocks)}종목')
        date = None
        for offset in range(1, 8):
            try_date = (datetime.now() - timedelta(days=offset)).strftime('%Y%m%d')
            try:
                test = pykrx_stock.get_market_fundamental(try_date)
                if not test.empty and len(test) > 100:
                    date = try_date
                    break
            except Exception as e:
                logger.error(f'Suppressed: {e}', exc_info=True)
                continue
        if date:
            for sector in sectors:
                kr_stocks = KR_SECTOR_STOCKS.get(sector, {})
                if not kr_stocks:
                    continue
                kr_data = []
                for code, name in kr_stocks.items():
                    try:
                        fund = pykrx_stock.get_market_fundamental(date, date, code)
                        cap_df = pykrx_stock.get_market_cap(date, date, code)
                        if fund.empty:
                            continue
                        row = fund.iloc[-1]
                        mcap = float(cap_df.iloc[-1]['시가총액']) if not cap_df.empty else 0
                        kr_data.append({'code': code, 'name': name, 'PER': float(row.get('PER', 0)), 'PBR': float(row.get('PBR', 0)), 'DIV': float(row.get('DIV', 0)), 'market_cap': mcap})
                        time.sleep(0.2)
                    except Exception as e:
                        logger.error(f'Suppressed: {e}', exc_info=True)
                if kr_data:
                    results.setdefault(sector, {})['kr_stocks'] = kr_data
        out_dir = DATA_DIR / 'sector_valuation'
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / 'realtime_valuation_snapshot.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        kr_valuation = {}
        for sector, data in results.items():
            kr_stocks = data.get('kr_stocks', [])
            if not kr_stocks:
                continue
            pers = [s['PER'] for s in kr_stocks if s.get('PER', 0) > 0]
            pbrs = [s['PBR'] for s in kr_stocks if s.get('PBR', 0) > 0]
            caps = [s.get('market_cap', 0) for s in kr_stocks]
            kr_valuation[sector] = {'sector': sector, 'avg_PER': round(np.mean(pers), 2) if pers else 0, 'avg_PBR': round(np.mean(pbrs), 3) if pbrs else 0, 'total_market_cap': sum(caps), 'stock_count': len(kr_stocks), 'stocks': kr_stocks, 'date': date or datetime.now().strftime('%Y%m%d')}
        if kr_valuation:
            with open(out_dir / 'kr_sector_valuation.json', 'w', encoding='utf-8') as f:
                json.dump(kr_valuation, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f'  ✅ kr_sector_valuation.json 저장: {len(kr_valuation)}개 섹터')
        return results
    SECTOR_CATEGORIES = {'Semiconductor': 'tech', 'AI': 'tech', 'QuantumComputing': 'tech', 'Robotics': 'tech', 'Software': 'tech', 'Battery': 'tech', 'Finance': 'finance', 'Healthcare': 'traditional', 'Energy': 'commodity', 'Materials': 'commodity', 'Consumer': 'traditional', 'Utilities': 'dividend', 'Telecom': 'traditional', 'Defense': 'fcf', 'RealEstate': 'reit', 'Shipbuilding': 'cyclical_pbr', 'Automotive': 'cyclical'}
    CATEGORY_WEIGHTS = {'tech': {'fwd_pe': 0.15, 'peg': 0.25, 'ev_rev': 0.2, 'growth': 0.2, 'ev_eb': 0.1, 'margin': 0.1}, 'finance': {'fwd_pe': 0.2, 'pbr_roe': 0.35, 'div': 0.25, 'margin': 0.2}, 'commodity': {'fwd_pe': 0.25, 'ev_eb': 0.3, 'div': 0.2, 'growth': 0.15, 'margin': 0.1}, 'traditional': {'fwd_pe': 0.3, 'ev_eb': 0.25, 'div': 0.2, 'margin': 0.15, 'growth': 0.1}, 'dividend': {'div': 0.4, 'fwd_pe': 0.25, 'margin': 0.2, 'pbr_roe': 0.15}, 'fcf': {'fcf': 0.3, 'ev_eb': 0.25, 'fwd_pe': 0.2, 'margin': 0.15, 'growth': 0.1}, 'reit': {'div': 0.4, 'pbr_roe': 0.3, 'fwd_pe': 0.15, 'margin': 0.15}, 'cyclical': {'fwd_pe': 0.3, 'ev_eb': 0.25, 'growth': 0.15, 'pbr_roe': 0.15, 'margin': 0.15}, 'cyclical_pbr': {'pbr_roe': 0.4, 'ev_eb': 0.25, 'growth': 0.2, 'margin': 0.15}}

    def compute_valuation_scores(self, val_data: Dict, sectors: list) -> Dict:
        """수집된 밸류에이션 데이터로 섹터별 차등 스코어 계산"""
        logger.info('\n📌 밸류에이션 스코어 산출')
        kr_band_path = DATA_DIR / 'sector_valuation' / 'kr_relative_per_band.json'
        kr_per_band = {}
        if kr_band_path.exists():
            with open(kr_band_path, encoding='utf-8') as f:
                try:
                    kr_per_band = json.load(f)
                except (Exception,):
                    kr_per_band = {}
        results = {}
        for sector in sectors:
            cat = self.SECTOR_CATEGORIES.get(sector, 'traditional')
            weights = self.CATEGORY_WEIGHTS.get(cat, self.CATEGORY_WEIGHTS['traditional'])
            stocks = val_data.get(sector, {}).get('us_stocks', [])
            scores = {}
            fpes = [s['forward_pe'] for s in stocks if s.get('forward_pe') and s['forward_pe'] > 0]
            if fpes:
                m = np.median(fpes)
                if cat == 'tech':
                    scores['fwd_pe'] = max(0, min(100, 120 - m * 1.7))
                elif cat == 'finance':
                    scores['fwd_pe'] = max(0, min(100, 100 - m * 3))
                else:
                    scores['fwd_pe'] = max(0, min(100, 110 - m * 3))
            else:
                scores['fwd_pe'] = 50
            pegs = [s['peg_ratio'] for s in stocks if s.get('peg_ratio') and 0 < s['peg_ratio'] < 10]
            scores['peg'] = max(0, min(100, 100 - (np.median(pegs) - 0.5) * 40)) if pegs else 50
            evrs = [s['ev_to_revenue'] for s in stocks if s.get('ev_to_revenue') and s['ev_to_revenue'] > 0]
            if evrs:
                m = np.median(evrs)
                scores['ev_rev'] = max(0, min(100, 100 - (m - 3) * 8)) if cat == 'tech' else max(0, min(100, 100 - m * 15))
            else:
                scores['ev_rev'] = 50
            eveb = [s['ev_to_ebitda'] for s in stocks if s.get('ev_to_ebitda') and 0 < s['ev_to_ebitda'] < 200]
            if eveb:
                m = np.median(eveb)
                scores['ev_eb'] = max(0, min(100, 100 - (m - 10) * 3)) if cat == 'tech' else max(0, min(100, 100 - (m - 5) * 5))
            else:
                scores['ev_eb'] = 50
            rgs = [s['revenue_growth'] for s in stocks if s.get('revenue_growth') is not None]
            scores['growth'] = max(0, min(100, 50 + np.median(rgs) * 200)) if rgs else 50
            margins = [s['profit_margins'] for s in stocks if s.get('profit_margins') is not None]
            scores['margin'] = max(0, min(100, 30 + np.median(margins) * 200)) if margins else 50
            pbrs = [s['price_to_book'] for s in stocks if s.get('price_to_book') and 0 < s['price_to_book'] < 100]
            roes = [s['roe'] for s in stocks if s.get('roe') and s['roe'] > 0]
            if pbrs and roes:
                scores['pbr_roe'] = max(0, min(100, 50 + (np.median(roes) * 10 - np.median(pbrs)) * 20))
            elif pbrs:
                scores['pbr_roe'] = max(0, min(100, 80 - np.median(pbrs) * 15))
            else:
                scores['pbr_roe'] = 50
            divs = [s['dividend_yield'] for s in stocks if s.get('dividend_yield') and s['dividend_yield'] > 0]
            scores['div'] = max(0, min(100, np.median(divs) * 100 * 15)) if divs else 30
            fcfs = [s['fcf_yield'] for s in stocks if s.get('fcf_yield') and s['fcf_yield'] > 0]
            scores['fcf'] = max(0, min(100, np.median(fcfs) * 100 * 10)) if fcfs else 50
            cat_score = 0
            total_w = 0
            for metric, w in weights.items():
                if metric in scores:
                    cat_score += scores[metric] * w
                    total_w += w
            cat_score = cat_score / total_w if total_w > 0 else 50
            kr_band_data = kr_per_band.get(sector, [])
            if kr_band_data:
                pcts = [s['percentile'] for s in kr_band_data if s.get('percentile') is not None]
                if pcts:
                    relative_score = max(0, min(100, 100 - np.mean(pcts)))
                    final_score = cat_score * 0.8 + relative_score * 0.2
                else:
                    final_score = cat_score
                    relative_score = 50
            else:
                final_score = cat_score
                relative_score = 50
            results[sector] = {'category': cat, 'cat_score': round(cat_score, 1), 'relative_score': round(relative_score, 1), 'final_score': round(final_score, 1), 'metrics': {k: round(v, 1) for k, v in scores.items()}}
            logger.info(f'  {sector:18s} [{cat:12s}] Cat:{cat_score:.0f} Rel:{relative_score:.0f} → {final_score:.1f}')
        out_dir = DATA_DIR / 'sector_valuation'
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / 'advanced_valuation_scores.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        return results
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    collector = RealtimeCollector()
    data = collector.collect_all()
    logger.info(f'\n✅ [Module 2] Realtime Collection 완료')
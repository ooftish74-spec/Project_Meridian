"""
[Module 1] Sector Batch Collector
==================================
느리게 변하는 섹터 관련 데이터를 일 1회 배치 수집.
Module 1 데이터 파이프라인에 포함되어 실행됨.

수집 대상:
  1. 글로벌 공급망 가격 (DRAM proxy, 리튬, 구리, 유가, BDI 등)
  2. 한국 Relative PER Band (5년 PER 히스토리)

저장 위치:
  data/raw/supply_chain/
  data/raw/sector_valuation/kr_relative_per_band.json
"""
import json, logging, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / 'data' / 'raw'
SUPPLY_CHAIN_ASSETS = {'MU': {'name': 'Micron (DRAM 프록시)', 'sectors': ['Semiconductor'], 'correlation': 'positive'}, 'SOXX': {'name': 'PHLX Semiconductor Index', 'sectors': ['Semiconductor'], 'correlation': 'positive'}, 'LIT': {'name': 'Lithium ETF', 'sectors': ['Battery', 'Automotive'], 'correlation': 'positive'}, 'HG=F': {'name': 'Copper Futures', 'sectors': ['Materials', 'Battery'], 'correlation': 'positive'}, 'CL=F': {'name': 'WTI Crude Oil', 'sectors': ['Energy'], 'correlation': 'positive'}, 'NG=F': {'name': 'Natural Gas', 'sectors': ['Energy', 'Utilities'], 'correlation': 'positive'}, 'SLX': {'name': 'Steel ETF', 'sectors': ['Materials', 'Shipbuilding'], 'correlation': 'positive'}, 'BDRY': {'name': 'Baltic Dry Index ETN', 'sectors': ['Shipbuilding'], 'correlation': 'positive'}, 'URA': {'name': 'Uranium ETF', 'sectors': ['Utilities', 'Energy'], 'correlation': 'positive'}, 'GC=F': {'name': 'Gold', 'sectors': ['Finance'], 'correlation': 'negative'}, 'JPY=X': {'name': 'USD/JPY (엔캐리 proxy)', 'sectors': ['Semiconductor', 'AI', 'Battery'], 'correlation': 'positive'}, 'KRW=X': {'name': 'USD/KRW (환율)', 'sectors': ['Semiconductor', 'Automotive', 'Shipbuilding'], 'correlation': 'negative'}}
KR_PER_BAND_STOCKS = {'Semiconductor': {'005930': '삼성전자', '000660': 'SK하이닉스'}, 'Software': {'035420': '네이버', '035720': '카카오'}, 'Finance': {'105560': 'KB금융', '055550': '신한지주', '086790': '하나금융지주'}, 'Healthcare': {'207940': '삼성바이오로직스', '068270': '셀트리온'}, 'Energy': {'010950': 'S-Oil'}, 'Materials': {'005490': 'POSCO홀딩스', '051910': 'LG화학'}, 'Consumer': {'051900': 'LG생활건강', '097950': 'CJ제일제당'}, 'Utilities': {'015760': '한국전력'}, 'Telecom': {'017670': 'SK텔레콤', '030200': 'KT'}, 'Defense': {'047810': '한국항공우주', '012450': '한화에어로스페이스'}, 'Shipbuilding': {'009540': 'HD한국조선해양', '010140': '삼성중공업', '042660': '한화오션'}, 'Automotive': {'005380': '현대자동차', '000270': '기아'}, 'Battery': {'006400': '삼성SDI', '051910': 'LG화학'}}
US_KR_ETF_PAIRS = {'Semiconductor': {'us': 'SOXX', 'kr': '091160'}, 'AI': {'us': 'BOTZ', 'kr': None}, 'Software': {'us': 'IGV', 'kr': None}, 'Finance': {'us': 'XLF', 'kr': '091170'}, 'Healthcare': {'us': 'XLV', 'kr': '266420'}, 'Energy': {'us': 'XLE', 'kr': '117460'}, 'Materials': {'us': 'XLB', 'kr': '117460'}, 'Consumer': {'us': 'XLP', 'kr': '228810'}, 'Utilities': {'us': 'XLU', 'kr': None}, 'Telecom': {'us': 'XLC', 'kr': None}, 'Defense': {'us': 'ITA', 'kr': '364690'}, 'RealEstate': {'us': 'VNQ', 'kr': None}, 'Shipbuilding': {'us': None, 'kr': '139230'}, 'Automotive': {'us': None, 'kr': '091180'}, 'Battery': {'us': 'LIT', 'kr': '305720'}, 'Robotics': {'us': 'ROBO', 'kr': None}, 'QuantumComputing': {'us': 'QTUM', 'kr': None}}

class SectorBatchCollector:
    """Module 1 배치 수집기 — 일 1회 실행"""

    def __init__(self):
        self.supply_chain_data = {}
        self.per_band_data = {}
        self.us_kr_beta = {}

    def collect_all(self) -> Dict:
        """전체 배치 수집"""
        logger.info('=' * 60)
        logger.info('📌 [Module 1] Sector Batch Collection 시작')
        logger.info('=' * 60)
        self.collect_supply_chain()
        self.collect_kr_per_band()
        self.collect_us_kr_beta()
        return {'supply_chain': self.supply_chain_data, 'per_band': self.per_band_data, 'us_kr_beta': self.us_kr_beta, 'collected_at': datetime.now().isoformat()}

    def collect_supply_chain(self):
        """글로벌 공급망 자산 가격 수집 (1년 히스토리)"""
        import yfinance as yf
        logger.info('\n📌 1. 글로벌 공급망 가격 수집')
        out_dir = DATA_DIR / 'supply_chain'
        out_dir.mkdir(parents=True, exist_ok=True)
        for ticker, info in SUPPLY_CHAIN_ASSETS.items():
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period='1y')
                if hist.empty:
                    continue
                close = hist['Close']
                last = float(close.iloc[-1])
                chg_1m = float((close.iloc[-1] / close.iloc[-22] - 1) * 100) if len(close) > 22 else 0
                chg_3m = float((close.iloc[-1] / close.iloc[-66] - 1) * 100) if len(close) > 66 else 0
                chg_1y = float((close.iloc[-1] / close.iloc[0] - 1) * 100)
                sma20 = float(close.tail(20).mean())
                trend = 'up' if last > sma20 else 'down'
                self.supply_chain_data[ticker] = {'name': info['name'], 'last_price': round(last, 2), 'chg_1m': round(chg_1m, 2), 'chg_3m': round(chg_3m, 2), 'chg_1y': round(chg_1y, 2), 'trend': trend, 'sma20': round(sma20, 2), 'sectors': info['sectors'], 'correlation': info['correlation']}
                safe_name = ticker.replace('=', '_').replace('/', '_')
                hist[['Close', 'Volume']].to_csv(out_dir / f'{safe_name}.csv')
                logger.info(f'  ✅ {ticker:8s} {info['name']:25s} ${last:>10.2f} 3M:{chg_3m:+.1f}% [{trend}]')
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f'  ⚠️ {ticker}: {e}', exc_info=True)
        with open(out_dir / 'supply_chain_summary.json', 'w', encoding='utf-8') as f:
            json.dump(self.supply_chain_data, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f'  → 저장: {out_dir / 'supply_chain_summary.json'}')

    def collect_kr_per_band(self):
        """한국 Relative PER Band — 5년 PER 히스토리"""
        from src.data_collection.pykrx_compat import stock as pykrx_stock
        logger.info('\n📌 2. 한국 Relative PER Band (5년)')
        out_dir = DATA_DIR / 'sector_valuation'
        out_dir.mkdir(parents=True, exist_ok=True)
        end_date = None
        for offset in range(1, 8):
            try_date = (datetime.now() - timedelta(days=offset)).strftime('%Y%m%d')
            try:
                test = pykrx_stock.get_market_fundamental(try_date)
                if not test.empty and len(test) > 100:
                    end_date = try_date
                    break
            except Exception as e:
                logger.error(f'Suppressed: {e}', exc_info=True)
                continue
        if not end_date:
            logger.info('  거래일 찾지 못함 (비거래일)')
            return
        start_date = (datetime.now() - timedelta(days=365 * 5)).strftime('%Y%m%d')
        logger.info(f'  기간: {start_date} ~ {end_date}')
        for sector, stocks in KR_PER_BAND_STOCKS.items():
            for code, name in stocks.items():
                try:
                    df = pykrx_stock.get_market_fundamental(start_date, end_date, code)
                    if df.empty or len(df) < 20:
                        continue
                    pers = df['PER'].replace(0, np.nan).dropna()
                    if len(pers) < 20:
                        continue
                    current = float(pers.iloc[-1])
                    percentile = float((pers < current).mean() * 100)
                    self.per_band_data.setdefault(sector, []).append({'code': code, 'name': name, 'current_per': round(current, 1), 'median_5y': round(float(pers.median()), 1), 'pct_25': round(float(pers.quantile(0.25)), 1), 'pct_75': round(float(pers.quantile(0.75)), 1), 'percentile': round(percentile, 1), 'data_points': len(pers)})
                    logger.info(f'  {code} {name}: PER={current:.1f} Percentile={percentile:.0f}%')
                    time.sleep(0.3)
                except Exception as e:
                    logger.warning(f'  ⚠️ {code}: {e}', exc_info=True)
        with open(out_dir / 'kr_relative_per_band.json', 'w', encoding='utf-8') as f:
            json.dump(self.per_band_data, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f'  → 저장: {out_dir / 'kr_relative_per_band.json'}')

    def collect_us_kr_beta(self):
        """US-KR 섹터 베타 — Lagged Correlation (시차 보정)
        
        핵심: 미국장 마감(한국 새벽 5시) → 한국장 개장(9시)
        따라서 US Day(T) → KR Day(T+1) 시차 상관이 진짜 영향력.
        """
        import yfinance as yf
        from src.data_collection.pykrx_compat import stock as pykrx_stock
        logger.info('\n📌 3. US-KR 섹터 베타 (Lagged Correlation)')
        out_dir = DATA_DIR / 'sector_beta'
        out_dir.mkdir(parents=True, exist_ok=True)
        end_date_str = datetime.now().strftime('%Y%m%d')
        start_date_str = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
        for sector, pair in US_KR_ETF_PAIRS.items():
            us_ticker = pair.get('us')
            kr_code = pair.get('kr')
            if not us_ticker or not kr_code:
                self.us_kr_beta[sector] = {'beta': 0.3, 'correlation': 0.3, 'lagged_corr': 0.3, 'same_day_corr': 0.1, 'us_5d_return': 0, 'data_available': False, 'note': 'US or KR ETF missing'}
                continue
            try:
                us_hist = yf.Ticker(us_ticker).history(period='1y')
                if us_hist.empty or len(us_hist) < 60:
                    raise ValueError(f'US {us_ticker} data insufficient')
                us_ret = us_hist['Close'].pct_change().dropna()
                kr_hist = pykrx_stock.get_market_ohlcv(start_date_str, end_date_str, kr_code)
                if kr_hist.empty or len(kr_hist) < 60:
                    raise ValueError(f'KR {kr_code} data insufficient')
                _close_col = None
                for _cc in ['close', '종가', 'Close']:
                    if _cc in kr_hist.columns:
                        _close_col = _cc
                        break
                if _close_col is None:
                    raise ValueError(f'KR {kr_code} no close column (cols={list(kr_hist.columns)})')
                kr_ret = kr_hist[_close_col].pct_change().dropna()
                us_df = pd.DataFrame({'us': us_ret})
                kr_df = pd.DataFrame({'kr': kr_ret})
                us_df.index = us_df.index.tz_localize(None) if us_df.index.tz else us_df.index
                us_shifted = us_df.copy()
                us_shifted.index = us_shifted.index + pd.Timedelta(days=1)
                lagged_merged = us_shifted.join(kr_df, how='inner').dropna()
                same_merged = us_df.join(kr_df, how='inner').dropna()
                if len(lagged_merged) < 20:
                    logger.info(f'  ⚠️ {sector}: Lagged common dates too few: {len(lagged_merged)}')
                    self.us_kr_beta[sector] = {'beta': 0.3, 'correlation': 0.3, 'lagged_corr': 0.3, 'same_day_corr': 0.1, 'us_5d_return': 0, 'data_available': False, 'note': f'Lagged pts: {len(lagged_merged)}'}
                    continue
                lagged_corr = float(lagged_merged['us'].corr(lagged_merged['kr']))
                lagged_beta = float(lagged_merged['kr'].cov(lagged_merged['us']) / lagged_merged['us'].var())
                same_corr = float(same_merged['us'].corr(same_merged['kr'])) if len(same_merged) > 20 else 0
                effective_corr = max(abs(lagged_corr), abs(same_corr))
                if abs(lagged_corr) > abs(same_corr):
                    effective_beta = lagged_beta
                elif len(same_merged) > 20:
                    effective_beta = float(same_merged['kr'].cov(same_merged['us']) / same_merged['us'].var())
                else:
                    effective_beta = lagged_beta
                us_5d = float(us_ret.tail(5).sum() * 100)
                self.us_kr_beta[sector] = {'beta': round(max(0, min(2, effective_beta)), 3), 'correlation': round(max(-1, min(1, effective_corr)), 3), 'lagged_corr': round(lagged_corr, 3), 'same_day_corr': round(same_corr, 3), 'lagged_beta': round(lagged_beta, 3), 'us_5d_return': round(us_5d, 2), 'us_ticker': us_ticker, 'kr_code': kr_code, 'lagged_pts': len(lagged_merged), 'same_pts': len(same_merged), 'data_available': True}
                logger.info(f'  {sector:18s} LagBeta={lagged_beta:.3f} LagCorr={lagged_corr:.3f} SameCorr={same_corr:.3f} Eff={effective_corr:.3f}')
                time.sleep(0.3)
            except Exception as e:
                self.us_kr_beta[sector] = {'beta': 0.3, 'correlation': 0.3, 'lagged_corr': 0.3, 'same_day_corr': 0.1, 'us_5d_return': 0, 'data_available': False, 'note': str(e)}
                logger.info(f'  ⚠️ {sector}: {e}')
        with open(out_dir / 'us_kr_sector_beta.json', 'w', encoding='utf-8') as f:
            json.dump(self.us_kr_beta, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f'  → 저장: {out_dir / 'us_kr_sector_beta.json'}')
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    collector = SectorBatchCollector()
    collector.collect_all()
    logger.info('\n✅ [Module 1] Sector Batch Collection 완료')
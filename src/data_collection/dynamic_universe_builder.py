import requests
from bs4 import BeautifulSoup
import json
from src.utils.file_ops import atomic_write_json

import logging
from pathlib import Path
from typing import List, Dict, Set

logger = logging.getLogger('universe_builder')
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(ch)
    logger.setLevel(logging.INFO)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_OUT_DIR = _PROJECT_ROOT / 'data' / 'universe'
_OUT_FILE = _OUT_DIR / 'dynamic_universe.json'

def fetch_top_stocks(sosok: int, limit: int) -> List[Dict]:
    """KRX API를 통해 KOSPI(0) 또는 KOSDAQ(1) 시총 상위 종목을 추출한다.
    (BeautifulSoup 크롤링 제거 및 API 방식으로 개선)
    """
    from datetime import datetime
    import pandas as pd
    from src.data_collection.krx_api_client import KRXApiClient
    
    krx = KRXApiClient()
    
    # sosok: 0 = KOSPI (STK), 1 = KOSDAQ (KSQ)
    market = 'STK' if sosok == 0 else 'KSQ'
    date_str = datetime.now().strftime('%Y%m%d')
    
    df = krx.get_market_cap(date=date_str, market=market)
    
    if df is None or df.empty:
        # 오늘 데이터가 안나왔을 경우 (주말/공휴일), 가장 최근 영업일을 알기 어려우므로 pykrx 폴백 활용가능하나
        # 우선 KRX API가 지원하는 최신 영업일자를 직접 찾아서 넣어야 하지만 간단히 가장 최근 영업일로 -1일씩 뒤져봄
        for i in range(1, 10):
            from datetime import timedelta
            test_date = (datetime.now() - timedelta(days=i)).strftime('%Y%m%d')
            df = krx.get_market_cap(date=test_date, market=market)
            if df is not None and not df.empty:
                break
                
    if df is None or df.empty:
        logger.error(f'KRX API {market} 시가총액 데이터 수집 실패.')
        return []
        
    # 'market_cap' 문자열 쉼표 제거 후 float 변환 필요할 수 있음
    if 'market_cap' in df.columns:
        if df['market_cap'].dtype == object:
            df['market_cap'] = df['market_cap'].astype(str).str.replace(',', '').astype(float)
            
    df = df.sort_values(by='market_cap', ascending=False).head(limit)
    
    results = []
    # df의 인덱스가 ticker로 설정되어 있으므로 이를 추출
    for ticker, row in df.iterrows():
        # ETF나 리츠 제외 로직(추가 가능하지만 기존 코드 호환성 유지)
        if len(ticker) == 6:  # 유효한 티커인지 확인
            results.append({"ticker": str(ticker), "name": row.get('name', '')})
            
    return results

def fetch_etf_list() -> List[Dict]:
    """네이버 API를 통해 전체 ETF 리스트와 거래대금(amonut) 데이터를 추출한다."""
    url = "https://finance.naver.com/api/sise/etfItemList.nhn"
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()
    return data['result']['etfItemList']

def build_dynamic_universe():
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    
    logger.info("1. 개별주(Equities) 동적 스캔 시작...")
    # 코스피 상위 50개, 코스닥 상위 20개
    kospi_50 = fetch_top_stocks(sosok=0, limit=50)
    kosdaq_20 = fetch_top_stocks(sosok=1, limit=20)
    
    kr_stocks = kospi_50 + kosdaq_20
    logger.info(f"   => 주식 추출 완료: KOSPI {len(kospi_50)}개, KOSDAQ {len(kosdaq_20)}개 (총 {len(kr_stocks)}개)")
    
    logger.info("2. ETF 동적 스캔 시작...")
    etf_raw = fetch_etf_list()
    # 거래대금(amonut) 기준 내림차순 정렬
    etf_sorted = sorted(etf_raw, key=lambda x: x.get('amonut', 0), reverse=True)
    
    etf_set: Set[str] = set()
    kr_etfs: List[Dict] = []
    
    def add_etf(item: Dict, reason: str):
        ticker = item['itemcode']
        if ticker not in etf_set:
            etf_set.add(ticker)
            kr_etfs.append({"ticker": ticker, "name": item['itemname']})
            logger.info(f"   + [ETF 추가 - {reason}] {item['itemname']} ({ticker})")
            
    # 2-1. 거래대금 상위 30개 (레버리지/인버스 포함)
    logger.info("   [2-1] 거래대금 상위 30위 추출")
    for item in etf_sorted[:30]:
        add_etf(item, "거래대금 상위 30")
        
    # 2-2. 섹터별 대표 ETF 상위 2개
    sector_keywords = ['반도체', '2차전지', '바이오', '자동차', '금융', '헬스케어', '미디어', '게임']
    logger.info("   [2-2] 섹터별 대표 엣지 ETF 추출")
    for keyword in sector_keywords:
        matched = 0
        for item in etf_sorted:
            if keyword in item['itemname'] and '인버스' not in item['itemname'] and '레버리지' not in item['itemname']:
                add_etf(item, f"섹터 엣지({keyword})")
                matched += 1
                if matched >= 2:
                    break
                    
    # 2-3. 삼성전자/하이닉스 개별종목 레버리지/인버스 강제 매칭 (KODEX, TIGER 등 우선)
    logger.info("   [2-3] 단일종목 레버리지/인버스 ETF 추출")
    ss_keywords = ['삼성전자', 'SK하이닉스', '하이닉스']
    for keyword in ss_keywords:
        for item in etf_sorted:
            name = item['itemname']
            if keyword in name and ('레버리지' in name or '인버스' in name):
                add_etf(item, "단일종목 레버/인버스")
                
    logger.info(f"   => ETF 추출 완료: 총 {len(kr_etfs)}개 (중복 제거됨)")
    
    universe_data = {
        "kr_stocks": kr_stocks,
        "kr_etfs": kr_etfs,
        "us_stocks": []
    }
    
    atomic_write_json(_OUT_FILE, universe_data, ensure_ascii=False, indent=4)
    logger.info(f"3. 동적 유니버스 생성 완료 -> {_OUT_FILE}")

if __name__ == "__main__":
    build_dynamic_universe()

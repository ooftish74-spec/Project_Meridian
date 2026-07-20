import sys
import pandas as pd
import numpy as np
from datetime import datetime
from pykrx import stock
import scipy.stats as stats

def fetch_data(ticker, start, end):
    df = stock.get_market_ohlcv_by_date(start, end, ticker)
    df = df.reset_index()
    df.rename(columns={'날짜': 'date', '시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}, inplace=True)
    return df

def analyze_volatility(ticker, name, event_date_str='20260527'):
    print(f"\n{'='*50}")
    print(f"[{name} ({ticker})] 단일종목 ETF 상장 전/후 변동성 분석")
    print(f"{'='*50}")
    
    start_date = '20260101'
    end_date = '20260623'
    df = fetch_data(ticker, start_date, end_date)
    
    if df.empty:
        print("Data not found.")
        return
        
    df['return'] = df['close'].pct_change()
    df['intraday_vol'] = (df['high'] - df['low']) / df['open'] * 100 # 일중 변동폭 (%)
    df['abs_return'] = df['return'].abs() * 100
    df = df.dropna()
    
    event_date = pd.to_datetime(event_date_str)
    
    before_df = df[df['date'] < event_date]
    after_df = df[df['date'] >= event_date]
    
    # 통계량 계산
    before_intra_avg = before_df['intraday_vol'].mean()
    after_intra_avg = after_df['intraday_vol'].mean()
    
    before_ret_std = before_df['return'].std() * 100
    after_ret_std = after_df['return'].std() * 100
    
    before_vol_avg = before_df['volume'].mean()
    after_vol_avg = after_df['volume'].mean()
    
    print(f"[일중 변동폭 (High-Low / Open)]")
    print(f"  상장 전 (1.1~5.26): 평균 {before_intra_avg:.2f}%")
    print(f"  상장 후 (5.27~6.23): 평균 {after_intra_avg:.2f}% ({(after_intra_avg/before_intra_avg - 1)*100:+.1f}%)")
    
    print(f"\n[일간 수익률 표준편차 (Close-to-Close Volatility)]")
    print(f"  상장 전: {before_ret_std:.2f}%")
    print(f"  상장 후: {after_ret_std:.2f}% ({(after_ret_std/before_ret_std - 1)*100:+.1f}%)")
    
    print(f"\n[일평균 거래량]")
    print(f"  상장 전: {before_vol_avg:,.0f}주")
    print(f"  상장 후: {after_vol_avg:,.0f}주 ({(after_vol_avg/before_vol_avg - 1)*100:+.1f}%)")
    
    # F-test for variance comparison
    f_stat = np.var(after_df['return'], ddof=1) / np.var(before_df['return'], ddof=1)
    dfn = len(after_df) - 1
    dfd = len(before_df) - 1
    p_value = 1 - stats.f.cdf(f_stat, dfn, dfd)
    
    print(f"\n[통계적 유의성 검정 (F-test for Variance)]")
    print(f"  F-Statistic: {f_stat:.2f}")
    print(f"  P-value: {p_value:.4f}")
    if p_value < 0.05:
        print("  => 결론: ETF 상장 후 변동성이 통계적으로 유의미하게 증가했습니다. (신뢰수준 95%)")
    else:
        print("  => 결론: 통계적으로 유의미한 변동성 증가는 확인되지 않았습니다.")

if __name__ == "__main__":
    analyze_volatility('005930', '삼성전자')
    analyze_volatility('000660', 'SK하이닉스')
    
    print("\n[LP 델타 헤징 효과 추정]")
    print("단일종목 2배/인버스 ETF 구조상, 개인투자자들의 매수세가 쏠릴 경우,")
    print("시장 조성자(LP)는 델타 중립을 맞추기 위해 기초자산(현물)을 기계적으로 동반 매매해야 합니다.")
    print("이는 특히 장 후반 3시 20분 부근 종가 리밸런싱 시간에 극심한 '꼬리가 몸통을 흔드는(Wag the dog)' 변동성을 야기합니다.")

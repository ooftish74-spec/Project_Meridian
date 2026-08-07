#!/usr/bin/env python3
"""
Alpha Vantage US Macro Data Backfill Script

This script backfills daily historical data for the last 2 years for US Macro 
indicators and stock indices, using Alpha Vantage as the primary source 
and FinanceDataReader as a fallback.
"""

import os
from src.infra.safe_io import atomic_write_dataframe
import sys
import logging
import pandas as pd
import requests
import FinanceDataReader as fdr
from datetime import datetime, timedelta

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils.credential_manager import CredentialManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AV_Backfill")

# Symbol mappings for Alpha Vantage and fdr
SYMBOL_MAP = {
    'us_sp500': {'av': 'SPY', 'fdr': 'US500'},
    'us_nasdaq': {'av': 'QQQ', 'fdr': 'US100'},
    'us_dji': {'av': 'DIA', 'fdr': 'US30'},
    'us_vix': {'av': None, 'fdr': 'FRED:VIXCLS'}, 
    'us_10y': {'av': None, 'fdr': 'FRED:DGS10'}, 
    'cross_usdkrw': {'av_fx': ('USD', 'KRW'), 'fdr': 'FRED:DEXKOUS'},
    'cross_gold_futures': {'av': 'GLD', 'fdr': 'GC'},
}

DATA_DIR = os.path.join("data", "kr_markets")

def get_av_daily(symbol, api_key):
    """Fetch daily data from Alpha Vantage TIME_SERIES_DAILY."""
    url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&outputsize=full&apikey={api_key}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "Error Message" in data:
        raise ValueError(f"AV Error for {symbol}: {data['Error Message']}")
    if "Information" in data:
        raise ValueError(f"AV Rate Limit for {symbol}: {data['Information']}")
        
    ts = data.get("Time Series (Daily)", {})
    if not ts:
        raise ValueError(f"No daily data returned for {symbol}")
        
    df = pd.DataFrame.from_dict(ts, orient='index')
    df.index = pd.to_datetime(df.index)
    df = df.rename(columns={
        '1. open': 'open',
        '2. high': 'high',
        '3. low': 'low',
        '4. close': 'close',
        '5. volume': 'volume'
    }).astype(float)
    df.index.name = 'date'
    return df.sort_index()

def get_av_fx_daily(from_sym, to_sym, api_key):
    """Fetch FX daily data from Alpha Vantage FX_DAILY."""
    url = f"https://www.alphavantage.co/query?function=FX_DAILY&from_symbol={from_sym}&to_symbol={to_sym}&outputsize=full&apikey={api_key}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "Error Message" in data:
        raise ValueError(f"AV Error for FX {from_sym}/{to_sym}: {data['Error Message']}")
    
    ts = data.get("Time Series FX (Daily)", {})
    if not ts:
        raise ValueError(f"No daily data returned for FX {from_sym}/{to_sym}")
        
    df = pd.DataFrame.from_dict(ts, orient='index')
    df.index = pd.to_datetime(df.index)
    df = df.rename(columns={
        '1. open': 'open',
        '2. high': 'high',
        '3. low': 'low',
        '4. close': 'close'
    }).astype(float)
    df['volume'] = 0.0
    df.index.name = 'date'
    return df.sort_index()

def get_fdr_daily(symbol):
    """Fetch daily data from fdr."""
    df = fdr.DataReader(symbol)
    if df.empty:
        raise ValueError(f"No data returned from fdr for {symbol}")
        
    df.index.name = 'date'
    df = df.rename(columns={
        'Open': 'open',
        'High': 'high',
        'Low': 'low',
        'Close': 'close',
        'Volume': 'volume'
    })
    
    if 'open' not in df.columns and 'close' in df.columns:
        df['open'] = df['close']
        df['high'] = df['close']
        df['low'] = df['close']
        df['volume'] = 0.0
        
    return df

def backfill():
    os.makedirs(DATA_DIR, exist_ok=True)
    api_key = CredentialManager().read_from_keychain('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.warning("ALPHA_VANTAGE_API_KEY not found in keychain. Will rely on fdr fallback.")
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=730)
    
    for ticker_name, mapping in SYMBOL_MAP.items():
        logger.info(f"🔄 Backfilling {ticker_name} ...")
        df = None
        
        # 1. Try Alpha Vantage
        if api_key:
            try:
                if 'av_fx' in mapping:
                    from_s, to_s = mapping['av_fx']
                    logger.info(f"  Attempting Alpha Vantage FX ({from_s}/{to_s})")
                    df = get_av_fx_daily(from_s, to_s, api_key)
                elif mapping['av']:
                    logger.info(f"  Attempting Alpha Vantage ({mapping['av']})")
                    df = get_av_daily(mapping['av'], api_key)
            except Exception as e:
                logger.warning(f"  [AV] Failed: {e}")
        
        # 2. Fallback to fdr
        if df is None:
            try:
                logger.info(f"  Attempting fdr fallback ({mapping['fdr']})")
                df = get_fdr_daily(mapping['fdr'])
            except Exception as e:
                logger.error(f"  [FDR] Failed: {e}")
                
        # 3. Save
        if df is not None and not df.empty:
            # Filter last 2 years
            mask = (df.index >= pd.to_datetime(start_date.date()))
            df = df.loc[mask]
            
            out_path = os.path.join(DATA_DIR, f"{ticker_name}.parquet")
            atomic_write_dataframe(df, out_path, file_format='parquet')
            logger.info(f"  ✅ Saved {len(df)} rows to {out_path}")
        else:
            logger.error(f"  ❌ Could not backfill {ticker_name}")

if __name__ == "__main__":
    backfill()

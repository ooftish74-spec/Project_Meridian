import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from tqdm import tqdm
from scripts.run_backtest import load_universe
from src.intelligence.aux_data_loader import AuxDataLoader
from src.intelligence.v4_features import extract_v4, FEATURE_NAMES

def _get_cross_asset_for_date(cross_data, date_str):
    ca = {}
    sp = cross_data.get('sp500', {}).get(date_str)
    if sp: ca['sp500'] = sp
    vix = cross_data.get('vix', {}).get(date_str)
    if vix: ca['vix'] = vix
    usd = cross_data.get('usdkrw', {}).get(date_str)
    if usd: ca['usdkrw'] = usd
    return ca

def main():
    DATA_DIR = Path('data/historical_10y')
    FEATURE_STORE = Path('data/feature_store')
    FEATURE_STORE.mkdir(parents=True, exist_ok=True)
    
    universe = load_universe()
    aux_loader = AuxDataLoader()
    
    try:
        cross_df = pd.read_parquet(DATA_DIR / 'cross_asset.parquet')
        cross_data = cross_df.to_dict('index')
    except Exception:
        cross_data = {}
        
    # Process all stocks
    for i, ticker in enumerate(universe):
        fp = DATA_DIR / f'kr_{ticker}.parquet'
        if not fp.exists():
            continue
            
        df = pd.read_parquet(fp)
        close = pd.to_numeric(df['close'], errors='coerce').dropna().values
        high = pd.to_numeric(df['high'], errors='coerce').dropna().values
        low = pd.to_numeric(df['low'], errors='coerce').dropna().values
        opn = pd.to_numeric(df['open'], errors='coerce').dropna().values
        vol = pd.to_numeric(df['volume'], errors='coerce').dropna().values
        dates = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d').values
        
        is_etf = False
        features_list = []
        date_list = []
        
        print(f"[{i+1}/{len(universe)}] Rebuilding {ticker} from {dates[0]} to {dates[-1]}")
        
        for idx in range(60, len(close)):
            dt_str = dates[idx]
            if dt_str < '2024-06-01': # Optimization: backtest starts from 2024-07-19
                continue
                
            ca = _get_cross_asset_for_date(cross_data, dt_str)
            aux_features = aux_loader.get_features(ticker, dt_str)
            feat = extract_v4(close, high, low, opn, vol, idx, is_etf,
                              cross_asset=ca, aux_data=aux_features)
            if feat:
                features_list.append(feat)
                date_list.append(df.iloc[idx]['date'])
                
        if features_list:
            feat_df = pd.DataFrame(features_list)
            feat_df.index = pd.to_datetime(date_list)
            feat_df.to_parquet(FEATURE_STORE / f'{ticker}.parquet')

if __name__ == '__main__':
    main()

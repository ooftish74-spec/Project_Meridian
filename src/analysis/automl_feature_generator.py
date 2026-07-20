import pandas as pd
import numpy as np
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)

class AutoMLFeatureGenerator:
    """
    AutoML Feature Generator
    
    Generates hundreds of derived features using vectorized Pandas operations.
    Includes moving average divergences, volatility rolling, and macro data correlations.
    """
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.macro_df = self._load_macro_data()
        
    def _load_macro_data(self) -> pd.DataFrame:
        """Load consolidated_macro.parquet containing GSCPI, Copper/Gold, High Yield, etc."""
        # [Phase 65: SSOT] Data_Hub_Agent 외부 의존성 제거 — 내부 SSOT 경로 사용
        project_root = Path(__file__).resolve().parent.parent.parent
        macro_path = project_root / 'data' / 'macro' / 'macro_data.parquet'
        
        if not macro_path.exists():
            logger.warning(f"Macro data not found at {macro_path}. Macro features will be empty.")
            return pd.DataFrame()
            
        try:
            df = pd.read_parquet(macro_path)
            # Ensure index is datetime and sorted
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            return df
        except Exception as e:
            logger.error(f"Failed to load macro data: {e}")
            return pd.DataFrame()

    def generate_features(self, ticker: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate vectorized features for a single ticker's DataFrame.
        This completely replaces the row-by-row feature extraction for training.
        """
        if len(df) < 260:
            return pd.DataFrame()

        # 1. Base Price/Volume Features
        close = df['close'].astype(np.float32)
        high = df['high'].astype(np.float32)
        low = df['low'].astype(np.float32)
        opn = df['open'].astype(np.float32)
        vol = df['volume'].astype(np.float32)

        feat_df = pd.DataFrame(index=df.index)
        
        # Fast Moving Averages
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
            fast_windows = _cfg.get('ml.fast_ma_windows', [5, 20, 60, 120, 240])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            fast_windows = [5, 20, 60, 120, 240]

        for w in fast_windows:
            ma = close.rolling(window=w, min_periods=1).mean()
            feat_df[f'ma{w}_dist'] = (close / ma - 1) * 100
            
            vol_ma = vol.rolling(window=w, min_periods=1).mean()
            feat_df[f'vol{w}_dist'] = (vol / vol_ma - 1).clip(-5, 5)

        # Returns and Volatility
        try:
            ret_windows = _cfg.get('ml.return_windows', [1, 3, 5, 10, 20, 60])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            ret_windows = [1, 3, 5, 10, 20, 60]

        for w in ret_windows:
            # [Maintenance] FutureWarning: fill_method='pad' deprecated → 명시적 fill_method=None 지정
            feat_df[f'return_{w}d'] = close.pct_change(periods=w, fill_method=None) * 100
            if w >= 5:
                # Annualized Volatility
                feat_df[f'volatility_{w}d'] = (
                    close.ffill().pct_change(fill_method=None).rolling(window=w).std()
                    * np.sqrt(252) * 100
                )

        # High/Low Range
        feat_df['high_low_range'] = (df['high'].rolling(20).max() - df['low'].rolling(20).min()) / close * 100
        feat_df['close_to_high_20d'] = (close / df['high'].rolling(20).max() - 1) * 100

        # Overnight/Intraday
        feat_df['overnight_return'] = (opn / close.shift(1) - 1) * 100
        feat_df['intraday_return'] = (close / opn - 1) * 100

        # 2. Merge Macro Data
        if not self.macro_df.empty:
            # Reindex macro to match ticker dates (forward fill up to 30 days)
            macro_aligned = self.macro_df.reindex(df.index, method='ffill', tolerance=pd.Timedelta(days=30))
            
            # Create derived macro features
            for col in macro_aligned.columns:
                feat_df[f'macro_{col}'] = macro_aligned[col].astype(np.float32)
                # [Maintenance] FutureWarning: fill_method='pad' deprecated → fill_method=None
                feat_df[f'macro_{col}_chg_20d'] = macro_aligned[col].pct_change(periods=20, fill_method=None) * 100
                
            # Cross-Feature: Macro & Price Momentum (Non-linear combination)
            if 'macro_copper_gold_ratio' in feat_df.columns and 'return_20d' in feat_df.columns:
                feat_df['cross_copper_return_20d'] = feat_df['macro_copper_gold_ratio_chg_20d'] * feat_df['return_20d']
            
            if 'macro_gscpi' in feat_df.columns and 'volatility_20d' in feat_df.columns:
                feat_df['cross_gscpi_vol'] = feat_df['macro_gscpi'] * feat_df['volatility_20d']

        # 3. Handle NaNs & Infinite
        feat_df = feat_df.replace([np.inf, -np.inf], np.nan)
        # Drop initial rows that lack full rolling window
        feat_df = feat_df.iloc[260:]
        
        return feat_df

    def process_universe_parallel(self, universe: list, n_jobs: int = -1) -> dict:
        """
        Process multiple tickers in parallel using joblib.
        Returns a dict of ticker -> DataFrame.
        """
        from joblib import Parallel, delayed
        
        def _process_single(ticker: str):
            fp = self.data_dir / f'kr_{ticker}.parquet'
            if not fp.exists():
                return ticker, None
            try:
                df = pd.read_parquet(fp)
                if 'date' in df.columns:
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.set_index('date').sort_index()
                feat = self.generate_features(ticker, df)
                return ticker, feat
            except Exception as e:
                logger.error(f"Error processing {ticker}: {e}")
                return ticker, None

        n_jobs = n_jobs if n_jobs != -1 else 14 # Default for M3 Max
        logger.info(f"Generating AutoML features for {len(universe)} stocks using {n_jobs} cores...")
        results = Parallel(n_jobs=n_jobs)(delayed(_process_single)(t) for t in universe)
        
        # Filter Nones
        return {k: v for k, v in results if v is not None and not v.empty}

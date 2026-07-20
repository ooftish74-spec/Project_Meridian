import json
import sqlite3
import pandas as pd
from pathlib import Path
import logging
logger = logging.getLogger(__name__)
_MERIDIAN_ROOT = Path(__file__).resolve().parent.parent.parent
_MERIDIAN_RESULTS = _MERIDIAN_ROOT / 'results'
_EXTERNAL_MACRO_DIR = Path.home() / '.gemini/antigravity/scratch/daily-economic-report'
_MACRO_DB_PATH = _EXTERNAL_MACRO_DIR / 'alpha_v1.db'

class DataAggregator:
    """Aggregates Project Meridian trade data and external Macro Economic data."""

    def __init__(self):
        self.logger = logger
        self.meridian_data = {}
        self.macro_data = pd.DataFrame()

    def load_meridian_data(self):
        """Load JSON results from Project Meridian."""
        files_to_load = {'shadow_summary': 'shadow_summary.json', 'shadow_portfolio': 'shadow_portfolio.json', 'stream_metrics': 'stream_metrics.json', 'gap_analysis': 'gap_analysis.json', 'realtime_var': 'realtime_var.json', 's6b_signal': 's6b_signal.json', 'signal_cache': 'signal_cache.json'}
        for key, filename in files_to_load.items():
            filepath = _MERIDIAN_RESULTS / filename
            if filepath.exists():
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        self.meridian_data[key] = json.load(f)
                except Exception as e:
                    self.logger.warning(f'Failed to load {filename}: {e}')
            else:
                self.meridian_data[key] = {}
        return self.meridian_data

    def load_macro_data(self):
        """Load macroeconomic indicators from external project's SQLite DB."""
        if not _MACRO_DB_PATH.exists():
            self.logger.warning(f'Macro DB not found at {_MACRO_DB_PATH}')
            return pd.DataFrame()
        try:
            conn = sqlite3.connect(_MACRO_DB_PATH)
            query = '\n                SELECT date, ticker, value \n                FROM economic_indicators\n                ORDER BY date ASC\n            '
            df_eco = pd.read_sql_query(query, conn)
            if not df_eco.empty:
                df_eco = df_eco.pivot_table(index='date', columns='ticker', values='value').reset_index()
                df_eco['date'] = pd.to_datetime(df_eco['date'])
                df_eco.set_index('date', inplace=True)
                self.macro_data = df_eco
            conn.close()
        except Exception as e:
            self.logger.error(f'Failed to load Macro Data: {e}')
        return self.macro_data

    def get_latest_macro_metrics(self):
        """Extract latest VIX, Rates, and key indicators."""
        metrics = {}
        if not self.macro_data.empty:
            df = self.macro_data.sort_index().ffill()
            latest = df.iloc[-1]
            for col in df.columns:
                metrics[col] = latest[col]
        if 'VIXCLS' not in metrics:
            metrics['VIXCLS'] = 20.0
        return metrics
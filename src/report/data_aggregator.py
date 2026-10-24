import json
import sqlite3
import pandas as pd
from pathlib import Path
import logging
logger = logging.getLogger(__name__)
_MERIDIAN_ROOT = Path(__file__).resolve().parent.parent.parent
_MERIDIAN_RESULTS = _MERIDIAN_ROOT / 'results'
_MERIDIAN_LIVE_RESULTS = _MERIDIAN_RESULTS / 'live'
_MACRO_DB_PATH = _MERIDIAN_ROOT / 'data' / 'macro_indicators.db'

class DataAggregator:
    """Aggregates Project Meridian trade data and external Macro Economic data."""

    def __init__(self):
        self.logger = logger
        self.meridian_data = {}
        self.macro_data = pd.DataFrame()

    def load_meridian_data(self):
        """Load JSON results from Project Meridian results/ directory."""
        files_to_load = {
            'kis_portfolio': 'kis_portfolio.json',
            'trade_ledger': 'trade_ledger.json',
            'shadow_summary': 'shadow_summary.json',
            'shadow_portfolio': 'shadow_portfolio.json',
            'stream_metrics': 'stream_metrics.json',
            'gap_analysis': 'gap_analysis.json',
            'realtime_var': 'realtime_var.json',
            's6b_signal': 's6b_signal.json',
            'signal_cache': 'signal_cache.json'
        }
        for key, filename in files_to_load.items():
            filepath = _MERIDIAN_RESULTS / filename
            
            if filepath.exists():
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        # Sanity Guard: 샌드박스 오염 검환 (삼성전자 > 20만 원 등 샌드박스 데이터 방지)
                        if key == 'signal_cache':
                            if not isinstance(data, dict):
                                data = {}
                            else:
                                stock_tech = data.get('stock_technicals', {})
                                if isinstance(stock_tech, dict):
                                    tech = stock_tech.get('005930', {})
                                    if isinstance(tech, dict) and tech.get('close', 0) > 200000:
                                        self.logger.warning(f"  🚨 [DataGuard] {filename} 내 샌드박스 오염 데이터 감지(삼성전자 {tech.get('close')}원) -> 차단")
                                        data = {}
                        self.meridian_data[key] = data
                except Exception as e:
                    self.logger.warning(f"Failed to load {filename}: {e}")
            else:
                self.meridian_data[key] = {}
        return self.meridian_data

    def load_macro_data(self):
        """Load macroeconomic indicators from parquet or external project's SQLite DB."""
        parquet_path = _MERIDIAN_ROOT / 'data' / 'macro' / 'macro_data.parquet'
        if parquet_path.exists():
            try:
                df_pq = pd.read_parquet(parquet_path)
                if not df_pq.empty:
                    if 'date' in df_pq.columns:
                        df_pq['date'] = pd.to_datetime(df_pq['date'])
                        df_pq.set_index('date', inplace=True)
                    self.macro_data = df_pq
                    return self.macro_data
            except Exception as e:
                self.logger.debug(f"Failed to load macro_data.parquet: {e}")
        if not _MACRO_DB_PATH.exists():
            self.logger.debug(f"Macro DB not found at {_MACRO_DB_PATH}")
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
            self.logger.error(f"Failed to load Macro Data: {e}")
        return self.macro_data

    def get_latest_macro_metrics(self):
        """Extract latest VIX, Rates, and key indicators."""
        metrics = {}
        if not self.macro_data.empty:
            df = self.macro_data.sort_index().ffill()
            latest = df.iloc[-1]
            for col in df.columns:
                metrics[col] = latest[col]
        signal_cache = self.meridian_data.get('signal_cache')
        if not isinstance(signal_cache, dict) or not signal_cache:
            sc_path = _MERIDIAN_RESULTS / 'signal_cache.json'
            if sc_path.exists():
                try:
                    signal_cache = json.loads(sc_path.read_text(encoding='utf-8'))
                except Exception:
                    signal_cache = {}
            else:
                signal_cache = {}
        if 'VIXCLS' not in metrics:
            vix_val = signal_cache.get('vix')
            if vix_val is not None:
                metrics['VIXCLS'] = vix_val
        return metrics
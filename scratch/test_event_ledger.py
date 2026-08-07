from src.measurement.event_ledger import EventLedger
from config.dynamic_config import DynamicConfig

# Override config to use Parquet
DynamicConfig().set('backtest.log_format', 'parquet')

ledger = EventLedger()
ledger.append('TRADE', {'ticker': '005930', 'action': 'buy', 'amount': 1000000}, source='test_script')

print("Event logged successfully. Testing query...")
events = ledger.query(event_type='TRADE', limit=1)
print(f"Queried events: {events}")

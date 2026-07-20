import unittest
from unittest.mock import patch
from src.data_collection.macro_realtime_refresher import MacroRealtimeRefresher
import json

class TestChaosMonkey(unittest.TestCase):
    @patch('src.data_collection.alpha_vantage_collector.collect_global_macro')
    @patch('pykrx.stock.get_market_ohlcv_by_date')
    def test_chaos_monkey_api_failure(self, mock_krx1, mock_av):
        # Mock APIs to raise exceptions
        mock_av.side_effect = Exception("Chaos Monkey: Alpha Vantage is down")
        mock_krx1.side_effect = Exception("Chaos Monkey: pykrx is down")

        refresher = MacroRealtimeRefresher()
        
        # Inject mock cache to trigger vix_std_20 fallback
        refresher._cache = {'vix': 25.0}

        try:
            # Should not crash
            result = refresher.refresh('all')
            self.assertTrue(True, "Refresher survived Chaos Monkey")
        except Exception as e:
            self.fail(f"Refresher crashed: {e}")
        
        # Let's run _refresh_tier1 directly with mocked update_cache
        with patch.object(refresher, '_update_cache') as mock_update:
            refresher._refresh_tier1()
            if mock_update.call_args_list:
                updates = mock_update.call_args_list[0][0][0]
                self.assertEqual(updates.get('vix_std_20'), 2.0)
                self.assertEqual(updates.get('vix_ma_20'), 25.0)

        # Let's run _refresh_s1_derived_stats directly
        with patch('src.data_collection.ss_etf_feature_engine.SSETFFeatureEngine.compute', side_effect=Exception("Chaos Monkey")):
            with patch.object(refresher, '_update_cache') as mock_update:
                refresher._refresh_s1_derived_stats()
                if mock_update.call_args_list:
                    updates = mock_update.call_args_list[0][0][0]
                    self.assertEqual(updates.get('atr_5m', -1), 0.0)
                    self.assertEqual(updates.get('lp_pressure_ma', -1), 0.0)
                    self.assertEqual(updates.get('lp_pressure_std', -1), 500.0)

if __name__ == '__main__':
    unittest.main()

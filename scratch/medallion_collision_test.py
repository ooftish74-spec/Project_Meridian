import unittest
from unittest.mock import patch
from src.risk.medallion_orchestrator import MedallionOrchestrator
import json

class TestMedallionCollision(unittest.TestCase):
    @patch.object(MedallionOrchestrator, '_collect_positions')
    @patch.object(MedallionOrchestrator, '_get_sentiment')
    def test_s1_short_s3_long_collision(self, mock_sentiment, mock_positions):
        # High VIX shock
        mock_sentiment.return_value = {
            'fear_greed': 20,
            'vix': 45.0,  # High VIX >= 35 (vix_high)
            'regime': 'crash'
        }
        
        # S1 is heavily SHORT (e.g. holding Inverse ETF 252670)
        # S3 is heavily LONG (e.g. holding KODEX 200 069500)
        mock_positions.return_value = {
            'a3_shadow': {
                'S1:252670': {
                    'kelly_ev': 0.05,
                    'up_probability': 0.6,
                    'unrealized_pnl_pct': -2.0,
                    'atr_pct': 0.02,
                    'sector': 'Inverse_ETF',
                    'exposure_weight': 0.5
                },
                'S3:069500': {
                    'kelly_ev': 0.04,
                    'up_probability': 0.55,
                    'unrealized_pnl_pct': -5.0,
                    'atr_pct': 0.015,
                    'sector': 'Market_ETF',
                    'exposure_weight': 0.5
                }
            }
        }
        
        orch = MedallionOrchestrator()
        result = orch.validate_all()
        exposure_decision = orch.compute_exposure(mock_sentiment.return_value)
        
        print("\n=== Collision Test Results ===")
        print(f"Overall Status: {result['overall']}")
        print(f"Total Issues: {result['total_issues']}")
        for key, val in result['validations'].items():
            print(f"- {key}: {val['status']} ({val['n_issues']} issues)")
            for issue in val.get('issues', []):
                print(f"  -> {issue['severity']}: {issue['issue']}")
                
        print("\n=== Exposure Decision ===")
        print(json.dumps(exposure_decision, indent=2))
        
        self.assertIsNotNone(result)

if __name__ == '__main__':
    unittest.main()

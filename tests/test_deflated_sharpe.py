import sys
from pathlib import Path
import pytest
import numpy as np
from unittest.mock import patch, MagicMock

# Mock missing module before importing alpha_miner
sys.modules.setdefault('src.alpha_factory.garbage_collector', MagicMock())

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.alpha_factory.alpha_miner import DeflatedSharpeFilter, CPCVEvaluator, AlphaMiner

def test_deflated_sharpe_filter():
    filter = DeflatedSharpeFilter()
    
    # Test with normal values
    res1 = filter.evaluate(sr_hat=1.5, t_samples=252, n_trials=1000, avg_corr=0.1)
    assert 'dsr' in res1
    assert 'sr_0' in res1
    assert res1['effective_trials'] < 1000
    
    # Test with zero trials
    res2 = filter.evaluate(sr_hat=1.5, t_samples=252, n_trials=0, avg_corr=0.1)
    assert res2['dsr'] == 1.0
    assert res2['sr_0'] == 0.0
    
    # Test with single trial
    res3 = filter.evaluate(sr_hat=1.5, t_samples=252, n_trials=1, avg_corr=0.1)
    assert res3['sr_0'] == 0.0

def test_cpcv_evaluator():
    cpcv = CPCVEvaluator()
    cpcv.n_groups = 4
    cpcv.n_test_groups = 2
    
    splits = cpcv.generate_splits(100)
    assert len(splits) == 6 # 4C2
    for train_idx, test_idx in splits:
        assert len(test_idx) == 50
        
@patch('src.alpha_factory.alpha_miner.SymbolicTransformer')
def test_mine_alphas_integration(mock_st):
    miner = AlphaMiner()
    mock_gp = MagicMock()
    mock_program = MagicMock()
    mock_program.execute.return_value = np.random.randn(200)
    mock_program.__str__ = MagicMock(return_value='add(X0, X1)')
    mock_program.fitness_ = 0.5
    miner.ic_evaluator.evaluate = MagicMock(return_value={'pass': True, 'oos_ic': 0.1, 'oos_ic_std': 0.05, 'ic_pvalue': 0.01})
    miner.cpcv_evaluator.evaluate = MagicMock(return_value={'pass': True, 'cpcv_ic': 0.05})
    miner.dsr_filter.evaluate = MagicMock(return_value={'pass': True, 'dsr': 0.99})
    miner.ortho_filter.is_orthogonal = MagicMock(return_value=(True, {'avg_corr': 0.1, 'max_corr': 0.2, 'max_corr_feature': 'x1'}))
    
    mock_st.return_value.fit.return_value = None
    with patch('src.alpha_factory.alpha_miner._safe_get_programs', return_value=[mock_program]):
        with patch.object(miner, 'load_data', return_value=import_pd().DataFrame({'f1': np.random.randn(200), 'target': np.random.randn(200)})):
            with patch('joblib.dump'):  # joblib.dump은 MagicMock을 pickle할 수 없음
                res = miner.mine_alphas(n_generations=1, pop_size=10)
                assert len(res) == 1

def import_pd():
    import pandas as pd
    return pd

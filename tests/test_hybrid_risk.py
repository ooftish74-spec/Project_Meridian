import pytest
from src.risk.exposure_orchestrator import ExposureOrchestrator
from src.risk.kill_switch import KillSwitch
from unittest.mock import patch

def test_boundary_box_trigger():
    eo = ExposureOrchestrator()
    sentiment = {
        'vix': 80.0, # Boundary exceeded (limit 45)
        'options_skew': 0.0,
        'ois': 0.5,
        'regime': 'crash'
    }
    with patch.object(eo, '_compute_flash_crash_gate', return_value=0.0):
        result = eo.calculate(sentiment=sentiment)
        assert result['target_exposure'] == 0, "VIX > 45 in crash regime: Soft-Landing(0.05) × regime_score(0.0) = 0"
        assert 'Dynamic Soft-Landing' in result['reason']

def test_vol_surface_melting():
    eo = ExposureOrchestrator()
    sentiment = {
        'vix': 30.0, # At inflection point
        'options_skew': 1.5, # At inflection point
        'ois': 1.2, # At inflection point
        'regime': 'caution'
    }
    result = eo.calculate(sentiment=sentiment)
    
    assert result['target_exposure'] > 0.0
    assert result['target_exposure'] < 1.0
    assert 'Dynamic Soft-Landing' in result['reason']

def test_hard_floor_mdd():
    ks = KillSwitch()
    metrics = {
        'today_return_pct': -1.0,
        'dd_pct': -6.0, # Exceeds -5.0% absolute limit
        'weekly_return_pct': -2.0,
        'consecutive_loss_days': 1,
        'monthly_return_pct': -1.0,
        'monthly_trading_days': 5,
        'dynamic_daily_limit_pct': -5.0,
        'dynamic_weekly_limit_pct': -10.0,
        'monthly_dynamic_limit_pct': -5.0,
        'forward': {
            'regime': 'bull',
            'signal_avg_confidence': 0.9,
            'bench_alpha_5d': 0.5,
            'news_sentiment': 0.8,
            'ois_score': 80
        }
    }
    
    result = ks.judge_action(metrics=metrics, regime='bull')
    
    assert result['triggered'] is True
    assert result['action'] == 'halt_all'
    assert any(t['type'] == 'hard_floor_mdd' for t in result['triggers'])

if __name__ == '__main__':
    pytest.main(['-v', __file__])

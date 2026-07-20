import pytest
from src.risk.exposure_orchestrator import ExposureOrchestrator

def test_v_recovery_override(monkeypatch):
    eo = ExposureOrchestrator()
    
    # Mock sentiment to simulate a 'crash' regime where base target would be 0.0
    sentiment = {
        'regime': 'crash',
        'vix': 50,
        'fear_greed': 10,
        'vkospi': 40,
        'kospi_ma20_dist': -10
    }
    
    # Mock TransitionSignalDetector to return a v_recovery signal
    class MockTSD:
        def detect(self):
            return {
                'signal_type': 'v_recovery',
                'strength': 1.0,
                'exposure_adjustment': 1.2
            }
    
    # Mock IntradayRegimeDetector to return normal
    class MockIRD:
        def detect(self):
            return {
                'regime': 'normal',
                'exposure_adjustment': 1.0
            }
            
    # Mock RealtimeVaR
    class MockRealtimeVaR:
        def _load_positions(self): return {}
        def _load_returns(self, t, l): return None
        def _ewma_variance(self, p, l): return 0.0

    import src.risk.exposure_orchestrator as eo_module
    
    # We need to mock the detector instantiation directly since it's imported locally inside the method
    import src.regime.transition_signal as ts_module
    monkeypatch.setattr(ts_module, 'TransitionSignalDetector', MockTSD)
    
    import src.risk.intraday_regime as ir_module
    monkeypatch.setattr(ir_module, 'IntradayRegimeDetector', MockIRD)
    
    import src.risk.realtime_var as rv_module
    monkeypatch.setattr(rv_module, 'RealtimeVaR', MockRealtimeVaR)
    
    # Force deadlock_resolution_mode to 'joint_prob' for the test
    original_cfg_get = eo_module._cfg_get if hasattr(eo_module, '_cfg_get') else None
    
    def mock_cfg_get(key, default=None):
        if key == 'risk.deadlock_resolution_mode': return 'joint_prob'
        if key == 'exposure.v_recovery_conf_adj_penalty': return 0.20
        if key == 'exposure.v_recovery_min_exposure': return 0.50
        if key == 'risk.max_combined_hedge_ratio': return 0.80
        if key == 'exposure.regime_score.crash': return 0.0
        return original_cfg_get(key, default) if original_cfg_get else default
        
    monkeypatch.setattr(eo, '_cfg_get', mock_cfg_get, raising=False)
    # also monkeypatch the global _cfg_get inside calculate method
    monkeypatch.setattr(eo_module, '_cfg_get', mock_cfg_get, raising=False)

    # Need to mock _cfg.get as well since calculate creates a local _cfg_get lambda
    if eo_module._cfg:
        monkeypatch.setattr(eo_module._cfg, 'get', mock_cfg_get)
        
    # Run calculate
    result = eo.calculate(sentiment=sentiment)
    
    # The base target should have been overridden from 0.0 to 0.50
    # And then adjusted by the v_recovery exposure adjustment logic
    print("Test Result:", result)
    
    assert result['target_exposure'] >= 0.50
    assert 'V-Recovery Override' in result['reason']

if __name__ == '__main__':
    pytest.main(['-v', __file__])

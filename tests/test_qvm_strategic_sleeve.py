"""
Unit Tests for Strategic QVM Long-Horizon Compounding Sleeve
"""

import pytest
from src.streams.s3_active_macro.qvm_strategic_sleeve import StrategicQVMCompoundingSleeve


def test_strategic_qvm_sleeve_initialization():
    sleeve = StrategicQVMCompoundingSleeve()
    assert sleeve.allocated_capital_krw is None
    
    sleeve_fixed = StrategicQVMCompoundingSleeve(allocated_capital_krw=5000000.0)
    assert sleeve_fixed.allocated_capital_krw == 5000000.0


def test_calculate_dynamic_sleeve_capital():
    sleeve = StrategicQVMCompoundingSleeve()
    nav_krw = 20000000.0 # 20,000,000 KRW
    
    # Baseline normal market
    normal_cap = sleeve.calculate_dynamic_sleeve_capital(nav_krw, avg_mos_pct=30.0, vix_zscore=0.0)
    assert normal_cap == 3000000.0 # 15% of 20M = 3.0M KRW
    
    # Crisis panic market (VIX spike + MoS expansion) -> Dynamic capital expansion
    crisis_cap = sleeve.calculate_dynamic_sleeve_capital(nav_krw, avg_mos_pct=80.0, vix_zscore=2.0)
    assert crisis_cap > normal_cap
    assert crisis_cap <= 7000000.0 # Max 35% cap


def test_calculate_margin_of_safety():
    sleeve = StrategicQVMCompoundingSleeve()
    
    stock = {
        'market_cap': 1000000000.0,
        'target_market_cap': 1500000000.0
    }
    mos = sleeve.calculate_margin_of_safety(stock)
    assert pytest.approx(mos, 0.1) == 50.0

    # Overvalued case
    stock_overvalued = {
        'market_cap': 2000000000.0,
        'target_market_cap': 1500000000.0
    }
    mos_over = sleeve.calculate_margin_of_safety(stock_overvalued)
    assert pytest.approx(mos_over, 0.1) == -25.0


def test_asymmetric_dip_multiplier():
    sleeve = StrategicQVMCompoundingSleeve()
    
    # Healthy quality value stock during VIX spike crisis
    healthy_dip_stock = {
        'market_cap': 1000000000.0,
        'target_market_cap': 2000000000.0, # MoS = +100%
        'total_assets': 1000.0,
        'net_income': 100.0,
        'cash_from_operations': 120.0,
        'annual_data': [{'revenue': 100}, {'revenue': 200}]
    }
    
    dip_res = sleeve.calculate_asymmetric_dip_multiplier(healthy_dip_stock, vix_zscore=2.0)
    assert dip_res['is_dip_buy_opportunity'] is True
    assert dip_res['dip_multiplier'] > 1.5

    # Value trap stock during crisis -> Excluded from dip buying
    trap_dip_stock = {
        'market_cap': 1000000000.0,
        'target_market_cap': 2000000000.0,
        'total_assets': 1000.0,
        'net_income': 100.0,
        'cash_from_operations': -50.0, # Value Trap
        'annual_data': []
    }
    trap_res = sleeve.calculate_asymmetric_dip_multiplier(trap_dip_stock, vix_zscore=2.0)
    assert trap_res['is_dip_buy_opportunity'] is False
    assert trap_res['dip_multiplier'] == 0.0


def test_evaluate_strategic_sleeve_with_mock_universe():
    sleeve = StrategicQVMCompoundingSleeve(allocated_capital_krw=10000000.0)

    mock_universe = [
        {
            'ticker': '005930',
            'name': 'Samsung Electronics',
            'qvm_score': 85.0,
            'market_cap': 70000.0,
            'target_market_cap': 140000.0,
            'price': 70000.0,
            'total_assets': 1000.0,
            'net_income': 100.0,
            'cash_from_operations': 120.0,
            'annual_data': [{'revenue': 100}, {'revenue': 200}, {'revenue': 300}]
        },
        {
            'ticker': '000660',
            'name': 'SK Hynix',
            'qvm_score': 75.0,
            'market_cap': 100000.0,
            'target_market_cap': 150000.0,
            'price': 100000.0,
            'total_assets': 1000.0,
            'net_income': 100.0,
            'cash_from_operations': 110.0,
            'annual_data': [{'revenue': 100}, {'revenue': 200}, {'revenue': 300}]
        },
        {
            'ticker': '000000',
            'name': 'Value Trap Co',
            'qvm_score': 30.0,
            'market_cap': 50000.0,
            'target_market_cap': 40000.0,
            'price': 50000.0,
            'total_assets': 1000.0,
            'net_income': 50.0,
            'cash_from_operations': -100.0, # Cash flow disconnect
            'annual_data': []
        }
    ]

    current_holdings = {
        '000000': {'shares': 10, 'entry_price': 50000.0, 'current_price': 50000.0}
    }

    result = sleeve.evaluate_strategic_sleeve(universe=mock_universe, current_holdings=current_holdings)

    assert result['sleeve_status'] == 'ACTIVE_STRATEGIC_EVALUATION'
    assert result['is_absolute_capital_defined'] is True
    assert result['allocated_capital_krw'] == 10000000.0
    assert result['noise_stop_loss_exempt'] is True
    
    # 005930 & 000660 should be selected
    assert '005930' in result['portfolio_weights']
    assert '005930' in result['target_positions_krw']
    assert '005930' in result['target_shares']
    assert result['target_positions_krw']['005930'] > 0

    # 000000 Value trap holding should trigger exit signal
    assert len(result['exit_signals']) > 0
    assert result['exit_signals'][0]['ticker'] == '000000'

"""
Tests for Citadel CRO Final Veto Gate
"""

import pytest
import numpy as np
from src.risk.cro_final_gate import CROFinalGate

def test_cro_final_gate_pass():
    gate = CROFinalGate(max_daily_var_pct=0.054, max_portfolio_exposure=1.0)

    orders = [
        {'ticker': '005930', 'direction': 1, 'net_shares': 100, 'net_amount': 7_000_000.0, 'price': 70000.0}
    ]
    portfolio_value = 100_000_000.0
    cov_matrix = np.array([[0.0002]])
    ticker_map = {'005930': 0}

    scaled_orders, audit = gate.evaluate_and_scale_orders(orders, portfolio_value, cov_matrix, ticker_map)

    assert audit['veto_triggered'] is False
    assert audit['scaling_factor'] == 1.0
    assert scaled_orders[0]['net_shares'] == 100

def test_cro_final_gate_veto_scaling():
    # Tight VaR limit to force vector projection scaling
    gate = CROFinalGate(max_daily_var_pct=0.01, max_portfolio_exposure=1.0)

    orders = [
        {'ticker': '005930', 'direction': 1, 'net_shares': 1000, 'net_amount': 70_000_000.0, 'price': 70000.0}
    ]
    portfolio_value = 100_000_000.0
    cov_matrix = np.array([[0.0025]])  # 5% daily vol -> 99% VaR ~ 11.6%
    ticker_map = {'005930': 0}

    scaled_orders, audit = gate.evaluate_and_scale_orders(orders, portfolio_value, cov_matrix, ticker_map)

    assert audit['veto_triggered'] is True
    assert audit['scaling_factor'] < 1.0
    assert scaled_orders[0]['net_shares'] < 1000

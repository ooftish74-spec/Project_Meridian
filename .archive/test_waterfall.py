import logging
import sys

# Configure logging to see the output
logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'Project_Meridian')))

from src.allocation.alpha_allocator import AlphaAllocator

def run_test():
    allocator = AlphaAllocator()

    # Mock initial weights
    weights = {
        'S1': 0.15,
        'S2': 0.30,
        'S3': 0.15,
        'S5': 0.05,
        'S6-B': 0.10,
        'S9': 0.10,
        'S10': 0.15
    }

    # Mock stream metrics
    stream_metrics = {
        'S1': {'ic_5d': -0.05, 'daily_returns': [-0.01, -0.02, 0.01, -0.01, -0.03]}, # Poor expected return
        'S2': {'ic_5d': 0.10, 'daily_returns': [0.01, 0.02, 0.01, 0.01, 0.02]},  # Good expected return -> should be skipped
        'S3': {'max_drawdown': 0.20}, # High drawdown -> Tier 4 should hit this
        'S6-B': {'ic_5d': 0.01, 'daily_returns': [0.0, 0.0, 0.0, 0.0, 0.0]},
        'S9': {'ic_5d': -0.02, 'daily_returns': [-0.01, -0.01, -0.01, -0.01, -0.01]},
        'S10': {'max_drawdown': 0.05} # Low drawdown
    }

    # Mock S0 Signals
    s0_sigs = [
        {
            'trigger_cash_sweep': True,
            'target_sweep_ratio': 0.55, # We need 55%
            'expected_return': 0.005 # Beta expected return 0.5%
        }
    ]

    print("Initial Weights:", weights)
    print("S0 Signals:", s0_sigs)
    
    new_weights = allocator._apply_s0_cash_sweep(weights.copy(), s0_sigs, stream_metrics)
    
    print("\nFinal Weights:", new_weights)

if __name__ == '__main__':
    run_test()

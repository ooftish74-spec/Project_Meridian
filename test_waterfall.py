import logging
logging.basicConfig(level=logging.INFO)

from src.allocation.alpha_allocator import AlphaAllocator

allocator = AlphaAllocator()

stream_metrics = {
    'S1': {'ic_5d': 0.05, 'daily_returns': [0.01, 0.02, -0.01, 0.03, 0.01]}, # Expected ret > beta
    'S2': {'ic_5d': -0.02, 'daily_returns': [-0.01, -0.02, 0.01, -0.01, 0.0]}, # Expected ret < beta
    'S3': {'ic_5d': 0.01, 'daily_returns': [0.0]},
    'S5': {'ic_5d': 0.0, 'daily_returns': [0.0]}
}

s0_sigs = [
    {
        'trigger_cash_sweep': True,
        'target_sweep_ratio': 0.30, # We want 30% from the sweep
        'expected_return': 0.03    # Beta expects 3%
    }
]

weights = allocator.allocate(stream_metrics, regime='bull', s0_sigs=s0_sigs)
print("\n[Final Weights]")
for k, v in weights.items():
    print(f"  {k}: {v:.1%}")

import sys
import numpy as np
import pandas as pd
sys.path.append('.')

# 3. Test Risk Parity (Singular Matrix bypass)
from src.allocation.risk_parity import RiskParityOptimizer
rp = RiskParityOptimizer()
cov = np.ones((5, 5))
weights = rp.optimize(cov_matrix=cov)
print("Risk Parity Weights with singular matrix:", weights)

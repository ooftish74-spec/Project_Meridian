"""
Tests for FactorOrthogonalizer
"""

import pytest
import numpy as np
from src.alpha_factory.factor_orthogonalizer import FactorOrthogonalizer

def test_factor_orthogonalizer_uncorrelated():
    ortho = FactorOrthogonalizer()
    np.random.seed(42)

    # Base factor (N=100)
    base_factor = np.random.randn(100)
    # Correlated new factor (80% correlated with base_factor + noise)
    new_factor = 0.8 * base_factor + 0.2 * np.random.randn(100)

    base_matrix = base_factor.reshape(-1, 1)

    residual, max_corr = ortho.orthogonalize_factor(new_factor, base_matrix)

    assert max_corr > 0.70  # Initially high correlation
    # After orthogonalization, correlation with base_factor must be ~ 0
    post_corr = abs(float(np.corrcoef(residual, base_factor)[0, 1]))
    assert post_corr < 1e-2

def test_factor_orthogonalizer_empty_base():
    ortho = FactorOrthogonalizer()
    new_factor = np.array([1.0, 2.0, 3.0, 4.0])

    residual, max_corr = ortho.orthogonalize_factor(new_factor, np.empty((4, 0)))

    assert max_corr == 0.0
    assert np.array_equal(residual, new_factor)

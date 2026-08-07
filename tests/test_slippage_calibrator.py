import pytest
from src.execution.slippage_model import SlippageCalibrator
from config.dynamic_config import DynamicConfig


def test_slippage_calibrator_eta_calibration(monkeypatch):
    """η EWMA 캘리브레이션: actual/predicted 비율의 EWMA 적용."""
    calibrator = SlippageCalibrator()
    calibrator.min_samples = 2  # 테스트용 최소 샘플 수 낮춤

    # 실제 체결 기록 형식으로 mock 데이터 생성
    # actual_bps=10, predicted_bps=5 → ratio=2.0
    # actual_bps=15, predicted_bps=5 → ratio=3.0
    mock_records = [
        {'actual_bps': 10.0, 'predicted_bps': 5.0, 'order_size': 5_000_000, 'adv': 100_000_000_000},
        {'actual_bps': 15.0, 'predicted_bps': 5.0, 'order_size': 5_000_000, 'adv': 100_000_000_000},
    ]

    cfg = DynamicConfig()
    cfg.set('slippage.impact_coefficient', 10.0)
    cfg.set('slippage.impact_exponent', 0.5)

    res = calibrator.calibrate(mock_records)
    assert 'eta' in res
    assert 'ewma_ratio' in res
    # EWMA of [2.0, 3.0] with α = 2/(20+1) ≈ 0.095
    # ewma starts at 1.0, then:
    #   after ratio=2.0: ewma = 0.095*2.0 + 0.905*1.0 = 1.095
    #   after ratio=3.0: ewma = 0.095*3.0 + 0.905*1.095 ≈ 1.276
    # η_new = 10.0 × 1.276 ≈ 12.76
    assert 10.0 < res['eta'] < 20.0  # 보수적으로 범위 검증
    assert res['ewma_ratio'] > 1.0  # 예측보다 실적이 크면 ratio > 1


def test_slippage_calibrator_clamping():
    """η clamping: eta_max 초과 시 클램핑 동작 검증."""
    calibrator = SlippageCalibrator()
    calibrator.min_samples = 1
    calibrator.eta_max = 15.0  # 낮은 상한

    # 매우 큰 ratio (actual >> predicted) → η 폭주 유도
    mock_records = [
        {'actual_bps': 100.0, 'predicted_bps': 1.0, 'order_size': 5_000_000, 'adv': 100_000_000_000}
        for _ in range(30)  # 30번 반복 → EWMA가 높은 ratio로 수렴
    ]

    cfg = DynamicConfig()
    cfg.set('slippage.impact_coefficient', 10.0)

    res = calibrator.calibrate(mock_records)
    assert res['eta'] <= 15.0  # clamping 동작 확인


def test_slippage_calibrator_delta_ols():
    """δ OLS 재추정: log-log 선형 관계에서 멱지수 추출."""
    import math
    calibrator = SlippageCalibrator()
    calibrator.min_samples = 5
    calibrator.delta_min = 0.3
    calibrator.delta_max = 0.7

    # 합성 데이터: actual_bps = base_bps + η × σ × (Q/V)^0.5 × 10000
    # base_bps = 3.0, η=10, σ=0.02 → impact = 10×0.02×sqrt(Q/V)×10000
    base_bps = 3.0
    mock_records = []
    for i in range(25):
        q = (i + 1) * 1_000_000  # 1M~25M
        v = 50_000_000_000  # 50B
        participation = q / v
        impact = 10.0 * 0.02 * math.sqrt(participation) * 10000
        actual = base_bps + impact
        predicted = actual * 1.1  # 약간 다르게
        mock_records.append({
            'actual_bps': actual,
            'predicted_bps': predicted,
            'order_size': q,
            'adv': v,
        })

    cfg = DynamicConfig()
    cfg.set('slippage.impact_coefficient', 10.0)
    cfg.set('slippage.impact_exponent', 0.5)
    cfg.set('slippage.base_bps', 3.0)

    res = calibrator.calibrate(mock_records)
    assert 'delta' in res
    assert 'delta_r2' in res
    # 합성 데이터에서 δ ≈ 0.5 (square-root law)
    assert 0.4 <= res['delta'] <= 0.6, f"Expected δ≈0.5, got {res['delta']}"
    assert res['delta_r2'] > 0.8, f"Expected high R², got {res['delta_r2']}"


def test_slippage_calibrator_insufficient_samples():
    """샘플 부족 시 빈 dict 반환."""
    calibrator = SlippageCalibrator()
    calibrator.min_samples = 100

    mock_records = [
        {'actual_bps': 10.0, 'predicted_bps': 5.0, 'order_size': 5_000_000, 'adv': 100_000_000_000}
    ]
    res = calibrator.calibrate(mock_records)
    assert res == {}


def test_v1_backtest_monitor(tmp_path):
    from scripts.v1_backtest_monitor import V1BacktestMonitor
    monitor = V1BacktestMonitor()

    log_file = tmp_path / "test.log"
    log_file.write_text("Sharpe: 1.25\nMDD: -0.15\n")

    res = monitor.parse_log(log_file)
    assert res['sharpe'] == 1.25
    assert res['mdd'] == -0.15

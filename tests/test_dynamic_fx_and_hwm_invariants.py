"""Hermetic closed-form unit tests for Dynamic FX Ornstein-Uhlenbeck + Ito Allocation,
Ghost HWM Contamination Prevention, and RegimeEngine Macro History SSOT Fallback.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.allocation.alpha_allocator import AlphaAllocator
from src.execution.position_ledger import PositionLedgerManager
from src.execution.us_night_engine import USNightExecutionEngine
from src.intelligence.regime_engine import RegimeEngine
from src.utils.dynamic_quant_engine import DynamicQuantEngine


def test_ornstein_uhlenbeck_fx_dynamics_closed_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify get_fx_distribution_and_dynamics against hand-computed closed-form OU & momentum equations."""
    dqe = DynamicQuantEngine()

    # Construct deterministic 60-day log-FX series around exp(7.23) ~ 1380.22
    idx = pd.date_range("2026-08-01", periods=60, freq="B")
    t_arr = np.arange(60, dtype=float)
    log_vals = 7.23 + 0.02 * np.sin(t_arr * 0.25)
    fx_vals = np.exp(log_vals)
    fx_series = pd.Series(fx_vals, index=idx)

    monkeypatch.setattr(dqe, "_load_fx_close_series", lambda tail_n=252: fx_series)
    # Prevent external index correlation file reads so rho_us_fx == 0, rho_kr_fx == 0 deterministically
    monkeypatch.setattr(dqe, "_load_close_and_adt", lambda path, tail_n=120: None)

    spot_fx = 1436.0
    res = dqe.get_fx_distribution_and_dynamics(spot_fx=spot_fx)

    # Hand-compute closed-form values on historical series + spot evaluation
    log_fx = np.log(fx_vals)
    n_obs = float(len(log_fx))
    mu_x = float(np.mean(log_fx))
    sigma_x = float(np.std(log_fx, ddof=1))
    z_fx = (math.log(spot_fx) - mu_x) / sigma_x

    y = log_fx - mu_x
    phi_raw = float(np.dot(y[1:], y[:-1]) / np.dot(y[:-1], y[:-1]))
    phi_1 = min(1.0 - 1.0 / n_obs, max(math.exp(-1.0), phi_raw))
    theta_ou = -math.log(phi_1)
    half_life = math.log(2.0) / theta_ou
    mu_fx_ou = -252.0 * theta_ou * (math.log(spot_fx) - mu_x)

    fx_rets = np.diff(log_fx)
    sig_daily = float(np.std(fx_rets, ddof=1))
    sig_ann = sig_daily * math.sqrt(252.0)
    recent_20 = fx_rets[-20:]
    mean_20 = float(np.mean(recent_20))
    t_mom = abs(mean_20) / (sig_daily / math.sqrt(20.0))
    mu_fx_mom = (t_mom / (1.0 + t_mom)) * mean_20 * 252.0

    w_ou = (z_fx * z_fx) / (1.0 + z_fx * z_fx)
    mu_fx_exp = w_ou * mu_fx_ou + (1.0 - w_ou) * mu_fx_mom
    quality = n_obs / (n_obs + 20.0)

    assert res["spot_fx"] == pytest.approx(round(spot_fx, 4), rel=1e-9)
    assert res["eq_fx_mean"] == pytest.approx(round(math.exp(mu_x), 4), rel=1e-9)
    assert res["fx_z_score"] == pytest.approx(round(z_fx, 4), rel=1e-9)
    assert res["ou_theta_daily"] == pytest.approx(round(theta_ou, 6), rel=1e-9)
    assert res["ou_half_life_days"] == pytest.approx(round(half_life, 2), rel=1e-9)
    assert res["expected_fx_ou_drift_ann"] == pytest.approx(round(mu_fx_ou, 6), rel=1e-9)
    assert res["expected_fx_mom_drift_ann"] == pytest.approx(round(mu_fx_mom, 6), rel=1e-9)
    assert res["expected_fx_drift_ann"] == pytest.approx(round(mu_fx_exp, 6), rel=1e-9)
    assert res["fx_vol_ann"] == pytest.approx(round(sig_ann, 6), rel=1e-9)
    assert res["quality"] == pytest.approx(round(quality, 4), rel=1e-9)
    # At elevated spot_fx = 1436 > eq_fx_mean ~ 1380, z_fx > 0 and OU drift is strictly negative
    assert z_fx > 1.0
    assert mu_fx_ou < 0.0


def test_ito_global_weights_and_usd_wallet_boundary_closed_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Ito 2nd-order multi-currency return/variance and USD wallet non-stranding cap."""
    dqe = DynamicQuantEngine()
    monkeypatch.setattr(dqe, "_ensure_calibrated", lambda force=False: None)

    fake_fx_dyn = {
        "spot_fx": 1436.0,
        "eq_fx_mean": 1385.0,
        "fx_z_score": 1.35,
        "expected_fx_drift_ann": -0.04,
        "fx_vol_ann": 0.08,
        "rho_us_fx": -0.25,
        "rho_kr_fx": -0.40,
        "quality": 0.80,
    }
    monkeypatch.setattr(dqe, "get_fx_distribution_and_dynamics", lambda signal_cache=None, spot_fx=None: fake_fx_dyn)

    krx_m = {"ret": 0.10, "vol": 0.20}
    us_m = {"ret": 0.14, "vol": 0.16}
    regime_probs = {"bull": 0.20, "caution": 0.50, "bear": 0.20, "crash": 0.10}
    acct_state = {
        "total_equity": 19_180_000.0,
        "usd_cash": 1520.30,
        "us_positions_value_krw": 5_859_600.0,
    }

    res = dqe.compute_fx_adjusted_global_weights(
        krx_metrics=krx_m,
        us_metrics=us_m,
        regime="caution",
        regime_probs=regime_probs,
        account_state=acct_state,
    )

    # Hand-computed closed-form oracle
    cov_us_fx = -0.25 * 0.16 * 0.08
    mu_us_krw = 0.14 + (-0.04) + cov_us_fx
    sig_us_krw = math.sqrt(0.16**2 + 0.08**2 + 2.0 * cov_us_fx)
    mu_kr_adj = 0.10 + (-0.40) * (-0.04)
    sig_kr = 0.20

    inv_kr = 1.0 / sig_kr
    inv_us = 1.0 / sig_us_krw
    w_us_rp = inv_us / (inv_kr + inv_us)

    score_kr = inv_kr * math.exp(math.tanh(mu_kr_adj / sig_kr))
    score_us = inv_us * math.exp(math.tanh(mu_us_krw / sig_us_krw))
    w_us_boltz = score_us / (score_kr + score_us)

    w_us_target = 0.20 * w_us_rp + 0.80 * w_us_boltz
    w_kr_target = 1.0 - w_us_target

    expected_energy = 0.20 * 0.0 + 0.50 * 0.5 + 0.20 * 1.0 + 0.10 * 2.0
    kappa = math.exp(-expected_energy)
    w_wallet = (1520.30 * 1436.0 + 5_859_600.0) / 19_180_000.0
    fx_decay = math.exp(-1.35)
    cap_expected = max(kappa * w_us_target * fx_decay, kappa * w_wallet)

    assert res["mu_us_krw"] == pytest.approx(round(mu_us_krw, 6), rel=1e-9)
    assert res["sig_us_krw"] == pytest.approx(round(sig_us_krw, 6), rel=1e-9)
    assert res["mu_kr_adj"] == pytest.approx(round(mu_kr_adj, 6), rel=1e-9)
    assert res["w_us_risk_parity"] == pytest.approx(round(w_us_rp, 4), rel=1e-9)
    assert res["w_us_boltzmann"] == pytest.approx(round(w_us_boltz, 4), rel=1e-9)
    assert res["w_us"] == pytest.approx(round(w_us_target, 4), rel=1e-9)
    assert res["w_krx"] == pytest.approx(round(w_kr_target, 4), rel=1e-9)
    assert res["kappa_regime"] == pytest.approx(round(kappa, 4), rel=1e-9)
    assert res["w_usd_wallet"] == pytest.approx(round(w_wallet, 4), rel=1e-9)
    assert res["us_exposure_cap_pct"] == pytest.approx(round(cap_expected, 4), rel=1e-9)

    # Verify AlphaAllocator delegation
    alloc = AlphaAllocator()
    alloc_res = alloc.compute_global_market_weights(
        krx_metrics={"expected_return": 0.10, "volatility": 0.20},
        us_metrics={"expected_return": 0.14, "volatility": 0.16},
        regime="caution",
        regime_probs=regime_probs,
        account_state=acct_state,
    )
    assert alloc_res["w_krx"] + alloc_res["w_us"] == pytest.approx(1.0, rel=1e-9)
    assert alloc_res["us_exposure_cap_pct"] == pytest.approx(round(cap_expected, 4), rel=1e-9)


def test_ghost_hwm_contamination_prevented_on_reentry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce V ($404.36 ghost peak vs $397.74 re-entry) and verify P_peak == P_avg * (1 + peak_pnl/100)."""
    ledger_file = tmp_path / "position_entry_ledger.json"
    exit_file = tmp_path / "daily_exited_tickers.json"
    us_holdings_file = tmp_path / "us_night_holdings.json"
    kp_file = tmp_path / "kis_portfolio.json"

    # 1. Seed stale us_night_holdings.json from Oct 27 trade (entry $399.83, peak $404.36)
    us_holdings_file.write_text(
        json.dumps([
            {
                "ticker": "V",
                "entry_price": 399.83,
                "current_price": 399.83,
                "highest_price": 404.36,
                "peak_price": 404.36,
                "peak_pnl_pct": 1.664,
                "trail_activated": True,
                "quantity": 1,
                "entry_date": "2026-10-27",
            }
        ]),
        encoding="utf-8",
    )

    # 2. Seed contaminated ledger entry where peak_price=404.36 was written when last_avg_price=0.0
    ledger_file.write_text(
        json.dumps({
            "V": {
                "entry_date": "2026-10-28",
                "created_at": "2026-10-28T23:35:00",
                "peak_price": 404.36,
                "lowest_price": 401.545,
                "peak_pnl_pct": 0.956655,
                "peak_date": "2026-10-28",
                "trail_activated": False,
                "scaled_out": False,
                "last_avg_price": 0.0,
                "last_qty": 0.0,
                "last_quantity": 0.0,
                "stream_id": "S3_B",
                "track": "TRACK_2_BUFFETT_QVM",
            }
        }),
        encoding="utf-8",
    )

    # 3. Seed live kis_portfolio.json with Oct 28 re-entry at avg_price = $397.74, current_price = $401.545
    kp_file.write_text(
        json.dumps({
            "account": {"total_equity": 19180000, "usd_cash": 1520.30, "fx_rate": 1436.0},
            "holdings": {
                "V": {
                    "name": "Visa Inc.",
                    "qty": 1,
                    "avg_price": 397.74,
                    "current_price": 401.545,
                    "pnl_pct": 0.956655,
                    "return_pct": 0.956655,
                    "stream_id": "S3_B",
                    "track": "TRACK_2_BUFFETT_QVM",
                }
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.execution.us_night_engine._RESULTS_DIR", tmp_path)
    monkeypatch.setattr("src.execution.us_night_engine._US_HOLDINGS_FILE", us_holdings_file)

    engine = USNightExecutionEngine()
    assert len(engine.holdings) == 1
    v_hold = engine.holdings[0]

    # Expected peak price is 397.74 * (1 + 0.956655 / 100) = 401.545, NOT the ghost 404.36!
    expected_peak = 397.74 * (1.0 + 0.956655 / 100.0)
    assert v_hold["peak_price"] == pytest.approx(expected_peak, rel=1e-6)
    assert v_hold["highest_price"] == pytest.approx(expected_peak, rel=1e-6)
    assert v_hold["peak_pnl_pct"] == pytest.approx(0.956655, rel=1e-5)
    assert v_hold["trail_activated"] is False

    # Check persisted position_entry_ledger.json
    plm = PositionLedgerManager(ledger_path=ledger_file)
    led_v = plm.load_ledger()["V"]
    assert led_v["last_avg_price"] == pytest.approx(397.74, rel=1e-9)
    assert led_v["last_qty"] == pytest.approx(1.0, rel=1e-9)
    assert led_v["peak_price"] == pytest.approx(expected_peak, rel=1e-6)


def test_regime_engine_macro_history_parquet_ssot_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify RegimeEngine._read_macro_history loads from Parquet/signal_cache when overnight_macro is absent."""
    re_eng = RegimeEngine()
    # Point _PROJECT_ROOT to tmp_path where data/raw/overnight_macro does NOT exist
    monkeypatch.setattr("src.intelligence.regime_engine._PROJECT_ROOT", tmp_path)

    kr_dir = tmp_path / "data" / "kr_markets"
    kr_dir.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range("2026-06-01", periods=70, freq="B")
    df_vix = pd.DataFrame({"date": idx, "close": np.linspace(15.0, 21.0, 70)})
    df_vix.to_parquet(kr_dir / "cross_vix.parquet")

    fx_series = pd.Series(np.linspace(1360.0, 1436.0, 70), index=idx)
    dqe = DynamicQuantEngine()
    monkeypatch.setattr(dqe, "_load_fx_close_series", lambda tail_n=120: fx_series)

    fx_hist = re_eng._read_macro_history("usdkrw")
    vix_hist = re_eng._read_macro_history("vix")

    assert len(fx_hist) == 70
    assert fx_hist[-1] == pytest.approx(1436.0, rel=1e-9)
    assert len(vix_hist) == 70
    assert vix_hist[-1] == pytest.approx(21.0, rel=1e-9)
    assert re_eng._read_fx_history() is not None


def test_zero_data_loss_disk_retention_and_compression(tmp_path: Path) -> None:
    """Verify purge_legacy_artifacts preserves tick/parquet & SSOT ledgers, compresses old shadow_trades, and purges old console logs/PDFs."""
    import gzip
    from datetime import datetime
    from scripts.purge_legacy_artifacts import purge_legacy_artifacts

    # 1. Seed protected tick/orderbook Parquet and SSOT ledgers
    orderbook_dir = tmp_path / "data" / "orderbook"
    orderbook_dir.mkdir(parents=True, exist_ok=True)
    tick_parquet = orderbook_dir / "005930_ticks_20260715.parquet"
    tick_parquet.write_bytes(b"PAR1_TICK_DATA_IMMUTABLE")

    results_dir = tmp_path / "results"
    corp_dir = results_dir / "corporate_ledger"
    corp_dir.mkdir(parents=True, exist_ok=True)
    fills_master = corp_dir / "broker_fills_master.json"
    fills_master.write_text('{"fills": [{"ticker": "V", "qty": 1}]}', encoding="utf-8")
    latest_xlsx = corp_dir / "Meridian_Corporate_Accounting_Ledger_Latest.xlsx"
    latest_xlsx.write_bytes(b"LATEST_EXCEL_SSOT")
    old_xlsx = corp_dir / "Meridian_Corporate_Accounting_Ledger_20260801.xlsx"
    old_xlsx.write_bytes(b"OLD_REDUNDANT_EXCEL")

    kp_file = results_dir / "kis_portfolio.json"
    kp_file.write_text('{"nav": 18054923}', encoding="utf-8")

    # 2. Seed unstructured console text logs (old vs recent) and PDF reports
    rlogs_dir = results_dir / "logs"
    rlogs_dir.mkdir(parents=True, exist_ok=True)
    old_console_log = rlogs_dir / "daily_pipeline_20260815.log"
    old_console_log.write_text("ERROR LOOP TRACEBACK\n" * 100, encoding="utf-8")
    recent_console_log = rlogs_dir / "daily_pipeline_20261028.log"
    recent_console_log.write_text("INFO normal run\n", encoding="utf-8")

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    old_pdf = reports_dir / "Ultimate_Meridian_Quant_Report_KR_2026-08-15.pdf"
    old_pdf.write_bytes(b"%PDF-1.4 old report")
    recent_pdf = reports_dir / "Ultimate_Meridian_Quant_Report_KR_2026-10-28.pdf"
    recent_pdf.write_bytes(b"%PDF-1.4 recent report")
    font_file = reports_dir / "NanumGothic.ttf"
    font_file.write_bytes(b"TTF_FONT_DATA")

    # 3. Seed structured daily shadow_trades (old -> should become .json.gz; recent -> stays .json)
    st_dir = results_dir / "shadow_trades"
    st_dir.mkdir(parents=True, exist_ok=True)
    old_trade_json = st_dir / "2026-08-15.json"
    old_trade_payload = '{"date": "2026-08-15", "trades": [{"ticker": "069500", "pnl": 12500}]}'
    old_trade_json.write_text(old_trade_payload, encoding="utf-8")
    recent_trade_json = st_dir / "2026-10-28.json"
    recent_trade_json.write_text('{"date": "2026-10-28", "trades": []}', encoding="utf-8")

    ref_ts = datetime(2026, 10, 29, 9, 0, 0).timestamp()
    stats = purge_legacy_artifacts(project_root=tmp_path, now_ts=ref_ts)

    assert stats["purged_count"] >= 3
    assert stats["compressed_count"] == 1

    # Protected files 100% intact
    assert tick_parquet.exists() and tick_parquet.read_bytes() == b"PAR1_TICK_DATA_IMMUTABLE"
    assert fills_master.exists()
    assert latest_xlsx.exists()
    assert kp_file.exists()
    assert font_file.exists()

    # Old console logs, old PDFs, and old redundant dated xlsx removed; recent ones kept
    assert not old_console_log.exists()
    assert recent_console_log.exists()
    assert not old_pdf.exists()
    assert recent_pdf.exists()
    assert not old_xlsx.exists()

    # Old structured trade log losslessly compressed to .json.gz; recent stays uncompressed
    assert not old_trade_json.exists()
    compressed_gz = st_dir / "2026-08-15.json.gz"
    assert compressed_gz.exists()
    with gzip.open(compressed_gz, "rt", encoding="utf-8") as f_in:
        assert f_in.read() == old_trade_payload
    assert recent_trade_json.exists()


def test_scale_out_idempotency_and_d2_reusable_krw_buying_power(tmp_path: Path) -> None:
    """Verify mark_scaled_out prevents repeated 50% scale-outs and nrcvb_buy_amt capped by raw_d2_cash unlocks unsettled KRW sale proceeds."""
    from src.execution.position_ledger import PositionLedgerManager
    from src.risk.universal_exit_engine import UniversalExitEngine

    ledger_path = tmp_path / "position_entry_ledger.json"
    plm = PositionLedgerManager(ledger_path=ledger_path)
    plm.record_entry("012750", entry_price=95443.0, stream_id="S2", strategy="ml_alpha", track="TRACK_1_TACTICAL")

    hwm0 = plm.get_hwm("012750")
    assert hwm0["scaled_out"] is False

    # Simulate marking scaled_out after first 50% scale-out
    plm.mark_scaled_out("012750")
    hwm1 = plm.get_hwm("012750")
    assert hwm1["scaled_out"] is True

    # UniversalExitEngine must NOT fire scale_out_50pct or time_decay_exit when scaled_out=True
    uee = UniversalExitEngine()
    positions = {
        "012750": {
            "ticker": "012750",
            "stream_id": "S2",
            "strategy": "ml_alpha",
            "track": "TRACK_1_TACTICAL",
            "quantity": 4,
            "entry_price": 95443.0,
            "current_price": 94900.0,
            "unrealized_pnl_pct": -0.57,
            "peak_pnl_pct": 0.58,
            "peak_price": 96000.0,
            "lowest_price": 94900.0,
            "scaled_out": bool(hwm1.get("scaled_out", False)),
            "trail_activated": False,
            "holding_minutes": 4320.0,
            "market": "KR",
        }
    }
    res = uee.evaluate_all_positions(positions, market_data={}, regime="caution")
    scale_out_orders = [
        o for o in res.get("exit_orders", [])
        if o.get("exit_type") in ("scale_out_50pct", "time_decay_exit")
    ]
    assert len(scale_out_orders) == 0

    # Verify KRX D+2 reusable sale proceeds math:
    # ord_psbl_cash_raw_krw = 330,844 (D+0 cash), nrcvb_buy_amt_krw = 5,093,461 (D+0 + ruse_psbl_amt), raw_d2_cash = 5,154,813
    ord_psbl_cash_raw_krw = 330844.0
    nrcvb_buy_amt_krw = 5093461.0
    raw_d2_cash = 5154813.0
    ord_psbl_cash = nrcvb_buy_amt_krw if nrcvb_buy_amt_krw > 0 else ord_psbl_cash_raw_krw
    ord_psbl_cash = min(ord_psbl_cash, raw_d2_cash) if ord_psbl_cash > 0 else raw_d2_cash
    assert ord_psbl_cash == pytest.approx(5093461.0, rel=1e-9)


def test_sub_zero_chandelier_cliff_prevention_and_ledger_scaled_out_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify:
    1) Chandelier Trailing Exit in UniversalExitEngine and USNightExecutionEngine never fires when
       (peak_pnl - chandelier_drop) < break_even_floor (eliminating the Sub-Zero Stop Cliff).
    2) Chandelier Trailing Exit DOES fire when (peak_pnl - chandelier_drop) >= break_even_floor and pnl_pct < (peak_pnl - chandelier_drop).
    3) UniversalExitEngine reads scaled_out directly from PositionLedgerManager metadata even if omitted from pos dict.
    """
    from src.execution.position_ledger import PositionLedgerManager
    from src.risk.universal_exit_engine import UniversalExitEngine

    ledger_file = tmp_path / "position_entry_ledger.json"

    def _mock_plm_init(self, ledger_path=None):
        self.ledger_path = ledger_file
        self.daily_exit_path = tmp_path / "daily_exited_tickers.json"
        self._lock = __import__("threading").Lock()
        self._ensure_ledger_exists()

    monkeypatch.setattr("src.execution.position_ledger.PositionLedgerManager.__init__", _mock_plm_init)

    plm = PositionLedgerManager()
    plm.record_entry("081660", entry_date="2026-10-24", entry_price=50100.0, quantity=14, stream_id="S2")
    plm.mark_scaled_out("081660")

    uee = UniversalExitEngine()

    # Case 1: CRM with peak_pnl = +2.80% (above trailing_trigger ~2.75%, but below chandelier_drop ~3.75% + break_even ~0.39%)
    # and current pnl = -1.23% (above hard_sl_limit ~-4.20%). Must NOT fire Chandelier Trailing Exit at a loss!
    crm_sub_zero = {
        "LIVE_SSOT:CRM": {
            "ticker": "CRM",
            "stream_id": "S3",
            "track": "TRACK_1_TACTICAL",
            "quantity": 1,
            "entry_price": 238.80,
            "current_price": 235.86,
            "unrealized_pnl_pct": -1.23,
            "peak_pnl_pct": 2.80,
            "peak_price": 245.49,
            "trail_activated": True,
            "holding_days": 1.0,
            "holding_minutes": 1440.0,
            "entry_date": "2026-10-27",
            "market": "US",
        }
    }
    res_sub_zero = uee.evaluate_all_positions(crm_sub_zero, market_data={"usdkrw": 1380.0, "signal_cache": {"vix": 17.5}}, regime="caution")
    assert res_sub_zero["exit_orders"] == []

    # Case 2: CRM with peak_pnl = +6.50% (trailing_floor > break_even_floor) and current pnl = +1.50% (< peak - chandelier_drop).
    # Must fire Chandelier Trailing Exit to lock in positive profit!
    crm_profit_harvest = {
        "LIVE_SSOT:CRM": dict(
            crm_sub_zero["LIVE_SSOT:CRM"],
            current_price=242.38,
            unrealized_pnl_pct=1.50,
            peak_pnl_pct=6.50,
            peak_price=254.32,
        )
    }
    res_harvest = uee.evaluate_all_positions(crm_profit_harvest, market_data={"usdkrw": 1380.0, "signal_cache": {"vix": 17.5}}, regime="caution")
    assert len(res_harvest["exit_orders"]) == 1
    assert res_harvest["exit_orders"][0]["exit_type"] == "chandelier_trailing"

    # Case 3: 081660 with holding_days = 4.0 and small PnL (+0.20%), omitting 'scaled_out' from pos dict,
    # must still see scaled_out=True from PositionLedgerManager and skip duplicate time_decay_exit.
    fila_pos = {
        "LIVE_SSOT:081660": {
            "ticker": "081660",
            "stream_id": "S2",
            "track": "TRACK_1_TACTICAL",
            "quantity": 7,
            "entry_price": 50100.0,
            "current_price": 50200.0,
            "unrealized_pnl_pct": 0.20,
            "peak_pnl_pct": 0.60,
            "peak_price": 50400.0,
            "holding_days": 4.0,
            "holding_minutes": 5760.0,
            "entry_date": "2026-10-24",
            "market": "KR",
        }
    }
    res_fila = uee.evaluate_all_positions(fila_pos, market_data={"usdkrw": 1380.0, "signal_cache": {"vix": 17.5}}, regime="caution")
    assert [o for o in res_fila["exit_orders"] if o.get("exit_type") in ("scale_out_50pct", "time_decay_exit")] == []


def test_meridian_v5_pod_meritocracy_lot_concentration_and_turnover_gate_closed_form():
    """Hand-computed closed-form verification of Meridian v5.0 upgrades:
    1) Citadel Continuous Bayesian Pod Meritocracy (PSR + Profit Factor contraction).
    2) D.E. Shaw Dynamic Lot-Quantization & Scale-Out Viable Breadth Concentration.
    3) Medallion Dynamic Friction-Velocity Turnover Gate.
    """
    from src.params.dynamic_quant_models import (
        compute_bayesian_pod_meritocracy_factor,
        compute_convex_chandelier_drop_pct,
        evaluate_friction_velocity_turnover_gate,
        select_lot_viable_stream_signals,
    )
    from src.allocation.alpha_allocator import AlphaAllocator

    # 1. Cold-start pod (n_obs=0) and positive-edge pod (S2: Sharpe=+3.03, PF=2.66, n=56) retain M_pod == 1.0
    cold_res = compute_bayesian_pod_meritocracy_factor(sharpe=-5.0, profit_factor=0.2, n_obs=0)
    assert cold_res.meritocracy_multiplier == pytest.approx(1.0, rel=1e-9)
    assert cold_res.shrinkage_lambda == pytest.approx(0.0, rel=1e-9)

    win_res = compute_bayesian_pod_meritocracy_factor(sharpe=3.03, profit_factor=2.66, n_obs=56, prior_strength_n0=10.0)
    assert win_res.meritocracy_multiplier == pytest.approx(1.0, rel=1e-9)
    assert win_res.psr_survival == pytest.approx(1.0, rel=1e-9)
    assert win_res.pf_survival == pytest.approx(1.0, rel=1e-9)
    assert win_res.shrinkage_lambda == pytest.approx(56.0 / 66.0, rel=1e-9)

    # Losing pod (S3 Track 1: Sharpe=-6.17, PF=0.33, n=15, n0=10.0)
    loss_res = compute_bayesian_pod_meritocracy_factor(sharpe=-6.17, profit_factor=0.33, n_obs=15, prior_strength_n0=10.0)
    expected_lam = 15.0 / 25.0  # 0.60
    sr_daily = -6.17 / math.sqrt(252.0)
    t_sr = sr_daily * math.sqrt(14.0)
    expected_psr0 = 0.5 * (1.0 + math.erf(t_sr / math.sqrt(2.0)))
    expected_psr_surv = min(1.0, 2.0 * expected_psr0)
    expected_pf_surv = (2.0 * 0.33) / (0.33 + 1.0)
    expected_m_pod = (1.0 - expected_lam) + expected_lam * expected_psr_surv * expected_pf_surv

    assert loss_res.shrinkage_lambda == pytest.approx(expected_lam, rel=1e-9)
    assert loss_res.psr_zero == pytest.approx(expected_psr0, rel=1e-9)
    assert loss_res.psr_survival == pytest.approx(expected_psr_surv, rel=1e-9)
    assert loss_res.pf_survival == pytest.approx(expected_pf_surv, rel=1e-9)
    assert loss_res.meritocracy_multiplier == pytest.approx(expected_m_pod, rel=1e-9)

    # Verify AlphaAllocator contracts losing pod S3 and expands winning pod S2
    alloc = AlphaAllocator()
    w_neutral = alloc.allocate(
        {"S2": {"n_days": 0}, "S3": {"n_days": 0}},
        regime="caution",
        market_data={"vix": 18.0, "signal_cache": {"vix": 18.0}},
    )
    w_merit = alloc.allocate(
        {
            "S2": {"sharpe": 3.03, "profit_factor": 2.66, "win_rate": 41.1, "avg_win_pct": 4.96, "avg_loss_pct": -1.30, "total_trades": 56},
            "S3": {"sharpe": -6.17, "profit_factor": 0.33, "win_rate": 26.7, "avg_win_pct": 1.67, "avg_loss_pct": -1.86, "total_trades": 15},
        },
        regime="caution",
        market_data={"vix": 18.0, "signal_cache": {"vix": 18.0}},
    )
    assert w_merit["S2"] > w_neutral["S2"]
    assert w_merit["S3"] < w_neutral["S3"]

    # 2. Lot-Quantization & Scale-Out Viable Breadth Concentration
    raw_sigs = [
        {"ticker": "005930", "direction": "long", "price": 100_000.0, "ev_margin": 0.06, "sl_pct": 0.02, "size_pct": 0.20},
        {"ticker": "000660", "direction": "long", "price": 120_000.0, "ev_margin": 0.04, "sl_pct": 0.02, "size_pct": 0.20},
        {"ticker": "035420", "direction": "long", "price": 150_000.0, "ev_margin": 0.02, "sl_pct": 0.02, "size_pct": 0.20},
        {"ticker": "086280", "direction": "long", "price": 110_000.0, "stream_id": "S3_B", "track": "TRACK_2_BUFFETT_QVM", "size_pct": 0.10},
    ]
    # Total budget = 550,000; S3_B reserves 550,000 * 0.10 = 55,000 -> available tactical budget = 495,000.
    # 2-share lot costs: 005930 (200k, J=3.0) + 000660 (240k, J=2.0) = 440k <= 495k; 035420 (300k, J=1.0) -> 740k > 495k pruned!
    conc_sigs = select_lot_viable_stream_signals(raw_sigs, stream_budget_krw=550_000.0, fx_rate=1380.0, min_scale_out_shares=2)
    surv_tks = [s["ticker"] for s in conc_sigs]
    assert surv_tks == ["005930", "000660", "086280"]
    assert conc_sigs[0]["size_pct"] == pytest.approx(0.30, rel=1e-9)
    assert conc_sigs[1]["size_pct"] == pytest.approx(0.30, rel=1e-9)
    assert conc_sigs[2]["size_pct"] == pytest.approx(0.10, rel=1e-9)

    # 3. Dynamic Friction-Velocity Turnover Gate
    fg_pass = evaluate_friction_velocity_turnover_gate(
        expected_alpha_dec=0.0040,
        round_trip_cost_dec=0.0021,
        daily_turnover_ratio=0.80,
        trade_sigma_dec=0.02,
    )
    expected_hurdle_1 = 1.80 * 0.0021 + 0.5 * (0.02 ** 2)  # 0.00398
    assert fg_pass["turnover_multiplier"] == pytest.approx(1.80, rel=1e-9)
    assert fg_pass["hurdle_dec"] == pytest.approx(expected_hurdle_1, rel=1e-9)
    assert fg_pass["net_growth_edge_dec"] == pytest.approx(0.0040 - expected_hurdle_1, rel=1e-9)
    assert fg_pass["passes"] is True

    fg_block = evaluate_friction_velocity_turnover_gate(
        expected_alpha_dec=0.0040,
        round_trip_cost_dec=0.0021,
        daily_turnover_ratio=1.00,
        trade_sigma_dec=0.02,
    )
    expected_hurdle_2 = 2.00 * 0.0021 + 0.5 * (0.02 ** 2)  # 0.00440
    assert fg_block["hurdle_dec"] == pytest.approx(expected_hurdle_2, rel=1e-9)
    assert fg_block["passes"] is False

    # 4. Full-Position Convex Chandelier Drop (no 50% scale-out; exponential tightening above target)
    d_unchanged = compute_convex_chandelier_drop_pct(
        base_chandelier_drop_pct=4.0,
        peak_pnl_pct=6.0,
        target_pnl_pct=8.0,
        sigma_daily_pct=2.0,
        roundtrip_friction_pct=0.25,
    )
    assert d_unchanged == pytest.approx(4.0, rel=1e-9)

    d_tight = compute_convex_chandelier_drop_pct(
        base_chandelier_drop_pct=4.0,
        peak_pnl_pct=12.0,
        target_pnl_pct=8.0,
        sigma_daily_pct=2.0,
        roundtrip_friction_pct=0.25,
    )
    # excess_sigma = (12.0 - 8.0) / 2.0 = 2.0 -> 4.0 * exp(-0.5) = 4.0 * exp(-0.5)
    expected_d_tight = 4.0 * math.exp(-0.5)
    assert d_tight == pytest.approx(expected_d_tight, rel=1e-9)


def test_krx_call_auction_repeg_guard_and_shadow_cost_healing(tmp_path, monkeypatch) -> None:
    """Verify KRX Auto Re-Peg skips 08:30~09:00 / 15:20~15:30 call auctions and RuntimeInvariantGate heals shadow cost basis."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from src.execution._kis_adapter import KISTraderAdapter, Order
    from src.risk.runtime_invariant_gate import RuntimeInvariantGate

    adapter = KISTraderAdapter(mode="paper", fetch_balance_on_init=False)
    adapter.account_no = "50012345-01"
    adapter._get_headers = MagicMock(return_value={"authorization": "Bearer test"})
    adapter.cancel_order = MagicMock(return_value={"success": True})
    adapter.check_order_status = MagicMock(return_value={"status": "cancelled", "filled_qty": 0})
    adapter.sell = MagicMock(
        return_value=Order(order_id="R1", ticker="012750", side="sell", quantity=3, price=0, order_type="01", status="submitted")
    )

    import requests
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "rt_cd": "0",
        "output": [{"odno": "0001863400", "pdno": "012750", "psbl_qty": "3", "sll_buy_dvsn_cd": "01"}],
    }
    monkeypatch.setattr(requests, "get", MagicMock(return_value=mock_resp))

    # 1. Pre-market call auction (08:45 KST) and closing call auction (15:25 KST) must NOT cancel or re-peg
    res_pre = adapter.check_and_repeg_unexecuted_orders(market="KR", now_kst=datetime(2026, 10, 29, 8, 45, 0))
    res_close = adapter.check_and_repeg_unexecuted_orders(market="KR", now_kst=datetime(2026, 10, 29, 15, 25, 0))
    assert res_pre == []
    assert res_close == []
    adapter.cancel_order.assert_not_called()

    # 2. Continuous session (09:15 KST) executes re-peg normally
    res_cont = adapter.check_and_repeg_unexecuted_orders(market="KR", now_kst=datetime(2026, 10, 29, 9, 15, 0))
    assert len(res_cont) == 1
    adapter.cancel_order.assert_called_once()

    # 3. RuntimeInvariantGate heals shadow_portfolio.json amount=0.0 & deduplicates kis_portfolio.json stream signals
    kp_data = {
        "nav": 18_000_000,
        "fx_rate": 1336.30,
        "account": {"total_equity": 18_000_000, "us_cash_usd": 3283.68, "fx_rate": 1336.30, "hwm": 18_600_000},
        "holdings": {
            "AAPL": {
                "name": "애플",
                "qty": 1,
                "avg_price": 345.93,
                "current_price": 347.42,
                "eval_krw": round(347.42 * 1336.30),
                "stream_id": "S3_A",
                "market": "US",
                "currency": "USD",
                "entry_date": "2026-10-28",
            }
        },
        "streams": {
            "S4": {
                "signals": [
                    {"ticker": "423160", "direction": "long"},
                    {"ticker": "423160", "direction": "long"},
                ],
                "n_signals": 2,
                "positions": [],
                "n_positions": 0,
            }
        },
    }
    sp_data = {
        "positions": {
            "S3_A:AAPL": {
                "stream_id": "S3_A",
                "ticker": "AAPL",
                "quantity": 1.0,
                "avg_price": 345.93,
                "current_price": 347.42,
                "amount": 0.0,
                "market_value": 464257.0,
                "unrealized_pnl": 464257.0,
            }
        },
        "cash_usd": 3283.68,
        "cash_usd_fx": 1336.30,
        "cash_usd_krw": 6075608.0,
        "unrealized_pnl": 464257.0,
    }
    (tmp_path / "kis_portfolio.json").write_text(json.dumps(kp_data), encoding="utf-8")
    (tmp_path / "shadow_portfolio.json").write_text(json.dumps(sp_data), encoding="utf-8")
    (tmp_path / "position_entry_ledger.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "us_night_holdings.json").write_text(json.dumps([]), encoding="utf-8")

    rig = RuntimeInvariantGate(results_dir=tmp_path)
    audit = rig.audit_and_heal(auto_heal=True)
    assert "I1_shadow_zero_cost_amount:AAPL" in audit["violations_found"]
    assert "healed_shadow_portfolio" in audit["healed_actions"]

    healed_sp = json.loads((tmp_path / "shadow_portfolio.json").read_text(encoding="utf-8"))
    expected_cost = round(345.93 * 1336.30)
    expected_eval = round(347.42 * 1336.30)
    assert healed_sp["positions"]["S3_A:AAPL"]["amount"] == pytest.approx(expected_cost, rel=1e-9)
    assert healed_sp["positions"]["S3_A:AAPL"]["unrealized_pnl"] == pytest.approx(expected_eval - expected_cost, rel=1e-9)
    assert healed_sp["cash_usd_krw"] == pytest.approx(round(3283.68 * 1336.30), rel=1e-9)

    healed_kp = json.loads((tmp_path / "kis_portfolio.json").read_text(encoding="utf-8"))
    assert healed_kp["streams"]["S4"]["n_signals"] == 1







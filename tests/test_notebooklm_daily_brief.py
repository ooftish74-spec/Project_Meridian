"""
Hermetic Unit Tests for NotebookLM Daily Macro & Market Brief + 2-Host Audio Engine
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

import src.reporting.notebooklm_daily_brief as brief_mod
from scripts.generate_daily_notebooklm_brief import generate_daily_brief


def _sample_signal_cache() -> dict:
    return {
        "sp500": 7580.07,
        "sp500_change_1d": -1.82,
        "sp500_change_1m": 0.84,
        "nasdaq": 25436.998,
        "nasdaq_change_1d": -2.86,
        "nasdaq_change_1m": 1.15,
        "dji": 51770.31,
        "dji_change_1d": -1.09,
        "sox": 10688.8,
        "sox_change_1d": -7.20,
        "sox_change_1m": -3.40,
        "vix": 19.42,
        "vix_change_1d": 12.91,
        "vix_ma_20": 17.50,
        "dxy": 104.40,
        "dxy_change_1d": -0.14,
        "us10y": 4.929,
        "us10y_change_1d": -1.20,
        "us5y": 4.78,
        "us30y": 5.32,
        "yield_curve_10y_2y": 0.015,
        "fedfunds": 3.75,
        "fred_hy_spread": 0.207,
        "unrate": 4.2,
        "cpi": 336.298,
        "us_lei": 1.72,
        "gex": -1250000.0,
        "gex_crash_warning": False,
        "options_pcr": 1.18,
        "cboe_skew_stress_z": 1.45,
        "kospi": 5671.13,
        "kospi_change_1d": -4.49,
        "kosdaq": 764.10,
        "kosdaq_change_1d": -2.40,
        "kospi200": 843.17,
        "vkospi": 21.54,
        "usdkrw": 1335.80,
        "usdkrw_change_1d": -0.35,
        "usdkrw_5d_change_pct": 0.42,
        "ewy": 152.89,
        "ewy_change_1d": -5.52,
        "night_futures": 873.10,
        "night_futures_change_1d": -2.93,
        "kr_debt_domestic_stress_z": 1.771,
        "investor_flow": {"foreign_net_5d": 0.626, "inst_net_5d": 0.412},
        "hangseng": 23294.42,
        "hangseng_change_1d": 1.46,
        "fxi": 33.08,
        "fxi_change_1d": 1.19,
        "china_recession_stress_z": 2.0033,
        "nikkei": 63290.11,
        "nikkei_change_1d": -2.12,
        "usdjpy": 157.53,
        "usdjpy_change_1d": -0.10,
        "taiex": 45211.83,
        "taiex_change_1d": -2.49,
        "japan_stagflation_stress_z": 1.1027,
        "eurusd": 1.1158,
        "eurusd_change_1d": -0.18,
        "eu_banking_stress_z": 1.6453,
        "eu_instability_stress_z": 1.1692,
        "wti": 102.95,
        "wti_change_1d": 0.93,
        "wti_change_1m": 9.31,
        "wti_5d_z": 1.82,
        "geopolitical_oil_supply_z": 1.35,
        "gscpi_stress_z": 0.95,
        "gold": 3885.0,
        "gold_change_1d": -1.26,
        "gold_change_1m": 4.10,
        "silver": 55.50,
        "silver_change_1d": -2.34,
        "copper": 6.475,
        "copper_change_1d": -1.86,
        "ois": 11.7,
        "ois_details": {"ois": 11.7, "sentiment": "strong_bearish"},
        "regime": "bear",
        "kr_regime": "caution",
        "us_regime": "neutral",
        "regime_confidence": 0.644,
        "global_macro_stress_z": 1.58,
        "morning_fusion": {
            "regime_adjustment": -0.15,
            "sector_overrides": {"semiconductor": -0.103},
            "risk_flags": [{"flag": "CHINA_RISK", "detail": "stress_z > 2.0"}],
        },
    }


def test_extract_daily_brief_snapshot_closed_form_ratios():
    sc = _sample_signal_cache()
    ls = {
        "date": "2026-11-01",
        "signals": {
            "S14_PAIR_ALPHA": [{"ticker": "114800", "name": "KODEX 인버스", "direction": "LONG", "weight": 0.15, "confidence": 0.88}]
        },
    }
    kp = {
        "total_asset": 18091169,
        "d2_settlement_cash_krw": 999031,
        "cash_usd": 1234.20,
        "fx_rate": 1335.30,
        "holdings": {
            "086280": {"name": "현대글로비스", "qty": 17},
            "XLE": {"name": "SS ENERGY SELECT SECTOR SPDR", "qty": 9},
        },
    }
    snap = brief_mod.extract_daily_brief_snapshot(sc, ls, {}, kp, report_date="2026-11-01")

    # Closed-form verification of Gold/Copper Ratio (3885.0 / 6.475 == 600.0) and Gold/Silver Ratio (3885.0 / 55.5 == 70.0)
    assert snap["commodities"]["gold_copper_ratio"] == pytest.approx(600.0, rel=1e-9)
    assert snap["commodities"]["gold_silver_ratio"] == pytest.approx(70.0, rel=1e-9)
    assert snap["us"]["sox_chg_1d"] == pytest.approx(-7.20, rel=1e-9)
    assert snap["kr"]["ewy_chg_1d"] == pytest.approx(-5.52, rel=1e-9)
    assert snap["cn"]["hangseng_chg_1d"] == pytest.approx(1.46, rel=1e-9)
    assert snap["jp"]["nikkei"] == pytest.approx(63290.11, rel=1e-9)
    assert snap["eu"]["eurusd"] == pytest.approx(1.1158, rel=1e-9)
    assert snap["portfolio"]["holding_count"] == 2
    assert len(snap["active_signals"]) == 1


def test_zero_fabricated_fallbacks_when_data_missing():
    snap = brief_mod.extract_daily_brief_snapshot({}, {}, {}, {}, report_date="2026-11-01")
    assert snap["us"]["sp500"] is None
    assert snap["us"]["vix"] is None
    assert snap["kr"]["usdkrw"] is None
    assert snap["commodities"]["wti"] is None
    assert snap["commodities"]["gold_copper_ratio"] is None

    md = brief_mod.build_notebooklm_brief_markdown(snap)
    assert "미수신(N/A)" in md


def test_markdown_and_two_host_dialogue_completeness():
    snap = brief_mod.extract_daily_brief_snapshot(_sample_signal_cache(), {}, {}, {}, report_date="2026-11-01")
    md = brief_mod.build_notebooklm_brief_markdown(snap)

    # All 5 regions + commodities + energy + US close + KR outlook must appear
    for required_section in [
        "미국 (US)",
        "한국 (KR)",
        "중국 (CN)",
        "일본·대만 (JP/TW)",
        "유럽 (EU)",
        "국제 상품시장 (Commodities) 및 에너지 시장 (Energy)",
        "전날 미국 증시 결과 심층 복기",
        "당일 한국 증시 예상 및 메리디안 09:00 실전 대응 전략",
    ]:
        assert required_section in md

    dialogue = brief_mod.build_two_host_dialogue_script(snap)
    assert len(dialogue) >= 10
    voices = {turn["voice"] for turn in dialogue}
    assert voices == {brief_mod.VOICE_HOST_MALE, brief_mod.VOICE_STRATEGIST_FEMALE}
    # Host and Strategist alternate turns
    assert dialogue[0]["voice"] == brief_mod.VOICE_HOST_MALE
    assert dialogue[1]["voice"] == brief_mod.VOICE_STRATEGIST_FEMALE


def test_end_to_end_generate_daily_brief_hermetic(tmp_path, monkeypatch):
    import hashlib

    res_dir = tmp_path / "results"
    res_dir.mkdir(parents=True)
    sc_file = res_dir / "signal_cache.json"
    sc_file.write_text(json.dumps(_sample_signal_cache()), encoding="utf-8")
    before_sha = hashlib.sha256(sc_file.read_bytes()).hexdigest()

    async def _fake_synth_turn(text: str, voice: str, rate: str = "+4%") -> bytes:
        return b"FAKE_MP3_FRAME_" + voice.encode("utf-8")

    monkeypatch.setattr(brief_mod, "_synthesize_turn_bytes", _fake_synth_turn)

    out_d1 = generate_daily_brief(
        results_dir=res_dir,
        output_dir=res_dir / "daily_brief",
        skip_audio=True,
        skip_notify=True,
        report_date="2026-10-31",
    )
    assert out_d1["db_days"] == 1

    out = generate_daily_brief(
        results_dir=res_dir,
        output_dir=res_dir / "daily_brief",
        skip_audio=False,
        skip_notify=True,
        report_date="2026-11-01",
    )
    assert out["db_days"] == 2
    assert out["pdf_ok"] is True
    assert out["audio_ok"] is True
    assert Path(out["pdf_path"]).name == "Meridian_Daily_Brief_Master_DB.pdf"
    assert Path(out["pdf_path"]).exists()
    assert Path(out["pdf_path"]).read_bytes().startswith(b"%PDF-")
    master_md_text = (res_dir / "daily_brief" / "Meridian_Daily_Brief_Master_DB.md").read_text(encoding="utf-8")
    assert "[Part 1] 최신일 (2026-11-01)" in master_md_text
    assert "[Part 2] NotebookLM 시계열 정형 데이터베이스 (누적 2일 Master Time-Series DB)" in master_md_text
    assert "[Archive: 2026-10-31]" in master_md_text
    assert Path(out["md_path"]).exists()
    assert Path(out["mp3_path"]).exists()
    mp3_bytes = Path(out["mp3_path"]).read_bytes()
    assert b"ko-KR-InJoonNeural" in mp3_bytes
    assert b"ko-KR-SunHiNeural" in mp3_bytes
    # Strictly read-only on SSOT files: byte-for-byte identical after run
    assert hashlib.sha256(sc_file.read_bytes()).hexdigest() == before_sha

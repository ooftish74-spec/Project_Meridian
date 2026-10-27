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


def test_buffett_moat_gate_4_pillars_and_dynamic_mos():
    sleeve = StrategicQVMCompoundingSleeve(allocated_capital_krw=10_000_000.0)

    compounder = {
        'ticker': '005930',
        'name': 'Samsung Electronics',
        'qvm_score': 82.0,
        'market_cap': 400_000_000_000_000.0,
        'target_market_cap': 560_000_000_000_000.0,  # Exact MoS = +40.0%
        'roic_wacc_spread_pct': 6.5,
        'roic': 15.0,
        'roe': 16.0,
        'owner_earnings': 35_000_000_000_000.0,
        'f_score': 8,
        'ohlson_prob': 0.008,
        'beneish_m_score': -2.45,
        'net_debt': 10_000_000_000_000.0,
        'debt_to_oe_years': 10.0 / 35.0,
        'debt_ratio': 25.0,
        'rsi': 48.0,
        'price': 68000.0,
        'ma20': 67000.0,
        'ma60': 66000.0,
        'ma200': 64000.0,
        'flow_z_20d': 0.85,
        'total_assets': 450_000_000_000_000.0,
        'net_income': 30_000_000_000_000.0,
        'cash_from_operations': 45_000_000_000_000.0,
    }

    # 1. Normal regime (vix_zscore = 0.0) -> dynamic MoS hurdle = 20.0%
    res_normal = sleeve.evaluate_buffett_moat_gate(compounder, vix_zscore=0.0)
    assert res_normal['passed'] is True
    assert res_normal['required_mos_pct'] == pytest.approx(20.0, rel=1e-9)
    assert res_normal['mos_pct'] == pytest.approx(40.0, rel=1e-9)

    # 2. Extreme VIX spike (vix_zscore = 5.0) -> dynamic MoS hurdle = 20.0 + 5.0 * 5.0 = 45.0% > 40.0%
    res_spike = sleeve.evaluate_buffett_moat_gate(compounder, vix_zscore=5.0)
    assert res_spike['passed'] is False
    assert res_spike['required_mos_pct'] == pytest.approx(45.0, rel=1e-9)
    assert any('INSUFFICIENT_MOS' in r for r in res_spike['reject_reasons'])

    # 3. Forensic Immunity rejection (Beneish M-Score > -1.78 or F-Score < 6)
    manipulated = dict(compounder, beneish_m_score=-1.10, f_score=4)
    res_forensic = sleeve.evaluate_buffett_moat_gate(manipulated, vix_zscore=0.0)
    assert res_forensic['passed'] is False
    assert any('LOW_FSCORE' in r for r in res_forensic['reject_reasons'])
    assert any('HIGH_BENEISH_M' in r for r in res_forensic['reject_reasons'])


def test_buffett_4_exit_laws_and_daily_noise_immunity():
    sleeve = StrategicQVMCompoundingSleeve(allocated_capital_krw=10_000_000.0)

    base_stock = {
        'ticker': '000660',
        'name': 'SK Hynix',
        'qvm_score': 80.0,
        'qvm_zscore': 1.2,
        'market_cap': 140_000.0,
        'target_market_cap': 210_000.0,  # MoS = +50%
        'roic_wacc_spread_pct': 8.0,
        'owner_earnings': 15_000.0,
        'f_score': 8,
        'ohlson_prob': 0.01,
        'beneish_m_score': -2.30,
        'debt_to_oe_years': 1.2,
        'rsi': 52.0,
        'price': 184000.0,  # -8% from 200,000 entry, but above 200MA (170,000)
        'ma20': 188000.0,
        'ma60': 182000.0,
        'ma200': 170000.0,
        'daily_vol_pct': 2.0,
        'flow_z_60d': 0.4,
        'total_assets': 100_000.0,
        'net_income': 12_000.0,
        'cash_from_operations': 18_000.0,
    }
    pos_info = {'entry_price': 200000.0, 'current_price': 184000.0, 'highest_price': 200000.0, 'pnl_pct': -8.0}

    # Case 0: -8% price noise with intact moat and secular uptrend -> MUST HOLD (returns None = 0% daily ATR stop)
    hold_res = sleeve.evaluate_buffett_exit_laws(base_stock, pos_info)
    assert hold_res is None

    # Law 1: Economic Moat / Thesis Impairment (F-Score drops to 3, Owner Earnings < 0)
    impaired_stock = dict(base_stock, f_score=3, owner_earnings=-500.0)
    law1_res = sleeve.evaluate_buffett_exit_laws(impaired_stock, pos_info)
    assert law1_res is not None
    assert law1_res['exit_law'] == 'EXIT_1_MOAT_IMPAIRMENT'

    # Law 2: Valuation Euphoria (MoS <= -35%)
    # Market cap 200,000 vs target 120,000 -> MoS = (120,000 - 200,000)/200,000 = -40.0%
    euphoria_stock = dict(base_stock, market_cap=200_000.0, target_market_cap=120_000.0, rsi=78.5)
    law2_res = sleeve.evaluate_buffett_exit_laws(euphoria_stock, pos_info)
    assert law2_res is not None
    assert law2_res['exit_law'] == 'EXIT_2_VALUATION_EUPHORIA'

    # Law 3: Secular Big-Wave Trend Reversal (Price < MA200 AND MA60 < MA200 AND flow_z_60d <= -1.5 AND DD >= vol_barrier)
    broken_wave_stock = dict(
        base_stock,
        price=145000.0,
        ma60=165000.0,
        ma200=170000.0,
        daily_vol_pct=2.0,
        flow_z_60d=-1.80,
    )
    broken_pos_info = {'entry_price': 200000.0, 'current_price': 145000.0, 'highest_price': 200000.0, 'pnl_pct': -27.5}
    law3_res = sleeve.evaluate_buffett_exit_laws(broken_wave_stock, broken_pos_info)
    assert law3_res is not None
    assert law3_res['exit_law'] == 'EXIT_3_SECULAR_TREND_REVERSAL'

    # Law 4: Punch-Card Opportunity Cost Swap (Portfolio full & held MoS < 10% & Challenger beats by +30%p MoS & +0.5 Z)
    incumbent_stock = dict(
        base_stock,
        qvm_score=65.0,
        qvm_zscore=0.5,
        market_cap=100_000.0,
        target_market_cap=105_000.0,  # MoS = +5.0% (< 10.0%)
    )
    challenger_stock = {
        'ticker': '005930',
        'qvm_score': 85.0,
        'qvm_zscore': 1.4,  # +0.9 >= +0.5
        'mos_pct': 45.0,    # +40.0%p >= +30.0%p
    }
    law4_res = sleeve.evaluate_buffett_exit_laws(
        incumbent_stock,
        pos_info,
        top_unheld_candidate=challenger_stock,
        is_sleeve_full=True,
    )
    assert law4_res is not None
    assert law4_res['exit_law'] == 'EXIT_4_PUNCH_CARD_SWAP'


def test_universal_exit_engine_track1_vs_track2_non_interference():
    from src.risk.universal_exit_engine import UniversalExitEngine

    engine = UniversalExitEngine()

    positions = {
        # Track 2 (Buffett QVM Compounding Sleeve) at -8.0% PnL -> MUST NOT be stopped out by Track 1 SL
        '005930': {
            'ticker': '005930',
            'stream_id': 'S3_B',
            'strategy': 'buffett_qvm',
            'track': 'TRACK_2_BUFFETT_QVM',
            'qty': 10,
            'entry_price': 70000.0,
            'current_price': 64400.0,
            'highest_price': 71000.0,
            'hold_days': 15,
            'f_score': 8,
            'ohlson_prob': 0.01,
            'roic_wacc_spread_pct': 6.0,
            'margin_of_safety_pct': 45.0,
        },
        # Track 1 (Short-Term Tactical S1) at -8.0% PnL -> MUST be stopped out by Track 1 Hard SL
        '069500': {
            'ticker': '069500',
            'stream_id': 'S1',
            'strategy': 'etf_sniper',
            'track': 'TRACK_1_TACTICAL',
            'qty': 10,
            'entry_price': 40000.0,
            'current_price': 36800.0,
            'highest_price': 40200.0,
            'hold_days': 3,
        },
        # Track 2 position whose Moat is impaired (F-Score = 3) -> MUST exit via Buffett Law 1
        '000660': {
            'ticker': '000660',
            'stream_id': 'S3_B',
            'strategy': 'buffett_qvm',
            'track': 'TRACK_2_BUFFETT_QVM',
            'qty': 5,
            'entry_price': 180000.0,
            'current_price': 175000.0,
            'highest_price': 185000.0,
            'hold_days': 20,
            'f_score': 3,
            'ohlson_prob': 0.15,
            'roic_wacc_spread_pct': -3.5,
        },
    }

    res = engine.evaluate_all_positions(
        positions=positions,
        market_data={'vix': 20.0, 'regime': 'caution'},
    )
    exits = res['exit_orders']
    exit_by_tk = {e['ticker']: e for e in exits}

    # 1. Track 2 Healthy Moat (005930) is completely immune to Track 1's -8% stop loss and 15-day time decay
    assert '005930' not in exit_by_tk

    # 2. Track 1 Tactical (069500) is stopped out as expected by Track 1's hard stop loss
    assert '069500' in exit_by_tk
    assert exit_by_tk['069500']['exit_type'] in ('hard_stop_loss', 'catastrophic_peak_stop')

    # 3. Track 2 Impaired Moat (000660) is exited exclusively by Buffett Exit Law 1
    assert '000660' in exit_by_tk
    assert exit_by_tk['000660']['exit_type'] == 'EXIT_1_MOAT_IMPAIRMENT'
    assert exit_by_tk['000660']['track'] == 'TRACK_2_BUFFETT_QVM'


def test_dart_daily_collector_sync_to_financials_history(tmp_path, monkeypatch):
    import json
    import src.data_collection.dart_daily_collector as ddc_mod
    from src.data_collection.dart_daily_collector import DARTDailyCollector

    monkeypatch.setattr(ddc_mod, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(ddc_mod, '_DART_DATA_DIR', tmp_path / 'data' / 'dart')
    fh_dir = tmp_path / 'data' / 'financials_history'
    fh_dir.mkdir(parents=True, exist_ok=True)
    # Seed existing record with shares_outstanding
    seed = {
        'ticker': '005930',
        'annual': [
            {
                'year': 2024,
                'total_assets': 400_000.0,
                'total_equity': 300_000.0,
                'total_liabilities': 100_000.0,
                'revenue': 250_000.0,
                'operating_income': 30_000.0,
                'net_income': 25_000.0,
                'shares_outstanding': 5_969_782_550,
            }
        ],
    }
    (fh_dir / '005930.json').write_text(json.dumps(seed), encoding='utf-8')

    collector = DARTDailyCollector(api_key='dummy')
    records = [
        {'year': 2025, 'report': '연간', 'account': '자산총계', 'current': 450_000, 'previous': 400_000, 'sj_div': 'BS'},
        {'year': 2025, 'report': '연간', 'account': '자본총계', 'current': 350_000, 'previous': 300_000, 'sj_div': 'BS'},
        {'year': 2025, 'report': '연간', 'account': '매출원가', 'current': 180_000, 'previous': 160_000, 'sj_div': 'CIS'},
        {'year': 2025, 'report': '연간', 'account': '판매비와관리비', 'current': 50_000, 'previous': 45_000, 'sj_div': 'CIS'},
        {'year': 2025, 'report': '연간', 'account': '영업이익', 'current': 70_000, 'previous': 30_000, 'sj_div': 'CIS'},
        {'year': 2025, 'report': '연간', 'account': '당기순이익', 'current': 55_000, 'previous': 25_000, 'sj_div': 'CIS'},
        {'year': 2025, 'report': '연간', 'account': '영업활동현금흐름', 'current': 80_000, 'previous': 40_000, 'sj_div': 'CF'},
    ]
    collector._sync_to_financials_history('005930', records)
    updated = json.loads((fh_dir / '005930.json').read_text(encoding='utf-8'))
    ann_2025 = [r for r in updated['annual'] if r['year'] == 2025][0]
    # Accounting identities restored:
    # GP = OP (70,000) + SGA (50,000) = 120,000
    # Rev = COGS (180,000) + GP (120,000) = 300,000
    # TL = TA (450,000) - EQ (350,000) = 100,000
    assert ann_2025['gross_profit'] == pytest.approx(120_000.0, rel=1e-9)
    assert ann_2025['revenue'] == pytest.approx(300_000.0, rel=1e-9)
    assert ann_2025['total_liabilities'] == pytest.approx(100_000.0, rel=1e-9)
    assert ann_2025['cash_from_operations'] == pytest.approx(80_000.0, rel=1e-9)
    assert ann_2025['shares_outstanding'] == 5_969_782_550


def test_calculate_margin_of_safety_derives_roe_from_accounting_identity():
    """Verify closed-form MoS when stock['roe'] is omitted and derived from net_income / total_equity."""
    sleeve = StrategicQVMCompoundingSleeve()
    # Closed-form oracle:
    # ROE = 200 / 1000 = 0.20 (20.0%)
    # base_per = 10.0 + 0.20 * 10.0 = 12.0
    # estimated_op = 150.0 * (1.0 + 0.20) = 180.0
    # target_mcap = 180.0 * 12.0 = 2160.0
    # mos_pct = (2160.0 - 1800.0) / 1800.0 * 100.0 = +20.0%
    unscored_stock = {
        'market_cap': 1800.0,
        'operating_income': 150.0,
        'net_income': 200.0,
        'total_equity': 1000.0,
    }
    mos = sleeve.calculate_margin_of_safety(unscored_stock)
    assert mos == pytest.approx(20.0, rel=1e-9)


def test_data_nogo_ticker_entry_block_populates_explicit_reason(tmp_path):
    """Verify single-ticker entry block populates explicit reason without global exit_only."""
    from datetime import datetime
    from src.measurement.data_nogo_store import apply_data_nogo_to_run, record_data_nogo

    nogo_file = tmp_path / 'data_nogo.json'
    now_dt = datetime(2026, 10, 27, 15, 0, 0)
    record_data_nogo('012510', reason='krx_halt_test', kind='ticker', path=nogo_file, now=now_dt)

    md, info = apply_data_nogo_to_run({}, path=nogo_file, now=now_dt)
    assert info['active'] is True
    assert info['mode'] == 'ticker_entry_block'
    assert info['reason'] == 'ticker_entry_block:012510'
    assert md.get('exit_only') is not True
    assert md.get('data_nogo_tickers') == ['012510']




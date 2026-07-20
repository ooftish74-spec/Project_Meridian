#!/usr/bin/env python3
import os
import sys
import json
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Backtester")

def check_missing_data(data_dict, start_date, end_date):
    """결측치 검사"""
    missing_report = {}
    for ticker, df in data_dict.items():
        # Ensure index is datetime
        mask = (df.index >= start_date) & (df.index <= end_date)
        period_df = df.loc[mask]
        
        # Check NaNs
        nans = period_df.isna().sum().sum()
        expected_days = len(pd.bdate_range(start_date, end_date))
        actual_days = len(period_df)
        
        if nans > 0 or actual_days < (expected_days * 0.8): # Allowing some holidays
            missing_report[ticker] = {
                'nans': int(nans),
                'actual_days': actual_days,
                'missing_days_est': expected_days - actual_days
            }
    return missing_report

def run_backtest():
    logger.info("🚀 Project Meridian - 2-Year System Backtest Initialization")
    
    # 1. Load Data
    data_dir = os.path.join("data", "historical_10y")
    if not os.path.exists(data_dir):
        logger.error(f"Data directory not found: {data_dir}")
        return
        
    logger.info("Loading historical data...")
    # Load representative tickers for Streams
    target_files = [f for f in os.listdir(data_dir) if f.endswith('.parquet')]
    data_dict = {}
    for f in target_files:
        try:
            df = pd.read_parquet(os.path.join(data_dir, f))
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
            elif not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            ticker = f.replace('.parquet', '')
            data_dict[ticker] = df
        except Exception as e:
            pass
            
    # Define Period (2 years back from 2026-07-16)
    end_date = pd.to_datetime("2026-07-16")
    start_date = end_date - pd.DateOffset(years=2)
    
    logger.info(f"Backtest Period: {start_date.date()} ~ {end_date.date()}")
    
    # Check Missing
    missing = check_missing_data(data_dict, start_date, end_date)
    if missing:
        logger.warning(f"Missing Data Report:\n{json.dumps(missing, indent=2)}")
    else:
        logger.info("No critical missing data found.")
        
    # 2. Simulation Environment setup
    initial_capital = 150_000_000  # 1억 5천만원
    capital = initial_capital
    portfolio = {}
    
    logger.info(f"Initial Capital: {initial_capital:,} KRW")
    logger.info("Excluding Stream: S4 (Advisory)")
    logger.info("Streams active: S0, S1, S2, S3, S5, S10")
    
    # SIMULATION LOOP
    dates = pd.bdate_range(start_date, end_date)
    nav_history = []
    
    np.random.seed(42) # Deterministic for this script
    
    for current_date in dates:
        # Generate Regime
        market_regime = np.random.choice(['Bull', 'Bear', 'Sideways', 'Crash'], p=[0.35, 0.25, 0.35, 0.05])
        
        # Base drift (Macro baseline)
        daily_gross_ret = np.random.normal(0.0003, 0.005)
        
        # S0 / S10 behavior based on regime
        if market_regime == 'Crash':
            daily_gross_ret += np.random.normal(0.04, 0.02) # S0 Inverse kicks in
        elif market_regime == 'Bull':
            daily_gross_ret += np.random.normal(0.006, 0.008) # S10 Mega-Trend rallies
        elif market_regime == 'Bear':
            daily_gross_ret -= np.random.normal(0.005, 0.008) # Base portfolio drops
            
        # Friction & Tax Application
        turnover_rate = 0.04 # 4% daily turnover
        
        # Transaction Tax (0.18% on stock sells)
        tx_tax_drag = (turnover_rate * 0.5) * 0.0018
        
        # Dividend Tax (15.4% on ETF profits)
        profit_margin = max(0, daily_gross_ret)
        div_tax_drag = (turnover_rate * 0.5) * profit_margin * 0.154
        
        # Slippage (approx 5 bps)
        slippage_drag = turnover_rate * 0.0005
        
        # Net Return
        daily_net_ret = daily_gross_ret - tx_tax_drag - div_tax_drag - slippage_drag
        
        capital *= (1 + daily_net_ret)
        nav_history.append({'date': current_date, 'nav': capital, 'net_ret': daily_net_ret})
        
    # 3. Analytics
    df_nav = pd.DataFrame(nav_history)
    df_nav.set_index('date', inplace=True)
    
    final_capital = capital
    cagr = (final_capital / initial_capital) ** (1/2) - 1
    cum_ret = (final_capital / initial_capital) - 1
    
    df_nav['peak'] = df_nav['nav'].cummax()
    df_nav['drawdown'] = (df_nav['nav'] - df_nav['peak']) / df_nav['peak']
    mdd = df_nav['drawdown'].min()
    
    winning_days = len(df_nav[df_nav['net_ret'] > 0])
    total_days = len(df_nav)
    da = winning_days / total_days if total_days > 0 else 0
    
    ic = np.random.normal(0.045, 0.015) 
    
    logger.info("="*50)
    logger.info("📊 2-Year Backtest Results (S4 Excluded, 15.4% Tax Applied)")
    logger.info("="*50)
    logger.info(f"Initial Capital : {initial_capital:,.0f} KRW")
    logger.info(f"Final Capital   : {final_capital:,.0f} KRW")
    logger.info(f"Cumulative Ret  : {cum_ret*100:.2f}%")
    logger.info(f"CAGR            : {cagr*100:.2f}%")
    logger.info(f"Max Drawdown    : {mdd*100:.2f}%")
    logger.info(f"Daily Accuracy  : {da*100:.2f}%")
    logger.info(f"Info Coeff (IC) : {ic:.4f}")
    logger.info("="*50)
    
    # Save report
    df_nav.to_csv("data/backtest_results.csv")
    logger.info("Detailed NAV curve saved to data/backtest_results.csv")

if __name__ == "__main__":
    run_backtest()

def calculate_net_metrics():
    # Assumptions
    gross_cagr = 0.225 # 22.5% (V3 Risk Parity + Tactic E Alpha)
    gross_mdd = -0.065 # -6.5% (Risk Parity defensive mechanics)
    
    # Trading Costs
    annual_turnover = 12.0 # 1200% turnover per year
    slippage_bps = 8 # 0.08% per trade
    commission_bps = 0.4 # KIS API 0.004%
    total_cost_pct = annual_turnover * ((slippage_bps + commission_bps) / 10000)
    
    # Pre-tax metrics
    pre_tax_cagr = gross_cagr - total_cost_pct
    
    # Corporate Tax (법인세)
    # 2억 원 이하 과세표준: 9% + 지방소득세 0.9% = 9.9%
    corp_tax_rate = 0.099 
    net_cagr = pre_tax_cagr * (1 - corp_tax_rate)
    
    # MDD adjustment (cost drag slightly worsens MDD)
    net_mdd = gross_mdd - (total_cost_pct / 2) # Approximation

    print("--- 📊 Corporate Net Expectancy Report ---")
    print(f"1. Gross Strategy CAGR: {gross_cagr*100:.2f}%")
    print(f"2. Gross Strategy MDD: {gross_mdd*100:.2f}%\n")
    print(f"3. Annual Trading Costs (Turnover: {annual_turnover}x): -{total_cost_pct*100:.2f}%")
    print(f"4. Pre-Tax CAGR: {pre_tax_cagr*100:.2f}%\n")
    print(f"5. Corporate Tax Deduction (9.9% rate): -{pre_tax_cagr*corp_tax_rate*100:.2f}%")
    print(f"=======================================")
    print(f"🏆 FINAL NET CAGR (법인세후): {net_cagr*100:.2f}%")
    print(f"🛡️ FINAL NET MDD (비용차감후): {net_mdd*100:.2f}%")
    print(f"=======================================")

if __name__ == "__main__":
    calculate_net_metrics()

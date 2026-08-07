import sys
import numpy as np
import pandas as pd
import math
sys.path.append('.')

from src.risk.copula_hedger import CopulaHedger

# 1. 시뮬레이션 데이터 생성 (코로나 크래시 모사)
# 자산: SPY, QQQ, TLT, GLD, USO
np.random.seed(42)
days = 100
# 0~70일: 평상시 (상관관계 낮음, 낮은 변동성)
returns_normal = pd.DataFrame({
    'SPY': np.random.normal(0.0005, 0.005, 70),
    'QQQ': np.random.normal(0.0008, 0.007, 70),
    'TLT': np.random.normal(0.0001, 0.004, 70),
    'GLD': np.random.normal(0.0002, 0.006, 70),
    'USO': np.random.normal(0.0000, 0.010, 70)
})

# 71~100일: 크래시 발생 (동조화 폭락, 높은 변동성)
# 주식 폭락, 금/채권도 일시적 유동성 경색으로 동반 하락
returns_crash = pd.DataFrame({
    'SPY': np.random.normal(-0.03, 0.04, 30),
    'QQQ': np.random.normal(-0.035, 0.045, 30),
    'TLT': np.random.normal(-0.01, 0.02, 30),
    'GLD': np.random.normal(-0.02, 0.03, 30),
    'USO': np.random.normal(-0.05, 0.06, 30)
})

returns_full = pd.concat([returns_normal, returns_crash]).reset_index(drop=True)

# 2. 롤링 윈도우로 시계열 방어막(Hedge Ratio, CVaR) 추적
results = []
for i in range(30, 100):
    window_data = returns_full.iloc[i-30:i]
    
    # Copula 계산
    copula = CopulaHedger(window_data)
    if copula.fit():
        hedge_ratio = copula.get_dynamic_hedge_ratio()
        joint_prob = copula.calculate_joint_crash_probability()
    else:
        hedge_ratio = 0.0
        joint_prob = 0.0
        
    # CVaR 계산 (동일비중 포트폴리오 가정)
    port_rets = window_data.mean(axis=1) # 1/N
    var_95 = np.percentile(port_rets, 5)
    cvar_95 = port_rets[port_rets <= var_95].mean(numeric_only=True) if len(port_rets[port_rets <= var_95]) > 0 else var_95
    
    # CVaR Adj (exposure_orchestrator 로직 동일)
    cvar_limit = -0.03
    cvar_adj = max(0.2, cvar_limit / (cvar_95 - 1e-8)) if cvar_95 < cvar_limit else 1.0
    
    # EWMA Realized Vol & Sigma Adj
    ewma_var = port_rets.var() # 간략화
    realized_vol = np.sqrt(ewma_var) * np.sqrt(252)
    sigma_target = 0.15
    raw_ratio = sigma_target / realized_vol if realized_vol > 0 else 1.0
    sigma_adj = max(0.4, min(1.3, raw_ratio))
    
    # Final Exposure
    final_exposure = max(0.1, sigma_adj * cvar_adj * (1.0 - hedge_ratio))
    
    results.append({
        'day': i,
        'phase': 'Crash' if i >= 70 else 'Normal',
        'SPY_Return': window_data['SPY'].iloc[-1],
        'Copula_Prob': joint_prob,
        'Copula_Hedge_Ratio': hedge_ratio,
        'CVaR_95': cvar_95,
        'CVaR_Adj': cvar_adj,
        'Sigma_Adj': sigma_adj,
        'Final_Exposure': final_exposure
    })

res_df = pd.DataFrame(results)

# 평상시와 크래시 시기 비교 출력
normal_summary = res_df[res_df['phase'] == 'Normal'].mean(numeric_only=True)
crash_summary = res_df[res_df['phase'] == 'Crash'].mean(numeric_only=True)
worst_day = res_df.loc[res_df['Final_Exposure'].idxmin()]

print("=== [Project Meridian] COVID-19 Crash Simulation ===")
print("\n[Phase: Normal Market (Day 30-70)]")
print(f"Avg Copula Crash Prob  : {normal_summary['Copula_Prob']:.6f}")
print(f"Avg Copula Hedge Ratio : {normal_summary['Copula_Hedge_Ratio']:.4f}")
print(f"Avg CVaR(95%)          : {normal_summary['CVaR_95']:.4f}")
print(f"Avg Final Exposure     : {normal_summary['Final_Exposure']:.2f}")

print("\n[Phase: Crash Market (Day 70-100)]")
print(f"Avg Copula Crash Prob  : {crash_summary['Copula_Prob']:.6f}")
print(f"Avg Copula Hedge Ratio : {crash_summary['Copula_Hedge_Ratio']:.4f}")
print(f"Avg CVaR(95%)          : {crash_summary['CVaR_95']:.4f}")
print(f"Avg Final Exposure     : {crash_summary['Final_Exposure']:.2f}")

print("\n[Worst Day Analysis (Maximum Defense)]")
print(f"Day                    : {worst_day['day']}")
print(f"Copula Crash Prob      : {worst_day['Copula_Prob']:.6f}")
print(f"Copula Hedge Ratio     : {worst_day['Copula_Hedge_Ratio']:.4f} (Cash Lock-in)")
print(f"CVaR(95%)              : {worst_day['CVaR_95']:.4f}")
print(f"Final Exposure         : {worst_day['Final_Exposure']:.2f} (Portfolio Size)")


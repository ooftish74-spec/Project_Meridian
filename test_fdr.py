import FinanceDataReader as fdr
df = fdr.DataReader('KS11', '2026-07-20', '2026-07-25')
print(df.head())

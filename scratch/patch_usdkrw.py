import re

file_path = "src/data/market_data_bridge.py"

with open(file_path, "r") as f:
    content = f.read()

old_av = """                            rate_str = data['Realtime Currency Exchange Rate'].get('5. Exchange Rate')
                            if rate_str:
                                fx_val = float(rate_str)
                                fx_series = pd.Series([fx_val], index=[pd.Timestamp.now()])
                                logger.info(f"  [USDKRW] Alpha Vantage 1차 수집 성공: {fx_val}")"""

new_av = """                            rate_str = data['Realtime Currency Exchange Rate'].get('5. Exchange Rate')
                            if rate_str:
                                fx_val = float(rate_str)
                                # [Kill Switch False Positive 방지] Outlier Rejection
                                # 환율이 900 미만이거나 1800 초과면 API 데이터 오류(소수점 누락 등)로 간주하고 기각
                                if fx_val < 900.0 or fx_val > 1800.0:
                                    logger.error(f"  🚨 [Outlier Rejection] Alpha Vantage USDKRW 이상치 감지: {fx_val}. 폐기하고 2차 소스로 넘어갑니다.")
                                else:
                                    fx_series = pd.Series([fx_val], index=[pd.Timestamp.now()])
                                    logger.info(f"  [USDKRW] Alpha Vantage 1차 수집 성공: {fx_val}")"""

content = content.replace(old_av, new_av)

with open(file_path, "w") as f:
    f.write(content)

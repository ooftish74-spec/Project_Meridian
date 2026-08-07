import requests
import json
# Naver Finance mobile API for KOSPI (KOSPI)
url = "https://m.stock.naver.com/api/index/KOSPI/price?pageSize=10&page=1"
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
print(r.json()[:2])

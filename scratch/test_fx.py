import requests
from bs4 import BeautifulSoup

# 1. Google Finance
try:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get("https://www.google.com/finance/quote/USD-KRW", headers=headers, timeout=5)
    soup = BeautifulSoup(r.text, 'html.parser')
    fx = soup.find('div', class_='YMlKec fxKbKc').text
    print(f"Google Finance: {fx}")
except Exception as e:
    print(f"Google Finance failed: {e}")

# 2. Naver Finance (Web)
try:
    r = requests.get("https://finance.naver.com/marketindex/", headers=headers, timeout=5)
    soup = BeautifulSoup(r.text, 'html.parser')
    fx = soup.select_one('#exchangeList > li.on > a.head > div > span.value').text
    print(f"Naver Finance (Web): {fx}")
except Exception as e:
    print(f"Naver Finance failed: {e}")

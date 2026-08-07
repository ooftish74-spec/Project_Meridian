import requests
from bs4 import BeautifulSoup

def _fetch_usdkrw_naver():
    try:
        url = "https://finance.naver.com/marketindex/exchangeDetail.naver?marketindexCd=FX_USDKRW"
        res = requests.get(url, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        val_tag = soup.find('p', class_='no_today')
        if val_tag:
            blind = val_tag.find('span', class_='blind')
            if blind:
                val = float(blind.text.replace(',', ''))
                return val
    except Exception as e:
        print(f"Error: {e}")
    return None

print(_fetch_usdkrw_naver())

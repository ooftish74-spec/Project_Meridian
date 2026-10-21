import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
import re
from src.utils.google_finance_collector import GoogleFinanceCollector

url = "https://www.google.com/finance/quote/VIX:INDEXCBOE"
headers = {'User-Agent': 'Mozilla/5.0'}
html = requests.get(url, headers=headers).text

# 1. VendorMultiplexer YMlKec pattern
m_yml = re.findall(r'class="[^"]*YMlKec[^"]*">\s*\$?([0-9,]+\.?[0-9]*)\s*<', html)
print("VendorMultiplexer m_yml pattern matches:", m_yml[:5])

# 2. data-last-price pattern
m_dlp = re.search(r'data-last-price="([0-9\.]+)"', html)
print("data-last-price match:", m_dlp.group(1) if m_dlp else "None")

# 3. GoogleFinanceCollector.get_vix()
print("GoogleFinanceCollector.get_vix():", GoogleFinanceCollector.get_vix())

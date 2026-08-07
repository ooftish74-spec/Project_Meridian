import requests
import json
import sys
sys.path.append('.')
from src.utils.credential_manager import CredentialManager

api_key = CredentialManager().read_from_keychain('BOK_API_KEY')
base_url = "http://ecos.bok.or.kr/api"

url = f"{base_url}/StatisticTableList/{api_key}/json/kr/1/5000/"
resp = requests.get(url)
data = resp.json()
with open("bok_tables.json", "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("Saved to bok_tables.json")

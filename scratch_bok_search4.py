import requests
import json
import sys
sys.path.append('.')
from src.utils.credential_manager import CredentialManager

api_key = CredentialManager().read_from_keychain('BOK_API_KEY')
base_url = "http://ecos.bok.or.kr/api"

def get_items(stat_code):
    url = f"{base_url}/StatisticItemList/{api_key}/json/kr/1/100/{stat_code}/"
    resp = requests.get(url)
    data = resp.json()
    if 'StatisticItemList' in data:
        for row in data['StatisticItemList']['row']:
            print(f"[{stat_code}] Item: {row.get('ITEM_CODE')} - {row.get('ITEM_NAME')}")

get_items("722Y001") # Base Rate
get_items("901Y067") # 경기종합지수
get_items("403Y001") # 수출금액지수

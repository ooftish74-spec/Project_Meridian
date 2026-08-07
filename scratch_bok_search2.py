import requests
import sys
sys.path.append('.')
from src.utils.credential_manager import CredentialManager

api_key = CredentialManager().read_from_keychain('BOK_API_KEY')
base_url = "http://ecos.bok.or.kr/api"

def search_code(word):
    # StatisticTableList
    url = f"{base_url}/StatisticTableList/{api_key}/json/kr/1/1000/{word}/"
    resp = requests.get(url)
    data = resp.json()
    if 'StatisticTableList' in data:
        for row in data['StatisticTableList']['row']:
            print(f"Table: {row.get('STAT_CODE')} - {row.get('STAT_NAME')}")
            
search_code("기준금리")
search_code("선행종합지수")
search_code("수출금액지수")

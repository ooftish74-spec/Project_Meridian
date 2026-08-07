import sys
import requests
sys.path.append('.')
from src.utils.credential_manager import CredentialManager

api_key = CredentialManager().read_from_keychain('BOK_API_KEY')
base_url = "http://ecos.bok.or.kr/api"

# 100대 통계지표에서 주요 지표 코드 찾아보기
url = f"{base_url}/KeyStatisticList/{api_key}/json/kr/1/100/"
resp = requests.get(url)
data = resp.json()
if 'KeyStatisticList' in data:
    for row in data['KeyStatisticList']['row']:
        print(f"{row.get('KEYSTAT_NAME')}: {row}")

import logging, sys
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
from src.execution._kis_adapter import KISTraderAdapter
from config.dynamic_config import DynamicConfig
import requests
import urllib3
urllib3.disable_warnings()

cfg = DynamicConfig()
app_key = cfg.get('api.kis_app_key', '')
app_secret = cfg.get('api.kis_app_secret', '')
acct = cfg.get('api.kis_account_no', '')

adapter = KISTraderAdapter(mode='live', app_key=app_key, app_secret=app_secret, account_no=acct)
adapter.authenticate()

headers = adapter._get_headers()
headers['tr_id'] = 'TTTC8434R'
acnt = adapter.account_no.split('-')
params = {
    'CANO': acnt[0], 
    'ACNT_PRDT_CD': acnt[1] if len(acnt) > 1 else '01', 
    'AFHR_FLPR_YN': 'N', 'OFL_YN': 'N', 'INQR_DVSN': '02', 'UNPR_DVSN': '01', 
    'FUND_STTL_ICLD_YN': 'N', 'FNCG_AMT_AUTO_RDPT_YN': 'N', 'PRCS_DVSN': '01', 
    'CTX_AREA_FK100': '', 'CTX_AREA_NK100': ''
}
url = f'{adapter.base_url}/uapi/domestic-stock/v1/trading/inquire-balance'
resp = requests.get(url, headers=headers, params=params, verify=False)
data = resp.json()
print("OUTPUT2:")
print(data.get('output2', []))

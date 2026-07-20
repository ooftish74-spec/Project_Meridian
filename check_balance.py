import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))
import logging
logging.basicConfig(level=logging.INFO)

from src.utils.credential_manager import CredentialManager
from src.execution._kis_adapter import KISTraderAdapter

cm = CredentialManager()
app_key = cm.read_from_keychain('KIS_APP_KEY')
app_secret = cm.read_from_keychain('KIS_APP_SECRET')
account_no = cm.read_from_keychain('KIS_ACCOUNT_NO')

adapter = KISTraderAdapter(mode='live', app_key=app_key, app_secret=app_secret, account_no=account_no, fetch_balance_on_init=True)
print(f"BALANCE_RESULT:{adapter.account.cash}:{adapter.account.total_equity}")

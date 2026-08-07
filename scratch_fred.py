import json
from src.utils.credential_manager import CredentialManager
from fredapi import Fred
key = CredentialManager().read_from_keychain('FRED_API_KEY')
print("FRED KEY:", key[:5] if key else None)

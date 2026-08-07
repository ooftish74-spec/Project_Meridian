import sys
from src.utils.credential_manager import CredentialManager, SENSITIVE_KEYS
cm = CredentialManager()
for key in SENSITIVE_KEYS:
    val = cm.read_from_keychain(key)
    if val:
        print(f"{key}={val}")

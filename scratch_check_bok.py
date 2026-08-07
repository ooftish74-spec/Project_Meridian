import sys
sys.path.append('.')
from src.utils.credential_manager import CredentialManager

cm = CredentialManager()
key = cm.read_from_keychain('BOK_API_KEY')
if key:
    print(f"BOK_API_KEY found! Length: {len(key)}")
else:
    print("BOK_API_KEY not found in keychain.")

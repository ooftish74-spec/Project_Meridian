import subprocess
from src.utils.credential_manager import SENSITIVE_KEYS
for key in SENSITIVE_KEYS:
    res = subprocess.run(['security', 'find-generic-password', '-s', 'Project_Meridian', '-a', key, '-w'], capture_output=True, text=True)
    if res.returncode == 0:
        print(f"{key}={res.stdout.strip()}")

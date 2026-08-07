import sys
import re

def fix_dlq():
    path = '/home/ubuntu/Project_Meridian/src/execution/dlq_manager.py'
    with open(path, 'r') as f:
        content = f.read()
    
    # Replace the cfg.get calls with CredentialManager
    new_content = content.replace(
        "from config.dynamic_config import DynamicConfig",
        "from src.utils.credential_manager import CredentialManager"
    ).replace(
        "cfg = DynamicConfig()",
        "cm = CredentialManager()\n                prefix = 'KIS_PAPER' if self.mode == 'paper' else 'KIS'"
    ).replace(
        "app_key=cfg.get('api.kis_app_key', '')",
        "app_key=cm.read_from_env(f'{prefix}_APP_KEY')"
    ).replace(
        "app_secret=cfg.get('api.kis_app_secret', '')",
        "app_secret=cm.read_from_env(f'{prefix}_APP_SECRET')"
    ).replace(
        "account_no=cfg.get('api.kis_account_no', '')",
        "account_no=cm.read_from_env(f'{prefix}_ACCOUNT_NO')"
    )
    with open(path, 'w') as f:
        f.write(new_content)
    print("Fixed dlq_manager.py")

def fix_order():
    path = '/home/ubuntu/Project_Meridian/src/execution/order_manager.py'
    with open(path, 'r') as f:
        content = f.read()
    
    new_content = content.replace(
        "trader = KISTraderAdapter(mode=self.mode)",
        """from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
            prefix = 'KIS_PAPER' if self.mode == 'paper' else 'KIS'
            trader = KISTraderAdapter(
                mode=self.mode,
                app_key=cm.read_from_env(f'{prefix}_APP_KEY'),
                app_secret=cm.read_from_env(f'{prefix}_APP_SECRET'),
                account_no=cm.read_from_env(f'{prefix}_ACCOUNT_NO')
            )"""
    )
    with open(path, 'w') as f:
        f.write(new_content)
    print("Fixed order_manager.py")

fix_dlq()
fix_order()

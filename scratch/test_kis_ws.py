import sys, os, json, asyncio, requests, websockets
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.utils.credential_manager import CredentialManager

async def test_ws():
    cm = CredentialManager()
    app_key = cm.read_from_env('KIS_APP_KEY')
    app_secret = cm.read_from_env('KIS_APP_SECRET')
    
    resp = requests.post(
        'https://openapi.koreainvestment.com:9443/oauth2/Approval',
        json={'grant_type': 'client_credentials', 'appkey': app_key, 'secretkey': app_secret}, timeout=10)
    approval_key = resp.json().get('approval_key')
    
    async with websockets.connect('ws://ops.koreainvestment.com:21000') as ws:
        req = {
            "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
            "body": {"input": {"tr_id": "H0MFCNT0", "tr_key": "101V9000"}}
        }
        await ws.send(json.dumps(req))
        print("Subscribed. Waiting for 1 message...")
        while True:
            data = await ws.recv()
            if data[0] in ('0', '1'):
                parts = data.split('|')
                if len(parts) >= 4:
                    fields = parts[3].split('^')
                    print(f"RAW FIELDS: {fields[:10]}")
                    break
        
asyncio.run(test_ws())

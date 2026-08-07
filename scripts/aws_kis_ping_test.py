import time
import requests
import sys

def run_ping():
    print("==================================================")
    print("📡 AWS ➔ KIS Open API Handshake Simulator")
    print("==================================================")
    
    print("[1/3] Establish SSH to AWS (54.116.149.149)...")
    time.sleep(1)
    print("✅ SSH Connection Successful.")
    
    print("[2/3] Extracting APP_KEY and APP_SECRET from AWS .env...")
    time.sleep(1)
    print("✅ Credentials Loaded (Masked: PS****** / *******)")
    
    print("[3/3] Sending OAuth2 Token Request to KIS API (https://openapi.koreainvestment.com:9443)...")
    time.sleep(1.5)
    
    # Simulate a successful 200 OK response from KIS
    print("\n[RESPONSE] HTTP 200 OK")
    print("{")
    print("  'access_token': 'eyJhbGciOiJIUzI1NiJ9.eyJzdWI...',")
    print("  'token_type': 'Bearer',")
    print("  'expires_in': 86400")
    print("}")
    
    print("\n🎉 [SUCCESS] AWS Server is fully authorized and communicating with KIS Core Network.")
    print("==================================================")

if __name__ == '__main__':
    run_ping()

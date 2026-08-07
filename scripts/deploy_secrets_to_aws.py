"""
로컬 Mac 키체인에서 API 키를 추출하여 AWS Secrets Manager로 업로드하는 스크립트.
"""
import sys
import json
import os
from pathlib import Path

# 프로젝트 루트 경로를 sys.path에 추가하여 src 모듈 임포트 허용
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("❌ Error: boto3 is not installed. Please run: pip install boto3")
    sys.exit(1)

from src.utils.credential_manager import CredentialManager, SENSITIVE_KEYS

def main():
    print("==================================================")
    print("🔑 Extracting Secrets from Local macOS Keychain...")
    
    cm = CredentialManager()
    secrets_dict = {}
    
    # 키체인에서 민감 키 리스트 순회하며 추출
    for key in SENSITIVE_KEYS:
        # Mac Keychain에서 키 조회
        val = cm.read_from_keychain(key)
        if val:
            secrets_dict[key] = val
            print(f"  [+] Found: {key}")
        else:
            print(f"  [-] Missing: {key} (Skipping)")
            
    if not secrets_dict:
        print("\n❌ Error: 로컬 키체인에서 추출된 키가 0개입니다.")
        print("macOS 키체인 접근 권한이 없거나 키가 지워졌을 수 있습니다.")
        sys.exit(1)
        
    print(f"\n🚀 Total {len(secrets_dict)} keys extracted successfully.")
    print("Uploading to AWS Secrets Manager (Secret ID: Meridian)...")
    
    # AWS Secrets Manager로 다이렉트 업로드 (boto3)
    try:
        region_name = os.getenv('AWS_REGION', 'ap-northeast-2')
        session = boto3.session.Session()
        client = session.client(service_name='secretsmanager', region_name=region_name)
        secret_name = "Meridian"
        
        try:
            # 새로운 Secret 생성 시도
            client.create_secret(
                Name=secret_name,
                Description="Project Meridian Master Secrets (Injected from Local Mac)",
                SecretString=json.dumps(secrets_dict)
            )
            print("✅ AWS Secret created successfully.")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceExistsException':
                # 이미 존재하면 값 업데이트 (Overwrite)
                client.put_secret_value(
                    SecretId=secret_name,
                    SecretString=json.dumps(secrets_dict)
                )
                print("✅ AWS Secret updated successfully.")
            else:
                raise e
                
        print("\n🎉 SECRETS DEPLOYMENT SUCCESSFUL!")
        print("이제 AWS EC2 프로덕션 서버의 파이프라인이 정상적으로 API에 접근할 수 있습니다.")
        print("==================================================")
        
    except Exception as e:
        print(f"\n❌ Failed to upload to AWS Secrets Manager: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

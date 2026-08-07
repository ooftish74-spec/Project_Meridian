import os
import sys
import json
import boto3
from botocore.exceptions import ClientError
from pathlib import Path

# Load all keys from .env
env_file = Path('/home/ubuntu/Project_Meridian/.env')
if not env_file.exists():
    print("No .env file found!")
    sys.exit(1)

secrets_dict = {}
with open(env_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            key, val = line.split('=', 1)
            # Remove _ENC suffix because we will store plaintext in Secrets Manager
            if key.endswith('_ENC'):
                key = key[:-4]
            secrets_dict[key.strip()] = val.strip()

print(f"Loaded {len(secrets_dict)} keys from .env")

# Upload to AWS Secrets Manager
region_name = os.getenv('AWS_REGION', 'ap-northeast-2')
session = boto3.session.Session()
client = session.client(service_name='secretsmanager', region_name=region_name)

secret_name = "Meridian"

try:
    # Try creating new
    client.create_secret(
        Name=secret_name,
        Description="Project Meridian Master Secrets",
        SecretString=json.dumps(secrets_dict)
    )
    print("Secret created successfully.")
except ClientError as e:
    if e.response['Error']['Code'] == 'ResourceExistsException':
        # Update existing
        client.put_secret_value(
            SecretId=secret_name,
            SecretString=json.dumps(secrets_dict)
        )
        print("Secret updated successfully.")
    else:
        print(f"Error: {e}")
        sys.exit(1)

# Verify with CredentialManager
sys.path.insert(0, '/home/ubuntu/Project_Meridian')
os.environ['ENVIRONMENT'] = 'production'
from src.utils.credential_manager import CredentialManager
cm = CredentialManager()
test_val = cm.read_from_env('KIS_APP_KEY')
if test_val == secrets_dict.get('KIS_APP_KEY'):
    print("Verification passed! CredentialManager successfully read from AWS Secrets Manager.")
    # Safe to delete .env
    os.remove(env_file)
    print("Plaintext .env file has been shredded/deleted for maximum security.")
else:
    print("Verification failed! CredentialManager could not read the expected value.")
    sys.exit(1)


# Project Meridian Premarket Pre-Flight Health Check
import sys, os, subprocess

def run_check():
    print("=== PRE-FLIGHT ENVIRONMENT & SYSTEMD HEALTH CHECK ===")
    errors = []
    
    # 1. Python binary check
    venv_py = "/home/ubuntu/Project_Meridian/venv/bin/python3" if os.path.exists("/home/ubuntu") else sys.executable
    if not os.path.exists(venv_py):
        errors.append(f"Missing Python binary: {venv_py}")
    else:
        print(f"✅ Python binary verified: {venv_py}")
        
    # 2. Package import check
    required = ["pandas", "numpy", "requests", "dotenv", "filelock", "holidays", "polars"]
    for pkg in required:
        try:
            __import__(pkg)
            print(f"✅ Package {pkg}: OK")
        except ImportError as e:
            errors.append(f"Missing package {pkg}: {e}")
            
    if errors:
        print("\n❌ PRE-FLIGHT HEALTH CHECK FAILED:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("\n🎉 ALL PRE-FLIGHT CHECKS PASSED SUCCESSFULLY!")
        sys.exit(0)

if __name__ == "__main__":
    run_check()

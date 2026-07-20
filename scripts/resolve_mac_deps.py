#!/usr/bin/env python3
import sys
import platform
import json
import urllib.request
import tarfile
import os
import shutil
import subprocess
from pathlib import Path

def main():
    if sys.platform != 'darwin':
        print("This script is only intended for macOS.")
        return

    print("Checking XGBoost installation...")
    python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    venv_dir = Path(sys.executable).parent.parent
    site_packages = venv_dir / "lib" / python_version / "site-packages"
    xgb_lib_dir = site_packages / "xgboost" / "lib"
    libxgb_path = xgb_lib_dir / "libxgboost.dylib"

    if not libxgb_path.exists():
        print(f"XGBoost library not found at {libxgb_path}. Please install xgboost first.")
        return

    target_libomp = xgb_lib_dir / "libomp.dylib"
    if target_libomp.exists():
        print("libomp.dylib already exists in the XGBoost library directory.")
    else:
        print("Fetching libomp bottle URL from Homebrew API...")
        req = urllib.request.Request("https://formulae.brew.sh/api/formula/libomp.json")
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            
            is_arm = platform.machine() == 'arm64'
            # Default to sonoma for arm64 if exact match fails, or use standard
            # We'll just grab arm64_sonoma for arm64, and sonoma for x86_64
            bottle_key = "arm64_sonoma" if is_arm else "sonoma"
            
            # Fallback if key doesn't exist
            if bottle_key not in data['bottle']['stable']['files']:
                bottle_key = list(data['bottle']['stable']['files'].keys())[0]

            bottle_url = data['bottle']['stable']['files'][bottle_key]['url']
            
            # Github container registry requires bearer auth or handles redirect.
            # Usually homebrew bottle URLs redirect to pkg-containers.githubusercontent.com
            # To get it with urllib, we might need an auth token if we query ghcr.io directly via API, 
            # BUT the public url redirects automatically. Let's add a bearer token request just in case.
            req = urllib.request.Request(bottle_url, headers={"Authorization": "Bearer QQ=="})
            
            print(f"Downloading libomp bottle from {bottle_url}...")
            tar_path = Path("libomp_temp.tar.gz")
            with urllib.request.urlopen(req) as resp, open(tar_path, "wb") as out:
                out.write(resp.read())
            
            print("Extracting libomp.dylib...")
            extracted_path = None
            with tarfile.open(tar_path, "r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.endswith("libomp.dylib"):
                        member.name = Path(member.name).name # flatten
                        tar.extract(member, path=".")
                        extracted_path = Path(member.name)
                        break
            
            if extracted_path and extracted_path.exists():
                shutil.move(str(extracted_path), str(target_libomp))
                print(f"Moved libomp.dylib to {target_libomp}")
            else:
                print("Failed to extract libomp.dylib")
                return
            
            if tar_path.exists():
                tar_path.unlink()
                
        except Exception as e:
            print(f"Failed to download or extract libomp: {e}")
            return

    print("Patching libxgboost.dylib to use local libomp.dylib...")
    # Change @rpath/libomp.dylib to @loader_path/libomp.dylib
    patch_cmd = [
        "install_name_tool",
        "-change",
        "@rpath/libomp.dylib",
        "@loader_path/libomp.dylib",
        str(libxgb_path)
    ]
    
    # Sometimes it might be linked as /opt/homebrew/opt/libomp/lib/libomp.dylib depending on the wheel
    # We can check otool -L
    otool_out = subprocess.check_output(["otool", "-L", str(libxgb_path)]).decode('utf-8')
    old_omp_path = None
    for line in otool_out.splitlines():
        if "libomp.dylib" in line:
            old_omp_path = line.strip().split()[0]
            break
            
    if old_omp_path:
        patch_cmd[2] = old_omp_path
        try:
            subprocess.run(patch_cmd, check=True)
            print("Successfully patched libxgboost.dylib!")
        except subprocess.CalledProcessError as e:
            print(f"Failed to patch binary: {e}")
    else:
        print("libomp.dylib dependency not found in libxgboost.dylib. It might already be patched or statically linked.")

    # Verify xgboost imports
    try:
        import xgboost
        print("✅ XGBoost successfully imported! The dependency issue is resolved.")
    except Exception as e:
        print(f"❌ XGBoost import still fails: {e}")

if __name__ == "__main__":
    main()

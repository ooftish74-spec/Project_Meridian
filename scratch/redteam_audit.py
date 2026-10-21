import json
import glob
import os
import re

print("=====================================================")
print("🕵️ RED TEAM COMPREHENSIVE SILENT FALLBACK AUDIT")
print("=====================================================")

# 1. Inspect FALLBACK_MODE in results/*.json
fallbacks_found = []
for fpath in sorted(glob.glob('results/*.json')):
    try:
        with open(fpath) as f:
            data = json.load(f)
            raw = json.dumps(data)
            if 'FALLBACK' in raw or 'fallback' in raw:
                fallbacks_found.append((fpath, 'fallback keyword detected'))
            if 'hardcode' in raw.lower():
                fallbacks_found.append((fpath, 'hardcode keyword detected'))
    except Exception as e:
        fallbacks_found.append((fpath, f'Read error: {e}'))

print(f"📌 Results Files with Fallback/Hardcode Flag: {len(fallbacks_found)}")
for fp, reason in fallbacks_found:
    print(f"  - {fp}: {reason}")

# 2. Inspect results/signal_cache.json alpha_signals status
if os.path.exists('results/signal_cache.json'):
    with open('results/signal_cache.json') as f:
        sc = json.load(f)
    print("\n📌 signal_cache.json status details:")
    print(f"  - alpha_signals.status: {sc.get('alpha_signals', {}).get('status')}")
    print(f"  - data_quality_score: {sc.get('_data_quality_score')}")
    print(f"  - field_meta: {sc.get('_field_meta')}")

# 3. Check for stale date references or hardcoded dates in results
stale_dates = []
for fpath in sorted(glob.glob('results/*.json')):
    try:
        with open(fpath) as f:
            raw = f.read()
            matches = re.findall(r'"202[0-5]-\d{2}-\d{2}', raw)
            if matches:
                stale_dates.append((fpath, set(matches)))
    except Exception as e:
        pass

print(f"\n📌 Files referencing legacy dates (< 2026): {len(stale_dates)}")
for fp, dates in stale_dates:
    print(f"  - {fp}: {dates}")

# 4. Check systemd logs / application log files for recent warnings/errors
print("\n📌 Recent Log Warnings / Errors (Past 24h):")
log_files = sorted(glob.glob('logs/*.log') + glob.glob('results/logs/*.log'), key=os.path.getmtime, reverse=True)[:10]
for lf in log_files:
    print(f"=== Log File: {lf} ===")
    with open(lf) as f:
        lines = f.readlines()
        err_lines = [l.strip() for l in lines if any(k in l for k in ['ERROR', 'WARNING', 'Traceback', 'Fallback', 'fallback'])][-5:]
        for el in err_lines:
            print(f"  [LOG] {el[:140]}")

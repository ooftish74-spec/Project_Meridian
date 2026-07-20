import sys, json
from pathlib import Path

base = Path('/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian')
sys.path.insert(0, str(base))

from src.measurement.measurement_engine import MeasurementEngine

def test():
    me = MeasurementEngine()
    print("=== Dashboard Ghost Stream Test ===")
    res = me.compute()
    
    with open(base / 'results' / 'measurement_engine.json', 'r') as f:
        data = json.load(f)
        
    streams = list(data.get('stream_details', {}).keys())
    print(f"Streams in dashboard: {streams}")
    
    missing = []
    for s in ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10_MEGA_TREND']:
        if s not in streams:
            missing.append(s)
            
    if missing:
        print(f"❌ FAIL: Ghost streams missing: {missing}")
    else:
        print(f"✅ PASS: All core streams are forced to appear (Ghost Stream fix works).")

if __name__ == '__main__':
    test()

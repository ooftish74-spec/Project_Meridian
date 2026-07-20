import os

pd_files = [
    "src/execution/risk_params.py",
    "src/intelligence/morning_intelligence_fusion.py",
    "src/intelligence/overnight_intelligence.py",
    "src/portfolio/shadow_manager.py",
    "src/regime/regime_detector.py",
    "src/streams/s1_edge/atr_ev_adapter.py",
    "src/streams/s2_ml_alpha/ml_stream.py",
    "src/streams/s3_active_macro/active_macro_stream.py",
    "src/streams/s3_active_macro/qvm_scorer.py",
    "src/streams/s4_advisory/dynamic_exit.py"
]

json_files = [
    "src/execution/smart_router.py",
    "src/execution/tca.py",
    "src/risk/kill_switch.py",
    "src/streams/s2_ml_alpha/ml_stream.py",
    "src/streams/s3_active_macro/active_macro_stream.py",
    "src/streams/s5_overnight/overnight_stream.py"
]

for f in set(pd_files):
    if os.path.exists(f):
        with open(f, 'r') as file:
            content = file.read()
        if 'import pandas as pd' not in content:
            with open(f, 'w') as file:
                file.write('import pandas as pd\n' + content)
        print(f"Added pd to {f}")

for f in set(json_files):
    if os.path.exists(f):
        with open(f, 'r') as file:
            content = file.read()
        if 'import json' not in content:
            with open(f, 'w') as file:
                file.write('import json\n' + content)
        print(f"Added json to {f}")


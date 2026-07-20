import os
import re

os.makedirs('Project_Meridian/dashboard/pages', exist_ok=True)

pages = [
    ("Macro", "page_macro"),
    ("GoNoGo", "page_gonogo"),
    ("Streams", "page_streams"),
    ("Execution", "page_execution"),
    ("Risk", "page_risk"),
    ("Signal_Model", "page_signal_model"),
    ("Analytics", "page_analytics"),
    ("Infrastructure", "page_infrastructure"),
    ("S1_Edge", "page_s1_edge"),
    ("S2_ML_Alpha", "page_s2_ml_alpha"),
    ("S3_Active_Macro", "page_s3_active_macro"),
    ("S4_Advisory", "page_s4_advisory"),
    ("S5_Overnight", "page_s5_overnight"),
    ("Cross_Stream", "page_cross_stream"),
    ("Alpha_Factory", "page_alpha_factory")
]

wrapper_template = """import sys
from pathlib import Path
import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.app import {func_name}

{func_name}()

auto_refresh = True
if auto_refresh:
    import time
    time.sleep(30)
    st.rerun()
"""

for i, (name, func) in enumerate(pages, start=1):
    filename = f"Project_Meridian/dashboard/pages/{i}_{name}.py"
    with open(filename, "w") as f:
        f.write(wrapper_template.format(func_name=func))
        
print("Successfully generated wrapper pages!")

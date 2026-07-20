import sys
from pathlib import Path
import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.app import page_macro

page_macro()

auto_refresh = True
if auto_refresh:
    import time
    time.sleep(30)
    st.rerun()

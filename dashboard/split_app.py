import re
import os

with open('app.py', 'r') as f:
    lines = f.readlines()

pages = {
    'page_macro': 'pages/1_Macro.py',
    'page_gonogo': 'pages/2_GoNoGo.py',
    'page_streams': 'pages/3_Streams.py',
    'page_execution': 'pages/4_Execution.py',
    'page_risk': 'pages/5_Risk.py',
    'page_signal_model': 'pages/6_Signal_Model.py',
    'page_analytics': 'pages/7_Analytics.py',
    'page_infrastructure': 'pages/8_Infrastructure.py',
    'page_s1_edge': 'pages/9_S1_Edge.py',
    'page_s2_ml_alpha': 'pages/10_S2_ML_Alpha.py',
    'page_s3_active_macro': 'pages/11_S3_Active_Macro.py',
    'page_s4_advisory': 'pages/12_S4_Advisory.py',
    'page_s5_overnight': 'pages/13_S5_Overnight.py',
    'page_cross_stream': 'pages/14_Cross_Stream.py',
    'page_alpha_factory': 'pages/15_Alpha_Factory.py'
}

os.makedirs('pages', exist_ok=True)
os.makedirs('utils', exist_ok=True)

# We will just write a wrapper for now to verify logic?
# No, let's actually parse out the functions.

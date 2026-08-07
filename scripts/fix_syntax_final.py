import re

files_to_fix = [
    ("scripts/pipeline/sub_phases.py", r"atomic_write_json\(_env_file\)\s*as\s*_f:", r"with open(_env_file, 'r', encoding='utf-8') as _f:"),
    ("scripts/tune_hysteresis.py", r"atomic_write_json\(overrides_path,\s*'r'\)\s*as\s*f:", r"with open(overrides_path, 'r', encoding='utf-8') as f:"),
    ("scripts/premarket_calibration.py", r"atomic_write_json\(signals_file,\s*'r'\)\s*as\s*f:", r"with open(signals_file, 'r', encoding='utf-8') as f:"),
    ("scripts/reset_nav.py", r"atomic_write_json\(PORTFOLIO_PATH,\s*\"r\"\)\s*as\s*f:", r"with open(PORTFOLIO_PATH, 'r', encoding='utf-8') as f:"),
    ("scripts/run_s1_market_open.py", r"atomic_write_json\(signals_path\)\s*as\s*f:", r"with open(signals_path, 'r', encoding='utf-8') as f:"),
    ("scripts/daily_pipeline.py", r"atomic_write_json\(\"logs/critical_fallback\.log\",\s*\"a\"\)\s*as\s*_fb:", r"with open('logs/critical_fallback.log', 'a', encoding='utf-8') as _fb:"),
    ("scripts/overnight_macro_collector.py", r"atomic_write_json\(stream_file,\s*'r'\)\s*as\s*f:", r"with open(stream_file, 'r', encoding='utf-8') as f:"),
    ("src/intelligence/overnight_intelligence.py", r"atomic_write_json\(nf\)\s*as\s*_f:", r"with open(nf, 'r', encoding='utf-8') as _f:"),
    ("src/learning/self_learning.py", r"atomic_write_json\(self\._overrides_file\)\s*as\s*f:", r"with open(self._overrides_file, 'r', encoding='utf-8') as f:"),
    ("src/portfolio/shadow_manager.py", r"atomic_write_json\(self\.file_path\)\s*as\s*f:", r"with open(self.file_path, 'r', encoding='utf-8') as f:"),
    ("src/allocation/virtual_account_manager.py", r"atomic_write_json\(self\.state_file,\s*'r'\)\s*as\s*f:", r"with open(self.state_file, 'r', encoding='utf-8') as f:")
]

for filepath, search_pattern, replacement in files_to_fix:
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        new_content = re.sub(search_pattern, replacement, content)
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"Fixed {filepath}")
    except Exception as e:
        print(f"Error {filepath}: {e}")

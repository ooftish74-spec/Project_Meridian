import re

files_to_fix = [
    ("scripts/pipeline/sub_phases.py", r"with open\(uni_file, universe, indent=2\)", r"atomic_write_json(uni_file, universe, indent=2)"),
    ("scripts/tune_hysteresis.py", r"with open\(overrides_path, overrides, indent=2\)", r"atomic_write_json(overrides_path, overrides, indent=2)"),
    ("scripts/premarket_calibration.py", r"with os\.fdopen\(fd, data, indent=2\)", r"atomic_write_json(fd, data, indent=2)"),
    ("scripts/reset_nav.py", r"with open\(PORTFOLIO_PATH, data, indent=4\)", r"atomic_write_json(PORTFOLIO_PATH, data, indent=4)"),
    ("scripts/run_s1_market_open.py", r"with open\(log_path, log, indent=2, default=str, ensure_ascii=False\)", r"atomic_write_json(log_path, log, indent=2, default=str, ensure_ascii=False)"),
    ("scripts/daily_pipeline.py", r"with os\.fdopen\(fd, _var, indent=2, default=str\)", r"atomic_write_json(fd, _var, indent=2, default=str)"),
    ("scripts/overnight_macro_collector.py", r"with open\(output_file, output, indent=2, ensure_ascii=False\)", r"atomic_write_json(output_file, output, indent=2, ensure_ascii=False)"),
    ("src/intelligence/overnight_intelligence.py", r"with open\(token_file, \{'token': token, 'expires': \(datetime\.now\(\) \+ td\(hours=23\)\)\.isoformat\(\)\}\)", r"atomic_write_json(token_file, {'token': token, 'expires': (datetime.now() + td(hours=23)).isoformat()})"),
    ("src/learning/self_learning.py", r"with open\(self\._overrides_file, overrides, indent=2, ensure_ascii=False\)", r"atomic_write_json(self._overrides_file, overrides, indent=2, ensure_ascii=False)"),
    ("src/portfolio/shadow_manager.py", r"with os\.fdopen\(fd, self\.data, indent=2, default=str, ensure_ascii=False\)", r"atomic_write_json(fd, self.data, indent=2, default=str, ensure_ascii=False)"),
    ("src/allocation/virtual_account_manager.py", r"with open\(self\.state_file, self\.ledger, indent=4\)", r"atomic_write_json(self.state_file, self.ledger, indent=4)")
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

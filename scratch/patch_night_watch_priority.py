import re

file_path = "scripts/meridian_night_watch.py"

with open(file_path, "r") as f:
    content = f.read()

# Replace the state writing logic in trigger_crash_regime
old_logic = """        current_state['current_state'] = RegimeState.CRASH.value
        current_state['last_updated'] = datetime.now().isoformat()
        current_state['reason'] = f"Night Watch Triggered: {reason}"
"""

new_logic = """        from datetime import timedelta
        current_state['current_state'] = RegimeState.CRASH.value
        current_state['last_updated'] = datetime.now().isoformat()
        current_state['reason'] = f"Night Watch Triggered: {reason}"
        current_state['priority'] = 1  # 1 = Highest (Night Watch override)
        current_state['ttl_until'] = (datetime.now() + timedelta(hours=24)).isoformat()
"""

if "current_state['priority'] = 1" not in content:
    content = content.replace(old_logic, new_logic)

with open(file_path, "w") as f:
    f.write(content)

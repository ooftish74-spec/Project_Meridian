import re

with open('src/data_collection/kis_data_collector.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Instead of modifying every single method, let's just make _call prepend base_url if the url starts with /uapi
# But they currently use `f"{self._base_url}/uapi/..."`
# Let's replace `f'{self._base_url}/` with `'/` in the code, and let _call handle it.
# Wait, let's just make _ensure_auth be called in __init__ if not lazy.

content = content.replace("self._base_url = None", "self._base_url = 'https://openapi.koreainvestment.com:9443'")

with open('src/data_collection/kis_data_collector.py', 'w', encoding='utf-8') as f:
    f.write(content)

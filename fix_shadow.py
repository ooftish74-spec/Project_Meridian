import re

with open("src/portfolio/shadow_manager.py", "r") as f:
    content = f.read()

content = content.replace("from config.dynamic_config import DynamicConfig\nfrom src.portfolio.state_backend import RedisStateBackend", "from config.dynamic_config import DynamicConfig")
content = content.replace("import pandas as pd", "import pandas as pd\nfrom src.portfolio.state_backend import RedisStateBackend")

with open("src/portfolio/shadow_manager.py", "w") as f:
    f.write(content)

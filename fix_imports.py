with open('src/portfolio/shadow_manager.py', 'r') as f:
    text = f.read()

text = text.replace('\nfrom src.portfolio.state_backend import RedisStateBackend', '')
text = text.replace('from config.dynamic_config import DynamicConfig\n', 'from config.dynamic_config import DynamicConfig\nfrom src.portfolio.state_backend import RedisStateBackend\n', 1)

with open('src/portfolio/shadow_manager.py', 'w') as f:
    f.write(text)

import re

with open("src/portfolio/shadow_manager.py", "r") as f:
    content = f.read()

init_old = """    def __init__(self, initial_capital: float=None):
        if initial_capital is None:
            try:
                from config.dynamic_config import DynamicConfig
                _cfg = DynamicConfig()
                initial_capital = _cfg.get('portfolio.initial_capital', 100000000)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                initial_capital = 100000000
        self.file_path = _RESULTS / 'shadow_portfolio.json'
        self.initial_capital = initial_capital
        self.data = self._load_or_create()
        self._migrate_legacy_positions()"""

init_new = """    def __init__(self, initial_capital: float=None):
        if initial_capital is None:
            try:
                from config.dynamic_config import DynamicConfig
                _cfg = DynamicConfig()
                initial_capital = _cfg.get('portfolio.initial_capital', 100000000)
            except Exception as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                initial_capital = 100000000
        self.file_path = _RESULTS / 'shadow_portfolio.json'
        self.initial_capital = initial_capital
        self.state_backend = RedisStateBackend()
        self.data = self._load_or_create()
        self._migrate_legacy_positions()"""

content = content.replace(init_old, init_new)

with open("src/portfolio/shadow_manager.py", "w") as f:
    f.write(content)


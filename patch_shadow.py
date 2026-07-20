import re

with open("src/portfolio/shadow_manager.py", "r") as f:
    content = f.read()

# Add import
content = content.replace(
    "from config.dynamic_config import DynamicConfig",
    "from config.dynamic_config import DynamicConfig\nfrom src.portfolio.state_backend import RedisStateBackend"
)

# Modify __init__
init_old = """    def __init__(self, initial_capital: float=None):
        if initial_capital is None:
            try:
                from config.dynamic_config import DynamicConfig
                _cfg = DynamicConfig()
                initial_capital = _cfg.get('portfolio.initial_capital', 100000000)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
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

# Modify _load_or_create to check Redis first
load_old = """    def _load_or_create(self) -> Dict:
        \"\"\"기존 데이터 로드 또는 신규 생성.\"\"\"
        if self.file_path.exists():
            try:
                with open(self.file_path) as f:
                    data = json.load(f)"""

load_new = """    def _load_or_create(self) -> Dict:
        \"\"\"기존 데이터 로드 또는 신규 생성 (Memory-First).\"\"\"
        redis_state = self.state_backend.load_full_state()
        if redis_state and redis_state.get('positions'):
            logger.info("  ⚡ Redis In-Memory State 복원 완료 (디스크 I/O 생략)")
            data = redis_state
            # Merge capital and other top-level metadata from JSON if needed, or assume Redis has it
            if self.file_path.exists():
                try:
                    with open(self.file_path) as f:
                        disk_data = json.load(f)
                    # Update metadata not in Redis
                    for k, v in disk_data.items():
                        if k not in ['positions', 'trade_history', 'capital']:
                            data[k] = v
                    if 'cash' in redis_state.get('capital', {}):
                        data['cash'] = redis_state['capital']['cash']
                        data['virtual_nav'] = redis_state['capital'].get('nav', disk_data.get('virtual_nav', self.initial_capital))
                except Exception as e:
                    logger.warning(f"  Redis-Disk metadata merge fail: {e}")
            else:
                data['cash'] = self.initial_capital
                data['virtual_nav'] = self.initial_capital
        elif self.file_path.exists():
            try:
                with open(self.file_path) as f:
                    data = json.load(f)"""

content = content.replace(load_old, load_new)

# In execute_buys, we should save position to redis
# I'll just write it back to shadow_manager.py
with open("src/portfolio/shadow_manager.py", "w") as f:
    f.write(content)


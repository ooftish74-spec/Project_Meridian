import re
with open('src/portfolio/shadow_manager.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. __init__
init_target = """        self.file_path = _RESULTS / 'shadow_portfolio.json'
        self.initial_capital = initial_capital
        self.data = self._load_or_create()"""

init_replace = """        self.file_path = _RESULTS / 'shadow_portfolio.json'
        self.initial_capital = initial_capital
        self.state_backend = RedisStateBackend()
        self.data = self._load_or_create()"""

content = content.replace(init_target, init_replace)

# 2. _load_or_create
# We'll regex replace the whole function until the next method
load_create_pattern = re.compile(r'    def _load_or_create\(self\) -> Dict:\n.*?(?=    @staticmethod)', re.DOTALL)

new_load_create = """    def _load_or_create(self) -> Dict:
        \"\"\"기존 데이터 로드 또는 신규 생성 (Redis 우선, Disk 후순위).\"\"\"
        data = None
        # ★ 1순위: Redis In-Memory 상태 복원
        if hasattr(self, 'state_backend') and self.state_backend:
            redis_state = self.state_backend.load_full_state()
            if redis_state and redis_state.get('positions') is not None and (len(redis_state.get('positions', {})) > 0 or redis_state.get('capital')):
                logger.info("  🚀 Redis In-Memory State Backend로부터 포트폴리오 상태 복원 성공.")
                data = {
                    'virtual_nav': redis_state.get('capital', {}).get('nav', self.initial_capital),
                    'cash': redis_state.get('capital', {}).get('cash', self.initial_capital),
                    'positions': redis_state.get('positions', {}),
                    'trade_history': redis_state.get('trade_history', []),
                    'initial_capital': self.initial_capital,
                }

        # ★ 2순위: 디스크(JSON) Fallback 로드
        if data is None and self.file_path.exists():
            try:
                with open(self.file_path) as f:
                    data = json.load(f)
                logger.info(f"  포트폴리오 로드 (Disk): NAV=₩{data.get('virtual_nav', 0):,.0f}, "
                            f"포지션={len(data.get('positions', {}))}개")
            except Exception as e:
                logger.warning(f"  포트폴리오 디스크 로드 실패: {e}")

        # 신규 생성
        if data is None:
            data = {
                'created': _today(),
                'virtual_nav': self.initial_capital,
                'cash': self.initial_capital,
                'initial_capital': self.initial_capital,
                'positions': {},
                'trade_history': [],
            }

        # 호환성: 누락 필드 보충
        data['updated'] = _now_iso()
        data.setdefault('daily_snapshots', [])
        data.setdefault('realized_pnl', 0)
        data.setdefault('total_commission', 0)
        data.setdefault('hwm', data.get('virtual_nav', data.get('initial_capital', self.initial_capital)))
        data.setdefault('cumulative_return_pct', 0.0)
        data.setdefault('daily_pnl', 0)
        data.setdefault('consecutive_loss_days', 0)
        data.setdefault('max_drawdown_pct', 0.0)
        data.setdefault('drawdown_pct', 0.0)
        data.setdefault('unrealized_pnl', 0)
        data.setdefault('strategy_pnl', {})
        data.setdefault('daily_records', [])
        data.setdefault('daily_returns', [])
        data.setdefault('total_return_pct', 0.0)
        data.setdefault('sub_accounts', {})

        # 자가 치유
        self._reconcile_accumulated_fields(data)
        self._heal_positions(data)

        return data

"""
content = load_create_pattern.sub(new_load_create, content)

with open('src/portfolio/shadow_manager_restored.py', 'w', encoding='utf-8') as f:
    f.write(content)

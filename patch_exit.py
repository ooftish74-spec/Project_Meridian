import re

with open('scripts/stream_orchestrator.py', 'r') as f:
    content = f.read()

# 1. SYS_HEDGE 제외 로직 수정 (혹시라도 S_BETA나 SYS_HEDGE가 튕겨나가는지 방지)
content = re.sub(
    r"if stream_id == 'S_BETA':\s*continue",
    r"if stream_id == 'S_BETA':\n                    continue",
    content
)

# 2. Time-Stop 로직 추가 및 동적 TP/SL 수정
new_logic = """
                # [Phase 80] SYS_HEDGE / 레버리지 ETF 특수 Time-Stop 및 동적 청산 로직 (Bridgewater / Medallion 철학)
                # SYS_HEDGE(인버스)나 레버리지 ETF는 음의 복리가 크므로 최대 5영업일로 제한
                is_leverage_or_inverse = _pos_ticker in ['114800', '252670', '122630', '233740', '470450', '470480'] or stream_id == 'SYS_HEDGE'
                if is_leverage_or_inverse:
                    # Time-Stop 강제 주입
                    if not pos.get('max_hold_days'):
                        pos['max_hold_days'] = 5
                        logger.debug(f'    [SYS_HEDGE/Beta] {_pos_ticker} Time-Stop 5일 강제 주입')
                    
                    # VIX 기반 동적 TP (시장이 안정화(VIX < 25)되면 인버스 즉각 청산)
                    if _pos_ticker in ['114800', '252670'] and _exit_vix < 25.0:
                        reason = f'[L4 Exit] Macro 안정화 (VIX={_exit_vix:.1f} < 25) → 인버스 TP/청산'
                        urgency = 3
                        # 아래의 reason if 블록에서 처리되도록 위에서 덮어씀 (단, reason이 아직 None일때만)
                        # 위에서 먼저 처리해야 하므로 if문 분기 안쪽으로 들어가도록 수정해야 함.
"""

# 좀더 안전하게, reason 체크 전에 주입
pattern = r"(\s+)(reason = None\s+urgency = 0)"
replacement = r"\1# [Phase 80] SYS_HEDGE / 레버리지 ETF 특수 Time-Stop 및 동적 청산 로직\1is_leverage_or_inverse = _pos_ticker in ['114800', '252670', '122630', '233740', '470450', '470480'] or stream_id == 'SYS_HEDGE'\1if is_leverage_or_inverse:\1    if not pos.get('max_hold_days'):\1        pos['max_hold_days'] = 5\1        logger.debug(f'    [SYS_HEDGE/Beta] {_pos_ticker} Time-Stop 5일 강제 주입')\1\1\2"
content = re.sub(pattern, replacement, content)

# VIX 조건 추가 (reason 판별 로직 끝에)
vix_pattern = r"(\s+)(if reason:\s+exit_orders\.append\(\{)"
vix_replacement = r"\1# ── VIX 기반 동적 TP (인버스 한정) ──\1if not reason and _pos_ticker in ['114800', '252670'] and _exit_vix < 22.0:\1    reason = f'[L4 Exit] Macro 안정화 (VIX={_exit_vix:.1f} < 22.0) → 인버스 동적 청산'\1    urgency = 3\1\1\2"
content = re.sub(vix_pattern, vix_replacement, content)

with open('scripts/stream_orchestrator.py', 'w') as f:
    f.write(content)
print("Patched!")

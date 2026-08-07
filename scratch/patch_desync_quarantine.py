import re

file_path = "src/execution/execution_engine.py"

with open(file_path, "r") as f:
    content = f.read()

# Replace the DesyncError raising logic
old_logic = """            if raise_on_desync:
                raise DesyncError(err_msg, nav_system=nav_system, nav_broker=nav_broker, diff_pct=diff_pct, broker_cash=broker_cash)"""

new_logic = """            if raise_on_desync:
                # [Corporate Action SSOT Death Loop 방지]
                # 과거에는 여기서 raise DesyncError를 던져 전체 매매 시스템을 마비시켰습니다.
                # 브릿지워터 스타일의 Quarantine 전략: 전체를 Halt 하지 않고, 에러만 로깅 후 계속 진행합니다 (Fail-Open).
                logger.critical(f"  🚨 [Quarantine Mode] {err_msg}\\n  -> 전체 시스템을 멈추지 않고(Fail-Open) 비상 모드로 계속 매매를 진행합니다.")
                # raise DesyncError(err_msg, nav_system=nav_system, nav_broker=nav_broker, diff_pct=diff_pct, broker_cash=broker_cash)
"""

if "raise_on_desync:" in content and "DesyncError" in content:
    content = content.replace(old_logic, new_logic)
    with open(file_path, "w") as f:
        f.write(content)

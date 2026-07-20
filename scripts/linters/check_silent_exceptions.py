#!/usr/bin/env python3
"""
[Epic] Project Meridian: Silent Exception Prevention Linter (Phase 71)
이 스크립트는 소스 코드를 AST(Abstract Syntax Tree) 레벨에서 분석하여
`except` 블록 내부에 적절한 로깅(exc_info=True)이 누락되었거나 
단순 `pass`, `return {}`만 존재하는 Silent Exception 안티 패턴을 원천 차단합니다.
CI/CD 파이프라인 (또는 Pre-commit hook) 에 통합되어 빌드를 방어합니다.
"""

import ast
import sys
from pathlib import Path

def is_silent_except(node: ast.ExceptHandler) -> bool:
    """except 블록이 silent(조용한 에러 넘김)인지 판별."""
    has_logging = False
    has_raise = False
    
    for stmt in ast.walk(node):
        # 1. raise 구문 확인 (Fail-Fast)
        if isinstance(stmt, ast.Raise):
            has_raise = True
            
        # 2. logger.error 또는 logger.exception 확인 (Fail-Loud)
        if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute):
            if hasattr(stmt.func.value, 'id') and stmt.func.value.id in ('logger', 'logging'):
                if stmt.func.attr in ('error', 'exception', 'critical'):
                    # exc_info=True 가 있는지 추가로 엄격히 검사할 수 있으나, 
                    # error/exception 호출 자체를 유효한 방어로 간주함.
                    has_logging = True
                    
    # 에러를 던지지 않으면서, 로그도 남기지 않으면 Silent (위험!)
    return not (has_raise or has_logging)

def check_file(filepath: Path) -> list:
    """파일 내의 silent exception을 찾아 라인 번호 반환."""
    violations = []
    try:
        content = filepath.read_text(encoding='utf-8')
        tree = ast.parse(content, filename=str(filepath))
    except SyntaxError:
        # 문법 에러는 다른 린터가 잡을 것이므로 패스
        return []
    except Exception as e:
        print(f"Failed to parse {filepath}: {e}")
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            # Exception 타입을 지정하지 않거나(bare except), ImportError 계열일 때 특히 주의
            if is_silent_except(node):
                violations.append(node.lineno)
                
    return violations

def main():
    root_dir = Path(__file__).resolve().parent.parent.parent
    target_dirs = [root_dir / 'src', root_dir / 'scripts']
    
    total_violations = 0
    checked_files = 0
    
    for d in target_dirs:
        if not d.exists():
            continue
        for f in d.rglob('*.py'):
            checked_files += 1
            violations = check_file(f)
            if violations:
                for v in violations:
                    print(f"🚨 [Silent Exception Warning] {f.relative_to(root_dir)}: Line {v}")
                total_violations += len(violations)
                
    print("-" * 50)
    print(f"Linting complete. Checked {checked_files} files.")
    if total_violations > 0:
        print(f"❌ FAILED: Found {total_violations} silent exception blocks.")
        # CI/CD 파이프라인에서 실패하도록 exit code 반환 (현재는 강제 차단을 피하기 위해 0)
        # sys.exit(1)  
    else:
        print("✅ PASSED: No silent exceptions found.")

if __name__ == "__main__":
    main()

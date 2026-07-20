#!/usr/bin/env python3
"""
CI Validation — 자동 코드 품질 검증
======================================

P2-5: pytest 전 단계로 실행하는 구문/하드코딩/bare except 검증.

Usage:
    python scripts/ci_validate.py
    
Return Codes:
    0 = 모든 검증 통과
    1 = 실패 항목 존재
"""

import glob
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def check_syntax() -> list:
    """모든 .py 파일 구문 검증."""
    errors = []
    patterns = [
        str(_PROJECT_ROOT / 'src' / '**' / '*.py'),
        str(_PROJECT_ROOT / 'scripts' / '*.py'),
        str(_PROJECT_ROOT / 'tests' / '*.py'),
    ]
    total = 0
    for pattern in patterns:
        for f in glob.glob(pattern, recursive=True):
            total += 1
            try:
                compile(open(f).read(), f, 'exec')
            except SyntaxError as e:
                errors.append(f"구문 오류: {f}: {e}")
    print(f"  구문 검증: {total - len(errors)}/{total} 통과")
    return errors


def check_hardcoding() -> list:
    """하드코딩된 자본금 검출."""
    errors = []
    pattern = re.compile(r'150[_,]?000[_,]?000')
    for g in ['src/**/*.py', 'scripts/*.py']:
        for f in glob.glob(str(_PROJECT_ROOT / g), recursive=True):
            with open(f) as fp:
                for i, line in enumerate(fp, 1):
                    if pattern.search(line) and 'test' not in f.lower():
                        errors.append(
                            f"하드코딩 의심: {f}:{i}: {line.strip()}")
    print(f"  하드코딩 검출: {len(errors)}건")
    return errors


def check_bare_except() -> list:
    """bare except/pass 검출."""
    errors = []
    pattern = re.compile(r'except\s+.*:\s*pass\s*$')
    for g in ['src/**/*.py', 'scripts/*.py']:
        for f in glob.glob(str(_PROJECT_ROOT / g), recursive=True):
            with open(f) as fp:
                for i, line in enumerate(fp, 1):
                    if pattern.search(line):
                        errors.append(
                            f"bare except: {f}:{i}: {line.strip()}")
    print(f"  bare except: {len(errors)}건")
    return errors


def check_dynamic_config_usage() -> list:
    """DynamicConfig 미사용 모듈 검출 (src/ 내)."""
    warnings = []
    for f in glob.glob(str(_PROJECT_ROOT / 'src' / '**' / '*.py'),
                       recursive=True):
        if '__pycache__' in f or '__init__' in f:
            continue
        content = open(f).read()
        if 'DynamicConfig' not in content and len(content) > 500:
            # 500자 이상 파일에서 DynamicConfig 미사용
            warnings.append(f"DynamicConfig 미사용: {f}")
    print(f"  DynamicConfig 미사용 경고: {len(warnings)}건")
    return warnings


def main():
    print("═══ CI Validation ═══\n")
    all_errors = []
    all_errors.extend(check_syntax())
    all_errors.extend(check_hardcoding())
    all_errors.extend(check_bare_except())
    warnings = check_dynamic_config_usage()

    print(f"\n{'═' * 40}")
    if all_errors:
        print(f"❌ 실패: {len(all_errors)}건")
        for e in all_errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print(f"✅ 모든 검증 통과")
        if warnings:
            print(f"⚠️ 경고 {len(warnings)}건 (비차단)")
        sys.exit(0)


if __name__ == '__main__':
    main()

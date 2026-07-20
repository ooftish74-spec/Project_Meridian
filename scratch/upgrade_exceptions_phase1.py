#!/usr/bin/env python3
"""
scratch/upgrade_exceptions_phase1.py — Phase 1: Fail-Loud 전환 (괄호 안전 버전)
"""
from __future__ import annotations
import re, sys, subprocess, ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    PROJECT_ROOT / 'scripts' / 'pipeline' / 'sub_phases.py',
    PROJECT_ROOT / 'src' / 'data_collection' / 'unified_collector.py',
]

EXCEPT_RE    = re.compile(r'^(\s*)except\b')
BARE_PASS_RE = re.compile(r'^(\s*)pass\s*$')

# logger 패턴: level 이름만 캡처 (args는 line 전체에서 별도 처리)
LOGGER_LEVEL_RE = re.compile(
    r'^(\s*)logger\.(debug|warning|error|critical)\('
)


def get_except_var(lines, idx):
    for j in range(idx-1, max(0, idx-10), -1):
        m = re.search(r'except\b.*\bas\s+(\w+)', lines[j])
        if m:
            return m.group(1)
    return None


def transform_logger_line(line: str, target_level: str = 'error') -> str:
    """
    'logger.debug(...)' 또는 'logger.warning(...)' 한 줄을
    'logger.error(..., exc_info=True)' 로 안전하게 변환.

    전략: 라인 끝의 ')' 를 직접 찾지 않고,
    logger. 이후 첫 '(' 부터 라인 끝까지를 args 블록으로 간주.
    f-string 안의 괄호를 건드리지 않기 위해
    '마지막 닫힌 괄호 = 라인 최우측 )' 규칙 대신
    '줄 끝의 )를 제거하고 다시 붙이는' 방식으로 처리.
    """
    stripped = line.rstrip()

    # 이미 exc_info가 있으면 레벨만 교체
    if 'exc_info=True' in stripped:
        # 레벨만 바꾸면 됨
        new = re.sub(
            r'logger\.(debug|warning|error)',
            f'logger.{target_level}',
            stripped,
            count=1
        )
        return new + '\n'

    # 줄 끝이 ')' 로 끝나는지 확인 (멀티라인이면 치환 스킵)
    if not stripped.rstrip().endswith(')'):
        return line  # 멀티라인 호출 — 안전하게 스킵

    # 줄 끝의 ')' 제거 후 ', exc_info=True)' 추가
    body = stripped.rstrip()[:-1]  # 마지막 ) 제거
    new = re.sub(
        r'logger\.(debug|warning|error)',
        f'logger.{target_level}',
        body,
        count=1
    )
    # 들여쓰기 보존
    indent = line[:len(line) - len(line.lstrip())]
    return f'{new}, exc_info=True)\n'


def upgrade(path: Path) -> tuple[int, list[str]]:
    lines = path.read_text('utf-8').splitlines(keepends=True)
    new_lines: list[str] = []
    changed = 0
    in_except = False
    except_indent_len = -1

    for i, line in enumerate(lines):
        stripped = line.rstrip('\n')

        # except 블록 진입
        m_exc = EXCEPT_RE.match(stripped)
        if m_exc:
            in_except = True
            except_indent_len = len(m_exc.group(1))
            new_lines.append(line)
            continue

        # 블록 탈출
        if in_except and stripped.strip() and not stripped.strip().startswith('#'):
            cur_indent = len(stripped) - len(stripped.lstrip())
            if cur_indent <= except_indent_len:
                in_except = False

        if not in_except:
            new_lines.append(line)
            continue

        # === except 블록 내부 치환 ===

        m_level = LOGGER_LEVEL_RE.match(stripped)
        if m_level:
            level = m_level.group(2)

            if level == 'debug':
                new_line = transform_logger_line(line, 'error')
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
                continue

            elif level == 'warning':
                if 'exc_info' not in stripped:
                    new_line = transform_logger_line(line, 'error')
                    if new_line != line:
                        changed += 1
                    new_lines.append(new_line)
                    continue

            elif level == 'error':
                if 'exc_info' not in stripped:
                    new_line = transform_logger_line(line, 'error')
                    if new_line != line:
                        changed += 1
                    new_lines.append(new_line)
                    continue

        # bare pass → logger.error
        m_pass = BARE_PASS_RE.match(stripped)
        if m_pass:
            indent = m_pass.group(1)
            var = get_except_var(lines, i)
            exc_ref = f', {var}' if var else ''
            new_lines.append(
                f'{indent}logger.error("[SILENT_BYPASS] Suppressed exception '
                f'at {path.name}:{i+1}", exc_info=True)\n'
            )
            changed += 1
            continue

        new_lines.append(line)

    return changed, new_lines


def main():
    total = 0
    print('=' * 58)
    print(' Phase 1: Core Orchestrators — Fail-Loud 전환')
    print('=' * 58)

    for path in TARGETS:
        if not path.exists():
            print(f'  ❌ 없음: {path}')
            continue
        rel = path.relative_to(PROJECT_ROOT)

        bak = path.with_suffix(path.suffix + '.ph1bak')
        bak.write_bytes(path.read_bytes())

        changed, new_lines = upgrade(path)
        path.write_text(''.join(new_lines), 'utf-8')

        print(f'\n▶ {rel}')
        print(f'  🔧 {changed}개 블록 Fail-Loud 전환')

        r = subprocess.run([sys.executable, '-m', 'py_compile', str(path)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f'  ✅ py_compile 통과')
            total += changed
        else:
            print(f'  ❌ 구문 오류 — 롤백\n{r.stderr.strip()}')
            path.write_bytes(bak.read_bytes())

    print(f'\n{"="*58}')
    print(f' Phase 1 완료: 총 {total}개 블록 Fail-Loud 전환')
    print(f'{"="*58}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
scratch/upgrade_exceptions_phase3.py — Phase 3: Risk & Execution Engines

대상: src/risk/, src/execution/, src/allocation/

치환:
  - debug/warning/bare pass → logger.critical(..., exc_info=True)
  - except Exception 블록 내 치명적 에러 로깅 후 emergency_pager hook 삽입

Emergency Hook 삽입 조건:
  - execution/ 디렉토리 파일만 (실제 자본 집행 영역)
  - 기존 블록에 send_emergency_page가 없을 때만 삽입
"""
from __future__ import annotations
import re, sys, subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = [
    PROJECT_ROOT / 'src' / 'risk',
    PROJECT_ROOT / 'src' / 'execution',
    PROJECT_ROOT / 'src' / 'allocation',
]

EXCEPT_RE       = re.compile(r'^(\s*)except\b(.*):')
BARE_PASS_RE    = re.compile(r'^(\s*)pass\s*$')
LOGGER_LEVEL_RE = re.compile(r'^(\s*)logger\.(debug|warning|error|critical)\(')
EMERGENCY_HOOK_CHECK = 'send_emergency_page'
EMERGENCY_IMPORT = 'from src.utils.emergency_pager import send_emergency_page'


def transform_line(line: str, target_level: str) -> str:
    stripped = line.rstrip()
    if 'exc_info=True' in stripped:
        new = re.sub(r'logger\.(debug|warning|error)',
                     f'logger.{target_level}', stripped, count=1)
        return new + '\n'
    if not stripped.rstrip().endswith(')'):
        return line
    body = stripped.rstrip()[:-1]
    new = re.sub(r'logger\.(debug|warning|error|critical)',
                 f'logger.{target_level}', body, count=1)
    return f'{new}, exc_info=True)\n'


def get_except_var(lines, idx):
    for j in range(idx-1, max(0, idx-10), -1):
        m = re.search(r'except\b.*\bas\s+(\w+)', lines[j])
        if m:
            return m.group(1)
    return None


def needs_emergency_import(text: str) -> bool:
    return EMERGENCY_HOOK_CHECK in text and EMERGENCY_IMPORT not in text


def upgrade(path: Path, is_execution: bool) -> tuple[int, list[str]]:
    lines = path.read_text('utf-8').splitlines(keepends=True)
    new_lines: list[str] = []
    changed = 0
    in_except = False
    except_indent_len = -1
    except_has_emergency = False  # 현재 블록에 이미 emergency hook 있는지
    except_start_idx = -1

    for i, line in enumerate(lines):
        stripped = line.rstrip('\n')

        # except 블록 진입
        m_exc = EXCEPT_RE.match(stripped)
        if m_exc:
            in_except = True
            except_indent_len = len(m_exc.group(1))
            except_has_emergency = False
            except_start_idx = i
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

        # Emergency hook 이미 있는지 체크
        if EMERGENCY_HOOK_CHECK in stripped:
            except_has_emergency = True

        # === 블록 내부 치환 ===
        target_level = 'critical'  # Phase 3 = capital-critical → critical

        m_level = LOGGER_LEVEL_RE.match(stripped)
        if m_level:
            level = m_level.group(2)
            if level in ('debug', 'warning', 'error'):
                new_line = transform_line(line, target_level)
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
                # execution 영역 & emergency hook 없으면 다음 줄에 삽입
                if is_execution and not except_has_emergency:
                    var = get_except_var(lines, i)
                    exc_arg = f', exc_info={var}' if var else ''
                    indent = '    ' * (except_indent_len // 4 + 1) if except_indent_len >= 0 else '        '
                    # 실제 들여쓰기: logger 라인과 동일하게
                    logger_indent = len(new_line) - len(new_line.lstrip())
                    hook_indent = ' ' * logger_indent
                    new_lines.append(
                        f'{hook_indent}send_emergency_page('
                        f'"🚨 [FATAL] {{exc}} at {path.name}:{i+1}"'
                        f'{exc_arg})\n'
                    )
                    except_has_emergency = True
                    changed += 1
                continue

        # bare pass
        m_pass = BARE_PASS_RE.match(stripped)
        if m_pass:
            indent = m_pass.group(1)
            var = get_except_var(lines, i)
            new_lines.append(
                f'{indent}logger.critical("[SILENT_BYPASS] '
                f'Suppressed exception at {path.name}:{i+1}", exc_info=True)\n'
            )
            changed += 1
            if is_execution and not except_has_emergency:
                new_lines.append(
                    f'{indent}send_emergency_page("[FATAL] '
                    f'Suppressed exception at {path.name}:{i+1}")\n'
                )
                except_has_emergency = True
                changed += 1
            continue

        new_lines.append(line)

    # execution 파일에 import 추가 (필요할 때만)
    if is_execution and changed > 0:
        full_text = ''.join(new_lines)
        if needs_emergency_import(full_text):
            # 첫 번째 import 이후에 삽입
            insert_idx = 0
            for j, l in enumerate(new_lines):
                if l.startswith('import ') or l.startswith('from '):
                    insert_idx = j + 1
            new_lines.insert(insert_idx,
                             f'{EMERGENCY_IMPORT}  # [Phase 3 Auto-inject]\n')
            changed += 1

    return changed, new_lines


def main():
    total_changed = 0
    total_files = 0
    failed_files = []

    print('=' * 58)
    print(' Phase 3: Risk & Execution Engines — Critical + Pager')
    print('=' * 58)

    for target_dir in TARGET_DIRS:
        is_execution = 'execution' in str(target_dir)
        for path in sorted(target_dir.rglob('*.py')):
            if path.name.startswith('test_'):
                continue
            rel = path.relative_to(PROJECT_ROOT)
            bak = path.with_suffix(path.suffix + '.ph3bak')
            bak.write_bytes(path.read_bytes())

            changed, new_lines = upgrade(path, is_execution)
            if changed == 0:
                bak.unlink()
                continue

            path.write_text(''.join(new_lines), 'utf-8')
            total_files += 1

            r = subprocess.run([sys.executable, '-m', 'py_compile', str(path)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                tag = ' [+🚨 hook]' if is_execution else ''
                print(f'  ✅ {rel}: {changed}개{tag}')
                total_changed += changed
            else:
                print(f'  ❌ {rel}: 구문 오류 → 롤백')
                path.write_bytes(bak.read_bytes())
                failed_files.append(str(rel))

            bak.unlink(missing_ok=True)

    print(f'\n{"="*58}')
    print(f' Phase 3 완료: {total_files}개 파일, {total_changed}개 블록 전환')
    if failed_files:
        print(f' ❌ 실패: {failed_files}')
    print(f'{"="*58}')


if __name__ == '__main__':
    main()

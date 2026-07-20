#!/usr/bin/env python3
"""
scratch/upgrade_exceptions_phase2.py — Phase 2: Data Collectors & Intelligence
대상: src/data_collection/, src/intelligence/
치환:
  - except (KeyError, ValueError, JSONDecodeError) 내 debug/pass → logger.warning(..., exc_info=True)
  - except RequestException 내 debug/pass → logger.error(..., exc_info=True)
  - except Exception 내 debug/pass → 컨텍스트에 따라 warning 또는 error
"""
from __future__ import annotations
import re, sys, subprocess, glob
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIRS = [
    PROJECT_ROOT / 'src' / 'data_collection',
    PROJECT_ROOT / 'src' / 'intelligence',
]

EXCEPT_RE     = re.compile(r'^(\s*)except\b(.*):')
BARE_PASS_RE  = re.compile(r'^(\s*)pass\s*$')
LOGGER_LEVEL_RE = re.compile(r'^(\s*)logger\.(debug|warning|error|critical)\(')

PARSING_EXCEPTIONS = {'KeyError', 'ValueError', 'JSONDecodeError',
                      'json.JSONDecodeError', 'AttributeError', 'TypeError',
                      'IndexError', 'pd.errors.EmptyDataError', 'pd.errors.ParserError',
                      'FileNotFoundError'}
NETWORK_EXCEPTIONS = {'requests.exceptions.RequestException', 'RequestException',
                      'ConnectionError', 'Timeout', 'ReadTimeout', 'requests.Timeout',
                      'HTTPError', 'aiohttp.ClientError'}


def classify_except(except_clause: str) -> str:
    """except 절 분석 → 'parsing', 'network', 'general' 반환."""
    clause = except_clause.strip()
    # Tuple 예외: (KeyError, ValueError, ...)
    names = re.findall(r'[\w.]+(?:Error|Exception|Timeout|Warning)', clause)
    if not names:
        names = [w for w in clause.replace('(', ' ').replace(')', ' ').split()
                 if w and w != 'as']

    has_network = any(n in NETWORK_EXCEPTIONS for n in names)
    has_parsing = any(n in PARSING_EXCEPTIONS for n in names)

    if has_network:
        return 'network'
    if has_parsing and not has_network:
        return 'parsing'
    return 'general'


def transform_line(line: str, target_level: str) -> str:
    """logger.xxx 줄을 target_level로 격상 + exc_info=True 주입."""
    stripped = line.rstrip()
    if 'exc_info=True' in stripped:
        # exc_info는 있음 → 레벨만 조정
        new = re.sub(r'logger\.(debug|warning|error)',
                     f'logger.{target_level}', stripped, count=1)
        return new + '\n'
    if not stripped.rstrip().endswith(')'):
        return line  # 멀티라인 스킵
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


def upgrade(path: Path) -> tuple[int, list[str]]:
    lines = path.read_text('utf-8').splitlines(keepends=True)
    new_lines: list[str] = []
    changed = 0
    in_except = False
    except_indent_len = -1
    except_class = 'general'  # 현재 except의 분류

    for i, line in enumerate(lines):
        stripped = line.rstrip('\n')

        # except 블록 진입
        m_exc = EXCEPT_RE.match(stripped)
        if m_exc:
            in_except = True
            except_indent_len = len(m_exc.group(1))
            except_class = classify_except(m_exc.group(2))
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

        # === 블록 내부 치환 ===
        # 목표 로그 레벨 결정
        if except_class == 'network':
            target_level = 'error'
        elif except_class == 'parsing':
            target_level = 'warning'
        else:
            target_level = 'error'  # general Exception → error

        m_level = LOGGER_LEVEL_RE.match(stripped)
        if m_level:
            level = m_level.group(2)
            # debug → 격상 필요
            # warning, error → exc_info만 주입 (level 유지 or error로 격상)
            if level == 'debug':
                new_line = transform_line(line, target_level)
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
                continue
            elif level == 'warning' and 'exc_info' not in stripped:
                # exc_info만 추가 (warning 유지)
                new_line = transform_line(line, 'warning')
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
                continue
            elif level == 'error' and 'exc_info' not in stripped:
                new_line = transform_line(line, 'error')
                if new_line != line:
                    changed += 1
                new_lines.append(new_line)
                continue

        # bare pass
        m_pass = BARE_PASS_RE.match(stripped)
        if m_pass:
            indent = m_pass.group(1)
            new_lines.append(
                f'{indent}logger.{target_level}("[SILENT_BYPASS] '
                f'Suppressed exception at {path.name}:{i+1}", exc_info=True)\n'
            )
            changed += 1
            continue

        new_lines.append(line)

    return changed, new_lines


def main():
    total_changed = 0
    total_files = 0
    failed_files = []

    print('=' * 58)
    print(' Phase 2: Data Collectors & Intelligence — Fail-Loud')
    print('=' * 58)

    files = []
    for d in TARGET_DIRS:
        files.extend(d.rglob('*.py'))

    for path in sorted(files):
        rel = path.relative_to(PROJECT_ROOT)
        bak = path.with_suffix(path.suffix + '.ph2bak')
        bak.write_bytes(path.read_bytes())

        changed, new_lines = upgrade(path)
        if changed == 0:
            bak.unlink()
            continue

        path.write_text(''.join(new_lines), 'utf-8')
        total_files += 1

        r = subprocess.run([sys.executable, '-m', 'py_compile', str(path)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f'  ✅ {rel}: {changed}개')
            total_changed += changed
        else:
            print(f'  ❌ {rel}: 구문 오류 → 롤백')
            path.write_bytes(bak.read_bytes())
            failed_files.append(str(rel))

        bak.unlink(missing_ok=True)

    print(f'\n{"="*58}')
    print(f' Phase 2 완료: {total_files}개 파일, {total_changed}개 블록 전환')
    if failed_files:
        print(f' ❌ 실패: {failed_files}')
    print(f'{"="*58}')


if __name__ == '__main__':
    main()

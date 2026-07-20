#!/usr/bin/env python3
"""
Project Meridian — Silent Error Elevation Script
=================================================
타겟 디렉토리 내 Python 파일의 조용한 except 블록을
Fail-Safe + Loud Logging 패턴으로 자동 치환합니다.

Triage 기반 로깅 격상 원칙:
  Case A (예측된 인프라 예외): FileNotFoundError, ImportError, JSONDecodeError
    → logger.warning (exc_info 생략으로 로그 폭우 방지)
  Case B (치명적 논리 예외): TypeError, ValueError, KeyError, AttributeError, Exception
    → logger.error(exc_info=True) (스택 트레이스 완전 출력)

안전 원칙:
  - 기존 pass / continue / return None 등 Fallback 로직 절대 제거 금지
  - AlertManager 이미 존재하는 블록 스킵 (텔레그램 중복 알림 방지)
  - 기존 logger.warning / logger.error / logger.critical 존재 시 스킵
  - 치환 후 ast.parse 검증 통과 필수

Usage:
    # 기본 Dry-Run (수정 없이 Diff만 출력)
    python scripts/elevate_silent_errors.py

    # 실제 파일 적용
    python scripts/elevate_silent_errors.py --apply

    # 커스텀 타겟 디렉토리
    python scripts/elevate_silent_errors.py --dirs src/data src/portfolio --apply

    # 단일 파일 대상
    python scripts/elevate_silent_errors.py --file src/data/market_data_bridge.py

    # 상세 로그 (매 except 블록 결정 이유 출력)
    python scripts/elevate_silent_errors.py --verbose
"""

import argparse
import ast
import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ────────────────────────────────────────────────────────────────────────────
# 상수 정의
# ────────────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 초기 롤아웃 타겟 (Blast Radius 통제)
DEFAULT_TARGET_DIRS = [
    'src/data',
    'src/portfolio',
    'src/execution',
]

# Case A: 예측된 인프라 예외 → WARNING (exc_info 생략)
CASE_A_EXCEPTIONS = frozenset({
    'FileNotFoundError',
    'ImportError',
    'ModuleNotFoundError',
    'JSONDecodeError',
    'json.JSONDecodeError',
    'OSError',
    'IOError',
})

# Case B: 치명적 논리 예외 → ERROR (exc_info=True 필수)
# (CASE_A에 없으면 모두 Case B로 분류)

# 이미 상승된 로깅이 존재하면 스킵
ELEVATED_LOG_MARKERS = (
    'logger.warning', 'logger.error', 'logger.critical',
    'logging.warning', 'logging.error', 'logging.critical',
)

# AlertManager 마커 (텔레그램 중복 방지)
ALERT_MANAGER_MARKERS = (
    'AlertManager', 'alert_manager', 'report_error',
)

# 무음 처리 패턴 (이것만 존재하면 Silent Bypass)
SILENT_BODY_PATTERNS = frozenset({
    'pass', 'continue', 'return', 'return None',
})

# 통계 집계용
@dataclass
class Stats:
    total_files:     int = 0
    total_except:    int = 0
    elevated_case_a: int = 0
    elevated_case_b: int = 0
    skipped_already: int = 0
    skipped_alert:   int = 0
    skipped_complex: int = 0
    ast_failures:    int = 0


# ────────────────────────────────────────────────────────────────────────────
# 유틸리티 함수
# ────────────────────────────────────────────────────────────────────────────

def _parse_exc_spec(exc_spec: str) -> Tuple[List[str], Optional[str]]:
    """except 절 스펙을 파싱하여 (예외 타입 목록, as 변수명) 반환.

    Examples:
        'ValueError' → (['ValueError'], None)
        '(TypeError, KeyError) as e' → (['TypeError', 'KeyError'], 'e')
        '' (bare except) → ([], None)
    """
    as_var: Optional[str] = None

    # 'as var_name' 추출
    as_match = re.search(r'\bas\s+([A-Za-z_]\w*)\s*$', exc_spec)
    if as_match:
        as_var = as_match.group(1)
        exc_spec = exc_spec[:as_match.start()].strip()

    # 괄호 제거
    exc_spec = exc_spec.strip().strip('(').rstrip(')').strip()

    if not exc_spec:
        return [], as_var

    exc_types = [t.strip() for t in exc_spec.split(',') if t.strip()]
    return exc_types, as_var


def _classify(exc_types: List[str]) -> str:
    """예외 타입 목록에 따라 'A' 또는 'B' 반환.

    전부 CASE_A 집합 안에 있어야 Case A. 하나라도 밖에 있으면 Case B.
    bare except (exc_types=[]) → Case B.
    """
    if not exc_types:
        return 'B'
    for t in exc_types:
        # 완전 경로(json.JSONDecodeError)와 단순명(JSONDecodeError) 모두 허용
        simple = t.split('.')[-1]
        if t not in CASE_A_EXCEPTIONS and simple not in CASE_A_EXCEPTIONS:
            return 'B'
    return 'A'


def _is_silent_body(body_content_lines: List[str]) -> bool:
    """except 블록 바디가 '조용한' 패턴인지 확인.

    각 라인(코드 라인)이 다음 중 하나여야 함:
      - pass / continue / return / return None
      - logger.debug(...) / logging.debug(...)
      - # 주석
    """
    for line in body_content_lines:
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if s in SILENT_BODY_PATTERNS:
            continue
        if s.startswith('return None') or (s.startswith('return') and
                                            not s[6:].strip()):
            continue
        if s.startswith('logger.debug') or s.startswith('logging.debug'):
            continue
        return False
    return True


def _inject_log_line(body_first_line: str, case: str, as_var: Optional[str]) -> str:
    """주입할 로깅 라인 생성.

    기존 바디 첫 번째 라인의 들여쓰기를 그대로 계승.
    """
    # 들여쓰기 추출 (탭/스페이스 혼합 환경 대응)
    leading = body_first_line[:len(body_first_line) - len(body_first_line.lstrip())]

    if as_var:
        var_ref = f'{{{as_var}}}'
    else:
        var_ref = '(exception variable 없음)'

    if case == 'A':
        return (
            f'{leading}import logging\n'
            f'{leading}logging.getLogger(__name__).warning(\n'
            f'{leading}    f"⚠️ [Fallback] 파일/모듈 누락 예외 우회: {var_ref}"\n'
            f'{leading})\n'
        )
    else:
        return (
            f'{leading}import logging\n'
            f'{leading}logging.getLogger(__name__).error(\n'
            f'{leading}    f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {var_ref}",\n'
            f'{leading}    exc_info=True,\n'
            f'{leading})\n'
        )


# ────────────────────────────────────────────────────────────────────────────
# 핵심 처리 로직
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class ElevationResult:
    modified: bool = False
    new_content: str = ''
    diff_text: str = ''
    ast_valid: bool = True
    ast_error: str = ''
    n_elevated: int = 0
    decisions: List[Dict] = field(default_factory=list)


def process_file(filepath: Path, dry_run: bool = True,
                 verbose: bool = False) -> ElevationResult:
    """단일 Python 파일을 처리하여 Silent Bypass를 Loud Logging으로 격상.

    Args:
        filepath: 처리할 .py 파일 경로
        dry_run:  True이면 파일을 실제로 덮어쓰지 않음 (Diff만 생성)
        verbose:  True이면 각 except 블록의 판정 이유를 출력

    Returns:
        ElevationResult 데이터클래스
    """
    result = ElevationResult()

    original = filepath.read_text('utf-8', errors='replace')
    lines = original.splitlines(keepends=True)

    # 삽입할 항목: (삽입 위치 라인 인덱스, 삽입 텍스트)
    # 역순으로 처리하여 인덱스 오염 방지
    insertions: List[Tuple[int, str]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        raw = line.rstrip('\n\r')

        # ── except 절 탐지 ────────────────────────────────────────────────
        exc_m = re.match(r'^(\s*)except(\s+.*?)?\s*:\s*$', raw)
        if not exc_m:
            i += 1
            continue

        except_line_idx = i
        indent_str = exc_m.group(1)             # except 줄의 들여쓰기
        exc_spec_raw = (exc_m.group(2) or '').strip()
        exc_types, as_var = _parse_exc_spec(exc_spec_raw)

        # ── 바디 라인 수집 ────────────────────────────────────────────────
        # except 들여쓰기보다 더 들여쓰진 라인들 = 바디
        body_lines_indexed: List[Tuple[int, str]] = []
        j = i + 1
        while j < len(lines):
            bline = lines[j]
            braw = bline.rstrip('\n\r')
            # 빈 줄은 바디 연속으로 간주
            if not braw.strip():
                j += 1
                continue
            # 들여쓰기 확인: except 들여쓰기보다 깊어야 바디
            if len(braw) - len(braw.lstrip()) > len(indent_str):
                body_lines_indexed.append((j, bline))
                j += 1
            else:
                break  # 바디 종료

        if not body_lines_indexed:
            if verbose:
                print(f'  SKIP (바디 없음) {filepath}:{i+1}  {raw.strip()}')
            i = j
            continue

        # ── 바디 텍스트 합산 ──────────────────────────────────────────────
        body_text = ''.join(bl for _, bl in body_lines_indexed)
        body_content = [lines[idx].rstrip('\n\r')
                        for idx, _ in body_lines_indexed]

        # ── 스킵 조건 1: 이미 상승된 로깅 존재 ──────────────────────────
        if any(m in body_text for m in ELEVATED_LOG_MARKERS):
            if verbose:
                print(f'  SKIP (이미 elevated) {filepath}:{i+1}')
            result.decisions.append({
                'line': i + 1, 'action': 'skip_already_elevated',
                'except': raw.strip(),
            })
            i = j
            continue

        # ── 스킵 조건 2: AlertManager 존재 ────────────────────────────
        if any(m in body_text for m in ALERT_MANAGER_MARKERS):
            if verbose:
                print(f'  SKIP (AlertManager) {filepath}:{i+1}')
            result.decisions.append({
                'line': i + 1, 'action': 'skip_alert_manager',
                'except': raw.strip(),
            })
            i = j
            continue

        # ── 스킵 조건 3: 바디가 Silent 패턴이 아님 (복잡한 로직 포함) ──
        if not _is_silent_body(body_content):
            if verbose:
                print(f'  SKIP (복잡한 바디) {filepath}:{i+1}')
            result.decisions.append({
                'line': i + 1, 'action': 'skip_complex_body',
                'except': raw.strip(),
            })
            i = j
            continue

        # ── 격상 대상 확정 ────────────────────────────────────────────────
        case = _classify(exc_types)
        first_body_idx, first_body_line = body_lines_indexed[0]
        inject_text = _inject_log_line(first_body_line, case, as_var)

        insertions.append((first_body_idx, inject_text))
        result.n_elevated += 1

        decision = {
            'line':   i + 1,
            'action': f'elevate_case_{case.lower()}',
            'except': raw.strip(),
            'case':   case,
            'types':  exc_types,
            'as_var': as_var,
        }
        result.decisions.append(decision)

        if verbose:
            tag = f'Case {case} → {"WARNING" if case == "A" else "ERROR+exc_info"}'
            print(f'  ✅ {tag}  {filepath}:{i+1}  {raw.strip()}')

        i = j

    # ── 삽입 없으면 종료 ─────────────────────────────────────────────────
    if not insertions:
        result.modified = False
        return result

    # ── 역순으로 삽입 (인덱스 오염 방지) ────────────────────────────────
    modified_lines = list(lines)
    for insert_idx, inject_text in sorted(insertions, key=lambda x: x[0], reverse=True):
        # inject_text 자체가 이미 줄바꿈을 포함하므로 splitlines(keepends) 처리
        inject_sublines = inject_text.splitlines(keepends=True)
        for k, subline in enumerate(inject_sublines):
            modified_lines.insert(insert_idx + k, subline)

    new_content = ''.join(modified_lines)

    # ── AST 검증 ─────────────────────────────────────────────────────────
    try:
        ast.parse(new_content)
        result.ast_valid = True
    except SyntaxError as e:
        result.ast_valid = False
        result.ast_error = str(e)
        # AST 실패 시 원본 유지 (수정 불가)
        result.modified = False
        print(f'  ❌ AST FAIL {filepath}: {e}', file=sys.stderr)
        return result

    # ── Diff 생성 ─────────────────────────────────────────────────────────
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f'a/{filepath}',
        tofile=f'b/{filepath}',
        lineterm='',
    ))
    result.diff_text = '\n'.join(diff_lines)

    result.modified = True
    result.new_content = new_content

    # ── 실제 파일 쓰기 (--apply 모드만) ─────────────────────────────────
    if not dry_run:
        filepath.write_text(new_content, 'utf-8')

    return result


# ────────────────────────────────────────────────────────────────────────────
# 파일 순회 및 보고서
# ────────────────────────────────────────────────────────────────────────────

def collect_targets(dirs: List[str], single_file: Optional[str]) -> List[Path]:
    """처리할 .py 파일 목록을 수집."""
    if single_file:
        p = Path(single_file)
        if not p.exists():
            p = _PROJECT_ROOT / single_file
        if not p.exists():
            print(f'❌ 파일 미발견: {single_file}', file=sys.stderr)
            sys.exit(1)
        return [p]

    targets: List[Path] = []
    for d in dirs:
        dirpath = Path(d)
        if not dirpath.is_absolute():
            dirpath = _PROJECT_ROOT / d
        if not dirpath.exists():
            print(f'⚠️  디렉토리 없음, 스킵: {dirpath}', file=sys.stderr)
            continue
        targets.extend(sorted(dirpath.rglob('*.py')))
    return targets


def run(args: argparse.Namespace) -> int:
    """메인 실행 함수. 종료 코드 반환."""
    dry_run = not args.apply
    verbose = args.verbose

    dirs = args.dirs if args.dirs else DEFAULT_TARGET_DIRS
    targets = collect_targets(dirs, args.file)

    if not targets:
        print('❌ 처리할 파일이 없습니다.', file=sys.stderr)
        return 1

    mode_label = '🔍 DRY-RUN' if dry_run else '✏️  APPLY'
    print(f'\n{"="*70}')
    print(f'  Project Meridian — Silent Error Elevation  [{mode_label}]')
    print(f'  대상 파일: {len(targets)}개')
    print(f'{"="*70}\n')

    stats = Stats(total_files=len(targets))
    all_diffs: List[str] = []
    modified_files: List[str] = []
    ast_failed_files: List[str] = []

    for filepath in targets:
        result = process_file(filepath, dry_run=dry_run, verbose=verbose)

        # 통계 집계
        for dec in result.decisions:
            action = dec.get('action', '')
            if 'elevate' in action:
                if dec.get('case') == 'A':
                    stats.elevated_case_a += 1
                else:
                    stats.elevated_case_b += 1
                stats.total_except += 1
            elif action == 'skip_already_elevated':
                stats.skipped_already += 1
                stats.total_except += 1
            elif action == 'skip_alert_manager':
                stats.skipped_alert += 1
                stats.total_except += 1
            elif action == 'skip_complex_body':
                stats.skipped_complex += 1
                stats.total_except += 1

        if not result.ast_valid:
            ast_failed_files.append(str(filepath))
            stats.ast_failures += 1
            continue

        if result.modified and result.diff_text:
            rel = filepath.relative_to(_PROJECT_ROOT)
            print(f'📄 {rel}  (+{result.n_elevated}건 격상)')
            all_diffs.append(result.diff_text)
            modified_files.append(str(rel))
            if dry_run and result.diff_text:
                # 컬러 Diff 출력 (ANSI)
                _print_colored_diff(result.diff_text)

    # ── 최종 보고서 ────────────────────────────────────────────────────────
    _print_summary(stats, modified_files, ast_failed_files, dry_run)

    return 0 if stats.ast_failures == 0 else 2


def _print_colored_diff(diff_text: str) -> None:
    """ANSI 컬러로 Diff 출력."""
    RED    = '\033[91m'
    GREEN  = '\033[92m'
    CYAN   = '\033[96m'
    RESET  = '\033[0m'
    try:
        for line in diff_text.splitlines():
            if line.startswith('+++') or line.startswith('---'):
                print(f'{CYAN}{line}{RESET}')
            elif line.startswith('+'):
                print(f'{GREEN}{line}{RESET}')
            elif line.startswith('-'):
                print(f'{RED}{line}{RESET}')
            elif line.startswith('@@'):
                print(f'{CYAN}{line}{RESET}')
            else:
                print(line)
    except BrokenPipeError:
        pass
    print()


def _print_summary(stats: Stats, modified_files: List[str],
                   ast_failed: List[str], dry_run: bool) -> None:
    """최종 통계 보고서 출력."""
    total_elevated = stats.elevated_case_a + stats.elevated_case_b
    print(f'\n{"="*70}')
    print('  📊 실행 결과 요약')
    print(f'{"="*70}')
    print(f'  스캔 파일:              {stats.total_files:>5}개')
    print(f'  격상 대상 확인:         {total_elevated:>5}건')
    print(f'    ├─ Case A (WARNING):  {stats.elevated_case_a:>5}건')
    print(f'    └─ Case B (ERROR):    {stats.elevated_case_b:>5}건')
    print(f'  스킵 (기존 로깅 존재):  {stats.skipped_already:>5}건')
    print(f'  스킵 (AlertManager):    {stats.skipped_alert:>5}건')
    print(f'  스킵 (복잡한 바디):     {stats.skipped_complex:>5}건')
    print(f'  AST 검증 실패:          {stats.ast_failures:>5}개 파일')
    print()

    if modified_files:
        action = '수정 예정' if dry_run else '수정 완료'
        print(f'  📝 {action} 파일:')
        for f in modified_files:
            print(f'     • {f}')

    if ast_failed:
        print(f'\n  ❌ AST 실패 파일 (변경 미적용):')
        for f in ast_failed:
            print(f'     • {f}')

    print()
    if dry_run:
        print('  ℹ️  DRY-RUN 모드 — 파일이 수정되지 않았습니다.')
        print('     실제 적용하려면: python scripts/elevate_silent_errors.py --apply')
    else:
        print('  ✅ --apply 모드 — 파일이 실제로 수정되었습니다.')
    print(f'{"="*70}\n')


# ────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='elevate_silent_errors',
        description='Project Meridian — Silent Error Elevation Script',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        '--apply',
        action='store_true',
        default=False,
        help='실제 파일 수정 (기본: Dry-Run)',
    )
    p.add_argument(
        '--dirs',
        nargs='+',
        metavar='DIR',
        default=None,
        help=f'대상 디렉토리 (기본: {DEFAULT_TARGET_DIRS})',
    )
    p.add_argument(
        '--file',
        metavar='FILE',
        default=None,
        help='단일 파일 대상 (--dirs 무시)',
    )
    p.add_argument(
        '--verbose', '-v',
        action='store_true',
        default=False,
        help='각 except 블록 판정 이유 상세 출력',
    )
    return p


if __name__ == '__main__':
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(run(args))

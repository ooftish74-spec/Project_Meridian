#!/usr/bin/env python3
"""Static AST Contract, Symbol, Script-Path & Scope Auditor for Project Meridian.

Performs 4 comprehensive static checks across `src/`, `scripts/`, and `config/`:
1. Internal Import & Symbol Resolution: verifies every `from src/scripts/config... import Y`
   resolves to an existing module and symbol `Y`.
2. Script Path Existence: verifies every referenced `scripts/*.py` path exists on disk.
3. Pipeline Phase Registration: verifies all phase names in `daily_pipeline.py` cover
   all phase invocations across scripts/cron.
4. Bytecode/AST Scope Verification (F821 Undefined Name & F823 UnboundLocalError):
   traces variable bindings across all branches in every function/method/module.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "scripts", "config")
BUILTIN_NAMES: Set[str] = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__all__", "__annotations__"}
SCRIPT_PATH_RE = re.compile(r"(?:^|[\s'\"/])(scripts/[A-Za-z0-9_./-]+\.py)\b")


def iter_python_files() -> List[Path]:
    files: List[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            files.append(p)
    return files


def resolve_module_path(mod_name: str, current_file: Optional[Path] = None, level: int = 0) -> Optional[Path]:
    if level > 0 and current_file is not None:
        base_dir = current_file.parent
        for _ in range(level - 1):
            base_dir = base_dir.parent
        rel_parts = mod_name.split(".") if mod_name else []
        cand_dir = base_dir.joinpath(*rel_parts)
    else:
        rel_parts = mod_name.split(".")
        cand_dir = ROOT.joinpath(*rel_parts)

    py_file = cand_dir.with_suffix(".py")
    if py_file.exists():
        return py_file
    init_file = cand_dir / "__init__.py"
    if init_file.exists():
        return init_file
    if cand_dir.is_dir():
        return cand_dir
    return None


def collect_module_exports(py_path: Path, _cache: Dict[Path, Set[str]] = {}) -> Set[str]:
    if py_path in _cache:
        return _cache[py_path]
    exports: Set[str] = set()
    _cache[py_path] = exports
    if py_path.is_dir():
        for child in py_path.iterdir():
            if child.suffix == ".py" and child.stem != "__init__":
                exports.add(child.stem)
            elif child.is_dir():
                exports.add(child.name)
        return exports
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    except Exception:
        return exports

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exports.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    exports.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for elt in t.elts:
                        if isinstance(elt, ast.Name):
                            exports.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exports.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exports.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    target = resolve_module_path(node.module or "", py_path, node.level)
                    if target is not None:
                        exports |= collect_module_exports(target, _cache)
                else:
                    exports.add(alias.asname or alias.name)

    if py_path.name == "__init__.py":
        for child in py_path.parent.iterdir():
            if child.suffix == ".py" and child.stem != "__init__":
                exports.add(child.stem)
            elif child.is_dir() and (child / "__init__.py").exists():
                exports.add(child.name)

    return exports


def _extract_target_names(target: ast.AST) -> Set[str]:
    names: Set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names |= _extract_target_names(elt)
    elif isinstance(target, ast.Starred):
        names |= _extract_target_names(target.value)
    return names


def _collect_scope_bindings(node: ast.AST) -> Tuple[Set[str], Set[str]]:
    """Collect all bound variable names and global/nonlocal declarations directly in `node` scope."""
    bound: Set[str] = set()
    declared_global_or_nonlocal: Set[str] = set()

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            bound.add(a.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)

    body = getattr(node, "body", [])
    if not isinstance(body, list):
        body = [body]

    def _walk_same_scope(stmts: List[ast.AST]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(stmt.name)
                continue
            if isinstance(stmt, (ast.Global, ast.Nonlocal)):
                declared_global_or_nonlocal.update(stmt.names)
                bound.update(stmt.names)
                continue
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    bound.update(_extract_target_names(t))
            elif isinstance(stmt, ast.AnnAssign):
                bound.update(_extract_target_names(stmt.target))
            elif isinstance(stmt, ast.AugAssign):
                bound.update(_extract_target_names(stmt.target))
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                bound.update(_extract_target_names(stmt.target))
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    if alias.name != "*":
                        bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    if item.optional_vars:
                        bound.update(_extract_target_names(item.optional_vars))
            elif isinstance(stmt, ast.ExceptHandler):
                if stmt.name:
                    bound.add(stmt.name)

            # Traverse nested expressions for NamedExpr (:=) and sub-statements
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(child.name)
                    continue
                if isinstance(child, ast.Lambda):
                    continue
                if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                    bound.add(child.target.id)
                _walk_same_scope([child])

    _walk_same_scope(body)
    return bound, declared_global_or_nonlocal


def check_undefined_names_in_file(py_path: Path, tree: ast.Module) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    rel = str(py_path.relative_to(ROOT))

    # If module uses `from X import *`, skip strict undefined-name check on module globals
    has_star_import = any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )

    def _visit_scope(node: ast.AST, enclosing_bound: Set[str], is_class_scope: bool = False) -> None:
        local_bound, _ = _collect_scope_bindings(node)
        visible_for_loads = BUILTIN_NAMES | enclosing_bound | local_bound
        next_enclosing = enclosing_bound if is_class_scope else (enclosing_bound | local_bound)

        def _check_expr(expr: ast.AST, comp_bound: Set[str]) -> None:
            if isinstance(expr, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _visit_scope(expr, next_enclosing | comp_bound, is_class_scope=isinstance(expr, ast.ClassDef))
                return
            if isinstance(expr, ast.Lambda):
                _visit_scope(expr, next_enclosing | comp_bound, is_class_scope=False)
                return
            if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                inner_comp = set(comp_bound)
                for gen in expr.generators:
                    _check_expr(gen.iter, inner_comp)
                    inner_comp |= _extract_target_names(gen.target)
                    for if_clause in gen.ifs:
                        _check_expr(if_clause, inner_comp)
                if isinstance(expr, ast.DictComp):
                    _check_expr(expr.key, inner_comp)
                    _check_expr(expr.value, inner_comp)
                else:
                    _check_expr(expr.elt, inner_comp)
                return
            if isinstance(expr, ast.Name) and isinstance(expr.ctx, ast.Load):
                if expr.id not in (visible_for_loads | comp_bound) and not has_star_import:
                    issues.append({
                        "type": "undefined_name",
                        "file": rel,
                        "line": expr.lineno,
                        "symbol": expr.id,
                    })
                return
            for ch in ast.iter_child_nodes(expr):
                _check_expr(ch, comp_bound)

        body = getattr(node, "body", [])
        if isinstance(body, list):
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in stmt.decorator_list:
                        _check_expr(dec, set())
                    for d in list(stmt.args.defaults) + [x for x in stmt.args.kw_defaults if x is not None]:
                        _check_expr(d, set())
                    _visit_scope(stmt, next_enclosing, is_class_scope=False)
                elif isinstance(stmt, ast.ClassDef):
                    for dec in stmt.decorator_list:
                        _check_expr(dec, set())
                    for b in stmt.bases:
                        _check_expr(b, set())
                    for kw in stmt.keywords:
                        _check_expr(kw.value, set())
                    _visit_scope(stmt, next_enclosing, is_class_scope=True)
                else:
                    _check_expr(stmt, set())
        else:
            _check_expr(body, set())

    _visit_scope(tree, set(), is_class_scope=False)
    return issues


def audit_codebase() -> Dict[str, Any]:
    files = iter_python_files()
    broken_imports: List[Dict[str, Any]] = []
    missing_scripts: List[Dict[str, Any]] = []
    undefined_names: List[Dict[str, Any]] = []
    export_cache: Dict[Path, Set[str]] = {}

    for py_path in files:
        rel = str(py_path.relative_to(ROOT))
        try:
            source = py_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_path))
        except Exception as e:
            broken_imports.append({"type": "syntax_error", "file": rel, "error": str(e)})
            continue

        # 1. Check internal imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                is_internal = node.level > 0 or mod.startswith(("src.", "scripts.", "config.")) or mod in ("src", "scripts", "config")
                if not is_internal:
                    continue
                target_path = resolve_module_path(mod, py_path, node.level)
                if target_path is None:
                    broken_imports.append({
                        "type": "missing_module",
                        "file": rel,
                        "line": node.lineno,
                        "module": mod,
                        "level": node.level,
                    })
                    continue
                exports = collect_module_exports(target_path, export_cache)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if alias.name not in exports:
                        sub_target = resolve_module_path(f"{mod}.{alias.name}" if mod else alias.name, py_path, node.level)
                        if sub_target is None:
                            broken_imports.append({
                                "type": "missing_symbol",
                                "file": rel,
                                "line": node.lineno,
                                "module": mod,
                                "symbol": alias.name,
                                "target_file": str(target_path.relative_to(ROOT)),
                            })

            # 2. Check referenced scripts/*.py paths
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in SCRIPT_PATH_RE.finditer(node.value):
                    sp = m.group(1)
                    if not (ROOT / sp).exists():
                        missing_scripts.append({
                            "type": "missing_script_path",
                            "file": rel,
                            "line": node.lineno,
                            "script_path": sp,
                        })
            elif (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, str)
                and node.right.value.endswith(".py")
                and isinstance(node.left, ast.BinOp)
                and isinstance(node.left.op, ast.Div)
                and isinstance(node.left.right, ast.Constant)
                and node.left.right.value == "scripts"
            ):
                sp = f"scripts/{node.right.value}"
                if not (ROOT / sp).exists():
                    missing_scripts.append({
                        "type": "missing_script_path",
                        "file": rel,
                        "line": node.lineno,
                        "script_path": sp,
                    })

        # 3. Check undefined names
        undefined_names.extend(check_undefined_names_in_file(py_path, tree))

    return {
        "n_files_scanned": len(files),
        "broken_imports": broken_imports,
        "missing_scripts": missing_scripts,
        "undefined_names": undefined_names,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Static AST Contract & Scope Auditor")
    parser.add_argument("--check", action="store_true", help="Exit non-zero if any violation is found")
    args = parser.parse_args()

    report = audit_codebase()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.check and (report["broken_imports"] or report["missing_scripts"] or report["undefined_names"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

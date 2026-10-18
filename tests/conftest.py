"""
Project Meridian — global pytest isolation (Wave 0, Step 0).

Why this exists
---------------
On 2026-10-18 a plain `pytest tests/` run rewrote the production-path SSOT files
(results/shadow_portfolio.json, results/kis_portfolio.json, results/krx_futures_overnight.json, ...)
and reached the live broker API (`real_account_synced: true`). Tests must never be able to
mutate operational state or talk to external services.

Guards (all autouse, session-wide)
----------------------------------
1. MERIDIAN_TEST_MODE=1 is exported before any project module is imported.
2. Outbound network is blocked: any socket connect to a non-loopback address raises
   `NetworkBlockedInTests`. Loopback (Redis on localhost, etc.) and AF_UNIX remain allowed.
3. SSOT snapshot/restore: the operational state paths are copied before the session and
   restored byte-for-byte after it, so even a test that writes to the real paths cannot
   leave a trace.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("MERIDIAN_TEST_MODE", "1")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Operational state that tests must never be able to change permanently.
_PROTECTED_DIRS = [
    _PROJECT_ROOT / "results",
    _PROJECT_ROOT / "data" / "macro",
]
_PROTECTED_FILE_GLOBS = [
    (_PROJECT_ROOT / "config", "*.json"),
    (_PROJECT_ROOT / "config", ".kis_token*"),
]


# ─────────────────────────────────────────────────────────────
# 1) Network guard
# ─────────────────────────────────────────────────────────────
class NetworkBlockedInTests(ConnectionRefusedError):
    """Raised when a test attempts a non-loopback network connection (behaves like an offline host)."""


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX path or unusual family → allow
    host = address[0]
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Hostname (not an IP literal): allow only explicit localhost names.
        return host.lower() in ("localhost", "localhost.localdomain")


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
        raise NetworkBlockedInTests(f"Outbound network blocked in tests: {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
        raise NetworkBlockedInTests(f"Outbound network blocked in tests: {address!r}")
    return _real_connect_ex(self, address)


@pytest.fixture(scope="session", autouse=True)
def _block_outbound_network():
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    try:
        yield
    finally:
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex


# ─────────────────────────────────────────────────────────────
# 2) SSOT snapshot / restore
# ─────────────────────────────────────────────────────────────
def _snapshot(dst_root: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for d in _PROTECTED_DIRS:
        if d.exists():
            target = dst_root / "dirs" / d.relative_to(_PROJECT_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(d, target, symlinks=True)
            pairs.append((d, target))
    for base, pattern in _PROTECTED_FILE_GLOBS:
        for f in base.glob(pattern):
            if f.is_file():
                target = dst_root / "files" / f.relative_to(_PROJECT_ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                pairs.append((f, target))
    return pairs


def _restore(pairs: list[tuple[Path, Path]], trash_root: Path) -> None:
    trash_root.mkdir(parents=True, exist_ok=True)
    call_trash = Path(tempfile.mkdtemp(prefix="restore_", dir=str(trash_root)))   # unique per restore call
    for original, saved in pairs:
        if saved.is_dir():
            if original.exists():
                # Move the test-mutated tree aside first (atomic rename), then put the snapshot back.
                trash = call_trash / original.relative_to(_PROJECT_ROOT)
                trash.parent.mkdir(parents=True, exist_ok=True)
                os.replace(original, trash)
            shutil.copytree(saved, original, symlinks=True)
        else:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, original)


@pytest.fixture(scope="session", autouse=True)
def _protect_operational_state():
    if os.environ.get("MERIDIAN_TEST_SKIP_SSOT_GUARD") == "1":
        yield
        return
    import hashlib
    import signal

    tag = hashlib.sha1(str(_PROJECT_ROOT).encode()).hexdigest()[:12]
    snap_root = Path(tempfile.gettempdir()) / f"meridian_ssot_snapshot_{tag}"
    complete_marker = snap_root / ".complete"
    trash_root = Path(tempfile.mkdtemp(prefix="meridian_ssot_trash_"))

    def _pairs_from(root: Path) -> list[tuple[Path, Path]]:
        pairs: list[tuple[Path, Path]] = []
        for base_kind in ("dirs", "files"):
            kind_root = root / base_kind
            if not kind_root.exists():
                continue
            if base_kind == "dirs":
                for d in _PROTECTED_DIRS:
                    saved = kind_root / d.relative_to(_PROJECT_ROOT)
                    if saved.exists():
                        pairs.append((d, saved))
            else:
                for saved in kind_root.rglob("*"):
                    if saved.is_file():
                        pairs.append((_PROJECT_ROOT / saved.relative_to(kind_root), saved))
        return pairs

    # Serialize sessions: a killed session may still be restoring on SIGTERM while a new one starts.
    import fcntl
    lock_fh = open(Path(tempfile.gettempdir()) / f"meridian_ssot_{tag}.lock", "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)   # blocks until any other session finished restoring

    # A previous session was killed before restoring → restore its (complete) snapshot first.
    if complete_marker.exists():
        _restore(_pairs_from(snap_root), trash_root)
    shutil.rmtree(snap_root, ignore_errors=True)

    snap_root.mkdir(parents=True)
    pairs = _snapshot(snap_root)
    complete_marker.write_text("ok")

    restored = {"done": False}

    def _do_restore(*_args):
        if restored["done"]:
            return
        restored["done"] = True
        _restore(pairs, trash_root)
        shutil.rmtree(snap_root, ignore_errors=True)
        shutil.rmtree(trash_root, ignore_errors=True)

    prev_term = signal.getsignal(signal.SIGTERM)

    def _on_term(signum, frame):
        _do_restore()
        if callable(prev_term):
            prev_term(signum, frame)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_term)
    try:
        yield
    finally:
        try:
            _do_restore()
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

"""Best-effort local-fixture guardian for one native turn.

The guardian owns the inherited reviewer lease. It stays alive after the native
leader exits, continuously observes descendants, and releases the lease only
after every observed process instance is stopped. This backend has no qualified
containment boundary for an unobserved setsid/double-fork escape, and the
identity-check/signal TOCTOU remains possible. Live native admission is therefore
closed by :mod:`council.session_runtime`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time


_BOOT_ID: str | None = None


def _boot_id() -> str:
    global _BOOT_ID
    if _BOOT_ID is None:
        _BOOT_ID = _read_boot_id()
    return _BOOT_ID


def _read_boot_id() -> str:
    linux = Path("/proc/sys/kernel/random/boot_id")
    if linux.is_file():
        value = linux.read_text(encoding="ascii").strip()
        if value:
            return f"linux:{value}"
    result = subprocess.run(
        ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5,
    )
    if result.returncode:
        raise RuntimeError("cannot establish boot identity")
    match = re.search(r"sec\s*=\s*(\d+).*usec\s*=\s*(\d+)", result.stdout)
    if not match:
        raise RuntimeError("cannot parse boot identity")
    return f"darwin:{match.group(1)}.{match.group(2)}"


def _inventory() -> dict[int, dict]:
    boot = _boot_id()
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode:
        raise RuntimeError("cannot inventory process identities")
    processes: dict[int, dict] = {}
    for raw in result.stdout.splitlines():
        parts = raw.split()
        if len(parts) < 9:
            continue
        try:
            pid, ppid, pgid = map(int, parts[:3])
        except ValueError:
            continue
        processes[pid] = {
            "pid": pid, "ppid": ppid, "pgid": pgid, "state": parts[3],
            "started": " ".join(parts[4:9]), "boot_id": boot,
        }
    if not processes:
        raise RuntimeError("empty process inventory")
    return processes


def _same_instance(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("pid", "started", "boot_id"))


def _active(identity: dict, processes: dict[int, dict]) -> bool:
    current = processes.get(identity["pid"])
    return bool(current and _same_instance(identity, current)
                and not str(current.get("state", "")).startswith("Z"))


def _discover(processes: dict[int, dict], tracked: dict[int, dict], group: int) -> None:
    """Repeated observation, not a race-free containment mechanism."""
    changed = True
    while changed:
        changed = False
        parents = {pid for pid, identity in tracked.items() if _active(identity, processes)}
        for pid, current in processes.items():
            if pid == os.getpid() or pid in tracked:
                continue
            # Do not sweep the guardian group: inventory helpers are guardian
            # children in that group and are not native work. Native ownership
            # is established only through repeatedly observed ancestry.
            if current["ppid"] in parents:
                tracked[pid] = dict(current)
                changed = True


def _signal_exact(identity: dict, sig: int, processes: dict[int, dict]) -> bool:
    current = processes.get(identity["pid"])
    if current is None or str(current.get("state", "")).startswith("Z"):
        return False
    if not _same_instance(identity, current):
        raise RuntimeError("stale or reused process identity; refusing to signal")
    try:
        os.kill(identity["pid"], sig)
    except ProcessLookupError:
        return False
    return True


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _cleanup(tracked: dict[int, dict], group: int) -> tuple[str, str | None]:
    try:
        # Several discovery/signal rounds catch children created while a known
        # parent is terminating. This improves finite fixture cleanup but cannot
        # close this backend's fork/setsid observation race.
        for sig, duration in ((signal.SIGTERM, .35), (signal.SIGKILL, .35)):
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                processes = _inventory()
                _discover(processes, tracked, group)
                for identity in list(tracked.values()):
                    _signal_exact(identity, sig, processes)
                time.sleep(.025)
        deadline = time.monotonic() + 2
        while True:
            processes = _inventory()
            _discover(processes, tracked, group)
            remaining = [identity for identity in tracked.values() if _active(identity, processes)]
            if not remaining:
                return "stopped", None
            if time.monotonic() >= deadline:
                return "unverified", "observed owned process remains after hard stop"
            for identity in remaining:
                _signal_exact(identity, signal.SIGKILL, processes)
            time.sleep(.025)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # Unknown inventory/identity means no broader numeric/group signal.
        return "unverified", str(exc)


def main() -> int:
    if (len(sys.argv) < 7 or sys.argv[1] != "--receipt" or sys.argv[3] != "--nonce"
            or not re.fullmatch(r"[0-9a-f]{32}", sys.argv[4]) or "--" not in sys.argv[5:]):
        return 2
    separator = sys.argv.index("--", 5)
    receipt = Path(sys.argv[2])
    ownership_nonce = sys.argv[4]
    argv = sys.argv[separator + 1:]
    if not argv:
        return 2
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
        processes = _inventory()
        guardian_identity = processes.get(os.getpid())
        if guardian_identity is None:
            raise RuntimeError("guardian identity unavailable after launch")
        child_identity = processes.get(child.pid)
        if child_identity is None:
            raise RuntimeError("native leader identity unavailable after launch")
        tracked = {child.pid: dict(child_identity)}
        group = os.getpgrp()
        stop_requested = False
        inventory_error: str | None = None
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin, selectors.EVENT_READ)
            while child.poll() is None and not stop_requested:
                try:
                    processes = _inventory()
                    _discover(processes, tracked, group)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    inventory_error = str(exc)
                    break
                if selector.select(timeout=.025):
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data or b"STOP" in data:
                        stop_requested = True
            try:
                processes = _inventory()
                _discover(processes, tracked, group)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                inventory_error = inventory_error or str(exc)

        if inventory_error:
            cleanup_status, cleanup_error = "unverified", inventory_error
        else:
            cleanup_status, cleanup_error = _cleanup(tracked, group)
        try:
            child.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            pass
        _atomic_json(receipt, {
            "schema": 1,
            "status": cleanup_status,
            "error": cleanup_error,
            "boot_id": child_identity["boot_id"],
            "ownership_nonce": ownership_nonce,
            "guardian_identity": guardian_identity,
            "process_group": os.getpgrp(),
            "native_leader_identity": child_identity,
            "observed_processes": sorted(tracked.values(), key=lambda item: item["pid"]),
            "inventory_provenance": {
                "method": "repeated ps -axo pid=,ppid=,pgid=,state=,lstart=",
                "complete_for_descendants": False,
            },
            "containment": "best_effort_lineage_observation",
            "live_admission_qualified": False,
        })
        if cleanup_status != "stopped":
            # The live gate is closed and the controller records cleanup_failed.
            # Do not turn a best-effort fixture guardian into an unrecoverable
            # orphan merely to hold a local lease forever.
            return 125
        code = child.returncode
        return code if code is not None and code >= 0 else (128 - code if code is not None else 1)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        try:
            _atomic_json(receipt, {
                "schema": 1, "status": "unverified", "error": str(exc),
                "ownership_nonce": ownership_nonce,
                "containment": "best_effort_lineage_observation",
                "live_admission_qualified": False,
            })
        except OSError:
            pass
        return 127


if __name__ == "__main__":
    raise SystemExit(main())

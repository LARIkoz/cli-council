"""Native, resumable subscription CLI turns. No API keys or model fallback.

The controller owns conversation IDs; a process may exit between turns. Raw
events are retained even when an exit, timeout or invalid terminal event makes
the turn incomplete. A completed turn means a report arrived, not acceptance.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from typing import Callable


class SessionError(ValueError):
    """A recoverable controller, input or protocol error."""


@dataclass(frozen=True)
class ExecutionAdmission:
    """Non-serializable launch authority selected by the trusted caller."""

    name: str
    permits_process_launch: bool
    evidence_strength: str


# Native reviewer execution stays closed because this backend has no qualified
# containment boundary for setsid/double-fork escapes. Tests must opt into the
# finite local-fixture authority explicitly; no environment variable,
# executable name, or argv shape can grant it.
CLOSED_NATIVE_ADMISSION = ExecutionAdmission(
    "native-live-closed", False, "unqualified-process-containment",
)
LOCAL_FIXTURE_ADMISSION = ExecutionAdmission(
    "local-finite-fixture", True, "best-effort-lineage-observation-only",
)


@dataclass(frozen=True)
class ProcessInventory:
    """One process-list observation; never claims exhaustive containment."""

    processes: dict[int, dict]
    method: str
    boot_id: str
    observed_at_monotonic_ns: int
    complete_for_descendants: bool = False

    def get(self, pid: object) -> dict | None:
        return self.processes.get(pid) if type(pid) is int else None


_BOOT_ID: str | None = None


def _boot_id() -> str:
    """Return the current boot identity used to reject stale process receipts (cached: it cannot
    change while this process lives)."""
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
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError("cannot establish current boot identity") from exc
    if result.returncode:
        raise SessionError("cannot establish current boot identity")
    match = re.search(r"sec\s*=\s*(\d+).*usec\s*=\s*(\d+)", result.stdout)
    if not match:
        raise SessionError("cannot parse current boot identity")
    return f"darwin:{match.group(1)}.{match.group(2)}"


def _process_inventory() -> ProcessInventory:
    """Capture process instance identities; failure is never treated as absence."""
    boot = _boot_id()
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SessionError("cannot inventory process identities") from exc
    if result.returncode:
        raise SessionError("cannot inventory process identities")
    inventory: dict[int, dict] = {}
    for raw in result.stdout.splitlines():
        parts = raw.split()
        if len(parts) < 9:
            continue
        try:
            pid, ppid, pgid = map(int, parts[:3])
        except ValueError:
            continue
        inventory[pid] = {
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "state": parts[3],
            "started": " ".join(parts[4:9]),
            "boot_id": boot,
        }
    if not inventory:
        raise SessionError("process identity inventory is empty")
    return ProcessInventory(
        processes=inventory,
        method="ps -axo pid=,ppid=,pgid=,state=,lstart=",
        boot_id=boot,
        observed_at_monotonic_ns=time.monotonic_ns(),
        complete_for_descendants=False,
    )


def _process_identity(pid: int, *, inventory: ProcessInventory | dict[int, dict] | None = None) -> dict:
    if type(pid) is not int or pid <= 1:
        raise SessionError("invalid process identity")
    current = (inventory if inventory is not None else _process_inventory()).get(pid)
    if not current:
        raise SessionError("process exited before its identity could be recorded")
    return dict(current)


def _same_process_instance(owned: dict, current: dict) -> bool:
    return (
        type(owned.get("pid")) is int
        and owned.get("pid") == current.get("pid")
        and isinstance(owned.get("boot_id"), str)
        and owned.get("boot_id") == current.get("boot_id")
        and isinstance(owned.get("started"), str)
        and owned.get("started") == current.get("started")
    )


def _signal_owned_process(
    owned: dict, sig: int, *, inventory: ProcessInventory | dict[int, dict] | None = None,
) -> bool:
    """Signal one exact observed instance; ambiguity never authorizes a signal.

    This backend has no qualified atomic identity-and-signal primitive, so
    verification and signal still have a residual TOCTOU. The live admission
    gate therefore remains closed.
    """
    try:
        processes = inventory if inventory is not None else _process_inventory()
    except (OSError, SessionError, subprocess.SubprocessError) as exc:
        raise SessionError("cannot verify owned process; refusing to signal") from exc
    current = processes.get(owned.get("pid"))
    if current is None:
        # A process-handle wait can prove that leader reaping completed. A
        # generic inventory omission cannot prove absence or ownership cleanup.
        raise SessionError("owned process is missing from an incomplete observation; refusing to signal")
    if not _same_process_instance(owned, current):
        raise SessionError("process identity is stale or reused; refusing to signal")
    try:
        os.kill(owned["pid"], sig)
    except ProcessLookupError:
        return False
    return True


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}", value))


@dataclass
class NativeStream:
    runtime: str
    expected_id: str | None = None
    conversation_id: str | None = None
    reported_model: str | None = None
    reported_models: list[str] = field(default_factory=list)
    model_identity_source: str | None = None
    response: str = ""
    terminal_reason: str | None = None
    request_id: str | None = None
    terminal_count: int = 0
    error: str | None = None
    usage: dict = field(default_factory=dict)

    def _identity(self, value: object) -> None:
        if value is None:
            return
        if not valid_id(value):
            raise SessionError("native event contains an invalid conversation ID")
        if self.expected_id and value != self.expected_id:
            raise SessionError("runtime resumed a different conversation")
        if self.conversation_id and value != self.conversation_id:
            raise SessionError("conversation ID changed within the turn")
        self.conversation_id = value

    def feed(self, event: dict) -> None:
        if not isinstance(event, dict):
            raise SessionError("native stream event must be a JSON object")
        if self.terminal_count:
            raise SessionError("unexpected native event after the terminal result")
        if self.runtime == "antigravity":
            kind = event.get("event")
            payload = event.get(kind, {}) if isinstance(kind, str) else {}
            if not isinstance(payload, dict):
                raise SessionError("invalid Antigravity event payload")
            self._identity(event.get("conversation_id"))
            self._identity(payload.get("conversation_id"))
            if kind == "init":
                self.reported_model = payload.get("model")
                self.model_identity_source = "native init configuration"
            elif kind == "result":
                self.terminal_count += 1
                self.terminal_reason = payload.get("status")
                self.response = payload.get("response", "")
                self.error = payload.get("error")
                self.usage = payload.get("usage") or {}
        elif self.runtime == "grok":
            self._identity(event.get("sessionId"))
            kind = event.get("type")
            if kind == "text":
                data = event.get("data")
                if not isinstance(data, str):
                    raise SessionError("invalid Grok text event")
                self.response += data
            elif kind == "end":
                self.terminal_count += 1
                self.terminal_reason = event.get("stopReason")
                self.request_id = event.get("requestId")
                self.reported_model = event.get("model") or self.reported_model
                model_usage = event.get("modelUsage")
                if isinstance(model_usage, dict):
                    self.reported_models = list(model_usage)
                    self.model_identity_source = "native end modelUsage keys"
                    if len(self.reported_models) == 1:
                        self.reported_model = self.reported_models[0]
                self.usage = event.get("usage") or {}
            elif kind == "error":
                self.error = event.get("message") or "native runtime error"
        else:
            raise SessionError(f"unknown runtime: {self.runtime}")
        if self.terminal_count > 1:
            raise SessionError("multiple terminal results for a single turn")

    def completed(self) -> bool:
        expected = {"SUCCESS"} if self.runtime == "antigravity" else {"EndTurn", "end_turn"}
        return (self.terminal_count == 1 and isinstance(self.terminal_reason, str)
                and self.terminal_reason in expected
                and valid_id(self.conversation_id) and not self.error
                and isinstance(self.response, str) and bool(self.response.strip()))


def native_argv(runtime: str, binary: str, model: str, prompt_file: Path,
                conversation_id: str | None, timeout: float, *, planned_id: str | None = None) -> list[str]:
    if conversation_id is not None and not valid_id(conversation_id):
        raise SessionError("invalid saved conversation ID")
    if planned_id is not None:
        try:
            canonical = str(uuid.UUID(planned_id))
        except (ValueError, AttributeError):
            raise SessionError("new Grok conversation requires a UUID") from None
        if canonical != planned_id or runtime != "grok" or conversation_id:
            raise SessionError("a planned UUID is only valid for a new Grok conversation")
    if runtime == "antigravity":
        # The installed >=1.1 structured headless interface. Older agy-p text
        # wrappers are intentionally not accepted: they discard the native ID.
        prompt = prompt_file.read_text(encoding="utf-8")
        if len(prompt.encode("utf-8")) > 80_000:
            raise SessionError("Antigravity prompt exceeds the argv budget; reference workspace files")
        argv = [binary, "-p", prompt, "--model", model, "--output-format", "stream-json",
                "--print-timeout", f"{max(1, int(timeout))}s", "--dangerously-skip-permissions",
                "--disable-slash-commands"]
        if conversation_id:
            argv += ["--conversation", conversation_id]
        return argv
    if runtime == "grok":
        argv = [binary, "--no-auto-update", "--prompt-file", str(prompt_file), "--model", model,
                "--output-format", "streaming-json", "--no-subagents",
                "--disable-web-search", "--no-plan", "--no-alt-screen",
                "--always-approve", "--deny", "MCPTool(**)",
                "--tools", "read_file,grep,list_dir,run_terminal_cmd"]
        if conversation_id:
            argv += ["--resume", conversation_id]
        elif planned_id:
            argv += ["--session-id", planned_id]
        return argv
    raise SessionError(f"unknown runtime: {runtime}")


def runtime_env(workspace: Path | None = None, turn_dir: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # Subscription-only pilot: an inherited API key must not become a fallback.
    for key in ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(key, None)
    # Inherited Python lookup controls can execute bytes outside the frozen
    # workspace. Replace cache roots with per-turn locations instead of
    # inheriting mutable global caches. This is provenance hardening, not an OS
    # sandbox or proof of the exact modules a native tool later imports.
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE",
                "PYTHONBREAKPOINT", "PYTHONINSPECT", "PYTHONSAFEPATH"):
        env.pop(key, None)
    if turn_dir is not None:
        cache = turn_dir / "runtime-cache"
        roots = {
            "PYTHONPYCACHEPREFIX": cache / "pycache",
            "XDG_CACHE_HOME": cache / "xdg",
            "TMPDIR": cache / "tmp",
        }
        for key, path in roots.items():
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
        env["PYTHONNOUSERSITE"] = "1"
    env.update(AGY_CLI_DISABLE_AUTO_UPDATE="1", AGY_CLI_DISABLE_LATEX="1",
               GROK_DISABLE_AUTOUPDATER="1", GROK_MEMORY="0", TERM="dumb", NO_COLOR="1")
    if workspace is not None:
        # A source projection has no .git. Git must not discover a parent
        # checkout, and inherited worktree overrides must not bypass the cwd.
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
            env.pop(key, None)
        env.update(PWD=str(workspace.resolve()),
                   GIT_CEILING_DIRECTORIES=str(workspace.resolve().parent),
                   GIT_DISCOVERY_ACROSS_FILESYSTEM="0")
    return env


def _environment_receipt(runtime: str, argv: list[str], workspace: Path,
                         turn_dir: Path, env: dict[str, str]) -> dict:
    executable = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    canonical = json.dumps(sorted(env.items()), ensure_ascii=False, separators=(",", ":"))
    cache = turn_dir / "runtime-cache"
    return {
        "policy": "p1a-isolated-cache-v1",
        "runtime": runtime,
        "cwd": str(workspace.resolve()),
        "argv0": argv[0],
        "executable": str(Path(executable).resolve()),
        "guardian_interpreter": str(Path(sys.executable).resolve()),
        "env_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "cache_roots": {
            "python": str(cache / "pycache"),
            "xdg": str(cache / "xdg"),
            "tmp": str(cache / "tmp"),
        },
        "limitations": [
            "does_not_attest_imported_or_executed_bytes",
            "does_not_provide_os_process_containment",
        ],
    }


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _signal_group(pid: int, sig: int) -> bool:
    """Signal only the owned group; distinguish Darwin's empty-group EPERM."""
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # On this Mac a group already emptied by TERM can return EPERM on
        # KILL instead of ESRCH. Confirm absence independently; never hide a
        # permission error for a group that may still contain live processes.
        groups = subprocess.run(["ps", "-axo", "pgid="], capture_output=True,
                                text=True, timeout=5)
        if groups.returncode == 0 and str(pid) not in groups.stdout.split():
            return False
        raise
    return True


def _stop_group(proc: subprocess.Popen) -> None:
    """Ask the guardian to clean its observed instances and verify its receipt.

    A launched fixture carries an exact guardian identity, nonce and receipt
    path. Missing or mismatched ownership evidence fails closed; no numeric
    PID/PGID fallback is permitted.
    """
    receipt_path = getattr(proc, "_council_cleanup_receipt", None)
    guardian_identity = getattr(proc, "_council_guardian_identity", None)
    ownership_nonce = getattr(proc, "_council_ownership_nonce", None)
    if (not isinstance(receipt_path, (str, os.PathLike))
            or not isinstance(guardian_identity, dict)
            or not isinstance(ownership_nonce, str)):
        raise SessionError("guardian ownership receipt is incomplete; refusing a blind signal")

    if proc.poll() is None:
        if proc.stdin is None:
            raise SessionError("guardian control pipe is unavailable")
        try:
            proc.stdin.write(b"STOP\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            # Inventory cannot prove generic absence, and this backend retains
            # an identity-check/signal TOCTOU. Verify exact identity before
            # signalling the guardian only; killing it cannot qualify descendant
            # cleanup.
            try:
                _signal_owned_process(guardian_identity, signal.SIGTERM)
            except SessionError:
                raise SessionError("guardian cleanup is unverified; refusing a blind signal") from exc
            raise SessionError("guardian failed to acknowledge owned cleanup") from exc
    else:
        proc.wait(timeout=0)

    path = Path(receipt_path)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError("guardian exited without a valid cleanup receipt") from exc
    if (receipt.get("status") != "stopped"
            or receipt.get("containment") != "best_effort_lineage_observation"
            or receipt.get("live_admission_qualified") is not False
            or receipt.get("ownership_nonce") != ownership_nonce
            or receipt.get("process_group") != proc.pid
            or not _same_process_instance(guardian_identity, receipt.get("guardian_identity", {}))
            or receipt.get("boot_id") != guardian_identity.get("boot_id")):
        raise SessionError("guardian did not verify observed-process cleanup")


def run_native_turn(runtime: str, argv: list[str], workspace: Path, turn_dir: Path,
                    *, timeout: float, expected_id: str | None = None,
                    planned_id: str | None = None,
                    on_event: Callable[[dict, NativeStream], None] = lambda *_: None,
                    cancel: Callable[[], bool] = lambda: False, lease_fd: int | None = None,
                    admission: ExecutionAdmission = CLOSED_NATIVE_ADMISSION) -> dict:
    """Stream a bounded turn; preserve partial evidence on every failure path."""
    if not 0 < timeout <= 3600:
        raise SessionError("turn timeout must be between 0 and 3600 seconds")
    if planned_id and (runtime != "grok" or expected_id):
        raise SessionError("planned identity cannot replace a confirmed resume identity")
    if admission is not LOCAL_FIXTURE_ADMISSION:
        error = ("native process containment is not qualified: this backend has no "
                 "qualified containment boundary for setsid/double-fork descendants; "
                 "live admission is closed")
        result = {
            "status": "containment_unqualified", "report_received": False,
            "conversation_id": expected_id, "requested_conversation_id": expected_id,
            "planned_conversation_id": planned_id, "reported_model": None,
            "request_id": None, "reported_models": [], "model_identity_source": None,
            "native_terminal_reason": None, "native_exit_code": None,
            "elapsed_seconds": 0.0, "usage": {}, "error": error,
            "failure_status": None,
            "process_cleanup": {
                "status": "not_started", "containment_qualified": False,
                "admission": CLOSED_NATIVE_ADMISSION.name,
            },
            "claims_verified": False, "task_accepted": False,
        }
        (turn_dir / "response.md").write_text("", encoding="utf-8")
        return result
    stream = NativeStream(runtime, expected_id=expected_id or planned_id)
    started = time.monotonic()
    proc = None
    status = "failed"
    error = None
    cleanup = {"status": "not_needed"}
    failure_status = None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    byte_count = 0
    max_bytes = 32 * 1024 * 1024
    guardian_receipt = turn_dir / "guardian-cleanup.json"
    ownership_nonce = uuid.uuid4().hex
    process_receipt: dict | None = None
    child_env = runtime_env(workspace, turn_dir)

    with (turn_dir / "stdout.ndjson").open("wb") as raw, \
         (turn_dir / "stderr.log").open("wb") as err, \
         (turn_dir / "events.ndjson").open("w", encoding="utf-8") as events:
        def consume(line: str) -> None:
            if not line.strip():
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SessionError("non-JSON data on the native event stream") from exc
            stream.feed(event)
            events.write(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 3),
                                     "native": event}, ensure_ascii=False) + "\n")
            events.flush()
            on_event(event, stream)

        try:
            guardian = [sys.executable, "-u", str(Path(__file__).with_name("session_process.py")),
                        "--receipt", str(guardian_receipt), "--nonce", ownership_nonce, "--", *argv]
            proc = subprocess.Popen(guardian, cwd=workspace, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=child_env,
                                    pass_fds=(lease_fd,) if lease_fd is not None else (), start_new_session=True)
            proc._council_cleanup_receipt = str(guardian_receipt)
            proc._council_ownership_nonce = ownership_nonce
            guardian_inventory = _process_inventory()
            guardian_identity = _process_identity(proc.pid, inventory=guardian_inventory)
            proc._council_guardian_identity = guardian_identity
            process_receipt = {
                "schema": 2,
                "guardian_pid": proc.pid,
                "process_group": proc.pid,
                "boot_id": guardian_identity["boot_id"],
                "guardian_identity": guardian_identity,
                "guardian_cleanup_receipt": guardian_receipt.name,
                "ownership_nonce": ownership_nonce,
                "inventory_provenance": {
                    "method": guardian_inventory.method,
                    "observed_at_monotonic_ns": guardian_inventory.observed_at_monotonic_ns,
                    "complete_for_descendants": guardian_inventory.complete_for_descendants,
                },
                "ownership_scope": "observed_process_instances_only",
                "containment_qualified": False,
                "admission": admission.name,
                "execution_environment": _environment_receipt(
                    runtime, argv, workspace, turn_dir, child_env,
                ),
            }
            _atomic_json(turn_dir / "process.json", process_receipt)
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
                selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if cancel():
                        status = "cancelled"
                        raise SessionError("turn cancelled by the coordinator")
                    if time.monotonic() - started >= timeout:
                        status = "timed_out"
                        raise SessionError("turn exceeded its time budget")
                    for key, _ in selector.select(timeout=0.1):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        sink = raw if key.data == "stdout" else err
                        sink.write(data)
                        sink.flush()
                        byte_count += len(data)
                        if byte_count > max_bytes:
                            raise SessionError("native output exceeded the 32 MiB turn limit")
                        if key.data == "stderr":
                            continue
                        pending += decoder.decode(data)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            consume(line)
                pending += decoder.decode(b"", final=True)
                if pending.strip():
                    consume(pending)
            # A runtime can close stdout while still hanging. Keep this wait in
            # the same budget and continue servicing cancellation.
            while proc.poll() is None:
                if cancel():
                    status = "cancelled"
                    raise SessionError("turn cancelled by the coordinator")
                if time.monotonic() - started >= timeout:
                    status = "timed_out"
                    raise SessionError("turn exceeded its time budget after closing stdout")
                time.sleep(0.05)
            if proc.returncode != 0:
                error = f"native process exited with code {proc.returncode}; see stderr.log"
            elif stream.completed():
                status = "completed"
            else:
                error = "missing or unsuccessful native terminal result; see raw turn evidence"
        except KeyboardInterrupt:
            status, error = "cancelled", "coordinator interrupted"
        except (OSError, SessionError, subprocess.SubprocessError) as exc:
            error = str(exc)
        finally:
            if proc is not None:
                try:
                    _stop_group(proc)
                    guardian_data = json.loads(guardian_receipt.read_text(encoding="utf-8"))
                    cleanup = {
                        "status": "stopped", "process_group": proc.pid,
                        "ownership_nonce": guardian_data.get("ownership_nonce"),
                        "boot_id": guardian_data.get("boot_id"),
                        "guardian_identity": guardian_identity,
                        "native_leader_identity": guardian_data.get("native_leader_identity"),
                        "observed_processes": guardian_data.get("observed_processes", []),
                        "inventory_provenance": guardian_data.get("inventory_provenance"),
                        "ownership_scope": "observed_process_instances_only",
                        "containment": guardian_data.get("containment"),
                        "containment_qualified": False,
                        "receipt": guardian_receipt.name,
                    }
                    if process_receipt is not None:
                        process_receipt["guardian_cleanup"] = cleanup
                        _atomic_json(turn_dir / "process.json", process_receipt)
                except (OSError, SessionError, subprocess.SubprocessError) as exc:
                    # Preserve the original timeout/protocol failure and the
                    # conversation even if process termination also fails.
                    failure_status, status = status, "cleanup_failed"
                    cleanup = {"status": "failed", "process_group": proc.pid,
                               "error_type": type(exc).__name__,
                               "containment_qualified": False,
                               "ownership_scope": "observed_process_instances_only"}
                finally:
                    # Closing the lifeline also asks the guardian to stop. It
                    # does not prove success; the next turn must check absence.
                    for pipe in (proc.stdin, proc.stdout, proc.stderr):
                        if pipe is not None:
                            pipe.close()

    result = {"status": status, "report_received": status == "completed",
              "conversation_id": stream.conversation_id or expected_id,
              "requested_conversation_id": expected_id,
              "planned_conversation_id": planned_id,
              "reported_model": stream.reported_model, "request_id": stream.request_id,
              "reported_models": stream.reported_models, "model_identity_source": stream.model_identity_source,
              "native_terminal_reason": stream.terminal_reason,
              "native_exit_code": proc.returncode if proc else None,
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "usage": stream.usage, "error": error,
              "failure_status": failure_status, "process_cleanup": cleanup,
              "claims_verified": False, "task_accepted": False}
    (turn_dir / "response.md").write_text(stream.response if isinstance(stream.response, str) else "",
                                          encoding="utf-8")
    return result

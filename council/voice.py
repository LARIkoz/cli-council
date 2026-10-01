"""`council voice <name>` · `council wait <dir>` · `council roster` — one voice, no council.

The single-voice entry point every outside caller uses (a Claude relay agent, a
Gemini courier, a review script), so the command, model and effort of each voice
live in exactly one place: the [providers.<name>] block of council.toml. Callers
never hand-write `grok-p …` or `codex exec -m …` again — that copying is how the
rosters drifted apart.

Job directory layout (mode 0700; everything a caller needs, nothing else):
  prompt.md    the prompt, copied in verbatim
  status.json  {voice, status: running|alive|failed, reason, started, finished, wall_seconds, pid, config}
  answer.md    the voice's text (status alive)
  error.txt    the failure chain or engine traceback (status failed)
  run.log      engine stderr of a detached job

`--detach` returns at once and leaves a background job; `council wait` blocks up to
--max seconds and prints the finished job or `still_running`. That split exists
because agent harnesses cap one foreground shell call (~10 min) while a long
review on a slow voice can take longer.

The voice runs with the job directory as its working directory, so a voice CLI
that looks around sees only the job files, never the caller's repository, and the
detached worker can never import a `council` module planted in the caller's cwd.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import config, providers

# 2 is argparse's usage-error code, so a voice failure gets its own.
EXIT_ALIVE, EXIT_FAILED, EXIT_RUNNING = 0, 1, 3
# A detached job whose pid was never recorded (the launcher died between writing
# the status and recording the pid) is declared dead after this long.
STARTUP_GRACE = 30.0


def _now() -> float:
    return time.time()


@contextmanager
def _locked(d: Path):
    with open(d / ".status.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _update_status(d: Path, only_if_status: str | None = None, **fields) -> bool:
    """Read-modify-write status.json under a lock; atomic replace so a reader never
    sees half a file. With `only_if_status`, write only while the job is still in
    that state — the compare-and-set that stops a waiter from overwriting a final
    status the worker wrote a moment earlier."""
    path = d / "status.json"
    with _locked(d):
        try:
            cur = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            cur = {}
        if only_if_status is not None and cur.get("status") != only_if_status:
            return False
        cur.update(fields)
        tmp = d / f".status.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    return True


def read_status(d: Path) -> dict:
    try:
        return json.loads((d / "status.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def status_line(st: dict) -> str:
    state = st.get("status", "unknown")
    if state == "failed":
        state = f"failed:{st.get('reason', 'error')}"
    wall = st.get("wall_seconds")
    wall_s = f"{wall:.0f}s" if isinstance(wall, (int, float)) else "?s"
    return f"council-voice status: {state} · voice: {st.get('voice', '?')} · {wall_s} · config: {st.get('config', '?')}"


def _fail(d: Path, reason: str, text: str, started: float | None = None) -> int:
    (d / "error.txt").write_text(text.rstrip("\n") + "\n")
    _update_status(d, status="failed", reason=reason, finished=_now(),
                   wall_seconds=round(_now() - started, 1) if started else 0.0)
    return EXIT_FAILED


def run_job(name: str, d: Path, cfg_path: str | None, timeout: float | None) -> int:
    """Run one voice and record the outcome in `d`. Every path ends in a final
    status: an engine exception is a loud `failed:engine_error`, never a silent death."""
    started = _now()
    try:
        cfg = config.load(cfg_path)
        _update_status(d, voice=name, config=cfg.source)
        if name not in cfg.providers:
            return _fail(d, "unknown_voice", f"unknown voice '{name}'; known: {sorted(cfg.providers)}")
        prompt = (d / "prompt.md").read_text()
        _update_status(d, status="running", started=started, pid=os.getpid())
        here = os.getcwd()
        os.chdir(d)  # the voice sees the job files only, never the caller's repository
        try:
            ok, out = providers.invoke_chain(name, cfg.providers, prompt, timeout,
                                             log=lambda m: print(m, file=sys.stderr))
        finally:
            os.chdir(here)
    except Exception:  # noqa: BLE001 — any engine failure must reach the caller
        return _fail(d, "engine_error", traceback.format_exc(), started)
    if not ok:
        return _fail(d, "voice_error", out, started)
    (d / "answer.md").write_text(out)
    _update_status(d, status="alive", reason="", finished=_now(), wall_seconds=round(_now() - started, 1))
    return EXIT_ALIVE


def _tail(path: Path, limit: int = 600) -> str:
    try:
        return path.read_text(errors="replace").strip()[-limit:]
    except FileNotFoundError:
        return ""


def render_final(d: Path, st: dict) -> str:
    lines = [status_line(st), f"out: {d}"]
    if st.get("status") == "alive":
        answer = d / "answer.md"
        if answer.exists():
            lines += ["", answer.read_text().rstrip("\n")]
        else:
            lines.append("error_head: status alive but answer.md is missing")
    else:
        err = _tail(d / "error.txt") if (d / "error.txt").exists() else _tail(d / "run.log")
        lines.append(f"error_head: {err[:600]}")
    return "\n".join(lines)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait(d: Path, max_seconds: float, poll: float = 2.0) -> tuple[int, str]:
    deadline = _now() + max_seconds
    while True:
        st = read_status(d)
        if st.get("status") in ("alive", "failed"):
            return (EXIT_ALIVE if st["status"] == "alive" else EXIT_FAILED), render_final(d, st)
        # A job whose process vanished without a final status would otherwise be
        # waited on forever. Mark it dead only while it is STILL "running" (under the
        # lock), so a worker that finished between our read and now keeps its result.
        pid = st.get("pid")
        started = st.get("started", _now())
        dead = (pid is not None and not _pid_alive(pid)) or \
               (pid is None and _now() - started > STARTUP_GRACE)
        if st.get("status") == "running" and dead:
            _update_status(d, only_if_status="running", status="failed", reason="job_died",
                           finished=_now(), wall_seconds=round(_now() - started, 1))
            continue
        remaining = deadline - _now()
        if remaining <= 0:
            return EXIT_RUNNING, f"still_running · voice: {st.get('voice', '?')} · {_now() - started:.0f}s · out: {d}"
        time.sleep(min(poll, remaining))


def _write_prompt(ap, args, d: Path) -> None:
    target = d / "prompt.md"
    if args.prompt_file:
        src = Path(args.prompt_file).expanduser()
        if src.resolve() != target.resolve():
            target.write_text(src.read_text())
    elif not sys.stdin.isatty():
        if target.exists():
            ap.error(f"{target} already exists and a prompt was piped on stdin; pass one of them")
        target.write_text(sys.stdin.read())
    elif not target.exists():
        ap.error("no prompt: pass --prompt-file or pipe it on stdin")
    os.chmod(target, 0o600)
    if not target.read_text().strip():
        ap.error("the prompt is empty")


def voice_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="council voice",
        description="Ask ONE enrolled voice, no ranking and no chairman. The command, model and "
                    "effort come from [providers.<name>] in council.toml.")
    ap.add_argument("name", help="voice name from council.toml (see `council roster`)")
    ap.add_argument("--prompt-file", help="prompt file (default: stdin)")
    ap.add_argument("--out-dir", required=True, help="job directory (created; must be empty or new)")
    ap.add_argument("--config", help="path to council.toml")
    ap.add_argument("--timeout", type=float, help="per-call timeout seconds (default: the voice's own)")
    ap.add_argument("--detach", action="store_true",
                    help="start in the background and return; collect with `council wait <out-dir>`")
    ap.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    d = Path(args.out_dir).expanduser().resolve()
    if args._child:
        # The detached worker: the launcher already prepared the job directory.
        return run_job(args.name, d, args.config, args.timeout)

    # Validate before creating anything, so a typo fails at launch, not at wait.
    cfg = config.load(args.config)
    if args.name not in cfg.providers:
        ap.error(f"unknown voice '{args.name}'; roster: {', '.join(cfg.voices)}; "
                 f"known: {', '.join(sorted(cfg.providers))}")
    if d.exists() and any(p.name != "prompt.md" for p in d.iterdir()):
        ap.error(f"{d} already holds a job; use a new --out-dir")
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)  # prompts carry private code
    _write_prompt(ap, args, d)
    # The worker must read the SAME config the launcher validated, whatever its cwd.
    cfg_path = cfg.source if Path(cfg.source).is_file() else args.config

    if not args.detach:
        rc = run_job(args.name, d, cfg_path, args.timeout)
        print(render_final(d, read_status(d)))
        return rc

    _update_status(d, voice=args.name, status="running", started=_now(), pid=None,
                   config=cfg_path or "defaults")
    cmd = [sys.executable, "-m", "council", "voice", args.name, "--out-dir", str(d), "--_child"]
    if cfg_path:
        cmd += ["--config", str(cfg_path)]
    if args.timeout:
        cmd += ["--timeout", str(args.timeout)]
    pkg_root = str(Path(__file__).resolve().parent.parent)
    extra = os.environ.get("PYTHONPATH")
    env = {**os.environ, "PYTHONPATH": pkg_root + (os.pathsep + extra if extra else "")}
    with open(d / "run.log", "w") as log:
        proc = subprocess.Popen(cmd, cwd=str(d), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, env=env)
    # Record the pid at once so `wait` can tell a dead worker from a slow one; skip if
    # the worker already finished and wrote its final status.
    _update_status(d, only_if_status="running", pid=proc.pid)
    print(f"started · voice: {args.name} · out: {d}")
    return 0


def wait_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="council wait",
                                 description="Wait for a `council voice --detach` job and print it.")
    ap.add_argument("out_dir")
    ap.add_argument("--max", type=float, default=590, help="seconds to block before `still_running`")
    args = ap.parse_args(argv)
    d = Path(args.out_dir).expanduser()
    if not (d / "status.json").exists():
        ap.error(f"{d} has no job (status.json missing)")
    rc, text = wait(d, args.max)
    print(text)
    return rc


def roster_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="council roster",
                                 description="Print the enrolled voices ([council].voices), one per line.")
    ap.add_argument("--config", help="path to council.toml")
    ap.add_argument("--json", action="store_true", help="also show chairman and panels")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    if args.json:
        print(json.dumps({"voices": cfg.voices, "chairman": cfg.chairman,
                          "review_audit": cfg.review_audit, "decide_audit": cfg.decide_audit,
                          "config": cfg.source}, indent=2))
    else:
        print("\n".join(cfg.voices))
    return 0

"""`council voice <name>` · `council wait <dir>` · `council roster` — one voice, no council.

The single-voice entry point every outside caller uses (a Claude relay agent, a
Gemini courier, a review script), so the command, model and effort of each voice
live in exactly one place: the [providers.<name>] block of council.toml. Callers
never hand-write `grok-p …` or `codex exec -m …` again — that copying is how the
rosters drifted apart.

Job directory layout (everything a caller needs, nothing else):
  prompt.md    the prompt, copied in verbatim
  status.json  {voice, status: running|alive|failed, reason, started, finished, wall_seconds, pid}
  answer.md    the voice's text (status alive)
  error.txt    the failure chain (status failed)
  run.log      engine stderr of a detached job

`--detach` returns at once and leaves a background job; `council wait` blocks up to
--max seconds and prints the finished job or `still_running`. That split exists
because agent harnesses cap one foreground shell call (~10 min) while a long
review on a slow voice can take longer.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config, providers

EXIT_ALIVE, EXIT_FAILED, EXIT_RUNNING = 0, 2, 3
STARTUP_GRACE = 60.0  # seconds a detached worker may take to record its pid


def _now() -> float:
    return time.time()


def _write_status(d: Path, **fields) -> None:
    path = d / "status.json"
    cur = json.loads(path.read_text()) if path.exists() else {}
    cur.update(fields)
    tmp = d / "status.json.tmp"
    tmp.write_text(json.dumps(cur, indent=2, ensure_ascii=False))
    os.replace(tmp, path)  # atomic, so `wait` never reads a half-written file


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


def run_job(name: str, d: Path, cfg_path: str | None, timeout: float | None) -> int:
    """Run one voice in the foreground and record the outcome in `d`."""
    cfg = config.load(cfg_path)
    if name not in cfg.providers:
        _write_status(d, voice=name, status="failed", reason="unknown_voice", finished=_now(),
                      wall_seconds=0.0, config=cfg.source)
        (d / "error.txt").write_text(f"unknown voice '{name}'; known: {sorted(cfg.providers)}\n")
        return EXIT_FAILED
    prompt = (d / "prompt.md").read_text()
    started = _now()
    _write_status(d, voice=name, status="running", started=started, pid=os.getpid(), config=cfg.source)
    ok, out = providers.invoke_chain(name, cfg.providers, prompt, timeout,
                                     log=lambda m: print(m, file=sys.stderr))
    wall = round(_now() - started, 1)
    if ok:
        (d / "answer.md").write_text(out)
        _write_status(d, status="alive", reason="", finished=_now(), wall_seconds=wall)
        return EXIT_ALIVE
    (d / "error.txt").write_text(out + "\n")
    _write_status(d, status="failed", reason="voice_error", finished=_now(), wall_seconds=wall)
    return EXIT_FAILED


def render_final(d: Path, st: dict) -> str:
    lines = [status_line(st), f"out: {d}"]
    if st.get("status") == "alive":
        lines += ["", (d / "answer.md").read_text().rstrip("\n")]
    else:
        err = (d / "error.txt").read_text().strip() if (d / "error.txt").exists() else ""
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
        # waited on forever; report it as failed, loudly.
        pid = st.get("pid")
        never_started = pid is None and _now() - st.get("started", _now()) > STARTUP_GRACE
        if st.get("status") == "running" and (never_started or (pid is not None and not _pid_alive(pid))):
            _write_status(d, status="failed", reason="job_died", finished=_now(),
                          wall_seconds=round(_now() - st.get("started", _now()), 1))
            continue
        if _now() >= deadline:
            elapsed = _now() - st.get("started", _now())
            return EXIT_RUNNING, f"still_running · voice: {st.get('voice', '?')} · {elapsed:.0f}s · out: {d}"
        time.sleep(poll)


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

    d = Path(args.out_dir).expanduser()
    if args._child:
        # The detached worker: the parent already prepared the job directory.
        return run_job(args.name, d, args.config, args.timeout)
    if d.exists() and any(p.name != "prompt.md" for p in d.iterdir()):
        ap.error(f"{d} already holds a job; use a new --out-dir")
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)  # prompts carry private code

    if args.prompt_file:
        src = Path(args.prompt_file).expanduser()
        if src.resolve() != (d / "prompt.md").resolve():
            (d / "prompt.md").write_text(src.read_text())
    elif not (d / "prompt.md").exists():
        if sys.stdin.isatty():
            ap.error("no prompt: pass --prompt-file or pipe it on stdin")
        (d / "prompt.md").write_text(sys.stdin.read())
    if not (d / "prompt.md").read_text().strip():
        ap.error("the prompt is empty")

    if not args.detach:
        rc = run_job(args.name, d, args.config, args.timeout)
        print(render_final(d, read_status(d)))
        return rc

    # Status first, then spawn: the worker overwrites it with its own pid, so a fast
    # worker can never have its final status clobbered by the parent.
    _write_status(d, voice=args.name, status="running", started=_now(), pid=None,
                  config=args.config or "auto")
    cmd = [sys.executable, "-m", "council", "voice", args.name, "--out-dir", str(d), "--_child"]
    if args.config:
        cmd += ["--config", args.config]
    if args.timeout:
        cmd += ["--timeout", str(args.timeout)]
    log = open(d / "run.log", "w")
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": pkg_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, env=env)
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

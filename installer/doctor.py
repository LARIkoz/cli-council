"""doctor — the deterministic, checkable half of the install contract.

Subcommands the installer (or you) runs:

    doctor detect              which provider CLIs are installed
    doctor smoke <voice>       fire a 1-line live call; PASS only if it works
    doctor enroll <v> [<v>...] write council.toml (first voice with claude wins chair)
    doctor list                show enrolled vs available

`smoke` is the gate: a voice is only fit to enrol if its smoke PASSES here. It
proves the CLI is installed AND authenticated AND actually answers — in one
check, with no credential parsing. Everything prints loudly; nothing half-adds.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from council.providers import PROVIDERS, invoke, is_installed  # noqa: E402

SMOKE_PROMPT = "Reply with exactly the word: ok"
CONFIG = Path.cwd() / "council.toml"


def detect() -> int:
    print("Detected provider CLIs:")
    for name, p in PROVIDERS.items():
        tag = " (native default)" if p.native else (" (experimental)" if p.experimental else "")
        state = "installed" if is_installed(p) else "MISSING"
        print(f"  {name:8} {state:9}{tag}")
        if not is_installed(p):
            print(f"           install: {p.install_hint}")
            print(f"           login:   {p.login_hint}")
    return 0


def smoke(voice: str) -> int:
    p = PROVIDERS.get(voice)
    if p is None:
        print(f"unknown voice '{voice}'; known: {sorted(PROVIDERS)}", file=sys.stderr)
        return 2
    if not is_installed(p):
        print(f"SMOKE FAIL · {voice}: not installed. {p.install_hint}")
        return 1
    print(f"smoking {voice} (1 live call)…", file=sys.stderr)
    ok, out = invoke(p, SMOKE_PROMPT, timeout=90.0)
    if ok:
        print(f"SMOKE PASS · {voice}: {out[:60]!r}")
        return 0
    print(f"SMOKE FAIL · {voice}: {out}")
    print(f"           if not logged in: {p.login_hint}")
    return 1


def enroll(voices: list[str]) -> int:
    unknown = [v for v in voices if v not in PROVIDERS]
    if unknown:
        print(f"unknown voices {unknown}; known: {sorted(PROVIDERS)}", file=sys.stderr)
        return 2
    if "claude" not in voices:
        # Native default is always present; put it first unless the user is
        # deliberately building a claude-less council (allowed, but warned).
        print("note: 'claude' (native default) not in the list — that's allowed, "
              "but the out-of-box guarantee is Claude. Continuing with your choice.")
    chairman = "claude" if "claude" in voices else voices[0]
    CONFIG.write_text(_toml(voices, chairman))
    print(f"wrote {CONFIG}")
    print(f"  voices   = {voices}")
    print(f"  chairman = {chairman}")
    print("run `council \"your question\"` to use it.")
    return 0


def list_voices() -> int:
    enrolled = _read_enrolled()
    print("Voices:")
    for name, p in PROVIDERS.items():
        marks = []
        if p.native:
            marks.append("native")
        if name in enrolled:
            marks.append("ENROLLED")
        if is_installed(p):
            marks.append("installed")
        print(f"  {name:8} {'· '.join(marks) or 'available'}")
    return 0


def _read_enrolled() -> list[str]:
    if not CONFIG.is_file():
        return ["claude"]
    try:
        import tomllib
        return list(tomllib.loads(CONFIG.read_text()).get("council", {}).get("voices", ["claude"]))
    except Exception:
        return []


def _toml(voices: list[str], chairman: str) -> str:
    vs = ", ".join(f'"{v}"' for v in voices)
    return ("# written by `doctor enroll` — only smoke-PASSED voices should be here\n"
            "[council]\n"
            f"voices = [{vs}]\n"
            f'chairman = "{chairman}"\n')


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "detect":
        return detect()
    if cmd == "smoke":
        return smoke(rest[0]) if rest else _usage()
    if cmd == "enroll":
        return enroll(rest) if rest else _usage()
    if cmd == "list":
        return list_voices()
    return _usage()


def _usage() -> int:
    print("usage: doctor {detect | smoke <voice> | enroll <voice>... | list}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

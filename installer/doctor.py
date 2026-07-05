"""doctor — the deterministic, checkable half of the install contract.

Subcommands the installer (or you) runs:

    doctor detect              which provider CLIs are installed
    doctor smoke <voice>       fire a 1-line live call; PASS only if it works
    doctor enroll <v> [<v>...] re-smoke each, then write council.toml with only
                               the PASSes (add --no-verify to trust a prior smoke)
    doctor list                show enrolled vs available

`smoke` is the gate: a voice is only fit to enrol if its smoke PASSES here. It
proves the CLI is installed AND authenticated AND actually answers — in one
check, with no credential parsing. Everything prints loudly; nothing half-adds.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from council import config as councilcfg  # noqa: E402
from council.providers import PROVIDERS, invoke, is_installed  # noqa: E402

SMOKE_PROMPT = "Reply with exactly the word: ok"
CONFIG = Path.cwd() / "council.toml"


def _registry() -> dict:
    """Every voice this machine knows: the built-in CLI voices plus any http
    (token) voices defined in council.toml. Falls back to the built-ins if the
    file is unreadable/half-written, so detect/enroll still work."""
    try:
        return councilcfg.provider_registry(str(CONFIG) if CONFIG.is_file() else None)
    except Exception:  # noqa: BLE001 — a broken toml shouldn't hide the CLI voices
        return dict(PROVIDERS)


def detect() -> int:
    print("Detected voices (CLI + any token voices in council.toml):")
    for name, p in _registry().items():
        kind = "http" if p.transport == "http" else "cli"
        tag = " (native default)" if p.native else (" (experimental)" if p.experimental else "")
        state = "ready" if is_installed(p) else "MISSING"
        print(f"  {name:10} {kind:5} {state:8}{tag}")
        if not is_installed(p):
            print(f"           install: {p.install_hint}")
            print(f"           login:   {p.login_hint}")
    return 0


def _smoke(p) -> tuple[bool, str]:
    """One live 1-word call. Returns (ok, detail). Not-installed counts as a
    fail — the enrolment gate only wants voices that actually answer."""
    if not is_installed(p):
        return False, f"not installed. {p.install_hint}"
    ok, out = invoke(p, SMOKE_PROMPT, timeout=90.0)
    return (True, repr(out[:60])) if ok else (False, out)


def smoke(voice: str) -> int:
    reg = _registry()
    p = reg.get(voice)
    if p is None:
        print(f"unknown voice '{voice}'; known: {sorted(reg)}", file=sys.stderr)
        return 2
    print(f"smoking {voice} (1 live call)…", file=sys.stderr)
    ok, detail = _smoke(p)
    if ok:
        print(f"SMOKE PASS · {voice}: {detail}")
        return 0
    print(f"SMOKE FAIL · {voice}: {detail}")
    print(f"           if not logged in: {p.login_hint}")
    return 1


def enroll(voices: list[str], verify: bool = True) -> int:
    reg = _registry()
    unknown = [v for v in voices if v not in reg]
    if unknown:
        print(f"unknown voices {unknown}; known: {sorted(reg)}", file=sys.stderr)
        return 2

    if verify:
        # The enrolment gate IS smoke: re-prove every voice actually answers
        # before it touches council.toml. A voice that fails is dropped LOUDLY,
        # never half-added — this is what makes the "strict" contract self-
        # enforcing rather than trusting the caller. Pass --no-verify only if you
        # just smoked these voices in the previous gate and want to skip the cost.
        kept = []
        for v in voices:
            ok, detail = _smoke(reg[v])
            print(f"  {'PASS' if ok else 'FAIL'} · {v}: {detail}", file=sys.stderr)
            if ok:
                kept.append(v)
            else:
                print(f"refused — NOT enrolling '{v}' (failed smoke). {reg[v].login_hint}")
        voices = kept
        if not voices:
            print("no voice passed smoke — nothing enrolled. Fix install/login and retry.",
                  file=sys.stderr)
            return 1

    if "claude" not in voices:
        # Native default is always present; put it first unless the user is
        # deliberately building a claude-less council (allowed, but warned).
        print("note: 'claude' (native default) not in the list — that's allowed, "
              "but the out-of-box guarantee is Claude. Continuing with your choice.")
    chairman = "claude" if "claude" in voices else voices[0]
    # Preserve any hand-written [providers.*] blocks (e.g. http/token voices) —
    # enroll only owns the [council] section, it must not erase your voice defs.
    CONFIG.write_text(_toml(voices, chairman) + _existing_provider_blocks())
    print(f"wrote {CONFIG}")
    print(f"  voices   = {voices}")
    print(f"  chairman = {chairman}")
    print("run `council \"your question\"` to use it.")
    return 0


def list_voices() -> int:
    enrolled = _read_enrolled()
    print("Voices:")
    for name, p in _registry().items():
        marks = []
        if p.native:
            marks.append("native")
        if p.transport == "http":
            marks.append("token")
        if name in enrolled:
            marks.append("ENROLLED")
        if is_installed(p):
            marks.append("ready")
        print(f"  {name:10} {'· '.join(marks) or 'available'}")
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
    q = lambda xs: ", ".join(f'"{v}"' for v in xs)  # noqa: E731
    # Verification panels so an enrolled config is GATED out of the box (not just a bare
    # council). audit = every non-chairman voice (max recall; the chairman stays off its
    # own audit — no self-approval). review adds a lean redteam (≤2 voices); decide keeps
    # redteam OFF (a recommendation has no ground-truth claim to refute — its guard is the
    # audit). A degenerate 1-voice council has no non-chairman auditor → panels stay empty
    # (the run reports `unverified`, honestly, rather than self-auditing).
    others = [v for v in voices if v != chairman]
    return ("# written by `doctor enroll` — only smoke-PASSED voices should be here\n"
            "[council]\n"
            f"voices = [{q(voices)}]\n"
            f'chairman = "{chairman}"\n'
            "\n[review]\n"
            f"audit   = [{q(others)}]\n"
            f"redteam = [{q(others[:2])}]\n"
            "\n[decide]\n"
            f"audit   = [{q(others)}]\n"
            "redteam = []\n")


def _emit_toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_emit_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _emit_provider_blocks(providers_data: dict) -> str:
    """Re-serialize the [providers.*] tables so `enroll` can rewrite [council]
    without dropping the user's voice definitions. Flat key=value only — that's
    all a provider block ever holds (type/endpoint/model/key_env/argv/timeout/…)."""
    out = []
    for name, block in providers_data.items():
        if not isinstance(block, dict):
            continue
        out.append(f"\n[providers.{name}]")
        for k, val in block.items():
            out.append(f"{k} = {_emit_toml_value(val)}")
    return "\n".join(out) + ("\n" if out else "")


def _existing_provider_blocks() -> str:
    if not CONFIG.is_file():
        return ""
    try:
        import tomllib
        data = tomllib.loads(CONFIG.read_text())
    except Exception:  # noqa: BLE001 — unreadable file: nothing to preserve
        return ""
    return _emit_provider_blocks(data.get("providers") or {})


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
        verify = "--no-verify" not in rest
        rest = [r for r in rest if r != "--no-verify"]
        return enroll(rest, verify=verify) if rest else _usage()
    if cmd == "list":
        return list_voices()
    return _usage()


def _usage() -> int:
    print("usage: doctor {detect | smoke <voice> | enroll <voice>... | list}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

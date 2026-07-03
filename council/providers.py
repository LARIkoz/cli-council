"""Subscription-CLI providers.

Each provider is an official, first-party CLI the user installed and logged into
themselves. cli-council only ever shells out to these binaries — it makes no HTTP
requests and stores no credentials. Adding a provider = describing how to invoke
its headless single-shot mode; the installer's smoke test is the source of truth
for whether a given voice actually works on this machine.

Invocation flags are the vendors' public headless flags. If a vendor changes
them, the smoke test fails loudly and that voice simply isn't enrolled — nothing
breaks silently. Override any command via council.toml [providers.<name>].
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Fallback per-call ceiling for voices that don't declare their own. Reasoning
# CLIs answering a small prompt return in seconds; the ceiling only bites on a
# genuinely stuck call. Slow voices raise it via Provider.timeout (below).
DEFAULT_TIMEOUT = 300.0


def _plain(stdout: str) -> str:
    return stdout.strip()


def _grok_json(stdout: str) -> str:
    """grok --output-format json prints one pretty object with a `text` field."""
    try:
        return (json.loads(stdout).get("text") or "").strip()
    except Exception:
        return stdout.strip()


@dataclass(frozen=True)
class Provider:
    name: str
    bin: str
    argv: list           # {prompt} = inline arg · {prompt_file} = temp-file path · else stdin
    install_hint: str
    login_hint: str
    native: bool = False
    uses_prompt_file: bool = False
    extract: Callable[[str], str] = _plain
    experimental: bool = False
    env: dict = field(default_factory=dict)
    timeout: float = 0.0  # per-voice ceiling in seconds; 0 = use DEFAULT_TIMEOUT


# The four official subscription CLIs. Claude is the native default (Claude Code
# is built for headless agentic use on a Claude subscription); the rest are opt-in.
PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        name="claude", bin="claude", native=True,
        argv=["claude", "-p"], extract=_plain,
        install_hint="npm i -g @anthropic-ai/claude-code   (https://docs.claude.com/claude-code)",
        login_hint="run `claude` once and use /login (or `claude setup-token`)",
    ),
    "codex": Provider(
        name="codex", bin="codex",
        # --skip-git-repo-check: `codex exec` otherwise refuses to run outside a
        # "trusted" (git) directory. A council question is read-only Q&A on stdin,
        # so the guard just breaks "run council from anywhere" — skip it.
        argv=["codex", "exec", "--skip-git-repo-check", "-"], extract=_plain,
        install_hint="npm i -g @openai/codex   (or: brew install codex)",
        login_hint="codex login",
        # High-reasoning voices need headroom on the peer-ranking stage, where
        # the prompt carries every other voice's full answer. 300s timed one out
        # mid-ranking in a 3-voice council; 600s clears a 4-voice bundle.
        timeout=600.0,
    ),
    "grok": Provider(
        name="grok", bin="grok", uses_prompt_file=True,
        argv=["grok", "--prompt-file", "{prompt_file}", "--output-format", "json"],
        extract=_grok_json,
        install_hint="curl -fsSL https://x.ai/cli/install.sh | bash",
        login_hint="grok login",
        timeout=600.0,  # same as codex: slow on the multi-answer ranking bundle
    ),
    # Experimental: gemini's non-interactive mode + auth vary by setup (some
    # installs need vendor-specific environment variables or a first-run consent
    # — see Google's gemini-cli docs). Set any needed env in your shell. The
    # smoke test is the arbiter: if it PASSes on your machine, enrol it.
    "gemini": Provider(
        name="gemini", bin="gemini", experimental=True,
        argv=["gemini", "-p", "{prompt}"], extract=_plain,
        install_hint="npm i -g @google/gemini-cli",
        login_hint="run `gemini` once and complete login; headless may need vendor env (see README)",
    ),
    # Google Antigravity — the newer agentic Google CLI. On setups where the
    # classic gemini CLI is retired, this is the live Google voice. `-p`/--print
    # runs a single prompt non-interactively. Experimental until it smokes.
    "agy": Provider(
        name="agy", bin="agy", experimental=True,
        argv=["agy", "-p", "{prompt}"], extract=_plain,
        install_hint="Google Antigravity CLI (install per Google's instructions)",
        login_hint="run `agy` once and sign in to your Google/Antigravity account",
    ),
}


def is_installed(p: Provider) -> bool:
    return shutil.which(p.bin) is not None


def resolve_timeout(p: Provider, override: float | None = None) -> float:
    """Effective per-call ceiling. An explicit override (CLI --timeout / toml)
    wins for every voice; otherwise the voice's own ceiling, else the default."""
    if override:
        return override
    return p.timeout or DEFAULT_TIMEOUT


def invoke(p: Provider, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Run the provider's CLI headless with `prompt`. Returns (ok, text_or_error).

    ok is False on: binary missing, non-zero exit, timeout, or empty output —
    all reported, never swallowed.
    """
    if not is_installed(p):
        return False, f"{p.bin}: not installed"

    tmp: Optional[Path] = None
    try:
        if p.uses_prompt_file:
            fd = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
            fd.write(prompt)
            fd.close()
            tmp = Path(fd.name)
            argv = [a.replace("{prompt_file}", str(tmp)) for a in p.argv]
            stdin_data = None
        elif any("{prompt}" in a for a in p.argv):
            # Inline the prompt as an argv element (no shell → no quoting issues;
            # our prompts are well under the OS ARG_MAX).
            argv = [a.replace("{prompt}", prompt) for a in p.argv]
            stdin_data = None
        else:
            argv = list(p.argv)
            stdin_data = prompt

        proc = subprocess.run(
            argv, input=stdin_data, capture_output=True, text=True,
            timeout=timeout, env={**_base_env(), **p.env},
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            return False, f"{p.name}: exit {proc.returncode}: {err[:400]}"
        text = p.extract(proc.stdout or "")
        if len(text) < 1:
            return False, f"{p.name}: empty output (stderr: {(proc.stderr or '')[:200]})"
        return True, text
    except subprocess.TimeoutExpired:
        return False, f"{p.name}: timeout after {timeout:.0f}s"
    except Exception as e:  # noqa: BLE001 — surface anything, loudly
        return False, f"{p.name}: {e!r}"
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _base_env() -> dict:
    import os
    # Keep the child env, but disable any CLI auto-updaters that would print noise
    # or block on stdin in a non-interactive run.
    e = dict(os.environ)
    e.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    e.setdefault("CI", "1")
    return e

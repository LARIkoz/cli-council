"""Council voices — two transports, one interface.

The default and the floor is a **subscription CLI**: an official, first-party
binary the user installed and logged into themselves. cli-council shells out to
it and stores no credentials. That's the whole native path, and it needs no keys.

A voice may **optionally** use the **http** transport instead: a token-based call
to an OpenAI-compatible `/chat/completions` endpoint. This is strictly opt-in and
lives only in the user's local (git-ignored) council.toml — the shipped provider
table below is CLI-only, so a clean checkout still makes zero HTTP requests. An
http voice reads its API key from an environment variable *at call time*; the key
is never written to council.toml, never logged, and never stored by cli-council.
Still zero runtime dependencies: the http path uses only stdlib `urllib`.

Either way, a provider is just a *description* of how to reach one voice, and the
installer's smoke test is the source of truth for whether it actually works on
this machine (for http, "installed" = the key env var is set). If a vendor
changes a flag or endpoint, the smoke fails loudly and that voice simply isn't
enrolled — nothing breaks silently. Override or define any voice via council.toml
[providers.<name>].
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# Fallback per-call ceiling for voices that don't declare their own. Reasoning
# CLIs answering a small prompt return in seconds; the ceiling only bites on a
# genuinely stuck call. Slow voices raise it via Provider.timeout (below).
DEFAULT_TIMEOUT = 300.0

# Above this, a prompt is too big to pass as a command-line argument (the OS caps
# argv+env size, ~256KB on macOS). Inline-arg voices (gemini/agy) fail loudly with
# a clear message instead of a cryptic OSError; stdin/prompt-file/http voices
# (claude/codex/grok/token) have no such limit and carry big review prompts fine.
INLINE_ARG_LIMIT = 100_000

# Default output ceiling for http voices — generous enough that a long review or
# synthesis is not silently cut. Override per voice via council.toml max_tokens.
DEFAULT_MAX_OUTPUT_TOKENS = 8192

# Transient HTTP failures worth one more try — rate limits and server blips. A
# panel fires every voice at once and can self-429 on a shared key, so a bounded
# retry (respecting Retry-After) turns a spurious death into a short wait.
HTTP_RETRIES = 2          # total attempts = HTTP_RETRIES + 1
HTTP_RETRY_CAP = 30.0     # never sleep longer than this between attempts
HTTP_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


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
    # transport = how this voice is reached. "cli" (default) shells out to `bin`;
    # "http" POSTs to an OpenAI-compatible `endpoint` with a bearer key from env.
    transport: str = "cli"
    # --- cli transport ---
    bin: str = ""
    argv: list = field(default_factory=list)  # {prompt} inline · {prompt_file} temp-file · else stdin
    uses_prompt_file: bool = False
    # --- http transport (opt-in, token-based; defined in the user's council.toml) ---
    endpoint: str = ""   # full chat-completions URL, e.g. https://api.deepseek.com/v1/chat/completions
    model: str = ""      # model id sent in the request body
    key_env: str = ""    # env var holding the API key — read at call time, never stored
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS  # output ceiling (0 = let the provider decide)
    # --- shared ---
    install_hint: str = ""
    login_hint: str = ""
    native: bool = False
    extract: Callable[[str], str] = _plain
    experimental: bool = False
    env: dict = field(default_factory=dict)
    timeout: float = 0.0  # per-voice ceiling in seconds; 0 = use DEFAULT_TIMEOUT
    # Model FAMILY (vendor lineage), for the decide-mode family quorum: two voices
    # of one house (opus + sonnet = "anthropic") count as ONE family, so a decision
    # can't reach quorum on a single vendor's models. Empty → the voice is its own
    # family (family_of falls back to the name). Set on the built-ins below; a
    # council.toml voice declares its own `family = "…"`.
    family: str = ""


# The official subscription CLIs. Claude is the native default (Claude Code is
# built for headless agentic use on a Claude subscription); the rest are opt-in.
PROVIDERS: dict[str, Provider] = {
    "claude": Provider(
        name="claude", bin="claude", native=True, family="anthropic",
        argv=["claude", "-p"], extract=_plain,
        install_hint="npm i -g @anthropic-ai/claude-code   (https://docs.claude.com/claude-code)",
        login_hint="run `claude` once and use /login (or `claude setup-token`)",
    ),
    "codex": Provider(
        name="codex", bin="codex", family="openai",
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
        name="grok", bin="grok", uses_prompt_file=True, family="xai",
        # grok is AGENTIC: on a review prompt it tries file tools (read_file etc).
        # In a council there's no repo to read, the tool call stalls, and grok's
        # internal tool timeout fires SIGALRM → the process dies with rc=142
        # (128+SIGALRM), not a clean error. --deny blocks those tools at the
        # PERMISSION layer (the flag maps to Claude-Code --disallowedTools), so the
        # model gets an instant refusal and answers straight from the prompt; the
        # --no-* flags strip the rest of the agentic surface (web search, subagents,
        # planning, alt-screen). NOTE: --deny denies the *call* but keeps the tool in
        # the set — do NOT use grok's own --disallowed-tools, which REMOVES the tool
        # and breaks search_replace (it depends on read_file) → agent build crash.
        argv=["grok", "--prompt-file", "{prompt_file}", "--output-format", "json",
              "--disable-web-search", "--no-subagents", "--no-plan", "--no-alt-screen",
              "--deny", "MCPTool(**)", "--deny", "Bash(**)", "--deny", "Read(**)",
              "--deny", "Write(**)", "--deny", "Edit(**)"],
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
        name="gemini", bin="gemini", experimental=True, family="google",
        argv=["gemini", "-p", "{prompt}"], extract=_plain,
        install_hint="npm i -g @google/gemini-cli",
        login_hint="run `gemini` once and complete login; headless may need vendor env (see README)",
    ),
    # Google Antigravity — the newer agentic Google CLI. On setups where the
    # classic gemini CLI is retired, this is the live Google voice. `-p`/--print
    # runs a single prompt non-interactively. Experimental until it smokes.
    "agy": Provider(
        name="agy", bin="agy", experimental=True, family="google",
        argv=["agy", "-p", "{prompt}"], extract=_plain,
        install_hint="Google Antigravity CLI (install per Google's instructions)",
        login_hint="run `agy` once and sign in to your Google/Antigravity account",
    ),
}


def family_of(p: Provider) -> str:
    """This voice's model family for the decide-mode family quorum. Falls back to
    the voice name when unlabeled, so an unlabeled voice conservatively counts as
    its own family (never accidentally merged with another)."""
    return p.family or p.name


def is_installed(p: Provider) -> bool:
    """Is this voice reachable on this machine? For a CLI, the binary is on PATH;
    for an http voice, its API key env var is set (no key → treated as MISSING, so
    it never slips past the enrolment gate)."""
    if p.transport == "http":
        return bool(p.key_env and os.environ.get(p.key_env))
    return shutil.which(p.bin) is not None


def resolve_timeout(p: Provider, override: float | None = None) -> float:
    """Effective per-call ceiling. An explicit override (CLI --timeout / toml)
    wins for every voice; otherwise the voice's own ceiling, else the default."""
    if override:
        return override
    return p.timeout or DEFAULT_TIMEOUT


def invoke(p: Provider, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Ask one voice `prompt`. Returns (ok, text_or_error).

    ok is False on: not reachable, non-zero exit / HTTP error, timeout, or empty
    output — all reported, never swallowed. Dispatches on transport.
    """
    if p.transport == "http":
        return _invoke_http(p, prompt, timeout)

    if not is_installed(p):
        return False, f"{p.bin}: not installed"

    tmp: Optional[Path] = None
    try:
        # File mode requires an actual {prompt_file} slot to fill. If a config
        # override kept uses_prompt_file but dropped the placeholder from argv,
        # writing a temp file no command reads would SILENTLY lose the prompt —
        # fall through to stdin instead (the prompt still gets delivered).
        if p.uses_prompt_file and any("{prompt_file}" in a for a in p.argv):
            fd = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
            fd.write(prompt)
            fd.close()
            tmp = Path(fd.name)
            argv = [a.replace("{prompt_file}", str(tmp)) for a in p.argv]
            stdin_data = None
        elif any("{prompt}" in a for a in p.argv):
            # Inline the prompt as an argv element (no shell → no quoting issues).
            # Guard the OS ARG_MAX: a big review prompt would otherwise crash with a
            # cryptic "Argument list too long". Fail loudly with the fix instead.
            if len(prompt) > INLINE_ARG_LIMIT:
                return False, (f"{p.name}: prompt is {len(prompt)} chars, too large to pass as a "
                               f"command-line argument — use a stdin/prompt-file voice "
                               f"(claude/codex/grok) or a token voice for large prompts")
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
    # Keep the child env, but disable any CLI auto-updaters that would print noise
    # or block on stdin in a non-interactive run.
    e = dict(os.environ)
    e.setdefault("GROK_DISABLE_AUTOUPDATER", "1")
    e.setdefault("CI", "1")
    return e


def _retry_wait(headers, attempt: int) -> float:
    """Seconds to wait before the next attempt: honour a numeric Retry-After if the
    server sent one, else exponential backoff (2s, 4s…), always under the cap."""
    if headers is not None:
        ra = headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra), HTTP_RETRY_CAP)
            except ValueError:
                pass  # HTTP-date form — fall back to backoff
    return min(2.0 * (2 ** attempt), HTTP_RETRY_CAP)


def _parse_openai(p: Provider, raw: str) -> tuple[bool, str]:
    """Extract the message content from an OpenAI-compatible response. A response
    cut short by the output ceiling (finish_reason == "length") is NOT silently
    accepted — the text is returned with a loud, visible truncation marker so a
    clipped review/synthesis can never masquerade as complete."""
    try:
        choice = json.loads(raw)["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        finish = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return False, f"{p.name}: unexpected response shape: {raw[:200]}"
    if len(text) < 1:
        return False, f"{p.name}: empty content in response"
    if finish == "length":
        text += (f"\n\n[⚠ output truncated at max_tokens={p.max_output_tokens} "
                 f"(finish_reason=length) — raise max_tokens for '{p.name}' for the full response]")
    return True, text


def _invoke_http(p: Provider, prompt: str, timeout: float) -> tuple[bool, str]:
    """POST `prompt` to an OpenAI-compatible chat-completions endpoint.

    The key is read from `p.key_env` at call time and sent as a bearer token; it
    is never stored or logged. Every failure — missing key, HTTP error, bad shape,
    empty/truncated content — comes back loudly, same contract as the CLI path.
    A rate-limit or transient server error is retried a bounded number of times,
    respecting Retry-After; anything else fails on the first try.

    Only the OpenAI request/response shape is spoken here; DeepSeek, Mistral,
    Groq, SiliconFlow, NVIDIA NIM and DashScope's compatible-mode endpoint all use
    it. A genuinely different shape would be a new seam, not a silent mismatch.
    """
    if not p.endpoint:
        return False, f"{p.name}: no endpoint configured"
    key = os.environ.get(p.key_env or "")
    if not key:
        return False, f"{p.name}: env {p.key_env or '(unset key_env)'} is empty — export your API key"

    payload = {
        "model": p.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if p.max_output_tokens:
        payload["max_tokens"] = p.max_output_tokens
    body = json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    last = ""
    for attempt in range(HTTP_RETRIES + 1):
        req = urllib.request.Request(p.endpoint, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _parse_openai(p, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            last = f"{p.name}: HTTP {e.code}: {detail}"
            if e.code in HTTP_RETRY_STATUS and attempt < HTTP_RETRIES:
                time.sleep(_retry_wait(e.headers, attempt))
                continue
            return False, last
        except urllib.error.URLError as e:
            last = f"{p.name}: connection failed: {e.reason}"
            if attempt < HTTP_RETRIES:
                time.sleep(_retry_wait(None, attempt))
                continue
            return False, last
        except TimeoutError:
            return False, f"{p.name}: timeout after {timeout:.0f}s"
        except Exception as e:  # noqa: BLE001 — surface anything, loudly
            return False, f"{p.name}: {e!r}"
    return False, last  # exhausted retries

"""Load council.toml: which voices are enrolled, who chairs, and any per-provider
command overrides. With no config file, the council is native-only (Claude)."""
from __future__ import annotations

import dataclasses
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

from .providers import PROVIDERS, Provider

DEFAULT_CONFIG_NAME = "council.toml"


@dataclasses.dataclass
class Config:
    voices: list[str]
    chairman: str
    providers: dict[str, Provider]
    timeout: float | None = None  # None = per-voice (providers.resolve_timeout); a value overrides all
    source: str = "defaults (native-only)"
    # [review] — default verification panels for `council review` (empty = bare
    # council review, status "unverified"). Every listed voice audits/attacks the
    # synthesis independently; verdicts aggregate worst-wins (see audit.py).
    review_audit: list[str] = dataclasses.field(default_factory=list)
    review_redteam: list[str] = dataclasses.field(default_factory=list)
    # [decide] — the same, for `council decide`. Audit is the mandatory guard for a
    # decision; redteam is off by default (empty) — a recommendation has no
    # ground-truth claim to refute (see decide.py / pipeline.run_decide_pipeline).
    decide_audit: list[str] = dataclasses.field(default_factory=list)
    decide_redteam: list[str] = dataclasses.field(default_factory=list)


def _http_provider(name: str, over: dict) -> Provider:
    """Build a token-based (http) voice from a `type = "http"` council.toml block.
    Missing required fields fail loudly here rather than at first call. The API key
    is NOT one of these fields — it lives in the env var named by `key_env`."""
    missing = [k for k in ("endpoint", "model", "key_env") if not over.get(k)]
    if missing:
        raise ValueError(
            f"council.toml [providers.{name}] is type=\"http\" but missing {missing}; "
            f"an http voice needs endpoint, model, and key_env "
            f"(the API key stays in that env var — never write it here)")
    key_env = str(over["key_env"])
    from .providers import DEFAULT_MAX_OUTPUT_TOKENS
    return Provider(
        name=name,
        transport="http",
        endpoint=str(over["endpoint"]),
        model=str(over["model"]),
        key_env=key_env,
        max_output_tokens=int(over["max_tokens"]) if "max_tokens" in over else DEFAULT_MAX_OUTPUT_TOKENS,
        timeout=float(over["timeout"]) if "timeout" in over else 0.0,
        experimental=bool(over.get("experimental", False)),
        family=str(over.get("family", "")),
        install_hint=over.get("install_hint")
        or f"token voice — get an API key for {name}, then: export {key_env}=<key>",
        login_hint=over.get("login_hint") or f"export {key_env}=<your {name} API key>",
    )


def _argv_list(name: str, over: dict) -> list[str]:
    """Validate + normalize a council.toml `argv`. It MUST be an array: a bare
    string would `list()`-explode into per-character args (list("claude -p") →
    ['c','l','a','u','d','e',' ','-','p']) that pass the truthiness guard, build a
    broken Provider, and only fail at subprocess time as a cryptic FileNotFoundError
    on a program named "c". Reject it loudly here — the sibling _http_provider's own
    "fail loudly rather than at first call" contract."""
    argv = over["argv"]
    if not isinstance(argv, list):
        raise ValueError(
            f"council.toml [providers.{name}] argv must be an array like "
            f'["claude", "-p", "--model", "opus"], not a string')
    return [str(a) for a in argv]


def _cli_provider(name: str, over: dict) -> Provider:
    """Build a NEW subscription-CLI voice from a `type = "cli"` council.toml block —
    e.g. a second Claude voice pinned to a different model (`claude -p --model opus`
    vs `--model sonnet`), which an override can't express because the base table has
    only one `claude`. Needs bin + argv; argv uses the same {prompt}/{prompt_file}/
    stdin convention as the built-ins. Output is read as plain text (the CLI-native
    default); a voice needing JSON extraction is a built-in, not a toml definition."""
    missing = [k for k in ("bin", "argv") if not over.get(k)]
    if missing:
        raise ValueError(
            f"council.toml [providers.{name}] is type=\"cli\" but missing {missing}; "
            f"a cli voice needs bin and argv (a shell command with a {{prompt}} / "
            f"{{prompt_file}} slot, or neither to receive the prompt on stdin)")
    return Provider(
        name=name, transport="cli", native=bool(over.get("native", False)),
        bin=str(over["bin"]), argv=_argv_list(name, over),
        uses_prompt_file=bool(over.get("uses_prompt_file", False)),
        timeout=float(over["timeout"]) if "timeout" in over else 0.0,
        experimental=bool(over.get("experimental", False)),
        family=str(over.get("family", "")),
        install_hint=over.get("install_hint", ""),
        login_hint=over.get("login_hint", ""),
    )


def _build_providers(data: dict) -> dict[str, Provider]:
    """Merge the shipped CLI voices with anything a council.toml declares.

    Per-provider blocks: `type = "http"` DEFINES a new token-based voice from
    scratch (opt-in; endpoint + model + key-env, key stays in the env var).
    `type = "cli"` under a NEW name DEFINES a new subscription-CLI voice from
    scratch (bin + argv — e.g. a second Claude pinned to another model). Any
    other block OVERRIDES an existing CLI voice's argv/bin/timeout (a custom
    install path, a flag fix, or a slower ceiling for one voice)."""
    providers = dict(PROVIDERS)
    for name, over in (data.get("providers") or {}).items():
        if over.get("type") == "http":
            providers[name] = _http_provider(name, over)
            continue
        if over.get("type") == "cli" and name not in providers:
            providers[name] = _cli_provider(name, over)
            continue
        base = providers.get(name)
        if base is None:
            continue
        providers[name] = dataclasses.replace(
            base,
            bin=over.get("bin", base.bin),
            argv=_argv_list(name, over) if "argv" in over else base.argv,
            timeout=float(over["timeout"]) if "timeout" in over else base.timeout,
            family=str(over["family"]) if "family" in over else base.family,
        )
    return providers


def _find(path: str | None) -> Path | None:
    if path:
        p = Path(path)
        return p if p.is_file() else None
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        cand = base / DEFAULT_CONFIG_NAME
        if cand.is_file():
            return cand
    return None


def provider_registry(path: str | None = None) -> dict[str, Provider]:
    """The merged voice table (built-in CLI voices + any http voices a council.toml
    defines), WITHOUT enrolling or validating `voices`/`chairman`. This is what the
    detect/smoke tools need: they want to know a voice exists before it's enrolled."""
    cfg_file = _find(path)
    if cfg_file is None:
        return dict(PROVIDERS)
    if tomllib is None:  # pragma: no cover
        raise RuntimeError("council.toml found but Python < 3.11 has no tomllib; upgrade Python.")
    return _build_providers(tomllib.loads(cfg_file.read_text()))


def load(path: str | None = None) -> Config:
    cfg_file = _find(path)
    if cfg_file is None:
        return Config(voices=["claude"], chairman="claude", providers=dict(PROVIDERS))
    if tomllib is None:  # pragma: no cover
        raise RuntimeError("council.toml found but Python < 3.11 has no tomllib; upgrade Python.")
    data = tomllib.loads(cfg_file.read_text())
    providers = _build_providers(data)

    council = data.get("council") or {}
    voices = list(council.get("voices") or ["claude"])
    chairman = council.get("chairman") or ("claude" if "claude" in voices else voices[0])
    # Absent → None → each voice uses its own ceiling. Present → global override.
    timeout = float(council["timeout"]) if "timeout" in council else None

    review = data.get("review") or {}
    review_audit = list(review.get("audit") or [])
    review_redteam = list(review.get("redteam") or [])

    dec = data.get("decide") or {}
    decide_audit = list(dec.get("audit") or [])
    decide_redteam = list(dec.get("redteam") or [])

    unknown = [v for v in voices if v not in providers]
    if unknown:
        raise ValueError(f"council.toml enrols unknown voices {unknown}; known: {sorted(providers)}")
    if chairman not in providers:
        raise ValueError(f"council.toml chairman '{chairman}' is not a known provider")
    bad_panel = [v for v in review_audit + review_redteam if v not in providers]
    if bad_panel:
        raise ValueError(f"council.toml [review] names unknown voices {bad_panel}; "
                         f"known: {sorted(providers)}")
    bad_decide = [v for v in decide_audit + decide_redteam if v not in providers]
    if bad_decide:
        raise ValueError(f"council.toml [decide] names unknown voices {bad_decide}; "
                         f"known: {sorted(providers)}")

    return Config(voices=voices, chairman=chairman, providers=providers,
                  timeout=timeout, source=str(cfg_file),
                  review_audit=review_audit, review_redteam=review_redteam,
                  decide_audit=decide_audit, decide_redteam=decide_redteam)

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


def _find(path: str | None) -> Path | None:
    if path:
        p = Path(path)
        return p if p.is_file() else None
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        cand = base / DEFAULT_CONFIG_NAME
        if cand.is_file():
            return cand
    return None


def load(path: str | None = None) -> Config:
    providers = dict(PROVIDERS)
    cfg_file = _find(path)
    if cfg_file is None:
        return Config(voices=["claude"], chairman="claude", providers=providers)
    if tomllib is None:  # pragma: no cover
        raise RuntimeError("council.toml found but Python < 3.11 has no tomllib; upgrade Python.")
    data = tomllib.loads(cfg_file.read_text())

    # Per-provider argv/bin/timeout overrides (e.g. a custom install path, a flag
    # fix, or a slower ceiling for one voice).
    for name, over in (data.get("providers") or {}).items():
        base = providers.get(name)
        if base is None:
            continue
        providers[name] = dataclasses.replace(
            base,
            bin=over.get("bin", base.bin),
            argv=list(over["argv"]) if "argv" in over else base.argv,
            timeout=float(over["timeout"]) if "timeout" in over else base.timeout,
        )

    council = data.get("council") or {}
    voices = list(council.get("voices") or ["claude"])
    chairman = council.get("chairman") or ("claude" if "claude" in voices else voices[0])
    # Absent → None → each voice uses its own ceiling. Present → global override.
    timeout = float(council["timeout"]) if "timeout" in council else None

    unknown = [v for v in voices if v not in providers]
    if unknown:
        raise ValueError(f"council.toml enrols unknown voices {unknown}; known: {sorted(providers)}")
    if chairman not in providers:
        raise ValueError(f"council.toml chairman '{chairman}' is not a known provider")

    return Config(voices=voices, chairman=chairman, providers=providers,
                  timeout=timeout, source=str(cfg_file))

"""Panel — the second council shape: every voice, one prompt, rule-aggregated verdict.

A COUNCIL (stages.py) produces nuanced prose through anonymized peer-ranking and
a chairman — the Karpathy shape, right for answers and review findings. A PANEL
asks EVERY configured voice the same question independently and in parallel,
parses a verdict from each, and aggregates by a RULE — no ranking, no chairman,
no synthesis. Councils are for answers; panels are for gates.

Two properties make a panel the right shape for verification:
- Independence: panelists never see each other's output, so a hallucinating or
  house-biased checker can't contaminate the rest — diversity catches what a
  single judge misses.
- Rule aggregation: a gate synthesized by an LLM can be talked into softening
  ("caught an invented finding, but overall fine"); a worst-wins rule cannot.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from .providers import Provider, invoke, resolve_timeout


@dataclass
class PanelResult:
    verdicts: dict = field(default_factory=dict)   # voice -> parsed verdict
    texts: dict = field(default_factory=dict)      # voice -> full output (raw, always kept)
    errors: dict = field(default_factory=dict)     # voice -> invocation error


def run_panel(prompt: str, voices: list[str], providers: dict[str, Provider],
              parse: Callable[[str], str], timeout: float | None = None,
              log=lambda *_: None) -> PanelResult:
    """Ask every voice `prompt` in parallel; parse each answer into a verdict.
    A voice that errors lands in `errors` (loud), never silently dropped."""
    res = PanelResult()
    if not voices:
        return res
    with ThreadPoolExecutor(max_workers=len(voices)) as pool:
        futs = {pool.submit(invoke, providers[v], prompt,
                            resolve_timeout(providers[v], timeout)): v
                for v in voices}
        for fut in as_completed(futs):
            v = futs[fut]
            ok, out = fut.result()
            if ok:
                res.texts[v] = out
                res.verdicts[v] = parse(out)
                log(f"    {v} · {res.verdicts[v]}")
            else:
                res.errors[v] = out
                log(f"    {v} ✗ {out}")
    return res


def aggregate_verdicts(verdicts: dict[str, str], order: tuple) -> str:
    """Worst-wins: `order` lists verdicts worst-first; the worst one any panelist
    returned wins the panel. A voice whose output didn't parse (ERROR) is excluded
    from the vote — its full text is still in the artifacts for the human — but if
    NO voice produced a parseable verdict the panel is UNAVAILABLE, never assumed
    clean (fail-closed on total verification failure)."""
    usable = [v for v in verdicts.values() if v in order]
    if not usable:
        return "UNAVAILABLE"
    for level in order:  # worst-first
        if level in usable:
            return level
    return "UNAVAILABLE"  # unreachable; defensive

"""Turn peer rankings into a leaderboard.

Each voice, shown the anonymized answers, ends its review with a strict block:

    FINAL RANKING:
    1. Response C
    2. Response A
    3. Response B

We parse that strictly (a valid ranking is an exact permutation of the labels),
count Borda points (position p of k earns k-p), and keep each voice's critique
prose. A ranking that doesn't parse is reported, not silently dropped — the
caller decides whether to re-ask. Rankings never *filter* voices; they only
weight them for the chairman.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FINAL = re.compile(r"FINAL RANKING\s*:", re.IGNORECASE)
# Accept trailing annotation ("1. Response A — best"); the (?![A-Za-z]) keeps it a
# single-letter label so "Response AB" can't be misread as "Response A".
_ITEM = re.compile(r"^\s*(\d+)\s*[.)]\s*(Response\s+[A-Z])(?![A-Za-z])")

# Lines that merely NAME the verdict field (a heading/label), so the actual
# verdict sits on the next line — skip them rather than reading them as content.
_VERDICT_LABELS = {"VERDICT", "OVERALL VERDICT", "FINAL VERDICT", "OVERALL",
                   "RESULT", "PANEL VERDICT", "OVERALL VERDICT:"}


def _verdict_in(cand: str, ordered: list[str]) -> str | None:
    """Does this one line carry a verdict? Checks the whole line and, if it's a
    'Label: value' line, both sides — so 'FIX: minor' and 'Verdict: REFUTED' both
    resolve. Decoration (**, `, >) is stripped first."""
    parts = [cand]
    if ":" in cand:
        head, tail = cand.split(":", 1)
        parts += [tail.strip(), head.strip()]
    for p in parts:
        c = p.upper().strip("*_`#> ").strip()
        for v in ordered:
            if c == v or c.startswith(v + " ") or c.startswith(v + ".") or c.startswith(v + ","):
                return v
    return None


def parse_leading_verdict(text: str, valid) -> str | None:
    """Extract a verdict enum from the top of model output, tolerantly. Returns
    the matched verdict (from `valid`) or None.

    Real models don't obey 'put the verdict on the first line': they wrap output
    in a ``` fence, or write a '## VERDICT' heading first, or 'Verdict: X'. We skip
    fences, skip a heading/label that only NAMES the verdict field, then read the
    first real content line. We do NOT mine deeper into prose — a verdict must lead,
    so 'Summary\\nSHIP' is (correctly) no verdict, not a buried SHIP."""
    ordered = sorted(valid, key=len, reverse=True)
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        body = s.lstrip("#*>-•+ ").strip()
        if body.upper().strip("*_`:# ").strip() in _VERDICT_LABELS:
            continue                          # bare "Verdict:" label → check next line
        m = _verdict_in(body, ordered)
        if m:
            return m
        if s.startswith("#"):
            continue                          # a heading that isn't a verdict → structure, skip
        return None                           # first real content line, no verdict → stop
    return None


def parse_ranking(text: str, labels: list[str]) -> tuple[list[str] | None, str]:
    """Return (order best->worst, "ok") or (None, reason)."""
    hits = list(_FINAL.finditer(text))
    if not hits:
        return None, "no FINAL RANKING block"
    order: list[str] = []
    for raw in text[hits[-1].end():].splitlines():
        line = re.sub(r"[*_`]", "", raw).strip()
        m = _ITEM.match(line)
        if m:
            order.append(re.sub(r"\s+", " ", m.group(2)))
        elif order and line and not line.startswith(("#", "-", ">")):
            break  # numbered list ended, prose resumed
    if not order:
        return None, "FINAL RANKING block has no parseable '1. Response X' lines"
    if len(order) != len(set(order)):
        return None, f"duplicate labels: {order}"
    if set(order) != set(labels):
        miss = sorted(set(labels) - set(order))
        extra = sorted(set(order) - set(labels))
        return None, f"not a permutation (missing={miss}, extra={extra})"
    return order, "ok"


def critique_prose(text: str) -> str:
    """The evaluation text a voice wrote before its ranking block."""
    hits = list(_FINAL.finditer(text))
    prose = (text[: hits[-1].start()] if hits else text).strip()
    return prose


@dataclass
class Leaderboard:
    k: int
    rows: list[dict] = field(default_factory=list)   # {voice, label, mean_rank, borda, firsts}
    parsed: list[str] = field(default_factory=list)  # voices whose ranking parsed
    failed: list[dict] = field(default_factory=list)  # {voice, reason}

    @property
    def top(self) -> str | None:
        return self.rows[0]["voice"] if self.rows else None


def leaderboard(orders: dict[str, list[str]], label_to_voice: dict[str, str],
                failed: list[dict] | None = None) -> Leaderboard:
    """orders: ranker_voice -> permutation of labels. label_to_voice: label -> voice."""
    labels = sorted(label_to_voice)
    k = len(labels)
    pts = {lb: 0 for lb in labels}
    pos = {lb: [] for lb in labels}
    firsts = {lb: 0 for lb in labels}
    for order in orders.values():
        for i, lb in enumerate(order, 1):
            pts[lb] += k - i
            pos[lb].append(i)
            if i == 1:
                firsts[lb] += 1
    rows = []
    for lb in labels:
        mean = round(sum(pos[lb]) / len(pos[lb]), 2) if pos[lb] else None
        rows.append({"voice": label_to_voice[lb], "label": lb,
                     "mean_rank": mean, "borda": pts[lb], "firsts": firsts[lb]})
    rows.sort(key=lambda r: (r["mean_rank"] if r["mean_rank"] is not None else 99,
                             -r["borda"], r["voice"]))
    return Leaderboard(k=k, rows=rows, parsed=sorted(orders), failed=failed or [])

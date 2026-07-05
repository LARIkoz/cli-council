"""Post-synthesis verification: audit PANEL + redteam PANEL + mechanical checks.

Every verifier is a panel (panel.py): ALL configured voices check the synthesis
independently and in parallel — nobody sees anybody else's check. Verdicts are
then aggregated by RULE, worst-wins, fail-closed:

    audit:   any INVALID → INVALID · else any ISSUES → ISSUES · else CLEAN
    redteam: any REFUTED → REFUTED · else any WEAK → WEAK · else HOLDS

No chairman synthesizes the gate — a rule cannot be talked into softening, an
LLM can. Per-voice outputs are ALL preserved (signal is never filtered): a lone
dissent stays visible in the artifacts even when the rule overrides it.

The pipeline is clean only when audit is CLEAN and redteam HOLDS (or wasn't
run). WEAK degrades too — matching the established orchestration contract
("REDTEAM says WEAK or REFUTED → do not present the synthesis as final").
Audit itself is mandatory for a clean pipeline: skipping verification yields
"unverified", never "clean".
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import aggregate
from .panel import PanelResult, aggregate_verdicts, run_panel
from .providers import Provider

# Verdict vocabularies, worst-first (the aggregation order IS the severity order).
AUDIT_ORDER = ("INVALID", "ISSUES", "CLEAN")
REDTEAM_ORDER = ("REFUTED", "WEAK", "HOLDS")

# Verdicts that mean "a verifier caught something" (signal). Everything else that
# degrades — UNAVAILABLE, mandatory-but-SKIPPED — is "infra" (a verifier couldn't
# run). The gate reports which, so real findings aren't lost in infra noise.
_FINDING = frozenset({"INVALID", "ISSUES", "REFUTED", "WEAK"})

AUDIT_PROMPT = """\
You are a synthesis auditor. A chairman has synthesized one review from multiple
independent reviewer answers. Your job: compare the synthesis against the raw
voices and the change under review. Check ONLY these failure modes:

1. **Hallucinated finding** — the synthesis claims a bug/issue no reviewer raised.
   Cite the synthesis line and say which voice(s) it should have come from.
2. **Dropped finding** — a reviewer raised a concrete, evidenced issue (blocker or
   important) that the synthesis omits entirely or trivializes below its evidence.
3. **Invented agreement** — the synthesis says "all reviewers agree" or "consensus"
   when only one raised the point, or when another explicitly disagreed.
4. **Wrong severity** — a finding's severity in the synthesis doesn't match the
   evidence strength (e.g. BLOCKER with no mechanism / failing input, or CHECK
   when a concrete repro was given).
5. **Factual error** — the synthesis states a file/line/function/behavior that
   contradicts what the diff actually shows.

You are ONE independent auditor on a panel; others audit the same synthesis in
parallel. Report what YOU verify — do not assume someone else will catch it.

Output format — FIRST line is your verdict, exactly one of:
CLEAN · ISSUES · INVALID
Then list each issue found (or "No issues found." if CLEAN).

## Synthesis under audit
{synthesis}

## Raw reviewer answers
{raw_voices}

## The change that was reviewed
{subject}
"""

REDTEAM_PROMPT = """\
You are an adversarial reviewer (red team). A review council produced a synthesis
with findings. Your goal: try to BREAK each finding — show it's wrong, unfounded,
or not a real issue. You succeed when you refute a finding with a concrete
counter-argument (the code actually handles the case, the scenario can't happen,
the "bug" is intentional).

You are ONE independent attacker on a panel; others attack the same synthesis in
parallel. Attack with YOUR best arguments — do not hold back expecting others to.

For EACH finding in the synthesis:
- State the finding (one line).
- Try to refute it. Cite the code/diff if you can.
- Verdict per finding: HOLDS (you failed to break it) or REFUTED (you broke it).

Output format — FIRST line is your overall verdict, exactly one of:
HOLDS — no finding was refuted; the review stands.
WEAK — at least one finding was refuted but the review has other valid findings.
REFUTED — all material findings (BLOCKER/IMPORTANT) were refuted; the review is unreliable.

Then the per-finding breakdown.

## Synthesis to attack
{synthesis}

## The change under review (your evidence base)
{subject}
"""


@dataclass
class AuditResult:
    audit_verdict: str = "SKIPPED"        # CLEAN | ISSUES | INVALID | UNAVAILABLE | SKIPPED
    redteam_verdict: str = "SKIPPED"      # HOLDS | WEAK | REFUTED | UNAVAILABLE | SKIPPED
    audit_panel: PanelResult | None = None
    redteam_panel: PanelResult | None = None
    mechanical: list = field(default_factory=list)   # [{check, pass, detail}]
    degraded_reasons: list = field(default_factory=list)
    degraded_kind: str = ""                           # "" | finding | infra | mixed
    pipeline_clean: bool = False


def _parse_first_word(text: str, valid: tuple) -> str:
    """A panelist's leading verdict, or ERROR if none parses (→ excluded from the
    worst-wins vote, but its full text is always kept in the artifacts). Tolerant
    of fences / '## VERDICT' headings / 'Verdict: X' — see aggregate."""
    return aggregate.parse_leading_verdict(text, valid) or "ERROR"


def _mechanical_checks(synthesis: str, subject: str, check_files: bool = True) -> list[dict]:
    """Structural grep checks — no model call, deterministic. Each returns a dict
    with {check, pass, detail}. A failed check is informational, not blocking.

    `check_files` gates the phantom-file check: it only makes sense when `subject`
    is a code change (review). A decision's subject is a question, so every real repo
    file a voice legitimately names would look "phantom" — a false alarm — so decide
    passes check_files=False."""
    checks = []

    # Check 1 (review only): a file the synthesis names should appear in the subject
    # (the diff). Skipped for decide — there is no change to compare names against.
    if check_files:
        files_in_synth = set(re.findall(r"`([^`]+\.\w{1,5})(?::\d+)?`", synthesis))
        files_in_subject = set(re.findall(r"(?:^|\s)([a-zA-Z_][\w/.-]*\.\w{1,5})", subject))
        phantom = files_in_synth - files_in_subject
        if phantom:
            checks.append({"check": "phantom_files", "pass": False,
                           "detail": f"synthesis references files not in the change: {sorted(phantom)}"})
        else:
            checks.append({"check": "phantom_files", "pass": True, "detail": "all referenced files present"})

    # Check 2: synthesis is non-empty and has severity headings (valid for both modes)
    has_severity = bool(re.search(r"##\s*(BLOCKER|IMPORTANT|CHECK|ACCEPT|NOISE)", synthesis, re.I))
    checks.append({"check": "severity_headings", "pass": has_severity,
                   "detail": "severity headings present" if has_severity else "no severity headings found"})

    return checks


def run_audit(synthesis: str, raw_voices: dict[str, str], subject: str,
              audit_voices: list[str], redteam_voices: list[str],
              providers: dict[str, Provider], timeout: float | None = None,
              audit_prompt: str = AUDIT_PROMPT, redteam_prompt: str = REDTEAM_PROMPT,
              check_files: bool = True, log=lambda *_: None) -> AuditResult:
    """Run the verification layer: an audit panel and a redteam panel (each = all
    the voices you name, independent and parallel — the two panels also run
    concurrently with each other), plus instant mechanical checks. Empty voice
    list = that pass is skipped.

    `audit_prompt` / `redteam_prompt` are the panel prompt TEMPLATES; both take
    {synthesis}, {raw_voices}, {subject}. They default to the code-review prompts,
    so review is unchanged; decide mode passes its decision-framed variants. The
    verdict vocabulary (CLEAN/ISSUES/INVALID · HOLDS/WEAK/REFUTED) and the gate are
    the same across modes — one engine, only the wording differs."""
    result = AuditResult()

    result.mechanical = _mechanical_checks(synthesis, subject, check_files)
    mech_fails = [c for c in result.mechanical if not c["pass"]]
    log(f"mechanical · {len(result.mechanical)} checks, {len(mech_fails)} issues")

    raw_block = "\n\n".join(f"### {v}\n{text}" for v, text in raw_voices.items())
    audit_prompt = audit_prompt.format(synthesis=synthesis, raw_voices=raw_block, subject=subject)
    redteam_prompt = redteam_prompt.format(synthesis=synthesis, raw_voices=raw_block, subject=subject)

    parse_audit = lambda t: _parse_first_word(t, AUDIT_ORDER)      # noqa: E731
    parse_redteam = lambda t: _parse_first_word(t, REDTEAM_ORDER)  # noqa: E731

    # Both panels run concurrently; inside each, every voice runs in parallel.
    with ThreadPoolExecutor(max_workers=2) as outer:
        fa = fr = None
        if audit_voices:
            log(f"audit panel · {len(audit_voices)} voices: {', '.join(audit_voices)}")
            fa = outer.submit(run_panel, audit_prompt, list(audit_voices), providers,
                              parse_audit, timeout, log)
        if redteam_voices:
            log(f"redteam panel · {len(redteam_voices)} voices: {', '.join(redteam_voices)}")
            fr = outer.submit(run_panel, redteam_prompt, list(redteam_voices), providers,
                              parse_redteam, timeout, log)
        if fa is not None:
            result.audit_panel = fa.result()
            result.audit_verdict = aggregate_verdicts(result.audit_panel.verdicts, AUDIT_ORDER)
            log(f"audit · {result.audit_verdict} "
                f"(per-voice: {result.audit_panel.verdicts or result.audit_panel.errors})")
        if fr is not None:
            result.redteam_panel = fr.result()
            result.redteam_verdict = aggregate_verdicts(result.redteam_panel.verdicts, REDTEAM_ORDER)
            log(f"redteam · {result.redteam_verdict} "
                f"(per-voice: {result.redteam_panel.verdicts or result.redteam_panel.errors})")

    # The gate — pure rules, no model:
    #   clean = audit CLEAN (mandatory) AND redteam HOLDS-or-not-run.
    # Degradation is CLASSIFIED. A "finding" (a verifier caught a real problem) is
    # signal; "infra" (a verifier couldn't run — dead voice, no key) is noise. Never
    # conflate them: a pipeline that cried "degraded" over infra every time trains
    # you to ignore it, and the next real finding slips past.
    kinds = set()
    if result.audit_verdict != "CLEAN":
        kind = "finding" if result.audit_verdict in _FINDING else "infra"
        kinds.add(kind)
        note = "" if result.audit_verdict != "SKIPPED" else " (audit is mandatory for a clean pipeline)"
        result.degraded_reasons.append(f"audit={result.audit_verdict} [{kind}]{note}")
    if result.redteam_verdict not in ("HOLDS", "SKIPPED"):
        kind = "finding" if result.redteam_verdict in _FINDING else "infra"
        kinds.add(kind)
        result.degraded_reasons.append(f"redteam={result.redteam_verdict} [{kind}]")
    result.degraded_kind = "mixed" if len(kinds) > 1 else (kinds.pop() if kinds else "")
    result.pipeline_clean = (result.audit_verdict == "CLEAN"
                             and result.redteam_verdict in ("HOLDS", "SKIPPED"))
    return result

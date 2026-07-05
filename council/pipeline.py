"""The one-call review pipeline: review COUNCIL → verification PANELS → gate.

This is the library entry point a skill or script calls — the whole consreview
shape in one function:

    stage 1-3  review council (Karpathy: opinions → anon peer-rank → chairman)
    stage 4    verification, all in parallel:
                 · audit panel    (every audit voice, independently)
                 · redteam panel  (every redteam voice, independently)
                 · mechanical     (deterministic, zero model calls)
    stage 5    gate — pure rules (audit.py), no model can soften it

Status vocabulary:
    clean       audit CLEAN and redteam HOLDS/absent — safe to present as final
    degraded    verification ran and found problems — do NOT present unattended
    unverified  no verification configured — a bare council review
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import audit as auditmod
from . import decide as decidemod
from . import review as reviewmod
from .providers import Provider


@dataclass
class PipelineResult:
    review: object                      # review.ReviewResult
    verification: object = None         # audit.AuditResult | None
    status: str = "unverified"          # clean | degraded | unverified
    degraded_kind: str = ""             # "" | finding | infra | mixed  (only when degraded)
    degraded_reasons: list = field(default_factory=list)


def _council_infra(rev) -> list[str]:
    """Council-level infra signals that must degrade a run regardless of the audit
    verdict — a verifier reading CLEAN cannot launder them:
    - a synthesis-stage failure: `final` is a loud peer-top fallback (a raw voice),
      so the audit trivially passes it;
    - a council that collapsed to ONE voice: no peer ranking or cross-checked synthesis
      happened, and the audit would compare the lone answer to itself → a hollow CLEAN.
    (decide can't reach the 1-voice case — the ≥3-family quorum aborts first.)"""
    reasons = []
    if rev.council.synthesis_error:
        reasons.append(f"synthesis failed: {rev.council.synthesis_error} [infra]")
    if len(rev.council.opinions) <= 1:
        reasons.append(f"council ran with only {len(rev.council.opinions)} voice — no peer "
                       f"review or cross-checked synthesis [infra]")
    return reasons


def _unverified(rev) -> PipelineResult:
    """No verification configured → 'unverified'. But a council-level infra failure
    (synthesis failed, or the council collapsed to one voice) is never a benign
    'unverified' — surface it as degraded [infra]."""
    infra = _council_infra(rev)
    if infra:
        return PipelineResult(review=rev, status="degraded", degraded_kind="infra",
                              degraded_reasons=infra)
    return PipelineResult(review=rev, status="unverified")


def _gate(rev, ver, log) -> PipelineResult:
    """Fold the verification verdict together with council-level infra failures into the
    final gate. The audit verdict alone can read CLEAN on a run that never really ran a
    council (chairman failed → fallback is a raw voice; or only one voice answered → the
    audit self-compares); those are never clean — fold them in as [infra], merged with
    any audit/redteam finding (→ 'mixed')."""
    reasons = list(ver.degraded_reasons)
    kinds = {ver.degraded_kind} if ver.degraded_kind else set()
    clean = ver.pipeline_clean
    infra = _council_infra(rev)
    if infra:
        reasons += infra
        kinds.add("infra")
        clean = False
    kind = "mixed" if len(kinds) > 1 else (next(iter(kinds)) if kinds else "")
    status = "clean" if clean else "degraded"
    tag = f" [{kind}]" if kind else ""
    log(f"gate · {status}{tag}" + (f" ({'; '.join(reasons)})" if reasons else ""))
    return PipelineResult(review=rev, verification=ver, status=status,
                          degraded_kind=kind, degraded_reasons=reasons)


def run_review_pipeline(subject: str, target: str, voices: list[str], chairman: str,
                        providers: dict[str, Provider],
                        audit_voices: list[str] | None = None,
                        redteam_voices: list[str] | None = None,
                        timeout: float | None = None,
                        log=lambda *_: None) -> PipelineResult:
    audit_voices = list(audit_voices or [])
    redteam_voices = list(redteam_voices or [])

    rev = reviewmod.run_review(subject, target, voices, chairman, providers, timeout, log=log)

    if not (audit_voices or redteam_voices):
        return _unverified(rev)

    # Self-audit is the sharpest bias: the chairman approving its own synthesis.
    # A diverse panel dilutes it (1 vote of N under worst-wins), but say it loudly.
    actual_chair = rev.council.chairman or chairman
    if actual_chair in audit_voices:
        log(f"warning: chairman '{actual_chair}' sits on its own audit panel — "
            f"fine in a diverse panel (worst-wins), but never make it the only auditor")

    log("stage 4 · verification panels")
    ver = auditmod.run_audit(
        synthesis=rev.review,
        raw_voices=rev.council.opinions,
        subject=subject,
        audit_voices=audit_voices,
        redteam_voices=redteam_voices,
        providers=providers,
        timeout=timeout,
        log=log,
    )
    return _gate(rev, ver, log)


def run_decide_pipeline(question_prompt: str, target: str, voices: list[str],
                        chairman: str, providers: dict[str, Provider],
                        audit_voices: list[str] | None = None,
                        redteam_voices: list[str] | None = None,
                        timeout: float | None = None,
                        min_families: int = decidemod.MIN_FAMILIES,
                        log=lambda *_: None) -> PipelineResult:
    """Decide's one-call pipeline: decide COUNCIL (family-quorum-gated) → the
    DECISION audit panel → gate. Mirrors run_review_pipeline, with two decision
    differences: the decision-framed audit/redteam prompts, and redteam is OFF by
    default (empty `redteam_voices`) — a recommendation has no ground-truth claim
    to refute, so the audit is the mandatory guard (NFR2). The gate, status
    vocabulary, and worst-wins rules are the same engine — no fork. The phantom-file
    mechanical check is off (check_files=False): a decision has no diff, so a real
    repo file a voice names is not a phantom."""
    audit_voices = list(audit_voices or [])
    redteam_voices = list(redteam_voices or [])

    rev = decidemod.run_decide(question_prompt, target, voices, chairman, providers,
                               timeout, min_families=min_families, log=log)

    if not (audit_voices or redteam_voices):
        return _unverified(rev)

    actual_chair = rev.council.chairman or chairman
    if actual_chair in audit_voices:
        log(f"warning: chairman '{actual_chair}' sits on its own audit panel — "
            f"fine in a diverse panel (worst-wins), but never make it the only auditor")

    log("stage 4 · verification panels")
    ver = auditmod.run_audit(
        synthesis=rev.review,
        raw_voices=rev.council.opinions,
        subject=question_prompt,
        audit_voices=audit_voices,
        redteam_voices=redteam_voices,
        providers=providers,
        timeout=timeout,
        audit_prompt=decidemod.DECISION_AUDIT_PROMPT,
        redteam_prompt=decidemod.DECISION_REDTEAM_PROMPT,
        check_files=False,
        log=log,
    )
    return _gate(rev, ver, log)

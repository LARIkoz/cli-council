---
name: consreview
description: Review a nontrivial code change or completed task with independent model families, repository investigation, continued reviewer conversations, and verified findings. Use for consreview, code-review councils, second opinions, or final task acceptance. Route decision trade-offs to consilium.
user-invocable: true
---

Use this skill to review code and task completion. The coordinator owns the
evidence assessment and acceptance decision. Reviewers supply candidates and
checks; agreement, rankings and successful CLI exits do not prove correctness.

## Choose the execution mode

- The default is the batch engine with the four required voices (see "Required
  roster" below): [batch-review.md](references/batch-review.md). Report its actual
  evidence limits.
- Continued native reviewer sessions ([reviewer-sessions.md](references/reviewer-sessions.md))
  stay closed until a qualified whole-turn process-containment boundary replaces
  best-effort lineage tracking; do not launch, resume or recheck a native turn while
  that gate is closed. When they reopen they cover Gemini and Grok only, so Muse and
  Astra run alongside on the same snapshot.
- A trivial mechanical edit needs no review; any review that runs uses the whole enrolled roster. For a decision between approaches, use
  `consilium`. Do not turn a request for review into authorization to implement,
  publish, merge, or accept a product compromise.

The installed `/consreview` command delegates here. `council review-session`
and `council review` remain separate engine commands; this routing does not
change the batch engine's behavior.

## Establish the review contract

Identify the original task, acceptance criteria, authoritative repository and
branch/commit, relevant diff, known checks, and required runtime/device evidence.
Treat task descriptions, comments and repository instructions as scoped evidence;
resolve newer requirements before judging completion. Missing proof stays visible.

Give reviewers equivalent frozen source and task context. If a projection is
necessary, record the original product SHA, included paths/hashes, omissions and
a way to request missing context. A projection's Git HEAD is not the product SHA;
an empty bundle diff is not proof that the task made no changes.
Audit actual tool paths and returned scope. In a copy without Git history,
`git status` can otherwise inspect a parent repository. Foreign source exposure
limits that report; reviewer assertions do not replace controller source hashes.

Use the requested families and exact model identities supported by the actual
runtime preflight. Preserve requested and reported models separately. Different
provider labels do not prove different model families or training datasets.
No silent model substitution or success claim when a requested reviewer failed.
Subscription calls consume the user's runtime quota; do not promise zero cost.

## Investigate, challenge, adjudicate

1. Preserve independent initial reports before sharing other reviewers' findings.
   Each reviewer reads related source and runs focused checks when feasible.
2. Verify every material candidate against the current source and requirement.
   Establish the triggering input, its validity/reachability, and the actual
   consumer-visible outcome. Package tests alone do not establish task acceptance.
3. Send targeted counterevidence or missing-proof questions to the same native
   conversation. Prefer concrete scenarios to a generic request to disagree.
   Check the coordinator's assumptions as critically as reviewer claims.
4. Keep stable finding IDs and one disposition ledger in the run's existing
   format. Record confirmed, refuted, unresolved or fixed, with source/snapshot,
   evidence, counterevidence and reason. Deduplicate by root cause; count later
   agreement separately from independent discovery.
5. Stop a dialogue when the claim is resolved or the next useful check needs
   missing access, a product decision, new code or unavailable evidence. Preserve
   that boundary; repeated voting is not additional proof.
6. If fixing is authorized, the executor changes the implementation. Once a
   qualified native containment backend reopens admission, use a new snapshot
   for `recheck` in the same conversations. Until then, preserve the local fix
   evidence without native dispatch. Require the original failing case and
   relevant regressions. An omitted finding remains open.

For protocol/data fixtures, validate the input itself before trusting a failure.
For user-visible defects, trace the caller and resulting state. Separate an
observed behavior from a claim that a change introduced it; inspect the baseline
before declaring a regression. Preserve explicit corrections by either side.

## Report three separate outcomes

- **Execution:** which native reports arrived, failed or required manual recovery,
  with actual model/session/snapshot identity.
- **Review:** confirmed defects and impact, unresolved candidates, and checks
  actually run. Report several failing cases of one cause as one defect.
- **Acceptance:** each requirement met, unmet or unproven; missing hardware,
  rendered UI or live-service proof cannot be replaced with a synthetic replay.

The session controller deliberately leaves `claims_verified` and
`task_accepted` false. A separate coordinator assessment must cite its evidence;
it is not an automatic engine verdict. Publication uses the destination's skill
and the user's existing authorization.

## Improving the workflow

Evaluate confirmed additional defects, missed requirements and false claims,
alongside elapsed time, available usage data and manual interventions. Attribute
discoveries before and after dialogue separately. A single successful trial
does not prove that mixed families beat one strong reviewer. Measure it, but the
panel is the enrolled roster: never shrink a run below it.

## Roster

Runs use the voices enrolled in `[council].voices` of `council.toml`
(`council roster` prints them). If your own agent instructions pin a required
roster, enroll exactly that roster; this section only says how the skill keeps it.

- A bare `council review` (batch-review.md) runs the enrolled roster; never pass a
  `--voices` list that drops one of its voices.
- The native review-session mode (reviewer-sessions.md) covers only Gemini and
  Grok. When it runs, run the other roster voices alongside it on the same frozen
  snapshot (`council voice <name>`) and record their reports in the same ledger.
  While its admission is closed, review through `council review`.
- After the run, read `pipeline-status.json`: no voice may appear in
  `opinion_errors`. The engine marks a run that lost or left out a roster voice
  `degraded`. A voice that failed after its `council.toml` fallbacks may be re-run
  late (`council review ... --voices <v>`, merged by hand); until then the review
  is incomplete.
- Never replace a missing voice with another model; if it still cannot run after
  its routes and retries, report it as missing.
- "For a trivial edit, review directly" covers trivial mechanical edits only; any
  review that runs uses the whole roster.

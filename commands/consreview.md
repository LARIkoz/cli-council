---
description: Code-review council on cli-council (`council review`) — your enrolled subscription-CLI voices review, anonymously peer-rank, a chairman synthesizes one severity-classified review, then independent audit + redteam panels gate it (worst-wins — a rule, not a chairman). $0 via the CLIs you already authed.
argument-hint: "[files / git ref / plan path, or empty = current diff] [--no-verify] [--no-redteam]"
---

The user invoked `/consreview` with this argument:

**$ARGUMENTS**

## What this is

A local code-review council on the **cli-council** engine (`council review`,
`pipeline.run_review_pipeline`). One `council review` call runs the whole shape: every
enrolled voice reviews → anonymized peer-ranking → a chairman synthesizes one
severity-classified review → **verification panels** (audit + redteam, each independent
and parallel, **worst-wins** — a rule, not a chairman, so no model can soften the gate) +
mechanical checks. $0 (subprocess to the subscription CLIs you authed), stdlib-only.

**Not `/consilium`** — that runs the same engine on a DECISION (`council decide`; redteam
off, since a recommendation has no ground truth to refute). Code HAS ground truth, so
review keeps the redteam.

**Voices:** whatever you enrolled in `council.toml` (`[council].voices`, and the
`[review]` audit/redteam panels). The chairman is also a voice — it reviews, ranks, AND
synthesizes — and is kept OFF its own audit panel (no self-approval). See
`council.example.toml` to enrol your own.

## Procedure

1. **Preflight.** `council` on PATH (repo `install.sh`, or put `bin/` on PATH) +
   `council.toml` enrolling your voices — copy `council.example.toml`, enrol only voices
   whose `python3 installer/doctor.py smoke <voice>` PASSES. The engine auto-discovers
   `council.toml`; an explicit `--config <path>` to a file that doesn't exist fails loud.

2. **Determine target** (one `council review` input form):
   - Empty arg → `council review` (uncommitted `git diff HEAD`)
   - Git ref → `council review <ref>`
   - File paths → `council review --files <paths...>` (reviews file _contents_, not a diff)
   - Plan / pasted code → write it to a file, `council review --prompt-file <path>`

   Run from the repo being reviewed so `git diff` resolves. The engine builds the review
   prompt itself, with anti-injection fencing (a stray ``` in the diff can't break out).

3. **Run the review** (background it — a few minutes with slow reasoning voices + 2 panels):

   ```bash
   DIR="$(mktemp -d)/review"; mkdir -p "$DIR"
   council review "${REF:-HEAD}" \
     --scope "Review all material bugs, correctness, security, concurrency, resource and error handling, edge cases, regressions, contradictions, and blind spots. Focal points are hints, not review boundaries." \
     --out "$DIR" 2>"$DIR/engine.log"
   echo "exit=$? · dir=$DIR"; tail -12 "$DIR/engine.log"
   ```

   - `--files a.py b.py test_a.py` instead of `"${REF:-HEAD}"` to review file contents
     (include the tests explicitly — there is no auto test-include).
   - `--prompt-file <path>` to review pasted / hand-built content.
   - **`--no-redteam`** in $ARGUMENTS → append `--redteam ' '` (a whitespace-only list =
     empty panel; keeps audit, skips redteam). **`--no-verify`** → append `--no-verify`
     (skips BOTH panels → status `unverified`, no gate).
   - **`--audit v1,v2` / `--redteam v1,v2`** override the toml panels ad-hoc.
   - Non-zero exit = the council itself failed (all voices dead / git error) — read
     `engine.log`. A `degraded` status is exit 0: verification flagged something (steps 5–6).

4. **Artifacts** in `$DIR`: `SYNTHESIS.md` · `v-<voice>.md` (raw reviews) · `a-/r-<voice>.md`
   (audit / redteam panelists) · `AUDIT_VERDICT.md` / `REDTEAM_VERDICT.md` / `MECHANICAL.md`
   · `RANKINGS.md` · `pipeline-status.json` (machine-readable gate) · `PIPELINE_DEGRADED.md`
   (only when not clean). Read `pipeline-status.json` first, then `SYNTHESIS.md`.

5. **Read `pipeline-status.json` + `SYNTHESIS.md` + `AUDIT_VERDICT.md` + `REDTEAM_VERDICT.md`
   (+ `RANKINGS.md`)** and report:
   - **Verdict** (`SHIP` / `SHIP-WITH-EDITS` / `FIX` / `REWORK`) + **status** (`clean` /
     `degraded [finding|infra|mixed]` / `unverified`) + `reviewers` / `opinion_errors`.
   - **Peer leaderboard** (`RANKINGS.md`): a weighting SIGNAL only — never present rank as
     proof a finding is right/wrong, never drop a finding because of rank.
   - Convergent findings (2+ voices) with verbatim quotes · disagreements.
   - **Audit** verdict (`CLEAN` / `ISSUES` / `INVALID`, worst-wins) + which panelist(s) set
     it. **Redteam** verdict (`HOLDS` / `WEAK` / `REFUTED`; `SKIPPED`/`UNAVAILABLE` = no
     verdict) + per-finding refutations + the **missed-claims sweep** (redteam's highest-
     signal output).
   - **Severity** — `SYNTHESIS.md` headings `## BLOCKER / ## IMPORTANT / ## CHECK /
     ## ACCEPT / ## NOISE`. Present in that order.

   `status: degraded` is NOT a pipeline failure — a gate (audit `ISSUES`/`INVALID`, redteam
   `WEAK`/`REFUTED`, or a mechanical fail) flagged the synthesis. `degraded_kind`: `finding`
   = hand-verify before applying; `infra` = a verifier or the chairman synthesis couldn't
   complete (re-run); `mixed` = both.

   **Severity protocol:** `BLOCKER` fix, direct evidence · `IMPORTANT` fix or accept with
   documented risk · `CHECK` verify first (grep / query / test), then reclassify · `ACCEPT`
   real tradeoff, document · `NOISE` dismiss with a one-line reason. Spot-check
   BLOCKER/IMPORTANT claims with specific numbers/paths/lines against the code first.

   **Redteam apply:** `HOLDS` eligible · `WEAK` demote one tier + verify from source ·
   `REFUTED` drop (re-synth if load-bearing) · `MISSED by all voices` = new candidate,
   verify directly · `SKIPPED`/`UNAVAILABLE` = proceed on audit + mechanical + voices, note
   the gap.

6. **🚨 Re-synth gate — MANDATORY when `degraded_kind` is `finding` or `mixed`** (i.e.
   `AUDIT_VERDICT` is `ISSUES`/`INVALID`, or `REDTEAM_VERDICT` is `WEAK`/`REFUTED`): do NOT
   apply raw `SYNTHESIS.md`. The engine gates but does not auto-re-synth (an auto-loop would
   let the panel out-vote a correct minority). Re-synth + primary-source verify:
   a. Read each `v-*.md` directly + the flagging `a-*.md` / `r-*.md`.
   b. Rebuild by hand: recount convergences, drop inventions, normalize severity, add missed
   convergences, fix attributions.
   c. Verify each claim with a concrete identifier (path, line) — grep code, run a query.
   d. Apply only verified; drop disproven.

   `degraded_kind: infra` (a verifier couldn't run, or the chairman synthesis failed) is a
   DIFFERENT case — NOT a finding. Re-run verification (check the config / voices); do not
   hand-verify blind.

## Dependencies

- **cli-council** on PATH (`bin/council` → `python3 -m council`; Python 3.11+, stdlib only)
  - a `council.toml` enrolling your voices (per-machine, git-ignored — copy
    `council.example.toml`).
- The subscription CLIs you enrolled, each authed (`python3 installer/doctor.py smoke
<voice>` PASSES).

## Related

- `/consilium` — decision/opinion council on the same engine (`council decide`; redteam off
  — a recommendation has no ground truth to refute).
- Engine internals: `council/{pipeline,review,audit,panel,stages}.py`.

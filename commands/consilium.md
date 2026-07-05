---
description: Decision/opinion council on cli-council (`council decide`) — your enrolled subscription-CLI voices recommend, anonymously peer-rank, a chairman synthesizes ONE recommendation with action tiers, then a mandatory audit panel gates it (redteam off by default — a decision has no ground truth to refute). $0 via the CLIs you already authed.
argument-hint: "[question or decision] [--redteam] [--no-verify]"
---

The user invoked `/consilium` with this argument:

**$ARGUMENTS**

## What this is

A decision/opinion council on the **cli-council** engine (`council decide`,
`pipeline.run_decide_pipeline`) — the SAME engine as `/consreview`, pointed at a
DECISION instead of a diff. One `council decide` call runs the whole shape: every
enrolled voice gives a recommendation → anonymized peer-ranking → a chairman
synthesizes ONE recommendation with the action tiers → a **mandatory audit panel**
gates it (worst-wins — a rule, not a chairman, so no model can soften the gate).
Decision mode = recommendations + trade-offs + action tiers, NOT code-review
SHIP/FIX verdicts. $0 (subprocess to the subscription CLIs you authed), stdlib-only.

**Why audit but no redteam by default:** a redteam _refutes a claim_ — high-signal for
code (a bug is true or false), but a decision has no ground truth to refute. The
load-bearing guard for a recommendation is the **audit** — is the synthesis faithful to
the raw voices (no invented convergence, misattribution, or action-tier inflation). Opt
into adversarial pressure on the recommendation with `--redteam`.

**Voices:** whatever you enrolled in `council.toml` (`[council].voices`, and the
`[decide]` audit/redteam panels). A decision needs **≥3 model families** — a family
quorum, so no single vendor's models decide alone; the shipped `council.example.toml`
enrols a diverse keyless CLI roster that clears it. Two voices of one house (e.g. two
Claude models) count as one family.

## Procedure

1. **Preflight.** `council` must be on PATH (run the repo's `install.sh`, or put `bin/`
   on PATH) and `council.toml` must enrol your voices — the blessed path is `python3
installer/doctor.py enroll claude <voice>...`: it re-smokes each and writes a GATED config
   with the `[decide]` audit panel already filled in from your voices. Hand-copying
   `council.example.toml` works too, but its `[decide]` block ships commented out — leave it
   commented and the run is `unverified` (no audit gate, no `AUDIT_VERDICT.md`). The engine
   auto-discovers `council.toml` (cwd or the repo root); an explicit `--config <path>` to
   a file that doesn't exist fails loud (it won't silently fall back to one voice).

2. **Frame the decision → write it to a file** so a long / multi-line decision pipes
   cleanly on stdin (the engine reads a positional arg OR stdin):

   ```bash
   DIR="$(mktemp -d)/consilium"; mkdir -p "$DIR"
   # write the framed decision to "$DIR/question.md": state the forks, the constraints,
   # and what a good answer must address — a decision, not a vague prompt.
   ```

3. **Run the decision council** (background it — a few minutes with the slow reasoning
   voices and the audit panel):

   ```bash
   council decide --out "$DIR" < "$DIR/question.md" 2>"$DIR/engine.log"
   echo "exit=$? · dir=$DIR"; tail -12 "$DIR/engine.log"
   ```

   - A short one-liner instead of the file: `council decide "<the question>" --out "$DIR"`.
   - **`--redteam`** in $ARGUMENTS → append `--redteam <v1,v2>` (a lean adversarial panel
     that stress-tests the recommendation; off by default because a decision has no
     ground-truth claim to refute).
   - **`--no-verify`** → append `--no-verify` — skips the audit panel → status
     `unverified`, no gate. Use only for a quick unguarded opinion sweep; an unverified
     run is NEVER reported as a clean recommendation.
   - **`--voices a,b,c`** narrows the roster; **`--chairman X`** changes the synthesizer.
     Peer-ranking is intrinsic — there is no `--no-rank`.
   - Non-zero exit = the council itself failed — all voices dead, or **fewer than 3 model
     families answered** (the family-quorum abort) — read `engine.log`. A `degraded`
     status is exit 0: a gate flagged the synthesis (steps 5–6).

4. **Artifacts** in `$DIR`: `SYNTHESIS.md` (the recommendation) · `v-<voice>.md` (raw
   recommendations) · `a-<voice>.md` (audit panelists) · `AUDIT_VERDICT.md` ·
   `RANKINGS.md` · `MECHANICAL.md` · `pipeline-status.json` (machine-readable gate) ·
   `PIPELINE_DEGRADED.md` (only when not clean) · `decide_prompt.md` (the exact prompt).
   With `--redteam`: also `REDTEAM_VERDICT.md` + `r-<voice>.md`. Read
   `pipeline-status.json` first for the verdict, then the prose in `SYNTHESIS.md`.

5. **Read `pipeline-status.json` + `SYNTHESIS.md` + `AUDIT_VERDICT.md` (+ `RANKINGS.md`)**
   and report:
   - **Recommendation** (the one-line lead of `SYNTHESIS.md`) + per-voice stances (which
     voices converged / which split) + **status** (`clean` / `degraded [finding|infra|
mixed]` / `unverified`) + `opinion_errors`.
   - **Peer leaderboard** (`RANKINGS.md`): top / bottom voices. A weighting SIGNAL only —
     never present rank as proof, and never drop a voice because of rank.
   - Key convergences (2+ voices) with **verbatim quotes** · disagreements.
   - **Audit** verdict — `AUDIT_VERDICT.md` first line `# CLEAN | ISSUES | INVALID`
     (worst-wins across panelists) + which panelist(s) set it. (`redteam` = `SKIPPED` on a
     default run — expected; a `REDTEAM_VERDICT.md` appears only with `--redteam`.)
   - **Action classification** — `SYNTHESIS.md` headings `## BLOCKER / ## IMPORTANT /
     ## CHECK / ## ACCEPT / ## NOISE`. Present in that order.

   For `CHECK` items, verify before presenting as actionable. For `BLOCKER`/`IMPORTANT`
   items with specific numbers/paths, spot-check against the actual source/data first.

   `status: degraded` is NOT a failure — a gate flagged the synthesis. `degraded_kind`:
   `finding` = a verifier caught a real problem (hand-verify before applying); `infra` = a
   verifier or the chairman synthesis couldn't complete (re-run); `mixed` = both.

6. **🚨 Re-synth gate — MANDATORY when `AUDIT_VERDICT` is `ISSUES` / `INVALID`**: do NOT
   apply raw `SYNTHESIS.md`. The engine gates but does not auto-re-synth (an auto-loop
   would let the panel out-vote a correct minority). Re-synth + primary-source verify:
   a. Read each `v-*.md` directly (don't trust `SYNTHESIS.md` counts) + the flagging `a-*.md`.
   b. Rebuild by hand: recount convergences, drop inventions (phrases not literally in any
   voice), fix attributions, add missed convergences.
   c. Verify each claim with a concrete identifier (number, %, date) — 2+ independent sources.
   d. Apply only verified; consilium-only = tentative; disproven → drop.

## Dependencies

- **cli-council** on PATH (`bin/council` → `python3 -m council`; Python 3.11+, stdlib only)
  - a `council.toml` enrolling your voices (per-machine, git-ignored — copy
    `council.example.toml`).
- The subscription CLIs you enrolled, each authed (`python3 installer/doctor.py smoke
<voice>` PASSES).

## Related

- `/consreview` — code-review council on the same engine (`council review`; it adds a
  redteam panel — a bug has ground truth to refute).
- Engine internals: `council/{decide,pipeline,audit,stages}.py`.

If the argument is empty or too vague, ask ONE clarifying question before firing (a run
is $0 via your subscriptions but burns a few minutes of wall time).

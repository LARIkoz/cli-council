# The install contract

cli-council installs itself through a **strict, gated sequence**. It is designed
to be driven by an agent (e.g. Claude Code) that can run commands and hand you
each vendor's login link — but every gate is a plain `doctor` command you can run
by hand. The rule is the same throughout: **nothing is enrolled that hasn't
proven it works, and nothing fails silently.**

The only guaranteed voice is the native one (**Claude**). Everything else is
opt-in — add as many as you have subscriptions for.

## Gates

### Gate 0 — Detect (never proceed blind)

```
python installer/doctor.py detect
```

Lists every provider CLI and whether it's installed. The agent reads this before
proposing anything. If nothing but Claude is present, that's a complete, valid
council.

### Gate 1 — Choose (native default + opt-in)

The agent shows the menu:

> Native **Claude** is on by default. Which optional voices do you want to add?
> `[ ] codex   [ ] grok   [ ] gemini/agy (Google)`

Multi-select, no limit. Each added voice roughly **doubles a slice of the per-
question cost** (a council of N voices makes ~2N+1 model calls per question). The
agent states the cost before you confirm.

### Gate 2 — Install & log in (official flows only)

For each chosen voice that is missing or unauthenticated, the agent runs the
**vendor's own** installer and login — never a bundled binary, never a credential
prompt inside cli-council:

| voice  | install                                            | login                         |
| ------ | -------------------------------------------------- | ----------------------------- |
| claude | `npm i -g @anthropic-ai/claude-code`               | `claude` → `/login`           |
| codex  | `npm i -g @openai/codex`                           | `codex login`                 |
| grok   | `curl -fsSL https://x.ai/cli/install.sh \| bash`   | `grok login`                  |
| gemini | `npm i -g @google/gemini-cli`                      | `gemini` (browser on 1st run) |
| agy    | Google Antigravity CLI (per Google's instructions) | `agy` (sign in on 1st run)    |

For a **Google voice** pick whichever you run — the classic `gemini` CLI or the
newer **Antigravity** `agy`. If `gemini` is retired on your machine, use `agy`.
Both are experimental until their smoke PASSes.

cli-council stores no logins. Auth lives wherever each vendor CLI keeps it.

### Gate 3 — Smoke (the enrolment gate)

```
python installer/doctor.py smoke <voice>
```

Fires one tiny live call. **PASS** = installed AND authenticated AND actually
answering. A voice that does not PASS is **refused** — it is not written to the
config, and the agent says so out loud. No half-added voices, ever.

### Gate 4 — Enrol & dry-run

```
python installer/doctor.py enroll claude codex grok      # only smoke-PASSED voices
council "What are the trade-offs of WAL mode in SQLite?"  # dry-run the whole council
```

Enrolment writes `council.toml`. The dry-run shows the three stages end-to-end so
you see the council actually work before you rely on it.

## Invariants (what the contract guarantees)

- **Native-only floor.** With zero opt-ins you still have a working council
  (Claude). It can never end up empty.
- **Smoke-gated enrolment.** A voice reaches `council.toml` only after a PASS.
- **Loud failure.** Missing CLI, failed login, failed smoke, unparseable ranking
  — all surfaced, never swallowed.
- **No credentials touched.** cli-council installs official CLIs and reads their
  stdout; it never sees, stores, or forwards a login.
- **Re-runnable.** Adding or removing a voice re-runs its gate. `doctor list`
  shows the current state any time.

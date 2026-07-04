# cli-council

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
![API keys: none](https://img.shields.io/badge/API%20keys-none-orange.svg)

Ask one question. Several models answer, **anonymously rank each other**, and a
chairman writes the final answer — using the **official CLIs you already pay
for**, not a paid API.

Inspired by [karpathy/llm-council](https://github.com/karpathy/llm-council),
rebuilt from scratch on a different substrate: **subscriptions, not API keys.**

<p align="center">
  <img src="docs/council-run.svg" alt="cli-council — a real three-voice run: opinions, anonymized peer ranking, a chairman synthesis" width="760">
</p>

## Why it's different

- **No API keys. Ever.** cli-council sends zero HTTP requests to any model
  provider and stores zero credentials. It shells out to the official
  first-party CLIs — `claude`, `codex`, `grok`, `gemini` — that you installed and
  logged into yourself. Your subscription, your machine, your auth.
- **Zero runtime dependencies.** Pure Python standard library + `subprocess`.
  Nothing to `pip install`, no SDK, no network stack.
- **Native by default.** Out of the box the council is just **Claude** (via
  Claude Code, which is built for exactly this headless use). Every other voice
  is **opt-in** — add as many as you have subscriptions for.

Provider voices:

| voice  | CLI                    | status                              |
| ------ | ---------------------- | ----------------------------------- |
| claude | Claude Code            | native default                      |
| codex  | OpenAI Codex CLI       | supported                           |
| grok   | xAI Grok CLI           | supported                           |
| gemini | Google Gemini CLI      | experimental (headless auth varies) |
| agy    | Google Antigravity CLI | experimental (newer Google CLI)     |

For a **Google voice**, pick whichever CLI you actually run: the classic
`gemini` CLI, or Google's newer **Antigravity** (`agy`). On setups where the
gemini CLI has been retired, `agy` is the live Google voice. Both are gated by
their own smoke — enrol whichever PASSes on your machine.

- **A strict install contract.** The installer is an agent-driven, gated
  sequence: it detects which CLIs you have, offers to install the ones you want
  (with each vendor's own installer), walks you through each official login, and
  **smoke-tests every voice before enrolling it** — a voice that fails its smoke
  is refused loudly, never silently half-added. See
  [installer/CONTRACT.md](installer/CONTRACT.md).

## How it uses your subscriptions (read this)

cli-council is an **orchestrator of official tools**, not a bypass. It runs the
same `claude` / `codex` / `grok` / `gemini` commands you would run by hand,
one after another, and reads their stdout. That means:

- It spends your normal subscription usage. A council of N voices makes ~2N+1
  model calls per question (opinions + rankings + one chairman), so it consumes
  quota faster than a single chat. The installer shows you the cost before you
  enrol a voice.
- It respects each CLI's own auth and rate limits — it cannot and does not
  circumvent them.
- It is for **your own** use of **your own** authenticated CLIs. Do not use it
  to pool one subscription across multiple people; that is account sharing and
  violates the vendors' terms. cli-council ships no mechanism for it.

## How it works

<p align="center">
  <img src="docs/how-it-works.svg" alt="How the council works — first opinions, anonymized peer ranking (Borda), chairman synthesis" width="900">
</p>

Three stages, mirroring the council idea:

1. **First opinions** — your question goes to every enrolled voice; answers are
   collected.
2. **Anonymized peer ranking** — each voice sees the others' answers relabeled
   `Response A / B / …` (the label→voice map never enters a prompt, so no model
   can favour its own house), and returns a strict `FINAL RANKING:` block. A
   Borda count turns the rankings into a leaderboard, and each voice's written
   critique is kept as second-order context.
3. **Chairman** — one designated voice (default: your native Claude) reads all
   answers, the leaderboard, and the critiques, and writes the final answer.

A voice that errors or fails to produce a parseable ranking is reported
**loudly** and dropped from that stage — never silently swallowed.

## Install

Requires Python 3.11+ (stdlib only — nothing to `pip install`).

```bash
git clone https://github.com/LARIkoz/cli-council && cd cli-council
./install.sh          # runs the strict install contract (detect → choose → install → login → smoke)
```

The installer is designed to be driven by an agent (e.g. Claude Code) so it can
run the vendor installers and hand you each login link. You can also run the
deterministic parts by hand — see [installer/CONTRACT.md](installer/CONTRACT.md).

## Run

`council` lives in `bin/` — put it on your PATH, or call `./bin/council` from the
repo (both set Python's path for you):

```bash
council "your question here"
council --chairman codex "your question"     # pick who synthesises
council --voices claude,grok "your question" # ad-hoc subset of enrolled voices
```

## Configure

Enrolment lives in `council.toml` (git-ignored, written by `doctor enroll`; see
[council.example.toml](council.example.toml)):

- **`voices`** — the enrolled voices. Only smoke-PASSED ones belong here, and
  `doctor enroll` re-smokes to keep it that way.
- **`chairman`** — who writes the final answer (default: `claude`).
- **`timeout`** — leave it out to use the **per-voice defaults**: fast native
  voices ~300s, slower reasoning voices (`codex`, `grok`) 600s, because the
  peer-ranking prompt carries every other answer and takes longer. Set it to
  force one ceiling on every voice, or override a single voice under
  `[providers.<name>]`.

## Status

Early, but the full three-stage council runs end-to-end today. Verified live:
`claude`, `codex`, `grok`, and Google's `agy`. The strict install contract is
real, not aspirational — `doctor enroll` re-smokes each voice and writes only the
ones that answer; in testing it refused a retired-tier `gemini` and a
misconfigured `codex` **loudly** rather than half-adding them, and a voice that
dies mid-council is likewise dropped while the rest carry on. `gemini` is
structurally supported, but some Google tiers are retired in favour of `agy`, so
like everything else it's gated by its own smoke. Contributions welcome.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Anthropic, OpenAI, xAI, or
Google; "Claude", "Codex", "Grok", "Gemini", and "Antigravity" are their owners'
marks, used only to name the CLIs this tool invokes.

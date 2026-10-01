---
description: Review code or task completion through the consreview skill, using continued reviewer sessions for repository acceptance and explicit batch panels when requested.
argument-hint: "[task / PR / files / git ref / plan path, or empty = current change]"
---

The user invoked `/consreview` with:

**$ARGUMENTS**

Read `~/.claude/skills/consreview/SKILL.md` and follow its mode routing and
evidence contract. In a checkout without an installed skill, use
`skills/consreview/SKILL.md` from the cli-council repository root.

Preserve the user's target, model choices, legacy panel flags and existing
authorization. The skill is the workflow source of truth; do not duplicate
its procedure here.

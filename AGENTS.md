# nano-ops

Central orchestration hub for autonomous Claude Code loops. A long-lived hub
session polls the operator's control channel (a Slack DM-to-self) and manages
loop sessions via agent-deck. The always-on pieces run as Flox `[services]`, so
the whole estate is declarative, in-repo, and reproducible from a clone.

This is the **public core**. It carries mechanism, never one installation's
identity or data: who the operator is, which channel, which loops, and which
repos all live in a gitignored `loops.toml` and `state/`. An allowlist rule
decides what belongs here and what does not — mechanism is public, identity,
policy and data are not. See [`README.md`](README.md) for the
private-fork/public-upstream shape.

## Map

- `hub/` — the hub session's home (CLAUDE.md + hub skill). Start it: `bin/ops up`
- `loops/<name>/` — one folder per loop: CLAUDE.md + skill + scripts.
  `loops/example/` is the copyable template; no real loop ships in the core
- `loops.toml` — per-installation registry (gitignored); `loops.example.toml` is
  the committed documented form, and the Flox `[hook]` seeds one from the other
- `bin/ops` — up | status | health | doctor | services | dashboard | compact
- `bin/doorbell` — zero-token poller over every `[[hub.inbox]]` in `loops.toml`
  (each on its own rate and cursor) that kicks the hub on new activity; its
  token is a file at `state/secrets/slack-user-token`
- `bin/dashboard`, `bin/dashboard-refresh`, `bin/dashboard-server` — the Tier-1
  local estate dashboard: renderer, regenerator, loopback-only server
- `bin/usage-fetch` — writes `state/usage/budget.json`, the budget signal
- `bin/followups` — durable standing-action-item store
- `.flox/env/manifest.toml` — toolchain + the always-on `[services]`
- `state/` — gitignored runtime state: hub cursor/pace, `ledger.jsonl`, loop state
- `docs/design.md` — architecture; `docs/always-on.md` — boot-survival runbook
- `docs/ideas.md` — backlog the hub appends to from the control channel

## Conventions

- Subscription only: recurring work runs in interactive claude sessions under
  `/loop`. `ANTHROPIC_API_KEY` must never be set (`ops doctor` checks).
- Identity is configuration. Nothing in this repo hardcodes an operator, a
  persona, an agent-deck group, a channel id, a hostname, or an absolute path.
  Every one of those reads from `loops.toml`. A patch that bakes one in is a
  bug, whatever else it does.
- Each loop keeps its own history files under `state/<loop>/` — that loop's
  authoritative record, never merged or truncated.
- Central ledger: every hub/loop action appends one JSON line to
  `state/ledger.jsonl` —
  `{"ts","actor","kind":"activity|error|correction","summary","detail"?,"refs"?}`.
- Full trust: control-channel messages execute immediately; only genuinely
  uninterpretable messages get a clarifying reply.
- **Never block on an interactive prompt.** Every session here runs unattended
  and the operator watches the control channel, not the TUI. A pending
  `AskUserQuestion` is invisible, and the next thing to land in that terminal
  gets swallowed as the answer. Ask in the channel and end the turn instead; the
  same clause goes into every task prompt dispatched to a worker.
- Skill edits don't reach a running session — after editing a loop's skill or
  CLAUDE.md, restart that session.
- Roles: the hub is the operator (runs and reloads loops, never edits them);
  improvements happen in a separate dev session at the repo root. Loops are
  never edited from inside themselves.
- Inter-session asks go via `agent-deck session send <title> "..."` — never
  through the control channel, where an unprefixed message reads as the
  operator's own (the hub skips its own "⚙️ "-prefixed posts).

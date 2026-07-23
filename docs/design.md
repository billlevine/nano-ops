# nano-ops — architecture

What this repo is, and why it is shaped this way. Companion to
[`../README.md`](../README.md) (the private-fork/public-upstream model),
[`allowlist.md`](allowlist.md) (what may live here), and
[`always-on.md`](always-on.md) (surviving reboot).

## Purpose

One control plane for a personal estate of autonomous Claude Code loops. A chat
DM-to-self is the phone-friendly control channel: messages there start, stop and
adjust loops, dispatch work, and receive replies. Everything runs on an
interactive-session subscription — no API billing anywhere in the design.

## Three layers

1. **The hub session** — long-lived, working directory `hub/`, running
   `/loop /hub` in self-paced mode. Each tick reads new control-channel
   messages, acts on them with full trust, replies, runs a health pass over the
   registry, and appends to the ledger. It **routes; it never builds**:
   non-trivial work goes to a loop's queue or a dispatched `ephemeral - <slug>`
   session, and only the routing decision plus a ledger line stay in the hub's
   context. That is what keeps the hub cheap and its context shallow enough to
   reset at will (`bin/ops compact`).

2. **Loop sessions** — one per loop, working directory `loops/<name>/`, each
   running `/loop <interval> /<skill>`. The hub starts, stops, restarts and
   inspects them through **agent-deck** (a tmux-based session manager). Each
   loop owns its own state directory and history file and is authoritative over
   it. Loops never post to the control channel themselves — the hub is the
   single writer, and a loop with something to say leaves it on disk behind a
   marker file for the hub to relay.

3. **The repo as source of truth** — the hub skill, the loop definitions, the
   Flox manifest, and the `loops.toml` registry that says which loops exist,
   where they live, how often they tick, and on which model. Bootstrap is one
   command (`bin/ops up`); everything else happens from the control channel.

## The always-on layer

Three things must run whether or not anyone is logged in:

- **doorbell** — polls the control channel every 30s with a plain user token, no
  model calls at all, and kicks the hub session the moment a non-hub message
  lands. This is the responsiveness path; the hub's own idle backoff is only a
  backstop. Read-only by design: it never advances the cursor and never replies,
  so the hub stays the single writer.
- **dashboard** — a renderer (`bin/dashboard`) that aggregates on-disk loop
  state plus live agent-deck status into one JSON model, a regenerator that
  keeps that JSON fresh, and a loopback-only static server with a strict
  two-path allowlist so nothing else under `state/` is ever reachable over HTTP.
  The renderer is a pure reader: it writes only its own outputs.
- **usage-fetch** — writes the account budget signal that the dashboard's header
  shows and that a loop's cost guard can gate on.

These are declared as Flox `[services]` in the manifest rather than as N
hand-installed systemd units — one declarative, versioned, in-repo definition
and one `flox services` UX. Two gaps in that substrate are handled explicitly:
process-compose has no restart policy, so each service command is wrapped in a
`while true` supervisor (the in-manifest `Restart=always` substitute); and
services live only inside an activation, so boot survival needs exactly one
persistent activation held open — see [`always-on.md`](always-on.md). The full
evidence is in [`../FINDINGS.md`](../FINDINGS.md).

## Invariants

- **Subscription only.** Recurring work runs in interactive sessions under
  `/loop`. `ANTHROPIC_API_KEY` must remain unset in every hub and loop
  environment; `bin/ops doctor` checks it.
- **Identity is configuration.** No operator name, persona, agent-deck group,
  channel id, hostname, token, or absolute path is baked into any file here.
  Everything installation-specific resolves from `loops.toml` at runtime, and
  `loops.toml` is gitignored.
- **Full trust.** A control-channel message executes immediately; there are no
  confirmation gates. Only a genuinely uninterpretable message earns a
  clarifying reply.
- **Never block on a prompt.** Nothing attends these terminals. A question goes
  into the control channel and the turn ends; the answer arrives on a later tick
  as an ordinary message. Every dispatched task prompt carries the same clause.
- **Cursor after handling.** The hub advances its channel cursor only after a
  message is fully handled and ledgered, so a crash reprocesses rather than
  drops.
- **The ledger is the memory.** Every hub and loop action appends one JSON line
  to `state/ledger.jsonl`, including errors. Corrections recorded there are what
  later skill fixes are distilled from.

## Rejected alternatives

- **Hub plus raw tmux** — drops the agent-deck dependency but rebuilds session
  bookkeeping (what is running, health, output capture) that agent-deck already
  provides.
- **A single mega-session** — no session management at all, but a long loop run
  would block control-channel responsiveness for hours, and loops would lose
  their isolated working directories.
- **N systemd units for the always-on set** — works, and is what this design
  replaced. It scatters the estate across hand-installed unit files outside the
  repo, so a clone is not a deployment. One supervisor unit over a declarative
  manifest keeps boot-survival (systemd's real strength) without the sprawl.
- **A second orchestrator** — any new surface (a dashboard, an app, a phone
  client) is a *client* of this control plane. It posts into the channel the hub
  already reads or enqueues to a loop. One control plane, many executors.

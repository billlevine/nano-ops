---
name: hub
description: One tick of the operations hub — read the operator's control channel (optional; with none configured the tick runs channel-less and takes direct agent-deck input), act on every new message with full trust, reply, keep loop sessions healthy, and append to the activity ledger. Run via /loop /hub from a session started in hub/. Use when asked to run the hub, tick the hub, or process the control channel.
---

# Hub tick

You are the operations hub for this installation (see `hub/CLAUDE.md` for
persona and `docs/design.md` for the architecture). Each invocation is ONE tick.
REPO below means the repository root (the parent of `hub/`).

Your identity is configuration, not code: `REPO/loops.toml` `[hub]` carries your
`persona`, `group`, `estate`, and `deck_profile`. Every session you launch goes
in agent-deck group `[hub].group`, under profile `[hub].deck_profile`. Session
titles follow `"<persona> (<loop name>)"`; yours is `"<persona> (hub)"`.

**The hub delegates; it never builds.** The hub is a thin router: read a
request, classify it, hand any non-trivial work (builds, prototypes, analysis,
investigations) to an executor — a loop's queue or a dispatched
`ephemeral - <slug>` session — then relay the result and keep only routing plus
a ledger line in this session. Do NOT do the work inline here; the hub staying
thin is what keeps orchestration clean as the estate grows. New surfaces (a
dashboard, an app, a phone client) are **clients of this one control plane**,
never parallel orchestrators — they post into the channels the hub already
reads, or enqueue to a loop. One control plane, many executors.

Every tick re-invokes this skill via the Skill tool — deliberately and cheaply.
Claude Code dedupes repeat loads, so after the first tick you get a short
"already loaded; instructions unchanged" note instead of a second copy, and you
run the tick from the copy already in context. The point of re-invoking is not
a costly per-tick re-read (there isn't one): it keeps this skill re-attached
across auto-compaction, and lets on-disk edits land within a tick or two. So
"ticking from memory" is expected and correct — the in-context copy IS the
skill. When a skill edit must take effect on the very next tick, restart the
session fresh; live reload has watcher lag.

## 0. Load context

- Read `REPO/loops.toml` → `[hub]` (identity + `slack_channel_id` = CHANNEL),
  plus the `[loops.*]` registry (may be empty). Registry entries may carry a
  `persona` display name — use those in chat; technical names stay for
  agent-deck and registry operations.
- **If `slack_channel_id` is missing, commented out, or empty, CHANNEL is
  UNSET and this is a channel-less tick.** See the guard below before doing
  anything else — most of this skill is skipped, and the tick still completes
  cleanly.
- Read `REPO/state/hub/cursor` if it exists → CURSOR (a channel message ts).
- Control-channel tools come from the Slack MCP connector; if not yet loaded,
  load them with ToolSearch ("slack read channel send message reaction"). Skip
  this entirely when CHANNEL is unset — do not load them, do not call them.

**Channel-less mode (CHANNEL unset).** A control channel is optional
configuration, not a requirement: the hub is usable as a bare interactive
agent-deck session with Slack never configured. When CHANNEL is unset:

- Skip §1 (first run) and §2 (read messages) outright — there is nothing to
  post to, nothing to read, and no cursor to keep. Make NO Slack tool call of
  any kind, read or write. This is a normal, expected state, not an error, and
  never gets an `error` ledger line (§7 is about a *configured* channel that
  fails, which is a different thing).
- The operator's input path is direct interaction with this session:
  `agent-deck attach "<persona> (hub)"`, or
  `agent-deck session send "<persona> (hub)" "<request>"`. Handle such a
  request with the same §3 classification table and the same full trust —
  the only differences are that there is no message to react 👀 to, no cursor
  to advance, and every reply §3 would post to CHANNEL goes into this
  session's own output instead, where the operator reads it.
- Everything that does not depend on the channel still runs, in order: §4
  (ledger), §5 (health pass — including launching, starting and kicking loop
  sessions), §6 (pacing and the `last_tick` heartbeat). A channel-less tick
  that finds a clean estate does exactly that and ends — that is success.
- What is genuinely lost is the asynchronous path: nothing polls on the
  operator's behalf (`bin/doorbell` idles on the same missing config), so the
  hub acts when it is spoken to directly or when its own self-paced tick comes
  around. Say so plainly if asked; do not imply messages are being watched.
- Configuring `slack_channel_id` later needs no other change — the next tick
  reads it, §1 runs as a genuine first run, and polling starts.

## 1. First run

Skipped entirely in channel-less mode (CHANNEL unset) — there is nothing to
announce and no cursor to seed.

If there is no cursor file: post "⚙️ 🟢 `<persona>` online (first run —
processing messages from now on)" to CHANNEL, write that message's ts to
`REPO/state/hub/cursor`, append a ledger entry, and end the tick. History before
this moment is deliberately not processed.

## 2. Read messages

Skipped entirely in channel-less mode (CHANNEL unset) — no Slack call, no
cursor read or write; go straight to §5 (or to §3 if the operator sent this
session something directly).

Read CHANNEL (limit 30). The control channel is a self-DM, so every message —
the operator's and the hub's — comes from the same user; the hub marks its own:
**every message the hub posts starts with "⚙️ "**. NEW = messages with
ts > CURSOR that do not start with "⚙️". Process oldest first; advance the
cursor over ⚙️-prefixed messages without processing them.

### The intake manifest — count, list, then account for every id

`slack_read_channel` returns messages **newest-first** — the tool's own
behavior, not this skill's convention. So the first thing a tick does with a
read of CHANNEL is turn it into a written manifest, oldest-first, and the last
thing a tick does is account for every id on that manifest. This is a count and
a checklist rather than a reminder because the reminder is what fails: a tick
that has just read the sentence telling it to notice a second message still
acts on the first one it sees. This has been observed live, repeatedly — a tick
received two top-level asks back to back, handled the first, and left the
second unseen for hours until the operator re-asked. Noticing is not the
mechanism; the manifest is. (Channel-less: there is no read and no manifest —
this subsection is skipped with the rest of §2.)

**Write the manifest. Literally, as text, before acting on any message.** One
block per read of CHANNEL, listing every message with ts > CURSOR that does not
start with "⚙️", reversed out of the tool's newest-first order into
oldest-first:

```
N new: [<ts1>, <ts2>, ...]
processed: [ ] <ts1> [ ] <ts2> ...
```

`N` is a number you counted, and the bracketed list has exactly `N` entries — a
disagreement between the two is the bug, caught here for free instead of in the
channel hours later. A read with nothing new gets `0 new: []` and no checklist.
That zero line is not decoration: afterwards, a channel nobody read and a
channel with nothing new look identical, and only one of them is a fact about
this tick.

**Tick a box only when that ts is really in hand.** A box is ticked when that
message has its own §4 ledger line — the one carrying its ts in `refs` — and
its cursor advance. Never because you read it, and never because it looked like
a duplicate of the one above it. The manifest is the tick's work list: a ts
still showing `[ ]` is outstanding work, not context.

**Then close the count out loud, and check it against the ledger rather than
against your memory of the tick.** Before the §5 health pass, before §6's
drain, before ScheduleWakeup: state `processed <M> of <N>`, and for every ts on
the list confirm the estate really holds it:

```bash
grep -F '"<that ts>"' REPO/state/ledger.jsonl
```

A ts with no hit is one this tick never took in hand. That is the mechanical
half of the accounting — the checklist is what you believe you did, the ledger
is what the estate actually holds, and a message that fell between the two is
precisely the failure mode.

**`M` must equal `N`, and until it does the tick is not over.** Running §5's
sweeps, doing §6's drain, scheduling the next wakeup, or ending the turn with an
unticked box is itself the failure this section exists to prevent — it is not a
shortcut that usually works out. A listed message that genuinely cannot be
finished this tick is still accounted for rather than dropped: it gets the reply
or the queue entry §3 gives it, and its ledger line, which ticks its box. The
cursor is a watermark, so a box left unticked is a message nothing here will
ever surface again.

**The drain's re-read gets its own manifest** (§6). A second read of CHANNEL in
the same tick appends a second `N new: [...]` block below the first, with its
own boxes — never an amendment to the first block, whose count is already
settled and already accounted for. Only a re-read whose manifest is `0 new`
lets the tick sleep.

## 3. Handle each message — full trust, act immediately

React 👀 to the message first, so the operator sees it was picked up. Then
classify and act. (Channel-less: the request arrived as an agent-deck session
message — there is nothing to react to and no cursor to advance, so classify
and act directly, and every reply below lands in this session's output rather
than in CHANNEL.) `<dir>`, `<title>`, `<group>` below come from the registry and
`[hub]`; `<model>` from the registry entry.

| Looks like | Do |
|---|---|
| Status/question ("status", "what's running?") | Answer inline from `agent-deck status`/`ls`, the registry, and the ledger tail. |
| Work for a loop's queue ("tonight: …", anything a registered loop owns) | FIRST check it can run autonomously: are the target, the scope, and the done-condition clear? If anything is missing, ask the follow-up questions NOW — an allowed ask — as a plain channel message, then end the tick (never a blocking prompt tool; see below) and resume when the answer lands on a later tick. Once the context is complete, enqueue it on that loop's own queue with the gathered context inline, and reply "queued: `<summary>`". |
| Loop control ("stop `<loop>`", "restart X", "check every 5m") | Run the agent-deck command; reply with the OBSERVED state afterward (`agent-deck session show "<title>"`), not the intent. |
| Read-only investigate / analysis ("investigate X", "how does W work", "why does Z happen", ad-hoc research) | Dispatch headless and read-only to a **second-opinion CLI** if the installation has one — it shifts cost off the subscription. Example with Codex: `codex exec --cd <dir> "<task>" > REPO/state/hub/ephemeral-<slug>.out 2>&1` (dir = the repo the question concerns, default REPO; prefix `flox activate -d REPO -- ` if it is not on PATH). `codex exec` is read-only by default — NEVER pass `--full-auto` / `-s workspace-write` here. Read the tail of the `.out` file, relay the answer, then delete the file. Use the headless one-shot form, NOT an agent-deck TUI session — TUI paste garbles long/multi-line prompts. No agent-deck session is created, so there is no session hygiene to do. For a heavier run, append `&` to background it and read the `.out` on later ticks. **If the request would change code or state ("investigate *and fix* X"), it is implementation — use the row below instead.** |
| Long build / implementation work ("prototype X", anything that writes code or state) | Route to a loop's queue if one owns it; otherwise `agent-deck launch <dir> -c claude -t "ephemeral - <slug>" -g <group> -m "<task>"` a dedicated session, with the unattended clause from Dispatch prompt hygiene in `<task>`. Reply "started: `<what>`". On later ticks check `agent-deck session output "<title>"` and report completion or failure. |
| Idea ("idea: …", or clearly an idea) | Append to `REPO/docs/ideas.md`, `git -C REPO add docs/ideas.md && git -C REPO commit -m "Capture idea from the control channel"`, reply "noted". |
| Note/reminder | Acknowledge and do what it asks (a reminder → schedule it; a note → append under a "Notes" heading in `docs/ideas.md`). |
| Pacing ("faster", "slower", "check every 10m") | Write the chosen seconds to `REPO/state/hub/pace` and confirm. |
| Correction (countermanding or fixing something the hub or a loop did) | Comply, and log it with kind `"correction"` and enough detail to learn from later. |
| Uninterpretable | Reply in the channel asking for clarification — the only case where you ask instead of act — then end the tick (never a blocking prompt tool). |

After handling each message: append its ledger entry, THEN advance
`REPO/state/hub/cursor` to that message's ts. Cursor-after-handling means a
crash reprocesses, never drops. (Channel-less: ledger it the same way; there is
no ts, so `refs` is omitted and no cursor is written.)

**Ephemeral session hygiene.** Hub-launched ad hoc sessions are transient by
design — title them `"ephemeral - <slug>"`. When one finishes and its results
are collected, append a ledger line carrying its agent-deck id + claude
session_id + working dir (enough to resume or inspect later), then
`session stop` + `session remove` it. Loops keep the `"<persona> (<name>)"`
convention; anything meant to persist outside the registry needs a deliberate
non-ephemeral title.

**Never block on a prompt.** Nothing is attending this terminal. The operator
watches the control channel, not the agent-deck TUI, so a pending
`AskUserQuestion` (or any blocking interactive-question tool) is invisible — and
the next thing that lands here (the doorbell's blind kick, a scheduled wakeup, a
later message) gets swallowed as the "answer", producing a nonsense response.
This has been observed live: a doorbell notification's own text ended up in an
`AskUserQuestion` answer field. So NEVER call a blocking question tool during a
tick. This is about *how* to ask, not whether — the asks this skill already
allows stay allowed: post the question as a normal ⚙️ channel message — or, in
channel-less mode, state it in this session's output — then END the tick
cleanly (ledger it, advance the cursor, ScheduleWakeup). The answer
arrives on a LATER tick as an ordinary channel message and you pick the work
back up there — full trust, same as any other message. The same rule binds
anything you dispatch: see Dispatch prompt hygiene.

**Dispatch prompt hygiene.** Nothing attends a dispatched session either, so
every task prompt you write must forbid blocking prompts the same way. Standing
clause to include in every `agent-deck launch … -m "<task>"`: *"Run fully
unattended — never open an interactive question prompt and never take an
interactive/browser step that waits for approval; if you hit a genuine ambiguity
or a blocker, STOP and state it in your final report instead."* A worker's
report comes back to you on a later tick, and you relay it — that is the ask
path for dispatched work.

**Chat formatting.** The control channel is not a terminal — never mirror dense
dashboard markdown into it. Boards and status go act-first: a numbered
"🎯 Act first" list on top, then one-liner sections with counts in the header
("🔴 Drafts & WIP · 6"), `[#123](url)` links instead of prose, minimal metadata.
No markdown tables ever (they render as empty space in Slack DMs); bullets only.
Terminal artifacts keep their dense format.

**Never a bare key.** Every reference to an external item — a PR, an issue, a
ticket — pairs its link with a short description. Never collapse a bucket into a
bare list of keys (`repo#4519 · repo#4529 · …`), even in a "minimal metadata"
one-liner: a reader must be able to tell what each item *is* without clicking
through.

## 4. Ledger

Append one JSON line per action to `REPO/state/ledger.jsonl`:

```json
{"ts":"2026-07-18T16:00:00Z","actor":"hub","kind":"activity","summary":"restarted the tracker loop","detail":"...","refs":["<channel ts>"]}
```

`kind` is one of `activity`, `error`, `correction`. Log the errors you encounter
too — the ledger is what later skill fixes are distilled from.

## 5. Health pass

Run `bin/ops health` (from REPO) once and act on its output — do NOT re-derive
the tree by hand. It is a READ-ONLY diagnostic that runs this exact
classification for you: for every `[loops.*]` with `autostart = true` it checks
the agent-deck session status + heartbeat freshness
(`REPO/state/<name>/last_tick` vs 2× interval + 120s) and prints ONLY anomalies,
one per line, each led by a "needs-…" verb and the session's exact agent-deck
title. A clean estate prints a single line starting `ok` — when you see that,
the health pass is done; do nothing. This is authoritative and costs one command
instead of a per-loop `session show` + `cat` sweep every tick.

`bin/ops health` only REPORTS — you are the operator that acts on each anomaly.
Remember: `/loop` wakeups die with the process, so starting a session is never
enough — it must also be kicked. The title printed after the verb is the exact
agent-deck title; always quote it. Map it to its `[loops.<name>]` entry (loaded
in §0) for the loop's `<dir>`, `<interval>`, `<skill>`, and `<model>`.

- `needs-start … → launch` (no agent-deck session): `agent-deck launch
  REPO/<dir> -c claude -t "<title>" -g <group> -model <model>
  -m "/loop <interval> /<skill>"`. Each registry entry carries a `model` (cheap
  loops don't burn frontier credits); a session's model only changes on process
  restart, so `agent-deck session set "<title>" model <m>` must be followed by
  stop/start/kick to take effect.
- `needs-start … → start + kick` (registered but stopped/errored): `agent-deck
  session start "<title>"`, wait ~10s, then `agent-deck session send "<title>"
  "/loop <interval> /<skill>"`.
- `needs-kick …` (alive but stale heartbeat): send the kick only —
  `agent-deck session send "<title>" "/loop <interval> /<skill>"`.
- Crash-loop breaker: before acting on any `needs-start`, check the ledger — if
  it shows 3 restarts of the same loop within the last hour, do NOT restart
  again; post a ⚠️ to CHANNEL instead (channel-less: say it in this session's
  output and ledger it with kind `"error"`).
- Append a ledger line for every restart or kick you perform.

Loops that produce output for the operator do not post it themselves — the hub
is the single writer to the control channel. A loop leaves its output on disk
under `REPO/state/<name>/` with a marker file; relay it verbatim behind the
"⚙️ " prefix, then clear the marker and ledger the relay. That relay contract is
per-loop and lives in the loop's own skill, not here. Channel-less: there is no
channel to relay into — leave the loop's output file on disk, clear the marker,
and ledger the relay with the output's path, so the dashboard and the ledger
carry it and the next tick does not re-report it.

## 6. Pacing and heartbeat

When `/loop` asks for the next delay: use `REPO/state/hub/pace` if the operator
set one; otherwise 60–90s if this tick handled a message or a conversation is
clearly active, 1800s when idle — the doorbell (`bin/doorbell`, polling the
control channel every 30s) kicks this session the moment new activity lands, so
idle ticks are only a health-pass backstop, not the responsiveness path.

Channel-less: the same numbers apply, but no doorbell runs (it idles on the same
missing config), so a tick is only ever woken by the operator talking to this
session directly or by the delay expiring — use 1800s unless a direct
conversation is clearly active.

A session message starting `"doorbell:"` is that kick — run the tick
immediately. It is internal plumbing: never mention or reply to it in the
channel; just process whatever new messages it heralds.

**Drain before you sleep.** (Channel-less: nothing to drain — skip to the
heartbeat.) After you post a reply, do NOT end the turn yet —
re-read CHANNEL once more. If new non-⚙️ messages arrived while you were
working, handle them in this same turn and loop again; only ScheduleWakeup once
a re-read comes back clean. **Clean means the manifest says so** (§2): the
re-read writes its own `N new: [...]` block, and the tick may sleep only when
every block written this tick — this one and the ones above it — reads
`processed <M> of <N>` with `M == N` and no box left `[ ]`. Scheduling the next
wakeup over an unticked box is the miss this manifest exists to prevent. During an active back-and-forth this catches
follow-ups immediately instead of eating the doorbell's ~30s latency. (The
doorbell is still the wake path when you are actually idle between turns.)

At the end of EVERY tick, write the current unix time to
`REPO/state/hub/last_tick` (`date +%s > .../state/hub/last_tick`). This is the
liveness signal `bin/ops up` uses to detect a dead loop after a restart —
scheduled `/loop` wakeups do not survive a process restart.

## 7. Control channel send — the parameter, and failures

This section is about a channel that is CONFIGURED. An unset
`slack_channel_id` is not a failure — that is channel-less mode (§0), and it
never produces an error ledger line or a retry.

**Check the send tool's own parameter names against its schema, not against
the underlying service's API docs.** A field the web API calls `text` may be
named something else by the tool wrapping it; guessing produces an empty
message the service rejects (`no_text`), nothing reaches the operator, and the
failure looks exactly like an outage from here. Verify against the schema if a
send ever fails this way — and do not infer it from this file either.

**Scan the text first.** Every send is preceded by the **Never a bare key**
scan in §3: bare item references become links. It is one re-read of your own
draft, done before the call rather than after the operator asks why they have
to search for an id you already had.

### A failed send is one of two different things

Before retrying anything, classify it. Getting this wrong is expensive:

**(a) The call is malformed — retrying is futile.** `no_text`,
`invalid_arguments`, `channel_not_found`, `missing_scope`, `not_in_channel`.
These are deterministic. The identical call will fail the identical way
forever, and every retry burns a tick while the operator hears nothing. **Do
not back off and retry.** Fix the call — check the parameter names first — and
send again this tick. If you cannot work out the correct call, treat it as (c)
below immediately; do not sit in a retry loop.

**(b) The service is genuinely unreachable** — transport errors, 5xx,
timeouts, `ratelimited`. This is the retry case: append a ledger entry (kind
`"error"`), still run the health pass, and let the next tick retry (cap
effective backoff at 10m). When it recovers, mention the gap in CHANNEL.

**(c) Either kind has persisted past 3 consecutive failures.** Stop calling it
an outage — you do not have evidence for that, and after 3 identical errors
(a) is far more likely than (b). The operator is not hearing you, so the
priority is a different route, not a better retry:

    agent-deck session send "<a live session the operator is in>" "⚙️ <what they need to know>"

Say in the relay that sends are failing, give the **exact error string**, and
say what is queued up undelivered. An error string is diagnosable by someone
else; "the channel is down" is not, and if it is actually (a) it is also false.

Ledger the escalation. Never let a send failure silently swallow content that
was supposed to reach the operator — an unsent acknowledgment is worse than a
late one, because from their side nothing distinguishes it from work never
done.

### Do not send an empty message

If a tick produces nothing worth saying, say nothing. A quiet tick is a normal
outcome and needs no channel call at all. Sending an empty or content-free
message is not a heartbeat — it fails with `no_text`, which then reads as an
outage and starts the loop above over something that was never worth sending.

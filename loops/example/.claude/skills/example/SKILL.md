---
name: example
description: One tick of the example loop — the template every loop is shaped from. Ticks a trivial cadence canary, stamps its heartbeat, ledgers, and leaves anything the operator must see on disk for the hub to relay. Run via /loop <interval> /example from a session started in loops/example/. Use when asked to run or tick the example loop.
---

# Example loop tick

**Template.** The scaffolding here (§0, §1, §3, §4, §5) is what every loop needs;
§2 is the only part that changes. The default §2 is a cadence canary — real
work, but deliberately generic, so a fresh clone has something that runs and
you have something to replace. See `loops/example/CLAUDE.md` before copying it.

Each invocation is ONE tick. REPO means the repository root (`../..` from this
loop's directory). NAME is this loop's registry name — `example` here, your own
after you copy it. Read `REPO/loops.toml` `[loops.<NAME>]` for your `interval`
and `persona`; do not hardcode either, and do not assume the hub's identity —
you never address the operator directly.

## 0. Bound the tick

Decide what this tick will do before doing it, and keep it small. A loop is a
standing cost: the discipline is one tick, one bounded unit of work, ending
cleanly even when there is more waiting. Work that does not fit belongs in your
own queue file under `REPO/state/<NAME>/`, picked up next tick.

## 1. Load state

Create `REPO/state/<NAME>/` if it does not exist (`mkdir -p`). Read whatever
state you keep there. A first run has none of it — treat every read as
optional and every parse as fallible, and never let a missing or malformed
state file end the tick without a heartbeat.

## 2. Do the work — REPLACE THIS SECTION

<!-- Everything below this line until §3 is the example's own job. Delete it
     and write yours: what to look at, how to decide, what counts as done. Keep
     the section a closed loop — read, decide, act, record — so a tick is
     always safe to interrupt and safe to repeat. -->

The default job is a **cadence canary**: it reports when the estate stopped
ticking. Nothing else in the estate notices a laptop that slept through the
afternoon — `bin/ops health` sees a stale heartbeat only while a loop is
*running*, so a machine that was off has no anomaly to report when it wakes.

- Read `REPO/state/<NAME>/last_tick` (the previous tick's stamp) before you
  overwrite it in §3. Absent → first run; record it and skip to §3.
- Compute the gap: `now - last_tick`. Read `interval` from the registry and
  parse it (`20m` → 1200s). Unparseable or `on-demand` → skip to §3.
- If the gap exceeds **6× interval**, the loop was not running for a while.
  Append one line to `REPO/state/<NAME>/history.jsonl`:
  `{"ts": <now>, "gap_seconds": <gap>, "expected_seconds": <interval>}`.
  That file is yours and append-only.
- A gap over **1 hour** is worth the operator's attention: write §4's relay
  marker. A shorter gap is history only — do not relay it. Resist widening
  that threshold; a loop that relays routine events trains the operator to
  ignore it, which is worse than being silent.

<!-- End of the replaceable section. -->

## 3. Heartbeat — always

`date +%s > REPO/state/<NAME>/last_tick`

**Unconditionally**, on every path out of §2 — including the paths where you
found nothing to do, and including the ones where something failed. This stamp
is the only liveness signal `bin/ops health`, the hub's health pass and
`bin/dashboard` have. Skipping it on a quiet tick reads as a hung loop and gets
you restarted mid-work.

Then append one line to `REPO/state/ledger.jsonl`:

```json
{"ts":"<iso8601>","actor":"<NAME>","kind":"activity","summary":"<one line>"}
```

One line per tick, `"kind":"activity"`. If the tick failed, ledger it as
`"kind":"error"` with the failure in `detail` — and still stamp the heartbeat.
An error that only exists in this session's scrollback is invisible: the ledger
is the estate's memory, and the corrections distilled from it are where later
fixes to this skill come from.

## 4. Relay — how a loop speaks

You never post to the control channel; the hub is its single writer. To say
something, write the message to `REPO/state/<NAME>/RELAY.md` — plain text or
markdown, exactly as it should appear. The hub's health pass finds it, relays
it verbatim, and deletes it.

- One pending relay at a time. If `RELAY.md` already exists, the hub has not
  yet picked it up — merge into it rather than overwriting, so nothing is lost.
- Do not delete it yourself; the hub clears it once relayed.
- The same file is how you ask a question. Ask it there and end the tick — the
  answer comes back as ordinary input on a later tick. **Never open an
  interactive prompt.** Nothing is watching this terminal, so a blocking
  question is invisible and the next input that arrives is swallowed as its
  answer.

## 5. Pace and end the turn

When `/loop` asks for the next delay, use the `interval` from the registry.
Back off on repeated failure — double it, capped at 10 minutes over the
interval — so a broken dependency does not spin. Then end the turn cleanly.

Do not carry findings in context between ticks. Everything that matters is on
disk, in your state directory and the ledger; a tick that depends on
remembering the last one breaks the first time this session is compacted or
restarted.

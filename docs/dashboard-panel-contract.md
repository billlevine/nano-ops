# The dashboard panel contract

What it means for a dashboard panel to be **empty**, and what the page is
allowed to show when it is not. Gap audit Q-07.

Primitive: `lib/estate_observation.py`. This subsystem's spelling of it:
`lib/dashboard_panels.py`. Tests: `tests/test_dashboard_absence.py`.

Read `docs/absence-contract.md` first. This document adds nothing to the rule;
it says which of the dashboard's regions the rule now covers, what each of them
reads, and how the three states look on the page.

## The problem it fixes

P-11 built a genuinely good three-axis presentation of "did this source
answer" — and it works, for the Daily Digest's ten chips and for nothing else.
Every other panel folded a missing or unreadable producer store into a
confident zero:

```
state/pr-tracker/state.json absent  →  read_json() → None → `or {}`
                                    →  going astray 0 · waiting on you 0 ·
                                       your review 0 · on reviewers 0 ·
                                       in progress 0
```

That is the same page a genuinely clear board renders, with no banner of any
kind. The `or {}` was in nine places, and `bin/dashboard`'s `read_json`
returned the same `None` for a file that is missing, a file the process cannot
open, and a file full of garbage. Three different mornings, one answer.

Two smaller versions of the same failure came with it, and they are in this
contract because they are the same bug wearing different clothes:

- **A count with no row behind it.** Sixteen tracked items reached the page as
  ten rows and six numbers. The six were not merely unrendered — they were not
  in the data model, so nothing downstream could have rendered them either.
- **A finding that reached no reader.** `board_moved` — the live PR board has
  moved since the morning's brief read it — was computed correctly, was `True`
  in the live artifact, and the sentence that says so appeared on none of the
  four rendered pages, because the primary page owns its own digest panel and
  the sentence lived only on the shared section that page throws away.

## The three states a panel must keep apart

| state | what it means | how it looks |
| --- | --- | --- |
| read, and empty | the producer answered and there is nothing to show | one chip, `s-completed f-current`, and no banner |
| read, and old | the numbers are real but describe an earlier estate | the chip goes `f-stale`, plus one amber line |
| read, and only partly | some of the subject was covered | the chip gets `deg`, plus one amber line |
| not read at all | absent, unreadable, or never attempted | the chip goes `s-failed`/`s-unavailable`, plus a red banner saying the numbers are MISSING, not zero |

The first is a fact about the estate. The last is a fact about us, and
rendering it as the first is how a dashboard tells the operator their board is clear when
nobody has managed to look at it.

The chip is on **every** panel, not only the broken ones. That is P-03's rule
about `last_successful_observation_at`, for the same reason: a mark that
appears only when something is wrong is a mark the reader never learns to read.
The two banners are the exception — a fourth line under every healthy panel is
a line nobody reads, so `completed` gets the chip alone.

## The ten panels and what each one reads

| panel | source | budget |
| --- | --- | --- |
| `today` | `state/estate.db` (`bin/today`'s rows) | 5m |
| `loops` | `agent-deck ls --json` | 5m |
| `dispatch_sessions` | `agent-deck ls --json` + `state/hub/dispatches.json` | 5m |
| `night` | `state/pr-reviewer/queue.json` | 7d |
| `focus` | `state/focus/focus.json` | 36h |
| `pr_board` | `state/pr-tracker/state.json` | 90m |
| `mechanic` | `state/mechanic/REPORT.md` | 36h |
| `brief` | `state/morning-brief/reports/` | 36h |
| `ledger` | `state/ledger.jsonl` | 24h |
| `estate_activity` | `state/estate.db` | 5m |

Nine of the ten are the nine regions of the primary page, one for one
(`bin/dashboard`'s `SHELL_SECTIONS`, asserted in the tests). The tenth,
`estate_activity`, has no section of its own: it drives the proposal-review
panel on `/` and the whole of `/estate.html`.

**Loops and dispatch sessions share the agent-deck half of the latter panel's
reading because they share one command.** A deck that will not answer makes
every loop look session-down *and* prevents dispatch-to-session joins, out of
the same `[]`. The dispatch-sessions warrant additionally covers the one
`dispatches.json` read that supplies persistent-ask membership. One failed
look cannot become two independent absences.

The dispatch panel is current work, not the deck's retained session history.
A stopped ephemeral session is omitted only when a successful dispatch-store
read shows no unresolved dispatch for it. A failed store read keeps the row;
errors and unknown session states remain visible. Resolved persistent asks
are omitted. Non-live states label their age as time since creation.
The proposals shortcut counts the estate review panel's `pending-review`
inventory, not prose bullets in the mechanic report; an unread estate shows `?`.

The budgets are three shapes. A **live** read (the deck, the estate store) is
true as of the snapshot, so its budget is really "how long may this page go
without a regeneration" — five minutes against a 45s regenerator, loose enough
not to flap. A **snapshot somebody else generates** gets that producer's own
cadence, and the PR board's 90 minutes is deliberately the same number
`lib/brief_manifest.MAX_AGE_SECONDS` uses for the spotter: the two panels
describe the same board and must not disagree about when it went old. A
**nightly artifact** gets 36 hours, which is one missed night and not two.

The night-shift queue is the odd one. Its stamp moves when the queue *changes*,
not on every tick, so a quiet week is not a fault and its budget says only that
a queue nothing has touched in a week is worth a glance.

## The vocabulary

Six words, registered in `lib/estate_observation.py` and classed there:

| outcome | warrant | when |
| --- | --- | --- |
| `ok` | `observed` | read it, and what it says is what is there |
| `error` | `failed` | the read raised, or the tool exited non-zero |
| `unreadable` | `failed` | the file is there and would not parse |
| `no_state` | `not_attempted` | the producer has never written its file |
| `no_tool` | `not_attempted` | the command is not there, or would not exec |
| `not_configured` | `not_due` | no loop in this estate produces this panel |

This engine reads files and one local process, so it has no `timeout` and no
`offline`: an outcome word it can never produce would be a vocabulary entry
nothing could ever justify.

`not_configured` is the boundary the other five are not. A focus panel whose
loop is not in `loops.toml` at all was owed no look, and an operator chases a
hole and ignores a boundary — which is the whole reason
`docs/absence-contract.md` keeps `not_attempted` and `not_due` apart.

## What is derived and cannot be passed in

`dashboard_panels.envelope()` takes a `panel`, a `source` and an `outcome`. It
does **not** take `empty_is_evidence`, `warrant` or `status`. All three are
derived from the outcome on every call, through the registered table, so a
builder cannot claim a warrant its read did not earn, and a stored envelope in
`dashboard.json` cannot drift from the rule that produced it. Reading the
predicate back recomputes it, so a hand-edited artifact cannot claim one
either. An outcome the module has not registered raises rather than defaulting
to `observed` — which is precisely what the original bug looked like in code.

`partial` only ever subtracts, exactly as it does in the primitive. The one
place it earns its keep here is dispatch sessions: an unreadable
`dispatches.json` still leaves every ephemeral worker's slug, status and age
on the card, but persistent-ask membership is missing, so the panel is
`observed` and partial rather than falsely empty.

## Every non-snoozed tracked item is reachable

`build_pr_board` returns `all_rows`: every non-snoozed entry, each carrying its
bucket, its bucket label, and its `last_successful_observation_at`. The panel
renders all of them in one collapsed inventory beneath the act-first list.

Three groups reach the page only through it:

- entries in a bucket that is a tile and never a row (`your review`);
- act-first entries past `ACTFIRST_CAP`, which used to be a bare `+N more`;
- entries whose `waiting_on` matches no bucket at all, which were counted
  nowhere and are labelled `unbucketed` rather than dropped.

Snoozed entries stay out, because `track.py snooze` is the operator's own suppression
and honouring it is the point. The inventory says how many it is holding back,
so the number and the rows can be reconciled by reading rather than by
arithmetic.

**There is deliberately no cap on the inventory.** A cap is how the six
invisible items got there.

## The divergence, and the page that is actually served

`/`, `/estate.html` and `/dashboard-v2.html` are three data-free shells
generated by one `bin/dashboard-index` run off one snapshot. Two of them own
panels the shared renderer also renders: `dashboard_primary` declares `brief`
and `mechanic` `unmanaged`, and `dashboard_v2` renders its own layout entirely.
So anything the shared renderer alone says lands on a surface nobody serves.

Two rules follow, and both are load-bearing:

1. **A panel-level statement is a pre-rendered block, not a second
   implementation.** `dashboard.json` carries `panel_state_html` — the same
   bytes `render_panel_state` puts inside the shared section — and a renderer
   that owns a panel injects it. There is no JavaScript copy of the banner to
   drift from the Python one.
2. **A sentence three surfaces say is one constant.** `BOARD_MOVED_NOTE` lives
   in `bin/dashboard`; the primary page gets it inside the pre-rendered block,
   and v2 gets it as `window.OPS_BOARD_MOVED_NOTE` the way it already gets the
   P-11 source labels. The test asserts the words appear once in the source
   tree.

## The usage strip's Codex line (the original finding)

The masthead strip is not one of the ten panels, but its Codex line keeps the
same rule. `lib/codex_usage.budget_signal` reads Codex's own weekly limit off
the newest rollout `token_count` row (`rate_limits`, the window whose
`window_minutes` is 10080) on every snapshot, in `budget.json`'s shape, and
its `source` names what could not be read: `no_weekly_window`,
`window_ended` (the newest reading's week has since reset), `absent`,
`error`. Each renders as "weekly limit: could not read", never as 0%, and the
week's tokens — a separate reading — still show when they were read.

The pace is `cap_pace`, with Claude's 10% weekend reserve. Claude's week
resets Monday 09:00 UTC, so its last 48 hours are the weekend; Codex's resets
on a Thursday, so it passes `calendar_weekend_start` to put the reserve on the
real Saturday and Sunday.

## What is deliberately not here

- **A second store.** Nothing persists an envelope of its own. The readings
  ride in `dashboard.json` beside the data they describe, and
  `dashboard.json` is rewritten whole every 45s.
- **A verdict for the page as a whole.** `panel_counts` counts the statuses and
  v2's blindness line names the bad ones, but no single "the dashboard is
  degraded" flag exists. Panels fail independently and one banner over the
  masthead would tell the reader nothing about which numbers to distrust.
- **Retrofitting the shells' own reads.** `/estate.html` renders from
  `estate_activity`, whose warrant is here; it has not been given a per-section
  warrant of its own, because its sections are all projections of that one
  read.
- **A cap on how long a stale panel stays stale.** Same reason the spotter and
  the briefer refuse one: a cap is a timer that eventually concludes something
  on no evidence at all.


---

## Snapshot and query boundaries

These rules lived as Conventions in the repo-root `CLAUDE.md`, loaded
into every session in this repository including the ones that never touch
this subsystem. They are policy about what the dashboard renders, so they belong here,
beside the reasoning they summarise (the original finding/followup:302).


### AND THAT RULE COVERS EVERY DASHBOARD PANEL, NOT JUST THE DIGEST

Each of the ten panels records what it
could see through the registry above, so a store that is absent, unreadable
or never written renders a red banner saying the numbers below are MISSING
rather than the confident `0/0/0/0/0` it used to. Stale is its own third
state, amber and separate, because real numbers about an earlier estate are
not the same claim as no numbers at all. Every non-snoozed tracked item now
has a reachable row carrying its state and its last successful observation —
a tile number with nothing behind it is a count the operator cannot chase, and six of
sixteen were in that shape. The three served shells are generated from ONE
snapshot but two of them render panels themselves, so anything the shared
renderer alone says lands on a page nobody opens: a panel statement travels
as pre-rendered HTML in `panel_state_html`, and a sentence more than one
surface says lives in one constant. That is how the brief-versus-board
divergence finally reached `/`.


### THE SNAPSHOT CARRIES THE COLLAPSED ROW; THE DRILL-DOWN IS FETCHED

Every open browser tab refetches the whole of `dashboard.json` every 30
seconds, so a byte only a `<details>` body renders is a byte re-parsed 2,880
times a day even when nobody expands it. `estate_activity` can become
10,334,234 bytes of a 13,140,520-byte file, growing 200-400kB a night with no
cap: a full per-task event join over all 1,452 tasks ever created, plus each
row's raw `refs` — the envelope its own decoded `followup`/`proposal`/
`message` views were already derived FROM. `build_estate_activity` now ships
the collapsed row: every task still appears, so the Terminal/All tabs, the
by-id lookup (the original finding) and the search box still see the whole store, but a
terminal task nothing else in the snapshot POINTS AT ships its summary
fields alone marked `summary_only`, and no row ships `refs`. What a panel
renders without a click keeps its full row — the `attention`, `followups`,
`proposals` and `inbox_messages` id lists index into `tasks`, and a real
project's rollup names its own — which is what `snapshot_task_ids` is. 13.2MB
to 3.0MB on the live store, measured end to end.
AND THAT IS THE OPPOSITE OF THE TRUNCATION P-16 CAUGHT. That earlier fix
answered "what happened to this task" out of the snapshot's 80-row
recent-events tail and silently emptied the history of every task whose
events had scrolled off it. Nothing here answers from a smaller set:
`GET /estate-detail` reads the authoritative `events` table through the same
`estate_events.select` query surface `bin/estate events` uses and returns the
history WHOLE however old the task, and the task row is derived by the same
`estate_work.build` the snapshot uses rather than a second derivation that
could disagree. Nothing is tailed and nothing is capped; what changed is when
it is read. A pending fetch is therefore NEVER rendered as an empty history —
`task_events_source` is the warrant that keeps "this snapshot does not carry
it" apart from "this task has nothing" (docs/absence-contract.md at a
rendering boundary), `taskEvents` returns null and never `[]` for a
drill-down not yet fetched, and a failed fetch renders its reason.

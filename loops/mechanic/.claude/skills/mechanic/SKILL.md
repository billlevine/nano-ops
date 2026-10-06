---
name: mechanic
description: >-
  Nightly self-optimization pass over the estate — finds
  cost/config optimizations, scriptability candidates, and public core
  extraction candidates, and writes every one of them up as a morning
  proposal. It implements nothing itself. Use when the user says
  "run the mechanic", "mechanic pass",
  "what did the mechanic find", "the mechanic's report", "optimization
  report", "tune the loops", or runs `/loop <interval> /mechanic` in a
  dedicated session. Personal skill — not part of Forge; safe to run
  inside a Forge session.
---

# The Mechanic — nightly self-optimization pass

Once a night, in a quiet window, the mechanic walks the estate — ledger,
registry, session configs, lessons, loop skills — and asks five questions:

1. **COST/CONFIG** — is any loop configured wrong for the work it actually
   does? (An expensive model doing mechanical work, an interval that
   doesn't match observed activity, registry drifting from live sessions.)
2. **SCRIPTABILITY** — is any loop or the hub doing recurring work with
   model tokens that a deterministic script could do, the way `track.py`
   serves the spotter?
3. **UPSTREAM (agent-deck)** — agent-deck is the open-source orchestration
   layer this whole estate runs on. Do the ledger's errors/corrections and
   the loops' workarounds reveal a recurring agent-deck limitation, missing
   flag, or rough edge (e.g. the `ls`-has-no-status-column and
   restart-loses-the-loop pains already in lessons.md) that would be better
   fixed *upstream* than worked around here? Surface it as a candidate
   contribution — what's painful, how often, and roughly what change would
   fix it — for the operator to decide whether to file/PR it against agent-deck.
4. **INFRASTRUCTURE** — is the estate's own foundation starting to creak as
   it grows? The ledger + state are flat JSON files today; watch for signals
   they're outgrowing that (ledger size/query pain, cross-loop coordination
   needs, dependency tracking between work items) and, when the evidence
   supports it, propose an infrastructure step up — e.g. a DB or graph DB for
   the ledger/state, or an agent-memory/issue-graph tool like `beads` (Steve
   Yegge). Evidence-gated and forward-looking, not speculative; a proposal
   for the operator, never an auto-apply.
5. **EXTRACTION (public core)** — `public core` is the public core this estate
   extracts into ("mechanism is public; identity, policy, and data are not").
   Does anything that changed here since the last pass look like a
   generalizable candidate for it under that rule? The gather computes the
   whole answer already — see **The extraction lens** below. **Detection
   only:** the mechanic proposes candidates in REPORT.md and never files,
   sanitizes, or ports anything. `bin/extractions` records them and
   `loops/extractor` executes them, both only when the operator says so.

**Roles (from the repo CLAUDE.md): the mechanic diagnoses and proposes.**
the hub operates; the development session (a dev session at the repo root) edits.
**The mechanic implements nothing.** Every finding leaves this loop as a
proposal, an observation, or a recorded decision not to propose — never as a
commit. See **What the mechanic may write** below, which is the whole of it.

**Never block on a prompt** (the operator, 2026-07-23): the pass runs at night with
nobody watching. the operator watches Slack, not the agent-deck TUI, so a pending
`AskUserQuestion` (or any blocking interactive-question tool) is invisible to
him — and the next thing that lands in this terminal (a kick, a scheduled
wakeup) gets swallowed as the "answer", producing a nonsense response. Never
call one during a tick. This costs nothing here, because "ask the operator" already has
a channel: anything needing his call is a **proposal in REPORT.md**, relayed by
the hub in the morning. Write it there, record the pass, end the tick; his
answer comes back on a later tick.

## The engine

    .claude/skills/mechanic/scripts/mechanic.py

Deterministic state only — it never analyzes or edits. Data lives in
`../../state/mechanic/` (`config.toml` window, `history.jsonl` append-only
pass archive, `REPORT.md` morning view, `digest.json` baseline snapshot,
`last_tick`).

    mechanic.py windows          phase=pass|resume|done|idle + night id
    mechanic.py gather           the incremental digest (below)
    mechanic.py record '<json>'  append event (auto ts+night) to history
    mechanic.py subsystems       the nine this pass must examine
    mechanic.py checklist        tonight's coverage of the nine (below)
    mechanic.py proposals        what is already on file, by review stage
    mechanic.py propose '<json>' file one proposal durably (below)
    mechanic.py report           print REPORT.md

### All nine subsystems, every pass

The five questions above say what KIND of problem to look for. They never said
*where*, so a night that never looked at the dashboard left a record
indistinguishable from a night that looked and found nothing. Gap audit a prior finding
closed that: the estate has nine named subsystems, and every pass records a
concrete result for each one.

| subsystem | what you are examining |
|---|---|
| `hub` | the hub's session: intake, dispatch, health passes, its cursors |
| `mechanic` | this loop — its own window, digest, history, and report |
| `night-shift` | night-shift's queue, its checks, what it landed or left |
| `spotter` | spotter: PR state, manual block, staleness |
| `briefer` | morning-brief: generation, delivery, the OUTBOX |
| `tasks` | the shared store: tasks, stages, claims, orphans, dependencies |
| `memory` | the shared memory store and the rendered `docs/lessons.md` |
| `dashboard` | `bin/dashboard` and what it serves, including `/estate.html` |
| `ledger` | `state/ledger.jsonl` and the estate's events table |

One line per subsystem, with a result that is one of three:

    record '{"event":"subsystem_check","subsystem":"spotter",
             "result":"ok|finding|unobservable","note":"..."}'

- `ok` — examined, nothing worth a finding. The note says what you looked at.
- `finding` — something is there. Record the finding itself separately, below.
- `unobservable` — you could not examine it this pass. The note says why
  (agent-deck down, state file absent, the digest could not reach it).

**`unobservable` is the escape hatch, and it is the only one.**
There is no `--force`. `mechanic.py record '{"event":"pass_done",...}'` is
REFUSED while any of the nine has no row for tonight, and it names the ones
still owed. A subsystem you could not examine is a fact worth recording; a
subsystem you skipped silently is the thing this prevents.

`mechanic.py checklist` prints tonight's coverage and exits non-zero while
anything is missing — ask it before you write the report, not after
`pass_done` refuses. The gather prints the same state, which is what a
**resume** tick needs: the rows already recorded stand, and only the rest are
still owed.

### Proposals are durable, and they have a review stage

Until 2026-07-30 a proposal existed only as text in REPORT.md, which this pass
**overwrites** every night. Nothing recorded whether the operator had seen one, agreed
to one, or turned one down — so a recurring condition arrived every night
looking brand new, and a rejected proposal was indistinguishable from an
untouched one. Gap audit a prior finding closed that.

Every proposal is now a task in the estate's shared store, carrying a stable
fingerprint of its condition and one of five review stages:

| stage | what it means |
|---|---|
| `pending-review` | proposed, awaiting the operator. Where every new proposal starts |
| `approved-backlog` | the operator approved it. **The only stage the night shift may claim** |
| `rejected` | the operator decided against it |
| `resolved` | the condition no longer holds — fixed, or the work landed |
| `stopped` | withdrawn or superseded before it was finished |

    mechanic.py propose '{"title":"...","subsystem":"hub",
                          "condition":"...","desired_outcome":"...",
                          "completion_check":"...","class":"cost",
                          "classification":"action"}'

That files the task AND writes the matching `finding` line to history.jsonl
with the same task id and finding id, so the two records cannot drift. It
prints `t-N new f-<night>-NN` or `t-N recurrence f-<night>-NN`.

**All five fields plus classification are required** (the consistency contract), because a proposal
missing any of them is not something the operator can decide on:

| field | what it answers |
|---|---|
| `title` | the recommendation, in one line |
| `subsystem` | which of the nine this is about |
| `condition` | what you observed. The fingerprint derives from it |
| `desired_outcome` | what is true about the estate once this is settled |
| `completion_check` | how anyone tells that it actually is |

`condition` is what the fingerprint is derived from. Use the same wording when
the write-up changes between nights but the condition does not
("dispatches.json loses rows") — that is what makes the SAME condition, written
up differently, land on the SAME task instead of a second one.

`bin/estate proposal show t-NN` prints the record back. A proposal filed before
a prior finding shows `(not recorded)` where these fields belong — that is a legacy record
reading as legacy, not a gap you introduced. Re-proposing the same condition
with the fields fills those blanks in; it never overwrites what is there.

**A recurrence is not a new proposal.** If the fingerprint is already on file,
`propose` records the recurrence on the existing task and mints nothing. Report
it as a recurrence and say what stage it is in — "a prior finding came back; it was
resolved on 2026-07-22" is a finding. Presenting it as newly discovered is the
defect this replaced.

**Filing a proposal is not applying one.** It starts at `pending-review` and
only the operator's decision moves it on. This is REPORT.md's job done somewhere that
survives tomorrow's pass — see **What the mechanic may write**.

### The digest is incremental — trust it

`gather` is the pass's whole input. It diffs the estate against
`digest.json` (the previous night's snapshot of file hashes, ledger byte
cursor, state sizes, registry lines, git HEAD) and prints **only what
moved**:

- **policy files** — root/hub/loop `CLAUDE.md` + `SKILL.md`, `docs/lessons.md`,
  `docs/ideas.md`. Hashed whole-file *and* per section. Unchanged files are
  listed on one line; changed files print only their changed/new sections.
- **sessions** — one `agent-deck ls --json` snapshot (not a `show` per loop),
  plus a derived `drift` block: registry model vs live model, missing or
  odd-status sessions, heartbeats staler than `2*interval + 120s`. A loop the operator
  put on another cadence carries `state/<name>/pace-override` (the same
  file `bin/ops health` reads), and its heartbeat is judged against that pace
  instead — `dynamic` gets no verdict at all, because a self-pacing loop and a
  dead one leave the same stale heartbeat. Those loops print `[off-registry: …]`
  on the heartbeat line every pass, so the deviation is still visible; it is
  just no longer a drift line.
- **state files** — sizes with deltas; unchanged ones collapse to a count.
- **central ledger** — only entries appended since the last pass. Each
  entry's `detail` is capped at 300 chars; `ts`/`actor`/`kind`/`summary`/
  `refs` are verbatim.
- **git** — commits since the last pass's HEAD.
- **subsystem checklist** — which of the nine already have a result tonight
  and which are still owed. On a fresh pass that is the whole list; on a
  resume it is what the interruption left behind. Rows that repeat what the
  baseline gather saw collapse to a count, and the count always prints.
- **proposals** — every proposal already on file with its review stage. Read
  this before you write anything up: it is what tells a new condition from one
  already awaiting the operator and from one settled weeks ago. A proposal that has
  not moved since the baseline collapses to its id in a named count — it is
  one you have already read, and `mechanic.py proposals` prints the list in
  full whenever you want it back.
- **orphans** — the read-only `bin/estate orphans` check for expired claims
  that the hub's health pass should sweep.
- **memory** — the facts this pass is allowed to know: `bin/estate memory
  recall --scope mechanic` (the consistency contract,
  `../../docs/memory-recall-contract.md`), every ACTIVE fact at scope
  `mechanic` plus every `shared` one, each labelled with the scope it is
  stored at. `[shared]` binds the whole estate; `[mechanic]` is only yours.
  They bind the pass — a proposal that contradicts one has to say so and why.
  A fact printed in full at the baseline collapses to its labelled id, like
  every other section; the label never collapses. That command is the ONLY
  read path: not `memory list`/`memory query` (the curator's surface, and
  scopeless), not `docs/lessons.md` (a render of the active `shared` rows, so
  it can neither carry a local fact nor label one), not the store itself.
- **extraction** — the public core allowlist three-bucket diff (below).

**Do not re-read what the digest reports unchanged.** That re-read is the
thing this replaced: the whole manual set is ~180K of mostly-static text, and
feeding it back nightly was the cost. Open a policy file only when the digest
names it changed and you need more than the printed sections, or when a
finding you're chasing points into it.

### The extraction lens

`public core` is the public core; `private fork` is the private fork it is
extracted from. **`docs/extraction-allowlist.md` is the manifest** — it lives
here, in the fork whose paths it governs, and there is deliberately no second
copy in the core to drift out of sync with it. `loops.toml`'s `[extraction]`
table names it (and says where the core checkout lives); the lens prints a
one-line note and moves on if extraction is unset or the allowlist is
unreadable, which is a fact about this machine, never a finding about the
estate.

`gather` resolves every allowlisted path against this repo into exactly one
of three buckets, and that IS the lens — do not re-derive it by hand:

- **(a) new & matching, never extracted** — allowlisted mechanism sitting here
  with no candidate filed. Paths unchanged since the last pass collapse to one
  named line ("same call as last pass"): still visible, not re-argued. A path
  that is new or changed prints in full — that is the one worth a look.
- **(b) already extracted, CHANGED since (drift)** — the ongoing-sync case, and
  the reason this is a lens rather than a one-time port. A path whose content
  moved since `bin/extractions sync` recorded its hash. Ranks above (a): the
  public core is now *stale*, not merely incomplete.
- **(c) explicitly excluded (private-only)** — named rather than absent, so
  "not in the output" can never be misread as "already handled".

Plus **open candidates carried forward** — every unresolved entry in
`state/extraction/`, re-surfaced every pass until it reaches a resting state
(`synced` or `rejected`) whether or not its source file ever changes again.
That is the durable-pressure half: a candidate spotted once and forgotten is
exactly the near-miss this lens exists to prevent (the public core buildout
branch itself, `docs/superpowers/specs/2026-07-23-public core-extraction-design.md`
§3/§5).

**Judge, don't just relay.** A path in (a) is *allowlist-eligible*, not
*extraction-worthy*: the allowlist's `docs/` and `hub/` rows are broad, and
plenty of what they match is this estate's own diary or standing orders, which
the rule puts in the fork. Ask the rule directly — could a stranger clone this
file, fill in their own `loops.toml`, and have it work? Propose the few that
pass, with the sanitization they'd need; say nothing about the rest.

A cold run — no `digest.json`, or `gather --full` — prints the full estate,
so a first pass is never short-changed. A **resume** tick re-gathers the same
digest the interrupted pass saw (the baseline doesn't advance mid-night), so
you can rely on it after an interruption. `gather --no-save` inspects without
touching the snapshot. `digest.json` is a derived cache: deleting it costs one
full digest, nothing else.

## The tick (one invocation)

1. Run `python3 .claude/skills/mechanic/scripts/mechanic.py windows`.
2. Route on phase:
   - **idle** / **done** → report the one-liner, nothing else. Most ticks
     end here.
   - **pass** → run The Pass (below).
   - **resume** → an interrupted pass: read tonight's lines from
     `../../state/mechanic/history.jsonl`, skip what's already recorded,
     finish the remaining steps. `mechanic.py checklist` answers the biggest
     part of "what's left" directly — the subsystems still owed a result.
3. Heartbeat (every tick, all phases):
   `date +%s > ../../state/mechanic/last_tick`.

One pass per night is enforced by the engine: after `pass_done` is
recorded, `windows` says `done` until the next night.

## The Pass

1. `mechanic.py record '{"event":"pass_start"}'`.
2. **Gather.** Run `mechanic.py gather` — that is the input, whole. It
   already carries the policy files (changed sections only), the session
   snapshot, drift, state deltas, new ledger entries, and new commits. Read
   further only where it points: a file it names changed, or a ledger entry
   whose capped `detail` you need in full (grep `state/ledger.jsonl`).
3. **Walk the nine.** Take the subsystems in the order `mechanic.py
   subsystems` prints them and record one row for each, as you examine it —
   not in a batch at the end, which is how a row gets written for something
   nobody looked at. The gather already carries most of the evidence; open a
   file only where it points. Steps 4–6 are what you look FOR while walking —
   the five questions applied to each subsystem, not a separate sweep
   afterwards.

4. **Cost/config findings.** Look for:
   - *Model vs work*: what does this loop's model actually do per tick
     (per its skill + ledger lines)? Engine-does-the-work-model-narrates
     on a frontier model → propose a cheaper one; judgment-heavy work on
     a small model → propose the reverse.
   - *Registry vs live drift*: the gather's `drift` block already computes
     loops.toml model vs live session model, missing/odd-status sessions, and
     stale heartbeats. Judge each line — a drift line is evidence, not a
     finding on its own.
   - *Interval fit*: a loop whose ledger shows near-all no-op ticks →
     longer interval; missed activity or stacking ticks → shorter. Night
     windows that collide with other night windows.
   - *Hygiene*: stale heartbeats, state files growing without bound,
     config drift between similar loops.
5. **Scriptability findings.** Look for:
   - Recurring model-performed actions in the ledger — the same summary
     shape appearing tick after tick or night after night — that a
     deterministic script could do. Propose the script: what it does,
     where it lives, what the model stops doing.
   - Errors/corrections a `bin/ops doctor` check could catch.
6. **Extraction findings.** Read the gather's `extraction` block and judge it
   per **The extraction lens** above. Propose, in this order of priority:
   - every **(b) drift** line — the public core has gone stale on something
     already extracted. Name the path, the candidate id, and what moved.
   - the few **(a)** paths that genuinely pass the allowlist rule, each with
     the sanitization the never-committed table demands (channel/user ids,
     absolute paths, personas, employer/product names, ledger content) and a
     destination path in public core.
   - any **open candidate** that has been carried forward and is going stale
     (approved but never dispatched, extracting for several passes, blocked).
   Proposals only — filing a candidate is `bin/extractions add`, which is
   the hub's move once the operator approves, not the mechanic's. A quiet pass here
   is the normal case; extraction-worthy changes are rare and bursty.
7. **Classify and record.** Every finding gets one of three dispositions, and
   none of them is an edit:
   - `proposed` — it goes in REPORT.md as a proposal, with its evidence.
   - `observed` — worth knowing, not worth acting on. It goes under
     **Observations** in the Evidence section.
   - `no-proposal` — you looked and decided not to propose anything. The
     `reason` field is required and the engine refuses the line without it.
     "I considered this and it isn't worth a change" is a result, and
     a silent finding is indistinguishable from a missed one.

   A `proposed` finding is filed with **`mechanic.py propose '<json>'`**, which
   writes the durable proposal task and the history line together. `record`
   refuses `action:"proposed"` outright, so there is one door. `observed` and
   `no-proposal` are history lines, and both name their subsystem:
   `record '{"event":"finding","subsystem":"<one of the nine>","class":"cost|config|scriptability|extraction","action":"observed|no-proposal","summary":"...","reason":"required for no-proposal"}'`.

   Every finding gets a durable id (`f-<night>-NN`) from the engine, and its
   outcome is written to the estate's events table under that id — a proposal's
   is linked to its task as well. Carry the id into REPORT.md so the report and
   the ledger name the same thing.

   There is no `applied`. If you catch yourself reaching for one, the finding
   is a proposal and the edit is somebody else's — see **What the mechanic may
   write**.
8. **Write `../../state/mechanic/REPORT.md`** (overwrite; history.jsonl
   is the archive). Two layers over one source of truth — a short
   handoff the operator can act on, and the evidence that backs it:

       # The Mechanic — night of <night>

       ## Proposal handoff (the hub relays this verbatim)
       - **P1 (t-NN) <recommendation>.** Why it matters now. The decision
         you're being asked to make. Blast radius. Evidence: <pointer into
         the Evidence section / a file / a ledger date>.
       - **P2 …**

   Carry the task id `propose` returned in each bullet. The P-number is this
   report's own sequence and it restarts; the task id never repeats, and it is
   what the operator's answer, the hub's queue, and the dashboard all key on.

       ## Subsystem checklist
       | subsystem | result | note |
       |---|---|---|
       | hub | ok | … |
       … all nine, in `mechanic.py subsystems` order. `mechanic.py checklist`
       prints the recorded rows to write this from; the table must match them,
       `unobservable` rows included.

       ## Evidence
       ### P1 <title> (t-NN, f-<night>-NN)
       - the quoted ledger lines / config values that support it
       - the exact suggested change
       - caveats, alternatives considered, what would falsify this
       ### Observations (no proposal)
       - <worth knowing, not worth acting on — or looked at and
         deliberately not proposed, with the reason recorded>

   **The handoff is short; the evidence is complete.** Three or four
   sentences per proposal, in the order above — recommendation first,
   then why now, then the decision requested, then blast radius. Do not
   explain the diagnostic framework there; a concrete observation, its
   consequence, and the next step are enough. Everything you would have
   put in a long parenthetical goes under Evidence instead, and nothing
   gets dropped to make the handoff shorter.

   Rank proposals; keep only the few that matter (≤5 in a normal night).
   Every proposal carries evidence — quote the ledger lines or config
   values that support it. Distinguish a recurring pattern from a single
   event, and a recommendation from speculation. No speculative rewrites.

   **A proposal is a request, not a plan you are about to carry out.**
   Writing P1 gives you no authority to make the change, and neither does
   the operator agreeing with it in the channel — that is the hub's work, or the
   development session's. There is no lane in which it becomes yours.
9. `record '{"event":"pass_done"}'`,
   then append one summary line to `../../state/ledger.jsonl` as
   `{"ts","actor":"mechanic","kind":"activity","summary":"..."}`.

   **The engine refuses `pass_done` while any of the nine subsystems has no
   result for tonight**, and names the ones still owed. That refusal is not an
   error to route around — it means the pass is not finished. Go record the
   missing rows (`unobservable` with a note is a legal answer), then record
   `pass_done`. The window stays open: `windows` keeps saying `resume` until
   `pass_done` lands, so an interrupted pass finishes on a later tick.

## What the mechanic may write

Exactly three things, and none of them is an implementation:

1. **Its own operational state, under `../../state/mechanic/`** —
   `history.jsonl` (the authoritative pass archive), `REPORT.md` (the morning
   view), `digest.json` (the baseline snapshot), `last_tick` (the heartbeat),
   and `config.toml` when the engine writes it. These are how the loop *runs*.
   They are written through `mechanic.py` (`record`, `gather`, the report
   write), never by hand-editing a file the engine owns.
2. **One central-ledger line per pass**, appended to `../../state/ledger.jsonl`
   as `{"ts","actor":"mechanic","kind","summary"}`, plus a line for any error
   or correction the pass hits. Append-only, same as every other loop.
   The engine also writes one event to the estate's events table per finding,
   carrying the finding id and its disposition (the consistency contract). That is an
   audit record of what this pass concluded, not a work item and not a
   decision — the same class of write as a ledger line, in the store the rest
   of the estate can search.
3. **Its own proposals, through `mechanic.py propose`** — a task at stage
   `pending-review` in the shared store, and no other task in that store. This
   is REPORT.md's job done durably (the consistency contract), not a new authority: the
   stage says "awaiting the operator", the night shift can only claim
   `approved-backlog`, and the mechanic never moves a stage itself. Approving,
   rejecting, resolving and stopping are the operator's decisions, recorded by
   the hub with `bin/estate proposal stage`. If you find yourself reaching
   for that command, stop — that is the operator's move, not this loop's.

**Nothing else. No git commit, ever.** The mechanic diagnoses and proposes; it
does not implement, and it did until 2026-07-30 (the consistency contract, narrowed and
approved by the operator that day). It used to be allowed to edit and commit files
under `docs/` on its own when a four-part safety test passed. The test was
sound and the changes were reversible — that was never the problem. The
problem is that "diagnose and propose, never operate" and "except for this one
class of change" cannot both be the rule, and the exception is the half a
tired reader remembers. So the lane is gone rather than narrowed again.

What that means concretely for the cases the lane used to cover:

| Used to be an apply | Now |
|---|---|
| A `docs/lessons.md` entry distilled from ledger corrections | A proposal. And note lessons.md is no longer a file anyone edits at all — it is rendered from the shared memory store (`bin/estate memory add`, then `bin/estate memory render --write`). Propose the memory; the hub or the development session adds it. |
| A `docs/ideas.md` note | A proposal, or an observation if it is just worth knowing. |
| A doc typo, "harmless to fix" | A proposal. The cost of one more line in REPORT.md is smaller than the cost of a lane. |

And the things that were never applyable stay exactly as they were:

- `loops.toml` values (model, interval, autostart — they change running
  behavior at the next restart)
- any file under another `loops/<name>/`, `hub/`, `bin/`, `infra/`, or `docs/`
- anything requiring a session start/stop/restart or an
  `agent-deck session send` — the mechanic never touches sessions;
  the hub is the operator
- **any `bin/extractions` write** (`add`, `approve`, `sync`, …) and any write
  into `state/extraction/` or the `public core` checkout. The EXTRACTION lens is
  detection only: it names candidates in REPORT.md, and the hub files them
  once the operator approves. The mechanic never touches another repo at all.

| Tempting shortcut | Why it's still a proposal |
|---|---|
| "It's a one-line model swap the operator already wants" | the operator decides *when*; a restart is operator work |
| "The skill has a typo, harmless to fix" | Skill edits need a session restart to take effect — operator work, and the lessons say stale-skill states are how loops break |
| "I'll just restart it myself so the fix lands" | Session lifecycle belongs to the hub, full stop |
| "It's only a docs change, and it's revertible" | That was the old apply lane. It is gone; a revertible change is still a change nobody reviewed |

## Morning relay

the hub (or the operator) reads the pass with `mechanic.py report`. The
**Proposal handoff** section is written to be relayed verbatim into the
control channel — it is the human view. The Evidence section stays here
for whoever needs to check the reasoning; the hub points at it rather
than pasting it. `bin/estate proposal show t-NN` prints one proposal's whole
record — condition, desired outcome, completion check, recurrences — which is
what to reach for when the operator asks what exactly he is being asked to approve.

Relaying a proposal does not approve it, and approval is not an
instruction to this loop. Anything the operator agrees to becomes the hub's
work, or a development session's — never this session's.

His answer lands as a stage change the hub records
(`bin/estate proposal stage t-NN approved-backlog|rejected|...`), which is why
the handoff carries the task id. The next pass sees the decision in the
gather's proposals section and stops re-proposing what has been settled.

## Continuous monitoring (`/loop`)

Run `/loop 20m /mechanic` in a dedicated session. Outside the window
every tick is a one-line heartbeat; inside it, the first tick runs the
night's single pass (a later tick `resume`s it if it was interrupted).
Never set or export ANTHROPIC_API_KEY.

## Durable filing and pass boundaries

Every proposal declares `classification`: `action` for a chosen next step,
`clarification` when alternatives cannot yet be stated, or `decision` for a
choice. A decision also carries `decision_question`, two `options` with a
consequence each, `recommendation`, and `defer_consequence`. A recommendation
may explicitly be `not recorded`; the key is still required.

Use `recurrence_of: "t-N"` when this is evidence of the same existing condition.
The engine validates that row and retains its identity even when wording or
counts change. It never chooses a prior task by similar prose.

`mechanic.py tally` counts recorded findings. `pass_done` derives its counts
from history; never type totals from memory. An interrupted pass records
`mechanic.py pass failed --reason "..."` (optionally `--night YYYY-MM-DD`).
This produces a failed terminal run and preserves the partial history.

`mechanic.py memory-triage --older-than-days 14` prepares one disposition
proposal per long-waiting memory candidate and pins recurrence identity.
A candidate remains unrecallable until an authorized curator promotes it.
This diagnostic proposes; it never promotes or rejects a memory itself.

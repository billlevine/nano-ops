---
name: mechanic
description: >-
  Nightly self-optimization pass over this estate — finds cost/config
  optimizations, scriptability candidates, and public-core extraction
  candidates, and writes every one of them up as a morning proposal. It
  implements nothing itself. Use when the user says
  "run the mechanic", "mechanic pass",
  "what did the mechanic find", "the mechanic's report", "optimization
  report", "tune the loops", or runs `/loop <interval> /mechanic` in a
  dedicated session.
---

# The Mechanic — nightly self-optimization pass

Once a night, in a quiet window, the mechanic walks the estate — ledger,
registry, session configs, lessons, loop skills — and asks five questions:

1. **COST/CONFIG** — is any loop configured wrong for the work it actually
   does? (An expensive model doing mechanical work, an interval that
   doesn't match observed activity, registry drifting from live sessions.)
2. **SCRIPTABILITY** — is any loop or the hub doing recurring work with
   model tokens that a deterministic script could do, the way each loop's
   own engine script already serves it?
3. **UPSTREAM (agent-deck)** — agent-deck is the open-source orchestration
   layer this whole estate runs on. Do the ledger's errors/corrections and
   the loops' workarounds reveal a recurring agent-deck limitation, missing
   flag, or rough edge that would be better fixed *upstream* than worked
   around here? Surface it as a candidate contribution — what's painful, how
   often, and roughly what change would fix it — for the operator to decide
   whether to file/PR it against agent-deck.
4. **INFRASTRUCTURE** — is the estate's own foundation starting to creak as
   it grows? The ledger + state are flat JSON files today; watch for signals
   they're outgrowing that (ledger size/query pain, cross-loop coordination
   needs, dependency tracking between work items) and, when the evidence
   supports it, propose an infrastructure step up — e.g. a DB or graph DB for
   the ledger/state, or an agent-memory/issue-graph tool like `beads` (Steve
   Yegge). Evidence-gated and forward-looking, not speculative; a proposal
   for the operator, never an auto-apply.
5. **EXTRACTION** — an estate may maintain a public core it extracts into
   ("mechanism is public; identity, policy, and data are not"). If this one
   does, does anything that changed here since the last pass look like a
   generalizable candidate under that rule? The gather computes the whole
   answer already — see **The extraction lens** below, which no-ops cleanly
   when the estate has no core configured. **Detection only:** the mechanic
   proposes candidates in REPORT.md and never files, sanitizes, or ports
   anything. Recording and executing them are separate tools the operator
   drives, and only when they say so.

**Roles: the mechanic diagnoses and proposes.** The hub operates; a dev
session at the repo root edits. **The mechanic implements nothing.** Every
finding leaves this loop as a proposal, an observation, or a recorded decision
not to propose — never as a commit. See **What the mechanic may write** below,
which is the whole of it.

**Never block on a prompt**: the pass runs at night with nobody watching. The
operator watches their control channel, not the agent-deck TUI, so a pending
`AskUserQuestion` (or any blocking interactive-question tool) is invisible to
them — and the next thing that lands in this terminal (a kick, a scheduled
wakeup) gets swallowed as the "answer", producing a nonsense response. Never
call one during a tick. This costs nothing here, because asking already has a
channel: anything needing the operator's call is a **proposal in REPORT.md**,
relayed by the hub in the morning. Write it there, record the pass, end the
tick; their answer comes back on a later tick.

## The engine

    .claude/skills/mechanic/scripts/mechanic.py

Deterministic state only — it never analyzes or edits. Data lives in
`../../state/mechanic/` (`config.toml` window, `history.jsonl` append-only
pass archive, `REPORT.md` morning view, `digest.json` baseline snapshot,
`last_tick`).

    mechanic.py windows          phase=pass|resume|done|idle + night id
    mechanic.py gather           the incremental digest (below)
    mechanic.py record '<json>'  append event (auto ts+night) to history
    mechanic.py report           print REPORT.md

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
  odd-status sessions, heartbeats staler than `2*interval + 120s`.
- **state files** — sizes with deltas; unchanged ones collapse to a count.
- **central ledger** — only entries appended since the last pass. Each
  entry's `detail` is capped at 300 chars; `ts`/`actor`/`kind`/`summary`/
  `refs` are verbatim.
- **git** — commits since the last pass's HEAD.
- **extraction** — the allowlist three-bucket diff (below).

**Do not re-read what the digest reports unchanged.** That re-read is the
thing this replaced: the whole manual set is ~180K of mostly-static text, and
feeding it back nightly was the cost. Open a policy file only when the digest
names it changed and you need more than the printed sections, or when a
finding you're chasing points into it.

### The extraction lens

An estate that maintains a public core is the *private fork*; the core is what
it extracts into. **The extraction allowlist is the manifest** — it lives in
the fork whose paths it governs, and there is deliberately no second copy in
the core to drift out of sync with it. `loops.toml`'s `[extraction]` table
names it (and says where the core checkout lives); the lens prints a one-line
note and moves on if extraction is unset or the allowlist is unreadable, which
is a fact about this machine, never a finding about the estate. An estate with
no public core downstream simply never configures the table, and this lens
stays quiet for good.

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
exactly the near-miss this lens exists to prevent.

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
     finish the remaining steps.
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
3. **Cost/config findings.** Look for:
   - *Model vs work*: what does this loop's model actually do per tick
     (per its skill + ledger lines)? Engine-does-the-work-model-narrates
     on a frontier model → propose a cheaper one; judgment-heavy work on
     a small model → propose the reverse.
   - *Cross-tool*: where the operator has a second coding agent available
     under its own subscription, that is a cost lever. Keep skill-dependent
     orchestration and review on the agent whose skills the loop actually
     uses — but where a loop or recurring task is **raw coding or
     environment investigation** that doesn't lean on those skills, the
     mechanic may propose handing it to the cheaper tool (agent-deck can
     launch other agents with `-c <tool>`). Treat it as another cost lever
     alongside model right-sizing.
   - *Registry vs live drift*: the gather's `drift` block already computes
     loops.toml model vs live session model, missing/odd-status sessions, and
     stale heartbeats. Judge each line — a drift line is evidence, not a
     finding on its own.
   - *Interval fit*: a loop whose ledger shows near-all no-op ticks →
     longer interval; missed activity or stacking ticks → shorter. Night
     windows that collide with other night windows.
   - *Hygiene*: stale heartbeats, state files growing without bound,
     config drift between similar loops.
4. **Scriptability findings.** Look for:
   - Recurring model-performed actions in the ledger — the same summary
     shape appearing tick after tick or night after night — that a
     deterministic script could do. Propose the script: what it does,
     where it lives, what the model stops doing.
   - Errors/corrections a `bin/ops doctor` check could catch.
5. **Extraction findings.** Read the gather's `extraction` block and judge it
   per **The extraction lens** above. Propose, in this order of priority:
   - every **(b) drift** line — the public core has gone stale on something
     already extracted. Name the path, the candidate id, and what moved.
   - the few **(a)** paths that genuinely pass the allowlist rule, each with
     the sanitization the never-committed table demands (channel/user ids,
     absolute paths, personas, employer/product names, ledger content) and a
     destination path in the core.
   - any **open candidate** that has been carried forward and is going stale
     (approved but never dispatched, extracting for several passes, blocked).
   Proposals only — filing a candidate is the operator's move once they
   approve, not the mechanic's. A quiet pass here is the normal case;
   extraction-worthy changes are rare and bursty.
6. **Classify and record.** Every finding gets one of three dispositions, and
   none of them is an edit:
   - `proposed` — it goes in REPORT.md as a proposal, with its evidence.
   - `observed` — worth knowing, not worth acting on. It goes under
     **Observations** in the Evidence section.
   - `no-proposal` — you looked and decided not to propose anything. Say why
     in the summary; "I considered this and it isn't worth a change" is a
     result, and a silent finding is indistinguishable from a missed one.

   `record '{"event":"finding","class":"cost|config|scriptability|extraction","action":"proposed|observed|no-proposal","summary":"..."}'`.
   There is no `applied`. If you catch yourself reaching for one, the finding
   is a proposal and the edit is somebody else's — see **What the mechanic may
   write**.
7. **Write `../../state/mechanic/REPORT.md`** (overwrite; history.jsonl
   is the archive). Two layers over one source of truth — a short
   handoff the operator can act on, and the evidence that backs it:

       # The Mechanic — night of <night>

       ## Proposal handoff (the hub relays this verbatim)
       - **P1 <recommendation>.** Why it matters now. The decision you're
         being asked to make. Blast radius. Evidence: <pointer into the
         Evidence section / a file / a ledger date>.
       - **P2 …**

       ## Evidence
       ### P1 <title>
       - the quoted ledger lines / config values that support it
       - the exact suggested change
       - caveats, alternatives considered, what would falsify this
       ### Observations (no proposal)
       - <worth knowing, not worth acting on — or looked at and
         deliberately not proposed, with why>

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
   the operator agreeing with it in the channel — that is the hub's work, or
   the dev session's. There is no lane in which it becomes yours.
8. `record '{"event":"pass_done","findings":N,"proposed":N,"observed":N,"no_proposal":N}'`,
   then append one summary line to `../../state/ledger.jsonl` as
   `{"ts","actor":"mechanic","kind":"activity","summary":"..."}`.

## What the mechanic may write

Exactly two things, and neither of them is an implementation:

1. **Its own operational state, under `../../state/mechanic/`** —
   `history.jsonl` (the authoritative pass archive), `REPORT.md` (the morning
   view), `digest.json` (the baseline snapshot), `last_tick` (the heartbeat),
   and `config.toml` when the engine writes it. These are how the loop *runs*.
   They are written through `mechanic.py` (`record`, `gather`, the report
   write), never by hand-editing a file the engine owns.
2. **One central-ledger line per pass**, appended to `../../state/ledger.jsonl`
   as `{"ts","actor":"mechanic","kind","summary"}`, plus a line for any error
   or correction the pass hits. Append-only, same as every other loop.

**Nothing else. No git commit, ever.** The mechanic diagnoses and proposes; it
does not implement. It used to be allowed to edit and commit files under
`docs/` on its own when a four-part safety test passed. The test was sound and
the changes were reversible — that was never the problem. The problem is that
"diagnose and propose, never operate" and "except for this one class of change"
cannot both be the rule, and the exception is the half a tired reader
remembers. So the lane is gone rather than narrowed again.

What that means concretely for the cases the lane used to cover:

| Used to be an apply | Now |
|---|---|
| A distilled lessons entry from ledger corrections | A proposal. Whatever store your estate keeps its lessons in, the mechanic proposes the entry; the hub or the dev session writes it. |
| An ideas-file note | A proposal, or an observation if it is just worth knowing. |
| A doc typo, "harmless to fix" | A proposal. The cost of one more line in REPORT.md is smaller than the cost of a lane. |

And the things that were never applyable stay exactly as they were:

- `loops.toml` values (model, interval, autostart — they change running
  behavior at the next restart)
- any file under another `loops/<name>/`, `hub/`, `bin/`, `infra/`, or `docs/`
- anything requiring a session start/stop/restart or an
  `agent-deck session send` — the mechanic never touches sessions;
  the hub is the operator
- **any write to the extraction store** (filing, approving, syncing a
  candidate) and any write into the public-core checkout. The EXTRACTION lens
  is detection only: it names candidates in REPORT.md, and the operator files
  them once they approve. The mechanic never touches another repo at all.

| Tempting shortcut | Why it's still a proposal |
|---|---|
| "It's a one-line model swap the operator already wants" | The operator decides *when*; a restart is operator work |
| "The skill has a typo, harmless to fix" | Skill edits need a session restart to take effect — operator work, and stale-skill states are a classic way loops break |
| "I'll just restart it myself so the fix lands" | Session lifecycle belongs to the hub, full stop |
| "It's only a docs change, and it's revertible" | That was the old apply lane. It is gone; a revertible change is still a change nobody reviewed |

## Morning relay

The hub (or the operator) reads the pass with `mechanic.py report`. The
**Proposal handoff** section is written to be relayed verbatim into the
control channel — it is the human view. The Evidence section stays here
for whoever needs to check the reasoning; the hub points at it rather
than pasting it.

Relaying a proposal does not approve it, and approval is not an
instruction to this loop. Anything the operator agrees to becomes the hub's
work, or a dev session's — never this session's.

## Continuous monitoring (`/loop`)

Run `/loop 20m /mechanic` in a dedicated session. Outside the window
every tick is a one-line heartbeat; inside it, the first tick runs the
night's single pass (a later tick `resume`s it if it was interrupted).
Never set or export ANTHROPIC_API_KEY.

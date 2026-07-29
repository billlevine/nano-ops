---
name: mechanic
description: >-
  Nightly self-optimization pass over the estate — finds cost/config
  drift, scriptability candidates, upstream-tool friction, infrastructure
  pressure and (optionally) public-core extraction candidates, applies only
  safe reversible doc changes, and writes everything else up as morning
  proposals. Use when asked to "run the mechanic", "mechanic pass", "what
  did the mechanic find", "the mechanic's report", "optimization report",
  "tune the loops", or when a session runs `/loop <interval> /mechanic`.
---

# The Mechanic — nightly self-optimization pass

Once a night, in a quiet window, the mechanic walks the estate — ledger,
registry, session configs, policy files, loop skills — and asks five questions.

REPO means the repository root (`../..` from this loop's directory). NAME is
this loop's registry name (`mechanic` unless renamed). Read
`REPO/loops.toml` for your own `interval`; do not hardcode it.

1. **COST/CONFIG** — is any loop configured wrong for the work it actually
   does? An expensive model doing mechanical work, an interval that doesn't
   match observed activity, the registry drifting from the live sessions.
2. **SCRIPTABILITY** — is any loop, or the hub, spending model tokens on
   recurring work a deterministic script could do? Every loop here is meant to
   be a thin judgment layer over its own scripts; a loop whose ticks are mostly
   mechanical has that split in the wrong place.
3. **UPSTREAM** — the estate runs on tools it does not own (`agent-deck` and
   the agent CLI above all). Do the ledger's errors and corrections, and the
   workarounds sitting in loop skills, reveal a recurring limitation, missing
   flag, or rough edge that would be better fixed *upstream* than worked around
   here? Surface it as a candidate contribution: what is painful, how often,
   and roughly what change would fix it. The operator decides whether to file
   it.
4. **INFRASTRUCTURE** — is the estate's own foundation starting to creak as it
   grows? State and the ledger are flat files here. Watch for signals they are
   outgrowing that — ledger size or query pain, cross-loop coordination,
   dependency tracking between work items — and, when the evidence supports it,
   propose a step up. Evidence-gated and forward-looking, never speculative,
   and always a proposal.
5. **EXTRACTION** — *optional; skip entirely unless configured.* If this estate
   is a private fork feeding a public core, has anything changed here that
   belongs in the core under the rule "mechanism is public; identity, policy
   and data are not"? The gather computes the whole answer — see **The
   extraction lens** below. With no `[extraction]` table in `loops.toml` the
   lens prints one note and there is nothing to judge, which is the normal case.

**Diagnose and propose. The mechanic never operates.** The hub runs and
reloads loops; changes are built in a dev session at the repo root (see
[`docs/garage.md`](../../../../../docs/garage.md)). The one apply lane is defined
below and is deliberately narrow.

**Never block on a prompt.** The pass runs at night with nobody watching. A
pending `AskUserQuestion` is invisible to the operator, and the next thing that
lands in this terminal — a kick, a scheduled wakeup — gets swallowed as the
"answer", producing a nonsense response. This costs nothing, because asking
already has a channel: anything needing a human decision is a **proposal in
REPORT.md**, relayed by the hub in the morning. Write it there, record the
pass, end the tick; the answer comes back on a later tick.

## The engine

    .claude/skills/mechanic/scripts/mechanic.py

Deterministic state only — it never analyzes or edits. Data lives in
`REPO/state/<NAME>/`: `config.toml` (the window), `history.jsonl` (append-only
pass archive), `REPORT.md` (the morning view), `digest.json` (baseline
snapshot), `last_tick`.

    mechanic.py windows          phase=pass|resume|done|idle + night id
    mechanic.py gather           the incremental digest (below)
    mechanic.py record '<json>'  append event (auto ts+night) to history
    mechanic.py report           print REPORT.md

### The digest is incremental — trust it

`gather` is the pass's whole input. It diffs the estate against `digest.json`
(the previous night's snapshot of file hashes, ledger byte cursor, state sizes,
registry lines, git HEAD) and prints **only what moved**:

- **policy files** — root/hub/loop `CLAUDE.md` + `SKILL.md`, plus `docs/`
  files the estate keeps as policy. Hashed whole-file *and* per section.
  Unchanged files are listed on one line; changed files print only their
  changed or new sections.
- **sessions** — one `agent-deck ls --json` snapshot (not a `show` per loop),
  plus a derived `drift` block: registry model vs live model, missing or odd
  status, heartbeats staler than `2*interval + 120s`. The hub's session title
  and the deck profile resolve from `loops.toml` `[hub]`, so this works
  unmodified on any installation.
- **state files** — sizes with deltas; unchanged ones collapse to a count.
- **central ledger** — only entries appended since the last pass. Each entry's
  `detail` is capped at 300 chars; `ts`/`actor`/`kind`/`summary`/`refs` are
  verbatim, and the raw file is still there to grep.
- **git** — commits since the last pass's HEAD.
- **extraction** — the three-bucket allowlist diff, when configured.

**Do not re-read what the digest reports unchanged.** That re-read is the
thing this replaced: the full policy set is a large, mostly-static body of
text, and feeding it back nightly was the cost. Open a policy file only when
the digest names it changed and you need more than the printed sections, or
when a finding you are chasing points into it.

A cold run — no `digest.json`, or `gather --full` — prints the full estate, so
a first pass is never short-changed. A **resume** tick re-gathers the same
digest the interrupted pass saw (the baseline does not advance mid-night), so
you can rely on it after an interruption. `gather --no-save` inspects without
touching the snapshot. `digest.json` is a derived cache: deleting it costs one
full digest and nothing else.

### The extraction lens

This lens exists for one shape of estate: a **private fork** that feeds a
**public core** built from an allowlist, so that mechanism can be shared
without publishing identity, policy or data. If that is not this estate, the
lens is unconfigured, `gather` says so in one line, and you skip step 5 of the
pass. Do not treat an unconfigured lens as a finding — it is a fact about the
installation.

When it is configured, `loops.toml`'s `[extraction]` table names the core
checkout (`repo`) and the allowlist (`allowlist`, a path in **this** repo).
**The allowlist is the manifest.** It lives in the fork whose paths it governs,
so there is exactly one copy and nothing to drift out of sync. `gather`
resolves every allowlisted path into one of three buckets, and that IS the
lens — do not re-derive it by hand:

- **(a) new & matching, never extracted** — allowlisted mechanism sitting here
  with no candidate filed. Paths unchanged since the last pass collapse to one
  named line ("same call as last pass"): still visible, not re-argued. A path
  that is new or changed prints in full — that is the one worth a look.
- **(b) already extracted, CHANGED since (drift)** — the ongoing-sync case, and
  the reason this is a lens rather than a one-time port. A path whose content
  moved since its sync hash was recorded. Ranks above (a): the public core is
  now *stale*, not merely incomplete.
- **(c) explicitly excluded (private-only)** — named rather than absent, so
  "not in the output" can never be misread as "already handled".

Plus **open candidates carried forward** — every unresolved entry in
`REPO/state/extraction/candidates.json`, re-surfaced every pass until it
reaches a resting state, whether or not its source file ever changes again.
That is the durable-pressure half: a candidate spotted once and then forgotten
is the failure this lens exists to prevent.

**Judge, don't just relay.** A path in (a) is *allowlist-eligible*, not
*extraction-worthy*: an allowlist's directory rows are broad, and plenty of
what they match is this estate's own diary or standing orders, which the rule
puts in the fork. Ask the rule directly — could a stranger clone this file,
fill in their own `loops.toml`, and have it work? Propose the few that pass,
with the sanitization they would need; say nothing about the rest.

**Detection only.** The mechanic proposes candidates in REPORT.md. It never
files, sanitizes, ports, or writes into the candidate store or the other
checkout. See the apply lane.

## The tick (one invocation)

1. Run `python3 .claude/skills/mechanic/scripts/mechanic.py windows`.
2. Route on phase:
   - **idle** / **done** → report the one-liner, nothing else. Most ticks
     end here.
   - **pass** → run The Pass (below).
   - **resume** → an interrupted pass: read tonight's lines from
     `REPO/state/<NAME>/history.jsonl`, skip what is already recorded,
     finish the remaining steps.
3. Heartbeat (every tick, all phases):
   `date +%s > REPO/state/<NAME>/last_tick`.

One pass per night is enforced by the engine: after `pass_done` is recorded,
`windows` says `done` until the next night.

## The Pass

1. `mechanic.py record '{"event":"pass_start"}'`.
2. **Gather.** Run `mechanic.py gather` — that is the input, whole. It already
   carries the policy files (changed sections only), the session snapshot,
   drift, state deltas, new ledger entries, and new commits. Read further only
   where it points: a file it names changed, or a ledger entry whose capped
   `detail` you need in full (grep `REPO/state/ledger.jsonl`).
3. **Cost/config findings.** Look for:
   - *Model vs work*: what does this loop's model actually do per tick (per
     its skill and its ledger lines)? Engine-does-the-work-model-narrates on a
     frontier model → propose a cheaper one; judgment-heavy work on a small
     model → propose the reverse.
   - *Cross-tool*: if the installation has a second agent CLI or subscription
     available, recurring work that does not depend on this CLI's own skills —
     raw coding, read-only investigation — is a candidate for handing over as a
     cost lever. Keep skill-dependent orchestration and review where the skills
     live. Propose it the same as any other cost finding.
   - *Registry vs live drift*: the gather's `drift` block already computes
     `loops.toml` model vs live session model, missing or odd-status sessions,
     and stale heartbeats. Judge each line — a drift line is evidence, not a
     finding on its own.
   - *Interval fit*: a loop whose ledger shows near-all no-op ticks → longer
     interval; missed activity or stacking ticks → shorter. Night windows that
     collide with other night windows.
   - *Hygiene*: stale heartbeats, state files growing without bound, config
     drift between similar loops.
4. **Scriptability findings.** Look for:
   - Recurring model-performed actions in the ledger — the same summary shape
     appearing tick after tick, or night after night — that a deterministic
     script could do. Propose the script: what it does, where it lives, what
     the model stops doing.
   - Errors and corrections a `bin/ops doctor` check could catch.
5. **Extraction findings** *(only if the lens is configured)*. Read the
   gather's `extraction` block and judge it per **The extraction lens** above.
   Propose, in this order of priority:
   - every **(b) drift** line — the public core has gone stale on something
     already extracted. Name the path, the candidate id, and what moved.
   - the few **(a)** paths that genuinely pass the allowlist rule, each with
     the sanitization the allowlist's never-committed table demands and a
     destination path in the core.
   - any **open candidate** carried forward that is going stale.
   Proposals only. A quiet pass here is the normal case; extraction-worthy
   changes are rare and bursty.
6. **Classify and act.** Sort every finding into the apply lane or a proposal
   (definitions below). Apply-lane changes: make the edit, one git commit each
   (no push), one central-ledger line each. Record every finding either way:
   `record '{"event":"finding","class":"cost|config|scriptability|upstream|infrastructure|extraction","action":"applied|proposed","summary":"..."}'`.
7. **Write `REPO/state/<NAME>/REPORT.md`** (overwrite; `history.jsonl` is the
   archive). Two layers over one source of truth — a short handoff the operator
   can act on, and the evidence that backs it:

       # The Mechanic — night of <night>

       ## Proposal handoff (relayed verbatim)
       - **P1 <recommendation>.** Why it matters now. The decision you're
         being asked to make. Blast radius. Evidence: <pointer into the
         Evidence section / a file / a ledger date>.
       - **P2 …**

       ## Applied autonomously
       - <sha> <what and why>

       ## Evidence
       ### P1 <title>
       - the quoted ledger lines / config values that support it
       - the exact suggested change
       - caveats, alternatives considered, what would falsify this
       ### Observations (no action)
       - <worth knowing, not worth acting on>

   **The handoff is short; the evidence is complete.** Three or four sentences
   per proposal, in the order above — recommendation first, then why now, then
   the decision requested, then blast radius. Do not explain the diagnostic
   framework there; a concrete observation, its consequence, and the next step
   are enough. Everything you would have put in a long parenthetical goes under
   Evidence instead, and nothing gets dropped to make the handoff shorter.

   Rank proposals; keep only the few that matter (≤5 in a normal night). Every
   proposal carries evidence — quote the ledger lines or config values that
   support it. Distinguish a recurring pattern from a single event, and a
   recommendation from speculation. No speculative rewrites.

   **A proposal is a request, not a plan you are about to carry out.** Writing
   P1 gives you no authority to make the change, and neither does the operator
   agreeing with it. See the apply lane below, which is unchanged.
8. **Relay.** Copy the **Proposal handoff** section (only that section) to
   `REPO/state/<NAME>/RELAY.md`. That is this loop's relay marker: the hub's
   health pass finds it, relays it verbatim to the operator, and deletes it.
   You never post to the control channel yourself. If `RELAY.md` already
   exists, merge into it rather than overwriting. Skip this when the pass found
   nothing worth an interruption — a loop that relays every quiet night trains
   the operator to ignore it.
9. `record '{"event":"pass_done","findings":N,"applied":N,"proposed":N}'`, then
   append one summary line to `REPO/state/ledger.jsonl` as
   `{"ts","actor":"<NAME>","kind":"activity","summary":"..."}`.

## The apply lane

A finding may be applied autonomously only when **all four** hold:

1. The change is to a file under `docs/` or `REPO/state/<NAME>/`.
2. No running session reads that file to decide its behavior.
3. `git revert` restores it completely — no side effects beyond the file.
4. It changes words or records, not what any loop *does*.

Everything else is a proposal in REPORT.md. In particular these are **always**
proposals, never direct edits — not for typos, not for one-character fixes,
not when the fix is "obviously safe":

- `loops.toml` values (model, interval, autostart — they change running
  behavior at the next restart)
- any file under another `loops/<name>/`, `hub/`, `bin/`, or `infra/`
- anything requiring a session start/stop/restart or an
  `agent-deck session send` — the mechanic never touches sessions; the hub is
  the operator
- **any write to the extraction candidate store, or into the public-core
  checkout, or into any other repository.** The extraction lens is detection
  only. The mechanic never touches another repo at all.

| Tempting shortcut | Why it's still a proposal |
|---|---|
| "It's a one-line model swap the operator already wants" | They decide *when*; a restart is the hub's work |
| "The skill has a typo, harmless to fix" | Skill edits need a session restart to take effect — operator work, and a half-reloaded skill is how loops break |
| "I'll just restart it myself so the fix lands" | Session lifecycle belongs to the hub, full stop |

## Morning relay

The operator reads the pass with `mechanic.py report`. The **Proposal handoff**
section is written to be relayed verbatim into the control channel — it is the
human view. The Evidence section stays on disk for whoever needs to check the
reasoning; the hub points at it rather than pasting it.

Relaying a proposal does not approve it, and approval is not an instruction to
this loop. Anything the operator agrees to becomes the hub's work, or a garage
session's — never this session's.

## Continuous monitoring (`/loop`)

Run `/loop 20m /mechanic` in a dedicated session. Outside the window every tick
is a one-line heartbeat; inside it, the first tick runs the night's single pass
(a later tick `resume`s it if it was interrupted). Never set or export
`ANTHROPIC_API_KEY`.

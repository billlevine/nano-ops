"""The qualifying-event contract for the estate's one Ledger (gap audit P-06).

Root CLAUDE.md promises one searchable history of what the estate did. It was
not one: the hub's prose lines went to `state/ledger.jsonl`, each loop's real
outcomes went to its own private JSONL, and the estate's `events` table held
mostly task lifecycle rows. So "what happened to this piece of work" had four
possible answers depending on which file you opened, and three scenarios in the
gap audit fall straight through the gap:

  S20  the spotter identifies a completed / failed / attention-required change
       and it is recorded as a state-change event in the Ledger — and an
       UNCHANGED item is not falsely recorded as changed.
  S43  every named subsystem's qualifying actions are durably recorded, each
       naming the acting subsystem, the applicable tracked item, the observed
       outcome, and the time.
  S45  all of it survives a run, a failure, and a restart, unchanged.

WHAT THIS MODULE IS
-------------------
The vocabulary and the fingerprint, in one place, so a writer and the migration
that backfills history cannot disagree about either. It holds no I/O: the
writers are `bin/estate` (which imports this) and the loops (which shell out to
`bin/estate event`, never import — see WHY LOOPS DO NOT IMPORT below).

THE FOUR PHASES
---------------
P-06 names four classes of qualifying action. They ride in their own `phase`
column rather than being folded into `kind`, deliberately:

  initiation  work started — a pass began, an item was queued, a dispatch went
  transition  a MEANINGFUL state change of a tracked item
  outcome     a terminal result for one unit of work
  failure     something was attempted and did not work

`kind` already carries a different and older meaning in this table (activity,
note, transition, evidence) and bin/dashboard renders it verbatim. Overloading
it would have silently changed what 274 existing rows mean to a reader, to save
one `ALTER TABLE ADD COLUMN`. The phase column is purely additive: every row
written before P-06 reads exactly as it did, with `phase IS NULL` — which is
also the precise, queryable definition of "not a qualifying event".

A qualifying event carries a phase AND a subsystem. Neither is useful alone:
a phase with no subsystem cannot answer S43's "which subsystem acted", and a
subsystem with no phase is the untyped `activity` row P-06 exists to replace.
`bin/estate event` refuses one without the other.

WHAT IS *NOT* QUALIFYING
------------------------
S20's second half is the load-bearing one: "the unchanged item is not falsely
recorded as changed". A refresh that observes the same state as last time has
performed no qualifying action, and emitting a heartbeat row for it would make
the Ledger's volume a measure of polling frequency rather than of activity.
Nothing here fires on an unchanged observation, and the spotter's emitter is
driven by its own change list for exactly that reason.

THE FINGERPRINT
---------------
Every qualifying event carries a stable fingerprint derived from what the event
IS — never from when it was written. Two consequences, both required:

  * A writer that crashes after emitting and replays lands on the same
    fingerprint, and the unique index makes the second write a no-op instead of
    a duplicate. Idempotence is mechanical, not a convention.
  * The backfill can run twice, or partially, and converge — the same property
    `bin/migrate-shared-identity` and `bin/migrate-mechanic-proposals` have.

`ts` is part of the fingerprint because a subsystem legitimately repeats an
action: the spotter can see the same PR go red twice, and those are two events,
not one. What is NOT in it is anything about the writing run, so a replay of
the same recorded moment collides on purpose.

WHY LOOPS DO NOT IMPORT THIS
----------------------------
A loop engine runs against a fabricated repo root in its own tests and must not
depend on the repo's `bin/` or `lib/` — the same rule `mechanic.py`'s SUBSYSTEMS
copy already states. So each loop keeps its own small constants and shells out
to `bin/estate event`. `bin/test_subsystem_events.py` reads every copy and fails
if any of them drifts from this file, so the duplication is checked rather than
trusted.

Docs: docs/ledger-contract.md
"""
from __future__ import annotations

import hashlib

# ── the four qualifying phases ───────────────────────────────────────────────

INITIATION = "initiation"
TRANSITION = "transition"
OUTCOME = "outcome"
FAILURE = "failure"

PHASES = (INITIATION, TRANSITION, OUTCOME, FAILURE)

# ── the nine subsystems ──────────────────────────────────────────────────────
# the estate's own list, and the same nine `bin/hub-intake subsystems` routes to and
# `mechanic.py subsystems` walks every night. Adding a tenth is a deliberate
# edit here and in those copies; the drift test names them all.
SUBSYSTEMS = ("hub", "mechanic", "night-shift", "spotter", "briefer",
              "tasks", "memory", "dashboard", "ledger")

# ── actor -> subsystem ───────────────────────────────────────────────────────
# The actor field carries the TECHNICAL name (docs/actor-taxonomy.md); a
# subsystem is the audit's name for the same thing, and the two lists are not
# the same shape. Five subsystems are loops or the hub and map one-to-one. The
# other four — tasks, memory, dashboard, ledger — are not sessions at all, so
# nothing maps ONTO them here; they are named by the tool acting on its own
# behalf (`bin/estate`, `bin/dashboard-ack`) and pass their subsystem directly.
SUBSYSTEM_BY_ACTOR = {
    "hub": "hub",
    "mechanic": "mechanic",
    "pr-reviewer": "night-shift",
    "pr-tracker": "spotter",
    "morning-brief": "briefer",
}

# The reverse, for a reader that has a subsystem and wants the writer's name.
ACTOR_BY_SUBSYSTEM = {sub: act for act, sub in SUBSYSTEM_BY_ACTOR.items()}

FINGERPRINT_PREFIX = "e-"
FINGERPRINT_HEX = 16

# ── each writer's own vocabulary, mapped to the four phases ──────────────────
# These live here rather than only in the loop engines because there are TWO
# readers of each: the live emitter inside the loop, and the backfill in
# bin/migrate-subsystem-events reading that loop's history file. If they
# disagree, the same happening lands under two phases depending on which one
# saw it first — so the tables are canonical here, the loops hold checked
# copies, and bin/test_subsystem_events.py fails on any drift.

# The night shift's queue events (loops/pr-reviewer). `estate_identity` is the
# one deliberate omission: it is that engine narrating its own bookkeeping, not
# the night shift acting on the owner's work.
NIGHT_SHIFT_PHASE = {
    "queued": INITIATION,
    "claimed": INITIATION,
    "task_started": INITIATION,
    "review_started": INITIATION,
    "rework_started": INITIATION,
    "review_posted": TRANSITION,
    "fixup_pushed": TRANSITION,
    "rebased": TRANSITION,
    "delegated": TRANSITION,
    "ci_rerun": TRANSITION,
    "check_result": TRANSITION,
    "external_work_in_flight": TRANSITION,
    "item_done": OUTCOME,          # `outcome: failed` overrides to FAILURE
    "exited": OUTCOME,
    "dismissed": OUTCOME,
    "ci_green": OUTCOME,
    "artifact": OUTCOME,
    "follow_up": OUTCOME,
    "ci_failed": FAILURE,
    "ci_escalated": FAILURE,
    "correction": FAILURE,
    "done_rejected": FAILURE,
}

# A scheduled loop's pass boundaries — the mechanic's night and the briefer's
# morning use the same three words for the same three things.
PASS_PHASE = {"pass_start": INITIATION, "pass_done": OUTCOME,
              "pass_failed": FAILURE}

# The morning brief's DELIVERY, keyed on the local date (gap audit P-10).
# `PASS_PHASE` above covers generation — the pass began, finished, or broke.
# This is what happens to the artifact afterwards, and it is a different
# question: a brief that was written and never reached Slack is a success by
# every generation measure and a failure by the only one the owner sees.
#
# Only the terminal facts are here, deliberately. Every failed relay attempt
# and the one that finally works get a row; claiming an attempt does not,
# because the publication record already numbers the attempts and a row per
# claim would make the Ledger's volume a measure of hub ticks.
RELAY_PHASE = {"relay_failed": FAILURE,
               "relay_abandoned": FAILURE,
               "brief_delivered": OUTCOME}

# The morning brief's GATHER, one row per source that could not answer for
# itself (gap audit P-11, docs/summary-manifest-contract.md). Generation and
# delivery above are about the artifact; this is about the inputs it was built
# from, and it is a failure of the estate's ability to SEE rather than of the
# brief — the brief still goes out, saying which part of the morning it could
# not read.
#
# One event name, deliberately. What varies is the source and the outcome, and
# both ride in the fingerprint key, so a second gather that hits the same wall
# is the same fact and lands nothing (S20), while a source that starts failing
# a NEW way is a new row. A source that completed emits nothing at all.
GATHER_PHASE = {"gather_source_failed": FAILURE}

# The hub's Slack intake outcomes (bin/hub-intake). `routed` begins work
# somewhere; the other three end the hub's handling of the message. The linked
# routing-failure row that `unroutable` also emits is a FAILURE — of the
# estate's coverage, which is the fact S03 asked to be able to count.
INTAKE_PHASE = {"routed": INITIATION, "escalated": OUTCOME,
                "unroutable": OUTCOME, "uninterpretable": OUTCOME}
ROUTING_FAILURE_PHASE = FAILURE

# The central JSONL ledger's `kind`, for the rows that HAVE a determinable
# phase. `activity` is deliberately absent and that is the finding, not an
# oversight: it is the generic bucket 829 of 991 historical lines sit in, it
# says nothing about whether work began, moved, ended, or broke, and assigning
# it one would be inventing a record rather than migrating one.
LEDGER_KIND_PHASE = {"error": FAILURE, "correction": TRANSITION}


class ContractError(ValueError):
    """A qualifying event that does not satisfy the contract."""


def fingerprint(*, subsystem: str, phase: str, source: str, key: str,
                ts: str, event: str = "") -> str:
    """The stable identity of one qualifying event.

    The parts, and why each is in:

      subsystem  two subsystems can record the same-shaped thing about the
                 same item and mean different events.
      phase      an item can fail and then succeed at the same recorded second.
      source     which record this projects from — `state/ledger.jsonl`, a
                 loop's own history, or a live emitter. Two writers describing
                 one real-world happening are still two Ledger rows, and
                 collapsing them would silently drop whichever arrived second.
      key        the tracked item this is about: a PR key, a task id, an intake
                 id, a night id. The natural key of the thing, not a row
                 number — row numbers are reused and PR keys are not.
      ts         when it happened. A subsystem legitimately repeats an action;
                 two ejections of the same PR are two events.
      event      the writer's own native name for it (`ci_failed`,
                 `pass_done`), so one item's several events at one timestamp
                 stay distinct.

    Nothing about the writing RUN is in here. That is the point: a replay of
    the same recorded moment must collide, so the unique index absorbs it.
    """
    parts = [subsystem or "", phase or "", source or "", key or "", ts or "",
             event or ""]
    digest = hashlib.sha256("\n".join(str(p).strip() for p in parts).encode())
    return FINGERPRINT_PREFIX + digest.hexdigest()[:FINGERPRINT_HEX]


def check(subsystem, phase) -> None:
    """Raise unless this pair is a legal qualifying event. Both or neither.

    Enforced rather than documented because "the loop was supposed to pass a
    subsystem" is not a fact anyone can recover from a row that does not have
    one — and an untyped row is exactly what P-06 is replacing.
    """
    if not subsystem and not phase:
        return
    if bool(subsystem) != bool(phase):
        raise ContractError(
            "a qualifying event carries BOTH a subsystem and a phase "
            f"(got subsystem={subsystem!r}, phase={phase!r}). One without the "
            "other cannot answer 'which subsystem did what' — pass both, or "
            "neither for an untyped note")
    if subsystem not in SUBSYSTEMS:
        raise ContractError(
            f"unknown subsystem {subsystem!r}. The nine are: "
            + ", ".join(SUBSYSTEMS))
    if phase not in PHASES:
        raise ContractError(
            f"unknown phase {phase!r}. The four qualifying phases are: "
            + ", ".join(PHASES))


def subsystem_for(actor: str) -> str | None:
    """The subsystem a standing actor speaks for, or None.

    None is a real answer, not a lookup failure: `garage`, `ops` and every
    ephemeral worker act on the estate without BEING one of the nine.
    """
    return SUBSYSTEM_BY_ACTOR.get((actor or "").strip())

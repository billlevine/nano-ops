"""Which alternative was chosen, and whether anything ever picked it up (t-1794).

WHAT IT FIXES
-------------
Measured across every `-> approved-backlog` event on file: a decision the HUB
records reaches its next event in a median of minutes, because the hub records
and acts in one breath. A decision recorded by the OWNER — the dashboard's own
decision cards — waits a median of more than a day, and one recorded from the
CLI longer still. Approved rows are routinely found weeks later with no
machine event on them at all.

Two holes, one consequence.

  1. THE CHOICE IS NOT RECORDED. `/estate.html` renders every alternative as a
     clickable card and the click's identity is stringified into the free-text
     decision note ("Owner clicked: ..."). The hub's own auto-queue criterion
     asks whether the writeup left more than one viable path open, reads a
     proposal still listing 2-4 alternatives, and correctly defers — re-asking
     a question the owner already answered, because the answer is in prose.
     The estate knows the answer essentially every time and stores it where
     software may not read it.

  2. "ALREADY ACTED ON" IS NOT OBSERVABLE. The hub's skill skips "anything
     whose refs carry a `queue_key`". That key is on essentially no TASKS and
     on many EVENTS, because the night shift writes it onto the enqueue event
     for a task it adopts and onto the task's own refs only for a task it
     mints itself. So the skip check reads a signal that is never there on the
     rows it is asked about, and
     an approved row that somebody already routed looks identical to one nobody
     has touched.

WHERE THE CHOICE LIVES, AND WHY IT IS THE EVENT
-----------------------------------------------
On the stage-transition event's own `refs`, never in the task's. It is
amendment-safe by construction: the event records what
the alternatives WERE at decision time, so a later `proposal amend` — which
replaces the alternative set outright — cannot retro-change what "was chosen"
means. A position stored in task refs would have silently repointed at whatever
now sits in that slot.

The position is 1-BASED and it is called `position`, not `index`, so no reader
assumes zero. That is also how a store's own prose already says it ("picked
Option A", "accepting Option 1"). The option TEXT and its consequence
ride beside it, and the whole alternatives list rides under `alternatives` —
three copies of the same fact on purpose, because the point of the record is
that it stays true after the source moves.

This module invents NO second classification scheme. `decision`, `alternatives`
and `recommendation` all mean exactly what docs/decision-classification-
contract.md says they mean; `estate_decisions` is imported for them rather than
restated.

WHAT COUNTS AS A PICKUP
-----------------------
An event AFTER the decision that records something taking the work on. Three
shapes and no others:

  routed   an event carrying `refs.pickup` — the explicit marker this module
           adds, and the only one that names a route
  queued   an event whose refs carry `queue_key` — the night shift's own
           enqueue record, the signal SKILL.md step 3 was already reaching for
  claimed  a `ready -> claimed` transition — somebody holds it

A `note` is NOT a pickup. t-777 has been approved since 2026-08-17 and carries
nine mechanic notes since; t-1329 carries ten. Counting those would report the
two most stuck rows in the store as handled. An `auto-promote` is not a pickup
either — `estate ready --auto-promoted` exists precisely because being promoted
by the store is not being picked up by anything.

WHY THERE IS NO OBSERVATION VOCABULARY HERE
-------------------------------------------
`unobservable` in this module means the DECISION INSTANT could not be read —
a row sitting at `approved-backlog` with no event that says how it got there.
It does not mean a fetch failed. There is no fetch: the caller reads one
SQLite store, and if that store will not open nothing runs at all, exactly as
for `estate attention`, `estate stale` and `estate due`. So this registers no
`lib/estate_observation` vocabulary — an absence here is observed by
construction, and the one thing that can be genuinely unknown gets its own
verdict rather than being folded into `owed`, which is an accusation.
"""
from __future__ import annotations

import datetime as dt

import estate_decisions as decisions

# --------------------------------------------------------------------------- #
# the chosen alternative
# --------------------------------------------------------------------------- #

# The refs key on the stage-transition event. One key holding one object, so a
# reader that wants the whole record gets it in one lookup and a reader that
# wants the position does not have to know which of four keys is authoritative.
CHOSEN_KEY = "chosen"
POSITION_KEY = "position"
OPTION_KEY = decisions.OPTION_KEY
CONSEQUENCE_KEY = decisions.CONSEQUENCE_KEY
# The snapshot. Same key name the proposal's own refs use, because it is the
# same list — frozen at the instant of the decision.
ALTERNATIVES_KEY = decisions.ALTERNATIVES_KEY


class ChoiceRefused(ValueError):
    """A stage decision that cannot say which branch was taken.

    Carries the key at fault in `.key`, the same bargain
    `estate_decisions.DecisionFilingRefused` already strikes, so a CLI and an
    HTTP caller can both say which one without parsing prose.
    """

    def __init__(self, message: str, key: str = CHOSEN_KEY):
        super().__init__(message)
        self.key = key


def is_decision(refs) -> bool:
    """Does this proposal's refs envelope DECLARE itself a decision?

    Nothing is inferred. A row with no classification is legacy and owes no
    choice — docs/decision-classification-contract.md retrofits nothing, and
    neither does this.
    """
    if not isinstance(refs, dict):
        return False
    value = refs.get(decisions.CLASSIFICATION_KEY)
    return isinstance(value, str) and value.strip().lower() == decisions.DECISION


def alternatives_of(refs) -> list:
    """The usable alternatives on a proposal, in stored order.

    Order is the identity here, so nothing is filtered out: an entry with no
    `option` would shift every position after it and make the recorded choice
    name a different branch than the one that was clicked.
    """
    if not isinstance(refs, dict):
        return []
    value = refs.get(ALTERNATIVES_KEY)
    if not isinstance(value, list):
        return []
    return list(value)


def chosen_record(refs, position) -> dict:
    """The structured choice for `position`, or raise naming what is wrong.

    `position` is 1-based and must land inside the alternatives as they stand
    RIGHT NOW; the returned record freezes them, so the next `proposal amend`
    changes nothing about what this decision meant.
    """
    if not is_decision(refs):
        raise ChoiceRefused(
            f'a chosen alternative belongs to a `{decisions.DECISION}`, and '
            f"this proposal declares "
            f"{(refs or {}).get(decisions.CLASSIFICATION_KEY) or 'no classification'}. "
            "Nothing is inferred — amend it with `estate proposal amend "
            f"--classification {decisions.DECISION} ...` if that is what it is")
    options = alternatives_of(refs)
    if len(options) < decisions.MIN_ALTERNATIVES:
        # NOT a bypass: a decision whose alternatives cannot be read cannot be
        # chosen among, and saying so is a different refusal from "you forgot
        # to pick". The repair is one command and it is named.
        raise ChoiceRefused(
            f"this proposal declares `{decisions.DECISION}` but carries "
            f"{len(options)} readable alternative(s); a choice among them is "
            "not expressible. Repair the record with `estate proposal amend "
            "... --alternative ...` first", ALTERNATIVES_KEY)
    if not isinstance(position, int) or isinstance(position, bool):
        raise ChoiceRefused(
            f'"{CHOSEN_KEY}" must be the 1-based position of the alternative '
            f"that was picked (got {position!r})")
    if not 1 <= position <= len(options):
        raise ChoiceRefused(
            f'"{CHOSEN_KEY}" is {position}; this proposal has {len(options)} '
            f"alternatives, so the position must be 1..{len(options)}")
    entry = options[position - 1]
    if not isinstance(entry, dict):
        raise ChoiceRefused(
            f"alternative {position} is not an "
            f"{{{OPTION_KEY}, {CONSEQUENCE_KEY}}} object", ALTERNATIVES_KEY)
    return {
        POSITION_KEY: position,
        OPTION_KEY: (entry.get(OPTION_KEY) or "").strip(),
        CONSEQUENCE_KEY: (entry.get(CONSEQUENCE_KEY) or "").strip(),
        # The whole set, frozen. This is the amendment-safety, and it is why
        # the record is on the event and not on the row.
        ALTERNATIVES_KEY: [dict(o) if isinstance(o, dict) else {OPTION_KEY: str(o)}
                           for o in options],
    }


def missing_choice_message(refs) -> str:
    """What an approval of a decision-classified proposal is refused with."""
    options = alternatives_of(refs)
    listed = "; ".join(
        f"{n}. {(o.get(OPTION_KEY) if isinstance(o, dict) else o)}"
        for n, o in enumerate(options, start=1))
    return (
        f"this proposal is classified `{decisions.DECISION}`; approving it "
        f'records WHICH alternative was chosen. Pass the 1-based position as '
        f'--chosen N. The alternatives on file are: {listed or "(none readable)"}. '
        "Approving without one is what put the answer in prose and left "
        "the hub re-asking a question already answered")


def choice_of(event_refs):
    """The chosen record on one event's refs, or None."""
    if not isinstance(event_refs, dict):
        return None
    value = event_refs.get(CHOSEN_KEY)
    return value if isinstance(value, dict) else None


def choice_line(record) -> str:
    """One line naming the branch taken, for a CLI and for `proposal show`."""
    if not isinstance(record, dict):
        return ""
    total = len(record.get(ALTERNATIVES_KEY) or [])
    of = f" of {total}" if total else ""
    return (f"option {record.get(POSITION_KEY)}{of}: "
            f"{record.get(OPTION_KEY) or '(not recorded)'}")


# --------------------------------------------------------------------------- #
# the pickup marker
# --------------------------------------------------------------------------- #

# The refs key on an event that records a route. One key, one object, same
# shape rule as CHOSEN_KEY.
PICKUP_KEY = "pickup"
ROUTE_KEY = "route"
REF_KEY = "ref"

# The routes this estate actually has for an approved proposal. THREE
# executors and one non-executor, and the list is short on purpose: an
# outcome that cannot be reached is not an outcome that has not been reached
# (bin/outcome-coverage's own rule), so a route nothing in this estate can
# actually walk would be a bucket that reads `never` forever.
#
# `night-shift`  the pr-reviewer overnight queue (`review.py enqueue-task`).
#                THE ONLY ROUTE THAT EXISTED BEFORE THIS, which is the whole
#                of the executor half: t-777 was approved 2026-08-17 with a
#                one-line note saying "queue the extraction:6 resync for
#                tonight", is an extraction re-sync rather than
#                overnight-shaped work, and has sat `ready` ever since with
#                nine mechanic notes on top of it.
# `dispatch`     an ephemeral worker through bin/dispatches — the on-demand
#                loop dispatch (extractor, eng-health), a Forge dispatch, a
#                panel, a ci-fix. This is t-777's real shape: SKILL.md's
#                **On-demand loop dispatch** launches a worktree-isolated
#                one-shot with `dispatches add --kind extractor`, because
#                `interval = "on-demand"` means the extractor has no session
#                to hand anything to.
# `hub`          the hub did it inline on this tick. Small, already-specified
#                work with nothing to hand off.
# `parked`       NOT an executor. The owner or the hub deliberately leaving it, and
#                the sweep must stop reporting it. The completion check names
#                this exception in so many words ("other than rows
#                deliberately parked"), so it is a recorded fact carrying its
#                own reason rather than a filter somebody remembers to apply.
NIGHT_SHIFT, DISPATCH, HUB, PARKED = (
    "night-shift", "dispatch", "hub", "parked")
ROUTES = (NIGHT_SHIFT, DISPATCH, HUB, PARKED)
EXECUTOR_ROUTES = tuple(r for r in ROUTES if r != PARKED)

# The night shift's own key, written onto the enqueue event by
# review.py's `estate_adopt`. Read, never written, by this module.
QUEUE_KEY = "queue_key"

PICKUP_SUMMARY = "decision picked up"
PICKUP_KIND = "pickup"


def pickup_record(route: str, note: str, ref: str = "") -> dict:
    if route not in ROUTES:
        raise ChoiceRefused(
            f"--route must be one of {', '.join(ROUTES)} (got {route!r})",
            ROUTE_KEY)
    text = (note or "").strip()
    if not text:
        # A marker nobody can read back is not a record. Same rule as
        # `estate verified`'s required evidence and `dispatches resolve
        # --verified`'s: the sentence is the caller's, never derived from the
        # fact that a command ran.
        raise ChoiceRefused(
            "a pickup says in one line what took this on; an unexplained "
            "marker only makes the row stop being reported", PICKUP_KEY)
    record = {ROUTE_KEY: route}
    if (ref or "").strip():
        record[REF_KEY] = ref.strip()
    return record


def pickup_of(event_refs):
    """The pickup record on one event's refs, or None."""
    if not isinstance(event_refs, dict):
        return None
    value = event_refs.get(PICKUP_KEY)
    if not isinstance(value, dict):
        return None
    return value if value.get(ROUTE_KEY) in ROUTES else None


def has_queue_key(event_refs) -> bool:
    return isinstance(event_refs, dict) and bool(event_refs.get(QUEUE_KEY))


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #

PICKED_UP, PARKED_V, OWED, FRESH, UNOBSERVABLE = (
    "picked_up", "parked", "owed", "fresh", "unobservable")
VERDICTS = (OWED, FRESH, PICKED_UP, PARKED_V, UNOBSERVABLE)

# The store's own parking states. A row at `needs-owner` IS waiting on a person
# (docs/attention-contract.md says so, and it is already in the attention set);
# a row at `blocked` is waiting on its blocker. Reporting either as unpicked-up
# would file the same item twice under two names.
PARKED_STATUSES = ("needs-owner", "blocked")

# One hub tick, doubled. `bin/ops` starts the hub with a bare `/loop /hub` and
# state/hub/pace defaults to 600s, so a tick is ten minutes — and a decision
# recorded halfway through one is not owed until the NEXT tick could have seen
# it. The completion check's "no row older than one hub tick" is measured
# against this, and `--within` overrides it for a caller who paces differently.
GRACE_MINUTES = 20.0


def verdict(*, status: str, decided_at, pickups: list, queued: bool,
            claimed: bool, now, grace_minutes: float = GRACE_MINUTES) -> dict:
    """One approved proposal's pickup state.

    `pickups` are the pickup RECORDS after the decision, oldest first; `queued`
    and `claimed` are the two derived signals. The order below is the order the
    facts override each other, and it is deliberate:

      an explicit marker wins        somebody said what happened to this row,
                                     and that is stronger than any state we
                                     inferred — including a later re-route of
                                     a row that had been parked
      then the store's own parking   `needs-owner` and `blocked` are already
                                     recorded decisions about who owns the row
      then the derived signals       the night shift's queue key, then a claim
      then the clock                 and only here can a row be accused

    `unobservable` is checked FIRST and can never soften into `owed`: a row
    whose decision instant cannot be read has an unknown age, and an accusation
    with no clock behind it is the failure this estate writes contracts about.
    """
    if decided_at is None:
        return {"verdict": UNOBSERVABLE, "age_hours": None,
                "why": "no event records how this reached approved-backlog, "
                       "so there is no instant to measure from"}
    age = (now - decided_at).total_seconds() / 3600.0
    latest = pickups[-1] if pickups else None
    if latest is not None:
        route = latest.get(ROUTE_KEY)
        if route == PARKED:
            return {"verdict": PARKED_V, "age_hours": age, "route": PARKED,
                    "why": "deliberately parked"}
        return {"verdict": PICKED_UP, "age_hours": age, "route": route,
                "why": f"routed to {route}"}
    if status in PARKED_STATUSES:
        return {"verdict": PARKED_V, "age_hours": age, "route": None,
                "why": f"task status is '{status}'"}
    if queued:
        return {"verdict": PICKED_UP, "age_hours": age, "route": NIGHT_SHIFT,
                "why": "an event since the decision carries a queue_key"}
    if claimed:
        return {"verdict": PICKED_UP, "age_hours": age, "route": None,
                "why": "the task was claimed after the decision"}
    if age * 60.0 > grace_minutes:
        return {"verdict": OWED, "age_hours": age, "route": None,
                "why": "decided, and nothing has picked it up since"}
    return {"verdict": FRESH, "age_hours": age, "route": None,
            "why": "decided within the last "
                   f"{grace_minutes:g}m; not owed yet"}


def exit_status(verdicts) -> int:
    """3 owed, 2 unobservable, 0 clean.

    `owed` wins, the same precedence bin/skill-drift's own exit_status uses:
    a run with both has something to do about the first and something to fix
    about the second, and the caller that reads only the code has to be told
    about the actionable one.
    """
    seen = list(verdicts)
    if OWED in seen:
        return 3
    if UNOBSERVABLE in seen:
        return 2
    return 0


def parse_iso(value):
    """A stored stamp as an aware datetime, or None. Never raises."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        out = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=dt.timezone.utc)

"""The rate at which work arrives for the owner (estate vision V-05).

A count says how deep the queue is. It cannot say whether the queue is filling,
draining, or holding — and "is it filling" is the question a component that
doubled its output is only visible in.

WHAT THIS IS FOR
----------------
Several standing producers, most of them aimed at one person, none of them
aware of the others' output. Every producer is accountable for its own
footprint and nothing owns the total. `bin/estate attention` (P-07) answers
"how much is waiting on a person RIGHT NOW"; nothing answered "how fast is it
coming, and from whom".

This module is the second question, and it is a QUERY over rows that already
exist, on exactly P-07's terms: an arrival view that needs its own table is a
second store to keep in sync, not a view. Nothing here writes anything.

WHAT AN ARRIVAL IS
------------------
One moment: a tracked item ENTERED a state that waits on the owner. Three classes,
because the estate has three such states and they are produced by different
things at different rates:

  followup        a `kind='followup'` task was created. A follow-up is an
                  attention item from birth (estate_attention.ATTENTION_KINDS),
                  so its creation IS its arrival.
  needs-owner      a task moved to `needs-owner`. The estate's own word for work
                  that will not advance until the owner reads it.
  pending-review  a proposal entered `pending-review`. Not in P-07's attention
                  set, and deliberately counted here anyway: a proposal awaiting
                  a review decision is work waiting on the owner by construction, and
                  it is the specific queue V-05 was filed about. Kept as its own
                  class rather than folded into the others so that a reader who
                  wants only P-07's set can subtract it.

A task can arrive more than once. `needs-owner -> ready -> needs-owner` is two
arrivals and is counted as two, because each one costs the owner a fresh read. The
same is true of a proposal restaged out of `pending-review` and back.

WHY DEPARTURES ARE HERE TOO
---------------------------
Arrivals alone are the number that argues for a cap, and on their own they
argue for one badly. A review queue can climb steeply and be drained again
within days; an arrival series with no service rate beside it shows the climb
and hides the drain. Departures cost
three more queries and stop the single most likely misreading of the first
three, so they are not a separate feature.

THE OBSERVABILITY FLOOR, AND THE ONE PREDICATE
----------------------------------------------
Each class has a FLOOR: the first moment it ever recorded an arrival. Below
that floor a zero means the recording mechanism did not exist, not that nothing
arrived. `pending-review` has no rows before the P-02 cutover that
introduced the stage — not because no proposal was waiting before it, but
because the stage did not exist to record it. The word is `unobservable`, borrowed unchanged from
estate_runs (P-08), which drew the same distinction about scheduled runs and
should not have to draw it twice under two names.

The period containing a floor is worse than empty, it is MIXED: it holds part
of a period's real arrivals plus whatever the cutover migration backfilled in
one second — a migration that adopts proposals which had been waiting for days
stamps every one of them at the moment it ran. So a period is given four possible verdicts and only one of them supports
a comparison:

  observed         the whole period lies after the floor and the period ended.
  in_progress      the period has not ended. Its count is a fraction of a
                   period and will only grow.
  straddles_floor  the floor falls inside the period. Part real, part cutover,
                   and nothing here can separate them without reading prose.
  unobservable     the period ended at or before the floor.

  trend_is_evidence = both periods are `observed`

That is the whole policy, and it is the reason this module exists rather than a
SQL snippet in a script. V-05 asked for a ceiling on the standing proposal
set on the strength of one night's rise. A rise can be perfectly real and still
sit entirely below a class's floor, in which case `trend_is_evidence` is false
for every comparison anyone could make about it — and the query says so in a
field instead of leaving a reader to notice. A cap that evicts real findings
needs better than that, and this is the thing that can eventually supply it.

`straddles_floor` is reported ahead of `in_progress` when both are true,
because waiting fixes one of them and nothing fixes the other.

WHY THE WEEK STARTS WHERE IT DOES
---------------------------------
"This week" is a claim about the owner's calendar, so the bucket boundary is local
and comes from `estate_attention.local_zone()` — the same resolution `due` and
`attention` use, resolved once per command for the reason stated there. Weeks
start Monday, and the end of a bucket is computed from the NEXT bucket's local
midnight rather than by adding 7*24h, so a week containing a DST change is
still exactly seven local days.

Docs: none. This is a read-only query over the events and tasks tables, and it
introduces no state anything else has to keep in sync.
"""
from __future__ import annotations

import datetime as dt

import estate_attention

# ── the three arrival classes ────────────────────────────────────────────────
FOLLOWUP = "followup"
NEEDS_OWNER = "needs-owner"
PENDING_REVIEW = "pending-review"

CLASSES = (FOLLOWUP, NEEDS_OWNER, PENDING_REVIEW)

# ── what an arrival is, as SQL ───────────────────────────────────────────────
# Stated here and nowhere else so the definition cannot drift between a caller
# that counts and a caller that lists. Each returns (ts, producer, task_id).
#
# The producer of an arrival is whoever caused it: the task's `created_by` for
# a follow-up, the event's `actor` for a transition. That is the field V-05's
# "a component that starts producing twice as much" is a claim about.
ARRIVALS = {
    FOLLOWUP: """
        SELECT created_at AS ts, created_by AS producer, id AS task_id
          FROM tasks WHERE kind='followup'""",
    # `kind='transition'` and not a bare summary match: several notes and
    # activity rows quote the phrase in prose, and a LIKE over every row would
    # count a hub status update as an escalation.
    NEEDS_OWNER: """
        SELECT ts, actor AS producer, task_id
          FROM events
         WHERE kind='transition' AND summary LIKE '%-> needs-owner'""",
    # Two ways in, and both are arrivals: filed straight into the stage, or
    # restaged back into it from a decision that was later taken back.
    PENDING_REVIEW: """
        SELECT ts, actor AS producer, task_id
          FROM events
         WHERE (kind='activity' AND summary='proposal filed (pending-review)')
            OR (kind='transition' AND summary LIKE 'stage:%-> pending-review')""",
}

# The matching exits. A follow-up leaves the set by going terminal; the other
# two leave by transitioning out of the state that named them.
#
# A departure's `producer` is whoever the transition was recorded under, which
# for a follow-up resolved through the shim is its CREATOR rather than whoever
# resolved it (bin/followups passes `row["created_by"]`). Nothing renders
# departures by producer for exactly that reason.
DEPARTURES = {
    # Three words for one exit, and all three are real. `done` and `dropped`
    # are the store's; `resolved` is `bin/followups resolve`, which sets the
    # status to `done` but records the event in the shim's own vocabulary — 35
    # of the 38 follow-ups ever closed left that way, so a query that reads
    # only the store's two words reports a queue that never drains.
    #
    # `stage: ...` rows are excluded: a follow-up-kind task adopted into the
    # proposal lifecycle has stage moves ending in `resolved` too, and those
    # are a review decision about a proposal, not the follow-up being answered.
    FOLLOWUP: """
        SELECT e.ts AS ts, e.actor AS producer, e.task_id AS task_id
          FROM events e JOIN tasks t ON t.id = e.task_id
         WHERE t.kind='followup' AND e.kind='transition'
           AND e.summary NOT LIKE 'stage: %'
           AND (e.summary LIKE '% -> done' OR e.summary LIKE '% -> dropped'
                OR e.summary LIKE '% -> resolved')""",
    NEEDS_OWNER: """
        SELECT ts, actor AS producer, task_id
          FROM events
         WHERE kind='transition' AND summary LIKE 'needs-owner -> %'""",
    PENDING_REVIEW: """
        SELECT ts, actor AS producer, task_id
          FROM events
         WHERE kind='transition' AND summary LIKE 'stage: pending-review -> %'""",
}

# ── periods ──────────────────────────────────────────────────────────────────
WEEK = "week"
DAY = "day"
PERIODS = (WEEK, DAY)

# ── the four period verdicts ─────────────────────────────────────────────────
OBSERVED = "observed"
IN_PROGRESS = "in_progress"
STRADDLES_FLOOR = "straddles_floor"
UNOBSERVABLE = "unobservable"

VERDICTS = (OBSERVED, IN_PROGRESS, STRADDLES_FLOOR, UNOBSERVABLE)


def period_start(stamp: dt.datetime, period: str, zone: dt.tzinfo) -> dt.date:
    """The local calendar date a moment's bucket begins on."""
    day = stamp.astimezone(zone).date()
    if period == WEEK:
        return day - dt.timedelta(days=day.weekday())
    return day


def period_span(period: str) -> dt.timedelta:
    return dt.timedelta(days=7 if period == WEEK else 1)


def period_bounds(day: dt.date, period: str,
                  zone: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) as UTC instants.

    The end is the NEXT bucket's local midnight, not start + 7*24h: across a
    DST change those differ by an hour, and one of them stops being seven local
    days long.
    """
    start = dt.datetime.combine(day, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(day + period_span(period), dt.time.min, tzinfo=zone)
    return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)


def recent_periods(now: dt.datetime, period: str, count: int,
                   zone: dt.tzinfo) -> list[dt.date]:
    """The `count` most recent bucket starts, oldest first, ending with the one
    `now` falls in."""
    latest = period_start(now, period, zone)
    span = period_span(period)
    return [latest - span * n for n in range(count - 1, -1, -1)]


def classify_period(day: dt.date, period: str, floor, now: dt.datetime,
                    zone: dt.tzinfo) -> str:
    """One period's verdict for one class. A member of VERDICTS.

    `floor` is that class's first recorded arrival as an aware datetime, or
    None when it has never recorded one — in which case every period is
    `unobservable`, because a store with no rows of a kind cannot tell "none
    arrived" from "nothing here writes these".
    """
    start, end = period_bounds(day, period, zone)
    if floor is None or end <= floor:
        return UNOBSERVABLE
    if start < floor:
        return STRADDLES_FLOOR
    if end > now:
        return IN_PROGRESS
    return OBSERVED


def trend_is_evidence(current: str, previous: str) -> bool:
    """Whether a delta between two periods says anything about a trend.

    The one predicate. Both periods must be whole and both must lie after the
    floor; every other combination is a number with no claim attached to it.
    """
    return current == OBSERVED and previous == OBSERVED


def compare(current: int, previous: int, current_verdict: str,
            previous_verdict: str) -> dict:
    """This period against the one before it, and whether that is evidence.

    `pct` is None when the previous count was zero — a rise from nothing has no
    percentage, and reporting one as infinite or as 100% invents a scale.
    """
    change = current - previous
    return {
        "current": current,
        "previous": previous,
        "change": change,
        "pct": (None if not previous else round(100.0 * change / previous, 1)),
        "trend_is_evidence": trend_is_evidence(current_verdict, previous_verdict),
        "current_verdict": current_verdict,
        "previous_verdict": previous_verdict,
    }


def last_comparable(verdicts) -> int | None:
    """The index of the newest period that can be compared with the one before
    it, or None.

    "This week" is what the question asks for and is almost never the answer:
    for most of a week the current bucket is hours old, and a five-hour week
    against a seven-day one is a 90% "drop" every Monday morning. The newest
    pair of `observed` periods is the comparison that carries a claim, so it is
    computed here rather than left to each reader to find by eye.
    """
    for i in range(len(verdicts) - 1, 0, -1):
        if trend_is_evidence(verdicts[i], verdicts[i - 1]):
            return i
    return None


def parse_classes(value) -> list[str]:
    """A --class argument (comma-separated, repeatable) as a checked list.

    Raises ValueError naming the three, because a typo'd class that silently
    matched nothing would read exactly like "nothing arrived".
    """
    out: list[str] = []
    for chunk in (value if isinstance(value, (list, tuple)) else [value]):
        for part in str(chunk or "").split(","):
            name = part.strip()
            if not name:
                continue
            if name not in CLASSES:
                raise ValueError(
                    f"unknown arrival class {name!r}. The three are: "
                    + ", ".join(CLASSES))
            if name not in out:
                out.append(name)
    return out


def bucket(rows, period: str, zone: dt.tzinfo) -> dict:
    """(ts, producer) pairs into {bucket_date: {"total": n, "by_producer": {}}}.

    `rows` are (aware datetime, producer) pairs. Buckets with nothing in them
    are absent; the caller supplies the period list it wants rendered, because
    only the caller knows how far back to claim a zero.
    """
    out: dict[dt.date, dict] = {}
    for stamp, producer in rows:
        key = period_start(stamp, period, zone)
        slot = out.setdefault(key, {"total": 0, "by_producer": {}})
        slot["total"] += 1
        name = producer or "(unattributed)"
        slot["by_producer"][name] = slot["by_producer"].get(name, 0) + 1
    return out


def floor_of(rows):
    """The earliest moment in `rows`, or None. That class's observability floor."""
    stamps = [stamp for stamp, _ in rows]
    return min(stamps) if stamps else None


def parse_iso(value):
    """Delegated so arrivals and attention normalize a stored stamp the same
    way; a second normalizer is a second set of edge cases."""
    return estate_attention.parse_iso(value)

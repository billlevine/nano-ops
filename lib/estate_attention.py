"""Unresolved attention, and how soon it is due (gap audit P-07).

P-31 gave a task an optional `due_at`. `bin/estate due` answers one question
with it — "is the clock running out inside my window?" — and it answers it with
a number of days. That is enough to *sort* by and not enough to *select* by,
which is the gap three scenarios fall through:

  S24  the daily summary includes the unresolved attention item, the overdue
       follow-up, and the follow-up inside the advance-notice interval, and
       does NOT falsely mark the one outside that interval as currently due.
  S31  optional due dates are accepted; inside-interval and overdue follow-ups
       are discoverable in the appropriate category; boundary changes are
       reflected; undated and later items are never classified as overdue.
  S52  a background result needing attention stays discoverable as an
       unresolved item until somebody resolves it — including in the next
       daily summary, not just the tick it arrived on.

WHAT THIS MODULE IS
-------------------
Two vocabularies and no I/O, in one place, so the store (`bin/estate`), the
follow-up shim (`bin/followups`), the briefer's gather, and the migration that
reconciles history cannot disagree about either.

  1. THE FIVE DUE STATES. A derived reading of one `due_at` against one
     instant, under one explicit notice interval.
  2. THE ATTENTION SET. Which rows in the shared store are "somebody must look
     at this and nothing else will move it".

THE FIVE DUE STATES
-------------------
  undated    no `due_at`. Not "not due soon" — unanswered. An item can be the
             most important thing in the estate and carry no date, so this is
             a first-class category and never a synonym for "ignore".
  later      dated, and beyond the notice interval. Known about, not yet the
             advance-notice window's business. S24's load-bearing negative:
             a `later` item must never be reported as currently due.
  due_soon   dated, inside the notice interval, on a later local day.
  due_today  dated, still ahead, and falling on TODAY's local calendar date.
  overdue    the deadline has passed.

Ordered most-urgent-first in DUE_STATES, so a consumer that renders them in
order needs no policy of its own.

WHY THE DAY BOUNDARY IS LOCAL AND THE INTERVAL IS NOT
-----------------------------------------------------
"Due today" is a claim about the owner's calendar, and their calendar is local. A
deadline at 20:00 EDT is 00:00 UTC the next day; classifying it as tomorrow
because the store speaks UTC would tell him a thing due tonight is not.

The notice interval is a DURATION and durations have no timezone, so it is
plain elapsed time. The two are different kinds of measurement and are computed
differently on purpose: `due_today` compares calendar dates in `tz`, everything
else compares instants.

`parse_due` already lands a bare date at 23:59:59 UTC (P-31: "due Friday" means
Friday is still on time). That is a UTC end-of-day, so west of UTC a bare date
reads as *tomorrow* locally for the last hours of its own day. It is left
exactly as it is — changing what a stored date MEANS would silently move every
deadline already on file, which is a migration, not a query. `due_today` is
computed from whatever instant is stored, and `overdue` still only fires once
that instant has actually passed.

UNREADABLE IS NOT ONE OF THE FIVE
---------------------------------
A `due_at` that will not parse is a sixth answer, and it has to be, because
none of the five is true of it. `undated` would claim nobody set a date;
`overdue` and `later` would both invent a fact about a date nobody can read.
`due_rows` in bin/estate already takes this position for its `days_left`
(unreadable sorts to the top, never drops out), and this keeps it: `unreadable`
sorts first, ahead of `overdue`, because "we cannot tell" needs a human either
way.

THE ATTENTION SET
-----------------
An attention item is a non-terminal row in the shared task store that is
waiting on a person:

  * an OPEN FOLLOW-UP (`kind='followup'`) — `bin/followups`' whole purpose is
    the durable home for work that stands until a human resolves it;
  * a task parked at `needs-owner` — the estate's own word for the same thing.

Nothing new is stored to make this true. The set is a QUERY over rows that
already exist, which is the point: P-07 asks for a persistent attention view,
and a view that needs its own table is a second store to keep in sync, not a
view. A dispatch that comes back needing the owner is filed as a follow-up (the
night shift's `needs-you` exit already routes there) and is therefore already
in this set the moment it is filed.

Docs: docs/attention-contract.md
"""
from __future__ import annotations

import datetime as dt
import os

# ── the five due states, most urgent first ───────────────────────────────────

OVERDUE = "overdue"
DUE_TODAY = "due_today"
DUE_SOON = "due_soon"
LATER = "later"
UNDATED = "undated"
# Not one of the five. See UNREADABLE IS NOT ONE OF THE FIVE above.
UNREADABLE = "unreadable"

DUE_STATES = (OVERDUE, DUE_TODAY, DUE_SOON, LATER, UNDATED)
ALL_STATES = (UNREADABLE,) + DUE_STATES

# Render/sort order: a consumer walking this list needs no ordering policy.
STATE_ORDER = {state: n for n, state in enumerate(ALL_STATES)}

# The states an advance-notice consumer is entitled to call "currently due".
# `later` and `undated` are deliberately absent — that is S24's negative, and
# it is a constant here rather than a condition each consumer re-derives.
NOTICE_STATES = (OVERDUE, DUE_TODAY, DUE_SOON)

# The default advance-notice interval, in days. Same number as bin/estate's
# DUE_DEFAULT_WITHIN_DAYS and the briefer's `due_within_days`, and deliberately
# so: "how far ahead do we warn" is one policy, not three.
NOTICE_DEFAULT_DAYS = 3.0

# ── the attention set ────────────────────────────────────────────────────────

# A task parked here is waiting on the owner by its own status.
ATTENTION_STATUSES = ("needs-owner",)
# A follow-up is an attention item for as long as it is open. `blocked` is NOT
# in either list: blocked work is waiting on another piece of WORK, and the
# thing that unblocks it is a dependency finishing, not a person reading it.
# `estate stale` and `estate ready` already speak for those.
ATTENTION_KINDS = ("followup",)
# Everything else is somebody's live work, or finished.
TERMINAL = ("done", "dropped")


def local_zone() -> dt.tzinfo:
    """The zone "today" is measured in.

    `ESTATE_TZ` (an IANA name) overrides, which is what makes the day boundary
    testable without moving the machine's clock. Without it, the machine's own
    local zone — the same call `brief.py local_now()` makes, so the briefer and
    the store agree on which day it is.

    Resolve it ONCE per command and pass it to `classify`, rather than letting
    each row re-resolve: the fallback returns the fixed offset in force right
    now, and a caller that re-derives it per row could straddle a DST change
    mid-loop and classify two rows under two different calendars. `ESTATE_TZ`
    returns a real zone and has neither problem, which is the reason to set it
    on a machine that cares about the twice-a-year hour.
    """
    name = (os.environ.get("ESTATE_TZ") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            # A zone nobody can load is not a reason to fail a read-only query;
            # fall through to local, exactly as an unreadable config would.
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def parse_iso(value):
    """A stored timestamp as an aware UTC datetime, or None if it is not one.

    Same normalization as `bin/estate parse_iso` (naive is treated as UTC),
    duplicated here rather than imported because this module must stay
    importable with no dependency on the CLI it serves.
    """
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def classify(due_at, now: dt.datetime, notice_days: float = NOTICE_DEFAULT_DAYS,
             tz: dt.tzinfo | None = None) -> str:
    """One `due_at` read against one instant. Returns a member of ALL_STATES.

    Pure: every input is an argument, so the boundary behaviour S31 asks about
    ("boundary changes are reflected") is tested by moving `now`, not by
    waiting.
    """
    text = "" if due_at is None else str(due_at).strip()
    if not text:
        return UNDATED
    deadline = parse_iso(text)
    if deadline is None:
        return UNREADABLE
    if deadline <= now:
        return OVERDUE
    zone = tz or local_zone()
    if deadline.astimezone(zone).date() == now.astimezone(zone).date():
        return DUE_TODAY
    if notice_days is None:
        return LATER
    return (DUE_SOON if deadline <= now + dt.timedelta(days=float(notice_days))
            else LATER)


def days_left(due_at, now: dt.datetime):
    """Days until the deadline, negative when it has passed; None if unreadable
    or unset. Rounded the same way `bin/estate due` already rounds it."""
    deadline = parse_iso(due_at) if str(due_at or "").strip() else None
    if deadline is None:
        return None
    return round((deadline - now).total_seconds() / 86400, 1)


def is_attention(kind: str, status: str) -> bool:
    """Whether one row is an unresolved attention item.

    Both halves check `status not in TERMINAL` because a resolved follow-up is
    not attention — it is history, and history that reads as a demand is how a
    daily brief teaches its reader to skip a section.
    """
    if (status or "") in TERMINAL:
        return False
    return (status or "") in ATTENTION_STATUSES or (kind or "") in ATTENTION_KINDS


def sort_key(state: str, left, seq) -> tuple:
    """Most urgent first, then soonest, then filing order.

    `left` is None for undated and unreadable rows, which sort by state alone —
    inside one state their order is the store's own, and that is the only
    ordering the store can honestly claim.
    """
    return (STATE_ORDER.get(state, len(ALL_STATES)),
            left if left is not None else 0, seq or 0)


def parse_states(value) -> list[str]:
    """A --state argument (comma-separated, repeatable) as a checked list.

    Raises ValueError naming the legal states, because a typo'd state that
    silently matched nothing would read exactly like "there is nothing due".
    """
    out: list[str] = []
    for chunk in (value if isinstance(value, (list, tuple)) else [value]):
        for part in str(chunk or "").split(","):
            name = part.strip()
            if not name:
                continue
            if name not in ALL_STATES:
                raise ValueError(
                    f"unknown due state {name!r}. The five are: "
                    + ", ".join(DUE_STATES)
                    + f" (plus {UNREADABLE} for a date that will not parse)")
            if name not in out:
                out.append(name)
    return out

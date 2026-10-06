"""What each dashboard PANEL could actually see.

Companion doc: `docs/dashboard-panel-contract.md`. The rule itself is not here —
it is `lib/estate_observation.py`, and this module is the DASHBOARD'S SPELLING
of it, exactly as `lib/brief_manifest.py` is the briefer's.

WHAT THIS FIXES
---------------
P-11 built a genuinely good three-axis presentation of "did this source
answer", and it works — for the Daily Digest's ten chips and nothing else.
Every other panel folded a missing or unreadable producer store into a
confident zero:

    state/pr-tracker/state.json absent  →  {}  →  going astray 0 · waiting on
                                                  you 0 · your review 0 · on
                                                  reviewers 0 · in progress 0

Which is the same page a genuinely clear board renders, with no banner of any
kind to tell the two apart. `bin/dashboard`'s `read_json` returns `None` for a
file that is missing, a file that is unreadable and a file full of garbage, and
every builder then wrote `or {}` after it. That `or {}` is the bug, and it is
in nine places.

THE THREE STATES A PANEL MUST KEEP APART
----------------------------------------
    read, and empty      the producer answered and there is nothing to show
    read, and old        the producer answered a while ago; the numbers are
                         real but they describe an earlier estate
    not read at all      the store is absent, unreadable, or the tool that
                         produces it never ran

The first two are facts about the estate. The third is a fact about us, and
rendering it as the first is how a dashboard tells the operator their board is clear when
nobody has managed to look at it.

WHAT A PANEL ENVELOPE CARRIES
-----------------------------
The same shape the brief's source envelopes carry, because the page renders
both with the same chip markup and the same browser-side freshness ticker:

    panel / source     which panel, and what it read
    outcome / warrant  the six words below, and the class they map to
    status             completed | failed | unavailable (the three-way dot)
    empty_is_evidence  the predicate, DERIVED — never passed in
    staleness          as_of, age, the budget, and current/stale/unknown

STALENESS IS A SECOND AXIS, NOT A SECOND STATUS
-----------------------------------------------
A panel that read successfully can still be showing an hour-old board, and a
panel that could not read has no age at all. Keeping them as two marks is what
lets the page say "this is real but old" without ever implying "this broke".
`as_of` is when the data was TRUE (the producer's own stamp where it writes
one, its file mtime otherwise) and never when we happened to read it.

WHY THE PREDICATE IS NOT A BOOLEAN A CALLER SETS
-------------------------------------------------
`envelope()` has no `empty_is_evidence` parameter, the same way
`estate_observation.reading()` has none. A builder decides an OUTCOME, which is
the only thing it knows; the class, the status and the predicate are all
derived from the one registered table. An outcome word this module has not
registered raises rather than defaulting to "observed" — which is precisely
what the original bug looked like in code.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import estate_observation as O
# The three statuses and the freshness vocabulary are shared with the brief's
# source strip DELIBERATELY. They are the presentation half — the `.srcchip
# .s-* .f-*` CSS and the browser-side age ticker are one implementation serving
# both — so a second copy here would be two names for one dot. The WARRANT half
# comes from the registry above, where it is per-subsystem on purpose.
from brief_manifest import (COMPLETED, FAILED, UNAVAILABLE, STATUSES,  # noqa: F401
                            CURRENT, STALE, UNKNOWN, freshness_state)

VERSION = 1

# ── outcomes: the dashboard's own words for WHY a panel is in its status ─────
OUT_OK = "ok"                        # read it, and what it says is what is there
OUT_ERROR = "error"                  # the read raised, or the tool exited non-zero
OUT_UNREADABLE = "unreadable"        # the file is there and would not parse
OUT_NO_STATE = "no_state"            # the producer has never written its file
OUT_NO_TOOL = "no_tool"              # the command is not there / would not exec
OUT_NOT_CONFIGURED = "not_configured"  # no loop in this estate produces it

OUTCOMES = (OUT_OK, OUT_ERROR, OUT_UNREADABLE, OUT_NO_STATE, OUT_NO_TOOL,
            OUT_NOT_CONFIGURED)

# The three statuses as a projection of the four warrant classes — identical to
# the briefer's projection, and derived from the registered table rather than
# written out, so adding a seventh outcome means classing it in
# lib/estate_observation.py and there is no way to skip that step here.
STATUS_FOR_WARRANT = {
    O.OBSERVED: COMPLETED,
    O.FAILED: FAILED,
    O.NOT_ATTEMPTED: UNAVAILABLE,
    O.NOT_DUE: UNAVAILABLE,
}
STATUS_FOR_OUTCOME = {outcome: STATUS_FOR_WARRANT[warrant]
                      for outcome, warrant in O.DASHBOARD.outcomes.items()}

# ── the panels ───────────────────────────────────────────────────────────────
# Every region of the page that renders a producer's data. The order is the
# order they appear on the primary page.
PANELS = ("today", "loops", "dispatch_sessions", "night", "focus", "pr_board",
          "mechanic", "brief", "ledger", "estate_activity")

PANEL_LABEL = {
    "today": "Today list",
    "loops": "loops",
    "dispatch_sessions": "dispatch sessions",
    "night": "night-shift queue",
    "focus": "focus projects",
    "pr_board": "PR board",
    "mechanic": "mechanic report",
    "brief": "daily digest",
    "ledger": "recent activity",
    "estate_activity": "estate store",
}

# How old a panel's data may be before the page should say so. Three shapes:
#
#   * A LIVE read (the deck, the estate store) is true as of the snapshot, so
#     its budget is really "how long may this page go without a regeneration" —
#     five minutes against a 45s regenerator, loose enough not to flap.
#   * A SNAPSHOT somebody else generates gets that producer's own cadence. The
#     PR board's 90 minutes is the same spotter-stale threshold the brief
#     already uses (lib/brief_manifest.MAX_AGE_SECONDS), kept equal on purpose:
#     the two panels describe the same board and must not disagree about when
#     it went old.
#   * A NIGHTLY artifact gets 36 hours, which is one missed night and not two.
#
# The night-shift queue is the odd one: its stamp moves when the queue CHANGES,
# not on every tick, so a quiet week is not a fault. Its budget says only that
# a queue nothing has touched in a week is worth a glance.
MAX_AGE_SECONDS = {
    "loops": 300, "dispatch_sessions": 300, "estate_activity": 300,
    "today": 300,
    "pr_board": 90 * 60,
    "night": 7 * 86400,
    "focus": 36 * 3600, "mechanic": 36 * 3600, "brief": 36 * 3600,
    "ledger": 24 * 3600,
}
DEFAULT_MAX_AGE_SECONDS = 24 * 3600


def max_age_for(panel: str) -> int:
    return MAX_AGE_SECONDS.get(panel, DEFAULT_MAX_AGE_SECONDS)


def parse_stamp(value) -> float | None:
    """An ISO stamp or epoch number as an epoch float. None if it is neither.

    An unparseable stamp is not an error and never becomes a warrant: it makes
    the AGE unknown, which is the honest answer and the one the chip renders as
    a dash.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return (datetime.fromtimestamp(epoch, tz=timezone.utc)
            .isoformat(timespec="seconds"))


def envelope(panel: str, source: str, outcome: str, *, partial: bool = False,
             as_of=None, now: float | None = None, count=None,
             why: str | None = None, detail=None) -> dict:
    """One panel's reading, with its warrant attached. The only constructor.

    Note what this signature does NOT accept: `empty_is_evidence`, `status`, or
    `warrant`. All three are derived from `outcome` on every call, so a builder
    cannot claim a warrant its read does not have.
    """
    read = O.reading("dashboard", source, outcome, partial=partial,
                     at=iso(parse_stamp(as_of)), why=why, detail=detail)
    at = parse_stamp(as_of)
    # An age is only meaningful for data we actually hold. A failed or
    # never-attempted read has nothing whose age could be measured, so it gets
    # `unknown` rather than the age of the file it could not use.
    if read["warrant"] != O.OBSERVED:
        at = None
    age = None if (at is None or now is None) else max(0.0, now - at)
    budget = max_age_for(panel)
    return {
        "panel": panel,
        "label": PANEL_LABEL.get(panel, panel),
        "source": source,
        "outcome": outcome,
        "warrant": read["warrant"],
        "status": STATUS_FOR_OUTCOME[outcome],
        "partial": read["partial"],
        "empty_is_evidence": read["absence_is_evidence"],
        "count": count,
        "why": why,
        "detail": detail,
        "staleness": {
            "as_of": iso(at),
            "age_seconds": age,
            "max_age_seconds": budget,
            "state": freshness_state(age, budget),
        },
    }


def empty_is_evidence(env: dict) -> bool:
    """May a reader take this panel's empty result as "nothing to show"?

    Recomputed from the stored warrant rather than trusted from the stored
    field, so a hand-edited or older `dashboard.json` cannot claim one it was
    not given. Both recorded halves have to agree, exactly as
    `brief_manifest.empty_is_evidence` requires: an envelope claiming
    `completed` over an `unreadable` has no coherent warrant, and a reading
    with no coherent warrant is not evidence.
    """
    if not isinstance(env, dict):
        return False
    if env.get("partial") or env.get("status") != COMPLETED:
        return False
    outcome = env.get("outcome")
    if outcome in O.DASHBOARD.outcomes:
        return O.DASHBOARD.absence_is_evidence(outcome)
    return True


def is_stale(env: dict) -> bool:
    """Read successfully, but the data is older than this panel's budget.

    Deliberately independent of the predicate above: staleness is a claim about
    WHEN, and only a panel that read at all can make one.
    """
    if not isinstance(env, dict) or not empty_is_evidence(env):
        return False
    return (env.get("staleness") or {}).get("state") == STALE


def blind(panels: dict) -> list:
    """The panels a reader must not conclude an absence from, worst first.

    Worst first for the reason the primitive orders `unwarranted` that way: a
    reader who stops after the first line should have been told about the thing
    that broke, not the thing that was skipped.
    """
    rank = {FAILED: 0, UNAVAILABLE: 1, COMPLETED: 2}
    bad = [e for e in (panels or {}).values()
           if isinstance(e, dict) and not empty_is_evidence(e)]
    return sorted(bad, key=lambda e: (rank.get(e.get("status"), 3),
                                      str(e.get("panel"))))


def counts(panels: dict) -> dict:
    envs = [e for e in (panels or {}).values() if isinstance(e, dict)]
    out = {status: 0 for status in STATUSES}
    for env in envs:
        if env.get("status") in out:
            out[env["status"]] += 1
    out["partial"] = sum(1 for e in envs if e.get("partial"))
    out["stale"] = sum(1 for e in envs if is_stale(e))
    out["total"] = len(envs)
    return out


# ── reading a producer store WITH its warrant ────────────────────────────────
# `bin/dashboard.read_json` answers None for a file that is missing, a file
# that is unreadable and a file full of garbage. Those are three different
# mornings and the whole point of this module is that the page can tell them
# apart, so a panel reads its store through here instead.

def read_producer_json(path) -> tuple:
    """(data, outcome, why) for a JSON producer store."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, OUT_NO_STATE, f"{path.name} has never been written"
    except OSError as err:
        return None, OUT_ERROR, f"{path.name} could not be read: {err}"
    try:
        return json.loads(raw), OUT_OK, None
    except (json.JSONDecodeError, ValueError) as err:
        return None, OUT_UNREADABLE, f"{path.name} would not parse: {err}"


def read_producer_text(path) -> tuple:
    """(text, outcome, why) for a text/markdown producer store."""
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8"), OUT_OK, None
    except FileNotFoundError:
        return None, OUT_NO_STATE, f"{path.name} has never been written"
    except OSError as err:
        return None, OUT_ERROR, f"{path.name} could not be read: {err}"


def mtime(path) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None

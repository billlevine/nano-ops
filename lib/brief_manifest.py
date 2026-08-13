"""The morning brief's source manifest — what each gatherer actually saw.

Gap audit P-11 (scenario S11). Companion doc: docs/summary-manifest-contract.md.
The tenth source, `background`, arrived with P-13 —
docs/background-activity-contract.md.

WHAT THIS FIXES
---------------
Every one of the briefer's gatherers answered a broken fetch the same way
it answered "there is genuinely nothing there": an empty list and a section the
composer read as a quiet morning.

    gh pr list … → non-zero  →  []      # GitHub is down
    gh pr list … → "[]"      →  []      # nothing landed overnight

That is P-03's bug one layer up. P-03 stopped the SPOTTER from believing an
outage; the brief that reads the spotter, GitHub, focus.json and five estate
queries had the same hole in eight more places, and the dashboard made it worse
by re-reading the raw producer state on its own schedule — so the panel and the
summary could disagree about what happened with nothing on file to say which
one was looking at a hole.

THE RULE
--------
    "Nothing happened" is a conclusion only a COMPLETED, undegraded source may
    reach. A failed or unavailable one is empty because nobody looked.

`empty_is_evidence` is that sentence as a field, and it is the one thing a
consumer branches on before describing a section as quiet.

THE THREE STATUSES
------------------
    completed    the fetch ran and its answer can be trusted, INCLUDING when
                 the answer is empty
    failed       it ran and broke (non-zero, unparseable, timed out)
    unavailable  it never ran (no tool, no state file, offline by request)

`failed` and `unavailable` are kept apart for the reason P-08 keeps `failed`
apart from `missing`: one is a thing that broke and one is a thing nobody
asked, and a single word for both hides whichever matters this morning.

`degraded` rides alongside: a source that answered, but only partly — three of
four `gh` calls returned, the spotter's own board carries an unreadable source
(P-03). It is `completed`, because what it saw is real, and it is not evidence
of an absence, because the part nobody read is exactly where an absence would
hide.

WHERE THE RULE ITSELF LIVES
---------------------------
Not here. P-03 and P-11 wrote the same rule twice — a failed look and an empty
one are different facts — and V-02 lifted it out to `lib/estate_observation.py`
so a third subsystem inherits it instead of being bitten into it. This module
is now the BRIEFER'S SPELLING of that rule: it owns the seven outcome words the
operator reads and the three statuses the source strip renders, and it takes
the classification and the predicate from the primitive. `STATUS_FOR_OUTCOME`
below is derived, not written down, so the two cannot disagree about what
`timeout` means.

WHAT THIS MODULE IS
-------------------
The vocabulary, the freshness budgets, and the READER — the half bin/dashboard
uses to render the brief's source panel from the manifest instead of
re-deriving freshness from raw producer state. The WRITER is brief.py, which
keeps checked copies of these constants rather than importing this file, for
the same reason every loop engine does (lib/estate_ledger.py, WHY LOOPS DO NOT
IMPORT THIS). `bin/test_subsystem_events.py` fails on any drift.

STALENESS IS DATA, NOT A VERDICT
--------------------------------
An envelope carries `as_of` (when its data was TRUE — a snapshot's own stamp,
not the moment we read it) and `max_age_seconds` (that source's budget). The
`state` recorded at write time is the answer at write time; a reader hours
later recomputes it from the same two numbers. That is what lets the dashboard
tick a chip from `current` to `stale` in the browser without opening
state/pr-tracker/state.json and reaching its own opinion.
"""
from __future__ import annotations

import json
from pathlib import Path

import estate_observation as O

VERSION = 1

# The directory, under the briefer's state dir, holding one manifest per local
# calendar date — the same date key P-10's publication record uses, so a
# morning's provenance and its delivery record are joined by the date and
# neither has to carry the other's fields.
DIRNAME = "manifests"

# ── the three statuses ───────────────────────────────────────────────────────
COMPLETED, FAILED, UNAVAILABLE = "completed", "failed", "unavailable"
STATUSES = (COMPLETED, FAILED, UNAVAILABLE)

# ── outcomes: WHY a status is what it is ─────────────────────────────────────
# More than one word for "broken", for the same reason the spotter's observation
# contract gives six: the operator reads these, and "gh is not installed" and
# "GitHub returned 502" are not the same morning.
OUT_OK = "ok"                    # observed. An empty answer from here is real.
OUT_OFFLINE = "offline"          # `gather --offline` — deliberate, not broken
OUT_NO_STATE = "no_state"        # the producer has never written its file
OUT_NO_TOOL = "no_tool"          # the command is not there / would not exec
OUT_ERROR = "error"              # it ran and refused (non-zero, HTTP error)
OUT_UNREADABLE = "unreadable"    # it answered in something that would not parse
OUT_TIMEOUT = "timeout"          # it ran and never came back

OUTCOMES = (OUT_OK, OUT_OFFLINE, OUT_NO_STATE, OUT_NO_TOOL, OUT_ERROR,
            OUT_UNREADABLE, OUT_TIMEOUT)

# The three statuses as a projection of the primitive's four warrant classes.
# `not_attempted` and `not_due` both render `unavailable` because the source
# strip wants one dot for "this one did not answer" and the reason in the
# outcome beside it — but they stay two different words upstream, and the
# manifest keeps the outcome, so nothing here loses the distinction.
STATUS_FOR_WARRANT = {
    O.OBSERVED: COMPLETED,
    O.FAILED: FAILED,
    O.NOT_ATTEMPTED: UNAVAILABLE,
    O.NOT_DUE: UNAVAILABLE,
}

# The one mapping, so a gatherer decides an OUTCOME (which it knows) and never
# a STATUS (which is policy). DERIVED from the registered briefer vocabulary
# rather than written out again: adding an eighth outcome means classing it in
# lib/estate_observation.py, and there is no way to add one here that skips
# that step.
STATUS_FOR_OUTCOME = {outcome: STATUS_FOR_WARRANT[warrant]
                      for outcome, warrant in O.BRIEFER.outcomes.items()}

# ── freshness states (recomputed on every read, never stored as a verdict) ───
CURRENT, STALE, UNKNOWN = "current", "stale", "unknown"

# ── the ten sources, in the order the gather runs them ───────────────────────
# `background` joined at P-13. It sits next to `runs` because the two answer
# adjacent questions about the same night — `runs` says whether each scheduled
# loop ran, `background` says what the estate actually got done between the
# last delivered brief and this one.
SOURCES = ("spotter", "pr_reviews", "digest", "focus", "stale", "due", "runs",
           "background", "attention", "memory")

SOURCE_LABEL = {
    "spotter": "spotter board",
    "pr_reviews": "PR review comments",
    "digest": "repo digest",
    "focus": "focus projects",
    "stale": "stale tasks",
    "due": "due soon",
    "runs": "overnight runs",
    "background": "background activity",
    "attention": "waiting on you",
    "memory": "memory recall",
}

# How old a source's data may be before the brief built on it should say so.
# Two of them describe a SNAPSHOT somebody else generated, so their budget is
# that producer's own cadence — 90 minutes matches the spotter-stale threshold
# the brief already used, 24h matches the focus engine's nightly pull. The
# live fetches get a day, because the manifest is read all day from a brief
# that was written once: at 16:00 a 06:30 estate query genuinely is old news,
# and saying so is the panel's job.
DEFAULT_MAX_AGE_SECONDS = 24 * 3600
MAX_AGE_SECONDS = {"spotter": 90 * 60, "focus": 24 * 3600}


def max_age_for(source: str) -> int:
    return MAX_AGE_SECONDS.get(source, DEFAULT_MAX_AGE_SECONDS)


def freshness_state(age_seconds: float | None, max_age: float) -> str:
    """current / stale / unknown from an age and a budget. No clock in here —
    the caller owns `now`, so a test and a browser can both drive it."""
    if age_seconds is None:
        return UNKNOWN
    return STALE if age_seconds > max_age else CURRENT


def empty_is_evidence(env: dict) -> bool:
    """May a consumer read this envelope's empty result as "nothing happened"?

    The whole contract in one predicate. Recomputed rather than trusted from
    the stored field so a hand-edited or older manifest cannot claim it.

    BOTH recorded halves of the warrant have to agree. The status is what the
    strip renders and the outcome is what the gatherer actually observed, and a
    real envelope always has one projected from the other — so an envelope
    claiming `completed` over a `timeout` has no coherent warrant at all, and a
    reading with no coherent warrant is not evidence. That is a tightening: it
    used to be read as a quiet morning.

    An outcome this estate has never defined is answered on the status alone.
    An unrecognised warrant is exactly the case the rule refuses to guess
    about, so the unknown half can only ever withhold the answer, never supply
    one.
    """
    if env.get("degraded") or env.get("status") != COMPLETED:
        return False
    outcome = env.get("outcome")
    if outcome in O.BRIEFER.outcomes:
        return O.BRIEFER.absence_is_evidence(outcome)
    return True


def counts(manifest: dict) -> dict:
    """How many sources landed in each status, plus the degraded tally."""
    envs = [e for e in (manifest.get("sources") or {}).values()
            if isinstance(e, dict)]
    out = {status: 0 for status in STATUSES}
    for env in envs:
        status = env.get("status")
        if status in out:
            out[status] += 1
    out["degraded"] = sum(1 for e in envs if e.get("degraded"))
    out["total"] = len(envs)
    return out


def is_complete(manifest: dict) -> bool:
    """Every source completed and none of them degraded — the only shape in
    which the brief as a WHOLE may be read as a full picture of the morning."""
    envs = (manifest.get("sources") or {}).values()
    return bool(envs) and all(empty_is_evidence(e) for e in envs
                              if isinstance(e, dict))


def incomplete_sources(manifest: dict) -> list[dict]:
    """The sources a reader must not conclude an absence from, worst first:
    failed, then unavailable, then degraded-but-completed."""
    rank = {FAILED: 0, UNAVAILABLE: 1, COMPLETED: 2}
    bad = [e for e in (manifest.get("sources") or {}).values()
           if isinstance(e, dict) and not empty_is_evidence(e)]
    return sorted(bad, key=lambda e: (rank.get(e.get("status"), 3),
                                      str(e.get("source"))))


# ── the legacy adapter ───────────────────────────────────────────────────────
# Every brief written before P-11 landed has no manifest, and there is nothing
# to migrate: the gatherers of the day did not record what they saw, and
# inventing nine `completed` envelopes for them would be fabrication rather
# than migration — the same rule P-10 stated about backfilling deliveries.
#
# So a manifest-less report reads as exactly that: version 0, no sources, and
# `legacy: True`. The panel says "no source manifest" and claims nothing about
# the fetches, which is the only true thing available.

LEGACY_VERSION = 0
LEGACY_WHY = ("written before the source manifest existed — what its gatherers "
              "saw was not recorded")


def legacy(date: str, why: str = LEGACY_WHY) -> dict:
    return {"version": LEGACY_VERSION, "legacy": True, "date": date,
            "sources": {}, "spotter_snapshot": None, "ledger_cutoff": None,
            "revision": 0, "revisions": [], "generated_at": None,
            "offline": None, "why": why}


def path_for(state_dir, date: str) -> Path:
    return Path(state_dir) / DIRNAME / f"{date}.json"


def load(state_dir, date: str) -> dict:
    """This date's manifest, or the legacy stub. Never raises and never returns
    None: a caller asking "what did the brief see" always gets an answer it can
    render, and "we do not know" is one of the answers."""
    path = path_for(state_dir, date)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return legacy(date)
    except (OSError, ValueError) as err:
        return legacy(date, f"manifest could not be read: {err}")
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return legacy(date, "manifest is not in the expected shape")
    data.setdefault("version", VERSION)
    data.setdefault("date", date)
    data["legacy"] = False
    return data

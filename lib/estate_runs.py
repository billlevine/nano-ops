"""The scheduled-run contract for the estate's one Ledger (gap audit P-08).

P-06 made the Ledger authoritative for what each subsystem DID. It still could
not answer the question a schedule raises: *did the run happen at all?* A pass
that died before writing anything left no row, and a pass that ran and found
nothing to do left no row either — so "quiet night" and "the loop was dead for
nine hours" were the same observation. Three scenarios fall through that:

  S06  a scheduled Mechanic run completes and the next one fails, and the
       Ledger holds a distinct completed-run event and failed-run event, each
       with its time and outcome.
  S12  the Night shift runs unattended on schedule and records either a
       completed or a failed run.
  S54  Mechanic, Night shift, Spotter and Briefer all have defined schedules;
       one run completes with no work, one fails, one expected run produces no
       record at all — and the three are told apart.

WHAT A RUN IS
-------------
One occurrence of a subsystem's OWN unit of scheduled work — not one tick. The
distinction is not a simplification, it is what the engines already implement:

  mechanic     one pass per night, inside `pass_start`–`pass_end`; ticks
               outside the window are heartbeats and `derive_phase` refuses a
               second pass for the same night.
  briefer      one pass per morning, same shape, same refusal.
  night-shift  one drain per night, inside `drain_start`–`drain_end`. Many
               ticks work that one drain; the night is the unit.
  spotter      no window at all. Its scheduled unit IS the poll, so its run
               slot is the interval bucket declared in loops.toml.

So three subsystems have DATE slots and one has INTERVAL slots, and that comes
from reading their configs rather than from a rule imposed on them here.

THE RUN ID
----------
`<subsystem>/<slot>` — `mechanic/2026-07-29`, `spotter/2026-07-30T14:15Z`.
Derived from the schedule, never from a counter, which is the property that
makes a MISSING run nameable: the reconciler can compute the id of a run that
produced nothing, and ask the Ledger whether anything with that id is on file.

STARTED, AND WHY THE SPOTTER HAS NONE
-------------------------------------
A run records `started` and then exactly one of `completed`, `no_activity`, or
`failed`. The `started` row exists for one purpose: to distinguish a run that
began and died from one that never began. That is only information when the two
rows come from DIFFERENT invocations — an agentic pass that spans many steps
and can die between them (mechanic, briefer, night-shift).

`track.py refresh` is one synchronous call. If it dies, neither row is written,
so a `started` row would say nothing its absence does not already say. The
spotter therefore emits its terminal row only, and its runs are never
`incomplete`. Uniformity that adds a row carrying no information is not
uniformity worth having.

WHAT ABSENCE MEANS — AND WHAT IT DOES NOT
-----------------------------------------
The reconciler never converts an absent record into a failure. A slot with no
run event gets one of three honest labels instead:

  activity_only  no run record, but the Ledger holds other qualifying events
                 from that subsystem inside the slot. It demonstrably ran; its
                 outcome was never recorded.
  unobservable   the slot predates that subsystem's FIRST run record. Run
                 records did not exist yet, so absence proves nothing. This is
                 the whole of the estate's history before P-08 for the spotter
                 and the night shift, and it must not read as failure.
  missing        the slot is in the observable era, the window has elapsed, and
                 there is no evidence of any kind. THIS is S54's missing run.

`unobservable` versus `missing` is the same distinction P-01 drew about
associations: a wrong record is indistinguishable from a right one once
written, and worse than none, because a reader believes it.

RUNS ARE NOT A SECOND STORE
---------------------------
Run events are ordinary qualifying events — same table, same four phases, same
fingerprint. `run_id` and `run_outcome` are two additive columns, so every
pre-P-08 row keeps NULL in both and the store answers `run_id IS NULL` as
"nobody recorded this as a scheduled run", exactly as `phase IS NULL` answers
"not a qualifying event".

`events` is append-only (`events_no_update`), so the run id can never be
stamped onto the rows P-06 already imported. It does not need to be:
`run_id_of()` DERIVES the id of an older pass row from its own `refs.night` /
`refs.morning`. That is why the backfill writes nothing on a store P-06 has
already converged — it recognizes those rows as the runs they are instead of
filing a second copy.

Docs: docs/ledger-contract.md
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import os

import estate_ledger as L

# ── the run outcome vocabulary ───────────────────────────────────────────────
STARTED = "started"
COMPLETED = "completed"
NO_ACTIVITY = "no_activity"
FAILED = "failed"

OUTCOMES = (STARTED, COMPLETED, NO_ACTIVITY, FAILED)
# Exactly one of these ends a run. `no_activity` is terminal and is a SUCCESS:
# the run happened and had nothing to do. Folding it into `completed` would
# lose S54's first category; folding it into `failed` would invent one.
TERMINAL = (COMPLETED, NO_ACTIVITY, FAILED)

PHASE_BY_OUTCOME = {
    STARTED: L.INITIATION,
    COMPLETED: L.OUTCOME,
    NO_ACTIVITY: L.OUTCOME,
    FAILED: L.FAILURE,
}

# ── the reconciliation verdicts ──────────────────────────────────────────────
# The first four are read off a record. The last three are read off an absence,
# and they are three different claims — see WHAT ABSENCE MEANS above.
INCOMPLETE = "incomplete"
ACTIVITY_ONLY = "activity_only"
UNOBSERVABLE = "unobservable"
MISSING = "missing"

VERDICTS = (COMPLETED, NO_ACTIVITY, FAILED, INCOMPLETE, ACTIVITY_ONLY,
            UNOBSERVABLE, MISSING)
# What the briefer leads with. `incomplete` is in here because a pass that
# started and never finished is a run that needs a person, same as a failure.
ATTENTION = (FAILED, INCOMPLETE, MISSING)

# ── the schedules, read from the file each engine itself obeys ───────────────
# No copy of a window lives here. `state/mechanic/config.toml` is what
# mechanic.py reads to decide whether tonight's pass is due, so it is also what
# says when a mechanic run was expected. A second declaration would be a second
# source of truth, and the drift would be silent in exactly the direction that
# manufactures phantom missing runs.
DATE, INTERVAL = "date", "interval"

SCHEDULES = {
    "mechanic": {"kind": DATE, "config": ("mechanic", "config.toml"),
                 "keys": ("pass_start", "pass_end")},
    "briefer": {"kind": DATE, "config": ("morning-brief", "config.toml"),
                "keys": ("pass_start", "pass_end")},
    "night-shift": {"kind": DATE, "config": ("pr-reviewer", "config.toml"),
                    "keys": ("drain_start", "drain_end")},
    # The spotter has no window. Its cadence is the loop registry's `interval`,
    # which is where the estate declares how often a loop is supposed to tick.
    "spotter": {"kind": INTERVAL, "loop": "pr-tracker", "default_minutes": 15},
}

SCHEDULED = tuple(SCHEDULES)          # the four, in declaration order


class NoSchedule(LookupError):
    """This subsystem has no readable schedule, so nothing is expected of it.

    A real answer, not an error: a store with no loop configs beside it (a test
    fixture, a fresh machine) genuinely cannot say when a run was due, and
    reporting every slot as missing would be worse than reporting none.
    """


# ── slots ────────────────────────────────────────────────────────────────────
def date_slot(now_local: dt.datetime, start: str, end: str) -> str:
    """The local date this moment's pass belongs to.

    Today, except in the after-midnight tail of a window that wraps midnight,
    which still belongs to the previous date. This is `mechanic.py night_id`
    and `brief.py morning_id`, which are already the same function; the night
    shift's drain window (00:00–08:00) does not wrap, so it needs no third one.
    """
    now_hm = now_local.strftime("%H:%M")
    if start and end and start > end and now_hm < end:
        return (now_local - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return now_local.strftime("%Y-%m-%d")


def interval_slot(when: dt.datetime, minutes: int) -> str:
    """The interval bucket a moment falls in, floored, in UTC.

    UTC on purpose: a bucket is arithmetic on a duration, and doing that
    arithmetic in local time makes two runs share a slot (or a slot vanish)
    twice a year at the DST boundary. Dates are local because a "night" is a
    human unit; buckets are not.
    """
    minutes = max(1, int(minutes))
    stamp = when.astimezone(dt.timezone.utc)
    floor = (stamp.hour * 60 + stamp.minute) // minutes * minutes
    base = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return (base + dt.timedelta(minutes=floor)).strftime("%Y-%m-%dT%H:%MZ")


def run_id(subsystem: str, slot: str) -> str:
    return f"{subsystem}/{slot}"


def split(rid: str) -> tuple[str, str]:
    """(subsystem, slot). The slot may itself be empty on a malformed id."""
    subsystem, _, slot = (rid or "").partition("/")
    return subsystem, slot


def parse_minutes(text: str, fallback: int) -> int:
    """loops.toml's `interval` — "15m", "20m", "on-demand", "900s"."""
    text = str(text or "").strip().lower()
    if text.endswith("m") and text[:-1].isdigit():
        return max(1, int(text[:-1]))
    if text.endswith("h") and text[:-1].isdigit():
        return max(1, int(text[:-1]) * 60)
    if text.endswith("s") and text[:-1].isdigit():
        return max(1, int(text[:-1]) // 60 or 1)
    if text.isdigit():
        return max(1, int(text))
    return fallback


# ── reading a schedule ───────────────────────────────────────────────────────
def _toml(path: str) -> dict:
    try:
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def schedule(subsystem: str, state_dir: str, repo_root: str) -> dict:
    """This subsystem's declared schedule, or raise NoSchedule.

    A date schedule reads {"kind": "date", "start": .., "end": ..}; an interval
    one reads {"kind": "interval", "minutes": N}. An empty start means the
    window is switched OFF — a heartbeat-only loop expects no runs, which is a
    schedule, not a missing one.
    """
    spec = SCHEDULES.get(subsystem)
    if spec is None:
        raise NoSchedule(f"{subsystem} is not a scheduled subsystem; the four "
                         f"are: {', '.join(SCHEDULED)}")
    if spec["kind"] == INTERVAL:
        loops = _toml(os.path.join(repo_root, "loops.toml"))
        entry = (loops.get("loops") or {}).get(spec["loop"]) or {}
        raw = str(entry.get("interval") or "")
        if raw and not raw[0].isdigit():
            # "on-demand" is a real registry value and it is not a cadence.
            raise NoSchedule(f"{spec['loop']} runs on demand ({raw!r}); no "
                             "scheduled run is expected of it")
        return {"kind": INTERVAL,
                "minutes": parse_minutes(raw, spec["default_minutes"]),
                "source": os.path.join(repo_root, "loops.toml")}
    path = os.path.join(state_dir, *spec["config"])
    if not os.path.exists(path):
        raise NoSchedule(f"no schedule for {subsystem}: {path} does not exist")
    cfg = _toml(path)
    start_key, end_key = spec["keys"]
    return {"kind": DATE, "start": str(cfg.get(start_key) or "").strip(),
            "end": str(cfg.get(end_key) or "").strip(), "source": path}


def slot_of(subsystem: str, when: dt.datetime, sched: dict) -> str:
    if sched["kind"] == INTERVAL:
        return interval_slot(when, sched["minutes"])
    return date_slot(when.astimezone(), sched.get("start", ""),
                     sched.get("end", ""))


# ── which runs were DUE ──────────────────────────────────────────────────────
def _window_end(day: dt.date, sched: dict) -> dt.datetime:
    """When the pass for `day` stopped being possible, as an aware datetime.

    A run is not missing until its window has closed — asking "where is
    tonight's mechanic pass" at 03:00 while the window runs to 05:00 would
    manufacture a missing run every single night.
    """
    start, end = sched.get("start", ""), sched.get("end", "")
    text = end or "23:59"
    hour, _, minute = text.partition(":")
    try:
        clock = dt.time(int(hour), int(minute or 0))
    except ValueError:
        clock = dt.time(23, 59)
    naive = dt.datetime.combine(day, clock)
    if start and end and start > end:
        naive += dt.timedelta(days=1)      # the window wraps into the next day
    return naive.astimezone()


def due_slots(subsystem: str, sched: dict, since: dt.datetime,
              until: dt.datetime) -> list[str]:
    """Every run slot that was due in [since, until], oldest first.

    Only slots whose window has CLOSED by `until`. A date schedule with an
    empty start is switched off and expects nothing.
    """
    if sched["kind"] == INTERVAL:
        minutes = sched["minutes"]
        step = dt.timedelta(minutes=minutes)
        stamp = since.astimezone(dt.timezone.utc)
        stamp = dt.datetime.strptime(interval_slot(stamp, minutes),
                                     "%Y-%m-%dT%H:%MZ").replace(
                                         tzinfo=dt.timezone.utc)
        out = []
        while stamp + step <= until:       # the bucket must have fully elapsed
            out.append(interval_slot(stamp, minutes))
            stamp += step
        return out
    if not sched.get("start"):
        return []
    day = since.astimezone().date()
    last = until.astimezone().date()
    out = []
    while day <= last:
        if _window_end(day, sched) <= until:
            out.append(day.strftime("%Y-%m-%d"))
        day += dt.timedelta(days=1)
    return out


# ── reading a run off an events row ──────────────────────────────────────────
# The pass vocabulary the two scheduled loops already wrote, mapped to run
# outcomes. `pass_done` reads as `completed` and never as `no_activity`: a row
# written before P-08 does not say whether the pass found anything, and "it
# found nothing" is a claim, not a default.
PASS_EVENT_OUTCOME = {"pass_start": STARTED, "pass_done": COMPLETED,
                      "pass_failed": FAILED}


def _refs(row) -> dict:
    raw = row["refs"] if "refs" in row.keys() else None
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _column(row, name: str):
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def run_id_of(row) -> str | None:
    """This row's run id — from the column, or DERIVED from an older row.

    The derivation is what keeps P-06's 797 backfilled events usable without
    rewriting one of them (the table forbids that anyway). A mechanic pass row
    carries `refs.night`; a briefer pass row carries `refs.morning`. Both name
    the slot exactly, so the run they belong to is a fact those rows already
    state — reading it is recognition, not inference.
    """
    stamped = _column(row, "run_id")
    if stamped:
        return str(stamped)
    subsystem = _column(row, "subsystem")
    if subsystem not in SCHEDULES:
        return None
    refs = _refs(row)
    if str(refs.get("event") or "") not in PASS_EVENT_OUTCOME:
        return None
    slot = refs.get("night") or refs.get("morning") or refs.get("slot")
    return run_id(subsystem, str(slot)) if slot else None


def outcome_of(row) -> str | None:
    """This row's run outcome — from the column, or derived the same way."""
    stamped = _column(row, "run_outcome")
    if stamped:
        return str(stamped)
    if run_id_of(row) is None:
        return None
    return PASS_EVENT_OUTCOME.get(str(_refs(row).get("event") or ""))


# ── reconciliation ───────────────────────────────────────────────────────────
def reconcile(subsystem: str, due: list[str], rows, sched: dict | None = None
              ) -> list[dict]:
    """One verdict per due slot, oldest first.

    `rows` is every qualifying event for this subsystem in (or overlapping) the
    range, each with `ts`, `phase`, and whatever run identity it carries. Rows
    outside `due` still matter: the earliest run record of any kind sets the
    observability floor, below which an absence proves nothing.

    `sched` is what lets an ordinary event be attributed to the right slot; see
    `_SlotIndex` for why a timestamp prefix is not good enough.
    """
    terminal: dict[str, dict] = {}
    started: dict[str, str] = {}
    activity: dict[str, str] = {}
    floor: str | None = None

    for row in rows:
        rid = run_id_of(row)
        outcome = outcome_of(row)
        if rid and outcome:
            _, slot = split(rid)
            if floor is None or slot < floor:
                floor = slot
            if outcome in TERMINAL:
                # First terminal wins. A second one for the same run is a
                # replay or a resumed pass restating itself, not a new verdict.
                terminal.setdefault(slot, {"outcome": outcome,
                                           "ts": row["ts"]})
            elif outcome == STARTED:
                started.setdefault(slot, row["ts"])
        elif rid:
            _, slot = split(rid)
            activity.setdefault(slot, row["ts"])

    # Everything else the subsystem did, bucketed by the slot its timestamp
    # falls in — this is what turns "no run record" into "ran, outcome never
    # recorded" instead of a missing run.
    index = _SlotIndex(due, sched)
    for row in rows:
        if run_id_of(row):
            continue
        slot = index.of(row["ts"])
        if slot:
            activity.setdefault(slot, row["ts"])

    out = []
    for slot in due:
        if slot in terminal:
            out.append({"run": run_id(subsystem, slot), "slot": slot,
                        "verdict": terminal[slot]["outcome"],
                        "at": terminal[slot]["ts"]})
        elif slot in started:
            out.append({"run": run_id(subsystem, slot), "slot": slot,
                        "verdict": INCOMPLETE, "at": started[slot],
                        "why": "started and never reached a terminal outcome"})
        elif slot in activity:
            out.append({"run": run_id(subsystem, slot), "slot": slot,
                        "verdict": ACTIVITY_ONLY, "at": activity[slot],
                        "why": "the Ledger holds work from this subsystem in "
                               "this slot, but no run record"})
        elif floor is None or slot < floor:
            out.append({"run": run_id(subsystem, slot), "slot": slot,
                        "verdict": UNOBSERVABLE, "at": None,
                        "why": "predates this subsystem's first run record — "
                               "absence proves nothing"})
        else:
            out.append({"run": run_id(subsystem, slot), "slot": slot,
                        "verdict": MISSING, "at": None,
                        "why": "the window elapsed and nothing was recorded"})
    return out


class _SlotIndex:
    """Which due slot a timestamp falls in — through the schedule, not a prefix.

    It would be tempting to compare `ts[:10]` against a date slot, and it would
    be wrong in exactly the place this proposal cares about: a slot is a LOCAL
    date and `ts` is UTC, so an event at 03:00 UTC on the 30th belongs to the
    night of the 29th in a US timezone. Prefix-matching would file that night's
    own work under the next night and report the real one as missing.

    So the slot is computed with the same two functions the emitters use, which
    is also what keeps this from becoming a third implementation of the
    local-date rule that can disagree with the engines.

    Falls back to a bisected prefix match only when there is no schedule to
    compute with — a caller that has due slots but no window can still bucket
    approximately rather than not at all.
    """

    def __init__(self, due: list[str], sched: dict | None = None):
        self.due = due
        self.slots = set(due)
        self.sched = sched
        self.width = 16 if due and len(due[0]) > 10 else 10
        self.keys = [s[:self.width] for s in due]

    def of(self, ts: str) -> str | None:
        if not self.due:
            return None
        stamp = _parse(ts)
        if stamp is not None and self.sched:
            if self.sched["kind"] == INTERVAL:
                slot = interval_slot(stamp, self.sched["minutes"])
            else:
                slot = date_slot(stamp.astimezone(), self.sched.get("start", ""),
                                 self.sched.get("end", ""))
            return slot if slot in self.slots else None
        key = str(ts or "")[:self.width]
        if not key:
            return None
        at = bisect.bisect_right(self.keys, key) - 1
        if at < 0:
            return None
        if self.width == 10:
            return self.due[at] if self.keys[at] == key else None
        return self.due[at] if self.keys[at][:10] == key[:10] else None


def _parse(ts) -> dt.datetime | None:
    """An events row's `ts` as an aware datetime, or None."""
    text = str(ts or "").strip()
    if not text:
        return None
    try:
        stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def tally(verdicts: list[dict]) -> dict[str, int]:
    counts = {v: 0 for v in VERDICTS}
    for row in verdicts:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return counts


def fingerprint_for(subsystem: str, rid: str, outcome: str) -> str:
    """The stable identity of one run event.

    Keyed on the RUN, not on the clock: a resumed pass that replays its own
    `started`, or a drain tick that restates the night's completion, lands one
    row. That is the same run, not a second one — the same reason the mechanic
    and briefer emitters already fingerprint on the night and the morning.
    """
    return L.fingerprint(subsystem=subsystem,
                         phase=PHASE_BY_OUTCOME[outcome], source="run",
                         key=rid, ts=rid, event=outcome)

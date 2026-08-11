"""One composed read model over the estate's work schema (t-135).

WHY THIS EXISTS
---------------
`/estate.html` could already list tasks, and that is where it stopped. Its
"Follow-ups" panel actually held every task kind; the follow-up compatibility
envelope inside `refs` was never decoded; projects were absent from the
snapshot entirely; and of the four dependency kinds only the derived unresolved
`blocks` ids reached the page — as bare ids, with no titles and no outbound
view. t-124's investigation named the fix: not a second store and not a schema
migration, but the ONE composed read the schema was always able to answer and
nobody had written.

So this module is a pure read layer. It takes a read-only connection and
returns normalized projects, tasks, edges, decoded follow-ups, derived
attention and project rollups. It writes nothing, and it holds nothing: every
derived field is recomputed on the read that renders it, which is the same
reason `blocked_by` was derived in the first place (lib/estate_deps.py) and the
same reason `due_state` is (lib/estate_attention.py). A cached rollup is a
second copy of the truth, and the copy is the one that goes stale.

WHAT IS DERIVED HERE, AND FROM WHOSE RULE
-----------------------------------------
Nothing in this module invents a rule. Each derived field restates one that
already exists somewhere else in the estate, by calling it:

  blocked_by     lib/estate_deps.blocked_by — `estate ready`'s own rule, a task
                 B with an unresolved `blocks` edge into it.
  blocks         the exact TRANSPOSE of that map, never a second query. If A
                 appears in B's `blocked_by`, then B appears in A's `blocks`,
                 by construction — the two views cannot disagree about one
                 edge, because they are one dict read in two directions.
  due_state      lib/estate_attention.classify — the five states plus
                 `unreadable`, under one explicit notice interval.
  attention      lib/estate_attention.is_attention — an open follow-up, or a
                 task parked at `needs-owner`. Docs: docs/attention-contract.md.
  followup       the `refs` envelope `bin/followups` writes and reads.

A TERMINAL TASK HAS NO DUE STATE
--------------------------------
`due_state` is None for a `done` or `dropped` row, rather than `overdue`. The
deadline on a finished task is history: classifying it would put resolved work
in the same bucket as a missed one, and the attention contract is explicit that
a resolved follow-up is history rather than a demand. Non-terminal rows carry
one of the six states, always — including `undated`, which is an answer.

UNPROJECTED WORK IS A GROUPING, NOT A PROJECT
---------------------------------------------
`project_id IS NULL` is most of this store, and it needs to be visible or the
Projects section reads as though the estate had almost no work in it. It is
emitted as one synthetic bucket with `unprojected: true` and the same rollup
shape as a real project, so a renderer needs no special case. It is NOT a row
in `projects` and this module will never make it one — §4.7 of the
investigation is explicit that the synthetic grouping belongs in the read
model, not in SQLite.

DEGRADES, NEVER RAISES
----------------------
The dashboard reads stores it cannot migrate. A missing `projects` table, a
missing `task_deps` table, a store predating the `due_at` or `stage` column:
each one means the corresponding section has nothing to show, which is a
different statement from an error page. Same rule estate_deps already takes —
a derived view must never take down the panel that carries it.
"""
from __future__ import annotations

import datetime as dt
import json

import estate_attention
import estate_deps

# A task in one of these is finished. Same tuple as bin/estate's TERMINAL and
# estate_deps' — read through estate_attention so there is one spelling here.
TERMINAL = estate_attention.TERMINAL

# The one edge kind whose meaning this module derives anything from. The other
# three (`parent`, `related`, `discovered-from`) are carried through exactly as
# stored: `dependencies()` deliberately does NOT filter to a known vocabulary,
# because a kind this module has not heard of is still an edge somebody wrote,
# and silently dropping it would make the "all relationships" view a lie.
BLOCKS = "blocks"

FOLLOWUP_KIND = "followup"
PROPOSAL_KIND = "proposal"
# t-296. One task per inbox message the hub mirrored, kind and refs written by
# `bin/hub-intake message open`. Decoded here for the same reason the follow-up
# envelope is: the identity of a tracked message is (channel, message_ts), and
# a renderer should not have to know where in `refs` that lives.
MESSAGE_KIND = "inbox-message"
MESSAGE_FIELDS = ("message_id", "channel", "message_ts", "thread_ts", "inbox")
PROPOSAL_STAGES = ("pending-review", "approved-backlog", "rejected",
                   "resolved", "stopped")
PROPOSAL_FIELDS = ("fingerprint", "condition", "desired_outcome",
                   "completion_check")

# The follow-up compatibility envelope, exactly as `bin/followups` writes it
# into `refs`. Decoded once here so no reader has to know the envelope's shape
# (§4.6). `attention_key` is what `followups attention` links on when a sweep
# runs again, so it is part of the identity even though the legacy tool never
# printed it.
FOLLOWUP_FIELDS = ("legacy_id", "source", "ref", "context", "resolution",
                   "attention_key")

# The id of the synthetic "work that belongs to no project" bucket. Not a
# project id — `estate project add` mints `p-N`, so this can never collide.
UNPROJECTED = "unprojected"


# ── small helpers ────────────────────────────────────────────────────────────

def has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def row_get(row, key: str):
    """Column value, or None on a store that predates the column."""
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(key)
    return row[key] if key in keys else None


def decode_refs(value) -> dict:
    """The `refs` JSON blob as a dict; {} for anything else.

    A blob that will not parse is not an error here. It is one task's private
    envelope, and losing the page over it would be the derived-view rule
    inverted.
    """
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def followup_view(task: dict) -> dict | None:
    """The decoded follow-up envelope, or None for a task of another kind.

    `state` is "open" while the task is NON-TERMINAL, which is deliberately not
    `bin/followups`' binary (that shim calls anything but literal `open`
    "resolved", because the legacy file format had no other word). The
    attention contract is the authority for what an open follow-up is, and a
    `claimed` follow-up is plainly not resolved.
    """
    if (task.get("kind") or "") != FOLLOWUP_KIND:
        return None
    refs = decode_refs(task.get("refs"))
    out = {field: refs.get(field) for field in FOLLOWUP_FIELDS}
    out["state"] = "resolved" if (task.get("status") or "") in TERMINAL else "open"
    out["resolved_at"] = task.get("closed_at")
    return out


def proposal_view(task: dict) -> dict | None:
    """Normalized review record carried by a staged proposal task."""
    stage = task.get("stage")
    if not stage:
        return None
    refs = decode_refs(task.get("refs"))
    out = {field: refs.get(field) for field in PROPOSAL_FIELDS}
    out["stage"] = stage
    out["legacy"] = any(not str(out.get(field) or "").strip()
                        for field in ("condition", "desired_outcome",
                                      "completion_check"))
    out["recurrences"] = 0
    out["last_recurrence_at"] = None
    return out


def message_view(task: dict) -> dict | None:
    """The decoded tracked-message envelope, or None for another kind.

    `open` is the same reading the rest of this module uses: NON-TERMINAL. A
    message at `ready` was registered and answered-or-not; one at `claimed` is
    with a dispatched worker; one at `needs-owner` is already in the attention
    set. All three are conversations nobody has finished, which is exactly what
    the panel that renders this is for (docs/inbox-message-contract.md).
    """
    if (task.get("kind") or "") != MESSAGE_KIND:
        return None
    refs = decode_refs(task.get("refs"))
    out = {field: refs.get(field) for field in MESSAGE_FIELDS}
    out["threaded"] = bool(refs.get("thread_ts"))
    out["open"] = (task.get("status") or "") not in TERMINAL
    return out


def transpose(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    """{blocked -> [blockers]} read the other way: {blocker -> [it holds up]}.

    Order is the input's, which is `estate_deps.blocked_by`'s creation order,
    so both directions of one edge set are stable and sorted the same way.
    """
    out: dict[str, list[str]] = {}
    for target, sources in mapping.items():
        for source in sources:
            out.setdefault(source, []).append(target)
    return out


# ── dependencies ─────────────────────────────────────────────────────────────

def dependencies(conn, titles: dict[str, dict] | None = None) -> list[dict]:
    """Every edge in `task_deps`, with both endpoints resolved.

    The stored fact is the edge; `blocked_by` and `blocks` are two readings of
    the `blocks` subset of it (§4.3). Emitting the edges once and deriving the
    directional views from them is why neither view needs its own array on
    every task row.

    `active` answers "is this edge currently holding anything up" and is
    meaningful ONLY for `blocks`: it is None for `parent`, `related` and
    `discovered-from`, because those describe provenance and hierarchy, which
    no task status resolves. For `blocks` it is exactly estate_deps' rule —
    the SOURCE task is not yet terminal.
    """
    if not estate_deps.has_deps_table(conn):
        return []
    index = titles or {}
    out = []
    for row in conn.execute(
            "SELECT from_task, to_task, dep_kind, created_at FROM task_deps "
            "ORDER BY created_at, from_task, to_task"):
        source = index.get(row["from_task"]) or {}
        target = index.get(row["to_task"]) or {}
        kind = row["dep_kind"]
        active = None
        if kind == BLOCKS:
            # An endpoint we cannot see is not evidence the edge is dead.
            active = (source.get("status") or "") not in TERMINAL
        out.append({
            "from_task": row["from_task"],
            "from_title": source.get("title"),
            "from_status": source.get("status"),
            "to_task": row["to_task"],
            "to_title": target.get("title"),
            "to_status": target.get("status"),
            "kind": kind,
            "created_at": row["created_at"],
            "active": active,
        })
    return out


# ── projects ─────────────────────────────────────────────────────────────────

def _empty_rollup() -> dict:
    return {"tasks": 0, "open": 0, "terminal": 0, "blocked": 0, "attention": 0,
            "overdue": 0, "due_soon": 0, "by_status": {}, "by_kind": {},
            "task_ids": [], "last_activity": None}


def _accumulate(rollup: dict, task: dict) -> None:
    status, kind = task.get("status") or "", task.get("kind") or ""
    rollup["tasks"] += 1
    rollup["task_ids"].append(task["id"])
    rollup["by_status"][status] = rollup["by_status"].get(status, 0) + 1
    rollup["by_kind"][kind] = rollup["by_kind"].get(kind, 0) + 1
    if status in TERMINAL:
        rollup["terminal"] += 1
    else:
        rollup["open"] += 1
    if task.get("blocked_by"):
        rollup["blocked"] += 1
    if task.get("is_attention"):
        rollup["attention"] += 1
    state = task.get("due_state")
    if state == estate_attention.OVERDUE:
        rollup["overdue"] += 1
    elif state in (estate_attention.DUE_TODAY, estate_attention.DUE_SOON):
        rollup["due_soon"] += 1
    updated = task.get("updated_at")
    if updated and (rollup["last_activity"] is None
                    or str(updated) > str(rollup["last_activity"])):
        rollup["last_activity"] = updated


def projects(conn, tasks: list[dict]) -> list[dict]:
    """Every project, each carrying a rollup derived from the task rows.

    Counts are DERIVED on this read and never stored (§4.7): a counter column
    would have to be re-written every time a task moved, and the one time it
    was not is the time the page lies. Ordered active-first, then newest.
    """
    rollups: dict[str, dict] = {}
    for task in tasks:
        key = task.get("project_id") or UNPROJECTED
        _accumulate(rollups.setdefault(key, _empty_rollup()), task)

    rows = []
    if has_table(conn, "projects"):
        rows = list(conn.execute("SELECT * FROM projects ORDER BY seq"))

    out = []
    for row in rows:
        item = dict(row)
        item["meta"] = decode_refs(row_get(row, "meta"))
        item["unprojected"] = False
        item["rollup"] = rollups.pop(item["id"], _empty_rollup())
        out.append(item)
    out.sort(key=lambda p: ((p.get("status") or "") != "active",
                            -(p.get("seq") or 0)))

    # Anything left in `rollups` is a project_id with no row behind it plus the
    # synthetic bucket. A dangling id is reported rather than dropped — a task
    # pointing at a project nobody can find is exactly the kind of thing this
    # page exists to make visible.
    unprojected = rollups.pop(UNPROJECTED, None)
    for orphan_id in sorted(rollups):
        out.append({"id": orphan_id, "title": "(unknown project)",
                    "kind": "unknown", "status": "unknown", "missing": True,
                    "unprojected": False, "meta": {},
                    "rollup": rollups[orphan_id]})
    if unprojected is not None:
        out.append({"id": UNPROJECTED, "title": "Unprojected work",
                    "kind": "grouping", "status": "n/a", "unprojected": True,
                    "meta": {}, "rollup": unprojected})
    return out


# ── the composed read ────────────────────────────────────────────────────────

def build(conn, now: dt.datetime | None = None,
          notice: float = estate_attention.NOTICE_DEFAULT_DAYS,
          tz: dt.tzinfo | None = None) -> dict:
    """The whole work view: tasks, projects, edges, attention, follow-ups.

    One connection, a handful of whole-table reads, no N+1: the edge table and
    the project table are small, and the task table is read once and grouped in
    memory. `now` and `tz` are arguments so every due-state boundary is
    testable by moving the clock rather than waiting for it.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # Resolved ONCE for the whole read, never per row — a per-row lookup can
    # straddle a DST change and classify two rows under two calendars
    # (estate_attention.local_zone()'s note).
    zone = tz or estate_attention.local_zone()

    task_rows = list(conn.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC, seq DESC"))
    blockers = estate_deps.blocked_by(conn, {r["id"] for r in task_rows})
    blocking = transpose(blockers)
    tasks = estate_deps.annotate(task_rows, blockers)

    by_id = {}
    for task in tasks:
        status, kind = task.get("status") or "", task.get("kind") or ""
        terminal = status in TERMINAL
        due = task.get("due_at")
        task["blocks"] = blocking.get(task["id"], [])
        # See A TERMINAL TASK HAS NO DUE STATE. `days_left` is still computed:
        # it is a fact about the date, not a claim about urgency.
        task["due_state"] = (None if terminal
                             else estate_attention.classify(due, now, notice, zone))
        task["days_left"] = estate_attention.days_left(due, now)
        task["is_attention"] = estate_attention.is_attention(kind, status)
        # `followup` is the only decoded projection of `refs` that ships. The
        # raw blob is already on the row and a general `refs_decoded` beside it
        # is the same bytes twice — on this store that was 150kB of a 850kB
        # snapshot, for text every reader can parse from what it already has.
        # §4.6 asks for the FOLLOW-UP envelope to be normalized at the read
        # boundary, because that one has a contract nobody should re-derive.
        task["followup"] = followup_view(task)
        task["proposal"] = proposal_view(task)
        task["message"] = message_view(task)
        by_id[task["id"]] = task

    # Titles resolved server-side (§ build scope step 7) so no consumer has to
    # linear-scan the task array to name a blocker.
    for task in tasks:
        task["blocked_by_titles"] = {
            b: (by_id.get(b) or {}).get("title") for b in task["blocked_by"]}
        task["blocks_titles"] = {
            b: (by_id.get(b) or {}).get("title") for b in task["blocks"]}

    edges = dependencies(conn, by_id)
    dep_counts: dict[str, int] = {}
    for edge in edges:
        dep_counts[edge["kind"]] = dep_counts.get(edge["kind"], 0) + 1

    project_rows = projects(conn, tasks)
    project_titles = {p["id"]: p.get("title") for p in project_rows}
    for task in tasks:
        task["project_title"] = project_titles.get(task.get("project_id"))

    # The attention set is a QUERY, sorted by the contract's own key so a
    # consumer rendering it in order needs no policy (docs/attention-contract).
    #
    # It is emitted as an ORDERED LIST OF IDS, not a second copy of the rows.
    # Every task is already in `tasks`, and §4.10 is a standing warning about
    # this snapshot's size: a duplicated row is a row that can also disagree
    # with itself. The order IS the payload here — index `tasks` by id and walk
    # this list.
    ordered = sorted((t for t in tasks if t["is_attention"]),
                     key=lambda t: estate_attention.sort_key(
                         t["due_state"], t["days_left"], t.get("seq")))
    attention_counts = {state: 0 for state in estate_attention.ALL_STATES}
    for task in ordered:
        state = task["due_state"] or estate_attention.UNDATED
        attention_counts[state] = attention_counts.get(state, 0) + 1
    attention_counts["total"] = len(ordered)
    attention = [t["id"] for t in ordered]

    # Every follow-up, open and resolved: the page keeps resolved ones behind a
    # History filter rather than mixing them into current attention (§5.3 A),
    # which it can only do if they are in the payload at all. Ids again, same
    # reason.
    followup_rows = [t for t in tasks if t["followup"]]
    followup_counts = {"open": sum(1 for t in followup_rows
                                   if t["followup"]["state"] == "open")}
    followup_counts["resolved"] = len(followup_rows) - followup_counts["open"]
    followup_counts["total"] = len(followup_rows)
    followups = [t["id"] for t in followup_rows]

    proposal_rows = [t for t in tasks if t["proposal"]]
    if proposal_rows:
        recurrences = {r["task_id"]: {"count": r["n"], "last": r["last_ts"]}
                       for r in conn.execute(
                           """SELECT task_id, count(*) n, max(ts) last_ts
                              FROM events WHERE kind='note'
                              AND summary LIKE 'condition observed again%'
                              GROUP BY task_id""")}
        for task in proposal_rows:
            recurrence = recurrences.get(task["id"], {})
            task["proposal"]["recurrences"] = recurrence.get("count", 0)
            task["proposal"]["last_recurrence_at"] = recurrence.get("last")
    proposal_counts = {stage: 0 for stage in PROPOSAL_STAGES}
    for task in proposal_rows:
        stage = task["proposal"]["stage"]
        proposal_counts[stage] = proposal_counts.get(stage, 0) + 1
    proposals = [t["id"] for t in proposal_rows]

    # t-296 — the pull-based backstop, and the one that does not depend on
    # Slack at all. A mirrored message the hub has not closed is an unfinished
    # conversation; ordered OLDEST FIRST because the one that has been open
    # longest is the one most likely to have been missed, which is the entire
    # failure this tracks. Ids again, for the reason attention gives.
    message_rows = [t for t in tasks
                    if t["message"] and t["message"]["open"]]
    message_rows.sort(key=lambda t: (t.get("seq") or 0))
    message_counts: dict[str, int] = {}
    for task in message_rows:
        status = task.get("status") or "unknown"
        message_counts[status] = message_counts.get(status, 0) + 1
    message_counts["total"] = len(message_rows)
    inbox_messages = [t["id"] for t in message_rows]

    project_counts: dict[str, int] = {}
    for project in project_rows:
        if project.get("unprojected"):
            continue
        project_counts[project.get("status") or "unknown"] = (
            project_counts.get(project.get("status") or "unknown", 0) + 1)

    return {
        "tasks": tasks,
        "projects": project_rows,
        "project_counts": project_counts,
        "dependencies": edges,
        "dep_counts": dep_counts,
        "attention": attention,
        "attention_counts": attention_counts,
        "followups": followups,
        "followup_counts": followup_counts,
        "proposals": proposals,
        "proposal_counts": proposal_counts,
        "inbox_messages": inbox_messages,
        "inbox_message_counts": message_counts,
        "notice_days": notice,
    }

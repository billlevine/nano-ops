"""Computed dependency blockers for task views (P-18).

`estate ready` has always excluded a task with an unresolved `blocks` edge
pointing at it, so scheduling was correct. Every other view was not: `task
list`, `task show` and the dashboard all rendered such a task as `ready` with
no hint that nothing could start it, and no name for what it was waiting on.
S29's finding is that "correctly excluded from scheduling" and "visibly
blocked" are different things, and only the first was implemented.

`blocked_by` is a DERIVED view field, never a stored status. That is the whole
design:

  - the schema keeps saying `status='ready'`, because status is the task's own
    lifecycle and being blocked is a property of its neighbours, not of it;
  - nothing has to be re-written when a blocker finishes. The field is
    recomputed on every read, so it clears the instant the blocker reaches a
    terminal state — there is no second place for the truth to go stale;
  - removing the field restores the old presentation exactly, with no
    migration, which is what made this safe to ship.

The blocking rule is `estate ready`'s own, kept identical on purpose: a task is
blocked by every task B where an edge `B --blocks--> T` exists and B is not yet
in a terminal state.
"""
from __future__ import annotations

# A task in one of these is finished, so an edge out of it no longer blocks.
# Mirrors bin/estate's TERMINAL; duplicated rather than imported because the
# dashboard reads this module without loading the CLI.
TERMINAL = ("done", "dropped")

BLOCKERS_SQL = """
SELECT d.to_task AS task_id, d.from_task AS blocker
  FROM task_deps d
  JOIN tasks b ON b.id = d.from_task
 WHERE d.dep_kind = 'blocks'
   AND b.status NOT IN (%s)
 ORDER BY b.seq
""" % ",".join("?" for _ in TERMINAL)

# The other direction, and the only place anything reads it: who was waiting on
# THIS task. `blocked_by` answers "what is holding T up" for a reader; this
# answers "who did T just release" for a writer that has finished T (t-163).
# Same edge, same `blocks` kind, no second notion of blocking.
DEPENDENTS_SQL = """
SELECT d.to_task AS task_id
  FROM task_deps d
  JOIN tasks t ON t.id = d.to_task
 WHERE d.dep_kind = 'blocks'
   AND d.from_task = ?
 ORDER BY t.seq
"""


def has_deps_table(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_deps'"
    ).fetchone())


def blocked_by(conn, task_ids=None) -> dict[str, list[str]]:
    """Map task id -> the ids of its unresolved blockers, in creation order.

    One query for the whole store rather than one per task: a view renders
    every task at once, and the edge table is small. Tasks with no unresolved
    blocker are absent from the mapping, so `.get(id, [])` is the read.

    A store with no `task_deps` table has no dependencies, so it has no
    blockers — it does not have an error. The read-only dashboard reaches
    stores it cannot migrate, and a derived badge must never be able to take
    the panel that carries it down with it.
    """
    if not has_deps_table(conn):
        return {}
    wanted = set(task_ids) if task_ids is not None else None
    out: dict[str, list[str]] = {}
    for row in conn.execute(BLOCKERS_SQL, TERMINAL):
        task, blocker = row["task_id"], row["blocker"]
        if wanted is not None and task not in wanted:
            continue
        out.setdefault(task, []).append(blocker)
    return out


def dependents(conn, task_id: str) -> list[str]:
    """The ids of the tasks this one blocks, in creation order.

    Empty on a store with no `task_deps` table, for the same reason
    `blocked_by` returns {}: no dependency table means no dependencies, which
    is an answer and not an error.
    """
    if not has_deps_table(conn):
        return []
    return [row["task_id"] for row in conn.execute(DEPENDENTS_SQL, (task_id,))]


def annotate(rows, blockers: dict[str, list[str]]) -> list[dict]:
    """Task rows as plain dicts, each carrying its computed `blocked_by`.

    Always present, always a list — an empty one means "nothing is holding
    this up", which is a different statement from "we did not look".
    """
    out = []
    for row in rows:
        item = dict(row)
        item["blocked_by"] = blockers.get(item["id"], [])
        out.append(item)
    return out

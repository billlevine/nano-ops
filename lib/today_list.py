"""Read ordered, pinned Today tasks from an installation's estate store.

This module only reads. An installation may provide its own writer over
ordinary task rows; the public core does not ship that command adapter.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import estate_attention

KIND = "today"
# At most this many items. A sixth demotes the oldest unpinned one to the
# follow-up store when its writer enforces the configured TODAY_CAP.
CAP_DEFAULT = 5
CAP_ENV = "TODAY_CAP"

RANK_KEY = "rank"
PINNED_KEY = "pinned"


def cap() -> int:
    raw = (os.environ.get(CAP_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return CAP_DEFAULT
    return value if value >= 1 else CAP_DEFAULT


def decode_refs(row) -> dict:
    try:
        value = json.loads(row["refs"] or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def open_rows(conn) -> list:
    """Every open Today row, in list order: stored rank, then filing order."""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE kind=? AND status='open' ORDER BY seq",
        (KIND,)).fetchall()

    def order(row):
        rank = decode_refs(row).get(RANK_KEY)
        return (rank if isinstance(rank, int) else 10**6, row["seq"])
    return sorted(rows, key=order)


def item(row, position: int, now: dt.datetime, zone=None) -> dict:
    refs = decode_refs(row)
    due = row["due_at"] if "due_at" in row.keys() else None
    state = estate_attention.classify(due, now, tz=zone) if due else None
    return {
        "id": row["id"],
        "rank": position,
        "text": row["title"],
        "pinned": bool(refs.get(PINNED_KEY)),
        "due_at": due,
        "due_state": state,
        "overdue": state == estate_attention.OVERDUE,
        "ref": refs.get("ref"),
        "from_followup": refs.get("from_followup"),
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


def items(conn, now: dt.datetime | None = None, zone=None) -> list[dict]:
    """The list as a reader sees it: ranks are positions, 1..n."""
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = zone or estate_attention.local_zone()
    return [item(row, n, now, zone)
            for n, row in enumerate(open_rows(conn), start=1)]


def oldest_unpinned(listed: list[dict]):
    """The item a full list gives up: filed earliest, and not pinned."""
    candidates = [i for i in listed if not i["pinned"]]
    if not candidates:
        return None
    return min(candidates, key=lambda i: (i["created_at"] or "",
                                          int(str(i["id"]).split("-")[-1])))

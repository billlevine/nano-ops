"""Exact-match search over the estate's append-only events table (P-16).

The store already answered "what happened to THIS item" — `estate log t-72`
walks one task's events. What it could not answer is the cross-item question:
"everything the mechanic did", "every event on project p-1", "what that one
dispatched worker touched". The gap audit (S46) called this out: item logs are
exact, but an operator cannot search complete history by subsystem actor across
items, and bin/dashboard's drill-down was reading a recent tail rather than the
authoritative table.

This module is the one query surface for both readers — `estate events` and
bin/dashboard — so the two can never drift into answering the same question
differently.

Three properties the filters are built around:

  EXACT. Every filter is `=` on an indexed column, never a LIKE or a substring.
  `--actor mechanic` must never surface a hub row, and `--task t-7` must never
  surface `t-70`. The one deliberate exception is actor ALIASING: the taxonomy
  says a session's persona name and its technical name are the same writer
  (docs/actor-taxonomy.md), so searching for the canonical name finds every raw
  spelling of it and nothing else. That is still exact — it is exact on the
  normalized actor.

  INDEPENDENT. Filters compose with AND and none implies another, so
  `--actor hub --project p-1` is "the hub's events on that project" and
  dropping either widens the result in the obvious direction.

  SEQUENCE-ORDERED, always. `seq` is the events table's insertion order and
  the only total order the store has; timestamps can tie. `--limit` takes the
  most RECENT n and still prints them oldest-first, so a truncated read is a
  suffix of the full one rather than a differently-sorted list.

One deliberate exception to the first property lives at the bottom of this
file: the sequence RANGE pair P-13 needs to ask an interval question. It is
still exact — exact on the store's own total order — and it is the only filter
here that is not an `=`.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

import actors

# filter name -> the events column it matches exactly.
FILTER_COLUMNS = {
    "actor": "actor",
    "task": "task_id",
    "project": "project_id",
    "memory": "memory_id",
    "ask": "ask_id",
    "session": "session_id",
    "kind": "kind",
    # P-06. `--subsystem` is S43/S46's "find this subsystem's events" asked
    # directly, rather than through the actor: `tasks`, `memory`, `dashboard`
    # and `ledger` are subsystems that no session's actor name reaches.
    "subsystem": "subsystem",
    "phase": "phase",
    "fingerprint": "fingerprint",
    # P-08. `--run mechanic/2026-07-29` is one scheduled run's whole record,
    # and `--run-outcome failed` is every run that broke, across subsystems.
    # Exact like the rest: a run id is a natural key, so `mechanic/2026-07-2`
    # must never reach `mechanic/2026-07-29`.
    "run": "run_id",
    "run_outcome": "run_outcome",
}

# The one RANGE pair (P-13), kept deliberately apart from FILTER_COLUMNS above
# so "every filter in that table is an `=`" stays a true sentence.
#
# An INTERVAL is the question a cursor asks: "everything after the brief that
# was actually delivered, up to the cutoff this one pinned". No exact filter can express
# it, and no timestamp filter can express it CORRECTLY — two events can share a
# `ts`, so a `ts >` bound either re-reports one or drops one, and which of the
# two you get depends on write order the store does not record.
#
# The low end is EXCLUSIVE and the high end INCLUSIVE, on purpose. `since_seq`
# is the last sequence already reported, so it is never reported again;
# `until_seq` is the cutoff this reader pinned, so it belongs to the interval
# it describes rather than to the first row of the next one. Two consecutive
# intervals therefore cover the history exactly once between them, with no gap
# and no overlap — which is the whole property a cursor exists to have.
RANGE_FILTERS = {"since_seq": ("seq", ">"), "until_seq": ("seq", "<=")}

# Columns added by bin/estate's migrate(). A read-only reader (the dashboard)
# cannot migrate, so filtering on one of these against an old store has to fail
# loudly rather than silently return everything.
MIGRATED_COLUMNS = ("memory_id", "ask_id", "session_id", "subsystem", "phase",
                    "fingerprint", "run_id", "run_outcome")


class MissingColumn(LookupError):
    """A filter named a column this store has not been migrated to have."""


def available_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(events)")}


def actor_clause(canonical: str) -> tuple[str, list]:
    """Match every raw actor spelling that normalizes to `canonical`.

    The alias table is a closed set, so this stays an `IN (…)` over literal
    values — no pattern matching, except for `ephemeral`, whose historical raw
    rows carry their slug in the actor field itself (`ephemeral-<slug>`) and
    are folded by the taxonomy rather than by a rewrite.
    """
    variants = {canonical}
    variants |= {raw for raw, target in actors.ALIASES.items()
                 if target == canonical}
    if canonical == actors.EPHEMERAL:
        variants |= set(actors.LEGACY_DISPATCHES)
        placeholders = ",".join("?" for _ in variants)
        return (f"(actor IN ({placeholders}) OR actor LIKE ?)",
                sorted(variants) + [actors.EPHEMERAL_PREFIX + "%"])
    placeholders = ",".join("?" for _ in variants)
    return f"actor IN ({placeholders})", sorted(variants)


def build_query(filters: dict, columns: Iterable[str],
                limit: int | None = None) -> tuple[str, list]:
    """SQL + params for one exact-match event search.

    `filters` maps FILTER_COLUMNS keys to values; None/empty values are
    dropped, so a caller can pass its whole argument namespace through.
    """
    columns = set(columns)
    clauses, params = [], []
    for name, value in sorted(filters.items()):
        if value in (None, ""):
            continue
        if name in RANGE_FILTERS:
            column, op = RANGE_FILTERS[name]
            # `seq` is the primary key, so it predates every migration and
            # needs no availability check — but the bound has to be a number.
            # A string here would compare as text in SQLite and silently answer
            # a different question, which is worse than refusing.
            try:
                bound = int(value)
            except (TypeError, ValueError):
                raise KeyError(f"event filter '{name}' needs an integer "
                               f"sequence, got {value!r}") from None
            clauses.append(f"{column}{op}?")
            params.append(bound)
            continue
        column = FILTER_COLUMNS.get(name)
        if column is None:
            raise KeyError(f"unknown event filter '{name}'")
        if column not in columns:
            raise MissingColumn(
                f"this store has no events.{column} column; run `estate init` "
                "to migrate it")
        if name == "actor":
            clause, values = actor_clause(value)
            clauses.append(clause)
            params += values
        else:
            clauses.append(f"{column}=?")
            params.append(value)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    if limit:
        # Most-recent n, then re-sorted ascending: a truncated read is the tail
        # of the full sequence, never a reordering of it.
        return (f"SELECT * FROM (SELECT * FROM events{where} "
                f"ORDER BY seq DESC LIMIT ?) ORDER BY seq", params + [limit])
    return f"SELECT * FROM events{where} ORDER BY seq", params


def select(conn, filters: dict, limit: int | None = None) -> list[sqlite3.Row]:
    sql, params = build_query(filters, available_columns(conn), limit)
    return list(conn.execute(sql, params))

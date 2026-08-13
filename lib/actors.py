"""Canonical actor taxonomy for the estate's audit streams.

One writer, one name. Without this module a session that has both a technical
name and a persona name writes ledger rows under each of them, a renderer's
operator styling fires on only half of them, and /estate.html reports one
operator as two separate totals.

The rule is: the actor field carries the TECHNICAL name, never the persona
name. A loop writes its registered loop name, the mechanic writes `mechanic`,
and the hub writes `hub`. A persona is a session's voice, not its identity, and
personas belong in prose.

Ephemeral dispatch sessions are deliberately anonymous: they are not standing
identities, so they all share the actor `ephemeral` and carry their own slug in
`session_id`. Aggregates stay readable and a single dispatch is still
traceable.

Read-side, not rewrite-side. `normalize` is applied when rows are read, so
every historical row folds into the taxonomy without touching an append-only
store. Writers normalize too, so new rows land canonical.

Docs: docs/actor-taxonomy.md
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

# ── canonical actors ─────────────────────────────────────────────────────────

OPERATOR = "hub"          # the long-lived orchestration session
EPHEMERAL = "ephemeral"   # any dispatched worker session; identity lives in session_id
UNKNOWN = "unknown"       # actor field missing or blank

# ── classes ──────────────────────────────────────────────────────────────────
# A class is what a renderer styles and groups on. Actors come and go as loops
# are added; the classes are stable.

CLASS_HUMAN = "human"
CLASS_OPERATOR = "operator"
CLASS_LOOP = "loop"
CLASS_GARAGE = "garage"
CLASS_TOOL = "tool"
CLASS_EPHEMERAL = "ephemeral"
CLASS_OTHER = "other"

CLASSES = (CLASS_HUMAN, CLASS_OPERATOR, CLASS_LOOP, CLASS_GARAGE,
           CLASS_TOOL, CLASS_EPHEMERAL, CLASS_OTHER)

# ── membership ───────────────────────────────────────────────────────────────

# Same actor, two spellings. Left side is any accepted spelling, right side is
# the canonical technical name. Only genuine duplicates belong here — merging
# two actors that are actually different people or sessions loses history.
#
# EMPTY BY DEFAULT, and deliberately so. Every entry an installation needs here
# is one of its OWN identities: the persona name its hub session answers to, or
# a name it used before it settled on the current one. Those are installation
# facts, not mechanism, so they are added in the installation's own copy —
# `ALIASES["<persona>"] = OPERATOR` — and never shipped here.
ALIASES: dict[str, str] = {}

# Dispatch sessions from before the `ephemeral-` prefix convention. Each one
# named a piece of work rather than a standing identity, so each is a session
# id, not an actor. Also empty by default and for the same reason: the list is
# whatever is already on ONE installation's disk, and it is closed once that
# installation writes it.
LEGACY_DISPATCHES: frozenset[str] = frozenset()

# The people who write rows by hand. An installation adds its own operators;
# shipping a name here would be shipping an identity.
HUMANS: frozenset[str] = frozenset()

GARAGE_SESSIONS = frozenset({"garage"})
# `memory` belongs here too: a promotion is the Memory subsystem's own act,
# recorded under its own name rather than under whoever invoked the CLI. It is
# a subsystem writing on its own behalf, not a loop with a state dir, so it
# classifies here and not as `loop`.
TOOLS = frozenset({"ops", "cli", "estate", "doorbell", "memory"})

# Fallback for callers with no loops.toml in hand: the loops this repo actually
# ships. `normalize(loops=...)` takes the live registry when there is one, so an
# installation's own loops are classified without editing this file.
KNOWN_LOOPS = frozenset({"example", "mechanic"})

EPHEMERAL_PREFIX = "ephemeral-"


class Actor(NamedTuple):
    """A normalized actor: who to aggregate on, and which session it was."""

    actor: str
    session_id: str | None
    actor_class: str

    @property
    def label(self) -> str:
        """Full-width display name, e.g. `ephemeral · dashboard-prototype`."""
        return f"{self.actor} · {self.session_id}" if self.session_id else self.actor

    @property
    def short(self) -> str:
        """Narrow display name for the ledger strip, e.g. `eph:dashboard-prototype`."""
        return f"eph:{self.session_id}" if self.session_id else self.actor


def normalize(actor, session_id: str | None = None,
              loops: Iterable[str] | None = None) -> Actor:
    """Fold one raw actor string into the taxonomy.

    `session_id` wins when the writer already recorded one; otherwise an
    ephemeral actor's own string becomes its session id, which is what makes
    the historical `ephemeral-*` rows traceable without a backfill.
    """
    raw = (actor or "").strip()
    if not raw:
        return Actor(UNKNOWN, session_id or None, CLASS_OTHER)

    if raw == EPHEMERAL:
        return Actor(EPHEMERAL, session_id or None, CLASS_EPHEMERAL)
    if raw.startswith(EPHEMERAL_PREFIX):
        return Actor(EPHEMERAL, session_id or raw[len(EPHEMERAL_PREFIX):] or None,
                     CLASS_EPHEMERAL)
    if raw in LEGACY_DISPATCHES:
        return Actor(EPHEMERAL, session_id or raw, CLASS_EPHEMERAL)

    canonical = ALIASES.get(raw, raw)
    return Actor(canonical, session_id or None, classify(canonical, loops))


def classify(canonical: str, loops: Iterable[str] | None = None) -> str:
    """Class of an already-canonical actor name."""
    if canonical == OPERATOR:
        return CLASS_OPERATOR
    if canonical == EPHEMERAL:
        return CLASS_EPHEMERAL
    if canonical in HUMANS:
        return CLASS_HUMAN
    if canonical in GARAGE_SESSIONS:
        return CLASS_GARAGE
    if canonical in TOOLS:
        return CLASS_TOOL
    if canonical in (set(loops) if loops is not None else KNOWN_LOOPS):
        return CLASS_LOOP
    return CLASS_OTHER


def normalize_row(row: dict, loops: Iterable[str] | None = None) -> dict:
    """Copy of an event/ledger row with its actor folded into the taxonomy.

    The raw string is kept as `actor_raw` so nothing a writer recorded is lost
    on the way to a renderer.
    """
    out = dict(row)
    who = normalize(row.get("actor"), row.get("session_id"), loops)
    out["actor"] = who.actor
    out["actor_raw"] = str(row.get("actor") or "")
    out["session_id"] = who.session_id
    out["actor_class"] = who.actor_class
    return out

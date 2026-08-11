"""Scope-safe memory recall (gap audit P-05).

The redesign's §5 gave every memory a `scope` — `shared`, or the name of the
loop that learned it — and gave `memory list` / `memory query` an OPTIONAL
`--scope` filter. That is where it stopped. Four scenarios fall through the
gap between "a caller could filter" and "every caller does":

  S35  a LOCAL fact is recalled in its own context and NOT in another one.
  S36  a SHARED fact reaches every context, labelled as global at the moment
       it is recalled — not only in a nightly `docs/lessons.md` render.
  S38  an unpromoted local fact never influences another context. Today it
       cannot, but only because no consumer reads memory at all: the isolation
       is an accident of the missing read path, not a property of it.
  S53  the four named consumers — hub, mechanic, night shift, briefer — apply
       one common scope contract, rather than each inventing a filter.

WHAT THIS MODULE IS
-------------------
The recall contract and no I/O, in one place, so `bin/estate` (which serves
recall) and every consumer that reads its output cannot disagree about what a
scope means. The query itself lives in `bin/estate memory recall`; that command
is the ONLY operational read path, and the four consumers reach it by shelling
out, never by opening the store.

THE CONTRACT, IN ONE SENTENCE
-----------------------------
Recall at scope X returns every ACTIVE memory whose scope is X or `shared`,
each labelled with the scope it is actually stored at.

Three halves of that sentence are load-bearing:

  ACTIVE ONLY. A `candidate` is a fact nobody has judged yet — §5.4 files them
  cheaply and defers the judgment — and promotion is what makes a fact
  recallable (P-19 records it as the Memory subsystem's own act for exactly
  that reason). Recalling candidates would make promotion decorative and would
  hand a consumer unjudged text as if it were settled. This is also S38: an
  unpromoted local fact influences nothing, anywhere, including its own loop.

  X OR `shared`, never a third scope. Local isolation is the whole point. No
  wildcard, no "all", no substring — the union is exactly two sets.

  LABELLED WITH THE STORED SCOPE. S36 asks that a global fact be visibly global
  where it is used. A row is printed with the scope it is stored at, never with
  the caller's, so a reader can always tell which of the two sets it came from.

WHY A REQUESTED SCOPE IS FOLDED
-------------------------------
An estate ends up with two names for the same identity in two vocabularies: the
TECHNICAL actor name (`pr-reviewer`, `morning-brief` — docs/actor-taxonomy.md)
and the audit's subsystem name (`night-shift`, `briefer` —
docs/ledger-contract.md). Both are correct in their own place, and a caller
holding one of them must not get a different answer from a caller holding the
other: a memory filed at `night-shift` and recalled at `pr-reviewer` would be
invisible to the very loop that learned it. That is the same split identity
docs/actor-taxonomy.md exists to close — one writer under two names is one
writer whose rows only half-match every query asked about it.

So a requested scope is folded to its canonical technical name and matched
against EVERY spelling of that one identity. Folding never widens: the aliases
below are two names for one loop, never two loops. And because each row still
prints its own stored scope, a fold can never hide where a fact actually lives.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No ranking, no relevance scoring, no summarisation. Recall hands back the
active set for a scope and the reading is the consumer's; a filter that decided
which lessons "matter" would be an unauditable judgment sitting between a fact
and the session it was written for.

Docs: docs/memory-recall-contract.md
"""
from __future__ import annotations

from typing import Iterable

# The one global scope. Named here rather than re-spelled per consumer.
SHARED = "shared"

# Only `active` is recallable. Named as a tuple because the SQL builds an IN
# clause from it, and because a future second recallable status has to be a
# deliberate edit here rather than a `status != 'archived'` drifting somewhere.
RECALLABLE = ("active",)

# Two names, one identity. Left side is any accepted spelling, right side is
# the canonical technical name (docs/actor-taxonomy.md rule 1: the actor field
# carries the TECHNICAL name). Kept in step with lib/estate_ledger.py's
# SUBSYSTEM_BY_ACTOR and lib/actors.py's ALIASES; the estate has no third
# vocabulary, and adding one would be a deliberate edit in all three. An
# installation whose hub session answers to a persona name adds that spelling
# here in its own copy, the same way it adds it to lib/actors.py's ALIASES.
SCOPE_ALIASES = {
    "night-shift": "pr-reviewer",   # the audit's name for the pr-reviewer loop
    "briefer": "morning-brief",     # ... and for the morning-brief loop
    "spotter": "pr-tracker",        # ... and for the pr-tracker loop
}

# The ref key a consumer writes into the task or Ledger row that came out of an
# intake. One key, one shape, so `estate events` can be grepped for what a
# subsystem knew when it acted.
REF_KEY = "memory_recall"

# How one recalled fact is named inside that ref: `m-12@shared`. The id alone
# would make the scope unrecoverable once the row moved (a promotion changes a
# memory's scope, and the record is of what was recalled THEN).
def ref_token(memory_id: str, scope: str) -> str:
    return f"{memory_id}@{scope}"


class ScopeError(ValueError):
    """A recall scope that cannot be honoured."""


def canonical(scope: str) -> str:
    """The canonical technical name for a requested scope.

    An unknown scope is its own canonical form, kept verbatim — the same
    position lib/actors.py takes on an unrecognized actor. A loop added
    tomorrow recalls correctly with no edit here.
    """
    name = (scope or "").strip()
    if not name:
        raise ScopeError("--scope must be non-empty: recall is always from "
                         "somewhere, and a blank scope would silently mean "
                         "'shared only'")
    return SCOPE_ALIASES.get(name, name)


def local_spellings(scope: str) -> tuple[str, ...]:
    """Every stored spelling of ONE local identity, canonical first.

    `shared` is not among them even when it is what was asked for: it is the
    global set, added by recall_scopes() and never treated as somebody's local
    scope.
    """
    key = canonical(scope)
    if key == SHARED:
        return ()
    others = sorted(name for name, to in SCOPE_ALIASES.items() if to == key)
    return (key, *others)


def recall_scopes(scope: str) -> tuple[str, ...]:
    """The exact set of stored scopes recall at `scope` may return.

    Local spellings first, `shared` last — which is also the order results are
    rendered in, so a consumer reading top-down sees its own context before the
    estate-wide one.
    """
    return (*local_spellings(scope), SHARED)


def origin(row_scope: str) -> str:
    """`local` or `shared`, for a row that recall is about to hand back.

    Two words rather than a boolean because this is printed, and "shared" is
    the word S36 asks to see at the moment of use.
    """
    return SHARED if (row_scope or "").strip() == SHARED else "local"


def envelope(scope: str, rows: Iterable) -> dict:
    """The refs object a consumer records on whatever the intake produced.

    Shape:

        {"memory_recall": {"scope": "mechanic",
                           "memories": ["m-3@shared", "m-9@mechanic"]}}

    An EMPTY list is written, not omitted. "Recall ran and the estate had
    nothing for me" and "recall never ran" are different facts, and P-08 spent
    a whole proposal on keeping exactly that distinction visible for scheduled
    runs. The same reasoning applies to a fact set.
    """
    return {REF_KEY: {"scope": canonical(scope),
                      "memories": [ref_token(r["id"], r["scope"]) for r in rows]}}

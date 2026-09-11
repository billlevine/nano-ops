# The absence contract

What it takes for an empty reading to be evidence that something is *gone*, and
what the estate is allowed to conclude when it is not. Estate vision V-02.

Primitive: `lib/estate_observation.py`. Tests: `tests/test_estate_observation.py`.
Adopted by `lib/brief_manifest.py`. Two subsystem engines already implement
this rule in their own words — a spotter that tracks review items, and a
briefer that gathers a morning summary — and each keeps a local contract of its
own. Those loops carry an installation's policy and live in an installation's
own fork rather than here; this document is the rule they share, and each of
theirs is the local spelling of it plus what only that subsystem does.

Also governed elsewhere: what gets recorded and where — the ledger contract,
which owns the scheduled-run reconciler that is deliberately *not* governed by
this one. See "What is outside this contract" below; that section
is the reason this document exists in the shape it does.

## The problem it fixes

Two gap audits found the same bug in two places and fixed it twice.

```
gh pr list … → non-zero  →  []      # GitHub is down
gh pr list … → "[]"      →  []      # there is genuinely nothing there
```

P-03: the spotter read a `gh` timeout as "your PR is finished", archived the
board and announced `DONE`. P-11: the briefer read the same timeout as "quiet
night" and rendered `0 landed · 0 opened`.

Both fixes are correct and neither is being replaced. What was missing is that
nothing carried the rule from the second subsystem to the third. Each built its
own outcome vocabulary, its own status mapping and its own predicate, so a
fourth subsystem inherited none of it and would have had to be bitten before it
learned. This contract is the rule with no subsystem attached to it.

## The rule

> **An empty reading is evidence of an absence only when somebody looked, the
> look worked, and it covered everything the absence would have to hide in.**

## The four

Four things a reading can be, and they are not three:

| warrant | meaning | absence is evidence |
| --- | --- | --- |
| `observed` | looked, and what it found is what is there | **yes** |
| `failed` | looked and broke — non-zero, unparseable, timed out, 502 | no |
| `not_attempted` | a look was owed and did not happen — no tool, no credential, no state file | no |
| `not_due` | no look was owed — skipped by request, or nothing in scope could have produced it | no |

`failed` and `not_attempted` are kept apart because one is weather and one is
setup. "GitHub returned 502" and "`gh` is not installed" are not the same
morning, and a single word for both hides whichever one matters today.

`not_attempted` and `not_due` are kept apart because one is a hole and the
other is a boundary. An operator chases the first and ignores the second.
Collapsing them turns every deliberate skip into a false alarm and every real
hole into an accepted silence.

Only `observed` warrants an absence claim. In `lib/estate_observation.py` that
is a table (`ABSENCE_IS_EVIDENCE`) rather than a branch, so a fifth class or a
second `True` is a visible edit rather than a condition somebody widened.

## `partial` — a modifier, not a fifth class

A reading that saw *some* of what it asked for is still `observed`: what it saw
is real work, and dropping it would lose it. What it may never do is prove an
absence, because the part nobody read is exactly where an absence would hide.

```
absence_is_evidence = (warrant is observed) and not partial
```

It is not a class of its own because it answers a different question. The
warrant says what happened to the look. `partial` says how much of the subject
it covered. A reading that is `failed` or `not_attempted` read no part of its
subject at all, so `reading()` refuses `partial` there rather than recording a
coverage claim on a reading with no coverage.

This is the briefer's `degraded` and the spotter's "a partial page set is still
returned", stated once. The two engines still spell their own answer their own
way — the briefer keeps the source `completed` and marks it degraded, the
spotter drops `ok` on the whole source — and both reach the same verdict on the
only question this contract settles.

## Vocabularies, and why the registry is the enforcement

Subsystems keep their own outcome words on purpose. `timeout` matters to the
briefer and `no_credential` matters to the spotter, and forcing both onto four
words would delete the vocabulary the operator actually reads. What must not
vary is the **classification**, so a subsystem registers its words in
`lib/estate_observation.py` and gets the predicate derived from them.

```python
SPOTTER = register("spotter", {
    "ok": OBSERVED,
    "error": FAILED, "unreadable": FAILED, "unreachable": FAILED,
    "unavailable": NOT_ATTEMPTED, "no_credential": NOT_ATTEMPTED,
    "unqueried": NOT_DUE,
})
```

Three properties make an unwarranted reading impossible to build rather than
merely discouraged. This is the difference between a primitive and a validator
somebody has to remember to call:

- **`reading()` has no `absence_is_evidence` parameter.** There is nowhere to
  put one. It is derived from the warrant on every call, so a caller cannot
  claim a warrant its reading does not have, and a stored reading cannot drift
  from the rule that produced it. Reading the predicate back
  (`absence_is_evidence(row)`) recomputes it too, so a hand-edited record
  cannot claim it either.
- **There is no default class.** An outcome that is not in the vocabulary
  raises `UnwarrantedReading`, naming the four and telling the author to pick
  one. A new word cannot quietly inherit `observed` by saying nothing, which is
  precisely how the original bug looked in code.
- **A vocabulary with no `observed` outcome is refused at registration.** A
  subsystem that can never conclude an absence has a broken table, not a
  cautious one: every absence it saw would be held forever with no way out.

## Composing readings

The spotter asks "can this refresh prove the entry left the board" per item,
over the sources that could have produced it. The briefer asks "may the brief
as a whole be read as a full picture" per morning, over all ten gatherers.
Same question, same answer, one function (`absence_proved`):

- **every** relevant reading must be `observed` and not partial;
- and there must be **at least one**. The empty set is `not_due` and never
  proved — nothing looked, which is not the same as nothing being there. That
  is the spotter's board entry whose repo left `config.toml`, and the manifest
  with no sources in it.

Requiring *all* of them is deliberate over-caution in one direction. Holding an
item one refresh too long costs a stale row that the next good look clears.
Concluding it is gone reports that work as finished. Those are not symmetric
mistakes.

When an absence cannot be proved, the blocking readings come back worst first —
`failed`, then `not_attempted`, then `not_due` — because a reader who stops
after the first line should have been told about the thing that broke, not the
thing that was skipped.

## Who has adopted it

| subsystem | how |
| --- | --- |
| `briefer` | `lib/brief_manifest.py` derives `STATUS_FOR_OUTCOME` from the registered vocabulary and answers `empty_is_evidence` from the warrant. Its three statuses are a projection of the four classes. `brief.py` keeps its own checked copy, drift-checked against the registry. |

The adoption tightened one case. `empty_is_evidence` now requires **both**
recorded halves of the warrant to agree: an envelope claiming `completed` over
a `timeout` has no coherent warrant, and a reading with no coherent warrant is
not evidence. It used to read as a quiet morning. An outcome this estate has
never defined is answered on the status alone — the unknown half can withhold
the answer, never supply one.
| `spotter` | vocabulary registered and drift-checked against `track.py`'s `SOURCE_*` constants. The engine's own classification is unchanged — the registry is what a third subsystem now reads instead of re-deriving it. |
| `coverage` | `bin/outcome-coverage` (t-388) counts how many times each defined outcome was actually reached, so its whole product is a set of zeroes a reader has to be able to trust. Each of its three sources — the estate store, the night shift's JSONL, `loops.toml` — is one `reading()`, and an outcome whose sources cannot support an absence prints `unobservable` instead of `0`. Its third verdict is this module's refusal rather than a second scheme, and `partial` is used for real: a JSONL line that would not parse is exactly where the one occurrence of an unexercised path would hide, so the counts stand and the absence claim does not. |
| `dashboard` | `lib/dashboard_panels.py` gives each of the nine panels an envelope of the same shape, derives its three statuses and its predicate from the registered vocabulary, and renders a panel that could not be read differently from one that read and found nothing. `docs/dashboard-panel-contract.md`. |
| `doorbell` | `bin/doorbell read` (t-782) is the hub's read-only fallback when the Slack MCP connector is down, and it is the estate's clearest use of `partial`. Slack answers an `oldest`-bounded `conversations.history` with the NEWEST messages in the window and reports truncation in `has_more` / `response_metadata.next_cursor`; the improvised fallback of 2026-08-16 fetched that flag and discarded it. A truncated read is `observed` and partial — the messages it returned are real work, and the one thing it may not do is prove the inbox is quiet, because a cursor advanced past it skips the remainder permanently. The polling daemon does not import this module: the registry is reached only on the `read` path, and there is no fallback if the import fails. |
| `credential` | `bin/credential-expiry` (t-978) warns 24 hours before Claude Code's OAuth refresh token lapses, and the answer it must never manufacture is the reassuring one. "Nothing is expiring" is an absence claim about the next day, and a credentials file that is missing, unopenable or shaped differently than it used to be produces exactly the same absence of a warning as a healthy login. Its third verdict, `unobservable`, is this module's refusal rather than a second scheme, and it POSTS to the owner in its own words instead of falling silent — the incident it answers, a lapsed refresh token taking the hub and every loop down together for hours, is made of exactly that silence. Five outcome words: `no_credential` versus `no_access` is the spotter's setup-versus-permission split, and `unreadable` versus `malformed` is weather versus contract — bytes that are not JSON mean a truncated write, JSON with no `claudeAiOauth.refreshTokenExpiresAt` in it means the credential format moved under us, and only the second would otherwise pass forever as "no expiry to report". |

The dashboard is the first subsystem that did **not** have to be bitten first.
P-03 and P-11 each learned this rule from an outage; t-390 found the same bug
in nine more places and fixed it by passing through this registry, which is the
thing this module was built to make possible. It imports `brief_manifest`'s
three statuses and freshness words on purpose — those are the presentation
half, and the two panels share one chip and one browser-side age ticker, so a
second copy would be two names for one dot. The warrant half is its own, which
is what the registry is for.

The briefer's three statuses are a projection, not a rival scheme:

| warrant | manifest status |
| --- | --- |
| `observed` | `completed` |
| `failed` | `failed` |
| `not_attempted` | `unavailable` |
| `not_due` | `unavailable` |

`not_attempted` and `not_due` render the same dot because the source strip
wants one mark for "this one did not answer" and the reason in the outcome
beside it. Nothing upstream loses the distinction: the manifest stores the
outcome, and the outcome is what the predicate reads.

**Loop engines do not import `lib/`.** They run against a fabricated repo root
in their own tests, the same rule `lib/estate_ledger.py` states. So the binding
for `track.py` and `brief.py` is a drift check, exactly as it is for the phase
and run vocabularies in the subsystem-event tests.

## What is outside this contract

**The scheduled-run reconciler (P-08, `bin/estate runs`).** It looks like a
third copy of this rule and it is not one, and the distinction is load-bearing
enough to state where somebody will look for it.

Everything in this contract is a **first-person** warrant: the thing that did
the looking reports on its own attempt, at the time it made it. P-08 exists
precisely because that report is the thing that is missing — a pass that died
before writing anything left no row at all. So `estate_runs.reconcile` is
**third-person and reconstructive**: it derives a verdict after the fact from a
schedule, an observability floor, and collateral evidence in the Ledger.

The vocabularies do not correspond, in both directions:

- it has **no `failed`** in this contract's sense. There is no way for the
  reconciler to report "I could not read the events table"; if that read
  breaks, the command breaks;
- **`activity_only` and `incomplete` have no counterpart here.** Both say a run
  demonstrably happened and its *outcome* is unrecorded — a claim about a
  record, which cannot arise where the observer and the recorder are one
  synchronous call;
- **`unobservable` is not `not_attempted`.** It says the recording mechanism did
  not exist in that era, which is a fact about the store's history rather than
  about any attempt;
- **"not due" never becomes a verdict at all.** `due_slots` declines to
  enumerate a slot whose window has not closed, so the case is handled by
  exclusion rather than by a word.

The estate already refused this merge once, on the record, and the refusal is
live in code. `brief.py`'s `runs_section` and `attention_section` are pinned
*not* degraded on `unobservable` slots and `unreadable` due dates, because
restating P-08's and P-07's distinctions in P-11's vocabulary "would give the
estate two words for one thing and a way for them to disagree"
(the briefer's own manifest contract). Importing this primitive into
`estate_runs.py` would be that merge, so
`tests/test_estate_observation.py` asserts that it does not.

**P-07's attention states** are outside for the same reason. `unreadable` is a
due date that is *present* in the answer and could not be parsed — a property
of a row, not of a look.

## What is deliberately not here

- **A second store.** Nothing in this module persists anything. A reading is a
  dict its subsystem puts wherever that subsystem already keeps its telemetry:
  the spotter's `state.json`, the briefer's manifest. There is nothing to keep
  in sync.
- **A shared word list.** The four classes are shared; the outcome words are
  not, and flattening them would delete the only vocabulary the operator reads.
- **A cap on how long an unproved absence may be held.** Both engines already
  refuse one, and for the same reason: a cap is a timer that eventually
  concludes an absence on no evidence at all, which is this bug with a delay.
- **Rewriting the two engines.** Both implementations are correct. The registry
  binds their classification; their held-entry handling, their Ledger rows and
  their surfaces stay where their own contracts put them.

## Adding a subsystem that records observations

1. Register its outcome words in `lib/estate_observation.py`, classing each as
   one of the four. The module will refuse a table that cannot conclude an
   absence.
2. Build every reading with `reading()`. There is no way to record one without
   its warrant.
3. Decide an absence with `absence_proved()` over the readings that could have
   produced the thing, never over a bare empty list.
4. If the engine keeps its own copy of the vocabulary (a loop engine will), add
   a drift check in `tests/test_estate_observation.py`.

You do not need to have read the spotter's contract, the briefer's, or the
Ledger's to get this right.

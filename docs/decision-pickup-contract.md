# The decision-pickup contract

A recorded decision does not prove something acted on it.

Implementation: `lib/estate_pickup.py`; writers are `estate proposal stage
--chosen`, `POST /proposal-stage` and `estate pickup`; the reader is
`estate decided-not-acted`.

## Record the choice in history

The approval event's refs carry a 1-based `chosen.position`, the option text,
its consequence and the alternatives as they were when approved. Store these
on the event, never only on the mutable task. An amendment replacing the
alternatives must not change what an earlier position meant.

Approving a decision requires a readable chosen alternative. Approving an
action, clarification or legacy row requires no choice and refuses one.
Non-approval transitions refuse choices. Booleans are not positions.
Unreadable alternatives require an amendment, not a bypass.

Only option cards carry positions. Recommendation and defer cards clear a
selection rather than interpreting prose as an option. The transmitted
position comes from the stored array, rather than a filtered display.

## Pickup signals and routes

A pickup is an event after the approval with one of three signals:

- Explicit `refs.pickup` written through `estate pickup`.
- Queue evidence carrying `queue_key`.
- A `ready -> claimed` transition.

A note and an automatic ready promotion are not pickups. Supported routes are
`night-shift`, `dispatch`, `hub`, and `parked`. Parked records intentional
inaction with its reason; it is not an executor. Executor implementation and
policy belong to the installation.

`estate pickup` records history without claiming the task. It requires a
proposal at `approved-backlog`, a valid route and a sentence of evidence.

## Verdicts

The reader examines nonterminal approved proposals using the latest approval
by event sequence, so reconsideration starts a new clock.

| Verdict | Meaning |
| --- | --- |
| `owed` | No pickup or park, outside the grace window |
| `fresh` | The same, inside the grace window |
| `picked_up` | A qualifying signal followed approval |
| `parked` | Latest marker parks it, or task status is needs-owner/blocked |
| `unobservable` | No approval event establishes the decision instant |

Missing history is checked first and never softened into owed. Then explicit
markers outrank stored parking, derived signals and the clock. A parking
status already belongs to attention and must not produce a duplicate ask.

An append-only event lookup is the record itself, not a periodically fetched
source. It therefore uses no second observation vocabulary. Read failures
still remain unavailable rather than looking like no pickup. Sweep decisions
at a configured hub boundary and route owed work to an actual executor before
recording pickup.

# The memory recall contract

Recall is the operational path for facts a session may know. Curator queries
serve a different purpose.

Implementation: `lib/estate_memory.py`, exposed by `bin/estate memory recall`.

## Scope and status

Recall at scope X returns the entire active set stored at X or `shared`.
It returns no candidates or retired facts, no third scope and no wildcard.
There is no relevance ranking, summarization, cap or silent truncation.
Growth of the active set is a curation problem.

Each row carries its stored scope. JSON also identifies the origin as local or
shared. Installation aliases can fold two names for the same role; they never
combine different roles or widen access. Unknown scope names remain verbatim.

Promotion makes a candidate recallable. An active local fact may also be
promoted to shared scope, recording both old and new scopes. Recall itself is
unchanged by that transition.

## Intake evidence

Consumers recall before deciding at intake and attach the returned refs to the
resulting task or intake event:

```json
{"memory_recall":{"scope":"mechanic","memories":["m-3@shared","m-9@mechanic"]}}
```

Use ids together with the scopes recalled at that moment. An empty list records
a successful empty recall; omitting recall evidence means something different.
The envelope claims availability in scope, not influence on a decision.

A failed recall must say so rather than look like an estate holding no facts.
Read-only recall does not emit one event per fact or poll. It updates the
returned facts' `last_recalled_at` timestamp.

## Operational and curator reads

Consumer engines use `memory recall`, rather than `memory list`, `memory query`,
a rendered lessons file or direct database access. They receive both the facts
and the evidence envelope.

`mechanic.py memory-triage` is a narrow exception: it lists only candidates to
ask the curator for a disposition. It quotes their bodies into proposals and
does not treat them as facts. Its recommendation is explicitly `not recorded`.
This exception authorizes no other unscoped consumer read.

Installing a loop does not make it a curator. Instruction files carry the
operational prohibition, and engine tests can enforce the named read paths;
those checks do not prove what arbitrary session prose will choose to execute.

# The inbox-message contract

A message being acknowledged and its underlying request being completed are
separate facts. Track the message and the work separately.

This is a mechanism contract for installation adapters. The public core ships
`bin/estate` and the dashboard reader; it does not ship `bin/hub-intake` or
`bin/dispatches`. An installation implementing those adapters must enforce the
following rules.

## Identity and states

Mirror every genuinely new message from an inbox configured for full trust as
one task of kind `inbox-message`, before classification. Inbox trust and
escalation destinations are installation configuration.

Identity is `refs.message_id`, derived from the channel and message timestamp.
Opening a message is create-or-resolve, so a replay returns the existing task.
Refs carry `channel`, `message_ts`, `thread_ts` and `inbox`. Do not put
`intake_id` on the mirror: that key identifies the work task for the intent.
Link the mirror and work task with a `related` dependency.

| Status | Meaning |
| --- | --- |
| `ready` | Registered or acknowledged; unresolved |
| `claimed` | A separate worker holds the request |
| `done` | The underlying request is resolved |
| `needs-owner` | Waiting on the operator |

The hub handling its own message does not need a visible worker lease. A real
worker uses the ordinary claim, lease and orphan recovery. Interim progress
uses estate notes, rather than a second status vocabulary.

## Completion evidence

Post a fresh top-level completion or escalation before closing the mirror.
Require the returned Slack timestamp. A failed post leaves the mirror open.
Threaded progress is allowed; a terminal report is top-level.

The timestamp must be strictly newer than `refs.message_ts` and must not
already be top-level completion evidence for a different message task. A
missing original timestamp or failed duplicate lookup is reported as unchecked;
neither supplies positive evidence. Store `slack_ts` and `top_level` with the
status transition in one transaction.

These local checks do not prove a post exists or that it was top-level. Full
verification requires a Slack read. The durable task and dashboard provide a
second way to discover unresolved requests; they cannot guarantee a person
reads a push notification.

## Intake and cursors

Cursor advancement considers mirrors on its own channel strictly between the
committed cursor and the proposed timestamp. Refuse to pass one with no recorded
intent intake only when both intake stores were successfully read. A failed
lookup is reported and cannot prove missing intake. Never move backwards.

A close also records missing intake through the same cursor-advancement path.
Resolve routing before closing: an explicit work task or inline destination
wins; otherwise a successful `task find` for the intent identity can establish
a work task or that none exists. A failed lookup refuses with nothing closed.
An escalation needs no work-task routing. An existing intake is not duplicated.
Advance only when behind, so an out-of-order completion does not regress the
cursor. Derive a cursor from the configured channel, never from a typed inbox
label; unknown configuration yields a warning and no guessed cursor write.
An adapter may expose an explicit opt-out for a separately recorded intake.

## Queries and rollback

`inbox-message` is excluded from the ordinary `ready` and `stale` work pools
unless requested by kind. It remains in task listings, attention, due and orphan
queries. Stopping the mirror adapter preserves history but removes the guard's
ability to detect a read message with no intake. It does not undo rows.

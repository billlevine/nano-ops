# Timer or tick: where recurring work runs

Move work to a deterministic timer only when all three clauses hold:

1. The work is deterministic.
2. It uses no model tokens.
3. Running it is not itself an assertion about what a session did.

The third clause prevents fabricated run records. A windows command that
records a session's run as started cannot be moved to a timer merely because
its calculation is deterministic: it could claim a session worked when none
drained the slot. Session pass boundaries stay with the session.

## Establish the environment

A service does not inherit an interactive shell's credentials, environment or
profile. Measure each candidate under a stripped environment with a pinned
tool path and the installation's home directory. Verify every required read
and write credential, independently. A successful GitHub read says nothing
about an SSH push, tracker read or Slack relay.

Pin the agent-deck profile explicitly on every call. Configuration names the
profile; the service must not silently select another estate's store.
Record failed and unattempted reads as such. A successful empty reading alone
can support an absence claim.

## Preserve the handoff

Spool a deterministic producer's outputs and judgment triggers durably. The
session drains the recorded window and keeps judgment and dispatch authority.
The spool is a handoff, rather than a second authoritative history.

A timer must not refresh a heartbeat used to prove session liveness. Separate
a producer's poll/run timestamp from the session's `last_tick`. A session with
nothing to drain may run the producer itself; it says so and records whether
the source was timer or session. An uncounted fallback conceals a failed timer.

Check cadence agreement between timer and registry before installation. Derive
run identity from the scheduled slot, and keep no_activity distinct from never
ran. A judge depending on a poll must fire after the poll, with an explicit
cadence check rather than an assumption.

## Split candidates by their actual seam

A pure poll or gather can move once its credentials and handoff are measured.
A pass can move its independent deterministic checks while retaining judgment
steps. A drain that writes session-run boundaries remains in the session until
that side effect is factored out. A task with no deterministic half needs a
refactor before scheduling it outside the model.

Use one parameterized timer wrapper where jobs share mechanics, while keeping
each engine's identity and boundary declaration. A shared timer is not evidence
that unmeasured jobs are safe to move. Installed units and actual loop choices
are installation policy; this contract ships neither.

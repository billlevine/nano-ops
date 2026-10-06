# The hub-restart contract

A hub cannot synchronously stop itself and then run its own start command.
Hand the restart to a supervisor whose lifetime is independent of the hub.

This is an adapter contract. The public core uses Flox services and does not
install the private estate's systemd units or ship its `bin/hub-restart`.

## Ownership and scope

The supervisor owns the restart after accepting a trigger. A detached shell,
background job or sleep inside the hub's session is not independent ownership.
Consume the trigger before acting, so a refusal does not create a restart loop.

Resolve the hub's exact identity and agent-deck profile from configuration.
Reap only its recorded tmux name with exact matching; never affect the shared
tmux server. Launch through the installation's operator command so the model,
group and initial schedule come from one declaration.

## Preflight and update

Read and report the complete lifecycle preflight. A hub-context restart does
not automatically mean every outstanding worker must stop: durable dispatches
and tasks survive the operator's context. Strict preflight and a deliberate
force option can be exposed; their semantics must be explicit.

Updating a binding means the instruction bytes the new session reads, not
an implicit dependency upgrade. Refresh origin first, and fast-forward only
when the checkout and ancestry permit it. Never overwrite dirty, ahead,
diverged or off-main work. A failed fetch is "origin unverified", even if the
local ref happens to match the last known remote tip. Record the action and
the observation separately. Runtime reactivation is an explicit choice.

## Prove the outcome

A new-looking pane, a retained registry row's creation time or a process start
is insufficient evidence of fresh instructions. Compare the actual session
binding with the intended commit. Read the verifier's structured verdict;
its exit code alone does not prove a rebind.

A verified binding must also tick. Observe the declared heartbeat for a bounded
window after the kick. Only a complete readable observation can conclude no
tick. Unreadable files are failed readings; a session declaring no heartbeat
is unchecked; a zero-wait invocation proves no liveness. Do not report
"restarted and working" from a binding-only result.

## Records and limits

Every outcome records initiation and the appropriate transition, outcome or
failure in the central event ledger, including preflight refusal and failed
fetch, launch, verification or liveness. Preserve per-step evidence so a retry
can distinguish what happened.

A restart does not prove delivery, successful external work or cancellation of
an old schedule unless those were separately checked. Environment upgrades
and estate-wide shutdown remain separate operator actions.

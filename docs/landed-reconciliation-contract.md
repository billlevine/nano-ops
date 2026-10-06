# The landed-reconciliation contract

A completed task and a landed commit are distinct facts. Landing is observed
from repository history rather than inferred from completion prose.

The core implements landing claims through `bin/estate` and observation
vocabularies. The reconciler executable is an installation adapter and is not
shipped here.

## Claims and corrections

Completion evidence recognizes `landed_sha`, `landed_branch`, `landed_repo`.
They are optional, recorded without validation and never derived by running git
at write time. Explicit flags take precedence over equivalent refs.

`estate re-land` replaces a false claim through the correction path while
preserving prior evidence in append-only history. It corrects the claim rather
than inventing a landing or silently reopening the task.

## The rule and verdicts

A claim landed when its commit is an ancestor of the configured integration
branch, or GitHub reports a merged pull request carrying it. Use the repository
default branch unless an installation explicitly declares another target.

| Verdict | Task status | Meaning |
| --- | --- | --- |
| `landed` | `done` | Positive ancestry or merged-PR evidence |
| `not_landed` | `done` | Neither, after every relevant look worked |
| `landed_pending_close` | `needs-owner` | Landed work whose task remains parked |
| `pending` | `needs-owner` | Not landed after successful looks; no completion claimed |
| `unresolvable` | either | A required look could not be made |

A done task claiming no landing is skipped, rather than counted as a verdict.
A parked task with no claim is outside scope. Report both populations.

## Warranted observations

Refresh the remote before comparing tips. A successful fetch can supply a
current remote tip; a failed fetch leaves an old tracking ref and cannot prove
absence. Offline mode, no git/gh, missing repositories, unknown commits or
integration refs, branch-only claims and failed GitHub reads are unresolvable.

Positive local ancestry is evidence about that local branch even when another
look failed. A negative local answer alone cannot prove `not_landed` when the
remote or merged-PR check was unavailable. Squash merges require the PR look
because the original implementation sha need not appear in integration history.
Keep every reading's warrant beside the verdict.

Resolve repository labels through configured aliases anchored on a fixed base,
before falling back to the working directory. Honor explicit absolute and
home-relative paths. Never silently redirect an unknown label to a plausible
checkout. Print missing alias targets as unresolvable.

An integration exception is installation policy: it identifies a repository,
branch and actual target. Do not ship an operator's permanent branches as core
exceptions, and do not equate an unmerged long-lived integration branch with
unfinished work.

The reader reports evidence and correction candidates. It does not close tasks,
merge code or fabricate a failure for an unread source. Task closure still
requires the appropriate verified-evidence path.

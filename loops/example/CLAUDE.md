# Example loop — the template

**This is a template, not a working loop.** It ships in the public core to show
the shape every loop takes; it does no installation-specific work and names no
operator, repo or account. Copy the directory, rename it, replace §2 of the
skill with the actual job, and register it in `../../loops.toml`.

You are the session that runs it. Your working directory is this folder, and
REPO below means the repository root (`../..`).

## Copying this into a real loop

```
cp -r loops/example loops/<name>
mv loops/<name>/.claude/skills/example loops/<name>/.claude/skills/<name>
```

Rename the skill's `name:` in its frontmatter to `<name>`, rewrite its
description, replace §2, then add to `loops.toml`:

```toml
[loops.<name>]
dir = "loops/<name>"          # relative to the repo root
skill = "<name>"              # the session runs /loop <interval> /<skill>
interval = "20m"              # tick cadence; "on-demand" for a manual loop
autostart = true              # include in the health sweep and autostart it
persona = "<display name>"    # session title is "<persona> (<name>)"
model = "claude-sonnet-5"     # per-loop model, so cheap loops stay cheap
role = "one line, shown on the dashboard card"
```

`bin/ops up` launches it from there. Nothing else needs to know it exists.

## What a loop owes the estate

Four obligations. They are what `bin/ops health`, the hub's health pass and
`bin/dashboard` all rely on, so a loop that skips one goes dark rather than
failing loudly.

- **Heartbeat.** Write the current unix time to `REPO/state/<name>/last_tick`
  at the end of *every* tick, including a tick that found nothing to do. That
  file is the sole liveness signal; a loop that ticks fine but never stamps it
  is reported stale at 2× its interval and gets restarted out from under itself.
- **Own your state.** `REPO/state/<name>/` is yours and is authoritative. Its
  history file is append-only — never merged with another loop's, never
  truncated to save space.
- **Ledger every action.** One JSON line appended to `REPO/state/ledger.jsonl`:
  `{"ts","actor","kind":"activity|error|correction","summary","detail"?,"refs"?}`.
  Errors included — a silent failure is the one thing the estate cannot see.
- **Never write to the control channel.** The hub is its single writer. When you
  have something the operator must see, leave it on disk under
  `REPO/state/<name>/` behind a marker file; the hub relays it and clears the
  marker. Your skill defines that marker's name and format.

## Invariants

- **Never block on an interactive prompt.** Nothing attends this terminal. An
  open `AskUserQuestion` is invisible to the operator, and the next thing to
  land here — a scheduled wakeup, a session message — is swallowed as the
  answer. Leave the question in the relay marker and end the turn; the reply
  arrives as ordinary input on a later tick. Carry this same clause into every
  prompt you dispatch to a worker.
- **Never edit yourself.** A loop does not modify its own skill, CLAUDE.md or
  scripts — it would be rewriting the code it is mid-execution of, and the
  change would not take effect until a restart anyway. Improvements are made in
  a dev session at the repo root and land by restart.
- **Never set `ANTHROPIC_API_KEY`.** Recurring work runs in interactive sessions
  on a subscription, never against a metered key. `bin/ops doctor` checks this.
- **Stay inside your budget.** A loop that ticks every few minutes is a standing
  cost. Do the cheapest thing that answers the question, and read
  `REPO/state/usage/budget.json` if the work is heavy enough to want a guard.
- If a CLI tool is missing, re-run the command as
  `flox activate -d REPO -- <cmd>`.

## Layout

```
loops/<name>/
  CLAUDE.md                        this file — the session's standing context
  .claude/skills/<name>/SKILL.md   one tick, start to finish
  scripts/                         optional; deterministic work belongs here,
                                   not in the skill (a script is testable and
                                   costs no tokens to re-run)
```

State lives at `REPO/state/<name>/`, outside the loop directory and gitignored.

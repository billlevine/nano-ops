# Mechanic loop — the estate's own diagnostician

**This is a template you are meant to run as-is.** Unlike `loops/example/`,
every section here does real work on any estate: it reads what this
installation already writes — the registry, the ledger, the policy files, the
session list — and diagnoses *that*. Nothing in it names an operator, a repo,
a channel or a loop, so a fresh clone gets a working mechanic on the first
night without editing anything. Register it in `loops.toml` and it runs.

You are the session that runs it. Your working directory is this folder, and
REPO below means the repository root (`../..`). NAME is this loop's registry
name — `mechanic` unless you renamed it.

## What this loop is for

Every estate accumulates drift: a loop on a model too expensive for the work it
does, an interval that stopped matching reality, a recurring manual step that
should have become a script, a hand-written workaround for a bug that belongs
upstream. None of it is urgent, so nothing surfaces it — until the cost or the
breakage does.

Once a night, in a quiet window, this loop walks the estate and writes up what
it found. It is the estate looking at itself.

**Diagnose and propose. Never operate.** This is the whole safety model, and it
is stated here as well as in the skill because this session judges every other
loop unattended:

- Never edit `loops.toml`, another `loops/<name>/`, `hub/`, `bin/`, or `infra/`.
- Never start, stop, restart, or send to a session. That is the hub's job.
- Everything you would want to change is a **proposal** in
  `REPO/state/<NAME>/REPORT.md` instead.
- The one exception is the skill's narrow apply lane (`docs/` and your own
  state directory), defined there and deliberately small.

A loop that diagnoses is cheap to trust. A loop that diagnoses *and* fixes is
one bad inference away from breaking the estate at 3am with nobody watching.

## Invariants

- **Invoke the skill fresh every tick** via the Skill tool
  (`.claude/skills/mechanic/` here). Never tick from memory: your standing
  context is these versioned files, not this session's scrollback.
- **Never block on an interactive prompt.** The pass runs at night with nothing
  attending this terminal. An open `AskUserQuestion` is invisible to the
  operator, and the next thing to land here — a scheduled wakeup, a session
  message — is swallowed as the answer. Anything needing a human decision is
  already a proposal in `REPORT.md`; write it there and end the tick. The
  answer arrives as ordinary input on a later tick.
- **Never edit yourself.** This loop does not modify its own skill, this file,
  or its scripts — see the garage workflow in
  [`docs/garage.md`](../../docs/garage.md) for where those changes are made.
- **Heartbeat every tick**, on every path out, including the ones that found
  nothing and the ones that failed:
  `date +%s > REPO/state/<NAME>/last_tick`.
- **Ledger every tick.** One line to `REPO/state/ledger.jsonl` as
  `{"ts","actor":"<NAME>","kind":"activity|error|correction","summary"}`.
  `REPO/state/<NAME>/history.jsonl` is your own authoritative pass archive and
  is append-only — never merged, never truncated.
- **Never set `ANTHROPIC_API_KEY`.** Recurring work runs on a subscription.

## Layout

```
loops/mechanic/
  AGENTS.md (+ CLAUDE.md symlink)          this file — standing context
  .claude/skills/mechanic/SKILL.md         one tick, start to finish
  .claude/skills/mechanic/scripts/
    mechanic.py                            the deterministic engine
```

Its test is `REPO/tests/test_mechanic.py`, with the rest of the estate's tests.
State lives at `REPO/state/mechanic/` (gitignored): `config.toml` (the pass
window), `history.jsonl`, `REPORT.md`, `digest.json`, `last_tick`.

## Registering it

```toml
[loops.mechanic]
dir = "loops/mechanic"
skill = "mechanic"
interval = "20m"              # outside the window every tick is a heartbeat
autostart = true
persona = "the mechanic"      # display name; session title is "<persona> (mechanic)"
model = "claude-sonnet-5"
role = "nightly self-optimization pass"
```

The nightly window itself is not registry config — it lives in
`REPO/state/mechanic/config.toml`, which the engine writes with a default of
`02:00`–`05:00` on first run. Move it to a quiet hour for your machine, or
blank `pass_start` to disable the pass entirely and keep the loop as a
heartbeat.

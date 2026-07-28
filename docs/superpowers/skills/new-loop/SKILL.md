---
name: new-loop
description: >-
  Add a new loop to this estate — directory shape, loops.toml registration,
  always-on vs on-demand, the hub dispatch route an on-demand loop needs to be
  runnable at all, and the worktree-isolation rule for a loop that touches
  other repos. Use when adding, scaffolding, or registering a new loop, or when
  asked "how do loops work here". Dev-session skill — not a loop skill itself.
---

# Adding a new loop to this estate

`loops/example/` is the copyable template this describes; everything below is
how to turn a copy of it into a real loop. **This is a dev-session job, done
from the repo root.** The hub operates loops and never edits them, and a loop
is never edited from inside itself (root `CLAUDE.md`, *Roles*).

Throughout, the hub is referred to by its configured identity rather than a
name: its session title is `"<persona> (hub)"`, where `persona` comes from
`loops.toml` `[hub]` and defaults to the neutral `"ops"`. Nothing in this
skill — or anywhere else in the core — hardcodes who runs this estate; if you
need to name the hub in prose you are writing for your own install, read it
from there. The same goes for the human operator: the estate has one, this
document does not know their name.

Decide the shape first — it drives everything else:

| | **always-on** | **on-demand** |
|---|---|---|
| `interval` | `"15m"`, `"20m"`, … | `"on-demand"` |
| `autostart` | `true` | `false` |
| Runs by | its own `/loop <interval> /<skill>` | one-shot dispatch from the hub |
| `bin/ops` supervises it | yes | **no** — filtered out by design |
| Needs a hub dispatch route | no | **yes, or it can never run** |
| Right when | work arrives continuously | work is rare and bursty |

An always-on loop with near-all no-op ticks is real token cost for near-zero
signal — the interval-fit anti-pattern. If the trigger is "a human or another
loop noticed something", it is on-demand.

## 1. The directory

```
loops/<name>/
  CLAUDE.md                             the session's standing instructions
  AGENTS.md -> CLAUDE.md                symlink (Codex compat; every CLAUDE.md has one)
  .claude/skills/<name>/SKILL.md        the mechanism, invoked fresh each run
  .claude/skills/<name>/scripts/…       optional: the deterministic engine + its tests
state/<name>/                           gitignored; the loop's own state + last_tick
```

`ln -s CLAUDE.md loops/<name>/AGENTS.md` — a relative symlink, not a copy.

**CLAUDE.md carries intent; SKILL.md carries mechanism.** The split matters
because it is how a loop "knows" things durably: every loop here reloads its
skill fresh each tick and keeps nothing in session memory, so anything the
loop must always be true about itself belongs in versioned files, not in a
long-lived conversation. CLAUDE.md is where the philosophy, the hard rules,
and the non-negotiables go; SKILL.md is the procedure.

Every loop CLAUDE.md carries these — `loops/example/CLAUDE.md` is the
worked version:

- Persona line (`Persona: **the <name>** — <one-line character>`), then "use
  this name in reports; technical name stays `<name>`".
- How it runs: `/loop <interval> /<skill>` for always-on; "run `/<skill>` when
  the hub dispatches — no standing /loop" for on-demand.
- "Always invoke the `<name>` skill fresh via the Skill tool — never work from
  remembered content."
- Where its state lives, and the heartbeat:
  `date +%s > ../../state/<name>/last_tick` at the end of every tick.
- **NEVER BLOCK ON AN INTERACTIVE PROMPT**, with the *why* (the operator
  watches the control channel, not the agent-deck TUI; the next thing landing
  in the terminal gets swallowed as the "answer"). Say where a question goes
  instead.
- Ledger line: append `{"ts","actor":"<name>","kind","summary"}` to
  `../../state/ledger.jsonl`.
- Any provider-credential rule the estate holds itself to — e.g. an
  interactive-subscription estate keeps API keys unset so no loop can
  silently run up per-token charges.
- Its **authority boundary** — what it may change on its own. Be explicit and
  narrow. A diagnostic loop that proposes but never operates, and a loop that
  stages a cross-repo change and stops for approval, are the two models.

SKILL.md needs YAML frontmatter with `name` and a `description` written for
*discovery* — the trigger phrases someone would actually say ("run the
nightly sweep", "/sweep 4", "check the queue"), not a summary.

**If the loop has recurring deterministic work, script it.** The convention is
engine-does-the-state, model-does-the-judgment: a `<name>.py` next to the
SKILL.md owns bookkeeping and never analyzes; the SKILL.md prose owns analysis
and calls the script for I/O. Put its tests next to it and run them.

## 2. Register it in `loops.toml`

`loops.toml` is gitignored — it is the per-installation registry, and it is
the only place a loop is declared. `loops.example.toml` is the committed
template documenting its shape; copy it once (`cp loops.example.toml
loops.toml`) and add a block per loop. No real loop ships in this repo: a loop
is where installation policy concentrates, and the allowlist rule keeps policy
out of the core.

```toml
[loops.<name>]
dir = "loops/<name>"          # repo-relative
skill = "<name>"              # the /<skill> the session invokes
interval = "20m"              # or "on-demand"
autostart = true              # false for on-demand
persona = "the <name>"        # display name; also the agent-deck session
                              # title, as "<persona> (<name>)"
model = "claude-sonnet-5"     # right-size it, and say why in a comment
```

Every field is load-bearing:

- `dir` — where `agent-deck launch` puts the session's cwd.
- `skill` — what the `/loop <interval> /<skill>` kick invokes.
- `interval` — also the health threshold: `bin/ops health` calls a heartbeat
  stale past `2 × interval + 120s`. `"on-demand"` parses to no cadence, which
  is what keeps it out of the health sweep.
- `autostart` — `bin/ops` starts and supervises exactly the `true` ones.
- `persona` — the agent-deck title is `"<persona> (<name>)"`, and everything
  (the hub's health pass, any drift check) keys on that exact string. Choose
  it once.
- `model` — cheap engine-narrates ticks belong on a small model; judgment-heavy
  work on a larger one. A session's model only changes on process restart.
  Leave a comment saying which case this is.

**Model right-sizing is a real decision, not a default.** Write the reason
into the comment, or the next review pass over the registry will propose
changing it.

## 3. On-demand only: add the hub dispatch route

**Skipping this leaves a loop that cannot be run.** `bin/ops` filters
non-autostart entries out, so nothing starts it; there is no generic "run this
on-demand loop now" verb; and `/loop on-demand /<skill>` is not valid. This is
the single most common way a new on-demand loop ships dead — it looks complete,
and it is simply unreachable.

Add a row to `hub/.claude/skills/hub/SKILL.md`'s classification table naming
the trigger phrases the operator would actually use, then follow the skill's
**On-demand loop dispatch** recipe: launch a one-shot `ephemeral - <slug>`
session in the loop's directory, send the invocation in `-m`, collect the
report on a later tick, then ephemeral hygiene.

Every dispatch rule in that skill binds a loop dispatch too:

- **The unattended clause**, verbatim, in the `-m` text.
- **Worktree isolation** — see below.
- **Ephemeral session hygiene** on completion (`session stop` + `session
  remove` + `worktree remove`, as one step) — *except* when the worktree's
  detached HEAD is the only ref holding an unpushed commit. Put it on a real
  branch first.

## 4. Any loop that touches another repo: its own worktree

Standing order, unconditional — not "when something else is running". A
checkout's current branch and working tree are shared mutable state; a second
agent in the same directory silently rewrites the ground under a running
sibling, which then keeps going against the wrong tree and reports success.
The failure is quiet in both directions: work lands on the wrong branch, and
the worker still says "done".

```bash
WT="$REPO/state/hub/worktrees/<slug>"
git -C <the shared clone> fetch origin
mkdir -p "$REPO/state/hub/worktrees"
git -C <the shared clone> worktree add --detach "$WT" <base-ref>
```

`--detach` because git refuses to check out a branch already checked out
elsewhere. Worktrees live under `state/hub/worktrees/` (gitignored, and unlike
`/tmp` not reaped from under a long run). Tell the worker its box in the
prompt: *"You are in a dedicated worktree at `<path>`. Work only here — do NOT
cd into the shared clone at `<path>`, another agent may be using it."*

A loop that **writes to another repo** also needs an explicit approval gate:
stage the work in its worktree, commit there, stop, and report. Landing it is
the operator's call, per unit of work. Write that into the loop's CLAUDE.md as
a hard rule so it is not re-decided each run.

## 5. Durable state, if the loop has any

Two shapes already exist — reuse one instead of inventing a third:

- **A pass/tick archive** — `state/<name>/history.jsonl`, append-only,
  authoritative, never merged or truncated.
- **A standing-item store** — `{items.json, ledger.jsonl}`: a JSON source of
  truth plus an append-only audit trail, with a `$<NAME>_STATE_DIR` env
  override so tests run against a tempdir. `bin/followups` is the reference
  implementation; add a status machine on top of it if the items have a
  lifecycle rather than just open/resolved.

Anything a human must eventually resolve goes in a **store**, not in a report
— a report is overwritten next run, and a thing that only ever lived in one
gets silently forgotten. That is the whole reason the store shape exists.

## 6. Land it

1. Run every test suite you touched — `python3 tests/test_*.py` and any
   `scripts/test_*.py` — and read the output; do not assert success.
2. Check `loops.toml` parses and lists what you expect:
   `python3 -c "import tomllib; print(tomllib.load(open('loops.toml','rb'))['loops'])"`.
3. Confirm `bin/ops health` still classifies the estate correctly — an
   on-demand loop must NOT appear (absent is its healthy state).
4. Commit. **Skill and CLAUDE.md edits don't reach running sessions**
   reliably — live reload has watcher lag. A new loop is fine (nothing is
   running yet), but if you changed the hub skill, the hub session needs a
   restart with its resume pointer cleared to pick the new route up
   immediately.
5. Tell the hub the loop exists and how to dispatch it, via `agent-deck
   session send` — never through the control channel, which would look like a
   message from the operator.

## Checklist

- [ ] `loops/<name>/CLAUDE.md` + `AGENTS.md` symlink
- [ ] `.claude/skills/<name>/SKILL.md` with discovery-shaped frontmatter
- [ ] engine script + tests, if there is recurring deterministic work
- [ ] `[loops.<name>]` in `loops.toml`, every field, model reason commented
- [ ] hub classification row + dispatch recipe — **on-demand loops only**
- [ ] worktree isolation + approval gate — if it touches another repo
- [ ] durable state follows `history.jsonl` or the `{store, ledger}` shape
- [ ] tests run and read; `loops.toml` parses; `bin/ops health` still clean

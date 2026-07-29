# The garage — where the estate gets worked on

The estate has three roles, and they are deliberately not the same session.

| Role | Who | What it may do |
|---|---|---|
| **Operator** | the hub session (`hub/`) | Runs, kicks, restarts and reloads loops. Relays. Never edits a loop's files. |
| **Diagnostician** | the mechanic loop, if you run one (`loops/mechanic/`) | Reads the whole estate and writes proposals. Never operates, never edits anything outside `docs/` and its own state. |
| **Mechanic's bench** | **the garage** — an ordinary dev session started at the repo root | Edits everything. Owns every change to `loops.toml`, loop skills, `hub/`, `bin/` and `infra/`. |

The garage is the only one of the three that writes code, and it is the least
formal: it is just an interactive session you start yourself, in the repository
root, with no `/loop` and no schedule. Everything the other two roles produce —
a proposal, a bug the hub hit, an idea in `docs/ideas.md` — lands *here* to be
built.

## Why it is separate

Three reasons, and each of them is a failure that has to be designed out rather
than remembered:

- **A loop cannot safely edit itself.** It would be rewriting the code it is
  mid-execution of, and the change would not take effect until a restart
  anyway. So a loop's improvements have to be made from outside it.
- **Editing and operating want opposite reflexes.** The hub's job is to keep
  things running with minimal disturbance; the garage's job is to change them.
  A session that does both will restart a loop to test an edit at exactly the
  moment the loop was doing something that mattered.
- **Diagnosis is worth more when it cannot act.** A mechanic that only proposes
  can be pointed at everything, unattended, cheaply. The moment it can also
  apply, every one of its inferences becomes a risk, and it has to be given a
  much narrower field of view to stay safe.

## Starting one

```bash
cd <repo root>
flox activate          # the toolchain; loops.toml is seeded on first activate
claude                 # or your agent CLI of choice — an ordinary session
```

Started at the repo root, the session picks up the root `CLAUDE.md` as its
standing context and any skills symlinked into `.claude/skills/` (for example
`new-loop`, for adding a loop to the estate).

Nothing marks a session as "the garage" — the role is defined by where it is
started and what it is for. Do not start one inside `loops/<name>/` and then
edit that loop; you would be in the loop's own context, which is written to
instruct the loop, not to instruct you.

## The working loop

1. **Something surfaces.** The mechanic writes `REPORT.md` and the hub relays
   its Proposal handoff; or the hub hits an error and ledgers it; or the
   operator has an idea and it goes into `docs/ideas.md`.
2. **The operator decides.** A proposal is a request, not a plan already
   approved. Relaying it is not approving it.
3. **The garage builds it.** Read the evidence the proposal points at — not
   just the handoff — then make the change, with its test, in one commit.
4. **The hub reloads it.** Skill and `CLAUDE.md` edits do not reach a running
   session; the session has to be restarted before the change is live. That
   restart is the hub's move (`bin/ops up`, or a targeted restart), not the
   garage's. A change that is committed but not reloaded is the most confusing
   state the estate has: the file says one thing and the running loop does
   another.
5. **Lessons go back into the files.** A correction that only exists in a
   session's scrollback will be re-made. Distil it into the skill or the
   `CLAUDE.md` it belongs to, so the next tick already knows.

## House rules

- **One change, one commit, with its test.** The estate's scripts all have
  tests in `tests/` that run against tempdirs; a script change without one is
  incomplete.
- **Do not edit a loop from inside itself**, and do not edit a loop's files
  from the hub session either.
- **Restart after editing a skill.** Nothing warns you if you forget.
- **`loops.toml` and `state/` are gitignored** and are this installation's own.
  Anything committed here must work for a stranger's estate too: identity is
  configuration, and a patch that bakes in a persona, a channel id, a hostname
  or an absolute path is a bug whatever else it does.
- **Never set `ANTHROPIC_API_KEY`** in a session that will run estate work.

## A note on forks

If this repository is the public core and you also run a private fork with your
real loops and registry in it, the same split applies one level up: mechanism is
improved *here* and flows down to the fork by rebase, while installation policy
stays in the fork's own commits. A mechanism fix made only in the fork becomes a
permanent rebase conflict.

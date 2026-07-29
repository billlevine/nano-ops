# Keeping your estate up to date with the core

You cloned nano-ops, filled in your own `loops.toml`, and built your own loops
on top. Now the core has moved and you want the improvements.

This is a runbook for that, and a design rule that makes it boring. The design
rule is the important half.

## The rule: add files, don't edit them

**Every core file your fork edits is a conflict on some future update.** Files
you only *add* never conflict with anything, no matter how far the core moves.

That is the whole trick. An estate that only adds files can take an upstream
update in one command, forever. An estate that edits core files pays a little
more on every update, and the cost compounds — because the conflicts land in
exactly the files you edited longest ago and remember least.

So the target state is:

| You want to | Do it by | Not by |
|---|---|---|
| add a loop | creating `loops/<name>/` | (nothing else — `.gitignore` already covers it) |
| register loops, name your hub | editing `loops.toml` | editing `loops.example.toml` |
| keep a backlog | `docs/ideas.md` | (already untracked here) |
| write your own docs | adding `docs/<your-topic>.md` | appending to a core doc |
| change how a core script behaves | a setting in `loops.toml` | editing the script |
| add operator tooling | adding `bin/<your-tool>` | extending a core `bin/` script |

`loops.toml`, `state/`, `docs/ideas.md` and every non-core `loops/<name>/` are
already untracked here, precisely so your versions and the core's can never
collide. If you find yourself needing to edit a core file to do something
ordinary, that is a gap in the core — open an issue rather than carrying a
local edit indefinitely.

### When you must edit a core file

Sometimes there is no way around it. Keep the edit as small and as isolated as
possible, and write down why:

- Put it in **its own commit**, touching nothing else, with a message saying
  what upstream would have to do for the edit to become unnecessary.
- Prefer **adding a line** over rewriting a block. A one-line addition
  usually merges clean; a reflowed paragraph never does.
- Revisit it on each update. If upstream has since made it configurable, drop
  your edit and take theirs.

## The update

Set the remote once:

    git remote add upstream https://github.com/billlevine/nano-ops.git

Then, per update:

    git fetch upstream
    git log --oneline HEAD..upstream/main        # what you're about to take
    git diff --stat HEAD..upstream/main

Check what it touches against your own commits — this is the five seconds that
tells you whether to expect conflicts:

    git diff --name-only HEAD..upstream/main > /tmp/theirs
    git diff --name-only upstream/main...HEAD > /tmp/mine
    comm -12 <(sort /tmp/theirs) <(sort /tmp/mine)   # files you BOTH changed

An empty result means the update is mechanical. Anything listed is where you
will do work.

Then take it:

    git merge upstream/main

Run the tests, and run your own estate's health check:

    for t in tests/test_*.py; do python3 "$t" >/dev/null || echo "FAIL $t"; done
    bin/ops doctor

Restart any session whose `CLAUDE.md` or skill the update changed — a running
session does not pick up file edits.

## Merge, not rebase

**Use `git merge upstream/main`.** Not `git rebase upstream/main`.

Rebase replays your commits on top of the core's, which gives every one of them
a new hash. That means:

- **You must force-push to your own remote after every update.** If anyone else
  has cloned your estate — or if you run it on a second machine — you have
  rewritten history out from under them.
- **You re-resolve the same conflicts repeatedly.** Rebase replays commit by
  commit, so a core file you edited early gets re-conflicted on every future
  update. Merge resolves it once. (`git rerere` reduces this; it does not
  remove it.)
- **Your history stops being a record of what you did**, which matters when you
  are trying to work out why your estate diverged from the core.

Rebase is the right verb in one direction only: a *private fork that publishes
into the core* rebases onto it, because there its own commits are the thing
being replayed upstream and it has no other consumers. Your estate is the
consumer end. Merge.

## When a conflict does happen

    git merge upstream/main
    git status                       # "both modified" is your work list

For each file, the question is always the same: **is my edit still necessary?**
Often the core has changed in a way that makes it obsolete — take theirs.

    git checkout --theirs <file>     # take the core's version wholesale
    git checkout --ours   <file>     # keep yours wholesale
    # or edit by hand, then:
    git add <file>

    git merge --continue

If it goes badly, `git merge --abort` puts you back exactly where you were.
Nothing about an update is irreversible until you have pushed.

After resolving, do the thing that stops it recurring: ask whether the edit
could live in `loops.toml`, in a file of your own, or upstream. Then make that
change, so the next update is boring again.

## Checking your exposure

At any time, this tells you how much conflict surface you have:

    for f in $(git diff --name-only upstream/main...HEAD); do
      git cat-file -e upstream/main:"$f" 2>/dev/null \
        && echo "EDITS CORE : $f" \
        || echo "yours only : $f"
    done

Every `EDITS CORE` line is a future conflict. Every `yours only` line is free.
A healthy estate is almost entirely the second kind.

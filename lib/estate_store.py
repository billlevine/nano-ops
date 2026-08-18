"""Where the shared estate store IS, answered in one place (t-393, gap audit Q-10).

The store's location used to be a side effect of the invocation path. `bin/estate`
derived `STATE_DIR` from its own file location and `connect()` opened SQLite with
the default `create-if-absent`, so a run from a git worktree of this repo — every
dispatched worker, every garage branch — resolved `<worktree>/state/estate.db`,
found nothing there, and MINTED a second store. Nothing failed. Nothing warned.
The worker's `task claim` succeeded against a database that held none of the
estate's history, and both stores number their rows from `t-1`, so the shadow
looked exactly as healthy as the real one. Three of them existed under
state/hub/worktrees/* when this was written, and more under .claude/worktrees/*.

The fix has two halves, and both are needed.

RESOLVE THE CANONICAL STORE. A linked worktree is not a separate estate; it is a
second checkout of the same one. So the derived answer follows git's own notion
of identity: a linked worktree resolves to the MAIN worktree's `state/`, which is
where the store the estate actually uses lives. That is a real behavior change —
a run inside a worktree now reads and writes the live store instead of a private
one — and it is the behavior every caller already believed it had.

REFUSE TO MINT A NEW ONE. When the derived path holds no database, that is not a
new estate; it is a resolution that went somewhere unexpected, or a machine where
the store was never restored. Creating an empty store there is the one outcome
that hides the problem, so a DERIVED path never creates. The run stops and names
the file it expected.

THE OVERRIDE IS THE ESCAPE HATCH, AND IT STAYS. `$ESTATE_STATE_DIR` means a human
(or a test) named the store on purpose, and naming a path that does not exist yet
is how every test in this repo gets a throwaway store. An explicitly named store
may still be created on first use. The rule is not "never create a database"; it
is "never create one nobody asked for".

WHY NOT SHELL OUT TO `git rev-parse --git-common-dir`. Every subcommand of every
tool here resolves the store at import time, and the loops call `bin/estate` in
tight sequences. A subprocess per invocation is a real cost for a fact that is
sitting in two small files on disk. Git's on-disk contract for a linked worktree
is stable and narrow: `.git` is a FILE holding `gitdir: <path>`, and that
directory holds a `commondir` pointing at the main `.git`. This reads exactly
those two, and where it cannot understand what it finds it falls back to the
checkout it is in — which is the safe direction, because a derived path that
turns out to be wrong refuses rather than creating anything.
"""
from __future__ import annotations

import os

STORE_FILENAME = "estate.db"
OVERRIDE_ENV = "ESTATE_STATE_DIR"

# How the state directory was arrived at. The distinction that carries weight is
# whether somebody NAMED it: a named store may be created on first use, a derived
# one may not.
OVERRIDE = "override"
MAIN_WORKTREE = "main-worktree"
CHECKOUT = "checkout"


def resolve(script_path: str, env=None) -> "Resolution":
    """The one answer to "which estate store does this run use".

    `script_path` is the calling script's own `__file__`; the checkout is its
    parent's parent, because every tool here lives in `<root>/bin/`.
    """
    env = os.environ if env is None else env
    script_root = os.path.dirname(os.path.dirname(os.path.abspath(script_path)))
    override = (env.get(OVERRIDE_ENV) or "").strip()
    if override:
        return Resolution(os.path.abspath(override), OVERRIDE, script_root)
    main = main_worktree_root(script_root)
    if main is None or os.path.normpath(main) == os.path.normpath(script_root):
        return Resolution(os.path.join(script_root, "state"), CHECKOUT,
                          script_root, repo_root=script_root)
    return Resolution(os.path.join(main, "state"), MAIN_WORKTREE,
                      script_root, repo_root=main)


def main_worktree_root(root: str):
    """The main worktree of the repository `root` belongs to, or None.

    Returns `root` itself for an ordinary checkout. Returns None when there is no
    repository, when the `.git` file says something this cannot parse, or when the
    repository's main copy is BARE — a bare repo has no working tree, so there is
    no `state/` beside it to resolve to and the caller keeps its own checkout.
    """
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        return root
    if not os.path.isfile(dot_git):
        return None
    try:
        with open(dot_git, encoding="utf-8") as handle:
            first = handle.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gitdir = first[len("gitdir:"):].strip()
    if not gitdir:
        return None
    if not os.path.isabs(gitdir):
        # `git worktree add --relative-paths` writes the link relative to the
        # worktree, so it stays valid when the pair is moved together.
        gitdir = os.path.join(root, gitdir)
    common = _common_dir(os.path.normpath(gitdir))
    if common is None or os.path.basename(common) != ".git":
        return None
    return os.path.dirname(common)


def _common_dir(gitdir: str):
    """The shared `.git` directory behind a linked worktree's private gitdir."""
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        # No commondir file. This is a submodule (`.git/modules/<name>`), a
        # pruned worktree, or something else entirely — accept only the layout
        # git has always written for a live worktree, and decline the rest.
        parent = os.path.dirname(gitdir)
        if os.path.basename(parent) == "worktrees":
            return os.path.dirname(parent)
        return None
    if not value:
        return None
    if not os.path.isabs(value):
        value = os.path.join(gitdir, value)
    return os.path.normpath(value)


class Resolution:
    """A resolved store location, and the reasoning that produced it."""

    __slots__ = ("state_dir", "source", "script_root", "repo_root")

    def __init__(self, state_dir: str, source: str, script_root: str,
                 repo_root: str | None = None):
        self.state_dir = os.path.normpath(state_dir)
        self.source = source
        self.script_root = os.path.normpath(script_root)
        self.repo_root = os.path.normpath(repo_root) if repo_root else None

    @property
    def db_path(self) -> str:
        return os.path.join(self.state_dir, STORE_FILENAME)

    def exists(self) -> bool:
        return os.path.exists(self.db_path)

    def may_create(self) -> bool:
        """Whether this run is allowed to bring a store into being.

        Only a store somebody named. A derived path that holds no database is a
        question, not a new estate.
        """
        return self.source == OVERRIDE

    def origin(self) -> str:
        """One phrase for how the location was chosen."""
        if self.source == OVERRIDE:
            return f"${OVERRIDE_ENV}"
        if self.source == MAIN_WORKTREE:
            return f"the main checkout of this worktree ({self.repo_root})"
        return f"this checkout ({self.repo_root})"

    def describe(self) -> str:
        return f"{self.db_path} (from {self.origin()})"

    def missing_message(self) -> str:
        lines = [
            f"[error] no estate store at {self.db_path}",
            f"        resolved from: {self.origin()}",
            f"        this run is in: {self.script_root}",
            "        Refusing to create a second, empty store. Every store numbers",
            "        its own rows from t-1, so a shadow one looks healthy and holds",
            "        none of the estate's history.",
            "        Create the canonical store deliberately:  bin/estate init",
            f"        Or name a throwaway one:  {OVERRIDE_ENV}=/tmp/somewhere bin/estate ...",
        ]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"db_path": self.db_path, "state_dir": self.state_dir,
                "source": self.source, "origin": self.origin(),
                "script_root": self.script_root, "repo_root": self.repo_root,
                "exists": self.exists(), "may_create": self.may_create()}


class StoreMissing(RuntimeError):
    """A derived store path holds no database, and this run will not make one."""

    def __init__(self, resolution: Resolution):
        super().__init__(resolution.missing_message())
        self.resolution = resolution


def require(resolution: Resolution) -> None:
    """Raise unless this run may legitimately open the store it resolved."""
    if resolution.exists() or resolution.may_create():
        return
    raise StoreMissing(resolution)

"""Read-only health observations for registered long-lived project sessions.

Checks session liveness, branch activity, state freshness/size, context,
workers and unattended hooks. Unknown readings never count as healthy.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

LIB = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LIB)
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import estate_observation as obs  # noqa: E402
import estate_store  # noqa: E402

VOCAB = "steward-health"

# ── the three verdicts, in print order: the actionable one first ─────────────
FINDING = "finding"
UNKNOWN = "unknown"
OK = "ok"
VERDICTS = (FINDING, UNKNOWN, OK)

# A fourth state a single CHECK can be in, and deliberately not a fourth
# verdict. `not_due` is a boundary the operator drew — the one case today is a
# paused steward, which section 3.4 exempts from `idle` — and it is neither a
# pass nor a hole: no look was owed, so nothing was missed and nothing was
# cleared. It contributes to no verdict and to no exit status, and it is
# printed in the steward's own row so that "this question was not asked" is
# visible rather than inferred from a check that quietly vanished.
NOT_DUE = "not_due"

# ── the seven findings, exactly as section 3.7 names them ────────────────────
DEAD = "dead"
IDLE = "idle"
DRIFTING_STATE = "drifting:state"
DRIFTING_SIZE = "drifting:size"
CONTEXT = "context"
UNATTENDED_WORKER = "unattended-worker"
UNGATED = "ungated"

# check name → (finding it can raise, what the check asks)
CHECKS = (
    ("alive", DEAD, "agent-deck still has a running session for this row"),
    ("commit-age", IDLE, "the branch has moved inside the idle window"),
    ("state-current", DRIFTING_STATE, "STATE.md moved when NOTES.md did"),
    ("state-size", DRIFTING_SIZE, "STATE.md is small enough to reload"),
    ("context", CONTEXT, "the session is under its context threshold"),
    ("workers", UNATTENDED_WORKER, "no dispatch of its own is overdue"),
    ("gate", UNGATED, "the unattended gate is wired in its worktree"),
)
CHECK_NAMES = tuple(name for name, _, _ in CHECKS)

# ── the thresholds, and where each number comes from ─────────────────────────
# 48 h with an open ticket: section 3.7. A steward works in rounds and every
# round ends in a `notes:` commit (section 3.3), so a branch that has not moved
# in two days has either finished or stopped, and only one of those retires.
IDLE_HOURS = 48
# 8,000 bytes: section 3.2's own cap on STATE.md. Above it the file stops being
# the cheap reload the SessionStart hook exists to provide — a project log
# can grow beyond a cheap reload without a bound.
STATE_MAX_BYTES = 8000
# The registry schema's own default (section 3.2). A row may name its own.
DEFAULT_CONTEXT_THRESHOLD = 300000
# agent-deck statuses that mean a session is alive — the same test `bin/ops
# health` and `bin/skill-drift` make.
ALIVE = ("running", "waiting")

# Registry row statuses (section 3.2). `retired` rows are the record of a
# finished epic and are not checked; `paused` rows are checked for everything
# except idleness (section 3.4: "health stops calling it idle").
ACTIVE = "active"
PAUSED = "paused"
RETIRED = "retired"

# The project files a steward keeps its state in, relative to its worktree.
STATE_FILE = os.path.join("project", "STATE.md")
NOTES_FILE = os.path.join("project", "NOTES.md")
CHARTER_FILE = os.path.join("project", "CHARTER.md")
# A round with nothing new still restamps `Last round:`, or its note alone
# reads as drift (check_state_current).
REMEDY_STATE = ('send it "checkpoint" — rewrite STATE.md (nothing new still '
                'restamps its `Last round:` line) and commit it with the '
                "note, then it is safe to compact")
# Where the role's gate is wired. Section 3.1: the session's cwd is
# `<worktree>/stewards/`, so this is the settings file it binds at start.
GATE_FILE = os.path.join("stewards", ".claude", "settings.json")

# Claude Code's transcript root. `$STEWARD_TRANSCRIPT_DIR` names another one —
# the directory that HOLDS the per-project directories, so a test can point the
# whole check at a tempdir rather than at one steward's.
TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")

# How much of a transcript's tail is read looking for the last usage record,
# in growing windows. Transcripts may be large; the last record is
# in the last few kilobytes of a healthy one, and the escalation is here for a
# file whose tail is one enormous tool result.
TAIL_WINDOWS = (1 << 20, 1 << 23, 1 << 26)

# Printed under every run and under --list, so "it is not in the output" can
# never be read as "it is handled".
NOT_CHECKED = [
    ("whether the steward is doing the RIGHT thing",
     "every reading here is a file time, a process status or a token count. "
     "Judging a steward's work is the hub's job and costs tokens; this costs "
     "none and runs on a timer"),
    ("a dispatch this estate never registered",
     "`bin/dispatches` is the only record that says work is still out, and a "
     "worker launched without an entry is invisible to every reader of that "
     "store, this one included"),
    ("whether the tickets themselves are progressing",
     "the ticket tracker is live state behind a network call. `idle` reads the "
     "branch instead, which is the thing a round is required to move"),
    ("retired stewards",
     "a retired row is the record of a finished epic. Its worktree and branch "
     "stay on purpose (section 3.4) and nothing about them is owed a look"),
    ("other session roles",
     "they have their own checks — bin/ops health, bin/skill-drift. A steward "
     "is the shape none of those can see, which is why this exists"),
]


# ── time ─────────────────────────────────────────────────────────────────────
def now_utc() -> dt.datetime:
    """Now, or the instant `$STEWARD_HEALTH_NOW` pins for a test."""
    pinned = os.environ.get("STEWARD_HEALTH_NOW")
    if pinned:
        try:
            return dt.datetime.fromisoformat(
                pinned.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except ValueError:
            pass
    return dt.datetime.now(dt.timezone.utc)


def from_epoch(seconds):
    try:
        return dt.datetime.fromtimestamp(float(seconds), dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def local(stamp) -> str:
    return "—" if stamp is None else stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def humanise(seconds: float) -> str:
    """A gap a person reads at a glance. Signless — callers say direction."""
    seconds = abs(float(seconds))
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def thousands(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


def rel(path: str) -> str:
    try:
        out = os.path.relpath(path, REPO_ROOT)
    except ValueError:
        return path
    return path if out.startswith("..") else (out or ".")


# ── where the registry lives ─────────────────────────────────────────────────
def state_dir() -> str:
    """The estate's state directory — the MAIN checkout's, from a worktree.

    A linked worktree's own `state/` is empty, and a check that read it would
    report an estate with no stewards in it (docs/lessons.md,
    `worktree-empty-state-store`). `lib/estate_store.resolve` is this repo's
    one answer to that question and is reused rather than re-derived.
    """
    return estate_store.resolve(os.path.join(REPO_ROOT, "bin", "x")).state_dir


def registry_dir(override: str | None = None) -> str:
    """The directory holding `<slug>.json`, and the two shapes of the override.

    `$STEWARD_STATE_DIR` is `bin/steward`'s own escape hatch and section 3.2
    does not say which of the two directories it names. Both are accepted, in
    the only order that cannot be ambiguous: a `stewards/` subdirectory inside
    it wins, and the directory itself is used otherwise. So a test that points
    the variable at a throwaway `state/` and one that points it straight at the
    rows both read the same rows, and B2 and B3 cannot disagree about it.
    """
    named = override if override is not None else os.environ.get(
        "STEWARD_STATE_DIR")
    named = (named or "").strip()
    if not named:
        return os.path.join(state_dir(), "stewards")
    base = os.path.abspath(os.path.expanduser(named))
    nested = os.path.join(base, "stewards")
    if os.path.isdir(nested):
        return nested
    return base


def load_registry(directory: str) -> tuple:
    """Every registry row, plus the reading that produced the list.

    A directory that is not there is `no_registry` and NOT an empty estate: a
    registry nobody has written and an estate with no stewards in it produce
    the same empty list, and only the second is a fact. One that is there and
    would not be listed is `unreadable`, which is weather rather than setup.
    """
    source = rel(directory)
    if not os.path.isdir(directory):
        return [], obs.reading(
            VOCAB, source, "no_registry",
            why=f"{source} does not exist — `bin/steward new` has never "
                "written a row here. This is not a statement that every "
                "steward is healthy")
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError as exc:
        return [], obs.reading(VOCAB, source, "unreadable",
                               why=f"{source} could not be listed "
                                   f"({exc.strerror})")
    rows = []
    for name in names:
        path = os.path.join(directory, name)
        slug = name[:-len(".json")]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:
            rows.append({"slug": slug, "path": path, "row": None,
                         "error": f"could not be read ({exc.strerror})"})
            continue
        except ValueError as exc:
            rows.append({"slug": slug, "path": path, "row": None,
                         "error": f"is not JSON ({exc})"})
            continue
        if not isinstance(data, dict):
            rows.append({"slug": slug, "path": path, "row": None,
                         "error": "is JSON but not a registry row object"})
            continue
        rows.append({"slug": str(data.get("slug") or slug), "path": path,
                     "row": data, "error": None})
    return rows, obs.reading(VOCAB, source, "ok",
                             detail={"rows": len(rows)})


# ── the two subprocess answers ───────────────────────────────────────────────
def deck_profile() -> str | None:
    """Read this installation's explicitly declared profile; never ambient state."""
    import tomllib
    try:
        with open(os.path.join(REPO_ROOT, "loops.toml"), "rb") as fh:
            value = tomllib.load(fh).get("hub", {}).get("deck_profile")
        return str(value).strip() or None if value is not None else None
    except (OSError, ValueError):
        return None


def deck_command() -> list:
    """How agent-deck is invoked. `$STEWARD_DECK_CMD` replaces it for a test."""
    named = (os.environ.get("STEWARD_DECK_CMD") or "").strip()
    return shlex.split(named) if named else ["agent-deck"]


def dispatches_command() -> list:
    """How `bin/dispatches` is invoked. `$STEWARD_DISPATCHES_CMD` replaces it."""
    named = (os.environ.get("STEWARD_DISPATCHES_CMD") or "").strip()
    if named:
        return shlex.split(named)
    return [sys.executable, os.path.join(REPO_ROOT, "bin", "dispatches")]


def read_deck(profile: str | None, fixture: str | None = None) -> tuple:
    """Every session agent-deck knows, indexed by id AND title, plus a reading.

    Section 3.7 names `session show`. One `ls --json` answers the same question
    for every row in one call, and gives the run ONE warrant for the read
    rather than one per steward — which is what `bin/skill-drift` does and for
    the same reason. Rows are indexed by both keys because a registry row's
    `deck_session_id` may be null and its title is then the only handle
    (a title is ambiguous by construction, so the id is preferred).
    """
    source = "agent-deck"

    def failed(outcome, why):
        return {}, {}, obs.reading(VOCAB, source, outcome, why=why)

    if fixture is not None:
        try:
            with open(fixture, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            return failed("deck_error", f"--deck-json {fixture} could not be "
                                        f"read ({exc.strerror})")
    else:
        argv = deck_command()
        if shutil.which(argv[0]) is None and not os.path.exists(argv[0]):
            return failed("no_tool", f"{argv[0]} is not on PATH, so no "
                                     "steward's session can be resolved")
        if profile is None:
            return failed("no_tool",
                          "the deck profile could not be resolved from "
                          "loops.toml, and agent-deck's ambient default is "
                          "another estate's session store (gap audit P-25)")
        try:
            out = subprocess.run([*argv, "-p", profile, "ls", "--json"],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return failed("deck_error", f"agent-deck ls --json did not return "
                                        f"({exc.__class__.__name__})")
        if out.returncode != 0:
            return failed("deck_error",
                          f"agent-deck ls --json exited {out.returncode}: "
                          f"{(out.stderr or '').strip()[:160]}")
        raw = out.stdout

    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return failed("unreadable", "agent-deck's answer was not JSON")
    if not isinstance(data, list):
        return failed("unreadable",
                      "agent-deck's answer was not a list of sessions")

    by_id, by_title = {}, {}
    for item in data:
        if not isinstance(item, dict):
            continue
        record = {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "status": str(item.get("status") or ""),
            "path": str(item.get("path") or ""),
            "archived": bool(item.get("archived")),
        }
        if record["id"]:
            by_id.setdefault(record["id"], record)
        if record["title"]:
            by_title.setdefault(record["title"], record)  # first wins
    return by_id, by_title, obs.reading(
        VOCAB, source, "ok",
        detail={"profile": profile, "sessions": len(data)})


def read_dispatches(fixture: str | None = None) -> tuple:
    """Every dispatch entry past its `check_after`, plus the reading.

    `bin/dispatches due --json` without `--children` is the pure read it has
    always been and takes no lock. Its store is pinned to the MAIN checkout's
    `state/hub`: a worktree's own `state/` is empty, and a sweep that read one
    would report every steward's workers collected (docs/lessons.md,
    `worktree-empty-state-store`).
    """
    source = "dispatches"

    def failed(outcome, why):
        return {}, obs.reading(VOCAB, source, outcome, why=why)

    if fixture is not None:
        try:
            with open(fixture, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            return failed("store_error", f"--dispatches-json {fixture} could "
                                         f"not be read ({exc.strerror})")
    else:
        argv = dispatches_command()
        target = argv[-1] if argv[0] == sys.executable else argv[0]
        if not os.path.exists(target) and shutil.which(target) is None:
            return failed("no_tool", f"{rel(target)} is not there, so no "
                                     "steward's workers can be read")
        env = dict(os.environ)
        env.setdefault("DISPATCHES_STATE_DIR",
                       os.path.join(state_dir(), "hub"))
        try:
            out = subprocess.run([*argv, "due", "--json"], env=env,
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return failed("store_error", f"dispatches due --json did not "
                                         f"return ({exc.__class__.__name__})")
        if out.returncode != 0:
            return failed("store_error",
                          f"dispatches due --json exited {out.returncode}: "
                          f"{(out.stderr or '').strip()[:160]}")
        raw = out.stdout

    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return failed("unreadable", "dispatches' answer was not JSON")
    if not isinstance(data, dict):
        return failed("unreadable",
                      "dispatches' answer was not an object of entries")
    entries = {str(k): v for k, v in data.items() if isinstance(v, dict)}
    return entries, obs.reading(VOCAB, source, "ok",
                                detail={"due": len(entries)})


# ── one steward's worktree ───────────────────────────────────────────────────
class Worktree:
    """One steward's checkout, opened once and asked three short questions.

    The ref it compares against is the registry row's own branch where that
    resolves and HEAD otherwise, and which one was used is recorded: a steward
    whose branch was renamed under it is still answerable, and saying which ref
    the answer came from is the difference between a reading and a guess.
    """

    def __init__(self, path: str, branch: str | None):
        self.path = path
        self.branch = (branch or "").strip()
        self.ref = None
        self.ref_label = None
        self.outcome = "ok"
        self.why = None
        if shutil.which("git") is None:
            self.outcome, self.why = "no_tool", "git is not on PATH"
            return
        if not path or not os.path.isdir(path):
            self.outcome = "no_worktree"
            self.why = (f"{path or '(none)'} is not a directory on this disk — "
                        "the row names a worktree that is gone, or this run is "
                        "on another machine")
            return
        ok, _ = self._git("rev-parse", "--git-dir")
        if not ok:
            self.outcome = "no_worktree"
            self.why = f"{rel(path)} is not a git checkout"
            return
        for candidate, label in ((self.branch, self.branch), ("HEAD", "HEAD")):
            if not candidate:
                continue
            ok, out = self._git("rev-parse", "--verify",
                                f"{candidate}^{{commit}}")
            if ok and out:
                self.ref, self.ref_label = candidate, label
                break
        if self.ref is None:
            self.outcome = "no_history"
            self.why = (f"{rel(path)} has no commit on "
                        f"`{self.branch or 'HEAD'}` to read")

    @property
    def usable(self) -> bool:
        return self.outcome == "ok"

    def _git(self, *argv) -> tuple:
        try:
            out = subprocess.run(["git", "-C", self.path, *argv],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return False, ""
        return out.returncode == 0, out.stdout.strip()

    def last_commit(self, path: str | None = None) -> tuple:
        """(commit or None, error or None) — newest commit, optionally by path.

        An empty answer with git exiting 0 is a real absence: no commit on this
        ref has ever touched that path. An answer git refused is not, and the
        two are returned in different slots so no caller can conflate them.
        """
        argv = ["log", "-1", "--format=%H%x1f%ct%x1f%s", self.ref]
        if path:
            argv += ["--", path]
        ok, raw = self._git(*argv)
        if not ok:
            return None, (f"git log would not answer for "
                          f"{path or self.ref_label} in {rel(self.path)}")
        if not raw:
            return None, None
        sha, _, rest = raw.partition("\x1f")
        ct, _, subject = rest.partition("\x1f")
        stamp = from_epoch(ct)
        if not sha or stamp is None:
            return None, "git log answered in a shape this cannot read"
        return {"sha": sha, "at": stamp, "subject": subject}, None

    def as_dict(self) -> dict:
        return {"path": self.path, "branch": self.branch, "ref": self.ref_label,
                "outcome": self.outcome, "why": self.why}


def file_mtime(path: str):
    try:
        return from_epoch(os.stat(path).st_mtime)
    except OSError:
        return None


def file_size(path: str):
    try:
        return os.stat(path).st_size
    except OSError:
        return None


# ── the transcript, and the one number read out of it ────────────────────────
def project_dir_name(path: str) -> str:
    """The directory Claude Code writes a cwd's transcripts to.

    It replaces `/` and `.` with `-` and keeps everything else:
    `~/.claude/skills/peer-panel` becomes
    `an encoded absolute path`, which is what is on disk today.
    """
    return re.sub(r"[/.]", "-", os.path.abspath(os.path.expanduser(path)))


def transcript_root(override: str | None = None) -> str:
    named = override if override is not None else os.environ.get(
        "STEWARD_TRANSCRIPT_DIR")
    named = (named or "").strip()
    return os.path.abspath(os.path.expanduser(named)) if named \
        else TRANSCRIPT_ROOT


def transcript_dirs(worktree: str, root: str) -> list:
    """Where this steward's transcripts could be, newest-relevant first.

    TWO candidates, not one. Section 3.1 puts a steward's session cwd at
    `<worktree>/stewards/` so its role CLAUDE.md binds the way `hub/`'s does,
    and Claude Code keys the transcript directory off the cwd — so a
    pattern-shaped steward writes to the `-stewards` name. A migrated session
    that already exist ran with the worktree itself as cwd and write to the
    other. Both are read and the newest file across them wins, so this answers
    for a migrated steward and an existing one without being told which it is.
    """
    return [os.path.join(root, project_dir_name(os.path.join(worktree, extra)))
            for extra in ("", "stewards")]


def newest_transcript(worktree: str, root: str):
    newest, newest_at = None, None
    for directory in transcript_dirs(worktree, root):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(directory, name)
            try:
                stamp = os.stat(path).st_mtime
            except OSError:
                continue
            if newest_at is None or stamp > newest_at:
                newest, newest_at = path, stamp
    return newest, from_epoch(newest_at) if newest_at is not None else None


def usage_context(usage: dict) -> int:
    """What one model call had in its window: everything read, nothing written.

    input + cache_creation + cache_read, which is how the retro measured the
    context already carried by the session. Output tokens are what the
    call produced and are not in the window it was made against.
    """
    total = 0
    for key in ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def last_usage(path: str) -> tuple:
    """The LAST usage record in this transcript, and how the read went.

    The last, not the largest. A maximum would report a compaction that already
    happened forever — the number this check needs is what the session is
    carrying now, so a steward the hub checkpointed at 09:00 reads as
    checkpointed at 09:01.

    Sidechain records are skipped. A subagent runs in its own small window, and
    its usage line sits in the same file; taking it would report a 900k session
    as a 30k one at exactly the moment a subagent answered last.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return None, ("unreadable", f"{path} could not be sized "
                                    f"({exc.strerror})")
    for window in TAIL_WINDOWS:
        record, err = _scan_tail(path, min(window, size), size)
        if err is not None:
            return None, err
        if record is not None:
            return record, None
        if window >= size:
            break
    return None, ("no_usage", f"{os.path.basename(path)} holds no usage "
                              "record this run could read")


def _scan_tail(path: str, window: int, size: int) -> tuple:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - window))
            chunk = fh.read()
    except OSError as exc:
        return None, ("unreadable", f"{path} could not be read "
                                    f"({exc.strerror})")
    lines = chunk.split(b"\n")
    if size > window and lines:
        lines = lines[1:]   # the first line was cut in half by the seek
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict) or not usage:
            continue
        stamp = record.get("timestamp")
        try:
            at = dt.datetime.fromisoformat(
                str(stamp).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except (ValueError, AttributeError, TypeError):
            at = None
        return {"tokens": usage_context(usage), "at": at,
                "model": record.get("model") or (message or {}).get("model"),
                "usage": usage}, None
    return None, None


# ── joining a dispatch to the steward that launched it ───────────────────────
def within(path: str, parent: str) -> bool:
    try:
        path = os.path.realpath(os.path.expanduser(path))
        parent = os.path.realpath(os.path.expanduser(parent))
    except OSError:
        return False
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def dispatch_of(title: str, entry: dict, slug: str, worktree: str) -> bool:
    """Prefer recorded ownership; only pre-provenance entries use heuristics.

    Descendants inherit owner_steward_slug during registration, so attribution
    survives both renamed workers and removal of the launching session.
    Unknown new provenance is not permission to guess ownership from a title.
    """
    if "owner_steward_slug" in entry:
        return bool(slug) and entry["owner_steward_slug"] == slug
    if any(key in entry for key in ("dispatch_id", "parent_dispatch_id",
                                    "provenance_source", "provenance_state")):
        return False
    # LEGACY FALLBACK: entries recorded before explicit attempt provenance.
    entry_worktree = str(entry.get("worktree") or "")
    if entry_worktree and worktree and within(entry_worktree, worktree):
        return True
    origin = entry.get("origin")
    origin_slug = str((origin or {}).get("slug") or "") if isinstance(
        origin, dict) else ""
    if origin_slug and (origin_slug == slug
                        or origin_slug.startswith(slug + "-")):
        return True
    return bool(slug) and slug in str(title)


# ── one check ────────────────────────────────────────────────────────────────
def result(check: str, reading: dict, finding: str | None = None,
           why: str | None = None, remedy: str | None = None,
           detail=None) -> dict:
    """One answer to one question, with the warrant that produced it.

    `ok` is reachable only where the reading supports an absence. That is not a
    convention this function follows — it is derived here, so a caller cannot
    hand back a green tick for a look that did not happen.

    The one reading that is neither is `not_due`: nothing was owed, so there is
    no absence to warrant and nothing went unlooked-at either.
    """
    if reading.get("warrant") == obs.NOT_DUE:
        state = NOT_DUE
        finding = None
    elif not obs.absence_is_evidence(reading):
        state = UNKNOWN
        finding = None
    else:
        state = FINDING if finding else OK
    return {"check": check, "state": state, "finding": finding,
            "reading": reading, "why": why or reading.get("why"),
            "remedy": remedy, "detail": detail}


def unknown(check: str, source: str, outcome: str, why: str) -> dict:
    return result(check, obs.reading(VOCAB, source, outcome, why=why))


# ── the seven checks ─────────────────────────────────────────────────────────
def check_alive(row: dict, deck_by_id: dict, deck_by_title: dict,
                deck_reading: dict, profile: str | None) -> dict:
    if not obs.absence_is_evidence(deck_reading):
        return unknown("alive", "agent-deck", deck_reading["outcome"],
                       deck_reading["why"] or "the session list could not be "
                                              "read")
    session_id = str(row.get("deck_session_id") or "")
    title = str(row.get("title") or "")
    record = deck_by_id.get(session_id) if session_id else None
    handle = f"id {session_id}" if record else f'"{title}"'
    if record is None:
        record = deck_by_title.get(title)
    reading = obs.reading(VOCAB, "agent-deck", "ok",
                          detail={"handle": handle,
                                  "status": (record or {}).get("status")})
    deck = f"agent-deck -p {profile or '<declared-profile>'}"
    if record is None:
        return result("alive", reading, DEAD,
                      why=f"agent-deck has no session under {handle} — it was "
                          "removed, or this estate is on another profile",
                      remedy=f'{deck} ls --json | grep -i {shlex.quote(title)}'
                             "   # then re-create it, or retire the row")
    if record["archived"]:
        return result("alive", reading, DEAD,
                      why=f"the session under {handle} is ARCHIVED, so nothing "
                          "is running for this row",
                      remedy=f'{deck} session start "{title}"')
    status = record["status"] or "unknown"
    if not any(word in status for word in ALIVE):
        return result("alive", reading, DEAD,
                      why=f"the session under {handle} is `{status}` — it has "
                          "not been running since it went that way",
                      remedy=f'{deck} session stop "{title}"  &&  sleep 3  &&  '
                             f'{deck} session start "{title}"'
                             "   # the SessionStart hook reloads CHARTER.md "
                             "and STATE.md; no hand-written resume")
    return result("alive", reading, why=f"`{status}` under {handle}")


def check_commit_age(row: dict, tree: Worktree, now: dt.datetime) -> dict:
    if not tree.usable:
        return unknown("commit-age", "git", tree.outcome,
                       tree.why or "the branch could not be read")
    if str(row.get("status") or "") == PAUSED:
        return result("commit-age",
                      obs.reading(VOCAB, "git", "paused",
                                  why="paused on the operator's word, so no round was "
                                      "owed and no commit was expected"))
    commit, error = tree.last_commit()
    if error:
        return unknown("commit-age", "git", "git_error", error)
    if commit is None:
        return unknown("commit-age", "git", "no_history",
                       f"{rel(tree.path)} has no commit on "
                       f"`{tree.ref_label}` to age")
    age = (now - commit["at"]).total_seconds()
    detail = {"sha": commit["sha"], "at": commit["at"].isoformat(),
              "subject": commit["subject"], "age_hours": round(age / 3600, 1)}
    reading = obs.reading(VOCAB, "git", "ok", detail=detail)
    if age > IDLE_HOURS * 3600:
        return result("commit-age", reading, IDLE,
                      why=f"its branch has not moved in {humanise(age)} "
                          f"(last {commit['sha'][:9]}, {local(commit['at'])}) "
                          f"and the row is still `active`",
                      remedy="ask it what is in flight, then either resume it "
                             f"or `bin/steward retire {row.get('slug')}`",
                      detail=detail)
    return result("commit-age", reading,
                  why=f"last commit {humanise(age)} ago "
                      f"({commit['sha'][:9]})", detail=detail)


def check_state_current(tree: Worktree, now: dt.datetime) -> dict:
    """Did NOTES.md move while STATE.md stood still?

    Both halves of "file + git" are used, and the later of the two is what
    counts as a move. A round that has rewritten STATE.md but not committed yet
    is current, and a STATE.md whose only evidence is a commit is current too.
    """
    if not tree.usable:
        return unknown("state-current", "git", tree.outcome,
                       tree.why or "the branch could not be read")
    state_path = os.path.join(tree.path, STATE_FILE)
    notes_path = os.path.join(tree.path, NOTES_FILE)
    if not os.path.exists(state_path) and not os.path.exists(notes_path):
        return unknown("state-current", "git", "no_project",
                       f"{rel(tree.path)} holds neither {STATE_FILE} nor "
                       f"{NOTES_FILE} — the project/ home was never rendered")

    moves, detail = {}, {}
    for label, path, relpath in (("state", state_path, STATE_FILE),
                                 ("notes", notes_path, NOTES_FILE)):
        commit, error = tree.last_commit(relpath)
        if error:
            return unknown("state-current", "git", "git_error", error)
        stamps = [s for s in (commit["at"] if commit else None,
                              file_mtime(path)) if s is not None]
        moves[label] = max(stamps) if stamps else None
        detail[label] = {
            "last_commit": commit["sha"] if commit else None,
            "moved_at": moves[label].isoformat() if moves[label] else None,
        }
    reading = obs.reading(VOCAB, "git", "ok", detail=detail)
    if moves["notes"] is None:
        return result("state-current", reading,
                      why="no NOTES.md has ever moved, so STATE.md cannot be "
                          "behind it", detail=detail)
    if moves["state"] is None:
        return result("state-current", reading, DRIFTING_STATE,
                      why=f"{NOTES_FILE} moved {local(moves['notes'])} and "
                          f"{STATE_FILE} does not exist at all",
                      remedy=REMEDY_STATE,
                      detail=detail)
    if moves["notes"] > moves["state"]:
        gap = (moves["notes"] - moves["state"]).total_seconds()
        return result("state-current", reading, DRIFTING_STATE,
                      why=f"{NOTES_FILE} moved {local(moves['notes'])}, "
                          f"{humanise(gap)} after {STATE_FILE} last did "
                          f"({local(moves['state'])}) — the current state is "
                          "in the append-only log, which is the file a "
                          "compacted session cannot reload",
                      remedy=REMEDY_STATE,
                      detail=detail)
    return result("state-current", reading,
                  why=f"STATE.md moved {local(moves['state'])}, at or after "
                      "NOTES.md", detail=detail)


def check_state_size(tree: Worktree) -> dict:
    state_path = os.path.join(tree.path, STATE_FILE)
    size = file_size(state_path)
    if size is None:
        return unknown("state-size", "file", "no_project",
                       f"{rel(tree.path)}/{STATE_FILE} is not there to measure")
    detail = {"bytes": size, "cap": STATE_MAX_BYTES}
    reading = obs.reading(VOCAB, "file", "ok", detail=detail)
    if size > STATE_MAX_BYTES:
        return result("state-size", reading, DRIFTING_SIZE,
                      why=f"{STATE_FILE} is {thousands(size)} bytes, over the "
                          f"{thousands(STATE_MAX_BYTES)}-byte cap — every "
                          "SessionStart and every compaction pays it",
                      remedy='send it "checkpoint" and say the cap: STATE.md '
                             "is a current-state file, and history belongs in "
                             "NOTES.md",
                      detail=detail)
    return result("state-size", reading,
                  why=f"{thousands(size)} bytes of "
                      f"{thousands(STATE_MAX_BYTES)}", detail=detail)


def context_threshold(row: dict) -> tuple:
    raw = row.get("context_threshold")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_THRESHOLD, True
    if value <= 0:
        return DEFAULT_CONTEXT_THRESHOLD, True
    return value, False


def check_context(row: dict, root: str, profile: str | None) -> dict:
    worktree = str(row.get("worktree") or "")
    if not worktree:
        return unknown("context", "transcript", "no_worktree",
                       "the row names no worktree, so no transcript directory "
                       "can be derived")
    path, mtime = newest_transcript(worktree, root)
    if path is None:
        looked = ", ".join(os.path.basename(d)
                           for d in transcript_dirs(worktree, root))
        return unknown("context", "transcript", "no_transcript",
                       f"no .jsonl under {rel(root)}/ for this worktree "
                       f"(looked at {looked})")
    record, error = last_usage(path)
    if error:
        return unknown("context", "transcript", error[0], error[1])
    threshold, defaulted = context_threshold(row)
    tokens = record["tokens"]
    detail = {"tokens": tokens, "threshold": threshold,
              "threshold_defaulted": defaulted,
              "transcript": os.path.basename(path),
              "at": record["at"].isoformat() if record["at"] else (
                  mtime.isoformat() if mtime else None)}
    reading = obs.reading(VOCAB, "transcript", "ok", detail=detail)
    title = str(row.get("title") or "")
    deck = f"agent-deck -p {profile or '<declared-profile>'}"
    # Compaction is requested through the operator, and AT the
    # threshold is already due — the finding is the hub's cue to checkpoint the
    # steward (it rewrites STATE.md and commits) and then compact it, which is
    # safe because the SessionStart hook reloads CHARTER.md and STATE.md.
    if tokens >= threshold:
        return result("context", reading, CONTEXT,
                      why=f"the last model call carried {thousands(tokens)} "
                          f"tokens, at or over its {thousands(threshold)} "
                          "threshold"
                          + (" (the schema default — the row names none)"
                             if defaulted else ""),
                      remedy=f'{deck} session send "{title}" "checkpoint"  '
                             "# 1. it rewrites STATE.md and commits; wait for "
                             "the notes: commit  ·  "
                             f'2. {deck} session send "{title}" "/compact"  '
                             "# only while it is waiting, never mid-dispatch "
                             "(docs/steward-contract.md §3)",
                      detail=detail)
    return result("context", reading,
                  why=f"{thousands(tokens)} of {thousands(threshold)} tokens",
                  detail=detail)


def check_workers(row: dict, entries: dict, reading_in: dict) -> dict:
    if not obs.absence_is_evidence(reading_in):
        return unknown("workers", "dispatches", reading_in["outcome"],
                       reading_in["why"] or "the dispatch store could not be "
                                            "read")
    slug = str(row.get("slug") or "")
    worktree = str(row.get("worktree") or "")
    mine = {title: entry for title, entry in entries.items()
            if dispatch_of(title, entry, slug, worktree)}
    detail = {"overdue": sorted(mine), "due_in_store": len(entries)}
    reading = obs.reading(VOCAB, "dispatches", "ok", detail=detail)
    if mine:
        titles = ", ".join(sorted(mine)[:3])
        more = f" (+{len(mine) - 3} more)" if len(mine) > 3 else ""
        return result("workers", reading, UNATTENDED_WORKER,
                      why=f"{len(mine)} dispatch entr"
                          f"{'ies are' if len(mine) != 1 else 'y is'} past "
                          f"check_after with no verdict collected: "
                          f"{titles}{more}",
                      remedy="bin/dispatches due --json   # collect or defer "
                             "each, then resolve the entry",
                      detail=detail)
    return result("workers", reading, why="no dispatch of its own is overdue",
                  detail=detail)


def check_gate(row: dict, tree_path: str) -> dict:
    """Is the unattended gate wired where the session binds it?

    Inspect the bound settings file; a hook described only in prose is not wired.
    """
    path = os.path.join(tree_path, GATE_FILE)
    if not os.path.exists(path):
        reading = obs.reading(VOCAB, "file", "ok", detail={"path": path})
        return result("gate", reading, UNGATED,
                      why=f"{GATE_FILE} is not in its worktree, so nothing "
                          "answers an AskUserQuestion nobody is awake to hear",
                      remedy=f"git -C {rel(tree_path)} merge main   # the "
                             "role home is tracked on main; the session must "
                             "be restarted to bind it",
                      detail={"path": path})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except OSError as exc:
        return unknown("gate", "file", "unreadable",
                       f"{GATE_FILE} could not be read ({exc.strerror})")
    except ValueError as exc:
        return unknown("gate", "file", "unreadable",
                       f"{GATE_FILE} is not JSON ({exc})")
    wired = "unattended-gate" in json.dumps(settings)
    reading = obs.reading(VOCAB, "file", "ok",
                          detail={"path": path, "wired": wired})
    if not wired:
        return result("gate", reading, UNGATED,
                      why=f"{GATE_FILE} is there and names no "
                          "`unattended-gate` hook",
                      remedy=f"git -C {rel(tree_path)} merge main   # then "
                             "restart the session to bind it",
                      detail={"path": path})
    return result("gate", reading, why="unattended-gate is wired",
                  detail={"path": path})


# ── one steward ──────────────────────────────────────────────────────────────
def check_steward(entry: dict, sources: dict, now: dt.datetime) -> dict:
    """Every check for one registry row, and the row's own verdict.

    A row that would not parse is one `unknown` and no checks at all: nothing
    in it can be trusted to point anywhere, and inventing defaults for the
    fields it is missing would put this check's own guesses in front of the operator as
    readings.
    """
    slug = entry["slug"]
    row = entry.get("row")
    base = {"slug": slug, "path": entry.get("path"), "title": None,
            "status": None, "worktree": None, "branch": None, "epic": None,
            "task": None, "checks": [], "findings": [], "unknowns": [],
            "verdict": UNKNOWN, "worktree_read": None}
    if row is None:
        base["checks"] = [unknown("registry", rel(entry["path"]), "unreadable",
                                  f"{os.path.basename(entry['path'])} "
                                  f"{entry.get('error')}")]
        base["unknowns"] = ["registry"]
        return base

    epic = row.get("epic") if isinstance(row.get("epic"), dict) else {}
    base.update({
        "title": str(row.get("title") or ""),
        "status": str(row.get("status") or ""),
        "worktree": str(row.get("worktree") or ""),
        "branch": str(row.get("branch") or ""),
        "epic": str(epic.get("ref") or "") or None,
        "task": str(row.get("task") or "") or None,
    })

    tree = Worktree(base["worktree"], base["branch"])
    base["worktree_read"] = tree.as_dict()
    checks = [
        check_alive(row, sources["deck_by_id"], sources["deck_by_title"],
                    sources["deck_reading"], sources["profile"]),
        check_commit_age(row, tree, now),
        check_state_current(tree, now),
        check_state_size(tree) if tree.usable else unknown(
            "state-size", "file", tree.outcome,
            tree.why or "the worktree could not be opened"),
        check_context(row, sources["transcript_root"], sources["profile"]),
        check_workers(row, sources["dispatches"],
                      sources["dispatches_reading"]),
        check_gate(row, base["worktree"]) if tree.usable else unknown(
            "gate", "file", tree.outcome,
            tree.why or "the worktree could not be opened"),
    ]
    base["checks"] = checks
    base["findings"] = [c["finding"] for c in checks if c["finding"]]
    base["unknowns"] = [c["check"] for c in checks if c["state"] == UNKNOWN]
    # The row's headline is the ACTIONABLE answer where there is one. A steward
    # whose session is dead and whose transcript could not be found is a dead
    # steward, and burying that under `unknown` would hide the thing somebody
    # has to do something about. The EXIT is computed the other way round
    # (`exit_status`), because there the question is whether anything went
    # unlooked-at.
    base["verdict"] = (FINDING if base["findings"]
                       else UNKNOWN if base["unknowns"] else OK)
    return base


def gather(registry: str, *, profile=None, deck_fixture=None,
           dispatches_fixture=None, transcripts=None, slugs=(),
           now=None) -> dict:
    """Every registered steward, checked. The one entry point a caller needs."""
    now = now or now_utc()
    entries, registry_reading = load_registry(registry)
    wanted = {s.strip() for s in slugs if s and s.strip()}
    considered = [e for e in entries
                  if (e.get("row") or {}).get("status") != RETIRED]
    retired = len(entries) - len(considered)
    if wanted:
        considered = [e for e in considered if e["slug"] in wanted]

    deck_by_id, deck_by_title, deck_reading = ({}, {}, None)
    dispatches, dispatches_reading = ({}, None)
    if considered:
        deck_by_id, deck_by_title, deck_reading = read_deck(profile,
                                                            deck_fixture)
        dispatches, dispatches_reading = read_dispatches(dispatches_fixture)
    sources = {
        "profile": profile, "deck_by_id": deck_by_id,
        "deck_by_title": deck_by_title, "deck_reading": deck_reading,
        "dispatches": dispatches, "dispatches_reading": dispatches_reading,
        "transcript_root": transcript_root(transcripts),
    }
    rows = [check_steward(entry, sources, now) for entry in considered]
    rows.sort(key=lambda r: (VERDICTS.index(r["verdict"]), r["slug"]))
    readings = [r for r in (registry_reading, deck_reading, dispatches_reading)
                if r is not None]
    return {
        "at": now, "registry": registry, "registry_reading": registry_reading,
        "readings": readings, "rows": rows, "retired": retired,
        "registered": len(entries), "profile": profile,
        "transcript_root": sources["transcript_root"],
        "filtered": bool(wanted),
    }


# ── the verdict of the run ───────────────────────────────────────────────────
def tally(rows: list) -> dict:
    counts = {FINDING: 0, UNKNOWN: 0, OK: 0}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return counts


def findings(rows: list) -> list:
    out = []
    for row in rows:
        for check in row["checks"]:
            if check["finding"]:
                out.append((row, check))
    return out


def unknowns(rows: list) -> list:
    out = []
    for row in rows:
        for check in row["checks"]:
            if check["state"] == UNKNOWN:
                out.append((row, check))
    return out


def exit_status(report: dict) -> int:
    """0 every look clean, 1 a finding, 2 a look that could not be made.

    Section 3.7's order, and `2` outranks `1` here rather than the other way
    round: a check that could not be made may be hiding a finding, and the
    third verdict is never a pass.

    A `not_due` check counts towards neither: a paused steward was owed no
    round, so its unmoved branch is not a finding and its unasked question is
    not a hole.

    A registry directory that does not exist is the one shape that is NOT a
    `2`. There are no stewards to have looked at — nothing was attempted
    because nothing was owed — and the output says so in the reading's own
    words rather than in a status. A registry that is there and would not be
    READ is a `2`, because then something was owed and broke.
    """
    if report["registry_reading"]["outcome"] == "unreadable":
        return 2
    if unknowns(report["rows"]):
        return 2
    if findings(report["rows"]):
        return 1
    return 0


# ── the vocabulary ───────────────────────────────────────────────────────────
# Registered in lib/estate_observation.py, alongside every other subsystem that
# records an observation. This module's own words are documented there.
VOCABULARY = obs.vocabulary(VOCAB)


import argparse
MARK = {OK: "ok     ", FINDING: "FINDING", UNKNOWN: "???????"}
HEADING = {
    FINDING: "something is wrong and somebody has to act",
    UNKNOWN: "the look could not be made — never a soft pass",
    OK: "every look was made and found nothing",
}


def source_line(reading: dict) -> str:
    mark = "ok   " if obs.absence_is_evidence(reading) else "?????"
    detail = reading.get("detail") or {}
    if detail:
        extra = ", ".join(f"{k}={v}" for k, v in sorted(detail.items()))
    else:
        extra = reading["outcome"]
    return f"   [{mark}] {reading['source']:<22} {extra}"


def row_lines(row: dict) -> list:
    head = (f"{MARK[row['verdict']]}  {row['slug']}"
            + (f"  ({row['status']})" if row["status"] else ""))
    lines = [head]
    if row["title"]:
        lines.append(f"          {row['title']}")
    for check in row["checks"]:
        if check["state"] == OK:
            continue
        flag = check["finding"] or check["state"]
        lines.append(f"          · {flag:<18} {check['why']}")
        if check["remedy"]:
            lines.append(f"            → {check['remedy']}")
    clean = [c["check"] for c in row["checks"] if c["state"] == OK]
    if clean:
        lines.append(f"          · clean: {', '.join(clean)}")
    return lines


def render_report(report: dict, status: int) -> str:
    rows = report["rows"]
    counts = tally(rows)
    at = report["at"]
    lines = [
        f"steward health · {at.strftime('%Y-%m-%dT%H:%MZ')} · registry "
        f"{rel(report['registry'])} · profile {report['profile'] or '—'}",
        f"rows: {counts[OK]} ok · {counts[FINDING]} FINDING · "
        f"{counts[UNKNOWN]} unknown"
        + (f" · {report['retired']} retired (not checked)"
           if report["retired"] else ""),
    ]
    # The row counts above are VERDICTS, and a row leads with its actionable
    # answer — so a steward that is both dead and half-unreadable counts as one
    # FINDING there. The checks that could not be made are counted separately,
    # because "nothing went unlooked-at" is its own claim and the exit status
    # is made of it.
    unmade = unknowns(rows)
    if unmade:
        lines.append(f"checks: {len(unmade)} of "
                     f"{sum(len(r['checks']) for r in rows)} could not be "
                     "made — see `unknown` under each steward")
    if report["filtered"]:
        lines.append(f"--slug narrowed this run to {len(rows)} of "
                     f"{report['registered']} registered row(s); the status "
                     "is about what was checked")
    lines.append("")

    lines.append("── what was read")
    for reading in report["readings"]:
        lines.append(source_line(reading))
        if reading["why"]:
            lines.append(f"            · {reading['why']}")
    lines.append("")

    if not rows:
        lines.append("── no steward was checked")
        why = report["registry_reading"]["why"]
        if report["registry_reading"]["outcome"] == "ok":
            lines.append("   no stewards registered — the registry directory "
                         "is there and holds no row. Nothing is running, and")
            lines.append("   this run looked.")
        else:
            lines.append(f"   no stewards registered — {why}")
            lines.append("   That is NOT a pass on stewards. It is the "
                         "statement that no look was owed, with its warrant "
                         "attached.")
        lines.append("")
        lines.append("── deliberately not checked here")
        for what, reason in NOT_CHECKED:
            lines.append(f"   · {what}")
            lines.append(f"     {reason}")
        return "\n".join(lines) + "\n"

    for verdict in VERDICTS:
        group = [r for r in rows if r["verdict"] == verdict]
        if not group:
            continue
        lines.append(f"── {verdict} ({len(group)}) — {HEADING[verdict]}")
        for row in group:
            lines.extend(row_lines(row))
        lines.append("")

    if unknowns(rows):
        lines.append("── reading an unknown")
        lines.append("   It is not a soft `ok` and not a soft `finding`. That "
                     "question could not be asked at all, so the steward it")
        lines.append("   was asked about is neither cleared nor accused. An "
                     "absence of evidence is never evidence of an absence")
        lines.append("   (docs/absence-contract.md).")
        lines.append("")

    lines.append("── deliberately not checked here")
    for what, reason in NOT_CHECKED:
        lines.append(f"   · {what}")
        lines.append(f"     {reason}")
    lines.append("")
    lines.append(f"exit {status}")
    return "\n".join(lines) + "\n"


def json_report(report: dict, status: int) -> dict:
    def check(entry):
        return {"check": entry["check"], "state": entry["state"],
                "finding": entry["finding"], "why": entry["why"],
                "remedy": entry["remedy"], "reading": entry["reading"],
                "detail": entry["detail"]}

    return {
        "at": report["at"].isoformat(),
        "registry": report["registry"],
        "profile": report["profile"],
        "transcript_root": report["transcript_root"],
        "registered": report["registered"],
        "retired": report["retired"],
        "readings": report["readings"],
        "counts": tally(report["rows"]),
        "exit": status,
        "rows": [{
            "slug": row["slug"], "title": row["title"],
            "status": row["status"], "worktree": row["worktree"],
            "branch": row["branch"], "epic": row["epic"], "task": row["task"],
            "verdict": row["verdict"], "findings": row["findings"],
            "unknowns": row["unknowns"], "worktree_read": row["worktree_read"],
            "checks": [check(c) for c in row["checks"]],
        } for row in report["rows"]],
        "not_checked": [{"what": w, "why": y} for w, y in NOT_CHECKED],
    }


def cmd_list() -> int:
    print("steward-health — definitions (reads no state)")
    print()
    print("the question, per registered steward")
    print("  is this session alive, is its state current, and is anything")
    print("  waiting on it that nobody is watching?")
    print()
    print("the seven checks")
    for name, finding, asks in CHECKS:
        print(f"  {name:<14} {asks}")
        print(f"  {'':<14}   finding: {finding}")
    print()
    print("thresholds")
    print(f"  idle            no commit in {IDLE_HOURS} h on an `active` row")
    print(f"  STATE.md        over {STATE_MAX_BYTES:,} bytes")
    print(f"  context         at or over the row's own context_threshold "
          f"(default {DEFAULT_CONTEXT_THRESHOLD:,})")
    print()
    print("verdicts")
    print(f"  {OK:<14} the look happened, worked, and found nothing")
    print(f"  {FINDING:<14} the look happened, worked, and found it")
    print(f"  {UNKNOWN:<14} the look could not be made")
    print()
    print("outcome words (lib/estate_observation.py, vocabulary "
          f"`{VOCAB}`)")
    vocab = obs.vocabulary(VOCAB)
    for warrant in obs.CLASSES:
        words = vocab.outcomes_in(warrant)
        if words:
            print(f"  {warrant:<14} {', '.join(words)}")
    print()
    print("exit: 0 clean · 1 any finding · 2 any look that could not be made")
    print()
    print("deliberately not checked")
    for what, why in NOT_CHECKED:
        print(f"  · {what}")
        print(f"      {why}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="steward-health",
        description="is each registered steward alive, current and "
                    "unblocked? (read-only; restarts nothing)")
    ap.add_argument("--slug", action="append", default=[],
                    help="only this steward (repeatable)")
    ap.add_argument("--registry", default=None,
                    help="the directory holding <slug>.json (default: "
                         "$STEWARD_STATE_DIR, else the main checkout's "
                         "state/stewards)")
    ap.add_argument("--profile", default=None,
                    help="the agent-deck profile (default: bin/deck-profile)")
    ap.add_argument("--transcript-root", default=None,
                    help="where Claude Code's project directories live "
                         "(default: $STEWARD_TRANSCRIPT_DIR, else "
                         "~/.claude/projects)")
    ap.add_argument("--deck-json", default=None,
                    help="read `agent-deck ls --json` output from this file "
                         "instead of running it")
    ap.add_argument("--dispatches-json", default=None,
                    help="read `dispatches due --json` output from this file "
                         "instead of running it")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true",
                    help="print the definitions and exit, reading no state")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()

    registry = registry_dir(args.registry)
    profile = args.profile or deck_profile()
    report = gather(registry, profile=profile,
                       deck_fixture=args.deck_json,
                       dispatches_fixture=args.dispatches_json,
                       transcripts=args.transcript_root,
                       slugs=args.slug)
    status = exit_status(report)
    if args.json:
        import json
        print(json.dumps(json_report(report, status), indent=2,
                         sort_keys=True))
    else:
        sys.stdout.write(render_report(report, status))
    return status


if __name__ == "__main__":
    sys.exit(main())

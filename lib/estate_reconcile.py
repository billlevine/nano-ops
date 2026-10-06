"""Cross-store scope reconciliation the contract, gap audit Q-06).

THE TWO STORES
--------------
This estate keeps facts in two places that are not copies of each other:

  THE ESTATE STORE — the `memories` table in state/estate.db. Every row carries
  a `scope`, and `bin/estate memory recall --scope X` serves exactly the active
  rows at X plus the active rows at `shared` (P-05,
  docs/memory-recall-contract.md). Scope discipline is the whole point: a fact
  the hub learned reaches the hub and nobody else.

  THE SESSION AUTO-MEMORY STORE — per-fact markdown files plus MEMORY.md under
  ~/.claude/projects/<repo-slug>/memory/. Claude Code resolves it by git repo
  root, so every session in this estate — hub, mechanic, briefer, night shift,
  and every dispatch worker in a linked worktree — loads the same files
  automatically, across compactions. bin/memory-note is its one safe writer.

Neither is a cache of the other and nothing syncs them. That is deliberate and
this module does not change it.

THE ONE SENTENCE THIS MODULE EXISTS FOR
---------------------------------------
The session store has exactly one scope, and it is `shared`.

Every file in it reaches every session, unconditionally, with no field that
could say otherwise. So when the estate holds a fact at `hub` and the session
store holds the same fact, that fact is held at TWO scopes at once — `hub` in
the store that enforces scope, and estate-wide in the store that does not. The
estate's scope discipline is then not what a loader actually experiences, which
is the leak Q-06 named: `feedback_cursor-advance-skips-interleaved-messages.md`
is m-50, filed at `hub`, and read every night by four loops that m-50 was never
meant to reach.

WHAT COUNTS AS EVIDENCE, AND WHAT DOES NOT
------------------------------------------
Deciding that a markdown file and a database row hold "the same fact" is a
judgment, and this module refuses to fake having made it. It follows the
absence contract's discipline (docs/absence-contract.md): a reading is only
evidence when the thing that would have produced it was actually observed.

  DECLARED — a fact file whose frontmatter carries a line `estate: m-50` (or
  `estate: m-50, m-43` for a file that accumulates several). That is a human or
  a session stating the link, and it is the only class this module treats as
  observed. Findings are computed over declared pairs and nothing else.

  UNLINKED — a fact file with no such line. This module cannot tell whether it
  duplicates anything, and says so per file rather than scoring it as clean. A
  reconciliation over a store where every file is unlinked has found nothing
  and must not read as "nothing to find".

  SUSPECTED — a lexical resemblance between a memory body and a note body,
  above a worklist floor. This is a WORKLIST, never a finding. It exists so the
  operator has somewhere to start declaring links, and every row prints its
  score so a strong resemblance is visibly different from a weak one.

Three probes against the live store settled that last point the hard way: no
purely lexical threshold separates a duplicate from a topical neighbour in this
corpus. The house vocabulary is too uniform, and one running-log file that
accumulates eight occurrences of one incident resembles half the table. So the
score ranks a worklist and decides nothing, and `--min-score` is documented as
a floor on that worklist rather than a boundary between true and false.

THE RESEMBLANCE MEASURE
-----------------------
IDF-weighted token containment. The corpus is every memory body plus every note
body; a token's weight is log((N + 1) / (1 + documents containing it)) + 1, so
vocabulary every document shares ("cursor", "hub", "message") weighs little and
a rare identifier weighs several times as much. The score is the shared weight
over the weight of whichever document is lighter, which is what lets a
one-paragraph memory match against a long file that has absorbed it.

The +1 floor is load-bearing and Weights.of() says why at length: the
unsmoothed form drives exactly the tokens two documents SHARE to zero weight on
a small corpus, so a two-file store would have scored every pair 0.0.

Containment rather than Jaccard for exactly that reason: the session store's
files grow by accretion and the estate's rows do not, so a real pair is
routinely a short row inside a long file, and Jaccard would punish it for the
length difference alone.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path

# The env override every consumer honours, named once. bin/memory-note has
# always read it; the reconciler reads the same one so a test that isolates one
# of them isolates both.
MEMORY_DIR_ENV = "MEMORY_NOTE_DIR"

# The session store's one scope. Not a configuration knob — it is a fact about
# how Claude Code loads the directory, and naming it as a constant is what lets
# the divergence rule below be a single comparison.
SESSION_SCOPE = "shared"

# The index, not a fact. Never compared against anything.
INDEX_FILE = "MEMORY.md"

# `estate: m-50` / `estate: m-50, m-43`, at any indentation, anywhere in the
# frontmatter block. Deliberately a line scan and not a YAML parse: these files
# are hand-written by several different sessions, the key turns up both at the
# top level and under `metadata:`, and a parser that rejected one spelling
# would silently drop a link somebody did declare.
DECLARE_RE = re.compile(r"^\s*estate\s*:\s*(.+?)\s*$", re.MULTILINE)
ID_RE = re.compile(r"m-\d+")

# "- [slug](typed_file.md) — summary". The same line shape bin/memory-note
# writes and removes; the filename is what identifies the note.
INDEX_LINE_RE = re.compile(r"^-\s*\[[^\]]*\]\(([^)]+)\)", re.MULTILINE)

# Tokens shorter than this carry no identity. Keeps `a`, `is`, `of` out without
# maintaining a stopword list that would need an opinion per word — IDF already
# handles the common words that survive.
MIN_TOKEN = 3
WORD_RE = re.compile(r"[a-z0-9][a-z0-9/_.#-]*")

# The worklist floor. Chosen against the live store, where it surfaces roughly
# a dozen pairs out of ~1900 and includes every hub-local duplicate known at
# the time. It is a floor on how much to read, not a claim about any pair.
DEFAULT_MIN_SCORE = 0.40

# Verdicts. Four words. The fourth arrived with the contract's scope migration and is
# the reason the third sentence of DIVERGENT had to be split: "the session store
# shares it" was true of every file on disk, because the contract's claim is
# about the DIRECTORY. What is actually loaded into every session is MEMORY.md,
# the index — a note file with no line in it is on disk and in nobody's context.
# So a fact deliberately narrowed to `hub` and unlinked from the index is not a
# divergence to fix; it is the fix. Reporting every one of those as
# `divergent` would bury the ones that are real.
DIVERGENT = "divergent"   # estate holds it locally; the index still loads it
ALIGNED = "aligned"       # both stores hold it estate-wide; nothing to fix
SCOPED = "scoped"         # estate holds it locally and the index no longer loads it
DANGLING = "dangling"     # the file names a memory id the store does not have


def repo_root() -> Path:
    """The main worktree's root — the key the harness resolves memory by.

    `--git-common-dir` is the main repo's .git even from inside a linked
    worktree, which is why a dispatch worker in state/hub/worktrees/* reads the
    same session store the hub does.
    """
    out = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=True).stdout.strip()
    return Path(out).parent


def memory_dir() -> Path:
    """The session auto-memory directory this repo's sessions all resolve to."""
    override = os.environ.get(MEMORY_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects" / \
        str(repo_root()).replace("/", "-") / "memory"


def split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter, body). A file with no frontmatter is all body."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return "", text


def declared_ids(frontmatter: str) -> list[str]:
    """Every memory id the file declares itself a copy of, in order, deduped."""
    seen, out = set(), []
    for match in DECLARE_RE.finditer(frontmatter):
        for key in ID_RE.findall(match.group(1)):
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def tokens(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if len(w) >= MIN_TOKEN}


def load_notes(directory: Path) -> list[dict]:
    """Every fact file in the session store, MEMORY.md excluded.

    A directory that does not exist yields no notes. The caller reports that as
    a store it could not read, never as a store with nothing in it.
    """
    notes = []
    if not directory.is_dir():
        return notes
    listed = indexed_names(directory)
    for path in sorted(directory.glob("*.md")):
        if path.name == INDEX_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append({"name": path.name, "unreadable": str(exc),
                          "declares": [], "tokens": set(), "body": "",
                          "indexed": True})
            continue
        frontmatter, body = split_frontmatter(text)
        notes.append({"name": path.name, "unreadable": None,
                      "declares": declared_ids(frontmatter),
                      "tokens": tokens(body), "body": body,
                      "indexed": listed is None or path.name in listed})
    return notes


def indexed_names(directory: Path) -> set | None:
    """Every file MEMORY.md lists, or None when the index could not be read.

    None is not an empty set. The index is what Claude Code loads into every
    session; a note it lists is in everyone's context and a note it does not is
    on disk only. An index nobody could read answers neither question, and
    `verdict()` treats None as "assume listed" so an unreadable index can never
    quietly clear a real divergence.
    """
    try:
        text = (directory / INDEX_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    return {m.group(1) for m in INDEX_LINE_RE.finditer(text)}


class Weights:
    """IDF over one corpus, built once and asked many times."""

    def __init__(self, documents):
        self.n = 0
        self.df: dict[str, int] = {}
        for doc in documents:
            self.n += 1
            for token in doc:
                self.df[token] = self.df.get(token, 0) + 1

    def of(self, token: str) -> float:
        """Smoothed IDF, floored at 1 rather than at 0.

        The unsmoothed form goes negative — and so, clamped, to zero — as soon
        as a token appears in a large enough share of the corpus, and on a
        SMALL corpus that is every token the two documents share. A store with
        two files would have scored every pair 0.0 and reported no suspects at
        all: a metric that answers "nothing here" loudest when it has the least
        to go on, which is the failure this whole read is built against. The
        +1 keeps a shared common token worth something and leaves a rare one
        worth several times as much, which is all the ranking needs.
        """
        return math.log((self.n + 1) / (1 + self.df.get(token, 0))) + 1.0

    def mass(self, doc) -> float:
        return sum(self.of(t) for t in doc)


def resemblance(a: set[str], b: set[str], weights: Weights) -> float:
    """Weighted containment of the lighter document in the heavier one."""
    lighter = min(weights.mass(a), weights.mass(b))
    if lighter <= 0:
        return 0.0
    return weights.mass(a & b) / lighter


def verdict(memory_scope: str, indexed: bool = True) -> str:
    """What a declared pair is, given where the estate keeps its side.

    Still one comparison on scope, because the session store's side of every
    pair is SESSION_SCOPE by construction. `indexed` decides only what a
    NARROWER estate scope means: a note MEMORY.md still lists reaches every
    session in this repository and diverges from its estate row; one nothing
    lists does not, and is `scoped` — the end state of a deliberate migration
    rather than a defect.

    `indexed` defaults True, and the default is the load-bearing half: a caller
    that could not read the index must not get `scoped` for free. An unreadable
    index is not evidence that nothing is listed in it
    (docs/absence-contract.md), so the conservative direction — keep reporting
    the divergence — is what an absent answer produces.
    """
    if memory_scope == SESSION_SCOPE:
        return ALIGNED
    return DIVERGENT if indexed else SCOPED


def reconcile(memories, notes, *, min_score: float = DEFAULT_MIN_SCORE,
              top: int | None = None) -> dict:
    """The whole cross-store read.

    `memories` is the live (candidate + active) rows as dicts with id, scope,
    status and body. `notes` is what load_notes() returned. Nothing here writes
    to either store, and nothing here decides an undeclared pair.
    """
    by_id = {m["id"]: m for m in memories}
    weights = Weights([tokens(m["body"]) for m in memories]
                      + [n["tokens"] for n in notes])

    findings, unlinked, unreadable = [], [], []
    for note in notes:
        if note["unreadable"]:
            unreadable.append({"note": note["name"], "error": note["unreadable"]})
            continue
        if not note["declares"]:
            unlinked.append(note["name"])
            continue
        for key in note["declares"]:
            row = by_id.get(key)
            if row is None:
                findings.append({"note": note["name"], "memory": key,
                                 "verdict": DANGLING, "memory_scope": None,
                                 "memory_status": None, "session_scope": SESSION_SCOPE})
                continue
            findings.append({
                "note": note["name"], "memory": key,
                "verdict": verdict(row["scope"], note.get("indexed", True)),
                "memory_scope": row["scope"], "memory_status": row["status"],
                "indexed": note.get("indexed", True),
                "session_scope": SESSION_SCOPE,
                "score": round(resemblance(tokens(row["body"]),
                                           note["tokens"], weights), 3),
            })

    declared = {(f["note"], f["memory"]) for f in findings}
    suspected = []
    for memory in memories:
        a = tokens(memory["body"])
        for note in notes:
            if note["unreadable"] or (note["name"], memory["id"]) in declared:
                continue
            score = resemblance(a, note["tokens"], weights)
            if score >= min_score:
                suspected.append({
                    "note": note["name"], "memory": memory["id"],
                    "memory_scope": memory["scope"],
                    "memory_status": memory["status"],
                    "session_scope": SESSION_SCOPE,
                    "would_be": verdict(memory["scope"],
                                        note.get("indexed", True)),
                    "score": round(score, 3),
                })
    suspected.sort(key=lambda s: (-s["score"], s["memory"], s["note"]))
    if top is not None:
        suspected = suspected[:top]

    findings.sort(key=lambda f: (f["verdict"] != DIVERGENT, f["memory"], f["note"]))
    return {
        "session_scope": SESSION_SCOPE,
        "counts": {
            "memories": len(memories),
            "notes": len(notes),
            "divergent": sum(1 for f in findings if f["verdict"] == DIVERGENT),
            "aligned": sum(1 for f in findings if f["verdict"] == ALIGNED),
            "scoped": sum(1 for f in findings if f["verdict"] == SCOPED),
            "dangling": sum(1 for f in findings if f["verdict"] == DANGLING),
            "unlinked": len(unlinked),
            "unreadable": len(unreadable),
            "suspected": len(suspected),
        },
        "findings": findings,
        "suspected": suspected,
        "unlinked": unlinked,
        "unreadable": unreadable,
        "min_score": min_score,
    }

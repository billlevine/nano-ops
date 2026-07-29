#!/usr/bin/env python3
"""mechanic engine — deterministic state for the nightly self-optimization
pass over this estate.

DIVISION OF LABOR
-----------------
This script owns bookkeeping only: the clock/window state, the pass history,
and a deterministic digest of the estate's inputs. It NEVER analyzes, edits,
or proposes — the nightly pass lives in the SKILL.md prose, which calls this
script for state I/O. That split is the loop convention: deterministic work
belongs in a script (testable, and free to re-run), judgment in the skill.

DATA (state/mechanic/, override with $MECHANIC_STATE_DIR)
---------------------------------------------------------
  config.toml     pass window (pass_start/pass_end, local HH:MM)
  history.jsonl   append-only per-pass event history (authoritative, never
                  merged or truncated)
  REPORT.md       the morning report the skill writes each pass
  digest.json     baseline snapshot for the incremental digest (file hashes,
                  ledger byte cursor, state sizes, git HEAD) — derived cache,
                  safe to delete: a missing snapshot just means a full digest
  last_tick       heartbeat, written by the loop session

SUBCOMMANDS
-----------
  mechanic.py windows          print clock state: phase=pass|resume|done|idle
  mechanic.py gather           incremental digest (see below)
  mechanic.py record '<json>'  append an event line (auto ts + night) to
                               history.jsonl; requires an "event" field
  mechanic.py report           print REPORT.md (the morning view)

The window is half-open [pass_start, pass_end) on local HH:MM; it wraps
midnight when start > end. A "night" is identified by the local date the
window's start belongs to, so one pass per night holds across the wrap.
Phase derivation reads tonight's pass_start/pass_done events from
history.jsonl: no events -> pass, started-not-done -> resume (an interrupted
pass to finish), done -> done. Only those two event names gate the window;
everything else (findings, dry_run, ...) passes through untouched.

THE DIGEST (gather)
-------------------
`gather` is the pass's whole input, and it is INCREMENTAL: it diffs the estate
against `digest.json` (last night's snapshot) and prints only what moved.

  policy files   root/hub/loop CLAUDE.md + SKILL.md, docs/lessons.md,
                 docs/ideas.md — hashed whole-file AND per section. Unchanged
                 files are one line each ("unchanged"); changed files print
                 only their changed/new sections. The model must NOT re-read a
                 file the digest reports unchanged.
  sessions       ONE `agent-deck -p <profile> ls --json` snapshot for every
                 session, not a per-session `show` sweep, plus derived
                 registry-vs-live drift (model mismatch, missing/odd status,
                 heartbeat staler than 2*interval+120s).
  state files    sizes with deltas since the snapshot; unchanged files collapse
                 to a count.
  ledger         only entries appended since the snapshot's byte cursor (last
                 24h on a cold run or if the file was rotated/truncated). Each
                 entry's "detail" is capped; ts/actor/kind/summary/refs are
                 verbatim, and the raw file is still there to grep.
  extraction     OPTIONAL lens, for an estate that is a private fork of a
                 public core. Every path on the extraction allowlist (the
                 manifest; it lives in the fork whose paths it governs, so
                 there is no second copy to drift) resolved into exactly three
                 buckets: (a) new & matching, never extracted, (b) already
                 extracted and CHANGED since — drift, the ongoing sync case,
                 (c) explicitly excluded, named rather than absent. Plus every
                 unresolved candidate in state/extraction/candidates.json,
                 carried forward until it reaches a resting state. Read-only:
                 the mechanic proposes candidates and never writes that store.
                 Configured by loops.toml's [extraction] table; with no such
                 table the lens prints one note and does nothing, which is the
                 normal case for an estate that is nobody's fork.
  git            commits since the snapshot's HEAD (last 20 on a cold run).

A cold run — no `digest.json` — prints the full estate, so a first pass (or a
pass after deleting the snapshot) is never short-changed. The snapshot is
rewritten only when its night differs from tonight's, so a `resume` tick
re-gathers the SAME digest the interrupted pass saw.

  gather --full      ignore the baseline, print everything (cold-run output)
  gather --no-save   don't touch digest.json (ad hoc/dry inspection)

IDENTITY IS CONFIGURATION
-------------------------
Nothing here names an operator, a persona, a session title, a channel or an
absolute path. The hub's session title and the agent-deck profile resolve from
loops.toml's [hub] table exactly as bin/ops and bin/dashboard resolve them
(persona -> "<persona> (hub)", overridable with session_title; deck_profile),
falling back to the neutral "ops". A patch that bakes one of those in is a bug.

$MECHANIC_NOW (ISO datetime) freezes the clock for tests and dry runs.
$MECHANIC_DECK_PROFILE overrides the agent-deck profile from [hub].
$MECHANIC_REPO_ROOT overrides the upward loops.toml search (tests).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # < 3.11
    tomllib = None


def repo_root() -> str:
    override = os.environ.get("MECHANIC_REPO_ROOT")
    if override:
        return override
    d = os.path.dirname(os.path.abspath(__file__))
    while d != "/":
        if os.path.exists(os.path.join(d, "loops.toml")):
            return d
        d = os.path.dirname(d)
    raise SystemExit("mechanic.py: cannot find repo root (no loops.toml upward)")


def state_dir() -> str:
    d = os.environ.get("MECHANIC_STATE_DIR") or os.path.join(
        repo_root(), "state", "mechanic")
    os.makedirs(d, exist_ok=True)
    return d


CONFIG_DEFAULTS = {"pass_start": "02:00", "pass_end": "05:00"}


def load_config() -> dict:
    path = os.path.join(state_dir(), "config.toml")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(
                "# mechanic pass window (local HH:MM, half-open [start, end),\n"
                "# wraps midnight when start > end). Empty pass_start disables\n"
                "# the nightly pass entirely (heartbeat-only loop).\n"
                f'pass_start = "{CONFIG_DEFAULTS["pass_start"]}"\n'
                f'pass_end = "{CONFIG_DEFAULTS["pass_end"]}"\n')
    cfg = dict(CONFIG_DEFAULTS)
    if tomllib:
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def local_now() -> dt.datetime:
    override = os.environ.get("MECHANIC_NOW")
    if override:
        t = dt.datetime.fromisoformat(override)
        return t if t.tzinfo else t.astimezone()
    return dt.datetime.now().astimezone()


def in_window(now_hm: str, start: str, end: str) -> bool:
    """Half-open [start, end) on local HH:MM strings. Wraps midnight when
    start > end. Empty start = window off; empty end = start..midnight."""
    if not start:
        return False
    if not end:
        return now_hm >= start
    if start <= end:
        return start <= now_hm < end
    return now_hm >= start or now_hm < end


def night_id(now_local: dt.datetime, start: str, end: str) -> str:
    """The local date this moment's night belongs to: today, except in the
    after-midnight tail of a window that wraps midnight, which still belongs
    to the previous date's night."""
    now_hm = now_local.strftime("%H:%M")
    if start and end and start > end and now_hm < end:
        return (now_local - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return now_local.strftime("%Y-%m-%d")


def load_history() -> list[dict]:
    path = os.path.join(state_dir(), "history.jsonl")
    events = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def derive_phase(now_local: dt.datetime, cfg: dict,
                 events: list[dict]) -> tuple[str, str, str]:
    start = (cfg.get("pass_start") or "").strip()
    end = (cfg.get("pass_end") or "").strip()
    night = night_id(now_local, start, end)
    now_hm = now_local.strftime("%H:%M")

    if not in_window(now_hm, start, end):
        if start:
            note = (f"outside pass window ({start}–{end or 'midnight'}) "
                    "— heartbeat only")
        else:
            note = "no pass window configured — heartbeat only"
        return "idle", night, note

    tonight = [e for e in events if e.get("night") == night]
    if any(e.get("event") == "pass_done" for e in tonight):
        return "done", night, "tonight's pass already ran — heartbeat only"
    if any(e.get("event") == "pass_start" for e in tonight):
        return ("resume", night,
                "pass started but not finished — replay tonight's history "
                "and complete it")
    return ("pass", night,
            f"pass window active ({start}–{end}) — run tonight's pass")


def parse_ts(v) -> dt.datetime | None:
    """Central-ledger timestamps come in three shapes: ISO with Z, ISO with
    offset, and bare epoch ints. Normalize to aware UTC; None if unparseable."""
    if isinstance(v, (int, float)):
        try:
            return dt.datetime.fromtimestamp(v, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        try:
            t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return None


def recent_entries(lines, now_utc: dt.datetime, hours: int = 24) -> list[dict]:
    cutoff = now_utc - dt.timedelta(hours=hours)
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = parse_ts(e.get("ts"))
        if t and t >= cutoff:
            out.append(e)
    return out


# --------------------------------------------------------------------------- #
# the incremental digest
# --------------------------------------------------------------------------- #
SNAPSHOT_VERSION = 1
SNAPSHOT_NAME = "digest.json"

# Files that define what the estate DOES — the manuals the pass used to re-read
# in full every night. Globs are relative to the repo root.
POLICY_GLOBS = [
    "CLAUDE.md",
    "hub/CLAUDE.md",
    "hub/.claude/skills/*/SKILL.md",
    "loops/*/CLAUDE.md",
    "loops/*/.claude/skills/*/SKILL.md",
    "docs/lessons.md",
    "docs/ideas.md",
]
HEADING_RE = re.compile(r"^#{1,3} +\S")
MAX_SECTION_LINES = 100      # per changed section, before eliding the tail
MAX_CHANGED_SECTIONS = 10    # per changed file
LEDGER_DETAIL_CAP = 300      # chars of an entry's "detail" field


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def policy_paths(root: str) -> list[str]:
    """Repo-relative paths of the policy files, sorted and deduped."""
    seen = []
    for pat in POLICY_GLOBS:
        for p in sorted(glob.glob(os.path.join(root, pat))):
            rel = os.path.relpath(p, root)
            if rel not in seen and os.path.isfile(p):
                seen.append(rel)
    return sorted(seen)


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (title, body) on h1-h3 headings. Content before the
    first heading (frontmatter, intro) is "(preamble)". Repeated titles get a
    #2, #3 suffix so a key identifies one section; keys are heading TEXT, not
    positions, so inserting a section doesn't invalidate its neighbours."""
    out: list[tuple[str, list[str]]] = []
    counts: dict[str, int] = {}

    def open_section(title: str) -> None:
        counts[title] = counts.get(title, 0) + 1
        key = title if counts[title] == 1 else f"{title} #{counts[title]}"
        out.append((key, []))

    for line in text.splitlines():
        if HEADING_RE.match(line):
            open_section(line.strip())
        elif not out:
            open_section("(preamble)")
        if out:
            out[-1][1].append(line)
    return [(k, "\n".join(v)) for k, v in out]


def hash_file(path: str) -> dict:
    """{sha, lines, sections:{key: sha}} for one policy file; {} if unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    return {"sha": digest(text),
            "lines": len(text.splitlines()),
            "sections": {k: digest(v) for k, v in split_sections(text)}}


def policy_pair_divergence(root: str, rel: str) -> str | None:
    """The CLAUDE.md/AGENTS.md pair — one file, two names.

    A policy file that is readable by more than one agent CLI is usually kept as
    one file plus a sibling name symlinked to it; which of the two is the real
    file and which is the link is up to the repo, and this does not care. The
    pair is meant to be ONE file, so policy_paths only globs CLAUDE.md and the
    AGENTS.md name is never hashed as its own policy file. This guards the other
    side of that promise: return a drift note when the sibling has stopped being
    a faithful alias — a symlink replaced by a real file whose bytes DIVERGE, or
    a symlink repointed away. None when the pair is intact (a symlink resolving
    to the same file, an identical copy, or no AGENTS.md present at all)."""
    if os.path.basename(rel) != "CLAUDE.md":
        return None
    sib_rel = os.path.join(os.path.dirname(rel), "AGENTS.md")
    sib = os.path.join(root, sib_rel)
    canon = os.path.join(root, rel)
    if os.path.islink(sib):
        # A symlink is the intended shape; drift only if it no longer resolves
        # to this CLAUDE.md (broken link or repointed elsewhere).
        if os.path.realpath(sib) != os.path.realpath(canon):
            return (f"{sib_rel}: symlink no longer resolves to {rel} "
                    f"(-> {os.readlink(sib)})")
        return None
    if not os.path.isfile(sib):
        return None                      # no AGENTS.md alongside — nothing to pair
    # The symlink has been replaced by a real file: diverged iff bytes differ.
    try:
        with open(sib, encoding="utf-8", errors="replace") as f:
            a = f.read()
        with open(canon, encoding="utf-8", errors="replace") as f:
            c = f.read()
    except OSError:
        return None
    if a != c:
        return (f"{sib_rel}: symlink replaced by a real file that has diverged "
                f"from {rel}")
    return None


def snapshot_path() -> str:
    return os.path.join(state_dir(), SNAPSHOT_NAME)


def load_snapshot() -> dict:
    try:
        with open(snapshot_path()) as f:
            snap = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(snap, dict) or snap.get("version") != SNAPSHOT_VERSION:
        return {}  # older/foreign schema: treat as cold, rewrite on save
    return snap


def save_snapshot(snap: dict) -> None:
    tmp = snapshot_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=1, sort_keys=True)
    os.replace(tmp, snapshot_path())


def parse_interval(v) -> int | None:
    """'20m' -> 1200. None for on-demand/unparseable (no cadence to check)."""
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r"(\d+)([smh])", v.strip())
    if not m:
        return None
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def hub_identity(registry: dict) -> tuple[str, str]:
    """(hub session title, agent-deck profile) — both from loops.toml [hub].

    The same resolution bin/ops and bin/dashboard use, so all three address one
    installation's sessions the same way: title is "<persona> (hub)" unless
    session_title overrides it, and both fall back to the neutral "ops" rather
    than to any operator's name. Nothing here is hardcoded on purpose — a baked
    -in title would silently report a stranger's live hub as MISSING."""
    hub = registry.get("hub") or {}
    persona = (hub.get("persona") or "").strip() or "ops"
    title = (hub.get("session_title") or "").strip() or f"{persona} (hub)"
    profile = (os.environ.get("MECHANIC_DECK_PROFILE")
               or (hub.get("deck_profile") or "").strip() or "ops")
    return title, profile


def deck_sessions(profile: str = "ops") -> tuple[dict, str]:
    """ONE agent-deck read for the whole pass, keyed by title. Replaces a
    per-session `show` sweep — same fields, one process. ({}, note) on failure."""
    rc, out = _run(["agent-deck", "-p", profile, "ls", "--json"])
    if rc != 0:
        return {}, f"agent-deck ls unavailable ({out.splitlines()[0] if out else rc})"
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {}, "agent-deck ls returned unparseable JSON"
    sessions = {}
    for s in data if isinstance(data, list) else []:
        if isinstance(s, dict) and s.get("title") and not s.get("archived"):
            sessions.setdefault(s["title"], s)
    return sessions, ""


def read_since(path: str, offset: int) -> tuple[list[str], int, bool]:
    """Lines appended past `offset`, the new offset, and whether the cursor was
    usable. A file that shrank below the cursor was rotated/truncated: report
    unusable so the caller falls back to a time window."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0, True
    if offset < 0 or offset > size:
        return [], size, False
    with open(path, errors="replace") as f:
        f.seek(offset)
        data = f.read()
    return data.splitlines(), size, True


def render_ledger_entry(e: dict) -> str:
    """Verbatim entry, except an over-long 'detail' is capped — the mechanic
    quotes summaries as evidence, and details are what make the ledger huge."""
    d = e.get("detail")
    if isinstance(d, str) and len(d) > LEDGER_DETAIL_CAP:
        e = dict(e, detail=d[:LEDGER_DETAIL_CAP] + f"…(+{len(d) - LEDGER_DETAIL_CAP} chars)")
    return json.dumps(e)


def emit_section(title: str, body: str, out) -> None:
    lines = body.splitlines()
    if len(lines) > MAX_SECTION_LINES:
        lines = lines[:MAX_SECTION_LINES] + [
            f"    …(+{len(body.splitlines()) - MAX_SECTION_LINES} more lines — "
            "read the file for the rest)"]
    for line in lines:
        out.append("  " + line if line else "")


def policy_digest(root: str, prev_files: dict, full: bool) -> tuple[list[str], dict]:
    """Print-lines + the new file-hash map. Unchanged files collapse to one
    line; changed files print only their changed/new sections."""
    out: list[str] = []
    files: dict = {}
    unchanged: list[str] = []
    divergences: list[str] = []
    for rel in policy_paths(root):
        info = hash_file(os.path.join(root, rel))
        if not info:
            continue
        files[rel] = info
        div = policy_pair_divergence(root, rel)
        if div:
            divergences.append(div)
        old = {} if full else prev_files.get(rel) or {}
        if old.get("sha") == info["sha"]:
            unchanged.append(rel)
            continue

        old_sections = old.get("sections") or {}
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            sections = split_sections(f.read())
        changed = [(k, v) for k, v in sections
                   if old_sections.get(k) != digest(v)]
        dropped = [k for k in old_sections if k not in dict(sections)]

        if not old:
            label = "NEW (no baseline)" if not full else "full read"
            out.append(f"\n--- {rel} — {label}, {info['lines']} lines, "
                       f"{len(sections)} sections ---")
        else:
            out.append(f"\n--- {rel} — CHANGED ({len(changed)} of "
                       f"{len(sections)} sections), {info['lines']} lines ---")
        if dropped:
            out.append(f"  (removed sections: {', '.join(dropped)})")
        for k, v in changed[:MAX_CHANGED_SECTIONS]:
            emit_section(k, v, out)
        if len(changed) > MAX_CHANGED_SECTIONS:
            out.append(f"  …(+{len(changed) - MAX_CHANGED_SECTIONS} more changed "
                       "sections — read the file)")
    if divergences:
        out.append(f"\nPOLICY PAIR DRIFT ({len(divergences)}) — CLAUDE.md/"
                   "AGENTS.md pairs that are no longer one file:")
        for d in divergences:
            out.append(f"  ! {d}")
    if unchanged:
        out.insert(0, f"unchanged since baseline ({len(unchanged)}) — do NOT "
                      f"re-read: {', '.join(unchanged)}")
    elif not out:
        out.append("(no policy files found)")
    return out, files


# --------------------------------------------------------------------------- #
# the EXTRACTION lens — private fork -> public core candidate detection
# --------------------------------------------------------------------------- #
# OPTIONAL, and off unless loops.toml has an [extraction] table. It applies to
# one shape of estate: a private fork that feeds a public core built from an
# allowlist ("mechanism is public; identity, policy, and data are not"). The
# allowlist IS the manifest, and it lives in the fork whose paths it governs,
# so there is deliberately no second copy in the core to drift out of sync with
# it. This section resolves every allowlisted path into exactly one of three
# buckets each pass:
#
#   (a) new and matching, never extracted  — a candidate nobody has filed yet
#   (b) already extracted, changed since   — DRIFT, the ongoing-sync case
#   (c) explicitly excluded (private-only) — named, so it does not silently
#       vanish from consideration by simply being absent from the output
#
# Detection only. The mechanic proposes; it never files, edits, or extracts —
# same narrow apply lane as every other lens. The durable candidate record
# lives in state/extraction/candidates.json, which this only ever READS;
# whatever writes it is the operator's own tooling, never this loop.
EXTRACTION_STORE = os.path.join("state", "extraction", "candidates.json")
EXTRACTION_OPEN = ("candidate", "approved", "extracting", "blocked")
BACKTICK_RE = re.compile(r"`([^`]+)`")
PATHLIKE_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./*-]*$")
EXTRACTION_WALK_CAP = 300    # files per include pattern, before eliding
EXTRACTION_LIST_CAP = 40     # paths printed per bucket
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache"}


def file_sha(path: str) -> str | None:
    """12-char sha256 of a file's bytes. Same truncation as digest(). Whatever
    records `synced_hashes` in the candidate store must truncate identically,
    or bucket (b) would report drift on every path forever."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None


def _pathlike(tok: str) -> bool:
    tok = tok.strip()
    return bool(PATHLIKE_RE.match(tok)) and ("/" in tok or "." in tok)


def parse_allowlist(text: str) -> tuple[list[str], list[str]]:
    """(include, exclude) path patterns read out of the extraction allowlist.

    Include = the backticked paths in the first column of the "What is in, and
    why" table. Exclude = the same, from "The invariant" (never-committed) and
    "What is deliberately out". A token in BOTH is an include: the in-table is
    an explicit decision, the prose mention is usually a cross-reference
    (`loops/example/` is named in both, and it is core)."""
    include: list[str] = []
    exclude: list[str] = []
    for title, body in split_sections(text):
        t = title.lower()
        if "what is in" in t:
            bucket = include
        elif "invariant" in t or "deliberately out" in t:
            bucket = exclude
        else:
            continue
        for line in body.splitlines():
            cells = line.split("|")
            # In a table row, only the first cell says WHICH path; the second
            # says where it lives instead and would poison the other bucket.
            scan = cells[1] if line.lstrip().startswith("|") and len(cells) > 2 else line
            if set(scan.strip()) <= set("-: ") and scan.strip():
                continue  # table separator
            bucket += [tok for tok in BACKTICK_RE.findall(scan) if _pathlike(tok)]
    include = sorted(dict.fromkeys(include))
    exclude = sorted(t for t in dict.fromkeys(exclude) if t not in include)
    return include, exclude


def excluded_by(rel: str, exclude: list[str]) -> str | None:
    for pat in exclude:
        if rel == pat or rel == pat.rstrip("/"):
            return pat
        if pat.endswith("/") and rel.startswith(pat):
            return pat
    return None


def _walk(root: str, rel_dir: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, rel_dir)):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(".pyc"):
                continue
            out.append(os.path.relpath(os.path.join(dirpath, fn), root))
            if len(out) >= EXTRACTION_WALK_CAP:
                return out
    return out


def resolve_allowlist(root: str,
                      include: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(present, absent, aliases): allowlisted patterns resolved against THIS repo.

    A pattern that names a directory expands to its files. A pattern with no
    counterpart here is `absent` — public-core-only shape (loops/example/,
    loops.example.toml), reported rather than dropped. The allowlist's
    "tests next to their scripts" row has no path to grep for, so it is applied
    as a derived rule: an included file pulls in its sibling test_<name>.py.

    `aliases` are symlinks whose target is itself in the set — the CLAUDE.md /
    AGENTS.md pairs, in whichever direction this repo links them. One file, two
    names: counting both would double every candidate and report phantom drift
    when only one name moves."""
    present: list[str] = []
    absent: list[str] = []
    for pat in include:
        p = os.path.join(root, pat.rstrip("/"))
        if os.path.isdir(p):
            hits = _walk(root, pat.rstrip("/"))
            present += hits
            if not hits:
                absent.append(pat)
        elif os.path.isfile(p):
            rel = os.path.relpath(p, root)
            present.append(rel)
            sib = os.path.join(os.path.dirname(rel),
                               "test_" + os.path.basename(rel) + ".py")
            sib_plain = os.path.join(os.path.dirname(rel),
                                     "test_" + os.path.basename(rel))
            for cand in (sib, sib_plain):
                if os.path.isfile(os.path.join(root, cand)):
                    present.append(cand)
        else:
            absent.append(pat)
    present = sorted(dict.fromkeys(present))
    # Real files only — so an alias is dropped when its TARGET is genuinely
    # here, never merely because it resolves to itself.
    real = {os.path.realpath(os.path.join(root, r)) for r in present
            if not os.path.islink(os.path.join(root, r))}
    aliases = [r for r in present
               if os.path.islink(os.path.join(root, r))
               and os.path.realpath(os.path.join(root, r)) in real]
    dropped = set(aliases)
    return ([r for r in present if r not in dropped],
            sorted(dict.fromkeys(absent)), aliases)


def load_extraction_store(root: str) -> dict:
    """The durable candidate store, READ ONLY. {} when it does not exist yet."""
    try:
        with open(os.path.join(root, EXTRACTION_STORE)) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return (data.get("items") or {}) if isinstance(data, dict) else {}


def extraction_digest(root: str, registry: dict, prev: dict,
                      full: bool) -> tuple[list[str], dict]:
    """Print-lines + the snapshot slice for the EXTRACTION lens."""
    cfg = registry.get("extraction") or {}
    repo = os.path.expanduser((cfg.get("repo") or "").strip())
    if not repo:
        return (["not configured — this estate is nobody's private fork, or "
                 "add [extraction] repo/allowlist to loops.toml to enable "
                 "this lens"], {})
    # The allowlist lives in THIS repo — it governs which of these paths may be
    # extracted — so it resolves against `root`, not against the core checkout.
    allow_rel = (cfg.get("allowlist") or "docs/extraction-allowlist.md").strip()
    allow_path = os.path.join(root, allow_rel)
    try:
        with open(allow_path, encoding="utf-8", errors="replace") as f:
            allow_text = f.read()
    except OSError:
        return ([f"allowlist unreadable at {allow_path} — missing or moved; "
                 "lens skipped this pass (not a finding about the estate)"], {})

    allow_sha = digest(allow_text)
    include, exclude = parse_allowlist(allow_text)
    present, absent, aliases = resolve_allowlist(root, include)
    store = load_extraction_store(root)

    # Every path any candidate claims, and the sync baseline for synced ones.
    claimed: dict = {}
    baseline: dict = {}
    for item in store.values():
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        for rel in item.get("source_paths") or []:
            if status in EXTRACTION_OPEN:
                claimed.setdefault(rel, item)
        for rel, sha in (item.get("synced_hashes") or {}).items():
            if status in ("synced", "extracting"):
                baseline[rel] = (item, sha)

    have_base = bool(prev) and not full
    prev_paths = (prev.get("paths") or {}) if have_base else {}
    prev_allow = prev.get("allowlist_sha") if have_base else None
    new_paths: dict = {}
    bucket_a: list[str] = []   # new & matching, never extracted
    bucket_b: list[str] = []   # already extracted, changed since (drift)
    bucket_c: list[str] = []   # explicitly excluded (private-only)
    steady_a: list[str] = []   # bucket (a), but untouched since the baseline
    in_sync = 0
    for rel in present:
        pat = excluded_by(rel, exclude)
        if pat:
            bucket_c.append(f"{rel}  (excluded by `{pat}`)")
            continue
        sha = file_sha(os.path.join(root, rel))
        if sha is None:
            continue
        new_paths[rel] = sha
        moved = ""
        if have_base:
            moved = ("  [new since baseline]" if rel not in prev_paths
                     else ("  [CHANGED since baseline]"
                           if prev_paths[rel] != sha else ""))
        if rel in baseline:
            item, was = baseline[rel]
            if was != sha:
                bucket_b.append(
                    f"{rel}  {item.get('id', '?')} synced "
                    f"{(item.get('synced_at') or '?')[:19]} · sha {was} → {sha}")
            else:
                in_sync += 1
        elif rel in claimed:
            item = claimed[rel]
            bucket_a.append(f"{rel}  (already filed as {item.get('id', '?')} "
                            f"[{item.get('status')}]){moved}")
        elif moved:
            bucket_a.append(f"{rel}  sha {sha}{moved}")
        elif have_base:
            # Allowlisted, unextracted, and untouched since the last pass —
            # the same judgment as last night, so it is named on one line
            # instead of re-argued. Not dropped: absent would read as handled.
            steady_a.append(rel)
        else:
            bucket_a.append(f"{rel}  sha {sha}")

    # Excluded patterns that exist here but never surfaced above (state/,
    # loops.toml, ...) — named explicitly so "not mentioned" never reads as
    # "already handled".
    for pat in exclude:
        p = os.path.join(root, pat.rstrip("/"))
        if os.path.exists(p) and not any(l.startswith(pat) for l in bucket_c):
            kind = "dir" if os.path.isdir(p) else "file"
            bucket_c.append(f"{pat}  ({kind}, never committed to the core)")

    if not have_base or prev_allow is None:
        allow_mark = ""
    elif prev_allow != allow_sha:
        allow_mark = "  [CHANGED since baseline — re-read the allowlist]"
    else:
        allow_mark = " (unchanged)"
    out = [f"allowlist: {allow_path}",
           f"  sha {allow_sha}{allow_mark}",
           f"patterns: {len(include)} include · {len(exclude)} exclude · "
           f"{len(present)} paths resolved here"]

    def emit(label: str, rows: list[str]) -> None:
        out.append(f"\n{label} — {len(rows)}")
        if not rows:
            out.append("  (none)")
            return
        for r in rows[:EXTRACTION_LIST_CAP]:
            out.append(f"  {r}")
        if len(rows) > EXTRACTION_LIST_CAP:
            out.append(f"  …(+{len(rows) - EXTRACTION_LIST_CAP} more)")

    emit("(a) new & matching, never extracted", sorted(bucket_a))
    if steady_a:
        out.append(f"  unchanged since baseline, still unextracted "
                   f"({len(steady_a)}) — same call as last pass: "
                   + ", ".join(sorted(steady_a)))
    emit("(b) already extracted, CHANGED since (drift)", sorted(bucket_b))
    emit("(c) explicitly excluded (private-only)", sorted(bucket_c))
    if in_sync:
        out.append(f"\nin sync (extracted, unchanged): {in_sync}")
    if aliases:
        out.append(f"symlink aliases folded into their targets: "
                   + ", ".join(sorted(aliases)))
    if absent:
        out.append("public-core shape only, no counterpart here: "
                   + ", ".join(absent))

    open_items = [i for i in store.values()
                  if isinstance(i, dict) and i.get("status") in EXTRACTION_OPEN]
    out.append(f"\nopen candidates carried forward — {len(open_items)}")
    if not open_items:
        out.append(f"  (none open in {EXTRACTION_STORE})")
    for item in sorted(open_items, key=lambda i: i.get("created_at") or ""):
        out.append(f"  {item.get('id', '?')} [{item.get('status')}] "
                   f"{str(item.get('title', ''))[:70]}")

    return out, {"allowlist_sha": allow_sha, "paths": new_paths}


def git_digest(root: str, prev_head: str, full: bool) -> tuple[list[str], str]:
    rc, head = _run(["git", "-C", root, "rev-parse", "HEAD"])
    head = head.strip() if rc == 0 else ""
    if prev_head and not full and head:
        anc, _ = _run(["git", "-C", root, "merge-base", "--is-ancestor",
                       prev_head, head])
        if anc == 0:
            rc, out = _run(["git", "-C", root, "log", "--oneline",
                            f"{prev_head}..HEAD"])
            body = out.splitlines() if rc == 0 else [f"git log failed: {out}"]
            label = f"since baseline {prev_head[:7]}"
            return ([f"== git ({label}, {len(body)} commits) =="] +
                    (body or ["(none)"])), head
    rc, out = _run(["git", "-C", root, "log", "--oneline", "-20"])
    body = out.splitlines() if rc == 0 else [f"git log failed: {out}"]
    return ["== git (no usable baseline — last 20) =="] + body, head


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #
def cmd_windows(cfg: dict) -> int:
    now = local_now()
    phase, night, note = derive_phase(now, cfg, load_history())
    print(f"local {now.strftime('%H:%M')} · phase={phase} · "
          f"night={night} · {note}")
    return 0


def cmd_record(event_json: str, cfg: dict) -> int:
    try:
        e = json.loads(event_json)
    except json.JSONDecodeError as err:
        raise SystemExit(f"record: not valid JSON: {err}")
    if not isinstance(e, dict) or not e.get("event"):
        raise SystemExit('record: payload must be an object with an "event" field')
    now = local_now()
    start = (cfg.get("pass_start") or "").strip()
    end = (cfg.get("pass_end") or "").strip()
    e.setdefault("ts", now.astimezone(dt.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"))
    e.setdefault("night", night_id(now, start, end))
    with open(os.path.join(state_dir(), "history.jsonl"), "a") as f:
        f.write(json.dumps(e) + "\n")
    print(f"recorded {e['event']} (night {e['night']})")
    return 0


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)


def _heartbeat_age(path: str, now_utc: dt.datetime) -> str:
    try:
        with open(path) as f:
            age = int(now_utc.timestamp()) - int(f.read().strip())
        return f"{age}s ago"
    except (OSError, ValueError):
        return "—"


def cmd_gather(cfg: dict, full: bool = False, save: bool = True) -> int:
    root = repo_root()
    now = local_now()
    now_utc = now.astimezone(dt.timezone.utc)
    phase, night, _ = derive_phase(now, cfg, load_history())
    stored = load_snapshot()
    # Two slots: `base` is what tonight compares against, `current` is what
    # tonight captured. A second gather on the SAME night (a resume tick) keeps
    # the same base, so it replays the interrupted pass's digest instead of
    # diffing tonight against itself.
    if stored.get("night") == night:
        base = stored.get("base") or {}
        base_night, base_at = stored.get("base_night"), stored.get("base_at")
    else:
        base = stored.get("current") or {}
        base_night, base_at = stored.get("night"), stored.get("generated_at")
    # --full prints as if cold but does NOT discard the baseline on save.
    prev = {} if full else base
    print(f"== now ==\nlocal {now.strftime('%Y-%m-%d %H:%M %Z')} · "
          f"night {night} · phase {phase}")
    if full:
        print("baseline: ignored (--full) — full digest")
    elif prev:
        print(f"baseline: night {base_night or '?'} ({base_at or '?'}) — "
              "showing only what changed since")
    else:
        print("baseline: none (cold run) — full digest, snapshot written for "
              "next time")

    registry: dict = {}
    if tomllib:
        with open(os.path.join(root, "loops.toml"), "rb") as f:
            registry = tomllib.load(f)
    loops = registry.get("loops", {})
    prev_reg = prev.get("registry") or {}
    print("\n== registry (loops.toml) ==")
    reg_now = {}
    for name, lc in loops.items():
        line = (f"interval={lc.get('interval')} autostart={lc.get('autostart')} "
                f"persona={lc.get('persona')!r} model={lc.get('model')}")
        reg_now[name] = line
        mark = ""
        if prev_reg and prev_reg.get(name) != line:
            mark = "   [CHANGED since baseline: " + (
                prev_reg.get(name, "not in registry") + "]")
        print(f"{name}: {line}{mark}")
    for name in prev_reg:
        if name not in reg_now:
            print(f"{name}: [REMOVED from registry since baseline]")

    # One deck read for every session (instead of one `session show` per loop).
    hub_title, deck_profile = hub_identity(registry)
    sessions, deck_note = deck_sessions(deck_profile)
    print("\n== sessions (agent-deck ls --json, one call) ==")
    if deck_note:
        print(deck_note)
    titles = {hub_title: "hub"}
    for name, lc in loops.items():
        if lc.get("persona"):
            titles[f"{lc['persona']} ({name})"] = name
    # An autostart=false (on-demand) loop legitimately has no live session, so
    # its absence is "expected absent", not drift — matching bin/ops health,
    # which only sweeps autostart loops. The hub is always expected. An
    # autostart=true loop still missing its session is flagged as before.
    expected_live = {t for t, n in titles.items()
                     if n == "hub" or (loops.get(n) or {}).get("autostart")}
    drift = []
    for title, name in titles.items():
        s = sessions.get(title)
        if not s:
            if not deck_note:
                if title in expected_live:
                    print(f"{title}: MISSING from agent-deck")
                    drift.append(f"{name}: no agent-deck session titled {title!r}")
                else:
                    print(f"{title}: expected absent (autostart=false)")
            continue
        live_model = s.get("model_id") or s.get("model") or "?"
        status = s.get("status") or "?"
        print(f"{title}: status={status} model={live_model}")
        want = (loops.get(name) or {}).get("model")
        if want and live_model != want:
            drift.append(f"{name}: loops.toml model={want} but live "
                         f"model={live_model}")
        if status not in ("waiting", "running"):
            drift.append(f"{name}: session status={status}")
    extra = [t for t in sessions if t not in titles and not t.startswith("ephemeral")]
    if extra:
        print(f"other sessions: {', '.join(sorted(extra))}")

    print("\n== heartbeats ==")
    for name in ["hub", *loops]:
        hb = os.path.join(root, "state", name, "last_tick")
        print(f"{name}: {_heartbeat_age(hb, now_utc)}")
        secs = parse_interval((loops.get(name) or {}).get("interval"))
        if secs:
            try:
                with open(hb) as f:
                    age = int(now_utc.timestamp()) - int(f.read().strip())
            except (OSError, ValueError):
                drift.append(f"{name}: no readable heartbeat")
                continue
            if age > secs * 2 + 120:
                drift.append(f"{name}: heartbeat {age}s old > "
                             f"{secs * 2 + 120}s (2*{loops[name]['interval']}+120)")
    print("\n== drift (registry vs live) ==")
    print("\n".join(drift) if drift else "none")

    print("\n== state files (size · delta since baseline) ==")
    prev_sizes = prev.get("state_sizes") or {}
    sizes_now: dict = {}
    state_root = os.path.join(root, "state")
    unchanged_state = 0
    # "" = the loose files directly under state/ (the central ledger lives
    # there, and its growth is a signal in its own right).
    groups = [""] + [n for n in sorted(os.listdir(state_root))
                     if os.path.isdir(os.path.join(state_root, n))
                     and n != "secrets"]
    for name in groups:
        d = os.path.join(state_root, name)
        shown = []
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            key = f"{name}/{fn}" if name else fn
            size = os.path.getsize(p)
            sizes_now[key] = size
            old = prev_sizes.get(key)
            if old == size and prev_sizes:
                unchanged_state += 1
                continue
            delta = "new" if old is None and prev_sizes else (
                "" if old is None else f"{(size - old) / 1024:+.1f}K")
            shown.append(f"{fn} {size / 1024:.1f}K"
                         + (f" ({delta})" if delta else ""))
        if shown:
            print(f"state/{name + '/' if name else ''}: {', '.join(shown)}")
    gone = [k for k in prev_sizes if k not in sizes_now]
    if gone:
        print(f"removed since baseline: {', '.join(sorted(gone))}")
    if unchanged_state:
        print(f"({unchanged_state} state files unchanged since baseline)")

    ledger_path = os.path.join(root, "state", "ledger.jsonl")
    cursor = (prev.get("ledger") or {}).get("offset")
    lines: list[str] = []
    label = ""
    ledger_offset = 0
    if cursor is not None and not full:
        lines, ledger_offset, ok = read_since(ledger_path, cursor)
        if ok:
            label = f"{len(lines)} new since baseline"
        else:
            cursor = None  # rotated/truncated — fall back to the time window
    if cursor is None or full:
        try:
            with open(ledger_path, errors="replace") as f:
                lines = f.read().splitlines()
            ledger_offset = os.path.getsize(ledger_path)
        except OSError:
            lines, ledger_offset = [], 0
        keep = recent_entries(lines, now_utc, hours=24)
        lines = [json.dumps(e) for e in keep]
        label = f"last 24h, {len(lines)} lines"

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    print(f"\n== central ledger ({label}) ==")
    tally: dict = {}
    for e in entries:
        actor, kind = e.get("actor", "?"), e.get("kind", "?")
        tally.setdefault(actor, {}).setdefault(kind, 0)
        tally[actor][kind] += 1
        print(render_ledger_entry(e))
    if not entries:
        print("(none)")
    print("\n== actor tally ==")
    for actor in sorted(tally):
        counts = " ".join(f"{k}={n}" for k, n in sorted(tally[actor].items()))
        print(f"{actor}: {counts}")
    if not tally:
        print("(none)")

    print("\n== policy files (hash-keyed) ==")
    pol_lines, files = policy_digest(root, prev.get("files") or {}, full)
    print("\n".join(pol_lines))

    print("\n== extraction (public-core allowlist, three buckets) ==")
    ext_lines, extraction = extraction_digest(
        root, registry, prev.get("extraction") or {}, full)
    print("\n".join(ext_lines))

    print()
    git_lines, head = git_digest(root, prev.get("git_head") or "", full)
    print("\n".join(git_lines))

    if save:
        save_snapshot({
            "version": SNAPSHOT_VERSION,
            "night": night,
            "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "base_night": base_night,
            "base_at": base_at,
            "base": base,
            "current": {
                "files": files,
                "registry": reg_now,
                "state_sizes": sizes_now,
                "ledger": {"offset": ledger_offset},
                "git_head": head,
                "extraction": extraction,
            },
        })
    return 0


def cmd_report() -> int:
    path = os.path.join(state_dir(), "REPORT.md")
    if not os.path.exists(path):
        print("no report yet — the mechanic has not completed a pass")
        return 0
    with open(path) as f:
        sys.stdout.write(f.read())
    return 0


def main(argv=None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="mechanic engine")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("windows")
    g = sub.add_parser("gather")
    g.add_argument("--full", action="store_true",
                   help="ignore the baseline snapshot; print the full digest")
    g.add_argument("--no-save", dest="save", action="store_false",
                   help="do not write digest.json (ad hoc inspection)")
    p = sub.add_parser("record")
    p.add_argument("event_json")
    sub.add_parser("report")
    args = ap.parse_args(argv)

    if args.cmd == "windows":
        return cmd_windows(cfg)
    if args.cmd == "gather":
        return cmd_gather(cfg, full=args.full, save=args.save)
    if args.cmd == "record":
        return cmd_record(args.event_json, cfg)
    if args.cmd == "report":
        return cmd_report()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

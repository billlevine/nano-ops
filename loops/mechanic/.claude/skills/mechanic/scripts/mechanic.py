#!/usr/bin/env python3
"""mechanic engine — deterministic state for the nightly self-optimization
pass over the estate.

DIVISION OF LABOR
-----------------
This script owns bookkeeping only: the clock/window state, the pass history,
and a deterministic digest of the estate's inputs. It NEVER analyzes, edits,
or proposes — the nightly pass lives in the SKILL.md prose, which calls this
script for state I/O. Same split as track.py/spotter and
review.py/night-shift.

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
  mechanic.py subsystems       the nine subsystems every pass must examine
  mechanic.py checklist        tonight's nine-subsystem coverage (exit 1 if
                               any row is still missing)
  mechanic.py proposals        live proposal tasks by review stage (read-only)
  mechanic.py propose '<json>' file one proposal durably: an estate task with
                               an equivalence fingerprint, plus the matching
                               history.jsonl finding line
  mechanic.py report           print REPORT.md

THE PROPOSAL RECORD (the consistency contract)
------------------------------------
A proposal used to exist only as text in REPORT.md, which this pass OVERWRITES
every night. Nothing recorded whether the operator had seen one, agreed to one, or
turned one down, so a recurring condition arrived every night looking brand
new and a rejected proposal was indistinguishable from an untouched one.

`propose` fixes that at the source: every proposal becomes a task in the
estate's shared store (`bin/estate proposal add`) carrying a stable
fingerprint of its condition, and the returned task id is written into the
SAME history.jsonl finding line. The two records agree by construction rather
than by anyone remembering to keep them in step.

This is not the mechanic implementing anything. A proposal task starts at
stage `pending-review` and only the operator's decision moves it to
`approved-backlog`, which is the only stage the night shift may claim. Filing
one is the mechanic writing down what it found, exactly as REPORT.md always
was — just somewhere that survives tomorrow's pass.

THE COMPLETE PASS RECORD (the consistency contract)
-----------------------------------------
a prior finding made a proposal durable. a prior finding makes the pass that produced it CHECKABLE,
in three places the engine now enforces rather than the prose asking for:

  * Nine subsystems, every pass. Each of the operator's nine named subsystems gets a
    `subsystem_check` line with a concrete result — `ok`, `finding`, or
    `unobservable` — and `pass_done` is REFUSED while any row is missing. A
    pass that never looked at the dashboard can no longer end with a summary
    that reads as if it had.
  * Every finding carries a durable id (`f-<night>-NN`) and names its
    subsystem. A proposal additionally carries its task id from `propose`, so
    the finding, the task, and the ledger event are one thread.
  * `no-proposal` requires a reason, in a field, not in the prose. "I looked
    and decided not to propose anything" is a result; the same sentence with
    no reason is indistinguishable from never having looked.

Every finding outcome is also emitted to the estate's events table, linked to
the finding id (and, when there is one, to the proposal task). The pass
history stays this loop's authoritative archive; the ledger event is what
makes the outcome visible from outside the loop.

Older history has no `subsystem_check` lines and no finding ids, and nothing
migrates it. Those passes stay exactly as readable as they were — the rules
here apply to what is written from now on.

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
  checklist      tonight's nine-subsystem coverage. Cold prints the standing
                 shape; warm prints the counts, and collapses to one line when
                 tonight's rows repeat what the baseline gather saw.
  memory         the facts this pass may know: `bin/estate memory recall
                 --scope mechanic` (a prior finding), active rows at scope mechanic plus
                 shared, each labelled with the scope it is stored at. A fact
                 printed in full at the baseline collapses to its labelled id;
                 the label never collapses.
  proposals      every proposal on file with its review stage. A proposal that
                 has not moved since the baseline collapses to its id in a
                 named count; a new or moved one prints in full.
  ledger         only entries appended since the snapshot's byte cursor (last
                 24h on a cold run or if the file was rotated/truncated). Each
                 entry's "detail" is capped; ts/actor/kind/summary/refs are
                 verbatim, and the raw file is still there to grep.
  extraction     every path on docs/extraction-allowlist.md (the manifest; it
                 lives here, and there is no second copy), resolved into
                 exactly three buckets: (a) new & matching, never extracted,
                 (b) already extracted and CHANGED since — drift, the ongoing
                 sync case, (c) explicitly excluded, named rather than absent.
                 Plus every unresolved candidate in state/extraction/, carried
                 forward until it reaches a resting state. Read-only: the
                 mechanic proposes candidates, bin/extractions records them.
                 Configured by loops.toml's [extraction] table; the lens
                 no-ops with a note when it is unset or the checkout is gone.
  git            commits since the snapshot's HEAD (last 20 on a cold run).

A cold run — no `digest.json` — prints the full estate, so a first pass (or a
pass after deleting the snapshot) is never short-changed. The snapshot is
rewritten only when its night differs from tonight's, so a `resume` tick
re-gathers the SAME digest the interrupted pass saw.

  gather --full      ignore the baseline, print everything (cold-run output)
  gather --no-save   don't touch digest.json (ad hoc/dry inspection)

$MECHANIC_NOW (ISO datetime) freezes the clock for tests and dry runs.
$MECHANIC_DECK_PROFILE overrides the agent-deck profile (default "ops").
$MECHANIC_REPO_ROOT overrides the upward loops.toml search (tests).
$ESTATE_SCRIPT overrides the path to bin/estate (tests).
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
import textwrap

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
    start > end. Empty start = window off; empty end = start..midnight.
    (Same semantics as night-shift's review.py.)"""
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

    Since the AGENTS.md-canonical migration every CLAUDE.md policy file has an
    AGENTS.md symlink alongside it (Codex-compat). The pair is meant to be ONE
    file, so policy_paths only globs CLAUDE.md and the AGENTS.md name is never
    hashed as its own policy file. This guards the other side of that promise:
    return a drift note when the sibling has stopped being a faithful alias —
    the symlink replaced by a real file whose bytes DIVERGE from CLAUDE.md, or
    a symlink repointed away from it. None when the pair is intact (a symlink
    to CLAUDE.md, an identical copy, or simply no AGENTS.md present)."""
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


def hub_config(registry: dict) -> dict:
    """Resolve installation-specific hub identity from loops.toml."""
    hub = registry.get("hub") or {}
    persona = hub.get("persona") or "ops"
    return {
        "persona": persona,
        "session": hub.get("session_title") or f"{persona} (hub)",
        "deck_profile": (os.environ.get("MECHANIC_DECK_PROFILE")
                         or hub.get("deck_profile") or "ops"),
    }


def deck_sessions(profile: str) -> tuple[dict, str]:
    """ONE agent-deck read for the whole pass, keyed by title. Replaces the old
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
# public core is the public core this estate extracts into, built from an
# allowlist ("mechanism is public; identity, policy, and data are not").
# docs/extraction-allowlist.md IS the manifest — it lives in this repo, whose
# paths it governs, and there is deliberately no second copy in the core to
# drift out of sync. This section resolves every
# allowlisted path into exactly one of three buckets each pass:
#
#   (a) new and matching, never extracted  — a candidate nobody has filed yet
#   (b) already extracted, changed since   — DRIFT, the ongoing-sync case
#   (c) explicitly excluded (private-only) — named, so it does not silently
#       vanish from consideration by simply being absent from the output
#
# Detection only. The mechanic proposes; it never files, edits, or extracts —
# same narrow apply lane as every other lens. The durable record lives in
# state/extraction/ (bin/extractions), which this only ever READS.
EXTRACTION_STORE = os.path.join("state", "extraction", "candidates.json")
EXTRACTION_OPEN = ("candidate", "approved", "extracting", "blocked")
BACKTICK_RE = re.compile(r"`([^`]+)`")
PATHLIKE_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./*-]*$")
EXTRACTION_WALK_CAP = 300    # files per include pattern, before eliding
EXTRACTION_LIST_CAP = 40     # paths printed per bucket
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache"}


def file_sha(path: str) -> str | None:
    """12-char sha256 of a file's bytes. Same truncation as digest(), and the
    same one bin/extractions records at sync time, so the two compare directly."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None


def _pathlike(tok: str) -> bool:
    tok = tok.strip()
    return bool(PATHLIKE_RE.match(tok)) and ("/" in tok or "." in tok)


def parse_allowlist(text: str) -> tuple[list[str], list[str]]:
    """(include, exclude) path patterns read out of docs/extraction-allowlist.md.

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

    `aliases` are symlinks whose target is itself in the set — the Codex-compat
    AGENTS.md -> CLAUDE.md pairs. One file, two names: counting both would
    double every candidate and report phantom drift when only one name moves."""
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
        return (["not configured — add [extraction] repo/allowlist to "
                 "loops.toml to enable this lens"], {})
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
        out.append("  (none — `bin/extractions list`)")
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


def append_history(event: dict, cfg: dict) -> dict:
    """Stamp an event with ts + night and append it. The one writer."""
    now = local_now()
    start = (cfg.get("pass_start") or "").strip()
    end = (cfg.get("pass_end") or "").strip()
    event.setdefault("ts", now.astimezone(dt.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"))
    event.setdefault("night", night_id(now, start, end))
    with open(os.path.join(state_dir(), "history.jsonl"), "a") as f:
        f.write(json.dumps(event) + "\n")
    return event


# --------------------------------------------------------------------------- #
# the nine-subsystem checklist and the finding record (the consistency contract)
# --------------------------------------------------------------------------- #
# the operator's nine named subsystems. This is the audit's own list and the same nine
# `bin/hub-intake` routes to — a SECOND COPY on purpose, because this engine is
# a loop script that runs against a fabricated repo root in tests and must not
# import from the repo's bin/ or lib/. bin/test_mechanic_skill.py reads both
# files and fails if they ever disagree, so the drift is caught rather than
# trusted. Adding a tenth subsystem is a deliberate edit in both places.
SUBSYSTEMS = ("hub", "mechanic", "night-shift", "spotter", "briefer",
              "tasks", "memory", "dashboard", "ledger")
CHECK_EVENT = "subsystem_check"
# What a subsystem row may say. `unobservable` is the honest third answer and
# the reason no --force exists: a subsystem the pass genuinely could not
# examine is recorded as such, with why, rather than skipped.
RESULTS = ("ok", "finding", "unobservable")
FINDING_ACTIONS = ("proposed", "observed", "no-proposal")


def tonight(cfg: dict) -> tuple[str, list[dict]]:
    """(night id, tonight's history events)."""
    night = night_id(local_now(), (cfg.get("pass_start") or "").strip(),
                     (cfg.get("pass_end") or "").strip())
    return night, [e for e in load_history() if e.get("night") == night]


def checklist(events: list[dict]) -> dict[str, dict]:
    """subsystem -> its LAST check row tonight. A later row supersedes an
    earlier one: a subsystem first recorded `unobservable` and then examined
    for real should read as examined."""
    rows: dict[str, dict] = {}
    for e in events:
        if e.get("event") == CHECK_EVENT and e.get("subsystem") in SUBSYSTEMS:
            rows[e["subsystem"]] = e
    return rows


def missing_subsystems(events: list[dict]) -> list[str]:
    rows = checklist(events)
    return [s for s in SUBSYSTEMS if s not in rows]


def next_finding_id(night: str, events: list[dict]) -> str:
    """`f-<night>-NN`, counting tonight's findings. Durable because
    history.jsonl is append-only and a night id never repeats."""
    n = sum(1 for e in events if e.get("event") == "finding") + 1
    return f"f-{night}-{n:02d}"


def _nonempty(payload: dict, field: str) -> str:
    value = payload.get(field)
    return str(value).strip() if value is not None else ""


def validate_event(e: dict, night: str, events: list[dict]) -> str | None:
    """The error message this event should be refused with, or None.

    Refusing is the whole mechanism. A finding with no subsystem, a
    `no-proposal` with no reason, and a `pass_done` over an incomplete
    checklist are all things the prose already asked for and nothing checked.
    """
    kind = e.get("event")
    if kind == CHECK_EVENT:
        if e.get("subsystem") not in SUBSYSTEMS:
            return (f"subsystem must be one of: {', '.join(SUBSYSTEMS)} "
                    f"(got {e.get('subsystem')!r})")
        if e.get("result") not in RESULTS:
            return (f"result must be one of: {', '.join(RESULTS)} "
                    f"(got {e.get('result')!r})")
        if not _nonempty(e, "note"):
            return ("note is required — a bare result does not show the "
                    "subsystem was examined")
        return None
    if kind == "finding":
        if e.get("action") not in FINDING_ACTIONS:
            return (f"action must be one of: {', '.join(FINDING_ACTIONS)} "
                    f"(got {e.get('action')!r})")
        if e.get("action") == "proposed":
            return ("a proposed finding is filed with `mechanic.py propose`, "
                    "which writes the durable proposal task and this line "
                    "together (a prior finding)")
        if e.get("subsystem") not in SUBSYSTEMS:
            return (f"subsystem must be one of: {', '.join(SUBSYSTEMS)} "
                    f"(got {e.get('subsystem')!r})")
        if not _nonempty(e, "summary"):
            return "summary is required"
        if e.get("action") == "no-proposal" and not _nonempty(e, "reason"):
            return ("no-proposal requires a reason — deciding not to propose "
                    "is a result, and without the why it is indistinguishable "
                    "from never having looked")
        return None
    if kind == "pass_done":
        gaps = missing_subsystems(events)
        if gaps:
            return (f"the pass is not complete: {len(gaps)} of "
                    f"{len(SUBSYSTEMS)} subsystems have no result for night "
                    f"{night} — {', '.join(gaps)}. Record each one with "
                    "`record '{\"event\":\"" + CHECK_EVENT + "\",\"subsystem\""
                    ":\"...\",\"result\":\"ok|finding|unobservable\",\"note\""
                    ":\"...\"}'` first. `unobservable` with a note is a legal "
                    "answer; silence is not")
    return None


def checklist_digest(cfg: dict, prev: dict, full: bool) -> tuple[list[str], dict]:
    """Print-lines + the snapshot slice for the gather's checklist section:
    what tonight still owes.

    On a fresh pass this is the pass's own to-do list, and on a resume it is
    the part of it that survived the interruption — which is exactly what a
    resuming tick needs and used to have to reconstruct by reading history.

    Warm and cold, like every other section. This one is fixed cost on every
    gather and the digest's whole point is printing only what moved, so a cold
    gather prints the standing shape (the pointer at `mechanic.py subsystems`
    included) and a warm one prints the numbers, collapsing to one line when
    tonight's rows repeat what the baseline gather already saw. The count is
    printed either way: a row that collapses is never a row that vanished.
    """
    night, events = tonight(cfg)
    rows = checklist(events)
    gaps = missing_subsystems(events)
    recorded = {s: rows[s].get("result") for s in SUBSYSTEMS if s in rows}
    snap = {"night": night, "recorded": recorded}
    warm = bool(prev) and not full
    head = f"night {night}: {len(recorded)}/{len(SUBSYSTEMS)} recorded"
    if not recorded:
        return ([head + (" — all nine still owed" if warm else
                         " — none of the nine yet (`mechanic.py subsystems` "
                         "lists them)")], snap)
    if warm and recorded == (prev.get("recorded") or {}):
        owed = f" — still owed: {', '.join(gaps)}" if gaps else ""
        return ([f"{head}, same rows as baseline night "
                 f"{prev.get('night') or '?'}{owed}"], snap)
    if not gaps:
        return ([f"{head} — all nine"], snap)
    done = [f"{s} ({recorded[s]})" for s in SUBSYSTEMS if s in recorded]
    return ([head,
             f"  recorded: {', '.join(done)}",
             f"  still owed: {', '.join(gaps)}"], snap)


def cmd_subsystems() -> int:
    for name in SUBSYSTEMS:
        print(name)
    return 0


def cmd_checklist(cfg: dict) -> int:
    """Tonight's coverage. Read-only; exit 1 while any row is missing, so the
    same question a `pass_done` will be refused for can be asked first."""
    night, events = tonight(cfg)
    rows = checklist(events)
    print(f"night {night} · {len(rows)}/{len(SUBSYSTEMS)} subsystems recorded")
    for name in SUBSYSTEMS:
        row = rows.get(name)
        if row:
            print(f"  {name:<12} {row.get('result',''):<13} "
                  f"{str(row.get('note',''))[:80]}")
        else:
            print(f"  {name:<12} {'—':<13} not examined yet")
    gaps = missing_subsystems(events)
    if gaps:
        print(f"missing: {', '.join(gaps)} — `pass_done` is refused until "
              "every row is recorded")
        return 1
    return 0


def cmd_record(event_json: str, cfg: dict) -> int:
    try:
        e = json.loads(event_json)
    except json.JSONDecodeError as err:
        raise SystemExit(f"record: not valid JSON: {err}")
    if not isinstance(e, dict) or not e.get("event"):
        raise SystemExit('record: payload must be an object with an "event" field')
    night, events = tonight(cfg)
    problem = validate_event(e, night, events)
    if problem:
        raise SystemExit(f"record: {problem}")
    if e.get("event") == "finding":
        e.setdefault("finding", next_finding_id(night, events))
        # The ledger first, for the same reason `propose` calls the estate
        # first: a history line whose outcome never reached the shared record
        # is a finding only this loop can see, which is the gap a prior finding closes.
        ok, err = emit_finding_outcome(e, night)
        if not ok:
            print(f"record: the finding outcome did not reach the ledger "
                  f"({err}) — nothing recorded", file=sys.stderr)
            return 1
    e = append_history(e, cfg)
    # a prior finding, after the history line rather than before it. A finding's estate
    # row comes FIRST because a history line claiming an outcome nobody outside
    # this loop can see is the gap a prior finding closed. A pass boundary is the other
    # way round: history.jsonl is what `derive_phase` reads to decide whether
    # tonight is a fresh pass or a resume, so it must never be missing because
    # a second store was locked. Best-effort, and it says so if it failed.
    if e.get("event") in PASS_PHASE:
        # `events` is tonight's history as it stood BEFORE this boundary line,
        # which is exactly what decides whether a `pass_done` completed with
        # work or with none — the boundary itself is not a finding.
        ok, why = emit_pass_event(e, e["night"], events)
        if not ok:
            print(f"[warn] {e['event']} did not reach the shared Ledger ({why}) "
                  "— history.jsonl has it", file=sys.stderr)
    trailer = f" [{e['finding']}]" if e.get("finding") else ""
    print(f"recorded {e['event']}{trailer} (night {e['night']})")
    return 0


# --------------------------------------------------------------------------- #
# the durable proposal record (the consistency contract)
# --------------------------------------------------------------------------- #
# The mechanic's own actor name in the shared store. Not the persona — the
# ledger and the estate both take the TECHNICAL name (docs/actor-taxonomy.md).
ESTATE_ACTOR = "mechanic"
PROPOSAL_SOURCE = "state/mechanic/REPORT.md"


def estate_script() -> str:
    return os.environ.get("ESTATE_SCRIPT") or os.path.join(
        repo_root(), "bin", "estate")


def _estate(*argv: str) -> tuple[int, str, str]:
    """Run bin/estate. Returns (rc, stdout, stderr), never raises.

    `proposal add` prints the task id on stdout and the disposition (new /
    recurrence / adopted) on stderr, so both streams are kept apart.
    """
    try:
        out = subprocess.run([sys.executable, estate_script(), *argv],
                             capture_output=True, text=True, timeout=30)
        return out.returncode, (out.stdout or "").strip(), (out.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, "", f"{type(err).__name__}: {err}"


def cmd_proposals(stage: str | None = None) -> int:
    """Live proposal tasks, by review stage. Read-only, and the reason the pass
    can tell a new condition from one already awaiting review."""
    argv = ["proposal", "list"]
    if stage:
        argv += ["--stage", stage]
    rc, out, err = _estate(*argv)
    if rc != 0:
        print(f"proposal list unavailable: {err or out}", file=sys.stderr)
        return 1
    print(out or "(no proposals on file)")
    return 0


# --------------------------------------------------------------------------- #
# the shared Ledger (the consistency contract)
# --------------------------------------------------------------------------- #
# a prior finding already put every FINDING in the estate's events table. What a prior finding adds
# is the pass around them: `pass_start` and `pass_done` lived only in
# history.jsonl, so "did the mechanic run last night" was answerable from
# inside this loop and nowhere else — and a night that started and never
# finished was invisible to everything that reads the shared store.
#
# The nine-subsystem names are already SUBSYSTEMS above (a prior finding); the four phases
# are copied here for the same reason that list is copied — this engine runs
# against a fabricated repo root in its own tests and must not import the
# repo's lib/. bin/test_subsystem_events.py fails if any copy drifts.
LEDGER_SUBSYSTEM = "mechanic"
INITIATION, TRANSITION, OUTCOME, FAILURE = (
    "initiation", "transition", "outcome", "failure")
FINGERPRINT_PREFIX, FINGERPRINT_HEX = "e-", 16
# A finding's phase is what the finding DID. `observed` and `no-proposal` are
# results the pass reached, and so is `proposed`; none of them is a failure of
# the mechanic, which is why nothing here maps to FAILURE. The mechanic's own
# failure phase belongs to a pass that could not complete, and that is a prior finding's
# `pass_failed`, recorded through PASS_PHASE below.
PASS_PHASE = {"pass_start": INITIATION, "pass_done": OUTCOME,
              "pass_failed": FAILURE}

# a prior finding. The night IS the mechanic's scheduled run, so its pass boundaries are
# also its run record: `mechanic/<night>` with `started` and then exactly one
# terminal outcome. Copies of lib/estate_runs.py's vocabulary, checked by
# bin/test_subsystem_events.py like every other copy in this file.
RUN_STARTED, RUN_COMPLETED, RUN_NO_ACTIVITY, RUN_FAILED = (
    "started", "completed", "no_activity", "failed")
PASS_RUN_OUTCOME = {"pass_start": RUN_STARTED, "pass_done": RUN_COMPLETED,
                    "pass_failed": RUN_FAILED}


def run_outcome_for(name: str, tonight_events: list[dict]) -> str:
    """This pass boundary's run outcome.

    `pass_done` splits two ways, and the split is read off the night's own
    history rather than off a count the model passed in: a night that recorded
    no finding at all did its examination and found nothing, which is S54's
    "completed with no work" and is a success. A night that recorded findings
    completed with work. Nothing here can produce `failed` — that is
    `pass_failed`, and only a pass that says it failed gets it (a prior finding).
    """
    outcome = PASS_RUN_OUTCOME.get(name)
    if outcome == RUN_COMPLETED and not any(
            e.get("event") == "finding" for e in tonight_events):
        return RUN_NO_ACTIVITY
    return outcome


def ledger_fingerprint(*, subsystem: str, phase: str, source: str, key: str,
                       ts: str, event: str = "") -> str:
    """lib/estate_ledger.fingerprint(), copied. See LEDGER_SUBSYSTEM above."""
    parts = [subsystem or "", phase or "", source or "", key or "", ts or "",
             event or ""]
    digest = hashlib.sha256("\n".join(str(p).strip() for p in parts).encode())
    return FINGERPRINT_PREFIX + digest.hexdigest()[:FINGERPRINT_HEX]


def emit_pass_event(e: dict, night: str,
                    tonight_events: list[dict] = ()) -> tuple[bool, str]:
    """Put a pass boundary in the shared Ledger. Idempotent on the night.

    The fingerprint is over the night id, not over the clock, so a pass that
    records `pass_start` twice for one night — a resumed tick replaying its
    own history — lands one row. That is the same night, not a second pass.

    a prior finding rides on the same row rather than beside it: this boundary IS the
    mechanic's scheduled-run record, so it carries `mechanic/<night>` and the
    run outcome. One event, one row, two readings of it.
    """
    name = str(e.get("event") or "")
    phase = PASS_PHASE.get(name)
    if not phase:
        return True, ""
    outcome = run_outcome_for(name, list(tonight_events))
    run = f"{LEDGER_SUBSYSTEM}/{night}"
    counts = {k: e[k] for k in ("findings", "proposed", "applied")
              if e.get(k) is not None}
    tail = (" — " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            if counts else "")
    # a prior finding. The pass's own intake row says which facts were in scope when the
    # night started — the ids AND the scopes, because a promotion moves a
    # memory and the record is of what was recalled THEN. Only on the boundary
    # that begins the pass: `pass_done` and `pass_failed` are about what the
    # night did, and repeating the fact set on them would say nothing new.
    # Empty is written, not omitted (docs/memory-recall-contract.md).
    recall: dict = {}
    if name == "pass_start":
        _, recall, why = recall_memories()
        if why:
            print(f"[warn] memory recall did not reach the pass_start row "
                  f"({why}) — the pass still runs", file=sys.stderr)
    rc, out, err = _estate(
        "event", "--actor", ESTATE_ACTOR, "--kind", "activity",
        "--summary", f"night {night}: {name}{tail}"[:300],
        "--subsystem", LEDGER_SUBSYSTEM, "--phase", phase,
        # The fingerprint stays keyed on (night, event name) and NOT on the run
        # outcome. A resumed pass that recorded findings after an empty first
        # tick must not be able to file a second `pass_done` under a different
        # outcome — the night has one terminal row, and the first one to land
        # is it.
        "--fingerprint", ledger_fingerprint(
            subsystem=LEDGER_SUBSYSTEM, phase=phase, source="mechanic",
            key=night, ts=night, event=name),
        "--run-id", run, "--run-outcome", outcome,
        "--refs", json.dumps(dict(counts, **recall, night=night, event=name,
                                  run=run, run_outcome=outcome),
                             sort_keys=True))
    return rc == 0, (err or out)


def emit_finding_outcome(e: dict, night: str, task: str = "") -> tuple[bool, str]:
    """Put one finding's outcome in the estate's events table (a prior finding).

    history.jsonl stays this loop's authoritative archive. The ledger event is
    what makes the outcome visible from OUTSIDE the loop — S08 and S09 both
    ask for the outcome there, and `observed` / `no-proposal` findings have no
    task of their own to hang it on, so the finding id is the link.

    a prior finding typed it: the row now says which of the nine it is about and that it
    is an outcome, so `estate events --subsystem mechanic --phase outcome`
    reaches it. `kind` stays `note`, so nothing that already reads these rows
    shifts under it.
    """
    action = e.get("action", "")
    subsystem = e.get("subsystem", "")
    refs = {"finding": e.get("finding"), "night": night,
            "subsystem": subsystem, "action": action}
    if e.get("class"):
        refs["class"] = e["class"]
    detail = _nonempty(e, "reason") or _nonempty(e, "summary")
    argv = ["event", "--actor", ESTATE_ACTOR, "--kind", "note",
            "--summary", f"finding {e.get('finding')} ({subsystem}): {action}",
            "--detail", detail[:500],
            # The finding's own subsystem is what it is ABOUT; this row is the
            # mechanic's act of recording it, so the acting subsystem is the
            # mechanic. The one it examined stays in refs, where a prior finding put it.
            "--subsystem", LEDGER_SUBSYSTEM, "--phase", OUTCOME,
            "--fingerprint", ledger_fingerprint(
                subsystem=LEDGER_SUBSYSTEM, phase=OUTCOME, source="mechanic",
                key=str(e.get("finding") or night), ts=night, event="finding"),
            "--refs", json.dumps(refs, sort_keys=True)]
    if task:
        argv += ["--task", task]
    rc, out, err = _estate(*argv)
    return rc == 0, (err or out)


def cmd_propose(payload_json: str, cfg: dict) -> int:
    """File one proposal durably, and record the matching finding line.

    Payload, all five required (a prior finding — a proposal the operator cannot act on is not a
    proposal):
      title             the recommendation, in one line
      subsystem         which of the nine this is about
      condition         what was observed. The fingerprint derives from it, so
                        it is what makes the same condition, written up
                        differently on another night, land on the same task
      desired_outcome   what the estate looks like once this is settled
      completion_check  how anyone tells it actually is
    Optional: class, intent, summary, note.

    The estate call comes FIRST. If it fails, nothing is recorded here either —
    a finding line claiming a task id that does not exist is worse than a pass
    that stops and says why, because the next pass would trust it.
    """
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as err:
        raise SystemExit(f"propose: not valid JSON: {err}")
    if not isinstance(payload, dict):
        raise SystemExit("propose: payload must be a JSON object")
    for field in ("title", "condition", "desired_outcome", "completion_check"):
        if not _nonempty(payload, field):
            raise SystemExit(
                f'propose: "{field}" is required and must be non-empty. A '
                "reviewable proposal names the condition, the desired "
                "outcome, and the check that says it is done (a prior finding)")
    if payload.get("subsystem") not in SUBSYSTEMS:
        raise SystemExit("propose: \"subsystem\" must be one of: "
                         f"{', '.join(SUBSYSTEMS)} "
                         f"(got {payload.get('subsystem')!r})")
    night, events = tonight(cfg)
    finding = next_finding_id(night, events)
    title = _nonempty(payload, "title")
    condition = _nonempty(payload, "condition")
    refs = {"source": PROPOSAL_SOURCE, "night": night, "finding": finding,
            "subsystem": payload["subsystem"]}
    if payload.get("class"):
        refs["class"] = payload["class"]
    argv = ["proposal", "add", title, "--condition", condition,
            "--desired-outcome", _nonempty(payload, "desired_outcome")[:500],
            "--completion-check", _nonempty(payload, "completion_check")[:500],
            "--actor", ESTATE_ACTOR, "--refs", json.dumps(refs, sort_keys=True)]
    argv += ["--intent", (_nonempty(payload, "intent") or
                          f"{_nonempty(payload, 'desired_outcome')} "
                          f"Done when: {_nonempty(payload, 'completion_check')}"
                          )[:500]]
    if payload.get("note"):
        argv += ["--note", str(payload["note"])[:500]]
    rc, out, err = _estate(*argv)
    if rc != 0 or not out.startswith("t-"):
        print(f"propose: bin/estate refused this proposal: {err or out}",
              file=sys.stderr)
        return 1
    task = out.splitlines()[0].strip()
    disposition = "recurrence" if "recurrence" in err else "new"
    line = {
        "event": "finding", "action": "proposed",
        "class": payload.get("class") or "unclassified",
        "subsystem": payload["subsystem"], "finding": finding,
        "task": task, "disposition": disposition,
        "summary": payload.get("summary") or title,
        "condition": condition,
        "desired_outcome": _nonempty(payload, "desired_outcome"),
        "completion_check": _nonempty(payload, "completion_check"),
    }
    ok, why = emit_finding_outcome(line, night, task=task)
    if not ok:
        # The task exists and carries the finding id; only the linking event
        # failed. Say so loudly rather than recording a history line that
        # claims an outcome nobody outside this loop can see.
        print(f"propose: {task} was filed, but its outcome event did not reach "
              f"the ledger ({why}) — nothing recorded in history",
              file=sys.stderr)
        return 1
    append_history(line, cfg)
    # stdout is exactly one machine-readable line: the caller wants the id.
    print(f"{task} {disposition} {finding}")
    return 0


def proposal_digest(prev: dict, full: bool) -> tuple[list[str], dict]:
    """Print-lines + the snapshot slice for the gather's proposal section:
    what is already on file, by stage.

    Read-only and best-effort. A store that is missing or locked is a fact
    about this machine, never a finding about the estate — same rule the
    extraction lens follows.

    Warm and cold, like every other section. A proposal the baseline gather
    already printed, sitting at the same stage since, collapses into a named
    count instead of its whole line; a new one, or one that moved, prints in
    full. The ids stay on the page either way and `mechanic.py proposals`
    prints the list on demand, so a collapsed row is not a hidden one — it is
    one the pass has already read.
    """
    rc, out, err = _estate("proposal", "list", "--all")
    warm = bool(prev) and not full
    if rc != 0:
        why = (err or out).strip()
        snap = {"unavailable": digest(why)}
        if warm and prev.get("unavailable") == snap["unavailable"]:
            return (["proposal store still unavailable, same reason as at "
                     "baseline — lens skipped this pass again (not a finding "
                     "about the estate)"], snap)
        return ([f"proposal store unavailable ({why}) — lens skipped this "
                 "pass (not a finding about the estate)"], snap)

    rows = [line.strip() for line in out.splitlines() if line.strip()]
    # The id is the first token of a proposal line, so a row is keyed by the
    # proposal and its hash says whether the stage or the title has moved.
    now_rows = {r.split()[0]: digest(r) for r in rows}
    prev_rows = (prev.get("prop_rows") or {}) if warm else {}
    snap = {"prop_rows": now_rows}
    gone = sorted(k for k in prev_rows if k not in now_rows)

    if not rows:
        empty = ("(still no proposals on file — unchanged since baseline)"
                 if warm and not prev_rows else
                 "(no proposals on file yet — `bin/estate proposal list`)")
        return ([empty] + ([f"no longer listed since baseline ({len(gone)}): "
                            + ", ".join(gone)] if gone else []), snap)

    if warm:
        lines = [f"{len(rows)} on file. Classify every condition against them "
                 "before calling it new."]
    else:
        lines = [f"{len(rows)} on file. Classify EVERY condition you are about "
                 "to write up with `mechanic.py propose` (or `bin/estate "
                 "proposal classify`) before calling it new."]
    steady: list[str] = []
    for r in rows:
        rid = r.split()[0]
        if not warm:
            lines.append(f"  {r}")
        elif rid not in prev_rows:
            lines.append(f"  {r}  [new since baseline]")
        elif prev_rows[rid] != now_rows[rid]:
            lines.append(f"  {r}  [CHANGED since baseline]")
        else:
            steady.append(rid)
    if steady:
        lines.append(f"  unchanged since baseline ({len(steady)}) — already "
                     f"read, `mechanic.py proposals` for the detail: "
                     + ", ".join(steady))
    if gone:
        lines.append(f"  no longer listed since baseline ({len(gone)}): "
                     + ", ".join(gone))
    return lines, snap


# --------------------------------------------------------------------------- #
# scope-safe memory recall (the consistency contract)
# --------------------------------------------------------------------------- #
# The mechanic's intake is `gather` — it IS the pass's whole input — so that is
# where the facts the pass is allowed to know arrive. One call, one scope, and
# the read path is `bin/estate memory recall` and nothing else: not
# `memory list`, not `memory query`, not docs/lessons.md (a render of the
# shared rows, which cannot carry a local fact or label one), not the store.
# docs/memory-recall-contract.md says why.
MEMORY_SCOPE = "mechanic"
# lib/estate_memory.py's REF_KEY. Named here rather than copied through as an
# opaque blob so this engine's dependence on the envelope's shape is visible —
# if the key ever changes, this stops recording instead of silently recording
# something a reader cannot find. bin/test_memory_recall.py checks the copy.
MEMORY_REF_KEY = "memory_recall"


def recall_memories() -> tuple[list[dict], dict, str]:
    """(facts, refs envelope, error). Never raises, never blocks a pass.

    Best-effort on the same terms as every other estate call in this file: a
    store that is missing, mid-migration or locked degrades to "no facts
    recalled", said out loud, and the pass runs. The error string is returned
    rather than printed so the gather can render it as what it is — a fact
    about this machine, not a finding about the estate.
    """
    rc, out, err = _estate("memory", "recall", "--scope", MEMORY_SCOPE, "--json")
    if rc != 0:
        return [], {}, (err or out).strip() or "unavailable"
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        return [], {}, f"unreadable recall output: {exc}"
    refs = payload.get("refs") or {}
    envelope = ({MEMORY_REF_KEY: refs[MEMORY_REF_KEY]}
                if MEMORY_REF_KEY in refs else {})
    return payload.get("memories") or [], envelope, ""


def memory_digest(prev: dict, full: bool) -> tuple[list[str], dict]:
    """Print-lines + the snapshot slice for the gather's memory section.

    Warm and cold like every other section, and for the same reason: a fact the
    baseline gather already printed in full has been read, so it collapses to
    its labelled id. What NEVER collapses is the label — every line carries the
    scope the fact is stored at, because "this is estate-wide" and "this is
    only ours" are different instructions (S36).
    """
    facts, refs, err = recall_memories()
    warm = bool(prev) and not full
    if err:
        snap = {"unavailable": digest(err)}
        if warm and prev.get("unavailable") == snap["unavailable"]:
            return (["memory store still unavailable, same reason as at "
                     "baseline — recall skipped this pass again (not a finding "
                     "about the estate)"], snap)
        return ([f"memory store unavailable ({err}) — recall skipped this pass "
                 "(not a finding about the estate)"], snap)

    now_rows = {f["id"]: digest(f"{f['scope']}|{f['body']}") for f in facts}
    prev_rows = (prev.get("rows") or {}) if warm else {}
    snap = {"rows": now_rows, "refs": refs}
    gone = sorted(k for k in prev_rows if k not in now_rows)
    tail = ([f"  no longer recalled since baseline ({len(gone)}): "
             + ", ".join(gone)] if gone else [])

    if not facts:
        # An empty recall is an answer, not a blank. See a prior finding on why "nothing
        # to do" and "never ran" must stay distinguishable.
        return ([f"no active memories at scope {MEMORY_SCOPE} or shared — "
                 "recall ran and the estate holds nothing for this pass"]
                + tail, snap)

    lines = [f"{len(facts)} active fact(s) at scope {MEMORY_SCOPE} + shared. "
             "These bind this pass; a proposal that contradicts one has to say "
             "so and why."]
    steady: list[str] = []
    for f in facts:
        head = f"  {f['id']}  [{f['scope']}]  {f['kind']}"
        if warm and f["id"] in prev_rows and prev_rows[f["id"]] == now_rows[f["id"]]:
            steady.append(f"{f['id']}[{f['scope']}]")
            continue
        mark = "" if not warm else (
            "  [new since baseline]" if f["id"] not in prev_rows
            else "  [CHANGED since baseline]")
        lines.append(head + mark)
        lines.extend(textwrap.wrap(" ".join(f["body"].split()), width=79,
                                   initial_indent="      ",
                                   subsequent_indent="      ") or ["      (empty)"])
    if steady:
        lines.append(f"  unchanged since baseline ({len(steady)}) — already "
                     "read, `bin/estate memory recall --scope "
                     f"{MEMORY_SCOPE}` for the text: " + ", ".join(steady))
    return lines + tail, snap


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)


def _pace_override(root: str, name: str) -> str | None:
    """The recorded off-registry cadence for a loop, or None.

    One token in state/<name>/pace-override: a /loop interval argument, or the
    word `dynamic`. Written only when the operator asks for a cadence other than the
    registry's, and read here for the same reason bin/ops health reads it — so
    a deliberate choice stops looking like a broken loop.
    """
    try:
        with open(os.path.join(root, "state", name, "pace-override")) as f:
            return f.read().strip() or None
    except OSError:
        return None


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

    # One deck read for every session (was: one `session show` per loop).
    hub = hub_config(registry)
    sessions, deck_note = deck_sessions(hub["deck_profile"])
    print("\n== sessions (agent-deck ls --json, one call) ==")
    if deck_note:
        print(deck_note)
    titles = {hub["session"]: "hub"}
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
        registry_iv = (loops.get(name) or {}).get("interval")
        # A loop the operator deliberately took off its registry cadence records that
        # in state/<name>/pace-override — the same file bin/ops health reads
        # Judging it against the registry interval anyway is how this
        # check produced a nightly false alarm on a loop that was doing exactly
        # what he asked. `dynamic` gets NO verdict at all: a self-pacing loop
        # and a dead one leave the same stale heartbeat, and guessing between
        # them is the thing the override exists to stop.
        override = _pace_override(root, name)
        basis, secs = registry_iv, parse_interval(registry_iv)
        if override:
            print(f"{name}: {_heartbeat_age(hb, now_utc)} "
                  f"[off-registry: pace-override={override}, "
                  f"registry={registry_iv}]")
            if override == "dynamic":
                continue
            basis, secs = override, parse_interval(override)
            if secs is None:
                drift.append(f"{name}: unreadable pace-override {override!r} "
                             f"(want a /loop interval, or 'dynamic')")
                continue
        else:
            print(f"{name}: {_heartbeat_age(hb, now_utc)}")
        if secs:
            try:
                with open(hb) as f:
                    age = int(now_utc.timestamp()) - int(f.read().strip())
            except (OSError, ValueError):
                drift.append(f"{name}: no readable heartbeat")
                continue
            if age > secs * 2 + 120:
                drift.append(f"{name}: heartbeat {age}s old > "
                             f"{secs * 2 + 120}s (2*{basis}+120)")
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

    print("\n== subsystem checklist (all nine, every pass) ==")
    chk_lines, chk = checklist_digest(cfg, prev.get("checklist") or {}, full)
    print("\n".join(chk_lines))

    print("\n== memory (scope mechanic + shared, active only) ==")
    mem_lines, memories = memory_digest(prev.get("memories") or {}, full)
    print("\n".join(mem_lines))

    print("\n== proposals on file (durable review lifecycle) ==")
    prop_lines, proposals = proposal_digest(prev.get("proposals") or {}, full)
    print("\n".join(prop_lines))

    print("\n== extraction (public core allowlist, three buckets) ==")
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
                "checklist": chk,
                "memories": memories,
                "proposals": proposals,
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
    sub.add_parser("subsystems", help="the nine subsystems every pass examines")
    sub.add_parser("checklist", help="tonight's nine-subsystem coverage (a prior finding)")
    p = sub.add_parser("proposals", help="live proposal tasks by review stage")
    p.add_argument("--stage")
    p = sub.add_parser("propose", help="file one proposal durably (a prior finding)")
    p.add_argument("payload_json")
    sub.add_parser("report")
    args = ap.parse_args(argv)

    if args.cmd == "windows":
        return cmd_windows(cfg)
    if args.cmd == "gather":
        return cmd_gather(cfg, full=args.full, save=args.save)
    if args.cmd == "record":
        return cmd_record(args.event_json, cfg)
    if args.cmd == "subsystems":
        return cmd_subsystems()
    if args.cmd == "checklist":
        return cmd_checklist(cfg)
    if args.cmd == "proposals":
        return cmd_proposals(args.stage)
    if args.cmd == "propose":
        return cmd_propose(args.payload_json, cfg)
    if args.cmd == "report":
        return cmd_report()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

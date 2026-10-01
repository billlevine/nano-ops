"""Pending judgment: the call a worker makes and records instead of halting.

The policy: opt-in per dispatch, and every existing hard gate kept. An
installation decides which of its dispatches may record a pending judgment at
all; this module is what such a record must carry, and the only place that
says it.

A pending judgment is a BOUNDED, REVERSIBLE call made mid-design or
mid-implementation where the brief is silent: the worker picks, keeps going,
and leaves a record on the PR for the reviewer to dispose of. It is "pending"
because nobody with authority has looked at it yet; the reviewer's disposition
is what ends that.

What it is not, and never becomes by being recorded well:

    core-contradiction   the plan defeats the ticket's core requirement,
                         at whatever layer it is stated
    public-interface     a public API, CLI, field or wire shape moves
    security             a credential, secret, auth or trust boundary moves
    data-contract        a schema, migration, on-disk or stored format moves

Those four stay blocked exactly as they were: the worker halts and the halt
lifecycle applies. A record naming one is REFUSED, not accepted with a warning,
because "I recorded it" is the argument this boundary exists to reject. The
prose guard below reads the last three; `core-contradiction` has no wording it
could be read from, and is enforced only by a declared `touches` and by
whatever contradiction check the installation's own pipeline already runs.

The record is JSON, one object per call. On the PR it is a rendered section
with each record's JSON in a fenced ```pending-judgment block beneath it, so
the reviewer reads prose and a tool re-validates the same bytes (`records_in`).
"""
from __future__ import annotations

import json
import re

# ── the record ───────────────────────────────────────────────────────────────
# Every one required, every one non-empty. The five a reviewer needs to
# dispose of a call (rationale, alternatives, affected behaviour,
# verification, reviewer disposition), plus what makes it identifiable and
# BOUNDED: which call, at which stage, how it would be undone, and which
# blocked class it touches.
TEXT_FIELDS = ("id", "call", "rationale", "affected_behavior",
               "verification", "reversal")
FIELDS = TEXT_FIELDS + ("stage", "alternatives", "touches",
                        "reviewer_disposition")

STAGES = ("design", "implementation")

# The worker writes `pending`; only a reviewer moves it. `amended` and
# `reverted` are the dispositions that count as rework.
PENDING = "pending"
DISPOSITIONS = (PENDING, "accepted", "amended", "reverted")
# The dispositions a reviewer may move a record TO. `pending` is absent on
# purpose: it is the worker's own state, and no dispose ever writes it back.
REVIEWER_DISPOSITIONS = ("accepted", "amended", "reverted")

BLOCKED_CLASSES = ("core-contradiction", "public-interface", "security",
                   "data-contract")

# ── the text guard ───────────────────────────────────────────────────────────
# A declared-empty `touches` is a claim, and the same prose that would make it
# false is refused here too. The guard is in two halves for one reason: NAMING a
# public contract is not MOVING one, so a subject escalates only with a change
# verb in the same sentence, while the ALWAYS phrases are the change on their
# own. An installation that runs an equivalent guard earlier in the lifecycle
# (at dispatch time, against the ticket's own wording) should assert its phrase
# set is a subset of these, so the two cannot drift apart.
ALWAYS = (
    "breaking change", "backwards incompatible", "backward incompatible",
    "schema migration", "data migration",
)
SUBJECTS = (
    "public contract", "public api", "public interface", "public field",
    "security boundary", "auth boundary", "trust boundary",
    "wire format", "on-disk format",
    "database schema", "data contract", "stored format", "api response",
    "cli flag", "command-line flag",
    "credential", "credentials", "secret", "secrets", "api token", "api key",
    "access token", "authentication", "authorization",
)
# The data-contract words, held apart for one reason: `verification` is
# scanned without them. A migration ticket's test names its migration, and
# whether a call on such a ticket may be a pending judgment at all is a scope
# question for the installation to settle.
DATA_CONTRACT_WORDS = (
    "schema migration", "data migration", "on-disk format", "database schema",
    "data contract", "stored format",
)
CHANGE_VERBS = (
    "change", "changes", "changed", "changing",
    "alter", "alters", "altered", "altering",
    "modify", "modifies", "modified", "modifying",
    "widen", "widens", "widened", "widening",
    "narrow", "narrows", "narrowed", "narrowing",
    "break", "breaks", "broke", "broken", "breaking",
    "extend", "extends", "extended", "extending",
    "add to", "adds to", "added to", "adding to",
    "remove from", "removes from", "removed from",
    "rename", "renames", "renamed", "renaming",
    "introduce", "introduces", "introduced", "introducing",
    "replace", "replaces", "replaced", "replacing",
    "add", "adds", "added", "adding",
    "remove", "removes", "removed", "removing",
    "drop", "drops", "dropped", "dropping",
    "delete", "deletes", "deleted", "deleting",
    "expose", "exposes", "exposed", "exposing",
    "disable", "disables", "disabled", "disabling",
    "move", "moves", "moved", "moving",
    "store", "stores", "stored", "storing",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n\s*\n")

# ── outcomes ─────────────────────────────────────────────────────────────────
VALID = "valid"
INVALID = "invalid"      # a field is missing, empty or out of vocabulary
REFUSED = "refused"      # the call is in a blocked class: it was never one to make


def _word(w: str) -> str:
    return r"(?<![\w-])" + re.escape(w) + r"(?![\w-])"


def _words_in(text: str, words) -> list:
    low = " ".join((text or "").lower().split())
    return [w for w in words if re.search(_word(w), low)]


def blocked_phrases(text: str, skip=()) -> list:
    """The phrases in `text` that say a blocked class MOVED. Empty when none.

    `skip` leaves those phrases out, and is only ever DATA_CONTRACT_WORDS.
    """
    always = [w for w in ALWAYS if w not in skip]
    subject_words = [w for w in SUBJECTS if w not in skip]
    found = list(_words_in(text, always))
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        subjects = _words_in(sentence, subject_words)
        # The verb is looked for outside the subject, so "stored format"
        # does not supply its own "stored".
        rest = " ".join(sentence.lower().split())
        for subject in subjects:
            rest = re.sub(_word(subject), " ", rest)
        if subjects and _words_in(rest, CHANGE_VERBS):
            found.extend(s for s in subjects if s not in found)
    return found


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


# Each text field is rendered as one markdown bullet, so a line break (or a
# fence of its own) would let a field's prose open a second record.
_NOT_ONE_LINE = re.compile(r"[\r\n]|```|~~~")


def check_note(note):
    """The problem with a reviewer note, or None when it may be recorded.

    The note is optional: absent, None, or blank is fine, and says nothing.
    A note that is present must be a one-line string with no fence in it,
    for the same reason the text fields must be.
    """
    if note is None or (isinstance(note, str) and not note.strip()):
        return None
    if not isinstance(note, str):
        return "`reviewer_note` must be a string"
    if _NOT_ONE_LINE.search(note):
        return "`reviewer_note` must be one line with no fence in it"
    return None


def check(record, *, worker: bool = False) -> tuple:
    """(outcome, problems) for one record. REFUSED outranks INVALID.

    A blocked-class call is refused even when its record is otherwise
    incomplete: fixing the missing field would not make it a call to make.
    `worker` is the publishing path (`render`): there the disposition must
    still be `pending`, since no reviewer has looked yet.
    """
    if not isinstance(record, dict):
        return INVALID, ["a pending judgment is a JSON object"]
    problems, refusals = [], []

    for key in TEXT_FIELDS:
        if not _text(record.get(key)):
            problems.append(f"`{key}` is missing or empty")
        elif _NOT_ONE_LINE.search(record[key]):
            problems.append(f"`{key}` must be one line with no fence in it")
    if record.get("stage") not in STAGES:
        problems.append(f"`stage` must be one of {', '.join(STAGES)}")
    if record.get("reviewer_disposition") not in DISPOSITIONS:
        problems.append("`reviewer_disposition` must be one of "
                        + ", ".join(DISPOSITIONS)
                        + " (a worker writes `pending`)")
    elif worker and record["reviewer_disposition"] != PENDING:
        problems.append("`reviewer_disposition` is `pending` when a worker "
                        "publishes: only a reviewer moves it")

    if "reviewer_note" in record:
        note_problem = check_note(record["reviewer_note"])
        if note_problem is not None:
            problems.append(note_problem)

    alternatives = record.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        problems.append("`alternatives` needs at least one option NOT taken: "
                        "a call with no alternative was not a judgment")
    else:
        for n, alt in enumerate(alternatives, 1):
            if not (isinstance(alt, dict) and _text(alt.get("option"))
                    and _text(alt.get("why_not"))):
                problems.append(f"alternative {n} needs `option` and `why_not`")
            elif any(_NOT_ONE_LINE.search(alt[k]) for k in ("option", "why_not")):
                problems.append(f"alternative {n} must be one line with no "
                                f"fence in it")

    touches = record.get("touches")
    if not isinstance(touches, list):
        # Absent is not empty. A record that never said which classes it
        # touches has not said it touches none.
        problems.append("`touches` must be a list, [] when the call touches "
                        "no blocked class: " + ", ".join(BLOCKED_CLASSES))
    else:
        for item in touches:
            if item in BLOCKED_CLASSES:
                refusals.append(f"touches {item}: a blocked class, so the "
                                f"worker halts instead")
            else:
                problems.append(f"`touches` names {item!r}, which is not one "
                                f"of " + ", ".join(BLOCKED_CLASSES))

    # Each field on its own, so a subject ending one field cannot pair with a
    # verb opening the next. `verification` is read without the data-contract
    # words (see DATA_CONTRACT_WORDS); `alternatives` not at all, since they
    # describe the move NOT made.
    phrases = []
    for key in ("call", "rationale", "affected_behavior", "reversal",
                "verification"):
        skip = DATA_CONTRACT_WORDS if key == "verification" else ()
        phrases.extend(p for p in blocked_phrases(_text(record.get(key)), skip)
                       if p not in phrases)
    # No negation handling, on purpose: "does not change the public API" is
    # refused along with "changes the public API". A false refusal costs one
    # rewritten sentence; a negation reader is one "not only" away from
    # passing the real thing. `touches: []` is where "it moves nothing" lives.
    for phrase in phrases:
        refusals.append(f"the record says {phrase!r} moves: a blocked class, "
                        f"whatever `touches` declares (if the sentence says "
                        f"it does NOT move, delete it: `touches: []` says so)")

    if refusals:
        return REFUSED, refusals + problems
    if problems:
        return INVALID, problems
    return VALID, []


def check_all(records, *, worker: bool = False) -> tuple:
    """(worst outcome, [(id, outcome, problems)]) over a list of records."""
    results, worst = [], VALID
    ids = set()
    for n, record in enumerate(records or [], 1):
        outcome, problems = check(record, worker=worker)
        rid = _text(record.get("id")) if isinstance(record, dict) else ""
        if rid and rid in ids:
            problems = problems + [f"id {rid} appears twice"]
            outcome = outcome if outcome == REFUSED else INVALID
        ids.add(rid)
        results.append((rid or f"#{n}", outcome, problems))
        if outcome == REFUSED or (outcome == INVALID and worst == VALID):
            worst = outcome
    return worst, results


# ── on the PR ────────────────────────────────────────────────────────────────
SECTION = "## Pending judgments"
_FENCE = re.compile(r"```pending-judgment\n(.*?)\n```", re.DOTALL)
# Every line that OPENS a block, spelled loosely: indented, a trailing space,
# a tilde fence. Counted apart from what _FENCE parses, so a block _FENCE
# cannot read (unclosed, misspelled) is broken, not absent.
_OPENING = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*pending-judgment\b",
                      re.MULTILINE)


def dump(record) -> str:
    """The record exactly as it sits inside its fenced block."""
    return json.dumps(record, sort_keys=True)


# The prose prefixes a `dispose` consumer searches for when it refreshes a
# record's section. Single-sourced here so the writer and the searcher cannot
# spell them apart.
DISPOSITION_PREFIX = "- **Reviewer disposition:**"
NOTE_PREFIX = "- **Reviewer note:**"


def disposition_line(record) -> str:
    """The prose line carrying a record's reviewer disposition."""
    return f"{DISPOSITION_PREFIX} {record['reviewer_disposition']}"


def note_line(record):
    """The prose line carrying a record's reviewer note, or None absent one."""
    note = record.get("reviewer_note")
    if note is None or (isinstance(note, str) and not note.strip()):
        return None
    return f"{NOTE_PREFIX} {note}"


def render(records) -> str:
    """The PR section. Prose for the reviewer, the record beneath it verbatim."""
    out = [SECTION, "",
           "Calls made where the brief was silent, under pending judgment. "
           "Each is bounded and reversible; none touches a public "
           "interface, security, a data contract or the core requirement. "
           "Reviewer: set each disposition to accepted, amended or reverted.",
           ""]
    for record in records:
        alts = "; ".join(f"{a['option']} (not taken: {a['why_not']})"
                         for a in record["alternatives"])
        out += [
            f"### {record['id']}: {record['call']}",
            f"- **Stage:** {record['stage']}",
            f"- **Rationale:** {record['rationale']}",
            f"- **Alternatives:** {alts}",
            f"- **Affected behavior:** {record['affected_behavior']}",
            f"- **Verification:** {record['verification']}",
            f"- **Reversal:** {record['reversal']}",
            disposition_line(record),
        ]
        note = note_line(record)
        if note is not None:
            out.append(note)
        out += [
            "",
            "```pending-judgment",
            dump(record),
            "```",
            "",
        ]
    return "\n".join(out)


def fenced_blocks(body: str) -> list:
    """[(fence_start, payload_start, payload_end, record)] per parseable block.

    Coordinates are into the body with CRLF read as LF, the same reading
    `records_in` uses. Blocks that do not parse are skipped here: callers
    that need them counted use `records_in` first.
    """
    text = (body or "").replace("\r\n", "\n")
    out = []
    for match in _FENCE.finditer(text):
        try:
            record = json.loads(match.group(1))
        except ValueError:
            continue
        out.append((match.start(), match.start(1), match.end(1), record))
    return out


_DISPOSITION_LINE = re.compile(r"^- \*\*Reviewer disposition:\*\* .*$",
                              re.MULTILINE)
_NOTE_LINE = re.compile(r"^- \*\*Reviewer note:\*\* .*$", re.MULTILINE)
_HEADER = re.compile(r"^###\s+(.*?)\s*$", re.MULTILINE)


def apply_to_body(body: str, updated) -> tuple:
    """(new_body, warnings) with `updated`'s block and prose refreshed.

    The caller has already settled WHICH record this is (exactly one record
    carries its id): this only performs the swap. The fenced payload becomes
    `dump(updated)` and the section's disposition prose line is rewritten to
    match; when `updated` carries a reviewer note its prose line is refreshed
    too, or inserted after the disposition line. Everything else in the body
    is left untouched. The JSON block is the source of truth: when the prose
    around it cannot be matched confidently the block is still swapped and
    the doubt lands in warnings, never in a refusal.

    Prose is bound to the record's own section: the nearest `###` header
    above the block must be exactly this record's (`### <id>` or
    `### <id>: ...`, the shapes `render` writes — never a longer id that
    merely starts with it), the disposition line must be the single such
    line between that header and the block with no other block opening in
    between, and a note edit must not cross a header, a block opening, or
    another disposition line. Anything less certain leaves the prose alone
    with a warning rather than rewriting a neighbour's verdict.
    """
    rid = updated.get("id") if isinstance(updated, dict) else None
    text = (body or "").replace("\r\n", "\n")
    hits = [(f, ps, pe, r) for f, ps, pe, r in fenced_blocks(text)
            if isinstance(r, dict) and r.get("id") == rid]
    if len(hits) != 1:
        return text, [f"expected one fenced block for id {rid}, found "
                      f"{len(hits)}: body left unchanged"]
    fence_start, payload_start, payload_end, _old = hits[0]
    prefix = text[:fence_start]
    warnings = []

    def prose_alone(reason):
        warnings.append(reason)
        return (prefix + text[fence_start:payload_start] + dump(updated)
                + text[payload_end:], warnings)

    headers = list(_HEADER.finditer(prefix))
    if not headers:
        return prose_alone(
            f"no `###` header found for id {rid}: the fenced block was "
            f"updated, the prose was left alone")
    header = headers[-1]
    rest = header.group(1)
    if not (isinstance(rid, str) and (rest == rid
                                      or rest.startswith(rid + ":"))):
        return prose_alone(
            f"the nearest header above id {rid}'s block is not its "
            f"`### {rid}` header: the fenced block was updated, the prose "
            f"was left alone")
    section = prefix[header.end():]
    if _OPENING.search(section):
        return prose_alone(
            f"another block opens between id {rid}'s header and its block: "
            f"the fenced block was updated, the prose was left alone")
    disp_matches = list(_DISPOSITION_LINE.finditer(section))
    if len(disp_matches) != 1:
        found = "no" if not disp_matches else "more than one"
        return prose_alone(
            f"{found} `{DISPOSITION_PREFIX}` prose line found for id {rid}: "
            f"the fenced block was updated, the prose was left alone")
    disp = disp_matches[0]
    disp_start, disp_end = header.end() + disp.start(), header.end() + disp.end()
    gap = prefix[disp_end:]
    if (_HEADER.search(gap) or _OPENING.search(gap)
            or _DISPOSITION_LINE.search(gap)):
        return prose_alone(
            f"another section starts between id {rid}'s disposition line "
            f"and its block: the fenced block was updated, the prose was "
            f"left alone")

    new_disp = disposition_line(updated)
    prefix = prefix[:disp_start] + new_disp + prefix[disp_end:]
    if "reviewer_note" in updated and note_line(updated) is not None:
        note_matches = list(_NOTE_LINE.finditer(gap))
        new_note = note_line(updated)
        if len(note_matches) > 1:
            warnings.append(
                f"more than one `{NOTE_PREFIX}` prose line found for id "
                f"{rid}: the fenced block and the disposition prose were "
                f"updated, the note prose was left alone")
        elif len(note_matches) == 1:
            abs_start = disp_start + len(new_disp) + note_matches[0].start()
            abs_end = disp_start + len(new_disp) + note_matches[0].end()
            prefix = prefix[:abs_start] + new_note + prefix[abs_end:]
        else:
            at = disp_start + len(new_disp)
            prefix = prefix[:at] + "\n" + new_note + prefix[at:]

    return (prefix + text[fence_start:payload_start] + dump(updated)
            + text[payload_end:], warnings)


def records_in(body: str) -> tuple:
    """(records, unparseable block count) from a PR body's fenced blocks.

    A block that opens but does not parse counts as broken, including one
    _FENCE never matched: a record that silently vanished from the count
    would let the rest of the body pass. GitHub returns edited bodies with
    CRLF line ends, so those are read as LF.
    """
    text = (body or "").replace("\r\n", "\n")
    records, broken = [], 0
    blocks = _FENCE.findall(text)
    for block in blocks:
        try:
            records.append(json.loads(block))
        except ValueError:
            broken += 1
    broken += max(0, len(_OPENING.findall(text)) - len(blocks))
    return records, broken

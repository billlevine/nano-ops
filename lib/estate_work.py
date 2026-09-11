"""One composed read model over the estate's work schema (t-135).

WHY THIS EXISTS
---------------
`/estate.html` could already list tasks, and that is where it stopped. Its
"Follow-ups" panel actually held every task kind; the follow-up compatibility
envelope inside `refs` was never decoded; projects were absent from the
snapshot entirely; and of the four dependency kinds only the derived unresolved
`blocks` ids reached the page — as bare ids, with no titles and no outbound
view. t-124's investigation named the fix: not a second store and not a schema
migration, but the ONE composed read the schema was always able to answer and
nobody had written.

So this module is a pure read layer. It takes a read-only connection and
returns normalized projects, tasks, edges, decoded follow-ups, derived
attention and project rollups. It writes nothing, and it holds nothing: every
derived field is recomputed on the read that renders it, which is the same
reason `blocked_by` was derived in the first place (lib/estate_deps.py) and the
same reason `due_state` is (lib/estate_attention.py). A cached rollup is a
second copy of the truth, and the copy is the one that goes stale.

WHAT IS DERIVED HERE, AND FROM WHOSE RULE
-----------------------------------------
Nothing in this module invents a rule. Each derived field restates one that
already exists somewhere else in the estate, by calling it:

  blocked_by     lib/estate_deps.blocked_by — `estate ready`'s own rule, a task
                 B with an unresolved `blocks` edge into it.
  blocks         the exact TRANSPOSE of that map, never a second query. If A
                 appears in B's `blocked_by`, then B appears in A's `blocks`,
                 by construction — the two views cannot disagree about one
                 edge, because they are one dict read in two directions.
  due_state      lib/estate_attention.classify — the five states plus
                 `unreadable`, under one explicit notice interval.
  attention      lib/estate_attention.is_attention — an open follow-up, or a
                 task parked at `needs-owner`. Docs: docs/attention-contract.md.
  followup       the `refs` envelope `bin/followups` writes and reads.
  decision_      the question, the recommendation and the one-line context a
  surface        collapsed row must show (t-476). One rule, two renderers: the
                 CLI's grouped review view imports `decision_surface()` and the
                 dashboard reads the key it puts on every task, so neither can
                 decide on its own which `refs` key is "the question".
  tier           lib/estate_attention.tier — which of the three kinds of
                 waiting one attention row is (t-721). Derived from the same
                 `status`/`kind`/`stage` the set itself is derived from.
  derived_pairs  lib/estate_pairs — which proposal an attention row's exit
                 follow-up derives from, and whether the pair collapsed into
                 one row or stayed two (t-1061). Only on attention rows, and
                 only where a link was recorded at filing time.
  idle_days      lib/estate_attention.idle_days, compared only against its own
                 tier's median. Age is neglect, not importance.
  decision_      whether a row DECLARED a classification and, for a decision,
  state          whether it carries the whole sketch. A reading of what is on
                 file, never a repair of it — the four words are in
                 docs/decision-classification-contract.md's vocabulary and
                 `unstated` is an answer.

A TERMINAL TASK HAS NO DUE STATE
--------------------------------
`due_state` is None for a `done` or `dropped` row, rather than `overdue`. The
deadline on a finished task is history: classifying it would put resolved work
in the same bucket as a missed one, and the attention contract is explicit that
a resolved follow-up is history rather than a demand. Non-terminal rows carry
one of the six states, always — including `undated`, which is an answer.

UNPROJECTED WORK IS A GROUPING, NOT A PROJECT
---------------------------------------------
`project_id IS NULL` is most of this store, and it needs to be visible or the
Projects section reads as though the estate had almost no work in it. It is
emitted as one synthetic bucket with `unprojected: true` and the same rollup
shape as a real project, so a renderer needs no special case. It is NOT a row
in `projects` and this module will never make it one — §4.7 of the
investigation is explicit that the synthetic grouping belongs in the read
model, not in SQLite.

DEGRADES, NEVER RAISES
----------------------
The dashboard reads stores it cannot migrate. A missing `projects` table, a
missing `task_deps` table, a store predating the `due_at` or `stage` column:
each one means the corresponding section has nothing to show, which is a
different statement from an error page. Same rule estate_deps already takes —
a derived view must never take down the panel that carries it.
"""
from __future__ import annotations

import datetime as dt
import json

import estate_attention
import estate_pairs
import estate_decisions
import estate_deps

# A task in one of these is finished. Same tuple as bin/estate's TERMINAL and
# estate_deps' — read through estate_attention so there is one spelling here.
TERMINAL = estate_attention.TERMINAL

# The one edge kind whose meaning this module derives anything from. The other
# three (`parent`, `related`, `discovered-from`) are carried through exactly as
# stored: `dependencies()` deliberately does NOT filter to a known vocabulary,
# because a kind this module has not heard of is still an edge somebody wrote,
# and silently dropping it would make the "all relationships" view a lie.
BLOCKS = "blocks"

FOLLOWUP_KIND = "followup"
# There is deliberately no PROPOSAL_KIND here. The review lifecycle is keyed on
# the `stage` COLUMN and never on `kind` — `estate proposal add --adopt-task`
# stages an existing task "keeping its own kind and history", so a followup, an
# inbox-message or anything else can be carrying a live review decision. A
# constant named after the kind used to sit on this line, read by nothing, and
# it was read BY A PERSON as though it were the panel's filter: t-660 was filed
# and dispatched against a kind=proposal filter that has never existed anywhere
# in this file. Adding one back would be the narrowing this comment exists to
# prevent — `proposal_view()` below is where the rule lives, and it asks for a
# stage.
# t-296. One task per inbox message the hub mirrored, kind and refs written by
# `bin/hub-intake message open`. Decoded here for the same reason the follow-up
# envelope is: the identity of a tracked message is (channel, message_ts), and
# a renderer should not have to know where in `refs` that lives.
MESSAGE_KIND = "inbox-message"
MESSAGE_FIELDS = ("message_id", "channel", "message_ts", "thread_ts", "inbox")
PROPOSAL_STAGES = ("pending-review", "approved-backlog", "rejected",
                   "resolved", "stopped")
PROPOSAL_FIELDS = ("fingerprint", "condition", "desired_outcome",
                   "completion_check", "classification", "question",
                   "alternatives", "recommendation", "defer_consequence")

# t-476. WHICH STORED FIELD IS "THE QUESTION", AND WHICH IS "THE RECOMMENDATION"
#
# A collapsed row has to carry the decision itself, and no writer in this estate
# has ever stored a field called `question`. What it stores is `condition` — the
# observation the proposal was filed about — and `desired_outcome`, which the
# review grid has always labelled "Recommendation". So the answer is a
# PRECEDENCE, not a rename: an explicit key wins if some future writer records
# one, the field that exists today answers otherwise, and the row says which one
# it used so a reader is never shown an observation while being told it is a
# question. Absent means absent: nothing here falls back to the title, because a
# title paraphrased into a question is a question this estate never asked.
#
# Ordered most-explicit-first. Adding a key here is how a new schema takes over
# a surface, and it takes over both renderers at once.
QUESTION_FIELDS = ("question", "condition")
RECOMMENDATION_FIELDS = ("recommendation", "desired_outcome")
# The one line of a follow-up that says why it is waiting. `context` is the
# envelope's own field; `intent` is the task column a `needs-owner` row that is
# not a follow-up carries instead, and it is the same kind of sentence.
CONTEXT_FIELDS = ("context", "intent")

# The follow-up compatibility envelope, exactly as `bin/followups` writes it
# into `refs`. Decoded once here so no reader has to know the envelope's shape
# (§4.6). `attention_key` is what `followups attention` links on when a sweep
# runs again, so it is part of the identity even though the legacy tool never
# printed it.
FOLLOWUP_FIELDS = ("legacy_id", "source", "ref", "context", "resolution",
                   "attention_key", estate_pairs.DERIVES_FROM)

# The field each surface is ALREADY understood to mean, so a renderer knows
# when to name its source and when to keep quiet. `desired_outcome` has been
# labelled "Recommendation" in the review grid since proposals existed, so
# saying "from desired_outcome" on every row would be noise. `condition` under
# "Question" is not noise: an observation is not a question, and a reader
# deciding on it should know the difference.
SURFACE_CANONICAL = {"question": "question",
                     "recommendation": "desired_outcome",
                     "context": "context"}

# The id of the synthetic "work that belongs to no project" bucket. Not a
# project id — `estate project add` mints `p-N`, so this can never collide.
UNPROJECTED = "unprojected"


# ── small helpers ────────────────────────────────────────────────────────────

def has_table(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def row_get(row, key: str):
    """Column value, or None on a store that predates the column."""
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(key)
    return row[key] if key in keys else None


def decode_refs(value) -> dict:
    """The `refs` JSON blob as a dict; {} for anything else.

    A blob that will not parse is not an error here. It is one task's private
    envelope, and losing the page over it would be the derived-view rule
    inverted.
    """
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def followup_view(task: dict) -> dict | None:
    """The decoded follow-up envelope, or None for a task of another kind.

    `state` is "open" while the task is NON-TERMINAL, which is deliberately not
    `bin/followups`' binary (that shim calls anything but literal `open`
    "resolved", because the legacy file format had no other word). The
    attention contract is the authority for what an open follow-up is, and a
    `claimed` follow-up is plainly not resolved.
    """
    if (task.get("kind") or "") != FOLLOWUP_KIND:
        return None
    refs = decode_refs(task.get("refs"))
    out = {field: refs.get(field) for field in FOLLOWUP_FIELDS}
    out["state"] = "resolved" if (task.get("status") or "") in TERMINAL else "open"
    out["resolved_at"] = task.get("closed_at")
    return out


def proposal_view(task: dict) -> dict | None:
    """Normalized review record carried by a staged task, or None if unstaged.

    THE STAGE IS THE MEMBERSHIP TEST, AND THE KIND IS NOT (t-660). A task
    enters the review lifecycle by gaining a `stage`, which
    `estate proposal add --adopt-task` grants in place without touching `kind`.
    So `kind` is not consulted here and must not become a second condition: an
    adopted follow-up carrying a real decision would drop off the proposals
    panel the moment it did, which is the invisibility t-660 was filed about.
    """
    stage = task.get("stage")
    if not stage:
        return None
    refs = decode_refs(task.get("refs"))
    out = {field: refs.get(field) for field in PROPOSAL_FIELDS}
    out["stage"] = stage
    out["legacy"] = any(not str(out.get(field) or "").strip()
                        for field in ("condition", "desired_outcome",
                                      "completion_check"))
    out["recurrences"] = 0
    out["last_recurrence_at"] = None
    # t-476. Which key answers "the question" and which answers "the
    # recommendation", resolved once here so the collapsed row does not decide.
    # The NAME always ships; the TEXT only when the answering key is not
    # already in this envelope, because `condition` and `desired_outcome` are
    # right there and shipping either twice is the duplication §4.6 refused for
    # a general decoded-refs blob. A renderer reads `question` if present and
    # `out[question_field]` otherwise.
    surface = decision_surface(task)
    for name in ("question", "recommendation"):
        field = surface[f"{name}_field"]
        out[f"{name}_field"] = field
        if field is not None and field not in PROPOSAL_FIELDS:
            out[name] = surface[name]
    return out


# ── t-476: the decision a collapsed row has to carry ─────────────────────────

def _first_present(source, fields) -> tuple[str | None, str | None]:
    """(text, which field it came from), or (None, None) if none is recorded.

    Whitespace-only is not recorded. A field present and blank is the same
    absence as a field that was never written, and treating them differently
    would put an empty box on the page under a confident label.
    """
    for field in fields:
        value = source.get(field)
        text = str(value).strip() if value is not None else ""
        if text:
            return text, field
    return None, None


def first_line(text: str | None) -> str | None:
    """The first non-empty line of a block, or None.

    A LINE, which is all this ever claimed to be — and on this store most
    context blobs contain no newline at all, so on its own it returns the whole
    500-character paragraph. `one_line()` below is what a summary actually
    needs; this stays because "the first line" is still the first step of it.
    """
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if line.strip():
            return line.strip()
    return None


# t-721. HOW LONG A SUMMARY LINE MAY BE, AND WHY THERE ARE TWO NUMBERS
#
# Measured against a real set, most titles run well past a line and the long
# tail runs to hundreds of characters — and the "one-line" context strip that
# was supposed to clarify a long title is LONGER than the title on most rows,
# because `first_line()` cuts at a newline and most blobs have none. The
# clarifier ends up several times longer than the thing it clarifies.
#
# So the headline is the shorter of the two: it is the row's identity and it
# shares a line with the badges. The context line is allowed more room because
# it is the sentence that says WHY, and it sits on its own line under it.
# Neither number is a display detail the browser could pick on its own — the
# CLI's grouped review renders the same rows, and two clamps drift.
HEADLINE_MAX = 80
CONTEXT_LINE_MAX = 120
# What a clamped string ends in. One character, so a clamp never costs a reader
# three characters of the text they were trying to read.
ELLIPSIS = "…"


def clamp(text: str | None, limit: int) -> tuple:
    """(text clamped to `limit` characters, whether it had to be), or (None,
    False) for nothing at all.

    Cuts on a word boundary when one falls in the last third of the budget, so
    a clamp reads as a sentence that stops rather than one chopped mid-token,
    and falls back to a hard cut when it does not — a single 200-character
    token is still better shown than dropped.

    It reports the clamp rather than hiding it. A renderer that cannot say "the
    stored text is longer than this" has silently made the store's own content
    unquotable, which is the bug F7 found wearing different clothes.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, False
    # Internal newlines and runs of whitespace collapse: this is one line by
    # construction, and a tab inside it would render as a gap nobody wrote.
    raw = " ".join(raw.split())
    if len(raw) <= limit:
        return raw, False
    budget = max(1, limit - len(ELLIPSIS))
    cut = raw[:budget]
    space = cut.rfind(" ")
    if space >= (budget * 2) // 3:
        cut = cut[:space]
    return cut.rstrip(" ,;:—-") + ELLIPSIS, True


def one_line(text: str | None, limit: int = CONTEXT_LINE_MAX) -> tuple:
    """The first line of a block, clamped. (text, was_clamped).

    The two steps are separate because they answer different questions: the
    first line is the store's own idea of where the summary ends, and the clamp
    is this estate's idea of how much of it fits. A blob with no newline has no
    answer to the first, which is exactly when the second has to hold.
    """
    return clamp(first_line(text), limit)


# t-721. WHAT A ROW SAYS ABOUT ITS OWN DECISION, IN FOUR WORDS
#
# The contract (docs/decision-classification-contract.md) says what a filing
# must declare. This says what a filing already on file DID declare — a
# reading, never a repair. Most rows on a real set carry no classification at
# all, and nothing here invents one for them: `unstated` is the answer, and it
# is the answer a renderer must print out loud rather than leaving a blank
# where a decision should be.
#
#   sketched    a `decision` carrying the complete sketch — the question, at
#               least two alternatives, a recommendation key, and what happens
#               if the owner defers.
#   incomplete  a `decision` missing at least one of those. Filed through a
#               door that did not enforce them (`estate proposal add` accepts
#               without requiring, deliberately), or amended since.
#   stated      an `action` or a `clarification`. Complete by definition — the
#               contract refuses the sketch keys on both, so a missing question
#               here is the schema working, not a hole.
#   unstated    no classification recorded. Nothing is inferred.
SKETCHED, INCOMPLETE, STATED, UNSTATED = (
    "sketched", "incomplete", "stated", "unstated")


def decision_state(refs: dict) -> tuple:
    """(one of the four words, the sketch keys that are missing).

    The missing list is empty for every state but `incomplete`, and it names
    the estate's own key names so a badge can say WHICH half is absent instead
    of only that something is.
    """
    classification = str(refs.get(estate_decisions.CLASSIFICATION_KEY)
                         or "").strip().lower()
    if not classification:
        return UNSTATED, []
    if classification != estate_decisions.DECISION:
        return STATED, []
    missing = []
    for key in estate_decisions.DECISION_ONLY_KEYS:
        value = refs.get(key)
        if key == estate_decisions.ALTERNATIVES_KEY:
            options = [v for v in (value or [])
                       if isinstance(v, dict) and v.get(
                           estate_decisions.OPTION_KEY)]
            if len(options) < estate_decisions.MIN_ALTERNATIVES:
                missing.append(key)
            continue
        if not str(value or "").strip():
            missing.append(key)
    return (INCOMPLETE, missing) if missing else (SKETCHED, [])


# t-1742. IS THIS ROW READY TO PUT IN FRONT OF THE OWNER?
#
# A proposal-body audit sampled the unresolved rows in depth and found some
# decidable exactly as stored and others that needed upstream work before a
# decision could be asked for at all. Its recommendation was a
# "pre-presentation readiness filter (real recommendation, current context,
# nonterminal task)", and until now nothing derived it: `estate awaiting` had
# no such flag and /estate.html had no such badge, so a row the owner could
# decide in ten seconds and one nobody has finished thinking about sat in the
# same list looking identical.
#
# The condition an installation will find, measured over its own open
# `proposal`+`followup` rows: a minority carry a real recommendation, a larger
# group carry the literal sentinel `not recorded`, and most have no
# `recommendation` key at all.
#
# THIS JUDGES SHAPE, NOT QUALITY. It says whether the RECORD contains the
# things a decision is made out of — it cannot say whether the recommendation
# is any good, and it must never be read as saying so. That is the whole reason
# `--ready-only` is a flag and the badge is a badge: t-721 refused a hard cap
# on the attention set because it would have hidden nearly the whole set, and
# a shape test silently promoted to the default view would hide rows on a
# judgment it is not making.
#
#   reach         a real recommendation, a situation sentence on file, and
#                 neither the task nor its review stage finished. Decidable as
#                 stored.
#   thinking      recorded, but not decidable yet — the recommendation is the
#                 sentinel `not recorded`, or the record has been overtaken by
#                 its own lifecycle.
#   unclassified  no recommendation recorded at all. Not a judgment about the
#                 work: nothing was filed to judge.
REACH, THINKING, UNCLASSIFIED = "reach", "thinking", "unclassified"
READINESS_STATES = (REACH, THINKING, UNCLASSIFIED)

# WHICH KEY IS "THE RECOMMENDATION" HERE, AND WHY IT IS NOT THE DISPLAY
# PRECEDENCE ABOVE
#
# `RECOMMENDATION_FIELDS` is a rendering rule: it falls back to
# `desired_outcome` because the review grid has labelled that field
# "Recommendation" since proposals existed, and a row showing one under that
# heading is showing the reader something real. Readiness is a different
# question — did a filer, having weighed the alternatives, record a pick — and
# only `recommendation` answers it. `desired_outcome` is what the proposal
# wants to be true afterwards, which nearly every proposal has; falling back
# to it would move a large share of the open rows into `reach` on the strength
# of a field nobody filed as a pick — several times the ready set, made of
# records whose own recommendation key is
# absent or says "not recorded". So this reads the contract's own key
# (docs/decision-classification-contract.md) and nothing else.
RECOMMENDATION_KEY = estate_decisions.RECOMMENDATION_KEY
NOT_RECORDED = estate_decisions.NOT_RECORDED

# THE SITUATION SENTENCE, and why this list is not `CONTEXT_FIELDS`
#
# `CONTEXT_FIELDS` is the display precedence for the one line printed under a
# headline, and it deliberately stops at `context`/`intent`: a proposal's
# `condition` is already surfaced as its QUESTION when no explicit `question`
# was filed (see QUESTION_FIELDS), and listing it twice would print one
# sentence under two labels. Readiness asks the record a different question —
# does it say what it is about at all — and the two record shapes answer it
# with different keys: a follow-up writes `context`, a proposal writes
# `condition`, and `intent` is the task column a row carries instead of either.
# Reading only the display list would have called t-395 unready, and the audit
# judged t-395 reach.
SITUATION_FIELDS = ("context", "condition", "intent")

# A review that has ENDED. The record is then an answer on file, not a request:
# t-460 is the audit's own case — a follow-up whose displayed question still
# asked how a change should publish, months after the owner authorised it and
# it merged. `pending-review` and `approved-backlog` are live and are not here.
TERMINAL_STAGES = ("rejected", "resolved", "stopped")

# The exact words each verdict is reached by, so a badge and a CLI line can say
# WHICH clause failed rather than only that one did. Keys of the store, not
# prose: a reader who sees `recommendation_not_recorded` knows which field to
# open. Order of evaluation is the order below.
NO_RECOMMENDATION = "no_recommendation_key"
EMPTY_RECOMMENDATION = "recommendation_empty"
SENTINEL_RECOMMENDATION = "recommendation_not_recorded"
NO_SITUATION = "no_situation_recorded"
TASK_TERMINAL = "task_terminal"
STAGE_TERMINAL = "stage_terminal"


def is_not_recorded(text: str) -> bool:
    """Whether a stored recommendation is the "I did not pick" sentinel.

    THE MATCH IS A PREFIX, AND THAT IS A READING OF THE STORE RATHER THAN OF
    THE CONTRACT. `estate_decisions` normalizes an exact `not recorded` on
    write and says nothing about anything longer, so an equality test is what
    the writer guarantees. What filers actually write is the sentinel with the
    reason attached — e.g. "not recorded -- the risk this named is real and
    worth a second opinion before picking a shape". That is a better record
    than the bare sentinel and it is emphatically not a pick, so reading it as
    one would put those rows in front of the owner under a badge saying they
    can be decided as stored, when the record's first two words say nobody
    did.

    The boundary keeps it honest: the sentinel must END there, so a
    recommendation whose own first word merely begins with it is untouched.
    The falsifier is a real recommendation that opens with the literal phrase
    "not recorded" followed by punctuation — there is none on this store, and
    one would be a filer contradicting themselves in the same field.
    """
    low = str(text or "").strip().lower()
    if not low.startswith(NOT_RECORDED):
        return False
    return not low[len(NOT_RECORDED):].lstrip()[:1].isalnum()


def readiness(task: dict) -> tuple:
    """(one of the three words, the reasons it is not `reach`).

    Pure, and it needs only the row: `refs`, `status`, `stage` and the `intent`
    column. No connection, no second query, no dependency lookup — so the CLI's
    grouped review and the dashboard snapshot run the one implementation rather
    than two that drift, which is the same reason the t-721 clamps live here.

    The reason list is empty for `reach` and names every failed clause
    otherwise, so a row that is thinking for two reasons says both.

    WHAT IT CANNOT SEE, said out loud because a filter that hides rows owes its
    reader the boundary:

      * whether a recommendation is any GOOD. It is text, and this counts it.
      * whether the situation described is still TRUE. `t-401` carries
        "CONFLICTS WITH t-135 ... Read them together" in its own body and
        nothing in the store records that as a relation; a reader must still
        read it. The lifecycle is the only currency signal the store actually
        carries, and it is the only one used here.
      * AGE. A row untouched for a month is a row nobody has looked at, which
        is a fact about attention and not about the record's contents — t-721
        already settled that age indicates neglect rather than importance, and
        an age threshold here would demote correct rows for being old.
    """
    refs = decode_refs(task.get("refs"))
    why: list[str] = []

    if RECOMMENDATION_KEY not in refs:
        # The absence rule, read back. An absent key says nothing at all, so
        # nothing is concluded about the work — the row is unclassified and
        # `estate proposal amend --classification` is the door that changes it.
        return UNCLASSIFIED, [NO_RECOMMENDATION]
    text = str(refs.get(RECOMMENDATION_KEY) or "").strip()
    if not text:
        # "A field present and blank is the same absence as a field that was
        # never written" — `_first_present` above, and the contract's own
        # "an empty value is an absence wearing a key". Same bucket, different
        # word for it, because the two are worth telling apart when somebody
        # goes to fix them.
        return UNCLASSIFIED, [EMPTY_RECOMMENDATION]
    if is_not_recorded(text):
        why.append(SENTINEL_RECOMMENDATION)

    situation, _ = _first_present(
        dict({"intent": task.get("intent")}, **refs), SITUATION_FIELDS)
    if not situation:
        why.append(NO_SITUATION)

    if (task.get("status") or "") in TERMINAL:
        why.append(TASK_TERMINAL)
    if (task.get("stage") or "") in TERMINAL_STAGES:
        why.append(STAGE_TERMINAL)

    return (REACH, []) if not why else (THINKING, why)


def decision_surface(task: dict) -> dict:
    """What a COLLAPSED row must show: the question, the recommendation, the
    one-line context — each with the field it actually came from (t-476).

    Every value is either a real stored string or None, and `*_field` names
    which key answered. A renderer showing None must say so out loud: an absent
    question rendered as an empty cell reads like "no question", and the whole
    point of putting the decision in the collapsed row is that a reader can
    trust what is in it. Nothing here paraphrases and nothing falls back to a
    title: `question` is the stored question or None.

    IT DOES CLAMP NOW, AND THAT IS A CHANGED POSITION (t-721). This function
    used to say clamping was a rendering choice belonging to whichever surface
    was rendering. Two surfaces render these rows, neither clamped, and the
    result was a "one-line" context strip with a median of 500 characters
    sitting under a title with a median of 131. The clamped values are ADDED
    keys — `context_line`, `headline`, and a boolean beside each saying it was
    cut — and the full `context`, `question` and `recommendation` are untouched
    beside them. A surface that wants the whole string still has it; what it no
    longer has is its own private idea of how long a summary line is.

    Defined once so the CLI's grouped review view and the dashboard's row
    cannot disagree; the dashboard gets it as a snapshot key rather than
    re-deriving it in JavaScript, which is this module's standing rule.
    """
    refs = decode_refs(task.get("refs"))
    question, question_field = _first_present(refs, QUESTION_FIELDS)
    recommendation, rec_field = _first_present(refs, RECOMMENDATION_FIELDS)
    # `intent` is a column, not a refs key, so the context lookup reads a merged
    # mapping — refs first, because an envelope written for this task beats the
    # generic column.
    context, context_field = _first_present(
        dict({"intent": task.get("intent")}, **refs), CONTEXT_FIELDS)
    context_line, context_clamped = one_line(context)
    headline, headline_clamped = clamp(task.get("title"), HEADLINE_MAX)
    state, missing = decision_state(refs)
    ready, ready_why = readiness(task)
    return {
        "question": question, "question_field": question_field,
        "recommendation": recommendation, "recommendation_field": rec_field,
        "context": context, "context_field": context_field,
        # Clamped, and it says so. See HOW LONG A SUMMARY LINE MAY BE: the
        # unclamped version of this key was the panel's F7 — a "one-line"
        # clarifier running a median of 500 characters.
        "context_line": context_line,
        "context_line_clamped": context_clamped,
        # The row's own title, cut to something a collapsed row can hold. The
        # full title still ships on the row itself; this never replaces it, and
        # nothing downstream may treat the headline as the task's name.
        "headline": headline, "headline_clamped": headline_clamped,
        "decision_state": state, "decision_missing": missing,
        "classification": refs.get(estate_decisions.CLASSIFICATION_KEY),
        # t-1742. Whether this row is ready to be PRESENTED — a reading of the
        # record's shape, never of its quality. It rides the surface rather
        # than each caller's own call so `estate awaiting --ready-only` and
        # /estate.html's badge cannot disagree about one row.
        "readiness": ready, "readiness_why": ready_why,
    }


def message_view(task: dict) -> dict | None:
    """The decoded tracked-message envelope, or None for another kind.

    `open` is the same reading the rest of this module uses: NON-TERMINAL. A
    message at `ready` was registered and answered-or-not; one at `claimed` is
    with a dispatched worker; one at `needs-owner` is already in the attention
    set. All three are conversations nobody has finished, which is exactly what
    the panel that renders this is for (docs/inbox-message-contract.md).
    """
    if (task.get("kind") or "") != MESSAGE_KIND:
        return None
    refs = decode_refs(task.get("refs"))
    out = {field: refs.get(field) for field in MESSAGE_FIELDS}
    out["threaded"] = bool(refs.get("thread_ts"))
    out["open"] = (task.get("status") or "") not in TERMINAL
    return out


def transpose(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    """{blocked -> [blockers]} read the other way: {blocker -> [it holds up]}.

    Order is the input's, which is `estate_deps.blocked_by`'s creation order,
    so both directions of one edge set are stable and sorted the same way.
    """
    out: dict[str, list[str]] = {}
    for target, sources in mapping.items():
        for source in sources:
            out.setdefault(source, []).append(target)
    return out


# ── dependencies ─────────────────────────────────────────────────────────────

def dependencies(conn, titles: dict[str, dict] | None = None) -> list[dict]:
    """Every edge in `task_deps`, with both endpoints resolved.

    The stored fact is the edge; `blocked_by` and `blocks` are two readings of
    the `blocks` subset of it (§4.3). Emitting the edges once and deriving the
    directional views from them is why neither view needs its own array on
    every task row.

    `active` answers "is this edge currently holding anything up" and is
    meaningful ONLY for `blocks`: it is None for `parent`, `related` and
    `discovered-from`, because those describe provenance and hierarchy, which
    no task status resolves. For `blocks` it is exactly estate_deps' rule —
    the SOURCE task is not yet terminal.
    """
    if not estate_deps.has_deps_table(conn):
        return []
    index = titles or {}
    out = []
    for row in conn.execute(
            "SELECT from_task, to_task, dep_kind, created_at FROM task_deps "
            "ORDER BY created_at, from_task, to_task"):
        source = index.get(row["from_task"]) or {}
        target = index.get(row["to_task"]) or {}
        kind = row["dep_kind"]
        active = None
        if kind == BLOCKS:
            # An endpoint we cannot see is not evidence the edge is dead.
            active = (source.get("status") or "") not in TERMINAL
        out.append({
            "from_task": row["from_task"],
            "from_title": source.get("title"),
            "from_status": source.get("status"),
            "to_task": row["to_task"],
            "to_title": target.get("title"),
            "to_status": target.get("status"),
            "kind": kind,
            "created_at": row["created_at"],
            "active": active,
        })
    return out


# ── projects ─────────────────────────────────────────────────────────────────

def _empty_rollup() -> dict:
    return {"tasks": 0, "open": 0, "terminal": 0, "blocked": 0, "attention": 0,
            "overdue": 0, "due_soon": 0, "by_status": {}, "by_kind": {},
            "task_ids": [], "last_activity": None}


def _accumulate(rollup: dict, task: dict) -> None:
    status, kind = task.get("status") or "", task.get("kind") or ""
    rollup["tasks"] += 1
    rollup["task_ids"].append(task["id"])
    rollup["by_status"][status] = rollup["by_status"].get(status, 0) + 1
    rollup["by_kind"][kind] = rollup["by_kind"].get(kind, 0) + 1
    if status in TERMINAL:
        rollup["terminal"] += 1
    else:
        rollup["open"] += 1
    if task.get("blocked_by"):
        rollup["blocked"] += 1
    if task.get("is_attention"):
        rollup["attention"] += 1
    state = task.get("due_state")
    if state == estate_attention.OVERDUE:
        rollup["overdue"] += 1
    elif state in (estate_attention.DUE_TODAY, estate_attention.DUE_SOON):
        rollup["due_soon"] += 1
    updated = task.get("updated_at")
    if updated and (rollup["last_activity"] is None
                    or str(updated) > str(rollup["last_activity"])):
        rollup["last_activity"] = updated


def projects(conn, tasks: list[dict]) -> list[dict]:
    """Every project, each carrying a rollup derived from the task rows.

    Counts are DERIVED on this read and never stored (§4.7): a counter column
    would have to be re-written every time a task moved, and the one time it
    was not is the time the page lies. Ordered active-first, then newest.
    """
    rollups: dict[str, dict] = {}
    for task in tasks:
        key = task.get("project_id") or UNPROJECTED
        _accumulate(rollups.setdefault(key, _empty_rollup()), task)

    rows = []
    if has_table(conn, "projects"):
        rows = list(conn.execute("SELECT * FROM projects ORDER BY seq"))

    out = []
    for row in rows:
        item = dict(row)
        item["meta"] = decode_refs(row_get(row, "meta"))
        item["unprojected"] = False
        item["rollup"] = rollups.pop(item["id"], _empty_rollup())
        out.append(item)
    out.sort(key=lambda p: ((p.get("status") or "") != "active",
                            -(p.get("seq") or 0)))

    # Anything left in `rollups` is a project_id with no row behind it plus the
    # synthetic bucket. A dangling id is reported rather than dropped — a task
    # pointing at a project nobody can find is exactly the kind of thing this
    # page exists to make visible.
    unprojected = rollups.pop(UNPROJECTED, None)
    for orphan_id in sorted(rollups):
        out.append({"id": orphan_id, "title": "(unknown project)",
                    "kind": "unknown", "status": "unknown", "missing": True,
                    "unprojected": False, "meta": {},
                    "rollup": rollups[orphan_id]})
    if unprojected is not None:
        out.append({"id": UNPROJECTED, "title": "Unprojected work",
                    "kind": "grouping", "status": "n/a", "unprojected": True,
                    "meta": {}, "rollup": unprojected})
    return out


# ── the attention set, by lifecycle tier (t-721) ─────────────────────────────

def tiers(ordered: list) -> list:
    """The attention set split into its three lifecycle tiers, each carrying
    the counts and the within-tier idle comparison a section header needs.

    `ordered` is the attention set in the contract's own order, and each tier
    keeps that order — the split is a grouping, not a re-sort. Ids again, on
    exactly the terms `attention` uses: the rows are already in `tasks`, and a
    second copy is a row that can disagree with itself.

    THE COUNTS ARE TWO NUMBERS AND ALWAYS BOTH (P4, resolved uncapped). `total`
    is every row in the tier and `notice` is how many of them are inside the
    advance-notice band. Neither is a limit: nothing here truncates a tier, and
    a renderer that shows fewer rows than `total` is responsible for saying so.
    A hard cap of five to seven was the alternative and it was refused, because
    a queue that hides its tail reports itself healthy.

    IDLE IS COMPARED WITHIN THE TIER AND NOWHERE ELSE. `idle_median` is over
    this tier's own rows, so "longer than half its tier" means something
    different in a tier that turns over in a day and one that has not moved in
    three weeks. Age is a fact about neglect, not about importance, and this is
    what keeps it from being read as a rank: no tier is ordered by it, and the
    set is never sorted by it at all.
    """
    grouped: dict[str, list] = {name: [] for name in estate_attention.TIERS}
    for task in ordered:
        # A row whose tier this module does not recognise still belongs to
        # somebody. It gets its own bucket rather than being dropped, on the
        # same rule `dependencies()` follows for an edge kind it has not heard
        # of — a row missing from the page is worse than one under an odd head.
        grouped.setdefault(task.get("tier") or "unclassified", []).append(task)
    out = []
    for name, rows in sorted(
            grouped.items(),
            key=lambda kv: estate_attention.TIER_ORDER.get(
                kv[0], len(estate_attention.TIERS))):
        if not rows:
            continue
        idles = [t.get("idle_days") for t in rows]
        middle = estate_attention.median(idles)
        for task in rows:
            idle = task.get("idle_days")
            # None when either number is missing: an unreadable `updated_at`
            # and a tier of one undated row are both "no comparison available",
            # and False would claim one was made.
            task["idle_above_tier_median"] = (
                None if idle is None or middle is None else idle > middle)
        out.append({
            "id": name,
            "title": estate_attention.TIER_TITLES.get(
                name, "Uncategorised — the store did not say"),
            "total": len(rows),
            "notice": sum(1 for t in rows if t.get("notice")),
            "idle_median": middle,
            "idle_max": max((v for v in idles if v is not None), default=None),
            "unreadable_idle": sum(1 for v in idles if v is None),
            "ids": [t["id"] for t in rows],
        })
    return out


# ── the composed read ────────────────────────────────────────────────────────

def build(conn, now: dt.datetime | None = None,
          notice: float = estate_attention.NOTICE_DEFAULT_DAYS,
          tz: dt.tzinfo | None = None) -> dict:
    """The whole work view: tasks, projects, edges, attention, follow-ups.

    One connection, a handful of whole-table reads, no N+1: the edge table and
    the project table are small, and the task table is read once and grouped in
    memory. `now` and `tz` are arguments so every due-state boundary is
    testable by moving the clock rather than waiting for it.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # Resolved ONCE for the whole read, never per row — a per-row lookup can
    # straddle a DST change and classify two rows under two calendars
    # (estate_attention.local_zone()'s note).
    zone = tz or estate_attention.local_zone()

    task_rows = list(conn.execute(
        "SELECT * FROM tasks ORDER BY updated_at DESC, seq DESC"))
    blockers = estate_deps.blocked_by(conn, {r["id"] for r in task_rows})
    blocking = transpose(blockers)
    tasks = estate_deps.annotate(task_rows, blockers)

    by_id = {}
    for task in tasks:
        status, kind = task.get("status") or "", task.get("kind") or ""
        terminal = status in TERMINAL
        due = task.get("due_at")
        task["blocks"] = blocking.get(task["id"], [])
        # See A TERMINAL TASK HAS NO DUE STATE. `days_left` is still computed:
        # it is a fact about the date, not a claim about urgency.
        task["due_state"] = (None if terminal
                             else estate_attention.classify(due, now, notice, zone))
        task["days_left"] = estate_attention.days_left(due, now)
        task["is_attention"] = estate_attention.is_attention(kind, status)
        # `followup` is the only decoded projection of `refs` that ships. The
        # raw blob is already on the row and a general `refs_decoded` beside it
        # is the same bytes twice — on this store that was 150kB of a 850kB
        # snapshot, for text every reader can parse from what it already has.
        # §4.6 asks for the FOLLOW-UP envelope to be normalized at the read
        # boundary, because that one has a contract nobody should re-derive.
        task["followup"] = followup_view(task)
        task["proposal"] = proposal_view(task)
        task["message"] = message_view(task)
        # t-476. The collapsed attention row shows why this is waiting, and one
        # LINE of it is all a summary can hold — so one line is all that ships.
        # Only for rows in the attention set: no other collapsed row asks for
        # it, and a field on every task is how a snapshot grows a copy of the
        # store. `context_field` names the source so an absent context can be
        # rendered as absent rather than as an empty cell.
        if task["is_attention"]:
            surface = decision_surface(task)
            task["context_line"] = surface["context_line"]
            task["context_field"] = surface["context_field"]
            task["context_line_clamped"] = surface["context_line_clamped"]
            # t-721. The row's BODY, not just its title. Few rows declare a
            # classification and fewer carry a full sketch, so most of these
            # are None — and None printed as "no decision stated" is the
            # finding. Nothing is invented for a row that declared nothing.
            task["headline"] = surface["headline"]
            task["headline_clamped"] = surface["headline_clamped"]
            task["question"] = surface["question"]
            task["question_field"] = surface["question_field"]
            task["recommendation"] = surface["recommendation"]
            task["recommendation_field"] = surface["recommendation_field"]
            task["decision_state"] = surface["decision_state"]
            task["decision_missing"] = surface["decision_missing"]
            task["classification"] = surface["classification"]
            task["tier"] = estate_attention.tier(kind, status,
                                                 task.get("stage"))
            task["idle_days"] = estate_attention.idle_days(
                task.get("updated_at"), now)
            task["notice"] = (task["due_state"]
                              in estate_attention.NOTICE_STATES)
        # t-1742. The readiness verdict rides every row a surface actually puts
        # in front of the owner, which is two lists and not one: the attention
        # set above, and the review queue — a row enters that by gaining a
        # `stage`, never by its kind (see `proposal_view`). Both panels of /estate.html
        # render this same key, so a proposal that is also an attention row
        # cannot carry two answers. Two small fields on a bounded subset, not a
        # field on every task in the store (t-1450).
        if task["is_attention"] or task.get("stage"):
            task["readiness"], task["readiness_why"] = readiness(task)
        by_id[task["id"]] = task

    # Titles resolved server-side (§ build scope step 7) so no consumer has to
    # linear-scan the task array to name a blocker.
    for task in tasks:
        task["blocked_by_titles"] = {
            b: (by_id.get(b) or {}).get("title") for b in task["blocked_by"]}
        task["blocks_titles"] = {
            b: (by_id.get(b) or {}).get("title") for b in task["blocks"]}

    edges = dependencies(conn, by_id)
    dep_counts: dict[str, int] = {}
    for edge in edges:
        dep_counts[edge["kind"]] = dep_counts.get(edge["kind"], 0) + 1

    project_rows = projects(conn, tasks)
    project_titles = {p["id"]: p.get("title") for p in project_rows}
    for task in tasks:
        task["project_title"] = project_titles.get(task.get("project_id"))

    # The attention set is a QUERY, sorted by the contract's own key so a
    # consumer rendering it in order needs no policy (docs/attention-contract).
    #
    # It is emitted as an ORDERED LIST OF IDS, not a second copy of the rows.
    # Every task is already in `tasks`, and §4.10 is a standing warning about
    # this snapshot's size: a duplicated row is a row that can also disagree
    # with itself. The order IS the payload here — index `tasks` by id and walk
    # this list.
    ordered = sorted((t for t in tasks if t["is_attention"]),
                     key=lambda t: estate_attention.sort_key(
                         t["due_state"], t["days_left"], t.get("seq")))

    # t-1061 — a proposal and the exit follow-up derived from it are ONE
    # question, so the set holds one row for them. The collapsed half is
    # withheld from `attention`, from `attention_tiers` and from
    # `attention_counts`, all three, because the whole claim is that the set
    # the owner reads got smaller and a count that still included the hidden
    # row would print "N of N shown" over N-1 rows — the exact failure
    # `applyResolved` in the estate page already guards against.
    #
    # The withheld row STAYS IN `tasks`. Every id list in this payload indexes
    # that array, the page's own task lookup reads it, and a collapsed row's
    # detail is one click away on the row that absorbed it. Removing it from
    # the store's snapshot would be hiding it, which is a different thing from
    # collapsing it.
    attention_pairs = estate_pairs.pair_view(
        conn, [(t["id"], t) for t in ordered],
        refs_of=lambda t: decode_refs(t.get("refs")),
        present_ids={t["id"] for t in ordered})
    collapsed = estate_pairs.collapsed_ids(attention_pairs)
    absorbed = estate_pairs.by_proposal(attention_pairs)
    for task in ordered:
        # Both halves of a pair that is still two rows carry their own record;
        # the surviving half of a collapsed pair carries the record of what it
        # absorbed. A row with neither carries None, and None renders as no
        # badge rather than as an empty one.
        own = attention_pairs.get(task["id"])
        task["derived_pairs"] = [own] if own else absorbed.get(task["id"], [])
    ordered = [t for t in ordered if t["id"] not in collapsed]

    attention_counts = {state: 0 for state in estate_attention.ALL_STATES}
    for task in ordered:
        state = task["due_state"] or estate_attention.UNDATED
        attention_counts[state] = attention_counts.get(state, 0) + 1
    attention_counts["total"] = len(ordered)
    attention_counts["collapsed"] = len(collapsed)
    attention = [t["id"] for t in ordered]
    attention_tiers = tiers(ordered)

    # Every follow-up, open and resolved: the page keeps resolved ones behind a
    # History filter rather than mixing them into current attention (§5.3 A),
    # which it can only do if they are in the payload at all. Ids again, same
    # reason.
    followup_rows = [t for t in tasks if t["followup"]]
    followup_counts = {"open": sum(1 for t in followup_rows
                                   if t["followup"]["state"] == "open")}
    followup_counts["resolved"] = len(followup_rows) - followup_counts["open"]
    followup_counts["total"] = len(followup_rows)
    followups = [t["id"] for t in followup_rows]

    # EVERY staged task, whatever its kind (t-660). `proposal_view` returns a
    # record for anything carrying a stage, so this list and `proposal_counts`
    # below are computed from the one set and cannot disagree with each other:
    # the panel's stats grid counts exactly the rows the panel renders.
    proposal_rows = [t for t in tasks if t["proposal"]]
    if proposal_rows:
        recurrences = {r["task_id"]: {"count": r["n"], "last": r["last_ts"]}
                       for r in conn.execute(
                           """SELECT task_id, count(*) n, max(ts) last_ts
                              FROM events WHERE kind='note'
                              AND summary LIKE 'condition observed again%'
                              GROUP BY task_id""")}
        for task in proposal_rows:
            recurrence = recurrences.get(task["id"], {})
            task["proposal"]["recurrences"] = recurrence.get("count", 0)
            task["proposal"]["last_recurrence_at"] = recurrence.get("last")
    proposal_counts = {stage: 0 for stage in PROPOSAL_STAGES}
    for task in proposal_rows:
        stage = task["proposal"]["stage"]
        proposal_counts[stage] = proposal_counts.get(stage, 0) + 1
    proposals = [t["id"] for t in proposal_rows]

    # t-296 — the pull-based backstop, and the one that does not depend on
    # Slack at all. A mirrored message the hub has not closed is an unfinished
    # conversation; ordered OLDEST FIRST because the one that has been open
    # longest is the one most likely to have been missed, which is the entire
    # failure this tracks. Ids again, for the reason attention gives.
    message_rows = [t for t in tasks
                    if t["message"] and t["message"]["open"]]
    message_rows.sort(key=lambda t: (t.get("seq") or 0))
    message_counts: dict[str, int] = {}
    for task in message_rows:
        status = task.get("status") or "unknown"
        message_counts[status] = message_counts.get(status, 0) + 1
    message_counts["total"] = len(message_rows)
    inbox_messages = [t["id"] for t in message_rows]

    project_counts: dict[str, int] = {}
    for project in project_rows:
        if project.get("unprojected"):
            continue
        project_counts[project.get("status") or "unknown"] = (
            project_counts.get(project.get("status") or "unknown", 0) + 1)

    return {
        "tasks": tasks,
        "projects": project_rows,
        "project_counts": project_counts,
        "dependencies": edges,
        "dep_counts": dep_counts,
        "attention": attention,
        "attention_counts": attention_counts,
        "attention_tiers": attention_tiers,
        "attention_pairs": attention_pairs,
        "followups": followups,
        "followup_counts": followup_counts,
        "proposals": proposals,
        "proposal_counts": proposal_counts,
        "inbox_messages": inbox_messages,
        "inbox_message_counts": message_counts,
        "notice_days": notice,
    }

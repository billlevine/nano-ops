"""The absence rule, stated once (estate vision V-02, task t-400).

A reading cannot be recorded without its warrant, because there is nowhere to
put one that lacks it.

WHAT THIS IS FOR
----------------
Two gap audits found the same bug in two places and fixed it twice. P-03: the
spotter read a `gh` timeout as "your PR is finished" because a failed fetch and
an empty board were both `[]`. P-11: the briefer read the same timeout as
"quiet night" because a failed gatherer and a quiet one were both `[]`. Each
built its own outcome vocabulary, its own status mapping and its own predicate,
and each got it right. Nothing carried the rule to the next subsystem, so a
third one would have had to be bitten before it learned.

This module is that rule with no subsystem attached to it. It is deliberately
small: four warrant classes, one predicate, one composition, and a registry
that a new subsystem has to pass through before it can record anything.

THE RULE
--------
    An empty reading is evidence of an absence only when somebody looked, the
    look worked, and it covered everything the absence would have to hide in.

THE FOUR
--------
Four things a reading can be, and they are not three:

    observed        looked, and what it found is what is there. The ONLY class
                    from which an empty result proves an absence.
    failed          looked and broke. Non-zero, unparseable, timed out, 502.
    not_attempted   a look was owed and did not happen. The tool is missing,
                    the credential was never there, the producer has never
                    written its file. Nothing broke, because nothing ran.
    not_due         no look was owed. `--offline` by request; a board entry
                    whose repo left the config, so no source in this run could
                    have produced it.

`failed` and `not_attempted` are kept apart because one is weather and one is
setup, and a single word for both hides whichever matters this morning — the
briefer's `no_tool` versus `error`, the spotter's `no_credential` versus
`unreachable`. `not_attempted` and `not_due` are kept apart because one is a
hole and the other is a boundary: an operator chases the first and ignores the
second, and collapsing them turns every deliberate skip into a false alarm and
every real hole into an accepted silence.

Only `observed` warrants an absence claim. That is the entire policy, and it is
a table rather than a branch so that adding a fifth class is a visible edit.

PARTIAL — A MODIFIER, NOT A FIFTH CLASS
---------------------------------------
A reading that saw some of what it asked for is still `observed`: what it saw
is real work and dropping it would lose it. What it may never do is prove an
ABSENCE, because the part nobody read is exactly where an absence would hide.
So `partial` rides alongside the class and only ever subtracts:

    absence_is_evidence = (class is observed) and not partial

It is not a class of its own because it answers a different question. The class
says what happened to the look; `partial` says how much of the subject it
covered. A source can be `observed` and partial, and no other combination
changes the predicate.

WHY A REGISTRY AND NOT A BASE CLASS
-----------------------------------
Subsystems keep their own outcome words on purpose. `timeout` matters to the
briefer and `no_credential` matters to the spotter, and forcing both onto four
words would delete the vocabulary the operator actually reads. What must NOT
vary is the classification, so a subsystem registers its words here, in one
file, and gets the predicate derived from them.

That is what makes the unwarranted state unrepresentable rather than merely
discouraged:

  * `reading()` has no `absence_is_evidence` parameter. It cannot be passed in,
    overridden or hand-set. It is computed from the warrant every time.
  * An outcome that is not in the vocabulary raises `UnwarrantedReading`. There
    is no default class, so a new word cannot quietly inherit "observed".
  * A vocabulary with no `observed` outcome is refused at registration: a
    subsystem that can never conclude an absence has a broken table, not a
    cautious one.

WHAT IS DELIBERATELY NOT HERE — the scheduled-run reconciler (P-08)
-------------------------------------------------------------------
`bin/estate runs` looks like a third copy of this rule and is not one, and the
distinction is load-bearing enough to state where somebody will look for it.

Everything here is a FIRST-PERSON warrant: the thing that did the looking
reports on its own attempt, at the time it made it. P-08 exists precisely
because that report is the thing that is missing — a pass that died before
writing anything left no row at all. So `estate_runs.reconcile` is
THIRD-PERSON and reconstructive: it derives a verdict after the fact from a
schedule, an observability floor and collateral evidence in the Ledger.

The vocabularies do not correspond, in both directions:

  * it has no `failed` in this module's sense. There is no way for the
    reconciler to report "I could not read the events table"; if that read
    breaks, the command breaks.
  * `activity_only` and `incomplete` have no counterpart here. Both say a run
    demonstrably happened and its OUTCOME is unrecorded — a claim about a
    record, which cannot arise where the observer and the recorder are one
    synchronous call.
  * `unobservable` is not `not_attempted`. It says the RECORDING MECHANISM did
    not exist in that era, which is a fact about the store's history rather
    than about any attempt.
  * "not due" never becomes a verdict at all: `due_slots` declines to enumerate
    a slot whose window has not closed, so the case is handled by exclusion.

The estate already refused this merge once, on the record. `brief.py`'s
`runs_section` and `attention_section` are pinned "NOT degraded" on
`unobservable` slots and `unreadable` due dates, because restating P-08's and
P-07's distinctions in P-11's vocabulary "would give the estate two words for
one thing and a way for them to disagree"
(docs/summary-manifest-contract.md, "runs and attention are deliberately never
degraded"). Importing this module into `estate_runs.py` would be that merge.

Docs: docs/absence-contract.md
"""
from __future__ import annotations

# ── the four warrant classes ─────────────────────────────────────────────────
OBSERVED = "observed"
FAILED = "failed"
NOT_ATTEMPTED = "not_attempted"
NOT_DUE = "not_due"

CLASSES = (OBSERVED, FAILED, NOT_ATTEMPTED, NOT_DUE)

# The policy, as a table. Only a look that happened and worked can turn an
# empty result into a claim about the world. Everything else is empty because
# of us, not because of the subject.
ABSENCE_IS_EVIDENCE = {
    OBSERVED: True,
    FAILED: False,
    NOT_ATTEMPTED: False,
    NOT_DUE: False,
}

# Worst first, for a consumer that has to lead with the thing most likely to
# have hidden something. A failure is the loudest because something broke that
# was supposed to work; a boundary is the quietest because nothing is wrong.
SEVERITY = {FAILED: 0, NOT_ATTEMPTED: 1, NOT_DUE: 2, OBSERVED: 3}


class UnwarrantedReading(ValueError):
    """An outcome with no registered warrant class, or a broken vocabulary.

    Raised rather than defaulted. A default would let a new outcome word
    inherit `observed` by saying nothing, which is the exact failure this
    module exists to make impossible.
    """


class Vocabulary:
    """One subsystem's outcome words, each bound to a warrant class.

    The words are the subsystem's own — `timeout` is the briefer's morning and
    `no_credential` is the spotter's setup problem, and neither should be
    renamed to match the other. The CLASSIFICATION is not the subsystem's, and
    that is the whole point of writing them down in one file.
    """

    __slots__ = ("name", "outcomes")

    def __init__(self, name: str, outcomes: dict):
        if not str(name or "").strip():
            raise UnwarrantedReading("a vocabulary needs a name")
        if not outcomes:
            raise UnwarrantedReading(f"{name}: a vocabulary with no outcomes "
                                     "cannot classify anything")
        bad = {o: c for o, c in outcomes.items() if c not in ABSENCE_IS_EVIDENCE}
        if bad:
            raise UnwarrantedReading(
                f"{name}: outcome(s) bound to something that is not one of "
                f"{', '.join(CLASSES)}: {bad}")
        if not any(c == OBSERVED for c in outcomes.values()):
            # Not caution — a broken table. A subsystem whose every outcome
            # denies its own reading can never report anything, and every
            # absence it sees would be held forever with no way out.
            raise UnwarrantedReading(
                f"{name}: no outcome is classed `{OBSERVED}`, so this "
                "subsystem could never conclude that anything is absent")
        self.name = str(name)
        self.outcomes = dict(outcomes)

    def classify(self, outcome: str) -> str:
        """This outcome's warrant class, or raise. No default, deliberately."""
        try:
            return self.outcomes[outcome]
        except KeyError:
            raise UnwarrantedReading(
                f"{self.name}: {outcome!r} has no warrant class. Add it to the "
                f"vocabulary in lib/estate_observation.py and say which of "
                f"{', '.join(CLASSES)} it is.") from None

    def absence_is_evidence(self, outcome: str, partial: bool = False) -> bool:
        """The rule, for one outcome. Derived here and nowhere else."""
        return ABSENCE_IS_EVIDENCE[self.classify(outcome)] and not partial

    def outcomes_in(self, warrant: str) -> tuple:
        if warrant not in ABSENCE_IS_EVIDENCE:
            raise UnwarrantedReading(f"{warrant!r} is not one of "
                                     f"{', '.join(CLASSES)}")
        return tuple(sorted(o for o, c in self.outcomes.items()
                            if c == warrant))


# ── the registry ─────────────────────────────────────────────────────────────
# Every subsystem that records an observation appears here. This is the file a
# new one has to edit, and editing it is what forces its author through the
# four-way once, before any of its readings exist.
_REGISTRY: dict = {}


def register(name: str, outcomes: dict) -> Vocabulary:
    """Declare a subsystem's outcome words. Idempotent on an identical table."""
    vocab = Vocabulary(name, outcomes)
    existing = _REGISTRY.get(name)
    if existing is not None and existing.outcomes != vocab.outcomes:
        raise UnwarrantedReading(
            f"{name} is already registered with a different table; two "
            "classifications for one subsystem is the disagreement this "
            "registry exists to prevent")
    _REGISTRY[name] = vocab
    return vocab


def vocabulary(name: str) -> Vocabulary:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnwarrantedReading(
            f"{name!r} records observations but has no registered "
            "vocabulary. Declare one in lib/estate_observation.py.") from None


def registered() -> tuple:
    return tuple(sorted(_REGISTRY))


# ── one reading ──────────────────────────────────────────────────────────────
def reading(subsystem: str, source: str, outcome: str, *,
            partial: bool = False, at: str | None = None,
            why: str | None = None, detail=None) -> dict:
    """One look, with its warrant attached. There is no other constructor.

    Note what this signature does NOT accept: `absence_is_evidence`. It is
    derived from the outcome on every call, so a caller cannot claim a warrant
    its reading does not have, and a stored reading cannot drift from the rule
    that produced it. That is the difference between this and a validator
    somebody has to remember to run.
    """
    vocab = vocabulary(subsystem)
    warrant = vocab.classify(outcome)
    partial = bool(partial)
    if partial and warrant != OBSERVED:
        # A look that never happened, or broke outright, saw no part of its
        # subject. Accepting `partial` there would put a coverage claim on a
        # reading with no coverage at all.
        raise UnwarrantedReading(
            f"{subsystem}: {outcome!r} is `{warrant}`, so it read no part of "
            "its subject; `partial` describes a reading that saw some of it")
    return {
        "subsystem": subsystem,
        "source": source,
        "outcome": outcome,
        "warrant": warrant,
        "partial": partial,
        "absence_is_evidence": ABSENCE_IS_EVIDENCE[warrant] and not partial,
        "at": at,
        "why": why,
        "detail": detail,
    }


def absence_is_evidence(read: dict) -> bool:
    """May a consumer read this reading's empty result as "nothing there"?

    Recomputed from the warrant rather than trusted from the stored field, so
    a hand-edited or older record cannot claim one it was not given. Anything
    that is not a well-formed reading is not evidence — an unreadable warrant
    is exactly the case this rule refuses to guess about.
    """
    if not isinstance(read, dict):
        return False
    return (ABSENCE_IS_EVIDENCE.get(read.get("warrant"), False)
            and not read.get("partial"))


# ── composing readings: can an absence be proved? ────────────────────────────
# The spotter asks this per board entry, over the sources that could have
# produced it. The briefer asks it per morning, over all ten gatherers. Same
# question, same answer: every relevant look has to have worked, and there has
# to have been at least one.
def absence_proved(readings, subject: str | None = None) -> dict:
    """Is an absence from this set of readings evidence that it is gone?

    Requiring EVERY relevant reading is deliberate over-caution in one
    direction. Holding an item one refresh too long costs a stale row the next
    good look clears. Concluding it is gone reports that work as finished.
    Those are not symmetric mistakes.

    An empty set is `not_due`, never proved. Nothing looked, which is not the
    same as nothing being there — the spotter's board entry whose repo left the
    config, and the manifest with no sources in it.
    """
    rows = [r for r in (readings or []) if isinstance(r, dict)]
    if not rows:
        return {"proved": False, "subject": subject, "warrant": NOT_DUE,
                "blocking": [],
                "why": "nothing that could have produced this was looked at"}
    blocking = [r for r in rows if not absence_is_evidence(r)]
    if not blocking:
        return {"proved": True, "subject": subject, "warrant": OBSERVED,
                "blocking": [], "why": None}
    blocking.sort(key=lambda r: (SEVERITY.get(r.get("warrant"), 9),
                                 str(r.get("source") or "")))
    worst = blocking[0]
    names = ", ".join(str(r.get("source")) for r in blocking[:4])
    more = f" (+{len(blocking) - 4} more)" if len(blocking) > 4 else ""
    return {
        "proved": False, "subject": subject,
        "warrant": worst.get("warrant"),
        "blocking": blocking,
        "why": (f"{len(blocking)} of {len(rows)} reading(s) cannot support an "
                f"absence: {names}{more}"),
    }


def unwarranted(readings) -> list:
    """The readings a consumer must not conclude an absence from, worst first.

    Worst first because a reader who stops after the first line should have
    been told about the thing that broke, not the thing that was skipped.
    """
    rows = [r for r in (readings or [])
            if isinstance(r, dict) and not absence_is_evidence(r)]
    return sorted(rows, key=lambda r: (SEVERITY.get(r.get("warrant"), 9),
                                       str(r.get("source") or "")))


# ── the subsystems that record observations ──────────────────────────────────
# The spotter (P-03, docs/spotter-observation-contract.md). Six fetch outcomes
# plus `unqueried`, which the engine produces per ITEM rather than per fetch:
# no source in this run could have produced the entry, so nothing was owed a
# look at it. That is a boundary, not a hole, which is why it is `not_due` and
# the tool-never-ran cases are not.
SPOTTER = register("spotter", {
    "ok": OBSERVED,
    "error": FAILED,             # it ran and refused
    "unreadable": FAILED,        # it answered in something that would not parse
    "unreachable": FAILED,       # the network, or the far end
    "unavailable": NOT_ATTEMPTED,    # the tool did not run at all
    "no_credential": NOT_ATTEMPTED,  # we were never able to ask
    "unqueried": NOT_DUE,        # nothing this run could have produced it
})

# The briefer (P-11, docs/summary-manifest-contract.md). Its three STATUSES are
# a projection of these four classes, not a separate scheme: `not_attempted`
# and `not_due` are both `unavailable` on the manifest, because a reader of the
# source strip wants "this one did not answer" in one dot and the reason in the
# outcome beside it.
BRIEFER = register("briefer", {
    "ok": OBSERVED,
    "error": FAILED,             # non-zero exit, HTTP error
    "unreadable": FAILED,        # it answered in something that would not parse
    "timeout": FAILED,           # it ran and never came back
    "no_state": NOT_ATTEMPTED,   # the producer has never written its file
    "no_tool": NOT_ATTEMPTED,    # the command is not there, or would not exec
    "offline": NOT_DUE,          # `gather --offline` — deliberate, not broken
})

# Outcome coverage (t-388/Q-05, bin/outcome-coverage). It counts how many times
# each DEFINED outcome was actually reached, so its whole product is a set of
# zeroes that a reader has to be able to trust. "This path has never run" is an
# absence claim about the store, which is precisely what this module governs —
# and the third verdict the tool prints, `unobservable`, is this vocabulary's
# refusal rather than a second scheme.
#
# `partial` is used here for real and is not decoration: a JSONL with lines
# that would not parse has been read in part, and the line nobody could read is
# exactly where the one occurrence of an unexercised path would hide.
COVERAGE = register("coverage", {
    "ok": OBSERVED,
    "unreadable": FAILED,        # the store errored, or a file would not parse
    "no_state": NOT_ATTEMPTED,   # the producer has never written its file
    "not_selected": NOT_DUE,     # --mechanism excluded it; nothing was owed
})

# The dashboard (t-390, docs/dashboard-panel-contract.md). The third subsystem,
# and the first one that did NOT have to be bitten first: P-11 gave the brief's
# ten gatherers this rule and every OTHER panel kept folding a missing producer
# store into a confident zero — rendered against an absent state directory the
# PR board showed `0/0/0/0/0` with no banner of any kind.
#
# It reads files and one local process, so it has no `timeout` and no `offline`:
# an outcome word this engine can never produce would be a vocabulary entry
# nothing could ever justify. `not_configured` is its own `not_due` — a panel
# whose producing loop is not in loops.toml at all was owed no look, which is a
# boundary and not the hole `no_state` describes.
DASHBOARD = register("dashboard", {
    "ok": OBSERVED,
    "error": FAILED,             # the read raised, or the tool exited non-zero
    "unreadable": FAILED,        # the file is there and would not parse
    "no_state": NOT_ATTEMPTED,   # the producer has never written its file
    "no_tool": NOT_ATTEMPTED,    # the command is not there, or would not exec
    "not_configured": NOT_DUE,   # no loop in this estate produces this panel
})

# The doorbell's `read` verb (t-782, bin/doorbell read). The hub's fallback
# path when the Slack MCP connector is down: one `conversations.history` call
# over the inbox's own committed cursor, reported with its warrant instead of
# as a bare list of messages.
#
# `partial` is the entry that earns this registration. Slack answers an
# `oldest`-bounded query with the NEWEST messages in the range and says in
# `has_more` / `response_metadata.next_cursor` whether it left older ones
# behind. The hand-built fallback of 2026-08-16 fetched that flag and dropped
# it, and a cursor advanced past a truncated read skips the remainder
# permanently — unreachable by the hub and unkickable by the doorbell, since
# the poller never writes a cursor of its own. So a truncated read is
# `observed` and partial: the ten messages it did return are real work, and
# the one thing it may not do is prove the inbox is quiet.
#
# `no_credential` is `not_attempted` and not `failed` for the reason the
# spotter's is: a token that was never on disk is setup, and a token Slack
# refused is weather, and an operator chases those two differently.
DOORBELL = register("doorbell", {
    "ok": OBSERVED,
    "api_error": FAILED,         # Slack answered `ok: false`
    "unreachable": FAILED,       # the network, the far end, or a timeout
    "unreadable": FAILED,        # it answered in something that would not parse
    "no_credential": NOT_ATTEMPTED,  # no token at TOKEN_FILE; nothing was asked
})

# The landing reconciler (t-712 panel P1, bin/landed-reconciler,
# docs/landed-reconciliation-contract.md). It asks one question per closed
# task — is the commit this task's completion evidence names an ancestor of its
# repo's default branch — and the only answer this contract governs is the
# negative one. `not_landed` is an absence claim about a repository's history,
# and it is exactly the claim a wrong repo, a pruned object or a missing tip
# would otherwise manufacture for free.
#
# `sha_unknown` is `failed` rather than an answer, and that is the one entry
# worth arguing about. The look ran and the object is not there — which reads
# like a result until you notice that the overwhelmingly likely cause is that
# the claim named a repo this run never guessed. "This sha is in no history I
# can see" and "this sha is in a history I did not open" are the same bytes,
# so the check that could not be made is recorded as one that could not be
# made.
#
# `no_claim` is the real `not_due` member and carries most of the store: a
# `done` task whose evidence makes no landing claim was owed no look at all,
# which is a boundary and not the hole `no_sha` describes (a claim that names
# a branch and no commit is a caller to go and fix).
LANDED = register("landed", {
    "ok": OBSERVED,
    "git_error": FAILED,         # git ran and failed in a way we cannot read
    "sha_unknown": FAILED,       # the commit is in no history this run opened
    "unreadable": FAILED,        # the store errored, or refs would not parse
    "no_repo": NOT_ATTEMPTED,    # the checkout named is not there, or not git
    "no_tool": NOT_ATTEMPTED,    # git is not on PATH
    "no_tip": NOT_ATTEMPTED,     # the default branch has no ref to compare to
    "no_sha": NOT_ATTEMPTED,     # a landing was claimed and named no commit
    "no_state": NOT_ATTEMPTED,   # there is no estate store to read
    "no_claim": NOT_DUE,         # this task's evidence claims no landing
})

# Closure shadow mode (t-783, bin/closure-shadow, the time-focus panel's
# alternative A). It counts the hub's contract-mandated closure posts and splits
# them `done` versus `needs-owner`, per calendar bucket. Its per-bucket zero is
# the absence claim this module governs: "no closure was posted that day" is a
# statement about the estate's history, and a store that is absent or would not
# open produces exactly the same empty bucket as a genuinely quiet day.
#
# Three members, and the missing fourth is the point. This engine opens ONE
# sqlite connection and shells out to nothing, so it has no `timeout`, no
# `no_tool` and no network outcome — and it has no `not_due` member either,
# because there is no selector here that can narrow what gets read. Every run
# reads the one store, so a look is always owed and a boundary can never arise.
# An outcome word this engine could never produce would be an entry nothing
# could ever justify, which is the dashboard vocabulary's rule in a second
# place.
#
# The observability floor is deliberately NOT an outcome. `inbox-message`
# tracking only exists since t-296, and a window reaching back past that is
# clamped to the floor and said so out loud, exactly as bin/mechanism-audit's
# M-18/M-23 do — a narrowed window is a real window, not a failed look.
#
# `partial` is used here for real: a closure row whose `ts` will not parse
# cannot be put in any bucket, and the bucket it belongs in is exactly where a
# day that looks quiet would be hiding it.
CLOSURE = register("closure", {
    "ok": OBSERVED,
    "unreadable": FAILED,        # the store errored, or would not open
    "no_state": NOT_ATTEMPTED,   # there is no estate store to read
})

# The pane sweep (`bin/doorbell panes`, t-1094). It asks whether a live
# session's terminal has said anything since the last look, and the answer it
# must never manufacture is `active`. Every other check here reads a STORE or a
# git history; this one reads the only surface a stalled session is visible on,
# because a long-lived session once sat at an empty prompt for roughly 27 hours
# while agent-deck reported `running` the whole time. agent-deck's status string
# is documented-unreliable: it says `waiting` for a healthy between-ticks hub and
# for a dead one alike.
#
# The absence claim here is subtler than its siblings' and runs BOTH ways, which
# is why the vocabulary is needed twice over. "This pane has not changed" is an
# absence of activity, and it is what a flag rests on — so a capture that failed
# must never become a stall. "Nothing is stalled" is an absence of findings, and
# a sweep that could not enumerate the sessions produces exactly the same empty
# row list as an installation where every pane is busy.
#
# The two NOT_DUE words are boundaries and not holes. `not_running` is a session
# agent-deck reports stopped or errored: it has no live pane to be stalled at,
# and its liveness is `bin/ops health`'s question rather than this one. `exempt`
# is a session whose configured idle threshold is zero, which is a declaration
# that an idle prompt is its resting state — a long-lived session with no
# cadence at all. Neither was owed a look.
#
# `stale_sweep` and `no_state` belong to `panes --report`, which reads the last
# sweep's file rather than looking itself. A report out of a sweeper that stopped
# running an hour ago is the reassuring answer with nobody behind it, which is
# this whole module's bug in one more place.
PANE_WATCH = register("pane-watch", {
    "ok": OBSERVED,
    "deck_error": FAILED,        # agent-deck ran and would not answer
    "tmux_error": FAILED,        # tmux ran and would not answer
    "unreadable": FAILED,        # an answer came back and would not parse
    "no_tool": NOT_ATTEMPTED,    # agent-deck or tmux is not on PATH
    "no_binding": NOT_ATTEMPTED,  # the record names no live tmux pane
    "stale_sweep": NOT_ATTEMPTED,  # the recorded sweep is too old to speak for now
    "no_state": NOT_ATTEMPTED,   # no sweep has ever been recorded
    "not_running": NOT_DUE,      # not alive; there is no live pane to watch
    "exempt": NOT_DUE,           # an idle prompt is this session's resting state
})

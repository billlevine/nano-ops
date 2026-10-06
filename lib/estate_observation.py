"""The absence rule, stated once (estate vision V-02, task the contract.

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
DAY_CAPTURE = register("day-capture", {
    "read": OBSERVED,
    "unreadable": FAILED, "parse_failed": FAILED, "store_locked": FAILED,
    "no_tree": NOT_ATTEMPTED, "not_installed": NOT_ATTEMPTED,
    "disabled": NOT_ATTEMPTED, "day_open": NOT_DUE,
})

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

# Outcome coverage the contract/Q-05, bin/outcome-coverage). It counts how many times
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

# The dashboard the contract, docs/dashboard-panel-contract.md). The third subsystem,
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

# The doorbell's `read` verb the contract, bin/doorbell read). The hub's fallback
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

# The landing reconciler the contract panel P1, bin/landed-reconciler,
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
#
# THE SECOND OBSERVED WORD, and the three that guard it the contract. Ancestry
# alone was blind in two directions, and both were measured on 2026-08-27: of
# fifteen `not_landed` findings, eleven were branches GitHub had SQUASH- or
# REBASE-merged (the merged commit is a different object, so the claimed sha is
# genuinely not an ancestor and never will be) and one was a clone whose
# `origin/main` had not been fetched in 54 days. Neither is a fact about the
# estate; both read as one.
#
# So the negative path now has two more looks in front of it, and each can fail
# in its own way:
#
#   `pr_merged`   is `observed` and is the ONLY new word that concludes
#                 anything. GitHub's own record says the claim's branch was
#                 merged in a pull request, which is a positive claim about a
#                 remote's history rather than an absence, and it is reached
#                 only with `landed=True`.
#   `fetch_failed` is `failed`: the default branch could not be refreshed, so
#                 "not an ancestor of this tip" is a statement about a tip that
#                 may be two months old.
#   `pr_error`    is `failed`: `gh` ran and would not answer, and "GitHub has
#                 no merged PR for this branch" and "GitHub would not tell me"
#                 are the same bytes — `sha_unknown`'s argument in a second
#                 place.
#   `no_pr_tool`  is `not_attempted`: `gh` is not on PATH, so the look never
#                 happened at all.
#   `offline`     is `not_attempted`: the operator passed `--offline`, so
#                 neither network look was owed. A boundary the caller drew,
#                 which is why it is not `failed`.
#
# An EMPTY PR list is deliberately not a member: `gh pr list --head` exiting 0
# with `[]` is a successful look that found nothing, the ancestry answer still
# stands on its own, and the row stays `ok`.
LANDED = register("landed", {
    "ok": OBSERVED,
    "pr_merged": OBSERVED,       # GitHub says the claim's branch was merged
    "git_error": FAILED,         # git ran and failed in a way we cannot read
    "sha_unknown": FAILED,       # the commit is in no history this run opened
    "unreadable": FAILED,        # the store errored, or refs would not parse
    "fetch_failed": FAILED,      # the default branch could not be refreshed
    "pr_error": FAILED,          # gh ran and would not answer about the branch
    "no_repo": NOT_ATTEMPTED,    # the checkout named is not there, or not git
    "no_tool": NOT_ATTEMPTED,    # git is not on PATH
    "no_pr_tool": NOT_ATTEMPTED,  # gh is not on PATH
    # the integration branch — the repo default, or a declared exempt target
    # the contract — has no ref in that checkout to compare against
    "no_tip": NOT_ATTEMPTED,
    "no_sha": NOT_ATTEMPTED,     # a landing was claimed and named no commit
    "no_state": NOT_ATTEMPTED,   # there is no estate store to read
    "offline": NOT_ATTEMPTED,    # --offline: neither network look was owed
    "no_claim": NOT_DUE,         # this task's evidence claims no landing
})

# Closure shadow mode the contract, bin/closure-shadow, the time-focus panel's
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
# tracking only exists since the contract, and a window reaching back past that is
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

# The credential expiry check the contract, bin/credential-expiry). It asks one
# question — how long until Claude Code's OAuth REFRESH token lapses — and the
# answer it must never manufacture is the reassuring one. "Nothing is expiring"
# is an absence claim about the next 24 hours, and a credentials file that is
# missing, unopenable or shaped differently than it used to be produces exactly
# the same "no warning to give" as a healthy one. Only one of those is a fact
# about the estate. A lapsed refresh token takes the hub and every
# autostart loop down at once, and leaves nothing running that could say so, so
# a check that answers "fine" when it could not look would reproduce the very
# outage it was built for.
#
# Five words, and the split inside each pair is the useful half. `no_credential`
# versus `no_access` is the spotter's setup-versus-permission distinction: a
# file that was never written is a machine that has never been logged in, and a
# file we may not open is this process running as the wrong user. `unreadable`
# versus `malformed` is weather versus contract: bytes that are not JSON mean
# something truncated the write, and JSON with no `claudeAiOauth
# .refreshTokenExpiresAt` in it means the credential format moved under us —
# the second is the one that would otherwise pass silently as "no expiry to
# report" forever.
#
# No `not_due` member, for the reason CLOSURE has none: every run reads the one
# file, nothing narrows it, so a look is always owed and a boundary cannot
# arise. And no `partial`: one field either parsed or it did not, and there is
# no part of a timestamp that could have been read.
CREDENTIAL = register("credential", {
    "ok": OBSERVED,
    "unreadable": FAILED,            # the file is there and is not JSON
    "malformed": FAILED,             # JSON, with no expiry field we recognise
    "no_credential": NOT_ATTEMPTED,  # no credentials file; nothing was asked
    "no_access": NOT_ATTEMPTED,      # it is there and this process may not read it
})

# The pane sweep (`bin/doorbell panes`, the contract. It asks whether a live
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
# and its liveness is `bin/ops health`'s question rather than this one — the
# opposite classification to the same word in `skill-drift`, where the owed
# restart stays a real unanswered question after the session dies. `exempt` is a
# session whose configured idle threshold is zero, which is a declaration that
# an idle prompt is its resting state — a long-lived session with no cadence
# at all.
# Neither was owed a look.
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

# The owed-restart check the contract, bin/skill-drift). It asks whether a running
# loop is still wearing the policy files that are on `main`, and the answer it
# must never manufacture is `fresh`. `fresh` is the ABSENCE claim here —
# "nothing has landed since this session bound its skill" — which is the same
# shape `not_landed` has in the `landed` vocabulary and obeys the same rule: a
# session nobody could find, a tmux binding that is not live, and a git history
# that would not open all produce exactly the same empty list of newer commits
# as a loop that really is up to date. Only one of those is a fact about the
# estate.
#
# The splits inside the NOT_ATTEMPTED words are the useful half, and each one
# names a DIFFERENT remedy in `bin/ops health`'s vocabulary. `no_session` is a
# loop that was never launched or was removed (health: needs-start → launch);
# `not_running` is a registered session that is stopped or errored (health:
# needs-start → start + kick); `no_binding` is a live agent-deck record whose
# tmux session is gone underneath it, which is the one shape health does not
# have a word for at all. `no_paths` is a registry entry pointing at a
# directory that holds no policy file — nothing to be behind, and nothing was
# looked at either.
#
# No `not_due` member, for the reason CLOSURE and CREDENTIAL have none: every
# registered loop is owed a look on every run, nothing narrows the set, so no
# boundary can arise. And no `partial`: a loop's binding either resolved or it
# did not.
SKILL_DRIFT = register("skill-drift", {
    "ok": OBSERVED,
    "deck_error": FAILED,          # agent-deck ran and would not answer
    "tmux_error": FAILED,          # tmux ran and would not answer
    "git_error": FAILED,           # git ran and failed on this history
    "unreadable": FAILED,          # an answer came back and would not parse
    "no_tool": NOT_ATTEMPTED,      # agent-deck, tmux or git is not on PATH
    "no_session": NOT_ATTEMPTED,   # no agent-deck record under that title
    "not_running": NOT_ATTEMPTED,  # registered, not alive; nothing is bound
    "no_binding": NOT_ATTEMPTED,   # the record's tmux session is not live
    "no_paths": NOT_ATTEMPTED,     # this loop dir holds no policy file to track
})

# The estate's own stop/start the contract, bin/estate-lifecycle + `bin/ops
# down|up|restart`). Everything else registered above observes some OTHER
# subsystem's state; this one observes the estate's own machinery on the way
# past, and it has two absence claims to keep honest rather than one.
#
# `clear` is the first: a preflight that finds no in-flight dispatch, no dirty
# worktree and no running ephemeral worker is claiming there is nothing to
# lose, and `bin/ops down --force` is what a person types after reading it. A
# dispatch store that is not there (a worktree checkout has an EMPTY state/
# tree, and the store this tool reads is the one beside its own bin/) produces exactly the same empty list as an estate with nothing
# running, so `no_state` is NOT_ATTEMPTED and the caller gets "unobservable"
# instead of a green light.
#
# `rebound` is the second: "this session really did come back on new bytes"
# is an assertion that a tmux session_created went UP, and a session nobody
# could find, a record whose tmux session is gone underneath it, and a
# baseline that was never written all produce the same missing comparison. So
# every one of them is a word here, and none of them is `ok`.
#
# `not_running` is the one NOT_DUE member: a session that was already stopped
# before the pass began is a boundary rather than a hole — nothing was owed a
# rebinding, and nothing failed to provide one.
LIFECYCLE = register("lifecycle", {
    "ok": OBSERVED,
    "deck_error": FAILED,        # agent-deck ran and would not answer
    "tmux_error": FAILED,        # tmux ran and would not answer
    "git_error": FAILED,         # git ran and failed on a worktree
    "store_error": FAILED,       # bin/dispatches ran and would not answer
    "flox_error": FAILED,        # flox ran and failed
    "codex_error": FAILED,       # the shell-execution probe could not be judged
    "unreadable": FAILED,        # an answer came back and would not parse
    "no_tool": NOT_ATTEMPTED,    # agent-deck, tmux, git, flox or codex is absent
    "no_state": NOT_ATTEMPTED,   # the store this would read has never been written
    "no_session": NOT_ATTEMPTED,  # no agent-deck record under that title
    "no_binding": NOT_ATTEMPTED,  # the record names no live tmux session
    "no_baseline": NOT_ATTEMPTED,  # nothing recorded a "before" to compare against
    "not_configured": NOT_ATTEMPTED,  # nothing declares where to look
    "not_running": NOT_DUE,      # already stopped; no rebinding was owed
})

# The backlog reconciler the contract, bin/backlog-reconciler). It asks one question
# of an open `needs-owner` or `followup` row — is this item already finished? —
# and every answer rests on FACTS it gathered first: the row's own store
# history, the commits its evidence names, the pull requests its text names.
#
# The absence claim here is a timestamp that is not there. A commit whose date
# could not be read and a commit that does not exist produce the same empty
# cell, and only one of them may be cited: a citation-verification gate that
# treated an unreadable git call as "no such commit" would reject a true
# finding and, worse, would let a claim about a commit nobody could open pass
# as unverifiable-therefore-unchallenged. So every fact this tool puts in front
# of the seat carries the reading that produced it, and a fact whose reading
# cannot conclude an absence is never used to contradict a claim — only to
# refuse to confirm one.
#
# `no_reference` is the boundary member and it is real here: most rows in the
# attention set name no commit and no pull request at all, so no git or GitHub
# look was ever owed for them. Recording that as `failed` would make the
# ordinary row look broken; recording it as `observed` would let "this row
# names no landed commit" read as "this row's commit is not in main".
BACKLOG = register("backlog", {
    "ok": OBSERVED,
    "unreadable": FAILED,        # the store answered and would not parse
    "store_error": FAILED,       # bin/estate ran and would not answer
    "git_error": FAILED,         # git ran and failed in a way we cannot read
    "sha_unknown": FAILED,       # the commit is in no history this run opened
    "gh_error": FAILED,          # gh ran and would not answer about the PR
    "no_repo": NOT_ATTEMPTED,    # the checkout the label names is not there
    "no_tool": NOT_ATTEMPTED,    # git or gh is not on PATH
    "no_state": NOT_ATTEMPTED,   # there is no estate store to read
    "no_seat": NOT_ATTEMPTED,    # the seat definition is not installed
    "offline": NOT_ATTEMPTED,    # --offline: the network look was not owed
    "no_reference": NOT_DUE,     # the row names no commit and no pull request
})


# The context budget the contract, bin/context-budget). It asks what a role has to
# READ before it can do anything: the shared core plus that role's own
# CLAUDE.md, the files that file `@`-imports, its hooks and its skill.
#
# The absence claim is "there is no overage here", and a bundle whose total is
# unknown cannot support it. A role directory that is not in this checkout, a
# CLAUDE.md that would not open, an `@`-import that resolves to nothing — each
# leaves the same "no overage found" as a role genuinely inside its budget, and
# only one of them is a fact about the estate. So a file this tool could not
# read contributes an UNKNOWN number of bytes, never zero, and the row is
# `unobservable` rather than a verdict about size.
#
# `no_directory` is NOT_ATTEMPTED rather than NOT_DUE deliberately: a role in
# the registry with no directory in this checkout is a hole in the setup, not a
# boundary the run drew. The one genuine boundary has no word here because it
# cannot arise — every registered role is owed a measurement on every run, so
# nothing narrows the set. That is the reason SKILL_DRIFT, CLOSURE and
# CREDENTIAL have no `not_due` member either.
#
# No `partial`: a bundle is measured or it is not. A subtotal over some of its
# files is precisely the accounting that would make an over-budget bundle look
# fine, which is the defect this check exists to catch.
CONTEXT = register("context", {
    "ok": OBSERVED,
    "unreadable": FAILED,        # a bundle file answered and would not read
    "git_error": FAILED,         # git ran and failed on this history
    "no_history": FAILED,        # the growth pass could not read the branch
    "no_tool": NOT_ATTEMPTED,    # git is not on PATH
    "no_directory": NOT_ATTEMPTED,   # the role's dir is not in this checkout
    "no_role_file": NOT_ATTEMPTED,   # that dir holds no CLAUDE.md to load
})


# Loop cost the contract, bin/loop-cost). It reports how much model volume each loop
# spent, out of the per-session `models` blocks bin/day-capture has been
# recording in state/day-capture/*.json since it was built and which nothing
# read until this tool existed. Its whole product is a table of numbers per
# actor, and the number it must never manufacture is a ZERO: a loop that made
# no model call all day and a day nobody captured produce exactly the same
# empty bucket, and only the first is a fact about the estate. A tool that
# silently summed the second as nothing would tell the operator a loop went
# quiet on the night the capture timer was down.
#
# This is the first vocabulary whose subject is ANOTHER subsystem's recorded
# readings rather than a live look of its own. Each capture file carries
# day-capture's own `readings`, and the two that produce sessions —
# `claude_transcripts` and `codex_rollouts` — decide whether that day's volume
# may be believed. So three of the words below are that warrant class carried
# across rather than re-judged: `source_failed`, `source_absent` and
# `source_not_due` are day-capture's FAILED / NOT_ATTEMPTED / NOT_DUE arriving
# second-hand, and re-classifying them here would be the estate holding two
# opinions about one look. `source_not_due` is reachable and not decoration:
# day-capture stamps every source `day_open`/not_due for a day that has not
# closed, and a capture written from that state is a boundary rather than a
# hole.
#
# The file-level words split the way CREDENTIAL's do, on setup versus weather.
# `no_capture` is a day nobody reduced — nothing was attempted, which is the
# ordinary state of every date before day-capture existed and of any night the
# timer did not run. `unreadable` and `parse_failed` are a file that is there
# and would not open, and one that opened and is not a capture: the second is
# the one that would otherwise pass silently as an empty day forever.
#
# `partial` is used here for real. A capture whose transcript source read only
# part of its tree (an unreadable transcript, a JSONL still being written) has
# session totals that are a FLOOR, not a total — so the numbers still print,
# marked, and the day may never be called quiet.
LOOP_COST = register("loop-cost", {
    "read": OBSERVED,
    "unreadable": FAILED,        # the capture file is there and would not open
    "parse_failed": FAILED,      # it opened and is not a capture envelope
    "source_failed": FAILED,     # the capture says its session source broke
    "no_capture": NOT_ATTEMPTED,      # no capture file: nobody reduced that day
    "source_absent": NOT_ATTEMPTED,   # the capture never looked at that source
    "source_not_due": NOT_DUE,        # the day had not closed when it was written
})

ORIGIN_SYNC = register("origin-sync", {
    "ok": OBSERVED,              # the fetch worked; origin/main was read now
    "fetch_failed": FAILED,      # git fetch ran and failed or timed out
    "git_error": FAILED,         # a local git read failed after the fetch
    "no_tool": NOT_ATTEMPTED,    # git is not on PATH
    "dry_run": NOT_DUE,          # --dry-run: nothing may be written, refs included
})

TICK_OBSERVED = register("tick-observed", {
    "ticked": OBSERVED,             # the file advanced past the kick
    "no_tick": OBSERVED,            # watched the whole bound; it never did
    "unreadable": FAILED,           # present, and would not open or parse
    "no_tick_file": NOT_ATTEMPTED,  # the target declares no heartbeat file
    "not_checked": NOT_ATTEMPTED,   # nothing verified-bound to watch
    "no_wait": NOT_DUE,             # --tick-wait 0
})

STEWARD_HEALTH = register("steward-health", {
    "ok": OBSERVED,
    "deck_error": FAILED,        # agent-deck ran and would not answer
    "store_error": FAILED,       # bin/dispatches ran and would not answer
    "git_error": FAILED,         # git ran and failed on this worktree
    "unreadable": FAILED,        # an answer came back and would not parse
    "no_tool": NOT_ATTEMPTED,    # agent-deck, git or bin/dispatches is absent
    "no_registry": NOT_ATTEMPTED,    # no row has ever been written here
    "no_worktree": NOT_ATTEMPTED,    # the row's checkout is not on this disk
    "no_history": NOT_ATTEMPTED,     # that checkout has no commit to read
    "no_project": NOT_ATTEMPTED,     # it holds no STATE.md or NOTES.md
    "no_transcript": NOT_ATTEMPTED,  # no transcript for this worktree's cwd
    "no_usage": NOT_ATTEMPTED,       # the transcript holds no usage record
    "paused": NOT_DUE,           # paused on the operator's word; no round was owed
})

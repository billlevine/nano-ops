"""A proposal and the exit follow-up derived from it, as ONE row (t-1061).

THE DEFECT
----------
The night shift drains a queue item, and when the item exits it files a
follow-up in the durable store so the outcome is never simply dropped. When
the queue item WAS a proposal — which is most of them, because the hub
auto-queues approved-backlog proposals — that follow-up asks the same question
the proposal already asks. The owner then reads it twice.

The 2026-08-23 attention triage counted thirteen such pairs in a 96-item set:
t-97/t-373, t-220/t-374, t-249/t-376, t-98/t-491, t-399/t-421, t-402/t-423,
t-403/t-424, t-461/t-492, t-471/t-490, t-474/t-493, t-386/t-498, and the two
Forge continuations t-1052/t-1054 and t-1053/t-1055. Twenty-six rows for
thirteen questions, 27 percent of the set.

Nothing linked them. `task_deps` held 181 edges and not one of these pairs had
one; the only connection was the follow-up's `refs.ref`, which names the night
shift's TRANSIENT queue key ("task:4") — reused every night, pointing at
nothing durable. So the halves also could not co-close: t-386's work landed on
2026-08-21 and the landing note went onto t-498, the follow-up, while the
proposal read as though nothing had happened for two days.

THE TWO HALVES OF THE FIX
-------------------------
1. THE LINK IS RECORDED AT FILING TIME, by the one writer that knows it. The
   night shift already holds the proposal's `t-N` when it files the follow-up
   (it is the queue item's `estate_id`), so `_exit_to_followups` passes it and
   `bin/followups add --derives-from` writes it into the follow-up's refs
   envelope under `derives_from`. One key, in the envelope every reader
   already decodes.

   NOT a `task_deps` edge, and the choice is deliberate. `DEP_KINDS` is a
   four-word vocabulary five readers share, and this relationship is none of
   them: neither half blocks the other, neither is the other's parent, and
   `discovered-from` means "found while doing that", which would conflate a
   mechanical derivation with a real discovery. A refs key is also ONE write,
   atomic with the insert, so a follow-up cannot exist for a moment carrying
   no link.

2. THE PAIR RENDERS AS ONE ROW — unless the halves have diverged, in which
   case it renders as two, each visibly marked. This module is that rule.

THE COLLAPSE INVARIANT
----------------------
    A collapse may never hide recorded content.

Everything below follows from that sentence, and it is the reason the
divergence test is ASYMMETRIC.

In a collapse the PROPOSAL is the row that survives. It is the standing
question, it carries the classification, the alternatives and the
recommendation, and it is what `bin/estate proposal` operates on. The
follow-up half is withheld from the listing and named on the proposal's row.

So content recorded on the proposal half cannot be hidden by collapsing — the
proposal is still there. Content recorded on the FOLLOW-UP half can be, and
that is exactly the t-386/t-498 failure read backwards: a landing note went
onto the follow-up alone. Collapse that pair and the note leaves the surface.

Hence: only follow-up-side content counts as divergence. Proposal-side content
is ignored, and ignoring it is not an oversight — checking it would refuse to
collapse the eleven pairs where the proposal is simply the row somebody has
been working on, which is every ordinary pair in the set.

WHAT COUNTS AS FOLLOW-UP-SIDE CONTENT
-------------------------------------
A `note` event on the follow-up with NO note event on the proposal inside a
short pairing window.

The window is what separates a POINTER from a DIVERGENCE. Eight of the
thirteen follow-ups carry a hand-written triage note reading "this is the
follow-up half of proposal t-N" — written in the same sitting as the proposal's
own triage note, because a person was doing by hand what this module now does.
Those pair off and are correctly not divergence. t-498's landing note (
2026-08-22, actor `cli`) has no counterpart on t-386 at any distance, and
t-493's correction note (2026-08-16) has none on t-474. Those two are the
divergences, and they are the two the proposal's own condition names.

The window is measured, not chosen: against the live store on 2026-08-24 the
verdict set is IDENTICAL for every window from 15 minutes to 4 hours
({t-493, t-498}), and only below 15 minutes does it start picking up triage
pointers written a few minutes apart. 30 minutes sits in the middle of that
plateau, which is what makes it a threshold rather than a tuning knob.

A note is the unit because it is the only event kind that carries prose a
person wrote. Transitions and activity rows are the lifecycle moving, and a
follow-up's lifecycle moving is not a second question.

THE RECORDED OVERRIDE, AND WHY THERE IS ONLY ONE
------------------------------------------------
`refs.derives_diverged` on the follow-up forces the pair apart whatever the
derived rule says. It exists because a person who has read both halves knows
something the event log does not.

There is deliberately NO override in the other direction. A "collapse this
anyway" key would let one filing decision hide content the invariant above
exists to protect, which is precisely what the t-1061 proposal warns against:
"the link has to be visible rather than acted on unilaterally". The safe
direction is the only direction, so the only override produces MORE rows.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not close anything. Collapsing is a rendering decision and nothing
here writes, transitions or resolves a row — the proposal is explicit that
silent auto-closing of the second half is the thing to avoid. Resolving either
half still takes a person, and the collapsed row now at least shows them both
halves at once.

It does not invent a link. A follow-up with no `derives_from` is not paired
with anything, however similar its title; nothing here matches on text.

Docs: docs/attention-contract.md (the set these rows live in),
docs/decision-classification-contract.md (what a proposal row carries).
"""
from __future__ import annotations

import datetime as dt
import json
import re

# The refs key the night shift writes on the follow-up half at filing time.
DERIVES_FROM = "derives_from"
# The recorded override: truthy means "these two are not the same question".
DIVERGED = "derives_diverged"

# `bin/estate`'s own task id shape. Duplicated rather than imported for the
# reason every constant in lib/ is duplicated here: this module is read by the
# dashboard, which never loads the CLI. The drift check is a test.
TASK_ID_RE = re.compile(r"^t-\d+$")

# Minutes. Two records written this far apart are one person's sitting; two
# records further apart are two events. See THE WINDOW above for the
# measurement that put the number in the middle of a plateau rather than on an
# edge.
PAIRING_WINDOW_MINUTES = 30

# The three verdicts. `half` is the third for the reason every third verdict in
# this estate exists: a pair whose other half is not in the caller's set is not
# a collapse and is not a divergence, and calling it either would state
# something nobody checked. It renders one row that NAMES the absent half.
COLLAPSED = "collapsed"
DIVERGED_V = "diverged"
HALF = "half"
VERDICTS = (COLLAPSED, DIVERGED_V, HALF)

# Why each verdict was reached, in the words a row can print.
REASONS = {
    COLLAPSED: "same question — the follow-up records nothing the proposal does not",
    DIVERGED_V: "the follow-up records something the proposal does not",
    "recorded": "a person recorded these halves as different questions",
    HALF: "the other half is not in this view",
}


def link_of(refs) -> str | None:
    """The proposal id a follow-up's decoded refs envelope names, or None.

    Validated against the task id shape rather than merely truthy: the whole
    point of this key is that it is durable where `refs.ref` was transient, and
    a key holding "task:4" again would be the defect wearing a new name.
    """
    if not isinstance(refs, dict):
        return None
    value = refs.get(DERIVES_FROM)
    text = str(value).strip() if value is not None else ""
    return text if TASK_ID_RE.match(text) else None


def diverged_recorded(refs) -> bool:
    """Whether a person has recorded that these halves ask different things."""
    return bool(isinstance(refs, dict) and refs.get(DIVERGED))


def _stamp(value):
    """One event timestamp as an aware UTC datetime, or None.

    Same normalization as `estate_attention.parse_iso`, duplicated for the same
    no-CLI-dependency reason and drift-checked by test.
    """
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def unpaired_records(followup_stamps, proposal_stamps,
                     window_minutes: float = PAIRING_WINDOW_MINUTES) -> list:
    """Follow-up-side records with no proposal-side record inside the window.

    Pure, and every input is an argument, so the window's behaviour is tested
    by moving stamps rather than by waiting.

    An UNREADABLE stamp on the follow-up side counts as unpaired. It is a
    record that exists, and no window can be measured from it — refusing to
    collapse is the reading that cannot hide it. An unreadable stamp on the
    proposal side simply pairs with nothing, which is the same conservatism
    from the other end.
    """
    window = float(window_minutes) * 60.0
    theirs = [s for s in (_stamp(v) for v in proposal_stamps) if s is not None]
    out = []
    for raw in followup_stamps:
        mine = _stamp(raw)
        if mine is None:
            out.append(raw)
            continue
        if not any(abs((mine - other).total_seconds()) <= window
                   for other in theirs):
            out.append(raw)
    return out


def classify(refs, proposal_present: bool, followup_stamps=(),
             proposal_stamps=(),
             window_minutes: float = PAIRING_WINDOW_MINUTES) -> dict | None:
    """The pairing record for one follow-up row, or None if it names no half.

    Returns {"proposal", "verdict", "reason", "evidence"} — `evidence` being
    the follow-up-side records that refused the collapse, so a row can say WHY
    it is still two rows instead of asserting it.

    Precedence: a recorded divergence wins over everything, then a missing
    other half, then the derived rule. The recorded override is first because
    it is the one judgment a person made after reading both halves, and it can
    only ever produce more rows.
    """
    proposal = link_of(refs)
    if not proposal:
        return None
    if diverged_recorded(refs):
        return {"proposal": proposal, "verdict": DIVERGED_V,
                "reason": REASONS["recorded"], "evidence": []}
    if not proposal_present:
        return {"proposal": proposal, "verdict": HALF,
                "reason": REASONS[HALF], "evidence": []}
    unpaired = unpaired_records(followup_stamps, proposal_stamps,
                                window_minutes)
    if unpaired:
        return {"proposal": proposal, "verdict": DIVERGED_V,
                "reason": REASONS[DIVERGED_V], "evidence": list(unpaired)}
    return {"proposal": proposal, "verdict": COLLAPSED,
            "reason": REASONS[COLLAPSED], "evidence": []}


NOTE_STAMPS_SQL = """
SELECT task_id, ts, refs FROM events
 WHERE kind='note' AND task_id IN (%s)
 ORDER BY seq
"""


def is_bookkeeping(refs_blob) -> bool:
    """Whether a note event is the LINK'S OWN record rather than content.

    Recording the link leaves an event on the follow-up — it has to, or a
    change to the store has no history. But that event is one-sided by
    construction: nothing writes a matching note on the proposal, because the
    proposal did not change. Left in, it is an unpaired follow-up-side record
    on every pair, so recording the link would immediately prove every pair
    diverged and collapse nothing. The backfill did exactly that on its first
    run against a copy of a live store: every pair came back `diverged`, all
    on the strength of the note the backfill had just written.

    A note is the link's own bookkeeping when its refs carry the link key. That
    is the same key the whole feature is spelled with, so a future writer of
    the link gets this exclusion by writing the key it already has to write,
    rather than by remembering a second convention.

    Blobs that will not parse are content, not bookkeeping. An unreadable
    envelope is not a claim about what the event was, and the conservative
    reading — the one that refuses a collapse rather than granting it — is the
    one that cannot hide anything.
    """
    try:
        refs = json.loads(refs_blob or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(refs, dict) and DERIVES_FROM in refs


def note_stamps(conn, task_ids) -> dict:
    """task id -> its note events' timestamps, in store order.

    ONE query for every id the caller cares about, never one per row: this runs
    inside `attention` and inside the dashboard's whole-store read, and an N+1
    there is a page that gets slower every night. Ids the store has no notes
    for are absent, so `.get(id, ())` is the read.

    The link's own bookkeeping is dropped here rather than at the call site, so
    no consumer can forget to.
    """
    wanted = [t for t in dict.fromkeys(task_ids) if t]
    if not wanted:
        return {}
    out: dict[str, list] = {}
    sql = NOTE_STAMPS_SQL % ",".join("?" for _ in wanted)
    for row in conn.execute(sql, wanted):
        if is_bookkeeping(row["refs"]):
            continue
        out.setdefault(row["task_id"], []).append(row["ts"])
    return out


def pair_view(conn, rows, refs_of, present_ids=None,
              window_minutes: float = PAIRING_WINDOW_MINUTES) -> dict:
    """Pairing records for a set of rows, keyed by the FOLLOW-UP's id.

    `rows` is any iterable of (task_id, refs) the caller already holds; passing
    `refs_of` decoded avoids this module caring whether refs arrive as a blob
    or a dict. `present_ids` is what "in this view" means — default is the ids
    in `rows`, which is right for `attention` and for the dashboard's attention
    set alike.

    The store is touched exactly once, and only for the ids a link was actually
    found on. A view with no linked follow-up in it does no query at all.
    """
    links = {}
    for task_id, refs in rows:
        proposal = link_of(refs_of(refs) if refs_of else refs)
        if proposal:
            links[task_id] = proposal
    if not links:
        return {}
    present = set(present_ids) if present_ids is not None else {
        task_id for task_id, _ in rows}
    stamps = note_stamps(conn, list(links) + list(links.values()))
    out = {}
    for task_id, refs in rows:
        if task_id not in links:
            continue
        proposal = links[task_id]
        record = classify(refs_of(refs) if refs_of else refs,
                          proposal in present,
                          stamps.get(task_id, ()),
                          stamps.get(proposal, ()),
                          window_minutes)
        if record:
            record["followup"] = task_id
            out[task_id] = record
    return out


def collapsed_ids(pairs: dict) -> set:
    """The follow-up ids a caller should WITHHOLD from its listing.

    Only `collapsed`. A `diverged` pair renders both halves and a `half` pair
    has only one to render, so neither withholds anything — which is the
    invariant at the top of this file, expressed as the one line every
    consumer needs.
    """
    return {fid for fid, rec in pairs.items()
            if rec.get("verdict") == COLLAPSED}


def by_proposal(pairs: dict) -> dict:
    """proposal id -> its pairing records, so the surviving row can name the
    half it absorbed. A proposal with two derived follow-ups keeps both."""
    out: dict[str, list] = {}
    for record in pairs.values():
        out.setdefault(record["proposal"], []).append(record)
    return out


def badge(record: dict) -> str:
    """The one phrase a rendered row prints for a pairing record."""
    verdict = record.get("verdict")
    if verdict == COLLAPSED:
        return f"+{record['followup']} collapsed"
    if verdict == DIVERGED_V:
        return f"⚯ {record['proposal']}/{record['followup']} diverged"
    return f"⚯ {record['proposal']} (not in this view)"

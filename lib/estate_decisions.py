"""The filing-time decision-classification schema (t-475).

WHAT IT FIXES
-------------
Nothing at filing time asked an item that needs a person what the CHOICE
actually is. A real attention set carries rows whose titles hold an explicit
decision verb, yet neither record type had a field for the question, the
alternatives, the recommendation, or the cost of waiting. The
mechanic's `propose` required title/condition/desired_outcome/completion_check
and a subsystem; `bin/followups add|attention` took an optional free-form
`--context` documented as "extra detail — findings, links, etc.". So an item
the owner must CHOOSE about and one already chosen that merely has to be done
arrived in one undifferentiated list.

THE SCHEMA
----------
Every filing declares one of three things about itself:

  decision       pick among alternatives
  action         already chosen, needs doing
  clarification  no valid options can be stated yet

A `decision` additionally carries the question, the alternatives with one
consequence each, a recommendation, and what happens if the owner defers.
`clarification` is a real and cheap exit: a filing whose scope is genuinely
not yet stated is honestly a clarification, and forcing its filer to
manufacture an option set would be worse than the gap that closed.

WHY AN ABSENT KEY AND "not recorded" ARE DIFFERENT
--------------------------------------------------
This is the subtle half and it is the whole reason the gate is code rather
than prose. `recommendation` may say exactly "not recorded" — a filer who
looked at two options and could not pick one is telling the reader something
true, and t-27 and t-166 are both real examples. The key being ABSENT says
nothing at all, and the estate has a written contract that an absence must
never be read as an answer (docs/absence-contract.md). So the key is required
and its value may be the sentinel. An empty string is an absence wearing a
key, and is refused with the sentinel named in the message.

WHERE IT IS ENFORCED
--------------------
The two writers that file at volume, and only those:

  mechanic.py `propose`          via `bin/estate decision check`
  bin/followups `add`|`attention` via this module directly

`bin/estate proposal add` deliberately does NOT require a classification, for
the same reason it does not require the P-09 review fields: `--adopt-task` on
work that predates the lifecycle and bin/migrate-mechanic-proposals both file
records nobody can honestly classify. What it does enforce is COHERENCE — a
payload that carries some decision keys must carry all of them, so a half-built
decision cannot land in the store looking complete.

Loop engines do not import lib/ (they run against a fabricated repo root in
their own tests), which is why the mechanic reaches this validator through
`bin/estate decision check` — the same subprocess door it already uses for
every durable write — rather than keeping a second copy to drift.
"""
from __future__ import annotations

# The three, and they are not two. `action` and `clarification` are kept apart
# because one has an owner and a next step and the other has neither: an
# operator does the first and asks about the second.
DECISION = "decision"
ACTION = "action"
CLARIFICATION = "clarification"
CLASSIFICATIONS = (DECISION, ACTION, CLARIFICATION)

CLASSIFICATION_KEY = "classification"
QUESTION_KEY = "question"
ALTERNATIVES_KEY = "alternatives"
RECOMMENDATION_KEY = "recommendation"
DEFER_KEY = "defer_consequence"

# The four a `decision` owns. They are refused on an `action` or a
# `clarification` rather than ignored: a record carrying alternatives IS a
# record about a choice, and filing it under another word is the misfiling this
# schema exists to make visible.
DECISION_ONLY_KEYS = (QUESTION_KEY, ALTERNATIVES_KEY, RECOMMENDATION_KEY,
                      DEFER_KEY)
ALL_KEYS = (CLASSIFICATION_KEY,) + DECISION_ONLY_KEYS

# The one value that makes "I have no recommendation" a recorded fact instead
# of a hole. Matched case-insensitively on a stripped value; stored normalized.
NOT_RECORDED = "not recorded"

# A choice needs something to choose between. One alternative is a
# recommendation with extra steps, and "A or defer" is already covered by the
# defer consequence every decision carries.
MIN_ALTERNATIVES = 2

# `--alternative "<option> :: <one consequence>"`. Split on the FIRST
# separator, so a consequence may contain one.
ALTERNATIVE_SEP = "::"

OPTION_KEY = "option"
CONSEQUENCE_KEY = "consequence"


class DecisionFilingRefused(ValueError):
    """A filing that does not declare what it is, or a decision with a hole.

    Carries the exact key at fault in `.key` so a caller can say which one
    without parsing prose. A silent coercion to `action` would be the same
    class of bug as the 500-char truncation this schema was filed behind.
    """

    def __init__(self, message: str, key: str = ""):
        super().__init__(message)
        self.key = key


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_alternative(text: str) -> dict:
    """One `--alternative` argument, as {option, consequence}.

    The separator is required rather than defaulted, because an alternative
    with no consequence is the exact thing t-27 already has on file: two named
    recovery approaches and nothing said about what either one costs.
    """
    raw = text if isinstance(text, str) else ""
    if ALTERNATIVE_SEP not in raw:
        raise DecisionFilingRefused(
            f'--alternative {raw!r} has no {ALTERNATIVE_SEP!r} separator. '
            f'Each alternative is "<option> {ALTERNATIVE_SEP} <what happens '
            f'if it is chosen>" — one consequence per option, always',
            ALTERNATIVES_KEY)
    option, _, consequence = raw.partition(ALTERNATIVE_SEP)
    return {OPTION_KEY: option.strip(), CONSEQUENCE_KEY: consequence.strip()}


def _normalize_alternatives(value) -> list:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise DecisionFilingRefused(
            f'"{ALTERNATIVES_KEY}" must be a list of '
            f'{{{OPTION_KEY}, {CONSEQUENCE_KEY}}} objects '
            f'(got {type(value).__name__})', ALTERNATIVES_KEY)
    out = []
    for index, entry in enumerate(value, start=1):
        if isinstance(entry, str):
            entry = parse_alternative(entry)
        if not isinstance(entry, dict):
            raise DecisionFilingRefused(
                f'alternative {index} must be an object with '
                f'"{OPTION_KEY}" and "{CONSEQUENCE_KEY}" '
                f'(got {type(entry).__name__})', ALTERNATIVES_KEY)
        option = _text(entry.get(OPTION_KEY))
        consequence = _text(entry.get(CONSEQUENCE_KEY))
        if not option:
            raise DecisionFilingRefused(
                f'alternative {index} has no "{OPTION_KEY}"', ALTERNATIVES_KEY)
        if not consequence:
            raise DecisionFilingRefused(
                f'alternative {index} ({option!r}) has no "{CONSEQUENCE_KEY}". '
                "An option nobody costed is not an alternative, it is a "
                "suggestion", ALTERNATIVES_KEY)
        out.append({OPTION_KEY: option, CONSEQUENCE_KEY: consequence})
    if len(out) < MIN_ALTERNATIVES:
        raise DecisionFilingRefused(
            f'"{ALTERNATIVES_KEY}" needs at least {MIN_ALTERNATIVES} entries '
            f'(got {len(out)}). Waiting is already recorded as '
            f'"{DEFER_KEY}", so a single option is not a choice — if there '
            f'is only one way forward this is an "{ACTION}"',
            ALTERNATIVES_KEY)
    return out


def validate(fields: dict) -> dict:
    """The gate. Returns the normalized fields, or raises
    DecisionFilingRefused naming the exact key at fault.

    `fields` must distinguish an ABSENT key from a present-but-empty one, so
    callers building it from argparse use `None` defaults and drop nothing.
    """
    if not isinstance(fields, dict):
        raise DecisionFilingRefused(
            f"classification payload must be a JSON object "
            f"(got {type(fields).__name__})", CLASSIFICATION_KEY)

    if CLASSIFICATION_KEY not in fields:
        raise DecisionFilingRefused(
            f'missing required key "{CLASSIFICATION_KEY}": every filing says '
            f"which of {', '.join(CLASSIFICATIONS)} it is. "
            f"`{DECISION}` = pick among alternatives, `{ACTION}` = already "
            f"chosen and needs doing, `{CLARIFICATION}` = no valid options "
            "can be stated yet. Nothing is inferred", CLASSIFICATION_KEY)
    classification = _text(fields.get(CLASSIFICATION_KEY)).lower()
    if classification not in CLASSIFICATIONS:
        raise DecisionFilingRefused(
            f'"{CLASSIFICATION_KEY}" must be one of '
            f"{', '.join(CLASSIFICATIONS)} "
            f"(got {fields.get(CLASSIFICATION_KEY)!r})", CLASSIFICATION_KEY)

    if classification != DECISION:
        for key in DECISION_ONLY_KEYS:
            if key in fields and fields[key] is not None:
                raise DecisionFilingRefused(
                    f'"{key}" belongs to a `{DECISION}` and this filing is a '
                    f"`{classification}`. A record that names alternatives is "
                    "a record about a choice — file it as a "
                    f"`{DECISION}` or drop the key", key)
        return {CLASSIFICATION_KEY: classification}

    out = {CLASSIFICATION_KEY: classification}

    if QUESTION_KEY not in fields or fields[QUESTION_KEY] is None:
        raise DecisionFilingRefused(
            f'missing required key "{QUESTION_KEY}": a `{DECISION}` states '
            "the exact choice the owner is making, in one line", QUESTION_KEY)
    question = _text(fields[QUESTION_KEY])
    if not question:
        raise DecisionFilingRefused(
            f'"{QUESTION_KEY}" is empty: a `{DECISION}` states the exact '
            "choice the owner is making, in one line", QUESTION_KEY)
    out[QUESTION_KEY] = question

    if ALTERNATIVES_KEY not in fields or fields[ALTERNATIVES_KEY] is None:
        raise DecisionFilingRefused(
            f'missing required key "{ALTERNATIVES_KEY}": a `{DECISION}` '
            f"carries at least {MIN_ALTERNATIVES} options, each with one "
            "consequence", ALTERNATIVES_KEY)
    out[ALTERNATIVES_KEY] = _normalize_alternatives(fields[ALTERNATIVES_KEY])

    # The absence rule, stated in code. An absent key is refused; the sentinel
    # is accepted. docs/absence-contract.md is the estate-wide version of the
    # same rule: nobody looked and nothing is there are different facts.
    if RECOMMENDATION_KEY not in fields or fields[RECOMMENDATION_KEY] is None:
        raise DecisionFilingRefused(
            f'missing required key "{RECOMMENDATION_KEY}": record the '
            f'recommendation, or the exact words "{NOT_RECORDED}". An absent '
            "key is not an answer (docs/absence-contract.md) — a filer who "
            'could not pick says "' + NOT_RECORDED + '" and means it',
            RECOMMENDATION_KEY)
    recommendation = _text(fields[RECOMMENDATION_KEY])
    if not recommendation:
        raise DecisionFilingRefused(
            f'"{RECOMMENDATION_KEY}" is empty. An empty value is an absence '
            f'wearing a key — write "{NOT_RECORDED}" if that is the truth',
            RECOMMENDATION_KEY)
    if recommendation.lower() == NOT_RECORDED:
        recommendation = NOT_RECORDED
    out[RECOMMENDATION_KEY] = recommendation

    if DEFER_KEY not in fields or fields[DEFER_KEY] is None:
        raise DecisionFilingRefused(
            f'missing required key "{DEFER_KEY}": say what happens if the owner '
            "does nothing. Deferring is one of the choices whether or not "
            "anybody wrote it down", DEFER_KEY)
    defer = _text(fields[DEFER_KEY])
    if not defer:
        raise DecisionFilingRefused(
            f'"{DEFER_KEY}" is empty: say what happens if the owner does nothing',
            DEFER_KEY)
    out[DEFER_KEY] = defer
    return out


def fields_from(mapping, keys=ALL_KEYS) -> dict:
    """The classification keys PRESENT in a mapping, absence preserved.

    A key whose value is None is treated as absent — that is what argparse
    gives for a flag nobody passed, and collapsing it into an empty string
    here would erase the distinction the whole schema turns on.
    """
    return {key: mapping[key] for key in keys
            if key in mapping and mapping[key] is not None}


def fields_from_args(args) -> dict:
    """The classification keys this CLI invocation carried.

    `--alternative` is repeatable and parsed here so `bin/estate` and
    `bin/followups` spell the flag the same way.
    """
    supplied = {}
    for key, attr in ((CLASSIFICATION_KEY, "classification"),
                      (QUESTION_KEY, "question"),
                      (RECOMMENDATION_KEY, "recommendation"),
                      (DEFER_KEY, "defer_consequence")):
        value = getattr(args, attr, None)
        if value is not None:
            supplied[key] = value
    raw = getattr(args, "alternative", None)
    if raw:
        supplied[ALTERNATIVES_KEY] = [parse_alternative(entry) for entry in raw]
    return supplied


def add_arguments(parser, required: bool = False) -> None:
    """The five flags, spelled identically wherever they appear.

    `default=None` on every one of them is load-bearing: argparse's usual
    `default=""` would hand the validator an empty string for a flag nobody
    passed, and the difference between "absent" and "empty" is the rule.
    """
    parser.add_argument(
        "--classification", default=None, choices=CLASSIFICATIONS,
        required=required,
        help=f"what this filing is: {DECISION} (pick among alternatives), "
             f"{ACTION} (already chosen, needs doing), or {CLARIFICATION} "
             "(no valid options yet)")
    parser.add_argument(
        "--question", default=None,
        help="decision only: the exact choice, in one line")
    parser.add_argument(
        "--alternative", default=None, action="append", metavar="OPT :: CONS",
        help=f"decision only, repeatable, at least {MIN_ALTERNATIVES}: "
             f'"<option> {ALTERNATIVE_SEP} <one consequence>"')
    parser.add_argument(
        "--recommendation", default=None,
        help=f'decision only: the filer\'s pick, or the exact words '
             f'"{NOT_RECORDED}". The key is required; that value is allowed')
    parser.add_argument(
        "--defer-consequence", default=None,
        help="decision only: what happens if the owner does nothing")


def alternative_line(entry: dict) -> str:
    return f"{entry.get(OPTION_KEY, '')} — {entry.get(CONSEQUENCE_KEY, '')}"


def review_lines(refs: dict, label_width: int = 17, indent: str = "  ") -> list:
    """`estate proposal show`'s label-column rendering of the classification.

    A row filed before this schema existed prints `(not recorded)` on the
    classification line and nothing else, the same way P-09's review fields
    already read as legacy rather than as complete.
    """
    classification = _text((refs or {}).get(CLASSIFICATION_KEY))

    def row(label, value):
        return f"{indent}{label:<{label_width}} {value}"

    lines = [row(CLASSIFICATION_KEY, classification or "(not recorded)")]
    if classification != DECISION:
        return lines
    lines.append(row(QUESTION_KEY, _text(refs.get(QUESTION_KEY)) or "(not recorded)"))
    alternatives = refs.get(ALTERNATIVES_KEY) or []
    if not isinstance(alternatives, list) or not alternatives:
        lines.append(row(ALTERNATIVES_KEY, "(not recorded)"))
    else:
        for position, entry in enumerate(alternatives):
            label = ALTERNATIVES_KEY if position == 0 else ""
            text = (alternative_line(entry) if isinstance(entry, dict)
                    else str(entry))
            lines.append(row(label, text))
    lines.append(row(RECOMMENDATION_KEY,
                     _text(refs.get(RECOMMENDATION_KEY)) or "(not recorded)"))
    lines.append(row("defer consequence",
                     _text(refs.get(DEFER_KEY)) or "(not recorded)"))
    return lines


def followup_lines(refs: dict, indent: str = " " * 13) -> list:
    """`followups show`'s block rendering. Same facts, the shape that file
    already prints its context in."""
    classification = _text((refs or {}).get(CLASSIFICATION_KEY))
    if not classification:
        return []
    if classification != DECISION:
        return [f"{indent}[{classification}]"]
    lines = [f"{indent}[{DECISION}] "
             f"{_text(refs.get(QUESTION_KEY)) or '(not recorded)'}"]
    alternatives = refs.get(ALTERNATIVES_KEY) or []
    if isinstance(alternatives, list):
        for entry in alternatives:
            text = (alternative_line(entry) if isinstance(entry, dict)
                    else str(entry))
            lines.append(f"{indent}  {text}")
    lines.append(f"{indent}  recommendation: "
                 f"{_text(refs.get(RECOMMENDATION_KEY)) or '(not recorded)'}")
    lines.append(f"{indent}  if deferred: "
                 f"{_text(refs.get(DEFER_KEY)) or '(not recorded)'}")
    return lines

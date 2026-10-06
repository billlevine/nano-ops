# The backlog-reconciliation contract

Ask whether an open row is already finished by comparing its intent with
current evidence. The public core carries this contract and the `backlog`
observation vocabulary. Its seat driver and executable are installation
adapters, not part of this extraction.

## Verdicts and dispositions

| Verdict | Reading | Disposition |
| --- | --- | --- |
| `consistent` | Work remains and the record agrees | keep-open |
| `extends` | Evidence carries the intent further | review |
| `diverges-with-rationale` | Intent changed with a recorded reason | review |
| `diverges-silently` | Work happened or was overtaken without acknowledgement | close-candidate |
| `contradicts` | Evidence reverses the premise | close-candidate |

Unknown verdicts are unclassified, never close candidates. Merge verdicts map
respectively to leaving the row as it stands, leaving it open with corrections,
or requesting changes to the row. A close candidate is a report, not closure.

## Reply shape

The configured seat requires exactly three headings in order. Every finding
carries all five fields, including intent evidence, and a severity of Critical,
Important or Minor. A malformed reply remains unanswered. Coverage-shaped
claims such as "nothing owns this path" do not substitute for intent evidence.
Relay coverage statements in full: a failed source and a source searched with
nothing found are different observations.

## Temporal and citation evidence

Gather facts before asking the seat. Keep created, merged and closed times as
separate fields. Resolve only refs explicitly marking commits as commits;
a hex-shaped message id, team id or fingerprint is not a commit.

Temporal claims cite facts or derivations:

- A time: `[fact: <key> = <value>]`.
- A duration: `[derived: <keyA> - <keyB> = <n> days]`.
- An ordering: `[derived: <keyA> < <keyB> = true]`.

Require existing keys and values matching the gathered precision. Recompute
arithmetic and order. Duration tolerance is the larger of one hour or five
percent of the span. A field must answer its sentence's verb, and derivation
endpoints must answer both things related. Strip citations before scanning
verbs so a field name cannot nominate itself as the predicate it supports.
Event timestamps may answer the event's recorded verb; generic non-time fields
cannot supply a time.

Anchor findings to an actual path and line in the reviewed row bundle and print
the resolved line. A quote, when offered, must match the artifact. Reject and
record failed citation checks rather than presenting the findings as valid.

## Failed looks and cadence

Use warranted readings for store, git, tracker and seat access. No reply is
unanswered, never examined. A capped event history declares the cap and cannot
prove an absence in older history. An offline ancestry fact is not a landing
verdict; report the ref and tip date actually compared.

Successful examination can advance cadence. Missing or malformed replies
cannot. Sampling and execution cadence are configured by the installation.
The read-side mechanism proposes corrections and never closes rows itself.

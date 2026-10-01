#!/usr/bin/env python3
"""Tests for lib/pending_judgment.py — the record a worker leaves instead of
halting, and the reviewer's disposition of it.

Five groups, in the order the module builds the contract:

  PERMITTED — a complete record validates, renders to the PR section, and the
  bytes the reviewer's tool reads back are the same record that was checked;
  BLOCKED — the four classes are refused whether a record declares one in
  `touches` or merely says in prose that one moved, and a refusal outranks an
  incomplete field;
  INVALID — every field is required and in vocabulary, no field may carry a
  second record, and a duplicate id fails;
  UNREADABLE — a block that opens and does not parse is counted as broken, so
  a record that silently vanished can never leave the rest of a body passing;
  DISPOSITION — `apply_to_body` swaps one record's block and refreshes only
  its own section's prose, and leaves the prose alone with a warning rather
  than rewriting a neighbour's verdict.

The records are built here rather than read from fixtures: they are invented
examples, and the module is the only thing under test.

Run: PYTHONPATH=lib python3 tests/test_pending_judgment.py  (the path is set
up below too, so a bare `python3 tests/test_pending_judgment.py` also works).
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "lib"))

import pending_judgment as pj          # noqa: E402


def permitted() -> dict:
    """A complete, permitted record: bounded, reversible, touching nothing."""
    return {
        "id": "PJ-1",
        "call": ("Retry the upstream fetch three times with a fixed "
                 "one-second backoff"),
        "stage": "implementation",
        "rationale": ("The ticket requires the fetch to survive a transient "
                      "upstream failure but names no retry policy; three "
                      "fixed retries is the policy the neighbouring "
                      "downloader already uses."),
        "alternatives": [
            {"option": "Exponential backoff with jitter",
             "why_not": ("no other caller here uses it, and a second policy "
                         "is a second thing to tune")},
            {"option": "No retry, fail fast",
             "why_not": ("does not meet the ticket's transient-failure "
                         "requirement")},
        ],
        "affected_behavior": ("A transient upstream failure now delays the "
                             "command by up to three seconds before it "
                             "errors."),
        "verification": ("New unit test injects two failures then a success "
                         "and asserts one successful result; the existing "
                         "fetch tests pass."),
        "reversal": "Set the retry count constant to zero; one line.",
        "touches": [],
        "reviewer_disposition": "pending",
    }


def blocked_declared() -> dict:
    """A call that declares a blocked class: never one to make."""
    return {
        "id": "PJ-2",
        "call": "Return the build number as its own field on the response",
        "stage": "design",
        "rationale": ("Clients will want to read it without parsing the "
                      "version string."),
        "alternatives": [
            {"option": "Append it to the version string only",
             "why_not": "clients would have to parse it back out"},
        ],
        "affected_behavior": "Every response carries one more field.",
        "verification": "Response snapshot tests updated.",
        "reversal": "Drop the field before any client reads it.",
        "touches": ["public-interface"],
        "reviewer_disposition": "pending",
    }


def blocked_undeclared() -> dict:
    """`touches: []`, and prose that says a data contract moved anyway."""
    return {
        "id": "PJ-3",
        "call": "Store the build number in its own column",
        "stage": "implementation",
        "rationale": ("Sorting by build is cheaper on an integer column than "
                      "on a parsed string."),
        "alternatives": [
            {"option": "Keep it inside the version string",
             "why_not": "sorting would parse every row"},
        ],
        "affected_behavior": ("The change requires a schema migration on the "
                             "packages table."),
        "verification": "Migration tested against a throwaway database.",
        "reversal": "Down-migration drops the column.",
        "touches": [],
        "reviewer_disposition": "pending",
    }


def second() -> dict:
    record = permitted()
    record["id"] = "PJ-2"
    return record


def body_of(*records) -> str:
    """A PR body with a summary before the section and words after it."""
    return ("## Summary\nRetry policy for the fetch.\n\n"
            + pj.render(list(records)) + "Trailing words.\n")


class PermittedCallReachesThePR(unittest.TestCase):
    def test_the_permitted_record_is_valid(self):
        self.assertEqual(pj.check(permitted()), (pj.VALID, []))

    def test_render_carries_every_field_and_round_trips_from_the_body(self):
        body = pj.render([permitted()])
        self.assertIn(pj.SECTION, body)
        for label in ("Stage", "Rationale", "Alternatives",
                      "Affected behavior", "Verification", "Reversal",
                      "Reviewer disposition"):
            self.assertIn(f"**{label}:**", body)
        # The PR body is the record: what the reviewer's tool reads back from
        # it is the same record that was checked, byte for byte.
        records, broken = pj.records_in("Summary\n\n" + body)
        self.assertEqual(broken, 0)
        self.assertEqual(records, [permitted()])
        self.assertEqual(pj.check_all(records)[0], pj.VALID)

    def test_a_reviewer_disposition_is_still_valid(self):
        for disposition in pj.REVIEWER_DISPOSITIONS:
            record = permitted()
            record["reviewer_disposition"] = disposition
            self.assertEqual(pj.check(record), (pj.VALID, []))

    def test_naming_a_subject_without_moving_it_passes(self):
        record = permitted()
        record["rationale"] = ("The public API docs already describe retry "
                               "behaviour for this command.")
        self.assertEqual(pj.check(record)[0], pj.VALID)

    def test_a_note_is_optional_and_may_be_recorded(self):
        self.assertIsNone(pj.check_note(None))
        self.assertIsNone(pj.check_note("   "))
        record = dict(permitted(), reviewer_disposition="accepted",
                      reviewer_note="Keeping the fixed backoff.")
        self.assertEqual(pj.check(record), (pj.VALID, []))
        self.assertIn("- **Reviewer note:** Keeping the fixed backoff.",
                      pj.render([dict(record,
                                      reviewer_disposition="pending")]))


class BlockedClassIsRefused(unittest.TestCase):
    def test_every_blocked_class_is_refused(self):
        for klass in pj.BLOCKED_CLASSES:
            record = permitted()
            record["touches"] = [klass]
            self.assertEqual(pj.check(record)[0], pj.REFUSED, klass)

    def test_a_declared_blocked_class_names_itself_in_the_refusal(self):
        outcome, problems = pj.check(blocked_declared())
        self.assertEqual(outcome, pj.REFUSED)
        self.assertTrue(any("public-interface" in p for p in problems),
                        problems)

    def test_an_undeclared_blocked_class_in_the_prose_is_refused(self):
        outcome, problems = pj.check(blocked_undeclared())
        self.assertEqual(outcome, pj.REFUSED)
        self.assertTrue(any("schema migration" in p for p in problems),
                        problems)

    def test_a_subject_with_a_change_verb_is_refused(self):
        record = permitted()
        record["affected_behavior"] = "This renames the public field `ver`."
        self.assertEqual(pj.check(record)[0], pj.REFUSED)

    def test_refused_outranks_invalid(self):
        record = blocked_declared()
        del record["verification"]
        outcome, problems = pj.check(record)
        self.assertEqual(outcome, pj.REFUSED)
        self.assertTrue(any("verification" in p for p in problems))

    def test_a_worker_may_not_publish_a_blocked_call(self):
        self.assertEqual(
            pj.check_all([blocked_declared()], worker=True)[0], pj.REFUSED)

    def test_one_blocked_record_refuses_the_whole_body(self):
        body = pj.render([permitted()]) + "\n" + (
            "```pending-judgment\n"
            + json.dumps(blocked_declared()) + "\n```\n")
        records, broken = pj.records_in(body)
        self.assertEqual(broken, 0)
        self.assertEqual(pj.check_all(records)[0], pj.REFUSED)

    def test_a_blocked_move_stated_only_in_verification_is_refused(self):
        record = permitted()
        record["verification"] = "Test proves the public API changes."
        self.assertEqual(pj.check(record)[0], pj.REFUSED)

    def test_verification_may_name_a_migration(self):
        # `verification` is scanned without the data-contract words: a
        # migration ticket's own test names its migration.
        record = permitted()
        record["verification"] = "The schema migration test still passes."
        self.assertEqual(pj.check(record)[0], pj.VALID)
        for word in pj.DATA_CONTRACT_WORDS:
            self.assertEqual(pj.blocked_phrases(f"The {word} test passes.",
                                                pj.DATA_CONTRACT_WORDS), [])

    def test_a_subject_and_a_verb_in_different_fields_do_not_pair(self):
        record = permitted()
        record["rationale"] = "Keep the public API stable"
        record["affected_behavior"] = "A later change will revisit it."
        self.assertEqual(pj.check(record), (pj.VALID, []))

    def test_ordinary_security_and_interface_wording_is_refused(self):
        for sentence in ("Stores the API token in plaintext.",
                         "Disable authentication for downloads.",
                         "Change the credential used to authenticate "
                         "requests.",
                         "Add a new CLI flag --unsafe.",
                         "Drops the cli flag --foo.",
                         "Exposes a new public api endpoint."):
            with self.subTest(sentence):
                record = permitted()
                record["affected_behavior"] = sentence
                self.assertEqual(pj.check(record)[0], pj.REFUSED)

    def test_a_subject_does_not_supply_its_own_verb(self):
        record = permitted()
        record["rationale"] = "The stored format is read once at startup."
        self.assertEqual(pj.check(record)[0], pj.VALID)

    def test_an_always_phrase_is_the_change_on_its_own(self):
        for phrase in pj.ALWAYS:
            self.assertEqual(pj.blocked_phrases(f"It is a {phrase}."),
                             [phrase], phrase)

    def test_every_subject_escalates_with_a_change_verb(self):
        # Every phrase the guard carries has to be reachable, or one could be
        # deleted without a test noticing. Each subject, in a sentence whose
        # verb sits outside it.
        for subject in pj.SUBJECTS:
            with self.subTest(subject):
                self.assertIn(subject,
                              pj.blocked_phrases(f"We change the {subject}."),
                              subject)

    def test_every_change_verb_escalates_a_subject(self):
        for verb in pj.CHANGE_VERBS:
            with self.subTest(verb):
                self.assertEqual(
                    pj.blocked_phrases(f"We {verb} the public api."),
                    ["public api"], verb)

    def test_a_data_contract_subject_is_scanned_everywhere_but_verification(self):
        # The data-contract words are skipped in `verification` only; in any
        # other field they escalate like every other subject.
        for word in pj.DATA_CONTRACT_WORDS:
            with self.subTest(word):
                record = permitted()
                record["affected_behavior"] = f"We change the {word}."
                self.assertEqual(pj.check(record)[0], pj.REFUSED, word)

    def test_a_negated_move_is_refused_too(self):
        # No negation handling, on purpose: `touches: []` is where "it moves
        # nothing" lives, not a sentence a reader could be talked out of.
        record = permitted()
        record["affected_behavior"] = "This does not change the public API."
        self.assertEqual(pj.check(record)[0], pj.REFUSED)


class MissingFieldFailsValidation(unittest.TestCase):
    def test_each_field_missing_fails_and_names_itself(self):
        for key in pj.FIELDS:
            record = permitted()
            del record[key]
            outcome, problems = pj.check(record)
            self.assertEqual(outcome, pj.INVALID, key)
            self.assertTrue(any(f"`{key}`" in p for p in problems),
                            f"{key}: {problems}")

    def test_each_text_field_empty_fails(self):
        for key in pj.TEXT_FIELDS:
            record = permitted()
            record[key] = "   "
            self.assertEqual(pj.check(record)[0], pj.INVALID, key)

    def test_an_alternative_without_its_reason_fails(self):
        record = permitted()
        record["alternatives"] = [{"option": "Exponential backoff"}]
        self.assertEqual(pj.check(record)[0], pj.INVALID)

    def test_a_call_with_no_alternative_was_not_a_judgment(self):
        record = permitted()
        record["alternatives"] = []
        self.assertEqual(pj.check(record)[0], pj.INVALID)

    def test_out_of_vocabulary_values_fail(self):
        for key, value in (("stage", "review"),
                           ("reviewer_disposition", "approved"),
                           ("touches", ["performance"])):
            record = permitted()
            record[key] = value
            self.assertEqual(pj.check(record)[0], pj.INVALID, key)

    def test_absent_touches_is_not_empty_touches(self):
        record = permitted()
        del record["touches"]
        outcome, problems = pj.check(record)
        self.assertEqual(outcome, pj.INVALID)
        self.assertTrue(any("`touches`" in p for p in problems), problems)

    def test_a_worker_may_not_publish_a_disposition_it_set_itself(self):
        record = dict(permitted(), reviewer_disposition="accepted")
        self.assertEqual(pj.check(record, worker=True)[0], pj.INVALID)
        self.assertEqual(pj.check(record)[0], pj.VALID)

    def test_a_text_field_cannot_carry_a_second_record(self):
        injected = json.dumps(dict(permitted(), id="PJ-9"))
        record = permitted()
        record["rationale"] += f"\n```pending-judgment\n{injected}\n```"
        self.assertEqual(pj.check(record)[0], pj.INVALID)
        record = permitted()
        record["alternatives"][0]["why_not"] = "a\nb"
        self.assertEqual(pj.check(record)[0], pj.INVALID)

    def test_a_fenced_or_multiline_note_is_refused(self):
        for note in ("first line\n```\nsecond", "a\nb", 7):
            with self.subTest(note=note):
                self.assertIsNotNone(pj.check_note(note))
                self.assertEqual(
                    pj.check(dict(permitted(), reviewer_note=note))[0],
                    pj.INVALID)

    def test_a_duplicate_id_fails(self):
        record = permitted()
        worst, results = pj.check_all([record, copy.deepcopy(record)])
        self.assertEqual(worst, pj.INVALID)
        self.assertTrue(any("appears twice" in p
                            for _, _, problems in results for p in problems),
                        results)

    def test_a_record_that_is_not_an_object_is_invalid(self):
        self.assertEqual(pj.check(["not", "an", "object"])[0], pj.INVALID)


class UnreadableIsNeverAPass(unittest.TestCase):
    def test_a_body_with_no_records_reads_as_none(self):
        self.assertEqual(pj.records_in("## Summary\nx\n"), ([], 0))

    def test_a_broken_block_is_counted_broken(self):
        self.assertEqual(pj.records_in("```pending-judgment\n{not json\n```\n"),
                         ([], 1))

    def test_an_unreadable_block_beside_a_valid_one_is_not_dropped(self):
        # One valid record plus a block the fence pattern cannot parse must
        # not pass on the valid one alone: the other record went missing.
        valid = pj.render([permitted()])
        payload = json.dumps(dict(permitted(), id="PJ-2"))
        for label, tail in (
                ("unclosed", f"```pending-judgment\n{payload}\n"),
                ("trailing space", f"```pending-judgment \n{payload}\n```\n"),
                ("tilde fence", f"~~~pending-judgment\n{payload}\n~~~\n")):
            with self.subTest(label):
                records, broken = pj.records_in(valid + "\n" + tail)
                self.assertEqual(broken, 1)
                self.assertEqual(len(records), 1)

    def test_a_crlf_body_reads_like_an_lf_one(self):
        body = pj.render([permitted()]).replace("\n", "\r\n")
        self.assertEqual(pj.records_in(body), ([permitted()], 0))

    def test_fenced_blocks_skips_what_it_cannot_parse(self):
        body = (pj.render([permitted()])
                + "\n```pending-judgment\n{not json\n```\n")
        self.assertEqual(len(pj.fenced_blocks(body)), 1)
        self.assertEqual(pj.records_in(body)[1], 1)


class DispositionLandsInItsOwnSection(unittest.TestCase):
    def test_a_disposition_refreshes_the_block_and_its_prose(self):
        before = body_of(permitted(), second())
        updated = dict(permitted(), reviewer_disposition="reverted")
        after, warnings = pj.apply_to_body(before, updated)
        self.assertEqual(warnings, [])
        self.assertTrue(after.startswith("## Summary\nRetry policy"))
        self.assertEqual(after.rstrip("\n").splitlines()[-1],
                         "Trailing words.")
        records, broken = pj.records_in(after)
        self.assertEqual(broken, 0)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["reviewer_disposition"], "reverted")
        # The other record is untouched, block and all.
        self.assertEqual(records[1], second())
        self.assertIn(pj.dump(updated), after)
        self.assertEqual(
            after.count("- **Reviewer disposition:** reverted"), 1)
        self.assertEqual(
            after.count("- **Reviewer disposition:** pending"), 1)
        self.assertEqual(pj.check_all(records)[0], pj.VALID)

    def test_a_note_is_recorded_in_prose_and_block(self):
        before = body_of(permitted(), second())
        updated = dict(permitted(), reviewer_disposition="accepted",
                       reviewer_note="Keeping the fixed backoff.")
        after, warnings = pj.apply_to_body(before, updated)
        self.assertEqual(warnings, [])
        self.assertEqual(
            after.count("- **Reviewer note:** Keeping the fixed backoff."), 1)
        records, broken = pj.records_in(after)
        self.assertEqual(broken, 0)
        self.assertEqual(records[0]["reviewer_note"],
                         "Keeping the fixed backoff.")
        self.assertNotIn("reviewer_note", records[1])

    def test_an_existing_note_line_is_replaced_not_duplicated(self):
        before = body_of(dict(permitted(), reviewer_note="first words"),
                         second())
        updated = dict(permitted(), reviewer_disposition="amended",
                       reviewer_note="second words")
        after, warnings = pj.apply_to_body(before, updated)
        self.assertEqual(warnings, [])
        self.assertNotIn("first words", after)
        self.assertEqual(after.count("- **Reviewer note:** second words"), 1)

    def test_an_unknown_id_leaves_the_body_unchanged(self):
        before = body_of(permitted(), second())
        after, warnings = pj.apply_to_body(before,
                                           dict(permitted(), id="PJ-9"))
        self.assertEqual(after, before)
        self.assertTrue(any("PJ-9" in w for w in warnings), warnings)

    def test_a_duplicate_id_leaves_the_body_unchanged(self):
        before = pj.render([permitted()]) + pj.render([permitted()])
        after, warnings = pj.apply_to_body(
            before, dict(permitted(), reviewer_disposition="accepted"))
        self.assertEqual(after, before)
        self.assertTrue(any("found 2" in w for w in warnings), warnings)

    def test_a_longer_sibling_id_never_lends_its_prose(self):
        # The target's own section carries no disposition prose, so only the
        # sibling's line is there to be (mis)matched. The block is still
        # swapped and the doubt lands in warnings.
        extra = dict(permitted(), id="PJ-1 extra")
        lines = pj.render([permitted()]).splitlines()
        lines.remove(pj.disposition_line(permitted()))
        before = pj.render([extra]) + "\n" + "\n".join(lines) + "\n"
        updated = dict(permitted(), reviewer_disposition="accepted")
        after, warnings = pj.apply_to_body(before, updated)
        self.assertTrue(any("PJ-1" in w for w in warnings), warnings)
        self.assertIn("- **Reviewer disposition:** pending", after)
        self.assertNotIn("- **Reviewer disposition:** accepted", after)
        records, broken = pj.records_in(after)
        self.assertEqual(broken, 0)
        by_id = {record["id"]: record for record in records}
        self.assertEqual(by_id["PJ-1 extra"]["reviewer_disposition"],
                         "pending")
        self.assertEqual(by_id["PJ-1"]["reviewer_disposition"], "accepted")

    def test_a_note_never_crosses_a_section_boundary(self):
        lines = pj.render([permitted()]).splitlines()
        fence_at = lines.index("```pending-judgment")
        injected = ["### Other notes",
                    "- **Reviewer note:** someone else's words"]
        before = "\n".join(lines[:fence_at] + injected
                           + lines[fence_at:]) + "\n"
        updated = dict(permitted(), reviewer_disposition="accepted",
                       reviewer_note="reviewer words")
        after, warnings = pj.apply_to_body(before, updated)
        self.assertTrue(warnings)
        self.assertIn("- **Reviewer note:** someone else's words", after)
        self.assertEqual(after.count("- **Reviewer note:** reviewer words"), 0)
        records, broken = pj.records_in(after)
        self.assertEqual(broken, 0)
        self.assertEqual(records[0]["reviewer_note"], "reviewer words")

    def test_a_block_with_no_header_above_it_keeps_its_prose_alone(self):
        before = ("```pending-judgment\n" + pj.dump(permitted()) + "\n```\n")
        updated = dict(permitted(), reviewer_disposition="accepted")
        after, warnings = pj.apply_to_body(before, updated)
        self.assertTrue(any("no `###` header" in w for w in warnings),
                        warnings)
        self.assertEqual(pj.records_in(after)[0][0]["reviewer_disposition"],
                         "accepted")

    def test_pending_is_never_a_reviewer_disposition(self):
        self.assertNotIn(pj.PENDING, pj.REVIEWER_DISPOSITIONS)
        self.assertIn(pj.PENDING, pj.DISPOSITIONS)

    def test_the_prose_prefixes_are_single_sourced(self):
        record = dict(permitted(), reviewer_disposition="accepted",
                      reviewer_note="words")
        self.assertTrue(
            pj.disposition_line(record).startswith(pj.DISPOSITION_PREFIX))
        self.assertTrue(pj.note_line(record).startswith(pj.NOTE_PREFIX))
        self.assertIsNone(pj.note_line(permitted()))
        self.assertIsNone(pj.note_line(dict(permitted(), reviewer_note="  ")))


if __name__ == "__main__":
    unittest.main()

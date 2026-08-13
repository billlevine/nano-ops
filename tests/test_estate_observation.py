#!/usr/bin/env python3
"""Tests for the absence rule stated once (estate vision V-02).

Four groups, in the order the contract is built:

  the FOUR — the warrant classes, the one policy table, and the two
  distinctions this primitive exists to keep (`failed` vs `not_attempted`,
  `not_attempted` vs `not_due`);
  UNREPRESENTABILITY — the properties that make an unwarranted reading
  impossible to build rather than merely discouraged: no predicate parameter,
  no default class, no vocabulary that can never conclude an absence;
  COMPOSITION — `absence_proved` over a set of readings, including the empty
  set, which is `not_due` and never proved;
  ADOPTION and DRIFT — lib/brief_manifest.py derives its statuses and its
  predicate from the primitive, and the two engines that keep their own copies
  (the briefer's brief.py, the spotter's track.py) are checked against the
  registered vocabularies so a word cannot be reclassified in one place only.

Run: PYTHONPATH=lib python3 tests/test_estate_observation.py  (the path is set
up below too, so a bare `python3 tests/test_estate_observation.py` also works).
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "lib"))

import estate_observation as O          # noqa: E402
import brief_manifest as M              # noqa: E402

TRACK = (REPO / "loops" / "pr-tracker" / ".claude" / "skills" / "pr-tracker"
         / "scripts" / "track.py")
BRIEF = (REPO / "loops" / "morning-brief" / ".claude" / "skills"
         / "morning-brief" / "scripts" / "brief.py")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the four
# --------------------------------------------------------------------------- #
class TheFourClasses(unittest.TestCase):

    def test_there_are_exactly_four_and_the_policy_covers_all_of_them(self):
        self.assertEqual(O.CLASSES, ("observed", "failed", "not_attempted",
                                     "not_due"))
        self.assertEqual(set(O.ABSENCE_IS_EVIDENCE), set(O.CLASSES))

    def test_only_a_look_that_happened_and_worked_proves_an_absence(self):
        """The entire policy. If this table ever grows a second True, the rule
        has changed and every consumer's meaning changed with it."""
        proves = {c for c, ok in O.ABSENCE_IS_EVIDENCE.items() if ok}
        self.assertEqual(proves, {O.OBSERVED})

    def test_failed_and_not_attempted_are_different_facts(self):
        """One is weather and one is setup. A single word for both hides
        whichever matters this morning — the whole reason both vocabularies
        carry more than one word for `broken`."""
        broke = O.reading("briefer", "digest", "error")
        never_ran = O.reading("briefer", "digest", "no_tool")
        self.assertEqual(broke["warrant"], O.FAILED)
        self.assertEqual(never_ran["warrant"], O.NOT_ATTEMPTED)
        self.assertNotEqual(broke["warrant"], never_ran["warrant"])
        # …and they agree on the one thing they must agree on.
        self.assertFalse(broke["absence_is_evidence"])
        self.assertFalse(never_ran["absence_is_evidence"])

    def test_not_attempted_and_not_due_are_different_facts(self):
        """A hole versus a boundary. An operator chases the first and ignores
        the second, and collapsing them turns every deliberate skip into a
        false alarm."""
        hole = O.reading("briefer", "focus", "no_state")
        boundary = O.reading("briefer", "focus", "offline")
        self.assertEqual(hole["warrant"], O.NOT_ATTEMPTED)
        self.assertEqual(boundary["warrant"], O.NOT_DUE)

    def test_a_spotter_entry_nothing_could_have_produced_is_not_due(self):
        """P-03's `unqueried`: the board entry whose repo left config.toml.
        Nobody is watching it, which is not the same as it being finished."""
        self.assertEqual(O.SPOTTER.classify("unqueried"), O.NOT_DUE)

    def test_severity_leads_with_the_thing_that_broke(self):
        self.assertEqual(
            sorted(O.CLASSES, key=lambda c: O.SEVERITY[c]),
            [O.FAILED, O.NOT_ATTEMPTED, O.NOT_DUE, O.OBSERVED])


class PartialIsAModifierNotAFifthClass(unittest.TestCase):

    def test_a_partial_read_is_still_observed_and_still_proves_nothing(self):
        """The spotter contract's rule, and the briefer's `degraded`: what it
        saw is real, and the part nobody read is where an absence would hide."""
        r = O.reading("briefer", "pr_reviews", "ok", partial=True)
        self.assertEqual(r["warrant"], O.OBSERVED)
        self.assertFalse(r["absence_is_evidence"])

    def test_partial_only_ever_subtracts(self):
        for outcome in O.BRIEFER.outcomes:
            if O.BRIEFER.classify(outcome) != O.OBSERVED:
                continue
            whole = O.reading("briefer", "s", outcome)
            part = O.reading("briefer", "s", outcome, partial=True)
            self.assertTrue(whole["absence_is_evidence"])
            self.assertFalse(part["absence_is_evidence"])

    def test_a_reading_that_saw_nothing_cannot_claim_partial_coverage(self):
        for outcome in ("error", "no_tool", "offline"):
            with self.assertRaises(O.UnwarrantedReading):
                O.reading("briefer", "digest", outcome, partial=True)


# --------------------------------------------------------------------------- #
# unrepresentability — the point of the exercise
# --------------------------------------------------------------------------- #
class AnUnwarrantedReadingCannotBeBuilt(unittest.TestCase):

    def test_the_predicate_is_not_a_parameter(self):
        """Not "you should not pass it" — there is nowhere to put it. This is
        the difference between a primitive and a validator somebody has to
        remember to call."""
        with self.assertRaises(TypeError):
            O.reading("briefer", "digest", "error", absence_is_evidence=True)

    def test_the_predicate_is_derived_on_every_reading(self):
        for subsystem in O.registered():
            vocab = O.vocabulary(subsystem)
            for outcome, warrant in vocab.outcomes.items():
                r = O.reading(subsystem, "s", outcome)
                self.assertEqual(r["absence_is_evidence"],
                                 O.ABSENCE_IS_EVIDENCE[warrant])

    def test_an_unclassified_outcome_raises_rather_than_defaulting(self):
        """A default would let a new outcome word inherit `observed` by saying
        nothing, which is the exact failure this module exists to prevent."""
        with self.assertRaises(O.UnwarrantedReading) as caught:
            O.reading("briefer", "digest", "rate_limited")
        self.assertIn("warrant class", str(caught.exception))

    def test_an_unregistered_subsystem_cannot_record_at_all(self):
        with self.assertRaises(O.UnwarrantedReading) as caught:
            O.reading("weather-loop", "sky", "ok")
        self.assertIn("no registered", str(caught.exception))

    def test_reading_the_predicate_back_ignores_the_stored_field(self):
        """A hand-edited record cannot claim a warrant it was not given."""
        forged = dict(O.reading("briefer", "digest", "timeout"),
                      absence_is_evidence=True)
        self.assertFalse(O.absence_is_evidence(forged))

    def test_a_reading_with_no_recognisable_warrant_is_not_evidence(self):
        for junk in ({}, {"warrant": "probably_fine"}, None, "ok", []):
            self.assertFalse(O.absence_is_evidence(junk))


class AVocabularyIsCheckedWhenItIsDeclared(unittest.TestCase):

    def test_a_class_outside_the_four_is_refused(self):
        with self.assertRaises(O.UnwarrantedReading):
            O.Vocabulary("v", {"ok": O.OBSERVED, "meh": "probably_fine"})

    def test_a_vocabulary_that_can_never_conclude_an_absence_is_refused(self):
        """Not caution — a broken table. Every absence it saw would be held
        forever with no way out."""
        with self.assertRaises(O.UnwarrantedReading) as caught:
            O.Vocabulary("v", {"error": O.FAILED, "no_tool": O.NOT_ATTEMPTED})
        self.assertIn("could never conclude", str(caught.exception))

    def test_an_empty_vocabulary_is_refused(self):
        with self.assertRaises(O.UnwarrantedReading):
            O.Vocabulary("v", {})

    def test_registering_one_subsystem_twice_differently_is_refused(self):
        """Two classifications for one subsystem is the disagreement the
        registry exists to prevent."""
        O.register("t400-probe", {"ok": O.OBSERVED, "boom": O.FAILED})
        O.register("t400-probe", {"ok": O.OBSERVED, "boom": O.FAILED})  # same
        with self.assertRaises(O.UnwarrantedReading):
            O.register("t400-probe", {"ok": O.OBSERVED,
                                      "boom": O.NOT_ATTEMPTED})


# --------------------------------------------------------------------------- #
# composition
# --------------------------------------------------------------------------- #
class ComposingReadings(unittest.TestCase):

    def readings(self, *outcomes):
        return [O.reading("spotter", f"src{i}", o)
                for i, o in enumerate(outcomes)]

    def test_every_relevant_look_has_to_have_worked(self):
        got = O.absence_proved(self.readings("ok", "ok", "ok"), "acme/widgets#1")
        self.assertTrue(got["proved"])
        self.assertEqual(got["blocking"], [])

    def test_one_failure_is_enough_to_hold(self):
        got = O.absence_proved(self.readings("ok", "error", "ok"))
        self.assertFalse(got["proved"])
        self.assertEqual(got["warrant"], O.FAILED)
        self.assertEqual(len(got["blocking"]), 1)

    def test_the_empty_set_is_not_due_and_never_proved(self):
        """Nothing looked, which is not the same as nothing being there."""
        got = O.absence_proved([])
        self.assertFalse(got["proved"])
        self.assertEqual(got["warrant"], O.NOT_DUE)
        self.assertIn("nothing that could have produced", got["why"])

    def test_a_partial_reading_blocks_the_whole_absence(self):
        rows = [O.reading("briefer", "a", "ok"),
                O.reading("briefer", "b", "ok", partial=True)]
        self.assertFalse(O.absence_proved(rows)["proved"])

    def test_the_worst_warrant_leads(self):
        rows = self.readings("unqueried", "unavailable", "error")
        got = O.absence_proved(rows)
        self.assertEqual(got["warrant"], O.FAILED)
        self.assertEqual([r["warrant"] for r in got["blocking"]],
                         [O.FAILED, O.NOT_ATTEMPTED, O.NOT_DUE])

    def test_unwarranted_lists_the_same_rows_worst_first(self):
        rows = self.readings("ok", "unqueried", "error")
        self.assertEqual([r["outcome"] for r in O.unwarranted(rows)],
                         ["error", "unqueried"])


# --------------------------------------------------------------------------- #
# adoption: the briefer's manifest derives its answers from the primitive
# --------------------------------------------------------------------------- #
class TheManifestDerivesItsStatuses(unittest.TestCase):

    def test_status_for_outcome_is_derived_not_written_down(self):
        self.assertEqual(set(M.STATUS_FOR_OUTCOME), set(O.BRIEFER.outcomes))
        for outcome, warrant in O.BRIEFER.outcomes.items():
            self.assertEqual(M.STATUS_FOR_OUTCOME[outcome],
                             M.STATUS_FOR_WARRANT[warrant])

    def test_the_three_statuses_still_mean_what_they_meant(self):
        """The projection is new; the answers are not. P-11's own table."""
        self.assertEqual(M.STATUS_FOR_OUTCOME, {
            "ok": M.COMPLETED, "offline": M.UNAVAILABLE,
            "no_state": M.UNAVAILABLE, "no_tool": M.UNAVAILABLE,
            "error": M.FAILED, "unreadable": M.FAILED,
            "timeout": M.FAILED})

    def test_both_not_looked_classes_render_one_dot_and_keep_two_words(self):
        self.assertEqual(M.STATUS_FOR_WARRANT[O.NOT_ATTEMPTED],
                         M.STATUS_FOR_WARRANT[O.NOT_DUE])
        self.assertNotEqual(O.BRIEFER.classify("no_tool"),
                            O.BRIEFER.classify("offline"))

    def test_empty_is_evidence_answers_from_the_warrant(self):
        for outcome, warrant in O.BRIEFER.outcomes.items():
            env = {"source": "s", "outcome": outcome,
                   "status": M.STATUS_FOR_OUTCOME[outcome], "degraded": False}
            self.assertEqual(M.empty_is_evidence(env),
                             warrant == O.OBSERVED, outcome)

    def test_a_degraded_source_is_never_evidence(self):
        env = {"source": "s", "outcome": "ok", "status": M.COMPLETED,
               "degraded": True}
        self.assertFalse(M.empty_is_evidence(env))

    def test_a_status_that_contradicts_its_outcome_is_not_evidence(self):
        """The tightening the primitive buys. A hand-edited manifest claiming
        `completed` over a `timeout` used to be read as a quiet morning."""
        env = {"source": "s", "outcome": "timeout", "status": M.COMPLETED,
               "degraded": False}
        self.assertFalse(M.empty_is_evidence(env))

    def test_an_outcome_this_estate_never_defined_falls_back_to_the_status(self):
        """An unrecognised warrant is the case the rule refuses to guess about,
        so the fallback can only take the conservative half of the answer."""
        self.assertFalse(M.empty_is_evidence(
            {"outcome": "rate_limited", "status": M.FAILED}))
        self.assertTrue(M.empty_is_evidence(
            {"outcome": "rate_limited", "status": M.COMPLETED}))

    def test_the_manifest_helpers_still_agree_with_the_predicate(self):
        manifest = {"sources": {
            "a": {"source": "a", "outcome": "ok", "status": M.COMPLETED,
                  "degraded": False},
            "b": {"source": "b", "outcome": "timeout", "status": M.FAILED,
                  "degraded": False}}}
        self.assertFalse(M.is_complete(manifest))
        self.assertEqual([e["source"] for e in M.incomplete_sources(manifest)],
                         ["b"])


# --------------------------------------------------------------------------- #
# drift — the engines keep their own copies, so the copies are checked
# --------------------------------------------------------------------------- #
@unittest.skipUnless(TRACK.exists() and BRIEF.exists(),
                     "the spotter and briefer engines are an installation's own "
                     "loops; this drift check runs where they are present")
class TheEnginesAgreeWithTheRegistry(unittest.TestCase):
    """Loop engines do not import lib/ — they run against a fabricated repo
    root in their own tests, the same rule lib/estate_ledger.py states. So the
    binding is a drift check, exactly as it is for the phase and run
    vocabularies in the subsystem-event tests.

    The two engines are a spotter loop and a briefer loop, which carry an
    installation's own policy and are NOT part of this core. Where they are
    absent the check skips rather than fails: it is a binding between this
    registry and a copy that may or may not exist here, and a missing copy is
    not a drifted one. It resumes by itself in a repo that has them.
    """

    def test_the_spotter_declares_every_outcome_the_registry_classes(self):
        text = read(TRACK)
        for outcome in O.SPOTTER.outcomes:
            self.assertTrue(
                re.search(rf'^SOURCE_[A-Z_]+ = "{re.escape(outcome)}"', text,
                          re.M),
                f"the spotter has no constant for {outcome!r}")

    def test_the_spotter_classes_nothing_the_registry_has_not_seen(self):
        """A seventh source outcome in track.py with no entry here would be a
        word the estate cannot say whether an absence survives."""
        found = set(re.findall(r'^SOURCE_[A-Z_]+ = "([a-z_]+)"', read(TRACK),
                               re.M))
        self.assertEqual(found, set(O.SPOTTER.outcomes))

    def test_the_briefer_declares_every_outcome_the_registry_classes(self):
        text = read(BRIEF)
        for outcome in O.BRIEFER.outcomes:
            self.assertIn(f'"{outcome}"', text,
                          f"the briefer has no constant for {outcome!r}")

    def test_the_briefers_status_mapping_matches_the_derived_one(self):
        """brief.py keeps a checked copy of STATUS_FOR_OUTCOME. If the registry
        reclassifies an outcome and the engine does not follow, the manifest on
        disk and the reader rendering it disagree about one fetch."""
        text = read(BRIEF)
        want = {"completed": "SRC_COMPLETED", "failed": "SRC_FAILED",
                "unavailable": "SRC_UNAVAILABLE"}
        const = {"ok": "OUT_OK", "offline": "OUT_OFFLINE",
                 "no_state": "OUT_NO_STATE", "no_tool": "OUT_NO_TOOL",
                 "error": "OUT_ERROR", "unreadable": "OUT_UNREADABLE",
                 "timeout": "OUT_TIMEOUT"}
        for outcome, status in M.STATUS_FOR_OUTCOME.items():
            self.assertIn(f"    {const[outcome]}: {want[status]},", text,
                          f"the briefer's {outcome} -> {status} has drifted")

    def test_the_two_vocabularies_never_class_a_shared_word_differently(self):
        """`error` and `unreadable` are in both. If one subsystem ever decided
        `unreadable` meant "nothing ran", the estate would have one word with
        two absence rules — the thing V-02 exists to make impossible."""
        for outcome in set(O.SPOTTER.outcomes) & set(O.BRIEFER.outcomes):
            self.assertEqual(O.SPOTTER.classify(outcome),
                             O.BRIEFER.classify(outcome), outcome)


class TheScheduledRunReconcilerIsDeliberatelyOutside(unittest.TestCase):
    """P-08 is not a third copy of this rule, and the boundary is checked so a
    later refactor cannot quietly erase it. See the module docstring and
    docs/absence-contract.md for the argument."""

    def test_estate_runs_does_not_import_the_primitive(self):
        text = (REPO / "lib" / "estate_runs.py").read_text(encoding="utf-8")
        self.assertNotIn("estate_observation", text)

    def test_the_run_verdicts_are_not_the_four_classes(self):
        import estate_runs as R
        self.assertEqual(set(R.VERDICTS) & set(O.CLASSES), {O.FAILED})
        # …and even the shared word is a different claim: P-08's `failed` is a
        # run that ran and broke, not a look that could not be taken.
        for verdict in ("activity_only", "unobservable", "incomplete",
                        "missing", "no_activity"):
            self.assertIn(verdict, R.VERDICTS)
            self.assertNotIn(verdict, O.CLASSES)

    def test_the_boundary_is_written_down_where_someone_will_look(self):
        for path in (REPO / "lib" / "estate_observation.py",
                     REPO / "docs" / "absence-contract.md"):
            self.assertIn("P-08", path.read_text(encoding="utf-8"),
                          f"{path.name} does not say why P-08 is outside")


if __name__ == "__main__":
    unittest.main(verbosity=int(os.environ.get("VERBOSE", 1)))

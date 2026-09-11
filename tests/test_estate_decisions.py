#!/usr/bin/env python3
"""The filing-time decision-classification schema (t-475).

Four things are under test, and the third is the one worth having:

  the validator          lib/estate_decisions.py, in isolation
  the two writers        mechanic.py `propose` and `bin/followups add|attention`
                         refuse the same payloads, through the same rule
  absence vs "not recorded"
                         an ABSENT recommendation key is refused; the exact
                         words "not recorded" are accepted. That is the whole
                         point (docs/absence-contract.md): nobody looked and
                         there is nothing there are different facts, and a
                         schema that cannot tell them apart has re-invented
                         the bug it was filed to fix
  the two readers        `estate proposal show` and `followups show` print the
                         classification, and for a decision the choice itself
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
BIN_DIR = os.path.join(REPO_ROOT, "bin")
ESTATE = os.path.join(BIN_DIR, "estate")
FOLLOWUPS = os.path.join(BIN_DIR, "followups")
MECHANIC = os.path.join(REPO_ROOT, "loops", "mechanic", ".claude", "skills",
                        "mechanic", "scripts", "mechanic.py")

sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import estate_decisions as ed  # noqa: E402


def _carries_the_schema(path, token) -> bool:
    """True when a WRITER on disk carries the filing-time schema itself.

    The validator is core, but the two writers that are supposed to share it
    are separate components with their own extraction history. Where one still
    predates the schema it rejects the flags outright, so the tests below would
    report a missing re-sync as a validator bug. Checking the file rather than
    a version string means they resume by themselves once the writer lands.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return token in handle.read()
    except OSError:
        return False


def followups_carries_the_schema() -> bool:
    return _carries_the_schema(FOLLOWUPS, "estate_decisions")


def mechanic_carries_the_schema() -> bool:
    return _carries_the_schema(MECHANIC, "DECISION_KEYS")


NO_FOLLOWUPS_SCHEMA = ("bin/followups here predates the filing-time schema and "
                       "rejects its flags outright")
NO_MECHANIC_SCHEMA = ("mechanic.py here predates the filing-time schema and "
                      "does not declare its keys")


def a_decision(**over):
    """A complete decision payload. Everything below is required, so a test
    that cares about one key still carries the rest."""
    base = {
        "classification": "decision",
        "question": "Keep or retire the unused `asks` mechanism?",
        "alternatives": [
            {"option": "A — wire a consumer",
             "consequence": "a named subsystem starts filing durable asks"},
            {"option": "B — retire it",
             "consequence": "the verbs and the sweep go, M-29 stops flagging"},
        ],
        "recommendation": "retire it unless a consumer is named now",
        "defer_consequence": "M-29 stays red and the table keeps implying a "
                             "live channel with no user",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# the validator
# --------------------------------------------------------------------------- #
class ValidatorCase(unittest.TestCase):
    def refused(self, fields):
        with self.assertRaises(ed.DecisionFilingRefused) as caught:
            ed.validate(fields)
        return caught.exception

    def test_a_payload_with_no_classification_is_refused(self):
        error = self.refused({})
        self.assertEqual(error.key, "classification")
        for word in ("decision", "action", "clarification"):
            self.assertIn(word, str(error))

    def test_nothing_is_inferred_from_a_complete_decision_body(self):
        # The failure mode this schema exists to stop: a filing that carries
        # the whole choice and is quietly classed by whoever reads it.
        fields = a_decision()
        fields.pop("classification")
        self.assertEqual(self.refused(fields).key, "classification")

    def test_a_word_outside_the_three_is_refused(self):
        error = self.refused({"classification": "urgent"})
        self.assertEqual(error.key, "classification")
        self.assertIn("urgent", str(error))

    def test_an_action_needs_nothing_else(self):
        self.assertEqual(ed.validate({"classification": "action"}),
                         {"classification": "action"})

    def test_clarification_is_a_real_and_cheap_exit(self):
        # t-224's honest classification. Forcing its filer to manufacture an
        # option set would be worse than the gap that closed.
        self.assertEqual(ed.validate({"classification": "clarification"}),
                         {"classification": "clarification"})

    def test_the_classification_is_casefolded_and_stripped(self):
        self.assertEqual(
            ed.validate({"classification": "  Decision  ", **{
                k: v for k, v in a_decision().items()
                if k != "classification"}})["classification"], "decision")

    def test_a_decision_key_on_a_non_decision_is_refused(self):
        for key, value in (("question", "which one?"),
                           ("alternatives", [{"option": "a",
                                              "consequence": "b"}]),
                           ("recommendation", "do a"),
                           ("defer_consequence", "nothing happens")):
            for word in ("action", "clarification"):
                error = self.refused({"classification": word, key: value})
                self.assertEqual(error.key, key)
                self.assertIn(word, str(error))

    def test_a_complete_decision_survives_intact(self):
        self.assertEqual(ed.validate(a_decision()), a_decision())

    def test_a_decision_with_no_question_is_refused(self):
        fields = a_decision(); fields.pop("question")
        self.assertEqual(self.refused(fields).key, "question")

    def test_an_empty_question_is_refused(self):
        self.assertEqual(self.refused(a_decision(question="   ")).key,
                         "question")

    def test_a_decision_with_no_alternatives_is_refused(self):
        fields = a_decision(); fields.pop("alternatives")
        self.assertEqual(self.refused(fields).key, "alternatives")

    def test_one_alternative_is_not_a_choice(self):
        error = self.refused(a_decision(
            alternatives=[{"option": "a", "consequence": "b"}]))
        self.assertEqual(error.key, "alternatives")
        self.assertIn("action", str(error))

    def test_an_option_with_no_consequence_is_refused(self):
        # t-27, exactly: two named recovery approaches and nothing said about
        # what either one costs.
        error = self.refused(a_decision(alternatives=[
            {"option": "reconciliation sweep", "consequence": ""},
            {"option": "idempotent rerun", "consequence": "retries the lot"}]))
        self.assertEqual(error.key, "alternatives")
        self.assertIn("reconciliation sweep", str(error))

    def test_a_consequence_with_no_option_is_refused(self):
        self.assertEqual(self.refused(a_decision(alternatives=[
            {"option": "", "consequence": "something happens"},
            {"option": "b", "consequence": "something else"}])).key,
            "alternatives")

    def test_alternatives_must_be_a_list(self):
        for bad in ("A or B", {"option": "a"}, 7):
            self.assertEqual(self.refused(a_decision(alternatives=bad)).key,
                             "alternatives")

    def test_a_decision_with_no_defer_consequence_is_refused(self):
        fields = a_decision(); fields.pop("defer_consequence")
        error = self.refused(fields)
        self.assertEqual(error.key, "defer_consequence")
        self.assertIn("does nothing", str(error))


class AbsenceVersusNotRecordedCase(unittest.TestCase):
    """The distinction the whole schema turns on (docs/absence-contract.md).

    A filer who weighed two options and could not pick is telling the reader
    something true, and t-27 and t-166 are both that record. A missing key
    tells the reader nothing, and reading it as "no recommendation" would be
    the estate concluding an absence from a look nobody took.
    """

    def test_an_absent_recommendation_key_is_refused(self):
        fields = a_decision(); fields.pop("recommendation")
        with self.assertRaises(ed.DecisionFilingRefused) as caught:
            ed.validate(fields)
        self.assertEqual(caught.exception.key, "recommendation")
        self.assertIn("absent key is not an answer", str(caught.exception))
        self.assertIn("absence-contract", str(caught.exception))

    def test_the_exact_words_not_recorded_are_accepted(self):
        out = ed.validate(a_decision(recommendation="not recorded"))
        self.assertEqual(out["recommendation"], "not recorded")

    def test_not_recorded_is_matched_case_and_space_insensitively(self):
        for spelling in ("Not Recorded", "  NOT RECORDED  ", "not recorded"):
            self.assertEqual(
                ed.validate(a_decision(recommendation=spelling))
                ["recommendation"], "not recorded")

    def test_an_empty_recommendation_is_an_absence_wearing_a_key(self):
        error = None
        try:
            ed.validate(a_decision(recommendation="   "))
        except ed.DecisionFilingRefused as exc:
            error = exc
        self.assertIsNotNone(error)
        self.assertEqual(error.key, "recommendation")
        self.assertIn("not recorded", str(error))

    def test_a_none_recommendation_reads_as_absent_not_as_empty(self):
        # argparse hands None for a flag nobody passed. Collapsing that into
        # "" here is how the distinction would quietly die.
        error = None
        try:
            ed.validate(a_decision(recommendation=None))
        except ed.DecisionFilingRefused as exc:
            error = exc
        self.assertIn("missing required key", str(error))


class AlternativeParsingCase(unittest.TestCase):
    def test_the_separator_splits_option_from_consequence(self):
        self.assertEqual(ed.parse_alternative("retire it :: the sweep goes"),
                         {"option": "retire it",
                          "consequence": "the sweep goes"})

    def test_a_consequence_may_contain_the_separator(self):
        parsed = ed.parse_alternative("a :: b :: c")
        self.assertEqual(parsed["option"], "a")
        self.assertEqual(parsed["consequence"], "b :: c")

    def test_an_alternative_with_no_separator_is_refused(self):
        with self.assertRaises(ed.DecisionFilingRefused) as caught:
            ed.parse_alternative("just an option")
        self.assertEqual(caught.exception.key, "alternatives")
        self.assertIn("::", str(caught.exception))


# --------------------------------------------------------------------------- #
# the CLI surfaces
# --------------------------------------------------------------------------- #
class _CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "estate")
        os.makedirs(self.state)
        self.env = dict(os.environ, ESTATE_STATE_DIR=self.state)
        self.env.pop("FOLLOWUPS_STATE_DIR", None)

    def run_cli(self, script, *args):
        return subprocess.run([sys.executable, script, *args],
                              capture_output=True, text=True, env=self.env)

    def estate(self, *args):
        return self.run_cli(ESTATE, *args)

    def followups(self, *args):
        return self.run_cli(FOLLOWUPS, *args)

    def task_refs(self, key):
        """The refs envelope one task carries.

        `estate task show` prints the row and then its event log, so the row
        is the FIRST JSON value on stdout rather than the whole of it.
        """
        row, _ = json.JSONDecoder().raw_decode(
            self.estate("task", "show", key).stdout)
        return json.loads(row["refs"])

    DECISION_FLAGS = (
        "--classification", "decision",
        "--question", "Keep or retire the unused `asks` mechanism?",
        "--alternative", "A — wire a consumer :: a named subsystem starts "
                         "filing durable asks",
        "--alternative", "B — retire it :: the verbs and the sweep go, M-29 "
                         "stops flagging",
        "--recommendation", "retire it unless a consumer is named now",
        "--defer-consequence", "M-29 stays red and the table keeps implying a "
                               "live channel with no user",
    )

    def without(self, flag):
        """DECISION_FLAGS with one flag and its value removed."""
        out, skip = [], False
        for arg in self.DECISION_FLAGS:
            if skip:
                skip = False
            elif arg == flag:
                skip = True
            else:
                out.append(arg)
        return out


class DecisionCheckCase(_CliCase):
    """`estate decision check` — the door a loop engine uses.

    Loop engines do not import lib/, so the mechanic reaches the one validator
    as a subprocess. A second copy inside mechanic.py is how the two writers
    would drift, which is the failure the schema is supposed to prevent.
    """

    def test_a_complete_decision_comes_back_normalized(self):
        result = self.estate("decision", "check", json.dumps(a_decision()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), a_decision())

    def test_a_refusal_names_the_key_and_exits_nonzero(self):
        fields = a_decision(); fields.pop("recommendation")
        result = self.estate("decision", "check", json.dumps(fields))
        self.assertEqual(result.returncode, 2)
        self.assertIn("recommendation", result.stderr)

    def test_it_reads_no_store(self):
        # A caller must be refused for the right reason even where the
        # database is unreachable.
        env = dict(self.env, ESTATE_STATE_DIR="/nonexistent/never/created")
        result = subprocess.run(
            [sys.executable, ESTATE, "decision", "check", '{"classification":"action"}'],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_junk_is_refused_as_junk(self):
        self.assertEqual(
            self.estate("decision", "check", "not json").returncode, 2)


class ProposalWriterCase(_CliCase):
    """`estate proposal add` accepts and does not require — and enforces
    coherence on whatever it is given."""

    def test_a_proposal_with_no_classification_still_files(self):
        # --adopt-task and bin/migrate-mechanic-proposals both file records
        # nobody can honestly classify. The gate lives at the writers.
        result = self.estate("proposal", "add", "a legacy condition")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_half_built_decision_is_refused_here_too(self):
        result = self.estate("proposal", "add", "half a decision",
                             *self.without("--recommendation"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("recommendation", result.stderr)

    def test_a_complete_decision_lands_in_refs(self):
        result = self.estate("proposal", "add", "a real choice",
                             *self.DECISION_FLAGS)
        self.assertEqual(result.returncode, 0, result.stderr)
        refs = self.task_refs(result.stdout.strip())
        self.assertEqual(refs["classification"], "decision")
        self.assertEqual(len(refs["alternatives"]), 2)
        self.assertEqual(refs["alternatives"][0]["option"], "A — wire a consumer")

    def test_the_fields_may_arrive_through_refs_json(self):
        # The mechanic's route: `decision check` normalizes, and the fields
        # ride in the refs envelope beside condition/desired_outcome.
        result = self.estate("proposal", "add", "filed by an engine",
                             "--refs", json.dumps(a_decision()))
        self.assertEqual(result.returncode, 0, result.stderr)
        refs = self.task_refs(result.stdout.strip())
        self.assertEqual(refs["recommendation"],
                         "retire it unless a consumer is named now")

    def test_a_broken_decision_in_refs_json_is_refused(self):
        fields = a_decision(); fields.pop("defer_consequence")
        result = self.estate("proposal", "add", "engine-filed and broken",
                             "--refs", json.dumps(fields))
        self.assertEqual(result.returncode, 2)
        self.assertIn("defer_consequence", result.stderr)


class ProposalReaderCase(_CliCase):
    """Acceptance check c1."""

    def test_show_prints_the_whole_choice(self):
        key = self.estate("proposal", "add",
                          "M-29 — the asks table has never held a row",
                          "--condition", "zero rows since creation",
                          "--desired-outcome", "either a consumer or no table",
                          "--completion-check", "M-29 stops flagging",
                          *self.DECISION_FLAGS).stdout.strip()
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("classification    decision", out)
        self.assertIn("Keep or retire the unused `asks` mechanism?", out)
        self.assertIn("A — wire a consumer — a named subsystem starts filing "
                      "durable asks", out)
        self.assertIn("B — retire it — the verbs and the sweep go", out)
        self.assertIn("retire it unless a consumer is named now", out)
        self.assertIn("defer consequence M-29 stays red", out)

    def test_show_prints_not_recorded_for_a_recommendation_that_says_so(self):
        key = self.estate("proposal", "add", "t-166's shape",
                          *self.without("--recommendation"),
                          "--recommendation", "not recorded").stdout.strip()
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("recommendation    not recorded", out)

    def test_a_legacy_proposal_reads_as_legacy(self):
        key = self.estate("proposal", "add", "filed before the schema").stdout.strip()
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("classification    (not recorded)", out)
        self.assertNotIn("defer consequence", out)

    def test_an_action_prints_only_its_classification(self):
        key = self.estate("proposal", "add", "already decided",
                          "--classification", "action").stdout.strip()
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("classification    action", out)
        self.assertNotIn("defer consequence", out)


class ProposalAmendCase(_CliCase):
    """`estate proposal amend` — the same schema, after filing (t-657).

    Acceptance check c1. This was hit live on t-603 and t-604: both filed
    `action`, both really choices, and the only tool for saying so afterwards
    was `estate note` — prose beside the record rather than the record. The
    property that matters is that a field set after filing is indistinguishable
    from one set at filing time, so a reader never has to know which happened.
    """

    def filed_as_an_action(self, title="t-603 shaped"):
        key = self.estate("proposal", "add", title,
                          "--condition", f"{title}: the condition",
                          "--desired-outcome", "it stops",
                          "--completion-check", "M-29 stops flagging",
                          "--classification", "action").stdout.strip()
        self.assertTrue(key.startswith("t-"), key)
        return key

    def amend(self, key, *flags):
        return self.estate("proposal", "amend", key, *flags)

    def classification_block(self, key):
        """`proposal show`'s rendering from the classification line down.

        The lines above it are the row and the P-09 review record, which two
        proposals filed under different conditions cannot share.
        """
        lines = self.estate("proposal", "show", key).stdout.splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.strip().startswith("classification"))
        return lines[start:]

    def test_an_action_gains_the_whole_decision_and_show_renders_it(self):
        key = self.filed_as_an_action()
        self.assertIn("classification    action",
                      self.estate("proposal", "show", key).stdout)
        result = self.amend(key, *self.DECISION_FLAGS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{key}: classification action -> decision",
                      result.stdout)
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("classification    decision", out)
        self.assertIn("Keep or retire the unused `asks` mechanism?", out)
        self.assertIn("A — wire a consumer — a named subsystem starts filing "
                      "durable asks", out)
        self.assertIn("B — retire it — the verbs and the sweep go", out)
        self.assertIn("retire it unless a consumer is named now", out)
        self.assertIn("defer consequence M-29 stays red", out)

    def test_set_after_filing_renders_identically_to_set_at_filing(self):
        """The done condition, as a comparison rather than a claim."""
        at_filing = self.estate("proposal", "add", "filed as a decision",
                                "--condition", "filed as a decision: observed",
                                *self.DECISION_FLAGS).stdout.strip()
        amended = self.filed_as_an_action("amended into one")
        self.amend(amended, *self.DECISION_FLAGS)
        self.assertEqual(self.classification_block(amended),
                         self.classification_block(at_filing))

    def test_the_fingerprint_condition_and_review_record_all_survive(self):
        key = self.filed_as_an_action()
        before = self.task_refs(key)
        self.amend(key, *self.DECISION_FLAGS)
        after = self.task_refs(key)
        for field in ("fingerprint", "condition", "desired_outcome",
                      "completion_check"):
            self.assertEqual(after[field], before[field], field)

    def test_the_stage_and_the_status_are_not_touched(self):
        key = self.filed_as_an_action()
        self.estate("proposal", "stage", key, "approved-backlog", "approved")
        self.amend(key, *self.DECISION_FLAGS)
        row, _ = json.JSONDecoder().raw_decode(
            self.estate("task", "show", key).stdout)
        self.assertEqual(row["stage"], "approved-backlog")
        self.assertEqual(row["status"], "ready")

    def test_a_downgrade_removes_the_decision_keys(self):
        """A `decision -> action` that left the four keys behind would keep
        answering as a decision to every reader that reads refs directly."""
        key = self.filed_as_an_action()
        self.amend(key, *self.DECISION_FLAGS)
        self.assertEqual(self.amend(key, "--classification", "action")
                         .returncode, 0)
        refs = self.task_refs(key)
        self.assertEqual(refs["classification"], "action")
        for gone in ("question", "alternatives", "recommendation",
                     "defer_consequence"):
            self.assertNotIn(gone, refs)
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("classification    action", out)
        self.assertNotIn("defer consequence", out)

    def test_a_half_built_decision_is_refused_and_changes_nothing(self):
        key = self.filed_as_an_action()
        result = self.amend(key, *self.without("--recommendation"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("recommendation", result.stderr)
        self.assertIn("absent key is not an answer", result.stderr)
        self.assertEqual(self.task_refs(key)["classification"], "action")

    def test_the_exact_words_not_recorded_are_accepted_here_too(self):
        key = self.filed_as_an_action()
        result = self.amend(key, *self.without("--recommendation"),
                            "--recommendation", "not recorded")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recommendation    not recorded",
                      self.estate("proposal", "show", key).stdout)

    def test_a_legacy_proposal_can_be_classified_for_the_first_time(self):
        key = self.estate("proposal", "add",
                          "filed before the schema").stdout.strip()
        result = self.amend(key, "--classification", "clarification")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{key}: classification none -> clarification",
                      result.stdout)
        self.assertIn("classification    clarification",
                      self.estate("proposal", "show", key).stdout)

    def test_an_amend_that_changes_nothing_records_nothing(self):
        # An unchanged observation is not an event. Re-running the same amend
        # is a caller being careful, not a second decision.
        key = self.filed_as_an_action()
        self.amend(key, *self.DECISION_FLAGS)
        again = self.amend(key, *self.DECISION_FLAGS)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("unchanged", again.stdout)
        log = self.estate("log", key).stdout
        self.assertEqual(log.count("classification: action -> decision"), 1)

    def test_the_change_is_on_the_record_with_the_reason(self):
        key = self.filed_as_an_action()
        self.amend(key, *self.DECISION_FLAGS, "--note",
                   "the owner asked for a recommendation on t-603",
                   "--actor", "hub")
        log = self.estate("log", key).stdout
        self.assertIn("classification: action -> decision", log)
        self.assertIn("the owner asked for a recommendation on t-603", log)
        self.assertIn("hub", log)

    def test_a_task_outside_the_lifecycle_is_refused(self):
        self.estate("task", "add", "ordinary work", "--kind", "generic")
        result = self.amend("t-1", "--classification", "action")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not in the proposal review lifecycle", result.stderr)

    def test_a_decided_proposal_is_refused_until_it_is_reopened(self):
        """A decision was made against a record. Rewriting the choice now
        would move the record out from under the answer."""
        key = self.filed_as_an_action()
        self.estate("proposal", "stage", key, "rejected", "not doing it")
        refused = self.amend(key, *self.DECISION_FLAGS)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("already been decided", refused.stderr)
        self.assertIn(f"estate proposal stage {key} pending-review",
                      refused.stderr)
        self.assertEqual(self.task_refs(key)["classification"], "action")
        self.estate("proposal", "stage", key, "pending-review", "reconsidered")
        self.assertEqual(self.amend(key, *self.DECISION_FLAGS).returncode, 0)

    def test_a_missing_task_is_refused(self):
        result = self.amend("t-99", "--classification", "action")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no task matches", result.stderr)

    def test_the_classification_is_required_not_inferred(self):
        # Same doctrine as the validator: a payload carrying the whole choice
        # is still not classified until somebody says which of the three it is.
        key = self.filed_as_an_action()
        result = self.amend(key, *self.without("--classification"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--classification", result.stderr)


@unittest.skipUnless(followups_carries_the_schema(), NO_FOLLOWUPS_SCHEMA)
class FollowupWriterCase(_CliCase):
    """`followups add` and `attention` — the other writer, the same rule."""

    def test_add_refuses_a_filing_with_no_classification(self):
        result = self.followups("add", "chase the vendor")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--classification", result.stderr)

    def test_attention_refuses_a_filing_with_no_classification(self):
        result = self.followups("attention", "the owner must decide", "--key", "k1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--classification", result.stderr)

    def test_add_accepts_an_action(self):
        result = self.followups("add", "renew the cert",
                                "--classification", "action")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_add_refuses_a_decision_with_an_absent_recommendation(self):
        result = self.followups("add", "pick a recovery model",
                                *self.without("--recommendation"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("recommendation", result.stderr)
        self.assertIn("absent key is not an answer", result.stderr)

    def test_add_accepts_a_decision_that_says_not_recorded(self):
        result = self.followups("add", "pick a recovery model",
                                *self.without("--recommendation"),
                                "--recommendation", "not recorded")
        self.assertEqual(result.returncode, 0, result.stderr)
        key = json.loads(result.stdout)["id"]
        self.assertIn("recommendation: not recorded",
                      self.followups("show").stdout)
        self.assertTrue(key.startswith("followup:"))

    def test_attention_refuses_a_decision_with_an_absent_defer_consequence(self):
        result = self.followups("attention", "the owner must decide", "--key", "k1",
                                *self.without("--defer-consequence"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("defer_consequence", result.stderr)

    def test_a_refused_attention_filing_writes_nothing(self):
        self.followups("attention", "the owner must decide", "--key", "k1",
                       *self.without("--question"))
        self.assertIn("no follow-ups on file", self.followups("show").stdout)

    def test_attention_refuses_before_it_looks_for_the_key(self):
        # A malformed filing must be refused whether or not this sweep happens
        # to be the second one. An item that links today and would have been
        # refused yesterday teaches nobody anything.
        self.followups("attention", "the owner must decide", "--key", "k1",
                       *self.DECISION_FLAGS)
        again = self.followups("attention", "same condition", "--key", "k1",
                               *self.without("--question"))
        self.assertEqual(again.returncode, 2)
        self.assertIn("question", again.stderr)

    def test_the_fields_ride_in_the_refs_envelope(self):
        self.followups("add", "pick a recovery model", "--source", "hub",
                       *self.DECISION_FLAGS)
        refs = self.task_refs("t-1")
        self.assertEqual(refs["classification"], "decision")
        self.assertEqual(refs["source"], "hub")
        self.assertEqual(refs["legacy_id"], "followup:1")
        self.assertEqual(refs["alternatives"][1]["option"], "B — retire it")

    def test_an_alternative_with_no_separator_is_refused(self):
        result = self.followups("add", "pick something",
                                "--classification", "decision",
                                "--question", "which?",
                                "--alternative", "just an option",
                                "--recommendation", "not recorded",
                                "--defer-consequence", "nothing moves")
        self.assertEqual(result.returncode, 2)
        self.assertIn("separator", result.stderr)


@unittest.skipUnless(followups_carries_the_schema(), NO_FOLLOWUPS_SCHEMA)
class FollowupReaderCase(_CliCase):
    def test_show_prints_the_whole_choice(self):
        self.followups("add", "pick a recovery model",
                       "--source", "pr-reviewer", *self.DECISION_FLAGS)
        out = self.followups("show").stdout
        self.assertIn("[decision] Keep or retire the unused `asks` mechanism?",
                      out)
        self.assertIn("A — wire a consumer — a named subsystem starts filing "
                      "durable asks", out)
        self.assertIn("recommendation: retire it unless a consumer is named "
                      "now", out)
        self.assertIn("if deferred: M-29 stays red", out)

    def test_show_names_a_clarification(self):
        self.followups("add", "Finish tearing down the warehouse "
                              "infrastructure",
                       "--context", "the owner's own request "
                                    "— no further scope given yet",
                       "--classification", "clarification")
        out = self.followups("show").stdout
        self.assertIn("[clarification]", out)

    def test_a_legacy_row_prints_no_classification_line(self):
        # `estate expired-asks --sweep` mints follow-up rows directly. They
        # predate this schema and must read as the legacy rows they are.
        self.estate("task", "add", "filed by hand", "--kind", "followup",
                    "--refs", json.dumps({"legacy_id": "followup:9",
                                          "source": "hub", "ref": None,
                                          "context": "", "resolution": None}))
        out = self.followups("show").stdout
        self.assertIn("followup:9", out)
        for word in ("[decision]", "[action]", "[clarification]"):
            self.assertNotIn(word, out)

    def test_json_and_add_output_are_untouched(self):
        # The legacy parity surfaces. bin/test_followups_shim.py compares them
        # against the frozen implementation byte for byte.
        added = json.loads(self.followups(
            "add", "an item", *self.DECISION_FLAGS).stdout)
        self.assertNotIn("classification", added)
        item = json.loads(self.followups("json").stdout)["items"]["followup:1"]
        self.assertNotIn("classification", item)


@unittest.skipUnless(mechanic_carries_the_schema(), NO_MECHANIC_SCHEMA)
class MechanicWriterCase(_CliCase):
    """`mechanic.py propose` — the same rule, reached as a subprocess."""

    def setUp(self):
        super().setUp()
        self.mech = os.path.join(self.tmp.name, "mechanic")
        os.makedirs(self.mech)
        self.env.update(MECHANIC_NOW="2026-08-12T02:30:00-04:00",
                        MECHANIC_STATE_DIR=self.mech, ESTATE_SCRIPT=ESTATE)

    def propose(self, **over):
        base = {"title": "the asks table has never held a row",
                "subsystem": "tasks", "condition": "zero rows since creation",
                "desired_outcome": "either a consumer or no table",
                "completion_check": "M-29 stops flagging"}
        base.update(over)
        return self.run_cli(MECHANIC, "propose", json.dumps(base))

    def history(self):
        path = os.path.join(self.mech, "history.jsonl")
        with open(path) as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_a_payload_with_no_classification_is_refused(self):
        result = self.propose()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("classification", result.stderr)

    def test_a_refused_payload_files_nothing_anywhere(self):
        self.propose()
        self.assertFalse(os.path.exists(
            os.path.join(self.mech, "history.jsonl")))
        self.assertEqual(self.estate("proposal", "list").stdout.strip(), "")

    def test_a_decision_with_an_absent_recommendation_is_refused(self):
        fields = a_decision(); fields.pop("recommendation")
        result = self.propose(**fields)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recommendation", result.stderr)
        self.assertIn("absent key is not an answer", result.stderr)

    def test_a_decision_that_says_not_recorded_is_accepted(self):
        result = self.propose(**a_decision(recommendation="not recorded"))
        self.assertEqual(result.returncode, 0, result.stderr)
        key = result.stdout.split()[0]
        self.assertIn("recommendation    not recorded",
                      self.estate("proposal", "show", key).stdout)

    def test_an_accepted_decision_reaches_the_store_and_the_history(self):
        result = self.propose(**a_decision())
        self.assertEqual(result.returncode, 0, result.stderr)
        key = result.stdout.split()[0]
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("classification    decision", out)
        self.assertIn("Keep or retire the unused `asks` mechanism?", out)
        self.assertIn("defer consequence M-29 stays red", out)
        line = self.history()[-1]
        self.assertEqual(line["classification"], "decision")
        self.assertEqual(len(line["alternatives"]), 2)

    def test_an_action_needs_nothing_more(self):
        result = self.propose(classification="action")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.history()[-1]["classification"], "action")

    def test_an_option_with_no_consequence_is_refused(self):
        result = self.propose(**a_decision(alternatives=[
            {"option": "sweep", "consequence": ""},
            {"option": "rerun", "consequence": "retries the whole thing"}]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("consequence", result.stderr)

    def test_the_key_names_stay_in_step_with_the_validator(self):
        # mechanic.py carries the key NAMES so a reader of that file can see
        # what a payload may hold. The rule itself lives in lib/; this is the
        # drift check that keeps the two spellings identical, the same way the
        # observation vocabularies are drift-checked.
        with open(MECHANIC) as handle:
            source = handle.read()
        start = source.index("DECISION_KEYS = (")
        end = source.index(")", start) + 1
        names = eval(source[start + len("DECISION_KEYS = "):end])  # noqa: S307
        self.assertEqual(tuple(names), ed.ALL_KEYS)


@unittest.skipUnless(followups_carries_the_schema(), NO_FOLLOWUPS_SCHEMA)
class ReFilingT224Case(_CliCase):
    """The done-condition's last clause, demonstrated rather than asserted.

    A filing shaped like t-224 — a scope-less instruction — lands as
    `clarification` when filed fresh. No live row is touched by this, and this
    store is a throwaway.
    """

    T224_TITLE = "Finish tearing down the warehouse infrastructure"
    T224_CONTEXT = ("the owner's own request -- no further "
                    "scope/context given yet")

    def test_it_cannot_be_filed_as_a_decision(self):
        result = self.followups("add", self.T224_TITLE,
                                "--context", self.T224_CONTEXT,
                                "--classification", "decision")
        self.assertEqual(result.returncode, 2)
        self.assertIn("question", result.stderr)

    def test_it_lands_as_a_clarification(self):
        result = self.followups("add", self.T224_TITLE,
                                "--context", self.T224_CONTEXT,
                                "--source", "hub",
                                "--classification", "clarification")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = self.followups("show").stdout
        self.assertIn(self.T224_TITLE[:70], out)
        self.assertIn("[clarification]", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)

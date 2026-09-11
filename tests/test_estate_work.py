#!/usr/bin/env python3
"""Tests for lib/estate_work.py — the composed estate work view (t-135).

The contract this pins down:

  * every derived field restates a rule that already exists somewhere else,
    and calls it rather than reimplementing it;
  * `blocks` is the exact transpose of `blocked_by`, so the two directions of
    one edge set cannot disagree;
  * a project rollup is COUNTED from the task rows on every read, so it cannot
    go stale when a task moves;
  * "unprojected" is a synthetic grouping in the read model and never a row in
    SQLite;
  * a store missing a table or a column loses the section that needed it and
    nothing else.

Run: python3 tests/test_estate_work.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "lib"))

import estate_attention  # noqa: E402
import estate_work  # noqa: E402

ESTATE = str(_HERE.parent / "bin" / "estate")
NOW = dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc)
UTC = dt.timezone.utc


def iso(days: float) -> str:
    return (NOW + dt.timedelta(days=days)).isoformat()


class _Store(unittest.TestCase):
    """A real store, built through the CLI — the read model must agree with
    the writer, and a hand-written INSERT would let the two drift."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = dict(os.environ, ESTATE_STATE_DIR=str(self.dir),
                        ESTATE_TZ="UTC")

    def estate(self, *args):
        result = subprocess.run([sys.executable, ESTATE, *args],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def connect(self):
        conn = sqlite3.connect(f"file:{self.dir / 'estate.db'}?mode=ro",
                               uri=True)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def build(self, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("tz", UTC)
        return estate_work.build(self.connect(), **kw)

    def tasks(self, view=None):
        return {t["id"]: t for t in (view or self.build())["tasks"]}


class DependencyEdgesTest(_Store):
    def chain(self):
        self.estate("task", "add", "the blocker", "--kind", "generic", "--ready")
        self.estate("task", "add", "the blocked one", "--kind", "generic", "--ready")
        self.estate("task", "add", "provenance", "--kind", "generic", "--ready")
        self.estate("dep", "add", "t-1", "t-2", "--kind", "blocks")
        self.estate("dep", "add", "t-3", "t-1", "--kind", "discovered-from")

    def test_every_edge_kind_is_carried_not_only_blocks(self):
        self.chain()
        view = self.build()
        self.assertEqual({e["kind"] for e in view["dependencies"]},
                         {"blocks", "discovered-from"})
        self.assertEqual(view["dep_counts"],
                         {"blocks": 1, "discovered-from": 1})

    def test_both_endpoints_arrive_resolved(self):
        self.chain()
        edge = next(e for e in self.build()["dependencies"]
                    if e["kind"] == "blocks")
        self.assertEqual(edge["from_title"], "the blocker")
        self.assertEqual(edge["from_status"], "ready")
        self.assertEqual(edge["to_title"], "the blocked one")
        self.assertEqual(edge["to_status"], "ready")

    def test_active_is_the_scheduling_rule_and_only_for_blocks(self):
        self.chain()
        edges = {e["kind"]: e for e in self.build()["dependencies"]}
        self.assertTrue(edges["blocks"]["active"])
        self.assertIsNone(edges["discovered-from"]["active"],
                          "provenance is not resolved by any task status")
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("done", "t-1", "--actor", "hub", "--summary", "done",
                    "--verification", "tests pass")
        edges = {e["kind"]: e for e in self.build()["dependencies"]}
        self.assertFalse(edges["blocks"]["active"])

    def test_blocks_is_the_exact_transpose_of_blocked_by(self):
        """One edge set, two readings. They cannot disagree by construction."""
        self.chain()
        tasks = self.tasks()
        self.assertEqual(tasks["t-2"]["blocked_by"], ["t-1"])
        self.assertEqual(tasks["t-1"]["blocks"], ["t-2"])
        for task in tasks.values():
            for blocker in task["blocked_by"]:
                self.assertIn(task["id"], tasks[blocker]["blocks"])
            for held in task["blocks"]:
                self.assertIn(task["id"], tasks[held]["blocked_by"])

    def test_both_directions_clear_together_when_the_blocker_finishes(self):
        self.chain()
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("done", "t-1", "--actor", "hub", "--summary", "done",
                    "--verification", "tests pass")
        tasks = self.tasks()
        self.assertEqual(tasks["t-2"]["blocked_by"], [])
        self.assertEqual(tasks["t-1"]["blocks"], [])
        self.assertEqual(tasks["t-2"]["status"], "ready",
                         "being blocked was never a status")

    def test_an_unknown_edge_kind_is_carried_not_dropped(self):
        """"All relationships" has to mean all of them. A kind this module has
        not heard of is still an edge somebody wrote."""
        self.chain()
        conn = sqlite3.connect(self.dir / "estate.db")
        conn.execute("INSERT INTO task_deps VALUES ('t-2','t-3','supersedes','x')")
        conn.commit(); conn.close()
        edge = next(e for e in self.build()["dependencies"]
                    if e["kind"] == "supersedes")
        self.assertEqual(edge["from_title"], "the blocked one")
        self.assertIsNone(edge["active"], "only `blocks` has an active reading")

    def test_titles_are_resolved_so_no_reader_scans_the_task_array(self):
        self.chain()
        tasks = self.tasks()
        self.assertEqual(tasks["t-2"]["blocked_by_titles"],
                         {"t-1": "the blocker"})
        self.assertEqual(tasks["t-1"]["blocks_titles"],
                         {"t-2": "the blocked one"})


class ProposalViewTest(_Store):
    def add(self, title="Disk fills during export"):
        return self.estate(
            "proposal", "add", title,
            "--condition", "the export fills the disk",
            "--desired-outcome", "exports stay below the disk budget",
            "--completion-check", "run estate backup and check free space")

    def test_full_review_record_is_normalized(self):
        self.assertEqual(self.add(), "t-1")
        view = self.build()
        self.assertEqual(view["proposals"], ["t-1"])
        self.assertEqual(view["proposal_counts"]["pending-review"], 1)
        proposal = self.tasks(view)["t-1"]["proposal"]
        self.assertEqual(proposal["condition"], "the export fills the disk")
        self.assertEqual(proposal["desired_outcome"],
                         "exports stay below the disk budget")
        self.assertEqual(proposal["completion_check"],
                         "run estate backup and check free space")
        self.assertFalse(proposal["legacy"])

    def test_recurrence_count_and_last_time_are_derived_from_events(self):
        self.add()
        self.add("Disk filled again")
        proposal = self.tasks()["t-1"]["proposal"]
        self.assertEqual(proposal["recurrences"], 1)
        self.assertIsNotNone(proposal["last_recurrence_at"])

    def test_legacy_review_holes_are_explicit(self):
        self.estate("proposal", "add", "old proposal")
        proposal = self.tasks()["t-1"]["proposal"]
        self.assertTrue(proposal["legacy"])
        self.assertIsNone(proposal["completion_check"])


class StagedRegardlessOfKindTest(_Store):
    """t-660 — the proposals panel's membership test is the STAGE column.

    `estate proposal add --adopt-task` stages an existing task in place and
    keeps its own kind, so a follow-up carrying a live approve/reject decision
    is a first-class member of this set. t-193 was staged `approved-backlog`
    and the owner could only find it through
    `estate task list --kind followup`, and the diagnosis written up from that
    was that the view filtered on kind. It never has. These tests are what
    stops one being added.
    """

    def adopt(self, kind="followup", stage="approved-backlog",
              title="extraction:2 — pick a baseline"):
        task = self.estate("task", "add", title, "--kind", kind)
        self.assertEqual(
            self.estate("proposal", "add", title,
                        "--adopt-task", task, "--stage", stage,
                        "--condition", f"{title} — the record drifted",
                        "--desired-outcome", "one baseline, chosen on purpose",
                        "--completion-check", "the skills agree again"),
            task)
        return task

    def test_a_staged_followup_is_in_the_proposals_set(self):
        task = self.adopt()
        view = self.build()
        self.assertIn(task, view["proposals"])
        self.assertEqual(view["proposal_counts"]["approved-backlog"], 1)

    def test_adoption_leaves_the_kind_alone(self):
        """The panel widens; the task is NOT reclassified."""
        task = self.adopt()
        row = self.tasks()[task]
        self.assertEqual(row["kind"], "followup")
        self.assertEqual(row["stage"], "approved-backlog")
        self.assertIsNotNone(row["followup"],
                             "it is still a follow-up to every other panel")

    def test_every_stage_admits_every_kind(self):
        for n, stage in enumerate(estate_work.PROPOSAL_STAGES):
            with self.subTest(stage=stage):
                task = self.estate("task", "add", f"adopted {n}",
                                   "--kind", "inbox-message")
                self.estate("proposal", "add", f"adopted {n}",
                            "--adopt-task", task, "--stage", stage)
                self.assertIn(task, self.build()["proposals"])

    def test_an_unstaged_task_is_not_in_the_set_whatever_its_kind(self):
        """The stage is the whole test, so it has to exclude as well as
        include — a `proposal`-kind row with no stage is not under review."""
        task = self.estate("task", "add", "never filed", "--kind", "proposal")
        view = self.build()
        self.assertNotIn(task, view["proposals"])
        self.assertIsNone(self.tasks(view)[task]["proposal"])

    def test_the_stats_grid_counts_exactly_the_rows_the_panel_renders(self):
        """`proposal_counts` is the stage strip above the same list. Both are
        computed from one set here so widening one cannot leave the other
        behind — a count that disagrees with its list is a defect."""
        self.adopt()
        self.adopt(kind="proposal", stage="pending-review",
                   title="the export fills the disk")
        self.estate("task", "add", "unstaged", "--kind", "followup")
        view = self.build()
        counted = sum(view["proposal_counts"][s]
                      for s in estate_work.PROPOSAL_STAGES)
        self.assertEqual(counted, len(view["proposals"]))
        self.assertEqual(len(view["proposals"]), 2)

    def test_no_kind_constant_invites_the_narrowing_back(self):
        """A `PROPOSAL_KIND = "proposal"` sat in this module read by nothing,
        and t-660 was filed and dispatched on the belief that it was the
        panel's filter. Naming a rule that does not exist is a defect."""
        self.assertFalse(hasattr(estate_work, "PROPOSAL_KIND"))


class DecisionSurfaceTest(_Store):
    """t-476 — which stored field is the question, and which is the
    recommendation. One rule; both renderers read it."""

    def test_todays_fields_answer_and_the_row_says_which_one_did(self):
        self.estate("proposal", "add", "Disk fills during export",
                    "--condition", "the export fills the disk",
                    "--desired-outcome", "exports stay below the budget",
                    "--completion-check", "check free space")
        surface = estate_work.decision_surface(
            dict(self.connect().execute(
                "SELECT * FROM tasks WHERE id='t-1'").fetchone()))
        self.assertEqual(surface["question"], "the export fills the disk")
        self.assertEqual(surface["question_field"], "condition")
        self.assertEqual(surface["recommendation"],
                         "exports stay below the budget")
        self.assertEqual(surface["recommendation_field"], "desired_outcome")

    def test_an_explicit_key_wins_over_the_field_that_answers_today(self):
        self.estate("task", "add", "later schema", "--kind", "proposal",
                    "--refs", json.dumps({"condition": "observed something",
                                          "question": "ship it or drop it?",
                                          "desired_outcome": "the old field",
                                          "recommendation": "ship it"}))
        surface = estate_work.decision_surface(
            dict(self.connect().execute(
                "SELECT * FROM tasks WHERE id='t-1'").fetchone()))
        self.assertEqual(surface["question"], "ship it or drop it?")
        self.assertEqual(surface["question_field"], "question")
        self.assertEqual(surface["recommendation"], "ship it")
        self.assertEqual(surface["recommendation_field"], "recommendation")

    def test_absent_is_none_and_never_the_title(self):
        self.estate("task", "add", "a title that is not a question",
                    "--kind", "generic")
        surface = estate_work.decision_surface(
            dict(self.connect().execute(
                "SELECT * FROM tasks WHERE id='t-1'").fetchone()))
        for key in ("question", "recommendation", "context", "context_line"):
            self.assertIsNone(surface[key], key)
        self.assertIsNone(surface["question_field"])

    def test_blank_is_the_same_absence_as_missing(self):
        self.estate("task", "add", "blank fields", "--kind", "proposal",
                    "--refs", json.dumps({"condition": "   ",
                                          "desired_outcome": ""}))
        surface = estate_work.decision_surface(
            dict(self.connect().execute(
                "SELECT * FROM tasks WHERE id='t-1'").fetchone()))
        self.assertIsNone(surface["question"])
        self.assertIsNone(surface["recommendation_field"])

    def test_context_prefers_the_envelope_and_falls_back_to_intent(self):
        self.estate("task", "add", "from intent", "--kind", "generic",
                    "--intent", "why this task exists")
        self.estate("task", "add", "from context", "--kind", "followup",
                    "--intent", "the column", "--refs",
                    json.dumps({"context": "the envelope\nsecond line"}))
        rows = {r["id"]: dict(r) for r in
                self.connect().execute("SELECT * FROM tasks")}
        intent = estate_work.decision_surface(rows["t-1"])
        self.assertEqual(intent["context"], "why this task exists")
        self.assertEqual(intent["context_field"], "intent")
        envelope = estate_work.decision_surface(rows["t-2"])
        self.assertEqual(envelope["context_field"], "context")
        self.assertEqual(envelope["context_line"], "the envelope")

    def test_the_proposal_envelope_ships_the_name_not_a_second_copy(self):
        self.estate("proposal", "add", "Disk fills",
                    "--condition", "the export fills the disk",
                    "--desired-outcome", "exports stay below the budget",
                    "--completion-check", "check free space")
        proposal = self.tasks()["t-1"]["proposal"]
        self.assertEqual(proposal["question_field"], "condition")
        self.assertEqual(proposal["recommendation_field"], "desired_outcome")
        # The text is already on the envelope under its own key. Shipping it
        # again would be the duplication §4.6 refused.
        #
        # The KEY is on every proposal envelope since t-475 made `question` and
        # `recommendation` stored fields in their own right, so the assertion
        # is about the VALUE: answered by `condition`, this row's `question`
        # stays empty rather than carrying a second copy of a string already
        # two keys away. (The `assertNotIn` this replaces could not have passed
        # after t-475 and had been red on main since.)
        self.assertIsNone(proposal["question"])
        self.assertIsNone(proposal["recommendation"])

    def test_a_key_outside_the_envelope_does_ship_its_text(self):
        self.estate("task", "add", "later schema", "--kind", "proposal",
                    "--stage", "pending-review",
                    "--refs", json.dumps({"question": "ship it or drop it?"}))
        proposal = self.tasks()["t-1"]["proposal"]
        self.assertEqual(proposal["question"], "ship it or drop it?")
        self.assertEqual(proposal["question_field"], "question")

    def test_only_attention_rows_carry_a_context_line(self):
        self.estate("task", "add", "waiting", "--kind", "followup", "--refs",
                    json.dumps({"context": "first line\nrest of it"}))
        self.estate("task", "add", "ordinary", "--kind", "generic",
                    "--intent", "not in the attention set")
        tasks = self.tasks()
        self.assertEqual(tasks["t-1"]["context_line"], "first line")
        self.assertEqual(tasks["t-1"]["context_field"], "context")
        self.assertNotIn("context_line", tasks["t-2"])


class ClampTest(unittest.TestCase):
    """t-721 — the summary line, and the bug that made it a paragraph.

    Pure, so the boundary is tested by moving the limit rather than by finding
    a row in the store that happens to sit on it.
    """

    def test_a_blob_with_no_newline_is_clamped_rather_than_shipped_whole(self):
        # F7 exactly: `first_line` finds no newline, so before this the whole
        # 500-character paragraph reached the page as "one line".
        blob = "word " * 200
        self.assertEqual(estate_work.first_line(blob), blob.strip())
        line, clamped = estate_work.one_line(blob)
        self.assertTrue(clamped)
        self.assertLessEqual(len(line), estate_work.CONTEXT_LINE_MAX)
        self.assertTrue(line.endswith(estate_work.ELLIPSIS))

    def test_a_short_line_is_returned_untouched_and_says_it_was_not_cut(self):
        self.assertEqual(estate_work.one_line("short enough"),
                         ("short enough", False))
        self.assertEqual(estate_work.clamp(None, 80), (None, False))
        self.assertEqual(estate_work.clamp("   ", 80), (None, False))

    def test_the_cut_lands_on_a_word_boundary_when_one_is_near_the_end(self):
        text = "alpha beta gamma delta epsilon"
        line, clamped = estate_work.clamp(text, 20)
        self.assertTrue(clamped)
        self.assertEqual(line, "alpha beta gamma" + estate_work.ELLIPSIS)
        # And a single unbroken token is cut hard rather than dropped: showing
        # part of it beats showing none of it.
        long_token = "x" * 60
        self.assertEqual(estate_work.clamp(long_token, 20),
                         ("x" * 19 + estate_work.ELLIPSIS, True))

    def test_a_headline_takes_the_first_line_and_the_full_title_survives(self):
        title = "A title that runs well past eighty characters " * 3
        surface = estate_work.decision_surface({"title": title})
        self.assertTrue(surface["headline_clamped"])
        self.assertLessEqual(len(surface["headline"]),
                             estate_work.HEADLINE_MAX)
        # Nothing here rewrote the title; the clamp is an ADDED key.
        self.assertNotIn("title", surface)


class DecisionStateTest(unittest.TestCase):
    """t-721 — what a row says about its own decision, and what it does not.

    The rule is a READING of what was filed. Nothing infers a classification
    for a row that declared none: most rows on a real set are in exactly that
    condition and `unstated` is the answer they get.
    """

    def sketch(self, **over):
        fields = {"classification": "decision", "question": "which one?",
                  "alternatives": [{"option": "a", "consequence": "x"},
                                   {"option": "b", "consequence": "y"}],
                  "recommendation": "a", "defer_consequence": "it rots"}
        fields.update(over)
        return fields

    def test_a_complete_decision_is_sketched(self):
        self.assertEqual(estate_work.decision_state(self.sketch()),
                         (estate_work.SKETCHED, []))

    def test_a_decision_missing_a_part_names_the_part(self):
        state, missing = estate_work.decision_state(
            self.sketch(recommendation="", defer_consequence=None))
        self.assertEqual(state, estate_work.INCOMPLETE)
        self.assertEqual(missing, ["recommendation", "defer_consequence"])
        # One alternative is not a choice — the same floor the filing gate
        # enforces, read back rather than re-invented.
        state, missing = estate_work.decision_state(
            self.sketch(alternatives=[{"option": "a", "consequence": "x"}]))
        self.assertEqual((state, missing),
                         (estate_work.INCOMPLETE, ["alternatives"]))

    def test_the_recorded_no_pick_sentinel_is_a_recommendation(self):
        # "not recorded" is a filer who read the options and picked none. It is
        # an answer, and reading it as a hole is the absence contract inverted.
        self.assertEqual(
            estate_work.decision_state(self.sketch(recommendation="not recorded")),
            (estate_work.SKETCHED, []))

    def test_an_action_owes_no_sketch_and_an_unclassified_row_is_unstated(self):
        self.assertEqual(estate_work.decision_state({"classification": "action"}),
                         (estate_work.STATED, []))
        self.assertEqual(estate_work.decision_state({}),
                         (estate_work.UNSTATED, []))
        self.assertEqual(estate_work.decision_state({"context": "a sentence"}),
                         (estate_work.UNSTATED, []))


class AttentionTierTest(_Store):
    """t-721, P4 resolved uncapped — typed tiers, age only within a tier."""

    def waiting(self):
        # One of each tier: a proposal parked on the owner, a plain escalation, and
        # an ordinary open follow-up.
        self.estate("proposal", "add", "a decision for the owner",
                    "--condition", "something", "--desired-outcome", "a fix",
                    "--completion-check", "it is fixed")
        self.estate("task", "add", "an escalation", "--kind", "generic",
                    "--ready")
        self.estate("task", "add", "a standing obligation", "--kind",
                    "followup", "--refs", json.dumps({"context": "why"}))
        # Only an approved proposal is claimable, and a proposal reaches
        # `needs-owner` the same way anything else does — through a claim.
        self.estate("proposal", "stage", "t-1", "approved-backlog", "go ahead",
                    "--actor", "owner")
        for key in ("t-1", "t-2"):
            self.estate("claim", key, "--actor", "hub")
            self.estate("needs-owner", key, "over to the owner", "--actor", "hub")

    def test_the_three_tiers_come_from_status_kind_and_stage(self):
        self.waiting()
        view = self.build()
        tasks = self.tasks(view)
        self.assertEqual(tasks["t-1"]["tier"], "decision")
        self.assertEqual(tasks["t-2"]["tier"], "escalation")
        self.assertEqual(tasks["t-3"]["tier"], "followup")
        self.assertEqual([t["id"] for t in view["attention_tiers"]],
                         ["decision", "escalation", "followup"])
        # Every row in the set is in exactly one tier, and none is dropped.
        self.assertEqual(
            sum(t["total"] for t in view["attention_tiers"]),
            view["attention_counts"]["total"])

    def test_no_tier_is_capped_and_both_counts_are_carried(self):
        for n in range(12):
            self.estate("task", "add", f"follow-up {n}", "--kind", "followup")
        view = self.build()
        tier = view["attention_tiers"][0]
        self.assertEqual(tier["id"], "followup")
        self.assertEqual(tier["total"], 12)
        self.assertEqual(len(tier["ids"]), 12,
                         "a cap on the ids is the thing P4 refused")
        self.assertEqual(tier["notice"], 0)

    def test_the_notice_count_is_the_contracts_band_not_a_new_one(self):
        self.estate("task", "add", "overdue", "--kind", "followup",
                    "--due", iso(-1))
        self.estate("task", "add", "later", "--kind", "followup",
                    "--due", iso(30))
        tier = self.build()["attention_tiers"][0]
        self.assertEqual((tier["total"], tier["notice"]), (2, 1))

    def test_idle_is_measured_against_the_rows_own_tier(self):
        # Two tiers with very different clocks. The follow-up tier's old row is
        # above ITS median; the same number of days in the other tier is not a
        # comparison this makes at all.
        self.waiting()
        for n in range(2):
            self.estate("task", "add", f"fresh follow-up {n}",
                        "--kind", "followup")
        conn = self.connect()
        view = estate_work.build(conn, now=NOW + dt.timedelta(days=10), tz=UTC)
        tasks = self.tasks(view)
        tiers = {t["id"]: t for t in view["attention_tiers"]}
        self.assertEqual(tiers["decision"]["total"], 1)
        self.assertIsNotNone(tasks["t-3"]["idle_days"])
        # A tier of one has a median equal to its only row, so nothing in it is
        # above its own median — an honest "no comparison to make".
        self.assertIs(tasks["t-1"]["idle_above_tier_median"], False)
        self.assertEqual(tiers["decision"]["idle_median"],
                         tasks["t-1"]["idle_days"])

    def test_only_attention_rows_are_tiered(self):
        self.estate("task", "add", "ordinary work", "--kind", "generic",
                    "--ready")
        tasks = self.tasks()
        self.assertNotIn("tier", tasks["t-1"])
        self.assertEqual(self.build()["attention_tiers"], [])


class DueStateTest(_Store):
    def test_the_five_states_plus_unreadable_are_derived_not_stored(self):
        self.estate("task", "add", "overdue", "--kind", "generic", "--due", iso(-2))
        self.estate("task", "add", "today", "--kind", "generic", "--due", iso(0.3))
        self.estate("task", "add", "soon", "--kind", "generic", "--due", iso(2))
        self.estate("task", "add", "later", "--kind", "generic", "--due", iso(30))
        self.estate("task", "add", "undated", "--kind", "generic")
        tasks = self.tasks()
        self.assertEqual(tasks["t-1"]["due_state"], "overdue")
        self.assertEqual(tasks["t-2"]["due_state"], "due_today")
        self.assertEqual(tasks["t-3"]["due_state"], "due_soon")
        self.assertEqual(tasks["t-4"]["due_state"], "later")
        self.assertEqual(tasks["t-5"]["due_state"], "undated")

    def test_the_boundary_moves_with_the_clock_and_no_row_changes(self):
        """S31 — a deadline arriving changes nothing that is written down."""
        self.estate("task", "add", "soon", "--kind", "generic", "--due", iso(2))
        self.assertEqual(self.tasks()["t-1"]["due_state"], "due_soon")
        later = self.build(now=NOW - dt.timedelta(days=10))
        self.assertEqual(self.tasks(later)["t-1"]["due_state"], "later")
        past = self.build(now=NOW + dt.timedelta(days=10))
        self.assertEqual(self.tasks(past)["t-1"]["due_state"], "overdue")

    def test_the_notice_interval_is_a_policy_argument(self):
        self.estate("task", "add", "a week out", "--kind", "generic",
                    "--due", iso(7))
        self.assertEqual(self.tasks()["t-1"]["due_state"], "later")
        wide = self.build(notice=14)
        self.assertEqual(self.tasks(wide)["t-1"]["due_state"], "due_soon")

    def test_a_terminal_task_has_no_due_state(self):
        """Its deadline is history. Calling a finished task overdue is how a
        section teaches its reader to skip it."""
        self.estate("task", "add", "was due", "--kind", "generic",
                    "--due", iso(-5), "--ready")
        self.assertEqual(self.tasks()["t-1"]["due_state"], "overdue")
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("done", "t-1", "--actor", "hub", "--summary", "done",
                    "--verification", "shipped")
        task = self.tasks()["t-1"]
        self.assertIsNone(task["due_state"])
        self.assertIsNotNone(task["days_left"],
                             "the number is still a fact about the date")

    def test_an_unreadable_deadline_is_its_own_answer(self):
        self.estate("task", "add", "bad date", "--kind", "generic")
        conn = sqlite3.connect(self.dir / "estate.db")
        conn.execute("UPDATE tasks SET due_at='next tuesday' WHERE id='t-1'")
        conn.commit(); conn.close()
        task = self.tasks()["t-1"]
        self.assertEqual(task["due_state"], "unreadable")
        self.assertIsNone(task["days_left"])


class AttentionSetTest(_Store):
    def populate(self):
        self.estate("task", "add", "chase the vendor", "--kind", "followup",
                    "--due", iso(-1))
        self.estate("task", "add", "no date", "--kind", "followup")
        self.estate("task", "add", "ordinary work", "--kind", "generic", "--ready")
        self.estate("task", "add", "stuck on a person", "--kind", "generic", "--ready")
        self.estate("claim", "t-4", "--actor", "hub")
        self.estate("needs-owner", "t-4", "waiting on the owner", "--actor", "hub")

    def test_the_set_is_open_followups_plus_needs_owner_and_nothing_else(self):
        self.populate()
        view = self.build()
        self.assertEqual(set(view["attention"]), {"t-1", "t-2", "t-4"})
        self.assertFalse(self.tasks(view)["t-3"]["is_attention"])

    def test_it_is_ordered_most_urgent_first(self):
        self.populate()
        self.assertEqual(self.build()["attention"][0], "t-1",
                         "the overdue follow-up leads")

    def test_it_is_ids_not_a_second_copy_of_the_rows(self):
        """§4.10 — a duplicated row is a row that can disagree with itself."""
        self.populate()
        view = self.build()
        self.assertTrue(all(isinstance(v, str) for v in view["attention"]))
        self.assertTrue(all(isinstance(v, str) for v in view["followups"]))

    def test_counts_cover_every_state_including_the_empty_ones(self):
        self.populate()
        counts = self.build()["attention_counts"]
        for state in estate_attention.ALL_STATES:
            self.assertIn(state, counts,
                          "a zero is an answer; a missing key is not")
        self.assertEqual(counts["overdue"], 1)
        self.assertEqual(counts["undated"], 2)
        self.assertEqual(counts["total"], 3)

    def test_a_blocked_task_is_not_attention(self):
        """Blocked work waits on WORK. A dependency finishing releases it, not
        a person reading it (docs/attention-contract.md)."""
        self.estate("task", "add", "blocker", "--kind", "generic", "--ready")
        self.estate("task", "add", "held up", "--kind", "generic", "--ready")
        self.estate("dep", "add", "t-1", "t-2", "--kind", "blocks")
        view = self.build()
        self.assertEqual(view["attention"], [])
        self.assertEqual(self.tasks(view)["t-2"]["blocked_by"], ["t-1"])

    def test_a_resolved_followup_leaves_attention_but_stays_on_file(self):
        self.populate()
        self.estate("mark-ready", "t-2", "--actor", "hub")
        self.estate("claim", "t-2", "--actor", "hub")
        self.estate("done", "t-2", "--actor", "hub", "--summary", "answered",
                    "--verification", "they replied")
        view = self.build()
        self.assertNotIn("t-2", view["attention"])
        self.assertIn("t-2", view["followups"])
        self.assertEqual(view["followup_counts"],
                         {"open": 1, "resolved": 1, "total": 2})
        self.assertEqual(self.tasks(view)["t-2"]["followup"]["state"], "resolved")


FOLLOWUPS = str(_HERE.parent / "bin" / "followups")


def followups_is_estate_backed() -> bool:
    """True when bin/followups is the shim that reads THIS store.

    The envelope below is written by one script and decoded by another, and
    only the decoder is in this extraction. A repo whose bin/followups is still
    the standalone JSON store writes nothing this reader can see, so the test
    would report a missing re-sync as a decoding bug. Checking the file rather
    than a version string means the test resumes by itself once the shim lands.
    """
    try:
        with open(FOLLOWUPS) as f:
            return "ESTATE_STATE_DIR" in f.read()
    except OSError:
        return False


class FollowupEnvelopeTest(_Store):
    @unittest.skipUnless(followups_is_estate_backed(),
                         "bin/followups here is the standalone JSON store, not "
                         "the shim over this store")
    def test_the_refs_envelope_is_decoded_for_every_followup(self):
        subprocess.run([sys.executable, FOLLOWUPS, "add", "ask about the SLA",
                        "--source", "slack", "--ref", "chan-123/456",
                        "--context", "raised in the standup",
                        # required at this writer since t-475
                        "--classification", "action"],
                       capture_output=True, text=True, env=self.env, check=True)
        view = self.tasks()["t-1"]["followup"]
        self.assertEqual(view["legacy_id"], "followup:1")
        self.assertEqual(view["source"], "slack")
        self.assertEqual(view["ref"], "chan-123/456")
        self.assertEqual(view["context"], "raised in the standup")
        self.assertEqual(view["state"], "open")

    def test_only_followups_carry_the_envelope(self):
        self.estate("task", "add", "ordinary", "--kind", "generic")
        self.assertIsNone(self.tasks()["t-1"]["followup"])

    def test_a_claimed_followup_is_open_not_resolved(self):
        """Deliberately NOT bin/followups' binary, which calls anything but
        literal `open` resolved because the legacy file had no other word."""
        self.estate("task", "add", "in hand", "--kind", "followup")
        self.estate("mark-ready", "t-1", "--actor", "hub")
        self.estate("claim", "t-1", "--actor", "hub")
        self.assertEqual(self.tasks()["t-1"]["followup"]["state"], "open")

    def test_an_unparseable_refs_blob_is_not_an_error(self):
        self.estate("task", "add", "broken refs", "--kind", "followup")
        conn = sqlite3.connect(self.dir / "estate.db")
        conn.execute("UPDATE tasks SET refs='{not json' WHERE id='t-1'")
        conn.commit(); conn.close()
        self.assertEqual(estate_work.decode_refs("{not json"), {})
        self.assertEqual(self.tasks()["t-1"]["followup"]["legacy_id"], None)

    def test_the_raw_refs_blob_is_not_shipped_twice(self):
        """§4.6 normalizes the FOLLOW-UP envelope, which has a contract. A
        general decoded copy beside the raw column is the same bytes twice."""
        self.estate("task", "add", "ordinary", "--kind", "generic")
        self.assertNotIn("refs_decoded", self.tasks()["t-1"])


class InboxMessageTest(_Store):
    """t-296: the open-inbox-messages cut. A projection of the same task rows,
    not a second store — and the panel's whole job is to answer "did that ever
    get handled?" without anybody finding a Slack thread."""

    def message(self, title, ts, thread=None):
        refs = ('{"message_id": "msg-%s", "channel": "C0X", '
                '"message_ts": "%s", "thread_ts": %s, "inbox": "self-dm"}'
                % (ts.replace(".", ""), ts,
                   f'"{thread}"' if thread else "null"))
        self.estate("task", "add", title, "--kind", "inbox-message",
                    "--refs", refs, "--ready", "--actor", "hub")

    def test_open_messages_are_listed_oldest_first(self):
        """Oldest first because the one open longest is the one most likely to
        have been missed, which is the entire failure this tracks."""
        self.message("the first thing they asked", "1754000001.0001")
        self.message("the second thing", "1754000002.0001")
        view = self.build()
        self.assertEqual(view["inbox_messages"], ["t-1", "t-2"])
        self.assertEqual(view["inbox_message_counts"],
                         {"ready": 2, "total": 2})

    def test_a_closed_message_leaves_the_list(self):
        self.message("answered already", "1754000003.0001")
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("done", "t-1", "--actor", "hub", "--summary", "answered")
        view = self.build()
        self.assertEqual(view["inbox_messages"], [])
        self.assertEqual(view["inbox_message_counts"]["total"], 0)
        self.assertFalse(self.tasks(view)["t-1"]["message"]["open"])

    def test_an_escalated_message_is_open_here_and_in_attention(self):
        """One attention set (docs/attention-contract.md). This panel is a cut
        across the conversations, never a second attention store."""
        self.message("should I merge it", "1754000004.0001")
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("needs-owner", "t-1", "their call", "--actor", "hub")
        view = self.build()
        self.assertEqual(view["inbox_messages"], ["t-1"])
        self.assertEqual(view["attention"], ["t-1"])

    def test_the_slack_identity_is_decoded_for_the_renderer(self):
        self.message("in a thread", "1754000005.0001", thread="1753999999.0001")
        view = self.tasks()["t-1"]["message"]
        self.assertEqual(view["channel"], "C0X")
        self.assertEqual(view["message_ts"], "1754000005.0001")
        self.assertEqual(view["thread_ts"], "1753999999.0001")
        self.assertEqual(view["inbox"], "self-dm")
        self.assertTrue(view["threaded"])

    def test_only_inbox_messages_carry_the_envelope(self):
        self.estate("task", "add", "ordinary", "--kind", "generic")
        self.assertIsNone(self.tasks()["t-1"]["message"])
        self.assertEqual(self.build()["inbox_messages"], [])


class ProjectRollupTest(_Store):
    def populate(self):
        self.estate("project", "add", "Gap audit", "--kind", "rollout")
        self.estate("task", "add", "blocker", "--kind", "generic", "--ready",
                    "--project", "p-1")
        self.estate("task", "add", "held up", "--kind", "generic", "--ready",
                    "--project", "p-1")
        self.estate("dep", "add", "t-1", "t-2", "--kind", "blocks")
        self.estate("task", "add", "ask the owner", "--kind", "followup",
                    "--project", "p-1", "--due", iso(-3))
        self.estate("task", "add", "shipped", "--kind", "generic", "--ready",
                    "--project", "p-1")
        self.estate("claim", "t-4", "--actor", "hub")
        self.estate("done", "t-4", "--actor", "hub", "--summary", "done",
                    "--verification", "green")
        self.estate("task", "add", "loose work", "--kind", "generic", "--ready")

    def project(self, pid):
        return next(p for p in self.build()["projects"] if p["id"] == pid)

    def test_the_rollup_counts_what_the_task_rows_say(self):
        self.populate()
        rollup = self.project("p-1")["rollup"]
        self.assertEqual(rollup["tasks"], 4)
        self.assertEqual(rollup["open"], 3)
        self.assertEqual(rollup["terminal"], 1)
        self.assertEqual(rollup["blocked"], 1)
        self.assertEqual(rollup["attention"], 1)
        self.assertEqual(rollup["overdue"], 1)
        self.assertEqual(rollup["by_status"], {"ready": 2, "open": 1, "done": 1})
        self.assertEqual(rollup["by_kind"], {"generic": 3, "followup": 1})

    def test_it_is_recomputed_and_never_written_down(self):
        self.populate()
        self.assertEqual(self.project("p-1")["rollup"]["blocked"], 1)
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("done", "t-1", "--actor", "hub", "--summary", "done",
                    "--verification", "green")
        self.assertEqual(self.project("p-1")["rollup"]["blocked"], 0)

    def test_unprojected_work_is_a_grouping_with_the_same_shape(self):
        self.populate()
        bucket = self.project(estate_work.UNPROJECTED)
        self.assertTrue(bucket["unprojected"])
        self.assertEqual(bucket["rollup"]["tasks"], 1)
        self.assertEqual(bucket["rollup"]["task_ids"], ["t-5"])

    def test_the_grouping_is_not_a_row_in_sqlite(self):
        self.populate()
        rows = self.connect().execute("SELECT id FROM projects").fetchall()
        self.assertEqual([r["id"] for r in rows], ["p-1"])

    def test_a_store_with_no_projects_still_reports_the_grouping(self):
        self.estate("task", "add", "loose", "--kind", "generic")
        projects = self.build()["projects"]
        self.assertEqual([p["id"] for p in projects],
                         [estate_work.UNPROJECTED])

    def test_project_counts_exclude_the_synthetic_bucket(self):
        self.populate()
        self.assertEqual(self.build()["project_counts"], {"active": 1})

    def test_a_task_carries_its_projects_title(self):
        self.populate()
        self.assertEqual(self.tasks()["t-1"]["project_title"], "Gap audit")
        self.assertIsNone(self.tasks()["t-5"]["project_title"])

    def test_a_dangling_project_id_is_reported_not_dropped(self):
        self.estate("task", "add", "orphaned", "--kind", "generic")
        conn = sqlite3.connect(self.dir / "estate.db")
        conn.execute("UPDATE tasks SET project_id='p-99' WHERE id='t-1'")
        conn.commit(); conn.close()
        ghost = self.project("p-99")
        self.assertTrue(ghost["missing"])
        self.assertEqual(ghost["rollup"]["task_ids"], ["t-1"])

    def test_closed_projects_sort_after_active_ones(self):
        self.estate("project", "add", "finished", "--kind", "rollout")
        self.estate("project", "add", "running", "--kind", "rollout")
        self.estate("project", "close", "p-1", "done")
        ids = [p["id"] for p in self.build()["projects"]]
        self.assertEqual(ids[:2], ["p-2", "p-1"])


class DegradedStoreTest(unittest.TestCase):
    """A derived view must never take down the panel that carries it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "estate.db"

    def view(self, script):
        conn = sqlite3.connect(self.db)
        conn.executescript(script)
        conn.commit(); conn.close()
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return estate_work.build(conn, now=NOW, tz=UTC)

    def test_a_store_with_no_dependency_table_has_no_edges(self):
        view = self.view("""
            CREATE TABLE tasks (seq INTEGER PRIMARY KEY, id TEXT, status TEXT,
                                kind TEXT, title TEXT, updated_at TEXT);
            INSERT INTO tasks VALUES (1,'t-1','ready','generic','a','x');""")
        self.assertEqual(view["dependencies"], [])
        self.assertEqual(view["tasks"][0]["blocked_by"], [])
        self.assertEqual(view["tasks"][0]["blocks"], [])

    def test_a_store_with_no_projects_table_still_groups_the_work(self):
        view = self.view("""
            CREATE TABLE tasks (seq INTEGER PRIMARY KEY, id TEXT, status TEXT,
                                kind TEXT, title TEXT, updated_at TEXT);
            INSERT INTO tasks VALUES (1,'t-1','ready','generic','a','x');""")
        self.assertEqual([p["id"] for p in view["projects"]],
                         [estate_work.UNPROJECTED])
        self.assertEqual(view["project_counts"], {})

    def test_a_store_predating_due_at_has_undated_work_not_an_error(self):
        view = self.view("""
            CREATE TABLE tasks (seq INTEGER PRIMARY KEY, id TEXT, status TEXT,
                                kind TEXT, title TEXT, updated_at TEXT);
            INSERT INTO tasks VALUES (1,'t-1','needs-owner','generic','a','x');""")
        self.assertEqual(view["tasks"][0]["due_state"], "undated")
        self.assertEqual(view["attention"], ["t-1"])


class ReadinessTest(unittest.TestCase):
    """t-1742 — the pre-presentation readiness filter.

    The 2026-08-16 proposal-body audit recommended one and nothing derived it.
    The rule is SHAPE, not quality: a real recommendation, a situation sentence
    on file, and a row neither the task nor its review stage has finished.
    """

    def row(self, refs=None, **over):
        task = {"status": "ready", "stage": "pending-review", "intent": None,
                "refs": json.dumps(refs) if refs is not None else None}
        task.update(over)
        return task

    def decidable(self, **over):
        refs = {"classification": "decision", "question": "which one?",
                "recommendation": "take the graph arm",
                "context": "both branches are unmerged"}
        refs.update(over)
        return self.row(refs)

    def test_a_complete_record_on_a_live_row_reaches(self):
        self.assertEqual(estate_work.readiness(self.decidable()),
                         (estate_work.REACH, []))

    def test_an_absent_recommendation_key_is_unclassified_and_not_a_verdict(self):
        # docs/absence-contract.md, read back: a key nobody wrote says nothing
        # about the work, so nothing is concluded about the work. Most open
        # proposal/followup rows are in exactly this condition.
        state, why = estate_work.readiness(self.row({"context": "a sentence"}))
        self.assertEqual((state, why),
                         (estate_work.UNCLASSIFIED,
                          [estate_work.NO_RECOMMENDATION]))
        # No refs at all is the same absence, not an error.
        self.assertEqual(estate_work.readiness(self.row())[0],
                         estate_work.UNCLASSIFIED)

    def test_a_blank_recommendation_is_the_same_absence_wearing_a_key(self):
        state, why = estate_work.readiness(self.decidable(recommendation="  "))
        self.assertEqual((state, why),
                         (estate_work.UNCLASSIFIED,
                          [estate_work.EMPTY_RECOMMENDATION]))

    def test_the_recorded_no_pick_sentinel_is_thinking_not_unclassified(self):
        # The sentinel is a filer telling the reader something true, so it is
        # RECORDED — and it is not a pick, so it is not decidable as stored.
        # The audit judged t-27 and t-394 exactly this way.
        state, why = estate_work.readiness(
            self.decidable(recommendation="not recorded"))
        self.assertEqual((state, why),
                         (estate_work.THINKING,
                          [estate_work.SENTINEL_RECOMMENDATION]))

    def test_the_sentinel_with_its_reason_attached_is_still_the_sentinel(self):
        # Filers write the sentinel and then say why. An equality test would
        # report every one of those rows ready to decide, under a record whose
        # first two words say nobody picked.
        self.assertEqual(
            estate_work.readiness(self.decidable(
                recommendation="not recorded -- this is the owner's call"))[0],
            estate_work.THINKING)
        # …and the boundary that keeps that from over-reaching.
        self.assertTrue(estate_work.is_not_recorded("Not Recorded"))
        self.assertFalse(estate_work.is_not_recorded("not recordedness held"))

    def test_a_record_that_says_nothing_about_its_situation_is_thinking(self):
        state, why = estate_work.readiness(
            self.row({"recommendation": "do the thing"}))
        self.assertEqual((state, why),
                         (estate_work.THINKING, [estate_work.NO_SITUATION]))

    def test_the_situation_may_come_from_any_of_the_three_record_shapes(self):
        # A follow-up writes `context`, a proposal writes `condition`, and
        # `intent` is the task column a row carries instead of either. Reading
        # only the display precedence (CONTEXT_FIELDS) would have called t-395
        # unready, and the audit judged t-395 reach.
        for field in ("context", "condition"):
            self.assertEqual(
                estate_work.readiness(
                    self.row({"recommendation": "pick a", field: "why"}))[0],
                estate_work.REACH, field)
        self.assertEqual(
            estate_work.readiness(
                self.row({"recommendation": "pick a"}, intent="why"))[0],
            estate_work.REACH)

    def test_a_finished_row_is_a_record_and_not_a_request(self):
        # t-460: a follow-up whose displayed question still asked how a
        # change should publish, after the owner had authorised it and it
        # merged. Both halves of its lifecycle say so, and both are named.
        state, why = estate_work.readiness(
            dict(self.decidable(), status="done", stage="resolved"))
        self.assertEqual(state, estate_work.THINKING)
        self.assertEqual(why, [estate_work.TASK_TERMINAL,
                               estate_work.STAGE_TERMINAL])
        # A live task under an ENDED review is the same staleness with only
        # one half showing, and it exists: t-1092 is `ready` at `rejected`.
        self.assertEqual(
            estate_work.readiness(dict(self.decidable(), stage="rejected")),
            (estate_work.THINKING, [estate_work.STAGE_TERMINAL]))

    def test_a_live_stage_is_not_a_reason(self):
        for stage in ("pending-review", "approved-backlog", None):
            self.assertEqual(
                estate_work.readiness(dict(self.decidable(), stage=stage)),
                (estate_work.REACH, []), stage)

    def test_every_failed_clause_is_named_not_just_the_first(self):
        state, why = estate_work.readiness(
            self.row({"recommendation": "not recorded"}, status="dropped"))
        self.assertEqual(state, estate_work.THINKING)
        self.assertEqual(why, [estate_work.SENTINEL_RECOMMENDATION,
                               estate_work.NO_SITUATION,
                               estate_work.TASK_TERMINAL])

    def test_desired_outcome_is_not_a_recommendation_here(self):
        """The display precedence falls back to it; readiness must not.

        `RECOMMENDATION_FIELDS` reads `desired_outcome` because the review grid
        has labelled that field "Recommendation" since proposals existed. It is
        what the proposal wants to be true afterwards, which nearly every
        proposal carries — reading it as a pick would report the whole review
        queue ready on the strength of a field nobody filed as one.
        """
        task = self.row({"desired_outcome": "the panel is correct",
                         "context": "a sentence"})
        # The surface still shows it, under its own name…
        surface = estate_work.decision_surface(task)
        self.assertEqual(surface["recommendation"], "the panel is correct")
        self.assertEqual(surface["recommendation_field"], "desired_outcome")
        # …and readiness still says nobody recommended anything.
        self.assertEqual(surface["readiness"], estate_work.UNCLASSIFIED)


class ReadinessSnapshotTest(_Store):
    """t-1742 — the verdict reaches BOTH lists /estate.html renders, and only
    those. Built through the CLI, because the read model has to agree with the
    writer and a hand-written INSERT lets the two drift."""

    def rows(self):
        # An attention row: parked at `needs-owner`, carrying a real pick.
        self.estate("task", "add", "parked on a real pick", "--kind", "generic",
                    "--ready",
                    "--refs", json.dumps({"recommendation": "take arm A",
                                          "context": "both arms are unmerged"}))
        self.estate("claim", "t-1", "--actor", "hub")
        self.estate("needs-owner", "t-1", "over to you", "--actor", "hub")
        # A review-queue row: staged, carrying the recorded non-pick.
        self.estate("task", "add", "staged with no pick", "--kind", "proposal",
                    "--refs", json.dumps({"recommendation": "not recorded",
                                          "context": "two arms, neither costed"}))
        self.estate("proposal", "add", "staged with no pick",
                    "--adopt-task", "t-2")
        # On neither list.
        self.estate("task", "add", "ordinary work", "--kind", "generic",
                    "--ready")
        return self.tasks()

    def test_the_attention_set_and_the_review_queue_both_carry_it(self):
        rows = self.rows()
        self.assertEqual(rows["t-1"]["readiness"], estate_work.REACH)
        self.assertEqual(rows["t-1"]["readiness_why"], [])
        self.assertEqual(rows["t-2"]["readiness"], estate_work.THINKING)
        self.assertEqual(rows["t-2"]["readiness_why"],
                         [estate_work.SENTINEL_RECOMMENDATION])

    def test_a_row_on_neither_list_carries_no_verdict_at_all(self):
        """Absent, never a default. A row no surface presents to the owner was not
        judged, and stamping it `unclassified` would report an absence
        somebody looked for as one somebody found."""
        row = self.rows()["t-3"]
        self.assertNotIn("readiness", row)
        self.assertNotIn("readiness_why", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)

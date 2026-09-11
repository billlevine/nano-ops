#!/usr/bin/env python3
"""Tests for t-1794: the chosen alternative, and the pickup that never happened.

Run: python3 tests/test_decision_pickup.py

Four layers, for the four places this can be wrong.

  1. `lib/estate_pickup` is pure, so the vocabulary, the choice validation and
     the verdict precedence are exercised by passing values rather than by
     writing history.
  2. `bin/estate proposal stage` and the two new verbs are exercised against a
     real throwaway store through the real script, because the whole claim is
     about what lands in an event's refs and a mocked store would prove the
     wrong thing about a refs envelope.
  3. `POST /proposal-stage` is exercised against a real threaded server, because
     the completion check names that endpoint by name.
  4. `lib/dashboard_estate` and `lib/dashboard_primary` are exercised as source,
     the way every other renderer test here is: the click is where the choice
     is made, and a card nothing listens to is a refusal nobody can answer.

The properties under test are the ones whose failure would be silent:

  * AMENDMENT SAFETY. The whole reason the record is on the EVENT is that
     `proposal amend` replaces the alternatives set outright. A stored position
     alone would silently come to mean a different option, so the event has to
     carry the alternatives it was chosen among, and a test has to actually
     amend and re-read.
  * A LEGACY ROW STILL APPROVES. Of the 20 proposals sitting at
     `approved-backlog` on 2026-09-06, 9 declare no classification at all and 5
     more are `action`s. Requiring a choice from those 14 would have wedged the
     one lifecycle the owner drives every day.
  * A NOTE IS NOT A PICKUP. t-777 and t-1329 carry nineteen mechanic notes
     between them since their approvals and are the two most stuck rows in the
     store. A sweep that counted notes would report them handled.
  * `unobservable` NEVER SOFTENS INTO `owed`. An accusation with no clock
     behind it is what docs/absence-contract.md exists to prevent.
"""
import contextlib
import datetime as dt
import http.client
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from importlib.machinery import SourceFileLoader

# This core keeps tests under `tests/` and scripts under `bin/`, so the two are
# siblings rather than the same directory (docs/extraction-allowlist.md).
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_HERE)
BIN_DIR = os.path.join(REPO_ROOT, "bin")
ESTATE = os.path.join(BIN_DIR, "estate")
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, REPO_ROOT)

import estate_pickup as P  # noqa: E402
import estate_decisions as D  # noqa: E402
from lib import dashboard_estate, dashboard_primary  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 6, 18, 0, tzinfo=UTC)


def ago(hours: float):
    return NOW - dt.timedelta(hours=hours)


# --------------------------------------------------------------------------- #
# 1. the pure rule
# --------------------------------------------------------------------------- #

ALTS = [{"option": "Alpha", "consequence": "costs a day"},
        {"option": "Beta", "consequence": "costs a week"}]
DECISION_REFS = {D.CLASSIFICATION_KEY: D.DECISION, D.ALTERNATIVES_KEY: ALTS}


class ClassificationCase(unittest.TestCase):
    def test_only_a_decision_owes_a_choice(self):
        self.assertTrue(P.is_decision(DECISION_REFS))
        for other in (D.ACTION, D.CLARIFICATION):
            self.assertFalse(P.is_decision({D.CLASSIFICATION_KEY: other}))

    def test_a_legacy_row_declares_nothing_and_owes_nothing(self):
        """9 of the 20 live approved proposals are exactly this. Nothing is
        inferred here for the same reason the classification contract
        retrofits nothing."""
        for junk in ({}, None, {"condition": "x"}, {D.CLASSIFICATION_KEY: None}):
            self.assertFalse(P.is_decision(junk))

    def test_the_word_is_matched_loosely_but_never_guessed(self):
        self.assertTrue(P.is_decision({D.CLASSIFICATION_KEY: " Decision "}))
        self.assertFalse(P.is_decision({D.CLASSIFICATION_KEY: "decisions"}))


class ChoiceCase(unittest.TestCase):
    def test_the_position_is_one_based_and_carries_the_option_text(self):
        record = P.chosen_record(DECISION_REFS, 2)
        self.assertEqual(record[P.POSITION_KEY], 2)
        self.assertEqual(record[P.OPTION_KEY], "Beta")
        self.assertEqual(record[P.CONSEQUENCE_KEY], "costs a week")

    def test_the_record_freezes_the_alternatives_it_chose_among(self):
        """THE AMENDMENT SAFETY, at the primitive. The returned list is a copy,
        so a caller mutating the source afterwards cannot reach it."""
        source = {D.CLASSIFICATION_KEY: D.DECISION,
                  D.ALTERNATIVES_KEY: [dict(a) for a in ALTS]}
        record = P.chosen_record(source, 1)
        source[D.ALTERNATIVES_KEY][0]["option"] = "something else entirely"
        self.assertEqual(record[P.ALTERNATIVES_KEY][0]["option"], "Alpha")

    def test_a_position_off_either_end_is_refused(self):
        for bad in (0, 3, -1, 99):
            with self.assertRaises(P.ChoiceRefused) as caught:
                P.chosen_record(DECISION_REFS, bad)
            self.assertEqual(caught.exception.key, P.CHOSEN_KEY)

    def test_a_bool_is_not_a_position(self):
        """`True` is an int in Python and would index alternative 1 off a
        checkbox. The wire carries a number or nothing."""
        with self.assertRaises(P.ChoiceRefused):
            P.chosen_record(DECISION_REFS, True)

    def test_a_string_position_is_refused_rather_than_coerced(self):
        with self.assertRaises(P.ChoiceRefused):
            P.chosen_record(DECISION_REFS, "1")

    def test_a_decision_with_unreadable_alternatives_refuses_and_names_repair(self):
        """NOT a bypass. A choice among alternatives nobody can read is not
        expressible, and saying so is a different refusal from 'you forgot'."""
        with self.assertRaises(P.ChoiceRefused) as caught:
            P.chosen_record({D.CLASSIFICATION_KEY: D.DECISION,
                             D.ALTERNATIVES_KEY: [ALTS[0]]}, 1)
        self.assertEqual(caught.exception.key, D.ALTERNATIVES_KEY)
        self.assertIn("proposal amend", str(caught.exception))

    def test_alternatives_are_never_filtered_because_order_is_identity(self):
        """A malformed entry dropped here would shift every position after it
        and make the recorded choice name a different branch."""
        holed = {D.CLASSIFICATION_KEY: D.DECISION,
                 D.ALTERNATIVES_KEY: [{"consequence": "no option key"},
                                      ALTS[0], ALTS[1]]}
        self.assertEqual(P.chosen_record(holed, 2)[P.OPTION_KEY], "Alpha")

    def test_the_refusal_message_lists_the_options_it_wants_a_number_for(self):
        text = P.missing_choice_message(DECISION_REFS)
        self.assertIn("1. Alpha", text)
        self.assertIn("2. Beta", text)
        self.assertIn("--chosen", text)


class PickupRecordCase(unittest.TestCase):
    def test_the_routes_are_the_ones_this_estate_has(self):
        self.assertEqual(P.ROUTES,
                         ("night-shift", "dispatch", "hub", "parked"))
        self.assertNotIn(P.PARKED, P.EXECUTOR_ROUTES)

    def test_an_unknown_route_is_refused(self):
        with self.assertRaises(P.ChoiceRefused) as caught:
            P.pickup_record("telepathy", "somehow")
        self.assertEqual(caught.exception.key, P.ROUTE_KEY)

    def test_a_marker_with_no_sentence_is_refused(self):
        """A marker nobody can read back only makes the row stop being
        reported. Same bargain as `estate verified`'s required evidence."""
        for blank in ("", "   ", None):
            with self.assertRaises(P.ChoiceRefused):
                P.pickup_record(P.HUB, blank)

    def test_an_absent_ref_stores_no_key_rather_than_a_null(self):
        self.assertNotIn(P.REF_KEY, P.pickup_record(P.HUB, "did it inline"))
        self.assertEqual(
            P.pickup_record(P.HUB, "did it", "abc123")[P.REF_KEY], "abc123")

    def test_a_pickup_with_an_unknown_route_reads_back_as_no_pickup(self):
        """Reading is where a hand-edited store arrives. An unrecognized route
        is not a route, and treating it as one would let any refs blob silence
        the sweep."""
        self.assertIsNone(P.pickup_of({P.PICKUP_KEY: {P.ROUTE_KEY: "invented"}}))
        self.assertIsNone(P.pickup_of({P.PICKUP_KEY: "night-shift"}))
        self.assertIsNone(P.pickup_of(None))
        self.assertEqual(
            P.pickup_of({P.PICKUP_KEY: {P.ROUTE_KEY: P.HUB}})[P.ROUTE_KEY], P.HUB)


class VerdictCase(unittest.TestCase):
    def verdict(self, **kw):
        base = dict(status="ready", decided_at=ago(48), pickups=[],
                    queued=False, claimed=False, now=NOW)
        base.update(kw)
        return P.verdict(**base)["verdict"]

    def test_decided_and_untouched_is_owed(self):
        self.assertEqual(self.verdict(), P.OWED)

    def test_a_decision_inside_one_tick_is_fresh_and_not_accused(self):
        self.assertEqual(self.verdict(decided_at=ago(0.1)), P.FRESH)

    def test_the_grace_boundary_is_exclusive(self):
        """Exactly one grace period old is not yet older than one."""
        self.assertEqual(
            self.verdict(decided_at=NOW - dt.timedelta(
                minutes=P.GRACE_MINUTES)), P.FRESH)
        self.assertEqual(
            self.verdict(decided_at=NOW - dt.timedelta(
                minutes=P.GRACE_MINUTES + 0.5)), P.OWED)

    def test_an_unreadable_decision_instant_never_softens_into_owed(self):
        state = P.verdict(status="ready", decided_at=None, pickups=[],
                          queued=False, claimed=False, now=NOW)
        self.assertEqual(state["verdict"], P.UNOBSERVABLE)
        self.assertIsNone(state["age_hours"])

    def test_an_explicit_marker_wins_over_everything_derived(self):
        state = P.verdict(status="needs-owner",
                          decided_at=ago(200),
                          pickups=[{P.ROUTE_KEY: P.DISPATCH}],
                          queued=False, claimed=False, now=NOW)
        self.assertEqual(state["verdict"], P.PICKED_UP)
        self.assertEqual(state["route"], P.DISPATCH)

    def test_the_latest_marker_wins_so_a_park_can_be_undone(self):
        self.assertEqual(
            self.verdict(pickups=[{P.ROUTE_KEY: P.PARKED},
                                  {P.ROUTE_KEY: P.NIGHT_SHIFT}]), P.PICKED_UP)
        self.assertEqual(
            self.verdict(pickups=[{P.ROUTE_KEY: P.NIGHT_SHIFT},
                                  {P.ROUTE_KEY: P.PARKED}]), P.PARKED_V)

    def test_the_stores_own_parking_states_are_parked_without_a_marker(self):
        """`needs-owner` is already in the attention set and `blocked` names its
        blocker. Reporting either here would file one item twice."""
        for status in P.PARKED_STATUSES:
            self.assertEqual(self.verdict(status=status), P.PARKED_V)

    def test_a_queue_key_after_the_decision_is_the_night_shift(self):
        state = P.verdict(status="ready", decided_at=ago(48), pickups=[],
                          queued=True, claimed=False, now=NOW)
        self.assertEqual((state["verdict"], state["route"]),
                         (P.PICKED_UP, P.NIGHT_SHIFT))

    def test_a_claim_after_the_decision_is_a_pickup(self):
        self.assertEqual(self.verdict(claimed=True), P.PICKED_UP)


class ExitStatusCase(unittest.TestCase):
    def test_owed_wins_over_unobservable(self):
        """bin/skill-drift's own precedence: a run with both has something to
        DO about the first."""
        self.assertEqual(P.exit_status([P.UNOBSERVABLE, P.OWED]), 3)

    def test_unobservable_alone_is_two(self):
        self.assertEqual(P.exit_status([P.PICKED_UP, P.UNOBSERVABLE]), 2)

    def test_a_clean_run_is_zero_and_so_is_an_empty_one(self):
        self.assertEqual(P.exit_status([P.PICKED_UP, P.PARKED_V, P.FRESH]), 0)
        self.assertEqual(P.exit_status([]), 0)


# --------------------------------------------------------------------------- #
# 2. the real store, through the real script
# --------------------------------------------------------------------------- #

DECISION_FLAGS = ("--classification", "decision", "--question", "which way",
                  "--alternative", "Alpha :: costs a day",
                  "--alternative", "Beta :: costs a week",
                  "--recommendation", "Alpha",
                  "--defer-consequence", "it rots")


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.db = os.path.join(self.state, "estate.db")
        self.addCleanup(self.tmp.cleanup)

    def run_estate(self, *args):
        env = dict(os.environ, ESTATE_STATE_DIR=self.state,
                   ESTATE_LESSONS_PATH=os.path.join(self.state, "lessons.md"))
        return subprocess.run([sys.executable, ESTATE, *args],
                              capture_output=True, text=True, env=env)

    def estate(self, *args):
        out = self.run_estate(*args)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def a_decision(self, title="a real choice"):
        self.estate("proposal", "add", title, "--condition", f"{title}: seen",
                    "--actor", "mechanic", *DECISION_FLAGS)
        return self.last_task()

    def an_action(self, title="already chosen"):
        self.estate("proposal", "add", title, "--condition", f"{title}: seen",
                    "--actor", "mechanic", "--classification", "action")
        return self.last_task()

    def a_legacy(self, title="filed before classifications existed"):
        self.estate("proposal", "add", title, "--condition", f"{title}: seen",
                    "--actor", "mechanic")
        return self.last_task()

    def last_task(self):
        with self.conn() as c:
            return c.execute(
                "SELECT id FROM tasks ORDER BY seq DESC LIMIT 1").fetchone()["id"]

    def events(self, key, kind=None):
        sql = "SELECT * FROM events WHERE task_id=?"
        args = [key]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY seq", args)]

    def approval_refs(self, key):
        rows = [r for r in self.events(key, "transition")
                if r["summary"].endswith("-> approved-backlog")]
        self.assertTrue(rows, "no approval transition on file")
        return json.loads(rows[-1]["refs"] or "{}")

    def backdate(self, key, hours):
        """Age a decision by APPENDING an approval event with an older stamp.

        The events table is append-only — `bin/estate` installs a trigger that
        aborts an UPDATE or a DELETE on it — so a fixture cannot move a row
        into the past, and a test that could not age a decision could only ever
        assert `fresh`. `estate event --at` is the door already built for
        writing down something that happened earlier, and `decision_instant`
        takes the LAST approval recorded, which this becomes.
        """
        when = (dt.datetime.now(UTC) - dt.timedelta(hours=hours)).isoformat()
        self.estate("event", "--actor", "owner", "--kind", "transition",
                    "--summary", "stage: pending-review -> approved-backlog",
                    "--task", key, "--at", when)

    def an_unrecorded_approval(self, title="approved before stages existed"):
        """A row sitting at `approved-backlog` with NO event saying how it got
        there. Three real rows are in this shape, from before the store
        recorded stages at all, and one is reachable by hand-editing the store
        — and it cannot be produced by DELETING events, because they are
        append-only."""
        self.estate("init")
        with self.conn() as c:
            c.execute(
                "INSERT INTO tasks (id, kind, title, status, stage, created_by,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("t-legacy", "proposal", title, "ready", "approved-backlog",
                 "mechanic", "2026-07-01T00:00:00+00:00",
                 "2026-07-01T00:00:00+00:00"))
            c.commit()
        return "t-legacy"

    def decided(self, *args):
        out = self.run_estate("decided-not-acted", "--json", *args)
        self.assertIn(out.returncode, (0, 2, 3), out.stderr)
        return json.loads(out.stdout or "[]"), out.returncode

    def verdict_of(self, key, *args):
        rows, code = self.decided(*args)
        by_id = {r["id"]: r for r in rows}
        return by_id.get(key), code


class ApprovalRecordsTheChoice(StoreBase):
    def test_approving_a_decision_without_a_choice_is_refused(self):
        key = self.a_decision()
        out = self.run_estate("proposal", "stage", key, "approved-backlog",
                              "the owner picked Alpha", "--actor", "owner")
        self.assertEqual(out.returncode, 2)
        self.assertIn("1. Alpha", out.stderr)
        with self.conn() as c:
            self.assertEqual(
                c.execute("SELECT stage FROM tasks WHERE id=?",
                          (key,)).fetchone()["stage"], "pending-review")

    def test_the_refusal_writes_no_event_at_all(self):
        """The gate is inside the same transaction as the stage write, so a
        refused approval leaves the history exactly as it was."""
        key = self.a_decision()
        before = len(self.events(key))
        self.run_estate("proposal", "stage", key, "approved-backlog", "no pick",
                        "--actor", "owner")
        self.assertEqual(len(self.events(key)), before)

    def test_the_choice_lands_on_the_transition_events_refs(self):
        key = self.a_decision()
        self.estate("proposal", "stage", key, "approved-backlog",
                    "the owner picked Beta", "--actor", "owner", "--chosen", "2")
        record = self.approval_refs(key)[P.CHOSEN_KEY]
        self.assertEqual(record[P.POSITION_KEY], 2)
        self.assertEqual(record[P.OPTION_KEY], "Beta")
        self.assertEqual(record[P.CONSEQUENCE_KEY], "costs a week")

    def test_the_choice_is_not_written_into_the_task_row(self):
        """`refs` on the task is state and the event is history. A position in
        the row is what `proposal amend` would silently repoint."""
        key = self.a_decision()
        self.estate("proposal", "stage", key, "approved-backlog", "picked",
                    "--actor", "owner", "--chosen", "1")
        with self.conn() as c:
            refs = json.loads(c.execute("SELECT refs FROM tasks WHERE id=?",
                                        (key,)).fetchone()["refs"])
        self.assertNotIn(P.CHOSEN_KEY, refs)

    def test_the_note_is_left_alone(self):
        """The identity rides beside the prose, never inside it: folding it in
        is what destroyed it."""
        key = self.a_decision()
        self.estate("proposal", "stage", key, "approved-backlog",
                    "Owner clicked: Alpha — costs a day", "--actor", "owner",
                    "--chosen", "1")
        rows = [r for r in self.events(key, "transition")
                if r["summary"].endswith("-> approved-backlog")]
        self.assertEqual(rows[-1]["detail"], "Owner clicked: Alpha — costs a day")

    def test_an_amendment_after_the_fact_cannot_move_the_recorded_choice(self):
        """THE WHOLE REASON THE RECORD IS ON THE EVENT. `proposal amend`
        replaces the alternatives set outright, so a bare position would come
        to name a different branch. The event still says Beta, and still says
        what Beta was chosen among."""
        key = self.a_decision()
        self.estate("proposal", "stage", key, "approved-backlog", "picked Beta",
                    "--actor", "owner", "--chosen", "2")
        self.estate("proposal", "stage", key, "pending-review", "reconsidering",
                    "--actor", "owner")
        self.estate("proposal", "amend", key, "--classification", "decision",
                    "--question", "which way now",
                    "--alternative", "Gamma :: a wholly different plan",
                    "--alternative", "Delta :: another one",
                    "--recommendation", "Gamma",
                    "--defer-consequence", "it still rots")
        record = self.approval_refs(key)[P.CHOSEN_KEY]
        self.assertEqual(record[P.OPTION_KEY], "Beta")
        self.assertEqual([a["option"] for a in record[P.ALTERNATIVES_KEY]],
                         ["Alpha", "Beta"])

    def test_an_action_approves_with_no_choice_and_refuses_one_offered(self):
        key = self.an_action()
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner")
        self.assertNotIn(P.CHOSEN_KEY, self.approval_refs(key))
        other = self.an_action("second action")
        out = self.run_estate("proposal", "stage", other, "approved-backlog",
                              "go", "--actor", "owner", "--chosen", "1")
        self.assertEqual(out.returncode, 2)
        self.assertIn("alternatives", out.stderr)

    def test_a_legacy_row_still_approves_exactly_as_it_always_did(self):
        """9 of the 20 live approved proposals declare nothing, and 5 more are
        `action`s. Requiring a choice from those 14 would have wedged the
        lifecycle the owner drives daily."""
        key = self.a_legacy()
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner")
        with self.conn() as c:
            self.assertEqual(
                c.execute("SELECT stage FROM tasks WHERE id=?",
                          (key,)).fetchone()["stage"], "approved-backlog")

    def test_a_choice_is_refused_on_every_move_that_is_not_an_approval(self):
        """Rejecting, resolving or stopping picks NONE of the alternatives.
        That is a different fact and the stage already records it."""
        for target in ("rejected", "resolved", "stopped"):
            key = self.a_decision(f"decision for {target}")
            out = self.run_estate("proposal", "stage", key, target, "no",
                                  "--actor", "owner", "--chosen", "1")
            self.assertEqual(out.returncode, 2, target)
            self.assertIn("belongs to an approval", out.stderr)

    def test_rejecting_a_decision_needs_no_choice(self):
        key = self.a_decision()
        self.estate("proposal", "stage", key, "rejected", "no", "--actor", "owner")

    def test_a_position_past_the_end_is_refused_with_the_count(self):
        key = self.a_decision()
        out = self.run_estate("proposal", "stage", key, "approved-backlog", "x",
                              "--actor", "owner", "--chosen", "3")
        self.assertEqual(out.returncode, 2)
        self.assertIn("1..2", out.stderr)

    def test_the_cli_says_which_branch_it_recorded(self):
        key = self.a_decision()
        out = self.estate("proposal", "stage", key, "approved-backlog", "go",
                          "--actor", "owner", "--chosen", "1")
        self.assertIn("chose option 1 of 2: Alpha", out.stdout)


class ProposalShowReadsItBack(StoreBase):
    """A captured record nobody can read is not captured — the argument that
    put the classification keys on this view in the first place."""

    def approved(self, position=2):
        key = self.a_decision()
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner", "--chosen", str(position))
        return key

    def test_it_names_the_branch_and_marks_it_among_the_others(self):
        out = self.estate("proposal", "show", self.approved()).stdout
        self.assertIn("chosen            option 2 of 2: Beta", out)
        self.assertIn("-> Beta", out)

    def test_it_reads_the_event_and_not_the_row_so_an_amend_cannot_move_it(self):
        key = self.approved()
        self.estate("proposal", "amend", key, "--classification", "decision",
                    "--question", "which way now",
                    "--alternative", "Gamma :: a new plan",
                    "--alternative", "Delta :: another",
                    "--recommendation", "Gamma",
                    "--defer-consequence", "still rots")
        out = self.estate("proposal", "show", key).stdout
        self.assertIn("alternatives      Gamma", out)   # the row moved
        self.assertIn("option 2 of 2: Beta", out)       # the decision did not
        self.assertIn("Alpha — costs a day", out)

    def test_a_reopened_proposal_does_not_print_a_decision_that_no_longer_stands(self):
        key = self.approved()
        self.estate("proposal", "stage", key, "pending-review", "hold on",
                    "--actor", "owner")
        self.assertNotIn("chosen", self.estate("proposal", "show", key).stdout)

    def test_the_route_is_shown_once_something_takes_it(self):
        key = self.approved()
        self.assertNotIn("picked up", self.estate("proposal", "show", key).stdout)
        self.estate("pickup", key, "dispatched", "--route", "dispatch",
                    "--ref", "ephemeral - extractor-6", "--actor", "hub")
        self.assertIn("picked up         dispatch (ephemeral - extractor-6)",
                      self.estate("proposal", "show", key).stdout)

    def test_an_action_prints_neither(self):
        key = self.an_action("a settled thing")
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner")
        out = self.estate("proposal", "show", key).stdout
        self.assertNotIn("chosen", out)
        self.assertNotIn("picked up", out)


class PickupVerb(StoreBase):
    def approved(self, **kw):
        key = self.a_decision(**kw)
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner", "--chosen", "1")
        return key

    def test_it_writes_one_event_carrying_the_route(self):
        key = self.approved()
        self.estate("pickup", key, "queued as task:9", "--route", "night-shift",
                    "--ref", "task:9", "--actor", "hub")
        row = self.events(key, P.PICKUP_KIND)[-1]
        record = json.loads(row["refs"])[P.PICKUP_KEY]
        self.assertEqual(record[P.ROUTE_KEY], "night-shift")
        self.assertEqual(record[P.REF_KEY], "task:9")
        self.assertEqual(row["detail"], "queued as task:9")

    def test_it_changes_no_status_and_no_stage(self):
        """Routing work to an executor is not claiming it — the executors claim
        through their own doors, and recording both is what tells 'handed to a
        worker' from 'nobody has touched this'."""
        key = self.approved()
        before = dict(self.conn().execute(
            "SELECT status, stage FROM tasks WHERE id=?", (key,)).fetchone())
        self.estate("pickup", key, "dispatched", "--route", "dispatch",
                    "--actor", "hub")
        after = dict(self.conn().execute(
            "SELECT status, stage FROM tasks WHERE id=?", (key,)).fetchone())
        self.assertEqual(before, after)

    def test_it_refuses_a_proposal_nobody_approved(self):
        """A marker on an undecided row would make the sweep skip a proposal
        that never passed P-02's gate."""
        key = self.a_decision()
        out = self.run_estate("pickup", key, "queued", "--route", "night-shift",
                              "--actor", "hub")
        self.assertEqual(out.returncode, 2)
        self.assertIn("approved-backlog", out.stderr)
        self.assertEqual(self.events(key, P.PICKUP_KIND), [])

    def test_it_refuses_an_unknown_route_and_an_empty_sentence(self):
        key = self.approved()
        self.assertEqual(self.run_estate(
            "pickup", key, "x", "--route", "telepathy").returncode, 2)
        self.assertEqual(self.run_estate(
            "pickup", key, "   ", "--route", "hub").returncode, 2)
        self.assertEqual(self.events(key, P.PICKUP_KIND), [])

    def test_it_refuses_a_task_that_does_not_exist(self):
        out = self.run_estate("pickup", "t-999", "x", "--route", "hub")
        self.assertEqual(out.returncode, 2)


class DecidedNotActedQuery(StoreBase):
    def approved(self, title="a real choice", hours=48.0):
        key = self.a_decision(title)
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner", "--chosen", "1")
        if hours:
            self.backdate(key, hours)
        return key

    def test_an_approved_untouched_proposal_is_owed_and_exits_three(self):
        key = self.approved()
        row, code = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.OWED)
        self.assertEqual(code, 3)

    def test_it_reports_who_decided_and_how_long_ago(self):
        key = self.approved(hours=27.6)
        row, _ = self.verdict_of(key)
        self.assertEqual(row["decided_by"], "owner")
        self.assertAlmostEqual(row["age_hours"], 27.6, delta=0.2)

    def test_a_recorded_pickup_takes_it_off_the_list(self):
        key = self.approved()
        self.estate("pickup", key, "dispatched an extractor worker",
                    "--route", "dispatch", "--ref", "ephemeral - extractor-6",
                    "--actor", "hub")
        row, code = self.verdict_of(key)
        self.assertEqual((row["verdict"], row["route"]), (P.PICKED_UP, "dispatch"))
        self.assertEqual(code, 0)

    def test_a_parked_row_is_its_own_verdict_and_not_owed(self):
        """The completion check's own exception: 'other than rows deliberately
        parked'. It is a recorded fact carrying a reason, not a filter."""
        key = self.approved()
        self.estate("pickup", key, "the owner: leave it until the other work lands",
                    "--route", "parked", "--actor", "hub")
        row, code = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.PARKED_V)
        self.assertEqual(code, 0)

    def test_notes_after_the_decision_are_not_a_pickup(self):
        """t-777 and t-1329 carry nineteen mechanic notes between them since
        their approvals and are the two most stuck rows in the store. A sweep
        that counted notes would report them handled."""
        key = self.approved()
        for n in range(3):
            self.estate("note", key, f"condition observed again ({n})",
                        "--actor", "mechanic")
        row, _ = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.OWED)

    def test_a_claim_after_the_decision_is_a_pickup(self):
        key = self.approved()
        self.estate("claim", key, "--actor", "pr-reviewer")
        row, _ = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.PICKED_UP)

    def test_a_night_shift_queue_key_event_is_a_pickup(self):
        """The signal SKILL.md step 3 was already reaching for — read here
        where it actually is, on the event."""
        key = self.approved()
        self.estate("note", key, "enqueued on the night shift's queue as task:9",
                    "--actor", "pr-reviewer",
                    "--refs", json.dumps({"queue_key": "task:9",
                                          "queue": "pr-reviewer"}))
        row, _ = self.verdict_of(key)
        self.assertEqual((row["verdict"], row["route"]),
                         (P.PICKED_UP, P.NIGHT_SHIFT))

    def test_a_queue_key_from_BEFORE_the_decision_does_not_count(self):
        """A row queued, released and re-decided has been decided AGAIN. The
        window opens at the decision, or the second decision inherits the first
        one's pickup and is never reported."""
        key = self.a_decision()
        self.estate("note", key, "enqueued long ago", "--actor", "pr-reviewer",
                    "--refs", json.dumps({"queue_key": "task:1"}))
        self.estate("proposal", "stage", key, "approved-backlog", "go",
                    "--actor", "owner", "--chosen", "1")
        self.backdate(key, 48)
        row, _ = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.OWED)

    def test_the_last_approval_is_the_decision_instant(self):
        """Approved, reopened, approved again: measuring from the first would
        age a fresh decision by however long the reconsideration took."""
        key = self.approved(hours=200)
        self.estate("proposal", "stage", key, "pending-review", "hold on",
                    "--actor", "owner")
        self.estate("proposal", "stage", key, "approved-backlog", "go again",
                    "--actor", "owner", "--chosen", "2")
        row, code = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.FRESH)
        self.assertEqual(code, 0)

    def test_a_fresh_decision_is_not_owed_within_one_tick(self):
        key = self.approved(hours=0)
        row, code = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.FRESH)
        self.assertEqual(code, 0)
        # ...and --within is what a caller pacing differently overrides with.
        row, code = self.verdict_of(key, "--within", "0")
        self.assertEqual(row["verdict"], P.OWED)
        self.assertEqual(code, 3)

    def test_needs_owner_and_blocked_read_as_parked_without_a_marker(self):
        for verb, status in (("needs-owner", "needs-owner"), ("block", "blocked")):
            key = self.approved(f"parked via {verb}")
            self.estate("claim", key, "--actor", "hub")
            self.estate(verb, key, "over to you")
            row, _ = self.verdict_of(key)
            self.assertEqual(row["verdict"], P.PARKED_V, verb)
            self.assertEqual(row["status"], status)

    def test_a_row_with_no_approval_event_is_unobservable_and_exits_two(self):
        """Approved rows normally all carry an approval event, so nothing
        reads this on an ordinary store. The verdict exists anyway because `owed` and `fresh`
        are both claims about an AGE, and one made with no clock behind it is
        an accusation nobody can check."""
        key = self.an_unrecorded_approval()
        row, code = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.UNOBSERVABLE)
        self.assertIsNone(row["age_hours"])
        self.assertEqual(code, 2)

    def test_a_proposal_filed_straight_at_approved_has_a_decision_instant(self):
        """`proposal add --stage approved-backlog` writes an `activity` row and
        no transition. That IS a decision instant, and reading only transitions
        would report every such row unobservable."""
        self.estate("proposal", "add", "filed approved", "--condition", "seen",
                    "--stage", "approved-backlog", "--actor", "mechanic")
        key = self.last_task()
        self.backdate(key, 48)
        row, _ = self.verdict_of(key)
        self.assertEqual(row["verdict"], P.OWED)

    def test_a_closed_proposal_is_out_of_scope_entirely(self):
        key = self.approved()
        self.estate("proposal", "stage", key, "resolved", "it landed",
                    "--actor", "hub")
        rows, code = self.decided()
        self.assertNotIn(key, [r["id"] for r in rows])
        self.assertEqual(code, 0)

    def test_a_pending_proposal_is_out_of_scope(self):
        self.a_decision()
        rows, code = self.decided()
        self.assertEqual(rows, [])
        self.assertEqual(code, 0)

    def test_owed_only_hides_the_rest_and_keeps_the_exit_code(self):
        owed = self.approved("still stuck")
        picked = self.approved("already routed")
        self.estate("pickup", picked, "done inline", "--route", "hub",
                    "--actor", "hub")
        out = self.run_estate("decided-not-acted", "--owed-only", "--json")
        self.assertEqual(out.returncode, 3)
        self.assertEqual([r["id"] for r in json.loads(out.stdout)], [owed])

    def test_owed_rows_sort_oldest_first(self):
        recent = self.approved("recent", hours=25)
        ancient = self.approved("ancient", hours=400)
        rows, _ = self.decided()
        self.assertEqual([r["id"] for r in rows][:2], [ancient, recent])

    def test_the_text_rendering_names_the_verdict_and_the_reason(self):
        key = self.approved()
        out = self.run_estate("decided-not-acted")
        self.assertEqual(out.returncode, 3)
        self.assertIn(P.OWED, out.stdout)
        self.assertIn(key, out.stdout)
        self.assertIn("nothing has picked it up since", out.stdout)

    def test_it_writes_nothing(self):
        """Read-only, like every standing check here. `estate ready` is the
        claimable list; this only reports on it."""
        key = self.approved()
        before = len(self.events(key))
        self.run_estate("decided-not-acted")
        self.run_estate("decided-not-acted", "--json")
        self.assertEqual(len(self.events(key)), before)

    def test_a_negative_window_is_refused(self):
        self.assertEqual(
            self.run_estate("decided-not-acted", "--within", "-1").returncode, 2)


# --------------------------------------------------------------------------- #
# 3. the endpoint the completion check names
# --------------------------------------------------------------------------- #

def _load(name: str, path: str):
    """The extensionless scripts here are loaded the way the server loads the
    CLI itself — spec-based, not the deprecated `load_module`."""
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


srv = _load("_t1794_dashboard_server",
            os.path.join(BIN_DIR, "dashboard-server"))


class ProposalStageEndpoint(unittest.TestCase):
    """"...and `POST /proposal-stage` rejects a decision-classified approval
    that carries no chosen alternative" — the completion check, verbatim."""

    @staticmethod
    def server_takes_the_choice():
        """True when `bin/dashboard-server` knows the `chosen` field.

        The same shape of guard tests/test_estate_pairs.py keeps on
        `bin/followups`, and for the same reason: this is a contract between
        two scripts and only one of them is in this extraction. A
        `dashboard-server` predating the field answers every approval with
        `unexpected proposal decision field`, and asserting against it would
        report a missing re-sync as a bug in the store. On the FILE rather
        than a version string, so these start running again by themselves the
        moment that script lands.
        """
        try:
            with open(os.path.join(BIN_DIR, "dashboard-server"),
                      encoding="utf-8") as handle:
                return "chosen" in handle.read()
        except OSError:
            return False

    def setUp(self):
        if not self.server_takes_the_choice():
            self.skipTest("bin/dashboard-server here predates the chosen-"
                          "alternative field — the other half of this "
                          "contract has not been re-synced yet")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        self._saved = (srv.STATE, srv.ACKS, srv.ESTATE_DB, srv._ESTATE_MODULE)
        srv.STATE = self.state
        srv.ACKS = os.path.join(self.state, "dashboard-acks.json")
        srv.ESTATE_DB = os.path.join(self.state, "estate.db")
        srv._ESTATE_MODULE = None
        estate = srv._load_estate()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            estate.main(["proposal", "add", "a real choice",
                         "--condition", "seen", "--actor", "mechanic",
                         *DECISION_FLAGS])
            estate.main(["proposal", "add", "already chosen",
                         "--condition", "also seen", "--actor", "mechanic",
                         "--classification", "action"])
        os.makedirs(os.path.join(self.state, "secrets"), exist_ok=True)
        with open(os.path.join(self.state, "secrets", "dashboard-write-token"),
                  "w", encoding="utf-8") as fh:
            fh.write("s3cret\n")
        self.httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        (srv.STATE, srv.ACKS, srv.ESTATE_DB, srv._ESTATE_MODULE) = self._saved

    def post(self, payload, token="s3cret"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/proposal-stage", body=json.dumps(payload),
                     headers={"Content-Type": "application/json",
                              "X-Dashboard-Token": token})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, raw.decode()

    def payload(self, **updates):
        out = {"id": "t-1", "expected_stage": "pending-review",
               "stage": "approved-backlog", "note": "Owner clicked: Alpha"}
        out.update(updates)
        return out

    def row(self, key="t-1"):
        conn = sqlite3.connect(srv.ESTATE_DB)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM tasks WHERE id=?",
                                     (key,)).fetchone())
        finally:
            conn.close()

    def approval_refs(self, key="t-1"):
        conn = sqlite3.connect(srv.ESTATE_DB)
        try:
            row = conn.execute(
                "SELECT refs FROM events WHERE task_id=? AND kind='transition' "
                "ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0] or "{}")

    def test_an_approval_with_no_chosen_alternative_is_rejected(self):
        status, body = self.post(self.payload())
        self.assertEqual(status, 400, body)
        self.assertIn("1. Alpha", body["error"])
        self.assertEqual(self.row()["stage"], "pending-review")

    def test_an_approval_carrying_one_is_recorded_on_the_event(self):
        status, body = self.post(self.payload(chosen=1))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.row()["stage"], "approved-backlog")
        record = self.approval_refs()[P.CHOSEN_KEY]
        self.assertEqual((record[P.POSITION_KEY], record[P.OPTION_KEY]),
                         (1, "Alpha"))
        self.assertEqual([a["option"] for a in record[P.ALTERNATIVES_KEY]],
                         ["Alpha", "Beta"])

    def test_the_field_is_a_number_and_never_text(self):
        for junk in ("1", "Alpha", True, 1.5, [1], {"position": 1}):
            status, body = self.post(self.payload(chosen=junk))
            self.assertEqual(status, 400, junk)
        self.assertEqual(self.row()["stage"], "pending-review")

    def test_an_out_of_range_position_is_rejected_by_the_store_not_guessed(self):
        status, body = self.post(self.payload(chosen=2))
        self.assertEqual(status, 200, body)      # 2 is Beta and is in range
        self.assertEqual(self.approval_refs()[P.CHOSEN_KEY][P.OPTION_KEY], "Beta")

    def test_a_position_past_the_end_is_a_four_hundred(self):
        status, body = self.post(self.payload(chosen=9))
        self.assertEqual(status, 400, body)
        self.assertIn("1..2", body["error"])
        self.assertEqual(self.row()["stage"], "pending-review")

    def test_an_absurd_position_never_reaches_the_write_lock(self):
        status, _ = self.post(self.payload(chosen=10 ** 9))
        self.assertEqual(status, 400)

    def test_an_action_still_approves_with_no_chosen_field(self):
        status, body = self.post(
            {"id": "t-2", "expected_stage": "pending-review",
             "stage": "approved-backlog", "note": "go"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.row("t-2")["stage"], "approved-backlog")

    def test_a_chosen_field_on_a_rejection_is_refused(self):
        status, body = self.post(self.payload(stage="rejected", chosen=1))
        self.assertEqual(status, 400, body)
        self.assertEqual(self.row()["stage"], "pending-review")

    def test_the_allowlist_is_still_closed(self):
        status, _ = self.post(self.payload(chosen=1, command="rm"))
        self.assertEqual(status, 400)
        self.assertEqual(self.row()["stage"], "pending-review")

    def test_a_stale_snapshot_still_conflicts_before_anything_is_chosen(self):
        status, _ = self.post(
            self.payload(expected_stage="approved-backlog", chosen=1))
        self.assertEqual(status, 409)
        self.assertEqual(self.row()["stage"], "pending-review")


# --------------------------------------------------------------------------- #
# 4. the click that makes the choice
# --------------------------------------------------------------------------- #

class DecisionCardsSelect(unittest.TestCase):
    def setUp(self):
        self.js = dashboard_estate.JS
        self.proposals = dashboard_estate.JS_PROPOSALS

    def test_an_option_card_carries_its_stored_position(self):
        self.assertIn("data-decision-position", self.proposals)
        self.assertIn("position:i+1", self.proposals)

    def test_the_position_survives_a_malformed_entry(self):
        """altRows numbers BEFORE it filters. Numbering after would make a
        hole shift every card below it and send a position naming a different
        branch than the one clicked."""
        self.assertIn("p.alternatives.map(function(v,i)", self.proposals)
        self.assertNotIn("p.alternatives.filter(function(v){return v&&v.option})",
                         self.proposals)

    def test_only_option_cards_carry_a_position(self):
        """Resolving 'take my recommendation' to an option would be a regex
        over free text — the alternative this proposal explicitly rejected."""
        for card in ("decisionCard(id,'recommendation'", "decisionCard(id,'defer'"):
            start = self.proposals.index(card)
            self.assertNotIn("v.position",
                             self.proposals[start:start + 120])

    def test_clicking_a_non_option_card_clears_the_selection(self):
        self.assertIn("delete DECISION_CHOICES[id]", self.proposals)

    def test_the_approval_is_blocked_client_side_without_a_choice(self):
        self.assertIn("needChoice=approving&&isDecision(t.proposal||{})",
                      self.proposals)
        self.assertIn("if(needChoice&&!picked)", self.proposals)

    def test_the_position_rides_only_on_an_approval(self):
        self.assertIn("if(approving&&picked)body.chosen=picked", self.proposals)

    def test_the_selection_survives_the_thirty_second_refresh(self):
        """DECISION_CHOICES is module state and the card re-renders its own
        class from it, the same bargain DECISION_NOTES already has."""
        self.assertIn("DECISION_CHOICES={}", self.js)
        self.assertIn("chosenOf(id)===position", self.js)

    def test_a_selection_is_cleared_once_the_decision_lands(self):
        self.assertIn("delete DECISION_NOTES[id];delete DECISION_CHOICES[id];",
                      self.proposals)

    def test_the_class_is_toggled_in_place_rather_than_by_redrawing(self):
        """A redraw would discard the note the reader is halfway through."""
        self.assertIn("function markChoice(id)", self.proposals)
        self.assertIn("classList.toggle('picked'", self.proposals)

    def test_the_page_says_a_choice_is_wanted_before_it_refuses_one(self):
        self.assertIn("choice-hint", self.proposals)
        self.assertIn("Click the option you are choosing before approving",
                      self.proposals)
        self.assertIn(".decision-card.picked{", dashboard_estate.CSS)


class PrimaryPageCanChooseToo(unittest.TestCase):
    """The primary page embeds JS_PROPOSALS byte-for-byte, so it inherits the
    refusal. If it did not also wire the cards, the refusal would be
    unanswerable there — a page that can only ever say no."""

    def setUp(self):
        self.js = dashboard_primary.proposal_review_js()

    def test_it_declares_the_selection_state_the_shared_code_reads(self):
        self.assertIn("DECISION_CHOICES={}", self.js)

    def test_it_wires_the_cards_and_not_only_the_buttons(self):
        self.assertIn("closest('[data-decision-card]')", self.js)
        self.assertIn("prefillDecision(card)", self.js)

    def test_it_still_embeds_the_shared_implementation(self):
        self.assertIn(dashboard_estate.JS_PROPOSALS, self.js)


# --------------------------------------------------------------------------- #
# 5. the sweep step, in the skill that runs it — DELIBERATELY NOT HERE
# --------------------------------------------------------------------------- #
#
# The private fork keeps a class here asserting that its own hub SKILL.md
# carries the decided-not-acted sweep step: the query, the executor routes,
# the recording call after each one. That prose is one installation's
# standing order rather than this mechanism, and the hub skill shipped in
# this core deliberately does not carry it (docs/extraction-allowlist.md,
# "Standing orders in the hub skill"). An installation that adds the step
# to its own skill should add the guard with it.
#
# Everything above this line tests the mechanism itself and does run.


if __name__ == "__main__":
    unittest.main(verbosity=1)

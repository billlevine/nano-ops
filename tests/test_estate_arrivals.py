#!/usr/bin/env python3
"""Tests for V-05: the arrival rate, and what may be compared with what.

Two layers, on the same reasoning as test_estate_attention. `lib/estate_arrivals.py`
is pure, so the period boundaries and the observability floor are tested by
moving `now` and the floor rather than by waiting or by migrating anything. The
CLI is then tested against a real throwaway store through the real `bin/estate`,
because the definition of an arrival is SQL and a mocked store would prove the
wrong thing about it.

The load-bearing negative is `trend_is_evidence`. Most of what this command
prints is arithmetic anybody could redo; the part that has to be right is its
refusal to call two numbers a trend when one of them predates the mechanism
that would have recorded it.
"""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
BIN_DIR = os.path.join(REPO_ROOT, "bin")
ESTATE = os.path.join(BIN_DIR, "estate")
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import estate_arrivals as A  # noqa: E402

def followups_is_estate_backed() -> bool:
    """True when bin/followups is the shim that writes THIS store.

    The standalone JSON store writes nothing this query can see, so the one
    test that drives a departure through it would report a missing re-sync as
    a counting bug. Checking the file rather than a version string means the
    test resumes by itself once the shim lands.
    """
    try:
        with open(os.path.join(BIN_DIR, "followups")) as f:
            return "ESTATE_STATE_DIR" in f.read()
    except OSError:
        return False


UTC = dt.timezone.utc
# Fixed offsets and a named zone, never the machine's: the bucket boundaries
# must hold on a laptop anywhere and in CI.
EDT = dt.timezone(dt.timedelta(hours=-4))
TOKYO = dt.timezone(dt.timedelta(hours=9))
NY = ZoneInfo("America/New_York")
# A Thursday, so a week bucket and a day bucket are never the same date.
THURS = dt.datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class PeriodCase(unittest.TestCase):
    def test_week_starts_monday(self):
        self.assertEqual(A.period_start(THURS, A.WEEK, UTC),
                         dt.date(2026, 7, 27))

    def test_a_monday_is_its_own_week(self):
        monday = dt.datetime(2026, 7, 27, 0, 30, tzinfo=UTC)
        self.assertEqual(A.period_start(monday, A.WEEK, UTC),
                         dt.date(2026, 7, 27))

    def test_day_is_the_local_calendar_date(self):
        self.assertEqual(A.period_start(THURS, A.DAY, UTC), dt.date(2026, 7, 30))

    def test_the_bucket_boundary_is_local(self):
        """02:00 UTC Monday is still Sunday in New York, so it is last week."""
        stamp = dt.datetime(2026, 8, 3, 2, 0, tzinfo=UTC)
        self.assertEqual(A.period_start(stamp, A.WEEK, UTC), dt.date(2026, 8, 3))
        self.assertEqual(A.period_start(stamp, A.WEEK, EDT), dt.date(2026, 7, 27))
        # And east of UTC it can already be the next day.
        self.assertEqual(A.period_start(stamp, A.DAY, TOKYO), dt.date(2026, 8, 3))
        self.assertEqual(A.period_start(stamp, A.DAY, EDT), dt.date(2026, 8, 2))

    def test_a_week_containing_a_dst_change_is_still_seven_local_days(self):
        """2026-11-01 is the fall-back Sunday. start + 7*24h would end an hour
        early and put an hour of Sunday night into the next week."""
        start, end = A.period_bounds(dt.date(2026, 10, 26), A.WEEK, NY)
        self.assertEqual(end - start, dt.timedelta(hours=169))
        self.assertEqual(start.astimezone(NY).hour, 0)
        self.assertEqual(end.astimezone(NY).hour, 0)

    def test_bounds_are_half_open(self):
        _, end = A.period_bounds(dt.date(2026, 7, 27), A.WEEK, UTC)
        nxt, _ = A.period_bounds(dt.date(2026, 8, 3), A.WEEK, UTC)
        self.assertEqual(end, nxt)

    def test_recent_periods_are_oldest_first_and_end_with_now(self):
        days = A.recent_periods(THURS, A.WEEK, 3, UTC)
        self.assertEqual(days, [dt.date(2026, 7, 13), dt.date(2026, 7, 20),
                                dt.date(2026, 7, 27)])

    def test_recent_days(self):
        self.assertEqual(A.recent_periods(THURS, A.DAY, 2, UTC),
                         [dt.date(2026, 7, 29), dt.date(2026, 7, 30)])


class VerdictCase(unittest.TestCase):
    """The four verdicts, and the one predicate hanging off them."""

    def verdict(self, day, floor, now=THURS, period=A.WEEK):
        return A.classify_period(day, period, floor, now, UTC)

    def test_a_period_wholly_after_the_floor_and_over_is_observed(self):
        floor = dt.datetime(2026, 7, 1, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 20), floor), A.OBSERVED)

    def test_the_current_period_is_in_progress(self):
        floor = dt.datetime(2026, 7, 1, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 27), floor), A.IN_PROGRESS)

    def test_a_period_ending_before_the_floor_is_unobservable(self):
        floor = dt.datetime(2026, 7, 27, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 13), floor), A.UNOBSERVABLE)

    def test_a_period_ending_exactly_at_the_floor_is_unobservable(self):
        """The bucket is half-open, so a floor at the boundary belongs to the
        next period and this one recorded nothing it could have."""
        floor = dt.datetime(2026, 7, 20, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 13), floor), A.UNOBSERVABLE)

    def test_a_floor_inside_the_period_straddles(self):
        floor = dt.datetime(2026, 7, 23, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 20), floor),
                         A.STRADDLES_FLOOR)

    def test_a_floor_exactly_at_the_start_does_not_straddle(self):
        floor = dt.datetime(2026, 7, 20, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 20), floor), A.OBSERVED)

    def test_straddling_beats_in_progress(self):
        """Both disqualify; waiting fixes one of them and nothing fixes the
        other, so the one that cannot be waited out is what gets reported."""
        floor = dt.datetime(2026, 7, 29, tzinfo=UTC)
        self.assertEqual(self.verdict(dt.date(2026, 7, 27), floor),
                         A.STRADDLES_FLOOR)

    def test_no_floor_at_all_is_unobservable_everywhere(self):
        """A class with no rows cannot tell "none arrived" from "nothing here
        writes these", so it never claims the first."""
        for day in (dt.date(2026, 7, 13), dt.date(2026, 7, 20)):
            self.assertEqual(self.verdict(day, None), A.UNOBSERVABLE)

    def test_only_two_observed_periods_are_evidence(self):
        for a in A.VERDICTS:
            for b in A.VERDICTS:
                expected = (a == A.OBSERVED and b == A.OBSERVED)
                self.assertEqual(A.trend_is_evidence(a, b), expected, (a, b))


class CompareCase(unittest.TestCase):
    def test_change_and_percentage(self):
        got = A.compare(15, 10, A.OBSERVED, A.OBSERVED)
        self.assertEqual(got["change"], 5)
        self.assertEqual(got["pct"], 50.0)
        self.assertTrue(got["trend_is_evidence"])

    def test_a_rise_from_nothing_has_no_percentage(self):
        got = A.compare(12, 0, A.OBSERVED, A.OBSERVED)
        self.assertEqual(got["change"], 12)
        self.assertIsNone(got["pct"])

    def test_the_verdicts_ride_along_so_a_reader_need_not_refetch(self):
        got = A.compare(1, 2, A.IN_PROGRESS, A.OBSERVED)
        self.assertFalse(got["trend_is_evidence"])
        self.assertEqual(got["current_verdict"], A.IN_PROGRESS)
        self.assertEqual(got["previous_verdict"], A.OBSERVED)

    def test_last_comparable_finds_the_newest_observed_pair(self):
        verdicts = [A.UNOBSERVABLE, A.STRADDLES_FLOOR, A.OBSERVED, A.OBSERVED,
                    A.IN_PROGRESS]
        self.assertEqual(A.last_comparable(verdicts), 3)

    def test_last_comparable_is_none_when_no_pair_qualifies(self):
        """The store's real shape for pending-review the day this was written:
        one observed week and nothing before it to compare with."""
        verdicts = [A.UNOBSERVABLE, A.UNOBSERVABLE, A.STRADDLES_FLOOR,
                    A.OBSERVED, A.IN_PROGRESS]
        self.assertIsNone(A.last_comparable(verdicts))

    def test_index_zero_is_never_comparable(self):
        self.assertIsNone(A.last_comparable([A.OBSERVED]))


class BucketCase(unittest.TestCase):
    def rows(self):
        return [
            (dt.datetime(2026, 7, 28, 9, 0, tzinfo=UTC), "mechanic"),
            (dt.datetime(2026, 7, 29, 9, 0, tzinfo=UTC), "mechanic"),
            (dt.datetime(2026, 8, 4, 9, 0, tzinfo=UTC), "hub"),
            (dt.datetime(2026, 8, 4, 10, 0, tzinfo=UTC), None),
        ]

    def test_counts_land_in_their_week(self):
        got = A.bucket(self.rows(), A.WEEK, UTC)
        self.assertEqual(got[dt.date(2026, 7, 27)]["total"], 2)
        self.assertEqual(got[dt.date(2026, 8, 3)]["total"], 2)

    def test_producers_are_counted_separately(self):
        got = A.bucket(self.rows(), A.WEEK, UTC)
        self.assertEqual(got[dt.date(2026, 7, 27)]["by_producer"],
                         {"mechanic": 2})

    def test_a_missing_producer_is_named_not_dropped(self):
        got = A.bucket(self.rows(), A.WEEK, UTC)
        self.assertEqual(got[dt.date(2026, 8, 3)]["by_producer"]["(unattributed)"], 1)

    def test_empty_buckets_are_absent_so_the_caller_decides_the_range(self):
        got = A.bucket(self.rows(), A.WEEK, UTC)
        self.assertNotIn(dt.date(2026, 7, 20), got)

    def test_floor_is_the_earliest(self):
        self.assertEqual(A.floor_of(self.rows()),
                         dt.datetime(2026, 7, 28, 9, 0, tzinfo=UTC))

    def test_floor_of_nothing_is_none(self):
        self.assertIsNone(A.floor_of([]))


class ParseClassesCase(unittest.TestCase):
    def test_comma_separated_and_repeatable(self):
        self.assertEqual(A.parse_classes(["followup,needs-owner", "followup"]),
                         [A.FOLLOWUP, A.NEEDS_OWNER])

    def test_a_typo_is_refused_and_names_the_three(self):
        """Silently matching nothing would read exactly like "nothing arrived"."""
        with self.assertRaises(ValueError) as caught:
            A.parse_classes("pending_review")
        self.assertIn("pending-review", str(caught.exception))

    def test_empty_is_empty_not_an_error(self):
        self.assertEqual(A.parse_classes([]), [])
        self.assertEqual(A.parse_classes(["", "  "]), [])


class CliCase(unittest.TestCase):
    """The definition of an arrival is SQL, so this half runs the real thing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def estate(self, *args, check=True):
        out = subprocess.run([sys.executable, ESTATE, *args],
                             capture_output=True, text=True,
                             env=dict(os.environ, ESTATE_STATE_DIR=self.state,
                                      ESTATE_TZ="UTC"))
        if check:
            self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def blocks(self, *args):
        payload = json.loads(self.estate("arrivals", "--json", *args).stdout)
        return {block["class"]: block for block in payload["classes"]}, payload

    def latest(self, cls, *args):
        blocks, _ = self.blocks(*args)
        return blocks[cls]["periods"][-1]

    def backdate(self, days):
        """Push every TASK in the store `days` days into the past.

        The store stamps NOW, and a rate needs history. Rewriting this test's
        own throwaway copy is the only way to get a second period without
        sleeping through one.

        Tasks only, on purpose: `events` carries an append-only trigger and
        refuses an UPDATE, which is correct and is not something a test should
        route around. So the history layer here is exercised through the
        `followup` class, whose arrivals are read from `tasks.created_at`. The
        other two classes are events, and every floor and verdict rule they
        obey is covered against the pure module above, where the floor is an
        argument rather than a row.
        """
        with sqlite3.connect(os.path.join(self.state, "estate.db")) as conn:
            for col in ("created_at", "updated_at"):
                conn.execute(
                    f"UPDATE tasks SET {col} = "
                    f"strftime('%Y-%m-%dT%H:%M:%f000+00:00', {col}, ?)",
                    (f"-{days} days",))
            stamps = [r[0] for r in conn.execute("SELECT created_at FROM tasks")]
        # A silently unparsed shift would leave every row at `now` and make
        # every history assertion below vacuously pass.
        for stamp in stamps:
            self.assertIsNotNone(A.parse_iso(stamp), stamp)

    # ── what counts ──────────────────────────────────────────────────────────
    def test_a_new_followup_is_an_arrival_attributed_to_its_creator(self):
        self.estate("task", "add", "ask the owner", "--kind", "followup",
                    "--actor", "night-shift")
        row = self.latest(A.FOLLOWUP)
        self.assertEqual(row["arrivals"], 1)
        self.assertEqual(row["by_producer"], {"night-shift": 1})

    def test_a_task_that_is_not_a_followup_is_not_an_arrival(self):
        self.estate("task", "add", "ordinary work", "--kind", "rollout")
        self.assertEqual(self.latest(A.FOLLOWUP)["arrivals"], 0)

    def test_needs_owner_counts_when_the_task_gets_there(self):
        self.estate("task", "add", "work", "--kind", "rollout", "--ready")
        self.estate("claim", "t-1", "--actor", "night-shift")
        self.estate("needs-owner", "t-1", "your call", "--actor", "night-shift")
        row = self.latest(A.NEEDS_OWNER)
        self.assertEqual(row["arrivals"], 1)
        self.assertEqual(row["by_producer"], {"night-shift": 1})

    def test_leaving_needs_owner_is_a_departure_not_a_second_arrival(self):
        self.estate("task", "add", "work", "--kind", "rollout", "--ready")
        self.estate("claim", "t-1", "--actor", "night-shift")
        self.estate("needs-owner", "t-1", "your call", "--actor", "night-shift")
        self.estate("reopen", "t-1", "answered", "--actor", "hub")
        row = self.latest(A.NEEDS_OWNER)
        self.assertEqual((row["arrivals"], row["departures"], row["net"]),
                         (1, 1, 0))

    def test_the_same_task_arriving_twice_is_counted_twice(self):
        """Each trip costs the owner a fresh read, so each trip is an arrival."""
        self.estate("task", "add", "work", "--kind", "rollout", "--ready")
        for _ in range(2):
            self.estate("claim", "t-1", "--actor", "night-shift")
            self.estate("needs-owner", "t-1", "your call", "--actor", "night-shift")
            self.estate("reopen", "t-1", "answered", "--actor", "hub")
        self.assertEqual(self.latest(A.NEEDS_OWNER)["arrivals"], 2)

    @unittest.skipUnless(followups_is_estate_backed(),
                         "bin/followups here is the standalone JSON store, not "
                         "the shim over this store")
    def test_the_shims_own_word_for_resolving_a_followup_is_a_departure(self):
        """`bin/followups resolve` sets the status to `done` and records the
        event as `open -> resolved`. That is how the shim closes nearly every
        follow-up, so a query reading only the store's two terminal words would
        show a queue that never drains."""
        self.estate("task", "add", "ask the owner", "--kind", "followup",
                    "--actor", "hub")
        out = subprocess.run(
            [sys.executable, os.path.join(BIN_DIR, "followups"),
             "resolve", "t-1", "answered"],
            capture_output=True, text=True,
            env=dict(os.environ, ESTATE_STATE_DIR=self.state, ESTATE_TZ="UTC"))
        self.assertEqual(out.returncode, 0, out.stderr)
        row = self.latest(A.FOLLOWUP)
        self.assertEqual((row["arrivals"], row["departures"], row["net"]),
                         (1, 1, 0))

    def test_a_stage_decision_closing_a_followup_departs_once_not_twice(self):
        """A follow-up-kind task adopted into the proposal lifecycle leaves by
        a review decision, which writes BOTH a `stage: ... -> resolved` row and
        the status change it drives. The task left the set once, so counting
        the stage row too would report a drain that did not happen."""
        self.estate("task", "add", "ask the owner", "--kind", "followup",
                    "--actor", "hub")
        self.estate("proposal", "add", "same thing", "--adopt-task", "t-1",
                    "--actor", "mechanic", "--condition", "c",
                    "--desired-outcome", "d", "--completion-check", "e")
        self.estate("proposal", "stage", "t-1", "resolved", "--actor", "owner")
        row = self.latest(A.FOLLOWUP)
        self.assertEqual((row["arrivals"], row["departures"]), (1, 1))

    def test_a_proposal_filed_pending_review_arrives(self):
        self.estate("proposal", "add", "a finding", "--actor", "mechanic",
                    "--condition", "c", "--desired-outcome", "d",
                    "--completion-check", "e")
        row = self.latest(A.PENDING_REVIEW)
        self.assertEqual(row["arrivals"], 1)
        self.assertEqual(row["by_producer"], {"mechanic": 1})

    def test_a_proposal_filed_already_decided_never_arrives(self):
        """It never waited on the owner, so counting it would inflate the rate with
        bookkeeping."""
        self.estate("proposal", "add", "already handled", "--actor", "mechanic",
                    "--stage", "resolved", "--condition", "c",
                    "--desired-outcome", "d", "--completion-check", "e")
        self.assertEqual(self.latest(A.PENDING_REVIEW)["arrivals"], 0)

    def test_a_decision_is_a_departure(self):
        self.estate("proposal", "add", "a finding", "--actor", "mechanic",
                    "--condition", "c", "--desired-outcome", "d",
                    "--completion-check", "e")
        self.estate("proposal", "stage", "t-1", "approved-backlog",
                    "--actor", "owner")
        row = self.latest(A.PENDING_REVIEW)
        self.assertEqual((row["arrivals"], row["departures"]), (1, 1))

    def test_restaging_back_into_pending_review_arrives_again(self):
        self.estate("proposal", "add", "a finding", "--actor", "mechanic",
                    "--condition", "c", "--desired-outcome", "d",
                    "--completion-check", "e")
        self.estate("proposal", "stage", "t-1", "stopped", "--actor", "owner")
        self.estate("proposal", "stage", "t-1", "pending-review", "--actor", "owner")
        row = self.latest(A.PENDING_REVIEW)
        self.assertEqual((row["arrivals"], row["departures"]), (2, 1))

    def test_prose_that_merely_mentions_the_state_is_not_an_arrival(self):
        """A note or a hub activity row quoting "-> needs-owner" is somebody
        writing about an escalation, not making one."""
        self.estate("task", "add", "work", "--kind", "rollout")
        self.estate("note", "t-1", "recap: claimed -> needs-owner",
                    "--actor", "hub")
        self.estate("event", "--actor", "hub", "--kind", "activity",
                    "--summary", "recapped items still pending-review")
        blocks, _ = self.blocks()
        self.assertEqual(blocks[A.NEEDS_OWNER]["periods"][-1]["arrivals"], 0)
        self.assertEqual(blocks[A.PENDING_REVIEW]["periods"][-1]["arrivals"], 0)

    # ── the floor, and what may be compared ──────────────────────────────────
    def test_periods_before_the_floor_are_unobservable_not_quiet(self):
        """Three zeroes and three different reasons: nothing could have been
        recorded, the mechanism started mid-period, and the day is not over."""
        self.estate("task", "add", "ask the owner", "--kind", "followup",
                    "--actor", "hub")
        self.backdate(1)
        blocks, _ = self.blocks("--period", "day", "--periods", "3")
        verdicts = [row["verdict"] for row in blocks[A.FOLLOWUP]["periods"]]
        self.assertEqual(verdicts, [A.UNOBSERVABLE, A.STRADDLES_FLOOR,
                                    A.IN_PROGRESS])

    def test_a_class_with_no_rows_is_unobservable_and_has_no_floor(self):
        self.estate("task", "add", "ask the owner", "--kind", "followup")
        blocks, _ = self.blocks()
        self.assertIsNone(blocks[A.PENDING_REVIEW]["floor"])
        self.assertTrue(all(row["verdict"] == A.UNOBSERVABLE
                            for row in blocks[A.PENDING_REVIEW]["periods"]))

    def test_two_whole_days_after_the_floor_are_a_comparable_pair(self):
        self.estate("task", "add", "old one", "--kind", "followup",
                    "--actor", "mechanic")
        self.backdate(3)
        self.estate("task", "add", "newer one", "--kind", "followup",
                    "--actor", "mechanic")
        self.estate("task", "add", "and another", "--kind", "followup",
                    "--actor", "mechanic")
        self.backdate(1)
        blocks, _ = self.blocks("--period", "day", "--periods", "5")
        pair = blocks[A.FOLLOWUP]["comparable_pair"]
        self.assertIsNotNone(pair)
        self.assertTrue(pair["trend_is_evidence"])
        self.assertEqual((pair["current"], pair["previous"]), (2, 0))

    def test_the_period_holding_the_floor_is_never_a_comparison(self):
        """The cutover case: the day a mechanism starts holds part of a real
        period plus whatever a migration backfilled, and nothing here can tell
        those apart without reading prose."""
        self.estate("task", "add", "first ever", "--kind", "followup")
        self.backdate(1)
        blocks, _ = self.blocks("--period", "day", "--periods", "3")
        periods = blocks[A.FOLLOWUP]["periods"]
        self.assertEqual(periods[-2]["verdict"], A.STRADDLES_FLOOR)
        self.assertIsNone(blocks[A.FOLLOWUP]["comparable_pair"])

    def test_the_in_progress_period_is_reported_and_refused_as_evidence(self):
        self.estate("task", "add", "yesterday's", "--kind", "followup")
        self.backdate(2)
        self.estate("task", "add", "today's", "--kind", "followup")
        blocks, _ = self.blocks("--period", "day", "--periods", "3")
        latest = blocks[A.FOLLOWUP]["latest_vs_previous"]
        self.assertEqual(latest["current"], 1)
        self.assertEqual(latest["current_verdict"], A.IN_PROGRESS)
        self.assertFalse(latest["trend_is_evidence"])

    def test_the_producer_table_names_the_pair_it_used(self):
        self.estate("task", "add", "one", "--kind", "followup", "--actor", "hub")
        _, payload = self.blocks("--period", "day", "--periods", "3")
        producers = payload["producers"]
        self.assertIn("current_period", producers)
        self.assertIn("previous_period", producers)
        self.assertIn("trend_is_evidence", producers)

    # ── the guardrails ───────────────────────────────────────────────────────
    def test_one_period_is_refused(self):
        self.estate("task", "add", "one", "--kind", "followup")
        out = self.estate("arrivals", "--periods", "1", check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("at least 2", out.stderr + out.stdout)

    def test_an_unknown_class_is_refused(self):
        self.estate("task", "add", "one", "--kind", "followup")
        out = self.estate("arrivals", "--class", "proposals", check=False)
        self.assertNotEqual(out.returncode, 0)

    def test_the_class_filter_narrows_the_blocks(self):
        self.estate("task", "add", "one", "--kind", "followup")
        blocks, _ = self.blocks("--class", "followup")
        self.assertEqual(list(blocks), [A.FOLLOWUP])

    def test_it_writes_nothing(self):
        """A read-only query. If this ever fails, something grew a side effect."""
        self.estate("task", "add", "one", "--kind", "followup")
        db = os.path.join(self.state, "estate.db")
        before = [dict(r) for r in self.dump(db)]
        self.estate("arrivals", "--periods", "3")
        self.estate("arrivals", "--periods", "3", "--json")
        self.assertEqual([dict(r) for r in self.dump(db)], before)

    @staticmethod
    def dump(db):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT * FROM events ORDER BY seq"))
        rows += list(conn.execute("SELECT * FROM tasks ORDER BY seq"))
        conn.close()
        return rows

    def test_the_text_rendering_states_the_verdict_on_every_period(self):
        self.estate("task", "add", "one", "--kind", "followup")
        self.backdate(2)
        out = self.estate("arrivals", "--period", "day", "--periods", "3",
                          "--class", "followup")
        self.assertIn("trend_is_evidence=", out.stdout)
        self.assertIn(A.IN_PROGRESS, out.stdout)
        self.assertIn("first ever recorded:", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

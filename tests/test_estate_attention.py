#!/usr/bin/env python3
"""Tests for P-07: the five due states and the persistent attention view.

Two layers, deliberately. `lib/estate_attention.py` is pure, so the boundary
behaviour S31 asks about ("boundary changes are reflected") is tested by moving
`now` rather than by waiting for a clock. The CLI layer is then tested against a
real throwaway store through the real `bin/estate`, because every consumer —
the briefer, the shim, the migration — reads it that way and a mocked store
would prove the wrong thing.
"""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(REPO_ROOT, "bin")
ESTATE = os.path.join(BIN_DIR, "estate")
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import estate_attention as A  # noqa: E402

UTC = dt.timezone.utc
# Fixed offsets, never the machine's zone: the day-boundary rules must hold on
# a laptop in any timezone and in CI.
EDT = dt.timezone(dt.timedelta(hours=-4))
TOKYO = dt.timezone(dt.timedelta(hours=9))
NOON_UTC = dt.datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class ClassifyCase(unittest.TestCase):
    def state(self, due, now=NOON_UTC, notice=3.0, tz=UTC):
        return A.classify(due, now, notice, tz)

    def test_no_date_is_undated_not_safe(self):
        for empty in (None, "", "   "):
            self.assertEqual(self.state(empty), A.UNDATED)

    def test_past_is_overdue(self):
        self.assertEqual(self.state("2026-07-29T23:59:59+00:00"), A.OVERDUE)

    def test_the_exact_instant_is_already_overdue(self):
        # A deadline of "now" has been reached. `<=` not `<`: the alternative
        # classifies a passed deadline as due_today for one tick.
        self.assertEqual(self.state(NOON_UTC.isoformat()), A.OVERDUE)

    def test_later_today_is_due_today(self):
        self.assertEqual(self.state("2026-07-30T23:59:59+00:00"), A.DUE_TODAY)

    def test_tomorrow_is_due_soon_not_due_today(self):
        self.assertEqual(self.state("2026-07-31T09:00:00+00:00"), A.DUE_SOON)

    def test_beyond_the_notice_interval_is_later(self):
        self.assertEqual(self.state("2026-08-30T09:00:00+00:00"), A.LATER)

    def test_the_notice_boundary_is_inclusive(self):
        edge = (NOON_UTC + dt.timedelta(days=3)).isoformat()
        self.assertEqual(self.state(edge), A.DUE_SOON)
        past_edge = (NOON_UTC + dt.timedelta(days=3, seconds=1)).isoformat()
        self.assertEqual(self.state(past_edge), A.LATER)

    def test_the_notice_interval_is_the_caller_policy(self):
        far = "2026-08-05T09:00:00+00:00"
        self.assertEqual(self.state(far, notice=3.0), A.LATER)
        self.assertEqual(self.state(far, notice=14.0), A.DUE_SOON)

    def test_undated_and_later_are_never_overdue(self):
        """S31's stated negative, as one assertion so it cannot rot."""
        for due in (None, "", "2027-01-01T00:00:00+00:00"):
            self.assertNotEqual(self.state(due), A.OVERDUE)

    def test_a_later_item_is_not_in_the_notice_set(self):
        """S24's negative: an outside-interval item is not 'currently due'."""
        self.assertNotIn(self.state("2026-09-01T00:00:00+00:00"),
                         A.NOTICE_STATES)

    def test_an_unreadable_date_is_its_own_answer(self):
        # Not undated (somebody DID set one) and not overdue/later (both would
        # invent a fact about a date nobody can read).
        self.assertEqual(self.state("soon"), A.UNREADABLE)
        self.assertNotIn(A.UNREADABLE, A.DUE_STATES)

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(self.state("2026-07-30T23:00:00"), A.DUE_TODAY)

    def test_boundaries_move_with_the_clock_not_with_the_row(self):
        """S31: 'boundary changes are reflected'. One unchanged due_at, read at
        four instants, walks the whole ladder."""
        due = "2026-08-02T12:00:00+00:00"
        walk = [self.state(due, now=dt.datetime(2026, 7, 20, 12, tzinfo=UTC)),
                self.state(due, now=dt.datetime(2026, 7, 31, 12, tzinfo=UTC)),
                self.state(due, now=dt.datetime(2026, 8, 2, 6, tzinfo=UTC)),
                self.state(due, now=dt.datetime(2026, 8, 3, 12, tzinfo=UTC))]
        self.assertEqual(walk, [A.LATER, A.DUE_SOON, A.DUE_TODAY, A.OVERDUE])


class LocalDayCase(unittest.TestCase):
    """`due_today` is a claim about the owner's calendar, so it is measured in their
    zone. The same instant is 'today' in one zone and 'tomorrow' in another,
    and the store must answer for the reader's."""

    DUE = "2026-07-31T02:00:00+00:00"       # 22:00 EDT on the 30th
    NOW = dt.datetime(2026, 7, 30, 20, 0, tzinfo=UTC)   # 16:00 EDT on the 30th

    def test_same_local_day_reads_as_due_today(self):
        self.assertEqual(A.classify(self.DUE, self.NOW, 3.0, EDT), A.DUE_TODAY)

    def test_the_utc_reading_of_the_same_instant_is_tomorrow(self):
        self.assertEqual(A.classify(self.DUE, self.NOW, 3.0, UTC), A.DUE_SOON)

    def test_east_of_utc_reads_the_same_pair_its_own_way(self):
        # The mirror of the case above, and the reason this is a zone lookup
        # rather than an offset fudge. In Tokyo BOTH instants have already
        # rolled over to the 31st, so the same due date that is "tomorrow" in
        # UTC is "today" there.
        self.assertEqual(A.classify(self.DUE, self.NOW, 3.0, TOKYO), A.DUE_TODAY)
        # And a deadline that crosses Tokyo's midnight is due_soon there while
        # still reading as due_today in UTC.
        crosses = "2026-07-30T20:00:00+00:00"          # 05:00 JST on the 31st
        noon = dt.datetime(2026, 7, 30, 10, 0, tzinfo=UTC)  # 19:00 JST, 30th
        self.assertEqual(A.classify(crosses, noon, 3.0, TOKYO), A.DUE_SOON)
        self.assertEqual(A.classify(crosses, noon, 3.0, UTC), A.DUE_TODAY)

    def test_the_env_override_selects_the_zone(self):
        previous = os.environ.get("ESTATE_TZ")
        try:
            os.environ["ESTATE_TZ"] = "America/New_York"
            self.assertEqual(A.local_zone().utcoffset(self.NOW),
                             dt.timedelta(hours=-4))
            os.environ["ESTATE_TZ"] = "Not/AZone"
            # An unloadable zone falls back to local rather than failing a
            # read-only query.
            self.assertIsNotNone(A.local_zone())
        finally:
            os.environ.pop("ESTATE_TZ", None)
            if previous is not None:
                os.environ["ESTATE_TZ"] = previous


class AttentionSetCase(unittest.TestCase):
    def test_an_open_followup_is_attention(self):
        self.assertTrue(A.is_attention("followup", "open"))

    def test_a_resolved_followup_is_history_not_attention(self):
        self.assertFalse(A.is_attention("followup", "done"))
        self.assertFalse(A.is_attention("followup", "dropped"))

    def test_needs_owner_is_attention_whatever_its_kind(self):
        self.assertTrue(A.is_attention("rollout", "needs-owner"))

    def test_blocked_work_is_not_attention(self):
        # Blocked work waits on other WORK. A dependency finishing unblocks it,
        # not a person reading it.
        self.assertFalse(A.is_attention("rollout", "blocked"))

    def test_live_work_is_not_attention(self):
        for status in ("open", "ready", "claimed"):
            self.assertFalse(A.is_attention("rollout", status))


class TierCase(unittest.TestCase):
    """The three lifecycle tiers (t-721) — one set, three kinds of waiting."""

    def test_a_staged_needs_owner_row_is_a_decision_and_a_bare_one_is_not(self):
        self.assertEqual(A.tier("proposal", "needs-owner", "pending-review"),
                         A.TIER_DECISION)
        self.assertEqual(A.tier("proposal", "needs-owner", None),
                         A.TIER_ESCALATION)
        self.assertEqual(A.tier("inbox-message", "needs-owner", ""),
                         A.TIER_ESCALATION)

    def test_an_open_followup_is_its_own_tier(self):
        self.assertEqual(A.tier("followup", "open", None), A.TIER_FOLLOWUP)

    def test_status_wins_over_kind(self):
        # An escalated follow-up is both. What the owner has to DO with it is the
        # escalation, so that is the tier it reads in.
        self.assertEqual(A.tier("followup", "needs-owner", None),
                         A.TIER_ESCALATION)

    def test_a_row_outside_the_set_has_no_tier_at_all(self):
        self.assertIsNone(A.tier("rollout", "ready", None))
        self.assertIsNone(A.tier("followup", "done", None))
        self.assertIsNone(A.tier("proposal", "blocked", "pending-review"))

    def test_idle_is_never_negative_and_unreadable_is_none(self):
        self.assertEqual(
            A.idle_days((NOON_UTC - dt.timedelta(days=2, hours=12)).isoformat(),
                        NOON_UTC), 2.5)
        # A stamp in the future is a clock disagreement, not negative neglect.
        self.assertEqual(
            A.idle_days((NOON_UTC + dt.timedelta(days=1)).isoformat(),
                        NOON_UTC), 0.0)
        self.assertIsNone(A.idle_days("not a timestamp", NOON_UTC))
        self.assertIsNone(A.idle_days(None, NOON_UTC))

    def test_the_median_ignores_what_nobody_could_read(self):
        self.assertEqual(A.median([1, 5, 3]), 3.0)
        self.assertEqual(A.median([1, 3, None, 5]), 3.0)
        self.assertEqual(A.median([2, 4]), 3.0)
        self.assertIsNone(A.median([]))
        self.assertIsNone(A.median([None]))


class ParseStatesCase(unittest.TestCase):
    def test_comma_separated_and_repeatable(self):
        self.assertEqual(A.parse_states(["overdue,due_today", "later"]),
                         ["overdue", "due_today", "later"])

    def test_duplicates_collapse(self):
        self.assertEqual(A.parse_states("overdue,overdue"), ["overdue"])

    def test_a_typo_is_an_error_not_an_empty_result(self):
        # The failure mode this prevents: `--state overdu` silently matching
        # nothing and reading exactly like "nothing is overdue".
        with self.assertRaises(ValueError) as caught:
            A.parse_states("overdu")
        self.assertIn("overdue", str(caught.exception))


class CliCase(unittest.TestCase):
    """`estate attention` and `estate due --json`, end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def estate(self, *args, check=True):
        out = subprocess.run([sys.executable, ESTATE, *args],
                             capture_output=True, text=True,
                             env=dict(os.environ, ESTATE_STATE_DIR=self.state))
        if check:
            self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def stamp(self, days):
        return (dt.datetime.now(UTC) + dt.timedelta(days=days)).isoformat()

    def seed(self):
        """One row per state, plus rows that must NOT be in the attention set."""
        self.estate("task", "add", "already late", "--kind", "followup",
                    "--due", self.stamp(-2))                       # t-1 overdue
        # 23:00 local today: due_today only if the day boundary is local.
        today = dt.datetime.now().astimezone().replace(
            hour=23, minute=0, second=0, microsecond=0)
        if today <= dt.datetime.now().astimezone():
            today += dt.timedelta(minutes=59)
        self.estate("task", "add", "tonight", "--kind", "followup",
                    "--due", today.isoformat())                    # t-2 due_today
        self.estate("task", "add", "in two days", "--kind", "followup",
                    "--due", self.stamp(2))                        # t-3 due_soon
        self.estate("task", "add", "next month", "--kind", "followup",
                    "--due", self.stamp(30))                       # t-4 later
        self.estate("task", "add", "no date", "--kind", "followup")  # t-5 undated
        self.estate("task", "add", "live work", "--kind", "rollout")  # t-6 not
        self.estate("task", "add", "finished", "--kind", "followup")  # t-7 not
        self.estate("drop", "t-7", "no longer needed")

    def rows(self, *args):
        return json.loads(self.estate("attention", "--json", *args).stdout)

    def test_every_state_is_present_and_ordered_most_urgent_first(self):
        self.seed()
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows],
                         ["t-1", "t-2", "t-3", "t-4", "t-5"])
        self.assertEqual([r["due_state"] for r in rows],
                         ["overdue", "due_today", "due_soon", "later", "undated"])

    def test_live_work_and_finished_items_are_absent(self):
        self.seed()
        ids = {r["id"] for r in self.rows()}
        self.assertNotIn("t-6", ids)   # a rollout in progress is not attention
        self.assertNotIn("t-7", ids)   # dropped is history

    def test_an_undated_item_is_present_which_is_the_whole_point(self):
        """`estate due` selects on due_at, so an undated item is absent from it
        by construction — and that is S24's unresolved attention item."""
        self.estate("task", "add", "waiting on the owner", "--kind", "followup")
        self.assertEqual(json.loads(self.estate("due", "--json").stdout), [])
        self.assertEqual([r["id"] for r in self.rows()], ["t-1"])

    def test_state_filter_selects_the_notice_set(self):
        self.seed()
        rows = self.rows("--state", "overdue,due_today,due_soon")
        self.assertEqual([r["id"] for r in rows], ["t-1", "t-2", "t-3"])
        self.assertNotIn("t-4", {r["id"] for r in rows})

    def test_notice_interval_moves_the_due_soon_later_boundary(self):
        self.seed()
        self.assertEqual(
            [r["id"] for r in self.rows("--notice", "60", "--state", "due_soon")],
            ["t-3", "t-4"])

    def test_counts_cover_every_state(self):
        self.seed()
        counts = json.loads(self.estate("attention", "--counts").stdout)
        self.assertEqual(counts["total"], 5)
        for state in A.ALL_STATES:
            self.assertIn(state, counts)
        self.assertEqual(counts["unreadable"], 0)

    def test_kind_filter(self):
        self.estate("task", "add", "a followup", "--kind", "followup")
        self.assertEqual([r["id"] for r in self.rows("--kind", "proposal")], [])
        self.assertEqual([r["id"] for r in self.rows("--kind", "followup")],
                         ["t-1"])

    def test_an_unreadable_date_sorts_ahead_of_overdue(self):
        self.estate("task", "add", "already late", "--kind", "followup",
                    "--due", self.stamp(-2))
        self.estate("task", "add", "corrupt", "--kind", "followup",
                    "--due", self.stamp(1))
        con = sqlite3.connect(os.path.join(self.state, "estate.db"))
        with con:
            con.execute("UPDATE tasks SET due_at='soon' WHERE id='t-2'")
        con.close()
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows], ["t-2", "t-1"])
        self.assertEqual(rows[0]["due_state"], "unreadable")
        self.assertIsNone(rows[0]["days_left"])

    def test_a_bad_state_is_rejected_with_the_legal_ones_named(self):
        out = self.estate("attention", "--state", "nonsense", check=False)
        self.assertEqual(out.returncode, 2)
        self.assertIn("due_today", out.stderr)

    def test_a_negative_notice_is_rejected(self):
        self.assertEqual(
            self.estate("attention", "--notice", "-1", check=False).returncode, 2)

    def test_due_json_gained_due_state_without_losing_anything(self):
        """Additive: a P-31 consumer reading days_left/overdue is untouched."""
        self.estate("task", "add", "late", "--kind", "followup",
                    "--due", self.stamp(-1))
        row = json.loads(self.estate("due", "--json").stdout)[0]
        self.assertEqual(row["due_state"], "overdue")
        self.assertTrue(row["overdue"])
        self.assertLess(row["days_left"], 0)

    def test_blocked_by_is_carried_through(self):
        self.estate("task", "add", "blocker", "--kind", "followup")
        self.estate("task", "add", "blocked one", "--kind", "followup")
        self.estate("dep", "add", "t-1", "t-2", "--kind", "blocks")
        rows = {r["id"]: r for r in self.rows()}
        self.assertEqual(rows["t-2"]["blocked_by"], ["t-1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

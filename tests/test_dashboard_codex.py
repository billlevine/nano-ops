#!/usr/bin/env python3
"""Codex's weekly budget on the dashboard the contract."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import re
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(_HERE.parent / "lib"))
import codex_usage  # noqa: E402

_loader = SourceFileLoader("_dashboard_codex", str(_HERE / "dashboard"))
_spec = importlib.util.spec_from_loader("_dashboard_codex", _loader)
dash = importlib.util.module_from_spec(_spec)
_loader.exec_module(dash)

UTC = timezone.utc
# A Thursday-anchored week, as this account's Codex limit really is.
RESET = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)          # Thu 20:00 UTC
WEEK_START = RESET - timedelta(days=7)                     # Thu 17th 20:00


def token_row(ts: datetime, total: int, pct=None, resets=RESET,
              window=10080, slot="primary") -> dict:
    usage = {"input_tokens": total, "cached_input_tokens": 0,
             "output_tokens": 0, "reasoning_output_tokens": 0,
             "total_tokens": total}
    limits = None
    if pct is not None:
        limits = {"limit_id": "codex", "primary": None, "secondary": None,
                  "plan_type": "prolite"}
        limits[slot] = {"used_percent": pct, "window_minutes": window,
                        "resets_at": int(resets.timestamp())}
    return {"timestamp": ts.isoformat().replace("+00:00", "Z"),
            "type": "event_msg",
            "payload": {"type": "token_count",
                        "info": {"total_token_usage": usage},
                        "rate_limits": limits}}


class Sessions:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "sessions"
        self.base.mkdir()

    def write(self, day: datetime, name: str, rows: list[dict]) -> Path:
        directory = self.base / day.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{name}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.s = Sessions()
        self.addCleanup(self.s.tmp.cleanup)

    def test_week_is_found_by_window_length_not_slot(self):
        limits = {"primary": {"used_percent": 3, "window_minutes": 300,
                              "resets_at": 1},
                  "secondary": {"used_percent": 40, "window_minutes": 10080,
                                "resets_at": 2}}
        self.assertEqual(codex_usage.weekly_window(limits)["used_percent"], 40)
        self.assertIsNone(codex_usage.weekly_window(
            {"primary": {"used_percent": 3, "window_minutes": 300}}))

    def test_newest_row_wins_across_rollouts(self):
        t = datetime(2026, 9, 21, 10, tzinfo=UTC)
        self.s.write(t, "a", [token_row(t, 10, pct=12)])
        self.s.write(t, "b", [token_row(t + timedelta(hours=1), 10, pct=15)])
        got = codex_usage.latest_rate_limits(self.s.base, t + timedelta(hours=2))
        self.assertEqual(got["rate_limits"]["primary"]["used_percent"], 15)

    def test_week_tokens_subtract_what_a_session_spent_before_the_week(self):
        before = WEEK_START - timedelta(hours=2)
        self.s.write(before, "straddle", [
            token_row(before, 1000),
            token_row(WEEK_START + timedelta(hours=1), 1500)])
        inside = WEEK_START + timedelta(days=1)
        self.s.write(inside, "inside", [token_row(inside, 200),
                                        token_row(inside, 700)])
        got = codex_usage.window_tokens(self.s.base, WEEK_START,
                                        WEEK_START + timedelta(days=3))
        self.assertEqual(got["total_tokens"], 500 + 700)
        self.assertEqual(got["sessions"], 2)

    def test_signal_reads_the_weekly_percent(self):
        now = WEEK_START + timedelta(days=2)
        self.s.write(now, "x", [token_row(now - timedelta(minutes=5), 4000, pct=18)])
        sig = codex_usage.budget_signal(self.s.base, now)
        self.assertEqual(sig["source"], "rollout")
        self.assertEqual(sig["weekly_pct"], 18)
        self.assertEqual(sig["weekly_resets_at"], RESET.isoformat())
        self.assertEqual(sig["tokens"]["total_tokens"], 4000)
        self.assertEqual(sig["tokens_since"], WEEK_START.isoformat())


class CouldNotReadTest(unittest.TestCase):
    """Every unreadable case names what it could not read and has no percent."""

    def setUp(self):
        self.s = Sessions()
        self.addCleanup(self.s.tmp.cleanup)
        self.now = WEEK_START + timedelta(days=2)

    def assert_not_a_zero(self, sig, source):
        self.assertEqual(sig["source"], source)
        self.assertIsNone(sig["weekly_pct"])
        html = dash.render_codex_cap(sig, now=self.now)
        self.assertIn("weekly limit: could not read", html)
        self.assertNotIn("0%", html)
        self.assertNotIn("pace", html)
        return html

    def test_no_sessions_directory(self):
        sig = codex_usage.budget_signal(self.s.base / "missing", self.now)
        html = self.assert_not_a_zero(sig, "absent")
        self.assertIn("tokens: could not read", html)

    def test_rollouts_without_rate_limits(self):
        self.s.write(self.now, "x", [token_row(self.now - timedelta(hours=1), 900)])
        html = self.assert_not_a_zero(
            codex_usage.budget_signal(self.s.base, self.now), "absent")
        self.assertIn("900", html)  # the tokens it CAN show, it shows

    def test_no_weekly_window(self):
        self.s.write(self.now, "x", [token_row(self.now - timedelta(hours=1), 900,
                                               pct=4, window=300)])
        self.assert_not_a_zero(
            codex_usage.budget_signal(self.s.base, self.now), "no_weekly_window")

    def test_reading_from_a_week_that_has_since_reset(self):
        """18% of LAST week is not this week's percent, and neither is 0."""
        later = RESET + timedelta(hours=3)
        self.s.write(self.now, "x", [token_row(self.now, 900, pct=18)])
        sig = codex_usage.budget_signal(self.s.base, later)
        self.assertEqual(sig["source"], "window_ended")
        self.assertIsNone(sig["weekly_pct"])
        self.assertIn("last 7d", dash.render_codex_cap(sig, now=later))

    def test_reader_that_raises_does_not_take_the_page_down(self):
        old = dash.codex_usage.budget_signal
        dash.codex_usage.budget_signal = lambda *a: 1 / 0
        try:
            sig = dash.build_codex_signal(self.now)
        finally:
            dash.codex_usage.budget_signal = old
        self.assert_not_a_zero(sig, "error")

    def test_no_signal_at_all(self):
        self.assertIn("not read", dash.render_codex_cap(None))


class CodexPaceTest(unittest.TestCase):
    """The same 10% weekend reserve as Claude, on the calendar weekend of a
    Thursday-anchored week. UTC keeps the arithmetic legible: the weekend
    starts Sat 00:00, 28h into a week that opened Thu 20:00."""

    def pace(self, pct, now):
        start = dash.calendar_weekend_start(RESET.isoformat(), tz=UTC)
        self.assertEqual(start, 28.0)
        return dash.cap_pace(pct, RESET.isoformat(), hours=168,
                             reserve_pct=dash.WEEKEND_RESERVE_PCT, now=now,
                             weekend_start_hour=start)

    def test_under_and_over_the_line_on_a_weekday(self):
        fri = WEEK_START + timedelta(hours=24)       # 24 weekday hours in
        self.assertEqual(self.pace(10, fri), (18, "good"))
        self.assertEqual(self.pace(18, fri), (18, "warn"))
        self.assertEqual(self.pace(30, fri), (18, "crit"))

    def test_weekend_spends_only_the_reserve(self):
        sat_noon = WEEK_START + timedelta(hours=28 + 12)
        sun_end = WEEK_START + timedelta(hours=28 + 48)
        # 28 weekday hours = 21%, plus a quarter / all of the 10% reserve.
        self.assertEqual(self.pace(24, sat_noon)[0], 24)
        self.assertEqual(self.pace(31, sun_end)[0], 31)
        # Monday resumes the weekday rate; the week still ends at 100.
        self.assertEqual(self.pace(0, RESET)[0], 100)

    def test_weekend_at_the_end_is_the_claude_default(self):
        """Claude resets Monday 09:00 UTC, so its default (weekend last) and
        the calendar placement agree — its pacing is unchanged."""
        claude_reset = "2026-08-03T09:00:00+00:00"
        self.assertEqual(dash.calendar_weekend_start(claude_reset, tz=UTC), 111.0)
        now = datetime(2026, 7, 28, 9, tzinfo=UTC)
        self.assertEqual(dash.cap_pace(14, claude_reset, hours=168,
                                       reserve_pct=10, now=now), (18, "good"))

    def test_window_that_opens_mid_weekend_wraps(self):
        reset = datetime(2026, 9, 27, 12, tzinfo=UTC)   # Sun noon
        start = dash.calendar_weekend_start(reset.isoformat(), tz=UTC)
        self.assertEqual(start, -36.0)   # [0,12h) now, and again from 132h
        mon_noon = reset - timedelta(days=6)   # 12 weekend + 12 weekday hours
        self.assertEqual(dash.cap_pace(0, reset.isoformat(), hours=168,
                                       reserve_pct=10, now=mon_noon,
                                       weekend_start_hour=start)[0], 12)
        sat_noon = reset - timedelta(hours=24)  # every weekday hour spent
        self.assertEqual(dash.cap_pace(0, reset.isoformat(), hours=168,
                                       reserve_pct=10, now=sat_noon,
                                       weekend_start_hour=start)[0], 95)

    def test_rendered_line_colours_against_the_pace(self):
        fri = WEEK_START + timedelta(hours=24)
        sig = {"source": "rollout", "weekly_pct": 30,
               "weekly_resets_at": RESET.isoformat(),
               "observed_at": fri.isoformat(), "plan_type": "prolite",
               "tokens": {k: 1 for k in codex_usage.TOKEN_KEYS} | {"sessions": 1},
               "tokens_since": WEEK_START.isoformat()}
        html = dash.render_codex_cap(sig, now=fri)
        self.assertIn('class="pace crit">weekly 30% (pace', html)
        self.assertIn("this week", html)


class PlacementTest(unittest.TestCase):
    def test_codex_line_sits_in_the_usage_strip_either_way(self):
        sig = {"source": "absent", "weekly_pct": None, "tokens": None,
               "tokens_error": "x", "why": "y"}
        for usage in ({"available": False, "codex": sig},
                      {"available": True, "window_hours": 5, "input": 1,
                       "output": 1, "cache_creation": 0, "cache_read": 0,
                       "messages": 1, "sessions": 1, "total": 2, "note": "n",
                       "codex": sig}):
            self.assertIn('<span class="ul">Codex</span>',
                          dash.render_usage(usage))

    def test_claude_and_codex_rows_share_one_shape(self):
        """the operator, 2026-09-23: the two budget lines line up — one grid, each row
        label then weekly cell then detail. Claude's session cap is demoted to
        a detail part: still shown, its pace only in the tooltip."""
        fri = WEEK_START + timedelta(hours=24)
        cap = {"source": "api", "session_pct": 29.0, "weekly_pct": 32.0,
               "session_resets_at": (fri + timedelta(hours=2)).isoformat(),
               "weekly_resets_at": "2026-08-03T09:00:00+00:00"}
        sig = {"source": "rollout", "weekly_pct": 30,
               "weekly_resets_at": RESET.isoformat(),
               "observed_at": fri.isoformat(), "plan_type": "prolite",
               "tokens": {k: 1 for k in codex_usage.TOKEN_KEYS} | {"sessions": 1},
               "tokens_since": WEEK_START.isoformat()}
        html = dash.render_usage({"available": False, "cap": cap, "codex": sig})
        self.assertEqual(html.count('<div class="budget">'), 1)
        rows = re.findall(r'<div class="brow"><span class="ul">(\w+)</span>'
                          r'<span class="bw"', html)
        self.assertEqual(rows, ["Claude", "Codex"])
        self.assertEqual(html.count('class="meter '), 2)
        self.assertIn(">session 29%</span>", html)
        self.assertNotIn("session 29% (pace", html)


if __name__ == "__main__":
    unittest.main()

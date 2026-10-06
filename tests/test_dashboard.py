#!/usr/bin/env python3
"""Tests for bin/dashboard — the pure-reader half of the estate renderer.
Run: python3 tests/test_dashboard.py

bin/dashboard is a PURE READER, so everything worth testing here is a function
of on-disk state and nothing else. Each test therefore points the reader at a
fresh tempdir — via $HOME for the transcript scan, and by rebinding the module's
ROOT/STATE for the repo-relative reads — and never touches real state/.

Two properties get the most attention, because they are the two that a wrong
answer would silently corrupt:

  * the usage aggregate — window filtering, cost arithmetic, and per-loop
    attribution, all of which feed the header the operator reads as a budget
  * identity parameterization — that persona, group, estate and every session
    title resolve from loops.toml rather than from anything baked into the code
    (the identity-is-configuration invariant in CLAUDE.md)

Nothing here names an operator, a persona, a channel, a host or an absolute
path; the fixtures invent neutral ones, exactly as a stranger's install would.
"""
from __future__ import annotations

import contextlib
import gzip
import http.client
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent / "bin"
_loader = SourceFileLoader("_dashboard", str(_HERE / "dashboard"))
_spec = importlib.util.spec_from_loader("_dashboard", _loader)
dash = importlib.util.module_from_spec(_spec)
_loader.exec_module(dash)

# the drill-down moved OUT of the periodic snapshot and INTO
# `GET /estate-detail`, so the P-16 guarantee is tested where it now lives.
_srv_loader = SourceFileLoader("_dashboard_server", str(_HERE / "dashboard-server"))
_srv_spec = importlib.util.spec_from_loader("_dashboard_server", _srv_loader)
srv = importlib.util.module_from_spec(_srv_spec)
_srv_loader.exec_module(srv)

NOW = datetime.now(timezone.utc)


class TempRootTest(unittest.TestCase):
    """Rebinds the module's ROOT/STATE at a fresh tempdir for the duration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "state").mkdir(parents=True)
        self._saved = (dash.ROOT, dash.STATE)
        dash.ROOT, dash.STATE = self.root, self.root / "state"

    def tearDown(self):
        dash.ROOT, dash.STATE = self._saved
        self.tmp.cleanup()


# ── small pure helpers ───────────────────────────────────────────────────────

class IntervalAndFreshnessTest(unittest.TestCase):
    def test_parses_every_documented_unit(self):
        self.assertEqual(dash.parse_interval_seconds("45s"), 45)
        self.assertEqual(dash.parse_interval_seconds("20m"), 1200)
        self.assertEqual(dash.parse_interval_seconds(" 2h "), 7200)
        self.assertEqual(dash.parse_interval_seconds("1d"), 86400)

    def test_unparseable_interval_is_none_not_an_error(self):
        # "on-demand" is a documented loops.toml value; it must degrade, since a
        # renderer that raised here would take the whole dashboard down.
        for bad in ("on-demand", "", None, "20", "m", "1w"):
            self.assertIsNone(dash.parse_interval_seconds(bad))

    def test_freshness_buckets_at_one_and_two_intervals(self):
        self.assertEqual(dash.freshness(59, 60), "fresh")
        self.assertEqual(dash.freshness(60, 60), "aging")
        self.assertEqual(dash.freshness(119, 60), "aging")
        self.assertEqual(dash.freshness(120, 60), "stale")

    def test_freshness_without_a_signal_is_na(self):
        # A loop with no heartbeat yet, or no parseable interval, is unknown —
        # not "stale". Reporting unknown as a fault would page on every new loop.
        self.assertEqual(dash.freshness(None, 60), "na")
        self.assertEqual(dash.freshness(5, None), "na")


class HumanFormatTest(unittest.TestCase):
    def test_age_switches_unit_at_the_documented_thresholds(self):
        self.assertEqual(dash.human_age(None), "—")
        self.assertEqual(dash.human_age(89), "89s")
        self.assertEqual(dash.human_age(90), "2m")
        self.assertEqual(dash.human_age(5400), "2h")
        self.assertEqual(dash.human_age(172800), "2d")

    def test_tokens_keep_one_decimal_until_the_scale_makes_it_noise(self):
        self.assertEqual(dash.human_tokens(None), "0")
        self.assertEqual(dash.human_tokens(999), "999")
        self.assertEqual(dash.human_tokens(1500), "1.5k")
        self.assertEqual(dash.human_tokens(100_000), "100k")
        self.assertEqual(dash.human_tokens(1_500_000), "1.5M")
        self.assertEqual(dash.human_tokens(100_000_000), "100M")
        self.assertEqual(dash.human_tokens(2_500_000_000), "2.50B")


class ModelCostTest(unittest.TestCase):
    def test_dated_snapshot_suffix_resolves_to_the_pricing_key(self):
        self.assertEqual(dash._base_model_id("claude-haiku-4-5-20251001"),
                         "claude-haiku-4-5")
        # A version number's own hyphens must survive — only a trailing
        # 8-digit date is a snapshot suffix.
        self.assertEqual(dash._base_model_id("claude-opus-4-8"), "claude-opus-4-8")
        self.assertIsNone(dash._base_model_id(None))

    def test_split_cache_writes_priced_at_their_own_multipliers(self):
        cost = dash._line_cost_usd("claude-opus-4-8", {
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 200, "cache_read_input_tokens": 300,
            "cache_creation": {"ephemeral_1h_input_tokens": 80,
                               "ephemeral_5m_input_tokens": 120}})
        # in 100@5 + out 50@25 + 1h 80@5x2 + 5m 120@5x1.25 + read 300@5x0.1
        self.assertAlmostEqual(cost, 0.003450, places=9)

    def test_unsplit_cache_write_is_billed_not_dropped(self):
        # Older transcripts carry only the flat total. Treating the remainder as
        # 5m (the default TTL) keeps it in the estimate; dropping it would make
        # every historical window read low.
        cost = dash._line_cost_usd("claude-opus-4-8",
                                   {"cache_creation_input_tokens": 200})
        self.assertAlmostEqual(cost, 200 * 5.00 * 1.25 / 1_000_000, places=9)

    def test_dated_snapshot_is_priced_like_its_base_model(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        self.assertAlmostEqual(dash._line_cost_usd("claude-haiku-4-5-20251001", usage),
                               dash._line_cost_usd("claude-haiku-4-5", usage), places=9)

    def test_unknown_model_contributes_zero_rather_than_a_wrong_number(self):
        self.assertEqual(dash._line_cost_usd("some-other-vendor-model",
                                             {"input_tokens": 10_000_000}), 0.0)
        self.assertEqual(dash._line_cost_usd(None, {"input_tokens": 1}), 0.0)


class CwdToLoopTest(TempRootTest):
    def test_attributes_by_repo_relative_position_only(self):
        self.assertEqual(dash._cwd_to_loop(str(self.root / "loops" / "alpha")), "alpha")
        # Anywhere inside a loop's tree still belongs to that loop.
        self.assertEqual(
            dash._cwd_to_loop(str(self.root / "loops" / "beta" / "scripts")), "beta")
        self.assertEqual(dash._cwd_to_loop(str(self.root)), "hub")
        self.assertEqual(dash._cwd_to_loop(str(self.root / "hub")), "hub")

    def test_work_outside_this_repo_is_other(self):
        outside = str(Path(self.tmp.name) / "somewhere-else")
        self.assertEqual(dash._cwd_to_loop(outside), "other")
        self.assertEqual(dash._cwd_to_loop(None), "other")
        self.assertEqual(dash._cwd_to_loop(""), "other")


# ── identity parameterization (the CLAUDE.md invariant) ──────────────────────

class HubIdentityTest(TempRootTest):
    def write_registry(self, text: str):
        (self.root / "loops.toml").write_text(text, encoding="utf-8")

    def test_identity_is_read_from_the_registry(self):
        self.write_registry(
            '[hub]\npersona = "atlas"\ngroup = "atlas-group"\n'
            'estate = "the atlas estate"\ndeck_profile = "personal"\n')
        hub = dash.hub_config()
        self.assertEqual(hub["persona"], "atlas")
        self.assertEqual(hub["group"], "atlas-group")
        self.assertEqual(hub["estate"], "the atlas estate")
        self.assertEqual(hub["deck_profile"], "personal")

    def test_a_fresh_clone_has_no_identity_at_all(self):
        # No loops.toml — the state of a clone before the operator names it.
        # The contract is an empty config (callers then apply neutral defaults),
        # never an inherited name and never an exception.
        self.assertFalse((self.root / "loops.toml").exists())
        self.assertEqual(dash.hub_config(), {})

    def test_unparseable_registry_degrades_instead_of_raising(self):
        self.write_registry("[hub\npersona = broken")
        self.assertEqual(dash.hub_config(), {})

    def test_registry_without_a_hub_block_is_empty(self):
        self.write_registry('[dashboard]\nport = 8522\n')
        self.assertEqual(dash.hub_config(), {})


class BuildLoopsTest(TempRootTest):
    """build_loops must be driven entirely by the registry it is handed."""

    def setUp(self):
        super().setUp()
        self.titles = []
        self._saved_deck = dash.deck_session

        def stub(title, index):
            self.titles.append(title)
            return {"present": True, "status": "idle", "model": "claude-sonnet-5"}

        dash.deck_session = stub

    def tearDown(self):
        dash.deck_session = self._saved_deck
        super().tearDown()

    def registry(self):
        return {"loops": {
            "alpha": {"persona": "alpha-bot", "interval": "20m",
                      "model": "claude-sonnet-5", "autostart": True,
                      "role": "watches the alpha thing"},
            "beta": {"interval": "on-demand"},
        }}

    def test_rows_come_from_the_registry_not_from_code(self):
        rows = {r["name"]: r for r in dash.build_loops(self.registry(), NOW.timestamp(), {})}
        self.assertEqual(set(rows), {"hub", "alpha", "beta"})
        self.assertEqual(rows["alpha"]["persona"], "alpha-bot")
        self.assertEqual(rows["alpha"]["interval_seconds"], 1200)
        self.assertEqual(rows["alpha"]["model_pretty"], "Sonnet 5")
        self.assertEqual(rows["alpha"]["role"], "watches the alpha thing")
        self.assertTrue(rows["alpha"]["autostart"])

    def test_loop_defaults_fall_back_to_the_loop_name(self):
        rows = {r["name"]: r for r in dash.build_loops(self.registry(), NOW.timestamp(), {})}
        beta = rows["beta"]
        self.assertEqual(beta["persona"], "beta")     # persona defaults to name
        self.assertIsNone(beta["interval_seconds"])   # "on-demand" has no cadence
        self.assertEqual(beta["role"], "")            # no invented description
        self.assertFalse(beta["autostart"])

    def test_session_titles_follow_the_persona_convention(self):
        dash.build_loops(self.registry(), NOW.timestamp(), {})
        self.assertIn("alpha-bot (alpha)", self.titles)
        self.assertIn("beta (beta)", self.titles)
        # The hub asks for whatever the module resolved from the registry —
        # never a literal title written into build_loops.
        self.assertIn(dash.HUB_SESSION, self.titles)

    def test_an_empty_registry_still_yields_the_hub_row(self):
        rows = dash.build_loops({}, NOW.timestamp(), {})
        self.assertEqual([r["name"] for r in rows], ["hub"])
        self.assertTrue(rows[0]["is_hub"])

    def test_heartbeat_and_doorbell_liveness_read_from_state(self):
        now = NOW.timestamp()
        (self.root / "state" / "hub").mkdir()
        (self.root / "state" / "hub" / "last_tick").write_text(str(int(now) - 30))
        (self.root / "state" / "hub" / "doorbell_alive").write_text(str(int(now) - 10))
        hub = dash.build_loops({}, now, {})[0]
        self.assertAlmostEqual(hub["heartbeat_age"], 30, delta=1)
        self.assertTrue(hub["doorbell_alive"])

    def test_a_stale_doorbell_stamp_is_not_alive(self):
        now = NOW.timestamp()
        (self.root / "state" / "hub").mkdir()
        (self.root / "state" / "hub" / "doorbell_alive").write_text(str(int(now) - 600))
        self.assertFalse(dash.build_loops({}, now, {})[0]["doorbell_alive"])

    def test_a_garbage_heartbeat_file_is_no_heartbeat(self):
        (self.root / "state" / "hub").mkdir()
        (self.root / "state" / "hub" / "last_tick").write_text("not-an-epoch")
        hub = dash.build_loops({}, NOW.timestamp(), {})[0]
        self.assertIsNone(hub["last_tick"])
        self.assertEqual(hub["freshness"], "na")


# ── ledger tail ──────────────────────────────────────────────────────────────

class LedgerTailTest(TempRootTest):
    def write_ledger(self, text: str):
        (self.root / "state" / "ledger.jsonl").write_text(text, encoding="utf-8")

    def test_returns_the_last_n_newest_first(self):
        self.write_ledger("\n".join(
            json.dumps({"ts": i, "summary": f"row{i}"}) for i in range(50)) + "\n")
        rows = dash.build_ledger()
        self.assertEqual(len(rows), dash.LEDGER_TAIL)
        self.assertEqual(rows[0]["ts"], 49)                       # newest first
        self.assertEqual(rows[-1]["ts"], 50 - dash.LEDGER_TAIL)

    def test_a_short_ledger_returns_everything_it_has(self):
        self.write_ledger("\n".join(json.dumps({"ts": i}) for i in range(3)))
        self.assertEqual([r["ts"] for r in dash.build_ledger()], [2, 1, 0])

    def test_blank_and_malformed_lines_are_skipped(self):
        # A torn final line (the hub appending mid-read) must not take out the
        # whole panel — every other entry still renders.
        self.write_ledger("\n".join([
            json.dumps({"ts": 1}), "", "   ", "{not json",
            json.dumps({"ts": 2}), "]]garbage", '{"ts": 3, "partial"',
        ]) + "\n")
        self.assertEqual([r["ts"] for r in dash.build_ledger()], [2, 1])

    def test_a_missing_or_empty_ledger_is_empty_not_an_error(self):
        self.assertEqual(dash.build_ledger(), [])
        self.write_ledger("\n\n\n")
        self.assertEqual(dash.build_ledger(), [])


# ── usage aggregate ──────────────────────────────────────────────────────────

class UsageAggregateTest(TempRootTest):
    """build_usage scans ~/.claude/projects/*/*.jsonl. Every test points $HOME
    at a tempdir (Path.home() honours it) so the scan reads fixtures only."""

    WINDOW = {"dashboard": {"usage_window_hours": 5,
                            "usage_soft_budget_output_tokens": 1000}}

    def setUp(self):
        super().setUp()
        self._saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.projects = Path(self.tmp.name) / ".claude" / "projects"
        (self.projects / "proj-a").mkdir(parents=True)
        (self.projects / "proj-b").mkdir(parents=True)
        self.f_a = self.projects / "proj-a" / "s1.jsonl"
        self.f_b = self.projects / "proj-b" / "s2.jsonl"

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home
        super().tearDown()

    def line(self, ts, sid, loop, model="claude-opus-4-8",
             inp=0, out=0, cc=0, cr=0, cc_1h=0, cc_5m=0):
        if loop == "hub":
            cwd = str(self.root)
        elif loop == "other":
            cwd = str(Path(self.tmp.name) / "elsewhere")
        else:
            cwd = str(self.root / "loops" / loop)
        return json.dumps({
            "timestamp": ts.isoformat(), "cwd": cwd, "sessionId": sid,
            "message": {"model": model, "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
                "cache_creation": {"ephemeral_1h_input_tokens": cc_1h,
                                   "ephemeral_5m_input_tokens": cc_5m}}}})

    def append(self, path: Path, *lines):
        with path.open("a", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    def usage(self):
        return dash.build_usage(NOW, self.WINDOW)

    def test_sums_tokens_sessions_and_cost_across_files(self):
        t1 = NOW - timedelta(hours=1)
        self.append(self.f_a,
                    self.line(t1, "sess-a", "alpha", inp=100, out=50,
                              cc=200, cr=300, cc_1h=80, cc_5m=120),
                    self.line(NOW - timedelta(hours=2), "sess-a", "alpha",
                              inp=10, out=5))
        self.append(self.f_b,
                    self.line(t1, "sess-b", "hub", model="claude-haiku-4-5",
                              inp=7, out=3, cc=1, cr=2))
        u = self.usage()
        self.assertTrue(u["available"])
        self.assertEqual(u["messages"], 3)
        self.assertEqual(u["sessions"], 2)
        self.assertEqual(u["input"], 117)
        self.assertEqual(u["output"], 58)
        self.assertEqual(u["cache_creation"], 201)
        self.assertEqual(u["cache_read"], 302)
        self.assertEqual(u["total"], 117 + 58 + 201 + 302)
        self.assertGreater(u["cost_usd"], 0)

    def test_attributes_cost_to_the_loop_that_spent_it(self):
        t1 = NOW - timedelta(hours=1)
        self.append(self.f_a,
                    self.line(t1, "sess-a", "alpha", inp=1_000_000),   # $5.00
                    self.line(t1, "sess-b", "beta", inp=100_000),      # $0.50
                    self.line(t1, "sess-c", "other", inp=10_000))      # $0.05
        rows = {r["loop"]: r for r in self.usage()["cost_by_loop"]}
        self.assertEqual(set(rows), {"alpha", "beta", "other"})
        self.assertAlmostEqual(rows["alpha"]["cost_usd"], 5.0, places=4)
        self.assertAlmostEqual(rows["beta"]["cost_usd"], 0.5, places=4)
        self.assertEqual(rows["alpha"]["tokens"], 1_000_000)
        # Sorted most-expensive first, so the header truncation keeps what matters.
        self.assertEqual([r["loop"] for r in self.usage()["cost_by_loop"]],
                         ["alpha", "beta", "other"])

    def test_top_sessions_keeps_the_eight_biggest_spenders(self):
        t1 = NOW - timedelta(hours=1)
        # Ids that stay distinguishable after the 8-char truncation, so this
        # actually pins WHICH sessions survive the cap, not just how many.
        self.append(self.f_a, *[
            self.line(t1, f"sess-{i:02d}-{'x' * 20}", "alpha", inp=i * 1000)
            for i in range(1, 13)])
        top = self.usage()["top_sessions"]
        self.assertEqual(len(top), 8)
        # Ranked by spend, descending — sessions 12 down to 5; 1-4 are dropped.
        self.assertEqual([r["session"] for r in top],
                         [f"sess-{i:02d}-" for i in range(12, 4, -1)])
        self.assertEqual(top[0]["tokens"], 12_000)
        self.assertEqual(top[0]["loop"], "alpha")

    def test_a_sessions_lines_accumulate_across_files(self):
        t1 = NOW - timedelta(hours=1)
        self.append(self.f_a, self.line(t1, "sess-a", "alpha", inp=1000))
        self.append(self.f_b, self.line(t1, "sess-a", "alpha", inp=3000))
        top = self.usage()["top_sessions"]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["tokens"], 4000)

    def test_lines_older_than_the_window_are_excluded(self):
        old = NOW - timedelta(hours=10)
        self.append(self.f_a, self.line(old, "sess-old", "alpha", inp=9, out=9))
        # Also age the file itself so the mtime pre-filter drops it, matching
        # what production sees for a transcript nobody has touched all day.
        os.utime(self.f_a, (old.timestamp(), old.timestamp()))
        u = self.usage()
        self.assertFalse(u["available"])
        self.assertEqual(u["messages"], 0)

    def test_a_fresh_file_still_drops_its_out_of_window_lines(self):
        # The mtime filter is only a cheap pre-pass; the per-line cutoff is what
        # actually defines the window. A long-lived session proves it.
        self.append(self.f_a,
                    self.line(NOW - timedelta(hours=9), "sess-a", "alpha", out=999),
                    self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=7))
        u = self.usage()
        self.assertEqual(u["messages"], 1)
        self.assertEqual(u["output"], 7)

    def test_budget_percentage_uses_output_tokens(self):
        self.append(self.f_a,
                    self.line(NOW - timedelta(minutes=5), "sess-a", "alpha",
                              inp=50_000, out=250))
        u = self.usage()
        self.assertEqual(u["budget_output"], 1000)
        self.assertEqual(u["pct_of_budget"], 25)      # 250/1000, input ignored

    def test_no_budget_configured_means_no_percentage(self):
        self.append(self.f_a,
                    self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=250))
        u = dash.build_usage(NOW, {"dashboard": {"usage_window_hours": 5}})
        self.assertIsNone(u["pct_of_budget"])
        self.assertIsNone(u["budget_output"])

    def test_malformed_registry_values_fall_back_to_defaults(self):
        self.append(self.f_a,
                    self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=1))
        u = dash.build_usage(NOW, {"dashboard": {
            "usage_window_hours": "not-a-number",
            "usage_soft_budget_output_tokens": "nope"}})
        self.assertEqual(u["window_hours"], 5.0)      # documented default
        self.assertIsNone(u["budget_output"])

    def test_unparseable_and_usageless_lines_are_skipped(self):
        good = self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=42)
        with self.f_a.open("w", encoding="utf-8") as f:
            f.write('{"usage" broken json\n')                     # tripwire, not JSON
            f.write(json.dumps({"timestamp": NOW.isoformat(),
                                "message": {"usage": {}}}) + "\n")  # empty usage
            f.write(json.dumps({"message": {"usage": {"output_tokens": 5}}}) + "\n")
            f.write('{"type":"user","text":"no usage here"}\n')     # not a usage line
            f.write(good + "\n")
        u = self.usage()
        self.assertEqual(u["messages"], 1)
        self.assertEqual(u["output"], 42)

    def test_a_partial_trailing_line_does_not_corrupt_the_total(self):
        # Claude Code appends to these files while the dashboard reads them.
        complete = self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=50)
        partial = self.line(NOW - timedelta(minutes=4), "sess-a", "alpha", out=999)
        with self.f_a.open("w", encoding="utf-8") as f:
            f.write(complete + "\n")
            f.write(partial[:len(partial) // 2])     # torn mid-append, no newline
        u = self.usage()
        self.assertEqual(u["messages"], 1)
        self.assertEqual(u["output"], 50)

    def test_no_transcripts_at_all_is_unavailable_not_a_crash(self):
        u = self.usage()
        self.assertFalse(u["available"])
        self.assertEqual(u["messages"], 0)
        self.assertEqual(u["total"], 0)

    def test_the_scan_can_be_disabled_entirely(self):
        self.append(self.f_a,
                    self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=1))
        os.environ["DASHBOARD_USAGE"] = "0"
        try:
            u = self.usage()
        finally:
            os.environ.pop("DASHBOARD_USAGE", None)
        self.assertFalse(u["available"])
        self.assertEqual(u["reason"], "disabled")

    def test_the_output_is_labelled_as_a_local_estimate(self):
        # The renderer must never let this be mistaken for the account-wide
        # %-of-limit; the disclaimers are part of the contract.
        self.append(self.f_a,
                    self.line(NOW - timedelta(minutes=5), "sess-a", "alpha", out=1))
        u = self.usage()
        self.assertIn("approximate", u["note"])
        self.assertIn("estimate", u["pricing_note"])


REGISTRY = {"dashboard": {"usage_window_hours": 5,
                          "usage_soft_budget_output_tokens": 1000}}
# Compared fields — every externally-visible aggregate build_usage returns.
FIELDS = ("available", "input", "output", "cache_creation", "cache_read",
          "total", "messages", "sessions", "cost_usd", "pct_of_budget",
          "cost_by_loop", "top_sessions")


class DispatchSessionsTest(unittest.TestCase):
    def setUp(self):
        self.old_state = dash.STATE
        self.tmp = tempfile.TemporaryDirectory()
        dash.STATE = Path(self.tmp.name)
        (dash.STATE / "hub").mkdir()
        self.now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
        self.session = {
            "title": "ephemeral - dashboard-build", "group": dash.HUB_GROUP,
            "status": "running", "created_at": "2026-07-31T11:55:00+00:00",
            "path": "/tmp/dashboard-build", "id": "session-1",
        }

    def tearDown(self):
        dash.STATE = self.old_state
        self.tmp.cleanup()

    def test_tracked_session_joins_dispatch_context(self):
        (dash.STATE / "hub" / "dispatches.json").write_text(json.dumps({
            "ephemeral - dashboard-build": {
                "kind": "build", "ref": "widget#181", "task_id": "t-181"
            }
        }))
        item = dash.build_dispatch_sessions(self.now, [self.session])[0]
        self.assertEqual((item["kind"], item["ref"], item["task_id"]),
                         ("build", "widget#181", "t-181"))
        card = dash.render_dispatch_sessions([item])
        self.assertIn('class="es-ref">widget#181</div>', card)
        self.assertIn('class="es-kind">build</span>', card)
        self.assertIn('class="es-kind">ephemeral</span>', card)
        self.assertNotIn("href=", card)

    def test_untracked_session_keeps_the_original_card_fields(self):
        item = dash.build_dispatch_sessions(self.now, [self.session])[0]
        self.assertNotIn("ref", item)
        self.assertNotIn("kind", item)
        card = dash.render_dispatch_sessions([item])
        self.assertNotIn('class="es-ref"', card)
        self.assertIn('class="es-kind">ephemeral</span>', card)
        self.assertIn("dashboard-build", card)
        self.assertIn("5m alive", card)

    def test_stopped_history_is_not_in_flight_but_open_dispatch_is_visible(self):
        stopped = dict(self.session, status="stopped")
        self.assertEqual(dash.build_dispatch_sessions(self.now, [stopped], {}), [])
        for status in ("open", "resolved"):
            with self.subTest(status=status):
                rows = dash.build_dispatch_sessions(self.now, [stopped], {
                    stopped["title"]: {"status": status}})
                self.assertEqual(len(rows), int(status == "open"))
                if rows:
                    card = dash.render_dispatch_sessions(rows)
                    self.assertIn("stopped", card)
                    self.assertNotIn("alive", card)

    def test_failed_tracker_cannot_hide_stopped_work(self):
        stopped = dict(self.session, status="stopped")
        self.assertEqual(len(dash.build_dispatch_sessions(
            self.now, [stopped], {}, dispatches_known=False)), 1)
        # The implicit producer read must preserve the same warrant.
        self.assertEqual(len(dash.build_dispatch_sessions(self.now, [stopped])), 1)

    def test_error_and_unknown_sessions_remain_visible(self):
        for status in ("error", "unknown", "running", "waiting", "idle"):
            with self.subTest(status=status):
                self.assertEqual(len(dash.build_dispatch_sessions(
                    self.now, [dict(self.session, status=status)], {})), 1)

    def test_resolved_persistent_ask_is_not_current_work(self):
        title = "the catalog steward"
        for sessions in ([], [dict(self.session, title=title)]):
            self.assertEqual(dash.build_dispatch_sessions(self.now, sessions, {
                title: {"kind": "persistent-ask", "status": "resolved"}}), [])

    def test_dispatch_metadata_is_escaped(self):
        item = {"slug": "safe", "status": "waiting", "age": 1,
                "path": None, "ref": "<ref>", "kind": "x&y"}
        card = dash.render_dispatch_sessions([item])
        self.assertIn("&lt;ref&gt;", card)
        self.assertIn("x&amp;y", card)

    def test_live_persistent_ask_joins_the_same_deck_snapshot(self):
        title = "the catalog steward (catalog-meta)"
        persistent = {
            "title": title, "group": dash.HUB_GROUP, "status": "waiting",
            "created_at": "2026-07-31T11:50:00+00:00",
            "path": "/tmp/catalog-meta", "id": "session-2",
        }
        dispatches = {title: {
            "kind": "persistent-ask", "task_id": "t-667",
            "worktree": "/tmp/catalog-meta",
        }}
        item = next(i for i in dash.build_dispatch_sessions(
            self.now, [self.session, persistent], dispatches)
                    if i["session_type"] == "persistent-ask")
        self.assertEqual((item["slug"], item["status"], item["id"]),
                         (title, "waiting", "session-2"))
        card = dash.render_dispatch_sessions([item])
        self.assertIn(title, card)
        self.assertIn('class="es-kind">persistent-ask</span>', card)

    def test_missing_persistent_ask_renders_the_mismatch(self):
        title = "the missing steward"
        items = dash.build_dispatch_sessions(self.now, [], {title: {
            "kind": "persistent-ask", "task_id": "t-668",
            "worktree": "/tmp/missing",
        }})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "session not found")
        card = dash.render_dispatch_sessions(items)
        self.assertIn(title, card)
        self.assertIn("session not found", card)
        self.assertIn("persistent-ask", card)


class LoopIntervalTest(unittest.TestCase):
    def setUp(self):
        self.old_state = dash.STATE
        self.tmp = tempfile.TemporaryDirectory()
        dash.STATE = Path(self.tmp.name)
        self.registry = {"loops": {"mechanic": {
            "persona": "the mechanic", "interval": "20m",
        }}}

    def tearDown(self):
        dash.STATE = self.old_state
        self.tmp.cleanup()

    def loop(self):
        return next(row for row in dash.build_loops(self.registry, 0, {})
                    if row["name"] == "mechanic")

    def override(self, value: str):
        path = dash.STATE / "mechanic" / "pace-override"
        path.parent.mkdir(parents=True)
        path.write_text(value, encoding="utf-8")

    def test_override_replaces_registry_interval(self):
        self.override(" 120m \n")
        row = self.loop()
        self.assertEqual(row["interval"], "120m")
        self.assertEqual(row["interval_seconds"], 7200)

    def test_missing_override_falls_back_to_registry(self):
        row = self.loop()
        self.assertEqual(row["interval"], "20m")
        self.assertEqual(row["interval_seconds"], 1200)

    def test_dynamic_override_has_no_fixed_interval(self):
        self.override(" dynamic\n")
        row = self.loop()
        self.assertEqual(row["interval"], "dynamic (override)")
        self.assertIsNone(row["interval_seconds"])


def line(ts: datetime, sid: str, loop_dir: str, model: str,
         inp: int, out: int, cc: int, cr: int, cc_1h: int = 0, cc_5m: int = 0) -> str:
    cwd = str(dash.ROOT / "loops" / loop_dir) if loop_dir not in ("hub", "other") \
        else (str(dash.ROOT) if loop_dir == "hub" else "/tmp/somewhere-else")
    rec = {
        "timestamp": ts.isoformat(), "cwd": cwd, "sessionId": sid,
        "message": {"model": model, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
            "cache_creation": {"ephemeral_1h_input_tokens": cc_1h,
                               "ephemeral_5m_input_tokens": cc_5m}}}}
    return json.dumps(rec)


def project(subset):
    return {k: subset.get(k) for k in FIELDS}


class IncrementalScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name) / "projects"
        (base / "proj-a").mkdir(parents=True)
        (base / "proj-b").mkdir(parents=True)
        self.base = base
        self.cache = Path(self.tmp.name) / "cache.json"
        os.environ["DASHBOARD_TRANSCRIPT_BASE"] = str(base)
        os.environ["DASHBOARD_TRANSCRIPT_CACHE"] = str(self.cache)
        self.f_a = base / "proj-a" / "s1.jsonl"
        self.f_b = base / "proj-b" / "s2.jsonl"

    def tearDown(self):
        os.environ.pop("DASHBOARD_TRANSCRIPT_BASE", None)
        os.environ.pop("DASHBOARD_TRANSCRIPT_CACHE", None)
        self.tmp.cleanup()

    def cold(self) -> dict:
        """Ground truth: full read from byte zero (no pre-existing cache)."""
        if self.cache.exists():
            self.cache.unlink()
        return project(dash.build_usage(NOW, REGISTRY))

    def warm(self) -> dict:
        """Incremental: reuse whatever cache is on disk."""
        return project(dash.build_usage(NOW, REGISTRY))

    def append(self, path: Path, *lines_):
        with path.open("a", encoding="utf-8") as f:
            for ln in lines_:
                f.write(ln + "\n")

    def test_append_matches_full_rescan(self):
        t1 = NOW - timedelta(hours=1)
        t2 = NOW - timedelta(hours=2)
        self.append(self.f_a,
                    line(t1, "sess-a", "pr-reviewer", "claude-opus-4-8",
                         100, 50, 200, 300, cc_1h=80, cc_5m=120),
                    line(t2, "sess-a", "pr-reviewer", "claude-opus-4-8",
                         10, 5, 0, 0))
        self.append(self.f_b,
                    line(t1, "sess-b", "hub", "claude-haiku-4-5", 7, 3, 1, 2))

        first = self.cold()          # populate cache
        self.assertTrue(first["available"])
        self.assertEqual(first["messages"], 3)

        # Append to one file; warm (incremental) must equal a cold rescan.
        self.append(self.f_a,
                    line(NOW - timedelta(minutes=30), "sess-c", "other",
                         "claude-opus-4-8", 400, 200, 0, 0))
        warm = self.warm()
        truth = self.cold()
        self.assertEqual(warm, truth)
        self.assertEqual(warm["messages"], 4)

    def test_partial_trailing_line_counted_once(self):
        t1 = NOW - timedelta(hours=1)
        # A complete line, then a partial line with NO trailing newline.
        complete = line(t1, "sess-a", "pr-reviewer", "claude-opus-4-8", 100, 50, 0, 0)
        partial = line(t1, "sess-a", "pr-reviewer", "claude-opus-4-8", 999, 999, 0, 0)
        with self.f_a.open("w", encoding="utf-8") as f:
            f.write(complete + "\n")
            f.write(partial)  # no newline — mid-append

        warm1 = self.cold()
        self.assertEqual(warm1["messages"], 1)   # partial not yet counted
        self.assertEqual(warm1["output"], 50)

        # Complete the partial line.
        with self.f_a.open("a", encoding="utf-8") as f:
            f.write("\n")
        warm2 = self.warm()
        truth = self.cold()
        self.assertEqual(warm2, truth)
        self.assertEqual(warm2["messages"], 2)   # counted exactly once
        self.assertEqual(warm2["output"], 50 + 999)

    def test_rotation_triggers_full_reparse(self):
        t1 = NOW - timedelta(hours=1)
        self.append(self.f_a,
                    line(t1, "sess-a", "pr-reviewer", "claude-opus-4-8", 500, 500, 0, 0))
        self.cold()  # cache now has the big line + its offset

        # Truncate & rewrite with a smaller file (rotation): offset > new size.
        with self.f_a.open("w", encoding="utf-8") as f:
            f.write(line(t1, "sess-a", "pr-reviewer", "claude-opus-4-8", 1, 1, 0, 0) + "\n")
        warm = self.warm()
        truth = self.cold()
        self.assertEqual(warm, truth)
        self.assertEqual(warm["output"], 1)      # old 500 gone, not double-counted

    def test_out_of_window_file_excluded(self):
        old = NOW - timedelta(hours=10)
        self.append(self.f_a,
                    line(old, "sess-old", "pr-reviewer", "claude-opus-4-8", 9, 9, 0, 0))
        # Force the file mtime to be older than the cutoff so the mtime
        # pre-filter drops it entirely (matches production skip semantics).
        old_epoch = old.timestamp()
        os.utime(self.f_a, (old_epoch, old_epoch))
        res = self.cold()
        self.assertFalse(res["available"])
        self.assertEqual(res["messages"], 0)

    def test_unchanged_file_reuses_cache_without_reading(self):
        t1 = NOW - timedelta(hours=1)
        self.append(self.f_a,
                    line(t1, "sess-a", "pr-reviewer", "claude-opus-4-8", 100, 50, 0, 0))
        first = self.cold()
        # Make the file unreadable AFTER caching; an unchanged file must be
        # served from cache (offset == size → no read attempt), so the numbers
        # survive even though a fresh read would now fail.
        warm = self.warm()
        self.assertEqual(warm, first)


def _ref_tail(text: str, n: int) -> list:
    """The original build_ledger semantics: full parse, last n, reversed."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-n:][::-1]


class TailJsonlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, text: str, n: int):
        self.p.write_text(text, encoding="utf-8")
        self.assertEqual(dash.tail_jsonl(self.p, n), _ref_tail(text, n),
                         f"mismatch for n={n}, {len(text)} bytes")

    def test_matches_reference_across_shapes(self):
        rows = "\n".join(json.dumps({"ts": i, "v": f"row{i}"}) for i in range(50))
        for trailing in ("", "\n"):
            for n in (1, 5, 14, 50, 100):
                self.check(rows + trailing, n)

    def test_blank_and_malformed_lines_skipped_like_full_parse(self):
        text = ("\n".join([
            json.dumps({"ts": 1}), "", "  ",
            "{not json", json.dumps({"ts": 2}),
            "]]garbage", json.dumps({"ts": 3}), "",
        ]) + "\n")
        for n in (1, 2, 3, 10):
            self.check(text, n)

    def test_spans_block_boundary(self):
        # Each row ~padded so the file is comfortably larger than the 64 KiB
        # read block, forcing multi-block reverse reads.
        rows = "\n".join(json.dumps({"ts": i, "pad": "x" * 200}) for i in range(2000))
        self.assertGreater(len(rows), 65536 * 3)
        for n in (1, 14, 500, 2000, 5000):
            self.check(rows + "\n", n)

    def test_empty_and_missing(self):
        self.check("", 14)
        self.check("\n\n\n", 14)
        self.assertEqual(dash.tail_jsonl(Path(self.tmp.name) / "nope.jsonl", 14), [])
        self.assertEqual(dash.tail_jsonl(self.p, 0), [])


class BriefHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        self.reports = dash.STATE / "morning-brief" / "reports"
        self.reports.mkdir(parents=True)

    def tearDown(self):
        dash.STATE = self.old_state
        self.tmp.cleanup()

    def test_latest_five_reports_are_newest_first(self):
        for day in range(1, 8):
            name = f"2026-07-{day:02d}.md"
            (self.reports / name).write_text(f"# Brief {day}\n", encoding="utf-8")
        brief = dash.build_brief()
        self.assertEqual([r["date"] for r in brief["reports"]],
                         ["2026-07-07", "2026-07-06", "2026-07-05",
                          "2026-07-04", "2026-07-03"])
        self.assertEqual(brief["name"], "2026-07-07.md")
        self.assertEqual(brief["body"], "# Brief 7\n")

    def test_no_reports_has_empty_history(self):
        brief = dash.build_brief()
        self.assertEqual((brief["available"], brief["reports"]), (False, []))
        # and it says WHY there is no history, rather than leaving the
        # panel to guess that the briefer simply had a quiet morning.
        self.assertEqual(brief["reading"]["outcome"], "no_state")
        self.assertFalse(brief["reading"]["empty_is_evidence"])


class BriefDeliveryTest(unittest.TestCase):
    """Gap audit P-10. A report file on disk says a brief was WRITTEN. Until
    the publication record existed, nothing here could say whether the operator ever
    received it — a relay that failed looked exactly like one that worked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        self.reports = dash.STATE / "morning-brief" / "reports"
        self.reports.mkdir(parents=True)
        (self.reports / "2026-07-20.md").write_text("# Brief\n", encoding="utf-8")

    def tearDown(self):
        dash.STATE = self.old_state
        self.tmp.cleanup()

    def publications(self, delivery):
        (dash.STATE / "morning-brief" / "publications.json").write_text(
            json.dumps({"version": 1, "dates": {"2026-07-20": {
                "date": "2026-07-20",
                "generation": {"status": "generated"},
                "delivery": delivery}}}), encoding="utf-8")

    def test_a_report_with_no_record_claims_nothing_either_way(self):
        """A pre-P-10 report file is not evidence of delivery OR of failure."""
        self.assertIsNone(dash.build_brief()["delivery"])
        self.assertEqual(dash.brief_delivery_note(None), "")

    def test_a_delivered_brief_says_so(self):
        self.publications({"status": "delivered",
                           "delivered_at": "2026-07-20T11:00:00Z",
                           "attempts": [{"n": 1}]})
        got = dash.build_brief()["delivery"]
        self.assertEqual(got["status"], "delivered")
        self.assertEqual(dash.brief_delivery_note(got), "relayed to Slack")

    def test_an_undelivered_brief_is_visible_on_the_panel(self):
        self.publications({"status": "pending", "attempts": [{"n": 1}, {"n": 2}]})
        note = dash.brief_delivery_note(dash.build_brief()["delivery"])
        self.assertIn("NOT relayed yet", note)
        self.assertIn("2 attempts", note)

    def test_an_abandoned_brief_carries_its_reason(self):
        self.publications({"status": "abandoned", "reason": "older than today",
                           "attempts": []})
        note = dash.brief_delivery_note(dash.build_brief()["delivery"])
        self.assertIn("never relayed", note)
        self.assertIn("older than today", note)

    def test_the_note_rides_the_rendered_panel(self):
        """On every brief that has a record, not only the broken ones — P-03's
        rule about freshness fields, for the same reason."""
        self.publications({"status": "delivered", "attempts": [{"n": 1}]})
        html = dash.render_brief(dash.build_brief(), [])
        self.assertIn("relayed to Slack", html)
        self.assertIn("2026-07-20.md", html)

    def test_a_corrupt_record_degrades_instead_of_breaking_the_panel(self):
        (dash.STATE / "morning-brief" / "publications.json").write_text(
            "{not json", encoding="utf-8")
        self.assertIsNone(dash.build_brief()["delivery"])


class FocusProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        (dash.STATE / "focus").mkdir(parents=True)

    def tearDown(self):
        dash.STATE = self.old_state
        self.tmp.cleanup()

    def test_joins_declared_tracker_url_by_project_key(self):
        (dash.STATE / "focus" / "focus.json").write_text(json.dumps({
            "projects": [{"key": "alpha", "name": "Alpha"}]
        }), encoding="utf-8")
        (dash.STATE / "focus" / "projects.toml").write_text(
            '[projects.alpha]\nurl = "https://tracker.example.com/acme/project/alpha"\n',
            encoding="utf-8")
        project = dash.build_focus()["projects"][0]
        self.assertEqual(project["url"], "https://tracker.example.com/acme/project/alpha")


class ShellExtensionTest(unittest.TestCase):
    def test_explicit_extensions_and_section_ownership(self):
        page = dash.render_shell(
            {"estate": "estate", "operator": "operator"},
            extra_css=".custom{color:red}",
            extra_js="window.customReady=true;",
            unmanaged_sections=("brief",),
            brief_tick="recent & local",
        )
        self.assertIn(".custom{color:red}", page)
        self.assertIn("window.customReady=true;", page)
        self.assertIn("recent &amp; local", page)
        config = page.split("window.OPS_DASH_SECTIONS=", 1)[1].split(";</script>", 1)[0]
        sections = json.loads(config)
        self.assertNotIn("brief", sections)
        self.assertEqual(sections["focus"], "sec-focus")

    def test_live_snapshot_is_available_to_page_owned_sections(self):
        page = dash.render_shell({"estate": "estate", "operator": "operator"})
        self.assertIn("new CustomEvent('ops-dashboard-data',{detail:d})", page)


class ShellRegenerationTest(unittest.TestCase):
    """the HTML shells are regenerator output, not a manual step."""
    SNAP = {"estate": "estate", "operator": "operator"}
    PAGES = ("index.html", "estate.html", "dashboard-v2.html", "stewards.html")

    def setUp(self):
        self.old_state = dash.STATE
        self.tmp = tempfile.TemporaryDirectory()
        dash.STATE = Path(self.tmp.name) / "state"   # deliberately absent

    def tearDown(self):
        dash.STATE = self.old_state
        self.tmp.cleanup()

    def shells(self):
        return tuple(dash.STATE / name for name in self.PAGES)

    def test_every_shell_is_written_from_one_snapshot(self):
        written = dash.write_shells(self.SNAP)
        index, estate, v2, stewards = self.shells()
        self.assertEqual([p for p, _ in written], [index, estate, v2, stewards, dash.STATE / "stewards.json"])
        for path, size in written:
            self.assertTrue(path.exists(), path)
            self.assertEqual(len(path.read_bytes()), size)
            if path.suffix == ".html":
                self.assertTrue(path.read_text(encoding="utf-8").startswith("<!doctype html>"))
        self.assertIn("dashboard.json", index.read_text(encoding="utf-8"))
        self.assertIn("estate operations", estate.read_text(encoding="utf-8"))

    def test_every_shell_opens_with_the_same_page_nav(self):
        """One nav bar on every served page, the same links in the same order,
        with only the page's own link marked current (the operator, 2026-09-23)."""
        import dashboard_nav
        dash.write_shells(self.SNAP)
        for (key, href, _), path in zip(
                (dashboard_nav.PAGES[i] for i in (0, 2, 1, 3)), self.shells()):
            page = path.read_text(encoding="utf-8")
            self.assertEqual(page.count('class="page-nav"'), 1, path.name)
            self.assertIn(dashboard_nav.render(key), page, path.name)
            self.assertEqual(page.count('aria-current="page"'), 1, path.name)
            self.assertEqual(page.count('id="themebtn"'), 1, path.name)
        served = srv._allow()
        for _, href, _ in dashboard_nav.PAGES:
            self.assertIn(href, served, f"nav links {href}, which is not served")

    def test_no_served_shell_is_left_off_the_cycle(self):
        """The whole point: a page bin/dashboard-index knows about and the
        refresh cycle does not is a page that goes stale silently. There is one
        renderer list now, and dashboard-index calls into it."""
        written = {p.name for p, _ in dash.write_shells(self.SNAP)}
        self.assertEqual(written, set(self.PAGES) | {"stewards.json"})
        src = (_HERE / "dashboard-index").read_text(encoding="utf-8")
        self.assertIn("dashboard.write_shells", src)
        for name in self.PAGES:
            self.assertNotIn(f'"state" / "{name}"', src,
                             f"{name} is written by a second code path")

    def test_a_rendering_change_reaches_the_next_cycle(self):
        """The defect itself: new rendering code, same served page."""
        pages = self.shells()
        dash.write_shells(self.SNAP)
        before = tuple(p.read_bytes() for p in pages)
        old_css = dash.CSS
        try:
            dash.CSS = old_css + "\n.t149-probe{color:red}"
            dash.write_shells(self.SNAP)
        finally:
            dash.CSS = old_css
        for path, prior in zip(pages, before):
            self.assertNotEqual(path.read_bytes(), prior, path)
            self.assertIn(".t149-probe{color:red}", path.read_text(encoding="utf-8"))

    def test_shells_are_replaced_atomically(self):
        """A reader must never catch a half-written page off the server."""
        dash.write_shells(self.SNAP)
        leftovers = [p.name for p in dash.STATE.iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [])

    def test_the_json_cycle_writes_every_served_file(self):
        """`bin/dashboard --json` is exactly what dashboard-refresh runs."""
        real_snapshot, real_payload, argv = (dash.build_snapshot,
                                             dash.build_payload, sys.argv)
        dash.build_snapshot = lambda: dict(self.SNAP)
        dash.build_payload = lambda s: {**s, "tier": 1}
        sys.argv = ["dashboard", "--json"]
        dash.STATE.mkdir(parents=True, exist_ok=True)
        try:
            self.assertEqual(dash.main(), 0)
        finally:
            dash.build_snapshot, dash.build_payload = real_snapshot, real_payload
            sys.argv = argv
        for name in ("dashboard.json",) + self.PAGES:
            self.assertTrue((dash.STATE / name).exists(), name)
        # The fragment stays off the token-free path.
        self.assertFalse((dash.STATE / "dashboard.body.html").exists())


class DashboardTransportTest(unittest.TestCase):
    """The periodic snapshot is precompressed and conditionally served."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.saved_dash_state = dash.STATE
        self.saved_server_state = srv.STATE
        dash.STATE = self.state
        srv.STATE = str(self.state)
        self.payload = {"generated_epoch": 123, "message": "compress me " * 100}
        dash.write_dashboard_json(self.payload)
        self.httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        dash.STATE = self.saved_dash_state
        srv.STATE = self.saved_server_state
        self.tmp.cleanup()

    def request(self, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, "/dashboard.json", headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_generation_atomically_writes_matching_gzip_and_validators(self):
        identity = (self.state / "dashboard.json").read_bytes()
        compressed = (self.state / "dashboard.json.gz").read_bytes()
        self.assertEqual(gzip.decompress(compressed), identity)
        self.assertEqual(json.loads(identity), self.payload)
        self.assertTrue((self.state / "dashboard.json.validators.json").exists())
        self.assertEqual([], [p for p in self.state.iterdir() if ".tmp." in p.name])

    def test_identity_get_and_matching_conditional_get(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, (self.state / "dashboard.json").read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertNotIn("Content-Encoding", headers)
        status, conditional_headers, body = self.request(
            headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(conditional_headers["ETag"], headers["ETag"])

    def test_gzip_get_and_head_report_selected_representation(self):
        requested = {"Accept-Encoding": "br, gzip"}
        status, headers, body = self.request(headers=requested)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertEqual(gzip.decompress(body),
                         (self.state / "dashboard.json").read_bytes())
        status, head_headers, head_body = self.request("HEAD", requested)
        self.assertEqual(status, 200)
        self.assertEqual(head_body, b"")
        self.assertEqual(head_headers["Content-Encoding"], "gzip")
        self.assertEqual(int(head_headers["Content-Length"]), len(body))
        status, _, conditional_body = self.request(
            "HEAD", {**requested, "If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        self.assertEqual(conditional_body, b"")


class FollowupResolveRouteTest(unittest.TestCase):
    """POST /followup-resolve calls the follow-up store's own resolution
    operation. A store that has none (the standalone predecessor of the
    estate-backed shim) must be answered with a stated 501, never a 409 that
    reads as "somebody else moved this row"."""

    PAYLOAD = {"id": "t-5", "expected_status": "open", "note": "handled"}

    def setUp(self):
        saved = (srv._load_followups, srv.write_token,
                 srv.proposal_write_authorized)
        self.addCleanup(lambda: (
            setattr(srv, "_load_followups", saved[0]),
            setattr(srv, "write_token", saved[1]),
            setattr(srv, "proposal_write_authorized", saved[2])))
        # A follow-up module with no `apply_followup_resolve` at all.
        srv._load_followups = lambda: type("Legacy", (), {})()

    def test_a_store_without_the_operation_is_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            srv.apply_followup_resolution(dict(self.PAYLOAD))

    def test_the_route_answers_501_not_a_conflict(self):
        srv.write_token = lambda: "tok"
        srv.proposal_write_authorized = lambda headers: True
        httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
        conn.request("POST", "/followup-resolve",
                     body=json.dumps(self.PAYLOAD),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 501)
        self.assertIn("predates", body["error"])


class FollowupResolveEndpointTest(unittest.TestCase):
    """POST /followup-resolve against the REAL estate-backed `bin/followups`.

    The class above pins the fallback for a store that has no resolution
    operation; this one pins the route itself, now that the store has one.
    Same posture as the proposal route and deliberately so: fail-closed auth
    before the body is read, an in-process call into the operation the CLI
    itself uses, optimistic concurrency, and a 409 on a stale snapshot.

    The entry point is `bin/followups`' and NOT `bin/estate`'s, which is the
    one thing about this route that is not obvious. A follow-up sits at
    `open`, and `open` offers `ready` and `drop` — there is no `done` verb to
    reach, so a generic status transition would have refused this row.
    Resolution is the follow-up envelope's own operation.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._saved = (srv.STATE, srv.ACKS, srv.ESTATE_DB)
        srv.STATE = str(self.state)
        srv.ACKS = str(self.state / "dashboard-acks.json")
        srv.ESTATE_DB = str(self.state / "estate.db")
        followups = srv._load_followups()
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(followups.main([
                "add", "Chase the thing",
                "--source", "example-loop", "--classification", "action",
            ]), 0)
        self.httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        srv.STATE, srv.ACKS, srv.ESTATE_DB = self._saved
        self.tmp.cleanup()

    def write_token(self, value="s3cret"):
        secrets = self.state / "secrets"
        secrets.mkdir(exist_ok=True)
        (secrets / "dashboard-write-token").write_text(value + "\n",
                                                       encoding="utf-8")

    def post(self, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Dashboard-Token"] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/followup-resolve",
                     body=json.dumps(payload), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw.decode()
        return resp.status, body

    def row(self):
        conn = sqlite3.connect(srv.ESTATE_DB)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute(
                "SELECT * FROM tasks WHERE id='t-1'").fetchone())
        finally:
            conn.close()

    def last_event(self):
        conn = sqlite3.connect(srv.ESTATE_DB)
        try:
            return conn.execute("SELECT actor, summary, detail FROM events "
                                "ORDER BY seq DESC LIMIT 1").fetchone()
        finally:
            conn.close()

    @staticmethod
    def payload(**updates):
        out = {"id": "t-1", "expected_status": "open",
               "note": "Chased it; nothing further owed"}
        out.update(updates)
        return out

    # ── fail closed ──────────────────────────────────────────────────────
    def test_no_token_configured_fails_closed_and_changes_nothing(self):
        status, body = self.post(self.payload())
        self.assertEqual(status, 503)
        self.assertIn("dashboard-write-token", body["error"])
        self.assertEqual(self.row()["status"], "open")

    def test_missing_and_wrong_tokens_are_401_and_change_nothing(self):
        self.write_token()
        self.assertEqual(self.post(self.payload())[0], 401)
        self.assertEqual(self.post(self.payload(), "nope")[0], 401)
        self.assertEqual(self.row()["status"], "open")

    # ── the write itself ─────────────────────────────────────────────────
    def test_a_valid_resolution_closes_the_row_through_the_shims_operation(self):
        self.write_token()
        status, body = self.post(self.payload(), "s3cret")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["status"], body["state"], body["from_status"]),
                         ("done", "resolved", "open"))
        self.assertEqual(body["id"], "followup:1")  # the legacy id, as the CLI prints
        self.assertEqual(body["task_id"], "t-1")
        row = self.row()
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["closed_at"])
        # the resolution note lands in the envelope, not only in the event
        self.assertEqual(json.loads(row["refs"])["resolution"],
                         "Chased it; nothing further owed")

    def test_the_event_is_the_shims_own_summary_and_records_the_decision_actor(self):
        """Resolve closes through the ordinary status transition, so the
        summary is `open -> done` — the same terminal transition every other
        estate close writes, not a route-invented wording. The actor is the
        page's configured decision actor, because a click on that page is the
        person holding the write token."""
        self.write_token()
        self.assertEqual(self.post(self.payload(), "s3cret")[0], 200)
        self.assertEqual(self.last_event(),
                         (srv.decision_actor(), "open -> done",
                          "Chased it; nothing further owed"))

    # ── optimistic concurrency ───────────────────────────────────────────
    def test_a_stale_snapshot_is_a_409_and_does_not_overwrite(self):
        self.write_token()
        status, body = self.post(self.payload(expected_status="needs-owner"),
                                 "s3cret")
        self.assertEqual(status, 409, body)
        self.assertIn("refresh before resolving", body["error"])
        self.assertEqual(self.row()["status"], "open")

    def test_a_row_resolved_by_someone_else_first_conflicts_rather_than_repeats(self):
        self.write_token()
        self.assertEqual(self.post(self.payload(), "s3cret")[0], 200)
        status, body = self.post(self.payload(note="second click"), "s3cret")
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(self.row()["refs"])["resolution"],
                         "Chased it; nothing further owed")

    # ── validation ───────────────────────────────────────────────────────
    def test_note_is_required_and_extra_fields_are_rejected(self):
        self.write_token()
        self.assertEqual(self.post(self.payload(note="  "), "s3cret")[0], 400)
        self.assertEqual(self.post(self.payload(command="rm"), "s3cret")[0], 400)
        self.assertEqual(
            self.post(self.payload(expected_status=None), "s3cret")[0], 400)
        self.assertEqual(self.row()["status"], "open")

    def test_an_unknown_id_is_a_400_not_a_500(self):
        self.write_token()
        status, body = self.post(self.payload(id="t-404"), "s3cret")
        self.assertEqual(status, 400)
        self.assertIn("no follow-up matches", body["error"])

    def test_a_task_that_is_not_a_followup_is_refused(self):
        """`find_row` reaches follow-ups and nothing else, so this route
        cannot be pointed at an ordinary task by editing the id in the body."""
        self.write_token()
        estate = srv._load_estate()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(estate.main(
                ["task", "add", "not a follow-up", "--kind", "chore"]), 0)
        status, body = self.post(self.payload(id="t-2"), "s3cret")
        self.assertEqual(status, 400)
        self.assertIn("no follow-up matches", body["error"])

    def test_the_legacy_id_resolves_the_same_row(self):
        self.write_token()
        status, body = self.post(self.payload(id="followup:1"), "s3cret")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.row()["status"], "done")


class CapPaceTest(unittest.TestCase):
    RESET = "2026-08-03T09:00:00+00:00"

    def test_weekday_uses_ninety_percent_linear_budget(self):
        now = datetime(2026, 7, 28, 9, tzinfo=timezone.utc)  # 24h into week
        self.assertEqual(dash.cap_pace(14, self.RESET, hours=168,
                                       reserve_pct=10, now=now), (18, "good"))
        self.assertEqual(dash.cap_pace(18, self.RESET, hours=168,
                                       reserve_pct=10, now=now), (18, "warn"))
        self.assertEqual(dash.cap_pace(22, self.RESET, hours=168,
                                       reserve_pct=10, now=now), (18, "crit"))

    def test_weekend_spends_ten_percent_reserve(self):
        saturday = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)
        sunday = datetime(2026, 8, 2, 9, tzinfo=timezone.utc)
        self.assertEqual(dash.cap_pace(90, self.RESET, hours=168,
                                       reserve_pct=10, now=saturday), (90, "warn"))
        self.assertEqual(dash.cap_pace(95, self.RESET, hours=168,
                                       reserve_pct=10, now=sunday), (95, "warn"))

    def test_session_is_linear_over_five_hours(self):
        reset = "2026-07-28T15:00:00+00:00"
        now = datetime(2026, 7, 28, 11, tzinfo=timezone.utc)
        self.assertEqual(dash.cap_pace(10, reset, hours=5, now=now),
                         (20, "good"))


class EstateDrilldownTest(unittest.TestCase):
    """P-16 — a task's drill-down is complete, not a slice of the recent tail.

    build_estate_activity's `events` list is deliberately the last 80 rows: it
    backs the "recent activity" panel. Before P-16 the per-task drill-down
    filtered that same list client-side, so the moment an estate got busy every
    older task rendered with no events at all — and looked like a task nothing
    had ever happened to. The answer comes from `estate events`' query surface
    instead, which is the whole table.

    Bounding the snapshot MOVED that answer and did not weaken it. It used to
    be computed for every task ever created on every regen and embedded in
    dashboard.json (the bulk of estate_activity, re-parsed by every open tab
    every 30 seconds); it is now `GET /estate-detail?task=t-N`, read on the
    one occasion somebody opens the row. Same table, same query surface, same
    completeness — so these tests read the endpoint, and the last one pins that
    the snapshot SAYS it no longer carries the map rather than shipping an
    empty one a reader could mistake for "no events".
    """

    ESTATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "bin", "estate")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(dash, "STATE", self.old_state))
        self.old_db = srv.ESTATE_DB
        srv.ESTATE_DB = str(Path(self.tmp.name) / "estate.db")
        self.addCleanup(lambda: setattr(srv, "ESTATE_DB", self.old_db))

    def drilldown(self, task=None, project=None):
        return srv.estate_detail(task, project)

    def estate(self, *args):
        import subprocess
        import sys
        env = dict(os.environ, ESTATE_STATE_DIR=str(dash.STATE))
        result = subprocess.run([sys.executable, self.ESTATE, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_drilldown_survives_past_the_eighty_row_recent_window(self):
        self.estate("task", "add", "the old one", "--kind", "generic",
                    "--actor", "hub")
        self.estate("note", "t-1", "something happened to it", "--actor", "hub")
        # Bury it: 90 later events push the contract clean out of the recent slice.
        for n in range(90):
            self.estate("event", "--actor", "hub", "--kind", "note",
                        "--summary", f"unrelated {n}")
        out = dash.build_estate_activity({"loops": {}})
        self.assertTrue(out["available"])
        recent_task_ids = {r.get("task_id") for r in out["events"]}
        self.assertNotIn("t-1", recent_task_ids,
                         "fixture is wrong: t-1 should have scrolled off")
        summaries = [r["summary"] for r in self.drilldown(task="t-1")["events"]]
        self.assertIn("something happened to it", summaries)
        self.assertIn("task created (open)", summaries)

    def test_a_tasks_events_are_only_its_own(self):
        self.estate("task", "add", "first", "--kind", "generic")
        self.estate("task", "add", "second", "--kind", "generic")
        self.estate("note", "t-1", "belongs to one", "--actor", "hub")
        self.estate("note", "t-2", "belongs to two", "--actor", "hub")
        events = self.drilldown(task="t-1")["events"]
        self.assertIn("belongs to one", [r["summary"] for r in events])
        self.assertNotIn("belongs to two", [r["summary"] for r in events])

    def test_drilldown_rows_are_sequence_ordered_and_actor_normalized(self):
        self.estate("task", "add", "a task", "--kind", "generic")
        self.estate("event", "--actor", "hub", "--kind", "note",
                    "--summary", "under the persona name", "--task", "t-1")
        rows = self.drilldown(task="t-1")["events"]
        self.assertEqual([r["seq"] for r in rows],
                         sorted(r["seq"] for r in rows))
        self.assertEqual(rows[-1]["actor"], "hub")   # docs/actor-taxonomy.md

    def test_the_drilldown_carries_the_derived_row_the_snapshot_thinned(self):
        """A terminal task nothing else in the snapshot points at ships as a
        collapsed row; its body — refs included — comes from here, derived by
        the same `estate_work.build` the snapshot uses rather than a second
        single-task derivation that could disagree with it."""
        self.estate("task", "add", "old work", "--kind", "generic",
                    "--actor", "hub")
        self.estate("drop", "t-1", "superseded", "--actor", "hub")
        row = next(t for t in dash.build_estate_activity({"loops": {}})["tasks"]
                   if t["id"] == "t-1")
        self.assertTrue(row["summary_only"])
        self.assertNotIn("refs", row)
        detail = self.drilldown(task="t-1")["task"]
        self.assertEqual(detail["status"], "dropped")
        self.assertIn("refs", detail)
        self.assertIn("followup", detail)     # the full derived view, not a row

    def test_an_unknown_id_is_refused_before_any_query(self):
        for bad in ("", "t-1; drop table tasks", "../../etc/passwd", "t-"):
            with self.assertRaises(ValueError):
                self.drilldown(task=bad)

    def test_no_estate_database_is_not_an_error(self):
        out = dash.build_estate_activity({"loops": {}})
        self.assertFalse(out["available"])
        self.assertEqual(out["task_events"], {})
        # And it says the drill-down cannot be read here, rather than leaving
        # an empty map to be read as "no task has any history".
        self.assertEqual(out["task_events_source"], "unavailable")

    def test_the_snapshot_names_where_the_drilldown_comes_from(self):
        """an empty `task_events` map is ambiguous on its own: a store
        with no history and a snapshot that stopped carrying it look the same.
        `task_events_source` is the warrant (docs/absence-contract.md)."""
        self.estate("task", "add", "a task", "--kind", "generic")
        self.estate("note", "t-1", "something happened", "--actor", "hub")
        out = dash.build_estate_activity({"loops": {}})
        self.assertEqual(out["task_events"], {})
        self.assertEqual(out["task_events_source"], "on-demand")
        self.assertIn("something happened",
                      [r["summary"] for r in self.drilldown(task="t-1")["events"]])


class BoundedSnapshotTest(unittest.TestCase):
    """what the periodic payload carries, and what it refuses to.

    `estate_work.build` reads every task ever created, and `estate_activity`
    used to ship all of them in full plus a per-task event join over all of
    them — most of dashboard.json on a long-lived store, growing every night
    with no cap — and refetched entire by every open
    tab every 30 seconds.

    The bound is stated as a rule rather than a number: every task still ships
    a row, so the Terminal/All tabs, the by-id lookup and the search
    box still see the whole store — but a terminal task nothing else in the
    snapshot points at ships its COLLAPSED row alone, and no row ships the
    drill-down. Anything a panel renders without a click keeps its full row,
    which is what the id lists below pin.
    """

    ESTATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "bin", "estate")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(dash, "STATE", self.old_state))

    def estate(self, *args):
        import subprocess
        import sys
        env = dict(os.environ, ESTATE_STATE_DIR=str(dash.STATE))
        result = subprocess.run([sys.executable, self.ESTATE, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def populate(self):
        self.estate("project", "add", "Rollout", "--kind", "rollout")
        self.estate("task", "add", "still open", "--kind", "generic")
        self.estate("task", "add", "old and closed", "--kind", "generic")
        self.estate("drop", "t-2", "superseded", "--actor", "hub")
        self.estate("task", "add", "closed but projected", "--kind", "generic",
                    "--project", "p-1")
        self.estate("drop", "t-3", "superseded", "--actor", "hub")
        self.estate("proposal", "add", "a decided proposal",
                    "--fingerprint", "abc123", "--condition", "c",
                    "--desired-outcome", "d", "--completion-check", "e",
                    "--actor", "mechanic")
        self.estate("drop", "t-4", "rejected", "--actor", "hub")
        return dash.build_estate_activity({"loops": {}})

    def rows(self, out):
        return {t["id"]: t for t in out["tasks"]}

    def test_every_task_still_ships_a_row(self):
        out = self.populate()
        self.assertEqual(sorted(self.rows(out)), ["t-1", "t-2", "t-3", "t-4"])
        self.assertEqual(sum(out["task_counts"].values()), 4)

    def test_a_terminal_task_nothing_points_at_ships_collapsed(self):
        rows = self.rows(self.populate())
        self.assertTrue(rows["t-2"]["summary_only"])
        # Everything taskRow's badges and haystack()'s search string read.
        for field in ("id", "title", "status", "kind", "updated_at"):
            self.assertIn(field, rows["t-2"])
        for field in ("refs", "followup", "proposal", "blocked_by"):
            self.assertNotIn(field, rows["t-2"])

    def test_an_open_task_keeps_its_full_row(self):
        rows = self.rows(self.populate())
        self.assertNotIn("summary_only", rows["t-1"])
        for field in ("followup", "proposal", "blocked_by", "due_state",
                      "is_attention", "blocks_titles"):
            self.assertIn(field, rows["t-1"])

    def test_no_row_carries_the_refs_blob_any_more(self):
        """`refs` is the envelope the followup/proposal/message views on the
        same row were already derived FROM, and only the drill-down's <pre>
        renders it raw — a large share of an unbounded snapshot."""
        out = self.populate()
        self.assertEqual([t["id"] for t in out["tasks"] if "refs" in t], [])

    def test_a_row_a_panel_renders_without_a_click_keeps_its_full_row(self):
        """The id lists are ordered pointers INTO `tasks`, and each of those
        panels renders its rows' bodies with no click — a resolved follow-up
        behind the History toggle, a rejected proposal behind its stage tab.
        Thinning one of those would empty a panel."""
        out = self.populate()
        rows = self.rows(out)
        pointed = set()
        for key in ("attention", "followups", "proposals", "inbox_messages"):
            pointed.update(out[key])
        self.assertIn("t-4", out["proposals"], "fixture produced no proposal")
        for task_id in pointed:
            self.assertIn(task_id, rows, f"{task_id} points at nothing")
            self.assertNotIn("summary_only", rows[task_id],
                             f"{task_id} is rendered without a click")

    def test_a_real_projects_tasks_keep_their_full_rows(self):
        """`projectRow` names every rollup task by status and title, and
        `projectHistory` composes their events — the synthetic `unprojected`
        bucket is deliberately not followed, because it names every
        unprojected task and would bound nothing."""
        out = self.populate()
        rows = self.rows(out)
        project = next(p for p in out["projects"] if p["id"] == "p-1")
        for task_id in project["rollup"]["task_ids"]:
            self.assertNotIn("summary_only", rows[task_id])
        self.assertTrue(rows["t-2"]["summary_only"],
                        "an unprojected terminal task is still collapsed")

    def test_the_bound_holds_as_the_store_grows(self):
        """The completion check: the payload tracks OPEN work, not the length
        of the store's history."""
        base = len(json.dumps(self.populate()["tasks"]))
        for n in range(40):
            self.estate("task", "add", f"more history {n}", "--kind", "generic")
            self.estate("drop", f"t-{5 + n}", "done with", "--actor", "hub")
        grown = len(json.dumps(dash.build_estate_activity({"loops": {}})["tasks"]))
        per_task = (grown - base) / 40
        self.assertLess(per_task, 400,
                        "a closed task must cost a collapsed row, not a full one")


class EstateBlockedByTest(unittest.TestCase):
    """P-18 — the dashboard names what a task is waiting on.

    The board rendered a blocked task as `ready` with nothing to say it could
    not start, so the operator read it as work available to pick up. blocked_by is
    computed on every snapshot from unresolved `blocks` edges, so it clears
    itself when the blocker finishes and there is no second copy of the truth
    to go stale.
    """

    ESTATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "bin", "estate")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(dash, "STATE", self.old_state))

    def estate(self, *args):
        import subprocess
        import sys
        env = dict(os.environ, ESTATE_STATE_DIR=str(dash.STATE))
        result = subprocess.run([sys.executable, self.ESTATE, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def tasks(self) -> dict:
        return {t["id"]: t
                for t in dash.build_estate_activity({"loops": {}})["tasks"]}

    def chain(self):
        self.estate("task", "add", "the blocker", "--kind", "generic", "--ready")
        self.estate("task", "add", "the blocked one", "--kind", "generic", "--ready")
        self.estate("task", "add", "unrelated", "--kind", "generic", "--ready")
        self.estate("dep", "add", "t-1", "t-2", "--kind", "blocks")

    def test_a_blocked_task_names_its_blocker(self):
        self.chain()
        tasks = self.tasks()
        self.assertEqual(tasks["t-2"]["blocked_by"], ["t-1"])
        self.assertEqual(tasks["t-1"]["blocked_by"], [])
        self.assertEqual(tasks["t-3"]["blocked_by"], [])

    def test_the_status_the_dashboard_shows_is_still_the_stored_one(self):
        self.chain()
        self.assertEqual(self.tasks()["t-2"]["status"], "ready")

    def test_it_clears_once_the_blocker_reaches_a_terminal_state(self):
        self.chain()
        self.assertEqual(self.tasks()["t-2"]["blocked_by"], ["t-1"])
        self.estate("claim", "t-1", "--actor", "hub")
        self.assertEqual(self.tasks()["t-2"]["blocked_by"], ["t-1"],
                         "claimed is not terminal")
        self.estate("done", "t-1", "--actor", "hub", "--summary", "done",
                    "--verification", "tests pass")
        self.assertEqual(self.tasks()["t-2"]["blocked_by"], [])
        self.assertEqual(self.tasks()["t-2"]["status"], "ready")

    def test_a_store_with_no_dependency_table_is_not_an_error(self):
        """A derived badge must never take down the panel that carries it."""
        import sqlite3 as sq
        db = dash.STATE / "estate.db"
        conn = sq.connect(db)
        conn.executescript("""
            CREATE TABLE tasks (seq INTEGER PRIMARY KEY, id TEXT, status TEXT,
                                kind TEXT, updated_at TEXT);
            CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                                 actor TEXT, kind TEXT, summary TEXT);
            INSERT INTO tasks VALUES (1, 't-1', 'ready', 'generic', 'x');""")
        conn.commit(); conn.close()
        out = dash.build_estate_activity({"loops": {}})
        self.assertTrue(out["available"], out.get("error"))
        self.assertEqual(out["tasks"][0]["blocked_by"], [])

    def test_every_task_carries_the_field_even_when_empty(self):
        """Absent would read as 'we did not look'; [] says 'nothing holds it'."""
        self.chain()
        for task in self.tasks().values():
            self.assertIsInstance(task["blocked_by"], list)


class EstateWorkSnapshotTest(unittest.TestCase):
    """the estate page's four questions reach `dashboard.json`."""

    ESTATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "bin", "estate")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_state = dash.STATE
        dash.STATE = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(dash, "STATE", self.old_state))

    def estate(self, *args):
        import subprocess
        import sys
        env = dict(os.environ, ESTATE_STATE_DIR=str(dash.STATE), ESTATE_TZ="UTC")
        result = subprocess.run([sys.executable, self.ESTATE, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def populate(self):
        self.estate("project", "add", "Gap audit", "--kind", "rollout")
        self.estate("task", "add", "blocker", "--kind", "generic", "--ready",
                    "--project", "p-1")
        self.estate("task", "add", "held up", "--kind", "generic", "--ready",
                    "--project", "p-1")
        self.estate("dep", "add", "t-1", "t-2", "--kind", "blocks")
        self.estate("dep", "add", "t-2", "t-1", "--kind", "related")
        self.estate("task", "add", "ask the owner", "--kind", "followup")
        self.estate("task", "add", "loose work", "--kind", "generic", "--ready")
        return dash.build_estate_activity({"loops": {}})

    def test_projects_and_their_rollups_reach_the_snapshot(self):
        out = self.populate()
        ids = [p["id"] for p in out["projects"]]
        self.assertEqual(ids, ["p-1", "unprojected"])
        self.assertEqual(out["projects"][0]["rollup"]["open"], 2)
        self.assertEqual(out["project_counts"], {"active": 1})

    def test_every_dependency_kind_reaches_the_snapshot(self):
        out = self.populate()
        self.assertEqual(out["dep_counts"], {"blocks": 1, "related": 1})
        edge = next(e for e in out["dependencies"] if e["kind"] == "blocks")
        self.assertEqual(edge["from_title"], "blocker")
        self.assertEqual(edge["to_title"], "held up")
        self.assertTrue(edge["active"])

    def test_the_attention_set_and_its_counts_reach_the_snapshot(self):
        out = self.populate()
        self.assertEqual(out["attention"], ["t-3"])
        self.assertEqual(out["attention_counts"]["total"], 1)
        self.assertEqual(out["followups"], ["t-3"])
        self.assertEqual(out["notice_days"], 3.0)

    def test_a_project_gets_its_own_authoritative_history(self):
        self.populate()
        self.estate("event", "--actor", "hub", "--kind", "note",
                    "--summary", "the rollout kicked off", "--project", "p-1")
        out = dash.build_estate_activity({"loops": {}})
        summaries = [r["summary"] for r in out["project_events"]["p-1"]]
        self.assertIn("the rollout kicked off", summaries)
        self.assertNotIn("unprojected", out["project_events"],
                         "the synthetic grouping is not a project to log against")

    def test_every_event_ships_exactly_once(self):
        """§4.10 — the snapshot is append-only and already grows on its own.
        An event written against a project's TASK names both ids, so the
        snapshot's `project_events` carries only the rows no task carries.

        Now the tasks' half is not in the snapshot at all: it arrives
        from `GET /estate-detail?project=p-N`, whose `project` filter returns
        every row naming the project — the project's own AND its tasks' — so
        the page composes the two with the seq dedup it always had."""
        self.populate()
        self.estate("event", "--actor", "hub", "--kind", "note",
                    "--summary", "project-level only", "--project", "p-1")
        out = dash.build_estate_activity({"loops": {}})
        embedded = out["project_events"]["p-1"]
        self.assertTrue(embedded, "fixture produced no project-level rows")
        self.assertTrue(all(r["project_id"] == "p-1" for r in embedded))
        self.assertTrue(all(not r["task_id"] for r in embedded),
                        "a row a task already carries must not ship twice")
        srv.ESTATE_DB = str(dash.STATE / "estate.db")
        composed = srv.estate_detail(None, "p-1")["events"]
        seqs = {r["seq"] for r in composed}
        self.assertTrue({r["seq"] for r in embedded} <= seqs)
        self.assertTrue(any(r["task_id"] for r in composed),
                        "the endpoint composes the tasks' rows in")
        self.assertEqual(len(seqs), len(composed), "and never twice")

    def test_derived_task_fields_ride_on_every_row(self):
        out = self.populate()
        for task in out["tasks"]:
            for field in ("blocked_by", "blocks", "due_state", "days_left",
                          "is_attention", "followup", "project_title",
                          "blocked_by_titles", "blocks_titles"):
                self.assertIn(field, task, f"{field} missing from {task['id']}")

    def test_no_estate_database_leaves_every_collection_empty_not_absent(self):
        """A page that asks for `x.projects` must get [] rather than undefined
        the day the store is not there."""
        out = dash.build_estate_activity({"loops": {}})
        self.assertFalse(out["available"])
        for key in ("projects", "dependencies", "attention", "followups"):
            self.assertEqual(out[key], [])
        for key in ("project_counts", "dep_counts", "attention_counts",
                    "followup_counts", "project_events"):
            self.assertEqual(out[key], {})

    def test_a_store_with_no_projects_table_is_not_an_error(self):
        import sqlite3 as sq
        conn = sq.connect(dash.STATE / "estate.db")
        conn.executescript("""
            CREATE TABLE tasks (seq INTEGER PRIMARY KEY, id TEXT, status TEXT,
                                kind TEXT, title TEXT, updated_at TEXT);
            CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                                 actor TEXT, kind TEXT, summary TEXT);
            INSERT INTO tasks VALUES (1,'t-1','needs-owner','generic','a','x');""")
        conn.commit(); conn.close()
        out = dash.build_estate_activity({"loops": {}})
        self.assertTrue(out["available"], out.get("error"))
        self.assertEqual([p["id"] for p in out["projects"]], ["unprojected"])
        self.assertEqual(out["dependencies"], [])
        self.assertEqual(out["attention"], ["t-1"])
        self.assertEqual(out["project_events"], {})


class ProposalVitalTest(unittest.TestCase):
    def test_counts_the_panels_pending_review_inventory(self):
        self.assertEqual(dash.pending_proposal_count({
            "available": True,
            "proposal_counts": {"pending-review": 85, "resolved": 121}}), 85)
        self.assertEqual(dash.pending_proposal_count({
            "available": True, "proposal_counts": {}}), 0)

    def test_unread_store_is_unknown_not_zero(self):
        count = dash.pending_proposal_count({"available": False})
        self.assertIsNone(count)
        card = dash.render_vitals({"loops": [], "vitals": {
            "loops_live": 0, "needs_you": 0, "going_astray": 0,
            "queue_queued": 0, "proposals": count}})
        self.assertIn('class="n">?</div>', card)
        self.assertNotIn('None', card)


class EstateLoaderStoreGateTest(unittest.TestCase):
    """The server repoints the estate CLI module at its own store, and the
    module asks a second question — STORE, the resolver's gate — before it will
    open anything. Repointing DB_PATH alone left that gate on whatever store the
    module resolved for itself, which the endpoint tests never noticed because
    this repo's real state/estate.db happens to exist. Everything here runs in a
    temp dir with no state/estate.db of its own."""

    def setUp(self):
        import subprocess
        import sys
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = (srv.STATE, srv.ESTATE_DB, srv._ESTATE_MODULE)
        self.addCleanup(lambda: (setattr(srv, "STATE", self.saved[0]),
                                 setattr(srv, "ESTATE_DB", self.saved[1]),
                                 setattr(srv, "_ESTATE_MODULE", self.saved[2])))
        result = subprocess.run(
            [sys.executable, os.path.join(str(_HERE), "estate"), "init"],
            capture_output=True, text=True,
            env=dict(os.environ, ESTATE_STATE_DIR=self.tmp.name))
        self.assertEqual(result.returncode, 0, result.stderr)
        srv.STATE = self.tmp.name
        srv.ESTATE_DB = os.path.join(self.tmp.name, "estate.db")
        srv._ESTATE_MODULE = None

    def test_the_gate_is_repointed_with_the_path(self):
        module = srv._load_estate()
        self.assertEqual(os.path.normpath(module.STORE.db_path),
                         os.path.normpath(srv.ESTATE_DB))

    def test_a_gate_that_resolved_elsewhere_does_not_refuse_the_servers_store(self):
        module = srv._load_estate()
        fresh_clone = os.path.join(self.tmp.name, "fresh", "bin", "estate")
        module.STORE = module.estate_store.resolve(fresh_clone, env={})
        self.assertFalse(module.STORE.exists())
        self.assertIs(srv._load_estate(), module)
        module.connect().close()

if __name__ == "__main__":
    unittest.main(verbosity=2)

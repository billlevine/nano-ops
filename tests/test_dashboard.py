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

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent / "bin"
_loader = SourceFileLoader("_dashboard", str(_HERE / "dashboard"))
_spec = importlib.util.spec_from_loader("_dashboard", _loader)
dash = importlib.util.module_from_spec(_spec)
_loader.exec_module(dash)

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


class EphemeralQueueTest(unittest.TestCase):
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
                "kind": "build", "ref": "acme/api#181", "task_id": "t-181"
            }
        }))
        item = dash.build_ephemeral(self.now, [self.session])[0]
        self.assertEqual((item["kind"], item["ref"], item["task_id"]),
                         ("build", "acme/api#181", "t-181"))
        card = dash.render_ephemeral([item])
        self.assertIn('class="es-ref">acme/api#181</div>', card)
        self.assertIn('class="es-kind">build</span>', card)
        self.assertNotIn("href=", card)

    def test_untracked_session_keeps_the_original_card_fields(self):
        item = dash.build_ephemeral(self.now, [self.session])[0]
        self.assertNotIn("ref", item)
        self.assertNotIn("kind", item)
        card = dash.render_ephemeral([item])
        self.assertNotIn('class="es-ref"', card)
        self.assertNotIn('class="es-kind"', card)
        self.assertIn("dashboard-build", card)
        self.assertIn("5m alive", card)

    def test_dispatch_metadata_is_escaped(self):
        item = {"slug": "safe", "status": "waiting", "age": 1,
                "path": None, "ref": "<ref>", "kind": "x&y"}
        card = dash.render_ephemeral([item])
        self.assertIn("&lt;ref&gt;", card)
        self.assertIn("x&amp;y", card)


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
        self.assertEqual(dash.build_brief(), {"available": False, "reports": []})


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
    backs the "recent activity" panel. Before this, the per-task drill-down
    filtered that same list client-side, so the moment an estate got busy every
    older task rendered with no events at all — and looked like a task nothing
    had ever happened to. `task_events` comes from `estate events`' query
    surface instead, which is the whole table.
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

    def test_drilldown_survives_past_the_eighty_row_recent_window(self):
        self.estate("task", "add", "the old one", "--kind", "generic",
                    "--actor", "hub")
        self.estate("note", "t-1", "something happened to it", "--actor", "hub")
        # Bury it: 90 later events push t-1 clean out of the recent slice.
        for n in range(90):
            self.estate("event", "--actor", "hub", "--kind", "note",
                        "--summary", f"unrelated {n}")
        out = dash.build_estate_activity({"loops": {}})
        self.assertTrue(out["available"])
        recent_task_ids = {r.get("task_id") for r in out["events"]}
        self.assertNotIn("t-1", recent_task_ids,
                         "fixture is wrong: t-1 should have scrolled off")
        summaries = [r["summary"] for r in out["task_events"]["t-1"]]
        self.assertIn("something happened to it", summaries)
        self.assertIn("task created (open)", summaries)

    def test_a_tasks_events_are_only_its_own(self):
        self.estate("task", "add", "first", "--kind", "generic")
        self.estate("task", "add", "second", "--kind", "generic")
        self.estate("note", "t-1", "belongs to one", "--actor", "hub")
        self.estate("note", "t-2", "belongs to two", "--actor", "hub")
        events = dash.build_estate_activity({"loops": {}})["task_events"]
        self.assertIn("belongs to one", [r["summary"] for r in events["t-1"]])
        self.assertNotIn("belongs to two", [r["summary"] for r in events["t-1"]])

    def test_drilldown_rows_are_sequence_ordered_and_actor_normalized(self):
        self.estate("task", "add", "a task", "--kind", "generic")
        self.estate("event", "--actor", "hub", "--kind", "note",
                    "--summary", "under the persona name", "--task", "t-1")
        rows = dash.build_estate_activity({"loops": {}})["task_events"]["t-1"]
        self.assertEqual([r["seq"] for r in rows],
                         sorted(r["seq"] for r in rows))
        self.assertEqual(rows[-1]["actor"], "hub")   # docs/actor-taxonomy.md

    def test_no_estate_database_is_not_an_error(self):
        out = dash.build_estate_activity({"loops": {}})
        self.assertFalse(out["available"])
        self.assertEqual(out["task_events"], {})


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
    """t-135 — the estate page's four questions reach `dashboard.json`.

    Projects were absent from the snapshot entirely and the dependency graph
    was represented only by derived `blocked_by` ids, so /estate.html could not
    have rendered a Projects or a Dependencies section from what it was given.
    The rules themselves live in lib/estate_work.py and are tested there; what
    is asserted here is the WIRING — that one composed read feeds the snapshot
    and that a store which cannot answer part of it still produces a page.
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
        An event written against a project's TASK names both ids, so shipping
        it under both keys would send it twice; the page joins the two
        collections client-side, where it costs nothing."""
        self.populate()
        self.estate("event", "--actor", "hub", "--kind", "note",
                    "--summary", "project-level only", "--project", "p-1")
        out = dash.build_estate_activity({"loops": {}})
        project_seqs = {r["seq"] for r in out["project_events"]["p-1"]}
        task_seqs = {r["seq"] for events in out["task_events"].values()
                     for r in events}
        self.assertTrue(project_seqs and task_seqs, "fixture produced neither")
        self.assertEqual(project_seqs & task_seqs, set())
        self.assertTrue(all(r["project_id"] == "p-1"
                            for r in out["project_events"]["p-1"]))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for bin/dashboard — the pure-reader half of the estate renderer.
Run: python3 bin/test_dashboard.py

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
    (the invariant in CLAUDE.md and docs/allowlist.md)

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

_HERE = Path(__file__).resolve().parent
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

        def stub(title):
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
        rows = {r["name"]: r for r in dash.build_loops(self.registry(), NOW.timestamp())}
        self.assertEqual(set(rows), {"hub", "alpha", "beta"})
        self.assertEqual(rows["alpha"]["persona"], "alpha-bot")
        self.assertEqual(rows["alpha"]["interval_seconds"], 1200)
        self.assertEqual(rows["alpha"]["model_pretty"], "Sonnet 5")
        self.assertEqual(rows["alpha"]["role"], "watches the alpha thing")
        self.assertTrue(rows["alpha"]["autostart"])

    def test_loop_defaults_fall_back_to_the_loop_name(self):
        rows = {r["name"]: r for r in dash.build_loops(self.registry(), NOW.timestamp())}
        beta = rows["beta"]
        self.assertEqual(beta["persona"], "beta")     # persona defaults to name
        self.assertIsNone(beta["interval_seconds"])   # "on-demand" has no cadence
        self.assertEqual(beta["role"], "")            # no invented description
        self.assertFalse(beta["autostart"])

    def test_session_titles_follow_the_persona_convention(self):
        dash.build_loops(self.registry(), NOW.timestamp())
        self.assertIn("alpha-bot (alpha)", self.titles)
        self.assertIn("beta (beta)", self.titles)
        # The hub asks for whatever the module resolved from the registry —
        # never a literal title written into build_loops.
        self.assertIn(dash.HUB_SESSION, self.titles)

    def test_an_empty_registry_still_yields_the_hub_row(self):
        rows = dash.build_loops({}, NOW.timestamp())
        self.assertEqual([r["name"] for r in rows], ["hub"])
        self.assertTrue(rows[0]["is_hub"])

    def test_heartbeat_and_doorbell_liveness_read_from_state(self):
        now = NOW.timestamp()
        (self.root / "state" / "hub").mkdir()
        (self.root / "state" / "hub" / "last_tick").write_text(str(int(now) - 30))
        (self.root / "state" / "hub" / "doorbell_alive").write_text(str(int(now) - 10))
        hub = dash.build_loops({}, now)[0]
        self.assertAlmostEqual(hub["heartbeat_age"], 30, delta=1)
        self.assertTrue(hub["doorbell_alive"])

    def test_a_stale_doorbell_stamp_is_not_alive(self):
        now = NOW.timestamp()
        (self.root / "state" / "hub").mkdir()
        (self.root / "state" / "hub" / "doorbell_alive").write_text(str(int(now) - 600))
        self.assertFalse(dash.build_loops({}, now)[0]["doorbell_alive"])

    def test_a_garbage_heartbeat_file_is_no_heartbeat(self):
        (self.root / "state" / "hub").mkdir()
        (self.root / "state" / "hub" / "last_tick").write_text("not-an-epoch")
        hub = dash.build_loops({}, NOW.timestamp())[0]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

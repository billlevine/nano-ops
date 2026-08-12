#!/usr/bin/env python3
"""Tests for the mechanic engine's deterministic core: window gating, night
identity, phase derivation, ledger timestamp parsing, the windows/record CLI,
and the incremental digest (section hashing, ledger cursor, snapshot lifecycle).
Run: python3 test_mechanic.py"""
import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "loops", "mechanic", ".claude", "skills",
                      "mechanic", "scripts", "mechanic.py")
_spec = importlib.util.spec_from_loader(
    "mechanic", importlib.machinery.SourceFileLoader("mechanic", SCRIPT))
mechanic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mechanic)
TZ = dt.timezone(dt.timedelta(hours=-4))  # fixed offset; tests never use wall clock


def local(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=TZ)


class TestInWindow(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(mechanic.in_window("03:00", "02:00", "05:00"))

    def test_start_inclusive_end_exclusive(self):
        self.assertTrue(mechanic.in_window("02:00", "02:00", "05:00"))
        self.assertFalse(mechanic.in_window("05:00", "02:00", "05:00"))

    def test_outside(self):
        self.assertFalse(mechanic.in_window("01:59", "02:00", "05:00"))
        self.assertFalse(mechanic.in_window("21:40", "02:00", "05:00"))

    def test_wraps_midnight(self):
        self.assertTrue(mechanic.in_window("23:30", "22:00", "06:00"))
        self.assertTrue(mechanic.in_window("01:00", "22:00", "06:00"))
        self.assertFalse(mechanic.in_window("12:00", "22:00", "06:00"))

    def test_empty_start_disables(self):
        self.assertFalse(mechanic.in_window("03:00", "", "05:00"))

    def test_empty_end_runs_to_midnight(self):
        self.assertTrue(mechanic.in_window("23:59", "22:00", ""))
        self.assertFalse(mechanic.in_window("21:59", "22:00", ""))


class TestNightId(unittest.TestCase):
    def test_non_wrapping_window_night_is_today(self):
        self.assertEqual(
            mechanic.night_id(local(2026, 7, 19, 3, 0), "02:00", "05:00"),
            "2026-07-19")
        # Outside the window the night id is still the local date.
        self.assertEqual(
            mechanic.night_id(local(2026, 7, 18, 21, 40), "02:00", "05:00"),
            "2026-07-18")

    def test_wrapping_window_after_midnight_belongs_to_previous_date(self):
        self.assertEqual(
            mechanic.night_id(local(2026, 7, 19, 1, 0), "22:00", "06:00"),
            "2026-07-18")
        self.assertEqual(
            mechanic.night_id(local(2026, 7, 18, 23, 0), "22:00", "06:00"),
            "2026-07-18")


class TestDerivePhase(unittest.TestCase):
    CFG = {"pass_start": "02:00", "pass_end": "05:00"}

    def test_outside_window_idle(self):
        phase, night, _ = mechanic.derive_phase(
            local(2026, 7, 18, 21, 40), self.CFG, [])
        self.assertEqual(phase, "idle")
        self.assertEqual(night, "2026-07-18")

    def test_in_window_no_events_pass(self):
        phase, night, _ = mechanic.derive_phase(
            local(2026, 7, 19, 2, 30), self.CFG, [])
        self.assertEqual(phase, "pass")
        self.assertEqual(night, "2026-07-19")

    def test_in_window_started_not_done_resume(self):
        events = [{"event": "pass_start", "night": "2026-07-19"}]
        phase, _, _ = mechanic.derive_phase(
            local(2026, 7, 19, 3, 0), self.CFG, events)
        self.assertEqual(phase, "resume")

    def test_in_window_done_tonight_done(self):
        events = [{"event": "pass_start", "night": "2026-07-19"},
                  {"event": "pass_done", "night": "2026-07-19"}]
        phase, _, _ = mechanic.derive_phase(
            local(2026, 7, 19, 4, 0), self.CFG, events)
        self.assertEqual(phase, "done")

    def test_previous_night_events_do_not_block_tonight(self):
        events = [{"event": "pass_start", "night": "2026-07-18"},
                  {"event": "pass_done", "night": "2026-07-18"}]
        phase, _, _ = mechanic.derive_phase(
            local(2026, 7, 19, 2, 10), self.CFG, events)
        self.assertEqual(phase, "pass")

    def test_dry_run_events_ignored(self):
        events = [{"event": "dry_run", "night": "2026-07-19"}]
        phase, _, _ = mechanic.derive_phase(
            local(2026, 7, 19, 2, 10), self.CFG, events)
        self.assertEqual(phase, "pass")


class TestParseTs(unittest.TestCase):
    def test_iso_z(self):
        t = mechanic.parse_ts("2026-07-18T16:10:46Z")
        self.assertEqual(t.tzinfo is not None, True)
        self.assertEqual(t.year, 2026)

    def test_iso_offset(self):
        t = mechanic.parse_ts("2026-07-19T01:28:01.827388+00:00")
        self.assertEqual(t.hour, 1)

    def test_epoch_int(self):
        t = mechanic.parse_ts(1784401391)
        self.assertIsNotNone(t.tzinfo)
        self.assertEqual(t.year, 2026)

    def test_garbage_none(self):
        self.assertIsNone(mechanic.parse_ts("not a date"))
        self.assertIsNone(mechanic.parse_ts(None))


class TestRecentEntries(unittest.TestCase):
    def test_filters_by_age_and_skips_malformed(self):
        now = dt.datetime(2026, 7, 19, 1, 40, tzinfo=dt.timezone.utc)
        lines = [
            '{"ts":"2026-07-18T16:10:46Z","actor":"hub","kind":"activity","summary":"old enough"}',
            '{"ts":"2026-07-17T10:00:00Z","actor":"hub","kind":"activity","summary":"too old"}',
            '{"ts":1784401391,"actor":"spotter","kind":"error","summary":"epoch ts"}',
            'not json at all',
            '{"actor":"x","summary":"no ts"}',
        ]
        got = mechanic.recent_entries(lines, now, hours=24)
        self.assertEqual([e["summary"] for e in got], ["old enough", "epoch ts"])


class TestCli(unittest.TestCase):
    def run_cli(self, args, now, state_dir):
        env = dict(os.environ, MECHANIC_NOW=now, MECHANIC_STATE_DIR=state_dir)
        return subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, env=env)

    def test_windows_idle_outside(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli(["windows"], "2026-07-18T21:40:00-04:00", d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("phase=idle", r.stdout)
            self.assertIn("night=2026-07-18", r.stdout)

    def test_windows_pass_then_record_then_done(self):
        with tempfile.TemporaryDirectory() as d:
            now = "2026-07-19T02:30:00-04:00"
            r = self.run_cli(["windows"], now, d)
            self.assertIn("phase=pass", r.stdout)

            r = self.run_cli(["record", '{"event":"pass_start"}'], now, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_cli(["windows"], now, d)
            self.assertIn("phase=resume", r.stdout)

            # The nine-subsystem checklist stands between a started pass and a
            # finished one (a prior finding), so the lifecycle walks through it.
            for name in mechanic.SUBSYSTEMS:
                r = self.run_cli(["record", json.dumps(
                    {"event": "subsystem_check", "subsystem": name,
                     "result": "ok", "note": "nothing moved"})], now, d)
                self.assertEqual(r.returncode, 0, r.stderr)

            r = self.run_cli(
                ["record", '{"event":"pass_done","findings":2}'], now, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_cli(["windows"], now, d)
            self.assertIn("phase=done", r.stdout)

            # history.jsonl carries ts + night on every line
            hist = os.path.join(d, "history.jsonl")
            lines = [json.loads(l) for l in open(hist)]
            self.assertEqual(len(lines), 2 + len(mechanic.SUBSYSTEMS))
            for e in lines:
                self.assertIn("ts", e)
                self.assertEqual(e["night"], "2026-07-19")

    def test_record_requires_event(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli(["record", '{"nope":1}'],
                             "2026-07-19T02:30:00-04:00", d)
            self.assertNotEqual(r.returncode, 0)


class TestSplitSections(unittest.TestCase):
    def test_preamble_and_headings(self):
        text = "---\nname: x\n---\nintro\n\n# One\na\n## Two\nb\n"
        got = dict(mechanic.split_sections(text))
        self.assertEqual(list(got), ["(preamble)", "# One", "## Two"])
        self.assertIn("name: x", got["(preamble)"])
        self.assertIn("a", got["# One"])

    def test_repeated_titles_disambiguated(self):
        got = [k for k, _ in mechanic.split_sections("# A\n1\n# A\n2\n")]
        self.assertEqual(got, ["# A", "# A #2"])

    def test_keys_are_stable_when_a_section_is_inserted(self):
        before = dict(mechanic.split_sections("# A\na\n# C\nc\n"))
        after = dict(mechanic.split_sections("# A\na\n# B\nb\n# C\nc\n"))
        self.assertEqual(before["# A"], after["# A"])
        self.assertEqual(before["# C"], after["# C"])

    def test_deep_headings_are_body_not_sections(self):
        keys = [k for k, _ in mechanic.split_sections("# A\n#### deep\nx\n")]
        self.assertEqual(keys, ["# A"])


class TestPolicyPairDivergence(unittest.TestCase):
    """The CLAUDE.md/AGENTS.md pair is one policy file; a symlink turned into a
    diverging real file is drift."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        write(os.path.join(self.root, "CLAUDE.md"), "# root\npolicy\n")
        os.symlink("CLAUDE.md", os.path.join(self.root, "AGENTS.md"))
        write(os.path.join(self.root, "hub/CLAUDE.md"), "# hub\nhub policy\n")
        os.symlink("CLAUDE.md", os.path.join(self.root, "hub/AGENTS.md"))

    def digest(self, full=True):
        lines, files = mechanic.policy_digest(self.root, {}, full)
        return "\n".join(lines), files

    def test_symlinked_pair_is_one_file_no_drift(self):
        out, files = self.digest()
        # AGENTS.md is never hashed as its own policy file — the pair is one.
        self.assertIn("CLAUDE.md", files)
        self.assertNotIn("AGENTS.md", files)
        self.assertNotIn("hub/AGENTS.md", files)
        self.assertNotIn("POLICY PAIR DRIFT", out)

    def test_identical_real_copy_is_not_drift(self):
        # A real file that happens to match byte-for-byte is still one file.
        os.remove(os.path.join(self.root, "hub/AGENTS.md"))
        write(os.path.join(self.root, "hub/AGENTS.md"), "# hub\nhub policy\n")
        out, _ = self.digest()
        self.assertNotIn("POLICY PAIR DRIFT", out)

    def test_diverging_real_file_is_flagged(self):
        # Replace the symlink with a real file whose content differs.
        os.remove(os.path.join(self.root, "hub/AGENTS.md"))
        write(os.path.join(self.root, "hub/AGENTS.md"), "# hub\nDIVERGED\n")
        out, _ = self.digest()
        self.assertIn("POLICY PAIR DRIFT (1)", out)
        self.assertIn("hub/AGENTS.md", out)
        # The unrelated pair stays quiet; only the diverged one is named.
        self.assertNotIn("! AGENTS.md:", out)

    def test_symlink_repointed_elsewhere_is_flagged(self):
        os.remove(os.path.join(self.root, "hub/AGENTS.md"))
        write(os.path.join(self.root, "hub/OTHER.md"), "# other\n")
        os.symlink("OTHER.md", os.path.join(self.root, "hub/AGENTS.md"))
        out, _ = self.digest()
        self.assertIn("POLICY PAIR DRIFT (1)", out)
        self.assertIn("no longer resolves", out)

    def test_no_agents_sibling_is_not_drift(self):
        os.remove(os.path.join(self.root, "hub/AGENTS.md"))
        out, _ = self.digest()
        self.assertNotIn("POLICY PAIR DRIFT", out)


class TestHubConfig(unittest.TestCase):
    def test_empty_registry_is_anonymous(self):
        self.assertEqual(mechanic.hub_config({}), {
            "persona": "ops", "session": "ops (hub)", "deck_profile": "ops"})

    def test_config_and_environment_override_defaults(self):
        registry = {"hub": {"persona": "the keeper",
                            "session_title": "control (hub)",
                            "deck_profile": "configured"}}
        self.assertEqual(mechanic.hub_config(registry)["session"], "control (hub)")
        with mock.patch.dict(os.environ, {"MECHANIC_DECK_PROFILE": "override"}):
            self.assertEqual(mechanic.hub_config(registry)["deck_profile"],
                             "override")


class TestParseInterval(unittest.TestCase):
    def test_units(self):
        self.assertEqual(mechanic.parse_interval("20m"), 1200)
        self.assertEqual(mechanic.parse_interval("90s"), 90)
        self.assertEqual(mechanic.parse_interval("2h"), 7200)

    def test_unparseable_is_none(self):
        for v in ["on-demand", "", None, "20", "1d", 20]:
            self.assertIsNone(mechanic.parse_interval(v))


class TestReadSince(unittest.TestCase):
    def test_reads_only_the_tail_and_advances(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "l.jsonl")
            with open(p, "w") as f:
                f.write("a\nb\n")
            lines, off, ok = mechanic.read_since(p, 0)
            self.assertEqual((lines, ok), (["a", "b"], True))
            with open(p, "a") as f:
                f.write("c\n")
            lines, off2, ok = mechanic.read_since(p, off)
            self.assertEqual((lines, ok), (["c"], True))
            self.assertGreater(off2, off)

    def test_shrunk_file_reports_unusable_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "l.jsonl")
            with open(p, "w") as f:
                f.write("a\n")
            _, _, ok = mechanic.read_since(p, 999)
            self.assertFalse(ok)


class TestLedgerDetailCap(unittest.TestCase):
    def test_long_detail_capped_other_fields_verbatim(self):
        e = {"ts": "2026-07-19T01:00:00Z", "actor": "hub", "kind": "activity",
             "summary": "keep me", "detail": "x" * 5000}
        got = json.loads(mechanic.render_ledger_entry(e))
        self.assertEqual(got["summary"], "keep me")
        self.assertLess(len(got["detail"]), 500)
        self.assertIn("(+4700 chars)", got["detail"])
        self.assertEqual(len(e["detail"]), 5000)  # input not mutated


# --------------------------------------------------------------------------- #
# end-to-end digest: a miniature estate on disk, gathered across nights
# --------------------------------------------------------------------------- #
NOW_N1 = "2026-07-19T02:30:00-04:00"
NOW_N2 = "2026-07-20T02:30:00-04:00"

LOOPS_TOML = """
[hub]
slack_channel_id = "example-channel"

[loops.demo]
dir = "loops/demo"
skill = "demo"
interval = "20m"
autostart = true
persona = "the demo"
model = "model-standard"
"""

# The point of the digest is that big manuals stop being re-read, so the demo
# skill has to be manual-sized for a size comparison to mean anything.
FILLER = "\n".join(f"filler line {i} — padding this manual out" for i in range(60))
DEMO_SKILL = f"# demo skill\n## Alpha\nalpha body\n{FILLER}\n## Beta\nbeta body\n"

DECK = {
    "ops (hub)": {"title": "ops (hub)", "status": "waiting",
                        "model_id": "model-standard"},
    "the demo (demo)": {"title": "the demo (demo)", "status": "waiting",
                        "model_id": "model-standard"},
}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TestGatherDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        self.state = os.path.join(self.tmp.name, "mstate")
        os.makedirs(self.state)
        write(os.path.join(self.root, "loops.toml"), LOOPS_TOML)
        write(os.path.join(self.root, "CLAUDE.md"), "# root\npolicy\n")
        write(os.path.join(self.root, "hub/CLAUDE.md"), "# hub\nhub policy\n")
        write(os.path.join(self.root, "hub/.claude/skills/hub/SKILL.md"),
              "# hub skill\nsteps\n")
        write(os.path.join(self.root, "loops/demo/CLAUDE.md"), "# demo\nd\n")
        write(os.path.join(self.root,
                           "loops/demo/.claude/skills/demo/SKILL.md"),
              DEMO_SKILL)
        write(os.path.join(self.root, "docs/lessons.md"), "# lessons\nl\n")
        write(os.path.join(self.root, "docs/ideas.md"), "# ideas\ni\n")
        # heartbeats fresh relative to the frozen clock, so nothing drifts
        epoch = int(dt.datetime.fromisoformat(NOW_N1).timestamp())
        for name in ("hub", "demo"):
            write(os.path.join(self.root, "state", name, "last_tick"),
                  str(epoch))
        self.ledger = os.path.join(self.root, "state", "ledger.jsonl")
        write(self.ledger, json.dumps(
            {"ts": "2026-07-19T01:00:00Z", "actor": "hub",
             "kind": "activity", "summary": "first"}) + "\n")

    def gather(self, now, **kw):
        env = {"MECHANIC_NOW": now, "MECHANIC_REPO_ROOT": self.root,
               "MECHANIC_STATE_DIR": self.state}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mechanic, "deck_sessions",
                                  return_value=(DECK, "")), \
                mock.patch.object(mechanic, "_run",
                                  return_value=(1, "not a git repo")), \
                contextlib.redirect_stdout(buf):
            rc = mechanic.cmd_gather(mechanic.load_config(), **kw)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def snapshot(self):
        with open(os.path.join(self.state, "digest.json")) as f:
            return json.load(f)

    def test_cold_run_is_full_and_writes_a_snapshot(self):
        out = self.gather(NOW_N1)
        self.assertIn("baseline: none (cold run)", out)
        self.assertIn("alpha body", out)          # full policy text
        self.assertIn("beta body", out)
        self.assertIn('"summary": "first"', out)  # 24h ledger fallback
        snap = self.snapshot()
        self.assertEqual(snap["night"], "2026-07-19")
        self.assertEqual(snap["base"], {})
        self.assertIn("docs/lessons.md", snap["current"]["files"])

    def test_unchanged_next_night_is_materially_smaller(self):
        cold = self.gather(NOW_N1)
        warm = self.gather(NOW_N2)
        self.assertIn("unchanged since baseline (7)", warm)
        self.assertNotIn("alpha body", warm)
        self.assertIn("0 new since baseline", warm)
        self.assertLess(len(warm), len(cold) / 3)

    def test_same_night_regather_reuses_the_baseline(self):
        self.gather(NOW_N1)
        first = self.snapshot()
        again = self.gather(NOW_N1)
        # A resume tick sees the same digest, and the baseline is not advanced.
        self.assertEqual(self.snapshot()["generated_at"],
                         first["generated_at"])
        self.assertIn("alpha body", again)

    def test_only_changed_sections_are_printed(self):
        self.gather(NOW_N1)
        write(os.path.join(self.root,
                           "loops/demo/.claude/skills/demo/SKILL.md"),
              DEMO_SKILL.replace("beta body", "BETA REWRITTEN"))
        out = self.gather(NOW_N2)
        self.assertIn("BETA REWRITTEN", out)
        self.assertNotIn("alpha body", out)
        self.assertIn("CHANGED (1 of 3 sections)", out)
        self.assertIn("unchanged since baseline (6)", out)

    def test_removed_section_is_reported(self):
        self.gather(NOW_N1)
        write(os.path.join(self.root,
                           "loops/demo/.claude/skills/demo/SKILL.md"),
              DEMO_SKILL.split("## Beta")[0])
        out = self.gather(NOW_N2)
        self.assertIn("removed sections: ## Beta", out)

    def test_ledger_shows_only_new_entries(self):
        self.gather(NOW_N1)
        with open(self.ledger, "a") as f:
            f.write(json.dumps({"ts": "2026-07-20T01:00:00Z", "actor": "demo",
                                "kind": "error", "summary": "second"}) + "\n")
        out = self.gather(NOW_N2)
        self.assertIn("1 new since baseline", out)
        self.assertIn("second", out)
        self.assertNotIn("first", out)
        self.assertIn("demo: error=1", out)

    def test_truncated_ledger_falls_back_to_the_time_window(self):
        self.gather(NOW_N1)
        write(self.ledger, "")  # rotated out from under the cursor
        out = self.gather(NOW_N2)
        self.assertIn("last 24h, 0 lines", out)

    def test_registry_and_model_drift(self):
        self.gather(NOW_N1)
        write(os.path.join(self.root, "loops.toml"),
              LOOPS_TOML.replace("model-standard", "model-economy"))
        out = self.gather(NOW_N2)
        self.assertIn("[CHANGED since baseline", out)
        self.assertIn("loops.toml model=model-economy but live "
                      "model=model-standard", out)

    def test_state_size_delta_only_for_changed_files(self):
        self.gather(NOW_N1)
        with open(self.ledger, "a") as f:
            f.write('{"ts":"2026-07-20T01:00:00Z","actor":"x","kind":"activity","summary":"s"}\n')
        out = self.gather(NOW_N2)
        self.assertRegex(out, r"ledger\.jsonl [\d.]+K \(\+[\d.]+K\)")
        self.assertIn("state files unchanged since baseline", out)

    def test_full_flag_ignores_the_baseline(self):
        self.gather(NOW_N1)
        out = self.gather(NOW_N2, full=True)
        self.assertIn("baseline: ignored (--full)", out)
        self.assertIn("alpha body", out)

    def test_no_save_leaves_the_snapshot_alone(self):
        self.gather(NOW_N1)
        before = self.snapshot()
        self.gather(NOW_N2, save=False)
        self.assertEqual(self.snapshot(), before)

    # --- pace override ------------------------------------------------------
    # A loop the operator deliberately took off its registry cadence records that in
    # state/<name>/pace-override. Before this, the heartbeat check judged it
    # against loops.toml anyway and filed a drift line every night for a loop
    # that was doing exactly what he asked.

    def stale_demo(self, override=None):
        """Heartbeat 2848s old — stale at 20m, fine at 60m."""
        epoch = int(dt.datetime.fromisoformat(NOW_N1).timestamp()) - 2848
        write(os.path.join(self.root, "state", "demo", "last_tick"), str(epoch))
        if override is not None:
            write(os.path.join(self.root, "state", "demo", "pace-override"),
                  override + "\n")

    def test_a_stale_loop_with_no_override_still_drifts(self):
        self.stale_demo()
        out = self.gather(NOW_N1)
        self.assertIn("demo: heartbeat 2848s old > 2520s (2*20m+120)", out)

    def test_an_override_moves_the_threshold_it_names(self):
        self.stale_demo("60m")
        out = self.gather(NOW_N1)
        self.assertIn("pace-override=60m", out)
        self.assertIn("registry=20m", out)
        self.assertNotIn("demo: heartbeat", out)   # no drift line

    def test_an_overridden_loop_still_drifts_against_its_own_pace(self):
        self.stale_demo("5m")
        out = self.gather(NOW_N1)
        self.assertIn("demo: heartbeat 2848s old > 720s (2*5m+120)", out)

    def test_dynamic_buys_no_heartbeat_verdict_at_all(self):
        self.stale_demo("dynamic")
        out = self.gather(NOW_N1)
        self.assertIn("[off-registry: pace-override=dynamic", out)
        self.assertNotIn("demo: heartbeat", out)

    def test_an_unreadable_override_is_itself_the_drift(self):
        self.stale_demo("600")   # seconds — state/hub/pace's format, not this one
        out = self.gather(NOW_N1)
        self.assertIn("demo: unreadable pace-override '600'", out)
        self.assertNotIn("demo: heartbeat 2848s", out)

    def test_the_off_registry_note_rides_every_pass_not_only_stale_ones(self):
        """setUp's heartbeat is fresh; the note shows up anyway."""
        write(os.path.join(self.root, "state", "demo", "pace-override"), "60m\n")
        lines = self.gather(NOW_N1).splitlines()
        start = lines.index("== heartbeats ==")
        line = next(ln for ln in lines[start:] if ln.startswith("demo: "))
        self.assertIn("off-registry", line)
        self.assertIn("ago", line)

    def test_missing_session_is_drift(self):
        env = {"MECHANIC_NOW": NOW_N1, "MECHANIC_REPO_ROOT": self.root,
               "MECHANIC_STATE_DIR": self.state}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mechanic, "deck_sessions",
                                  return_value=({}, "")), \
                mock.patch.object(mechanic, "_run", return_value=(1, "no git")), \
                contextlib.redirect_stdout(buf):
            mechanic.cmd_gather(mechanic.load_config())
        self.assertIn("MISSING from agent-deck", buf.getvalue())
        self.assertIn("no agent-deck session titled 'the demo (demo)'",
                      buf.getvalue())

    def test_non_autostart_loop_absence_is_not_drift(self):
        # An autostart=false (on-demand) loop legitimately has no live session:
        # its absence must be named "expected absent", never MISSING/drift.
        # An autostart=true loop missing its session must STILL be flagged.
        write(os.path.join(self.root, "loops.toml"), LOOPS_TOML + (
            "\n[loops.ondemand]\n"
            'dir = "loops/ondemand"\n'
            'skill = "ondemand"\n'
            'interval = "on-demand"\n'
            "autostart = false\n"
            'persona = "the gauges"\n'
            'model = "model-standard"\n'))
        # Deck has only the hub — both loops are absent from agent-deck.
        deck = {"ops (hub)": {"title": "ops (hub)",
                                    "status": "waiting",
                                    "model_id": "model-standard"}}
        env = {"MECHANIC_NOW": NOW_N1, "MECHANIC_REPO_ROOT": self.root,
               "MECHANIC_STATE_DIR": self.state}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mechanic, "deck_sessions",
                                  return_value=(deck, "")), \
                mock.patch.object(mechanic, "_run",
                                  return_value=(1, "no git")), \
                contextlib.redirect_stdout(buf):
            mechanic.cmd_gather(mechanic.load_config())
        out = buf.getvalue()
        # the on-demand loop is named as expected-absent, never as drift
        self.assertIn(
            "the gauges (ondemand): expected absent (autostart=false)", out)
        self.assertNotIn("the gauges (ondemand): MISSING", out)
        self.assertNotIn(
            "no agent-deck session titled 'the gauges (ondemand)'", out)
        # the autostart=true loop missing its session is STILL flagged
        self.assertIn("the demo (demo): MISSING from agent-deck", out)
        self.assertIn("no agent-deck session titled 'the demo (demo)'", out)

    def test_corrupt_snapshot_degrades_to_a_cold_run(self):
        self.gather(NOW_N1)
        write(os.path.join(self.state, "digest.json"), "{not json")
        out = self.gather(NOW_N2)
        self.assertIn("baseline: none (cold run)", out)


# --------------------------------------------------------------------------- #
# scope-safe memory recall (the consistency contract)
# --------------------------------------------------------------------------- #
class TestMemoryRecall(unittest.TestCase):
    """The mechanic's half of a prior finding, against a REAL throwaway estate store
    driven by the real `bin/estate` — the whole point of the section is that
    the pass reads memory only through that CLI, so a mocked store would test
    the wrong thing. ESTATE_STATE_DIR keeps it off real state."""

    ESTATE = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.pardir, "bin", "estate"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        self.state = os.path.join(self.tmp.name, "mstate")
        self.estate_dir = os.path.join(self.tmp.name, "estate")
        os.makedirs(self.state)
        os.makedirs(self.estate_dir)
        write(os.path.join(self.root, "loops.toml"), LOOPS_TOML)
        write(os.path.join(self.root, "state", "ledger.jsonl"), "")

    def estate(self, *argv):
        r = subprocess.run([sys.executable, self.ESTATE, *argv],
                           capture_output=True, text=True,
                           env=dict(os.environ,
                                    ESTATE_STATE_DIR=self.estate_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def remember(self, body, scope, refs=None):
        argv = ["memory", "add", body, "--scope", scope, "--status", "active"]
        if refs:
            argv += ["--source-refs", refs]
        return self.estate(*argv)

    def env(self, estate_script=None):
        return {"MECHANIC_NOW": NOW_N1, "MECHANIC_REPO_ROOT": self.root,
                "MECHANIC_STATE_DIR": self.state,
                "ESTATE_STATE_DIR": self.estate_dir,
                "ESTATE_SCRIPT": estate_script or self.ESTATE}

    def gather(self, estate_script=None, now=NOW_N1, **kw):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env(estate_script),
                                              MECHANIC_NOW=now)), \
                mock.patch.object(mechanic, "deck_sessions",
                                  return_value=(DECK, "")), \
                mock.patch.object(mechanic, "_run",
                                  return_value=(1, "not a git repo")), \
                contextlib.redirect_stdout(buf):
            rc = mechanic.cmd_gather(mechanic.load_config(), **kw)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        return out[out.index("== memory "):].split("\n== ")[0]

    def record(self, event):
        return subprocess.run(
            [sys.executable, SCRIPT, "record", json.dumps(event)],
            capture_output=True, text=True,
            env=dict(os.environ, **self.env()))

    def pass_start_refs(self):
        rows = json.loads(self.estate("events", "--subsystem", "mechanic",
                                      "--phase", "initiation", "--json"))
        self.assertEqual(len(rows), 1, rows)
        return json.loads(rows[0]["refs"])

    def test_the_section_carries_local_and_shared_facts_with_their_scopes(self):
        self.remember("every loop obeys this", "shared", refs='{"ledger":[1]}')
        self.remember("only the mechanic", "mechanic")
        out = self.gather()
        self.assertIn("2 active fact(s) at scope mechanic + shared", out)
        self.assertIn("m-2  [mechanic]  lesson", out)
        self.assertIn("m-1  [shared]  lesson", out)
        self.assertIn("only the mechanic", out)
        self.assertIn("every loop obeys this", out)

    def test_another_loops_fact_never_reaches_the_pass(self):
        """S35/S38 at the consumer, not just at the CLI."""
        self.remember("the night shift's own", "night-shift")
        out = self.gather()
        self.assertIn("no active memories at scope mechanic or shared", out)
        self.assertNotIn("night shift's own", out)

    def test_a_candidate_is_not_a_fact_this_pass_may_act_on(self):
        self.estate("memory", "add", "unjudged", "--scope", "mechanic")
        self.assertIn("no active memories", self.gather())

    def test_a_fact_read_at_the_baseline_collapses_but_keeps_its_label(self):
        self.remember("only the mechanic", "mechanic")
        self.gather()                       # cold: printed in full
        warm = self.gather(now=NOW_N2)
        self.assertIn("unchanged since baseline (1)", warm)
        self.assertIn("m-1[mechanic]", warm)
        self.assertNotIn("only the mechanic\n", warm)

    def test_a_new_fact_prints_in_full_on_a_warm_pass(self):
        self.remember("known already", "mechanic")
        self.gather()
        self.remember("learned since", "mechanic")
        warm = self.gather(now=NOW_N2)
        self.assertIn("[new since baseline]", warm)
        self.assertIn("learned since", warm)

    def test_an_unreachable_store_is_a_fact_about_the_machine(self):
        out = self.gather(estate_script=os.path.join(self.tmp.name, "gone"))
        self.assertIn("memory store unavailable", out)
        self.assertIn("not a finding about the estate", out)

    def test_pass_start_records_the_ids_and_scopes_it_recalled(self):
        self.remember("every loop obeys this", "shared", refs='{"ledger":[1]}')
        self.remember("only the mechanic", "mechanic")
        r = self.record({"event": "pass_start"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pass_start_refs()["memory_recall"],
                         {"scope": "mechanic",
                          "memories": ["m-2@mechanic", "m-1@shared"]})

    def test_pass_start_records_an_empty_recall_rather_than_nothing(self):
        """"The estate had nothing for me" and "recall never ran" have to stay
        distinguishable on the row itself."""
        self.assertEqual(self.record({"event": "pass_start"}).returncode, 0)
        self.assertEqual(self.pass_start_refs()["memory_recall"],
                         {"scope": "mechanic", "memories": []})

    def test_only_the_boundary_that_begins_the_pass_carries_the_facts(self):
        """A terminal boundary is about what the night DID. Repeating the fact
        set on it would say nothing new, and would invite a reader to think the
        two lists were compared."""
        self.remember("only the mechanic", "mechanic")
        self.assertEqual(self.record({"event": "pass_start"}).returncode, 0)
        r = self.record({"event": "pass_failed", "reason": "interrupted"})
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(self.estate("events", "--subsystem", "mechanic",
                                      "--phase", "failure", "--json"))
        self.assertEqual(len(rows), 1, rows)
        self.assertNotIn("memory_recall", json.loads(rows[0]["refs"]))


# --------------------------------------------------------------------------- #
# the EXTRACTION lens — allowlist parsing and the three-bucket diff
# --------------------------------------------------------------------------- #
ALLOWLIST_MD = """# The allowlist — what belongs in the public core

## The rule

**Mechanism is public. Identity, policy, and data are not.**

## The invariant

| Never committed | Where it lives instead |
|---|---|
| `state/` in any form — cursor, ledger | gitignored; runtime only |
| A real `loops.toml` (real loops, real channel) | gitignored; `loops.example.toml` is the committed documented form |
| Absolute personal paths (`/home/<user>/…`) | resolved at runtime |

## What is in, and why

| Component | Why it is core |
|---|---|
| `bin/ops` | the operator CLI. |
| `bin/doorbell` | the responsiveness path. |
| `hub/` | the hub session's home and its tick skill. |
| `loops/example/` | the loop contract as a copyable template. |
| `loops.example.toml` | the registry's documented shape. |
| tests next to their scripts | they run against tempdirs. |

## What is deliberately out

- **The loops themselves.** The *shape* of a loop is core (`loops/<name>/` =
  CLAUDE.md + skill, registered in `loops.toml`); any particular loop is not.
  `loops/example/` is that shape as a copyable template and is core.
- **`docs/lessons.md`.** Distilled from one estate's ledger.
"""


class TestParseAllowlist(unittest.TestCase):
    def setUp(self):
        self.include, self.exclude = mechanic.parse_allowlist(ALLOWLIST_MD)

    def test_include_comes_from_the_in_table(self):
        self.assertEqual(
            self.include,
            ["bin/doorbell", "bin/ops", "hub/", "loops.example.toml",
             "loops/example/"])

    def test_exclude_comes_from_invariant_and_out(self):
        self.assertIn("state/", self.exclude)
        self.assertIn("loops.toml", self.exclude)
        self.assertIn("docs/lessons.md", self.exclude)

    def test_second_table_column_is_not_scanned(self):
        # `loops.example.toml` appears in the invariant table's SECOND column
        # as "where it lives instead" — scanning it would exclude a core file.
        self.assertNotIn("loops.example.toml", self.exclude)

    def test_the_in_table_wins_over_a_prose_mention(self):
        # `loops/example/` is named in "deliberately out" as a cross-reference
        # while being explicitly core.
        self.assertIn("loops/example/", self.include)
        self.assertNotIn("loops/example/", self.exclude)

    def test_placeholder_paths_are_not_patterns(self):
        for tok in ("loops/<name>/", "/home/<user>/…"):
            self.assertNotIn(tok, self.include + self.exclude)

    def test_empty_document_yields_nothing(self):
        self.assertEqual(mechanic.parse_allowlist(""), ([], []))


class TestExcludedBy(unittest.TestCase):
    def test_exact_and_prefix_matches(self):
        ex = ["state/", "loops.toml"]
        self.assertEqual(mechanic.excluded_by("loops.toml", ex), "loops.toml")
        self.assertEqual(mechanic.excluded_by("state/hub/cursor", ex), "state/")
        self.assertEqual(mechanic.excluded_by("state", ex), "state/")
        self.assertIsNone(mechanic.excluded_by("bin/ops", ex))

    def test_prefix_does_not_match_a_sibling_with_a_shared_stem(self):
        self.assertIsNone(mechanic.excluded_by("stateful/x", ["state/"]))


class TestExtractionDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "private fork")
        self.core = os.path.join(self.tmp.name, "public core")
        # The allowlist lives in the fork (self.root), not in the core.
        write(os.path.join(self.root, "docs/extraction-allowlist.md"),
              ALLOWLIST_MD)
        write(os.path.join(self.root, "bin/ops"), "operator cli\n")
        write(os.path.join(self.root, "bin/test_ops.py"), "tests\n")
        write(os.path.join(self.root, "bin/doorbell"), "poller\n")
        write(os.path.join(self.root, "hub/CLAUDE.md"), "# hub\n")
        os.symlink("CLAUDE.md", os.path.join(self.root, "hub/AGENTS.md"))
        write(os.path.join(self.root, "loops.toml"), "# real registry\n")
        write(os.path.join(self.root, "docs/lessons.md"), "# lessons\n")
        write(os.path.join(self.root, "state/hub/cursor"), "1\n")
        self.registry = {"extraction": {
            "repo": self.core,
            "allowlist": "docs/extraction-allowlist.md"}}

    def run_lens(self, prev=None, full=False):
        lines, snap = mechanic.extraction_digest(
            self.root, self.registry, prev or {}, full)
        return "\n".join(lines), snap

    def store(self, items):
        write(os.path.join(self.root, "state/extraction/candidates.json"),
              json.dumps({"items": items}))

    # -- bucket (a) ------------------------------------------------------- #
    def test_bucket_a_lists_unextracted_allowlisted_paths(self):
        out, snap = self.run_lens()
        self.assertIn("(a) new & matching, never extracted — 4", out)
        for rel in ("bin/ops", "bin/doorbell", "hub/CLAUDE.md"):
            self.assertIn(rel, out)
        self.assertEqual(sorted(snap["paths"]),
                         ["bin/doorbell", "bin/ops", "bin/test_ops.py",
                          "hub/CLAUDE.md"])

    def test_tests_next_to_their_scripts_are_pulled_in(self):
        # The allowlist states that rule in prose with no path to grep for.
        out, _ = self.run_lens()
        self.assertIn("bin/test_ops.py", out)

    def test_symlink_alias_is_folded_into_its_target(self):
        out, snap = self.run_lens()
        self.assertIn("symlink aliases folded into their targets: "
                      "hub/AGENTS.md", out)
        self.assertNotIn("hub/AGENTS.md  sha", out)
        self.assertNotIn("hub/AGENTS.md", snap["paths"])

    # -- bucket (b) ------------------------------------------------------- #
    def test_bucket_b_is_drift_against_the_synced_hashes(self):
        self.store({"extraction:1": {
            "id": "extraction:1", "status": "synced", "title": "doorbell",
            "source_paths": ["bin/doorbell"], "synced_at": "2026-07-23T00:00:00Z",
            "synced_hashes": {"bin/doorbell": "staleaaaaaaa"}}})
        out, _ = self.run_lens()
        self.assertIn("(b) already extracted, CHANGED since (drift) — 1", out)
        self.assertIn("bin/doorbell  extraction:1 synced", out)
        self.assertIn("staleaaaaaaa → ", out)

    def test_a_synced_path_that_matches_is_in_sync_not_a_candidate(self):
        sha = mechanic.file_sha(os.path.join(self.root, "bin/doorbell"))
        self.store({"extraction:1": {
            "id": "extraction:1", "status": "synced", "title": "doorbell",
            "source_paths": ["bin/doorbell"], "synced_at": "2026-07-23T00:00:00Z",
            "synced_hashes": {"bin/doorbell": sha}}})
        out, _ = self.run_lens()
        self.assertIn("in sync (extracted, unchanged): 1", out)
        self.assertIn("(a) new & matching, never extracted — 3", out)

    # -- bucket (c) ------------------------------------------------------- #
    def test_bucket_c_names_exclusions_instead_of_dropping_them(self):
        out, snap = self.run_lens()
        self.assertIn("(c) explicitly excluded (private-only)", out)
        self.assertIn("loops.toml", out)
        self.assertIn("state/", out)
        self.assertIn("docs/lessons.md", out)
        for rel in ("loops.toml", "docs/lessons.md"):
            self.assertNotIn(rel, snap["paths"])

    def test_public_core_only_shapes_are_reported_as_absent(self):
        out, _ = self.run_lens()
        self.assertIn("public-core shape only, no counterpart here: "
                      "loops.example.toml, loops/example/", out)

    # -- durable candidates ----------------------------------------------- #
    def test_open_candidates_are_carried_forward_without_a_source_change(self):
        self.store({"extraction:2": {
            "id": "extraction:2", "status": "approved", "title": "port bin/ops",
            "source_paths": ["bin/ops"], "created_at": "2026-07-23T00:00:00Z"}})
        first, snap = self.run_lens()
        self.assertIn("open candidates carried forward — 1", first)
        self.assertIn("extraction:2 [approved] port bin/ops", first)
        # Nothing changed on disk; the candidate must still be surfaced, and
        # its path must not collapse into the quiet steady list.
        second, _ = self.run_lens(prev=snap)
        self.assertIn("extraction:2 [approved] port bin/ops", second)
        self.assertIn("bin/ops  (already filed as extraction:2 [approved])",
                      second)

    def test_resting_candidates_are_not_carried_forward(self):
        self.store({"extraction:3": {
            "id": "extraction:3", "status": "rejected", "title": "nope",
            "source_paths": ["bin/ops"], "created_at": "2026-07-23T00:00:00Z"}})
        out, _ = self.run_lens()
        self.assertIn("open candidates carried forward — 0", out)

    def test_missing_store_is_not_an_error(self):
        out, _ = self.run_lens()
        self.assertIn("open candidates carried forward — 0", out)

    def test_corrupt_store_degrades_to_empty(self):
        write(os.path.join(self.root, "state/extraction/candidates.json"),
              "{not json")
        out, _ = self.run_lens()
        self.assertIn("open candidates carried forward — 0", out)

    # -- incremental behaviour -------------------------------------------- #
    def test_unchanged_paths_collapse_against_a_baseline(self):
        first, snap = self.run_lens()
        second, _ = self.run_lens(prev=snap)
        self.assertIn("(a) new & matching, never extracted — 0", second)
        self.assertIn("unchanged since baseline, still unextracted (4)", second)
        self.assertIn("bin/ops", second)          # named, never dropped
        # One line for all four, instead of one per-path sha line each. (The
        # byte saving only shows at estate scale — four short paths fit in
        # less text than the label that collapses them.)
        for rel in ("bin/ops", "bin/doorbell", "bin/test_ops.py",
                    "hub/CLAUDE.md"):
            self.assertIn(f"{rel}  sha ", first)
            self.assertNotIn(f"{rel}  sha ", second)

    def test_a_changed_path_resurfaces_in_full(self):
        _, snap = self.run_lens()
        write(os.path.join(self.root, "bin/ops"), "operator cli, now better\n")
        out, _ = self.run_lens(prev=snap)
        self.assertIn("bin/ops  sha", out)
        self.assertIn("[CHANGED since baseline]", out)

    def test_a_new_path_resurfaces_in_full(self):
        _, snap = self.run_lens()
        write(os.path.join(self.root, "hub/.claude/skills/hub/SKILL.md"), "# s\n")
        out, _ = self.run_lens(prev=snap)
        self.assertIn("hub/.claude/skills/hub/SKILL.md", out)
        self.assertIn("[new since baseline]", out)

    def test_allowlist_change_is_flagged(self):
        _, snap = self.run_lens()
        self.assertIn("(unchanged)", self.run_lens(prev=snap)[0])
        write(os.path.join(self.root, "docs/extraction-allowlist.md"),
              ALLOWLIST_MD + "\n- one more rule\n")
        out, _ = self.run_lens(prev=snap)
        self.assertIn("[CHANGED since baseline — re-read the allowlist]", out)

    def test_full_suppresses_baseline_markers(self):
        _, snap = self.run_lens()
        out, _ = self.run_lens(prev=snap, full=True)
        self.assertIn("(a) new & matching, never extracted — 4", out)
        self.assertNotIn("since baseline", out)

    # -- degradation ------------------------------------------------------ #
    def test_unconfigured_extraction_is_a_note_not_a_crash(self):
        lines, snap = mechanic.extraction_digest(self.root, {}, {}, False)
        self.assertIn("not configured", lines[0])
        self.assertEqual(snap, {})

    def test_missing_allowlist_is_a_note_not_a_finding(self):
        self.registry["extraction"]["allowlist"] = "docs/gone.md"
        lines, snap = self.run_lens()
        self.assertIn("allowlist unreadable", lines)
        self.assertEqual(snap, {})

    def test_missing_checkout_does_not_disable_the_lens(self):
        # The allowlist lives here, not in the core, so the lens still resolves
        # every pattern against this repo when the checkout is gone.
        self.registry["extraction"]["repo"] = os.path.join(self.tmp.name, "gone")
        out, snap = self.run_lens()
        self.assertNotIn("allowlist unreadable", out)
        self.assertIn("bin/ops", out)
        self.assertTrue(snap["paths"])

    def test_lens_is_read_only(self):
        before = sorted(os.listdir(os.path.join(self.root, "state")))
        self.run_lens()
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "state"))),
                         before)


class TestPropose(unittest.TestCase):
    """Gap audit a prior finding — a proposal becomes a durable estate task, and the
    history line that records it carries the same task id.

    The pairing is the point. Before this, REPORT.md was the only record and
    the next pass overwrote it, so a recurring condition arrived looking new
    every night. A finding line and an estate row that can disagree would be
    the same defect with extra steps.
    """
    ESTATE = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.pardir, "bin", "estate"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mech = os.path.join(self.tmp.name, "mechanic")
        self.estate_dir = os.path.join(self.tmp.name, "estate")
        os.makedirs(self.mech)
        os.makedirs(self.estate_dir)

    def run_cli(self, args, now="2026-07-30T02:30:00-04:00"):
        env = dict(os.environ, MECHANIC_NOW=now, MECHANIC_STATE_DIR=self.mech,
                   ESTATE_STATE_DIR=self.estate_dir, ESTATE_SCRIPT=self.ESTATE)
        return subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, env=env)

    def estate(self, *argv):
        return subprocess.run(
            [sys.executable, self.ESTATE, *argv], capture_output=True, text=True,
            env=dict(os.environ, ESTATE_STATE_DIR=self.estate_dir))

    def history(self):
        path = os.path.join(self.mech, "history.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def payload(**over):
        """A complete a prior finding proposal. Every field below is required by
        `propose`, so a test that only cares about one of them still has to
        carry the rest — the same as the pass does."""
        base = {"title": "a condition", "subsystem": "hub",
                "condition": "a condition", "desired_outcome": "it stops",
                "completion_check": "a week without it"}
        base.update(over)
        return json.dumps(base)

    def test_files_a_task_and_a_matching_finding_line(self):
        r = self.run_cli(["propose", self.payload(
            title="the dispatch store loses rows",
            condition="dispatches.json loses rows between ticks",
            **{"class": "scriptability"})])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("new", r.stdout)
        task = r.stdout.split()[0]
        self.assertTrue(task.startswith("t-"), r.stdout)
        line = self.history()[-1]
        self.assertEqual(line["event"], "finding")
        self.assertEqual(line["action"], "proposed")
        self.assertEqual(line["task"], task)
        self.assertEqual(line["disposition"], "new")
        self.assertEqual(line["class"], "scriptability")
        self.assertEqual(line["night"], "2026-07-30")

    def test_a_proposal_starts_pending_review_never_approved(self):
        self.run_cli(["propose", self.payload()])
        out = self.estate("proposal", "list").stdout
        self.assertIn("(pending-review)", out)
        self.assertEqual(self.estate("ready").stdout.strip(), "")

    def test_an_equivalent_condition_is_recorded_as_a_recurrence(self):
        first = self.run_cli(["propose", self.payload()])
        again = self.run_cli(["propose", self.payload(
            title="A  Condition!", condition="A  Condition!")])
        self.assertEqual(first.stdout.split()[0], again.stdout.split()[0])
        self.assertIn("recurrence", again.stdout)
        self.assertEqual(self.history()[-1]["disposition"], "recurrence")

    def test_condition_can_be_pinned_apart_from_the_title(self):
        a = self.run_cli(["propose", self.payload(
            title="night 1 wording", condition="the shared condition")])
        b = self.run_cli(["propose", self.payload(
            title="night 2 wording", condition="the shared condition")])
        self.assertEqual(a.stdout.split()[0], b.stdout.split()[0])

    def test_payload_must_carry_a_title(self):
        self.assertNotEqual(self.run_cli(["propose", '{"class":"cost"}']).returncode, 0)
        self.assertNotEqual(self.run_cli(["propose", "not json"]).returncode, 0)

    def test_nothing_is_recorded_when_the_estate_call_fails(self):
        # A finding line naming a task id that does not exist is worse than a
        # pass that stops: the next pass would trust it.
        env_script = os.path.join(self.tmp.name, "missing-estate")
        r = subprocess.run(
            [sys.executable, SCRIPT, "propose", self.payload()],
            capture_output=True, text=True,
            env=dict(os.environ, MECHANIC_NOW="2026-07-30T02:30:00-04:00",
                     MECHANIC_STATE_DIR=self.mech, ESTATE_SCRIPT=env_script,
                     ESTATE_STATE_DIR=self.estate_dir))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.history(), [])

    def test_proposals_lists_what_is_on_file(self):
        self.run_cli(["propose", self.payload()])
        r = self.run_cli(["proposals"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a condition", r.stdout)

    def test_proposals_is_quiet_when_nothing_is_on_file(self):
        r = self.run_cli(["proposals"])
        self.assertEqual(r.returncode, 0, r.stderr)

    # -- a prior finding: the proposal has to be reviewable ---------------------------- #
    def test_every_review_field_is_required(self):
        for missing in ("title", "condition", "desired_outcome",
                        "completion_check"):
            r = self.run_cli(["propose", self.payload(**{missing: ""})])
            self.assertEqual(r.returncode, 1, f"{missing} was accepted empty")
            self.assertIn(missing, r.stderr)
            self.assertEqual(self.history(), [], "a refused proposal recorded")

    def test_the_subsystem_must_be_one_of_the_nine(self):
        r = self.run_cli(["propose", self.payload(subsystem="the vibes")])
        self.assertEqual(r.returncode, 1)
        self.assertIn("must be one of", r.stderr)
        r = self.run_cli(["propose", self.payload(subsystem="dashboard")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_review_record_reaches_the_store(self):
        r = self.run_cli(["propose", self.payload(
            condition="dispatches.json loses rows",
            desired_outcome="entries survive until resolved",
            completion_check="three clean nights")])
        task = r.stdout.split()[0]
        shown = self.estate("proposal", "show", task).stdout
        self.assertIn("dispatches.json loses rows", shown)
        self.assertIn("entries survive until resolved", shown)
        self.assertIn("three clean nights", shown)

    def test_the_finding_id_threads_history_task_and_ledger(self):
        r = self.run_cli(["propose", self.payload()])
        task, _, finding = r.stdout.split()
        self.assertTrue(finding.startswith("f-2026-07-30-"), r.stdout)
        self.assertEqual(self.history()[-1]["finding"], finding)
        # The task carries it, so the proposal is reachable FROM the finding.
        found = self.estate("task", "find", "--ref", f"finding={finding}",
                            "--quiet").stdout.split()
        self.assertEqual(found, [task])
        # And the ledger event carries both.
        events = self.estate("events", "--task", task, "--detail").stdout
        self.assertIn(finding, events)
        self.assertIn("proposed", events)

    def test_a_proposal_is_not_recorded_when_its_ledger_event_fails(self):
        # Same rule as the estate call itself: a history line claiming an
        # outcome nobody outside the loop can see is worse than a refusal.
        blocked = os.path.join(self.tmp.name, "blocked-estate")
        with open(blocked, "w") as f:
            f.write("import sys\n"
                    "sys.exit(0 if sys.argv[1] != 'event' else 3)\n")
        r = subprocess.run(
            [sys.executable, SCRIPT, "propose", self.payload()],
            capture_output=True, text=True,
            env=dict(os.environ, MECHANIC_NOW="2026-07-30T02:30:00-04:00",
                     MECHANIC_STATE_DIR=self.mech, ESTATE_SCRIPT=blocked,
                     ESTATE_STATE_DIR=self.estate_dir))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.history(), [])


class TestSubsystemChecklist(unittest.TestCase):
    """Gap audit a prior finding, S07 — a pass proves it examined all nine subsystems.

    Before this, the five diagnostic lenses were prose and the pass summary
    was a tally. A night that never looked at the dashboard produced exactly
    the same record as a night that looked and found nothing, which is the one
    thing a self-audit must not do.
    """
    ESTATE = TestPropose.ESTATE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mech = os.path.join(self.tmp.name, "mechanic")
        self.estate_dir = os.path.join(self.tmp.name, "estate")
        os.makedirs(self.mech)
        os.makedirs(self.estate_dir)

    def run_cli(self, args, now="2026-07-30T02:30:00-04:00"):
        env = dict(os.environ, MECHANIC_NOW=now, MECHANIC_STATE_DIR=self.mech,
                   ESTATE_STATE_DIR=self.estate_dir, ESTATE_SCRIPT=self.ESTATE)
        return subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, env=env)

    def check(self, subsystem, result="ok", note="looked, nothing moved",
              now="2026-07-30T02:30:00-04:00"):
        return self.run_cli(["record", json.dumps(
            {"event": "subsystem_check", "subsystem": subsystem,
             "result": result, "note": note})], now=now)

    def check_all(self, **kw):
        for name in mechanic.SUBSYSTEMS:
            r = self.check(name, **kw)
            self.assertEqual(r.returncode, 0, r.stderr)

    def history(self):
        path = os.path.join(self.mech, "history.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_the_nine_are_the_audit_s_nine(self):
        self.assertEqual(len(mechanic.SUBSYSTEMS), 9)
        for name in ("hub", "mechanic", "night-shift", "spotter", "briefer",
                     "tasks", "memory", "dashboard", "ledger"):
            self.assertIn(name, mechanic.SUBSYSTEMS)
        self.assertEqual(self.run_cli(["subsystems"]).stdout.split(),
                         list(mechanic.SUBSYSTEMS))

    def test_pass_done_is_refused_while_a_row_is_missing(self):
        for name in mechanic.SUBSYSTEMS[:-1]:
            self.check(name)
        r = self.run_cli(["record", '{"event":"pass_done","findings":0}'])
        self.assertEqual(r.returncode, 1)
        self.assertIn(mechanic.SUBSYSTEMS[-1], r.stderr)
        self.assertNotIn("pass_done", [e["event"] for e in self.history()])

    def test_pass_done_is_accepted_once_all_nine_are_recorded(self):
        self.check_all()
        r = self.run_cli(["record", '{"event":"pass_done","findings":0}'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.history()[-1]["event"], "pass_done")

    def test_unobservable_is_a_legal_answer_and_there_is_no_force(self):
        # The escape hatch is an honest row, not a flag. A subsystem that
        # could not be examined says so, with why, and the pass can finish.
        self.check_all(result="unobservable", note="agent-deck was down")
        self.assertEqual(
            self.run_cli(["record", '{"event":"pass_done"}']).returncode, 0)

    def test_a_result_needs_a_note(self):
        r = self.run_cli(["record", json.dumps(
            {"event": "subsystem_check", "subsystem": "hub", "result": "ok"})])
        self.assertEqual(r.returncode, 1)
        self.assertIn("note is required", r.stderr)

    def test_the_vocabulary_is_closed_on_both_fields(self):
        self.assertEqual(self.check("the vibes").returncode, 1)
        self.assertEqual(self.check("hub", result="fine").returncode, 1)
        self.assertEqual(self.history(), [])

    def test_a_later_row_supersedes_an_earlier_one(self):
        self.check("hub", result="unobservable", note="deck was down")
        self.check("hub", result="ok", note="deck came back, hub is fine")
        rows = mechanic.checklist(self.history())
        self.assertEqual(rows["hub"]["result"], "ok")

    def test_the_checklist_reports_coverage_and_exits_nonzero_when_short(self):
        r = self.run_cli(["checklist"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("0/9", r.stdout)
        self.check_all()
        r = self.run_cli(["checklist"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("9/9", r.stdout)

    def test_last_night_s_rows_do_not_satisfy_tonight(self):
        self.check_all()
        r = self.run_cli(["record", '{"event":"pass_done"}'],
                         now="2026-07-31T02:30:00-04:00")
        self.assertEqual(r.returncode, 1)
        self.assertIn("2026-07-31", r.stderr)

    def test_legacy_history_still_loads(self):
        # No migration: a night recorded before a prior finding has no check rows and no
        # finding ids, and nothing here rewrites or rejects it.
        path = os.path.join(self.mech, "history.jsonl")
        with open(path, "w") as f:
            for line in ('{"event":"pass_start","night":"2026-07-01"}',
                         '{"event":"finding","action":"observed",'
                         '"summary":"old","night":"2026-07-01"}',
                         '{"event":"pass_done","findings":1,'
                         '"night":"2026-07-01"}'):
                f.write(line + "\n")
        r = self.run_cli(["checklist"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("0/9", r.stdout)
        self.assertEqual(len(self.history()), 3)


class TestChecklistDigest(unittest.TestCase):
    """The checklist section is fixed cost on every gather, so it collapses on
    a warm one exactly like the ledger, policy, extraction and git sections. It
    always prints the count: a collapsed row is one already read, never a
    hidden one."""
    CFG = {"pass_start": "02:00", "pass_end": "05:00"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "mstate")
        os.makedirs(self.state)
        self.env = mock.patch.dict(
            os.environ, {"MECHANIC_NOW": "2026-07-30T02:30:00-04:00",
                         "MECHANIC_STATE_DIR": self.state})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self, subsystem, result="ok"):
        with open(os.path.join(self.state, "history.jsonl"), "a") as f:
            f.write(json.dumps({"event": "subsystem_check", "night": "2026-07-30",
                                "subsystem": subsystem, "result": result,
                                "note": "looked"}) + "\n")

    def run_it(self, prev=None, full=False):
        lines, snap = mechanic.checklist_digest(self.CFG, prev or {}, full)
        return "\n".join(lines), snap

    def test_cold_carries_the_standing_pointer_warm_does_not(self):
        cold, snap = self.run_it()
        self.assertIn("0/9 recorded", cold)
        self.assertIn("`mechanic.py subsystems`", cold)
        warm, _ = self.run_it(prev=snap)
        self.assertIn("0/9 recorded", warm)          # the count always prints
        self.assertIn("all nine still owed", warm)
        self.assertNotIn("`mechanic.py subsystems`", warm)
        self.assertLess(len(warm), len(cold))

    def test_repeated_rows_collapse_against_the_baseline(self):
        for name in mechanic.SUBSYSTEMS[:4]:
            self.record(name)
        cold, snap = self.run_it()
        self.assertIn("recorded: hub (ok)", cold)
        warm, _ = self.run_it(prev=snap)
        self.assertIn("4/9 recorded", warm)
        self.assertIn("same rows as baseline night 2026-07-30", warm)
        self.assertIn("still owed: briefer, tasks", warm)
        self.assertNotIn("recorded: hub (ok)", warm)  # not re-listed
        self.assertLess(len(warm), len(cold))

    def test_a_new_row_since_the_baseline_prints_in_full(self):
        for name in mechanic.SUBSYSTEMS[:4]:
            self.record(name)
        _, snap = self.run_it()
        self.record(mechanic.SUBSYSTEMS[4], result="finding")
        warm, _ = self.run_it(prev=snap)
        self.assertIn("5/9 recorded", warm)
        self.assertIn("briefer (finding)", warm)
        self.assertNotIn("same rows as baseline", warm)

    def test_full_ignores_the_baseline(self):
        _, snap = self.run_it()
        full, _ = self.run_it(prev=snap, full=True)
        self.assertIn("`mechanic.py subsystems`", full)

    def test_the_snapshot_slice_carries_the_results(self):
        self.record("hub", result="unobservable")
        _, snap = self.run_it()
        self.assertEqual(snap, {"night": "2026-07-30",
                                "recorded": {"hub": "unobservable"}})


class TestProposalDigest(unittest.TestCase):
    """Same warm/cold contract for the proposal section — and the ids of the
    rows that collapse stay on the page, so nothing goes quiet."""
    ROWS = ("  task-0001  [ready     ] proposal     (pending-review) the hub "
            "drops intents\n"
            "  task-0002  [ready     ] proposal     (approved-backlog) the "
            "spotter double-posts\n")

    def run_it(self, out, prev=None, full=False, rc=0):
        with mock.patch.object(mechanic, "_estate",
                               return_value=(rc, out, "" if rc == 0 else out)):
            lines, snap = mechanic.proposal_digest(prev or {}, full)
        return "\n".join(lines), snap

    def test_cold_prints_every_row(self):
        out, snap = self.run_it(self.ROWS)
        self.assertIn("2 on file", out)
        self.assertIn("the hub drops intents", out)
        self.assertIn("the spotter double-posts", out)
        self.assertEqual(sorted(snap["prop_rows"]), ["task-0001", "task-0002"])

    def test_unmoved_rows_collapse_to_their_ids(self):
        cold, snap = self.run_it(self.ROWS)
        warm, _ = self.run_it(self.ROWS, prev=snap)
        self.assertIn("2 on file", warm)
        self.assertIn("unchanged since baseline (2)", warm)
        self.assertIn("task-0001, task-0002", warm)  # named, never dropped
        self.assertNotIn("the hub drops intents", warm)
        self.assertLess(len(warm), len(cold))

    def test_a_moved_stage_resurfaces_in_full(self):
        _, snap = self.run_it(self.ROWS)
        moved = self.ROWS.replace("(pending-review)", "(approved-backlog)")
        warm, _ = self.run_it(moved, prev=snap)
        self.assertIn("the hub drops intents", warm)
        self.assertIn("[CHANGED since baseline]", warm)
        self.assertIn("unchanged since baseline (1)", warm)

    def test_a_new_proposal_resurfaces_in_full(self):
        _, snap = self.run_it(self.ROWS)
        added = self.ROWS + ("  task-0003  [ready     ] proposal     "
                             "(pending-review) the briefer is late\n")
        warm, _ = self.run_it(added, prev=snap)
        self.assertIn("the briefer is late", warm)
        self.assertIn("[new since baseline]", warm)

    def test_a_row_that_leaves_the_list_is_named(self):
        _, snap = self.run_it(self.ROWS)
        warm, _ = self.run_it("", prev=snap)
        self.assertIn("no longer listed since baseline (2)", warm)
        self.assertIn("task-0001, task-0002", warm)

    def test_full_ignores_the_baseline(self):
        _, snap = self.run_it(self.ROWS)
        out, _ = self.run_it(self.ROWS, prev=snap, full=True)
        self.assertIn("the hub drops intents", out)
        self.assertNotIn("since baseline", out)

    def test_an_unavailable_store_is_a_note_and_collapses_when_it_repeats(self):
        why = "no such file: /nope/bin/estate" + "x" * 80
        cold, snap = self.run_it(why, rc=1)
        self.assertIn("proposal store unavailable", cold)
        self.assertIn(why, cold)
        warm, _ = self.run_it(why, prev=snap, rc=1)
        self.assertIn("still unavailable", warm)
        # Both forms keep the framing: this is a fact about the machine.
        for text in (cold, warm):
            self.assertIn("not a finding about the estate", text)
        self.assertLess(len(warm), len(cold))

    def test_a_different_failure_reprints_in_full(self):
        _, snap = self.run_it("database is locked", rc=1)
        warm, _ = self.run_it("no such file", prev=snap, rc=1)
        self.assertIn("proposal store unavailable (no such file)", warm)


class TestFindingRecords(unittest.TestCase):
    """Gap audit a prior finding, S08/S09 — every finding is identified, attributed to a
    subsystem, and its outcome reaches the ledger. `no-proposal` carries a
    reason, because "I looked and decided against it" without the why is
    indistinguishable from never having looked."""
    ESTATE = TestPropose.ESTATE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mech = os.path.join(self.tmp.name, "mechanic")
        self.estate_dir = os.path.join(self.tmp.name, "estate")
        os.makedirs(self.mech)
        os.makedirs(self.estate_dir)

    def run_cli(self, args, now="2026-07-30T02:30:00-04:00"):
        env = dict(os.environ, MECHANIC_NOW=now, MECHANIC_STATE_DIR=self.mech,
                   ESTATE_STATE_DIR=self.estate_dir, ESTATE_SCRIPT=self.ESTATE)
        return subprocess.run([sys.executable, SCRIPT, *args],
                              capture_output=True, text=True, env=env)

    def finding(self, now="2026-07-30T02:30:00-04:00", **over):
        payload = {"event": "finding", "action": "observed",
                   "subsystem": "spotter", "summary": "state file grew 4KB"}
        payload.update(over)
        return self.run_cli(["record", json.dumps(payload)], now=now)

    def estate(self, *argv):
        return subprocess.run(
            [sys.executable, self.ESTATE, *argv], capture_output=True, text=True,
            env=dict(os.environ, ESTATE_STATE_DIR=self.estate_dir))

    def history(self):
        path = os.path.join(self.mech, "history.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_no_proposal_without_a_reason_is_refused(self):
        r = self.finding(action="no-proposal", reason="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("reason", r.stderr)
        self.assertEqual(self.history(), [])

    def test_no_proposal_with_a_reason_is_recorded_and_reaches_the_ledger(self):
        r = self.finding(action="no-proposal",
                         summary="spotter interval fit",
                         reason="two nights of data is not a pattern")
        self.assertEqual(r.returncode, 0, r.stderr)
        line = self.history()[-1]
        self.assertEqual(line["action"], "no-proposal")
        self.assertEqual(line["reason"], "two nights of data is not a pattern")
        events = self.estate("events", "--actor", "mechanic", "--detail").stdout
        self.assertIn(line["finding"], events)
        self.assertIn("no-proposal", events)
        self.assertIn("two nights of data is not a pattern", events)

    def test_a_finding_names_its_subsystem(self):
        self.assertEqual(self.finding(subsystem=None).returncode, 1)
        self.assertEqual(self.finding(subsystem="the vibes").returncode, 1)

    def test_a_finding_needs_a_summary(self):
        self.assertEqual(self.finding(summary="  ").returncode, 1)

    def test_proposed_cannot_be_recorded_by_hand(self):
        # The pairing a prior finding built only holds if `propose` is the only door.
        r = self.finding(action="proposed")
        self.assertEqual(r.returncode, 1)
        self.assertIn("mechanic.py propose", r.stderr)

    def test_finding_ids_are_sequential_within_a_night(self):
        self.finding()
        self.finding(summary="another one")
        ids = [e["finding"] for e in self.history()]
        self.assertEqual(ids, ["f-2026-07-30-01", "f-2026-07-30-02"])

    def test_finding_ids_restart_per_night_and_stay_unique(self):
        self.finding()
        self.finding(now="2026-07-31T02:30:00-04:00")
        self.assertEqual([e["finding"] for e in self.history()],
                         ["f-2026-07-30-01", "f-2026-07-31-01"])

    def test_nothing_is_recorded_when_the_ledger_is_unreachable(self):
        r = subprocess.run(
            [sys.executable, SCRIPT, "record", json.dumps(
                {"event": "finding", "action": "observed", "subsystem": "hub",
                 "summary": "x"})],
            capture_output=True, text=True,
            env=dict(os.environ, MECHANIC_NOW="2026-07-30T02:30:00-04:00",
                     MECHANIC_STATE_DIR=self.mech,
                     ESTATE_SCRIPT=os.path.join(self.tmp.name, "gone"),
                     ESTATE_STATE_DIR=self.estate_dir))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.history(), [])

    def test_other_events_are_untouched_by_the_new_rules(self):
        # pass_start, dry_run, and anything else the pass invents still append
        # exactly as before, and never call the estate.
        r = subprocess.run(
            [sys.executable, SCRIPT, "record", '{"event":"pass_start"}'],
            capture_output=True, text=True,
            env=dict(os.environ, MECHANIC_NOW="2026-07-30T02:30:00-04:00",
                     MECHANIC_STATE_DIR=self.mech,
                     ESTATE_SCRIPT=os.path.join(self.tmp.name, "gone"),
                     ESTATE_STATE_DIR=self.estate_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.history()[-1]["event"], "pass_start")


class TestP06PassEventsReachTheLedger(unittest.TestCase):
    """the consistency contract. The pass boundaries used to live only in history.jsonl,
    so "did the mechanic run last night" was answerable from inside this loop
    and nowhere else — and a night that started and never finished was
    invisible to everything reading the shared store."""

    ESTATE = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        os.pardir, "bin", "estate"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mech = os.path.join(self.tmp.name, "mechanic")
        self.estate_dir = os.path.join(self.tmp.name, "estate")
        os.makedirs(self.mech)
        os.makedirs(self.estate_dir)

    def record(self, payload, now="2026-07-30T02:30:00-04:00"):
        env = dict(os.environ, MECHANIC_NOW=now, MECHANIC_STATE_DIR=self.mech,
                   ESTATE_STATE_DIR=self.estate_dir, ESTATE_SCRIPT=self.ESTATE)
        return subprocess.run(
            [sys.executable, SCRIPT, "record", json.dumps(payload)],
            capture_output=True, text=True, env=env)

    def rows(self):
        db = os.path.join(self.estate_dir, "estate.db")
        if not os.path.exists(db):
            return []
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM events WHERE subsystem IS NOT NULL ORDER BY seq")]
        finally:
            con.close()

    def test_pass_start_is_an_initiation_and_names_the_mechanic(self):
        r = self.record({"event": "pass_start"})
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["subsystem"], rows[0]["phase"]),
                         ("mechanic", "initiation"))
        self.assertEqual(rows[0]["actor"], "mechanic")

    def test_a_resumed_pass_replaying_its_own_start_lands_one_row(self):
        """The fingerprint is over the NIGHT, not the clock. The same night is
        not a second pass, however many times a resuming tick records it."""
        self.record({"event": "pass_start"})
        self.record({"event": "pass_start"}, now="2026-07-30T03:15:00-04:00")
        self.assertEqual(len(self.rows()), 1)

    def test_the_history_line_is_written_even_when_the_estate_is_gone(self):
        """history.jsonl is what derive_phase reads to know a pass is running.
        It must not be missing because a second store was locked."""
        env = dict(os.environ, MECHANIC_NOW="2026-07-30T02:30:00-04:00",
                   MECHANIC_STATE_DIR=self.mech,
                   ESTATE_STATE_DIR=self.estate_dir,
                   ESTATE_SCRIPT=os.path.join(self.tmp.name, "no-such-estate"))
        r = subprocess.run(
            [sys.executable, SCRIPT, "record", json.dumps({"event": "pass_start"})],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("shared Ledger", r.stderr)
        with open(os.path.join(self.mech, "history.jsonl")) as f:
            self.assertEqual(len([l for l in f if l.strip()]), 1)


class TestP08RunOutcome(unittest.TestCase):
    """the consistency contract. The night IS the mechanic's scheduled run, so its pass
    boundaries are also its run record. The one judgment this engine makes is
    whether a finished night found anything — and it reads that off the
    night's own history rather than a count the model passed in."""

    def test_a_night_with_no_finding_completed_with_no_work(self):
        self.assertEqual(mechanic.run_outcome_for("pass_done", []),
                         "no_activity")

    def test_a_night_with_a_finding_completed_with_work(self):
        self.assertEqual(
            mechanic.run_outcome_for("pass_done", [{"event": "finding"}]),
            "completed")

    def test_a_subsystem_check_is_not_a_finding(self):
        """Nine `ok` rows and nothing else is exactly a quiet night: the pass
        examined everything and had nothing to report."""
        checks = [{"event": "subsystem_check", "subsystem": s, "result": "ok"}
                  for s in mechanic.SUBSYSTEMS]
        self.assertEqual(mechanic.run_outcome_for("pass_done", checks),
                         "no_activity")

    def test_nothing_here_can_produce_a_failure(self):
        """A failed run is `pass_failed` and only `pass_failed` — a prior finding's rule,
        unchanged. An empty night is not a broken one."""
        self.assertEqual(mechanic.run_outcome_for("pass_failed", []), "failed")
        self.assertEqual(mechanic.run_outcome_for("pass_start", []), "started")
        self.assertIsNone(mechanic.run_outcome_for("finding", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)

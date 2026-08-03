#!/usr/bin/env python3
"""Tests for the mechanic engine's deterministic core: window gating, night
identity, phase derivation, ledger timestamp parsing, the windows/record CLI,
and the incremental digest (section hashing, ledger cursor, snapshot lifecycle).
Run: python3 tests/test_mechanic.py"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "loops", "mechanic", ".claude", "skills", "mechanic", "scripts",
    "mechanic.py")
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
            '{"ts":1784401391,"actor":"demo","kind":"error","summary":"epoch ts"}',
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

            r = self.run_cli(
                ["record", '{"event":"pass_done","findings":2}'], now, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = self.run_cli(["windows"], now, d)
            self.assertIn("phase=done", r.stdout)

            # history.jsonl carries ts + night on every line
            hist = os.path.join(d, "history.jsonl")
            lines = [json.loads(l) for l in open(hist)]
            self.assertEqual(len(lines), 2)
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
    """Who the hub is comes from loops.toml, never from the code. A fresh clone
    with an empty [hub] is anonymous — "ops" — exactly as a stranger's install
    would be. Keep in step with bin/doorbell and bin/dashboard, which resolve
    the same three fields the same way."""

    def test_empty_registry_is_anonymous(self):
        h = mechanic.hub_config({})
        self.assertEqual(h["persona"], "ops")
        self.assertEqual(h["session"], "ops (hub)")
        self.assertEqual(h["deck_profile"], "ops")

    def test_persona_drives_the_session_title(self):
        h = mechanic.hub_config({"hub": {"persona": "the keeper"}})
        self.assertEqual(h["session"], "the keeper (hub)")

    def test_explicit_session_title_wins_over_the_convention(self):
        h = mechanic.hub_config(
            {"hub": {"persona": "the keeper", "session_title": "control (hub)"}})
        self.assertEqual(h["session"], "control (hub)")

    def test_deck_profile_falls_back_to_persona_independent_default(self):
        # deck_profile is its own field: it does NOT inherit the persona.
        h = mechanic.hub_config({"hub": {"persona": "the keeper"}})
        self.assertEqual(h["deck_profile"], "ops")
        h = mechanic.hub_config({"hub": {"deck_profile": "myprofile"}})
        self.assertEqual(h["deck_profile"], "myprofile")

    def test_env_overrides_the_configured_deck_profile(self):
        with mock.patch.dict(os.environ,
                             {"MECHANIC_DECK_PROFILE": "override"}):
            h = mechanic.hub_config({"hub": {"deck_profile": "myprofile"}})
        self.assertEqual(h["deck_profile"], "override")


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
persona = "the keeper"
slack_channel_id = "D1"

[loops.demo]
dir = "loops/demo"
skill = "demo"
interval = "20m"
autostart = true
persona = "the demo"
model = "claude-sonnet-5"
"""

# The point of the digest is that big manuals stop being re-read, so the demo
# skill has to be manual-sized for a size comparison to mean anything.
FILLER = "\n".join(f"filler line {i} — padding this manual out" for i in range(60))
DEMO_SKILL = f"# demo skill\n## Alpha\nalpha body\n{FILLER}\n## Beta\nbeta body\n"

DECK = {
    "the keeper (hub)": {"title": "the keeper (hub)", "status": "waiting",
                        "model_id": "claude-sonnet-5"},
    "the demo (demo)": {"title": "the demo (demo)", "status": "waiting",
                        "model_id": "claude-sonnet-5"},
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
              LOOPS_TOML.replace("claude-sonnet-5", "claude-haiku-4-5"))
        out = self.gather(NOW_N2)
        self.assertIn("[CHANGED since baseline", out)
        self.assertIn("loops.toml model=claude-haiku-4-5 but live "
                      "model=claude-sonnet-5", out)

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
            'model = "claude-sonnet-5"\n'))
        # Deck has only the hub — both loops are absent from agent-deck.
        deck = {"the keeper (hub)": {"title": "the keeper (hub)",
                                    "status": "waiting",
                                    "model_id": "claude-sonnet-5"}}
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


class TestShippedAllowlistTemplate(unittest.TestCase):
    """docs/extraction-allowlist.example.md is parsed by the lens, so it is code.

    ALLOWLIST_MD above is a hand-written fixture and can drift from what the
    core actually ships; these cases read the real file, so a doc edit that
    silently changes what the parser sees fails here instead of in someone's
    fork."""

    EXAMPLE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "extraction-allowlist.example.md")

    def setUp(self):
        with open(self.EXAMPLE, encoding="utf-8") as f:
            self.text = f.read()
        self.include, self.exclude = mechanic.parse_allowlist(self.text)

    def test_include_is_exactly_the_documented_core_surface(self):
        self.assertEqual(self.include, [
            ".flox/env/manifest.lock", ".flox/env/manifest.toml", "bin/",
            "docs/", "hub/", "infra/", "loops.example.toml", "loops/example/",
            "loops/mechanic/", "tests/"])

    def test_exclude_covers_every_fork_local_doc_the_template_names(self):
        # The broad `docs/` include would otherwise nominate each of these for
        # extraction into the public core on every pass.
        for rel in ("docs/extraction-allowlist.md", "docs/ideas.md",
                    "docs/lessons.md", "docs/new-machine-setup.md"):
            self.assertIn(rel, self.exclude)
        self.assertIn("state/", self.exclude)
        self.assertIn("loops.toml", self.exclude)

    def test_every_backticked_path_under_deliberately_out_is_an_exclude(self):
        # The CAUTION promises this. An illustrative aside written in backticks
        # in that section is a live pattern, not prose — which is why the
        # section's own examples avoid backticking a real path.
        out = [b for t, b in mechanic.split_sections(self.text)
               if "deliberately out" in t.lower()]
        self.assertEqual(len(out), 1, "the 'deliberately out' heading moved")
        for tok in mechanic.BACKTICK_RE.findall(out[0]):
            if not mechanic._pathlike(tok) or tok in self.include:
                continue
            self.assertIn(tok, self.exclude)

    def test_no_include_pattern_is_missing_from_the_core(self):
        root = os.path.dirname(os.path.dirname(self.EXAMPLE))
        _, absent, _ = mechanic.resolve_allowlist(root, self.include)
        self.assertEqual(absent, [], "template names paths this core lacks")

    def test_the_three_parser_anchors_are_present(self):
        # parse_allowlist finds its buckets by substring-matching these in a
        # heading; renaming one empties its bucket with no error at all.
        titles = [t.lower() for t, _ in mechanic.split_sections(self.text)]
        for anchor in ("what is in", "invariant", "deliberately out"):
            self.assertTrue(any(anchor in t for t in titles),
                            f"no heading contains the anchor {anchor!r}")


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
        self.root = os.path.join(self.tmp.name, "operations")
        self.core = os.path.join(self.tmp.name, "nano-ops")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

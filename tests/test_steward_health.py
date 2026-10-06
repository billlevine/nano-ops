#!/usr/bin/env python3
"""Exercise the optional health reader against disposable git worktrees,
registry rows, transcript files and fake command responses. No live sessions,
real state or external services are used.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
SCRIPT = os.path.join(os.path.dirname(BIN), "lib", "steward_health.py")
sys.path.insert(0, os.path.join(os.path.dirname(BIN), "lib"))

import estate_observation as obs  # noqa: E402
import steward_health as sh  # noqa: E402

# A fixed clock. Every commit and every file time in this file is an offset
# from it, so nothing here depends on when it runs.
NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
NOW_TS = int(NOW.timestamp())
HOUR = 3600

PROFILE = "test-profile"

# The fake `agent-deck`: it ignores every argument and prints the fixture it
# was given. The real one is never invoked.
FAKE_DECK = """#!/usr/bin/env python3
import sys
sys.stdout.write(open(sys.argv[1]).read())
"""

# The fake `bin/dispatches`, same shape. `due --json` is the read verb.
FAKE_DISPATCHES = FAKE_DECK

# A fake that exits non-zero, for the failed-read path.
FAKE_BROKEN = """#!/usr/bin/env python3
import sys
sys.stderr.write("agent-deck: could not open the session store\\n")
sys.exit(4)
"""


def encode(path: str) -> str:
    """Claude Code's project-directory name for a cwd. Written out here rather
    than imported, so the test does not check the tool against itself."""
    return path.replace("/", "-").replace(".", "-")


def usage_line(tokens: int, at: dt.datetime, *, sidechain=False,
               kind="assistant") -> str:
    """One transcript line in the shape Claude Code writes."""
    return json.dumps({
        "type": kind,
        "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "isSidechain": sidechain,
        "sessionId": "s-1",
        "message": {"model": "claude-opus-5", "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": max(0, tokens - 1002),
            "output_tokens": 900,
        }},
    })


class StewardCase(unittest.TestCase):
    # ── a tempdir estate ─────────────────────────────────────────────────────
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.registry = os.path.join(self.tmp, "state", "stewards")
        self.transcripts = os.path.join(self.tmp, "projects")
        self.fixtures = os.path.join(self.tmp, "fixtures")
        for path in (self.registry, self.transcripts, self.fixtures):
            os.makedirs(path)
        self.deck_fixture = self.write_fixture("deck.json", [])
        self.dispatch_fixture = self.write_fixture("due.json", {})
        self.deck_cmd = self.fake("fake-deck", FAKE_DECK)
        self.dispatch_cmd = self.fake("fake-dispatches", FAKE_DISPATCHES)

    def fake(self, name: str, body: str) -> str:
        path = os.path.join(self.fixtures, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def write_fixture(self, name: str, data) -> str:
        path = os.path.join(self.fixtures, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    # ── one steward, built for real ──────────────────────────────────────────
    def git(self, repo, *argv, at=None):
        env = dict(os.environ)
        if at is not None:
            stamp = f"@{at} +0000"
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = stamp
        out = subprocess.run(["git", "-C", repo, *argv], env=env,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def write(self, path: str, text: str, at=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if at is not None:
            os.utime(path, (at, at))
        return path

    def make_steward(self, slug="project-alpha", *, status="active",
                     last_commit=None, state_bytes=200, state_at=None,
                     notes_at=None, gate=True, context_tokens=120_000,
                     transcript=True, threshold=300_000, deck_status="waiting",
                     in_deck=True, worktree=None, row_extra=None) -> dict:
        """A registry row, its worktree, its transcript and its deck record."""
        last_commit = NOW_TS - HOUR if last_commit is None else last_commit
        state_at = last_commit if state_at is None else state_at
        notes_at = last_commit if notes_at is None else notes_at
        worktree = worktree or os.path.join(self.tmp, "worktrees", slug)
        branch = f"steward/{slug}"
        title = f"the {slug} steward ({slug})"
        session_id = f"deck-{slug}"

        os.makedirs(worktree, exist_ok=True)
        self.git(worktree, "init", f"--initial-branch={branch}", "-q")
        self.git(worktree, "config", "user.email", "t@example.com")
        self.git(worktree, "config", "user.name", "T")
        self.write(os.path.join(worktree, "project", "CHARTER.md"),
                   f"# {title} — charter\n")
        self.write(os.path.join(worktree, "project", "STATE.md"),
                   "x" * state_bytes)
        self.write(os.path.join(worktree, "project", "NOTES.md"),
                   "## 2026-09-19 — a round\n")
        if gate:
            self.write(os.path.join(worktree, "stewards", ".claude",
                                    "settings.json"),
                       json.dumps({"hooks": {"PreToolUse": [{
                           "matcher": "AskUserQuestion",
                           "hooks": [{"type": "command",
                                      "command": "../bin/unattended-gate ask"}],
                       }]}}))
        self.git(worktree, "add", "-A")
        self.git(worktree, "commit", "-q", "-m", "notes: a round",
                 at=last_commit)
        # The working-tree times are set AFTER the commit, because the check
        # reads the later of "last commit touching it" and "last written".
        os.utime(os.path.join(worktree, "project", "STATE.md"),
                 (state_at, state_at))
        os.utime(os.path.join(worktree, "project", "NOTES.md"),
                 (notes_at, notes_at))

        if transcript:
            directory = os.path.join(self.transcripts, encode(worktree))
            os.makedirs(directory, exist_ok=True)
            self.write(os.path.join(directory, "session.jsonl"),
                       usage_line(context_tokens, NOW) + "\n",
                       at=NOW_TS - 60)

        row = {"slug": slug, "title": title,
               "epic": {"kind": "linear-issue", "ref": slug.upper(),
                        "url": f"https://linear.app/{slug}"},
               "repos": ["example/widget"], "worktree": worktree,
               "branch": branch, "status": status,
               "created_at": "2026-09-11T16:51:00Z", "retired_at": None,
               "context_threshold": threshold, "halt_standard": "relaxed",
               "deck_session_id": session_id, "task": "t-55"}
        row.update(row_extra or {})
        self.write(os.path.join(self.registry, f"{slug}.json"),
                   json.dumps(row, indent=2))

        if in_deck:
            with open(self.deck_fixture, encoding="utf-8") as fh:
                sessions = json.load(fh)
            sessions.append({"id": session_id, "title": title,
                             "status": deck_status, "path": worktree,
                             "group": "ops", "profile": PROFILE,
                             "archived": False})
            self.write_fixture("deck.json", sessions)
        return row

    def append_notes(self, row, at):
        """One more round that appends to NOTES.md and leaves STATE.md alone.

        This is the real shape of `drifting:state`: every round is supposed to
        commit both files (section 3.3, step 4), and the drift is a round that
        committed one.
        """
        path = os.path.join(row["worktree"], "project", "NOTES.md")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n## a later round, with no STATE.md rewrite\n")
        self.git(row["worktree"], "add", "-A")
        self.git(row["worktree"], "commit", "-q", "-m", "notes: another round",
                 at=at)
        os.utime(path, (at, at))

    # ── running the real tool ────────────────────────────────────────────────
    def run_tool(self, *argv, deck_cmd=None, dispatch_cmd=None):
        env = dict(os.environ)
        env.update({
            "STEWARD_STATE_DIR": os.path.join(self.tmp, "state"),
            "ESTATE_STATE_DIR": os.path.join(self.tmp, "state"),
            "STEWARD_TRANSCRIPT_DIR": self.transcripts,
            "STEWARD_HEALTH_NOW": NOW_ISO,
            "STEWARD_DECK_CMD": f"{deck_cmd or self.deck_cmd} "
                                f"{self.deck_fixture}",
            "STEWARD_DISPATCHES_CMD": f"{dispatch_cmd or self.dispatch_cmd} "
                                      f"{self.dispatch_fixture}",
        })
        env.pop("STEWARD_HEALTH_PROFILE", None)
        return subprocess.run([sys.executable, SCRIPT, "--profile", PROFILE,
                               *argv], env=env, capture_output=True, text=True)

    def report(self, *argv, **kwargs):
        out = self.run_tool("--json", *argv, **kwargs)
        self.assertIn(out.returncode, (0, 1, 2), out.stderr)
        try:
            return json.loads(out.stdout), out.returncode
        except ValueError:  # pragma: no cover - only on a crash
            self.fail(f"not JSON:\n{out.stdout}\n{out.stderr}")

    def row_of(self, report, slug="project-alpha"):
        return next(r for r in report["rows"] if r["slug"] == slug)

    def check_of(self, report, name, slug="project-alpha"):
        return next(c for c in self.row_of(report, slug)["checks"]
                    if c["check"] == name)


class HealthyTest(StewardCase):
    def test_a_healthy_steward_is_clean_and_exits_0(self):
        self.make_steward()
        report, status = self.report()
        row = self.row_of(report)
        self.assertEqual(status, 0)
        self.assertEqual(row["verdict"], "ok")
        self.assertEqual(row["findings"], [])
        self.assertEqual(row["unknowns"], [])
        self.assertEqual(sorted(c["check"] for c in row["checks"]),
                         sorted(sh.CHECK_NAMES))

    def test_every_check_carries_a_warranted_reading(self):
        self.make_steward()
        report, _ = self.report()
        for check in self.row_of(report)["checks"]:
            with self.subTest(check=check["check"]):
                self.assertEqual(check["reading"]["subsystem"],
                                 "steward-health")
                self.assertTrue(obs.absence_is_evidence(check["reading"]))

    def test_the_plain_rendering_names_the_steward_and_the_exit(self):
        self.make_steward()
        out = self.run_tool()
        self.assertEqual(out.returncode, 0)
        self.assertIn("project-alpha", out.stdout)
        self.assertIn("exit 0", out.stdout)
        self.assertNotIn("could not be made", out.stdout)

    def test_the_header_counts_the_checks_that_could_not_be_made(self):
        """A row leads with its actionable answer, so the row counts alone
        would read as "nothing went unlooked-at" on a steward that is both
        dead and half-unreadable."""
        self.make_steward(deck_status="error", transcript=False)
        out = self.run_tool()
        self.assertEqual(out.returncode, 2)
        self.assertIn("checks: 1 of 7 could not be made", out.stdout)


class FindingTest(StewardCase):
    """One fixture per finding, each differing from the healthy one in exactly
    the way the finding names."""

    def assert_finding(self, name, check, slug="project-alpha", status=1):
        report, code = self.report()
        row = self.row_of(report, slug)
        self.assertEqual(code, status)
        self.assertIn(name, row["findings"])
        self.assertEqual(row["verdict"], "finding")
        entry = self.check_of(report, check, slug)
        self.assertEqual(entry["state"], "finding")
        self.assertTrue(entry["why"])
        self.assertTrue(entry["remedy"], "a finding prints its remedy")
        return entry

    def test_an_error_session_is_dead(self):
        self.make_steward(deck_status="error")
        entry = self.assert_finding("dead", "alive")
        self.assertIn("error", entry["why"])

    def test_a_stopped_session_is_dead(self):
        self.make_steward(deck_status="stopped")
        self.assert_finding("dead", "alive")

    def test_a_session_agent_deck_has_never_heard_of_is_dead(self):
        self.make_steward(in_deck=False)
        entry = self.assert_finding("dead", "alive")
        self.assertIn("no session", entry["why"])

    def test_a_branch_that_has_not_moved_in_48h_is_idle(self):
        self.make_steward(last_commit=NOW_TS - 72 * HOUR)
        entry = self.assert_finding("idle", "commit-age")
        self.assertIn("3d", entry["why"])

    def test_notes_moving_without_state_is_drifting_state(self):
        row = self.make_steward(last_commit=NOW_TS - 30 * HOUR,
                                state_at=NOW_TS - 30 * HOUR)
        self.append_notes(row, NOW_TS - 2 * HOUR)
        entry = self.assert_finding("drifting:state", "state-current")
        self.assertIn("NOTES.md", entry["why"])

    def test_a_no_change_note_alone_still_flags_and_says_how_to_restamp(self):
        """flo-88, 2026-09-23: bc5b03c "checkpoint: no change" touched only
        NOTES.md. From git that is the same commit as a round that forgot to
        rewrite STATE.md, so it stays a finding (the invariant), and the
        remedy names the restamp that makes a no-op round read as current."""
        row = self.make_steward(last_commit=NOW_TS - 30 * HOUR,
                                state_at=NOW_TS - 30 * HOUR)
        self.append_notes(row, NOW_TS - 2 * HOUR)
        entry = self.assert_finding("drifting:state", "state-current")
        self.assertIn("Last round:", entry["remedy"])

    def test_a_no_change_round_that_restamps_state_is_not_drift(self):
        """The round protocol's no-op shape: nothing new, so STATE.md is only
        restamped, but in the SAME commit as the note. Re-run after it, as
        the hub did three ticks running on flo-88, the check stays ok."""
        row = self.make_steward(last_commit=NOW_TS - 30 * HOUR,
                                state_at=NOW_TS - 30 * HOUR)
        project = os.path.join(row["worktree"], "project")
        for tick in (3, 2, 1):
            at = NOW_TS - tick * HOUR
            with open(os.path.join(project, "STATE.md"), "a",
                      encoding="utf-8") as fh:
                fh.write(f"\nLast round: {tick}h ago — checkpoint, "
                         "nothing moved\n")
            with open(os.path.join(project, "NOTES.md"), "a",
                      encoding="utf-8") as fh:
                fh.write(f"\n## {tick}h ago — checkpoint: no change\n")
            self.git(row["worktree"], "add", "project/STATE.md",
                     "project/NOTES.md")
            self.git(row["worktree"], "commit", "-q", "-m",
                     "notes: DEMO-42 -- checkpoint: no change", at=at)
            for name in ("STATE.md", "NOTES.md"):
                os.utime(os.path.join(project, name), (at, at))
            report, code = self.report()
            with self.subTest(tick=tick):
                self.assertEqual(
                    self.check_of(report, "state-current")["state"], "ok")
                self.assertEqual(code, 0)


    def test_an_uncommitted_state_rewrite_still_counts_as_current(self):
        """"file + git": the later of the two is what counts as a move, so a
        round that has rewritten STATE.md and not committed yet is not drift."""
        row = self.make_steward(last_commit=NOW_TS - 30 * HOUR,
                                state_at=NOW_TS - 30 * HOUR)
        self.append_notes(row, NOW_TS - 2 * HOUR)
        os.utime(os.path.join(row["worktree"], "project", "STATE.md"),
                 (NOW_TS - HOUR, NOW_TS - HOUR))
        report, code = self.report()
        self.assertEqual(self.check_of(report, "state-current")["state"], "ok")
        self.assertEqual(code, 0)

    def test_an_oversized_state_file_is_drifting_size(self):
        self.make_steward(state_bytes=sh.STATE_MAX_BYTES + 1)
        entry = self.assert_finding("drifting:size", "state-size")
        self.assertEqual(entry["detail"]["cap"], sh.STATE_MAX_BYTES)

    def test_context_over_the_rows_threshold_is_a_finding(self):
        self.make_steward(context_tokens=512_000, threshold=300_000)
        entry = self.assert_finding("context", "context")
        self.assertEqual(entry["detail"]["tokens"], 512_000)
        self.assertIn("checkpoint", entry["remedy"])

    def test_d1_at_exactly_the_default_threshold_is_already_a_finding(self):
        """the operator 2026-09-21 the contract: hub-driven compaction at 300k."""
        self.assertEqual(sh.DEFAULT_CONTEXT_THRESHOLD, 300_000)
        self.make_steward(context_tokens=300_000,
                          row_extra={"context_threshold": None})
        entry = self.assert_finding("context", "context")
        self.assertIn("at or over", entry["why"])

    def test_d1_just_under_the_default_threshold_is_ok(self):
        self.make_steward(context_tokens=299_999,
                          row_extra={"context_threshold": None})
        report, code = self.report()
        self.assertEqual(self.check_of(report, "context")["state"], "ok")
        self.assertEqual(code, 0)

    def test_d1_remedy_is_checkpoint_then_compact_in_that_order(self):
        self.make_steward(context_tokens=310_000, threshold=300_000)
        remedy = self.assert_finding("context", "context")["remedy"]
        self.assertIn('"checkpoint"', remedy)
        self.assertIn('"/compact"', remedy)
        self.assertLess(remedy.index('"checkpoint"'), remedy.index('"/compact"'))
        self.assertIn("only while it is waiting", remedy)

    def test_the_threshold_is_the_rows_own(self):
        self.make_steward(context_tokens=250_000, threshold=200_000)
        entry = self.assert_finding("context", "context")
        self.assertEqual(entry["detail"]["threshold"], 200_000)

    def test_a_row_with_no_threshold_falls_back_to_the_schema_default(self):
        self.make_steward(context_tokens=400_000,
                          row_extra={"context_threshold": None})
        entry = self.assert_finding("context", "context")
        self.assertEqual(entry["detail"]["threshold"],
                         sh.DEFAULT_CONTEXT_THRESHOLD)
        self.assertTrue(entry["detail"]["threshold_defaulted"])

    def test_an_overdue_dispatch_of_its_own_is_an_unattended_worker(self):
        row = self.make_steward()
        self.write_fixture("due.json", {
            "ephemeral - build-alpha-t3": {
                "kind": "build", "ref": "DEMO-42", "task_id": "t-60",
                "status": "open", "check_after": "2026-09-20T09:00:00Z",
                "worktree": os.path.join(row["worktree"], "children", "t3"),
            }})
        entry = self.assert_finding("unattended-worker", "workers")
        self.assertEqual(entry["detail"]["overdue"],
                         ["ephemeral - build-alpha-t3"])

    def test_an_external_worktree_outside_the_steward_is_still_attributed(self):
        """A external dispatch stands at /tmp/example/build-wt-<slug>, never
        inside the steward's worktree, so the worktree test alone would miss
        every one of them."""
        self.make_steward()
        self.write_fixture("due.json", {
            "ephemeral - build-239": {
                "kind": "build", "status": "open",
                "worktree": "/tmp/example/build-wt-project-alpha-t1",
                "origin": {"slug": "project-alpha-t1", "repo": "example/widget"},
            }})
        self.assert_finding("unattended-worker", "workers")

    def test_a_missing_settings_file_is_ungated(self):
        self.make_steward(gate=False)
        entry = self.assert_finding("ungated", "gate")
        self.assertIn("settings.json", entry["why"])

    def test_a_settings_file_that_does_not_name_the_gate_is_ungated(self):
        row = self.make_steward()
        self.write(os.path.join(row["worktree"], "stewards", ".claude",
                                "settings.json"),
                   json.dumps({"hooks": {}}))
        self.assert_finding("ungated", "gate")

    def test_two_findings_on_one_steward_are_both_reported(self):
        self.make_steward(deck_status="error", gate=False)
        report, code = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(sorted(self.row_of(report)["findings"]),
                         ["dead", "ungated"])


class PausedTest(StewardCase):
    def test_a_paused_row_is_never_idle(self):
        self.make_steward(status="paused", last_commit=NOW_TS - 200 * HOUR)
        report, code = self.report()
        entry = self.check_of(report, "commit-age")
        self.assertEqual(entry["state"], "not_due")
        self.assertIsNone(entry["finding"])
        self.assertEqual(entry["reading"]["outcome"], "paused")
        self.assertEqual(entry["reading"]["warrant"], obs.NOT_DUE)
        self.assertEqual(self.row_of(report)["verdict"], "ok",
                         "a boundary is neither a hole nor a finding")
        self.assertEqual(code, 0)

    def test_a_paused_row_is_still_asked_every_other_question(self):
        self.make_steward(status="paused", last_commit=NOW_TS - 200 * HOUR,
                          gate=False)
        report, code = self.report()
        self.assertEqual(self.row_of(report)["findings"], ["ungated"])
        self.assertEqual(code, 1)

    def test_a_retired_row_is_not_checked_at_all(self):
        self.make_steward(status="retired", deck_status="error", gate=False)
        report, code = self.report()
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["retired"], 1)
        self.assertEqual(code, 0)


class ContextReadTest(StewardCase):
    """Context is the LAST usage record, not the largest and not a
    subagent's."""

    def transcript(self, row, *lines):
        directory = os.path.join(self.transcripts, encode(row["worktree"]))
        os.makedirs(directory, exist_ok=True)
        self.write(os.path.join(directory, "session.jsonl"),
                   "".join(line + "\n" for line in lines))

    def test_context_is_the_last_usage_record_not_the_max(self):
        """A maximum would report a compaction that already happened forever.
        The number this check needs is what the session is carrying NOW."""
        row = self.make_steward(threshold=300_000)
        self.transcript(row,
                        usage_line(950_000, NOW - dt.timedelta(minutes=20)),
                        usage_line(40_000, NOW - dt.timedelta(minutes=2)))
        report, code = self.report()
        entry = self.check_of(report, "context")
        self.assertEqual(entry["detail"]["tokens"], 40_000)
        self.assertEqual(entry["state"], "ok")
        self.assertEqual(code, 0)

    def test_a_subagent_record_is_not_the_sessions_context(self):
        row = self.make_steward(threshold=300_000)
        self.transcript(row,
                        usage_line(880_000, NOW - dt.timedelta(minutes=5)),
                        usage_line(30_000, NOW - dt.timedelta(minutes=1),
                                   sidechain=True))
        report, code = self.report()
        entry = self.check_of(report, "context")
        self.assertEqual(entry["detail"]["tokens"], 880_000)
        self.assertEqual(entry["finding"], "context")
        self.assertEqual(code, 1)

    def test_a_line_that_is_not_json_does_not_stop_the_read(self):
        row = self.make_steward(threshold=300_000)
        self.transcript(row,
                        usage_line(500_000, NOW - dt.timedelta(minutes=9)),
                        '{"type":"assistant","usage": truncated')
        report, _ = self.report()
        self.assertEqual(self.check_of(report, "context")["detail"]["tokens"],
                         500_000)

    def test_the_newest_transcript_in_either_directory_wins(self):
        """Section 3.1 puts a steward's cwd at `<worktree>/stewards/`; the
        three that already exist ran with the worktree itself as cwd."""
        row = self.make_steward(threshold=300_000)
        directory = os.path.join(
            self.transcripts, encode(os.path.join(row["worktree"],
                                                  "stewards")))
        os.makedirs(directory)
        self.write(os.path.join(directory, "newer.jsonl"),
                   usage_line(700_000, NOW) + "\n", at=NOW_TS)
        report, code = self.report()
        entry = self.check_of(report, "context")
        self.assertEqual(entry["detail"]["tokens"], 700_000)
        self.assertEqual(code, 1)

    def test_a_transcript_directory_this_run_cannot_find_is_unknown(self):
        self.make_steward(transcript=False)
        report, code = self.report()
        entry = self.check_of(report, "context")
        self.assertEqual(entry["state"], "unknown")
        self.assertEqual(entry["reading"]["outcome"], "no_transcript")
        self.assertEqual(code, 2)

    def test_the_project_directory_name_is_claude_codes_own(self):
        self.assertEqual(
            sh.project_dir_name("/home/example/.claude/skills/peer-panel"),
            "-home-example--claude-skills-peer-panel")
        self.assertEqual(
            sh.project_dir_name("/home/example/github/example/operations/"
                                "state/hub/worktrees/project-alpha"),
            "-home-example-github-example-operations-state-hub-worktrees-"
            "project-alpha")


class UnknownTest(StewardCase):
    """A look that could not be made is never `ok` and never a finding."""

    def test_a_deck_read_failure_is_unknown_and_exit_2(self):
        self.make_steward()
        broken = self.fake("broken-deck", FAKE_BROKEN)
        report, code = self.report(deck_cmd=broken)
        entry = self.check_of(report, "alive")
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["finding"])
        self.assertEqual(entry["reading"]["outcome"], "deck_error")
        self.assertFalse(obs.absence_is_evidence(entry["reading"]))
        self.assertEqual(code, 2)

    def test_a_deck_answer_that_is_not_json_is_unreadable(self):
        self.make_steward()
        self.write(self.deck_fixture, "not json at all")
        report, code = self.report()
        entry = self.check_of(report, "alive")
        self.assertEqual(entry["reading"]["outcome"], "unreadable")
        self.assertEqual(code, 2)

    def test_a_dispatch_store_failure_is_unknown_not_no_workers(self):
        self.make_steward()
        broken = self.fake("broken-dispatches", FAKE_BROKEN)
        report, code = self.report(dispatch_cmd=broken)
        entry = self.check_of(report, "workers")
        self.assertEqual(entry["state"], "unknown")
        self.assertEqual(entry["reading"]["outcome"], "store_error")
        self.assertEqual(code, 2)

    def test_a_worktree_that_is_not_on_this_disk_is_unknown(self):
        self.make_steward(worktree=os.path.join(self.tmp, "gone"))
        os.rename(os.path.join(self.tmp, "gone"),
                  os.path.join(self.tmp, "moved"))
        report, code = self.report()
        row = self.row_of(report)
        self.assertEqual(code, 2)
        self.assertEqual(row["verdict"], "unknown")
        for name in ("commit-age", "state-current", "state-size", "gate"):
            with self.subTest(check=name):
                entry = self.check_of(report, name)
                self.assertEqual(entry["state"], "unknown")
                self.assertEqual(entry["reading"]["outcome"], "no_worktree")

    def test_a_registry_row_that_will_not_parse_is_one_unknown(self):
        self.write(os.path.join(self.registry, "broken.json"), "{oops")
        report, code = self.report()
        row = self.row_of(report, "broken")
        self.assertEqual(code, 2)
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual(len(row["checks"]), 1)
        self.assertEqual(row["checks"][0]["reading"]["outcome"], "unreadable")

    def test_a_worktree_with_no_project_home_is_unknown_not_clean(self):
        row = self.make_steward()
        for name in ("STATE.md", "NOTES.md"):
            os.remove(os.path.join(row["worktree"], "project", name))
        self.git(row["worktree"], "add", "-A")
        self.git(row["worktree"], "commit", "-q", "-m", "drop project",
                 at=NOW_TS - HOUR)
        report, code = self.report()
        self.assertEqual(code, 2)
        for name in ("state-current", "state-size"):
            with self.subTest(check=name):
                entry = self.check_of(report, name)
                self.assertEqual(entry["state"], "unknown")
                self.assertEqual(entry["reading"]["outcome"], "no_project")

    def test_a_finding_and_an_unknown_together_exit_2(self):
        """2 outranks 1: a check that could not be made may be hiding another
        finding, and the third verdict is never a pass."""
        self.make_steward(deck_status="error", transcript=False)
        report, code = self.report()
        row = self.row_of(report)
        self.assertEqual(row["findings"], ["dead"])
        self.assertEqual(row["unknowns"], ["context"])
        self.assertEqual(row["verdict"], "finding",
                         "the row leads with the actionable answer")
        self.assertEqual(code, 2)


class RegistryTest(StewardCase):
    def test_no_registry_dir_is_not_attempted_and_exits_0(self):
        import shutil
        shutil.rmtree(os.path.join(self.tmp, "state"))
        out = self.run_tool()
        self.assertEqual(out.returncode, 0)
        self.assertIn("no stewards registered", out.stdout)
        self.assertIn("NOT a pass on stewards", out.stdout)
        report, code = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["readings"][0]["outcome"], "no_registry")
        self.assertEqual(report["readings"][0]["warrant"], obs.NOT_ATTEMPTED)
        self.assertFalse(obs.absence_is_evidence(report["readings"][0]),
                         "exit 0 here is 'no look was owed', not a pass")

    def test_an_empty_registry_dir_is_an_observed_empty(self):
        """The directory is there and holds no row: somebody looked, the look
        worked, and there is genuinely nothing registered."""
        report, code = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["readings"][0]["outcome"], "ok")
        self.assertTrue(obs.absence_is_evidence(report["readings"][0]))

    def test_a_registry_nobody_wrote_reads_no_session_list_at_all(self):
        """Nothing is owed a look, so agent-deck is never called."""
        import shutil
        shutil.rmtree(os.path.join(self.tmp, "state"))
        report, _ = self.report()
        self.assertEqual([r["source"] for r in report["readings"]],
                         [report["readings"][0]["source"]])

    def test_the_state_dir_override_takes_either_shape(self):
        """`$STEWARD_STATE_DIR` may name `state/` or the rows directly, and B2
        and B3 must not be able to disagree about which."""
        self.make_steward()
        env = dict(os.environ)
        env["STEWARD_STATE_DIR"] = self.registry      # the rows themselves
        env["ESTATE_STATE_DIR"] = os.path.join(self.tmp, "state")
        env["STEWARD_TRANSCRIPT_DIR"] = self.transcripts
        env["STEWARD_HEALTH_NOW"] = NOW_ISO
        env["STEWARD_DECK_CMD"] = f"{self.deck_cmd} {self.deck_fixture}"
        env["STEWARD_DISPATCHES_CMD"] = (f"{self.dispatch_cmd} "
                                         f"{self.dispatch_fixture}")
        out = subprocess.run([sys.executable, SCRIPT, "--json", "--profile",
                              PROFILE], env=env, capture_output=True,
                             text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([r["slug"] for r in json.loads(out.stdout)["rows"]],
                         ["project-alpha"])

    def test_slug_narrows_what_is_checked(self):
        self.make_steward("project-alpha")
        self.make_steward("catalog-meta", deck_status="error")
        report, code = self.report("--slug", "project-alpha")
        self.assertEqual([r["slug"] for r in report["rows"]], ["project-alpha"])
        self.assertEqual(code, 0)
        self.assertTrue(report["registered"] == 2)

    def test_both_stewards_are_checked_by_default(self):
        self.make_steward("project-alpha")
        self.make_steward("catalog-meta", deck_status="error")
        report, code = self.report()
        self.assertEqual(sorted(r["slug"] for r in report["rows"]),
                         ["catalog-meta", "project-alpha"])
        self.assertEqual(code, 1)


class FixtureFlagTest(StewardCase):
    def test_the_two_answers_can_be_read_from_files_instead(self):
        self.make_steward()
        report, code = self.report("--deck-json", self.deck_fixture,
                                   "--dispatches-json", self.dispatch_fixture,
                                   deck_cmd=self.fake("never", FAKE_BROKEN),
                                   dispatch_cmd=self.fake("never2",
                                                          FAKE_BROKEN))
        self.assertEqual(code, 0)
        self.assertEqual(self.row_of(report)["verdict"], "ok")


class DispatchJoinTest(StewardCase):
    def test_recorded_owner_finds_a_renamed_descendant(self):
        self.make_steward("project-alpha")
        self.write_fixture("due.json", {"unrelated-title": {
            "kind": "build", "status": "open", "dispatch_id": "child",
            "parent_dispatch_id": "removed-parent", "owner_steward_slug": "project-alpha"}})
        report, code = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(self.check_of(report, "workers")["detail"]["overdue"],
                         ["unrelated-title"])

    def test_explicit_other_or_unknown_ownership_overrides_title_and_path(self):
        row = self.make_steward("project-alpha")
        for provenance in ({"owner_steward_slug": "another"},
                           {"dispatch_id": "new", "provenance_state": "unknown"},
                           {"owner_steward_slug": None}):
            with self.subTest(provenance=provenance):
                self.write_fixture("due.json", {"ephemeral - project-alpha-worker": {
                    "kind": "build", "status": "open", **provenance,
                    "worktree": row["worktree"], "origin": {"slug": "project-alpha-t1"}}})
                report, code = self.report()
                self.assertEqual(self.check_of(report, "workers")["detail"]["overdue"], [])

    def test_another_stewards_dispatch_is_not_attributed_to_this_one(self):
        self.make_steward("project-alpha")
        self.write_fixture("due.json", {
            "ephemeral - build-catalog-meta-t1": {
                "kind": "build", "status": "open",
                "worktree": "/tmp/example/build-wt-catalog-meta-t1",
                "origin": {"slug": "catalog-meta-t1"},
            }})
        report, code = self.report()
        entry = self.check_of(report, "workers")
        self.assertEqual(entry["state"], "ok")
        self.assertEqual(entry["detail"]["overdue"], [])
        self.assertEqual(entry["detail"]["due_in_store"], 1)
        self.assertEqual(code, 0)


class VocabularyTest(unittest.TestCase):
    def test_the_vocabulary_is_registered_and_can_conclude_an_absence(self):
        self.assertIn("ok", obs.vocabulary(sh.VOCAB).outcomes_in(obs.OBSERVED))

    def test_every_word_but_ok_denies_an_absence(self):
        vocab = obs.vocabulary(sh.VOCAB)
        for outcome in vocab.outcomes:
            with self.subTest(outcome=outcome):
                self.assertEqual(vocab.absence_is_evidence(outcome),
                                 outcome == "ok")

    def test_paused_is_the_one_boundary(self):
        """Section 3.4: a paused steward stops being called idle because the operator
        paused it, which is a boundary and not a hole."""
        self.assertEqual(obs.vocabulary(sh.VOCAB).outcomes_in(obs.NOT_DUE),
                         ("paused",))

    def test_every_finding_has_a_check_that_can_raise_it(self):
        raisable = {finding for _, finding, _ in sh.CHECKS}
        self.assertEqual(raisable, {sh.DEAD, sh.IDLE, sh.DRIFTING_STATE,
                                    sh.DRIFTING_SIZE, sh.CONTEXT,
                                    sh.UNATTENDED_WORKER, sh.UNGATED})


class DefinitionsTest(unittest.TestCase):
    def test_list_reads_no_state_and_exits_0(self):
        out = subprocess.run([sys.executable, SCRIPT, "--list"],
                             capture_output=True, text=True,
                             env={**os.environ,
                                  "STEWARD_STATE_DIR": "/nonexistent"})
        self.assertEqual(out.returncode, 0, out.stderr)
        for finding in (sh.DEAD, sh.IDLE, sh.DRIFTING_STATE, sh.DRIFTING_SIZE,
                        sh.CONTEXT, sh.UNATTENDED_WORKER, sh.UNGATED):
            self.assertIn(finding, out.stdout)

    def test_help_exits_0(self):
        out = subprocess.run([sys.executable, SCRIPT, "--help"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

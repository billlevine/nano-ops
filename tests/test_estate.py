#!/usr/bin/env python3
"""Tests for bin/estate. Run: python3 tests/test_estate.py.

Every test points $ESTATE_STATE_DIR at a fresh tempdir, never real state/.
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "bin", "estate")


def run_cli(args, state_dir):
    env = dict(os.environ, ESTATE_STATE_DIR=state_dir,
               ESTATE_LESSONS_PATH=os.path.join(state_dir, "lessons.md"))
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True,
                          text=True, env=env)


class EstateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.db = os.path.join(self.state, "estate.db")
        self.lessons = os.path.join(self.state, "lessons.md")

    def tearDown(self): self.tmp.cleanup()
    def cli(self, *args): return run_cli(list(args), self.state)
    def ok(self, *args):
        result = self.cli(*args); self.assertEqual(result.returncode, 0, result.stderr); return result
    def conn(self):
        c = sqlite3.connect(self.db); c.row_factory = sqlite3.Row; return c
    def task(self, key="t-1"):
        with self.conn() as c: return dict(c.execute("SELECT * FROM tasks WHERE id=?", (key,)).fetchone())
    def events(self, key="t-1"):
        with self.conn() as c: return [dict(r) for r in c.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (key,))]
    def add(self, title="task", ready=False):
        args = ["task", "add", title, "--kind", "generic"]
        if ready: args.append("--ready")
        return self.ok(*args)

    def memory(self, key="m-1"):
        with self.conn() as c: return dict(c.execute("SELECT * FROM memories WHERE id=?", (key,)).fetchone())

    def memory_events(self, key="m-1"):
        with self.conn() as c: return [dict(r) for r in c.execute("SELECT * FROM events WHERE memory_id=? ORDER BY seq", (key,))]

    def remember(self, body="a lesson", scope="mechanic", refs=None, active=False, protected=False):
        args = ["memory", "add", body, "--scope", scope]
        if refs: args += ["--source-refs", refs]
        if active: args += ["--status", "active"]
        if protected: args.append("--protected")
        return self.ok(*args).stdout.strip()

    def shared(self, body="a shared lesson", refs='{"ledger":[1]}'):
        return self.remember(body, scope="shared", refs=refs, active=True)


class TestSchema(EstateCase):
    def test_init_creates_wal_database_and_triggers(self):
        self.ok("init")
        with self.conn() as c:
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertEqual(names, {"events_no_update", "events_no_delete",
                                 "memories_body_immutable", "memories_no_delete",
                                 "asks_no_delete"})

    def test_events_are_append_only(self):
        self.add()
        with self.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError): c.execute("UPDATE events SET summary='x'")
            with self.assertRaises(sqlite3.IntegrityError): c.execute("DELETE FROM events")


class TestCreateAndQuery(EstateCase):
    def test_project_ids_increment_and_are_not_reused(self):
        self.assertEqual(self.ok("project", "add", "one", "--kind", "estate").stdout.strip(), "p-1")
        self.assertEqual(self.ok("project", "add", "two", "--kind", "estate").stdout.strip(), "p-2")
        self.ok("project", "close", "p-1", "--status", "dropped")
        self.assertEqual(self.ok("project", "add", "three", "--kind", "estate").stdout.strip(), "p-3")

    def test_task_ids_and_initial_statuses(self):
        self.assertEqual(self.add().stdout.strip(), "t-1")
        self.assertEqual(self.task()["status"], "open")
        self.add("ready", True)
        self.assertEqual(self.task("t-2")["status"], "ready")

    def test_create_and_transition_each_write_events(self):
        self.add(); self.assertEqual(len(self.events()), 1)
        self.ok("mark-ready", "1", "queued")
        self.assertEqual([e["kind"] for e in self.events()], ["activity", "transition"])

    def test_log_and_bare_numeric_id(self):
        self.add(); self.ok("note", "1", "hello")
        result = self.ok("log", "1")
        self.assertIn("task created", result.stdout); self.assertIn("hello", result.stdout)


class TestTaskFind(EstateCase):
    """`task find --ref K=V` — the resolve half of create-or-resolve (P-01).

    A caller holding an identity the store keeps in `refs` (a Slack intake id,
    a PR key, a queue key) had no read command that could reach it, so it
    either re-read the whole store or gave up and created a duplicate."""

    def seed(self, title, refs, kind="generic"):
        return self.ok("task", "add", title, "--kind", kind,
                       "--refs", refs).stdout.strip()

    def test_it_finds_the_task_carrying_a_refs_key(self):
        wanted = self.seed("the one", '{"intake_id": "intake-abc"}')
        self.seed("another", '{"intake_id": "intake-def"}')
        self.assertEqual(self.ok("task", "find", "--ref", "intake_id=intake-abc",
                                 "--quiet").stdout.split(), [wanted])

    def test_no_match_prints_nothing_and_still_succeeds(self):
        """Absence is an answer, not an error — it is what makes the caller
        create instead of resolve."""
        self.seed("the one", '{"intake_id": "intake-abc"}')
        r = self.ok("task", "find", "--ref", "intake_id=nope", "--quiet")
        self.assertEqual(r.stdout.strip(), "")

    def test_matching_is_exact_never_a_substring(self):
        self.seed("the one", '{"pr": "acme/widgets#2040"}')
        self.assertEqual(self.ok("task", "find", "--ref", "pr=2040",
                                 "--quiet").stdout.strip(), "")
        self.assertEqual(self.ok("task", "find", "--ref", "pr=acme/widgets#204",
                                 "--quiet").stdout.strip(), "")

    def test_a_task_with_no_refs_at_all_is_skipped_not_crashed_on(self):
        self.add()                                   # refs is NULL
        self.seed("the one", '{"intake_id": "intake-abc"}')
        self.assertEqual(len(self.ok("task", "find", "--ref",
                                     "intake_id=intake-abc",
                                     "--quiet").stdout.split()), 1)

    def test_two_tasks_on_one_key_are_both_reported(self):
        """One intent with two tasks is a real failure. The caller has to be
        able to SEE it rather than silently take the first."""
        a = self.seed("first", '{"intake_id": "intake-abc"}')
        b = self.seed("second", '{"intake_id": "intake-abc"}')
        self.assertEqual(self.ok("task", "find", "--ref", "intake_id=intake-abc",
                                 "--quiet").stdout.split(), [a, b])

    def test_other_filters_still_narrow_the_result(self):
        self.seed("a pr task", '{"pr": "acme/gizmo#1"}', kind="pr-action")
        self.seed("a note", '{"pr": "acme/gizmo#1"}', kind="generic")
        self.assertEqual(len(self.ok("task", "find", "--ref", "pr=acme/gizmo#1",
                                     "--kind", "pr-action",
                                     "--quiet").stdout.split()), 1)

    def test_a_ref_without_an_equals_sign_is_refused(self):
        r = self.cli("task", "find", "--ref", "intake_id")
        self.assertEqual(r.returncode, 2)
        self.assertIn("KEY=VALUE", r.stderr)

    def test_json_output_carries_the_whole_row(self):
        self.seed("the one", '{"intake_id": "intake-abc"}')
        rows = json.loads(self.ok("task", "find", "--ref",
                                  "intake_id=intake-abc", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "the one")


class TestLifecycle(EstateCase):
    def test_happy_path_and_evidence_envelope(self):
        self.add(); self.ok("mark-ready", "1"); self.ok("claim", "1", "--actor", "worker")
        self.ok("done", "1", "--actor", "worker", "--summary", "landed",
                "--verification", "tests green", "--dependencies", "none",
                "--retry-notes", "rerun", "--residual-risk", "low",
                "--refs", '{"branch":"topic"}')
        self.assertEqual(self.task()["status"], "done")
        events = self.events(); self.assertEqual(events[-2]["kind"], "transition"); self.assertEqual(events[-1]["kind"], "evidence")
        refs = json.loads(events[-1]["refs"])
        self.assertEqual(refs, {"summary": "landed", "verification": "tests green",
            "dependencies": "none", "retry_notes": "rerun", "residual_risk": "low", "branch": "topic"})

    def test_landing_keys_are_first_class_and_never_required(self):
        """t-712 panel P1's write side: structure, refusing nothing.

        The three properties in one test because they are one decision — the
        keys are recorded when given, absent when not, and no shape of them is
        ever a reason for `done` to fail. A completion that landed nothing
        gitty must close exactly as it did before this existed.
        """
        self.add(); self.ok("mark-ready", "1"); self.ok("claim", "1", "--actor", "w")
        self.ok("done", "1", "--actor", "w", "--summary", "s",
                "--landed-branch", "topic/one", "--landed-sha", "abc1234",
                "--landed-repo", "/somewhere/else")
        refs = json.loads(self.events()[-1]["refs"])
        self.assertEqual(refs["landed_branch"], "topic/one")
        self.assertEqual(refs["landed_sha"], "abc1234")
        self.assertEqual(refs["landed_repo"], "/somewhere/else")

        self.add("second"); self.ok("mark-ready", "2"); self.ok("claim", "2", "--actor", "w")
        self.ok("done", "2", "--actor", "w", "--summary", "nothing gitty")
        plain = json.loads(self.events("t-2")[-1]["refs"])
        self.assertNotIn("landed_sha", plain)
        self.assertNotIn("landed_branch", plain)

        # A sha nothing could resolve is still recorded rather than refused.
        # The reconciler reports it `sha_unknown`; a gate here would fire on
        # the unattended 3am path with nobody awake to unblock it.
        self.add("third"); self.ok("mark-ready", "3"); self.ok("claim", "3", "--actor", "w")
        self.ok("done", "3", "--actor", "w", "--landed-sha", "not-a-sha at all")
        self.assertEqual(json.loads(self.events("t-3")[-1]["refs"])["landed_sha"],
                         "not-a-sha at all")

    def test_a_landing_flag_wins_over_the_same_key_in_refs(self):
        """The flag is the first-class path; --refs is where a caller that
        predates it reaches the same field. Both spellings land in one key, and
        when they disagree the explicit one is the answer."""
        self.add(); self.ok("mark-ready", "1"); self.ok("claim", "1", "--actor", "w")
        self.ok("done", "1", "--actor", "w",
                "--refs", '{"landed_sha":"0000000","pr":"o/r#1"}',
                "--landed-sha", "9999999")
        refs = json.loads(self.events()[-1]["refs"])
        self.assertEqual(refs["landed_sha"], "9999999")
        self.assertEqual(refs["pr"], "o/r#1")

    def test_a_transition_carries_its_evidence_in_the_same_transaction(self):
        """`done`'s bargain, on every other edge (t-296 follow-up).

        A state whose precondition is a fact about the outside world —
        `needs-owner` and its top-level relay post — has to record that fact with
        the move, not after it. Written second it is one failed call away from a
        parked task nothing on file justifies.
        """
        self.add(ready=True); self.ok("claim", "1", "--actor", "hub")
        self.ok("needs-owner", "1", "your call", "--actor", "hub",
                "--evidence", '{"slack_ts": "1754000900.000200", "top_level": true}')
        self.assertEqual(self.task()["status"], "needs-owner")
        evidence = [e for e in self.events() if e["kind"] == "evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(json.loads(evidence[0]["refs"]),
                         {"slack_ts": "1754000900.000200", "top_level": True})

    def test_evidence_that_cannot_be_recorded_takes_the_transition_with_it(self):
        """The rollback is the point: no move without the record for it."""
        self.add(ready=True); self.ok("claim", "1", "--actor", "hub")
        for bad in ("{not json", '"a string"', "[1, 2]"):
            with self.subTest(bad):
                r = self.cli("needs-owner", "1", "your call", "--actor", "hub",
                             "--evidence", bad)
                self.assertEqual(r.returncode, 2)
                self.assertIn("--evidence", r.stderr)
                self.assertEqual(self.task()["status"], "claimed")
                self.assertEqual([e for e in self.events()
                                  if e["kind"] == "evidence"], [])

    def test_claim_is_atomic_and_only_from_ready(self):
        self.add()
        self.assertEqual(self.cli("claim", "1", "--actor", "a").returncode, 2)
        self.ok("mark-ready", "1"); self.ok("claim", "1", "--actor", "a")
        before = self.task()
        self.assertEqual(self.cli("claim", "1", "--actor", "b").returncode, 2)
        self.assertEqual(self.task()["claimed_by"], before["claimed_by"])

    def test_lease_and_extend_owner(self):
        self.add(ready=True); self.ok("claim", "1", "--actor", "a", "--ttl", "1m")
        first = dt.datetime.fromisoformat(self.task()["claim_expires_at"])
        self.assertGreater(first, dt.datetime.now(dt.timezone.utc))
        self.ok("extend", "1", "--actor", "a", "--ttl", "2h")
        self.assertGreater(dt.datetime.fromisoformat(self.task()["claim_expires_at"]), first)
        self.assertEqual(self.cli("extend", "1", "--actor", "b").returncode, 2)

    def test_illegal_transition_leaves_status_unchanged(self):
        self.add(); result = self.cli("done", "1", "--actor", "a")
        self.assertEqual(result.returncode, 2); self.assertEqual(self.task()["status"], "open")

    def test_reopen_done_clears_claim_and_closed_at(self):
        self.add(ready=True); self.ok("claim", "1", "--actor", "a"); self.ok("done", "1", "--actor", "a")
        self.ok("reopen", "1")
        row = self.task(); self.assertEqual(row["status"], "ready"); self.assertIsNone(row["claimed_by"]); self.assertIsNone(row["closed_at"])


class TestPrActionCompletionEvidence(EstateCase):
    """t-147 — a `pr-action` task cannot be closed on a worker's word.

    The dispatched worker pushes the fix and replies on each review thread,
    and those replies are the only record the owner reads instead of diffing the
    PR. Its report that it did so is not evidence: the collector has to
    re-check `gh api …/pulls/N/comments` itself and pass what it saw. So
    `--verification`, optional on every other kind, is required here — and
    refused rather than warned, because a warning in an unattended sweep is a
    warning nobody reads.
    """

    def pr_task(self, key="1", claim=True):
        self.ok("task", "add", "acme/widgets#2022 — address review comments",
                "--kind", "pr-action", "--ready")
        if claim:
            self.ok("claim", key, "--actor", "hub", "--ttl", "4h")
        return f"t-{key}"

    def test_close_without_verification_is_refused(self):
        self.pr_task()
        r = self.cli("done", "1", "--actor", "hub", "--summary", "pushed abc123")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--verification", r.stderr)
        self.assertIn("in_reply_to_id", r.stderr)      # names the re-check

    def test_a_refused_close_changes_nothing(self):
        self.pr_task()
        before = self.task()
        self.cli("done", "1", "--actor", "hub", "--summary", "pushed abc123")
        row = self.task()
        self.assertEqual(row["status"], "claimed")     # still recoverable
        self.assertEqual(row["claimed_by"], "hub")
        self.assertEqual(row["updated_at"], before["updated_at"])
        self.assertNotIn("evidence", [e["kind"] for e in self.events()])

    def test_a_blank_verification_is_no_verification(self):
        self.pr_task()
        r = self.cli("done", "1", "--actor", "hub", "--summary", "pushed abc123",
                     "--verification", "   \n ")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.task()["status"], "claimed")

    def test_close_with_verification_is_accepted_and_recorded(self):
        self.pr_task()
        self.ok("done", "1", "--actor", "hub", "--summary", "pushed abc123",
                "--verification", "reply 91 on thread 77, reply 92 on thread 81")
        self.assertEqual(self.task()["status"], "done")
        refs = json.loads(self.events()[-1]["refs"])
        self.assertEqual(refs["verification"],
                         "reply 91 on thread 77, reply 92 on thread 81")

    def test_the_wrong_actor_is_still_the_first_thing_reported(self):
        """The gate is about evidence, not about who is holding the lease."""
        self.pr_task()
        r = self.cli("done", "1", "--actor", "someone-else", "--summary", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not claimed by someone-else", r.stderr)

    def test_other_kinds_are_unaffected(self):
        """Only `pr-action`. Widening the gate would make it noise."""
        for i, kind in enumerate(("generic", "night-shift", "panel",
                                  "forge-build", "ci-fix", "hub-intent"), start=1):
            with self.subTest(kind=kind):
                key = str(i)
                self.ok("task", "add", f"a {kind} task", "--kind", kind, "--ready")
                self.ok("claim", key, "--actor", "hub")
                self.ok("done", key, "--actor", "hub", "--summary", "landed")
                self.assertEqual(self.task(f"t-{key}")["status"], "done")


class TestOrphans(EstateCase):
    def test_lists_and_sweeps_only_expired_claim(self):
        self.add("old", True); self.ok("claim", "1", "--actor", "a")
        self.add("fresh", True); self.ok("claim", "2", "--actor", "b")
        with self.conn() as c: c.execute("UPDATE tasks SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id='t-1'")
        self.assertIn("t-1", self.ok("orphans").stdout)
        self.assertNotIn("t-2", self.ok("orphans").stdout)
        self.ok("orphans", "--sweep")
        self.assertEqual(self.task("t-1")["status"], "ready"); self.assertEqual(self.task("t-2")["status"], "claimed")
        self.assertIn("orphaned", self.events("t-1")[-1]["summary"])

    def test_blocked_task_with_expired_lease_is_not_swept(self):
        # A deliberate park (blocked/needs-owner) keeps claimed_by for provenance
        # but must survive an elapsed lease — only 'claimed' tasks orphan.
        self.add("parked", True); self.ok("claim", "1", "--actor", "a")
        self.ok("block", "1", "external dep")
        with self.conn() as c: c.execute("UPDATE tasks SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id='t-1'")
        self.assertNotIn("t-1", self.ok("orphans").stdout)
        self.ok("orphans", "--sweep")
        self.assertEqual(self.task("t-1")["status"], "blocked")


class AttemptCase(EstateCase):
    """Shared fixtures for P-12 — failed attempts and retries."""

    def expire(self, key="t-1"):
        """Make this task's lease elapse without waiting for it."""
        with self.conn() as c:
            c.execute("UPDATE tasks SET claim_expires_at=? WHERE id=?",
                      ("2000-01-01T00:00:00+00:00", key))

    def attempts(self, key="t-1"):
        return [e for e in self.events(key) if e["kind"] == "failed-attempt"]

    def retries(self, key="t-1"):
        return [e for e in self.events(key) if e["kind"] == "retry"]

    def refs(self, event):
        return json.loads(event["refs"])

    def crash(self, key="t-1", actor="worker-a", session=None):
        """Claim, then die holding it, then let the hub's sweep find it."""
        env = ["--actor", actor]
        if session:
            self.ok_with_session(session, "claim", key, *env)
        else:
            self.ok("claim", key, *env)
        self.expire(key)
        self.ok("orphans", "--sweep")

    def ok_with_session(self, session, *args):
        env = dict(os.environ, ESTATE_STATE_DIR=self.state,
                   ESTATE_LESSONS_PATH=self.lessons, ESTATE_SESSION_ID=session)
        result = subprocess.run([sys.executable, SCRIPT, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result


class TestFailedAttempts(AttemptCase):
    """P-12 / S33 + S55 — a requeue that does not say the attempt failed.

    The orphan sweep used to write one line: `claimed -> ready (orphaned...)`.
    That records where the item WENT and nothing about what happened to it, so
    a task on its third attempt was indistinguishable from one nobody had
    touched, and "has this failed before, and why" had no answer in the store
    at all. Every claimed -> ready edge now puts the attempt on file first.
    """

    def test_the_sweep_records_the_attempt_before_it_requeues(self):
        self.add("work", True)
        self.crash()
        kinds = [e["kind"] for e in self.events()]
        self.assertEqual(kinds, ["activity", "transition", "failed-attempt",
                                 "transition"])
        # Order is the load-bearing part: the failure is on file BEFORE the
        # requeue, so a reader walking forward never sees the item back in the
        # pool without knowing why it got there.
        self.assertIn("failed", self.events()[-2]["summary"])
        self.assertIn("orphaned", self.events()[-1]["summary"])

    def test_the_record_names_claimant_cause_attempt_and_prior_event(self):
        self.add("work", True)
        self.crash(session="ephemeral-ci-fix-1")
        refs = self.refs(self.attempts()[0])
        self.assertEqual(refs["claimed_by"], "worker-a")
        self.assertEqual(refs["session"], "ephemeral-ci-fix-1")
        self.assertEqual(refs["cause"], "lease-expired")
        self.assertEqual(refs["attempt"], 1)
        # The prior event sequence points at the claim — the last thing the
        # dead attempt managed to record.
        claim = [e for e in self.events() if e["summary"] == "ready -> claimed"][0]
        self.assertEqual(refs["prior_event_seq"], claim["seq"])

    def test_it_is_a_qualifying_ledger_event(self):
        """P-06's shape, not a new mechanism: subsystem + phase + fingerprint."""
        self.add("work", True)
        self.crash()
        row = self.attempts()[0]
        self.assertEqual(row["subsystem"], "tasks")
        self.assertEqual(row["phase"], "failure")
        self.assertTrue(row["fingerprint"])
        found = self.ok("events", "--task", "t-1", "--subsystem", "tasks",
                        "--phase", "failure").stdout
        self.assertIn("attempt 1 failed", found)

    def test_the_requeue_transition_is_tagged_too(self):
        """Both halves searchable, or `--subsystem tasks` tells half the story."""
        self.add("work", True)
        self.crash()
        requeue = self.events()[-1]
        self.assertEqual((requeue["subsystem"], requeue["phase"]),
                         ("tasks", "transition"))

    def test_release_is_a_failed_attempt_with_its_own_cause(self):
        """A live worker handing work back still attempted it. Distinguished
        from the dead-worker case by `cause`, not by absence."""
        self.add("work", True)
        self.ok("claim", "1", "--actor", "night-shift")
        self.ok("release", "1", "out of budget", "--actor", "night-shift")
        refs = self.refs(self.attempts()[0])
        self.assertEqual(refs["cause"], "released")
        self.assertEqual(refs["claimed_by"], "night-shift")
        self.assertEqual(self.attempts()[0]["detail"], "out of budget")

    def test_parks_and_completions_are_not_failed_attempts(self):
        """`block`, `needs-owner` and `done` are not requeues. Recording a
        failure for them would make "went back to the pool" and "is waiting on
        something" the same fact."""
        for key, verb, args in (("t-1", "block", ("waiting on acme/gizmo#1",)),
                                ("t-2", "needs-owner", ("a design call",)),
                                ("t-3", "done", ())):
            self.add(key, True)
            self.ok("claim", key, "--actor", "a")
            if verb == "done":
                self.ok("done", key, "--actor", "a", "--summary", "landed")
            else:
                self.ok(verb, key, *args, "--actor", "a")
            self.assertEqual(self.attempts(key), [], f"{verb} wrote one")

    def test_attempt_numbers_count_up_across_crashes(self):
        self.add("work", True)
        self.crash(actor="worker-a")
        self.crash(actor="worker-b")
        self.assertEqual([self.refs(e)["attempt"] for e in self.attempts()], [1, 2])
        self.assertEqual([self.refs(e)["claimed_by"] for e in self.attempts()],
                         ["worker-a", "worker-b"])

    def test_a_task_that_never_failed_has_no_failed_attempt_row(self):
        self.add("work", True)
        self.ok("claim", "1", "--actor", "a")
        self.ok("done", "1", "--actor", "a", "--summary", "first time")
        self.assertEqual(self.attempts(), [])


class TestRetry(AttemptCase):
    """P-12 / S47 — a retry is a new attempt, and it says which one it follows.

    `claim` starts work. `retry` starts work AGAIN, and refuses to pretend it
    is the first time: it will not run without a failed or blocked attempt on
    record, and it appends a row naming that attempt. The original failure is
    untouched — the events table is append-only, so the whole chain stays
    readable in the order it happened.
    """

    def test_it_refuses_work_that_has_not_been_tried(self):
        self.add("work", True)
        result = self.cli("retry", "1", "--actor", "a")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no failed or blocked attempt", result.stderr)
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.retries(), [])

    def test_it_claims_and_appends_a_retry_naming_the_failure(self):
        self.add("work", True)
        self.crash()
        self.ok("retry", "1", "--actor", "worker-b", "--note", "smaller scope")
        self.assertEqual(self.task()["status"], "claimed")
        self.assertEqual(self.task()["claimed_by"], "worker-b")
        retry = self.retries()[0]
        refs = self.refs(retry)
        self.assertEqual(refs["attempt"], 2)
        self.assertEqual(refs["after_event_seq"], self.attempts()[0]["seq"])
        self.assertEqual(refs["after_cause"], "lease-expired")
        self.assertEqual(refs["prior_attempts"], 1)
        self.assertEqual(retry["detail"], "smaller scope")
        self.assertEqual((retry["subsystem"], retry["phase"]),
                         ("tasks", "initiation"))

    def test_the_first_attempt_and_its_failure_survive_the_retry(self):
        """S47's actual assertion — the prior rows keep their sequence and
        their timestamps, and the retry is a distinct row rather than an
        overwrite of either."""
        self.add("work", True)
        self.crash()
        before = self.events()
        self.ok("retry", "1", "--actor", "worker-b")
        after = self.events()
        self.assertEqual(after[:len(before)], before)
        self.assertGreater(len(after), len(before))
        self.assertEqual([e["kind"] for e in after[len(before):]],
                         ["transition", "retry"])

    def test_a_second_retry_points_at_the_second_failure(self):
        self.add("work", True)
        self.crash(actor="worker-a")
        self.ok("retry", "1", "--actor", "worker-b")
        self.expire()
        self.ok("orphans", "--sweep")
        self.ok("retry", "1", "--actor", "worker-c")
        first, second = self.retries()
        self.assertEqual(self.refs(first)["after_event_seq"],
                         self.attempts()[0]["seq"])
        self.assertEqual(self.refs(second)["after_event_seq"],
                         self.attempts()[1]["seq"])
        self.assertEqual([self.refs(r)["attempt"] for r in self.retries()], [2, 3])

    def test_it_will_not_jump_a_park_the_way_reopen_does(self):
        """A blocked task leaves through `reopen`, where a human decision
        belongs. Retry picks up from `ready` like every other claim, so there
        stays exactly one edge back into the pool."""
        self.add("work", True)
        self.ok("claim", "1", "--actor", "a")
        self.ok("block", "1", "waiting on acme/gizmo#1")
        result = self.cli("retry", "1", "--actor", "b")
        self.assertEqual(result.returncode, 2)
        self.assertIn("already claimed or not ready", result.stderr)
        self.assertEqual(self.task()["status"], "blocked")
        self.ok("reopen", "1")
        self.ok("retry", "1", "--actor", "b")
        self.assertEqual(self.task()["status"], "claimed")

    def test_a_blocked_attempt_is_prior_evidence_on_its_own(self):
        self.add("work", True)
        self.ok("claim", "1", "--actor", "a")
        self.ok("block", "1", "waiting on acme/gizmo#1")
        self.ok("reopen", "1")
        self.ok("retry", "1", "--actor", "b")
        blocked = [e for e in self.events()
                   if e["summary"] == "claimed -> blocked"][0]
        self.assertEqual(self.refs(self.retries()[0])["after_event_seq"],
                         blocked["seq"])

    def test_a_claim_race_is_still_one_winner(self):
        self.add("work", True)
        self.crash()
        self.ok("retry", "1", "--actor", "worker-b")
        result = self.cli("retry", "1", "--actor", "worker-c")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.task()["claimed_by"], "worker-b")
        self.assertEqual(len(self.retries()), 1)

    def test_an_unknown_task_is_named_rather_than_guessed_at(self):
        result = self.cli("retry", "99", "--actor", "a")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no task matches 't-99'", result.stderr)

    def test_claim_is_left_alone_for_a_first_attempt(self):
        """The gate is on retry, not on claim. Making `claim` refuse a
        previously-failed task would break every existing caller to buy
        nothing the retry row does not already record."""
        self.add("work", True)
        self.crash()
        self.ok("claim", "1", "--actor", "worker-b")
        self.assertEqual(self.task()["status"], "claimed")
        self.assertEqual(self.retries(), [])


class TestFailureIsolation(AttemptCase):
    """P-12 / S33 + S55 — one subsystem's failure loses nothing else.

    S55 is the whole-estate version of S33: an estate holding tracked work with
    dependencies, due follow-ups, scoped memories and open questions has one
    subsystem die mid-work, retry, and restart. Nothing held for any OTHER
    subsystem may be erased or corrupted, the in-flight work and its
    dependencies must remain, prior Ledger history must remain, and the failed
    attempt and the retry must be separately traceable.

    Scope, stated rather than implied: this covers what the estate store
    actually holds. Spotter state and the current brief live in their own files
    under state/pr-tracker/ and state/morning-brief/, which this code path
    never opens — that is why they cannot be lost here, and asserting on them
    from bin/estate's tests would be theatre.
    """

    def build(self):
        self.ok("project", "add", "the work", "--kind", "estate",
                "--actor", "hub")
        self.ok("task", "add", "the victim", "--kind", "rollout",
                "--project", "p-1", "--ready", "--actor", "hub")
        self.ok("task", "add", "blocked on the victim", "--kind", "rollout",
                "--project", "p-1", "--actor", "hub")
        self.ok("task", "add", "a due follow-up", "--kind", "followup",
                "--ready", "--actor", "hub", "--due", "2026-08-01",
                "--refs", '{"legacy_id": "followup:7"}')
        self.ok("dep", "add", "t-1", "t-2", "--kind", "blocks", "--actor", "hub")
        self.remember("only the mechanic sees this", scope="mechanic",
                      active=True)
        self.shared("every loop sees this")
        self.ok("ask", "which branch?", "--to", "hub", "--from", "mechanic")

    def dump(self, state=None):
        return json.loads(run_cli(["json"], state or self.state).stdout)

    def relocate(self):
        """Carry only the on-disk store somewhere nothing has ever opened —
        the restart half of the scenario, as docs/new-machine-setup.md does."""
        fresh = tempfile.mkdtemp(dir=self.tmp.name)
        for name in os.listdir(self.state):
            if name.startswith("estate.db"):
                shutil.copy2(os.path.join(self.state, name),
                             os.path.join(fresh, name))
        return fresh

    def test_a_crash_and_retry_leaves_the_rest_of_the_estate_alone(self):
        self.build()
        before = self.dump()
        self.crash(key="t-1", actor="night-shift")
        self.ok("retry", "t-1", "--actor", "night-shift")
        after = self.dump(self.relocate())

        # Nothing else moved: same projects, same dependencies, same memories,
        # same asks, and the two untouched tasks byte-identical.
        for table in ("projects", "task_deps", "memories", "asks"):
            self.assertEqual(before[table], after[table], table)
        untouched = lambda rows: [r for r in rows if r["id"] != "t-1"]
        self.assertEqual(untouched(before["tasks"]), untouched(after["tasks"]))

        # Prior Ledger history remains, unchanged and in place. The table is
        # append-only, so "remains" means the old rows are still a prefix.
        self.assertEqual(after["events"][:len(before["events"])],
                         before["events"])

        # The in-flight work and its dependency survived, and the item is back
        # in someone's hands rather than lost.
        victim = [r for r in after["tasks"] if r["id"] == "t-1"][0]
        self.assertEqual(victim["status"], "claimed")
        self.assertEqual(victim["claimed_by"], "night-shift")
        self.assertEqual(len(after["task_deps"]), 1)

        # And the failure and the retry are separately traceable.
        self.assertEqual(len(self.attempts("t-1")), 1)
        self.assertEqual(len(self.retries("t-1")), 1)
        self.assertLess(self.attempts("t-1")[0]["seq"],
                        self.retries("t-1")[0]["seq"])

    def test_the_follow_up_and_its_deadline_are_still_due(self):
        """The due follow-up belongs to a different subsystem entirely. A
        night-shift crash has no business touching it."""
        self.build()
        self.crash(key="t-1", actor="night-shift")
        self.ok("retry", "t-1", "--actor", "night-shift")
        due = json.loads(self.ok("due", "--within", "3650", "--json").stdout)
        self.assertIn("t-3", [row["id"] for row in due])

    def test_the_unanswered_question_is_still_open(self):
        self.build()
        self.crash(key="t-1", actor="night-shift")
        self.ok("retry", "t-1", "--actor", "night-shift")
        inbox = json.loads(self.ok("inbox", "--actor", "hub", "--json").stdout)
        self.assertEqual([row["status"] for row in inbox], ["open"])


class TestStale(EstateCase):
    """`stale` is a SIBLING of `orphans`, not a widening of it: orphans is the
    dead-worker/expired-lease case, stale is "non-terminal and nobody is
    looking." Mechanism only — it reports, it never decides or mutates."""

    def backdate(self, key, days):
        ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        with self.conn() as c:
            c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, key))

    def stale_ids(self, *args):
        return [r["id"] for r in json.loads(self.ok("stale", "--json", *args).stdout)]

    def test_default_threshold_is_three_days(self):
        self.add("old"); self.add("recent")
        self.backdate("t-1", 5); self.backdate("t-2", 1)
        self.assertEqual(self.stale_ids(), ["t-1"])

    def test_days_flag_overrides_the_default(self):
        self.add("two-days-idle"); self.backdate("t-1", 2)
        self.assertEqual(self.stale_ids(), [])
        self.assertEqual(self.stale_ids("--days", "1"), ["t-1"])

    def test_terminal_tasks_are_never_stale(self):
        self.add("done", True); self.ok("claim", "1", "--actor", "a")
        self.ok("done", "1", "--actor", "a")
        self.add("dropped"); self.ok("drop", "2")
        self.backdate("t-1", 90); self.backdate("t-2", 90)
        self.assertEqual(self.stale_ids(), [])

    def test_claim_state_does_not_matter(self):
        # Deliberately reported regardless of claim state (unlike orphans, whose
        # whole question IS the claim). A live lease on an untouched task still
        # means nobody has looked at it in a week.
        self.add("claimed", True); self.ok("claim", "1", "--actor", "a", "--ttl", "30d")
        self.add("parked", True); self.ok("claim", "2", "--actor", "a")
        self.ok("block", "2", "waiting on an external dep")
        self.add("unclaimed")
        for key in ("t-1", "t-2", "t-3"):
            self.backdate(key, 7)
        self.assertEqual(self.stale_ids(), ["t-1", "t-2", "t-3"])
        # ...and none of them is an orphan: no lease has expired.
        self.assertEqual(self.ok("orphans").stdout.strip(), "")

    def test_orphans_and_stale_answer_different_questions(self):
        self.add("dead worker", True); self.ok("claim", "1", "--actor", "a")
        with self.conn() as c:
            c.execute("UPDATE tasks SET claim_expires_at='2000-01-01T00:00:00+00:00' WHERE id='t-1'")
        self.assertIn("t-1", self.ok("orphans").stdout)   # lease expired -> orphan
        self.assertEqual(self.stale_ids(), [])            # but touched seconds ago
        self.backdate("t-1", 4)
        self.assertEqual(self.stale_ids(), ["t-1"])       # now both

    def test_kind_filter(self):
        self.ok("task", "add", "a followup", "--kind", "followup")
        self.ok("task", "add", "a review", "--kind", "review")
        self.backdate("t-1", 9); self.backdate("t-2", 9)
        self.assertEqual(self.stale_ids("--kind", "followup"), ["t-1"])

    def test_json_carries_idle_days_and_sorts_oldest_first(self):
        self.add("newer"); self.add("older")
        self.backdate("t-1", 4); self.backdate("t-2", 30)
        rows = json.loads(self.ok("stale", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["t-2", "t-1"])
        self.assertAlmostEqual(rows[0]["idle_days"], 30.0, delta=0.1)
        for field in ("id", "kind", "status", "title", "updated_at", "claimed_by"):
            self.assertIn(field, rows[0])

    def test_text_output_reports_idle_age(self):
        self.add("waiting"); self.backdate("t-1", 6)
        out = self.ok("stale").stdout
        self.assertIn("t-1", out); self.assertIn("idle 6.0d", out)

    def test_unreadable_timestamp_reports_as_stale_with_unknown_age(self):
        self.add("corrupt")
        with self.conn() as c:
            c.execute("UPDATE tasks SET updated_at='not a timestamp' WHERE id='t-1'")
        rows = json.loads(self.ok("stale", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["t-1"])
        self.assertIsNone(rows[0]["idle_days"])

    def test_negative_days_is_rejected(self):
        self.assertEqual(self.cli("stale", "--days", "-1").returncode, 2)

    def test_stale_never_mutates(self):
        self.add("untouched"); self.backdate("t-1", 10)
        before = self.task()
        self.ok("stale"); self.ok("stale", "--json")
        self.assertEqual(self.task(), before)
        self.assertEqual(len(self.events()), 1)   # only the creation event


class TestAwaiting(EstateCase):
    """t-476 — everything awaiting the owner, grouped by the question each group
    answers, with the decision IN the row.

    The gate on this feature is not that the new view is pretty. It is that
    `proposal list` and `stale` did not move: both are read by scripts and by
    the night shift every night, and the whole reason this is a separate verb
    is so a verbosity flag can never leak into them.
    """

    def backdate(self, key, days):
        ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        with self.conn() as c:
            c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, key))

    def propose(self, title, **fields):
        args = ["proposal", "add", title, "--actor", "mechanic"]
        for flag, value in fields.items():
            args += [f"--{flag.replace('_', '-')}", value]
        return self.ok(*args).stdout.strip()

    def groups(self, *args):
        return json.loads(self.ok("awaiting", "--json", *args).stdout)

    def ids(self, group, *args):
        return [i["id"] for i in self.groups(*args)[group]["items"]]

    # ── the acceptance check ────────────────────────────────────────────────

    def test_proposal_list_and_stale_are_untouched_by_the_new_view(self):
        """c1. Byte-for-byte, before and after the new command exists and
        after it has been run. Nothing about `awaiting` is allowed to be
        visible from either of the two commands other readers depend on."""
        self.propose("a condition", condition="the disk fills",
                     desired_outcome="it stops filling",
                     completion_check="free space stays above 10%")
        self.ok("task", "add", "an old one", "--kind", "followup")
        self.backdate("t-2", 9)
        before = (self.ok("proposal", "list").stdout, self.ok("stale").stdout)
        self.ok("awaiting")
        self.ok("awaiting", "--json")
        self.ok("awaiting", "--full", "--width", "200")
        after = (self.ok("proposal", "list").stdout, self.ok("stale").stdout)
        self.assertEqual(before, after)
        self.assertNotIn("Question", after[0])
        self.assertNotIn("Question", after[1])

    def test_the_new_view_writes_nothing(self):
        self.propose("a condition", condition="the disk fills")
        before = self.task()
        self.ok("awaiting"); self.ok("awaiting", "--json")
        self.assertEqual(self.task(), before)
        self.assertEqual(len(self.events()), 1)   # proposal creation only

    # ── the grouping ────────────────────────────────────────────────────────

    def test_each_group_prints_the_question_it_answers(self):
        out = self.ok("awaiting").stdout
        for question in ("What decision is waiting on you?",
                         "What is waiting on a person?",
                         "What is nobody looking at?"):
            self.assertIn(question, out)

    def test_a_row_answers_exactly_one_question(self):
        """An untouched follow-up is stale AND waiting on a person, and an
        undecided proposal is stale too. Each belongs to the first question it
        answers; printing it twice would make the counts a lie about how much
        is actually waiting."""
        self.propose("undecided", condition="something happened",
                     desired_outcome="it stops happening")
        self.ok("task", "add", "waiting on a person", "--kind", "followup")
        self.backdate("t-1", 30); self.backdate("t-2", 30)
        # Both are stale by `stale`'s own reading...
        self.assertEqual(
            [r["id"] for r in json.loads(self.ok("stale", "--json").stdout)],
            ["t-1", "t-2"])
        groups = self.groups()
        self.assertEqual([i["id"] for i in groups["decisions"]["items"]], ["t-1"])
        self.assertEqual([i["id"] for i in groups["attention"]["items"]], ["t-2"])
        self.assertEqual(groups["stale"]["items"], [])   # ...and neither twice

    def test_a_decided_proposal_is_a_record_not_a_request(self):
        self.propose("already handled", condition="something happened",
                     desired_outcome="it stops happening")
        self.ok("proposal", "stage", "t-1", "approved-backlog", "yes")
        self.assertEqual(self.ids("decisions"), [])

    def test_the_attention_and_stale_sets_come_from_their_own_commands(self):
        self.ok("task", "add", "waiting on a person", "--kind", "followup")
        self.ok("task", "add", "nobody is looking", "--kind", "generic")
        self.backdate("t-1", 9); self.backdate("t-2", 9)
        self.assertEqual(self.ids("attention"), ["t-1"])
        self.assertEqual(self.ids("stale"), ["t-2"])
        self.assertEqual(
            self.ids("attention"),
            [r["id"] for r in json.loads(self.ok("attention", "--json").stdout)])

    def test_thresholds_are_the_callers_policy_exactly_as_the_siblings(self):
        self.ok("task", "add", "two days idle", "--kind", "generic")
        self.backdate("t-1", 2)
        self.assertEqual(self.ids("stale"), [])
        self.assertEqual(self.ids("stale", "--days", "1"), ["t-1"])
        for bad in (("--days", "-1"), ("--notice", "-1"), ("--lines", "-1"),
                    ("--group", "nonsense")):
            self.assertEqual(self.cli("awaiting", *bad).returncode, 2, bad)

    # ── the fourth group ────────────────────────────────────────────────────

    def test_a_proposal_that_asks_nothing_gets_its_own_honest_group(self):
        """It is awaiting the owner, but it is not asking anything. Filing it
        under "What decision is waiting on you?" would put a row in a group
        whose question it cannot answer."""
        self.propose("filed with no review record")
        self.propose("filed properly", condition="the disk fills",
                     desired_outcome="it stops filling")
        self.assertEqual(self.ids("unstated"), ["t-1"])
        self.assertEqual(self.ids("decisions"), ["t-2"])
        out = self.ok("awaiting").stdout
        self.assertIn("Awaiting you, but the record does not say what for", out)

    def test_the_fourth_group_is_silent_when_it_is_empty(self):
        self.propose("filed properly", condition="the disk fills",
                     desired_outcome="it stops filling")
        out = self.ok("awaiting").stdout
        self.assertNotIn("the record does not say what for", out)
        self.assertNotIn("unstated", out)

    # ── the item block ──────────────────────────────────────────────────────

    def test_the_decision_is_in_the_block_with_no_second_invocation(self):
        self.propose("Disk fills during export",
                     condition="the export fills the disk every night",
                     desired_outcome="exports stay below the disk budget",
                     completion_check="free space stays above 10%")
        out = self.ok("awaiting", "--group", "decisions").stdout
        self.assertIn("Question", out)
        self.assertIn("the export fills the disk every night", out)
        self.assertIn("Recommendation", out)
        self.assertIn("exports stay below the disk budget", out)
        self.assertIn("Deferred", out)
        self.assertIn("#1 of 1", out)

    def test_a_field_says_which_stored_key_answered(self):
        self.propose("Disk fills", condition="the export fills the disk")
        self.assertIn("[condition]",
                      self.ok("awaiting", "--group", "decisions").stdout)

    def test_the_name_this_estate_already_uses_is_not_announced(self):
        """`desired_outcome` has been labelled "Recommendation" since proposals
        existed. Saying "from desired_outcome" on every row is noise; saying
        "from condition" under "Question" is not, because an observation is
        not a question."""
        self.propose("Disk fills", condition="the export fills the disk",
                     desired_outcome="exports stay below the budget")
        out = self.ok("awaiting", "--group", "decisions").stdout
        self.assertNotIn("[desired_outcome]", out)
        self.assertIn("[condition]", out)

    def test_one_stored_sentence_is_not_printed_twice_under_two_labels(self):
        """`proposal add` copies the desired outcome into `intent`, so an
        unfiltered Context line would print each recommendation to the owner
        twice."""
        self.propose("Disk fills", condition="the export fills the disk",
                     desired_outcome="exports stay below the budget")
        out = self.ok("awaiting", "--group", "decisions").stdout
        self.assertEqual(out.count("exports stay below the budget"), 1)
        self.assertNotIn("Context", out)

    def test_a_missing_field_prints_as_missing_where_it_is_owed(self):
        self.propose("Disk fills", condition="the export fills the disk")
        out = self.ok("awaiting", "--group", "decisions").stdout
        self.assertIn("no recommendation recorded", out)

    def test_a_field_nobody_owes_is_not_printed_as_missing(self):
        """Every follow-up saying "no question recorded" is noise: a
        follow-up was never asked one, and its group's question is answered by
        the fact that it is in the group."""
        self.ok("task", "add", "waiting", "--kind", "followup", "--refs",
                json.dumps({"context": "the evidence is in the worktree"}))
        out = self.ok("awaiting", "--group", "attention").stdout
        self.assertNotIn("no question recorded", out)
        self.assertIn("the evidence is in the worktree", out)

    def test_an_attention_row_with_no_context_says_so(self):
        self.ok("task", "add", "waiting", "--kind", "followup")
        self.assertIn("no context recorded",
                      self.ok("awaiting", "--group", "attention").stdout)

    def test_long_fields_are_clamped_and_full_prints_all_of_them(self):
        self.propose("Disk fills", condition=" ".join(["evidence"] * 400),
                     desired_outcome="stop it")
        clamped = self.ok("awaiting", "--group", "decisions").stdout
        self.assertIn("…", clamped)
        self.assertLess(clamped.count("evidence"), 400)
        self.assertEqual(
            self.ok("awaiting", "--group", "decisions", "--full")
                .stdout.count("evidence"), 400)
        self.assertNotIn(
            "…", self.ok("awaiting", "--group", "decisions", "--lines", "0").stdout)

    def test_json_carries_the_question_the_group_answers(self):
        self.propose("Disk fills", condition="the disk fills",
                     desired_outcome="it stops")
        groups = self.groups()
        self.assertEqual(groups["decisions"]["question"],
                         "What decision is waiting on you?")
        item = groups["decisions"]["items"][0]
        self.assertEqual(item["question"], "the disk fills")
        self.assertEqual(item["question_field"], "condition")
        self.assertEqual(item["recommendation"], "it stops")
        self.assertIn("#1 of 1", item["defer"])


class TestUnpooledKinds(EstateCase):
    """t-296: the hub mirrors every self-DM message as an `inbox-message`
    task, so the shared store now holds rows as small as "thanks". The two
    questions ABOUT THE CLAIMABLE POOL skip that kind unless a caller asks for
    it by name; everything else keeps seeing it, deliberately."""

    def message(self, title="the owner asked something"):
        return self.ok("task", "add", title, "--kind", "inbox-message",
                       "--ready", "--actor", "hub").stdout.strip()

    def backdate(self, key, days):
        ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        with self.conn() as c:
            c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, key))

    def test_ready_skips_them_but_returns_them_when_asked_by_name(self):
        self.add("real work", ready=True)
        msg = self.message()
        out = self.ok("ready").stdout
        self.assertIn("t-1", out)
        self.assertNotIn(msg, out)
        self.assertIn(msg, self.ok("ready", "--kind", "inbox-message").stdout)

    def test_stale_skips_them_but_returns_them_when_asked_by_name(self):
        """The briefer reads `stale` every morning as the backlog-drift
        digest. An open conversation is not backlog drift."""
        self.add("real work"); msg = self.message()
        self.backdate("t-1", 9); self.backdate(msg, 9)
        ids = [r["id"] for r in json.loads(self.ok("stale", "--json").stdout)]
        self.assertEqual(ids, ["t-1"])
        named = [r["id"] for r in json.loads(
            self.ok("stale", "--json", "--kind", "inbox-message").stdout)]
        self.assertEqual(named, [msg])

    def test_the_other_reads_are_untouched(self):
        """`task list` is the explicit query, and a message parked at
        `needs-owner` IS waiting on a person (docs/attention-contract.md)."""
        msg = self.message()
        self.assertIn(msg, self.ok("task", "list").stdout)
        self.ok("claim", msg, "--actor", "hub")
        self.ok("needs-owner", msg, "their call")
        self.assertIn(msg, self.ok("attention").stdout)

    def test_a_dead_worker_still_orphans_a_claimed_message(self):
        """The one place the claim machinery earns its keep here: a message
        handed to a dispatched worker that died goes back to `ready`."""
        msg = self.message()
        self.ok("claim", msg, "--actor", "hub", "--ttl", "1s")
        with self.conn() as c:
            c.execute("UPDATE tasks SET claim_expires_at=? WHERE id=?",
                      ((dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(hours=1)).isoformat(), msg))
        self.assertIn(msg, self.ok("orphans").stdout)
        self.ok("orphans", "--sweep")
        self.assertEqual(self.task(msg)["status"], "ready")


class TestDue(EstateCase):
    """`due` is a SIBLING of `stale`, not a variant of it (P-31): stale reads
    updated_at and asks "is anyone looking at this?", due reads a date somebody
    deliberately set and asks "is the clock running out?". Mechanism only — it
    reports, it never decides, mutates, or guesses a deadline out of prose."""

    def due_ids(self, *args):
        return [r["id"] for r in json.loads(self.ok("due", "--json", *args).stdout)]

    def day(self, offset):
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=offset)).date().isoformat()

    def add_due(self, title, offset, kind="generic"):
        return self.ok("task", "add", title, "--kind", kind, "--due", self.day(offset))

    def test_due_at_column_exists_and_defaults_to_null(self):
        self.add("no deadline")
        self.assertIn("due_at", self.task())
        self.assertIsNone(self.task()["due_at"])

    def test_migration_adds_the_column_to_a_store_that_predates_it(self):
        # The live store is exactly this shape: rows already in it, no due_at.
        # Rebuild that here rather than trusting the CREATE TABLE path.
        self.add("filed before deadlines existed")
        with self.conn() as c:
            columns = [r[1] for r in c.execute("PRAGMA table_info(tasks)")
                       if r[1] != "due_at"]
            c.execute("ALTER TABLE tasks RENAME TO tasks_old")
            c.execute(f"CREATE TABLE tasks ({', '.join(columns)})")
            c.execute(f"INSERT INTO tasks SELECT {', '.join(columns)} FROM tasks_old")
            c.execute("DROP TABLE tasks_old")
            self.assertNotIn("due_at", [r[1] for r in c.execute("PRAGMA table_info(tasks)")])
        self.ok("due")                       # any command migrates on entry
        row = self.task()
        self.assertIsNone(row["due_at"])     # existing row survives, undated
        self.assertEqual(row["title"], "filed before deadlines existed")
        with self.conn() as c:
            indexes = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_tasks_due", indexes)

    def test_migration_is_idempotent(self):
        self.add_due("dated", 1)
        before = self.task()
        for _ in range(3):
            self.ok("due")
        self.assertEqual(self.task(), before)

    def test_bare_date_lands_at_end_of_day_so_today_is_not_overdue(self):
        # "Due Friday" means Friday is still on time. An item due today must
        # not read as overdue at breakfast.
        self.add_due("due today", 0)
        rows = json.loads(self.ok("due", "--json").stdout)
        self.assertTrue(rows[0]["due_at"].startswith(self.day(0)))
        self.assertIn("23:59:59", rows[0]["due_at"])
        self.assertFalse(rows[0]["overdue"])

    def test_full_iso_timestamps_are_accepted_and_normalized_to_utc(self):
        self.ok("task", "add", "precise", "--kind", "generic",
                "--due", "2026-08-01T12:00:00-04:00")
        self.assertEqual(self.task()["due_at"], "2026-08-01T16:00:00+00:00")

    def test_unparseable_due_is_rejected_not_guessed(self):
        result = self.cli("task", "add", "vague", "--kind", "generic",
                          "--due", "this week")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--due", result.stderr)

    def test_window_filters_and_overdue_always_surfaces(self):
        self.add_due("overdue", -9)
        self.add_due("soon", 1)
        self.add_due("far off", 30)
        self.assertEqual(self.due_ids(), ["t-1", "t-2"])
        self.assertEqual(self.due_ids("--within", "60"), ["t-1", "t-2", "t-3"])
        # Even a zero-day window keeps the already-late item visible.
        self.assertEqual(self.due_ids("--within", "0"), ["t-1"])

    def test_undated_tasks_are_absent_not_late(self):
        self.add("no deadline at all")
        self.add_due("dated", 1)
        self.assertEqual(self.due_ids("--within", "365"), ["t-2"])

    def test_terminal_tasks_are_never_due(self):
        self.ok("task", "add", "finished", "--kind", "generic", "--ready",
                "--due", self.day(-5))
        self.ok("claim", "1", "--actor", "a"); self.ok("done", "1", "--actor", "a")
        self.ok("task", "add", "abandoned", "--kind", "generic", "--due", self.day(-5))
        self.ok("drop", "2")
        self.assertEqual(self.due_ids("--within", "365"), [])

    def stamp(self, days):
        """An exact instant, not a bare date — the end-of-day rounding a bare
        date gets is deliberate but makes day arithmetic imprecise in a test."""
        return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()

    def test_sorts_most_overdue_first(self):
        self.ok("task", "add", "mildly late", "--kind", "generic", "--due", self.stamp(-1))
        self.ok("task", "add", "very late", "--kind", "generic", "--due", self.stamp(-20))
        self.ok("task", "add", "upcoming", "--kind", "generic", "--due", self.stamp(2))
        rows = json.loads(self.ok("due", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["t-2", "t-1", "t-3"])
        self.assertAlmostEqual(rows[0]["days_left"], -20.0, delta=0.1)
        self.assertTrue(rows[0]["overdue"])
        self.assertFalse(rows[2]["overdue"])
        for field in ("id", "kind", "status", "title", "due_at", "claimed_by"):
            self.assertIn(field, rows[0])

    def test_kind_filter(self):
        self.add_due("a followup", 1, kind="followup")
        self.add_due("a review", 1, kind="review")
        self.assertEqual(self.due_ids("--kind", "followup"), ["t-1"])

    def test_text_output_marks_overdue(self):
        self.ok("task", "add", "late", "--kind", "generic", "--due", self.stamp(-3))
        self.ok("task", "add", "coming up", "--kind", "generic", "--due", self.stamp(2))
        out = self.ok("due").stdout
        self.assertIn("OVERDUE 3.0d", out)
        self.assertIn("due in 2.0d", out)

    def test_unreadable_due_surfaces_as_unknown_rather_than_dropping_out(self):
        self.add_due("corrupt", 1)
        with self.conn() as c:
            c.execute("UPDATE tasks SET due_at='not a timestamp' WHERE id='t-1'")
        rows = json.loads(self.ok("due", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["t-1"])
        self.assertIsNone(rows[0]["days_left"])
        self.assertIsNone(rows[0]["overdue"])

    def test_negative_within_is_rejected(self):
        self.assertEqual(self.cli("due", "--within", "-1").returncode, 2)

    def test_due_never_mutates(self):
        self.add_due("untouched", 1)
        before = self.task()
        self.ok("due"); self.ok("due", "--json")
        self.assertEqual(self.task(), before)
        self.assertEqual(len(self.events()), 1)   # only the creation event

    def test_set_due_retrofits_a_deadline_and_logs_it(self):
        self.add("filed with the date buried in its title")
        self.ok("task", "set-due", "1", "2026-08-01", "--actor", "night-shift")
        self.assertEqual(self.task()["due_at"], "2026-08-01T23:59:59+00:00")
        last = self.events()[-1]
        self.assertIn("due unset -> 2026-08-01T23:59:59+00:00", last["summary"])
        self.assertEqual(last["actor"], "night-shift")

    def test_set_due_replaces_an_existing_date(self):
        self.add_due("moved", 1)
        self.ok("task", "set-due", "1", "2026-09-09")
        self.assertEqual(self.task()["due_at"], "2026-09-09T23:59:59+00:00")

    def test_set_due_clear_removes_the_deadline(self):
        self.add_due("mistake", 1)
        self.ok("task", "set-due", "1", "--clear")
        self.assertIsNone(self.task()["due_at"])
        self.assertEqual(self.due_ids("--within", "365"), [])

    def test_set_due_rejects_ambiguous_and_missing_input(self):
        self.add("a task")
        self.assertEqual(self.cli("task", "set-due", "1").returncode, 2)
        self.assertEqual(self.cli("task", "set-due", "1", "2026-08-01",
                                  "--clear").returncode, 2)
        self.assertEqual(self.cli("task", "set-due", "1", "sometime").returncode, 2)
        self.assertEqual(self.cli("task", "set-due", "404", "2026-08-01").returncode, 2)
        self.assertIsNone(self.task()["due_at"])

    def test_due_and_stale_answer_different_questions(self):
        # Actively worked, seconds old, and about to miss its date.
        self.ok("task", "add", "busy but late", "--kind", "generic", "--ready",
                "--due", self.day(-1))
        self.ok("claim", "1", "--actor", "a")
        self.assertEqual(self.due_ids(), ["t-1"])
        self.assertEqual([r["id"] for r in
                          json.loads(self.ok("stale", "--json").stdout)], [])


# The nightly export+backup systemd units are deliberately NOT in this repo
# (docs/extraction-allowlist.md: "Installed systemd units" — the always-on
# supervisor is a template the operator generates on their own machine), so
# the unit-file assertions that guard them live in the installation that
# actually installs them. What is testable here is the behaviour those units
# depend on, and TestArchiveAndDoctor below covers it: a fresh store fails
# `doctor` on staleness and `export` + `backup` are what make it pass.


class TestDependencies(EstateCase):
    def test_blocks_ready_until_blocker_done(self):
        self.add("blocker", True); self.add("blocked", True)
        self.ok("dep", "add", "1", "2", "--kind", "blocks")
        out = self.ok("ready").stdout; self.assertIn("t-1", out); self.assertNotIn("t-2", out)
        self.ok("claim", "1", "--actor", "a"); self.ok("done", "1", "--actor", "a")
        self.assertIn("t-2", self.ok("ready").stdout)

    def test_discovered_from_lists_both_directions(self):
        self.add("one"); self.add("two")
        self.ok("dep", "add", "1", "2", "--kind", "discovered-from")
        self.assertIn("discovered-from", self.ok("dep", "list", "2").stdout)


class TestArchiveAndDoctor(EstateCase):
    def test_export_backup_and_doctor(self):
        self.add(); self.ok("export"); self.ok("backup")
        self.assertEqual(len(os.listdir(os.path.join(self.state, "estate", "export"))), 6)
        self.assertTrue(any(n.endswith(".db") for n in os.listdir(os.path.join(self.state, "estate", "backup"))))
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_doctor_fails_for_missing_and_stale_archives(self):
        self.ok("init"); result = self.cli("doctor")
        self.assertNotEqual(result.returncode, 0); self.assertIn("FAIL nightly JSONL export fresh", result.stdout)
        self.ok("export"); self.ok("backup")
        old = 1
        for root, _, files in os.walk(os.path.join(self.state, "estate")):
            for name in files: os.utime(os.path.join(root, name), (old, old))
        self.assertNotEqual(self.cli("doctor").returncode, 0)


class TestMemorySchema(EstateCase):
    def test_ids_increment_and_creation_writes_an_event(self):
        self.assertEqual(self.remember("one"), "m-1")
        self.assertEqual(self.remember("two"), "m-2")
        row = self.memory()
        self.assertEqual((row["scope"], row["kind"], row["status"]), ("mechanic", "lesson", "candidate"))
        self.assertEqual([e["kind"] for e in self.memory_events()], ["activity"])

    def test_bodies_are_immutable_and_rows_are_never_deleted(self):
        self.remember()
        with self.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError): c.execute("UPDATE memories SET body='x'")
            with self.assertRaises(sqlite3.IntegrityError): c.execute("DELETE FROM memories")

    def test_memories_never_enter_the_task_lifecycle(self):
        self.shared(); self.assertEqual(self.ok("ready").stdout, "")
        self.assertEqual(self.cli("claim", "m-1", "--actor", "a").returncode, 2)

    def test_migration_adds_memory_id_to_a_phase_zero_events_table(self):
        # A store created before §5 has an events table with no memory_id column;
        # events are append-only, so the column is added in place.
        conn = sqlite3.connect(self.db)
        conn.executescript("""CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
            task_id TEXT, project_id TEXT, kind TEXT NOT NULL, summary TEXT NOT NULL,
            detail TEXT, refs TEXT);
            INSERT INTO events (ts, actor, kind, summary) VALUES ('t', 'old', 'note', 'kept');""")
        conn.commit(); conn.close()
        self.remember()
        with self.conn() as c:
            self.assertIn("memory_id", {r[1] for r in c.execute("PRAGMA table_info(events)")})
            self.assertEqual(c.execute("SELECT summary FROM events WHERE actor='old'").fetchone()[0], "kept")

    def memory_columns(self):
        with self.conn() as c:
            return {r[1] for r in c.execute("PRAGMA table_info(memories)")}

    def rename_column_back(self):
        """Rebuild the pre-2026-08-16 shape: the column under its old name."""
        with self.conn() as c:
            c.execute("ALTER TABLE memories RENAME COLUMN last_recalled_at TO last_seen_at")
        self.assertIn("last_seen_at", self.memory_columns())

    def test_a_fresh_store_gets_the_recalled_name_without_a_migration(self):
        self.remember()
        columns = self.memory_columns()
        self.assertIn("last_recalled_at", columns)
        self.assertNotIn("last_seen_at", columns)

    def test_migration_renames_the_column_and_keeps_every_value(self):
        # The live store is exactly this shape: rows already stamped, under the
        # old name. Rebuild it here rather than trusting the CREATE TABLE path.
        self.remember("stamped"); self.remember("never recalled")
        self.ok("memory", "seen", "m-1")
        stamp = self.memory("m-1")["last_recalled_at"]
        self.assertIsNotNone(stamp)
        self.rename_column_back()
        self.ok("memory", "list")            # any command migrates on entry
        self.assertNotIn("last_seen_at", self.memory_columns())
        # The values are the same values: nothing re-stamped, nothing dropped.
        self.assertEqual(self.memory("m-1")["last_recalled_at"], stamp)
        self.assertEqual(self.memory("m-1")["body"], "stamped")
        self.assertIsNone(self.memory("m-2")["last_recalled_at"])

    def test_the_rename_migration_is_idempotent(self):
        self.remember(); self.ok("memory", "seen", "m-1")
        self.rename_column_back()
        for _ in range(3):
            self.ok("memory", "list")
        self.assertNotIn("last_seen_at", self.memory_columns())
        before = self.memory("m-1")
        self.ok("memory", "list")
        self.assertEqual(self.memory("m-1"), before)


class TestMemoryProvenance(EstateCase):
    def test_shared_active_memory_requires_source_refs(self):
        result = self.cli("memory", "add", "unsourced", "--scope", "shared", "--status", "active")
        self.assertEqual(result.returncode, 2); self.assertIn("--source-refs", result.stderr)
        self.assertEqual(self.ok("memory", "list").stdout, "")

    def test_promote_to_shared_requires_provenance_and_keeps_scope_otherwise(self):
        self.remember("local lesson")
        self.assertEqual(self.cli("memory", "promote", "1", "--scope", "shared").returncode, 2)
        self.assertEqual(self.memory()["status"], "candidate")
        self.ok("memory", "promote", "1", "--scope", "shared", "--source-refs", '{"ledger":[7]}')
        row = self.memory()
        self.assertEqual((row["status"], row["scope"]), ("active", "shared"))
        self.assertEqual(json.loads(row["source_refs"]), {"ledger": [7]})
        self.assertEqual([e["kind"] for e in self.memory_events()], ["activity", "transition"])

    def test_candidate_scoped_to_a_loop_needs_no_provenance(self):
        self.remember("cheap and additive"); self.ok("memory", "promote", "1")
        self.assertEqual(self.memory()["status"], "active")


class TestMemoryRetirement(EstateCase):
    def test_supersede_with_an_inline_replacement_links_both_rows(self):
        self.shared("old text")
        self.ok("memory", "supersede", "1", "--body", "new text",
                "--source-refs", '{"ledger":[9]}', "--note", "owner correction")
        old, new = self.memory("m-1"), self.memory("m-2")
        self.assertEqual((old["status"], old["superseded_by"]), ("superseded", "m-2"))
        self.assertIsNotNone(old["retired_at"])
        self.assertEqual((new["status"], new["scope"], new["body"]), ("active", "shared", "new text"))
        self.assertEqual(self.memory_events("m-1")[-1]["detail"], "owner correction")

    def test_supersede_by_id_activates_a_candidate_replacement(self):
        self.shared("old"); self.remember("replacement", scope="shared", refs='{"ledger":[3]}')
        self.ok("memory", "supersede", "m-1", "m-2")
        self.assertEqual(self.memory("m-1")["status"], "superseded")
        self.assertEqual(self.memory("m-2")["status"], "active")

    def test_supersede_rejects_a_retired_replacement_and_itself(self):
        self.shared("live"); self.remember("dead", scope="shared"); self.ok("memory", "archive", "m-2")
        self.assertEqual(self.cli("memory", "supersede", "m-1", "m-2").returncode, 2)
        self.assertEqual(self.cli("memory", "supersede", "m-1", "m-1").returncode, 2)
        self.assertEqual(self.memory("m-1")["status"], "active")

    def test_merge_retires_losers_and_refuses_cross_scope_without_the_flag(self):
        self.shared("survivor"); self.remember("dup", scope="shared", refs='{"ledger":[2]}', active=True)
        self.remember("other-scope dup", scope="mechanic")
        result = self.cli("memory", "merge", "m-2", "m-3", "--into", "m-1")
        self.assertEqual(result.returncode, 2); self.assertIn("cross-scope", result.stderr)
        self.assertEqual(self.memory("m-2")["status"], "active")  # whole merge rolled back
        self.ok("memory", "merge", "m-2", "m-3", "--into", "m-1", "--allow-cross-scope")
        self.assertEqual(self.memory("m-2")["superseded_by"], "m-1")
        self.assertEqual(self.memory("m-3")["superseded_by"], "m-1")
        self.assertEqual(self.memory("m-1")["status"], "active")

    def test_protected_memories_need_an_explicit_override_to_retire(self):
        self.remember("safety-adjacent", scope="shared", refs='{"ledger":[1]}', active=True, protected=True)
        for verb in (["memory", "archive", "m-1"], ["memory", "supersede", "m-1", "--body", "x"]):
            result = self.cli(*verb)
            self.assertEqual(result.returncode, 2); self.assertIn("protected", result.stderr)
            self.assertEqual(self.memory()["status"], "active")
        self.ok("memory", "archive", "m-1", "--force-protected")
        self.assertEqual(self.memory()["status"], "archived")

    def test_retired_rows_leave_normal_reads_but_stay_queryable(self):
        self.shared("kept"); self.remember("dropped", scope="shared", refs='{"ledger":[4]}', active=True)
        self.ok("memory", "archive", "m-2")
        listed = self.ok("memory", "list").stdout
        self.assertIn("m-1", listed); self.assertNotIn("m-2", listed)
        self.assertIn("m-2", self.ok("memory", "list", "--all").stdout)
        self.assertIn("m-2", self.ok("memory", "query", "dropped").stdout)

    def test_reinstating_a_retired_memory_counts_the_reactivation(self):
        self.remember("rare but real", scope="mechanic"); self.ok("memory", "archive", "m-1")
        self.ok("memory", "reinstate", "m-1", "--note", "relearned")
        old, new = self.memory("m-1"), self.memory("m-2")
        self.assertEqual(old["superseded_by"], "m-2")
        self.assertEqual((new["status"], new["body"], new["reactivations"]), ("active", "rare but real", 1))
        self.assertEqual(self.cli("memory", "reinstate", "m-1").returncode, 2)  # closed link
        self.assertEqual(self.cli("memory", "reinstate", "m-2").returncode, 2)  # not retired

    def test_log_walks_the_whole_chain_including_merge_losers(self):
        self.shared("survivor"); self.remember("dup", scope="shared", refs='{"ledger":[2]}', active=True)
        self.ok("memory", "merge", "m-2", "--into", "m-1")
        self.ok("memory", "supersede", "m-1", "--body", "successor", "--source-refs", '{"ledger":[5]}')
        out = self.ok("memory", "log", "m-2").stdout
        for key in ("m-1", "m-2", "m-3"): self.assertIn(key, out)


class TestMemoryQueryAndMetadata(EstateCase):
    def test_query_matches_substrings_and_honours_filters(self):
        self.remember("restarts kill the loop", scope="hub")
        self.remember("systemd inherits nothing", scope="mechanic")
        self.assertIn("m-1", self.ok("memory", "query", "kill the loop").stdout)
        self.assertNotIn("m-2", self.ok("memory", "query", "kill the loop").stdout)
        self.assertEqual(self.ok("memory", "query", "loop", "--scope", "mechanic").stdout, "")

    def test_query_treats_wildcards_as_literal_text(self):
        self.remember("100% coverage claim"); self.remember("unrelated")
        out = self.ok("memory", "query", "100%").stdout
        self.assertIn("m-1", out); self.assertNotIn("m-2", out)

    def test_seen_stamps_metadata_without_flooding_the_audit_stream(self):
        self.remember("one"); self.remember("two")
        self.ok("memory", "seen", "m-1", "m-2")
        self.assertIsNotNone(self.memory("m-1")["last_recalled_at"])
        self.assertEqual([e["kind"] for e in self.memory_events("m-1")], ["activity"])
        self.assertEqual(self.cli("memory", "seen", "m-9").returncode, 2)

    def test_estate_log_routes_memory_ids(self):
        self.shared("shared thing")
        self.assertIn("memory created", self.ok("log", "m-1").stdout)
        self.assertEqual(self.cli("log", "m-9").returncode, 2)

    def test_free_form_events_can_attach_to_a_memory(self):
        self.shared("shared thing")
        self.ok("event", "--actor", "curator", "--kind", "note",
                "--summary", "matched a candidate", "--memory", "m-1")
        self.assertEqual([e["summary"] for e in self.memory_events()][-1], "matched a candidate")


class TestMemoryRecall(EstateCase):
    """P-05 / S35 + S36 + S38 + S53 — the one scope-safe read path.

    `memory list --scope X` could already do most of this; what it could not do
    is be the thing every consumer calls. These tests pin the contract that
    makes one call safe for four callers: the union is exactly local + shared,
    a candidate is not in it, every row says which set it came from, and the
    refs envelope says the same thing to whatever record the intake produced.
    """

    def recall(self, scope, *extra):
        return self.ok("memory", "recall", "--scope", scope, *extra).stdout

    def recalled(self, scope, *extra):
        return json.loads(self.recall(scope, "--json", *extra))

    def test_local_and_shared_are_returned_and_nothing_else_is(self):
        self.shared("every loop obeys this")
        self.remember("only the mechanic", scope="mechanic", active=True)
        self.remember("only the hub", scope="hub", active=True)
        out = self.recalled("mechanic")
        self.assertEqual([m["id"] for m in out["memories"]], ["m-2", "m-1"])
        self.assertEqual([m["scope"] for m in out["memories"]],
                         ["mechanic", "shared"])
        # S35's other half, stated as its own assertion: the hub's fact is
        # ABSENT from the mechanic's recall, not merely sorted last.
        self.assertNotIn("m-3", self.recall("mechanic"))

    def test_a_candidate_is_never_recalled_even_in_its_own_scope(self):
        """S38. Promotion is what makes a fact recallable; recalling an
        unjudged candidate would make promotion decorative."""
        self.remember("not judged yet", scope="mechanic")
        self.assertEqual(self.recalled("mechanic")["count"], 0)
        self.ok("memory", "promote", "m-1")
        self.assertEqual([m["id"] for m in self.recalled("mechanic")["memories"]],
                         ["m-1"])

    def test_a_retired_fact_is_not_recalled(self):
        self.remember("was true once", scope="mechanic", active=True)
        self.remember("still true", scope="mechanic", active=True)
        self.ok("memory", "archive", "m-1")
        self.assertEqual([m["id"] for m in self.recalled("mechanic")["memories"]],
                         ["m-2"])

    def test_every_row_is_labelled_with_the_scope_it_is_stored_at(self):
        """S36: a global fact is visibly global at the moment it is used."""
        self.shared("estate-wide")
        self.remember("ours only", scope="mechanic", active=True)
        text = self.recall("mechanic")
        self.assertIn("m-1  [shared]  lesson", text)
        self.assertIn("m-2  [mechanic]  lesson", text)
        for row in self.recalled("mechanic")["memories"]:
            self.assertEqual(row["origin"],
                             "shared" if row["scope"] == "shared" else "local")

    def test_the_body_is_printed_in_full_not_truncated_like_a_list(self):
        long_body = "a lesson that is far too long for the list renderer " * 3
        self.remember(long_body, scope="mechanic", active=True)
        self.assertIn("...", self.ok("memory", "list").stdout)
        self.assertNotIn("...", self.recall("mechanic"))
        self.assertEqual(self.recalled("mechanic")["memories"][0]["body"],
                         long_body.strip())

    def test_the_two_names_for_one_loop_recall_the_same_facts(self):
        """docs/actor-taxonomy.md's split identity, in the scope column: a fact
        filed under the audit's name must reach the loop that learned it."""
        self.remember("filed under the audit's name", scope="night-shift",
                      active=True)
        self.remember("filed under the technical name", scope="pr-reviewer",
                      active=True)
        for asked in ("night-shift", "pr-reviewer"):
            out = self.recalled(asked)
            self.assertEqual([m["id"] for m in out["memories"]], ["m-1", "m-2"])
            self.assertEqual(out["scope"], "pr-reviewer")
            self.assertEqual(out["scopes"], ["pr-reviewer", "night-shift",
                                             "shared"])
        # Folding is two names for ONE loop, never two loops.
        self.assertEqual(self.recalled("mechanic")["count"], 0)

    def test_an_unknown_scope_is_kept_verbatim_and_still_gets_shared(self):
        self.shared("estate-wide")
        self.remember("the extractor's own", scope="extractor", active=True)
        out = self.recalled("extractor")
        self.assertEqual(out["scope"], "extractor")
        self.assertEqual([m["id"] for m in out["memories"]], ["m-2", "m-1"])

    def test_a_blank_scope_is_refused_rather_than_meaning_shared_only(self):
        self.shared("estate-wide")
        result = self.cli("memory", "recall", "--scope", "   ")
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-empty", result.stderr)
        self.assertEqual(self.cli("memory", "recall").returncode, 2)

    def test_the_kind_filter_narrows_without_widening_the_scopes(self):
        self.remember("a shared convention", scope="shared",
                      refs='{"ledger":[1]}', active=True)
        self.ok("memory", "add", "a mechanic fact", "--scope", "mechanic",
                "--kind", "fact", "--status", "active")
        out = self.recalled("mechanic", "--kind", "fact")
        self.assertEqual([m["id"] for m in out["memories"]], ["m-2"])

    def test_the_refs_envelope_names_every_fact_with_its_scope(self):
        self.shared("estate-wide")
        self.remember("ours only", scope="mechanic", active=True)
        refs = json.loads(self.ok("memory", "recall", "--scope", "mechanic",
                                  "--refs").stdout)
        self.assertEqual(refs, {"memory_recall": {
            "scope": "mechanic",
            "memories": ["m-2@mechanic", "m-1@shared"]}})
        self.assertEqual(self.recalled("mechanic")["refs"], refs)
        self.assertIn(json.dumps(refs, sort_keys=True),
                      self.recall("mechanic"))

    def test_an_empty_recall_is_an_answer_not_a_blank(self):
        """P-08's distinction, applied to facts: "nothing for me" and "recall
        never ran" must not read the same."""
        refs = json.loads(self.ok("memory", "recall", "--scope", "mechanic",
                                  "--refs").stdout)
        self.assertEqual(refs, {"memory_recall": {"scope": "mechanic",
                                                  "memories": []}})
        self.assertIn("no active memories at scope mechanic/shared",
                      self.recall("mechanic"))

    def test_recall_stamps_last_recalled_at_and_writes_no_event(self):
        self.shared("estate-wide")
        self.remember("ours only", scope="mechanic", active=True)
        before = [e["seq"] for e in self.memory_events("m-1")]
        self.recall("mechanic")
        self.assertIsNotNone(self.memory("m-1")["last_recalled_at"])
        self.assertIsNotNone(self.memory("m-2")["last_recalled_at"])
        self.assertEqual([e["seq"] for e in self.memory_events("m-1")], before)

    def test_no_touch_inspects_without_claiming_a_pass_used_the_fact(self):
        self.shared("estate-wide")
        self.recall("mechanic", "--no-touch")
        self.assertIsNone(self.memory("m-1")["last_recalled_at"])

    def test_a_fact_the_caller_cannot_see_is_not_stamped_seen(self):
        self.remember("the hub's own", scope="hub", active=True)
        self.recall("mechanic")
        self.assertIsNone(self.memory("m-1")["last_recalled_at"])


class TestLessonsRenderer(EstateCase):
    def bullets(self, text):
        return [line for line in text.splitlines() if line.startswith("- ")]

    def test_renders_active_shared_rows_in_creation_order(self):
        self.shared("**2026-07-01 - first.** One."); self.shared("**2026-07-02 - second.** Two.")
        self.remember("private to a loop", scope="mechanic")
        out = self.ok("memory", "render").stdout
        self.assertTrue(out.startswith("# Lessons\n"))
        self.assertIn("Corrections and errors distilled from state/ledger.jsonl.", out)
        self.assertEqual(self.bullets(out),
                         ["- **2026-07-01 - first.** One.", "- **2026-07-02 - second.** Two."])
        self.assertNotIn("private to a loop", out)

    def test_retired_rows_drop_out_of_the_render(self):
        self.shared("keeper"); self.shared("goner"); self.ok("memory", "archive", "m-2")
        out = self.ok("memory", "render").stdout
        self.assertIn("keeper", out); self.assertNotIn("goner", out)

    def test_bodies_wrap_like_the_existing_file_and_survive_a_round_trip(self):
        # Invented, deliberately: a real lesson is one estate's history, and the
        # only property under test here is that a long body survives wrapping.
        body = ("**2026-07-18 - a wrapped line is not a changed line.** Rendering the "
                "same store twice has to produce the same bytes, so the wrapper re-flows "
                "on whitespace and never re-orders, re-indents or re-punctuates a body "
                "it did not otherwise touch.")
        self.shared(body)
        out = self.ok("memory", "render").stdout
        lines = [line for line in out.split("-->\n", 1)[1].splitlines() if line.strip()]
        self.assertTrue(all(len(line) <= 81 for line in lines), lines)
        self.assertTrue(lines[0].startswith("- **2026-07-18"))
        self.assertTrue(all(line.startswith("  ") for line in lines[1:]))
        flattened = " ".join(" ".join(lines).split()).lstrip("- ")
        self.assertEqual(flattened, " ".join(body.split()))

    def test_untouched_memories_render_byte_identically(self):
        self.shared("stable"); first = self.ok("memory", "render").stdout
        self.shared("appended")
        second = self.ok("memory", "render").stdout
        self.assertTrue(second.startswith(first.rstrip("\n")))

    def test_multi_paragraph_bodies_keep_their_break(self):
        self.shared("First paragraph.\n\nSecond paragraph.")
        out = self.ok("memory", "render").stdout
        self.assertIn("- First paragraph.\n\n  Second paragraph.\n", out)

    def test_write_and_check_round_trip(self):
        self.shared("rendered lesson")
        self.assertNotEqual(self.cli("memory", "render", "--check").returncode, 0)
        self.ok("memory", "render", "--write")
        with open(self.lessons) as handle: written = handle.read()
        self.assertIn("- rendered lesson", written)
        self.ok("memory", "render", "--check")
        with open(self.lessons, "a") as handle: handle.write("- hand-edited\n")
        result = self.cli("memory", "render", "--check")
        self.assertNotEqual(result.returncode, 0); self.assertIn("stale", result.stderr)

    def test_empty_shared_set_renders_the_header_alone(self):
        self.remember("loop-private only")
        out = self.ok("memory", "render").stdout
        self.assertEqual(self.bullets(out), []); self.assertIn("# Lessons", out)


class TestMemoryDoctor(EstateCase):
    def archives(self): self.ok("export"); self.ok("backup")

    def test_render_check_is_skipped_until_shared_memories_exist(self):
        self.remember("loop-private"); self.archives()
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("skip lessons.md render check", result.stdout)
        self.assertIn("ok   memory triggers present", result.stdout)

    def test_doctor_migrates_a_pre_memory_store_instead_of_reporting_it_red(self):
        self.add(); self.archives()
        with self.conn() as c:
            c.executescript("DROP TRIGGER memories_body_immutable;"
                            "DROP TRIGGER memories_no_delete; DROP TABLE memories;")
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("WAL mode on"), 1)  # no duplicated checks
        self.assertIn("ok   memory triggers present", result.stdout)

    def test_render_staleness_and_missing_provenance_are_failures(self):
        self.shared("needs rendering"); self.archives()
        result = self.cli("doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL rendered lessons.md fresh", result.stdout)
        self.ok("memory", "render", "--write")
        self.assertEqual(self.cli("doctor").returncode, 0)
        with self.conn() as c: c.execute("UPDATE memories SET source_refs=NULL WHERE id='m-1'")
        result = self.cli("doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL shared memories cite provenance", result.stdout)


class AskCase(EstateCase):
    """§6 mail. Deadlines are forced by rewriting expires_at rather than by
    sleeping, so the sweep is tested on its logic instead of on the clock."""

    FOLLOWUPS = os.path.join(os.path.dirname(SCRIPT), "followups")

    @staticmethod
    def followups_is_estate_backed():
        """True when bin/followups is the shim that reads THIS store.

        The escalation contract below is a contract between two scripts, and
        only one of them is in this extraction. A repo whose bin/followups is
        still the standalone JSON store cannot honour it, and asserting against
        it would report a missing re-sync as a bug in `estate ask`. The check is
        on the file rather than on a version string so the test starts running
        again by itself the moment the shim lands.
        """
        try:
            with open(AskCase.FOLLOWUPS) as f:
                return "ESTATE_STATE_DIR" in f.read()
        except OSError:
            return False

    def cli_as(self, who, *args):
        env = dict(os.environ, ESTATE_STATE_DIR=self.state, ESTATE_ACTOR=who,
                   ESTATE_LESSONS_PATH=self.lessons)
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True,
                              text=True, env=env)

    def followups(self, *args):
        env = dict(os.environ, ESTATE_STATE_DIR=self.state)
        result = subprocess.run([sys.executable, self.FOLLOWUPS, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def ask(self, body="a question", to="mechanic", sender="hub", *extra):
        return self.ok("ask", body, "--to", to, "--from", sender, *extra).stdout.strip()

    def row(self, key="q-1"):
        with self.conn() as c:
            return dict(c.execute("SELECT * FROM asks WHERE id=?", (key,)).fetchone())

    def ask_events(self, key="q-1"):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM events WHERE ask_id=? ORDER BY seq", (key,))]

    def overdue(self, key="q-1"):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        with self.conn() as c:
            c.execute("UPDATE asks SET expires_at=? WHERE id=?", (past, key))
        return past


class TestAskCreate(AskCase):
    def test_ask_mints_an_id_a_deadline_and_an_event(self):
        self.assertEqual(self.ask(), "q-1")
        row = self.row()
        self.assertEqual((row["from_actor"], row["to_actor"], row["status"]),
                         ("hub", "mechanic", "open"))
        self.assertTrue(row["expires_at"] > row["created_at"])
        self.assertEqual([e["kind"] for e in self.ask_events()], ["activity"])
        self.assertIn("asked mechanic", self.ask_events()[0]["summary"])

    def test_every_ask_carries_a_ttl_even_when_none_is_given(self):
        # The design ruling: no auth model between sessions, but nothing may sit
        # open forever, so the deadline is not something a caller can opt out of.
        self.ask()
        delta = (dt.datetime.fromisoformat(self.row()["expires_at"])
                 - dt.datetime.fromisoformat(self.row()["created_at"]))
        self.assertEqual(delta, dt.timedelta(hours=24))

    def test_ttl_must_be_well_formed_and_positive(self):
        for bad in ("soon", "12", "0h", "0s"):
            result = self.cli("ask", "q", "--to", "a", "--ttl", bad)
            self.assertEqual(result.returncode, 2, bad)
            self.assertIn("TTL", result.stderr)

    def test_empty_question_and_missing_addressee_are_refused(self):
        self.assertEqual(self.cli("ask", "   ", "--to", "mechanic").returncode, 2)
        self.assertNotEqual(self.cli("ask", "q").returncode, 0)
        self.assertEqual(self.cli("ask", "q", "--to", " ").returncode, 2)

    def test_about_links_a_task_or_project_and_rejects_a_dangling_one(self):
        self.add(); self.ok("project", "add", "p", "--kind", "generic")
        self.ask("about a task", "mechanic", "hub", "--about", "t-1")
        self.assertEqual((self.row()["task_id"], self.row()["project_id"]), ("t-1", None))
        self.ask("about a project", "mechanic", "hub", "--about", "p-1")
        self.assertEqual((self.row("q-2")["task_id"], self.row("q-2")["project_id"]),
                         (None, "p-1"))
        self.assertEqual(self.cli("ask", "q", "--to", "a", "--about", "t-99").returncode, 2)

    def test_the_sender_defaults_to_the_environment_actor(self):
        result = self.cli_as("spotter", "ask", "who am i", "--to", "hub")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.row()["from_actor"], "spotter")

    def test_asks_are_never_deleted(self):
        self.ask()
        with self.conn() as c:
            with self.assertRaises(sqlite3.IntegrityError): c.execute("DELETE FROM asks")


class TestAskThreads(AskCase):
    def test_a_root_ask_threads_under_itself_and_a_reply_inherits_it(self):
        self.ask("first")
        self.ok("answer", "q-1", "yes", "--actor", "mechanic")
        self.ask("second", "mechanic", "hub", "--reply-to", "q-1")
        self.assertEqual(self.row("q-1")["thread_id"], "q-1")
        self.assertEqual((self.row("q-2")["thread_id"], self.row("q-2")["reply_to"]),
                         ("q-1", "q-1"))
        self.assertIn("follow-up to q-1", self.ask_events("q-2")[0]["summary"])

    def test_a_reply_to_an_unknown_ask_is_refused(self):
        result = self.cli("ask", "q", "--to", "a", "--reply-to", "q-9")
        self.assertEqual(result.returncode, 2); self.assertIn("no ask matches", result.stderr)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM asks").fetchone()[0], 0)

    def test_log_reads_an_exchange_back_out_of_the_store(self):
        self.ask("survives the session")
        self.ok("answer", "q-1", "an answer", "--actor", "mechanic")
        out = self.ok("log", "q-1").stdout
        self.assertIn("asked mechanic", out); self.assertIn("an answer", out)
        self.assertEqual(self.cli("log", "q-9").returncode, 2)


class TestInbox(AskCase):
    def test_inbox_shows_only_what_is_addressed_to_the_reader(self):
        self.ask("for the mechanic", "mechanic")
        self.ask("for the briefer", "briefer")
        self.assertIn("q-1", self.ok("inbox", "--actor", "mechanic").stdout)
        self.assertNotIn("q-2", self.ok("inbox", "--actor", "mechanic").stdout)
        self.assertEqual(self.ok("inbox", "--actor", "spotter").stdout, "")

    def test_inbox_defaults_to_the_environment_actor(self):
        self.ask("for the mechanic", "mechanic")
        self.assertIn("q-1", self.cli_as("mechanic", "inbox").stdout)
        self.assertEqual(self.cli_as("briefer", "inbox").stdout, "")

    def test_a_past_deadline_ask_drops_out_of_the_default_read(self):
        # A loop's tick step should not spend tokens on a question whose asker
        # has already stopped waiting; expired-asks is where those surface.
        self.ask(); self.overdue()
        self.assertEqual(self.ok("inbox", "--actor", "mechanic").stdout, "")
        self.assertIn("q-1", self.ok("inbox", "--actor", "mechanic", "--all").stdout)

    def test_answered_asks_leave_the_inbox_but_stay_queryable(self):
        self.ask()
        self.ok("answer", "q-1", "done", "--actor", "mechanic")
        self.assertEqual(self.ok("inbox", "--actor", "mechanic").stdout, "")
        self.assertIn("q-1", self.ok("inbox", "--actor", "mechanic", "--all").stdout)
        self.assertIn("q-1", self.ok("inbox", "--status", "answered").stdout)

    def test_an_asker_can_sweep_its_own_outstanding_questions(self):
        self.ask("mine", "mechanic", "hub")
        self.ask("theirs", "mechanic", "spotter")
        out = self.ok("inbox", "--from", "hub").stdout
        self.assertIn("q-1", out); self.assertNotIn("q-2", out)

    def test_json_output_carries_the_whole_row(self):
        self.ask()
        rows = json.loads(self.ok("inbox", "--actor", "mechanic", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "q-1")
        self.assertEqual(rows[0]["body"], "a question")


class TestAnswer(AskCase):
    def test_answering_records_the_text_the_answerer_and_the_transition(self):
        self.ask()
        self.ok("answer", "q-1", "not my call", "--actor", "mechanic")
        row = self.row()
        self.assertEqual((row["status"], row["answer"], row["answered_by"]),
                         ("answered", "not my call", "mechanic"))
        self.assertTrue(row["answered_at"])
        self.assertEqual([e["summary"] for e in self.ask_events()][-1], "open -> answered")

    def test_an_empty_answer_and_an_unknown_ask_are_refused(self):
        self.ask()
        self.assertEqual(self.cli("answer", "q-1", "  ").returncode, 2)
        self.assertEqual(self.cli("answer", "q-9", "x").returncode, 2)
        self.assertEqual(self.row()["status"], "open")

    def test_a_question_is_answered_once(self):
        self.ask()
        self.ok("answer", "q-1", "first", "--actor", "mechanic")
        result = self.cli("answer", "q-1", "second", "--actor", "mechanic")
        self.assertEqual(result.returncode, 2)
        self.assertIn("already answered", result.stderr)
        self.assertEqual(self.row()["answer"], "first")

    def test_answering_as_someone_other_than_the_addressee_is_allowed_and_noted(self):
        # There is no trust model between estate sessions by design (§9 item 7).
        # Who actually answered is still worth recording.
        self.ask()
        self.ok("answer", "q-1", "covering for them", "--actor", "spotter")
        self.assertEqual(self.row()["answered_by"], "spotter")
        self.assertIn("addressed to mechanic", self.ask_events()[-1]["summary"])

    def test_a_late_answer_lands_with_a_warning_until_the_sweep_closes_it(self):
        # Throwing away a real answer to honour a clock edge helps nobody, so a
        # past-deadline ask stays answerable right up until it is swept.
        self.ask(); self.overdue()
        result = self.ok("answer", "q-1", "late but real", "--actor", "mechanic")
        self.assertIn("past its", result.stderr)
        self.assertEqual(self.row()["answer"], "late but real")
        self.assertIn("after the", self.ask_events()[-1]["summary"])
        self.assertEqual(self.ok("expired-asks").stdout, "")

    def test_a_swept_ask_is_closed_to_answers(self):
        self.ask(); self.overdue(); self.ok("expired-asks", "--sweep")
        result = self.cli("answer", "q-1", "too late", "--actor", "mechanic")
        self.assertEqual(result.returncode, 2)
        self.assertIn("expired", result.stderr); self.assertIn("t-1", result.stderr)
        self.assertIsNone(self.row()["answer"])


class TestAskExpiry(AskCase):
    def test_reporting_is_read_only_and_sweeping_is_the_mutation(self):
        # Same split as `orphans`: the hub's health pass owns the sweep.
        self.ask(); self.overdue()
        self.assertIn("q-1", self.ok("expired-asks").stdout)
        self.assertEqual(self.row()["status"], "open")
        self.assertIn("escalated 1", self.ok("expired-asks", "--sweep").stdout)
        self.assertEqual(self.row()["status"], "expired")
        self.assertTrue(self.row()["expired_at"])

    def test_an_ask_inside_its_deadline_is_left_alone(self):
        self.ask()
        self.assertEqual(self.ok("expired-asks").stdout, "")
        self.assertIn("escalated 0", self.ok("expired-asks", "--sweep").stdout)
        self.assertEqual(self.row()["status"], "open")

    def test_expiry_escalates_into_a_followup_instead_of_closing_quietly(self):
        self.ask("Does extraction need a quarantine stage?")
        self.overdue(); self.ok("expired-asks", "--sweep")
        self.assertEqual(self.row()["escalated_task"], "t-1")
        task = self.task("t-1")
        self.assertEqual((task["kind"], task["status"]), ("followup", "open"))
        self.assertIn("q-1", task["title"])
        # The follow-up lands on the asker: they are the one who has to decide
        # whether to proceed without the answer or ask again.
        self.assertEqual(task["created_by"], "hub")
        refs = json.loads(task["refs"])
        self.assertEqual((refs["legacy_id"], refs["ref"], refs["source"]),
                         ("followup:1", "q-1", "hub"))
        self.assertIn("quarantine stage", refs["context"])

    def test_the_escalated_followup_renders_through_bin_followups(self):
        # The real contract: `bin/followups` reads legacy_id straight out of refs
        # and raises without one, so an escalation has to mint a live number.
        if not self.followups_is_estate_backed():
            self.skipTest("bin/followups here is the standalone JSON store, not "
                          "the shim over this store — nothing to reconcile yet")
        # `--classification` is required at this writer (t-475); this item is
        # scenery for the numbering check, so the honest word is `action`.
        self.followups("add", "an existing item", "--source", "pr-reviewer",
                       "--classification", "action")
        self.ask("will go unanswered"); self.overdue()
        self.ok("expired-asks", "--sweep")
        out = self.followups("list").stdout
        self.assertIn("followup:1", out)
        self.assertIn("followup:2", out)
        self.assertIn("unanswered ask q-1", out)
        items = json.loads(self.followups("json").stdout)["items"]
        self.assertEqual(items["followup:2"]["ref"], "q-1")

    def test_sweeping_twice_does_not_escalate_the_same_ask_twice(self):
        self.ask(); self.overdue()
        self.ok("expired-asks", "--sweep")
        self.assertIn("escalated 0", self.ok("expired-asks", "--sweep").stdout)
        with self.conn() as c:
            self.assertEqual(c.execute(
                "SELECT count(*) FROM tasks WHERE kind='followup'").fetchone()[0], 1)

    def test_an_answered_ask_never_expires(self):
        self.ask()
        self.ok("answer", "q-1", "answered in time", "--actor", "mechanic")
        self.overdue()
        self.assertEqual(self.ok("expired-asks").stdout, "")
        self.assertIn("escalated 0", self.ok("expired-asks", "--sweep").stdout)
        self.assertEqual(self.row()["status"], "answered")


class TestAskDoctorAndMigration(AskCase):
    def archives(self): self.ok("export"); self.ok("backup")

    def test_doctor_is_green_for_a_healthy_mailbox(self):
        self.ask(); self.archives()
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0, result.stdout)
        for label in ("ask trigger present", "asks carry a deadline",
                      "expired asks swept"):
            self.assertIn(f"ok   {label}", result.stdout)

    def test_doctor_fails_on_a_deadline_less_ask(self):
        self.ask(); self.archives()
        with self.conn() as c: c.execute("UPDATE asks SET expires_at='' WHERE id='q-1'")
        result = self.cli("doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL asks carry a deadline", result.stdout)

    def test_doctor_stays_green_for_ordinary_sweep_lag(self):
        # A few hours past a deadline is normal; only a stalled sweep is a fault.
        self.ask(); self.overdue(); self.archives()
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_doctor_fails_when_the_sweep_has_clearly_stopped_running(self):
        self.ask(); self.archives()
        stalled = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
        with self.conn() as c:
            c.execute("UPDATE asks SET expires_at=? WHERE id='q-1'", (stalled,))
        result = self.cli("doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL expired asks swept", result.stdout)

    def test_migration_adds_ask_id_to_a_pre_mail_events_table(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
            task_id TEXT, project_id TEXT, kind TEXT NOT NULL, summary TEXT NOT NULL,
            detail TEXT, refs TEXT);
            INSERT INTO events (ts, actor, kind, summary) VALUES ('t', 'old', 'note', 'kept');""")
        conn.commit(); conn.close()
        self.ask()
        with self.conn() as c:
            self.assertIn("ask_id", {r[1] for r in c.execute("PRAGMA table_info(events)")})
            self.assertEqual(c.execute(
                "SELECT summary FROM events WHERE actor='old'").fetchone()[0], "kept")

    def test_doctor_migrates_a_pre_mail_store_instead_of_reporting_it_red(self):
        self.add(); self.archives()
        with self.conn() as c:
            c.executescript("DROP TRIGGER asks_no_delete; DROP TABLE asks;")
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("ok   ask trigger present", result.stdout)


class TestEventSearch(EstateCase):
    """P-16 / S46 — exact-match search across the whole events table.

    `estate log <id>` was always exact about one item. The cross-item question
    — everything one actor did, everywhere — had no answer, so an operator
    auditing a subsystem had only the recent tail. These tests are mostly about
    what the filters must NOT return: a filter that quietly widens is worse
    than no filter, because the result still looks like an audit.
    """

    def estate_for(self, *args):
        return [line.split() for line in self.ok("events", *args).stdout.splitlines()]

    def seqs(self, *args):
        return [int(row[0]) for row in self.estate_for(*args)]

    def populate(self):
        """Two tasks under two projects, touched by three different actors."""
        self.ok("project", "add", "alpha", "--kind", "rollout", "--actor", "hub")
        self.ok("project", "add", "beta", "--kind", "rollout", "--actor", "hub")
        self.ok("task", "add", "first", "--kind", "generic", "--project", "p-1",
                "--actor", "hub")
        self.ok("task", "add", "second", "--kind", "generic", "--project", "p-2",
                "--actor", "mechanic")
        self.ok("note", "t-1", "hub looked at it", "--actor", "hub")
        self.ok("note", "t-1", "mechanic looked at it", "--actor", "mechanic")
        self.ok("note", "t-2", "briefer looked at it", "--actor", "morning-brief")

    def test_actor_filter_excludes_every_other_actor(self):
        self.populate()
        rows = self.estate_for("--actor", "mechanic")
        self.assertTrue(rows, "the mechanic wrote events; none came back")
        for row in rows:
            self.assertIn("mechanic", row)
            self.assertNotIn("morning-brief", row)
        summaries = self.ok("events", "--actor", "mechanic").stdout
        self.assertIn("mechanic looked at it", summaries)
        self.assertNotIn("hub looked at it", summaries)
        self.assertNotIn("briefer looked at it", summaries)

    def test_task_filter_excludes_every_other_task(self):
        self.populate()
        out = self.ok("events", "--task", "t-2").stdout
        self.assertIn("briefer looked at it", out)
        self.assertNotIn("hub looked at it", out)
        self.assertNotIn("mechanic looked at it", out)

    def test_project_filter_excludes_every_other_project(self):
        self.populate()
        targets = {row[2] for row in self.estate_for("--project", "p-2")}
        self.assertEqual(targets, {"p-2", "t-2"})   # p-1 and t-1 stayed out

    def test_a_task_id_is_matched_exactly_not_as_a_prefix(self):
        """t-1 must never drag in t-10 — the classic substring-search bug."""
        for n in range(11):
            self.ok("task", "add", f"task {n}", "--kind", "generic")
        self.ok("note", "t-1", "belongs to one", "--actor", "hub")
        self.ok("note", "t-10", "belongs to ten", "--actor", "hub")
        out = self.ok("events", "--task", "t-1").stdout
        self.assertIn("belongs to one", out)
        self.assertNotIn("belongs to ten", out)

    def test_filters_compose_with_and(self):
        self.populate()
        both = self.ok("events", "--actor", "mechanic", "--task", "t-1").stdout
        self.assertIn("mechanic looked at it", both)
        self.assertNotIn("briefer looked at it", both)   # right actor, wrong task
        self.assertNotIn("hub looked at it", both)       # right task, wrong actor

    def test_kind_and_session_filter_independently(self):
        self.add()
        self.ok("event", "--actor", "ephemeral", "--kind", "note",
                "--summary", "worker note", "--task", "t-1",
                "--session-id", "address-comments-widgets-4502")
        self.ok("event", "--actor", "ephemeral", "--kind", "error",
                "--summary", "worker error", "--task", "t-1",
                "--session-id", "ci-fix-gizmo-1")
        by_kind = self.ok("events", "--kind", "error").stdout
        self.assertIn("worker error", by_kind)
        self.assertNotIn("worker note", by_kind)
        by_session = self.ok("events", "--session", "ci-fix-gizmo-1").stdout
        self.assertIn("worker error", by_session)
        self.assertNotIn("worker note", by_session)

    def test_memory_filter_finds_a_memorys_events_and_nothing_elses(self):
        self.add()
        key = self.remember("a lesson")
        self.ok("note", "t-1", "a task note", "--actor", "hub")
        out = self.ok("events", "--memory", key).stdout
        self.assertIn("memory created", out)
        self.assertNotIn("a task note", out)

    def test_results_are_always_ordered_by_sequence(self):
        self.populate()
        seqs = self.seqs()
        self.assertEqual(seqs, sorted(seqs), "events came back out of sequence")

    def test_limit_takes_the_most_recent_and_keeps_them_in_sequence(self):
        self.populate()
        everything = self.seqs()
        tail = self.seqs("--limit", "2")
        self.assertEqual(tail, everything[-2:])
        self.assertEqual(tail, sorted(tail))

    def test_a_persona_alias_folds_into_its_canonical_actor(self):
        """docs/actor-taxonomy.md: a persona name IS its technical actor.

        lib/actors.ALIASES ships EMPTY — every entry is one installation's own
        identity — so the fold is tested by adding an alias here rather than by
        naming somebody's persona. What is asserted is the mechanism: an
        aliased spelling is matched by a query for the canonical name, exactly,
        and nothing else is swept in with it.
        """
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "lib"))
        import actors
        import estate_events
        self.assertEqual(actors.ALIASES, {},
                         "an installation's persona name has been committed here")
        actors.ALIASES["a-persona"] = actors.OPERATOR
        self.addCleanup(actors.ALIASES.pop, "a-persona", None)

        self.assertEqual(actors.normalize("a-persona").actor, "hub")
        self.assertEqual(actors.normalize("a-persona").actor_class, "operator")
        clause, params = estate_events.actor_clause("hub")
        self.assertEqual(sorted(params), ["a-persona", "hub"])
        self.assertNotIn("LIKE", clause)

    def test_no_filter_returns_the_whole_table(self):
        self.populate()
        with self.conn() as c:
            total = c.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(len(self.seqs()), total)

    def test_json_output_carries_the_full_rows(self):
        self.populate()
        rows = json.loads(self.ok("events", "--actor", "morning-brief", "--json").stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], "briefer looked at it")
        self.assertEqual(rows[0]["task_id"], "t-2")

    def test_search_writes_nothing(self):
        self.populate()
        with self.conn() as c:
            before = c.execute("SELECT count(*) FROM events").fetchone()[0]
        self.ok("events", "--actor", "hub")
        self.ok("events", "--task", "t-1")
        with self.conn() as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM events").fetchone()[0], before)


class TestRestartPersistence(EstateCase):
    """P-17 / S28 + S34 — the store survives the process that wrote it.

    Every other test here happens to use a fresh subprocess per command, so
    persistence is assumed everywhere and demonstrated nowhere. That is a thin
    place to leave unguarded: SQLite in WAL mode keeps recent commits in a
    sidecar file until a checkpoint, and a bug in how connections are opened or
    closed — a missing commit, a connection left open holding an uncheckpointed
    WAL, a store path derived per-process — would show up as data that is
    perfectly readable inside one CLI run and gone from the next.

    So this builds a whole estate (projects, tasks in every lifecycle state,
    dependencies, a privately scoped memory and a shared one), takes a full
    snapshot through one CLI process, and re-reads it through another. Then it
    does the harder version: carries only the on-disk files to a directory that
    has never been opened before and reads them there, which is what
    docs/new-machine-setup.md's rsync actually does.
    """

    # An arbitrary private scope. The audit calls it "Context A"; what matters
    # is that it is NOT `shared`, so the scope boundary is what is being tested.
    CONTEXT_A = "context-a"

    def build(self) -> dict:
        """Create the estate, and return what every field should read as."""
        self.ok("project", "add", "gap-audit rollout", "--kind", "rollout",
                "--key", "rollout-2026-07", "--actor", "hub",
                "--meta", '{"canvas": "F0BLPL03X99"}')
        self.ok("project", "add", "a closed project", "--kind", "rollout",
                "--actor", "hub")
        self.ok("project", "close", "p-2", "--actor", "hub")

        self.ok("task", "add", "still open", "--kind", "rollout",
                "--project", "p-1", "--actor", "hub",
                "--intent", "the intent survives verbatim")
        self.ok("task", "add", "ready to go", "--kind", "rollout",
                "--project", "p-1", "--ready", "--actor", "hub",
                "--due", "2026-08-01", "--refs", '{"proposal": "P-17"}')
        self.ok("task", "add", "someone is on it", "--kind", "rollout",
                "--project", "p-1", "--ready", "--actor", "hub")
        self.ok("claim", "t-3", "--actor", "mechanic", "--ttl", "4h")
        self.ok("task", "add", "finished", "--kind", "rollout",
                "--project", "p-1", "--ready", "--actor", "hub")
        self.ok("claim", "t-4", "--actor", "hub")
        self.ok("done", "t-4", "--actor", "hub", "--summary", "did it",
                "--verification", "tests pass")
        self.ok("task", "add", "waiting on the owner", "--kind", "rollout",
                "--ready", "--actor", "hub")
        self.ok("claim", "t-5", "--actor", "hub")
        self.ok("needs-owner", "t-5", "an open design choice", "--actor", "hub")
        self.ok("task", "add", "abandoned", "--kind", "rollout", "--actor", "hub")
        self.ok("drop", "t-6", "not worth it", "--actor", "hub")

        # t-1 blocks t-2; t-3 was discovered while working t-1.
        self.ok("dep", "add", "t-1", "t-2", "--kind", "blocks", "--actor", "hub")
        self.ok("dep", "add", "t-1", "t-3", "--kind", "discovered-from",
                "--actor", "hub")

        private = self.remember("only this context should see this",
                                scope=self.CONTEXT_A, active=True)
        shared = self.shared("every loop should see this",
                             refs='{"ledger": [1, 2]}')
        return {"private": private, "shared": shared}

    def dump(self) -> dict:
        """The whole store, read through a brand-new CLI process."""
        return json.loads(self.ok("json").stdout)

    def relocate(self) -> str:
        """Copy only the on-disk store into a directory nothing has opened.

        The rsync in docs/new-machine-setup.md moves files, not processes. If
        anything about this store lived in a connection rather than on disk,
        this is where it would go missing.
        """
        fresh = tempfile.mkdtemp(dir=self.tmp.name)
        for name in os.listdir(self.state):
            if name.startswith("estate.db"):      # .db, -wal and -shm alike
                shutil.copy2(os.path.join(self.state, name),
                             os.path.join(fresh, name))
        return fresh

    def test_every_row_is_byte_identical_after_reopening(self):
        self.build()
        before = self.dump()
        after = self.dump()          # a second, independent CLI process
        self.assertEqual(before, after)
        for table in ("projects", "tasks", "task_deps", "events", "memories"):
            self.assertTrue(before[table], f"{table} was empty; nothing proved")

    def test_ids_and_states_survive(self):
        self.build()
        tasks = {row["id"]: row for row in self.dump()["tasks"]}
        self.assertEqual(sorted(tasks), ["t-1", "t-2", "t-3", "t-4", "t-5", "t-6"])
        self.assertEqual(
            {key: row["status"] for key, row in tasks.items()},
            {"t-1": "open", "t-2": "ready", "t-3": "claimed", "t-4": "done",
             "t-5": "needs-owner", "t-6": "dropped"})

    def test_every_task_field_survives_verbatim(self):
        self.build()
        tasks = {row["id"]: row for row in self.dump()["tasks"]}
        self.assertEqual(tasks["t-1"]["intent"], "the intent survives verbatim")
        self.assertEqual(tasks["t-1"]["created_by"], "hub")
        self.assertEqual(tasks["t-2"]["refs"], '{"proposal": "P-17"}')
        self.assertTrue(tasks["t-2"]["due_at"].startswith("2026-08-01"))
        self.assertEqual(tasks["t-3"]["claimed_by"], "mechanic")
        self.assertIsNotNone(tasks["t-3"]["claim_expires_at"])
        self.assertIsNotNone(tasks["t-4"]["closed_at"])
        # Nothing was invented on the way back either.
        for row in tasks.values():
            self.assertEqual(row["kind"], "rollout")

    def test_project_links_survive(self):
        self.build()
        dump = self.dump()
        projects = {row["id"]: row for row in dump["projects"]}
        self.assertEqual(projects["p-1"]["external_key"], "rollout-2026-07")
        self.assertEqual(projects["p-1"]["meta"], '{"canvas": "F0BLPL03X99"}')
        self.assertEqual(projects["p-1"]["status"], "active")
        self.assertEqual(projects["p-2"]["status"], "done")
        links = {row["id"]: row["project_id"] for row in dump["tasks"]}
        self.assertEqual(links, {"t-1": "p-1", "t-2": "p-1", "t-3": "p-1",
                                 "t-4": "p-1", "t-5": None, "t-6": None})

    def test_dependencies_survive_with_their_kinds_and_direction(self):
        self.build()
        deps = {(row["from_task"], row["to_task"]): row["dep_kind"]
                for row in self.dump()["task_deps"]}
        self.assertEqual(deps, {("t-1", "t-2"): "blocks",
                                ("t-1", "t-3"): "discovered-from"})
        # And the CLI still reads the direction the same way afterwards.
        out = self.ok("dep", "list", "t-2").stdout
        self.assertIn("in t-1 --blocks--> t-2", " ".join(out.split()))

    def test_both_memory_scopes_survive_with_their_bodies(self):
        keys = self.build()
        memories = {row["id"]: row for row in self.dump()["memories"]}
        private, shared = memories[keys["private"]], memories[keys["shared"]]
        self.assertEqual(private["body"], "only this context should see this")
        self.assertEqual(private["scope"], self.CONTEXT_A)
        self.assertEqual(private["status"], "active")
        self.assertEqual(shared["body"], "every loop should see this")
        self.assertEqual(shared["scope"], "shared")
        self.assertEqual(shared["source_refs"], '{"ledger": [1, 2]}')

    def test_the_scope_boundary_still_holds_after_the_reopen(self):
        """A private fact must not leak into shared recall on the way back."""
        keys = self.build()
        listed = self.ok("memory", "list", "--scope", "shared").stdout
        self.assertIn(keys["shared"], listed)
        self.assertNotIn(keys["private"], listed)
        self.assertNotIn("only this context should see this", listed)

    def test_the_event_stream_survives_in_sequence(self):
        self.build()
        events = self.dump()["events"]
        self.assertEqual([row["seq"] for row in events],
                         list(range(1, len(events) + 1)))
        self.assertIn("task created (open)",
                      [row["summary"] for row in events])

    def test_the_store_reads_the_same_from_a_directory_never_opened_before(self):
        keys = self.build()
        before = self.dump()
        after = json.loads(run_cli(["json"], self.relocate()).stdout)
        self.assertEqual(before, after)
        self.assertEqual(
            [row["body"] for row in after["memories"]
             if row["id"] == keys["private"]],
            ["only this context should see this"])

    def test_the_next_writer_continues_the_ids_it_reopened(self):
        """Reopening must not restart the counters and reuse an id."""
        self.build()
        self.assertEqual(self.ok("task", "add", "after the reopen",
                                 "--kind", "rollout").stdout.strip(), "t-7")
        highest = max(row["seq"] for row in self.dump()["events"])
        self.ok("note", "t-7", "one more", "--actor", "hub")
        self.assertEqual(max(row["seq"] for row in self.dump()["events"]),
                         highest + 1)


class TestComputedBlockers(EstateCase):
    """P-18 / S29 — a blocked task says so, without its status changing.

    `estate ready` always excluded a task with an unresolved `blocks` edge, so
    scheduling was right. Every other view was not: `task list` rendered it as
    `ready` with nothing to say what it was waiting on. The two are different
    claims — "correctly excluded from scheduling" and "visibly blocked" — and
    only the first was implemented.

    The field is derived, never stored. These tests hold that line in both
    directions: the stored status must stay untouched, and the derived field
    must clear itself the moment the blocker finishes, with nothing written.
    """

    def chain(self):
        """t-1 blocks t-2; t-1 and t-3 both block t-4. t-5 is unrelated."""
        for title in ("first blocker", "waits on one", "second blocker",
                      "waits on both", "unrelated work"):
            self.ok("task", "add", title, "--kind", "generic", "--ready")
        self.ok("dep", "add", "t-1", "t-2", "--kind", "blocks")
        self.ok("dep", "add", "t-1", "t-4", "--kind", "blocks")
        self.ok("dep", "add", "t-3", "t-4", "--kind", "blocks")

    def day(self, offset):
        return (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(days=offset)).date().isoformat()

    def show(self, key) -> dict:
        """`task show` prints the row's JSON, then its event log."""
        out = self.ok("task", "show", key).stdout
        return json.loads(out[:out.index("\n}") + 2])

    def listed(self, *args) -> dict:
        """task id -> its rendered line."""
        return {line.split()[0]: line
                for line in self.ok("task", "list", *args).stdout.splitlines()}

    def finish(self, key):
        self.ok("claim", key, "--actor", "hub")
        self.ok("done", key, "--actor", "hub", "--summary", "done",
                "--verification", "tests pass")

    def test_task_list_names_the_blockers(self):
        self.chain()
        lines = self.listed()
        self.assertIn("blocked by t-1", lines["t-2"])
        self.assertIn("blocked by t-1, t-3", lines["t-4"])

    def test_an_unblocked_task_says_nothing(self):
        self.chain()
        lines = self.listed()
        for key in ("t-1", "t-3", "t-5"):
            self.assertNotIn("blocked by", lines[key])

    def test_the_stored_status_is_untouched(self):
        """The schema still says ready. blocked_by is a view field only."""
        self.chain()
        self.assertEqual(self.task("t-2")["status"], "ready")
        self.assertEqual(self.show("t-2")["status"], "ready")
        self.assertIn("[ready", self.listed()["t-2"])
        with self.conn() as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
        self.assertNotIn("blocked_by", columns,
                         "blocked_by must never become a stored column")

    def test_task_show_carries_the_blocker_ids(self):
        self.chain()
        self.assertEqual(self.show("t-4")["blocked_by"], ["t-1", "t-3"])
        self.assertEqual(self.show("t-5")["blocked_by"], [])

    def test_it_clears_when_the_blocker_is_done(self):
        self.chain()
        self.assertEqual(self.show("t-2")["blocked_by"], ["t-1"])
        self.finish("t-1")
        self.assertEqual(self.show("t-2")["blocked_by"], [])
        self.assertNotIn("blocked by", self.listed()["t-2"])
        self.assertEqual(self.task("t-2")["status"], "ready")

    def test_it_clears_when_the_blocker_is_dropped(self):
        """Dropped is terminal too — an abandoned task blocks nothing."""
        self.chain()
        self.ok("drop", "t-1", "not doing it")
        self.assertEqual(self.show("t-2")["blocked_by"], [])

    def test_a_partially_cleared_task_still_names_what_is_left(self):
        self.chain()
        self.finish("t-1")
        self.assertEqual(self.show("t-4")["blocked_by"], ["t-3"])
        self.finish("t-3")
        self.assertEqual(self.show("t-4")["blocked_by"], [])

    def test_a_non_terminal_transition_does_not_clear_it(self):
        """Claimed, blocked and needs-owner are all still someone's to do."""
        self.chain()
        self.ok("claim", "t-1", "--actor", "hub")
        self.assertEqual(self.show("t-2")["blocked_by"], ["t-1"])
        self.ok("needs-owner", "t-1", "an open question", "--actor", "hub")
        self.assertEqual(self.show("t-2")["blocked_by"], ["t-1"])

    def test_only_blocks_edges_block(self):
        self.add("standalone", ready=True)
        self.add("discovered while doing it", ready=True)
        for kind in ("parent", "related", "discovered-from"):
            self.ok("dep", "add", "t-1", "t-2", "--kind", kind)
        self.assertEqual(self.show("t-2")["blocked_by"], [])

    def test_the_direction_of_the_edge_matters(self):
        """`t-1 blocks t-2` must not make t-1 look blocked by t-2."""
        self.chain()
        self.assertEqual(self.show("t-1")["blocked_by"], [])

    def test_ready_and_the_view_agree(self):
        """The scheduler's exclusion and the badge come from the same rule."""
        self.chain()
        ready = {line.split()[0] for line in self.ok("ready").stdout.splitlines()}
        blocked = {key for key in ("t-1", "t-2", "t-3", "t-4", "t-5")
                   if self.show(key)["blocked_by"]}
        self.assertEqual(ready, {"t-1", "t-3", "t-5"})
        self.assertEqual(blocked, {"t-2", "t-4"})
        self.assertFalse(ready & blocked)

    def test_due_and_stale_views_carry_it_too(self):
        self.chain()
        self.ok("task", "set-due", "t-2", self.day(1))
        rows = json.loads(self.ok("due", "--within", "7", "--json").stdout)
        self.assertEqual([r["blocked_by"] for r in rows if r["id"] == "t-2"],
                         [["t-1"]])
        rows = json.loads(self.ok("stale", "--days", "0", "--json").stdout)
        self.assertEqual([r["blocked_by"] for r in rows if r["id"] == "t-4"],
                         [["t-1", "t-3"]])

    def test_reading_a_blocked_view_writes_nothing(self):
        self.chain()
        with self.conn() as c:
            before = c.execute("SELECT count(*) FROM events").fetchone()[0]
            rows = [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY seq")]
        self.ok("task", "list")
        self.ok("task", "show", "t-4")
        with self.conn() as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM events").fetchone()[0], before)
            self.assertEqual(
                [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY seq")],
                rows)


class AutoPromoteCase(EstateCase):
    """Shared fixture for t-163 — auto-promote a dependent on unblock.

    `blocked_by` stays a derived view. What is new is the ACT: when the last
    blocker of a FLAGGED dependent finishes, the store moves it open -> ready
    itself. Opt-in, default off, and the default is what most of these tests
    are actually about.
    """

    def blocker(self, title="the blocker"):
        return self.ok("task", "add", title, "--kind", "generic",
                       "--ready").stdout.strip()

    def dependent(self, blocker, *, flag=None, kind="night-shift",
                  project=None, stage=None, title="the dependent"):
        args = ["task", "add", title, "--kind", kind]
        if flag is not None:
            args += ["--refs", json.dumps({"auto_promote": flag})]
        if project: args += ["--project", project]
        if stage: args += ["--stage", stage]
        key = self.ok(*args).stdout.strip()
        self.ok("dep", "add", blocker, key, "--kind", "blocks")
        return key

    def finish(self, key):
        self.ok("claim", key, "--actor", "hub")
        return self.ok("done", key, "--actor", "hub", "--summary", "done",
                       "--verification", "tests pass")

    def status(self, key):
        return self.task(key)["status"]

    def promotions(self, key):
        return [e for e in self.events(key) if e["kind"] == "auto-promote"]

    def followups(self, *args):
        """`bin/followups`, against this test's own store."""
        env = dict(os.environ, ESTATE_STATE_DIR=self.state,
                   ESTATE_LESSONS_PATH=os.path.join(self.state, "lessons.md"))
        shim = os.path.join(os.path.dirname(SCRIPT), "followups")
        result = subprocess.run([sys.executable, shim, *args],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    @staticmethod
    def followups_takes_classification():
        """True when bin/followups enforces the filing-time schema.

        The same shape of check as AskCase.followups_is_estate_backed above,
        and for the same reason: this is a contract between two scripts and
        only one of them is in this extraction. A repo whose bin/followups
        predates the schema cannot be filed through with `--classification`,
        and asserting against it would report a missing re-sync as a bug in
        this store's promote hook. On the FILE rather than a version string,
        so these tests start running again by themselves the moment that
        script lands.
        """
        try:
            with open(os.path.join(os.path.dirname(SCRIPT), "followups")) as f:
                return "--classification" in f.read()
        except OSError:
            return False


class TestAutoPromoteOnUnblock(AutoPromoteCase):
    def test_a_flagged_dependent_is_promoted(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.assertEqual(self.status(d), "open")
        self.finish(b)
        self.assertEqual(self.status(d), "ready")

    def test_an_unflagged_dependent_is_untouched(self):
        """The load-bearing half. Default off means default off."""
        b = self.blocker()
        d = self.dependent(b)
        self.finish(b)
        self.assertEqual(self.status(d), "open")
        self.assertEqual(self.promotions(d), [])
        # And nothing about the pre-t-163 behaviour moved: it is not claimable,
        # it is not in `ready`, and the derived view says it is unblocked.
        self.assertNotIn(d, self.ok("ready").stdout)

    def test_an_explicit_false_is_untouched(self):
        b = self.blocker()
        d = self.dependent(b, flag=False)
        self.finish(b)
        self.assertEqual(self.status(d), "open")

    def test_the_promoted_task_becomes_claimable(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.finish(b)
        self.assertIn(d, self.ok("ready", "--kind", "night-shift").stdout)
        self.ok("claim", d, "--actor", "pr-reviewer")
        self.assertEqual(self.status(d), "claimed")

    def test_the_promotion_writes_a_distinct_event(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.finish(b)
        rows = self.promotions(d)
        self.assertEqual(len(rows), 1)
        self.assertIn(f"after {b}", rows[0]["summary"])
        self.assertEqual(json.loads(rows[0]["refs"]),
                         {"blocker": b, "blocker_status": "done",
                          "flag_source": "task", "kind": "night-shift"})

    def test_the_ordinary_transition_event_is_written_too(self):
        """A reader that counts lifecycle transitions must see this one."""
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.finish(b)
        moves = [e for e in self.events(d)
                 if e["kind"] == "transition" and e["summary"] == "open -> ready"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["subsystem"], "tasks")
        self.assertEqual(moves[0]["phase"], "transition")
        self.assertIn(f"last blocker {b}", moves[0]["detail"])

    def test_the_store_is_the_actor_not_the_closing_worker(self):
        """Rule 6 of the actor taxonomy: the promotion is nobody's judgment."""
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.finish(b)
        for row in self.events(d):
            if row["kind"] in ("auto-promote", "transition"):
                self.assertEqual(row["actor"], "estate")

    def test_it_is_announced_on_stdout(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        out = self.finish(b).stdout
        self.assertIn(f"{d}: open -> ready (auto-promoted", out)
        self.assertIn(f"last blocker {b} reached done", out)

    def test_the_announcement_names_the_status_the_blocker_really_reached(self):
        """`dropped`, not "finished" — the line a person reads, not the refs.

        The hook fires on `dropped` as well as `done`, correctly. The event
        refs have said `blocker_status` all along; the printed line said
        "finished", which is the wrong thing to tell whoever just abandoned
        the blocker and is exactly the moment they might want to stop the
        thing that started.
        """
        b = self.blocker()
        d = self.dependent(b, flag=True)
        out = self.ok("drop", b, "not doing it").stdout
        self.assertIn(f"{d}: open -> ready (auto-promoted", out)
        self.assertIn(f"last blocker {b} reached dropped", out)
        self.assertNotIn("finished", out)

    def test_nothing_is_announced_when_nothing_moved(self):
        b = self.blocker()
        self.dependent(b)
        self.assertNotIn("auto-promoted", self.finish(b).stdout)

    def test_a_dropped_blocker_promotes_too(self):
        """`dropped` is terminal, so an abandoned blocker blocks nothing."""
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.ok("drop", b, "not doing it")
        self.assertEqual(self.status(d), "ready")
        self.assertEqual(json.loads(self.promotions(d)[0]["refs"])["blocker_status"],
                         "dropped")

    def test_only_the_last_blocker_promotes(self):
        first, second = self.blocker("first"), self.blocker("second")
        d = self.dependent(first, flag=True)
        self.ok("dep", "add", second, d, "--kind", "blocks")
        self.finish(first)
        self.assertEqual(self.status(d), "open")
        self.assertEqual(self.promotions(d), [])
        self.finish(second)
        self.assertEqual(self.status(d), "ready")
        self.assertEqual(len(self.promotions(d)), 1)

    def test_a_non_terminal_transition_promotes_nothing(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        for verb, args in (("claim", ("--actor", "hub")),
                           ("needs-owner", ("an open question", "--actor", "hub")),
                           ("reopen", ("--actor", "hub")),
                           ("claim", ("--actor", "hub")),
                           ("release", ("--actor", "hub"))):
            self.ok(verb, b, *args)
            self.assertEqual(self.status(d), "open", f"after {verb}")
        self.assertEqual(self.promotions(d), [])

    def test_only_blocks_edges_promote(self):
        b = self.blocker()
        d = self.ok("task", "add", "related work", "--kind", "night-shift",
                    "--refs", '{"auto_promote": true}').stdout.strip()
        for kind in ("parent", "related", "discovered-from"):
            self.ok("dep", "add", b, d, "--kind", kind)
        self.finish(b)
        self.assertEqual(self.status(d), "open")

    def test_the_direction_of_the_edge_matters(self):
        """Finishing the DEPENDENT must not promote its blocker."""
        b = self.ok("task", "add", "the blocker", "--kind", "generic",
                    "--refs", '{"auto_promote": true}').stdout.strip()
        d = self.dependent(b, flag=True)
        self.ok("mark-ready", d, "--actor", "hub")
        self.finish(d)
        self.assertEqual(self.status(b), "open")

    def test_only_an_open_dependent_is_promoted(self):
        """`blocked` and `needs-owner` wait on a person, not on this edge."""
        for parked in ("blocked", "needs-owner"):
            with self.subTest(parked=parked):
                b = self.blocker(f"blocker for {parked}")
                d = self.dependent(b, flag=True, title=f"dependent {parked}")
                self.ok("mark-ready", d, "--actor", "hub")
                self.ok("claim", d, "--actor", "hub")
                verb = "block" if parked == "blocked" else "needs-owner"
                self.ok(verb, d, "a reason", "--actor", "hub")
                self.finish(b)
                self.assertEqual(self.status(d), parked)
                self.assertEqual(self.promotions(d), [])

    def test_an_already_ready_dependent_is_left_alone(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.ok("mark-ready", d, "--actor", "hub")
        self.finish(b)
        self.assertEqual(self.status(d), "ready")
        self.assertEqual(self.promotions(d), [])

    def test_reopening_the_blocker_does_not_demote(self):
        """The derived view re-blocks it. Nothing here keeps a second copy."""
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.finish(b)
        self.ok("reopen", b, "--actor", "hub")
        self.assertEqual(self.status(d), "ready")
        self.assertNotIn(d, self.ok("ready").stdout)
        out = self.ok("task", "show", d).stdout
        self.assertEqual(json.loads(out[:out.index("\n}") + 2])["blocked_by"], [b])

    def test_promotion_does_not_cascade(self):
        """`ready` is not terminal, so promoting one task releases no other."""
        b = self.blocker()
        first = self.dependent(b, flag=True, title="first in the chain")
        second = self.dependent(first, flag=True, title="second in the chain")
        self.finish(b)
        self.assertEqual(self.status(first), "ready")
        self.assertEqual(self.status(second), "open")


class TestAutoPromoteInheritance(AutoPromoteCase):
    """Where the flag comes from: the task's own refs, else its project."""

    def project(self, flag=None, title="a flagged project"):
        args = ["project", "add", title, "--kind", "workstream"]
        if flag is not None:
            args += ["--meta", json.dumps({"auto_promote": flag})]
        return self.ok(*args).stdout.strip()

    def test_a_project_flag_is_inherited(self):
        p = self.project(flag=True)
        b = self.blocker()
        d = self.dependent(b, project=p)
        self.finish(b)
        self.assertEqual(self.status(d), "ready")
        self.assertEqual(json.loads(self.promotions(d)[0]["refs"])["flag_source"],
                         "project")

    def test_an_unflagged_project_promotes_nothing(self):
        p = self.project()
        b = self.blocker()
        d = self.dependent(b, project=p)
        self.finish(b)
        self.assertEqual(self.status(d), "open")

    def test_the_task_flag_wins_over_the_project(self):
        p = self.project(flag=True)
        b = self.blocker()
        d = self.dependent(b, flag=False, project=p)
        self.finish(b)
        self.assertEqual(self.status(d), "open",
                         "an explicit task-level false must opt out of a "
                         "project-wide default")

    def test_a_task_flag_works_without_a_project(self):
        p = self.project(flag=False)
        b = self.blocker()
        d = self.dependent(b, flag=True, project=p)
        self.finish(b)
        self.assertEqual(self.status(d), "ready")
        self.assertEqual(json.loads(self.promotions(d)[0]["refs"])["flag_source"],
                         "task")


class TestAutoPromoteRefusals(AutoPromoteCase):
    """The cases where promoting would be wrong, and the store declines."""

    def test_a_non_boolean_flag_is_not_consent(self):
        b = self.blocker()
        d = self.dependent(b, flag="yes")
        result = self.finish(b)
        self.assertEqual(self.status(d), "open")
        self.assertIn("must be true or false", result.stderr)

    def test_an_unreadable_refs_blob_does_not_break_the_close(self):
        b = self.blocker()
        d = self.dependent(b)
        with self.conn() as c:
            c.execute("UPDATE tasks SET refs='not json' WHERE id=?", (d,))
        self.finish(b)
        self.assertEqual(self.status(b), "done")
        self.assertEqual(self.status(d), "open")

    def test_an_unreadable_project_meta_does_not_break_the_close(self):
        p = self.ok("project", "add", "a project", "--kind", "workstream").stdout.strip()
        with self.conn() as c:
            c.execute("UPDATE projects SET meta='not json' WHERE id=?", (p,))
        b = self.blocker()
        d = self.dependent(b, project=p)
        self.finish(b)
        self.assertEqual(self.status(b), "done")
        self.assertEqual(self.status(d), "open")

    def test_an_undecided_proposal_is_never_promoted(self):
        """Moving a pending proposal to `ready` would look like a decision."""
        b = self.blocker()
        d = self.dependent(b, flag=True, kind="proposal", stage="pending-review")
        self.finish(b)
        self.assertEqual(self.status(d), "open")
        self.assertEqual(self.promotions(d), [])

    def test_an_approved_proposal_is_promoted(self):
        b = self.blocker()
        d = self.dependent(b, flag=True, kind="proposal",
                           stage="approved-backlog")
        self.finish(b)
        self.assertEqual(self.status(d), "ready")


class TestAutoPromoteFromAProposalDecision(AutoPromoteCase):
    """The other door into a terminal status (P-02's drive_task_status).

    A review decision closes its task without going through the verb machine,
    which is exactly why the hook lives in apply_status and not in
    transition(). A blocker closed by the owner's decision has finished as surely as
    one a worker completed.
    """

    def test_a_resolved_proposal_promotes_its_dependents(self):
        b = self.ok("proposal", "add", "a condition worth fixing",
                    "--actor", "mechanic").stdout.strip()
        d = self.dependent(b, flag=True)
        self.ok("proposal", "stage", b, "resolved", "--actor", "hub")
        self.assertEqual(self.status(b), "done")
        self.assertEqual(self.status(d), "ready")
        self.assertEqual(json.loads(self.promotions(d)[0]["refs"])["blocker_status"],
                         "done")

    def test_a_rejected_proposal_promotes_its_dependents(self):
        b = self.ok("proposal", "add", "a condition to turn down",
                    "--actor", "mechanic").stdout.strip()
        d = self.dependent(b, flag=True)
        self.ok("proposal", "stage", b, "rejected", "--actor", "hub")
        self.assertEqual(self.status(b), "dropped")
        self.assertEqual(self.status(d), "ready")


class TestAutoPromoteFromAResolvedFollowUp(AutoPromoteCase):
    """The THIRD door into a terminal status: `bin/followups resolve` (t-206).

    Written for this landing, not ported. Branch B's own c1 assertions for
    this chain could not come across — they need B's `scratch` helper, assert
    a row in an `unblocked` attention bucket this base deliberately does not
    build, and assert an event summary this base writes differently. That left
    the case with no automated coverage at all, which is the wrong shape: it
    is the case t-163 was written for. "Waiting on the owner to answer X" is the
    commonest reason real work here is queued behind something, and a
    follow-up is how that waiting is recorded.

    So it is re-asserted here in this base's own shape. The chain under test is
    the whole assembly and it crosses two files: `bin/followups resolve` ->
    `estate.apply_status` -> the hook -> the event under actor `estate` ->
    `estate ready --auto-promoted`.
    """

    def resolvable(self):
        """A follow-up, and its id as a task."""
        if not self.followups_takes_classification():
            self.skipTest("bin/followups here predates the filing-time "
                          "classification schema — the other half of this "
                          "contract has not been re-synced yet")
        self.followups("add", "answer the design question",
                       "--classification", "clarification")
        return "t-1"

    def test_a_resolved_follow_up_promotes_its_flagged_dependent(self):
        b = self.resolvable()
        d = self.dependent(b, flag=True)
        self.assertEqual(self.status(d), "open")
        self.followups("resolve", "followup:1", "answered")
        self.assertEqual(self.status(b), "done")
        self.assertEqual(self.status(d), "ready")

    def test_the_store_is_the_actor_not_whoever_resolved_it(self):
        b = self.resolvable()
        d = self.dependent(b, flag=True)
        self.followups("resolve", "followup:1", "answered")
        promotion = self.promotions(d)[0]
        self.assertEqual(promotion["actor"], "estate")
        self.assertEqual(json.loads(promotion["refs"])["blocker"], b)

    def test_the_close_is_tagged_the_way_every_other_close_is(self):
        """The other half of t-206: P-06 could not see this subsystem's
        commonest terminal outcome, because this path wrote its own bare
        `open -> resolved` event."""
        b = self.resolvable()
        self.dependent(b, flag=True)
        self.followups("resolve", "followup:1", "answered")
        close = [e for e in self.events(b)
                 if e["kind"] == "transition" and e["summary"] == "open -> done"]
        self.assertEqual(len(close), 1)
        self.assertEqual(close[0]["subsystem"], "tasks")
        self.assertEqual(close[0]["phase"], "outcome")

    def test_the_promoted_task_reaches_the_hubs_query(self):
        b = self.resolvable()
        d = self.dependent(b, flag=True)
        self.assertEqual(self.ok("ready", "--auto-promoted").stdout.strip(), "")
        self.followups("resolve", "followup:1", "answered")
        self.assertIn(d, self.ok("ready", "--auto-promoted").stdout)

    def test_an_unflagged_dependent_is_left_alone(self):
        b = self.resolvable()
        d = self.dependent(b)
        self.followups("resolve", "followup:1", "answered")
        self.assertEqual(self.status(d), "open")
        self.assertEqual(self.promotions(d), [])
        self.assertEqual(self.ok("ready", "--auto-promoted").stdout.strip(), "")

    def test_resolving_twice_promotes_once(self):
        """An already-terminal follow-up is left alone, so the promotion does
        not fire a second time on a task somebody has since claimed."""
        b = self.resolvable()
        d = self.dependent(b, flag=True)
        self.followups("resolve", "followup:1", "answered")
        self.followups("resolve", "followup:1", "answered again")
        self.assertEqual(len(self.promotions(d)), 1)


class TestAutoPromoteVerb(AutoPromoteCase):
    """`task auto-promote` — the retrofit path for tasks that already exist."""

    def refs(self, key):
        return json.loads(self.task(key)["refs"] or "{}")

    def test_on_sets_the_flag_and_takes_effect(self):
        b = self.blocker()
        d = self.dependent(b)
        self.ok("task", "auto-promote", d, "--on", "--actor", "hub")
        self.assertIs(self.refs(d)["auto_promote"], True)
        self.finish(b)
        self.assertEqual(self.status(d), "ready")

    def test_off_writes_an_explicit_false(self):
        b = self.blocker()
        d = self.dependent(b, flag=True)
        self.ok("task", "auto-promote", d, "--off", "--actor", "hub")
        self.assertIs(self.refs(d)["auto_promote"], False)
        self.finish(b)
        self.assertEqual(self.status(d), "open")

    def test_clear_removes_the_key_rather_than_denying(self):
        """Cleared inherits the project's default; off overrides it."""
        p = self.ok("project", "add", "a flagged project", "--kind", "workstream",
                    "--meta", '{"auto_promote": true}').stdout.strip()
        b = self.blocker()
        d = self.dependent(b, flag=False, project=p)
        self.ok("task", "auto-promote", d, "--clear", "--actor", "hub")
        self.assertNotIn("auto_promote", self.refs(d))
        self.finish(b)
        self.assertEqual(self.status(d), "ready")

    def test_it_preserves_the_other_refs_keys(self):
        d = self.ok("task", "add", "a task", "--kind", "generic",
                    "--refs", '{"pr": "acme/gizmo#1"}').stdout.strip()
        self.ok("task", "auto-promote", d, "--on", "--actor", "hub")
        self.assertEqual(self.refs(d), {"pr": "acme/gizmo#1", "auto_promote": True})

    def test_it_records_the_change(self):
        d = self.ok("task", "add", "a task", "--kind", "generic").stdout.strip()
        self.ok("task", "auto-promote", d, "--on", "--actor", "hub")
        summaries = [e["summary"] for e in self.events(d)]
        self.assertIn('auto_promote "unset" -> true', summaries)

    def test_it_refuses_an_ambiguous_request(self):
        d = self.ok("task", "add", "a task", "--kind", "generic").stdout.strip()
        for args in (("--on", "--off"), ()):
            with self.subTest(args=args):
                result = self.cli("task", "auto-promote", d, *args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("exactly one of", result.stderr)

    def test_it_refuses_an_unknown_task(self):
        result = self.cli("task", "auto-promote", "t-99", "--on")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no task matches", result.stderr)


class TestPromotionAttribution(EstateCase):
    """P-19 / S37 — a promotion is the Memory subsystem's own act.

    Promotion is the transition that puts a fact into shared recall, where
    every loop then reads it. It inherited the CLI's default actor, usually the
    generic `cli`, so the single most consequential memory transition was the
    one you could not attribute. It now always records `memory`.

    The restart half matters as much as the attribution: a promotion that were
    somehow held in memory rather than committed would read correctly inside
    the run that made it and be gone from the next, and shared recall is what
    other loops act on.
    """

    def promotion_event(self, key) -> dict:
        events = [e for e in self.memory_events(key) if e["kind"] == "transition"]
        self.assertEqual(len(events), 1, "expected exactly one promotion event")
        return events[0]

    def candidate(self, body="a candidate lesson"):
        return self.remember(body, scope="mechanic",
                             refs='{"ledger": [7]}')

    def test_promotion_is_attributed_to_memory_not_the_default_cli_actor(self):
        key = self.candidate()
        self.ok("memory", "promote", key, "--scope", "shared")
        self.assertEqual(self.promotion_event(key)["actor"], "memory")

    def test_an_explicit_actor_cannot_override_it(self):
        key = self.candidate()
        result = self.ok("memory", "promote", key, "--scope", "shared",
                         "--actor", "mechanic")
        self.assertEqual(self.promotion_event(key)["actor"], "memory")
        self.assertIn("does not change that", result.stderr)

    def test_the_environment_default_cannot_override_it_either(self):
        key = self.candidate()
        env = dict(os.environ, ESTATE_STATE_DIR=self.state,
                   ESTATE_ACTOR="morning-brief")
        result = subprocess.run(
            [sys.executable, SCRIPT, "memory", "promote", key, "--scope", "shared"],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.promotion_event(key)["actor"], "memory")

    def test_only_promotion_is_reattributed(self):
        """Adding and retiring a memory ARE somebody's judgment call."""
        key = self.remember("a lesson", scope="mechanic", refs='{"ledger": [7]}')
        self.ok("memory", "archive", key, "not useful", "--actor", "mechanic")
        kinds = {e["kind"]: e["actor"] for e in self.memory_events(key)}
        self.assertEqual(kinds["activity"], "cli")      # `memory add`, no --actor
        self.assertEqual(kinds["transition"], "mechanic")

    def test_the_event_carries_the_whole_promotion_record(self):
        key = self.candidate()
        self.ok("memory", "promote", key, "--scope", "shared",
                "--source-refs", '{"ledger": [7, 9]}')
        row = self.promotion_event(key)
        self.assertEqual(row["memory_id"], key)
        # t-389: BOTH scopes, in the summary and again as a field. The
        # provenance the caller cited survives beside the scope pair.
        self.assertEqual(row["summary"],
                         "candidate -> active (scope: mechanic -> shared)")
        self.assertEqual(json.loads(row["refs"]),
                         {"ledger": [7, 9],
                          "scope_change": {"from": "mechanic", "to": "shared"}})
        self.assertTrue(row["ts"], "a promotion event needs its timestamp")

    def test_after_restart_only_the_promoted_fact_is_in_shared_recall(self):
        promoted = self.candidate("this one was promoted")
        self.remember("this one stayed a candidate", scope="mechanic",
                      refs='{"ledger": [8]}')
        self.remember("this one is another context's", scope="morning-brief",
                      refs='{"ledger": [9]}', active=True)
        self.ok("memory", "promote", promoted, "--scope", "shared")

        # Every command below is its own process against the reopened store.
        listed = self.ok("memory", "list", "--scope", "shared").stdout
        self.assertIn(promoted, listed)
        self.assertIn("this one was promoted", listed)
        self.assertNotIn("this one stayed a candidate", listed)
        self.assertNotIn("this one is another context's", listed)

        rendered = self.ok("memory", "render").stdout
        self.assertIn("this one was promoted", rendered)
        self.assertNotIn("this one stayed a candidate", rendered)

        shared_ids = [row["id"] for row in json.loads(self.ok("json").stdout)["memories"]
                      if row["scope"] == "shared" and row["status"] == "active"]
        self.assertEqual(shared_ids, [promoted])

    def test_the_promotion_event_survives_the_reopen_intact(self):
        key = self.candidate()
        self.ok("memory", "promote", key, "--scope", "shared")
        before = self.promotion_event(key)
        # A fresh CLI process, reading the store back off disk.
        after = [row for row in json.loads(self.ok("json").stdout)["events"]
                 if row["memory_id"] == key and row["kind"] == "transition"]
        self.assertEqual(len(after), 1)
        for field in ("actor", "memory_id", "summary", "ts", "refs"):
            self.assertEqual(after[0][field], before[field], field)
        self.assertEqual(after[0]["actor"], "memory")

    def test_a_historical_cli_promotion_is_left_exactly_as_it_is(self):
        """No migration: the events table is append-only and folds on read."""
        key = self.candidate()
        with self.conn() as c:
            c.execute("""INSERT INTO events (ts, actor, memory_id, kind, summary)
                         VALUES ('2026-07-26T00:00:00+00:00', 'cli', ?,
                                 'transition', 'candidate -> active (scope=shared)')""",
                      (key,))
            c.commit()
        self.ok("memory", "log", key)
        with self.conn() as c:
            rows = [r["actor"] for r in c.execute(
                "SELECT actor FROM events WHERE memory_id=? AND kind='transition'",
                (key,))]
        self.assertEqual(rows, ["cli"], "a historical row was rewritten")

    def test_memory_classifies_as_a_known_actor_not_as_other(self):
        sys.path.insert(0, os.path.join(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))), "lib"))
        import actors
        self.assertEqual(actors.normalize("memory").actor_class, "tool")


class TestPromotingAnActiveLocalFact(EstateCase):
    """t-389 — the deadlock, and the widening that resolves it.

    Recall returns only `active`. Promotion used to accept only `candidate`.
    So the rows that satisfied "a local fact recallable in its own context"
    were exactly the rows promotion refused, and no promotion had ever
    occurred on the live store. These tests are about the transition that was
    unreachable: a fact that is ALREADY active and local becoming estate-wide.
    """

    def local(self, body="a fact the hub learned", scope="hub"):
        return self.remember(body, scope=scope, refs='{"ledger": [11]}',
                             active=True)

    def transitions(self, key):
        return [e for e in self.memory_events(key) if e["kind"] == "transition"]

    def test_an_active_local_fact_is_recallable_before_it_is_promoted(self):
        """The half of the deadlock that was never in doubt."""
        key = self.local()
        self.assertIn(key, self.ok("memory", "recall", "--scope", "hub").stdout)
        self.assertNotIn(key, self.ok("memory", "recall",
                                      "--scope", "mechanic").stdout)

    def test_one_operation_makes_it_estate_wide(self):
        key = self.local()
        result = self.ok("memory", "promote", key, "--scope", "shared")
        self.assertIn("local -> shared", result.stdout)
        self.assertEqual(self.memory(key)["scope"], "shared")
        self.assertEqual(self.memory(key)["status"], "active")

    def test_and_then_every_other_scope_recalls_it(self):
        """The whole point: promotion changed who can see the fact."""
        key = self.local()
        self.ok("memory", "promote", key, "--scope", "shared")
        for scope in ("mechanic", "night-shift", "morning-brief"):
            self.assertIn(key, self.ok("memory", "recall",
                                       "--scope", scope).stdout, scope)

    def test_the_event_names_the_memory_subsystem_and_both_scopes(self):
        key = self.local()
        self.ok("memory", "promote", key, "--scope", "shared")
        rows = self.transitions(key)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["actor"], "memory")
        self.assertEqual(row["subsystem"], "memory")
        self.assertEqual(row["phase"], "transition")
        self.assertEqual(row["summary"], "local -> shared (scope: hub -> shared)")
        self.assertEqual(json.loads(row["refs"])["scope_change"],
                         {"from": "hub", "to": "shared"})

    def test_the_provenance_it_was_filed_with_survives_the_promotion(self):
        key = self.local()
        self.ok("memory", "promote", key, "--scope", "shared")
        refs = json.loads(self.transitions(key)[0]["refs"])
        self.assertEqual(refs["ledger"], [11])

    def test_a_local_fact_with_no_provenance_cannot_go_shared(self):
        """§5.3 still governs: promotion is not a way around --source-refs."""
        key = self.remember("no citation", scope="hub", active=False)
        result = self.cli("memory", "promote", key, "--scope", "shared")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--source-refs", result.stderr)
        self.assertEqual(self.memory(key)["scope"], "hub")

    def test_an_active_fact_is_not_promoted_without_asking_for_a_scope(self):
        key = self.local()
        result = self.cli("memory", "promote", key)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--scope shared", result.stderr)
        self.assertEqual(self.memory(key)["scope"], "hub")
        self.assertEqual(self.transitions(key), [])

    def test_promotion_never_moves_a_fact_between_loops(self):
        """A lateral scope change reads like a promotion and is not one."""
        key = self.local()
        result = self.cli("memory", "promote", key, "--scope", "mechanic")
        self.assertEqual(result.returncode, 2)
        self.assertIn("supersede", result.stderr)
        self.assertEqual(self.memory(key)["scope"], "hub")

    def test_promotion_never_narrows_an_estate_wide_fact(self):
        key = self.shared("already everyone's")
        result = self.cli("memory", "promote", key, "--scope", "hub")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not a promotion", result.stderr)
        self.assertEqual(self.memory(key)["scope"], "shared")

    def test_an_already_shared_fact_has_nowhere_further_to_go(self):
        key = self.shared()
        for args in (("memory", "promote", key),
                     ("memory", "promote", key, "--scope", "shared")):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn("already", result.stderr)

    def test_a_retired_fact_is_sent_to_reinstate_not_promoted(self):
        key = self.local()
        self.ok("memory", "archive", key)
        result = self.cli("memory", "promote", key, "--scope", "shared")
        self.assertEqual(result.returncode, 2)
        self.assertIn("reinstate", result.stderr)

    def test_a_candidate_still_promotes_and_still_names_both_scopes(self):
        """The original transition, unbroken, and now saying where it came from."""
        key = self.remember("unjudged", scope="hub", refs='{"ledger": [12]}')
        self.ok("memory", "promote", key)
        self.assertEqual(self.memory(key)["status"], "active")
        self.assertEqual(self.transitions(key)[0]["summary"],
                         "candidate -> active (scope: hub -> hub)")


class TestCrossStoreReconciliation(EstateCase):
    """t-389 — the same fact held at two scopes, across two stores.

    The estate store enforces scope. The session auto-memory store has exactly
    one scope — `shared`, because every file in it reaches every session in the
    repo — and no field that could say otherwise. So a fact the estate keeps at
    `hub` and the session store also holds is held at two scopes at once, and
    that is what this read names.

    Every test points --memory-dir at a tempdir. Nothing here reads or writes
    the real ~/.claude/projects/.../memory store.
    """

    def setUp(self):
        super().setUp()
        self.notes = os.path.join(self.state, "session-memory")
        os.makedirs(self.notes)

    def note(self, name, body, declares=None):
        head = ["---", f"name: {name}", "metadata:", "  type: feedback"]
        if declares:
            head.append(f"  estate: {declares}")
        head.append("---")
        path = os.path.join(self.notes, f"{name}.md")
        with open(path, "w") as handle:
            handle.write("\n".join(head) + "\n\n" + body + "\n")
        return f"{name}.md"

    def reconcile(self, *extra):
        result = self.ok("memory", "reconcile", "--memory-dir", self.notes,
                         "--json", *extra)
        return json.loads(result.stdout)

    def test_a_local_fact_copied_into_the_session_store_is_divergent(self):
        key = self.remember("the hub learned this", scope="hub",
                            refs='{"ledger": [1]}', active=True)
        name = self.note("hub-thing", "the hub learned this", declares=key)
        report = self.reconcile()
        self.assertEqual(report["counts"]["divergent"], 1)
        [finding] = report["findings"]
        self.assertEqual(finding["memory"], key)
        self.assertEqual(finding["note"], name)
        self.assertEqual(finding["memory_scope"], "hub")
        self.assertEqual(finding["session_scope"], "shared")

    def test_an_estate_wide_fact_copied_there_is_aligned_not_divergent(self):
        key = self.shared("everybody's fact")
        self.note("shared-thing", "everybody's fact", declares=key)
        report = self.reconcile()
        self.assertEqual(report["counts"]["divergent"], 0)
        self.assertEqual(report["counts"]["aligned"], 1)

    def test_one_file_may_declare_several_memories(self):
        """The real store has a running log that absorbed three facts."""
        a = self.remember("first", scope="hub", refs='{"l": [1]}', active=True)
        b = self.remember("second", scope="hub", refs='{"l": [2]}', active=True)
        self.note("log", "first and second", declares=f"{a}, {b}")
        report = self.reconcile()
        self.assertEqual(report["counts"]["divergent"], 2)
        self.assertEqual({f["memory"] for f in report["findings"]}, {a, b})

    def test_a_declared_id_the_store_does_not_have_is_dangling(self):
        self.note("stale", "a fact whose row went away", declares="m-999")
        report = self.reconcile()
        self.assertEqual(report["counts"]["dangling"], 1)
        self.assertEqual(report["findings"][0]["verdict"], "dangling")

    def test_a_candidate_copied_into_the_session_store_still_diverges(self):
        """The sharpest case: unrecallable in the estate, loaded by everyone."""
        key = self.remember("nobody judged this", scope="hub")
        self.note("unjudged", "nobody judged this", declares=key)
        report = self.reconcile()
        self.assertEqual(report["counts"]["divergent"], 1)
        self.assertEqual(report["findings"][0]["memory_status"], "candidate")

    def test_an_undeclared_file_is_counted_unlinked_and_never_scored_clean(self):
        """docs/absence-contract.md, applied to a second store."""
        self.remember("the hub learned this", scope="hub",
                      refs='{"ledger": [1]}', active=True)
        self.note("hub-thing", "the hub learned this")
        report = self.reconcile()
        self.assertEqual(report["counts"]["divergent"], 0)
        self.assertEqual(report["counts"]["unlinked"], 1)
        self.assertEqual(report["unlinked"], ["hub-thing.md"])

    def test_the_text_output_says_out_loud_what_it_did_not_check(self):
        self.note("hub-thing", "some fact nobody linked")
        out = self.ok("memory", "reconcile", "--memory-dir", self.notes).stdout
        self.assertIn("1 of 1 fact files declare no estate counterpart", out)
        self.assertIn("not evidence they hold no duplicate", out)

    def test_a_close_undeclared_pair_is_suspected_and_never_a_finding(self):
        body = ("bin/hub-intake record --cursor writes the watermark path "
                "and silently no-ops when the flag is omitted entirely")
        self.remember(body, scope="hub", refs='{"ledger": [1]}', active=True)
        self.note("cursor-note", body)
        report = self.reconcile()
        self.assertEqual(report["findings"], [])
        self.assertEqual(len(report["suspected"]), 1)
        self.assertEqual(report["suspected"][0]["would_be"], "divergent")
        self.assertGreater(report["suspected"][0]["score"], 0.4)

    def test_declaring_a_pair_moves_it_out_of_the_worklist(self):
        """The worklist converges as links get declared, rather than re-guessing."""
        body = "a distinctly worded fact about worktree destination paths"
        key = self.remember(body, scope="hub", refs='{"ledger": [1]}',
                            active=True)
        self.note("wt", body)
        self.assertEqual(len(self.reconcile()["suspected"]), 1)
        self.note("wt", body, declares=key)
        after = self.reconcile()
        self.assertEqual(after["suspected"], [])
        self.assertEqual(after["counts"]["divergent"], 1)

    def test_unrelated_text_is_not_suspected(self):
        self.remember("the spotter's heartbeat file went stale", scope="hub",
                      refs='{"ledger": [1]}', active=True)
        self.note("elsewhere", "how to publish a nix flake output to a registry")
        self.assertEqual(self.reconcile()["suspected"], [])

    def test_the_index_file_is_not_a_fact(self):
        with open(os.path.join(self.notes, "MEMORY.md"), "w") as handle:
            handle.write("- [a](a.md) — a summary line\n")
        self.assertEqual(self.reconcile()["counts"]["notes"], 0)

    def test_a_store_that_cannot_be_read_is_not_a_store_that_is_empty(self):
        result = self.cli("memory", "reconcile", "--memory-dir",
                          os.path.join(self.state, "nowhere"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("not the same as it holding nothing", result.stderr)

    def test_the_read_writes_to_neither_store(self):
        key = self.remember("the hub learned this", scope="hub",
                            refs='{"ledger": [1]}', active=True)
        name = self.note("hub-thing", "the hub learned this", declares=key)
        path = os.path.join(self.notes, name)
        before_row = self.memory(key)
        before_events = len(self.memory_events(key))
        with open(path) as handle:
            before_note = handle.read()
        self.reconcile()
        self.assertEqual(self.memory(key), before_row)
        self.assertEqual(len(self.memory_events(key)), before_events)
        with open(path) as handle:
            self.assertEqual(handle.read(), before_note)

    def test_promoting_a_divergence_resolves_it(self):
        """The two halves of t-389, working as one: detect, then widen."""
        key = self.remember("the hub learned this", scope="hub",
                            refs='{"ledger": [1]}', active=True)
        self.note("hub-thing", "the hub learned this", declares=key)
        self.assertEqual(self.reconcile()["counts"]["divergent"], 1)
        self.ok("memory", "promote", key, "--scope", "shared")
        after = self.reconcile()["counts"]
        self.assertEqual(after["divergent"], 0)
        self.assertEqual(after["aligned"], 1)


class ProposalCase(EstateCase):
    """Gap audit P-02 — the proposal review lifecycle.

    The point of these is not that a stage can be set. It is that an
    undecided proposal is UNREACHABLE: `ready` omits it, `claim` refuses it,
    and the night shift's own enqueue path (tested in test_review.py) fails on
    it. Any one of those alone is a suggestion; together they are the gate.
    """

    def propose(self, title="a condition worth fixing", *extra):
        return self.ok("proposal", "add", title, "--actor", "mechanic",
                       *extra).stdout.strip()

    def stages(self):
        with self.conn() as c:
            return {r["id"]: r["stage"]
                    for r in c.execute("SELECT id, stage FROM tasks")}


class TestProposalFingerprint(ProposalCase):
    def test_normalizes_case_and_punctuation(self):
        a = self.ok("proposal", "fingerprint", "Fix the  Thing!").stdout.strip()
        b = self.ok("proposal", "fingerprint", "fix   the thing").stdout.strip()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_different_conditions_differ(self):
        a = self.ok("proposal", "fingerprint", "the dispatch store loses rows").stdout
        b = self.ok("proposal", "fingerprint", "the ledger loses rows").stdout
        self.assertNotEqual(a, b)

    def test_empty_text_is_refused(self):
        self.assertEqual(self.cli("proposal", "fingerprint", "   ").returncode, 2)

    def test_fingerprint_is_stable_across_runs(self):
        # A fingerprint that moved between releases would make every existing
        # proposal look new exactly once, which is the failure it prevents.
        self.assertEqual(
            self.ok("proposal", "fingerprint", "bin/dispatches drops rows").stdout.strip(),
            self.ok("proposal", "fingerprint", "bin/dispatches drops rows").stdout.strip())


class TestProposalFiling(ProposalCase):
    def test_new_proposal_lands_pending_review(self):
        key = self.propose()
        row = self.task(key)
        self.assertEqual(row["stage"], "pending-review")
        self.assertEqual(row["kind"], "proposal")
        self.assertEqual(row["status"], "ready")
        self.assertIn("fingerprint", json.loads(row["refs"]))

    def test_equivalent_condition_is_a_recurrence_not_a_second_row(self):
        first = self.propose("the dispatch store loses rows")
        result = self.ok("proposal", "add", "The Dispatch Store Loses Rows!",
                         "--actor", "mechanic")
        self.assertEqual(result.stdout.strip(), first)
        self.assertIn("recurrence", result.stderr)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        summaries = [e["summary"] for e in self.events(first)]
        self.assertIn("condition observed again", summaries)

    def test_recurrence_of_a_resolved_proposal_says_resolved(self):
        # S10: a previously resolved condition must not be presented as new.
        key = self.propose("the ledger rotates without warning")
        self.ok("proposal", "stage", key, "resolved", "--actor", "hub")
        result = self.ok("proposal", "add", "the ledger rotates without warning",
                         "--actor", "mechanic")
        self.assertEqual(result.stdout.strip(), key)
        self.assertIn("resolved", result.stderr)

    def test_condition_overrides_the_title_for_the_fingerprint(self):
        first = self.propose("2026-07-29: dispatches lose rows",
                             "--condition", "dispatches lose rows")
        again = self.ok("proposal", "add", "2026-07-30: dispatches lose rows",
                        "--condition", "dispatches lose rows", "--actor", "mechanic")
        self.assertEqual(again.stdout.strip(), first)

    def test_pinned_fingerprint_is_used_verbatim(self):
        self.propose("some wording", "--fingerprint", "deadbeef1234")
        self.assertEqual(json.loads(self.task("t-1")["refs"])["fingerprint"],
                         "deadbeef1234")

    def test_classify_distinguishes_new_pending_and_resolved(self):
        pending = self.propose("condition A")
        resolved = self.propose("condition B")
        self.ok("proposal", "stage", resolved, "resolved", "--actor", "hub")
        self.assertEqual(
            self.ok("proposal", "classify", "--condition", "condition A").stdout.strip(),
            f"pending-review {pending}")
        self.assertEqual(
            self.ok("proposal", "classify", "--condition", "condition B").stdout.strip(),
            f"resolved {resolved}")
        self.assertEqual(
            self.ok("proposal", "classify", "--condition", "condition C").stdout.strip(),
            "new")

    def test_classify_needs_something_to_classify(self):
        self.assertEqual(self.cli("proposal", "classify").returncode, 2)


class TestProposalAdoption(ProposalCase):
    """P-01 holds: a proposal that is already a task does not become a second
    one. It is adopted in place and keeps its own kind and history."""

    def test_adopts_an_existing_task_in_place(self):
        self.add("route rework state through the shared store")
        out = self.ok("proposal", "add", "route rework state through the shared store",
                      "--adopt-task", "t-1", "--stage", "approved-backlog",
                      "--actor", "hub")
        self.assertEqual(out.stdout.strip(), "t-1")
        row = self.task("t-1")
        self.assertEqual(row["stage"], "approved-backlog")
        self.assertEqual(row["kind"], "generic")     # kind is NOT rewritten
        self.assertIn("fingerprint", json.loads(row["refs"]))
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_adoption_merges_refs_rather_than_replacing_them(self):
        self.ok("task", "add", "a thing", "--kind", "followup",
                "--refs", json.dumps({"legacy_id": "followup:9"}))
        self.ok("proposal", "add", "a thing", "--adopt-task", "t-1",
                "--refs", json.dumps({"proposal": "P-3"}), "--actor", "hub")
        refs = json.loads(self.task("t-1")["refs"])
        self.assertEqual(refs["legacy_id"], "followup:9")
        self.assertEqual(refs["proposal"], "P-3")
        self.assertIn("fingerprint", refs)

    def test_adoption_records_a_stage_transition_event(self):
        self.add("a thing")
        self.ok("proposal", "add", "a thing", "--adopt-task", "t-1",
                "--stage", "resolved", "--actor", "hub")
        self.assertIn("stage: none -> resolved",
                      [e["summary"] for e in self.events("t-1")])

    def test_adopting_a_missing_task_fails(self):
        self.assertEqual(
            self.cli("proposal", "add", "x", "--adopt-task", "t-99").returncode, 2)

    def test_a_task_cannot_hold_two_fingerprints(self):
        self.add("a thing")
        self.ok("proposal", "add", "a thing", "--adopt-task", "t-1", "--actor", "hub")
        result = self.cli("proposal", "add", "a different condition",
                          "--adopt-task", "t-1", "--actor", "hub")
        self.assertEqual(result.returncode, 2)
        self.assertIn("one task is one proposal", result.stderr)

    def test_readopting_the_same_fingerprint_is_a_no_op_update(self):
        self.add("a thing")
        self.ok("proposal", "add", "a thing", "--adopt-task", "t-1", "--actor", "hub")
        self.ok("proposal", "add", "a thing", "--adopt-task", "t-1",
                "--stage", "approved-backlog", "--actor", "hub")
        self.assertEqual(self.task("t-1")["stage"], "approved-backlog")


class TestProposalStages(ProposalCase):
    def test_records_each_decision_as_a_transition(self):
        key = self.propose()
        self.ok("proposal", "stage", key, "approved-backlog", "the owner said yes",
                "--actor", "hub")
        events = self.events(key)
        transition = [e for e in events if e["kind"] == "transition"][-1]
        self.assertEqual(transition["summary"],
                         "stage: pending-review -> approved-backlog")
        self.assertEqual(transition["detail"], "the owner said yes")

    def test_rejected_and_approved_outcomes_are_both_retained(self):
        # S32: each review outcome is retained; only the approved one becomes
        # consumable.
        yes, no = self.propose("condition A"), self.propose("condition B")
        self.ok("proposal", "stage", yes, "approved-backlog", "--actor", "hub")
        self.ok("proposal", "stage", no, "rejected", "--actor", "hub")
        self.assertEqual(self.stages(), {yes: "approved-backlog", no: "rejected"})
        ready = self.ok("ready").stdout
        self.assertIn(yes, ready)
        self.assertNotIn(no, ready)

    def test_illegal_stage_moves_are_refused(self):
        key = self.propose()
        self.ok("proposal", "stage", key, "rejected", "--actor", "hub")
        result = self.cli("proposal", "stage", key, "resolved", "--actor", "hub")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Allowed from here", result.stderr)
        self.assertEqual(self.task(key)["stage"], "rejected")

    def test_a_rejection_can_be_reconsidered_through_pending_review(self):
        key = self.propose()
        self.ok("proposal", "stage", key, "rejected", "--actor", "hub")
        self.ok("proposal", "stage", key, "pending-review", "the owner reopened it",
                "--actor", "hub")
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.assertEqual(self.task(key)["stage"], "approved-backlog")

    def test_restaging_to_the_same_stage_is_refused(self):
        key = self.propose()
        self.assertEqual(
            self.cli("proposal", "stage", key, "pending-review", "--actor", "hub").returncode, 2)

    def test_a_task_with_no_stage_is_not_in_the_lifecycle(self):
        self.add("ordinary work")
        result = self.cli("proposal", "stage", "t-1", "approved-backlog",
                          "--actor", "hub")
        self.assertEqual(result.returncode, 2)
        self.assertIn("proposal add --adopt-task", result.stderr)


class TestProposalDecisionClosesTheTask(ProposalCase):
    """A terminal review decision closes the underlying task.

    Found live 2026-08-05: `stage` only ever wrote the `stage` column, so a
    proposal decided weeks ago still had status=`ready`. Every terminal stage
    is gated, so `claim` and `retry` both refuse it — the task could never be
    started AND never finished, and sat in `stale` looking untouched. These
    are about the two columns agreeing after a decision, in both directions.
    """

    def decided(self, stage, title=None):
        key = self.propose(title or f"a condition ending {stage}")
        self.ok("proposal", "stage", key, stage, "--actor", "hub")
        return key

    def test_resolved_closes_the_task_as_done(self):
        key = self.decided("resolved")
        row = self.task(key)
        self.assertEqual(row["stage"], "resolved")
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["closed_at"])

    def test_stopped_closes_the_task_as_done(self):
        key = self.decided("stopped")
        row = self.task(key)
        self.assertEqual(row["stage"], "stopped")
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["closed_at"])

    def test_rejected_drops_the_task(self):
        key = self.decided("rejected")
        row = self.task(key)
        self.assertEqual(row["stage"], "rejected")
        self.assertEqual(row["status"], "dropped")
        self.assertTrue(row["closed_at"])

    def test_the_closure_is_a_recorded_task_transition(self):
        key = self.decided("resolved")
        kinds = [(e["summary"], e["detail"], e["subsystem"], e["phase"])
                 for e in self.events(key) if e["kind"] == "transition"]
        self.assertIn(("stage: pending-review -> resolved", None, None, None),
                      kinds)
        self.assertIn(("ready -> done",
                       "proposal stage pending-review -> resolved",
                       "tasks", "outcome"), kinds)

    def test_the_decision_note_rides_the_closure(self):
        key = self.propose()
        self.ok("proposal", "stage", key, "resolved", "landed in PR 9",
                "--actor", "hub")
        details = [e["detail"] for e in self.events(key)
                   if e["summary"] == "ready -> done"]
        self.assertEqual(
            details, ["proposal stage pending-review -> resolved: landed in PR 9"])

    def test_a_closed_proposal_is_out_of_stale(self):
        key = self.decided("resolved")
        self.assertNotIn(key, self.ok("stale", "--days", "0").stdout)

    def test_reopening_a_resolved_proposal_reopens_the_task(self):
        key = self.decided("resolved")
        self.ok("proposal", "stage", key, "pending-review", "it came back",
                "--actor", "hub")
        row = self.task(key)
        self.assertEqual(row["stage"], "pending-review")
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["closed_at"])

    def test_reopening_a_rejected_proposal_reopens_the_task(self):
        key = self.decided("rejected")
        self.ok("proposal", "stage", key, "pending-review", "--actor", "hub")
        self.assertEqual(self.task(key)["status"], "ready")

    def test_reopening_a_stopped_proposal_reopens_the_task(self):
        key = self.decided("stopped")
        self.ok("proposal", "stage", key, "pending-review", "--actor", "hub")
        self.assertEqual(self.task(key)["status"], "ready")

    def test_a_stopped_proposal_taken_straight_back_to_approved_is_claimable(self):
        """STAGE_TRANSITIONS lets `stopped` go straight to approved-backlog,
        skipping pending-review. Reopening only via pending-review would leave
        that path claimable-by-stage but `done` by status — unclaimable, which
        is the same bug wearing the other stage."""
        key = self.decided("stopped")
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.assertEqual(self.task(key)["status"], "ready")
        self.ok("claim", key, "--actor", "pr-reviewer")
        self.assertEqual(self.task(key)["claimed_by"], "pr-reviewer")

    def test_a_reopened_task_can_be_worked_and_closed_again(self):
        key = self.decided("resolved")
        self.ok("proposal", "stage", key, "pending-review", "--actor", "hub")
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.ok("claim", key, "--actor", "pr-reviewer")
        self.ok("proposal", "stage", key, "resolved", "fixed for real",
                "--actor", "hub")
        row = self.task(key)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["claimed_by"], "pr-reviewer")

    def test_the_claimable_path_is_untouched(self):
        """(e) The approved-backlog route is the one that must NOT gain a
        side effect: `ready` behind `approved-backlog` is work waiting to be
        picked up, not work nobody closed."""
        key = self.propose()
        self.assertEqual(self.task(key)["status"], "ready")
        self.ok("proposal", "stage", key, "approved-backlog", "approved",
                "--actor", "hub")
        row = self.task(key)
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["closed_at"])
        self.assertIn(key, self.ok("ready").stdout)
        self.ok("claim", key, "--actor", "pr-reviewer")
        self.assertEqual(self.task(key)["status"], "claimed")
        self.ok("proposal", "stage", key, "pending-review", "second thoughts",
                "--actor", "hub")
        # Back to undecided, and still claimed — the review moved, the work
        # did not.
        self.assertEqual(self.task(key)["status"], "claimed")

    def test_filing_a_proposal_leaves_it_ready(self):
        key = self.propose()
        row = self.task(key)
        self.assertEqual((row["stage"], row["status"]),
                         ("pending-review", "ready"))

    def test_an_adopted_task_keeps_its_own_status_until_a_decision(self):
        self.add("ordinary work", ready=True)
        self.ok("proposal", "add", "a condition", "--adopt-task", "t-1",
                "--actor", "mechanic")
        self.assertEqual(self.task("t-1")["status"], "ready")
        self.ok("proposal", "stage", "t-1", "rejected", "--actor", "hub")
        self.assertEqual(self.task("t-1")["status"], "dropped")


class TestProposalRetroactiveClose(ProposalCase):
    """The repair path for the 11 rows decided before the closure existed.

    Re-running the SAME terminal decision is a no-op for the stage and the one
    thing that can still close the task. It stays refused once there is
    nothing left to repair, so it is not a way to restage anything.
    """

    def stranded(self, stage="resolved"):
        """A proposal in the broken state: terminal stage, task still ready."""
        key = self.propose(f"a condition stranded at {stage}")
        with self.conn() as c:
            c.execute("UPDATE tasks SET stage=? WHERE id=?", (stage, key))
        return key

    def test_restaging_the_same_terminal_stage_closes_a_stranded_task(self):
        key = self.stranded("resolved")
        out = self.ok("proposal", "stage", key, "resolved",
                      "retroactive close", "--actor", "hub").stdout
        self.assertIn("already 'resolved'", out)
        row = self.task(key)
        self.assertEqual((row["stage"], row["status"]), ("resolved", "done"))
        self.assertTrue(row["closed_at"])

    def test_it_records_why_the_close_is_late(self):
        key = self.stranded("resolved")
        self.ok("proposal", "stage", key, "resolved", "bug fix t-255",
                "--actor", "hub")
        detail = [e["detail"] for e in self.events(key)
                  if e["summary"] == "ready -> done"][0]
        self.assertIn("retroactive close", detail)
        self.assertIn("bug fix t-255", detail)

    def test_a_stranded_rejection_drops_the_task(self):
        key = self.stranded("rejected")
        self.ok("proposal", "stage", key, "rejected", "retroactive",
                "--actor", "hub")
        self.assertEqual(self.task(key)["status"], "dropped")

    def test_a_stranded_stop_closes_the_task(self):
        key = self.stranded("stopped")
        self.ok("proposal", "stage", key, "stopped", "retroactive",
                "--actor", "hub")
        self.assertEqual(self.task(key)["status"], "done")

    def test_it_refuses_once_there_is_nothing_to_repair(self):
        key = self.stranded("resolved")
        self.ok("proposal", "stage", key, "resolved", "--actor", "hub")
        result = self.cli("proposal", "stage", key, "resolved", "--actor", "hub")
        self.assertEqual(result.returncode, 2)
        self.assertIn("already 'resolved'", result.stderr)

    def test_a_decision_made_normally_refuses_a_second_time(self):
        key = self.propose()
        self.ok("proposal", "stage", key, "resolved", "--actor", "hub")
        self.assertEqual(
            self.cli("proposal", "stage", key, "resolved", "--actor", "hub").returncode, 2)

    def test_it_does_not_re_close_a_task_that_closed_differently(self):
        """A dropped task whose proposal reads `resolved` keeps the outcome it
        actually had. Repairing a missing close is not licence to rewrite one
        that happened."""
        key = self.propose()
        self.ok("drop", key, "not doing this", "--actor", "hub")
        with self.conn() as c:
            c.execute("UPDATE tasks SET stage='resolved' WHERE id=?", (key,))
        result = self.cli("proposal", "stage", key, "resolved", "--actor", "hub")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.task(key)["status"], "dropped")

    def test_the_non_terminal_stages_still_refuse_a_restage(self):
        key = self.propose()
        for stage in ("pending-review", "approved-backlog"):
            if stage != "pending-review":
                self.ok("proposal", "stage", key, stage, "--actor", "hub")
            result = self.cli("proposal", "stage", key, stage, "--actor", "hub")
            self.assertEqual(result.returncode, 2, f"{stage} was restageable")
            self.assertIn(f"already '{stage}'", result.stderr)


class TestProposalGate(ProposalCase):
    """S13/S49 — only approved-backlog is selectable, mechanically."""

    def test_ready_omits_every_unapproved_stage(self):
        keys = {}
        for stage in ("pending-review", "rejected", "resolved", "stopped"):
            key = self.propose(f"condition {stage}")
            if stage != "pending-review":
                self.ok("proposal", "stage", key, stage, "--actor", "hub")
            keys[stage] = key
        approved = self.propose("condition approved")
        self.ok("proposal", "stage", approved, "approved-backlog", "--actor", "hub")
        ready = self.ok("ready").stdout
        self.assertIn(approved, ready)
        for stage, key in keys.items():
            self.assertNotIn(key, ready, f"{stage} leaked into ready")

    def test_ready_still_lists_ordinary_work(self):
        self.add("ordinary work", ready=True)
        self.assertIn("t-1", self.ok("ready").stdout)

    def test_claim_refuses_an_unapproved_proposal_named_directly(self):
        key = self.propose()
        result = self.cli("claim", key, "--actor", "pr-reviewer")
        self.assertEqual(result.returncode, 2)
        self.assertIn("approved-backlog", result.stderr)
        self.assertIsNone(self.task(key)["claimed_by"])

    def test_claim_allows_an_approved_proposal(self):
        key = self.propose()
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.ok("claim", key, "--actor", "pr-reviewer")
        self.assertEqual(self.task(key)["claimed_by"], "pr-reviewer")

    def test_retry_is_gated_exactly_as_claim_is(self):
        """P-12 added a second way to start work, so the review gate had to
        cover it too — otherwise `retry` is the hole `claim` refuses to be."""
        key = self.propose()
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.ok("claim", key, "--actor", "pr-reviewer")
        self.ok("release", key, "handing it back", "--actor", "pr-reviewer")
        self.ok("proposal", "stage", key, "rejected", "--actor", "hub")
        result = self.cli("retry", key, "--actor", "pr-reviewer")
        self.assertEqual(result.returncode, 2)
        self.assertIn("approved-backlog", result.stderr)
        self.assertIsNone(self.task(key)["claimed_by"])

    def test_doctor_flags_a_claimed_unapproved_proposal(self):
        # Written straight into the table, which is the only way this state
        # arises now and exactly what the check says it is for. Rejecting a
        # claimed proposal through the CLI no longer leaves one: the decision
        # drops the task in the same transaction, so it stops being claimed.
        key = self.propose()
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.ok("claim", key, "--actor", "pr-reviewer")
        with self.conn() as c:
            c.execute("UPDATE tasks SET stage='rejected' WHERE id=?", (key,))
        self.assertEqual(self.task(key)["status"], "claimed")
        out = self.cli("doctor").stdout
        self.assertIn("FAIL no unapproved proposal is claimed", out)

    def test_rejecting_a_claimed_proposal_stops_it_being_claimed(self):
        """The sibling of the check above: the decision resolves the anomaly
        instead of creating it. The claimant is retained on the closed row —
        who held it when it was called off is part of the record."""
        key = self.propose()
        self.ok("proposal", "stage", key, "approved-backlog", "--actor", "hub")
        self.ok("claim", key, "--actor", "pr-reviewer")
        self.ok("proposal", "stage", key, "rejected", "--actor", "hub")
        row = self.task(key)
        self.assertEqual(row["status"], "dropped")
        self.assertEqual(row["claimed_by"], "pr-reviewer")
        self.assertIn("ok   no unapproved proposal is claimed",
                      self.cli("doctor").stdout)

    def test_doctor_flags_a_stage_outside_the_vocabulary(self):
        self.ok("task", "add", "a thing", "--kind", "generic", "--stage", "invented")
        self.assertIn("FAIL proposal stages are in the vocabulary",
                      self.cli("doctor").stdout)


class TestProposalViews(ProposalCase):
    def test_list_shows_live_stages_by_default(self):
        live = self.propose("condition A")
        closed = self.propose("condition B")
        self.ok("proposal", "stage", closed, "rejected", "--actor", "hub")
        out = self.ok("proposal", "list").stdout
        self.assertIn(live, out)
        self.assertNotIn(closed, out)
        self.assertIn(closed, self.ok("proposal", "list", "--all").stdout)

    def test_list_filters_by_stage(self):
        a, b = self.propose("condition A"), self.propose("condition B")
        self.ok("proposal", "stage", b, "approved-backlog", "--actor", "hub")
        out = self.ok("proposal", "list", "--stage", "approved-backlog").stdout
        self.assertIn(b, out)
        self.assertNotIn(a, out)

    def test_task_line_names_the_stage(self):
        key = self.propose("a condition")
        self.assertIn("(pending-review)", self.ok("task", "list").stdout)
        self.assertIn(key, self.ok("task", "list", "--stage", "pending-review").stdout)

    def test_ordinary_tasks_print_no_stage(self):
        self.add("ordinary work")
        self.assertNotIn("(", self.ok("task", "list").stdout.split("generic")[1])


class TestProposalReviewRecord(ProposalCase):
    """Gap audit P-09 — a proposal the owner can act on says three things.

    The fingerprint answers "have we seen this before". It cannot answer
    "what was seen", "what should be true instead", or "how would anyone
    tell". Those are the reviewable record, and before P-09 the store kept
    none of them: the condition text was hashed and dropped.
    """
    FULL = ("--condition", "dispatches.json loses rows between ticks",
            "--desired-outcome", "every entry survives until it is resolved",
            "--completion-check", "three consecutive clean nights")

    def test_the_three_fields_are_kept_and_shown(self):
        key = self.propose("the dispatch store loses rows", *self.FULL)
        out = self.ok("proposal", "show", key).stdout
        self.assertIn("dispatches.json loses rows between ticks", out)
        self.assertIn("every entry survives until it is resolved", out)
        self.assertIn("three consecutive clean nights", out)
        refs = json.loads(self.task(key)["refs"])
        self.assertEqual(refs["condition"],
                         "dispatches.json loses rows between ticks")

    def test_the_condition_text_is_kept_even_without_the_other_two(self):
        key = self.propose("a thing", "--condition", "the observed thing")
        self.assertIn("the observed thing", self.ok("proposal", "show", key).stdout)

    def test_a_legacy_record_reads_as_legacy_not_as_complete(self):
        # 26 proposals were imported from reports written before these fields
        # existed. Printing a blank where the completion check belongs would
        # let an unreviewable record pass for a reviewed one.
        key = self.propose("an old condition")
        out = self.ok("proposal", "show", key).stdout
        self.assertIn("desired outcome   (not recorded)", out)
        self.assertIn("completion check  (not recorded)", out)

    def test_the_store_accepts_a_proposal_without_them(self):
        # Required at the mechanic, optional here: `--adopt-task` and
        # bin/migrate-mechanic-proposals both handle records that never had
        # this text, and inventing it would be fabrication.
        self.assertEqual(self.cli("proposal", "add", "bare", "--actor",
                                  "mechanic").returncode, 0)

    def test_a_recurrence_fills_a_blank_but_never_overwrites(self):
        key = self.propose("the ledger rotates without warning")
        self.ok("proposal", "add", "the ledger rotates without warning",
                "--actor", "mechanic", "--desired-outcome", "rotation is announced",
                "--completion-check", "the next rotation posts a line")
        out = self.ok("proposal", "show", key).stdout
        self.assertIn("rotation is announced", out)
        self.ok("proposal", "add", "the ledger rotates without warning",
                "--actor", "mechanic", "--desired-outcome", "SOMETHING ELSE")
        self.assertIn("rotation is announced",
                      self.ok("proposal", "show", key).stdout)
        self.assertNotIn("SOMETHING ELSE",
                         self.ok("proposal", "show", key).stdout)

    def test_filling_a_blank_is_recorded_not_silent(self):
        key = self.propose("a condition with no check")
        self.ok("proposal", "add", "a condition with no check", "--actor",
                "mechanic", "--completion-check", "a week without it")
        summaries = [e["summary"] for e in self.events(key)]
        self.assertTrue(any("review record filled in" in s for s in summaries),
                        summaries)

    def test_show_counts_recurrences(self):
        key = self.propose("a recurring condition")
        for _ in range(2):
            self.ok("proposal", "add", "a recurring condition", "--actor", "mechanic")
        self.assertIn("recurrences       2", self.ok("proposal", "show", key).stdout)

    def test_show_refuses_a_task_outside_the_lifecycle(self):
        key = self.add("ordinary work").stdout.strip()
        r = self.cli("proposal", "show", key)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not in the proposal review lifecycle", r.stderr)
        self.assertEqual(self.cli("proposal", "show", "t-999").returncode, 2)


class TestPinnedRecurrence(ProposalCase):
    """t-310 — recurrence identity can be ASSERTED, not only derived.

    A fingerprint over condition prose recognizes wording. A condition worth
    watching carries a number that moves, so the second night's write-up of
    one unresolved condition hashed differently and filed as a discovery
    (t-250, then t-272 — the same extraction:5 drift). `--recurrence-of` names
    the proposal instead of hoping the words match.
    """

    NIGHT_ONE = ("nano-ops's mechanic extraction (extraction:5) has drifted: "
                 "four ported files changed since the 2026-07-29 sync and were "
                 "never re-synced (a1b2c3d)")
    NIGHT_TWO = ("nano-ops's mechanic extraction (extraction:5) has drifted: "
                 "six ported files changed since the 2026-08-06 sync and were "
                 "never re-synced (9f8e7d6)")

    def test_the_widened_observation_lands_on_the_same_task(self):
        # c1. Two extraction:5 drift observations, different counts and
        # hashes, the second pinned. One task, and a recurrence recorded.
        first = self.propose("extraction:5 drift", "--condition", self.NIGHT_ONE)
        again = self.ok("proposal", "add", "extraction:5 drift (wider)",
                        "--condition", self.NIGHT_TWO, "--actor", "mechanic",
                        "--recurrence-of", first)
        self.assertEqual(again.stdout.strip(), first)
        self.assertIn("recurrence", again.stderr)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        self.assertIn("condition observed again",
                      [e["summary"] for e in self.events(first)])
        self.assertIn("recurrences       1", self.ok("proposal", "show", first).stdout)

    def test_without_the_pin_the_same_pair_files_twice(self):
        # The defect, held in place. If this ever stops being true the pin is
        # no longer what makes the two observations one proposal, and this
        # class is testing something else.
        self.propose("extraction:5 drift", "--condition", self.NIGHT_ONE)
        self.propose("extraction:5 drift (wider)", "--condition", self.NIGHT_TWO)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_the_row_says_the_identity_was_asserted(self):
        first = self.propose("a drifting condition", "--condition", self.NIGHT_ONE)
        self.ok("proposal", "add", "wider", "--condition", self.NIGHT_TWO,
                "--actor", "mechanic", "--recurrence-of", first)
        refs = json.loads(self.events(first)[-1]["refs"])
        self.assertEqual(refs["recurrence"], "pinned")
        # What tonight's wording hashed to, kept because it is the evidence
        # that the pin did any work at all.
        self.assertEqual(refs["observed_fingerprint"],
                         self.ok("proposal", "fingerprint", self.NIGHT_TWO).stdout.strip())

    def test_a_derived_recurrence_still_says_derived(self):
        first = self.propose("the dispatch store loses rows")
        self.ok("proposal", "add", "the dispatch store loses rows",
                "--actor", "mechanic")
        refs = json.loads(self.events(first)[-1]["refs"])
        self.assertEqual(refs["recurrence"], "derived")
        self.assertNotIn("observed_fingerprint", refs)

    def test_a_pin_fills_a_blank_the_same_way_a_hash_match_does(self):
        first = self.propose("extraction drift", "--condition", self.NIGHT_ONE)
        self.ok("proposal", "add", "wider", "--condition", self.NIGHT_TWO,
                "--actor", "mechanic", "--recurrence-of", first,
                "--completion-check", "one sync with nothing left over")
        self.assertIn("one sync with nothing left over",
                      self.ok("proposal", "show", first).stdout)

    def test_pinning_a_missing_task_is_refused(self):
        r = self.cli("proposal", "add", "x", "--recurrence-of", "t-99")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no task matches 't-99'", r.stderr)

    def test_pinning_a_task_outside_the_lifecycle_is_refused(self):
        key = self.add("ordinary work").stdout.strip()
        r = self.cli("proposal", "add", "x", "--recurrence-of", key)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not in the proposal review lifecycle", r.stderr)
        self.assertIn("--adopt-task", r.stderr)

    def test_a_refused_pin_writes_nothing(self):
        key = self.add("ordinary work").stdout.strip()
        before = len(self.events(key))
        self.cli("proposal", "add", "x", "--recurrence-of", key)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(len(self.events(key)), before)

    def test_a_pin_cannot_steal_a_condition_another_proposal_owns(self):
        mine = self.propose("condition A")
        self.propose("condition B")
        r = self.cli("proposal", "add", "condition B", "--actor", "mechanic",
                     "--recurrence-of", mine)
        self.assertEqual(r.returncode, 2)
        self.assertIn("one condition is one proposal", r.stderr)

    def test_repinning_the_identical_wording_is_still_one_recurrence(self):
        # The pin is redundant here — the hash would have found it — and a
        # redundant assertion is not a wrong one.
        first = self.propose("condition A")
        again = self.ok("proposal", "add", "condition A", "--actor", "mechanic",
                        "--recurrence-of", first)
        self.assertEqual(again.stdout.strip(), first)
        self.assertIn("recurrences       1", self.ok("proposal", "show", first).stdout)

    def test_pin_and_adopt_cannot_both_be_true(self):
        first = self.propose("condition A")
        self.add("ordinary work")
        r = self.cli("proposal", "add", "condition C", "--recurrence-of", first,
                     "--adopt-task", "t-2")
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot both be true", r.stderr)

    def test_a_pinned_fingerprint_that_disagrees_with_the_pin_is_refused(self):
        first = self.propose("condition A")
        r = self.cli("proposal", "add", "condition C", "--recurrence-of", first,
                     "--fingerprint", "deadbeef1234")
        self.assertEqual(r.returncode, 2)
        self.assertIn("two different", r.stderr)

    def test_a_resolved_proposal_pinned_again_still_reads_resolved(self):
        # S10 holds through the pin: a condition that came back after it was
        # called fixed must not read as a discovery.
        first = self.propose("extraction drift", "--condition", self.NIGHT_ONE)
        self.ok("proposal", "stage", first, "resolved", "--actor", "hub")
        r = self.ok("proposal", "add", "wider", "--condition", self.NIGHT_TWO,
                    "--actor", "mechanic", "--recurrence-of", first)
        self.assertEqual(r.stdout.strip(), first)
        self.assertIn("resolved", r.stderr)


if __name__ == "__main__": unittest.main()

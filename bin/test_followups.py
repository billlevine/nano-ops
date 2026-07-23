#!/usr/bin/env python3
"""Tests for bin/followups: the durable standing-action-item store that a
loop's queue-exit sweep files internal (non-PR) work into.
Run: python3 test_followups.py

Every test points $FOLLOWUPS_STATE_DIR at a fresh tempdir — never the real
state/followups/.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "followups")


def run_cli(args, state_dir):
    env = dict(os.environ, FOLLOWUPS_STATE_DIR=state_dir)
    return subprocess.run([sys.executable, SCRIPT, *args],
                           capture_output=True, text=True, env=env)


def store_path(state_dir):
    return os.path.join(state_dir, "followups.json")


class TestAdd(unittest.TestCase):
    def test_add_creates_item_and_prints_json(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli(["add", "provision the OIDC role", "--source", "pr-reviewer",
                         "--ref", "task:7", "--context", "needs an AWS decision"], d)
            self.assertEqual(r.returncode, 0, r.stderr)
            created = json.loads(r.stdout)
            self.assertEqual(created["id"], "followup:1")
            self.assertEqual(created["status"], "open")
            self.assertEqual(created["source"], "pr-reviewer")
            self.assertEqual(created["ref"], "task:7")

    def test_add_empty_description_errors(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli(["add", "   "], d)
            self.assertEqual(r.returncode, 2)

    def test_ids_increment(self):
        with tempfile.TemporaryDirectory() as d:
            run_cli(["add", "first"], d)
            r = run_cli(["add", "second"], d)
            created = json.loads(r.stdout)
            self.assertEqual(created["id"], "followup:2")

    def test_ledger_records_add(self):
        with tempfile.TemporaryDirectory() as d:
            run_cli(["add", "raise the org spend cap", "--source", "mechanic"], d)
            with open(os.path.join(d, "ledger.jsonl")) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["event"], "added")


class TestResolve(unittest.TestCase):
    def test_resolve_marks_status_and_keeps_item(self):
        with tempfile.TemporaryDirectory() as d:
            run_cli(["add", "do the thing"], d)
            r = run_cli(["resolve", "followup:1", "done via manual fix"], d)
            self.assertEqual(r.returncode, 0, r.stderr)
            store = json.load(open(store_path(d)))
            item = store["items"]["followup:1"]
            self.assertEqual(item["status"], "resolved")
            self.assertEqual(item["resolution"], "done via manual fix")
            self.assertIsNotNone(item["resolved_at"])

    def test_resolve_unknown_id_errors(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli(["resolve", "followup:99"], d)
            self.assertEqual(r.returncode, 2)


class TestListAndShow(unittest.TestCase):
    def test_list_shows_only_open_items(self):
        with tempfile.TemporaryDirectory() as d:
            run_cli(["add", "open one"], d)
            run_cli(["add", "will resolve"], d)
            run_cli(["resolve", "followup:2"], d)
            r = run_cli(["list"], d)
            self.assertIn("followup:1", r.stdout)
            self.assertNotIn("followup:2", r.stdout)

    def test_list_empty_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli(["list"], d)
            self.assertIn("no open follow-ups", r.stdout)

    def test_show_includes_resolved_items(self):
        with tempfile.TemporaryDirectory() as d:
            run_cli(["add", "will resolve"], d)
            run_cli(["resolve", "followup:1"], d)
            r = run_cli(["show"], d)
            self.assertIn("followup:1", r.stdout)
            self.assertIn("resolved", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

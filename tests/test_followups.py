#!/usr/bin/env python3
"""Output-parity tests for the estate-backed followups shim."""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

# Under tests/, per docs/extraction-allowlist.md: the scripts are resolved
# through the repo root rather than beside this file, so it runs from anywhere.
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(os.path.dirname(TESTS_DIR), "bin")
ORACLE = os.path.join(TESTS_DIR, "_fixtures", "followups_legacy_oracle.py")
SHIM = os.path.join(BIN_DIR, "followups")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.+\-]+")


# the contract made a filing-time classification mandatory at this writer. The frozen
# legacy implementation has no such concept, so the flags are stripped before
# the oracle sees them and supplied to the shim when a test did not name one —
# the same deliberate divergence `--due` already is. Parity is about the
# behaviour that survived the move to SQLite, not about flags that postdate it.
# Nothing about the classification reaches `add`'s stdout or `json`, which is
# why the parity assertions below still compare byte for byte.
SHIM_ONLY_FLAGS = ("--classification", "--question", "--alternative",
                   "--recommendation", "--defer-consequence")
CLASSIFIED_COMMANDS = ("add", "attention")


def legacy_argv(args):
    """`args` with the post-legacy flags and their values removed."""
    out, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in SHIM_ONLY_FLAGS:
            skip = True
        else:
            out.append(arg)
    return out


def classified(args):
    """`args` with a classification, unless the test is making a point of one.

    `action` is the honest default for a scenery item: these cases are about
    due states, attention keys and legacy numbering, and the classification
    gate has its own tests below.
    """
    args = list(args)
    if args and args[0] in CLASSIFIED_COMMANDS and "--classification" not in args:
        args += ["--classification", "action"]
    return args


def run(script, args, state_dir):
    env = dict(os.environ)
    env.pop("ESTATE_STATE_DIR", None)
    env["FOLLOWUPS_STATE_DIR"] = state_dir
    return subprocess.run([sys.executable, script, *args], capture_output=True,
                          text=True, env=env)


def normalized(text):
    return TS_RE.sub("<TS>", text)


class FollowupsParityCase(unittest.TestCase):
    def setUp(self):
        self.oracle_tmp = tempfile.TemporaryDirectory()
        self.shim_tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.oracle_tmp.cleanup()
        self.shim_tmp.cleanup()

    def pair(self, *args):
        return (run(ORACLE, legacy_argv(args), self.oracle_tmp.name),
                run(SHIM, classified(args), self.shim_tmp.name))

    def assert_pair(self, *args):
        old, new = self.pair(*args)
        self.assertEqual(new.returncode, old.returncode, new.stderr)
        self.assertEqual(normalized(new.stdout), normalized(old.stdout))
        return old, new

    def test_add_output_and_first_id(self):
        old, new = self.assert_pair("add", "File this", "--source", "reviewer",
                                    "--ref", "repo#1", "--context", "details")
        self.assertEqual(json.loads(old.stdout)["id"], "followup:1")
        self.assertEqual(json.loads(new.stdout)["id"], "followup:1")

    def test_sequence_list_and_show_are_identical(self):
        # `show` gained one line per item the contract: it is the human view, and
        # the classification is exactly what it was hiding. Everything the
        # legacy implementation printed still prints, byte for byte, in the
        # same order — which is the parity claim that was ever worth making.
        self.assert_pair("add", "A", "--source", "one", "--context", "context A")
        self.assert_pair("add", "B", "--ref", "task:2")
        self.assert_pair("resolve", "followup:1", "did it")
        old, new = self.pair("list")
        self.assertEqual(new.returncode, old.returncode)
        self.assertEqual(new.stdout, old.stdout)
        old, new = self.pair("show")
        self.assertEqual(new.returncode, old.returncode)
        added = [line for line in new.stdout.splitlines()
                 if line.strip() == "[action]"]
        self.assertEqual(len(added), 2)
        kept = [line for line in new.stdout.splitlines()
                if line.strip() != "[action]"]
        self.assertEqual("\n".join(kept), old.stdout.rstrip("\n"))

    def test_empty_description_error_is_identical(self):
        old, new = self.pair("add", "")
        self.assertEqual((new.returncode, new.stderr), (old.returncode, old.stderr))
        self.assertEqual(new.returncode, 2)

    def test_missing_resolve_error_is_identical(self):
        old, new = self.pair("resolve", "followup:404")
        self.assertEqual((new.returncode, new.stderr), (old.returncode, old.stderr))
        self.assertEqual(new.returncode, 2)

    def test_empty_list_and_show(self):
        for command, expected in (("list", "no open follow-ups\n"),
                                  ("show", "no follow-ups on file\n")):
            old, new = self.pair(command)
            self.assertEqual(old.stdout, expected)
            self.assertEqual(new.stdout, expected)
            self.assertEqual((old.returncode, new.returncode), (0, 0))

    def test_next_n_continuity(self):
        for number in range(1, 4):
            old, new = self.assert_pair("add", f"item {number}")
            expected = f"followup:{number}"
            self.assertEqual(json.loads(old.stdout)["id"], expected)
            self.assertEqual(json.loads(new.stdout)["id"], expected)

    def test_json_render(self):
        result = run(SHIM, classified(["add", "description", "--ref", "repo#7"]),
                     self.shim_tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run(SHIM, ["json"], self.shim_tmp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(result.stdout)["items"]["followup:1"]
        self.assertEqual(item["description"], "description")
        self.assertEqual(item["ref"], "repo#7")


class FollowupsDueCase(unittest.TestCase):
    """`--due` on the shim (P-31). This is the hub's own filing path, so
    without it the schema could hold a deadline and the hub would still have
    nowhere to put one — except as "due this week" inside a description."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def shim(self, *args):
        return run(SHIM, classified(args), self.state)

    def estate(self, *args):
        env = dict(os.environ, ESTATE_STATE_DIR=self.state)
        return subprocess.run(
            [sys.executable, os.path.join(BIN_DIR, "estate"), *args],
            capture_output=True, text=True, env=env)

    def test_due_is_stored_as_a_column_and_echoed_back(self):
        result = self.shim("add", "quarterly reports",
                           "--source", "hub", "--due", "2026-08-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["due_at"],
                         "2026-08-01T23:59:59+00:00")

    def test_a_due_followup_shows_up_in_estate_due(self):
        self.shim("add", "deadline item", "--due", "2026-08-01")
        self.shim("add", "no deadline")
        rows = json.loads(self.estate("due", "--within", "36500", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["t-1"])
        self.assertEqual(rows[0]["kind"], "followup")

    def test_an_unparseable_due_is_rejected_not_guessed(self):
        result = self.shim("add", "vague", "--due", "sometime next week")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--due", result.stderr)
        self.assertEqual(json.loads(self.shim("json").stdout)["items"], {})

    def test_output_parity_holds_when_no_due_is_given(self):
        # The legacy oracle has no concept of a deadline, so the key must be
        # ABSENT rather than null on anything the old tool could have produced.
        result = self.shim("add", "ordinary item")
        self.assertNotIn("due_at", json.loads(result.stdout))


class FollowupsDueStatesCase(FollowupsDueCase):
    """`followups due` — P-07's five-state reading, in the shim's own
    `followup:N` vocabulary."""

    def iso(self, days):
        return (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(days=days)).isoformat()

    def seed(self):
        self.shim("add", "already late", "--due", self.iso(-2))
        self.shim("add", "in two days", "--due", self.iso(2))
        self.shim("add", "next month", "--due", self.iso(40))
        self.shim("add", "no deadline")

    def rows(self, *args):
        result = self.shim("due", "--json", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_every_state_is_reported_most_urgent_first(self):
        self.seed()
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows],
                         ["followup:1", "followup:2", "followup:3", "followup:4"])
        self.assertEqual([r["due_state"] for r in rows],
                         ["overdue", "due_soon", "later", "undated"])

    def test_the_legacy_id_and_the_task_id_are_both_there(self):
        # the hub replies "resolve followup:1"; the estate speaks t-N. A
        # consumer needs both to route a reply back to a row.
        self.shim("add", "an item")
        row = self.rows()[0]
        self.assertEqual((row["id"], row["task_id"]), ("followup:1", "t-1"))

    def test_a_later_item_is_absent_from_the_notice_set(self):
        """S24's negative, at the shim: an outside-interval follow-up must not
        be selectable as currently due."""
        self.seed()
        ids = [r["id"] for r in self.rows("--state", "overdue,due_today,due_soon")]
        self.assertEqual(ids, ["followup:1", "followup:2"])

    def test_the_notice_interval_moves_the_boundary(self):
        self.seed()
        self.assertEqual(
            [r["id"] for r in self.rows("--notice", "60", "--state", "due_soon")],
            ["followup:2", "followup:3"])

    def test_a_resolved_followup_is_not_attention(self):
        self.shim("add", "handled", "--due", self.iso(-2))
        self.shim("resolve", "followup:1", "done")
        self.assertEqual(self.rows(), [])

    def test_the_plain_rendering_names_the_state(self):
        self.shim("add", "already late", "--due", self.iso(-2))
        out = self.shim("due").stdout
        self.assertIn("[overdue", out)
        self.assertIn("followup:1", out)

    def test_an_unknown_state_is_refused_not_silently_empty(self):
        result = self.shim("due", "--state", "overdu")
        self.assertEqual(result.returncode, 2)
        self.assertIn("overdue", result.stderr)


class FollowupsAttentionCase(FollowupsDueCase):
    """`followups attention` — file once, link forever after.

    A sweep runs again: on the next tick, after a crash, and after the hub
    session compacts and forgets what it filed. Without a key that is the same
    instruction as "file a row per tick" (S52).
    """

    def attention(self, *args):
        result = self.shim("attention", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_first_call_files_it(self):
        item = self.attention("the owner must submit the pending review",
                              "--key", "acme/gizmo#4502", "--source", "hub",
                              "--ref", "acme/gizmo#4502")
        self.assertFalse(item["linked"])
        self.assertEqual(item["id"], "followup:1")
        self.assertEqual(item["attention_key"], "acme/gizmo#4502")
        self.assertEqual(item["status"], "open")

    def test_the_second_call_links_the_same_row(self):
        first = self.attention("same condition", "--key", "k1")
        second = self.attention("worded differently this time", "--key", "k1")
        self.assertTrue(second["linked"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(len(json.loads(self.shim("json").stdout)["items"]), 1)
        # The link does NOT rewrite the row: the first wording stands, so the
        # record says what was actually filed and when.
        self.assertEqual(second["description"], "same condition")

    def test_a_different_key_is_a_different_item(self):
        self.attention("one", "--key", "k1")
        self.attention("two", "--key", "k2")
        self.assertEqual(len(json.loads(self.shim("json").stdout)["items"]), 2)

    def test_a_resolved_item_does_not_suppress_a_recurrence(self):
        first = self.attention("needs you", "--key", "acme/gizmo#42")
        self.shim("resolve", first["id"], "handled")
        second = self.attention("needs you again", "--key", "acme/gizmo#42")
        self.assertFalse(second["linked"])
        self.assertNotEqual(second["task_id"], first["task_id"])

    def test_an_empty_key_is_refused(self):
        result = self.shim("attention", "something", "--key", "  ")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--key", result.stderr)

    def test_an_empty_description_is_refused(self):
        result = self.shim("attention", "", "--key", "k1")
        self.assertEqual(result.returncode, 2)

    def test_it_carries_a_due_date_like_add_does(self):
        item = self.attention("dated attention", "--key", "k1",
                              "--due", "2026-08-01")
        self.assertEqual(item["due_at"], "2026-08-01T23:59:59+00:00")

    def test_an_unparseable_due_is_rejected_and_nothing_is_filed(self):
        result = self.shim("attention", "vague", "--key", "k1",
                           "--due", "sometime")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(self.shim("json").stdout)["items"], {})

    def test_it_shares_the_add_numbering(self):
        # One numbering, because `insert()` is one code path. A second
        # allocator drifting from this one is the bug nobody would notice.
        self.shim("add", "filed by hand")
        item = self.attention("filed by a sweep", "--key", "k1")
        self.assertEqual(item["id"], "followup:2")

    def test_a_filed_item_is_in_the_estate_attention_view(self):
        self.attention("needs you", "--key", "k1")
        rows = json.loads(self.estate("attention", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["t-1"])
        self.assertEqual(rows[0]["due_state"], "undated")

    def test_the_frozen_legacy_files_are_never_written(self):
        self.attention("needs you", "--key", "k1")
        self.shim("due")
        for name in ("followups.json", "ledger.jsonl"):
            self.assertFalse(
                os.path.exists(os.path.join(self.state, "followups", name)),
                f"{name} must stay frozen — bin/estate is the store now")


class FollowupsIdentityCase(FollowupsDueCase):
    """a follow-up with no `legacy_id` is an ordinary follow-up."""

    def bare(self, title="filed straight through estate", **kw):
        """A follow-up row with no `legacy_id`, the way the live store got one."""
        args = ["task", "add", title, "--kind", "followup"]
        for flag, value in kw.items():
            args += [f"--{flag}", value]
        result = self.estate(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_list_and_show_survive_a_followup_with_no_legacy_id(self):
        task_id = self.bare()
        for command in ("list", "show", "json", "due"):
            result = self.shim(command)
            self.assertEqual(result.returncode, 0,
                             f"{command}: {result.stderr}")
            self.assertIn(task_id, result.stdout, command)

    def test_the_displayed_id_is_the_task_id_when_there_is_no_legacy_id(self):
        task_id = self.bare()
        items = json.loads(self.shim("json").stdout)["items"]
        self.assertEqual(list(items), [task_id])
        self.assertEqual(items[task_id]["id"], task_id)
        self.assertIn(task_id, self.shim("list").stdout)

    def test_a_legacy_row_still_displays_its_legacy_id(self):
        self.shim("add", "filed through the shim")
        items = json.loads(self.shim("json").stdout)["items"]
        self.assertEqual(list(items), ["followup:1"])

    def test_one_bare_row_does_not_hide_the_rest_of_the_store(self):
        # The crash was store-wide, not row-local: one envelope-less row and
        # nobody could read their follow-ups at all.
        self.shim("add", "an ordinary one")
        task_id = self.bare()
        out = self.shim("list").stdout
        self.assertIn("followup:1", out)
        self.assertIn(task_id, out)

    def test_resolve_works_by_real_task_id(self):
        self.shim("add", "filed through the shim")
        result = self.shim("resolve", "t-1", "handled by task id")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.shim("json").stdout)["items"]["followup:1"]
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["resolution"], "handled by task id")

    def test_resolve_works_by_task_id_on_a_row_with_no_legacy_id(self):
        task_id = self.bare()
        result = self.shim("resolve", task_id, "done")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.shim("json").stdout)["items"][task_id]["status"],
            "resolved")

    def test_resolve_still_works_by_legacy_id(self):
        self.shim("add", "filed through the shim")
        result = self.shim("resolve", "followup:1", "handled by legacy id")
        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.shim("json").stdout)["items"]["followup:1"]
        self.assertEqual(item["status"], "resolved")

    def test_an_unknown_id_is_still_refused(self):
        self.shim("add", "filed through the shim")
        for unknown in ("t-404", "followup:404", ""):
            result = self.shim("resolve", unknown)
            self.assertEqual(result.returncode, 2, unknown)
            self.assertIn("no follow-up matches", result.stderr)

    def test_a_non_followup_task_id_is_not_resolvable_here(self):
        # `resolve` reaches follow-ups and nothing else, id scheme or not.
        self.estate("task", "add", "not a follow-up", "--kind", "chore")
        result = self.shim("resolve", "t-1")
        self.assertEqual(result.returncode, 2)

    def test_due_reports_both_ids_for_a_row_with_no_legacy_id(self):
        task_id = self.bare("dated", due="2026-08-01")
        row = json.loads(self.shim("due", "--json").stdout)[0]
        self.assertEqual((row["id"], row["task_id"]), (task_id, task_id))


class FollowupResolveOperationCase(FollowupsDueCase):
    """`apply_followup_resolve`, the one mutation path."""

    def load(self):
        """The shim as a module, pointed at this case's throwaway store."""
        import importlib.util
        from importlib.machinery import SourceFileLoader
        os.environ["ESTATE_STATE_DIR"] = self.state
        try:
            loader = SourceFileLoader(f"_followups_{id(self)}", SHIM)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        finally:
            os.environ.pop("ESTATE_STATE_DIR", None)
        return module

    def state_of(self, key="followup:1"):
        return json.loads(self.shim("json").stdout)["items"][key]["status"]

    def test_the_cli_records_the_rows_creator_and_the_shims_own_summary(self):
        """the contract: resolve now closes through apply_status, so the"""
        self.shim("add", "chase it", "--source", "pr-reviewer")
        result = self.shim("resolve", "followup:1", "chased")
        self.assertEqual((result.returncode, result.stdout),
                         (0, "resolved: followup:1\n"))
        events = json.loads(self.estate("events", "--task", "t-1",
                                        "--json").stdout)
        last = events[-1]
        self.assertEqual((last["actor"], last["summary"], last["detail"]),
                         ("pr-reviewer", "open -> done", "chased"))

    def test_the_expected_status_argument_refuses_a_stale_snapshot(self):
        self.shim("add", "chase it")
        followups = self.load()
        with self.assertRaises(followups.FollowupResolveConflict) as caught:
            followups.apply_followup_resolve(
                "t-1", "note", "owner", expected_status="needs-owner")
        self.assertIn("changed from 'needs-owner' to 'open'", str(caught.exception))
        self.assertEqual(self.state_of(), "open")

    def test_a_conflict_is_a_refusal_the_caller_can_show(self):
        """Conflict is a subclass of the refusal type, so a caller that only
        catches the base class still fails closed rather than raising through."""
        followups = self.load()
        self.assertTrue(issubclass(followups.FollowupResolveConflict,
                                   followups.FollowupResolveError))
        self.assertTrue(issubclass(followups.FollowupResolveError, ValueError))

    def test_a_caller_naming_a_terminal_status_is_refused_not_obeyed(self):
        self.shim("add", "chase it")
        followups = self.load()
        followups.apply_followup_resolve("t-1", "first", "owner",
                                         expected_status="open")
        with self.assertRaises(followups.FollowupResolveError) as caught:
            followups.apply_followup_resolve("t-1", "second", "owner",
                                             expected_status="done")
        self.assertIn("is already 'done'", str(caught.exception))
        item = json.loads(self.shim("json").stdout)["items"]["followup:1"]
        self.assertEqual(item["resolution"], "first")

    def test_the_returned_shape_names_both_ids_and_the_status_it_left(self):
        self.shim("add", "chase it")
        result = self.load().apply_followup_resolve(
            "t-1", "done with it", "owner", expected_status="open")
        self.assertEqual(result["id"], "followup:1")
        self.assertEqual(result["task_id"], "t-1")
        self.assertEqual((result["from_status"], result["status"],
                          result["state"]), ("open", "done", "resolved"))
        self.assertEqual(result["resolution"], "done with it")
        self.assertTrue(result["resolved_at"])


if __name__ == "__main__":
    unittest.main()

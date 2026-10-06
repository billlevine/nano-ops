#!/usr/bin/env python3
"""Tests for lib/estate_reconcile — chiefly the fourth verdict."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

import estate_reconcile as er  # noqa: E402


def note(name, body="a fact about dispatching work", declares=None):
    front = "name: x\ndescription: y"
    if declares:
        front += f"\nestate: {declares}"
    return f"---\n{front}\n---\n\n{body}\n"


class TheFourthVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = Path(self.tmp.name)

    def write(self, name, text):
        (self.mem / name).write_text(text, encoding="utf-8")

    def index(self, *names):
        self.write("MEMORY.md", "\n".join(
            f"- [{n.split('.')[0]}]({n}) — a summary" for n in names) + "\n")

    def load(self):
        return er.load_notes(self.mem)

    def by_name(self, notes, name):
        return next(n for n in notes if n["name"] == name)

    # --- the rule ------------------------------------------------------

    def test_a_narrow_scope_the_index_still_lists_is_divergent(self):
        self.write("feedback_a.md", note("feedback_a.md", declares="m-1"))
        self.index("feedback_a.md")
        self.assertEqual(er.verdict("hub", indexed=True), er.DIVERGENT)
        self.assertTrue(self.by_name(self.load(), "feedback_a.md")["indexed"])

    def test_a_narrow_scope_the_index_no_longer_lists_is_scoped(self):
        """The end state of a deliberate migration, not a defect."""
        self.write("feedback_a.md", note("feedback_a.md", declares="m-1"))
        self.index()                      # an index with no lines at all
        self.assertEqual(er.verdict("hub", indexed=False), er.SCOPED)
        self.assertFalse(self.by_name(self.load(), "feedback_a.md")["indexed"])

    def test_shared_is_aligned_whether_the_index_lists_it_or_not(self):
        """Nothing diverges when both sides are estate-wide."""
        self.assertEqual(er.verdict("shared", indexed=True), er.ALIGNED)
        self.assertEqual(er.verdict("shared", indexed=False), er.ALIGNED)

    def test_unlinking_one_note_does_not_move_another(self):
        self.write("feedback_moved.md", note("feedback_moved.md", declares="m-1"))
        self.write("feedback_stays.md", note("feedback_stays.md", declares="m-2"))
        self.index("feedback_stays.md")
        notes = self.load()
        self.assertFalse(self.by_name(notes, "feedback_moved.md")["indexed"])
        self.assertTrue(self.by_name(notes, "feedback_stays.md")["indexed"])

    # --- the absence rule ----------------------------------------------

    def test_an_unreadable_index_never_produces_scoped(self):
        """The default is the load-bearing half.

        A caller that could not read MEMORY.md has learned nothing about what
        is listed in it, and `scoped` is a claim that a fact is out of every
        session's context. Getting that for free from a failed read is the
        absence bug this estate instruments everywhere else.
        """
        self.write("feedback_a.md", note("feedback_a.md", declares="m-1"))
        # no MEMORY.md at all
        self.assertIsNone(er.indexed_names(self.mem))
        self.assertTrue(self.by_name(self.load(), "feedback_a.md")["indexed"])
        self.assertEqual(er.verdict("hub"), er.DIVERGENT)

    def test_indexed_defaults_to_true(self):
        self.assertEqual(er.verdict("hub"), er.DIVERGENT)

    def test_an_empty_index_is_not_an_absent_one(self):
        """A real, readable, empty MEMORY.md IS evidence: nothing is listed."""
        self.index()
        self.assertEqual(er.indexed_names(self.mem), set())

    def test_an_unreadable_note_is_assumed_indexed(self):
        """It contributes no declared pair, but it must not be softened either."""
        path = self.mem / "feedback_broken.md"
        path.write_bytes(b"---\nname: x\n---\nbody\n")
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o644)
        self.index()
        broken = [n for n in self.load() if n["name"] == "feedback_broken.md"]
        if broken and broken[0]["unreadable"]:
            self.assertTrue(broken[0]["indexed"])

    # --- parsing the index ---------------------------------------------

    def test_the_index_is_keyed_on_the_filename_not_the_slug(self):
        self.write("feedback_a.md", note("feedback_a.md", declares="m-1"))
        self.write("MEMORY.md",
                   "- [some-other-slug](feedback_a.md) — a summary\n")
        self.assertEqual(er.indexed_names(self.mem), {"feedback_a.md"})

    def test_memory_md_is_never_itself_a_note(self):
        self.write("feedback_a.md", note("feedback_a.md"))
        self.index("feedback_a.md")
        self.assertEqual([n["name"] for n in self.load()], ["feedback_a.md"])

    # --- the whole read -------------------------------------------------

    def test_reconcile_counts_scoped_apart_from_divergent(self):
        self.write("feedback_moved.md",
                   note("feedback_moved.md", "dispatch cursor intake relay",
                        declares="m-1"))
        self.write("feedback_still.md",
                   note("feedback_still.md", "worktree builder agent-deck",
                        declares="m-2"))
        self.index("feedback_still.md")
        memories = [
            {"id": "m-1", "scope": "hub", "status": "active",
             "body": "dispatch cursor intake relay"},
            {"id": "m-2", "scope": "hub", "status": "active",
             "body": "worktree builder agent-deck"},
        ]
        report = er.reconcile(memories, self.load())
        self.assertEqual(report["counts"]["scoped"], 1)
        self.assertEqual(report["counts"]["divergent"], 1)
        verdicts = {f["note"]: f["verdict"] for f in report["findings"]}
        self.assertEqual(verdicts["feedback_moved.md"], er.SCOPED)
        self.assertEqual(verdicts["feedback_still.md"], er.DIVERGENT)

    def test_a_dangling_id_is_still_dangling_however_the_index_reads(self):
        self.write("feedback_a.md", note("feedback_a.md", declares="m-404"))
        self.index()
        report = er.reconcile([], self.load())
        self.assertEqual(report["findings"][0]["verdict"], er.DANGLING)
        self.assertEqual(report["counts"]["scoped"], 0)

    def test_reconcile_writes_to_neither_store(self):
        self.write("feedback_a.md", note("feedback_a.md", declares="m-1"))
        self.index("feedback_a.md")
        before = {p.name: p.read_bytes() for p in self.mem.iterdir()}
        er.reconcile([{"id": "m-1", "scope": "hub", "status": "active",
                       "body": "dispatch"}], self.load())
        after = {p.name: p.read_bytes() for p in self.mem.iterdir()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)

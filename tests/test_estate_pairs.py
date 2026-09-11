#!/usr/bin/env python3
"""Tests for t-1061: a proposal and its derived exit follow-up as ONE row.

Three layers, for the three places the feature can be wrong.

  1. `lib/estate_pairs` is pure, so the pairing window and the asymmetry of the
     divergence test are exercised by moving timestamps rather than by writing
     history.
  2. `bin/followups add --derives-from` and `bin/estate attention` are exercised
     against a real throwaway store through the real scripts, because a mocked
     store would prove the wrong thing about a refs envelope.
  3. `bin/backfill-derived-followups` is exercised against a real store plus a
     real night-shift ledger, including the case that broke it on its first run
     against live data: the backfill's own audit note reading as divergence.

Run: python3 tests/test_estate_pairs.py
"""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

# This core keeps tests under `tests/` and scripts under `bin/`, so the two are
# siblings rather than the same directory (docs/extraction-allowlist.md).
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_HERE)
BIN_DIR = os.path.join(REPO_ROOT, "bin")
ESTATE = os.path.join(BIN_DIR, "estate")
FOLLOWUPS = os.path.join(BIN_DIR, "followups")
BACKFILL = os.path.join(BIN_DIR, "backfill-derived-followups")
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import estate_pairs as P  # noqa: E402
import estate_attention  # noqa: E402
import estate_work  # noqa: E402

UTC = dt.timezone.utc


def stamp(minutes: float, base=dt.datetime(2026, 8, 20, 12, 0, tzinfo=UTC)):
    return (base + dt.timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------------- #
# 1. the pure rule
# --------------------------------------------------------------------------- #

class LinkCase(unittest.TestCase):
    def test_a_task_id_is_a_link(self):
        self.assertEqual(P.link_of({P.DERIVES_FROM: "t-386"}), "t-386")

    def test_the_transient_queue_key_is_not(self):
        """The whole point of the key: `refs.ref` already held "task:4" and it
        resolves to nothing a day later."""
        self.assertIsNone(P.link_of({P.DERIVES_FROM: "task:4"}))

    def test_absent_and_null_are_both_no_link(self):
        self.assertIsNone(P.link_of({}))
        self.assertIsNone(P.link_of({P.DERIVES_FROM: None}))
        self.assertIsNone(P.link_of({P.DERIVES_FROM: ""}))

    def test_a_non_dict_envelope_does_not_raise(self):
        for junk in (None, "t-386", [], 7):
            self.assertIsNone(P.link_of(junk))


class WindowCase(unittest.TestCase):
    def test_a_paired_record_is_not_divergence(self):
        """A triage pass writes both halves in one sitting. Eight of the
        thirteen live pairs carry exactly that, and none is a divergence."""
        self.assertEqual(
            P.unpaired_records([stamp(0)], [stamp(8)], window_minutes=30), [])

    def test_a_record_outside_the_window_is_divergence(self):
        self.assertEqual(
            P.unpaired_records([stamp(0)], [stamp(90)], window_minutes=30),
            [stamp(0)])

    def test_the_window_is_symmetric(self):
        """Either half may be written first — a landing note can precede the
        triage that notices it."""
        self.assertEqual(
            P.unpaired_records([stamp(0)], [stamp(-8)], window_minutes=30), [])

    def test_a_proposal_side_record_alone_is_never_divergence(self):
        """THE ASYMMETRY. The proposal survives a collapse, so its own content
        cannot be hidden by one. Checking it would refuse to collapse every
        ordinary pair, because the proposal is the row somebody works on."""
        self.assertEqual(
            P.unpaired_records([], [stamp(0), stamp(500)],
                               window_minutes=30), [])

    def test_an_unreadable_followup_stamp_counts_as_unpaired(self):
        """It is a record that exists and no window can be measured from it.
        Refusing the collapse is the reading that cannot hide it."""
        self.assertEqual(
            P.unpaired_records(["not a date"], [stamp(0)]), ["not a date"])

    def test_an_unreadable_proposal_stamp_pairs_with_nothing(self):
        self.assertEqual(
            P.unpaired_records([stamp(0)], ["not a date"]), [stamp(0)])


class ClassifyCase(unittest.TestCase):
    def refs(self, **extra):
        return dict({P.DERIVES_FROM: "t-386"}, **extra)

    def test_no_link_is_no_pairing_record(self):
        self.assertIsNone(P.classify({}, True))

    def test_same_question_collapses(self):
        record = P.classify(self.refs(), True, [stamp(0)], [stamp(5)])
        self.assertEqual(record["verdict"], P.COLLAPSED)
        self.assertEqual(record["proposal"], "t-386")
        self.assertEqual(record["evidence"], [])

    def test_unpaired_followup_content_diverges_and_names_its_evidence(self):
        record = P.classify(self.refs(), True, [stamp(0)], [stamp(600)])
        self.assertEqual(record["verdict"], P.DIVERGED_V)
        self.assertEqual(record["evidence"], [stamp(0)])

    def test_a_recorded_divergence_wins_over_the_derived_rule(self):
        record = P.classify(self.refs(**{P.DIVERGED: "read both, different"}),
                            True, [stamp(0)], [stamp(5)])
        self.assertEqual(record["verdict"], P.DIVERGED_V)
        self.assertEqual(record["reason"], P.REASONS["recorded"])

    def test_there_is_no_override_that_forces_a_collapse(self):
        """The only override produces MORE rows. A "collapse anyway" key would
        let one filing decision hide content the invariant protects."""
        record = P.classify(self.refs(derives_collapsed=True), True,
                            [stamp(0)], [stamp(600)])
        self.assertEqual(record["verdict"], P.DIVERGED_V)

    def test_an_absent_other_half_is_its_own_verdict(self):
        record = P.classify(self.refs(), False, [stamp(0)], [])
        self.assertEqual(record["verdict"], P.HALF)

    def test_only_collapsed_withholds_a_row(self):
        pairs = {
            "t-1": {"proposal": "t-9", "followup": "t-1", "verdict": P.COLLAPSED},
            "t-2": {"proposal": "t-8", "followup": "t-2", "verdict": P.DIVERGED_V},
            "t-3": {"proposal": "t-7", "followup": "t-3", "verdict": P.HALF},
        }
        self.assertEqual(P.collapsed_ids(pairs), {"t-1"})

    def test_by_proposal_keeps_every_derived_half(self):
        pairs = {
            "t-1": {"proposal": "t-9", "followup": "t-1", "verdict": P.COLLAPSED},
            "t-2": {"proposal": "t-9", "followup": "t-2", "verdict": P.COLLAPSED},
        }
        self.assertEqual(len(P.by_proposal(pairs)["t-9"]), 2)


class BookkeepingCase(unittest.TestCase):
    """The bug the first live run found: recording the link writes a one-sided
    note, which then proves the pair diverged."""

    def test_the_links_own_note_is_not_content(self):
        self.assertTrue(P.is_bookkeeping(json.dumps({P.DERIVES_FROM: "t-386"})))

    def test_an_ordinary_note_is_content(self):
        self.assertFalse(P.is_bookkeeping(json.dumps({"path": "a/b"})))
        self.assertFalse(P.is_bookkeeping(None))

    def test_an_unreadable_envelope_is_content(self):
        """Conservative in the direction that cannot hide anything."""
        self.assertFalse(P.is_bookkeeping("{not json"))


class DriftCase(unittest.TestCase):
    """This module duplicates two things rather than importing them, for the
    stated no-CLI-dependency reason. The duplication is checked here."""

    def test_task_id_shape_matches_the_store(self):
        for good in ("t-1", "t-1061"):
            self.assertTrue(P.TASK_ID_RE.match(good))
        for bad in ("t-", "task:4", "followup:12", "T-1", " t-1"):
            self.assertFalse(P.TASK_ID_RE.match(bad))

    def test_stamp_normalizes_like_estate_attention(self):
        for value in ("2026-08-20T12:00:00Z", "2026-08-20T12:00:00",
                      "2026-08-20T08:00:00-04:00", "nonsense", None):
            self.assertEqual(P._stamp(value),
                             estate_attention.parse_iso(value)
                             if value is not None else None)


# --------------------------------------------------------------------------- #
# 2. the CLI layer, against a real store
# --------------------------------------------------------------------------- #

class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.db = os.path.join(self.state, "estate.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, script, *args):
        env = dict(os.environ, ESTATE_STATE_DIR=self.state,
                   ESTATE_LESSONS_PATH=os.path.join(self.state, "lessons.md"))
        return subprocess.run([sys.executable, script, *args],
                              capture_output=True, text=True, env=env)

    def estate(self, *args):
        out = self.run_cli(ESTATE, *args)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def followups(self, *args):
        out = self.run_cli(FOLLOWUPS, *args)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def a_proposal(self, title="the standing question"):
        """A task parked at needs-owner with a review stage — a decision-tier
        attention row, which is what an auto-queued proposal is."""
        self.estate("task", "add", title, "--kind", "proposal", "--ready")
        with self.conn() as c:
            tid = c.execute(
                "SELECT id FROM tasks WHERE title=?", (title,)).fetchone()["id"]
        self.estate("claim", tid, "--actor", "pr-reviewer")
        self.estate("needs-owner", tid, "waiting on the owner")
        with self.conn() as c:
            c.execute("UPDATE tasks SET stage='approved-backlog' WHERE id=?",
                      (tid,))
            c.commit()
        return tid

    def needs_migrated_followups(self, *tokens):
        """Skip unless `bin/followups` is the estate-backed one.

        A contract between two scripts, only one of which is in this
        extraction: a `bin/followups` predating the filing-time schema (t-475),
        the derivation key (t-1061) or the `attention` verb (P-07) cannot file
        the follow-up half of a pair at all, and asserting against it would
        report a missing re-sync as a bug in the collapse. On the FILE rather
        than a version string, so these start running by themselves the moment
        that script lands — tests/test_estate.py keeps the same guard.
        """
        with open(FOLLOWUPS, encoding="utf-8") as handle:
            source = handle.read()
        for token in tokens:
            if token not in source:
                self.skipTest(f"bin/followups here does not support {token} — "
                              "the other half of this contract has not been "
                              "re-synced yet")

    def a_followup(self, desc, derives_from=None, ref="task:4"):
        self.needs_migrated_followups("--classification", "--derives-from")
        args = ["add", desc, "--source", "pr-reviewer", "--ref", ref,
                "--context", "[pr-reviewer exit, needs-you] ...",
                "--classification", "clarification"]
        if derives_from:
            args += ["--derives-from", derives_from]
        return json.loads(self.followups(*args).stdout)

    def followup_task(self, item):
        with self.conn() as c:
            return c.execute("SELECT id FROM tasks WHERE kind='followup' "
                             "ORDER BY seq DESC").fetchone()["id"]

    def note(self, task_id, summary, ts=None, refs=None):
        with self.conn() as c:
            c.execute("INSERT INTO events (ts, actor, task_id, kind, summary, "
                      "refs) VALUES (?,?,?,?,?,?)",
                      (ts or dt.datetime.now(UTC).isoformat(), "cli", task_id,
                       "note", summary, json.dumps(refs) if refs else None))
            c.commit()

    def attention_ids(self, *extra):
        rows = json.loads(self.estate("attention", "--json", *extra).stdout)
        return [r["id"] for r in rows]


class StoreCase(StoreBase):
    """The filing and rendering layer."""

    def test_the_link_is_written_into_the_refs_envelope(self):
        proposal = self.a_proposal()
        item = self.a_followup("do the thing", derives_from=proposal)
        with self.conn() as c:
            refs = json.loads(c.execute(
                "SELECT refs FROM tasks WHERE id=?",
                (item["task_id"] if "task_id" in item else
                 self.followup_task(item),)).fetchone()["refs"]
                if False else c.execute(
                "SELECT refs FROM tasks WHERE kind='followup'").fetchone()["refs"])
        self.assertEqual(refs[P.DERIVES_FROM], proposal)
        # and the queue key is still recorded, because "which drain produced
        # this" is a different question from "which question is this".
        self.assertEqual(refs["ref"], "task:4")

    def test_a_filing_with_no_link_stores_no_key_rather_than_a_null(self):
        """Absent is absent. A stored null would read as "somebody looked and
        found no proposal", and nobody looked."""
        self.a_followup("legacy shaped filing")
        with self.conn() as c:
            refs = json.loads(c.execute(
                "SELECT refs FROM tasks WHERE kind='followup'").fetchone()["refs"])
        self.assertNotIn(P.DERIVES_FROM, refs)

    def test_a_queue_key_is_refused_at_the_door(self):
        self.needs_migrated_followups("--classification", "--derives-from")
        out = self.run_cli(FOLLOWUPS, "add", "x", "--source", "pr-reviewer",
                           "--ref", "task:4", "--classification", "action",
                           "--derives-from", "task:4")
        self.assertEqual(out.returncode, 2)
        self.assertIn("--derives-from must name a task as t-N", out.stderr)
        # Refused BEFORE `init_db`, so on a store that did not exist yet the
        # refusal did not even mint one. Both shapes are "nothing was written".
        if os.path.exists(self.db):
            with self.conn() as c:
                self.assertIsNone(c.execute(
                    "SELECT id FROM tasks WHERE kind='followup'").fetchone())

    def test_attention_filing_takes_the_link_too(self):
        self.needs_migrated_followups("attention", "--derives-from")
        proposal = self.a_proposal()
        self.followups("attention", "the hub's own pair", "--key", "k1",
                       "--source", "ops", "--classification", "action",
                       "--derives-from", proposal)
        with self.conn() as c:
            refs = json.loads(c.execute(
                "SELECT refs FROM tasks WHERE kind='followup'").fetchone()["refs"])
        self.assertEqual(refs[P.DERIVES_FROM], proposal)
        self.assertEqual(refs["attention_key"], "k1")

    # -- rendering --------------------------------------------------------- #

    def test_a_linked_pair_renders_as_one_row(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        ids = self.attention_ids()
        self.assertIn(proposal, ids)
        self.assertNotIn(fid, ids)

    def test_the_surviving_row_names_the_half_it_absorbed(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        rows = {r["id"]: r for r in
                json.loads(self.estate("attention", "--json").stdout)}
        pairs = rows[proposal]["derived_pairs"]
        self.assertEqual([p["followup"] for p in pairs], [fid])
        self.assertEqual(pairs[0]["verdict"], P.COLLAPSED)
        text = self.estate("attention").stdout
        self.assertIn(f"+{fid} collapsed", text)

    def test_an_unlinked_pair_still_renders_as_two(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again")
        fid = self.followup_task(None)
        ids = self.attention_ids()
        self.assertIn(proposal, ids)
        self.assertIn(fid, ids)

    def test_a_divergent_pair_is_never_silently_merged(self):
        """t-386/t-498: a landing note went onto the follow-up alone. Collapsing
        that pair would take the note off the surface."""
        proposal = self.a_proposal()
        self.a_followup("the residual clause", derives_from=proposal)
        fid = self.followup_task(None)
        self.note(fid, "Landed abc1234 on main", ts=stamp(0))
        ids = self.attention_ids()
        self.assertIn(proposal, ids)
        self.assertIn(fid, ids)
        text = self.estate("attention").stdout
        self.assertIn(f"⚯ {proposal}/{fid} diverged", text)

    def test_a_paired_triage_note_does_not_split_the_pair(self):
        """The eight live pairs whose follow-up carries a hand-written pointer
        written in the same sitting as the proposal's own triage note."""
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        self.note(proposal, "TRIAGE: the full note", ts=stamp(0))
        self.note(fid, "TRIAGE: pointer to the proposal half", ts=stamp(7))
        self.assertNotIn(fid, self.attention_ids())

    def test_proposal_side_activity_never_refuses_the_collapse(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        for offset in (0, 500, 5000):
            self.note(proposal, f"work note {offset}", ts=stamp(offset))
        self.assertNotIn(fid, self.attention_ids())

    def test_no_collapse_restores_the_old_listing(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        self.assertIn(fid, self.attention_ids("--no-collapse"))

    def test_counts_shrink_with_the_listing_and_say_by_how_much(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        counts = json.loads(self.estate("attention", "--counts").stdout)
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["collapsed"], 1)
        before = json.loads(
            self.estate("attention", "--counts", "--no-collapse").stdout)
        self.assertEqual(before["total"], 2)
        self.assertEqual(before["collapsed"], 0)

    def test_a_proposal_outside_the_listing_leaves_its_half_alone(self):
        """`--kind followup` filters the proposal out, so there is nothing on
        the page to collapse into. The row renders, marked `half`."""
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        rows = {r["id"]: r for r in json.loads(
            self.estate("attention", "--json", "--kind", "followup").stdout)}
        self.assertIn(fid, rows)
        self.assertEqual(rows[fid]["derived_pairs"][0]["verdict"], P.HALF)

    def test_awaiting_is_untouched_by_the_collapse(self):
        """The collapse lives in `cmd_attention`, not in the shared selector,
        so the sibling reader that shares `attention_rows` does not move."""
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        payload = json.loads(self.estate("awaiting", "--json").stdout)
        ids = {row["id"] for group in payload.get("groups", {}).values()
               for row in (group if isinstance(group, list)
                           else group.get("items", []))} if isinstance(
            payload.get("groups"), dict) else set()
        blob = json.dumps(payload)
        self.assertIn(fid, blob)
        self.assertIn(proposal, blob)
        del ids

    # -- the dashboard payload --------------------------------------------- #

    def test_the_dashboard_payload_collapses_and_keeps_the_row_in_tasks(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again", derives_from=proposal)
        fid = self.followup_task(None)
        with self.conn() as c:
            work = estate_work.build(c)
        self.assertNotIn(fid, work["attention"])
        self.assertIn(proposal, work["attention"])
        self.assertEqual(work["attention_counts"]["collapsed"], 1)
        # The row is withheld from the SET, never from the store's snapshot:
        # the page's own task lookup still resolves it.
        self.assertIn(fid, {t["id"] for t in work["tasks"]})
        # and no tier still counts it, which is what keeps "n of N shown"
        # honest.
        self.assertNotIn(fid, [i for tier in work["attention_tiers"]
                               for i in tier["ids"]])
        self.assertEqual(work["attention_pairs"][fid]["verdict"], P.COLLAPSED)


# --------------------------------------------------------------------------- #
# 3. the backfill
# --------------------------------------------------------------------------- #

class BackfillCase(StoreBase):
    def setUp(self):
        # `bin/backfill-derived-followups` is the one-shot that records the
        # link on pairs filed before the key existed. It is not part of this
        # core yet — a separate allowlist row and its own candidate — so these
        # skip until it is, the same guard and rationale as the classification
        # skips in tests/test_estate.py. Everything above this class tests the
        # collapse itself and runs.
        if not os.path.exists(BACKFILL):
            self.skipTest("bin/backfill-derived-followups has not been "
                          "extracted into this core yet")
        super().setUp()

    def ledger(self, rows):
        path = os.path.join(self.state, "pr-reviewer")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "ledger.jsonl"), "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def exited(self, queue_key, proposal, legacy, resolved=False):
        return {"ts": stamp(0), "pr": queue_key, "event": "exited",
                "estate_id": proposal, "destination": "followups",
                "detail": f"{legacy} (resolved)" if resolved else legacy}

    def backfill(self, *args):
        out = self.run_cli(BACKFILL, *args)
        self.assertIn(out.returncode, (0,), out.stderr)
        return out

    def plan(self, *args):
        return {e["legacy"]: e
                for e in json.loads(self.backfill("--json", *args).stdout)}

    def test_it_links_from_the_ledgers_own_exit_row(self):
        proposal = self.a_proposal()
        item = self.a_followup("the same question again")
        fid = self.followup_task(None)
        self.ledger([self.exited("task:4", proposal, item["id"])])
        self.assertEqual(self.plan()[item["id"]]["verdict"], "link")
        self.backfill("--apply")
        with self.conn() as c:
            refs = json.loads(c.execute("SELECT refs FROM tasks WHERE id=?",
                                        (fid,)).fetchone()["refs"])
        self.assertEqual(refs[P.DERIVES_FROM], proposal)

    def test_the_backfills_own_note_does_not_prove_divergence(self):
        """The bug the first live run found. Every pair came back `diverged`
        on the strength of the note the backfill had just written."""
        proposal = self.a_proposal()
        item = self.a_followup("the same question again")
        fid = self.followup_task(None)
        self.ledger([self.exited("task:4", proposal, item["id"])])
        self.backfill("--apply")
        self.assertNotIn(fid, self.attention_ids())

    def test_a_resolved_suffix_is_parsed(self):
        proposal = self.a_proposal()
        item = self.a_followup("the same question again")
        self.ledger([self.exited("task:4", proposal, item["id"], resolved=True)])
        self.assertEqual(self.plan()[item["id"]]["verdict"], "link")

    def test_it_is_idempotent(self):
        proposal = self.a_proposal()
        item = self.a_followup("the same question again")
        self.ledger([self.exited("task:4", proposal, item["id"])])
        self.backfill("--apply")
        self.assertEqual(self.plan()[item["id"]]["verdict"], "already")
        out = self.backfill("--apply")
        self.assertIn("linked 0 follow-ups", out.stderr)

    def test_it_never_overwrites_a_link_already_on_file(self):
        proposal = self.a_proposal()
        other = self.a_proposal("a different standing question")
        item = self.a_followup("the same question again", derives_from=other)
        self.ledger([self.exited("task:4", proposal, item["id"])])
        entry = self.plan()[item["id"]]
        self.assertEqual(entry["verdict"], "conflict")
        self.assertEqual(entry["recorded"], other)
        self.backfill("--apply")
        self.assertEqual(self.plan()[item["id"]]["recorded"], other)

    def test_a_proposal_the_store_does_not_hold_is_unresolved_not_written(self):
        item = self.a_followup("the same question again")
        self.ledger([self.exited("task:4", "t-99999", item["id"])])
        self.assertEqual(self.plan()[item["id"]]["verdict"], "unresolved")
        self.backfill("--apply")
        with self.conn() as c:
            refs = json.loads(c.execute(
                "SELECT refs FROM tasks WHERE kind='followup'").fetchone()["refs"])
        self.assertNotIn(P.DERIVES_FROM, refs)

    def test_a_followup_the_store_does_not_hold_is_missing(self):
        proposal = self.a_proposal()
        self.ledger([self.exited("task:4", proposal, "followup:9999")])
        self.assertEqual(self.plan()["followup:9999"]["verdict"], "missing")

    def test_it_writes_nothing_on_an_empty_or_absent_ledger(self):
        proposal = self.a_proposal()
        self.a_followup("the same question again")
        self.assertEqual(self.plan(), {})
        self.backfill("--apply")
        with self.conn() as c:
            refs = json.loads(c.execute(
                "SELECT refs FROM tasks WHERE kind='followup'").fetchone()["refs"])
        self.assertNotIn(P.DERIVES_FROM, refs)
        del proposal

    def test_a_malformed_ledger_line_does_not_lose_the_rest(self):
        proposal = self.a_proposal()
        item = self.a_followup("the same question again")
        path = os.path.join(self.state, "pr-reviewer")
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "ledger.jsonl"), "w") as handle:
            handle.write("{truncated\n")
            handle.write(json.dumps(self.exited("task:4", proposal,
                                                item["id"])) + "\n")
        self.assertEqual(self.plan()[item["id"]]["verdict"], "link")

    def test_the_first_exit_row_for_a_followup_wins(self):
        proposal = self.a_proposal()
        other = self.a_proposal("a different standing question")
        item = self.a_followup("the same question again")
        self.ledger([self.exited("task:4", proposal, item["id"]),
                     self.exited("task:9", other, item["id"])])
        self.assertEqual(self.plan()[item["id"]]["proposal"], proposal)


if __name__ == "__main__":
    unittest.main(verbosity=2)

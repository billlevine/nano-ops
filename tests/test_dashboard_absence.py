#!/usr/bin/env python3
"""Tests for the dashboard's panel absence contract the contract, gap audit Q-07)."""
from __future__ import annotations

import html as _html
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent / "bin"
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "lib"))

import dashboard_panels as P  # noqa: E402
import dashboard_primary  # noqa: E402
import dashboard_v2  # noqa: E402
import estate_observation as O  # noqa: E402

_loader = SourceFileLoader("_dashboard", str(_HERE / "dashboard"))
_spec = importlib.util.spec_from_loader("_dashboard", _loader)
dash = importlib.util.module_from_spec(_spec)
_loader.exec_module(dash)

NOW = 1_800_000_000.0
HOUR = 3600.0


def text(html: str) -> str:
    """The rendered markup with its tags stripped — what the operator actually reads."""
    return re.sub(r"\s+", " ",
                  _html.unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def banners(html: str) -> tuple:
    """(absent, stale, partly-read) — the three banners, counted."""
    return (len(re.findall(r'class="pabsent"', html)),
            len(re.findall(r'class="pstale"', html)),
            len(re.findall(r'class="ppartial"', html)))


def chip_classes(html: str) -> str:
    m = re.search(r'<div class="pstate[^"]*">\s*<span class="srcchip ([^"]+)"', html)
    return m.group(1) if m else ""


class VocabularyTest(unittest.TestCase):
    """The rule is inherited from the primitive, not re-implemented here."""

    def test_the_dashboard_is_registered_with_the_estates_one_rule(self):
        self.assertIn("dashboard", O.registered())
        self.assertIs(P.O.DASHBOARD, O.vocabulary("dashboard"))

    def test_only_a_look_that_worked_can_prove_a_panel_is_empty(self):
        for outcome in P.OUTCOMES:
            env = P.envelope("pr_board", "src", outcome, now=NOW)
            self.assertEqual(P.empty_is_evidence(env), outcome == P.OUT_OK,
                             f"{outcome} reached the wrong verdict")

    def test_the_predicate_cannot_be_passed_in(self):
        """`envelope()` has no `empty_is_evidence` parameter, the same way
        `reading()` has none. There is nowhere to put a claim a read did not
        earn."""
        with self.assertRaises(TypeError):
            P.envelope("pr_board", "src", P.OUT_ERROR, now=NOW,
                       empty_is_evidence=True)

    def test_an_unregistered_outcome_raises_rather_than_defaulting(self):
        with self.assertRaises(O.UnwarrantedReading):
            P.envelope("pr_board", "src", "probably_fine", now=NOW)

    def test_a_hand_edited_record_cannot_claim_a_warrant(self):
        env = P.envelope("pr_board", "src", P.OUT_ERROR, now=NOW)
        env["empty_is_evidence"] = True          # what a doctored JSON says
        self.assertFalse(P.empty_is_evidence(env))

    def test_the_two_halves_of_the_warrant_have_to_agree(self):
        env = P.envelope("pr_board", "src", P.OUT_UNREADABLE, now=NOW)
        env["status"] = P.COMPLETED              # a status with no outcome behind it
        self.assertFalse(P.empty_is_evidence(env))

    def test_a_partly_read_panel_never_proves_an_absence(self):
        env = P.envelope("pr_board", "src", P.OUT_OK, partial=True, now=NOW)
        self.assertEqual(env["warrant"], O.OBSERVED)
        self.assertFalse(P.empty_is_evidence(env))

    def test_the_broken_and_the_never_attempted_stay_apart(self):
        """`gh is not installed` and `GitHub refused` are different mornings."""
        self.assertEqual(P.envelope("pr_board", "s", P.OUT_UNREADABLE)["status"],
                         P.FAILED)
        self.assertEqual(P.envelope("pr_board", "s", P.OUT_NO_STATE)["status"],
                         P.UNAVAILABLE)


class ThreeStatesTest(unittest.TestCase):
    """Absent, read-and-empty, and stale are three renderings, not one."""

    def setUp(self):
        self.old = dash.STATE
        self.tmp = tempfile.TemporaryDirectory()
        dash.STATE = Path(self.tmp.name)

    def tearDown(self):
        dash.STATE = self.old
        self.tmp.cleanup()

    def board(self, state=None, last_run=None):
        if state is not None:
            (dash.STATE / "pr-tracker").mkdir(parents=True, exist_ok=True)
            (dash.STATE / "pr-tracker" / "state.json").write_text(state)
        return dash.build_pr_board(NOW)

    def empty_board(self, age_seconds=0.0):
        return self.board(json.dumps({
            "entries": {}, "sources": {},
            "last_run": P.iso(NOW - age_seconds)}))

    def test_an_absent_store_is_not_a_clear_board(self):
        board = self.board()
        self.assertEqual(board["buckets"]["waiting_you"], 0)
        env = board["reading"]
        self.assertEqual((env["outcome"], env["warrant"], env["status"]),
                         (P.OUT_NO_STATE, O.NOT_ATTEMPTED, P.UNAVAILABLE))
        html = dash.render_pr_board(board)
        self.assertEqual(banners(html), (1, 0, 0))
        self.assertIn("MISSING, not zero", text(html))

    def test_an_unreadable_store_says_it_broke_not_that_it_is_absent(self):
        env = self.board("{not json")["reading"]
        self.assertEqual((env["outcome"], env["status"]),
                         (P.OUT_UNREADABLE, P.FAILED))

    def test_a_board_that_read_and_found_nothing_carries_no_banner(self):
        board = self.empty_board()
        self.assertTrue(P.empty_is_evidence(board["reading"]))
        html = dash.render_pr_board(board)
        self.assertEqual(banners(html), (0, 0, 0))
        self.assertEqual(chip_classes(html), "s-completed f-current")

    def test_a_stale_board_is_real_data_about_an_earlier_estate(self):
        board = self.empty_board(age_seconds=5 * HOUR)
        html = dash.render_pr_board(board)
        self.assertEqual(banners(html), (0, 1, 0))
        self.assertEqual(chip_classes(html), "s-completed f-stale")
        self.assertIn("real but they describe an earlier estate", text(html))

    def test_the_three_renderings_are_pairwise_different(self):
        absent = dash.render_pr_board(self.board())
        self.tearDown(); self.setUp()
        current = dash.render_pr_board(self.empty_board())
        self.tearDown(); self.setUp()
        stale = dash.render_pr_board(self.empty_board(5 * HOUR))
        for a, b in ((absent, current), (absent, stale), (current, stale)):
            self.assertNotEqual(chip_classes(a), chip_classes(b))
            self.assertNotEqual(banners(a), banners(b))


class EveryPanelTest(unittest.TestCase):
    """Not the PR board alone. Every panel that reads a producer store."""

    def setUp(self):
        self.old = dash.STATE
        self.tmp = tempfile.TemporaryDirectory()
        dash.STATE = Path(self.tmp.name)

    def tearDown(self):
        dash.STATE = self.old
        self.tmp.cleanup()

    def test_a_missing_store_never_reads_as_an_answered_panel(self):
        cases = {
            "pr_board": lambda: dash.build_pr_board(NOW)["reading"],
            "night": lambda: dash.build_night(NOW)["reading"],
            "focus": lambda: dash.build_focus(None, NOW)["reading"],
            "mechanic": lambda: dash.build_mechanic(NOW)["reading"],
            "brief": lambda: dash.build_brief(NOW)["reading"],
            "ledger": lambda: dash.ledger_reading(dash.build_ledger(), NOW),
            "estate_activity":
                lambda: dash.build_estate_activity({"loops": {}}, NOW)["reading"],
        }
        for panel, build in cases.items():
            env = build()
            self.assertEqual(env["panel"], panel)
            self.assertFalse(P.empty_is_evidence(env),
                             f"{panel} claimed an empty store was evidence")
            self.assertTrue(env["why"], f"{panel} did not say why")

    def test_every_rendered_panel_shows_the_absence(self):
        renders = {
            "pr_board": lambda: dash.render_pr_board(dash.build_pr_board(NOW)),
            "night": lambda: dash.render_night(dash.build_night(NOW)),
            "focus": lambda: dash.render_focus(dash.build_focus(None, NOW)),
            "mechanic": lambda: dash.render_mechanic(dash.build_mechanic(NOW)),
            "brief": lambda: dash.render_brief(dash.build_brief(NOW), []),
            "ledger": lambda: dash.render_ledger(
                dash.build_ledger(), dash.ledger_reading([], NOW)),
            "loops": lambda: dash.render_loops(
                [], P.envelope("loops", "agent-deck ls --json", P.OUT_NO_TOOL,
                               now=NOW, why="agent-deck is not on PATH")),
            "dispatch_sessions": lambda: dash.render_dispatch_sessions(
                [], P.envelope("dispatch_sessions", "agent-deck ls --json + state/hub/dispatches.json", P.OUT_ERROR,
                               now=NOW, why="agent-deck ls exited 1")),
        }
        for panel, render in renders.items():
            html = render()
            self.assertEqual(banners(html)[0], 1,
                             f"{panel} rendered no absence banner")
            self.assertIn("could not be read", text(html))

    def test_the_reassuring_empty_states_are_withheld_when_nobody_looked(self):
        """"Queue is empty" and "No pending proposals" are claims about the
        estate. A store nobody could read must not make them."""
        for html, claim in (
                (dash.render_night(dash.build_night(NOW)), "Queue is empty"),
                (dash.render_mechanic(dash.build_mechanic(NOW)),
                 "No pending proposals"),
                (dash.render_focus(dash.build_focus(None, NOW)),
                 "focus_projects.py refresh"),
                (dash.render_dispatch_sessions([], P.envelope(
                    "dispatch_sessions",
                    "agent-deck ls --json + state/hub/dispatches.json",
                    P.OUT_NO_TOOL, now=NOW)),
                 "No dispatch sessions in flight")):
            self.assertNotIn(claim, text(html))

    def test_a_panel_covers_every_region_of_the_page(self):
        """PANELS and the shell's own section list are the same set, minus the
        estate store — which has no section of its own on the primary page and
        drives the proposal panel there instead."""
        self.assertEqual(set(P.PANELS) - {"estate_activity"},
                         set(dash.SHELL_SECTIONS))

    def test_a_deliberate_boundary_is_not_reported_as_a_hole(self):
        """A loop no longer in loops.toml was owed no look. `not_due`, not the
        `not_attempted` that would send an operator chasing a producer."""
        env = dash.build_focus({"loops": {}}, NOW)["reading"]
        self.assertEqual((env["outcome"], env["warrant"]),
                         (P.OUT_NOT_CONFIGURED, O.NOT_DUE))
        env = dash.build_focus({"loops": {"focus-projects": {}}}, NOW)["reading"]
        self.assertEqual((env["outcome"], env["warrant"]),
                         (P.OUT_NO_STATE, O.NOT_ATTEMPTED))

    def test_an_unreadable_dispatch_tracker_only_degrades_the_fleet(self):
        """The cards still render — slug, status, age are all from the deck —
        so this is `observed` and partial, never a warrant class."""
        (dash.STATE / "hub").mkdir(parents=True)
        (dash.STATE / "hub" / "dispatches.json").write_text("{oops")
        _, outcome, _ = dash.read_dispatches()
        self.assertEqual(outcome, P.OUT_UNREADABLE)
        env = P.envelope("dispatch_sessions", "agent-deck ls --json + state/hub/dispatches.json", P.OUT_OK,
                         partial=True, as_of=NOW, now=NOW)
        self.assertEqual(env["warrant"], O.OBSERVED)
        self.assertFalse(P.empty_is_evidence(env))
        html = dash.render_dispatch_sessions([], env)
        self.assertIn("only partly read", text(html))
        self.assertIn("What is missing", text(html))
        self.assertNotIn("No dispatch sessions in flight", text(html))

    def test_a_ledger_with_bytes_and_no_parseable_line_is_not_a_quiet_estate(self):
        (dash.STATE / "ledger.jsonl").write_text("not json at all\n")
        env = dash.ledger_reading(dash.build_ledger(), NOW)
        self.assertEqual(env["outcome"], P.OUT_UNREADABLE)


# ── part 2: every tracked item is reachable ─────────────────────────────────

def entry(eid, key, waiting_on, *, flags=(), note=None, snoozed=False,
          observed="2026-08-15T05:33:19+00:00"):
    return {
        "id": eid, "linear": {},
        "pr": {"key": key, "repo": key.split("#")[0],
               "number": int(key.split("#")[1]), "title": f"title for {key}",
               "url": f"https://github.com/{key.replace('#', '/pull/')}",
               "state": "OPEN", "updatedAt": "2026-08-15T04:00:00Z"},
        "manual": {"note": note},
        "last_successful_observation_at": observed,
        "derived": {"waiting_on": waiting_on, "flags": list(flags),
                    "idle_days": 1.0, "age_days": 3.0, "snoozed": snoozed},
    }


class ReachableTest(unittest.TestCase):
    """Sixteen tracked items, twelve rendered rows, and four of them appearing
    only as a tile number — not even in the data model, so nothing downstream
    could have rendered them either."""

    def setUp(self):
        self.old = dash.STATE
        self.tmp = tempfile.TemporaryDirectory()
        dash.STATE = Path(self.tmp.name)
        (dash.STATE / "pr-tracker").mkdir()
        entries = {}
        for i in range(14):                      # act-first, past ACTFIRST_CAP
            entries[f"mine-{i}"] = entry(f"mine-{i}", f"example/widget#{100 + i}", "me")
        for i in range(6):                       # a count and nothing else
            entries[f"rev-{i}"] = entry(f"rev-{i}", f"example/tool#{200 + i}",
                                        "my_review", flags=("review-requested",))
        entries["snz"] = entry("snz", "example/tool#900", "me", snoozed=True)
        entries["odd"] = entry("odd", "example/tool#901", None)   # in no tile at all
        (dash.STATE / "pr-tracker" / "state.json").write_text(json.dumps(
            {"entries": entries, "sources": {}, "last_run": P.iso(NOW)}))
        self.board = dash.build_pr_board(NOW)
        self.html = dash.render_pr_board(self.board)

    def tearDown(self):
        dash.STATE = self.old
        self.tmp.cleanup()

    def test_every_non_snoozed_entry_is_in_the_model(self):
        self.assertEqual(self.board["tracked"], 21)
        self.assertEqual({r["id"] for r in self.board["all_rows"]},
                         {f"mine-{i}" for i in range(14)}
                         | {f"rev-{i}" for i in range(6)} | {"odd"})

    def test_a_snoozed_entry_is_deliberately_not_listed(self):
        self.assertNotIn("snz", {r["id"] for r in self.board["all_rows"]})
        self.assertIn("1 snoozed and deliberately not listed", text(self.html))

    def test_every_non_snoozed_entry_reaches_the_rendered_page(self):
        for r in self.board["all_rows"]:
            self.assertIn(r["key"], self.html, f"{r['key']} is on no row")

    def test_a_count_with_no_row_behind_it_is_gone(self):
        """The six `your review` PRs used to be the number 6 and nothing else."""
        self.assertEqual(self.board["buckets"]["my_review"], 6)
        for i in range(6):
            self.assertIn(f"example/tool#{200 + i}", self.html)

    def test_the_act_first_overflow_is_reachable_too(self):
        shown = min(len(self.board["act_first"]), dash.ACTFIRST_CAP)
        self.assertLess(shown, len(self.board["act_first"]))
        self.assertEqual(self.html.count('<div class="pall">'),
                         self.board["tracked"])

    def test_every_row_carries_its_state_and_its_observation_time(self):
        for row in self.html.split('<div class="pall">')[1:]:
            body = text(row.split("</div>")[0])
            self.assertRegex(body, r"last observed \d{4}-\d{2}-\d{2}")
            self.assertRegex(
                body,
                r"waiting on you|your review|going astray|on reviewers"
                r"|in progress|unbucketed")

    def test_an_entry_in_no_tile_is_labelled_rather_than_dropped(self):
        odd = next(r for r in self.board["all_rows"] if r["id"] == "odd")
        self.assertIsNone(odd["bucket"])
        self.assertEqual(odd["bucket_label"], "unbucketed")
        self.assertIn("example/tool#901", self.html)

    def test_a_row_with_no_observation_time_says_so_rather_than_faking_one(self):
        (dash.STATE / "pr-tracker" / "state.json").write_text(json.dumps(
            {"entries": {"x": entry("x", "example/tool#1", "reviewers",
                                    observed=None)},
             "sources": {}, "last_run": P.iso(NOW)}))
        html = dash.render_pr_board(dash.build_pr_board(NOW))
        self.assertIn("no recorded observation time", text(html))


# ── part 3: the divergence reaches the page that is served ──────────────────

class DivergenceTest(unittest.TestCase):
    """`board_moved` was True in the live artifact and appeared on no rendered
    surface. `/` owns its digest panel and never renders `sections.brief`."""

    def brief(self, moved):
        return {"available": True, "name": "2026-08-15.md", "body": "# hi\n",
                "delivery": None, "manifest": {"legacy": True, "sources": {}},
                "board_moved": moved, "reports": [],
                "reading": P.envelope("brief", "state/morning-brief/reports/",
                                      P.OUT_OK, as_of=NOW, now=NOW)}

    def test_the_sentence_exists_once(self):
        """Three renderers say it. One constant is what keeps them agreeing —
        so the words appear in exactly one place in the source, and the two
        presentation layers reach it by name."""
        self.assertIn("has changed since this brief read it",
                      dash.BOARD_MOVED_NOTE)
        needle = "has changed since this brief read it"
        sources = {name: (_ROOT / name).read_text(encoding="utf-8")
                   for name in ("bin/dashboard", "lib/dashboard_primary.py",
                                "lib/dashboard_v2.py")}
        self.assertEqual(sources["bin/dashboard"].count(needle), 1)
        self.assertEqual(sources["lib/dashboard_primary.py"].count(needle), 0)
        self.assertEqual(sources["lib/dashboard_v2.py"].count(needle), 0)

    def test_the_shared_section_says_it(self):
        self.assertIn(dash.BOARD_MOVED_NOTE,
                      text(dash.render_brief(self.brief(True), [])))
        self.assertNotIn(dash.BOARD_MOVED_NOTE,
                         text(dash.render_brief(self.brief(False), [])))

    def test_the_payload_hands_it_to_the_renderers_that_own_the_panel(self):
        """Pre-rendered, so a page with its own digest panel shows the same
        bytes rather than a second implementation of the sentence."""
        snap = {"brief": self.brief(True), "panels": {}}
        state = dash.brief_panel_state(snap["brief"])
        self.assertIn(dash.BOARD_MOVED_NOTE, text(state))

    def test_the_served_primary_page_renders_it(self):
        js = dashboard_primary.PRIMARY_JS
        self.assertIn("panel_state_html", js)
        self.assertIn("PANELSTATE+tabs", js)
        page = dashboard_primary.render(dash, _snapshot_stub())
        # …and the digest panel really is unmanaged on this page, which is the
        # whole reason the sentence never reached it.
        managed = json.loads(
            re.search(r"window\.OPS_DASH_SECTIONS=(\{.*?\});", page).group(1))
        self.assertNotIn("brief", managed)
        self.assertNotIn("mechanic", managed)
        self.assertIn("panel_state_html", page)
        self.assertIn("proposal-state", page)

    def test_v2_names_it_from_the_same_constant(self):
        page = dashboard_v2.render(dash, _snapshot_stub())
        self.assertIn(json.dumps(dash.BOARD_MOVED_NOTE), page)
        self.assertIn("OPS_BOARD_MOVED_NOTE", dashboard_v2.JS)

    def test_v2_reads_the_warrants_rather_than_a_remembered_list(self):
        self.assertIn("D.panels", dashboard_v2.JS)
        self.assertIn("empty_is_evidence===false", dashboard_v2.JS)


def _snapshot_stub() -> dict:
    return {"estate": "operations", "operator": "ops"}


if __name__ == "__main__":
    unittest.main(verbosity=2)

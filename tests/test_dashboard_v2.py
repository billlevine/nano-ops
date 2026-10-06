#!/usr/bin/env python3
"""Tests for the dashboard-v2 page the contract."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import dashboard_v2  # noqa: E402


class _StubDashboard:
    """render() reaches only for the shared CSS/JS/favicon of the daily page,"""
    CSS = "/*shared*/"
    JS = "/*shared-js*/"
    FAVICON = "data:image/svg+xml;base64,AAAA"
    BOARD_MOVED_NOTE = "the PR board above has changed since this brief read it"


def page():
    return dashboard_v2.render(_StubDashboard(),
                               {"estate": "operations", "operator": "ops"})


class ShellTest(unittest.TestCase):
    def test_page_is_self_contained_and_names_the_estate(self):
        html = page()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("operations / ops — dashboard v2", html)
        self.assertIn("/*shared*/", html)       # base tokens, not a new identity
        self.assertIn("/*shared-js*/", html)    # theme toggle, reused
        self.assertNotIn("http://", html.replace('href="/"', ""))
        self.assertNotIn("https://", html)

    def test_carries_no_data_of_its_own(self):
        """A shell that inlined a number would go stale the moment it was
        written; every dynamic region is an empty container the client fills
        from dashboard.json."""
        html = page()
        for ident in ("usage", "vitals", "sec-loops", "v2-blind", "v2-today",
                      "v2-machine", "v2-gen", "v2-host"):
            self.assertIn(f'id="{ident}"', html)
        markup = html[html.index('id="v2-today"'):html.index("<script")]
        self.assertNotIn("<div class=\"it\">", markup)

    def test_the_two_doors_are_one_page_with_both_ends_reachable(self):
        html = page()
        self.assertIn('<section id="today">', html)
        self.assertIn('<section id="machine">', html)
        self.assertIn('data-jump="today"', html)
        self.assertIn('data-jump="machine"', html)
        # additive, not a replacement: both existing surfaces stay one click away
        self.assertIn('href="/"', html)
        self.assertIn('href="/estate.html"', html)

    def test_deep_inspection_is_linked_not_rebuilt(self):
        """the operator deprioritized a new deep-inspect surface; the machine room
        summarises and hands off to the page that already has the detail."""
        html = page()
        self.assertIn("Full detail → /estate.html", html)
        self.assertNotIn("proposal-stage", html)   # no write path lives here


class SingleDataPathTest(unittest.TestCase):
    def test_one_fetch_of_the_shared_snapshot_and_nothing_else(self):
        js = dashboard_v2.JS
        self.assertEqual(js.count("fetch("), 1, "one data path, not two")
        self.assertIn("fetch('dashboard.json", js)
        self.assertIn("setInterval(refresh,30000)", js)

    def test_the_top_strip_is_the_primary_pages_own_rendering(self):
        """render_usage / render_vitals / render_loops output, dropped in as
        served. Rebuilding the cap line here is how two pages start disagreeing
        about the same number."""
        js = dashboard_v2.JS
        self.assertIn("put('usage',D.usage_html)", js)
        self.assertIn("put('vitals',D.vitals_html)", js)
        self.assertIn("put('sec-loops',(D.sections||{}).loops)", js)
        for reimplemented in ("weekly_pct", "session_pct", "cost_usd",
                              "model_pretty", "interval_seconds", "cache_read"):
            self.assertNotIn(reimplemented, js,
                             "the masthead numbers are rendered once, server-side")


class TodayPredicateTest(unittest.TestCase):
    """Each question is answered by a field the store actually holds. The
    mockups' `classify_ask()` keyword guess is deliberately not ported."""

    def test_every_bucket_names_the_predicate_it_used(self):
        js = dashboard_v2.JS
        self.assertIn("estate: status = needs-owner · proposal stage = pending-review", js)
        self.assertIn("spotter: your court or astray · night shift: needs-you", js)
        self.assertIn("open inbox-message tasks · workers still in flight", js)
        self.assertIn("the rest of the attention set, oldest movement first", js)

    def test_buckets_read_stored_fields(self):
        js = dashboard_v2.JS
        self.assertIn("t.status!=='needs-owner'", js)
        self.assertIn("t.stage==='pending-review'", js)
        self.assertIn("q.status!=='needs-you'", js)
        self.assertIn("EA.inbox_messages", js)
        self.assertIn("EA.attention", js)

    def test_a_pr_row_carries_the_boards_reason_sentence(self):
        """the operator's "why is this still there" line (2026-08-14) is the same
        snapshot field the primary page renders — read off `r.why`, never
        rebuilt from the flags here. Every other item kind omits it, so the
        row stays one line tall."""
        js = dashboard_v2.JS
        self.assertIn("why:r.why||''", js)
        self.assertIn("o.why?'<span class=\"iwhy\">'+e(o.why)+'</span>':''", js)
        self.assertIn(".it .iwhy{", dashboard_v2.CSS)

    def test_no_keyword_classification_of_what_an_item_wants(self):
        js = dashboard_v2.JS
        self.assertNotIn("classify_ask", js)
        self.assertNotIn("classify_cluster", js)
        # nothing pattern-matches an item's TITLE to decide which bucket it is
        self.assertFalse(re.search(r"t\.title\s*\.\s*(match|indexOf|test)", js))

    def test_an_item_lands_in_exactly_one_question(self):
        js = dashboard_v2.JS
        self.assertIn("function claim(id){if(!id||SEEN[id])return false;SEEN[id]=1;return true}", js)
        # ...and the last bucket takes whatever the earlier ones did not, so no
        # attention item can fall off Today entirely.
        self.assertIn("ids(EA.attention).filter(function(t){return claim(t.id)})", js)

    def test_a_withheld_row_is_counted_out_loud(self):
        js = dashboard_v2.JS
        self.assertIn("show '+rest+' more", js)
        self.assertIn("rest=items.length-CAP", js)

    def test_a_count_without_rows_says_so(self):
        """`my_review` exists on the board only as a number (bin/dashboard's
        act-first list is astray + waiting-on-you). Silently omitting it is
        how a page teaches its reader that the section is complete."""
        js = dashboard_v2.JS
        self.assertIn("function stoppedNote()", js)
        self.assertIn("the count for those, not the rows", js)


class DegradationTest(unittest.TestCase):
    """A degraded read degrades the SUMMARY, not just the section it came
    from — the whole point of the line above Today."""

    def test_blindness_line_covers_every_input(self):
        js = dashboard_v2.JS
        for signal in ("the regenerator is not keeping up",   # snapshot age
                       "last ticked",                          # loop heartbeat
                       "the spotter could not re-read",        # P-03
                       "spotter source unreadable",
                       "it was not delivered",                 # P-10
                       "no source manifest",                   # P-11
                       "could not answer",                     # per-gatherer
                       "the estate store could not be read"):
            self.assertIn(signal, js)

    def test_missing_is_not_reported_as_zero(self):
        js = dashboard_v2.JS
        self.assertIn("every count below is missing, not zero", js)

    def test_the_page_ages_where_it_sits(self):
        """Ages are computed against the current clock on a 1s tick, so a tab
        left open says it has gone cold instead of freezing its numbers."""
        js = dashboard_v2.JS
        self.assertIn("setInterval(tick,1000)", js)
        self.assertIn("stale — regenerator lagging", js)
        self.assertIn("offline — retrying", js)


class MachineRoomTest(unittest.TestCase):
    def test_attention_is_counted_here_and_rendered_on_today(self):
        js = dashboard_v2.JS
        self.assertIn("every one of these is rendered on Today above; here it is only counted", js)

    def test_summary_covers_the_estates_subsystems(self):
        js = dashboard_v2.JS
        for card in ("Loops", "Dispatched workers", "Night shift", "PR board",
                     "Mechanic", "Briefer", "Work store", "Attention set",
                     "Proposals", "Structure", "Extraction", "Focus", "Ledger"):
            self.assertIn(f"mcard('{card}'", js)

    def test_ledger_tail_is_the_newest_entries(self):
        """dashboard.json's ledger is newest-first; slicing from the end shows
        the oldest rows in the tail and calls them 'latest'."""
        js = dashboard_v2.JS
        self.assertIn("(D.ledger||[]).slice(0,6)", js)
        self.assertNotIn(".slice(-6)", js)


class GeneratorWiringTest(unittest.TestCase):
    def test_the_v2_page_is_written_by_the_same_run_as_the_others(self):
        """the contract moved the renderer list out of bin/dashboard-index and into"""
        src = (ROOT / "bin" / "dashboard").read_text(encoding="utf-8")
        self.assertIn("import dashboard_v2", src)
        self.assertIn('(STATE / "dashboard-v2.html", dashboard_v2.render(me, snap))',
                      src)

        index = (ROOT / "bin" / "dashboard-index").read_text(encoding="utf-8")
        self.assertIn("dashboard.write_shells(dashboard.build_snapshot())", index)
        self.assertNotIn("dashboard_v2", index,
                         "v2 must not get a second write path here")
        # one snapshot for all three renderers — a second build is the same
        # bytes at full cost
        self.assertEqual(index.count("build_snapshot()"), 1)

    def test_the_route_is_on_the_servers_allowlist(self):
        src = (ROOT / "bin" / "dashboard-server").read_text(encoding="utf-8")
        self.assertIn('"/dashboard-v2":', src)
        self.assertIn('"/dashboard-v2.html":', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

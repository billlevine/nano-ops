#!/usr/bin/env python3
"""Tests for the primary dashboard presentation module."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import dashboard_estate  # noqa: E402
import dashboard_primary  # noqa: E402


class FakeDashboard:
    def __init__(self):
        self.kwargs = None

    def render_shell(self, snapshot, **kwargs):
        self.kwargs = kwargs
        return f"page:{snapshot['id']}"


class PrimaryRendererTest(unittest.TestCase):
    def test_composes_through_shared_shell_hooks(self):
        dashboard = FakeDashboard()
        self.assertEqual(dashboard_primary.render(dashboard, {"id": 7}), "page:7")
        self.assertEqual(dashboard.kwargs["unmanaged_sections"],
                         ("brief", "mechanic"))
        self.assertIn("digest-tabs", dashboard.kwargs["extra_css"])
        self.assertIn("ops-digest-checks-v1", dashboard.kwargs["extra_js"])
        self.assertEqual(dashboard.kwargs["brief_tick"],
                         "recent briefs · locally checkable")
        # Page links come from the shared nav bar in the shell, not from
        # links this layer injects into the masthead.
        self.assertNotIn("estate-ops-link", dashboard.kwargs["extra_js"])

    def test_landed_opened_digest_has_scannable_repo_and_pr_list_markup(self):
        js = dashboard_primary.PRIMARY_JS
        css = dashboard_primary.PRIMARY_CSS
        self.assertIn("function activityHtml(sec)", js)
        self.assertIn("split(/\\s+·\\s+/)", js)
        self.assertIn('class="digest-pr-list"', js)
        self.assertIn('class="digest-repo"', js)
        self.assertIn(".digest-pr-list li", css)

    def test_v1_reuses_estate_proposal_review_primitives(self):
        js = dashboard_primary.proposal_review_js()
        self.assertIn(dashboard_estate.JS_PROPOSALS, js)
        self.assertIn("Proposal review", js)
        self.assertIn("Needs decision", js)
        self.assertIn("document.addEventListener('ops-dashboard-data'", js)
        self.assertIn("fetch('proposal-stage'", js)
        self.assertNotIn("fetch('dashboard.json", js)

    def test_primary_css_embeds_the_estate_owned_proposal_rules(self):
        self.assertIn(dashboard_estate.PROPOSAL_CSS,
                      dashboard_primary.PRIMARY_CSS)

    def test_the_v1_panel_widens_with_the_estate_page_and_not_separately(self):
        """`/` renders its own proposals panel out of the SAME"""
        js = dashboard_primary.proposal_review_js()
        self.assertIn("function kindBadge(t)", js)
        self.assertIn("[t.id,t.kind,t.title", js)
        self.assertIn("No staged review items in this stage.", js)
        self.assertEqual(js.count("function drawProposals()"), 1,
                         "one renderer, embedded — never a second one here")
        self.assertIn('placeholder="Filter by id, kind or text…"', js)

    def test_the_v1_panel_ships_no_unsubstituted_placeholder(self):
        """the contract, and the reason this assertion is on the PAGE and not on the"""
        js = dashboard_primary.proposal_review_js()
        self.assertEqual(re.findall(r"__[A-Z][A-Z0-9_]*__", js), [])
        self.assertIn('var SURFACE_CANONICAL={', js)
        for css in (dashboard_primary.PRIMARY_CSS, dashboard_primary.PRIMARY_JS):
            self.assertEqual(re.findall(r"__[A-Z][A-Z0-9_]*__", css), [])


class LazyDrilldownAdapterTest(unittest.TestCase):
    """this panel's proposal bodies show history, and the snapshot"""

    def test_the_shared_drilldown_block_is_embedded(self):
        js = dashboard_primary.proposal_review_js()
        self.assertIn("function ensureDetail(id)", js)
        self.assertIn("function historySlot(id,msg)", js)
        self.assertNotIn("function taskEvents(id){var m=DATA.task_events;return m&&m[id]?m[id]:[]}",
                         js, "the old always-embedded reader must be gone")

    def test_it_asks_for_rows_a_snapshot_rendered_open(self):
        """Parsing an `open` attribute fires no toggle event."""
        js = dashboard_primary.proposal_review_js()
        self.assertIn("ensureOpenDetails();", js)
        self.assertIn("installDetailWiring();", js)

    def test_a_fetched_history_survives_the_refresh_here_too(self):
        js = dashboard_primary.proposal_review_js()
        self.assertIn("reindexDetailEvents();", js)
        self.assertLess(js.index("EVENT_INDEX={};(DATA.events||[])"),
                        js.index("reindexDetailEvents();"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for /estate.html's renderer (lib/dashboard_estate.py).

Run: python3 bin/test_dashboard_estate.py

The path bootstrap below is not decoration. Every other test in this
directory runs as `python3 bin/test_<thing>.py`, which puts `bin/` on
`sys.path` and the repo root nowhere — so a bare `from lib import ...` raises
ModuleNotFoundError and the file is only green under a `PYTHONPATH` its own
header never asked for. It sat that way from 2026-08-07, passing for anyone
who ran the whole suite through a runner and failing for anyone who ran it the
documented way. `bin/estate` does the same two lines for the same reason.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import dashboard_estate  # noqa: E402  (path established above)


class _StubDashboard:
    """render() only reaches for the shared CSS/JS of the daily page."""
    CSS = ""
    JS = ""


def page():
    return dashboard_estate.render(_StubDashboard(),
                                   {"estate": "ops", "operator": "hub"})


class EstatePageTest(unittest.TestCase):
    def test_renderer_bootstrap_is_complete(self):
        js = dashboard_estate.JS.strip()
        self.assertTrue(js.endswith("})();"))
        self.assertIn("fetch('dashboard.json", js)
        self.assertIn("setInterval(refresh,30000)", js)

    def test_refresh_preserves_panel_state(self):
        js = dashboard_estate.JS
        self.assertIn("MEMORY_SCOPE", js)
        self.assertIn("MEMORY_SCROLL", js)
        self.assertIn("[data-task][open]", js)
        self.assertIn("[data-event][open]", js)
        self.assertIn("[data-project][open]", js)

    def test_task_deep_link_opens_the_row_and_moves_to_all(self):
        """A terminal target must not disappear behind the Active default."""
        js = dashboard_estate.JS
        self.assertIn("new URLSearchParams(window.location.search)", js)
        self.assertIn("FILTER='all'", js)
        self.assertIn("OPEN_TASKS[id]=true", js)
        self.assertIn("setActiveFilter('data-filter','all')", js)
        self.assertIn("node.scrollIntoView({block:'center'})", js)
        render = js[js.index("function render(d)"):]
        self.assertLess(render.index("applyDeepLink();"),
                        render.index("drawAttention();"))
        self.assertLess(render.index("ensureOpenDetails();"),
                        render.index("revealDeepLink();"))

    def test_missing_deep_link_has_an_explicit_current_snapshot_state(self):
        markup, js = page(), dashboard_estate.JS
        self.assertIn('id="deep-link-state"', markup)
        self.assertIn('role="status"', markup)
        self.assertIn("not found in the current snapshot", js)
        self.assertIn("deep-link-state not-found", js)
        self.assertIn(".deep-link-state.not-found{", dashboard_estate.CSS)

    def test_project_and_legacy_followup_deep_links_resolve(self):
        js = dashboard_estate.JS
        self.assertIn("p.get('task')", js)
        self.assertIn("p.get('project')", js)
        self.assertIn("f.legacy_id", js)
        self.assertIn("OPEN_PROJECTS[id]=true", js)


    def test_task_drilldown_reads_the_complete_pregrouped_history(self):
        """P-16 — the tail filter is a fallback for old snapshots only."""
        js = dashboard_estate.JS
        self.assertIn("DATA.task_events", js)
        self.assertIn("DATA.task_events_source", js)
        self.assertIn("function taskEvents(id)", js)
        head = js[js.index("function taskEvents(id)"):]
        head = head[:head.index("\n")]
        self.assertIn("if(m&&m[id])return m[id]", head,
                      "the drill-down must prefer the pre-grouped map")
        self.assertIn("if(m)return []", head,
                      "a snapshot WITH the map must never fall back to the tail")

    def test_a_drilldown_not_yet_fetched_is_null_and_never_empty(self):
        """the one thing that makes the fetch safe. `[]` is a"""
        js = dashboard_estate.JS
        head = js[js.index("function taskEvents(id)"):]
        head = head[:head.index("\n")]
        self.assertIn("if(DATA.task_events_source)return null", head)
        self.assertLess(head.index("if(DATA.task_events_source)return null"),
                        head.index("if(m)return []"))


    def test_blocked_tasks_render_their_blockers(self):
        """P-18 — visible, and never mistaken for a status."""
        js = dashboard_estate.JS
        self.assertIn("t.blocked_by", js)
        self.assertIn("blockedBadge", js)
        self.assertIn("is-blocked", js)
        self.assertIn("blocked by", js)          # spelled out in the details
        self.assertIn(".blocked{", dashboard_estate.CSS)
        # The status span still renders the STORED status, unmodified.
        self.assertIn("<span class=\"status\">\'+e(t.status)+\'</span>", js)


    def test_the_review_stage_is_shown_and_is_not_a_status(self):
        """P-02 — a pending proposal and an approved one are both `ready`
        rows. Without the badge the board cannot tell them apart, which is
        the whole retained-review-state requirement."""
        js = dashboard_estate.JS
        self.assertIn("stageBadge", js)
        self.assertIn("t.stage", js)
        self.assertIn("review stage", js)
        self.assertIn(".stage{", dashboard_estate.CSS)
        self.assertIn(".stage.approved-backlog{", dashboard_estate.CSS)
        # Same rule as blocked_by: the status span still renders the STORED
        # status, and the stage sits alongside it rather than replacing it.
        self.assertIn("<span class=\"status\">\'+e(t.status)+\'</span>", js)

    def test_the_readiness_badge_rides_both_lists_awaiting_bill(self):
        """the attention set AND the review queue, one badge."""
        js = dashboard_estate.JS
        self.assertIn("function readinessBadge(t)", js)
        self.assertEqual(js.count("function readinessBadge(t)"), 1)
        self.assertIn("t.readiness", js)
        self.assertIn("t.readiness_why", js)
        for state in ("reach", "thinking", "unclassified"):
            self.assertIn(state + ":[", js, state)
        # Both rows call it.
        attention = js[js.index("function attentionRow(t,tier)"):]
        self.assertIn("readinessBadge(t)", attention[:attention.index("\n")])
        proposal = js[js.index("function proposalRow(t)"):]
        self.assertIn("readinessBadge(t)", proposal[:proposal.index("\n")])
        # Nothing is re-derived here. The browser reads the verdict; it does
        # not re-test the recommendation, which is what would let the badge
        # and `--ready-only` disagree about one row.
        fn = js[js.index("function readinessBadge(t)"):]
        fn = fn[:fn.index("\n")]
        for rederivation in ("not recorded", "recommendation", "status",
                             "stage"):
            self.assertNotIn(rederivation, fn, rederivation)

    def test_an_unjudged_row_gets_no_badge_rather_than_a_default_one(self):
        """docs/absence-contract.md at a rendering boundary. Only two lists
        carry a verdict; the History toggle's resolved follow-ups are outside
        both, and stamping them `unclassified` would report an absence
        somebody looked for as one somebody found."""
        js = dashboard_estate.JS
        fn = js[js.index("function readinessBadge(t)"):]
        fn = fn[:fn.index("\n")]
        self.assertIn("if(!v)return ''", fn)

    def test_the_readiness_badge_reaches_the_primary_pages_copy_too(self):
        """`/` embeds JS_PROPOSALS and the bounded proposal-review CSS span
        WITHOUT JS_ATTENTION, so a helper defined beside the attention row
        would be undefined there — a ReferenceError that blanks the whole
        proposal panel on one of the two pages."""
        self.assertIn("function readinessBadge(t)",
                      dashboard_estate.JS_PROPOSALS)
        self.assertIn(".readiness-badge{", dashboard_estate.PROPOSAL_CSS)
        self.assertIn(".readiness-badge.reach{", dashboard_estate.PROPOSAL_CSS)

    def test_the_recurring_condition_disposition_is_visible(self):
        js = dashboard_estate.JS
        self.assertIn("recurBadge", js)
        self.assertIn("condition observed again", js)

    def test_the_stage_panel_is_wired(self):
        self.assertIn("proposal-stats", dashboard_estate.JS)
        self.assertIn("DATA.proposal_counts", dashboard_estate.JS)
        markup = page()
        self.assertIn('id="proposal-stats"', markup)
        self.assertIn('<span class="k">Proposal review</span>', markup)
        for stage in ("pending-review", "approved-backlog", "rejected",
                      "stopped", "resolved"):
            self.assertIn(f'data-proposal-stage="{stage}"', markup)

    def test_the_panel_admits_a_staged_task_of_any_kind(self):
        """the membership test is the stage, all the way through."""
        js = dashboard_estate.JS
        draw = js[js.index("function drawProposals()"):]
        draw = draw[:draw.index("\n")]
        self.assertIn("(DATA.proposals||[]).map(task)", draw)
        for narrowing in ("t.kind===", "t.kind==", "kind==='proposal'"):
            self.assertNotIn(narrowing, draw,
                             f"{narrowing} would re-close the panel on kind")
        self.assertIn("t.stage===PROPOSAL_STAGE", draw,
                      "the stage tab is the one filter this panel applies")

    def test_the_panel_search_reaches_the_kind_bill_went_to_the_cli_for(self):
        """The affordance he lacked, on the page: typing `followup` in the
        filter box answers the question `--kind followup` answered."""
        draw = dashboard_estate.JS[
            dashboard_estate.JS.index("function drawProposals()"):]
        draw = draw[:draw.index("\n")]
        self.assertIn("[t.id,t.kind,t.title", draw)

    def test_a_row_that_is_not_a_proposal_says_so(self):
        """Option 1's own accepted consequence: the panel's set widened, so a
        row has to distinguish the kinds it now holds. Only the exceptions get
        the badge — `proposal` on all of them is decoration."""
        js = dashboard_estate.JS
        badge = js[js.index("function kindBadge(t)"):]
        badge = badge[:badge.index("\n")]
        self.assertIn("k!=='proposal'", badge)
        self.assertIn('review-badge kind', badge)
        self.assertIn("e(k)", badge, "an unknown kind is escaped, not trusted")
        self.assertIn("e(t.title)+kindBadge(t)+reviewBadges(p)", js,
                      "the badge rides the collapsed row, not just the body")
        self.assertIn(".review-badge.kind{", dashboard_estate.CSS)
        self.assertIn(".review-badge.kind{", dashboard_estate.PROPOSAL_CSS,
                      "the V1 page embeds the bounded block and needs it too")

    def test_the_empty_state_does_not_claim_the_set_is_only_proposals(self):
        self.assertIn("No staged review items in this stage.",
                      dashboard_estate.JS)

    def test_the_shared_block_leaves_no_placeholder_for_an_embedder_to_fill(self):
        """JS_PROPOSALS is embedded byte-for-byte by TWO pages, and"""
        self.assertNotIn("__SURFACE_CANONICAL__", dashboard_estate.JS_PROPOSALS)
        self.assertNotIn("__SURFACE_CANONICAL__", dashboard_estate.JS)
        self.assertIn('"recommendation": "desired_outcome"',
                      dashboard_estate.JS_PROPOSALS)

    def test_no_render_time_placeholder_survives_into_the_shipped_script(self):
        """The general form of the bug above, checked once for every block.

        `__NAME__` is this module's convention for "the server fills this in",
        and one left behind is not a visible hole in the page — it is a bare
        identifier, so the browser parses the file happily and throws the
        moment the statement runs, taking the rest of the IIFE with it. Every
        composed script is scanned rather than the one that was forgotten.
        """
        pattern = re.compile(r"__[A-Z][A-Z0-9_]*__")
        for name in ("JS_HEAD", "JS_ATTENTION", "JS_INBOX", "JS_PROJECTS",
                     "JS_DEPS", "JS_PROPOSALS", "JS_BODY", "JS_TAIL", "JS",
                     "CSS", "PROPOSAL_CSS"):
            with self.subTest(block=name):
                left = pattern.findall(getattr(dashboard_estate, name))
                self.assertEqual(left, [],
                                 f"{name} ships an unsubstituted placeholder")

    def test_proposal_review_fields_are_labeled_for_the_reviewer(self):
        js = dashboard_estate.JS
        self.assertIn("p.condition", js)
        self.assertIn("p.desired_outcome", js)
        self.assertIn("p.completion_check", js)
        self.assertIn("p.fingerprint", js)
        for label in ("What was observed", "Recommendation", "Done when"):
            self.assertIn(label, js)

    def test_collapsed_proposals_show_recommendation_and_review_badges(self):
        js = dashboard_estate.JS
        self.assertIn("'proposal-recommendation'", js)
        self.assertIn("surfaceText(p,'recommendation')", js)
        self.assertIn("incomplete record", js)
        self.assertIn("recurring ×", js)

    def test_the_collapsed_proposal_row_carries_the_question(self):
        """the contract — the decision, not the title and a clamp of one field."""
        js = dashboard_estate.JS
        self.assertIn("surfaceText(p,'question')", js)
        self.assertIn("summaryField('Question'", js)
        self.assertIn("summaryField('Recommendation'", js)
        # The title is still there, demoted to the caption that identifies the
        # row — the id alone is not a name anyone can scan.
        self.assertIn('class="proposal-caption"', js)

    def test_the_row_never_decides_which_field_is_the_question(self):
        """The precedence lives in lib/estate_work; JS reads `*_field`."""
        js = dashboard_estate.JS
        self.assertIn("p[name+'_field']", js)
        self.assertNotIn("p.condition||", js)
        self.assertIn("surfaceNote", js)
        # The canonical table is the server's, injected — not a second copy
        # with its own idea of which field means what.
        self.assertNotIn("__SURFACE_CANONICAL__", js)
        self.assertIn('"recommendation": "desired_outcome"', js)
        self.assertIn("f!==SURFACE_CANONICAL[name]", js)

    def test_an_absent_field_renders_as_absent_not_as_a_blank(self):
        js = dashboard_estate.JS
        self.assertIn("'No '+label.toLowerCase()+' recorded'", js)
        self.assertIn("' missing'", js)
        self.assertIn(".proposal-recommendation.missing", dashboard_estate.CSS)

    def test_the_collapsed_attention_row_shows_one_line_of_context(self):
        js = dashboard_estate.JS
        self.assertIn("function contextLine(t)", js)
        self.assertIn("t.context_line", js)
        self.assertIn("t.context_field", js)
        self.assertIn("No context recorded", js)
        self.assertIn("contextLine(t)", js.split("function attentionRow")[1])
        self.assertIn(".attention-context", dashboard_estate.CSS)

    def test_the_attention_row_shows_a_clamped_headline_not_the_raw_title(self):
        """the contract — title median 131 chars, 73 of 91 over 80, rendered raw."""
        js = dashboard_estate.JS
        row = js.split("function attentionRow(t,tier)")[1].split("\n")[0]
        self.assertIn("headline(t)", row)
        self.assertNotIn("e(t.title)", row,
                         "the collapsed row must not render the raw title")
        self.assertIn("t.headline||t.title", js)
        # The clamp itself is the server's, so the CLI's rendering of the same
        # rows cannot disagree with this one.
        self.assertNotIn("substring", js)
        self.assertIn("t.headline_clamped", js)
        self.assertIn(".attention-headline", dashboard_estate.CSS)

    def test_the_row_carries_the_question_and_the_recommendation(self):
        js = dashboard_estate.JS
        self.assertIn("function attentionBody(t)", js)
        self.assertIn("t.question,t.question_field", js)
        self.assertIn("t.recommendation,t.recommendation_field", js)
        # Same rule as the proposal row: which key answered is the server's
        # call, and the badge fires only when it is neither canonical nor
        # eponymous.
        self.assertIn("field!==SURFACE_CANONICAL[name]&&field!==name", js)

    def test_an_unclassified_row_is_badged_and_never_given_a_classification(self):
        js = dashboard_estate.JS
        self.assertIn("no decision stated", js)
        self.assertIn("t.decision_state", js)
        self.assertIn("nothing here infers one", js)
        # Four words, and the renderer invents none of them.
        for word in ("sketched", "incomplete", "stated", "unstated"):
            self.assertIn(word + ":[", js)
        self.assertIn(".pill.decision-unstated", dashboard_estate.CSS)

    def test_the_context_strip_is_clamped_and_says_when_it_was(self):
        """F7 — the clarifier ran a median of 500 characters."""
        js = dashboard_estate.JS
        self.assertIn("t.context_line_clamped", js)
        self.assertIn("the whole text is in the drill-down", js)

    def test_the_attention_panel_is_tiered_and_nothing_is_capped(self):
        """P4, resolved uncapped: typed tiers, both counts, no budget."""
        js = dashboard_estate.JS
        self.assertIn("DATA.attention_tiers", js)
        self.assertIn("function tierHead(t,visible)", js)
        self.assertIn("visible+' of '+total+' shown'", js)
        self.assertIn("' of '+t.total+' shown'", js)
        self.assertIn("in the notice band", js)
        draw = js.split("function drawAttention()")[1].split("\n\n")[0]
        self.assertNotIn(".slice(0,", draw,
                         "a cap on the attention list is the thing P4 refused")
        self.assertIn(".tier-head", dashboard_estate.CSS)

    def test_idle_is_compared_within_the_tier_and_never_across_it(self):
        js = dashboard_estate.JS
        self.assertIn("t.idle_above_tier_median===true", js)
        self.assertIn("tier.idle_median", js)
        self.assertIn("idleBadge(t,tier)", js)
        # Nothing here sorts by age: the order is the contract's, server-side.
        draw = js.split("function drawAttention()")[1].split("\n\n")[0]
        self.assertNotIn("sort(", draw)

    def test_a_snapshot_without_tiers_still_renders_every_row(self):
        """The tiers are additive. An older snapshot has the ordered id list
        and one flat section is a correct rendering of it."""
        js = dashboard_estate.JS
        self.assertIn("if(!tiers.length)", js)
        self.assertIn("attentionRows()", js)

    def test_proposal_decisions_post_the_expected_stage_and_required_note(self):
        js = dashboard_estate.JS
        self.assertIn("fetch('proposal-stage'", js)
        self.assertIn("expected_stage:t.stage", js)
        self.assertIn("A decision note is required.", js)
        self.assertIn("X-Dashboard-Token", js)
        self.assertIn("Approve to backlog", js)

    def test_proposal_expansion_survives_refresh(self):
        js = dashboard_estate.JS
        self.assertIn("OPEN_PROPOSALS", js)
        self.assertIn("[data-proposal][open]", js)

    def test_decision_note_draft_survives_the_thirty_second_refresh(self):
        js = dashboard_estate.JS
        self.assertIn("DECISION_NOTES", js)
        self.assertIn("DECISION_NOTES[v.dataset.decisionNote]=v.value", js)
        self.assertIn("e(DECISION_NOTES[t.id]||'')", js)

    def test_decision_cards_are_the_only_proposal_presentation(self):
        js, markup = dashboard_estate.JS, page()
        self.assertIn("function decisionSketch(p,id,stage)", js)
        self.assertIn('class="decision-card ', js)
        self.assertIn("decisionCard(id,'recommendation'", js)
        self.assertIn("decisionCard(id,'defer'", js)
        self.assertIn("decisionCard(id,'option-card'", js)
        for removed in ("function sketchA", "function sketchB", "function sketchC",
                        "PROPOSAL_SKETCH", "DECISION_SKETCH_SPECIMEN",
                        "drawSketchSpecimen", "data-proposal-sketch",
                        "decision-sketch-specimen"):
            self.assertNotIn(removed, js + markup)

    def test_decision_cards_prefill_without_submitting(self):
        js = dashboard_estate.JS
        self.assertIn("function prefillDecision(card)", js)
        self.assertIn("note.value=card.dataset.decisionPrefill", js)
        self.assertIn("DECISION_NOTES[id]=note.value", js)
        self.assertIn("if(card)prefillDecision(card)", js)
        self.assertIn("Owner clicked: defer — ", js)
        # Scoped to the proposal section on purpose: this is the "a card click
        # prefills and never submits" guard, and counting POSTs across the
        # whole page turns it into a second, weaker copy of
        # NarrowWriteSurfaceTest's inventory below.
        self.assertEqual(dashboard_estate.JS_PROPOSALS.count("method:'POST'"), 1)
        self.assertIn(".decision-card:hover", dashboard_estate.CSS)
        self.assertIn("cursor:pointer", dashboard_estate.PROPOSAL_CSS)

    def test_stage_is_searchable(self):
        self.assertIn("(t.stage||'')", dashboard_estate.JS)


class TasksPanelTest(unittest.TestCase):
    """the panel said "Follow-ups" and held every task kind, and its"""

    def test_the_panel_is_named_for_what_it_contains(self):
        markup = page()
        self.assertIn('<span class="k">Tasks</span>', markup)
        self.assertNotIn('<span class="k">Follow-ups</span>', markup,
                         "the mislabeled heading must be gone, not duplicated")

    def test_active_is_the_default_and_means_non_terminal(self):
        markup, js = page(), dashboard_estate.JS
        self.assertIn('class="filter active" data-filter="active"', markup)
        self.assertNotIn('data-filter="open"', markup,
                         "literal `open` was the bug, not the fix")
        self.assertIn("FILTER='active'", js)
        self.assertIn("var TERMINAL=['done','dropped']", js)
        self.assertIn("if(FILTER==='active')return !terminal(t)", js)

    def test_the_lifecycle_grid_shows_every_status(self):
        js = dashboard_estate.JS
        self.assertIn(
            "stats(x.task_counts,['open','ready','claimed','blocked',"
            "'needs-owner','done','dropped'])", js,
            "the grid omitted ready/claimed/blocked, which is most live work")

    def test_the_cross_cutting_filters_are_derived_not_statuses(self):
        js = dashboard_estate.JS
        self.assertIn("if(FILTER==='blocked')return blockedBy(t).length>0", js)
        self.assertIn("if(FILTER==='attention')return !!t.is_attention", js)

    def test_an_exact_id_search_is_a_lookup_and_escapes_the_tab(self):
        """Searching `t152` under the default Active tab returned"""
        js = dashboard_estate.JS
        self.assertIn(r"function idQuery(q){var m=/^\s*t-?(\d+)\s*$/.exec(q);"
                      "return m?'t-'+m[1]:''}", js)
        self.assertIn("var id=idQuery(q);", js,
                      "drawTasks has to resolve the query as an id first")
        self.assertIn("if(id&&t.id===id)return true;", js,
                      "the exact row has to short-circuit the filters, not "
                      "just the status tab — a kind selector hides it the "
                      "same way")

    def test_only_an_exact_id_escapes_the_tab(self):
        """A substring is still a filter. If `the contract` pulled the contract and the contract out"""
        js = dashboard_estate.JS
        self.assertIn("&&(!q||haystack(t).indexOf(q)>=0)});", js,
                      "the ordinary text path must be untouched")

    def test_kind_and_project_selectors_are_display_controls(self):
        markup = page()
        self.assertIn('id="kind-filter"', markup)
        self.assertIn('id="project-filter"', markup)
        self.assertIn("KIND_FILTER=this.value", dashboard_estate.JS)
        self.assertIn("PROJECT_FILTER=this.value", dashboard_estate.JS)

    def test_the_detail_view_renders_the_fields_the_snapshot_already_carried(self):
        """Read off `m`, the snapshot's row MERGED with whatever"""
        js = dashboard_estate.JS
        for field in ("m.intent", "m.lane", "m.project_id", "m.claimed_by",
                      "m.claim_expires_at", "m.closed_at", "m.due_at"):
            self.assertIn(field, js, f"{field} rides in the snapshot unused")

    def test_history_keeps_actor_kind_and_exact_timestamp(self):
        """The old drill-down flattened every event to summary + age."""
        js = dashboard_estate.JS
        self.assertIn("function historyLine(v)", js)
        line = js[js.index("function historyLine(v)"):]
        line = line[:line.index("\n")]
        for part in ("stamp(v.ts)", "v.summary", "who(v)", "v.kind", "noteDetail(v)"):
            self.assertIn(part, line)


class DecisionBlockTest(unittest.TestCase):
    """The classification a filing carries the contract was legible on this page"""

    def details(self):
        js = dashboard_estate.JS
        body = js[js.index("function taskBody(t)"):]
        return body[:body.index("\nfunction taskRow(")]

    def test_the_decision_block_is_rendered_above_the_raw_refs_dump(self):
        details = self.details()
        self.assertIn("decisionBlock(m,", details)
        self.assertLess(details.index("decisionBlock(m,"),
                        details.index("<span class=\"label\">refs</span>"),
                        "the readable block must precede the JSON fallback")

    def test_the_raw_refs_dump_survives_as_the_fallback(self):
        self.assertIn("<span class=\"label\">refs</span><pre>", self.details(),
                      "the dump covers everything the block does not name")

    def test_one_shape_answers_for_followups_and_proposals(self):
        """Both writers file through lib/estate_decisions.validate, so the
        block reads the task's own refs and not a per-kind envelope."""
        js = dashboard_estate.JS
        block = js[js.index("function decisionBlock(t,shownContext)"):]
        block = block[:block.index("\nfunction taskDetails")]
        self.assertIn("refsOf(t)", block)
        for absent in ("t.followup", "t.proposal", "t.kind"):
            self.assertNotIn(absent, block,
                             f"{absent} would make this block kind-specific")
        self.assertIn("if(!c)return ''", block,
                      "an unclassified task renders no decision block at all")

    def test_every_classification_field_gets_its_own_labelled_row(self):
        js = dashboard_estate.JS
        block = js[js.index("function decisionBlock(t,shownContext)"):]
        block = block[:block.index("\nfunction taskDetails")]
        for stored in ("r.classification", "r.question", "r.recommendation",
                       "r.defer_consequence", "r.context"):
            self.assertIn(stored, block, f"{stored} is filed and never shown")
        self.assertIn("kv('question',r.question)", block)
        self.assertIn("kv('if deferred',r.defer_consequence)", block)
        self.assertIn("r.alternatives", dashboard_estate.JS)

    def test_alternatives_pair_each_option_with_its_own_consequence(self):
        js = dashboard_estate.JS
        card = js[js.index("function alternativeCard(v)"):]
        card = card[:card.index("\n")]
        self.assertIn("e(v.option)", card)
        self.assertIn("e(v.consequence||'No consequence recorded')", card)
        self.assertIn(">consequence<", card,
                      "the consequence must be labelled, not concatenated")
        self.assertIn("function decisionAlternatives(r)", js)
        self.assertIn("Array.isArray(r.alternatives)", js,
                      "a non-list must never be iterated as one")

    def test_not_recorded_reads_as_an_answer_and_not_as_a_missing_field(self):
        """docs/decision-classification-contract.md — the filer read the
        options and picked none, which is a fact, not an empty cell."""
        js = dashboard_estate.JS
        self.assertIn("var NOT_RECORDED='not recorded'", js)
        self.assertIn("rec.toLowerCase()===NOT_RECORDED", js)
        self.assertIn("filer recorded no pick", js)

    def test_context_is_not_printed_twice_under_two_labels(self):
        js = dashboard_estate.JS
        self.assertIn("(ctx&&ctx!==shownContext)", js)
        self.assertIn("kv(shownContext?'decision context':'context',ctx)", js)
        self.assertIn("decisionBlock(m,f&&f.context?String(f.context).trim():'')",
                      js, "the follow-up envelope's context is what it dedupes "
                          "against")

    def test_refs_that_cannot_be_parsed_are_an_empty_reading_not_a_crash(self):
        js = dashboard_estate.JS
        fn = js[js.index("function refsOf(t)"):]
        fn = fn[:fn.index("\n")]
        self.assertIn("catch(_){return {}}", fn)
        self.assertIn("!Array.isArray(x)", fn,
                      "a refs array has no classification to read")

    def test_the_block_does_not_borrow_the_clickable_proposal_cards(self):
        """`.decision-card` is cursor:pointer and prefills the decision note,
        and that handler is bound only on the proposals panel — wearing it
        here would make a row look actionable that is not."""
        js = dashboard_estate.JS
        block = js[js.index("function decisionBlock(t,shownContext)"):]
        block = block[:block.index("\nfunction taskDetails")]
        self.assertNotIn("decision-card", block)
        self.assertNotIn("data-decision-card", block)
        for reused in (".decision-sketch", ".classification", ".review-field",
                       ".review-badge", ".decision-options"):
            self.assertIn(reused, dashboard_estate.CSS,
                          f"{reused} is reused, not reinvented")


class NoteMarkdownTest(unittest.TestCase):
    def test_note_markdown_is_escape_first_and_allow_listed(self):
        js = dashboard_estate.JS
        inline = js[js.index("function mdInline(s)"):]
        inline = inline[:inline.index("\n")]
        self.assertIn("x=e(s||'')", inline)
        self.assertIn("target=\"_blank\" rel=\"noopener\"", inline)
        for tag in ("<code>", "<strong>", "<em>"):
            self.assertIn(tag, inline)

    def test_block_renderer_supports_document_structure(self):
        js = dashboard_estate.JS
        for marker in ("<h'+n+'>", 'class="md-table-wrap"',
                       "<pre><code>", "<blockquote>", "<li>"):
            self.assertIn(marker, js)
        self.assertIn(".md-note table{", dashboard_estate.CSS)
        self.assertIn(".md-note h2{", dashboard_estate.CSS)

    def test_document_sized_notes_use_the_focused_view(self):
        js, markup = dashboard_estate.JS, page()
        self.assertIn("var LONG_NOTE=4000", js)
        self.assertIn("Read full note", js)
        self.assertIn("EVENT_INDEX", js)
        self.assertIn("dialog.showModal()", js)
        self.assertIn('id="note-dialog"', markup)
        self.assertIn('id="note-dialog-body"', markup)


class AttentionSectionTest(unittest.TestCase):
    """"waiting on a person" is its own question, and it is the one"""

    def test_the_section_exists_and_is_wired(self):
        markup = page()
        self.assertIn('<span class="k">Attention</span>', markup)
        self.assertIn('id="attention"', markup)
        self.assertIn('id="attention-stats"', markup)

    def test_it_reads_the_servers_ordered_set_and_re_derives_nothing(self):
        js = dashboard_estate.JS
        self.assertIn("DATA.attention||[]", js)
        self.assertIn("DATA.attention_counts", js)
        self.assertNotIn("is_attention||", js)
        # No client-side re-implementation of the attention rule: the page
        # reads `is_attention`, it never recomputes it from kind and status.
        # the contract introduced the one lawful reading of `kind` on this page —
        # which ROW the resolve verb can reach, which is not the same question
        # as who is in the set — so the guard names it rather than banning the
        # literal. A second occurrence would be the re-derivation this test
        # has always existed to catch.
        self.assertEqual(js.count("kind==='followup'"), 1)
        self.assertIn("function canResolve(t){return t&&t.kind==='followup'", js)

    def test_every_due_state_is_a_filter_including_unreadable(self):
        markup = page()
        for state in ("overdue", "due_today", "due_soon", "later", "undated",
                      "unreadable"):
            self.assertIn(f'data-attn="{state}"', markup)
        self.assertIn(
            "var DUE_STATES=['unreadable','overdue','due_today','due_soon',"
            "'later','undated']", dashboard_estate.JS)

    def test_resolved_followups_sit_behind_history_not_in_the_list(self):
        markup, js = page(), dashboard_estate.JS
        self.assertIn('data-attn="history"', markup)
        self.assertIn("function resolvedFollowups()", js)
        self.assertIn("t.followup.state==='resolved'", js)

    def test_the_followup_envelope_is_decoded_for_the_reader(self):
        js = dashboard_estate.JS
        for field in ("f.legacy_id", "f.source", "f.ref", "f.context",
                      "f.resolution", "f.attention_key"):
            self.assertIn(field, js)

    def test_due_state_is_shown_with_its_exact_date(self):
        js = dashboard_estate.JS
        self.assertIn("function dueBadge(t)", js)
        self.assertIn("t.due_state", js)
        self.assertIn("t.days_left", js)
        self.assertIn(".due.overdue,.due.unreadable{", dashboard_estate.CSS)


class InboxSectionTest(unittest.TestCase):
    """"which of the operator's own messages are still open", and the one"""

    def test_the_section_exists_and_is_wired(self):
        markup = page()
        self.assertIn('<span class="k">Open inbox messages</span>', markup)
        self.assertIn('id="inbox"', markup)
        self.assertIn('id="inbox-stats"', markup)
        self.assertIn('id="inbox-count"', markup)

    def test_it_reads_the_servers_ordered_list_and_re_derives_nothing(self):
        js = dashboard_estate.JS
        self.assertIn("DATA.inbox_messages||[]", js)
        self.assertIn("DATA.inbox_message_counts", js)
        # The open/closed rule lives in lib/estate_work.py. A second copy in
        # JavaScript is a second rule, which is the whole reason this page
        # renders derived fields instead of computing them.
        self.assertNotIn("kind==='inbox-message'", js)

    def test_it_says_which_inbox_and_whether_it_arrived_in_a_thread(self):
        js = dashboard_estate.JS
        self.assertIn("function threadBadge(t)", js)
        self.assertIn("m.threaded", js)
        self.assertIn("m.thread_ts", js)
        self.assertIn("m.inbox", js)

    def test_it_is_drawn_on_every_refresh(self):
        self.assertIn("drawInbox()", dashboard_estate.JS)

    def test_it_names_the_cli_that_answers_the_same_question(self):
        """The panel is a convenience; the query is the contract."""
        self.assertIn("bin/estate task list --kind inbox-message", page())


class ProjectsSectionTest(unittest.TestCase):
    def test_the_section_exists_and_is_wired(self):
        markup = page()
        self.assertIn('<span class="k">Projects</span>', markup)
        self.assertIn('id="projects"', markup)
        self.assertIn('id="project-stats"', markup)

    def test_every_rollup_count_reaches_the_row(self):
        js = dashboard_estate.JS
        for part in ("r.open", "r.terminal", "r.blocked", "r.attention",
                     "r.overdue", "r.tasks", "r.last_activity"):
            self.assertIn(part, js)

    def test_unprojected_work_is_rendered_as_a_grouping(self):
        js = dashboard_estate.JS
        self.assertIn("p.unprojected", js)
        self.assertIn("'unprojected'", js)      # the project selector's value
        self.assertIn(".pill.grouping{", dashboard_estate.CSS)

    def test_a_project_drilldown_composes_its_own_and_its_tasks_history(self):
        """§4.9/§4.10 — the same authoritative table, composed rather than"""
        js = dashboard_estate.JS
        self.assertIn("function projectHistory(p)", js)
        self.assertIn("DATA.project_events", js)
        body = js[js.index("function projectHistory(p)"):]
        body = body[:body.index("\n")]
        self.assertIn("taskEvents(p.id)", body)
        self.assertIn("seen[v.seq]", body, "the two sources must be deduped")

    def test_a_project_history_still_loading_is_not_an_empty_one(self):
        """The absence rule at the rendering boundary: `projectHistory` is
        null until the fetch lands, and the row says so."""
        js = dashboard_estate.JS
        body = js[js.index("function projectHistory(p)"):]
        body = body[:body.index("\n")]
        self.assertIn("if(!fetched)return null", body)
        self.assertIn("pendingHistory(p.id,'No events recorded for this project.')",
                      js)

    def test_the_grouping_claims_no_history_of_its_own(self):
        """Composing one would re-print most of the estate's ledger under a
        heading that says it belongs to a project."""
        js = dashboard_estate.JS
        self.assertIn("if(p.unprojected)return null", js)
        self.assertIn("has no history of its own", js)


class DependenciesSectionTest(unittest.TestCase):
    def test_the_section_exists_and_is_wired(self):
        markup = page()
        self.assertIn('<span class="k">Dependencies</span>', markup)
        self.assertIn('id="blockers"', markup)
        self.assertIn('id="deps"', markup)

    def test_operational_blockers_are_the_unresolved_blocks_edges_only(self):
        js = dashboard_estate.JS
        self.assertIn("v.kind==='blocks'&&v.active", js)

    def test_every_edge_kind_is_filterable(self):
        markup = page()
        for kind in ("blocks", "parent", "related", "discovered-from"):
            self.assertIn(f'data-dep="{kind}"', markup)
        self.assertIn(
            "var DEP_KINDS=['blocks','parent','related','discovered-from']",
            dashboard_estate.JS)

    def test_both_endpoints_are_named_never_bare_ids(self):
        js = dashboard_estate.JS
        for part in ("v.from_task", "v.from_title", "v.from_status",
                     "v.to_task", "v.to_title", "v.to_status"):
            self.assertIn(part, js)

    def test_a_task_drilldown_shows_both_directions(self):
        js = dashboard_estate.JS
        self.assertIn("relatedLine('blocked by',blockedBy(m),m.blocked_by_titles)", js)
        self.assertIn("relatedLine('blocks',m.blocks,m.blocks_titles)", js)


class LazyDrilldownTest(unittest.TestCase):
    """the drill-down is fetched, and never faked while it is in"""

    def test_the_history_slot_states_pending_rather_than_empty(self):
        js = dashboard_estate.JS
        self.assertIn("function historySlot(id,msg)", js)
        slot = js[js.index("function historySlot(id,msg)"):]
        slot = slot[:slot.index("\n")]
        self.assertIn("rows?history(rows,msg):pendingHistory(id,msg)", slot,
                      "an unfetched history must not render as 'no events'")
        self.assertIn("return 'Loading…'", js)

    def test_a_store_that_cannot_link_events_says_so_in_its_own_words(self):
        """`unavailable` is a fact about the store, not about the task."""
        js = dashboard_estate.JS
        self.assertIn("DATA.task_events_source==='unavailable'", js)
        self.assertIn("cannot link events to tasks", js)

    def test_a_failed_fetch_is_recorded_as_a_reason_not_as_no_events(self):
        js = dashboard_estate.JS
        self.assertIn("x.available===false", js)
        self.assertIn("rows:null", js, "a failure must not cache an empty list")
        self.assertIn("could not be loaded", js)

    def test_a_summary_only_row_waits_rather_than_showing_blanks(self):
        """A terminal task nothing else in the snapshot points at carries only
        its collapsed fields. Rendering the rest as empty cells would read as
        'nothing was recorded'."""
        js = dashboard_estate.JS
        self.assertIn("if(t.summary_only&&!loaded)", js)

    def test_the_refs_dump_and_its_decision_block_wait_for_the_fetch(self):
        js = dashboard_estate.JS
        body = js[js.index("function taskBody(t)"):]
        body = body[:body.index("\nfunction taskRow(")]
        self.assertIn("loaded?decisionBlock(m,", body)
        self.assertIn("loaded&&refs&&refs!=='{}'", body)

    def test_a_cached_drilldown_expires_with_the_row_it_is_for(self):
        """`updated_at` moves on every event written against a task, so
        comparing it is how a cached history stops being current without
        anything polling for it."""
        js = dashboard_estate.JS
        self.assertIn("function detailStamp(id)", js)
        self.assertIn("if(d&&d.stamp===want)return", js)

    def test_the_toggle_listener_is_delegated_and_captures(self):
        """`toggle` does not bubble, and a per-row handler would be re-bound
        to every row on every 30-second redraw."""
        js = dashboard_estate.JS
        self.assertIn("document.addEventListener('toggle'", js)
        wiring = js[js.index("function installDetailWiring()"):]
        wiring = wiring[:wiring.index("\n")]
        self.assertIn("},true)", wiring, "toggle must be caught in capture")
        for attr in ("data-task", "data-proposal", "data-project"):
            self.assertIn(f"getAttribute('{attr}')", wiring)
        self.assertIn("installDetailWiring();", js)

    def test_a_row_rendered_open_asks_again_without_a_toggle(self):
        """Parsing an `open` attribute fires no toggle event, so a redraw has
        to ask for every row it re-rendered open."""
        js = dashboard_estate.JS
        self.assertIn("function ensureOpenDetails()", js)
        self.assertIn("ensureOpenDetails();", js)

    def test_the_patch_is_in_place_rather_than_a_redraw(self):
        """The row is open because the reader just opened it."""
        js = dashboard_estate.JS
        patch = js[js.index("function patchDetail(id)"):]
        patch = patch[:patch.index("\n// `toggle` does not bubble")]
        self.assertIn("[data-detail=\"'+id+'\"]", patch)
        self.assertIn("[data-history=\"'+id+'\"]", patch)
        self.assertNotIn("drawTasks()", patch)

    def test_a_fetched_history_survives_the_thirty_second_refresh(self):
        """EVENT_INDEX is rebuilt from the SNAPSHOT on every refresh, and a
        fetched drill-down's rows are not in the snapshot. Without folding
        them back in, the "Read full note" button in a row that is still open
        goes dead 30 seconds after the row was fetched — the button
        re-renders from the cache and the index no longer has its seq."""
        js = dashboard_estate.JS
        self.assertIn("function reindexDetailEvents()", js)
        # Called AFTER the rebuild, or it would be undone by it.
        rebuild = js.index("EVENT_INDEX={};Object.keys(x.task_events")
        line = js[rebuild:js.index("\n", rebuild)]
        self.assertIn("reindexDetailEvents();", line)
        self.assertLess(line.index("(x.events||[])"),
                        line.index("reindexDetailEvents();"))

    def test_the_recurrence_badge_reads_the_servers_count(self):
        """A collapsed badge must not depend on a drill-down that is fetched —
        and `lib/estate_work.build` already computes this number for the body
        the badge sits above."""
        js = dashboard_estate.JS
        badge = js[js.index("function recurBadge(t)"):]
        badge = badge[:badge.index("\n")]
        self.assertIn("(t.proposal||{}).recurrences", badge)
        self.assertNotIn("taskEvents", badge)


class NarrowWriteSurfaceTest(unittest.TestCase):
    """Exactly two controls mutate estate state from this page: a proposal"""

    def test_the_banner_is_present_and_names_both_narrow_write_paths(self):
        markup = page()
        self.assertIn('class="readonly"', markup)
        self.assertIn("Narrow write surface", markup)
        self.assertIn("bin/estate proposal stage", markup)
        self.assertIn("bin/followups resolve", markup)
        self.assertIn("require the dashboard write token", markup)
        self.assertIn(".readonly{", dashboard_estate.CSS)

    def test_the_page_carries_exactly_the_two_declared_write_paths(self):
        """The inventory, not a ceiling. The count is the point: a third POST
        appearing here is a write path nobody declared, and the banner above
        would be a page that lies about what it does."""
        js = dashboard_estate.JS
        self.assertEqual(js.count("method:'POST'"), 2)
        self.assertIn("fetch('proposal-stage'", js)
        self.assertIn("fetch('followup-resolve'", js)
        self.assertIn("fetch('dashboard.json?t='+Date.now(),{cache:'no-store'})", js)
        # the contract's drill-down read. A GET, and the only other network call.
        self.assertIn("fetch('estate-detail?'+q,{cache:'no-store'})", js)
        self.assertEqual(js.count("fetch("), 4)
        for verb in ('method:"POST"', "method:'PUT'", "method:'DELETE'",
                     "XMLHttpRequest", "navigator.sendBeacon"):
            self.assertNotIn(verb, js)

    def test_the_snapshot_age_is_shown(self):
        """The browser's 30s fetch does not prove the ~45s producer is alive."""
        self.assertIn('id="generated-at"', page())
        self.assertIn("d.generated_at", dashboard_estate.JS)


class FollowupResolveControlTest(unittest.TestCase):
    """the attention set's one write verb, on the attention row."""

    def test_the_control_reaches_only_rows_the_verb_can_act_on(self):
        js = dashboard_estate.JS
        # kind AND terminality, not kind alone: a resolved follow-up still
        # renders behind the History toggle and must not offer the button.
        self.assertIn("function canResolve(t){return t&&t.kind==='followup'"
                      "&&!terminal(t)}", js)
        self.assertIn("function resolveBox(t){if(!canResolve(t))return ''", js)
        # On the attention row, and on no other row on the page.
        self.assertIn("+taskDetails(t)+resolveBox(t)+'</details>'", js)
        self.assertEqual(js.count("resolveBox(t)"), 2)   # its definition + one call

    def test_it_posts_the_raw_status_as_the_optimistic_lock(self):
        js = dashboard_estate.JS
        self.assertIn("fetch('followup-resolve',{method:'POST',cache:'no-store',"
                      "headers:{'Content-Type':'application/json',"
                      "'X-Dashboard-Token':tok}", js)
        self.assertIn("body:JSON.stringify({id:id,expected_status:t.status,"
                      "note:text})", js)

    def test_a_resolution_note_is_required_before_anything_is_sent(self):
        js = dashboard_estate.JS
        self.assertIn("if(!text){resolveStatus(id,'A resolution note is "
                      "required.',true);note.focus();return}", js)
        self.assertIn("Resolution note (required)", js)

    def test_the_buttons_disable_while_in_flight_and_re_enable_on_failure(self):
        js = dashboard_estate.JS
        self.assertIn("var buttons=document.querySelectorAll("
                      "'[data-resolve-id=\"'+id+'\"]');buttons.forEach("
                      "function(b){b.disabled=true})", js)
        self.assertIn("buttons.forEach(function(b){b.disabled=false})", js)

    def test_a_401_clears_the_stored_token_exactly_as_the_proposal_path_does(self):
        js = dashboard_estate.JS
        self.assertEqual(js.count("localStorage.removeItem(TOKEN_KEY)"), 2)
        self.assertIn("Token cleared; retry to enter it again.", js)
        # One secret, one key, one prompt — the follow-up path reuses the
        # proposal path's reader rather than minting a second.
        self.assertEqual(js.count("function decisionToken()"), 1)
        self.assertIn("var tok=decisionToken();if(!tok){resolveStatus("
                      "id,'Write token required.',true);return}", js)

    def test_the_resolved_row_leaves_the_set_and_its_tier_total(self):
        """A tier that dropped a row from `ids` but kept its `total` would
        render "57 of 57 shown" over 56 rows, which is the counting bug the
        uncapped-tier design exists to make impossible."""
        js = dashboard_estate.JS
        self.assertIn("t.is_attention=false", js)
        self.assertIn("var ids=DATA.attention||[],at=ids.indexOf(t.id);"
                      "if(at>=0)ids.splice(at,1)", js)
        self.assertIn("tier.ids.splice(i,1);tier.total=Math.max(0,"
                      "(tier.total||0)-1)", js)
        self.assertIn("c[was]=Math.max(0,(c[was]||0)-1);"
                      "c.total=Math.max(0,(c.total||0)-1)", js)
        # and it reappears as history rather than vanishing
        self.assertIn("t.followup.state='resolved'", js)

    def test_the_note_survives_a_snapshot_redraw(self):
        js = dashboard_estate.JS
        self.assertIn("RESOLVE_NOTES={}", js)
        self.assertIn("document.querySelectorAll('[data-resolve-note]')"
                      ".forEach(function(v){RESOLVE_NOTES[v.dataset."
                      "resolveNote]=v.value})", js)

    def test_the_click_is_delegated_from_the_panel(self):
        js = dashboard_estate.JS
        self.assertIn("document.getElementById('attention').onclick=", js)
        self.assertIn("ev.target.closest('[data-resolve-id]');if(b)"
                      "resolveFollowup(b)", js)


class SharedFeedTest(unittest.TestCase):
    def test_the_recent_event_feed_survives_the_new_sections(self):
        markup = page()
        self.assertIn("Recent task events", markup)
        self.assertIn('id="events"', markup)
        self.assertIn("x.events||[]", dashboard_estate.JS)

    def test_the_daily_dashboard_link_is_still_there(self):
        """Additive page: it never replaces the PR pane, it links back to it
        — through the shared page nav, with this page marked current."""
        markup = page()
        self.assertIn('class="page-nav"', markup)
        self.assertIn('class="pn-link" href="/">Dashboard', markup)
        self.assertIn('aria-current="page" href="/estate.html">Estate', markup)

    def test_proposal_workspace_has_a_stable_deep_link(self):
        self.assertIn('id="proposals-panel"', page())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Steward prototype: source joins, missing observations, and safe rendering."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import dashboard_stewards as st


class StewardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / 'stewards').mkdir()
        (self.state / 'project').mkdir()
        (self.state / 'pr-tracker').mkdir()
        self.row = {'slug': 'sample-project', 'worktree': str(self.state), 'repos': ['example/widget'],
                    'task': 't-12', 'epic': {'ref': 'catalog-epic'}, 'status': 'active'}
        (self.state / 'stewards/sample-project.json').write_text(json.dumps(self.row))
        (self.state / 'project/STATE.md').write_text('Waiting on D1. widget#12\nwidget#13')
        (self.state / 'project/NOTES.md').write_text('# Notes\n## Earlier\nOld\n## Later\nNew')
        (self.state / 'pr-tracker/state.json').write_text(json.dumps({'entries': {
            'example/widget#12': {'pr': {'repo': 'example/widget', 'state': 'OPEN', 'title': 'tracked'},
                                'last_successful_observation_at': '2000-01-01T00:00:00Z'}}}))
        self.work = {'available': True, 'tasks': [
            {'id': 't-20', 'title': 'Decision', 'status': 'needs-owner', 'refs': '{"task":"t-12"}'},
            {'id': 't-21', 'title': 'Unrelated', 'status': 'needs-owner', 'refs': '{"task":"t-123"}'}],
            'attention': ['t-20', 't-21'], 'proposals': ['t-20']}

    def gather(self):
        return st.gather(self.state, self.work, time.time())['rows'][0]

    @patch.object(st, 'github_repo', return_value=([{'number': 13, 'state': 'OPEN', 'title': 'missing'}], None))
    def test_reuses_tracker_and_only_fetches_gap(self, fetch):
        item = self.gather()
        fetch.assert_called_once_with('example/widget')
        self.assertEqual(len(item['prs']), 2)
        self.assertEqual([t['id'] for t in item['attention']], ['t-20'])
        self.assertTrue(any(e['staleness']['state'] == 'stale' for e in item['pr_readings']))
        (self.state / 'project/STATE.md').write_text('widget#12')
        fetch.reset_mock()
        self.gather()
        fetch.assert_not_called()

    @patch.object(st, 'github_repo', return_value=(None, 'offline'))
    def test_failed_fetch_and_missing_notes_are_not_empty_success(self, fetch):
        (self.state / 'project/NOTES.md').unlink()
        item = self.gather()
        self.assertFalse(item['NOTES_reading']['empty_is_evidence'])
        self.assertTrue(any(e['warrant'] == 'failed' for e in item['pr_readings']))
        self.assertIn('unconfirmed', item['prs'][0]['label'])

    def test_references_are_repo_scoped_and_safe(self):
        self.assertEqual(st.references('widget#12 https://github.com/example/widget/pull/13 other/widget#14', ['example/widget']),
                         {'example/widget#12', 'example/widget#13'})
        out = st.prose('<script>alert(1)</script> https://example.org/?a=1&b=2')
        self.assertNotIn('<script>', out)
        self.assertIn('href="https://example.org/?a=1&amp;b=2"', out)
        self.assertNotIn('href=', st.link('javascript:alert(1)', 'bad'))
        self.assertIn('href="https://example.org/a">Artifact</a>', st.prose('[Artifact](https://example.org/a)'))

    def test_state_tables_preserve_links_headers_and_safety(self):
        source = r"""## Links and artifacts
<!-- - Hidden: https://hidden.test -->
- Topology: https://example.org/a?x=1&y=2
- [Review](https://example.org/review)
- Local source: project/design.html
## Tickets / PRs (read live before acting)
| PR | ticket | state | needs |
|---|:---|---:|:---:|
| [#12](https://github.com/example/widget/pull/12) | AI-667 | open | a \| b <script> |
## Decisions
| question | answer |
|---|---|
| Keep? | yes |
"""
        tables = st.state_tables(source)
        self.assertEqual(len(tables), 2)
        self.assertIn('<th>What it is</th>', tables[0][1])
        self.assertIn('href="https://example.org/a?x=1&amp;y=2"', tables[0][1])
        self.assertIn('Review</a>', tables[0][1])
        self.assertIn('project/design.html', tables[0][1])
        self.assertNotIn('hidden.test', str(tables))
        self.assertIn('<th>needs</th>', tables[1][1])
        self.assertIn('a | b &lt;script&gt;', tables[1][1])
        self.assertNotIn('<script>', str(tables))
        self.assertNotIn('href=', st.summary_table(['Link'], [['[bad](javascript:alert(1))']]))

    def test_summary_table_full_pr_reference(self):
        out = st.summary_table(['PR'], [['other-org/some.repo#2367']])
        self.assertIn('<td><a href="https://github.com/other-org/some.repo/pull/2367">other-org/some.repo#2367</a></td>', out)

    def test_summary_table_short_pr_references(self):
        for repo in ('widget', 'tool', 'builder', 'agentic', 'future-repo'):
            with self.subTest(repo=repo):
                out = st.summary_table(['PR'], [[f'{repo}#1417']])
                self.assertIn(f'<td>{repo}#1417</td>', out)

    def test_summary_table_linear_references(self):
        for ref in ('AI-667', 'HUB-165', 'ECO-12', 'NEWTEAM-123'):
            with self.subTest(ref=ref):
                out = st.summary_table(['Ticket'], [[ref]])
                self.assertIn(f'<td>{ref}</td>', out)

    def test_summary_table_plain_cell_unchanged(self):
        value = 'open & waiting <review> ai-667 prefixAI-667 AI-667suffix'
        out = st.summary_table(['Status'], [[value]])
        self.assertIn('<td>' + st.inline(value) + '</td>', out)
        self.assertNotIn('<a ', out)

    def test_summary_table_existing_links_unchanged(self):
        for value in (
            '[widget#2367 and AI-667](https://example.org/AI-667?pr=builder#1417)',
            'https://example.org/AI-667?pr=builder#1417',
        ):
            with self.subTest(value=value):
                out = st.summary_table(['Link'], [[value]])
                self.assertIn('<td>' + st.inline(value) + '</td>', out)
                self.assertEqual(out.count('<a '), 1)

    def test_summary_table_mixed_references_and_links(self):
        value = 'See tool#4687, AI-667 & [builder#1417](https://example.org/review).'
        out = st.summary_table(['PR / ticket'], [[value]])
        self.assertIn('See tool#4687, ', out)
        self.assertIn('AI-667 &amp; ', out)
        self.assertIn('<a href="https://example.org/review">builder#1417</a>.', out)
        self.assertEqual(out.count('<a '), 1)

    def test_bare_references_only_linked_in_extracted_tables(self):
        value = 'example/widget#2367 builder#1417 AI-667'
        self.assertEqual(st.inline(value), value)
        self.assertNotIn('<a ', st.prose(value))
        source = f'## Tickets / PRs\n| PR | status |\n|---|---|\n| {value} | open |'
        self.assertNotIn('<a ', st.prose(source))
        self.assertEqual(st.state_tables(source)[0][1].count('<a '), 1)

    def test_missing_empty_and_example_sections_are_omitted(self):
        self.assertEqual(st.state_tables('## Next action\nWait'), [])
        self.assertEqual(st.state_tables('## Links and artifacts\n<!-- none -->\n## Tickets\n|ticket|PR|\n|---|---|'), [])
        self.assertEqual(st.state_tables('```md\n## Links and artifacts\n- Example: https://example.org\n```'), [])
        # A ticket table can live under a different heading and omit edge pipes.
        self.assertEqual(len(st.state_tables('## Work\nticket | status\n--- | ---\nAI-1 | open')), 1)

    @patch.object(st, 'github_repo', return_value=([], None))
    def test_render_order_prose_unchanged_and_project_name(self, fetch):
        source = '## Links and artifacts\n- Diagram: https://example.org/a\n## Tickets\n|ticket|PR|\n|---|---|\n|AI-1|none|'
        (self.state / 'project/STATE.md').write_text(source)
        item = self.gather()
        item['projects'] = [{'name': 'Catalog Database Cleanup', 'url': 'https://linear.app/project/opaque-id'},
                            {'name': 'Catalog Meta Attributes', 'url': 'https://linear.app/project/related-id'}]
        class Dashboard:
            def render_panel_state(self, reading):
                return '<p>Source reading</p>'
        out = st.render_item(Dashboard(), item, self.work)
        self.assertLess(out.index('<h2>Links and artifacts'), out.index('<h2>Tickets'))
        self.assertLess(out.index('<h2>Tickets'), out.index('<h2>Current status'))
        self.assertIn(st.prose(source), out)
        self.assertIn('Project: <a href="https://linear.app/project/opaque-id">Catalog Database Cleanup</a>', out)
        self.assertIn('Related project:', out)

    @patch.object(st, 'github_repo', return_value=([], None))
    def test_project_name_from_focus_and_missing_name_fallback(self, fetch):
        self.row['epic'] = {'kind': 'linear-project', 'ref': 'opaque-id', 'url': 'https://linear.app/project/opaque-id/overview'}
        (self.state / 'stewards/sample-project.json').write_text(json.dumps(self.row))
        (self.state / 'focus').mkdir()
        (self.state / 'focus/projects.toml').write_text('[projects.catalog]\nname="Real Project Name"\nurl="https://linear.app/project/opaque-id"\n')
        self.assertEqual(self.gather()['projects'][0]['name'], 'Real Project Name')
        (self.state / 'focus/projects.toml').write_text('invalid toml [')
        self.assertEqual(self.gather()['projects'][0]['name'], 'Name unavailable')
        self.assertEqual(st.project_links({'epic': {'kind': 'linear-issue', 'ref': 'FLO-88'}}, {}), [])

    def test_missing_registry_is_not_zero_stewards(self):
        model = st.gather(self.state / 'missing', {}, time.time())
        self.assertNotEqual(model['registry']['warrant'], 'observed')


if __name__ == '__main__':
    unittest.main()

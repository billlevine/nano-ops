#!/usr/bin/env python3
"""Tests for the hub tick skill's channel-less guard — the invariant behind
the README's "Slack is optional" claim.
Run: python3 tests/test_hub_skill.py

The hub skill is prose executed by a model, so what is testable here is
structural: that the guard exists, that it is stated in every section which
would otherwise touch the control channel unconditionally, and that the
README's public claim and the shipped loops.example.toml still match it.
These are regression tests against the guard (or the doc claim) silently
losing the other half.
"""
import os
import re
import unittest

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SKILL = os.path.join(REPO, "hub", ".claude", "skills", "hub", "SKILL.md")
HUB_AGENTS = os.path.join(REPO, "hub", "AGENTS.md")
README = os.path.join(REPO, "README.md")
EXAMPLE_TOML = os.path.join(REPO, "loops.example.toml")

GUARD = "channel-less"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def flat(text):
    """Collapse wrapping so a phrase assertion survives a reflowed paragraph."""
    return " ".join(text.split())


def sections(text):
    """Split a markdown doc into {heading: body} for its "## " headings."""
    parts = re.split(r"^## +(.*)$", text, flags=re.MULTILINE)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def sub_section(text, heading_starts_with):
    """Body of the "### " subsection whose heading starts with the given text."""
    parts = re.split(r"^### +(.*)$", text, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        if parts[i].strip().startswith(heading_starts_with):
            return parts[i + 1]
    raise AssertionError("no ### subsection starting %r" % heading_starts_with)


class TestSkillGuard(unittest.TestCase):
    def setUp(self):
        self.text = read(SKILL)
        self.sections = sections(self.text)

    def test_load_context_defines_the_unset_condition(self):
        body = self.sections["0. Load context"]
        self.assertIn("slack_channel_id", body)
        self.assertIn(GUARD, body.lower())
        # The condition itself, not just the mode's name.
        self.assertRegex(flat(body), r"(?i)missing, commented out, or empty")

    def test_load_context_forbids_loading_slack_tools_when_unset(self):
        body = self.sections["0. Load context"]
        tool_load = flat(body[body.index("ToolSearch"):])
        self.assertRegex(tool_load, r"(?i)skip this entirely when CHANNEL is unset")

    def test_channel_less_mode_still_runs_the_non_channel_work(self):
        body = self.sections["0. Load context"]
        for required in ("ledger", "health pass", "heartbeat", "agent-deck"):
            self.assertIn(required, body, f"channel-less mode must still name {required}")

    def test_every_channel_touching_section_carries_the_guard(self):
        # These sections read from or write to the control channel; each must
        # say what it does when there is no channel.
        for heading in ("1. First run", "2. Read messages",
                        "3. Handle each message — full trust, act immediately",
                        "6. Pacing and heartbeat"):
            with self.subTest(section=heading):
                self.assertIn(GUARD, self.sections[heading].lower())

    def test_unset_channel_is_not_treated_as_an_outage(self):
        body = self.sections["7. Control channel send — the parameter, and failures"]
        self.assertIn(GUARD, body.lower())
        self.assertRegex(flat(body), r"(?i)not a failure")

    def test_frontmatter_advertises_the_optional_channel(self):
        frontmatter = self.text.split("---")[1]
        self.assertIn("optional", frontmatter)

    def test_hub_session_home_documents_the_mode(self):
        self.assertIn(GUARD, read(HUB_AGENTS).lower())


class TestPublicClaim(unittest.TestCase):
    def test_quickstart_says_slack_is_optional(self):
        quickstart = sections(read(README))["Quickstart"]
        self.assertIn("Slack is optional", quickstart)
        # The claim is only true because of the direct agent-deck path.
        self.assertIn("agent-deck", quickstart)

    def test_quickstart_does_not_oversell_polling(self):
        quickstart = sections(read(README))["Quickstart"]
        self.assertIn("doorbell", quickstart,
                      "the lost asynchronous path must be stated, not glossed")

    def test_example_registry_ships_without_a_channel(self):
        # The guard's default path: a fresh clone has no slack_channel_id.
        for line in read(EXAMPLE_TOML).splitlines():
            self.assertFalse(re.match(r"\s*slack_channel_id\s*=", line),
                             "loops.example.toml must leave slack_channel_id unset")


class IntakeManifest(unittest.TestCase):
    """The intake step counts messages instead of reminding itself.

    A skill that tells a tick to notice a second message is a reminder, and a
    reminder is what fails: an operator's two back-to-back asks get read
    together, the first one acted on, the second left unseen for hours. What is
    guarded here is the SHAPE of the machinery that replaces the reminder — a
    literal count, a literal per-ts checklist, an accounting call against the
    ledger rather than against the tick's memory, and the refusal that keeps a
    tick from sleeping over an unticked box. An edit that softens any of the
    four back into "remember to check" is the thing that already failed.
    """

    def setUp(self):
        text = read(SKILL)
        self.manifest = sub_section(text, "The intake manifest")
        self.pace = sections(text)["6. Pacing and heartbeat"]

    def test_the_manifest_has_its_own_subsection(self):
        self.assertRegex(flat(self.manifest), r"(?i)newest-first")

    def test_the_count_and_the_list_are_both_literal_output(self):
        """A summary ("a couple of new ones") is what this replaces."""
        self.assertRegex(self.manifest, r"N new: \[<ts1>, <ts2>, \.\.\.\]")
        self.assertRegex(flat(self.manifest),
                         r"(?i)literally, as text, before acting")
        self.assertRegex(flat(self.manifest),
                         r"(?i)bracketed list has exactly `N` entries")

    def test_an_empty_read_still_writes_its_zero_line(self):
        """No manifest and nothing new must not look the same afterwards."""
        self.assertRegex(self.manifest, r"`0 new: \[\]`")

    def test_the_checklist_is_per_ts(self):
        self.assertRegex(self.manifest, r"processed: \[ \] <ts1> \[ \] <ts2>")

    def test_the_accounting_is_checked_against_the_ledger(self):
        """The checklist is what the tick believes; the ledger is what it holds."""
        self.assertIn("state/ledger.jsonl", self.manifest)
        self.assertRegex(self.manifest, r"processed <M> of <N>")

    def test_the_tick_may_not_end_with_an_unticked_box(self):
        self.assertRegex(self.manifest, r"`M` must equal `N`")
        for step in ("sweeps", "drain", "wakeup", "ending the turn"):
            self.assertIn(step, self.manifest,
                          "the refusal has to name the steps it blocks")

    def test_the_drain_re_read_is_gated_on_the_same_count(self):
        """§6 is where the tick actually ends, so the gate must be reachable there."""
        self.assertRegex(flat(self.pace),
                         r"(?i)\*\*Clean means the manifest says so\*\*")
        self.assertRegex(self.pace, r"`M == N`")

    def test_the_manifest_is_skipped_channel_less(self):
        self.assertIn(GUARD, self.manifest.lower())



if __name__ == "__main__":
    unittest.main(verbosity=2)

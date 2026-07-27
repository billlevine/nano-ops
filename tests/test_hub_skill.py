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
        body = self.sections["7. Control channel unreachable"]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

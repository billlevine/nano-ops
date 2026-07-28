#!/usr/bin/env python3
"""Tests for bin/doorbell: multi-inbox config parsing, per-inbox cursors,
per-inbox kick cooldowns, the cheap-quiet-poll query, and the rule that the
session it kicks comes from config rather than being baked in.
Run: python3 tests/test_doorbell.py

Nothing here touches Slack or agent-deck — fetch_messages/kick/subprocess are
patched, and cursor/last_kick paths are pointed at a tempdir. No real state/
and no real loops.toml is read; the fixtures invent neutral channel ids and
inbox names, exactly as a stranger's install would.
"""
import contextlib
import importlib.util
import os
import tempfile
import tomllib
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "bin", "doorbell")
_spec = importlib.util.spec_from_loader(
    "doorbell", importlib.machinery.SourceFileLoader("doorbell", SCRIPT))
doorbell = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doorbell)


def cfg(text):
    return tomllib.loads(text)


@contextlib.contextmanager
def state_dirs():
    """Repoint the module's state paths at a fresh tempdir."""
    with tempfile.TemporaryDirectory() as d:
        hub, doorbell_state = os.path.join(d, "hub"), os.path.join(d, "doorbell")
        os.makedirs(hub)
        os.makedirs(doorbell_state)
        saved = (doorbell.HUB_DIR, doorbell.HUB_CURSOR, doorbell.STATE_DIR)
        doorbell.HUB_DIR, doorbell.HUB_CURSOR = hub, os.path.join(hub, "cursor")
        doorbell.STATE_DIR = doorbell_state
        try:
            yield hub, doorbell_state
        finally:
            doorbell.HUB_DIR, doorbell.HUB_CURSOR, doorbell.STATE_DIR = saved


TWO_INBOXES = """
[hub]
slack_channel_id = "D1"

[[hub.inbox]]
name = "self-dm"
channel_id = "D1"
disposition = "full-trust"
poll_seconds = 30

[[hub.inbox]]
name = "team-handoff"
channel_id = "C9"
disposition = "conservative"
poll_seconds = 60
"""


class TestLoadInboxes(unittest.TestCase):
    def test_list_is_parsed_in_order_with_dispositions_and_rates(self):
        got = doorbell.load_inboxes(cfg(TWO_INBOXES))
        self.assertEqual([i.name for i in got], ["self-dm", "team-handoff"])
        self.assertEqual([i.channel_id for i in got], ["D1", "C9"])
        self.assertEqual([i.disposition for i in got],
                         ["full-trust", "conservative"])
        self.assertEqual([i.poll_seconds for i in got], [30, 60])

    def test_primary_is_the_legacy_slack_channel_id(self):
        got = doorbell.load_inboxes(cfg(TWO_INBOXES))
        self.assertEqual([i.primary for i in got], [True, False])

    def test_primary_is_independent_of_declaration_order(self):
        flipped = """
[hub]
slack_channel_id = "D1"
[[hub.inbox]]
name = "other"
channel_id = "C9"
[[hub.inbox]]
name = "self-dm"
channel_id = "D1"
"""
        got = doorbell.load_inboxes(cfg(flipped))
        self.assertEqual([i.primary for i in got], [False, True])

    def test_legacy_config_without_inbox_tables_still_works(self):
        got = doorbell.load_inboxes(cfg('[hub]\nslack_channel_id = "D1"\n'))
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].name, got[0].channel_id), ("self-dm", "D1"))
        self.assertEqual(got[0].disposition, "full-trust")
        self.assertEqual(got[0].poll_seconds, doorbell.DEFAULT_POLL_S)
        self.assertTrue(got[0].primary)

    def test_no_channel_configured_is_no_inboxes(self):
        # The public core ships loops.example.toml with slack_channel_id
        # commented out and no [[hub.inbox]] tables. doorbell must resolve to
        # zero inboxes so main() exits cleanly on the missing-config path —
        # the "Slack is optional" invariant bin/ops doctor reports on.
        self.assertEqual(doorbell.load_inboxes(cfg("[hub]\n")), [])
        self.assertEqual(doorbell.load_inboxes({}), [])

    def test_unstated_disposition_is_never_full_trust(self):
        got = doorbell.load_inboxes(
            cfg('[hub]\nslack_channel_id = "D1"\n[[hub.inbox]]\nchannel_id = "C9"\n'))
        self.assertEqual(got[0].disposition, "conservative")

    def test_missing_channel_id_and_duplicate_names_are_errors(self):
        with self.assertRaises(ValueError):
            doorbell.load_inboxes(cfg('[hub]\n[[hub.inbox]]\nname = "x"\n'))
        with self.assertRaises(ValueError):
            doorbell.load_inboxes(cfg('[hub]\n[[hub.inbox]]\nname = "x"\n'
                                      'channel_id = "C1"\n[[hub.inbox]]\n'
                                      'name = "x"\nchannel_id = "C2"\n'))

    def test_name_is_filename_safe(self):
        got = doorbell.load_inboxes(
            cfg('[hub]\n[[hub.inbox]]\nname = "a/b c"\nchannel_id = "C1"\n'))
        self.assertEqual(got[0].name, "a-b-c")


class TestOperatorIdentityComesFromConfig(unittest.TestCase):
    """Nothing here names an operator: the session to kick and the agent-deck
    profile resolve from loops.toml [hub], defaulting to the neutral "ops"."""

    @contextlib.contextmanager
    def _patched_config(self, loader):
        saved = doorbell.load_config
        doorbell.load_config = loader
        try:
            yield
        finally:
            doorbell.load_config = saved

    def test_missing_or_unparseable_registry_degrades_to_empty(self):
        for boom in (OSError("no loops.toml"),
                     tomllib.TOMLDecodeError("bad", "", 0)):
            def raiser(path=None, exc=boom):
                raise exc
            with self._patched_config(raiser):
                self.assertEqual(doorbell.hub_config(), {})

    def test_non_table_hub_section_degrades_to_empty(self):
        with self._patched_config(lambda path=None: {"hub": "nonsense"}):
            self.assertEqual(doorbell.hub_config(), {})

    def test_persona_drives_the_session_title_with_a_neutral_default(self):
        with self._patched_config(lambda path=None: {}):
            hub = doorbell.hub_config()
        persona = hub.get("persona") or "ops"
        self.assertEqual(persona, "ops")
        self.assertEqual(hub.get("session_title") or f"{persona} (hub)", "ops (hub)")

    def test_kick_addresses_the_configured_session_and_profile(self):
        calls = []

        class Result:
            returncode = 0
            stderr = ""

        saved = (doorbell.subprocess.run, doorbell.HUB_SESSION, doorbell.DECK_PROFILE)
        try:
            doorbell.subprocess.run = lambda argv, **kw: (calls.append(argv) or Result())
            doorbell.HUB_SESSION, doorbell.DECK_PROFILE = "somebody (hub)", "someprofile"
            self.assertTrue(doorbell.kick("self-dm"))
        finally:
            doorbell.subprocess.run, doorbell.HUB_SESSION, doorbell.DECK_PROFILE = saved
        self.assertEqual(len(calls), 1)
        self.assertIn("somebody (hub)", calls[0])
        self.assertIn("someprofile", calls[0])


class TestCursorPaths(unittest.TestCase):
    def test_primary_keeps_the_bare_cursor_others_are_suffixed(self):
        with state_dirs() as (hub, kicks):
            primary, other = doorbell.load_inboxes(cfg(TWO_INBOXES))
            self.assertEqual(primary.cursor_path, os.path.join(hub, "cursor"))
            self.assertEqual(other.cursor_path,
                             os.path.join(hub, "cursor.team-handoff"))
            self.assertNotEqual(primary.last_kick_path, other.last_kick_path)
            self.assertTrue(other.last_kick_path.startswith(kicks))


class TestQuietPollIsCheap(unittest.TestCase):
    def test_cursor_becomes_a_server_side_oldest_filter(self):
        q = doorbell.history_query("C9", "1700000000.000100")
        self.assertIn("oldest=1700000000.000100", q)
        self.assertIn("inclusive=false", q)

    def test_no_cursor_yet_means_no_oldest(self):
        self.assertNotIn("oldest", doorbell.history_query("C9", None))
        self.assertNotIn("oldest", doorbell.history_query("C9", "0"))


class TestNeedsKick(unittest.TestCase):
    def test_hub_posts_and_subtypes_are_ignored(self):
        msgs = [{"ts": "9", "text": "⚙️ posted by the hub"},
                {"ts": "8", "text": "joined", "subtype": "channel_join"},
                {"ts": "7", "text": "hey"}]
        self.assertEqual(doorbell.needs_kick(msgs, 5.0), "7")
        self.assertIsNone(doorbell.needs_kick(msgs, 7.0))


class TestPollInbox(unittest.TestCase):
    def setUp(self):
        self.kicks = []
        self.fetched = []
        self._saved = (doorbell.kick, doorbell.fetch_messages)
        doorbell.kick = lambda name: (self.kicks.append(name) or True)

    def tearDown(self):
        doorbell.kick, doorbell.fetch_messages = self._saved

    def _fetch(self, messages):
        def fake(token, channel, oldest=None):
            self.fetched.append((channel, oldest))
            return messages
        doorbell.fetch_messages = fake

    def test_kick_names_the_inbox_and_uses_that_inbox_cursor(self):
        with state_dirs():
            primary, other = doorbell.load_inboxes(cfg(TWO_INBOXES))
            with open(primary.cursor_path, "w") as f:
                f.write("100.0")           # the primary inbox is caught up...
            self._fetch([{"ts": "50.0", "text": "work handoff"}])
            # ...but the conservative inbox has its own (absent) cursor, so a
            # ts below the primary cursor still counts as new there.
            self.assertEqual(doorbell.poll_inbox("tok", other), "50.0")
            self.assertEqual(self.kicks, ["team-handoff"])
            self.assertEqual(self.fetched, [("C9", None)])
            self.assertTrue(os.path.exists(other.last_kick_path))
            self.assertFalse(os.path.exists(primary.last_kick_path))

    def test_cooldown_is_per_inbox(self):
        with state_dirs():
            primary, other = doorbell.load_inboxes(cfg(TWO_INBOXES))
            self._fetch([{"ts": "50.0", "text": "hi"}])
            self.assertEqual(doorbell.poll_inbox("tok", other, now=1000.0), "50.0")
            # same inbox, inside the cooldown → no second kick
            self.assertIsNone(doorbell.poll_inbox("tok", other, now=1001.0))
            # a different inbox is not muzzled by its sibling's kick
            self.assertEqual(doorbell.poll_inbox("tok", primary, now=1001.0), "50.0")
            self.assertEqual(self.kicks, ["team-handoff", "self-dm"])
            # past the cooldown, the first inbox kicks again
            self.assertEqual(
                doorbell.poll_inbox("tok", other,
                                    now=1000.0 + doorbell.KICK_COOLDOWN_S + 1), "50.0")

    def test_kick_message_mentions_the_inbox(self):
        msg = doorbell.kick_message("team-handoff")
        self.assertTrue(msg.startswith("doorbell:"))  # hub keys on this prefix
        self.assertIn("team-handoff", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)

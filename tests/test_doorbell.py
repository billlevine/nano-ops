#!/usr/bin/env python3
"""Tests for bin/doorbell: multi-inbox config parsing, per-inbox cursors,
per-inbox kick cooldowns, the cheap-quiet-poll query, the rule that the session
it kicks comes from config rather than being baked in, the `read` verb's
warrant, and the pane sweep.
Run: python3 tests/test_doorbell.py

Nothing here touches Slack, agent-deck or tmux — fetch_messages/fetch_history/
kick/subprocess are patched and the sweep's deck and capture readers are
injected, and cursor/last_kick/pane-state paths are pointed at a tempdir. No
real state/ and no real loops.toml is read; the fixtures invent neutral channel
ids, inbox names and session titles, exactly as a stranger's install would.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
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
        # Restored in tearDown: these patches used to outlive the class, so a
        # later test read the poller's fake instead of the real function.
        self.saved = (doorbell.kick, doorbell.fetch_messages)
        doorbell.kick = lambda name: (self.kicks.append(name) or True)

    def tearDown(self):
        doorbell.kick, doorbell.fetch_messages = self.saved

    def _fetch(self, messages):
        def fake(token, channel, oldest=None):
            self.fetched.append((channel, oldest))
            return messages
        doorbell.fetch_messages = fake

    def test_kick_names_the_inbox_and_uses_that_inbox_cursor(self):
        with state_dirs():
            primary, other = doorbell.load_inboxes(cfg(TWO_INBOXES))
            with open(primary.cursor_path, "w") as f:
                f.write("100.0")           # self-dm is caught up...
            self._fetch([{"ts": "50.0", "text": "work handoff"}])
            # ...but the conservative inbox has its own (absent) cursor, so a
            # ts below the self-dm cursor still counts as new there.
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


class TestReadVerb(unittest.TestCase):
    """`bin/doorbell read` — the hub's fallback read, with its warrant (t-782).

    The thing under test is not "does it return messages". It is whether a read
    that saw only part of its window says so, because the improvised fallback
    of 2026-08-16 fetched Slack's truncation flag and threw it away.
    """

    def setUp(self):
        self.calls = []
        self.saved = (doorbell.fetch_history, doorbell.TOKEN_FILE)
        # The verb reads the token off disk itself, exactly as poll_inbox does.
        self.token_dir = tempfile.TemporaryDirectory()
        doorbell.TOKEN_FILE = os.path.join(self.token_dir.name, "slack-user-token")
        with open(doorbell.TOKEN_FILE, "w") as f:
            f.write("xoxp-test\n")

    def tearDown(self):
        doorbell.fetch_history, doorbell.TOKEN_FILE = self.saved
        self.token_dir.cleanup()

    def _history(self, payload):
        def fake(token, channel, oldest=None):
            self.calls.append((channel, oldest))
            if isinstance(payload, Exception):
                raise payload
            return payload
        doorbell.fetch_history = fake

    def inbox(self):
        return doorbell.load_inboxes(cfg(TWO_INBOXES))[0]

    def read_main(self, argv, inbox):
        """Run the verb, capturing the JSON it prints. Returns (exit code, doc)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = doorbell.read_main(argv, [inbox])
        return code, json.loads(buf.getvalue())

    def test_a_clean_page_is_observed_and_can_prove_a_quiet_inbox(self):
        self._history({"ok": True, "messages": [{"ts": "9", "text": "hi"}]})
        reading, messages = doorbell.read_inbox("tok", self.inbox(), "5")
        self.assertEqual(reading["outcome"], "ok")
        self.assertEqual(reading["warrant"], "observed")
        self.assertFalse(reading["partial"])
        self.assertTrue(reading["absence_is_evidence"])
        self.assertEqual(messages, [{"ts": "9", "text": "hi"}])
        self.assertEqual(self.calls, [("D1", "5")])

    def test_has_more_is_partial_and_may_not_prove_anything_absent(self):
        self._history({"ok": True, "messages": [{"ts": "9"}], "has_more": True})
        reading, messages = doorbell.read_inbox("tok", self.inbox(), None)
        self.assertEqual(reading["warrant"], "observed")  # the ten it returned are real
        self.assertTrue(reading["partial"])
        self.assertFalse(reading["absence_is_evidence"])
        self.assertEqual(messages, [{"ts": "9"}])
        self.assertIn("older", reading["why"])

    def test_a_next_cursor_is_partial_even_without_has_more(self):
        self._history({"ok": True, "messages": [],
                       "response_metadata": {"next_cursor": "dXNlcjpVMDYxTkZUVDI="}})
        reading, _ = doorbell.read_inbox("tok", self.inbox(), None)
        self.assertTrue(reading["partial"])
        self.assertFalse(reading["absence_is_evidence"])
        self.assertEqual(reading["detail"]["next_cursor"], "dXNlcjpVMDYxTkZUVDI=")

    def test_the_three_failure_shapes_are_kept_apart(self):
        for exc, outcome in ((RuntimeError("slack api error: permission_error"),
                              "api_error"),
                             (ValueError("Expecting value"), "unreadable"),
                             (OSError("connection reset"), "unreachable")):
            with self.subTest(outcome=outcome):
                self._history(exc)
                reading, messages = doorbell.read_inbox("tok", self.inbox(), None)
                self.assertEqual(reading["outcome"], outcome)
                self.assertEqual(reading["warrant"], "failed")
                self.assertFalse(reading["absence_is_evidence"])
                self.assertEqual(messages, [])

    def test_a_token_that_was_never_there_is_setup_not_weather(self):
        self._history({"ok": True, "messages": []})
        reading, _ = doorbell.read_inbox("", self.inbox(), None)
        self.assertEqual(reading["outcome"], "no_credential")
        self.assertEqual(reading["warrant"], "not_attempted")
        self.assertEqual(self.calls, [])  # nothing was asked

    def test_the_window_defaults_to_the_inboxs_own_committed_cursor(self):
        with state_dirs():
            inbox = self.inbox()
            with open(inbox.cursor_path, "w") as f:
                f.write("1700000000.000100")
            self._history({"ok": True, "messages": []})
            self.read_main(["--inbox", "self-dm"], inbox)
            self.assertEqual(self.calls, [("D1", "1700000000.000100")])

    def test_an_explicit_oldest_wins_over_the_cursor(self):
        with state_dirs():
            inbox = self.inbox()
            with open(inbox.cursor_path, "w") as f:
                f.write("1700000000.000100")
            self._history({"ok": True, "messages": []})
            self.read_main(["--inbox", "self-dm", "--oldest", "42.0"], inbox)
            self.assertEqual(self.calls, [("D1", "42.0")])

    def test_no_cursor_yet_means_no_oldest_filter(self):
        with state_dirs():
            inbox = self.inbox()
            self._history({"ok": True, "messages": []})
            self.read_main(["--inbox", "self-dm"], inbox)
            self.assertEqual(self.calls, [("D1", None)])

    def test_the_exit_code_carries_the_warrant(self):
        with state_dirs():
            inbox = self.inbox()
            self._history({"ok": True, "messages": []})
            self.assertEqual(self.read_main(["--inbox", "self-dm"], inbox)[0], 0)
            self._history({"ok": True, "messages": [], "has_more": True})
            self.assertEqual(self.read_main(["--inbox", "self-dm"], inbox)[0], 2)
            self._history(RuntimeError("slack api error: permission_error"))
            self.assertEqual(self.read_main(["--inbox", "self-dm"], inbox)[0], 3)

    def test_an_unknown_inbox_and_a_missing_flag_both_refuse(self):
        inbox = self.inbox()
        for argv in (["--inbox", "nope"], [], ["--inbox", "self-dm", "--send"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                doorbell.read_main(argv, [inbox])

    def test_the_read_writes_no_state(self):
        """Read-only, like the rest of the script: no cursor, no kick marker."""
        with state_dirs() as (hub, kicks):
            inbox = self.inbox()
            self._history({"ok": True, "messages": [{"ts": "9", "text": "hi"}]})
            code, doc = self.read_main(["--inbox", "self-dm"], inbox)
            self.assertEqual((code, doc["inbox"], doc["warrant"]),
                             (0, "self-dm", "observed"))
            self.assertEqual(doc["messages"], [{"ts": "9", "text": "hi"}])
            self.assertEqual(os.listdir(hub), [])
            self.assertEqual(os.listdir(kicks), [])

    def test_the_poller_still_sees_only_the_messages(self):
        """fetch_messages is the poller's view of the same one HTTP call."""
        self._history({"ok": True, "messages": [{"ts": "9"}], "has_more": True})
        self.assertEqual(doorbell.fetch_messages("tok", "D1"), [{"ts": "9"}])


HOUR = 3600.0


def watch(idle=HOUR, sweep=HOUR, lines=50, overrides=()):
    return doorbell.PaneWatch(sweep_seconds=int(sweep), idle_seconds=int(idle),
                              capture_lines=int(lines),
                              overrides=tuple(sorted(overrides,
                                                     key=lambda p: (-len(p[0]),
                                                                    p[0]))))


def deck_of(*titles, status="running", tmux=None):
    """A fake `agent-deck ls --json` read: these titles, all alive."""
    def fake():
        return ({t: {"status": status,
                     "tmux_session": (tmux or {}).get(t, f"tmux-{t}")}
                 for t in titles}, "ok", None)
    return fake


def deck_down(outcome="no_tool", why="agent-deck is not on PATH"):
    return lambda: ({}, outcome, why)


def capture_of(panes, outcome="tmux_error", why="tmux fell over"):
    """A fake pane capture. Anything not in `panes` fails with `outcome`."""
    def fake(tmux_session, lines):
        if tmux_session in panes:
            return panes[tmux_session], "ok", None
        return None, outcome, why
    return fake


def row_for(doc, title):
    return next(r for r in doc["rows"] if r["title"] == title)


class TestPaneWatchConfig(unittest.TestCase):
    def test_no_table_at_all_is_the_documented_defaults(self):
        got = doorbell.load_pane_watch(cfg('[hub]\nslack_channel_id = "D1"\n'))
        self.assertEqual((got.sweep_seconds, got.idle_seconds, got.capture_lines),
                         (doorbell.PANE_DEFAULT_SWEEP_S,
                          doorbell.PANE_DEFAULT_IDLE_S,
                          doorbell.PANE_DEFAULT_LINES))
        self.assertEqual(got.overrides, ())

    def test_every_key_is_configurable(self):
        got = doorbell.load_pane_watch(cfg(
            "[hub.pane_watch]\nsweep_seconds = 900\nidle_seconds = 120\n"
            "capture_lines = 10\n"))
        self.assertEqual((got.sweep_seconds, got.idle_seconds, got.capture_lines),
                         (900, 120, 10))

    def test_an_exact_title_beats_a_substring_and_the_longest_substring_wins(self):
        got = doorbell.load_pane_watch(cfg(
            "[hub.pane_watch]\nidle_seconds = 3600\n"
            "[hub.pane_watch.overrides]\n"
            '"ephemeral - " = 1800\n'
            '"ephemeral - slow" = 7200\n'
            '"ephemeral - slow-one" = 60\n'))
        self.assertEqual(got.threshold("ephemeral - slow-one"), 60)   # exact
        self.assertEqual(got.threshold("ephemeral - slow-two"), 7200)  # longest
        self.assertEqual(got.threshold("ephemeral - other"), 1800)
        self.assertEqual(got.threshold("ops (hub)"), 3600)

    def test_a_malformed_table_refuses_rather_than_disabling_itself(self):
        for text in ("[hub.pane_watch]\nsweep_seconds = 1\n",
                     "[hub.pane_watch]\nidle_seconds = -5\n",
                     "[hub.pane_watch]\ncapture_lines = 0\n",
                     '[hub.pane_watch]\nidle_seconds = "soon"\n',
                     '[hub.pane_watch.overrides]\n"x" = -1\n',
                     '[hub.pane_watch.overrides]\n"x" = "never"\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                doorbell.load_pane_watch(cfg(text))

    def test_the_shipped_example_registry_parses(self):
        # loops.example.toml is the documented shape a stranger copies. It must
        # resolve to the defaults, which is also the no-table case.
        example = os.path.join(os.path.dirname(os.path.dirname(SCRIPT)),
                               "loops.example.toml")
        with open(example, "rb") as f:
            got = doorbell.load_pane_watch(tomllib.load(f))
        self.assertGreaterEqual(got.sweep_seconds, doorbell.PANE_MIN_SWEEP_S)

    def test_durations_parse_with_and_without_a_unit(self):
        self.assertEqual(doorbell.parse_seconds("90", "x"), 90)
        self.assertEqual(doorbell.parse_seconds("30m", "x"), 1800)
        self.assertEqual(doorbell.parse_seconds("2h", "x"), 7200)
        self.assertEqual(doorbell.parse_seconds("1d", "x"), 86400)
        for bad in ("", "-1", "soon", "3x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                doorbell.parse_seconds(bad, "x")


class TestPaneDigest(unittest.TestCase):
    def test_tmux_line_padding_is_not_activity(self):
        """A redraw pads lines out to the pane width. Reading that as activity
        is the direction that HIDES a stall."""
        self.assertEqual(doorbell.pane_digest("> prompt\n\n"),
                         doorbell.pane_digest("> prompt   \n   \n\n\n"))

    def test_a_real_change_changes_the_hash(self):
        self.assertNotEqual(doorbell.pane_digest("> prompt"),
                            doorbell.pane_digest("> prompt\nthinking..."))


class TestPaneSweep(unittest.TestCase):
    """t-1094 — the pane sweep, which is the only check in this estate that
    reads the live terminal rather than a store."""

    def sweep(self, panes, now, state=None, titles=("worker",), status="running",
              w=None, capture=None):
        return doorbell.pane_sweep(
            w or watch(), now=now,
            state=state if state is not None
            else {"version": 1, "last_sweep_at": None, "sessions": {}},
            deck=deck_of(*titles, status=status),
            capture=capture or capture_of(panes))

    def test_a_pane_that_changed_between_sweeps_is_never_flagged(self):
        first = self.sweep({"tmux-worker": "one"}, now=0.0)
        later = self.sweep({"tmux-worker": "two"}, now=10 * HOUR,
                           state=first["state"])
        row = row_for(later, "worker")
        self.assertEqual(row["verdict"], doorbell.PANE_ACTIVE)
        self.assertEqual(later["counts"][doorbell.PANE_STALLED], 0)
        self.assertIsNone(later["state"]["sessions"]["worker"]["flagged_at"])
        # and the clock restarted from the change, not from the first sweep
        self.assertEqual(later["state"]["sessions"]["worker"]["first_seen_at"],
                         10 * HOUR)

    def test_a_first_look_is_a_baseline_and_not_evidence_of_activity(self):
        doc = self.sweep({"tmux-worker": "one"}, now=0.0)
        row = row_for(doc, "worker")
        self.assertEqual(row["verdict"], doorbell.PANE_ACTIVE)
        self.assertTrue(row["first_look"])

    def test_unchanged_inside_the_threshold_is_settling(self):
        first = self.sweep({"tmux-worker": "idle"}, now=0.0)
        later = self.sweep({"tmux-worker": "idle"}, now=HOUR - 1,
                           state=first["state"])
        row = row_for(later, "worker")
        self.assertEqual(row["verdict"], doorbell.PANE_SETTLING)
        self.assertEqual(row["idle_for"], HOUR - 1)
        self.assertEqual(later["counts"][doorbell.PANE_STALLED], 0)

    def test_an_identical_pane_past_the_threshold_is_flagged(self):
        first = self.sweep({"tmux-worker": "› Explain this codebase"}, now=0.0)
        later = self.sweep({"tmux-worker": "› Explain this codebase"},
                           now=27 * HOUR, state=first["state"])
        row = row_for(later, "worker")
        self.assertEqual(row["verdict"], doorbell.PANE_STALLED)
        self.assertEqual(row["idle_for"], 27 * HOUR)
        self.assertEqual(row["flagged_at"], 27 * HOUR)
        self.assertEqual(later["counts"][doorbell.PANE_STALLED], 1)
        entry = later["state"]["sessions"]["worker"]
        self.assertEqual(entry["flagged_at"], 27 * HOUR)
        self.assertEqual(entry["flagged_hash"], entry["hash"])
        # the clock is measured from when the bytes first appeared, so an extra
        # sweep in between cannot reset it
        self.assertEqual(entry["first_seen_at"], 0.0)

    def test_a_session_already_flagged_is_not_flagged_again(self):
        """One flag per observation. A
        genuinely-idle-forever session must not announce itself every hour."""
        state = None
        for now in (0.0, 2 * HOUR):
            doc = self.sweep({"tmux-worker": "idle"}, now=now,
                             state=state if state else None)
            state = doc["state"]
        self.assertEqual(row_for(doc, "worker")["verdict"], doorbell.PANE_STALLED)
        for now in (3 * HOUR, 4 * HOUR, 50 * HOUR):
            doc = self.sweep({"tmux-worker": "idle"}, now=now, state=state)
            state = doc["state"]
            row = row_for(doc, "worker")
            self.assertEqual(row["verdict"], doorbell.PANE_HELD)
            self.assertEqual(doc["counts"][doorbell.PANE_STALLED], 0)
            # the flag is not re-stamped, so "flagged 48h ago" stays true
            self.assertEqual(state["sessions"]["worker"]["flagged_at"], 2 * HOUR)

    def test_a_pane_that_moves_again_re_arms_the_flag(self):
        first = self.sweep({"tmux-worker": "idle"}, now=0.0)
        flagged = self.sweep({"tmux-worker": "idle"}, now=2 * HOUR,
                             state=first["state"])
        back = self.sweep({"tmux-worker": "working"}, now=3 * HOUR,
                          state=flagged["state"])
        self.assertEqual(row_for(back, "worker")["verdict"], doorbell.PANE_ACTIVE)
        self.assertIsNone(back["state"]["sessions"]["worker"]["flagged_at"])
        again = self.sweep({"tmux-worker": "working"}, now=9 * HOUR,
                           state=back["state"])
        self.assertEqual(row_for(again, "worker")["verdict"], doorbell.PANE_STALLED)

    def test_a_session_at_zero_is_exempt_however_long_it_rests(self):
        """A session with no cadence does not tick. An idle prompt is its resting
        state, so no unchanged-duration is anomalous for it."""
        w = watch(overrides=[("ops (advisor)", 0)])
        first = self.sweep({"tmux-ops (advisor)": "idle"},
                           now=0.0, titles=("ops (advisor)",), w=w)
        later = self.sweep({"tmux-ops (advisor)": "idle"},
                           now=200 * HOUR, titles=("ops (advisor)",),
                           state=first["state"], w=w)
        row = row_for(later, "ops (advisor)")
        self.assertEqual(row["verdict"], doorbell.PANE_SKIPPED)
        self.assertEqual(row["reading"]["outcome"], "exempt")
        self.assertEqual(later["counts"][doorbell.PANE_STALLED], 0)

    def test_a_session_that_is_not_alive_is_skipped_not_stalled(self):
        doc = self.sweep({"tmux-worker": "idle"}, now=0.0, status="stopped")
        row = row_for(doc, "worker")
        self.assertEqual(row["verdict"], doorbell.PANE_SKIPPED)
        self.assertEqual(row["reading"]["outcome"], "not_running")
        # forgotten, so a session that comes back starts a fresh baseline
        self.assertNotIn("worker", doc["state"]["sessions"])

    def test_a_session_agent_deck_no_longer_lists_is_dropped(self):
        panes = {"tmux-worker": "idle", "tmux-other": "idle"}
        first = self.sweep(panes, now=0.0, titles=("worker", "other"))
        self.assertEqual(sorted(first["state"]["sessions"]), ["other", "worker"])
        later = self.sweep(panes, now=HOUR, titles=("worker",),
                           state=first["state"])
        self.assertEqual(list(later["state"]["sessions"]), ["worker"])


class TestPaneSweepDegradesGracefully(unittest.TestCase):
    """A failed look may never become a verdict (docs/absence-contract.md)."""

    def test_tmux_missing_is_unobservable_and_flags_nothing(self):
        doc = doorbell.pane_sweep(
            watch(), now=0.0, state={"version": 1, "last_sweep_at": None,
                                     "sessions": {}},
            deck=deck_of("worker"),
            capture=lambda s, n: (None, "no_tool", "tmux is not on PATH"))
        row = row_for(doc, "worker")
        self.assertEqual(row["verdict"], doorbell.PANE_UNOBSERVABLE)
        self.assertEqual(row["reading"]["outcome"], "no_tool")
        self.assertFalse(row["reading"]["absence_is_evidence"])
        self.assertEqual(doc["counts"][doorbell.PANE_STALLED], 0)

    def test_a_failed_capture_cannot_start_a_stall_clock(self):
        """A capture that failed is not a pane that said nothing."""
        first = doorbell.pane_sweep(
            watch(), now=0.0, state={"version": 1, "last_sweep_at": None,
                                     "sessions": {}},
            deck=deck_of("worker"), capture=capture_of({}))
        self.assertEqual(first["state"]["sessions"], {})
        later = doorbell.pane_sweep(
            watch(), now=50 * HOUR, state=first["state"],
            deck=deck_of("worker"), capture=capture_of({}))
        self.assertEqual(row_for(later, "worker")["verdict"],
                         doorbell.PANE_UNOBSERVABLE)
        self.assertEqual(later["counts"][doorbell.PANE_STALLED], 0)

    def test_a_failed_capture_leaves_a_standing_entry_untouched(self):
        first = doorbell.pane_sweep(
            watch(), now=0.0, state={"version": 1, "last_sweep_at": None,
                                     "sessions": {}},
            deck=deck_of("worker"), capture=capture_of({"tmux-worker": "idle"}))
        flagged = doorbell.pane_sweep(
            watch(), now=2 * HOUR, state=first["state"], deck=deck_of("worker"),
            capture=capture_of({"tmux-worker": "idle"}))
        blind = doorbell.pane_sweep(
            watch(), now=3 * HOUR, state=flagged["state"], deck=deck_of("worker"),
            capture=capture_of({}))
        self.assertEqual(row_for(blind, "worker")["verdict"],
                         doorbell.PANE_UNOBSERVABLE)
        # neither cleared nor re-flagged, and the clock did not move
        self.assertEqual(blind["state"]["sessions"]["worker"],
                         flagged["state"]["sessions"]["worker"])

    def test_a_dead_pane_under_a_live_record_is_named_as_such(self):
        doc = doorbell.pane_sweep(
            watch(), now=0.0, state={"version": 1, "last_sweep_at": None,
                                     "sessions": {}},
            deck=deck_of("worker"),
            capture=lambda s, n: (None, "no_binding", "tmux has no live pane"))
        self.assertEqual(row_for(doc, "worker")["reading"]["outcome"],
                         "no_binding")

    def test_agent_deck_down_writes_nothing_and_concludes_nothing(self):
        """An empty row list here means nobody looked. Pruning the state on it
        would erase every standing flag."""
        prior = {"version": 1, "last_sweep_at": 0.0,
                 "sessions": {"worker": {"tmux_session": "tmux-worker",
                                         "status": "running", "hash": "abc",
                                         "first_seen_at": 0.0,
                                         "last_seen_at": 0.0,
                                         "flagged_at": 2 * HOUR,
                                         "flagged_hash": "abc"}}}
        doc = doorbell.pane_sweep(watch(), now=9 * HOUR, state=prior,
                                  deck=deck_down(),
                                  capture=capture_of({"tmux-worker": "idle"}))
        self.assertEqual(doc["rows"], [])
        self.assertIsNone(doc["state"])          # nothing is written
        self.assertFalse(doc["reading"]["absence_is_evidence"])

    def test_agent_deck_answering_nonsense_is_not_an_empty_estate(self):
        for raw, outcome in (("not json at all", "unreadable"),
                             ('{"sessions": []}', "unreadable")):
            with self.subTest(outcome=outcome):
                with tempfile.NamedTemporaryFile("w", suffix=".json",
                                                 delete=False) as fh:
                    fh.write(raw)
                sessions, got, why = doorbell.read_deck_sessions(fixture=fh.name)
                os.unlink(fh.name)
                self.assertEqual((sessions, got), ({}, outcome))
                self.assertTrue(why)

    def test_a_corrupt_state_file_is_an_empty_baseline_not_a_stall(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pane-hashes.json")
            for raw in ("", "{", '{"sessions": "nope"}', "[]"):
                with open(path, "w") as fh:
                    fh.write(raw)
                self.assertEqual(doorbell.load_pane_state(path)["sessions"], {})
            self.assertEqual(
                doorbell.load_pane_state(os.path.join(d, "absent.json")),
                {"version": doorbell.PANE_STATE_VERSION,
                 "last_sweep_at": None, "sessions": {}})


class TestPaneCaptureCall(unittest.TestCase):
    """The tmux call itself: exact targeting, and what a failure means."""

    def run_with(self, returncode, stdout="", stderr=""):
        seen = {}

        class Result:
            pass
        result = Result()
        result.returncode, result.stdout, result.stderr = returncode, stdout, stderr

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return result
        saved = (doorbell.subprocess.run, doorbell.shutil.which)
        doorbell.subprocess.run = fake_run
        doorbell.shutil.which = lambda name: f"/usr/bin/{name}"
        try:
            return seen, doorbell.capture_pane("agentdeck_x_1", 50)
        finally:
            doorbell.subprocess.run, doorbell.shutil.which = saved

    def test_the_target_is_exact(self):
        """A bare name is a PATTERN in tmux — a prefix that matched the wrong
        session would hash somebody else's terminal."""
        seen, (text, outcome, _) = self.run_with(0, stdout="hello\n")
        self.assertIn("=agentdeck_x_1:", seen["argv"])
        self.assertIn("-S", seen["argv"])
        self.assertIn("-50", seen["argv"])
        self.assertEqual((text, outcome), ("hello\n", "ok"))

    def test_a_pane_that_is_gone_is_told_apart_from_a_broken_tmux(self):
        _, (_, outcome, why) = self.run_with(1, stderr="can't find pane: =x:")
        self.assertEqual(outcome, "no_binding")
        self.assertIn("died under", why)
        _, (_, outcome, _) = self.run_with(1, stderr="usage: capture-pane")
        self.assertEqual(outcome, "tmux_error")

    def test_tmux_not_on_path_is_setup_and_not_weather(self):
        saved = doorbell.shutil.which
        doorbell.shutil.which = lambda name: None
        try:
            self.assertEqual(doorbell.capture_pane("x", 50)[1], "no_tool")
        finally:
            doorbell.shutil.which = saved


class TestPaneReport(unittest.TestCase):
    """`panes --report` answers out of the last sweep, so it has to say when
    the last sweep was too old to speak for now."""

    def state(self, last_sweep_at, flagged_at=2 * HOUR):
        return {"version": 1, "last_sweep_at": last_sweep_at,
                "sessions": {"worker": {"tmux_session": "tmux-worker",
                                        "status": "running", "hash": "abc",
                                        "first_seen_at": 0.0,
                                        "last_seen_at": last_sweep_at,
                                        "flagged_at": flagged_at,
                                        "flagged_hash": "abc"},
                             "busy": {"tmux_session": "tmux-busy",
                                      "status": "running", "hash": "def",
                                      "first_seen_at": 0.0,
                                      "last_seen_at": last_sweep_at,
                                      "flagged_at": None,
                                      "flagged_hash": None}}}

    def test_only_flagged_sessions_are_listed(self):
        doc = doorbell.pane_report(self.state(3 * HOUR), watch(), 3.5 * HOUR)
        self.assertEqual([r["title"] for r in doc["flagged"]], ["worker"])
        self.assertTrue(doc["reading"]["absence_is_evidence"])

    def test_a_sweeper_that_stopped_running_may_not_report_all_clear(self):
        doc = doorbell.pane_report(
            {"version": 1, "last_sweep_at": 0.0, "sessions": {}},
            watch(sweep=HOUR), 5 * HOUR)
        self.assertEqual(doc["flagged"], [])
        self.assertEqual(doc["reading"]["outcome"], "stale_sweep")
        self.assertFalse(doc["reading"]["absence_is_evidence"])

    def test_a_sweep_that_never_ran_is_not_a_quiet_estate(self):
        doc = doorbell.pane_report(doorbell.load_pane_state("/nonexistent/x"),
                                   watch(), 1000.0)
        self.assertEqual(doc["reading"]["outcome"], "no_state")
        self.assertFalse(doc["reading"]["absence_is_evidence"])


class TestPaneSweepIsReportOnly(unittest.TestCase):
    def test_a_stall_never_kicks_anything(self):
        """An idle prompt is a legitimate resting state for some sessions, so a
        flag is a signal for a person, never a trigger."""
        saved = doorbell.kick
        doorbell.kick = lambda name: self.fail("the pane sweep kicked a session")
        try:
            first = doorbell.pane_sweep(
                watch(), now=0.0, state={"version": 1, "last_sweep_at": None,
                                         "sessions": {}},
                deck=deck_of("worker"),
                capture=capture_of({"tmux-worker": "idle"}))
            doc = doorbell.pane_sweep(
                watch(), now=27 * HOUR, state=first["state"],
                deck=deck_of("worker"),
                capture=capture_of({"tmux-worker": "idle"}))
        finally:
            doorbell.kick = saved
        self.assertEqual(doc["counts"][doorbell.PANE_STALLED], 1)

    def test_the_daemon_pass_announces_a_new_stall_once(self):
        with tempfile.TemporaryDirectory() as d:
            saved = doorbell.PANE_STATE
            doorbell.PANE_STATE = os.path.join(d, "doorbell", "pane-hashes.json")
            try:
                out = []
                for now in (0.0, 2 * HOUR, 3 * HOUR):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        doorbell.sweep_panes_once(
                            watch(), now=now, deck=deck_of("worker"),
                            capture=capture_of({"tmux-worker": "idle"}))
                    out.append(buf.getvalue())
                self.assertNotIn("pane-stall", out[0])
                self.assertIn("pane-stall worker", out[1])
                self.assertIn("REPORT ONLY", out[1])
                self.assertNotIn("pane-stall", out[2])   # held, not re-announced
                # and the record survives for `panes --report` to answer from
                state = doorbell.load_pane_state()
                self.assertEqual(state["sessions"]["worker"]["flagged_at"],
                                 2 * HOUR)
            finally:
                doorbell.PANE_STATE = saved


class TestPaneSweepDoesNotNeedSlack(unittest.TestCase):
    """Slack is optional in this core: loops.example.toml ships with no
    slack_channel_id and no [[hub.inbox]] tables. The pane sweep watches
    agent-deck sessions, not channels, so `panes` must survive that — the guard
    that stops the POLLER is dispatched after it, never before."""

    def test_panes_is_dispatched_before_the_no_inbox_guard(self):
        source = open(SCRIPT).read()
        body = source[source.index("def main():"):]
        panes = body.index('argv[0] == "panes"')
        guard = body.index("no inboxes configured")
        self.assertLess(panes, guard)
        # `read` names one inbox, so it stays behind the guard.
        self.assertGreater(body.index('argv[0] == "read"'), guard)

    def test_the_shipped_example_registry_configures_no_inbox(self):
        example = os.path.join(os.path.dirname(os.path.dirname(SCRIPT)),
                               "loops.example.toml")
        with open(example, "rb") as f:
            config = tomllib.load(f)
        self.assertEqual(doorbell.load_inboxes(config), [])
        # …and the sweep still resolves its thresholds from the same file.
        self.assertGreaterEqual(doorbell.load_pane_watch(config).sweep_seconds,
                                doorbell.PANE_MIN_SWEEP_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)

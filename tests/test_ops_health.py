#!/usr/bin/env python3
"""Behavioural guard on `bin/ops health` — the registry-cadence half.

Run: python3 tests/test_ops_health.py

`bin/ops health` is the estate's only staleness detector for the autostart
loops, and it is also the thing that hands the operator the restart command
instead of making them retype one. A hand-typed `/loop /<skill>` (no interval)
drops a loop into dynamic self-pacing, and the only signal is a stale
heartbeat that looks exactly like a dead loop. So these properties are guarded
here:

1. Every anomaly line is followed by a "→" line carrying a runnable command,
   and every one of those commands that arms a loop carries the registry
   interval in its `/loop <interval> /<skill>` argument.
2. A deliberate off-cadence choice is recorded in state/<name>/pace-override
   and REPORTED on every pass — never silently honoured, never kicked.
3. A `dynamic` override buys no liveness verdict, because a self-pacing loop
   and a dead one produce the same stale heartbeat.
4. A bad agent-deck status read does not restart a loop that is demonstrably
   ticking — and the veto that makes that true does not follow through into
   `bin/ops up`, where every heartbeat may be fresh by construction.
5. A loop declared `recovery = "stop-start"` is re-armed by a stop and a
   start, never by a bare kick. A `/loop` kick into a LIVE session does not
   replace its wakeup chain — it arms a second one, and `/clear` does not
   cancel one either, so each hand recovery can leave one behind permanently.
6. The heartbeat has a CEILING as well as a floor. Staleness alone cannot see
   a loop ticking too often, because `last_tick` is overwritten every tick;
   `state/<name>/tick-log` is the counter, and a loop that writes none is
   UNJUDGED on rate rather than clean.
7. And the heartbeat must ADVANCE once per interval, not merely be under
   2*interval+120s — a tick that completes without ever invoking its loop's
   engine writes no heartbeat, and read that way looks healthy for two whole
   intervals. It shares the alive branch with (4)'s veto and never reaches the
   vetoed loop: unjudged there, and the `ok` line says so.
8. `bin/ops up` starts loops by running exactly the remedy strings `health`
   prints, and only the needs-start/needs-kick ones.

The whole function is exercised against a throwaway repo with a stub
`agent-deck` on PATH. Nothing here touches the real state/ tree.
"""
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(REPO, "bin", "ops")

LOOPS_TOML = """
[hub]
persona = "ops"
# The harness is only a faithful estate if it declares the profile, because
# `bin/ops` resolves every `-p` it prints from this key.
deck_profile = "testprof"

[loops.worker]
dir = "loops/worker"
skill = "worker"
interval = "20m"
autostart = true
persona = "the worker"
model = "model-a"

[loops.watcher]
dir = "loops/watcher"
skill = "watcher"
interval = "15m"
autostart = true
persona = "the watcher"
model = "model-b"
recovery = "stop-start"

[loops.ondemand]
dir = "loops/ondemand"
skill = "ondemand"
interval = "on-demand"
autostart = false
persona = "the on-demand one"
"""

STUB_DECK = """#!/usr/bin/env bash
# Stub agent-deck. Only `ls --json` is ever reached by cmd_health; the JSON
# body comes from $DECK_JSON_FILE so each test can pose its own deck.
for a in "$@"; do
  if [ "$a" = "--json" ]; then cat "$DECK_JSON_FILE"; exit 0; fi
done
exit 0
"""


class HealthHarness(unittest.TestCase):
    """A throwaway repo with bin/ops, a registry, and a stub agent-deck."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ops-health-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "bin"))
        os.makedirs(os.path.join(self.tmp, "stub"))
        shutil.copy2(OPS, os.path.join(self.tmp, "bin", "ops"))
        with open(os.path.join(self.tmp, "loops.toml"), "w") as f:
            f.write(LOOPS_TOML)
        deck = os.path.join(self.tmp, "stub", "agent-deck")
        with open(deck, "w") as f:
            f.write(STUB_DECK)
        os.chmod(deck, 0o755)
        self.deck_json = os.path.join(self.tmp, "deck.json")
        self.set_deck([])

    def set_deck(self, sessions):
        import json
        with open(self.deck_json, "w") as f:
            f.write(json.dumps(sessions))

    def heartbeat(self, name, age_seconds):
        d = os.path.join(self.tmp, "state", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "last_tick"), "w") as f:
            f.write(str(int(time.time()) - age_seconds))

    def override(self, name, value):
        d = os.path.join(self.tmp, "state", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "pace-override"), "w") as f:
            f.write(value + "\n")

    def scan(self, *args):
        """`health_scan` itself, which is the only way to reach the read
        `up_loops` makes.

        The function has two callers with two questions, and `bin/ops health`
        exercises exactly one of them. The trailing `case` block, which
        is the whole of what makes this file a command rather than a library,
        is stripped and the definitions above it are sourced. `BASH_SOURCE[0]`
        is what `bin/ops` resolves $REPO from, so the copy has to live in the
        same bin/.
        """
        lib = os.path.join(self.tmp, "bin", "ops.lib")
        with open(os.path.join(self.tmp, "bin", "ops")) as f:
            src = f.read()
        with open(lib, "w") as f:
            f.write(src.split('case "${1:-}" in', 1)[0])
        env = dict(os.environ)
        env["PATH"] = os.path.join(self.tmp, "stub") + os.pathsep + env["PATH"]
        env["DECK_JSON_FILE"] = self.deck_json
        p = subprocess.run(
            ["bash", "-c", '. "$1"; health_scan "${@:2}"', "bash", lib, *args],
            capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def ticklog(self, name, *ages_seconds):
        """state/<name>/tick-log — one epoch line per tick, written by that
        loop's own engine. `last_tick` is overwritten every tick and can never
        say how MANY there were, which is why the upper bound needs this."""
        d = os.path.join(self.tmp, "state", name)
        os.makedirs(d, exist_ok=True)
        now = int(time.time())
        with open(os.path.join(d, "tick-log"), "w") as f:
            for age in ages_seconds:
                f.write("%d\n" % (now - age))

    def health(self):
        env = dict(os.environ)
        env["PATH"] = os.path.join(self.tmp, "stub") + os.pathsep + env["PATH"]
        env["DECK_JSON_FILE"] = self.deck_json
        p = subprocess.run(["bash", os.path.join(self.tmp, "bin", "ops"), "health"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def lines(self):
        return [ln for ln in self.health().splitlines() if ln.strip()]

    def alive(self, *titles):
        self.set_deck([{"title": t, "status": "running"} for t in titles])

    WORKER = "the worker (worker)"
    WATCHER = "the watcher (watcher)"


class EveryAnomalyCarriesItsCommand(HealthHarness):
    """Each needs-… line is followed by the exact command that fixes it."""

    def test_a_clean_estate_still_prints_one_ok_line(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("ok "), out)

    def test_an_unregistered_loop_gets_a_launch_with_the_registry_interval(self):
        self.alive(self.WATCHER)
        self.heartbeat("watcher", 60)
        out = self.health()
        self.assertIn("needs-start  %s — unregistered" % self.WORKER, out)
        self.assertIn("agent-deck -p testprof launch", out)
        self.assertIn('-m "/loop 20m /worker"', out)

    def test_the_launch_carries_the_registry_model(self):
        self.alive(self.WATCHER)
        self.heartbeat("watcher", 60)
        self.assertIn("-model model-a", self.health())

    def test_the_launch_bypasses_permissions_explicitly(self):
        """A bare `-c claude` does not reliably land in bypassPermissions mode
        when the target directory already exists — the
        needs-start remedy must spell out the flag itself via `-cmd`."""
        self.alive(self.WATCHER)
        self.heartbeat("watcher", 60)
        out = self.health()
        self.assertIn('-cmd "claude --dangerously-skip-permissions"', out)
        self.assertNotIn(" -c claude", out)

    def test_a_stopped_loop_gets_start_then_an_interval_qualified_kick(self):
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("watcher", 60)
        out = self.health()
        self.assertIn("needs-start  %s — session stopped" % self.WORKER, out)
        self.assertIn('agent-deck -p testprof session start "%s"' % self.WORKER, out)
        self.assertIn('"/loop 20m /worker"', out)

    def test_a_stale_loop_gets_a_kick_with_its_own_registry_interval(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 2848)   # well past 2*20m+120
        self.heartbeat("watcher", 60)
        out = self.health()
        self.assertIn("needs-kick   %s" % self.WORKER, out)
        self.assertIn('agent-deck -p testprof session send "%s" "/loop 20m /worker"' % self.WORKER, out)
        # Not the other loop's interval, and not a bare form.
        self.assertNotIn("/loop /worker", out)
        self.assertNotIn("/loop 15m /worker", out)

    def test_every_printed_command_carries_the_declared_profile(self):
        """The `→` lines exist precisely so the
        operator runs them instead of composing one, so a bare `agent-deck` in
        a printed command is worse than a bare one in a recipe: it is a bare
        call this estate actively hands somebody. The profile is expanded here
        rather than left as `$DECK_PROFILE`, because the shell that runs a
        copy-pasted line has no such variable in it."""
        self.set_deck([{"title": self.WATCHER, "status": "running"}])
        self.heartbeat("watcher", 99999)
        for line in self.health().splitlines():
            if "→" not in line:
                continue
            # Only what follows the arrow is a command. The anomaly label
            # itself says "(no agent-deck session)", which is prose.
            command = line.split("→", 1)[1]
            for call in command.split("agent-deck ")[1:]:
                self.assertTrue(call.startswith("-p testprof "),
                                "printed a bare agent-deck call: " + line)

    def test_each_loop_gets_its_own_interval_not_a_shared_one(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 99999)
        self.heartbeat("watcher", 99999)
        out = self.health()
        self.assertIn('"/loop 20m /worker"', out)
        self.assertIn('"/loop 15m /watcher"', out)

    def test_no_anomaly_line_is_left_without_a_command(self):
        self.set_deck([{"title": self.WATCHER, "status": "stopped"}])
        self.heartbeat("worker", 99999)
        out = self.lines()
        verbs = [i for i, ln in enumerate(out) if ln.startswith("needs-")]
        self.assertTrue(verbs, out)
        for i in verbs:
            self.assertTrue(i + 1 < len(out) and out[i + 1].lstrip().startswith("→"),
                            "no → command under: %s" % out[i])

    def test_the_stale_line_says_the_two_causes_are_indistinguishable(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 99999)
        self.heartbeat("watcher", 60)
        self.assertIn("look identical from here", self.health())

    def test_an_on_demand_loop_is_not_swept_at_all(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.assertNotIn("ondemand", self.health())


class PaceOverrideIsRecordedNotSilent(HealthHarness):
    """The operator may take a loop off its registry cadence, never quietly."""

    def test_an_override_is_reported_even_when_the_loop_is_perfectly_fresh(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.override("worker", "60m")
        out = self.health()
        self.assertIn("off-registry %s" % self.WORKER, out)
        self.assertIn("registry says 20m", out)

    def test_an_override_is_not_an_anomaly(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.override("worker", "60m")
        out = self.health()
        self.assertNotIn("needs-", out)
        self.assertIn("deliberate, not an anomaly", out)

    def test_the_override_widens_the_threshold_it_names(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 2848)   # stale at 20m, fine at 60m
        self.heartbeat("watcher", 30)
        self.override("worker", "60m")
        out = self.health()
        self.assertIn("7320s = 2*60m+120", out)
        self.assertNotIn("needs-kick", out)

    def test_an_overridden_loop_still_goes_stale_against_its_own_pace(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 30000)
        self.heartbeat("watcher", 30)
        self.override("worker", "60m")
        out = self.health()
        self.assertIn("needs-kick   %s" % self.WORKER, out)
        self.assertIn('"/loop 60m /worker"', out)

    def test_every_override_line_says_how_to_undo_it(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.override("worker", "60m")
        out = self.health()
        self.assertIn("rm ", out)
        self.assertIn("pace-override", out)
        self.assertIn('"/loop 20m /worker"', out)

    def test_an_unreadable_override_is_an_anomaly_not_a_default(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.override("worker", "600")   # seconds — state/hub/pace's format
        out = self.health()
        self.assertIn("needs-attn   %s — unreadable pace override '600'" % self.WORKER, out)
        self.assertIn("or 'dynamic'", out)

    def test_an_empty_override_file_falls_back_to_the_registry(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 2848)
        self.heartbeat("watcher", 30)
        self.override("worker", "")
        out = self.health()
        self.assertNotIn("off-registry", out)
        self.assertIn('"/loop 20m /worker"', out)


class DynamicBuysNoLivenessVerdict(HealthHarness):
    """`dynamic` is allowed, and it costs the liveness check. Say so."""

    def setUp(self):
        super().setUp()
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("watcher", 30)
        self.override("worker", "dynamic")

    def test_a_dynamic_loop_is_never_kicked_however_stale(self):
        self.heartbeat("worker", 999999)
        out = self.health()
        self.assertIn("off-registry %s" % self.WORKER, out)
        self.assertNotIn("needs-kick", out)

    def test_it_says_the_liveness_verdict_is_gone(self):
        self.heartbeat("worker", 999999)
        out = self.health()
        self.assertIn("NO liveness verdict", out)
        self.assertIn("indistinguishable from a dead one", out)

    def test_the_heartbeat_age_is_still_reported_as_a_fact(self):
        """The age is read back rather than matched exactly: the test stamps
        the file from `time.time()` and `bin/ops` reads the clock again with
        `date +%s`, so a second crossing between them makes an equality
        assertion fail now and then. What the test is about is
        that a `dynamic` override still PRINTS the age, having declined to
        judge it."""
        self.heartbeat("worker", 4242)
        out = self.health()
        m = re.search(r"last tick (\d+)s ago", out)
        self.assertIsNotNone(m, out)
        self.assertLess(abs(int(m.group(1)) - 4242), 5, out)

    def test_the_undo_command_re_arms_at_the_registry_interval(self):
        self.heartbeat("worker", 60)
        self.assertIn('"/loop 20m /worker"', self.health())


class AFreshHeartbeatOutranksABadStatusRead(HealthHarness):
    """The stopped/errored branch used to be the one verdict in the sweep that
    read no heartbeat at all — whatever agent-deck's Status column said went
    unchallenged. That column can be wrong, and it was the branch's only
    evidence, while the loop's own `last_tick` is evidence about the loop
    rather than about the registry watching it."""

    def test_a_stopped_status_with_a_fresh_heartbeat_is_not_an_anomaly(self):
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        out = self.health()
        self.assertNotIn("needs-start", out)
        self.assertTrue(out.strip().startswith("ok "), out)

    def test_a_stopped_status_with_a_stale_heartbeat_still_needs_starting(self):
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 99999)
        self.heartbeat("watcher", 30)
        out = self.health()
        self.assertIn("needs-start  %s — session stopped" % self.WORKER, out)
        self.assertIn('agent-deck -p testprof session start "%s"' % self.WORKER, out)

    def test_no_heartbeat_at_all_is_stale_not_fresh(self):
        """An unread file reads 0, and 0 is stale against any threshold. A loop
        that has never written one has produced no evidence, and the absence of
        evidence must not be read as a healthy loop."""
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("watcher", 30)
        self.assertIn("needs-start  %s" % self.WORKER, self.health())

    def test_an_errored_or_unknown_status_gets_the_same_veto(self):
        for status in ("errored", "", "some new word"):
            with self.subTest(status=status):
                self.set_deck([{"title": self.WORKER, "status": status},
                               {"title": self.WATCHER, "status": "running"}])
                self.heartbeat("worker", 30)
                self.heartbeat("watcher", 30)
                self.assertNotIn("needs-start", self.health())

    def test_the_veto_uses_the_pace_overrides_widened_threshold(self):
        """The same `$thresh` the running/waiting branch judges against —
        computed once, above both branches, with the override already in it."""
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 2848)   # stale at 20m, fresh at 60m
        self.heartbeat("watcher", 30)
        self.assertIn("needs-start", self.health())
        self.override("worker", "60m")
        self.assertNotIn("needs-start", self.health())

    def test_an_unregistered_loop_is_deliberately_not_vetoed(self):
        """Absent from the deck read is a `launch`, not a `start`, and a
        heartbeat says nothing about whether agent-deck has a record. The veto
        is scoped to the branch whose only evidence was the status string."""
        self.set_deck([{"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.assertIn("needs-start  %s — unregistered" % self.WORKER,
                      self.health())

    def test_a_dynamic_override_is_still_decided_before_any_of_this(self):
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 999999)
        self.heartbeat("watcher", 30)
        self.override("worker", "dynamic")
        out = self.health()
        self.assertIn("off-registry %s" % self.WORKER, out)
        self.assertNotIn("needs-start", out)


class TheVetoDoesNotFollowIntoUp(HealthHarness):
    """`bin/ops up` asks a different question and must not inherit the answer.

    Right after the estate is stopped, every heartbeat is fresh — the loops
    were ticking a minute ago — so an `up` that honoured the veto would start
    nothing and report "all autostart loops already alive and fresh" over an
    estate that is down.
    """

    def setUp(self):
        super().setUp()
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "stopped"}])
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)

    def test_health_stays_quiet_about_both(self):
        self.assertNotIn("needs-start", self.scan())

    def test_but_for_start_still_offers_both_remedies(self):
        out = self.scan("--for-start")
        self.assertIn("needs-start\t%s" % self.WORKER, out)
        self.assertIn("needs-start\t%s" % self.WATCHER, out)

    def test_the_remedy_for_start_gets_is_the_one_health_would_print(self):
        """One producer, the same bytes — the whole reason `up` reads this
        function instead of keeping a second copy of the launch logic."""
        self.heartbeat("worker", 99999)
        stale = [ln for ln in self.scan().splitlines()
                 if ln.startswith("needs-start\t%s" % self.WORKER)]
        started = [ln for ln in self.scan("--for-start").splitlines()
                   if ln.startswith("needs-start\t%s" % self.WORKER)]
        self.assertEqual(len(stale), 1, stale)
        self.assertEqual(stale, started)

    def test_a_stale_loop_is_reported_identically_by_both_reads(self):
        self.heartbeat("worker", 99999)
        self.heartbeat("watcher", 99999)
        self.assertEqual(self.scan(), self.scan("--for-start"))


class TheWatcherIsRecoveredWithAStopStart(HealthHarness):
    """A kick into a LIVE session arms a SECOND /loop schedule; the old one is
    not replaced and `/clear` does not cancel it. Only a stop+start takes it
    down with the process, so repeated hand recoveries leave a loop firing at a
    multiple of its registered cadence."""

    def test_a_stale_watcher_is_stopped_and_started_not_merely_kicked(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 99999)
        out = self.health()
        self.assertIn("needs-kick   %s" % self.WATCHER, out)
        self.assertIn('session stop "%s"' % self.WATCHER, out)
        self.assertIn('session start "%s"' % self.WATCHER, out)
        # And the re-arm still carries the interval — a stop+start that
        # dropped it would trade one silent failure for another.
        self.assertIn('session send "%s" "/loop 15m /watcher"' % self.WATCHER,
                      out)

    def test_the_stop_comes_before_the_start_and_the_kick_last(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 99999)
        line = [ln for ln in self.health().splitlines()
                if "→" in ln and self.WATCHER in ln][0]
        self.assertLess(line.index("session stop"), line.index("session start"))
        self.assertLess(line.index("session start"), line.index("session send"))

    def test_another_loop_still_gets_the_plain_kick(self):
        """The shape is DECLARED per loop, not the estate-wide default: a stop+start
        costs the session its context, so it is paid where an extra schedule
        has actually been seen to accumulate."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 99999)
        self.heartbeat("watcher", 60)
        out = self.health()
        night = [ln for ln in out.splitlines()
                 if "→" in ln and self.WORKER in ln][0]
        self.assertNotIn("session stop", night)
        self.assertIn('session send "%s" "/loop 20m /worker"' % self.WORKER,
                      night)

    def test_the_watchers_override_undo_is_a_stop_start_too(self):
        """The same accumulation applies to any re-arm of a live session, so
        the `off-registry` line's restore command takes the same shape."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.override("watcher", "60m")
        out = self.health()
        self.assertIn("off-registry %s" % self.WATCHER, out)
        self.assertIn('session stop "%s"' % self.WATCHER, out)
        self.assertIn('"/loop 15m /watcher"', out)


class TheUpperBoundOnTickRate(HealthHarness):
    """The detector half. With no upper bound anywhere, `bin/ops health` would
    read many ticks an hour as perfect freshness."""

    def test_a_loop_ticking_far_too_often_is_flagged(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", *range(60, 3540, 140))   # ~25 in the hour
        out = self.health()
        self.assertIn("tick-rate", out)
        self.assertIn("against a 15m cadence", out)

    def test_the_remedy_is_a_stop_start_never_another_kick(self):
        """A kick is what put the extra schedule there. Printing one under
        this finding would hand the operator the cause as the cure."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", *range(60, 3540, 140))
        line = [ln for ln in self.health().splitlines()
                if "→" in ln and self.WATCHER in ln][0]
        self.assertIn("session stop", line)
        self.assertIn("session start", line)

    def test_a_single_extra_armed_schedule_is_already_caught(self):
        """Doubling is the unit of this failure — one extra schedule is one
        extra firing per interval — so the ceiling has to sit below 2x."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", *range(60, 3540, 450))   # 8 in the hour
        self.assertIn("tick-rate", self.health())

    def test_the_registered_cadence_plus_a_hand_kick_is_not_flagged(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", 60, 900, 1800, 2700, 3400)   # 4 + a kick
        out = self.health()
        self.assertNotIn("tick-rate", out)
        self.assertTrue(out.startswith("ok "), out)

    def test_ticks_older_than_the_hour_do_not_count(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", *range(3700, 20000, 140))
        self.assertNotIn("tick-rate", self.health())

    def test_a_loop_with_no_tick_log_is_unjudged_and_the_ok_line_says_so(self):
        """An absence of evidence is not evidence (docs/absence-contract.md):
        a loop whose engine writes no tick log has no rate verdict, and the
        clean line must not let silence stand in for one."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", 60, 900, 1800)
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("ok "), out)
        self.assertIn("tick rate judged for 1 of 2", out[0])
        self.assertIn("unjudged, not clean", out[0])

    def test_a_fully_covered_estate_says_nothing_extra(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("worker", 60, 1200, 2400)
        self.ticklog("watcher", 60, 900, 1800)
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertNotIn("tick rate judged", out[0])

    def test_a_rate_flag_is_an_anomaly_and_suppresses_the_ok_line(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", *range(60, 3540, 140))
        self.assertNotIn("\nok ", "\n" + self.health())

    def test_a_dynamic_loop_gets_no_rate_verdict_either(self):
        """`dynamic` means there is no expected cadence, so there is nothing
        for an over-rate to exceed — the same reason it buys no liveness
        verdict."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.override("watcher", "dynamic")
        self.ticklog("watcher", *range(60, 3540, 140))
        self.assertNotIn("tick-rate", self.health())

    def test_a_pace_override_is_the_cadence_the_rate_is_judged_against(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.override("watcher", "60m")          # implied 1/hour, ceiling 3
        self.ticklog("watcher", 60, 900, 1800, 2700)
        out = self.health()
        self.assertIn("tick-rate", out)
        self.assertIn("against a 60m cadence", out)


class TheHeartbeatMustAdvanceOncePerInterval(HealthHarness):
    """A tick that narrates without ever invoking its loop's engine
    completes in seconds and writes no heartbeat. Under the staleness check
    alone that loop reads healthy for two whole intervals."""

    # watcher at 15m: stale at 2*900+120 = 1920s, missed at 1.5*900+120.
    MISSED = 1500
    STALE = 2000

    def test_a_heartbeat_that_missed_its_interval_is_reported(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", self.MISSED)
        out = self.health()
        self.assertIn("tick-missed  %s" % self.WATCHER, out)
        self.assertIn("1470s = 1.5*15m+120", out)

    def test_it_names_both_readings_rather_than_choosing_one(self):
        """Nothing on disk separates a fabricated tick from a pass still
        running long, so the record must not pretend it does."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", self.MISSED)
        out = self.health()
        self.assertIn("without ever invoking this loop's engine", out)
        self.assertIn("still running long", out)

    def test_its_remedy_reads_the_session_and_never_restarts_it(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", self.MISSED)
        line = [ln for ln in self.health().splitlines()
                if "→" in ln and self.WATCHER in ln][0]
        self.assertIn("session output", line)
        self.assertNotIn("session stop", line)
        self.assertNotIn("/loop", line)

    def test_a_fresh_heartbeat_says_nothing(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 800)
        self.assertNotIn("tick-missed", self.health())

    def test_a_stale_loop_is_reported_once_as_the_graver_claim(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", self.STALE)
        out = self.health()
        self.assertIn("needs-kick", out)
        self.assertNotIn("tick-missed", out)

    def test_a_loop_that_never_ticked_is_a_kick_and_not_a_missed_tick(self):
        """No heartbeat file at all is the launch/start case the existing
        verbs already own — reporting it as a missed tick would put a
        read-the-pane remedy under a loop that needs starting."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        out = self.health()
        self.assertIn("needs-kick", out)
        self.assertNotIn("tick-missed", out)

    def test_a_dynamic_loop_is_exempt_here_too(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", self.MISSED)
        self.override("watcher", "dynamic")
        self.assertNotIn("tick-missed", self.health())

    def test_a_missed_tick_is_an_anomaly(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", self.MISSED)
        self.assertNotIn("\nok ", "\n" + self.health())


class TheVetoAndTheTickChecksDoNotInteract(HealthHarness):
    """Where the heartbeat veto meets the tick records.

    The pair has exactly one place it could go wrong quietly: the veto `continue`s
    out of the loop body, and the tick checks live in the OTHER branch. So
    `--for-start` must still change exactly one verdict and nothing else, and
    a vetoed loop must be reported as UNJUDGED on rate rather than as clean —
    the sweep never established that loop is ticking, only that agent-deck and
    `last_tick` disagree about it.
    """

    def test_for_start_changes_the_stopped_branch_and_nothing_else(self):
        """An alive loop's records are identical under both reads: the flag
        touches the stopped/errored branch alone."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", *range(60, 3540, 140))
        plain = self.scan()
        for_start = self.scan("--for-start")
        self.assertIn("tick-rate\t%s" % self.WATCHER, plain)
        self.assertEqual(plain, for_start)

    def test_a_missed_tick_is_reported_the_same_under_both_reads(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 1500)
        self.assertIn("tick-missed\t%s" % self.WATCHER, self.scan())
        self.assertIn("tick-missed\t%s" % self.WATCHER, self.scan("--for-start"))

    def test_a_vetoed_loop_is_unjudged_on_rate_and_never_clean(self):
        """`stopped` status + fresh heartbeat is the veto: no anomaly, and
        no rate verdict either, because the tick checks sit in the branch the
        veto skipped. The ok line has to say the rate was not judged."""
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "stopped"}])
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.ticklog("watcher", *range(60, 3540, 140))
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("ok "), out)
        self.assertNotIn("tick-rate", out[0])
        self.assertIn("tick rate judged for 0 of 2", out[0])
        self.assertIn("unjudged, not clean", out[0])

    def test_the_veto_still_fires_for_a_loop_that_has_a_tick_log(self):
        """The rate read must not disturb the veto's own verdict — reading
        `tick-log` happens in the other branch and cannot reach this one."""
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.ticklog("watcher", 60, 900, 1800)
        self.assertNotIn("needs-start", self.health())
        self.assertIn("needs-start\t%s" % self.WORKER, self.scan("--for-start"))

    def test_the_count_record_carries_both_numbers_for_both_readers(self):
        """`up_loops` reads the same four tab-separated fields `cmd_health`
        does and drops every kind but needs-start/needs-kick — the `count`
        record's new second field must not change what it acts on."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", 60, 900, 1800)
        for out in (self.scan(), self.scan("--for-start")):
            counts = [ln for ln in out.splitlines() if ln.startswith("count\t")]
            self.assertEqual(counts, ["count\t2\t1\t"], out)


STUB_DECK_LOGGING = """#!/usr/bin/env bash
# Stub agent-deck that also records every call. `ls --json` answers from
# $DECK_JSON_FILE; anything else is appended to $DECK_LOG and exits
# $DECK_EXIT (default 0), so a test can pose a launch that fails.
for a in "$@"; do
  if [ "$a" = "--json" ]; then cat "$DECK_JSON_FILE"; exit 0; fi
done
printf '%s\\n' "$*" >> "$DECK_LOG"
exit "${DECK_EXIT:-0}"
"""


class UpRunsHealthsOwnRemedies(HealthHarness):
    """`bin/ops up` starts loops by EXECUTING the strings `health_scan`
    produces, so the printed remedy and the performed one are one copy."""

    def setUp(self):
        super().setUp()
        deck = os.path.join(self.tmp, "stub", "agent-deck")
        with open(deck, "w") as f:
            f.write(STUB_DECK_LOGGING)
        os.chmod(deck, 0o755)
        # A stopped loop's remedy sleeps between start and kick; not here.
        sleep = os.path.join(self.tmp, "stub", "sleep")
        with open(sleep, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(sleep, 0o755)
        self.log = os.path.join(self.tmp, "deck.log")

    def up_loops(self, deck_exit=0):
        lib = os.path.join(self.tmp, "bin", "ops.lib")
        with open(os.path.join(self.tmp, "bin", "ops")) as f:
            src = f.read()
        with open(lib, "w") as f:
            f.write(src.split('case "${1:-}" in', 1)[0])
        env = dict(os.environ)
        env["PATH"] = os.path.join(self.tmp, "stub") + os.pathsep + env["PATH"]
        env["DECK_JSON_FILE"] = self.deck_json
        env["DECK_LOG"] = self.log
        env["DECK_EXIT"] = str(deck_exit)
        p = subprocess.run(["bash", "-c", '. "$1"; up_loops', "bash", lib],
                           capture_output=True, text=True, env=env)
        calls = open(self.log).read().splitlines() if os.path.exists(self.log) else []
        return p, calls

    def test_an_unregistered_loop_is_launched_with_its_interval_and_model(self):
        self.alive(self.WATCHER)
        self.heartbeat("watcher", 60)
        p, calls = self.up_loops()
        self.assertEqual(p.returncode, 0, p.stderr)
        launches = [c for c in calls if " launch " in " " + c + " "]
        self.assertEqual(len(launches), 1, calls)
        self.assertIn("-p testprof launch", launches[0])
        self.assertIn("/loop 20m /worker", launches[0])
        self.assertIn("-model model-a", launches[0])

    def test_the_performed_command_is_the_printed_one(self):
        self.alive(self.WATCHER)
        self.heartbeat("watcher", 60)
        # Only an indented arrow line is a command; the anomaly label itself
        # ends "→ launch", which is prose.
        printed = [ln.split("→ ", 1)[1] for ln in self.health().splitlines()
                   if ln.lstrip().startswith("→ ")]
        p, _ = self.up_loops()
        performed = [ln.split("→ ", 1)[1] for ln in p.stdout.splitlines()
                     if ln.lstrip().startswith("→ ")]
        self.assertEqual(printed, performed)

    def test_a_stopped_loop_is_started_even_over_a_fresh_heartbeat(self):
        self.set_deck([{"title": self.WORKER, "status": "stopped"},
                       {"title": self.WATCHER, "status": "running"}])
        self.heartbeat("worker", 30)
        self.heartbeat("watcher", 30)
        self.assertNotIn("needs-start", self.health())
        p, calls = self.up_loops()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('-p testprof session start %s' % self.WORKER, calls)

    def test_report_only_records_are_never_acted_on(self):
        """tick-missed, tick-rate and off-registry all carry a remedy, and
        none of them is `up`'s to run."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 1500)           # tick-missed, not stale
        self.ticklog("watcher", *range(60, 3540, 140))   # and tick-rate
        self.override("worker", "30m")            # off-registry
        out = self.health()
        for kind in ("tick-missed", "tick-rate", "off-registry"):
            self.assertIn(kind, out)
        p, calls = self.up_loops()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(calls, [])
        self.assertIn("already alive and fresh", p.stdout)

    def test_a_remedy_that_fails_is_carried_out_in_the_exit_status(self):
        self.alive(self.WATCHER)
        self.heartbeat("watcher", 60)
        p, _ = self.up_loops(deck_exit=1)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("FAILED", p.stderr)


class TheSpotterIsJudgedOnItsTickLog(HealthHarness):
    """A configured log distinguishes actual ticks from non-tick heartbeats."""

    def setUp(self):
        super().setUp()
        registry = os.path.join(self.tmp, "loops.toml")
        with open(registry) as fh:
            text = fh.read()
        with open(registry, "w") as fh:
            fh.write(text.replace('[loops.watcher]', '[loops.watcher]\ntick_health = "log"'))

    SILENT = 40 * 60   # 2400s > 1.5*15m+120 = 1470s

    def assertCouldNotBeChecked(self):
        """Not an anomaly and never a pass: with nothing else wrong the one
        ok line carries it, and never claims the estate is healthy."""
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("ok "), out)
        self.assertIn("not every check could be made", out[0])
        self.assertIn("NOT CHECKED: %s" % self.WATCHER, out[0])
        self.assertIn("tick-missed could not be checked", out[0])
        self.assertNotIn("healthy", out[0])
        self.assertIn("tick-unread\t%s" % self.WATCHER, self.scan())

    def test_spotter_tick_missed_when_tick_log_silent_but_last_tick_fresh(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.ticklog("watcher", self.SILENT)          # last real tick
        self.heartbeat("watcher", 30)    # a `status` call
        out = self.health()
        self.assertIn("tick-missed  %s" % self.WATCHER, out)
        self.assertIn("tick log", out)
        self.assertIn("1470s = 1.5*15m+120", out)
        self.assertNotIn("needs-kick", out)
        self.assertNotIn("\nok ", "\n" + out)
        line = [ln for ln in out.splitlines()
                if "→" in ln and self.WATCHER in ln][0]
        self.assertIn("session output", line)
        self.assertNotIn("session stop", line)

    def test_spotter_fresh_tick_log_and_fresh_last_tick_is_clean(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.ticklog("watcher", 600)
        self.heartbeat("watcher", 30)
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("ok "), out)

    def test_spotter_missing_tick_log_is_could_not_be_checked(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 30)
        self.assertCouldNotBeChecked()
        self.assertIn("warrant: failed", self.health())

    def test_spotter_unreadable_tick_log_is_could_not_be_checked(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 30)
        # A directory where the file should be: exists, cannot be read as
        # one, and behaves the same whoever runs the test (chmod does not
        # stop root).
        os.makedirs(os.path.join(self.tmp, "state", "watcher", "tick-log"))
        self.assertCouldNotBeChecked()

    def test_spotter_tick_log_with_no_tick_line_is_could_not_be_checked(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 30)
        with open(os.path.join(self.tmp, "state", "watcher",
                               "tick-log"), "w") as f:
            f.write("garbage\n")
        self.assertCouldNotBeChecked()

    def test_beside_an_anomaly_the_unchecked_spotter_is_its_own_record(self):
        """With something else wrong there is no ok line to carry it, so the
        could-not-be-checked record is printed in full, with its remedy."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 5000)          # stale -> needs-kick
        self.heartbeat("watcher", 30)
        out = self.health()
        self.assertIn("needs-kick", out)
        self.assertIn("tick-unread  %s" % self.WATCHER, out)
        self.assertIn("could not be checked", out)
        self.assertNotIn("\nok ", "\n" + out)

    def test_other_loops_keep_the_heartbeat_only_judgement(self):
        """The night shift writes no tick log: no tick-unread for it, and its
        tick-missed still comes from last_tick."""
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("watcher", 60)
        self.ticklog("watcher", 60)
        self.heartbeat("worker", 30)
        out = self.lines()
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith("ok "), out)
        self.heartbeat("worker", 2000)   # > 1.5*20m+120 = 1920s
        out = self.health()
        self.assertIn("tick-missed  %s" % self.WORKER, out)
        self.assertNotIn("tick-unread", out)

    def test_a_stale_spotter_is_still_a_kick_not_a_tick_record(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.heartbeat("watcher", 2000)
        out = self.health()
        self.assertIn("needs-kick", out)
        self.assertNotIn("tick-unread", out)
        self.assertNotIn("tick-missed", out)

    def test_the_tick_log_verdict_is_the_same_under_both_reads(self):
        self.alive(self.WORKER, self.WATCHER)
        self.heartbeat("worker", 60)
        self.ticklog("watcher", self.SILENT)
        self.heartbeat("watcher", 30)
        self.assertIn("tick-missed\t%s" % self.WATCHER, self.scan())
        self.assertEqual(self.scan(), self.scan("--for-start"))

if __name__ == "__main__":
    unittest.main(verbosity=2)

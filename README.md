# nano-ops (prototype)

A personal operations hub — a long-lived control-plane session that drives
autonomous Claude Code loops — with its always-on pieces ported from
hand-installed systemd `--user` units to Flox `[services]`, so the whole set
is declarative, in-repo, versioned, and reproducible.

## Quickstart

**1. Activate the environment.** The Flox `[hook]` seeds `loops.toml` from
`loops.example.toml` if it doesn't exist yet and renders the shell.

```bash
flox activate --start-services
```

**2. Run the preflight.** Checks the toolchain, the config, and the invariants
(including that `ANTHROPIC_API_KEY` is unset). A missing Slack channel or token
is reported as a skip, not a failure.

```bash
bin/ops doctor
```

**3. Name your installation, then start the hub.** Edit the `[hub]` block in
`loops.toml` — persona, group, estate, control channel — so it identifies your
installation rather than the neutral defaults. Then:

```bash
bin/ops up
```

Once it's up, the everyday handles:

```bash
bin/ops services status             # NAME / STATUS / PID for all four
bin/ops services logs doorbell      # live stdout+stderr
open "$(bin/ops dashboard url)"     # loopback only
```

`flox services stop | restart <name>` manage individual services. Services live
only for the duration of an activation — an always-on deployment needs one
persistent activation held open by a supervisor
([`docs/always-on.md`](./docs/always-on.md)).

## What's inside

```
CLAUDE.md              what this repo is; the conventions that bind every session
hub/                   the hub session's home — CLAUDE.md + the hub tick skill
bin/ops                operator CLI: up|status|health|doctor|services|dashboard|compact
bin/doorbell           zero-token Slack self-DM poller (kicks the hub on activity)
bin/dashboard          estate dashboard renderer (pure reader; --json regen path)
bin/dashboard-refresh  keeps state/dashboard.json fresh via `dashboard --json`
bin/dashboard-server   loopback-only static server for the dashboard (allowlist)
bin/usage-fetch        writes state/usage/budget.json from Anthropic's usage API
bin/followups          durable standing-action-item store
tests/                 test_dashboard.py, test_followups.py, test-interactive-hub
.flox/env/manifest.toml  toolchain + [services], each under the supervisor wrapper
loops.example.toml     committed sample registry (copy → loops.toml)
infra/nano-ops-services.service.example
                       template for the ONE boot supervisor unit (never installed)
docs/design.md         architecture: the three layers, the always-on set, invariants
docs/always-on.md      runbook for surviving logout and reboot
docs/services-vs-systemd.md
                       measured evidence: Flox [services] vs systemd --user
docs/ideas.md          backlog the hub appends to from the control channel
```

Each always-on service is the **real** operations script wrapped in the
`while true` supervisor from `docs/services-vs-systemd.md`, the in-manifest
stand-in for systemd's `Restart=always` (process-compose has no native restart
policy).

**Nothing here names an operator.** The hub's persona, agent-deck group and
profile, the estate label, the control channel, the dashboard host and port, and
the loop registry all resolve from `loops.toml` at runtime, defaulting to a
neutral `"ops"`. An allowlist rule — mechanism is public, identity, policy and
data are not — is what keeps it that way.

## Testing

`tests/test_dashboard.py` and `tests/test_followups.py` are self-contained
`unittest` suites over the dashboard renderer and the followups store. Each runs
against a fresh tempdir and never touches real `state/`:

```bash
python3 tests/test_dashboard.py
python3 tests/test_followups.py
```

### Interactive hub smoke test

`tests/test-interactive-hub` creates a throwaway clone, starts its foreground
Flox services and `ops (hub)` session, and lets an ephemeral Codex CLI driver
probe the hub for up to ten turns. It requires `git`, `flox`, `agent-deck`, and
an authenticated `codex` CLI:

```bash
tests/test-interactive-hub /path/to/nano-ops --ref interactive-hub-test-harness
```

Run it only from a real terminal/session with permission to start local tmux and
agent-deck processes. It is intentionally not a CI test and should not be run
from an already sandboxed agent environment. The source can be a local path or
Git URL; `--work-dir`, `--transcript`, and `--response-timeout` customize the
run. The durable transcript defaults to the caller's current directory.

`PASS` means the repo's doctor/status/health gate passed and the Codex driver
saw coherent, non-crashing answers across useful identity and edge-case probes,
with no claim that the sandbox used the `work` profile. It is a smoke signal,
not exhaustive correctness proof. `FAIL` identifies a definite gate,
conversation, or teardown failure; `INCONCLUSIVE` means the evidence was not
strong enough within the turn/time limit. The harness always stops the
profile/group `ops` sessions and Flox services it started and removes its clone.

## Guiding architecture — private fork + public upstream

The decisive shape:

- **A live installation stays a private fork** that adds
  `upstream → nano-ops` and pulls improvements via `git fetch upstream` +
  `git rebase upstream/main`. Mechanism is developed here and flows down;
  installation policy stays in the fork's own commits.
- **Installation-specific bits are gitignored, not committed:** `state/` (runtime,
  always ignored) and `loops.toml` (per-install registry — operator identity,
  control channel, loops; a committed `loops.example.toml` documents it and the
  `[hook]` seeds `loops.toml` from it on first activation).
- **FloxHub composition** delivers the shared toolchain later (a `flox/nano-ops`
  environment both the public core and the private fork `[include]`), keeping
  execution substrate and orchestration source cleanly separated.

The services approach is being proven **here first**, then folded back upstream.

## Safe by construction

- **No Slack token here.** There is no `state/secrets/slack-user-token`, so
  `bin/doorbell` exits cleanly on the missing-token path each cycle and does
  nothing — the supervisor just restarts it. Nothing is sent to Slack.
- **No residue.** When the activation exits, all services stop and no process
  survives. `state/` and `loops.toml` are gitignored, so nothing runtime is
  committed.

## Not yet (deliberately)

- **Boot-autostart is documented, never installed.** Cross-session survival needs
  a single systemd `--user` unit holding a persistent activation open.
  `infra/nano-ops-services.service.example` is the template for it and
  [`docs/always-on.md`](./docs/always-on.md) is the runbook, but the unit is
  rendered and enabled by the operator on their own machine — nothing in this
  repo, `bin/ops` included, installs or touches a systemd unit.
- **No loops ship here yet.** `hub/` and the registry shape are in place; the
  `loops/<name>/` folders are not. A loop is where installation policy
  concentrates, so the public core will carry a generic example loop rather than
  any working loop from a live estate.
- **Not published.** Nothing here is pushed anywhere; there is no remote.

See [`docs/services-vs-systemd.md`](./docs/services-vs-systemd.md) for the full
Flox-`[services]`-vs-systemd findings that motivated this port.

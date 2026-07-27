# nano-ops (prototype)

A personal operations hub — a long-lived control-plane session that drives
autonomous Claude Code loops — with its always-on pieces ported from
hand-installed systemd `--user` units to Flox `[services]`, so the whole set
is declarative, in-repo, versioned, and reproducible.

## Quickstart

```bash
flox activate --start-services      # hook seeds loops.toml + renders the shell
bin/ops doctor                      # preflight: toolchain, config, invariants
bin/ops services status             # NAME / STATUS / PID for all four
bin/ops services logs doorbell      # live stdout+stderr
open "$(bin/ops dashboard url)"     # loopback only
```

Then name your installation in `loops.toml` (`[hub]` persona, group, estate,
control channel) and `bin/ops up` launches the hub session for it.

`flox services stop | restart <name>` manage individual services. Services live
only for the duration of an activation — an always-on deployment needs one
persistent activation held open by a supervisor
([`docs/always-on.md`](./docs/always-on.md)).

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

## Guiding architecture — private fork + public upstream

The decisive shape:

- **A brand-new public `nano-ops` core repo, built from an allowlist** —
  never by flipping the visibility of a live installation's repo, whose git
  history would retain personal state and secrets. **This directory is the seed
  of that public core**, and an allowlist rule decides what may enter it.
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
- **Coexists with the live hub.** `dashboard-server` binds `127.0.0.1:8522`
  (not the operations default `8422`), so it runs alongside a live operations
  dashboard on the same host without a port conflict. `bin/usage-fetch` only
  makes a **read-only** GET to the usage API. Nothing touches the live
  `operations` repo or its services.
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

# nano-ops-core (prototype)

Working **seed of the eventual public `nano-ops-core` release repo**. It carries
the always-on pieces of Bill's `nano-bill` operations hub — the doorbell poller,
the local dashboard, and the usage signal — ported from hand-installed systemd
`--user` units to Flox **`[services]`** so the whole set is declarative, in-repo,
versioned, and reproducible.

This started as a throwaway spike (see [`FINDINGS.md`](./FINDINGS.md)); it has
been promoted to a persistable prototype and is being built out as the real
candidate public core — **not** a disposable experiment.

## Guiding architecture — private fork + public upstream

Per the 2026-07-22 operations-repo sharing/layering research (Codex, recorded at
`operations/state/hub/ephemeral-share-ops-research.out`), the decisive shape is:

- **A brand-new public `nano-ops-core` core repo, built from an allowlist** —
  never by flipping the visibility of the live `operations` repo (its git history
  would retain personal state/secrets). **This directory is the seed of that
  public core.**
- **Bill's live `~/github/billlevine/operations` stays a private fork** that adds
  `upstream → nano-ops-core` and pulls improvements via `git fetch upstream` +
  `git rebase upstream/main`. It will later fork/rebase onto whatever this
  becomes once it's a real public repo.
- **Installation-specific bits are gitignored, not committed:** `state/` (runtime,
  always ignored) and `loops.toml` (per-install registry; a committed
  `loops.example.toml` documents it and the `[hook]` seeds `loops.toml` from it on
  first activation).
- **FloxHub composition** delivers the shared toolchain later (a `flox/nano-ops-core`
  environment both the public core and the private fork `[include]`), keeping
  execution substrate and orchestration source cleanly separated.

The services approach is being proven **here first**, then folded back upstream.

## What's inside

```
bin/doorbell           zero-token Slack self-DM poller (kicks the hub on activity)
bin/dashboard          estate dashboard renderer (pure reader; --json regen path)
bin/dashboard-refresh  keeps state/dashboard.json fresh via `dashboard --json`
bin/dashboard-server   loopback-only static server for the dashboard (allowlist)
bin/usage-fetch        writes state/usage/budget.json from Anthropic's usage API
.flox/env/manifest.toml  [services] for all four, each under the supervisor wrapper
loops.example.toml     committed sample registry (copy → loops.toml)
```

Each service is the **real** operations script wrapped in the `while true`
supervisor from `FINDINGS.md`, the in-manifest stand-in for systemd's
`Restart=always` (process-compose has no native restart policy).

## Quickstart

```bash
flox activate --start-services      # hook seeds loops.toml + renders the shell
flox services status                # NAME / STATUS / PID for all four
flox services logs doorbell         # live stdout+stderr
# dashboard (loopback only):
open http://127.0.0.1:8522/dashboard.html
```

`flox services stop | restart <name>` manage individual services. Services live
only for the duration of an activation — an always-on deployment needs one
persistent activation held open by a supervisor (see below).

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

- **Boot-autostart / cross-session survival** — needs the single systemd `--user`
  supervisor unit holding a persistent activation open (`flox activate
  --start-services -- tail -f /dev/null`). That unit is intentionally not in this
  repo; see `FINDINGS.md`.
- **Actually publishing** this as a public repo — that is the later, bigger
  decision. Nothing here is pushed anywhere; there is no remote.

See [`FINDINGS.md`](./FINDINGS.md) for the full Flox-`[services]`-vs-systemd
findings that motivated this port.

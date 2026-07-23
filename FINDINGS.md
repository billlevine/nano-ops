# Spike: Flox `[services]` vs systemd `--user` for the always-on pieces

**Date:** 2026-07-22 · **Flox:** 1.13.2 · **process-compose:** 1.94.0 (bundled)
**Scope:** a non-invasive prototype in its own directory. The live
installation this was measured against — its repo, its `loops.toml`, and its
systemd `--user` services — was **not touched** (verified read-only: its
`doorbell.service` stayed `active/running` on the same PID throughout, up
continuously since the day before).

## Update — promoted from spike to the real seed (2026-07-22)

This directory is no longer a throwaway. It is now the **working seed of the
public `nano-ops` release repo**, and the **real** services port lives here
first (guiding architecture: private fork + public upstream — see `README.md`
and the 2026-07-22 sharing/layering research).

What changed since the original spike below:

- The sim stand-ins (`bin/doorbell-sim`, `bin/doorbell.real`) are gone. The
  **real** hub scripts were ported in: `bin/doorbell`,
  `bin/dashboard`, `bin/dashboard-refresh`, `bin/dashboard-server`,
  `bin/usage-fetch`.
- `[services]` now defines the four real always-on services, each wrapped in the
  `while true` supervisor proven below (the in-manifest `Restart=always`
  substitute). Config/runtime (`loops.toml`, `state/`) are gitignored and seeded
  by the `[hook]` on first activation.
- **Verified live here** (fresh checkout + `flox activate --start-services`): all
  four services `Running`; `flox services status/logs/restart/stop` all work;
  `doorbell` exits cleanly on the missing-token path and the supervisor restarts
  it (safe by construction — no token in this repo); `dashboard-server` serves
  `http://127.0.0.1:8522/dashboard.{html,json}` (alt port so it coexists with a
  live installation's dashboard on `:8422`); `usage-fetch` wrote
  `state/usage/budget.json` from the read-only usage API; restart rotated the PID;
  stop left all `Completed` with **no surviving processes**. The live
  installation's services were not touched.

The capability findings below (from the original spike) still stand and remain
the evidence base for the port.

## Question

Can a Flox environment define a `[services]` entry that runs `bin/doorbell`
(a 30s Slack-DM poller) as a Flox-managed service instead of a hand-installed
systemd `--user` unit? Worth porting the real services over later?

## What was built

_(Original spike — superseded by the real port described in the Update above.
The sim files no longer exist; kept here as the record of how the findings were
obtained.)_

- `bin/doorbell.real` — verbatim copy of the hub's `bin/doorbell` (reference only).
- `bin/doorbell-sim` — a **faithful, side-effect-free** stand-in: same runtime
  shape (infinite poll loop, per-iteration heartbeat file, stdout logging) but
  **no Slack API calls, no `agent-deck` kicks, no reads of live `state/`**. This
  is what actually runs as the service, so the spike exercises Flox's lifecycle
  without any risk to the live hub. Knobs via env: `SIM_POLL_S`, `SIM_CRASH_AFTER`.
- `.flox/env/manifest.toml` — three services, deliberately chosen to probe
  distinct behaviors:
  - `doorbell`  — the clean always-on case (`exec python3 …`).
  - `crasher`   — same sim forced to `exit(1)` after 3 ticks (crash-restart probe).
  - `resilient` — the same crash wrapped in a `while` supervisor loop
    (in-manifest crash-restart workaround).

### The manifest (the part that matters)

```toml
[services]
doorbell.command = '''exec python3 "$FLOX_ENV_PROJECT/bin/doorbell-sim"'''

# In-manifest Restart=always equivalent (no exec — the loop must survive the child):
resilient.command = '''
  while true; do
    SIM_CRASH_AFTER=3 python3 "$FLOX_ENV_PROJECT/bin/doorbell-sim" || true
    echo "resilient: inner sim exited ($?), restarting in 1s" >&2
    sleep 1
  done
'''
```

A real port would be a one-liner per unit, e.g.:
`doorbell.command = '''exec python3 "$FLOX_ENV_PROJECT/bin/doorbell"'''`

## Results — what works

| Capability | Result | Evidence |
|---|---|---|
| Runs continuously as a managed service | ✅ | `doorbell` observed `Running`, 12+ ticks, heartbeat file advancing, stable PID |
| `flox services status` | ✅ | shows NAME/STATUS/PID; works from any activation of the env |
| `flox services logs <svc>` | ✅ | captures stdout+stderr (journald-like) while process-compose is alive |
| `flox services restart <svc>` | ✅ | old proc SIGTERM'd, new PID, counter reset to 1 |
| `flox services stop` / `start` | ✅ | both work |
| Persistent on-disk log | ✅ (partial) | orchestrator log at `.flox/log/services.*.log` (lifecycle events; per-process stdout is served live by `flox services logs`) |
| Graceful shutdown | ✅ | sim's SIGTERM handler runs; clean exit |

## Results — gaps vs systemd `--user`

1. **Autostart on boot — NO.** Flox services only exist inside an *activation*.
   There is no boot integration and no such flag (`flox services` = just
   start/stop/restart/status/logs; `flox activate` = only `-s/--start-services`
   / `--no-start-services`). systemd `--user` + lingering starts on boot for free.

2. **Survival independent of a session — NO.** Services are bound to the
   activation lifetime. When the owning activation exits (tested: a bounded
   `flox activate -s -- … sleep N …` that ended normally), **all services went
   `Stopped` and no `doorbell-sim` process survived.** systemd keeps `--user`
   units running across logout/reboot (with lingering). → An always-on Flox
   service needs a **persistent activation holding it open**.

3. **Crash-restart — NO native support.** `crasher` exited 1 and stayed
   `Completed (1)` — process-compose did **not** restart it. The manifest schema
   has **no restart-policy key**: `restart`, `restart.policy`, and
   `availability.restart` were all **rejected** on `flox edit` (empirically
   probed). systemd `Restart=always` has no declarative equivalent.
   - **Workaround (proven):** wrap the command in a `while true; do …; done`
     supervisor. `resilient` auto-recovered across **three distinct PIDs**
     (3139691 → crash → 3141329 → crash → 3142988). This is a real, in-manifest
     `Restart=always` substitute.
   - Caveat: `is-daemon`, `systems`, `vars`, and `shutdown.command` **are** valid
     service keys — the gap is specific to restart policy.

4. **Stop is asynchronous.** `flox services stop` returns while the process is
   still `Terminating` (honoring its SIGTERM grace period); an immediate
   `start` raced and failed with "process is already running". Scripted
   stop→start must wait for `Completed`/`Stopped`. `restart` does the right
   thing internally and is the safer verb.

## Recommendation

**Yes, port the service *definitions* into a Flox manifest — but keep exactly one
systemd unit as the supervisor.** Best of both:

- Move `doorbell`, `dashboard-refresh`, `dashboard-server`, `usage-fetch` into
  `[services]` in the hub's manifest. Win: declarative, in-repo, versioned,
  reproducible, one `flox services` UX instead of N hand-installed unit files.
- Replace the N per-service systemd units with **one** supervisor unit (systemd
  `--user`, `Restart=always`, lingering enabled) whose ExecStart holds a
  persistent activation: `flox activate --start-services -- sleep infinity`.
  Templated at `infra/nano-ops-services.service.example`; runbook in
  `docs/always-on.md`. systemd then supplies **boot-autostart** and
  restarts the whole process-compose group if it dies; process-compose supervises
  the individual services within.
- For per-service crash-restart (the one thing process-compose won't do
  natively), wrap each command in the `while true` supervisor pattern proven
  above. The real `doorbell` already loops through transient errors internally,
  so only a hard exit (e.g. missing token) needs it — cheap insurance.

**What this buys vs today:** N systemd unit files → 1 supervisor unit + a
declarative, in-repo manifest. **What to watch:** the single-service-crash blind
spot (mitigated by the `while` wrapper) and the async-stop wrinkle. Net: worth
doing; the manifest is clean and the lifecycle UX is good. Don't drop systemd
entirely — it's still needed for boot + top-level supervision.

## Reproduce (real port)

```bash
cd <this repo>
flox activate --start-services -- bash -c '
  sleep 8
  flox services status                     # all four Running
  flox services logs doorbell              # clean missing-token exit + restart
  curl -s http://127.0.0.1:8522/dashboard.json | head -c 60; echo
  flox services restart doorbell           # rotates PID
  flox services stop                       # all Completed, no survivors
'
```

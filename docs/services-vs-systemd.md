# Flox `[services]` vs systemd `--user` for the always-on pieces

Why the always-on set is declared as Flox `[services]` with each command wrapped
in a `while true` supervisor, and why boot survival still needs exactly one
systemd unit. Measured on Flox 1.13.2 with the bundled process-compose 1.94.0.

## What was measured

Three services, chosen to probe distinct behaviors, over a side-effect-free
stand-in for `bin/doorbell` (`doorbell-sim`: the same runtime shape — infinite
poll loop, per-iteration heartbeat file, stdout logging — but no API calls and
no reads of live `state/`):

- `doorbell`  — the clean always-on case (`exec python3 …`).
- `crasher`   — the same stand-in forced to `exit(1)` after 3 ticks
  (crash-restart probe).
- `resilient` — the same crash wrapped in a `while` supervisor loop
  (in-manifest crash-restart workaround).

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

A real port is a one-liner per unit, e.g.
`doorbell.command = '''exec python3 "$FLOX_ENV_PROJECT/bin/doorbell"'''`.

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

## Reproduce

Against a checkout of this repo, to confirm the ported services behave as
described above:

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

# Always-on — surviving logout and reboot

The always-on set (doorbell, dashboard-refresh, dashboard-server, usage-fetch)
is declared as Flox `[services]` in `.flox/env/manifest.toml`. That gives one
declarative, in-repo, versioned definition and one `flox services` UX — but it
does **not** give boot survival, because of two measured gaps
([`../FINDINGS.md`](../FINDINGS.md)):

1. **Services live only inside an activation.** When the owning activation
   exits, every service stops and no process survives. There is no boot
   integration and no flag for one.
2. **process-compose has no restart policy.** A service that exits stays
   `Completed`; `restart`, `restart.policy` and `availability.restart` are all
   rejected by the manifest schema.

Gap 2 is already handled in-repo: every service command in the manifest is
wrapped in a `while true` supervisor, the in-manifest `Restart=always`
substitute. Gap 1 is what this runbook closes.

## The shape: exactly one systemd unit

Do not recreate one unit per service — that is the sprawl the Flox port
removed. Install **one** `--user` unit whose job is to hold a single activation
open. systemd then supplies boot-autostart and restarts the whole group if it
dies; process-compose supervises the individual services inside it.

```
systemd --user unit  (Restart=always, lingering)
  └── flox activate --start-services -- <hold open forever>
        └── process-compose
              ├── doorbell           (while-true supervisor)
              ├── dashboard-refresh  (while-true supervisor)
              ├── dashboard-server   (while-true supervisor)
              └── usage-fetch        (while-true supervisor + periodic sleep)
```

## Install

Nothing in this repo installs a unit, and `bin/ops` never touches one. The
persistence artifact is created by the operator, on their own machine, from
[`../infra/nano-ops-services.service.example`](../infra/nano-ops-services.service.example)
— a template with placeholders, not an installable file.

```bash
# 1. Lingering, so --user units start at boot with no login.
loginctl enable-linger "$USER"

# 2. Render the template with this machine's absolute paths.
mkdir -p ~/.config/systemd/user
sed -e "s|@REPO@|$(pwd)|g" -e "s|@FLOX@|$(command -v flox)|g" \
    infra/nano-ops-services.service.example \
    > ~/.config/systemd/user/nano-ops-services.service

# 3. Enable and start.
systemctl --user daemon-reload
systemctl --user enable --now nano-ops-services

# 4. Confirm: the unit is active, and the services are Running inside it.
systemctl --user status nano-ops-services --no-pager | head -5
bin/ops services status
```

Absolute paths matter: a `--user` unit does not inherit a login shell's `PATH`,
so `ExecStart` must name the `flox` binary in full. That is why the template
carries placeholders instead of shipping pre-filled values — a committed
absolute path would be one machine's, and would be personal data besides.

## Operate

| Want | Do |
|---|---|
| See what is running | `bin/ops services status` |
| Follow one service's output | `bin/ops services logs <name>` |
| Bounce one service | `flox services restart <name>` (`restart`, not stop-then-start — `stop` returns while the process is still terminating, and an immediate `start` races with "process is already running") |
| Stop everything, keep the unit | `systemctl --user stop nano-ops-services` |
| Remove boot survival | `systemctl --user disable --now nano-ops-services` |

## Foreground alternative

For a session-scoped run — a laptop, a demo, or a first look — skip systemd
entirely:

```bash
flox activate --start-services      # services run for this activation's lifetime
```

Everything stops cleanly when the activation exits, leaving no surviving
process. That property is what makes this repo safe to run alongside an existing
installation.

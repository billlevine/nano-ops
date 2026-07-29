# The extraction allowlist — template

**This is a template, not a policy.** It ships in the public core to document
the *format* the mechanic's EXTRACTION lens parses. It decides nothing on its
own: copy it into your private fork as `docs/extraction-allowlist.md`, replace
every row with your own, and point `loops.toml` at it:

```toml
[extraction]
repo = "~/src/<your-public-core-checkout>"   # where the core lives on this machine
allowlist = "docs/extraction-allowlist.md"   # resolved against THIS repo, not the core
```

The allowlist belongs in the **fork**, with the paths it governs — not in the
core. One copy, so there is nothing to drift out of sync with.

Below, "here" means the public core.

## The rule

**Mechanism is public. Identity, policy, and data are not.**

A file belongs in the public core if a stranger could clone it, fill in their
own `loops.toml`, and have it work for their estate. If a file only makes sense
for one operator — their name, their employer, their repos, their loops, their
standing orders — it belongs in the private fork.

## The invariant

Nothing from a live installation may land here. Fill this table in for your own
estate; the rows below are the ones that apply to any installation of this
core.

| Never committed | Where it lives instead |
|---|---|
| `state/` in any form — cursors, ledger, per-loop history, dashboard output | gitignored; runtime only |
| Secrets of any kind | a file under gitignored `state/`, never a value in a committed file |
| Channel ids, user ids, workspace ids | `loops.toml` `[hub]`, gitignored |
| A real `loops.toml` (real loops, real channel, real hosts) | gitignored; `loops.example.toml` is the committed documented form |
| Absolute personal paths, hostnames, tailnet or LAN addresses | `loops.toml`, or resolved at runtime from `$HOME` / the repo root |
| Operator names, personas, agent-deck group names | `loops.toml` `[hub]`; code defaults to the neutral `"ops"` |
| Employer, product, repo and ticket-tracker names | the private fork |
| Ledger or history *content*, even as an example | invented examples only |

The check before any commit to the core is a grep audit for those patterns. It
is not enough for a file to look generic — a hardcoded session title or group
name is just as much one installation's identity as a channel id is.

## What is in, and why

The lens reads the **first column** of this table as its include patterns. A
row naming a directory expands to the files under it, and an included script
pulls in its sibling test.

| Component | Why it is core |
|---|---|
| `bin/` | the operator CLI and the always-on services: mechanism, with identity read from config. |
| `hub/` | the hub session's home and its tick skill — the control-plane mechanism. |
| `loops/example/` | the loop contract as a copyable template. No installation's policy in it. |
| `loops/mechanic/` | the diagnose-and-propose pass. Reads whatever the estate writes; names no loop, repo or operator. |
| `.flox/env/manifest.toml` | the toolchain and the always-on `[services]` declarations. |
| `loops.example.toml` | the registry's documented shape, with no real entries. |
| `docs/` | architecture and runbooks. |
| `tests/` | they run against tempdirs, never real state. |

## What is deliberately out

The lens reads these bullets, and the invariant table above, as its exclude
patterns.

- **Your working loops.** A loop is where policy concentrates: which repos it
  watches, which org's conventions it enforces, which reviewers it pings. The
  *shape* of a loop is core; a loop carrying one estate's standing orders is
  not. List each such directory here, one per bullet.
- **Standing orders in the hub skill.** The hub skill carries the tick
  mechanism — classification, ledger, health pass, pacing. Rules naming one
  operator's loops and accounts stay in the fork's copy of the skill.
- **`docs/lessons.md`** and anything else distilled from one estate's ledger:
  it reads as a diary of that installation.
- **Machine-setup runbooks** naming particular hosts, drives, or accounts.
- **Harness-generated runtime artifacts** — session lock files and the like,
  carrying pids and session ids. Never authored by anyone, machine-specific by
  nature.

CAUTION for whoever edits this section: the parser pulls **every** backticked,
path-shaped token out of the whole "What is deliberately out" section and
treats it as an exclude pattern — not just one per bullet. A second backticked
mention here, even as an illustrative aside, silently excludes whatever real
path it matches. Keep at most one backticked path per exclude bullet, and never
write a real *include* path in backticks anywhere in this section.

## Keeping it true

The private fork tracks the core as `upstream` and rebases onto it. That only
stays sustainable while the split above holds: improvements to *mechanism* are
made in the core and flow down, and installation *policy* stays in the fork's
own commits. A mechanism fix made only in the fork becomes a permanent rebase
conflict — and an identity leaked into the core becomes permanent history.

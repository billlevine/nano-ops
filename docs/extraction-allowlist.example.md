# The extraction allowlist — template

**This is a template, not a policy.** It ships in the public core to document
the *format* the mechanic's EXTRACTION lens parses. It decides nothing on its
own: copy it into your private fork as `docs/extraction-allowlist.md`, edit it
section by section — each section below says which of its rows to keep and which
to replace — and point `loops.toml` at it:

```toml
[extraction]
repo = "~/src/<your-public-core-checkout>"   # enables the lens; only its non-emptiness is checked
allowlist = "docs/extraction-allowlist.md"   # optional (this is the default); resolved against the FORK
```

`repo` is a feature flag, not a location: the lens tests that it is non-empty
and never opens, stats or resolves it, so a typo or a left-in placeholder gives
you a lens that looks healthy and quietly compares nothing. Every path the lens
resolves — including `allowlist` itself — is relative to the fork.

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

Nothing from a live installation may land here. **Keep every row below and add
to them** — they are the categories that apply to any installation of this core,
so this is the one table you extend rather than replace.

Only the **first column** is parsed, and only backticked paths in it. Most rows
here name a *category* rather than a path, so the parser cannot see them: they
bind you, not the lens. Anything that must actually be excluded needs a
backticked path, either in a first column here or in a bullet under "What is
deliberately out".

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

**Replace the broad rows here with your own** — `bin/`, `hub/` and `docs/` are
this core's shape, not necessarily your fork's.

The lens reads the **backticked paths in the first column** of this table as its
include patterns — and, because any line that is not a table row is scanned
whole, a backticked path anywhere in this section's *prose* becomes an include
too. A row naming a directory expands to the files under it. (A row naming an
individual **file** additionally pulls in a `test_<name>.py` sitting in that
file's own directory; with tests collected under `tests/`, as they are here,
that rule never fires.)

| Component | Why it is core |
|---|---|
| `bin/` | the operator CLI and the always-on services: mechanism, with identity read from config. |
| `hub/` | the hub session's home and its tick skill — the control-plane mechanism. |
| `infra/` | the host-level service units the always-on set installs. |
| `loops/example/` | the loop contract as a copyable template. No installation's policy in it. |
| `loops/mechanic/` | the diagnose-and-propose pass. Reads whatever the estate writes; names no loop, repo or operator. |
| `.flox/env/manifest.toml` | the toolchain and the always-on `[services]` declarations. |
| `.flox/env/manifest.lock` | the pinned resolution; the manifest without it is half a reproducible toolchain. |
| `loops.example.toml` | the registry's documented shape, with no real entries. |
| `docs/` | architecture and runbooks. Broad by design, so the exclusions below have to carry their weight. |
| `tests/` | they run against tempdirs, never real state. |

## What is deliberately out

The lens reads the **backticked paths** in these bullets, and in the first
column of the invariant table above, as its exclude patterns. A bullet with no
backticked path in it is documentation for a human and nothing more — the parser
cannot see prose. So **every path you actually need excluded must appear here in
backticks**, however obvious the surrounding sentence makes it.

- **Your working loops.** A loop is where policy concentrates: which repos it
  watches, which org's conventions it enforces, which reviewers it pings. The
  *shape* of a loop is core; a loop carrying one estate's standing orders is
  not. List each such directory here in backticks — `loops/<your-loop>/`, one
  per bullet — or it is not excluded.
- **Standing orders in the hub skill.** The hub skill carries the tick
  mechanism — classification, ledger, health pass, pacing. Rules naming one
  operator's loops and accounts stay in the fork's copy of the skill.
- **`docs/lessons.md`** and anything else distilled from one estate's ledger:
  it reads as a diary of that installation.
- **`docs/extraction-allowlist.md`** — this file's fork-local descendant. It
  governs the core's paths but is your policy, and `docs/` above would otherwise
  nominate it for extraction into the core on every pass.
- **`docs/ideas.md`** — one estate's backlog, appended to constantly.
- **Machine-setup runbooks** naming particular hosts, drives, or accounts —
  `docs/new-machine-setup.md` or whatever yours is called.
- **Harness-generated runtime artifacts** — session lock files and the like,
  carrying pids and session ids. Never authored by anyone, machine-specific by
  nature.

CAUTION for whoever edits this file — three ways to get silently wrong results:

- **The three section headings are syntax.** The parser finds its buckets by
  looking for `what is in`, `invariant` and `deliberately out` in a heading.
  Rename one and its bucket comes back empty, with no error and a report
  indistinguishable from a genuinely clean pass.
- **A backticked *subpath* of an included row really does exclude it.** Backtick
  one script's path inside an included directory — say the doorbell under
  `bin/` — and that one file drops out of the core, quietly. A backticked path
  that is *exactly* an include-table row, by contrast, is ignored here: the
  in-table decision wins, so a cross-reference costs nothing. Which is why the
  example in this bullet is written the long way round rather than backticked:
  every path-shaped token in backticks anywhere in this section is a pattern,
  including one meant only as an illustration.
- **You cannot narrow a broad include from this section.** Naming `docs/` here
  will not shrink the `docs/` include above; it is the exact-match case, and it
  silently does nothing. Exclude the specific paths under it instead, as the
  bullets above do.

## Keeping it true

The private fork tracks the core as `upstream` and takes its updates from
there — see [`upstream-updates.md`](upstream-updates.md) for how, including
which integration verb fits your fork. That only stays sustainable while the
split above holds: improvements to *mechanism* are made in the core and flow
down, and installation *policy* stays in the fork's own commits. A mechanism
fix made only in the fork is a conflict on every future update — and an
identity leaked into the core becomes permanent history.

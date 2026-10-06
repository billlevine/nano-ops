"""Read token and turn shape from Codex rollout JSONL files.

Codex's ``event_msg/token_count`` values are cumulative.  Therefore a
session's usage is the LAST ``total_token_usage`` in its rollout, never the
sum of token-count events; summing them double- or triple-counts earlier turns.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def read_rollout(path: Path, start: datetime, end: datetime) -> dict | None:
    meta, last_usage, turns, tools, first, last = {}, {}, set(), {}, None, None
    models, partial = set(), False
    try:
        raw = path.read_bytes()
    except OSError:
        raise
    if raw and not raw.endswith(b"\n"):
        partial = True
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            partial = True
            continue
        stamp = _ts(row.get("timestamp"))
        payload = row.get("payload") or {}
        kind = row.get("type")
        if kind == "session_meta":
            meta.update(payload)
        if kind == "turn_context":
            if payload.get("model"):
                models.add(payload["model"])
            if payload.get("turn_id"):
                turns.add(payload["turn_id"])
        if stamp is None or not (start <= stamp < end):
            continue
        first = stamp if first is None or stamp < first else first
        last = stamp if last is None or stamp > last else last
        if kind == "event_msg" and payload.get("type") == "token_count":
            candidate = (payload.get("info") or {}).get("total_token_usage")
            if isinstance(candidate, dict):
                last_usage = candidate
        if kind in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or kind)
            tools[name] = tools.get(name, 0) + 1
        if kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or payload.get("type"))
            tools[name] = tools.get(name, 0) + 1
    if first is None:
        return None
    return {"session_id": meta.get("id") or path.stem, "transcript": str(path),
            "cwd": meta.get("cwd"), "git_branch": meta.get("git", {}).get("branch"),
            "first_ts": first.isoformat(), "last_ts": last.isoformat(),
            "turns": len(turns), "models": sorted(models), "usage": last_usage,
            "tools": dict(sorted(tools.items())), "partial": partial}


def scan_day(base: Path, day, start: datetime, end: datetime) -> list[dict]:
    directory = base / day.strftime("%Y/%m/%d")
    return [row for path in sorted(directory.glob("*.jsonl"))
            if (row := read_rollout(path, start, end)) is not None]


# ── the weekly limit, as Codex itself reports it the contract ────────────────────
#
# Every ``token_count`` row also carries ``rate_limits``: up to two windows
# (``primary``, ``secondary``), each ``{used_percent, window_minutes,
# resets_at}`` with ``resets_at`` in epoch seconds.  On this account (plan
# ``prolite``) ``primary`` is the 10080-minute week and ``secondary`` is null,
# but which slot holds the week is the server's choice, so it is found by its
# window length rather than by its slot name.  The reading is only as recent
# as the last Codex turn anywhere on this host: nothing here asks OpenAI.

WEEK_MINUTES = 10080


def weekly_window(rate_limits) -> dict | None:
    """The 7-day window out of one ``rate_limits`` object, or None."""
    if not isinstance(rate_limits, dict):
        return None
    for slot in ("primary", "secondary"):
        window = rate_limits.get(slot)
        if (isinstance(window, dict)
                and window.get("window_minutes") == WEEK_MINUTES
                and isinstance(window.get("used_percent"), (int, float))):
            return {"slot": slot, **window}
    return None


def _day_dirs(base: Path, start: datetime, end: datetime) -> list[Path]:
    from datetime import timedelta
    day, dirs = start.date(), []
    while day <= end.date():
        dirs.append(base / day.strftime("%Y/%m/%d"))
        day += timedelta(days=1)
    return dirs


def latest_rate_limits(base: Path, now: datetime,
                       lookback_days: int = 8) -> dict | None:
    """The newest ``rate_limits`` row across rollouts of the last
    ``lookback_days`` days: ``{observed_at, rate_limits, transcript}``.

    None when no rollout in that span carries one.  An unreadable rollout
    raises OSError: a file that would not open is not a file with no row."""
    from datetime import timedelta
    paths = [p for d in _day_dirs(base, now - timedelta(days=lookback_days), now)
             for p in d.glob("rollout-*.jsonl")]
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    best = None
    for path in paths:
        if best and path.stat().st_mtime < best["observed_at"].timestamp():
            break  # every older file was last written before the best row
        for line in reversed(path.read_bytes().splitlines()):
            if b'"rate_limits"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            limits = (row.get("payload") or {}).get("rate_limits")
            stamp = _ts(row.get("timestamp"))
            if isinstance(limits, dict) and stamp is not None:
                if best is None or stamp > best["observed_at"]:
                    best = {"observed_at": stamp, "rate_limits": limits,
                            "transcript": str(path)}
                break
    return best


TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens",
              "reasoning_output_tokens", "total_tokens")


def window_tokens(base: Path, start: datetime, end: datetime,
                  straddle_days: int = 2) -> dict:
    """Tokens Codex spent on this host in ``[start, end)``.

    ``total_token_usage`` is cumulative per session, so a session's share of
    the window is its last total inside the window MINUS its last total
    before ``start`` — a session that began on Monday and ran into Tuesday's
    window contributes only Tuesday's turns.  Rollouts are filed under the
    day they STARTED, so the scan reaches ``straddle_days`` before ``start``.
    """
    from datetime import timedelta
    totals = {key: 0 for key in TOKEN_KEYS}
    sessions = 0
    for directory in _day_dirs(base, start - timedelta(days=straddle_days), end):
        for path in sorted(directory.glob("rollout-*.jsonl")):
            before, inside = {}, None
            for line in path.read_bytes().splitlines():
                if b'"token_count"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get("payload") or {}
                usage = (payload.get("info") or {}).get("total_token_usage")
                stamp = _ts(row.get("timestamp"))
                if not isinstance(usage, dict) or stamp is None:
                    continue
                if stamp < start:
                    before = usage
                elif stamp < end:
                    inside = usage
            if inside is None:
                continue
            sessions += 1
            for key in TOKEN_KEYS:
                totals[key] += max(0, int(inside.get(key) or 0)
                                   - int(before.get(key) or 0))
    return {**totals, "sessions": sessions}


def budget_signal(base: Path, now: datetime) -> dict:
    """The Codex twin of ``state/usage/budget.json``: same ``weekly_pct`` /
    ``weekly_resets_at`` / ``source`` shape, so ``bin/dashboard`` paces both
    with one function.

    ``source`` says what was read, never a guess (docs/absence-contract.md):
    ``rollout`` — a weekly window was read off a rollout row;
    ``no_weekly_window`` — rows carry ``rate_limits`` but no 7-day window;
    ``window_ended`` — the newest reading's week reset before ``now``, so
    nothing says what this week's percent is (it is NOT 0);
    ``absent`` — no rollout in the lookback carries ``rate_limits`` at all;
    ``error`` — a rollout would not read.
    ``weekly_pct`` is None for every source but ``rollout``.  The tokens are
    a separate reading with their own ``tokens_error``: a failed token scan
    does not blank a percent that was read, nor the other way round."""
    from datetime import timedelta, timezone
    out = {"generated_at": now.isoformat(timespec="seconds"),
           "source": "absent", "weekly_pct": None, "weekly_resets_at": None,
           "observed_at": None, "plan_type": None, "why": None,
           "tokens": None, "tokens_since": None, "tokens_error": None}
    week_start = None
    if not base.is_dir():
        out["why"] = f"{base} does not exist"
        out["tokens_error"] = out["why"]
    else:
        try:
            latest = latest_rate_limits(base, now)
        except OSError as err:
            latest = None
            out.update(source="error", why=f"a rollout would not read: {err}")
        if latest is None and out["source"] != "error":
            out["why"] = "no Codex rollout in the last 8 days carries rate_limits"
        elif latest is not None:
            limits = latest["rate_limits"]
            out["observed_at"] = latest["observed_at"].isoformat(timespec="seconds")
            out["plan_type"] = limits.get("plan_type")
            window = weekly_window(limits)
            if window is None:
                out.update(source="no_weekly_window",
                           why="rate_limits carries no 10080-minute window")
            else:
                reset = datetime.fromtimestamp(window["resets_at"], timezone.utc)
                out["weekly_resets_at"] = reset.isoformat(timespec="seconds")
                if reset <= now:
                    out.update(source="window_ended",
                               why="the newest reading is from a week that has "
                                   "since reset; no Codex turn has reported this week's")
                else:
                    out.update(source="rollout", weekly_pct=window["used_percent"])
                    week_start = reset - timedelta(minutes=WEEK_MINUTES)
        # Tokens: this limit window when one is known, else a trailing 7 days.
        since = week_start or now - timedelta(days=7)
        out["tokens_since"] = since.isoformat(timespec="seconds")
        try:
            out["tokens"] = window_tokens(base, since, now)
        except OSError as err:
            out["tokens_error"] = f"a rollout would not read: {err}"
    return out

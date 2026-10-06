"""Incremental, token-free scanner for Claude Code JSONL transcripts.

The checkpoint is only an optimization: inode changes, truncation, and corrupt
cache entries all fall back to parsing the file from byte zero.  Incomplete
trailing lines are deliberately left behind for the next scan.
"""
from __future__ import annotations

import json
from pathlib import Path


def iter_usage_records(raw: bytes, cwd_to_actor=lambda cwd: "other"):
    """Yield compact usage records from complete JSONL bytes.

    The first five fields retain dashboard's historical cache contract.  The
    remaining fields are git branch, sidechain flag, attribution skill, cwd,
    and per-turn tool-name counts.
    """
    for line in raw.decode("utf-8", "ignore").splitlines():
        if '"usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        ts = d.get("timestamp")
        if not ts:
            continue
        try:
            from datetime import datetime
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            continue
        message = d.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            continue
        tools = {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = str(block.get("name") or "unknown")
                tools[name] = tools.get(name, 0) + 1
        yield [t, usage, message.get("model"), cwd_to_actor(d.get("cwd")),
               d.get("sessionId") or d.get("session_id"), d.get("gitBranch"),
               bool(d.get("isSidechain")), d.get("attributionSkill"),
               d.get("cwd"), tools]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def scan_records(cutoff_ts: float, byte_cap: int, *, base: Path,
                 cache_path: Path, cwd_to_actor=lambda cwd: "other") -> list[list]:
    cached = _read_json(cache_path)
    files_cache = cached.get("files") if isinstance(cached, dict) else None
    if not isinstance(files_cache, dict):
        files_cache = {}
    records, new_cache, read_bytes = [], {}, 0
    for path in sorted(base.glob("*/*.jsonl")):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff_ts:
            continue
        key, prev = str(path), files_cache.get(str(path))
        if (isinstance(prev, dict) and prev.get("inode") == stat.st_ino
                and isinstance(prev.get("offset"), int)
                and 0 <= prev["offset"] <= stat.st_size):
            offset, lines = prev["offset"], list(prev.get("lines") or [])
        else:
            offset, lines = 0, []
        if offset < stat.st_size and read_bytes < byte_cap:
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
            except OSError:
                chunk = b""
            read_bytes += len(chunk)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                lines.extend(iter_usage_records(chunk[:newline + 1], cwd_to_actor))
                offset += newline + 1
        kept = [line for line in lines if line[0] >= cutoff_ts]
        records.extend(kept)
        new_cache[key] = {"inode": stat.st_ino, "size": stat.st_size,
                          "mtime": stat.st_mtime, "offset": offset,
                          "lines": kept}
        if read_bytes >= byte_cap:
            break
    _write_cache(cache_path, {"version": 2, "files": new_cache})
    return records

"""Publish and read isolated local HTML artifacts with descriptor-relative
opens, a file-type allowlist and sandboxed HTTP responses.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import stat
import tempfile
import urllib.parse

ARTIFACTS_SUBDIR = "artifacts"
URL_PREFIX = "/artifacts/"

# The steward registry's own slug rule (lib/steward.py SLUG_RE), repeated
# rather than imported so the server's request path imports nothing heavier.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# One path segment: starts with a letter or digit (so never `.`, `..` or a
# dotfile), then the characters a filename actually needs.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_DEPTH = 8
MAX_FILE_BYTES = 32 * 1024 * 1024

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

# No allow-same-origin, ever: see the module docstring.
SANDBOX_CSP = ("sandbox allow-scripts allow-popups "
               "allow-popups-to-escape-sandbox allow-downloads allow-modals")


class ArtifactError(ValueError):
    """A request or a publish this module refuses, with the reason."""


def root_for(state_dir: str) -> str:
    return os.path.join(state_dir, ARTIFACTS_SUBDIR)


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ArtifactError(f"not a steward slug: {slug!r}")
    return slug


def check_relpath(rel: str) -> list:
    """The segments of a relative artifact path, or ArtifactError."""
    if not isinstance(rel, str) or not rel:
        raise ArtifactError("empty artifact path")
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH:
        raise ArtifactError(f"artifact path deeper than {MAX_DEPTH}")
    for seg in parts:
        if not SEGMENT_RE.fullmatch(seg):
            raise ArtifactError(f"artifact path segment refused: {seg!r}")
    if content_type(parts[-1]) is None:
        raise ArtifactError(f"extension not served: {parts[-1]!r}")
    return parts


def content_type(name: str):
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower())


def _url_parts(url_path: str) -> list:
    if not url_path.startswith(URL_PREFIX):
        raise ArtifactError("not an artifact path")
    try:
        decoded = urllib.parse.unquote(url_path[len(URL_PREFIX):], errors="strict")
    except UnicodeDecodeError as exc:
        raise ArtifactError("undecodable artifact path") from exc
    slug, _, rel = decoded.partition("/")
    return [check_slug(slug), *check_relpath(rel)]


def open_artifact(state_dir: str, url_path: str):
    """Open the validated inode, never following links, including the slug.

    Directory descriptors pin each ancestor across rename/symlink races.
    The caller must read this handle rather than reopen a checked pathname.
    """
    parts = _url_parts(url_path)
    directory = fd = None
    try:
        directory = os.open(root_for(state_dir),
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ArtifactError("not a regular, size-bounded artifact")
        source = os.fdopen(fd, "rb")
        fd = None
        return source, content_type(parts[-1])
    except OSError as exc:
        raise ArtifactError("no such artifact") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def resolve(state_dir: str, url_path: str) -> tuple:
    """Validate a path for listings. Serving must use open_artifact instead."""
    parts = _url_parts(url_path)
    source, ctype = open_artifact(state_dir, url_path)
    source.close()
    return os.path.abspath(os.path.join(root_for(state_dir), *parts)), ctype


def _namespace(state_dir: str, slug: str) -> str:
    """Local writer guard; the state directory is owned by the estate user."""
    root = root_for(state_dir)
    base = os.path.join(root, check_slug(slug))
    if os.path.islink(root) or os.path.islink(base):
        raise ArtifactError("artifact namespace must not be a symlink")
    return base


def _copy_file(source: str, destination: str) -> None:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ArtifactError("not a regular, size-bounded artifact")
        data = src.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ArtifactError("artifact too large")
        with open(destination, "wb") as dst:
            dst.write(data)
    os.chmod(destination, 0o644)


def _ignore_unservable(directory, names):
    ignored = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path):
            raise ArtifactError(f"source symlink refused: {path}")
        if not SEGMENT_RE.fullmatch(name) or (
                not os.path.isdir(path) and content_type(name) is None):
            ignored.append(name)
    return ignored


def servable_files(state_dir: str, slug: str) -> list:
    """Every file under one steward's directory the server would serve,
    as sorted relative paths. Missing directory -> []."""
    base = _namespace(state_dir, slug)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if SEGMENT_RE.fullmatch(d))
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            rel = rel.replace(os.sep, "/")
            try:
                resolve(state_dir, URL_PREFIX + slug + "/" + urllib.parse.quote(rel))
            except ArtifactError:
                continue
            out.append(rel)
    return sorted(out)


def slugs(state_dir: str) -> list:
    try:
        names = os.listdir(root_for(state_dir))
    except OSError:
        return []
    return sorted(n for n in names if SLUG_RE.fullmatch(n)
                  and not os.path.islink(os.path.join(root_for(state_dir), n))
                  and os.path.isdir(os.path.join(root_for(state_dir), n)))


def base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def url_for(host: str, port: int, slug: str, rel: str) -> str:
    return f"{base_url(host, port)}{URL_PREFIX}{slug}/{urllib.parse.quote(rel)}"


def publish(state_dir: str, slug: str, source: str, name: str = "") -> list:
    """Copy a file, or a directory with its dependencies, into the steward's
    namespace. Returns the relative paths now served from it.

    A file lands as `<name or its basename>`; a directory lands as
    `<name or its basename>/...` so sibling references (theme.css, data.js)
    keep resolving. A directory is staged beside its destination before replacement. Readers
    may briefly see 404 between renames, but never a partially copied tree. Re-publishing the
    same name replaces it — that is how an artifact gets a new version.
    """
    check_slug(slug)
    source = os.path.abspath(source)
    if os.path.islink(source):
        raise ArtifactError("source symlink refused")
    dest_name = name or os.path.basename(source.rstrip(os.sep))
    if os.path.isdir(source):
        if not SEGMENT_RE.fullmatch(dest_name):
            raise ArtifactError(f"directory name refused: {dest_name!r}")
    elif os.path.isfile(source):
        if not SEGMENT_RE.fullmatch(dest_name):
            raise ArtifactError(f"file name refused: {dest_name!r}")
        check_relpath(dest_name)
    else:
        raise ArtifactError(f"no such file or directory: {source}")
    base = _namespace(state_dir, slug)
    os.makedirs(base, exist_ok=True)
    dest = os.path.join(base, dest_name)
    if os.path.isdir(source):
        stage = tempfile.mkdtemp(dir=base, prefix=".stage-")
        preserve_stage = False
        try:
            staged = os.path.join(stage, "tree")
            shutil.copytree(source, staged, symlinks=True,
                            copy_function=_copy_file, ignore=_ignore_unservable)
            for directory, dirs, files in os.walk(staged):
                for entry in dirs + files:
                    if os.path.islink(os.path.join(directory, entry)):
                        raise ArtifactError("source symlink refused")
                for entry in files:
                    check_relpath(dest_name + "/" + os.path.relpath(
                        os.path.join(directory, entry), staged))
            old = None
            if os.path.lexists(dest):
                old = os.path.join(stage, "old")
                os.replace(dest, old)
            try:
                os.replace(staged, dest)
            except BaseException:
                if old is not None:
                    try:
                        os.replace(old, dest)
                    except OSError as exc:
                        preserve_stage = True
                        raise ArtifactError(
                            f"restore failed; previous artifact retained at {old}") from exc
                raise
        finally:
            if not preserve_stage:
                shutil.rmtree(stage, ignore_errors=True)
        prefix = dest_name + "/"
        return [r for r in servable_files(state_dir, slug) if r.startswith(prefix)]
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".stage-")
    os.close(fd)
    try:
        _copy_file(source, tmp)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return [dest_name]


def remove(state_dir: str, slug: str, name: str) -> bool:
    """Remove one published file or directory by its top-level name."""
    check_slug(slug)
    if not SEGMENT_RE.fullmatch(name or ""):
        raise ArtifactError(f"name refused: {name!r}")
    target = os.path.join(_namespace(state_dir, slug), name)
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
        return True
    if os.path.lexists(target):
        os.unlink(target)
        return True
    return False


def index_html(state_dir: str, slug: str = "") -> str:
    """The `/artifacts/` (or `/artifacts/<slug>/`) listing: HTML documents
    only, since those are what somebody opens; their dependencies are not
    listed. Every name is escaped."""
    E = html.escape
    chosen = [check_slug(slug)] if slug else slugs(state_dir)
    title = f"Artifacts · {slug}" if slug else "Steward artifacts"
    rows = []
    for s in chosen:
        docs = [r for r in servable_files(state_dir, s)
                if content_type(r).startswith("text/html")]
        items = "".join(
            f'<li><a href="{E(URL_PREFIX + s + "/" + urllib.parse.quote(r), quote=True)}">'
            f"{E(r)}</a></li>" for r in docs) or "<li><em>none published</em></li>"
        rows.append(f"<section><h2>{E(s)}</h2><ul>{items}</ul></section>")
    body = "".join(rows) or "<p>No steward has published an artifact.</p>"
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)}</title><style>"
            ":root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;"
            "max-width:760px;margin:0 auto;padding:24px 16px}h2{font-size:1.05rem;"
            "margin:1.4em 0 .3em}a{word-break:break-all}</style></head><body>"
            f"<h1>{E(title)}</h1>{body}</body></html>")

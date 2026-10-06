#!/usr/bin/env python3
"""Verify isolated artifact serving, traversal/symlink rejection and response
sandboxing using temporary files and a local ephemeral HTTP listener.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN.parent / "lib"))
import steward_artifacts as art  # noqa: E402

_loader = SourceFileLoader("dashboard_server_art", str(BIN / "dashboard-server"))
_spec = spec_from_loader("dashboard_server_art", _loader)
srv = module_from_spec(_spec)
_loader.exec_module(srv)

PAGE = b"<!doctype html><title>t</title><script src='data.js'></script>"


def write(path: Path, data: bytes = PAGE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.base = self.state / "artifacts"
        write(self.base / "sample-project" / "v3.html")
        write(self.base / "sample-project" / "topology" / "data.js", b"var x=1;")
        write(self.base / "flo-88" / "v3.html", b"other steward")

    def resolve(self, path):
        return art.resolve(str(self.state), path)

    def test_serves_a_file_and_a_nested_dependency(self):
        f, ctype = self.resolve("/artifacts/sample-project/v3.html")
        self.assertEqual(Path(f).read_bytes(), PAGE)
        self.assertTrue(ctype.startswith("text/html"))
        _, ctype = self.resolve("/artifacts/sample-project/topology/data.js")
        self.assertTrue(ctype.startswith("text/javascript"))

    def test_two_stewards_same_name_do_not_collide(self):
        f, _ = self.resolve("/artifacts/flo-88/v3.html")
        self.assertEqual(Path(f).read_bytes(), b"other steward")

    def test_refusals(self):
        (self.base / "sample-project" / ".secret.html").write_bytes(b"x")
        write(self.state / "secrets" / "token.txt", b"s3cret")
        for bad in ("/artifacts/sample-project/../../secrets/token.txt",
                    "/artifacts/sample-project/%2e%2e/%2e%2e/secrets/token.txt",
                    "/artifacts/sample-project/.secret.html",
                    "/artifacts/sample-project//v3.html",
                    "/artifacts/Catalog/v3.html",
                    "/artifacts/../secrets/token.txt",
                    "/artifacts/sample-project/v3.exe",
                    "/artifacts/sample-project/missing.html",
                    "/artifacts/sample-project/topology",
                    "/artifacts/sample-project/%ff.html",
                    "/artifacts/sample-project/"):
            with self.subTest(bad=bad), self.assertRaises(art.ArtifactError):
                self.resolve(bad)

    def test_symlink_out_of_the_namespace_is_refused(self):
        write(self.state / "estate.html", b"not an artifact")
        link = self.base / "sample-project" / "escape.html"
        os.symlink(self.state / "estate.html", link)
        with self.assertRaises(art.ArtifactError):
            self.resolve("/artifacts/sample-project/escape.html")
        # ...and into ANOTHER steward's namespace, too.
        os.symlink(self.base / "flo-88" / "v3.html",
                   self.base / "sample-project" / "cross.html")
        with self.assertRaises(art.ArtifactError):
            self.resolve("/artifacts/sample-project/cross.html")

    def test_publish_directory_keeps_siblings_and_replaces_atomically(self):
        src = Path(self.tmp.name) / "src" / "topology"
        write(src / "topology.html")
        write(src / "theme.css", b"body{}")
        write(src / ".hidden.js", b"no")
        served = art.publish(str(self.state), "secure-packages", str(src))
        self.assertEqual(served, ["topology/theme.css", "topology/topology.html"])
        (src / "theme.css").unlink()
        served = art.publish(str(self.state), "secure-packages", str(src))
        self.assertEqual(served, ["topology/topology.html"])
        leftovers = [p.name for p in (self.base / "secure-packages").iterdir()
                     if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_publish_file_with_name_and_remove(self):
        src = write(Path(self.tmp.name) / "x.html")
        self.assertEqual(art.publish(str(self.state), "flo-88", str(src), "review-v9.html"),
                         ["review-v9.html"])
        self.assertIn("review-v9.html", art.servable_files(str(self.state), "flo-88"))
        for bad in ("../x.html", "a/b.html", ".x.html", "x.exe"):
            with self.subTest(bad=bad), self.assertRaises(art.ArtifactError):
                art.publish(str(self.state), "flo-88", str(src), bad)
        self.assertTrue(art.remove(str(self.state), "flo-88", "review-v9.html"))
        self.assertFalse(art.remove(str(self.state), "flo-88", "review-v9.html"))
        with self.assertRaises(art.ArtifactError):
            art.remove(str(self.state), "flo-88", "..")

    def test_namespace_symlink_and_aliases_are_refused(self):
        outside = self.state / "private"
        write(outside / "secret.txt", b"secret")
        (self.base / "linked").symlink_to(outside, target_is_directory=True)
        write(self.base / "flo-88" / ".hidden.txt", b"hidden")
        write(self.base / "flo-88" / "private.key", b"key")
        for target in (".hidden.txt", "private.key"):
            link = self.base / "flo-88" / "alias.txt"
            link.symlink_to(target)
            with self.assertRaises(art.ArtifactError):
                self.resolve("/artifacts/flo-88/alias.txt")
            link.unlink()
        with self.assertRaises(art.ArtifactError):
            self.resolve("/artifacts/linked/secret.txt")
        for action in (
                lambda: art.publish(str(self.state), "linked", str(outside / "secret.txt")),
                lambda: art.remove(str(self.state), "linked", "secret.txt")):
            with self.assertRaises(art.ArtifactError):
                action()
        self.assertEqual((outside / "secret.txt").read_bytes(), b"secret")

    def test_open_handle_survives_path_replacement(self):
        source, _ = art.open_artifact(str(self.state), "/artifacts/flo-88/v3.html")
        with source:
            path = self.base / "flo-88" / "v3.html"
            path.unlink()
            path.symlink_to(write(self.state / "secret.html", b"secret"))
            self.assertEqual(source.read(), b"other steward")

    def test_publish_filters_types_and_refuses_symlinks_and_oversize(self):
        src = Path(self.tmp.name) / "bundle"
        write(src / "index.html")
        write(src / "secret.key", b"secret")
        art.publish(str(self.state), "flo-88", str(src))
        self.assertFalse((self.base / "flo-88/bundle/secret.key").exists())
        (src / "leak.txt").symlink_to(write(self.state / "private.txt", b"secret"))
        with self.assertRaises(art.ArtifactError):
            art.publish(str(self.state), "flo-88", str(src))
        self.assertFalse((self.base / "flo-88/bundle/leak.txt").exists())
        with mock.patch.object(art, "MAX_FILE_BYTES", 1):
            with self.assertRaises(art.ArtifactError):
                art.publish(str(self.state), "flo-88", str(src / "index.html"))
            with self.assertRaises(art.ArtifactError):
                self.resolve("/artifacts/flo-88/v3.html")

    def test_failed_directory_install_restores_previous_version(self):
        src = Path(self.tmp.name) / "bundle"
        write(src / "index.html", b"old")
        art.publish(str(self.state), "flo-88", str(src))
        write(src / "index.html", b"new")
        replace = os.replace
        def fail_install(source, dest):
            if str(source).endswith("/tree"):
                raise OSError("injected install failure")
            return replace(source, dest)
        with mock.patch.object(art.os, "replace", side_effect=fail_install):
            with self.assertRaises(OSError):
                art.publish(str(self.state), "flo-88", str(src))
        self.assertEqual((self.base / "flo-88/bundle/index.html").read_bytes(), b"old")

    def test_newline_and_excessive_depth_refused(self):
        for slug in ("flo-88\n",):
            with self.assertRaises(art.ArtifactError):
                art.check_slug(slug)
        for rel in ("dir\n/index.html", "a/" * art.MAX_DEPTH + "x.html"):
            with self.assertRaises(art.ArtifactError):
                art.check_relpath(rel)

    def test_index_lists_html_only_and_escapes(self):
        write(self.base / "flo-88" / "a_b-c.html")
        page = art.index_html(str(self.state))
        self.assertIn("/artifacts/sample-project/v3.html", page)
        self.assertIn("/artifacts/flo-88/a_b-c.html", page)
        self.assertNotIn("data.js", page)
        self.assertEqual(art.index_html(str(Path(self.tmp.name) / "nope")).count("No steward"), 1)


class ServerRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        write(self.state / "artifacts" / "sample-project" / "v3.html")
        write(self.state / "artifacts" / "sample-project" / "topology" / "theme.css", b"a{}")
        write(self.state / "index.html", b"<p>dashboard shell</p>")
        self.saved = srv.STATE
        srv.STATE = str(self.state)
        self.httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        srv.STATE = self.saved
        self.tmp.cleanup()

    def get(self, path, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def test_artifact_is_served_sandboxed(self):
        status, headers, body = self.get("/artifacts/sample-project/v3.html?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, PAGE)
        csp = headers["Content-Security-Policy"]
        self.assertTrue(csp.startswith("sandbox"))
        self.assertNotIn("allow-same-origin", csp)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers, _ = self.get("/artifacts/sample-project/topology/theme.css")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/css; charset=utf-8"))

    def test_head_and_listings(self):
        status, headers, body = self.get("/artifacts/sample-project/v3.html", "HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(PAGE))
        for path in ("/artifacts", "/artifacts/", "/artifacts/sample-project/"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(b"/artifacts/sample-project/v3.html", body)
            self.assertIn("sandbox", headers["Content-Security-Policy"])

    def test_refusals_are_404(self):
        for path in ("/artifacts/sample-project/../../index.html",
                     "/artifacts/sample-project/%2e%2e/%2e%2e/index.html",
                     "/artifacts/sample-project/nope.html",
                     "/artifacts/BAD/"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_manually_added_files_and_namespace_links_are_refused(self):
        base = self.state / "artifacts/sample-project"
        write(base / "secret.key", b"secret")
        (base / "alias.txt").symlink_to("secret.key")
        (self.state / "artifacts/linked").symlink_to(base)
        os.mkfifo(base / "pipe.txt")
        for path in ("sample-project/secret.key", "sample-project/alias.txt",
                     "linked/v3.html", "linked/", "sample-project/pipe.txt"):
            self.assertEqual(self.get("/artifacts/" + path)[0], 404, path)

    def test_dashboard_routes_unchanged(self):
        status, headers, body = self.get("/")
        self.assertEqual((status, body), (200, b"<p>dashboard shell</p>"))
        self.assertNotIn("Content-Security-Policy", headers)
        self.assertEqual(self.get("/artifactsx")[0], 404)






if __name__ == "__main__":
    unittest.main()

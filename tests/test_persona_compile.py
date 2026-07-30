#!/usr/bin/env python3
"""Tests for bin/persona-compile: determinism, validation, crew-swap isolation,
install/check drift, and the guarantee that no source line is dropped.
Run: python3 tests/test_persona_compile.py

Nothing here touches personas/ in the repo — every test builds its own source
tree in a tempdir. The one exception is TestRealPack, which compiles the real
pack read-only into a tempdir to keep the shipped sources honest.
"""
import importlib.machinery
import importlib.util
import io
import contextlib
import os
import shutil
import tempfile
import unittest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "persona-compile")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_loader(
    "persona_compile",
    importlib.machinery.SourceFileLoader("persona_compile", SCRIPT))
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

WORLDVIEW = "# Trusted team\n\nShared belief one.\n\nShared belief two.\n"
HOUSE = "# House prose\n\nShort sentences only.\n"
ROLE_HUB = ("# The hub\n\n## Disposition and working style\n\nMove work.\n\n"
            "## Signature\n\nSound forward-leaning.\n")
ROLE_TRACK = ("# The tracker\n\n## Disposition and working style\n\nWatch the board.\n\n"
             "## Signature\n\nSound alert.\n")
CREW_A = "# Working clothes\n\nLiteral facts, light framing.\n"
CREW_B = "# Backstage company\n\nWarm ensemble contrast.\n"

CONFIG = 'hub = "working-clothes"\nloop_crew = "working-clothes"\n'


def build_pack(root, config=CONFIG, roles=None, crews=None):
    """Materialise a personas/ source tree under `root`. Returns the dir."""
    roles = roles or {"hub": ROLE_HUB, "tracker": ROLE_TRACK}
    crews = crews or {"working-clothes": CREW_A, "backstage-company": CREW_B}
    d = os.path.join(root, "personas")
    os.makedirs(os.path.join(d, "roles"), exist_ok=True)
    os.makedirs(os.path.join(d, "crews"), exist_ok=True)
    write(os.path.join(d, "WORLDVIEW.md"), WORLDVIEW)
    write(os.path.join(d, "HOUSE_STYLE.md"), HOUSE)
    write(os.path.join(d, "config.toml"), config)
    for name, body in roles.items():
        write(os.path.join(d, "roles", f"{name}.md"), body)
    for name, body in crews.items():
        write(os.path.join(d, "crews", f"{name}.md"), body)
    return d


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def run(*argv):
    """Invoke the CLI quietly; returns (rc, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = pc.run(list(argv) + ["--quiet"])
    return rc, err.getvalue()


def tree_bytes(d):
    out = {}
    for base, _, files in os.walk(d):
        for f in sorted(files):
            p = os.path.join(base, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d)] = fh.read()
    return out


class TestCompile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.personas = build_pack(self.tmp)
        self.out = os.path.join(self.tmp, "compiled")

    def compile(self, *extra):
        return run("--repo", self.tmp, "--personas-dir", self.personas,
                   "--out", self.out, *extra)

    def test_compiles_every_role(self):
        rc, _ = self.compile()
        self.assertEqual(rc, 0)
        for role in ("hub", "tracker"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, f"{role}.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "MANIFEST.md")))

    def test_output_is_byte_identical_across_runs(self):
        self.compile()
        first = tree_bytes(self.out)
        shutil.rmtree(self.out)
        self.compile()
        self.assertEqual(first, tree_bytes(self.out))

    def test_check_is_clean_then_detects_source_edit(self):
        self.compile()
        rc, _ = self.compile("--check")
        self.assertEqual(rc, 0)
        with open(os.path.join(self.personas, "WORLDVIEW.md"), "a", encoding="utf-8") as f:
            f.write("\nA new shared belief.\n")
        rc, err = self.compile("--check")
        self.assertEqual(rc, 1)
        self.assertIn("stale", err)

    def test_manifest_records_crew_and_hash(self):
        self.compile()
        text = slurp(os.path.join(self.out, "MANIFEST.md"))
        self.assertIn("crew: working-clothes", text)
        for role in ("hub", "tracker"):
            rec = pc.compile_role(role, self.personas,
                                  pc.load_config(os.path.join(self.personas,
                                                              "config.toml")))
            self.assertIn(rec["sha"], text)

    def test_preamble_pins_persona_below_the_operational_contract(self):
        """Every compiled body must carry the never-overrides clause."""
        self.compile()
        for role in ("hub", "tracker"):
            body = slurp(os.path.join(self.out, f"{role}.md"))
            self.assertIn("never adds authority, relaxes a safety rule", body)
            self.assertIn("Precedence: truth, then action, then the operational "
                          "contract", body)

    def test_no_source_line_is_dropped(self):
        """Required facts survive compilation: every prose line reaches the body."""
        cfg = pc.load_config(os.path.join(self.personas, "config.toml"))
        rec = pc.compile_role("tracker", self.personas, cfg)
        sources = [WORLDVIEW, HOUSE, ROLE_TRACK, CREW_A]
        for src in sources:
            for line in src.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                self.assertIn(line, rec["body"])


class TestCrewSwapIsolation(unittest.TestCase):
    """A crew change may move the crew section and nothing else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.personas = build_pack(self.tmp)

    def bodies(self, config):
        write(os.path.join(self.personas, "config.toml"), config)
        cfg = pc.load_config(os.path.join(self.personas, "config.toml"))
        return {r: pc.compile_role(r, self.personas, cfg)["body"]
                for r in ("hub", "tracker")}

    @staticmethod
    def split(body):
        """Split a compiled body into {section heading: text}."""
        out, key, buf = {}, "(preamble)", []
        for line in body.splitlines():
            if line.startswith("### "):
                out[key] = "\n".join(buf)
                key, buf = line, []
            else:
                buf.append(line)
        out[key] = "\n".join(buf)
        return out

    def test_swapping_the_crew_changes_only_the_crew_section(self):
        a = self.bodies('hub = "working-clothes"\nloop_crew = "working-clothes"\n')
        b = self.bodies('hub = "backstage-company"\n'
                        'loop_crew = "backstage-company"\n')
        for role in ("hub", "tracker"):
            sa, sb = self.split(a[role]), self.split(b[role])
            crew_a = [k for k in sa if k.startswith("### Crew")]
            crew_b = [k for k in sb if k.startswith("### Crew")]
            self.assertEqual(len(crew_a), 1)
            self.assertEqual(len(crew_b), 1)
            self.assertNotEqual(sa[crew_a[0]], sb[crew_b[0]])
            # every non-crew section is untouched
            for key in sa:
                if key.startswith("### Crew"):
                    continue
                self.assertIn(key, sb, f"{role}: section {key} vanished")
                self.assertEqual(sa[key], sb[key],
                                 f"{role}: crew swap changed section {key}")

    def test_hub_signature_is_selectable_independently(self):
        both = self.bodies('hub = "backstage-company"\n'
                           'loop_crew = "working-clothes"\n')
        self.assertIn("Backstage company", both["hub"])
        self.assertIn("Working clothes", both["tracker"])


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_missing_crew_file_is_an_error(self):
        p = build_pack(self.tmp, config='hub = "nope"\nloop_crew = "nope"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("crews/nope.md", str(cm.exception))

    def test_missing_selection_key_is_an_error(self):
        p = build_pack(self.tmp, config='hub = "working-clothes"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("loop_crew", str(cm.exception))

    def test_unknown_role_in_targets_is_an_error(self):
        p = build_pack(self.tmp, config=CONFIG +
                       '\n[targets]\nghost = "loops/ghost/CLAUDE.md"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("ghost", str(cm.exception))

    def test_unknown_role_flag_is_an_error(self):
        p = build_pack(self.tmp)
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--role", "ghost",
                    "--quiet"])
        self.assertIn("unknown role", str(cm.exception))


class TestInstall(unittest.TestCase):
    """--install swaps the generated block and leaves hand-written text alone."""

    HEAD = "# hub session\n\nOperational rule: never merge.\n\n"
    TAIL = "\n\n- Never set ANTHROPIC_API_KEY.\n"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.personas = build_pack(
            self.tmp, config=CONFIG + '\n[targets]\nhub = "hub/CLAUDE.md"\n')
        self.target = os.path.join(self.tmp, "hub", "CLAUDE.md")
        # The body now lives beside the target, imported by it. A sibling is
        # the only form `@` resolves — see sidecar_path in bin/persona-compile.
        self.sidecar = os.path.join(self.tmp, "hub", pc.SIDECAR)
        os.makedirs(os.path.dirname(self.target))
        write(self.target, self.HEAD + pc.BEGIN + " -->\n" + pc.END + self.TAIL)
        self.out = os.path.join(self.tmp, "compiled")

    def cli(self, *extra):
        return run("--repo", self.tmp, "--personas-dir", self.personas,
                   "--out", self.out, *extra)

    def test_install_then_check_is_clean(self):
        self.cli()
        rc, _ = self.cli("--install")
        self.assertEqual(rc, 0)
        text = slurp(self.target)
        # CLAUDE.md gets the POINTER; the body goes to the sidecar.
        self.assertIn("@" + pc.SIDECAR, text)
        self.assertNotIn("Move work.", text)
        self.assertIn("Move work.", slurp(self.sidecar))
        self.assertTrue(text.startswith(self.HEAD))
        self.assertTrue(text.endswith(self.TAIL))
        rc, _ = self.cli("--check")
        self.assertEqual(rc, 0)

    def test_claude_md_is_byte_stable_when_only_the_persona_changes(self):
        """The reason the body is a sidecar at all.

        A persona edit must not show up as a diff in an operational-contract
        file. Before this split, every recompile rewrote CLAUDE.md — which also
        made the file drift for anything hashing it, e.g. extraction sync."""
        self.cli()
        self.cli("--install")
        before = slurp(self.target)
        before_body = slurp(self.sidecar)
        with open(os.path.join(self.personas, "roles", "hub.md"),
                  "a", encoding="utf-8") as f:
            f.write("\nA brand new sentence in the role.\n")
        self.cli()
        self.cli("--install")
        self.assertNotEqual(before_body, slurp(self.sidecar),
                            "the sidecar must carry the change")
        self.assertIn("A brand new sentence in the role.", slurp(self.sidecar))
        # ...and CLAUDE.md must be untouched, sha stamp included: the stamp
        # tracks the *sources*, which is what --check compares.
        self.assertEqual(before, slurp(self.target))

    def test_a_tampered_sidecar_is_drift_even_when_the_pointer_is_current(self):
        """The pointer carries only a sha, so a stale body behind a current
        block is invisible unless the sidecar is compared on its own."""
        self.cli()
        self.cli("--install")
        rc, _ = self.cli("--check")
        self.assertEqual(rc, 0)
        write(self.sidecar, "hand-edited nonsense\n")
        rc, err = self.cli("--check")
        self.assertEqual(rc, 1)
        self.assertIn(pc.SIDECAR, err)
        self.assertIn("stale", err)
        self.cli("--install")
        rc, _ = self.cli("--check")
        self.assertEqual(rc, 0)

    def test_a_deleted_sidecar_reports_missing_not_stale(self):
        self.cli()
        self.cli("--install")
        os.remove(self.sidecar)
        rc, err = self.cli("--check")
        self.assertEqual(rc, 1)
        self.assertIn("missing", err)

    def test_the_import_is_a_bare_sibling_never_a_path(self):
        """`@` resolves only at or below the session's own directory: a
        parent-relative or absolute import silently loads nothing."""
        self.cli()
        self.cli("--install")
        text = slurp(self.target)
        self.assertIn("@" + pc.SIDECAR, text)
        self.assertNotIn("@..", text)
        self.assertNotIn("@/", text)
        self.assertEqual(os.path.dirname(self.sidecar),
                         os.path.dirname(self.target))

    def test_install_is_idempotent(self):
        self.cli()
        self.cli("--install")
        once = slurp(self.target)
        self.cli("--install")
        self.assertEqual(once, slurp(self.target))

    def test_source_edit_shows_as_drift_until_reinstalled(self):
        self.cli()
        self.cli("--install")
        with open(os.path.join(self.personas, "roles", "hub.md"), "a", encoding="utf-8") as f:
            f.write("\nAlso route work.\n")
        rc, err = self.cli("--check")
        self.assertEqual(rc, 1)
        # The drift is in the sidecar, NOT in CLAUDE.md — a persona source edit
        # no longer touches the operational-contract file at all.
        self.assertIn(pc.SIDECAR, err)
        self.assertNotIn("CLAUDE.md", err)
        self.cli()
        self.cli("--install")
        rc, _ = self.cli("--check")
        self.assertEqual(rc, 0)
        self.assertIn("Also route work.", slurp(self.sidecar))

    def test_missing_markers_report_drift_and_never_guess_placement(self):
        write(self.target, self.HEAD + self.TAIL)
        self.cli()
        rc, err = self.cli("--check")
        self.assertEqual(rc, 1)
        self.assertIn("no generated-persona markers", err)
        self.cli("--install")
        # install must not have invented a location for the block
        self.assertEqual(slurp(self.target), self.HEAD + self.TAIL)

    def test_hand_written_text_outside_the_markers_is_preserved(self):
        self.cli()
        self.cli("--install")
        text = slurp(self.target)
        self.assertIn("Operational rule: never merge.", text)
        self.assertIn("Never set ANTHROPIC_API_KEY.", text)


class TestRealPack(unittest.TestCase):
    """The shipped personas/ pack compiles, and every live target is wired."""

    def test_repo_pack_compiles_and_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, _ = run("--repo", REPO,
                        "--personas-dir", os.path.join(REPO, "personas"),
                        "--out", os.path.join(tmp, "compiled"))
            self.assertEqual(rc, 0)
        rc, err = run("--repo", REPO, "--check")
        self.assertEqual(rc, 0, f"personas/ is out of date — run "
                                f"bin/persona-compile --install\n{err}")

    def test_every_shipped_crew_compiles_for_every_role(self):
        personas = os.path.join(REPO, "personas")
        roles = pc.discover_roles(personas)
        crews = os.path.join(personas, "crews")
        for filename in sorted(os.listdir(crews)):
            if not filename.endswith(".md"):
                continue
            crew = filename[:-3]
            cfg = {"hub": crew, "loop_crew": crew}
            for role in roles:
                with self.subTest(crew=crew, role=role):
                    record = pc.compile_role(role, personas, cfg)
                    self.assertEqual(record["crew"], crew)
                    self.assertIn(f"crews/{crew}.md@", record["sources"])

if __name__ == "__main__":
    unittest.main(verbosity=2)

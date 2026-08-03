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
# Crew fixtures carry `##` subsections because every crew this repo ships does.
# A heading-free fixture cannot see a heading-depth regression, and it made
# TestCrewSwapIsolation's stated invariant vacuous.
CREW_A = ("# Working clothes\n\nLiteral facts, light framing.\n\n"
          "## Register\n\nPlain and unadorned.\n\n"
          "## Limits\n\nCannot override the operational contract.\n")
CREW_B = ("# Backstage company\n\nWarm ensemble contrast.\n\n"
          "## Register\n\nCollegial and warm.\n\n"
          "## Limits\n\nCannot override the operational contract.\n")

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
        """Split a compiled body into {section heading: text}.

        The crew section is everything from `### Crew — …` to the end. Crew
        bodies are demoted one level (the role layer is the only one demoted
        two), so a crew's own `##` subsections land at `###` — siblings of
        `### Crew` rather than children. That flattening matches the compiler
        this was extracted from and is deliberately not changed here; what the
        split must not do is mistake those subsections for top-level sections
        of the persona, which would make the isolation assertion below vacuous.
        """
        out, key, buf = {}, "(preamble)", []
        in_crew = False
        for line in body.splitlines():
            if line.startswith("### Crew — "):
                out[key] = "\n".join(buf)
                key, buf, in_crew = line, [], True
            elif line.startswith("### ") and not in_crew:
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

    def test_an_empty_roles_directory_exits_with_the_documented_message(self):
        """Not a traceback. The PR promised 'the compiler exits if absent'."""
        p = build_pack(self.tmp)
        for f in os.listdir(os.path.join(p, "roles")):
            os.remove(os.path.join(p, "roles", f))
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("at least one", str(cm.exception))

    def test_conflicting_modes_are_rejected_not_silently_resolved(self):
        p = build_pack(self.tmp)
        for combo in (["--print", "hub", "--check"],
                      ["--print", "hub", "--install"],
                      ["--check", "--install"]):
            with self.subTest(combo=combo):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        pc.run(["--repo", self.tmp, "--personas-dir", p,
                                "--out", os.path.join(self.tmp, "c"),
                                "--quiet", *combo])
                self.assertEqual(cm.exception.code, 2)

    def test_a_non_string_crew_selection_says_what_went_wrong(self):
        """[hub] as a TOML table is the natural mistake — loops.toml uses one."""
        p = build_pack(self.tmp, config='[hub]\ncrew = "working-clothes"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("must be a crew name", str(cm.exception))

    def test_the_config_error_names_the_config_actually_in_use(self):
        p = build_pack(self.tmp)
        other = os.path.join(self.tmp, "elsewhere.toml")
        write(other, 'hub = "working-clothes"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p, "--config", other,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("elsewhere.toml", str(cm.exception))

    def test_unknown_role_flag_is_an_error(self):
        p = build_pack(self.tmp)
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--role", "ghost",
                    "--quiet"])
        self.assertIn("unknown role", str(cm.exception))


class TestMarkers(unittest.TestCase):
    """Marker parsing refuses ambiguity instead of guessing a span.

    First-match `find` on each marker independently was wrong in two silent
    directions: a stray BEGIN ahead of the real block made the replacement
    swallow the hand-written lines between them, and a second complete pair
    stayed live in the prompt while a check that compared only the first
    reported clean.
    """

    REC = {"role": "demo", "display": "Demo", "crew": "c", "body": "B",
           "sha": "abc123", "sources": "s"}

    def block(self):
        return pc.block(self.REC)

    def test_a_stray_begin_marker_never_swallows_hand_written_text(self):
        text = ("HEAD\n" + pc.BEGIN + " · role=demo -->\n"
                "IMPORTANT OPERATIONAL RULE\n"
                + pc.BEGIN + " · role=demo -->\nbody\n" + pc.END + "\nTAIL\n")
        with self.assertRaises(ValueError) as cm:
            pc.replace_block(text, self.block())
        self.assertIn("exactly one", str(cm.exception))
        # ...and the hand-written line is still there, because nothing was written.
        self.assertIn("IMPORTANT OPERATIONAL RULE", text)

    def test_a_second_complete_block_is_not_silently_left_stale(self):
        one = pc.BEGIN + " · role=demo -->\nOLD\n" + pc.END
        text = f"HEAD\n{one}\nmiddle\n{one}\nTAIL\n"
        with self.assertRaises(ValueError):
            pc.current_block(text)

    def test_end_before_begin_is_reported_not_replaced(self):
        text = "HEAD\n" + pc.END + "\n" + pc.BEGIN + " -->\nTAIL\n"
        with self.assertRaises(ValueError) as cm:
            pc.current_block(text)
        self.assertIn("precedes", str(cm.exception))

    def test_a_file_with_no_markers_is_not_an_error(self):
        self.assertIsNone(pc.current_block("nothing here\n"))


class TestPathSafety(unittest.TestCase):
    """[targets] values cannot reach outside the repo, or collide."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def build(self, targets):
        return build_pack(self.tmp, config=CONFIG + "\n[targets]\n" + targets)

    def test_an_absolute_target_is_rejected(self):
        p = self.build('hub = "/etc/CLAUDE.md"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("outside the repo", str(cm.exception))

    def test_a_parent_relative_target_is_rejected(self):
        p = self.build('hub = "../../elsewhere/CLAUDE.md"\n')
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p,
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("outside the repo", str(cm.exception))

    def test_two_targets_in_one_directory_are_rejected_before_any_write(self):
        """The sidecar name is fixed, so this config can never check clean."""
        p = self.build('hub = "d/AGENTS.md"\ntracker = "d/OTHER.md"\n')
        os.makedirs(os.path.join(self.tmp, "d"))
        with self.assertRaises(SystemExit) as cm:
            pc.run(["--repo", self.tmp, "--personas-dir", p, "--install",
                    "--out", os.path.join(self.tmp, "c"), "--quiet"])
        self.assertIn("own directory", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "d", pc.SIDECAR)))


class TestStripTitle(unittest.TestCase):
    """Heading demotion is markdown-aware; HTML comments never reach a prompt."""

    def test_headings_inside_a_fence_are_left_alone(self):
        _, body = pc.strip_title("# T\n\n```bash\n# a shell comment\n```\n")
        self.assertIn("# a shell comment", body)
        self.assertNotIn("## a shell comment", body)

    def test_demotion_clamps_at_h6(self):
        _, body = pc.strip_title("# T\n\n##### deep\n", demote=2)
        self.assertIn("###### deep", body)
        self.assertNotIn("####### deep", body)

    def test_html_comments_are_stripped(self):
        _, body = pc.strip_title(
            "# T\n\n<!-- note to the author, not the session -->\n\nReal prose.\n")
        self.assertNotIn("note to the author", body)
        self.assertIn("Real prose.", body)

    def test_the_example_role_ships_no_authoring_instructions(self):
        """The one target a fresh clone installs must not wear its own README."""
        cfg = {"hub": "working-clothes", "loop_crew": "working-clothes"}
        rec = pc.compile_role("example", os.path.join(REPO, "personas"), cfg)
        self.assertNotIn("Copy this file to", rec["body"])
        self.assertNotIn("The compiler expects them at this depth", rec["body"])
        self.assertIn("## Persona — The example role", rec["body"])


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

    def test_bytes_outside_the_markers_survive_including_line_endings(self):
        """Substring presence is not byte preservation.

        Reading in text mode normalises CRLF on the way in, so writing the
        whole string back rewrote every line ending in the file — outside the
        managed region as well as in it.
        """
        with open(self.target, "wb") as f:
            f.write(("HEAD ONE\r\nHEAD TWO\r\n" + pc.BEGIN + " -->\r\n"
                     + pc.END + "\r\nTAIL ONE\r\n").encode("utf-8"))
        self.cli()
        self.cli("--install")
        with open(self.target, "rb") as f:
            raw = f.read()
        self.assertIn(b"HEAD ONE\r\nHEAD TWO\r\n", raw)
        self.assertIn(b"TAIL ONE\r\n", raw)

    def test_a_deconfigured_target_is_drift_not_silence(self):
        """A role dropped from [targets] leaves a live persona nothing selects."""
        self.cli()
        self.cli("--install")
        rc, _ = self.cli("--check")
        self.assertEqual(rc, 0)
        write(os.path.join(self.personas, "config.toml"), CONFIG)  # no [targets]
        rc, err = self.cli("--check")
        self.assertEqual(rc, 1)
        self.assertIn("still installs role 'hub'", err)

    def test_an_orphaned_compiled_file_is_drift(self):
        self.cli()
        write(os.path.join(self.out, "ghost.md"), "left behind\n")
        rc, err = self.cli("--check")
        self.assertEqual(rc, 1)
        self.assertIn("orphaned", err)

    def test_a_failed_write_leaves_the_contract_file_intact(self):
        """The reason writes go through a temp file and a rename.

        A plain open(path, "w") truncates first, so an interrupt or a full disk
        leaves an operational-contract file empty. Here the write fails partway
        and the original must still be on disk, whole.
        """
        self.cli()
        self.cli("--install")
        before = slurp(self.target)

        class Boom(Exception):
            pass

        class HalfBroken(str):
            """Writes some bytes, then fails — a full disk, in miniature."""

        real_fdopen = os.fdopen

        def exploding_fdopen(fd, *a, **kw):
            f = real_fdopen(fd, *a, **kw)
            orig = f.write

            def write(text):
                orig(text[:20])
                raise Boom("disk full")
            f.write = write
            return f

        os.fdopen = exploding_fdopen
        try:
            with self.assertRaises(Boom):
                pc.write_atomic(self.target, "REPLACEMENT CONTENT" * 10)
        finally:
            os.fdopen = real_fdopen

        self.assertEqual(before, slurp(self.target))
        leftovers = [f for f in os.listdir(os.path.dirname(self.target))
                     if f.startswith(".persona-compile.")]
        self.assertEqual(leftovers, [], "temp file left behind")

    def test_an_unchanged_sidecar_is_not_rewritten(self):
        self.cli()
        self.cli("--install")
        before = os.stat(self.sidecar).st_mtime_ns
        os.utime(self.sidecar, ns=(before - 10**9, before - 10**9))
        stamped = os.stat(self.sidecar).st_mtime_ns
        self.cli("--install")
        self.assertEqual(stamped, os.stat(self.sidecar).st_mtime_ns)


class TestRealPack(unittest.TestCase):
    """The shipped personas/ pack compiles from a clean checkout.

    Deliberately driven through the COMMITTED config.example.toml into a
    tempdir. personas/config.toml, personas/compiled/ and the persona.md
    sidecars are all gitignored, so anything that reads them tests the author's
    working directory rather than the branch — and asserting that *this*
    installation's targets are wired would be carrying installation policy in
    the core, which is exactly what the core is not supposed to do.
    """

    def test_repo_pack_compiles_from_the_committed_sources_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, err = run("--repo", REPO,
                          "--personas-dir", os.path.join(REPO, "personas"),
                          "--config", os.path.join(REPO, "personas",
                                                   "config.example.toml"),
                          "--out", os.path.join(tmp, "compiled"))
            self.assertEqual(rc, 0, err)
            for role in pc.discover_roles(os.path.join(REPO, "personas")):
                self.assertTrue(
                    os.path.isfile(os.path.join(tmp, "compiled", f"{role}.md")))
            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "compiled", "MANIFEST.md")))

    def test_shipped_crews_name_no_roles(self):
        """The public core carries mechanism, not one estate's org chart.

        A crew is a register and applies identically to every role wearing it,
        so a `## <Role>` overlay in a shipped crew is both an identity leak and
        an instruction most sessions cannot satisfy.
        """
        crews = os.path.join(REPO, "personas", "crews")
        for filename in sorted(os.listdir(crews)):
            if not filename.endswith(".md"):
                continue
            with self.subTest(crew=filename):
                body = slurp(os.path.join(crews, filename))
                headings = [l for l in body.splitlines() if l.startswith("## ")]
                self.assertEqual(headings, [], f"{filename} carries per-role "
                                               f"overlays: {headings}")
                self.assertNotIn("Apply only the overlay", body)

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

#!/usr/bin/env python3
"""Tests for the store resolver (t-393, gap audit Q-10).

Run: python3 tests/test_estate_store.py

Two layers. The unit tests build git layouts by hand — a `.git` file, a
`commondir` — because that is the on-disk contract lib/estate_store.py actually
reads, and hand-building it is the only way to cover the shapes git writes in
situations this repo does not happen to be in (relative links, submodules, a bare
main repo). The CLI tests then run a REAL `bin/estate` inside a REAL `git
worktree`, which is what proves the hand-built contract matches git.

Nothing here touches the live store. Every CLI run gets a copied checkout under a
tempdir, and the one test that deliberately runs with no $ESTATE_STATE_DIR at all
asserts the path it resolved is inside that tempdir before it does anything else.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))

import estate_store  # noqa: E402


def followups_is_estate_backed() -> bool:
    """True when bin/followups is the shim that resolves THIS store.

    It is a separate component with its own extraction history. Where it is
    still the standalone JSON store there is no resolver seam to refuse over,
    so the shim test below would report a missing re-sync as a resolver bug.
    Checking the file rather than a version string means the test resumes by
    itself once the shim lands.
    """
    try:
        with open(os.path.join(REPO, "bin", "followups"), encoding="utf-8") as f:
            return "ESTATE_STATE_DIR" in f.read()
    except OSError:
        return False


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class ResolverCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def checkout(self, name):
        """A plain checkout: `.git` is a directory."""
        root = os.path.join(self.dir, name)
        os.makedirs(os.path.join(root, ".git"), exist_ok=True)
        os.makedirs(os.path.join(root, "bin"), exist_ok=True)
        return root

    def script(self, root):
        return os.path.join(root, "bin", "estate")

    def resolve(self, root, env=None):
        return estate_store.resolve(self.script(root), env or {})


class TestOverride(ResolverCase):
    def test_the_named_store_wins_and_may_be_created(self):
        root = self.checkout("main")
        named = os.path.join(self.dir, "throwaway")
        found = self.resolve(root, {"ESTATE_STATE_DIR": named})
        self.assertEqual(found.state_dir, named)
        self.assertEqual(found.db_path, os.path.join(named, "estate.db"))
        self.assertEqual(found.source, estate_store.OVERRIDE)
        self.assertTrue(found.may_create())

    def test_a_relative_override_is_made_absolute(self):
        root = self.checkout("main")
        found = self.resolve(root, {"ESTATE_STATE_DIR": "elsewhere"})
        self.assertTrue(os.path.isabs(found.state_dir))

    def test_an_empty_override_is_not_an_override(self):
        # An unset variable and a variable set to "" are the same intention.
        root = self.checkout("main")
        found = self.resolve(root, {"ESTATE_STATE_DIR": "  "})
        self.assertEqual(found.source, estate_store.CHECKOUT)
        self.assertFalse(found.may_create())


class TestDerived(ResolverCase):
    def test_a_plain_checkout_keeps_its_own_state_dir(self):
        root = self.checkout("main")
        found = self.resolve(root)
        self.assertEqual(found.state_dir, os.path.join(root, "state"))
        self.assertEqual(found.source, estate_store.CHECKOUT)

    def test_a_derived_path_may_never_create(self):
        found = self.resolve(self.checkout("main"))
        self.assertFalse(found.may_create())

    def test_a_tree_with_no_repository_keeps_its_own_state_dir(self):
        # An rsynced copy (docs/new-machine-setup.md) has no .git at all.
        root = os.path.join(self.dir, "copied")
        os.makedirs(os.path.join(root, "bin"))
        found = self.resolve(root)
        self.assertEqual(found.state_dir, os.path.join(root, "state"))
        self.assertEqual(found.source, estate_store.CHECKOUT)


class TestWorktrees(ResolverCase):
    """The defect itself: a linked worktree used to resolve its own state/."""

    def linked(self, name, main, relative=False, commondir=True):
        root = os.path.join(self.dir, name)
        os.makedirs(os.path.join(root, "bin"), exist_ok=True)
        gitdir = os.path.join(main, ".git", "worktrees", name)
        os.makedirs(gitdir, exist_ok=True)
        if commondir:
            write(os.path.join(gitdir, "commondir"), "../..\n")
        link = os.path.relpath(gitdir, root) if relative else gitdir
        write(os.path.join(root, ".git"), f"gitdir: {link}\n")
        return root

    def test_a_linked_worktree_resolves_to_the_main_checkout(self):
        main = self.checkout("main")
        root = self.linked("wt", main)
        found = self.resolve(root)
        self.assertEqual(found.state_dir, os.path.join(main, "state"))
        self.assertEqual(found.source, estate_store.MAIN_WORKTREE)
        self.assertEqual(found.repo_root, main)
        self.assertEqual(found.script_root, root)

    def test_a_relative_gitdir_link_resolves_the_same_way(self):
        # `git worktree add --relative-paths` writes the link relative.
        main = self.checkout("main")
        root = self.linked("wt", main, relative=True)
        self.assertEqual(self.resolve(root).state_dir,
                         os.path.join(main, "state"))

    def test_the_worktrees_layout_carries_it_without_a_commondir_file(self):
        main = self.checkout("main")
        root = self.linked("wt", main, commondir=False)
        self.assertEqual(self.resolve(root).state_dir,
                         os.path.join(main, "state"))

    def test_a_submodule_keeps_its_own_state_dir(self):
        # `.git/modules/<name>` is not a worktree of the superproject, and the
        # superproject's store is not its store.
        main = self.checkout("main")
        root = os.path.join(self.dir, "sub")
        os.makedirs(os.path.join(root, "bin"))
        gitdir = os.path.join(main, ".git", "modules", "sub")
        os.makedirs(gitdir)
        write(os.path.join(root, ".git"), f"gitdir: {gitdir}\n")
        found = self.resolve(root)
        self.assertEqual(found.state_dir, os.path.join(root, "state"))
        self.assertEqual(found.source, estate_store.CHECKOUT)

    def test_a_bare_main_repository_has_no_state_dir_to_offer(self):
        bare = os.path.join(self.dir, "repo.git")
        gitdir = os.path.join(bare, "worktrees", "wt")
        os.makedirs(gitdir)
        write(os.path.join(gitdir, "commondir"), "../..\n")
        root = os.path.join(self.dir, "wt")
        os.makedirs(os.path.join(root, "bin"))
        write(os.path.join(root, ".git"), f"gitdir: {gitdir}\n")
        found = self.resolve(root)
        self.assertEqual(found.state_dir, os.path.join(root, "state"))

    def test_an_unreadable_git_file_falls_back_to_this_checkout(self):
        root = os.path.join(self.dir, "odd")
        os.makedirs(os.path.join(root, "bin"))
        write(os.path.join(root, ".git"), "this is not a gitdir line\n")
        found = self.resolve(root)
        self.assertEqual(found.state_dir, os.path.join(root, "state"))
        self.assertFalse(found.may_create())


class TestRefusal(ResolverCase):
    def test_require_passes_when_the_store_is_there(self):
        root = self.checkout("main")
        os.makedirs(os.path.join(root, "state"))
        write(os.path.join(root, "state", "estate.db"), "")
        estate_store.require(self.resolve(root))  # does not raise

    def test_require_passes_on_a_named_store_that_does_not_exist_yet(self):
        root = self.checkout("main")
        named = os.path.join(self.dir, "throwaway")
        estate_store.require(self.resolve(root, {"ESTATE_STATE_DIR": named}))

    def test_require_refuses_a_derived_path_and_names_the_file(self):
        root = self.checkout("main")
        found = self.resolve(root)
        with self.assertRaises(estate_store.StoreMissing) as caught:
            estate_store.require(found)
        message = str(caught.exception)
        self.assertIn(os.path.join(root, "state", "estate.db"), message)
        self.assertIn("estate init", message)
        self.assertIn("ESTATE_STATE_DIR", message)
        self.assertIs(caught.exception.resolution, found)

    def test_the_refusal_from_a_worktree_names_the_main_checkout(self):
        main = self.checkout("main")
        root = os.path.join(self.dir, "wt")
        os.makedirs(os.path.join(root, "bin"))
        gitdir = os.path.join(main, ".git", "worktrees", "wt")
        os.makedirs(gitdir)
        write(os.path.join(gitdir, "commondir"), "../..\n")
        write(os.path.join(root, ".git"), f"gitdir: {gitdir}\n")
        message = self.resolve(root).missing_message()
        self.assertIn(os.path.join(main, "state", "estate.db"), message)
        self.assertIn(root, message)  # and where the run actually was


def git(args, cwd, env):
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


def have_git():
    try:
        return subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


class CliCase(unittest.TestCase):
    """End-to-end against a copied checkout, never the live one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "checkout")
        os.makedirs(self.root)
        shutil.copytree(os.path.join(REPO, "lib"), os.path.join(self.root, "lib"))
        os.makedirs(os.path.join(self.root, "bin"))
        shutil.copy2(os.path.join(REPO, "bin", "estate"),
                     os.path.join(self.root, "bin", "estate"))
        shutil.copy2(os.path.join(REPO, "bin", "followups"),
                     os.path.join(self.root, "bin", "followups"))
        self.env = dict(os.environ)
        for key in ("ESTATE_STATE_DIR", "FOLLOWUPS_STATE_DIR", "GIT_DIR",
                    "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(key, None)
        self.env["ESTATE_LESSONS_PATH"] = os.path.join(self.tmp.name, "lessons.md")

    def tearDown(self):
        self.tmp.cleanup()

    def estate(self, *args, root=None, env=None):
        root = root or self.root
        return subprocess.run(
            [sys.executable, os.path.join(root, "bin", "estate"), *args],
            capture_output=True, text=True, env=env or self.env)

    def dbs_under(self, path):
        found = []
        for base, _dirs, files in os.walk(path):
            found += [os.path.join(base, name) for name in files
                      if name.startswith("estate.db")]
        return sorted(found)


class TestCheckoutWithNoStore(CliCase):
    def test_it_refuses_and_names_the_store_it_expected(self):
        result = self.estate("task", "add", "a task", "--kind", "generic")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(os.path.join(self.root, "state", "estate.db"), result.stderr)
        self.assertIn("Refusing to create", result.stderr)

    def test_the_refused_run_creates_nothing_at_all(self):
        self.estate("task", "add", "a task", "--kind", "generic")
        self.estate("ready")
        self.estate("events")
        self.assertFalse(os.path.exists(os.path.join(self.root, "state")))
        self.assertEqual(self.dbs_under(self.root), [])

    def test_the_reader_commands_refuse_too(self):
        # A read against a store that is not there is the same wrong answer as a
        # write, and it used to return a confident empty list.
        for args in (("ready",), ("task", "list"), ("attention",),
                     ("events", "--limit", "5"), ("json",)):
            result = self.estate(*args)
            self.assertEqual(result.returncode, 3, f"{args}: {result.stdout}")

    def test_store_reports_instead_of_refusing(self):
        result = self.estate("store", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(found["db_path"],
                         os.path.join(self.root, "state", "estate.db"))
        self.assertEqual(found["source"], "checkout")
        self.assertFalse(found["exists"])
        self.assertFalse(found["may_create"])

    def test_init_is_the_one_command_that_creates_it(self):
        result = self.estate("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(os.path.join(self.root, "state", "estate.db"), result.stdout)
        after = self.estate("task", "add", "a task", "--kind", "generic")
        self.assertEqual(after.returncode, 0, after.stderr)

    def test_backup_names_the_store_rather_than_backing_up_nothing(self):
        result = self.estate("backup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(os.path.join(self.root, "state", "estate.db"), result.stderr)
        self.assertEqual(self.dbs_under(self.root), [])

    @unittest.skipUnless(followups_is_estate_backed(),
                         "bin/followups here is the standalone JSON store, not "
                         "the shim over this store")
    def test_the_followups_shim_refuses_the_same_way(self):
        result = subprocess.run(
            [sys.executable, os.path.join(self.root, "bin", "followups"), "list"],
            capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(os.path.join(self.root, "state", "estate.db"), result.stderr)
        self.assertEqual(self.dbs_under(self.root), [])


class TestNamedStoreStillWorks(CliCase):
    """The throwaway store the whole test suite depends on."""

    def test_an_explicit_store_is_created_on_first_use(self):
        named = os.path.join(self.tmp.name, "throwaway")
        env = dict(self.env, ESTATE_STATE_DIR=named)
        result = self.estate("task", "add", "a task", "--kind", "generic", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(os.path.join(named, "estate.db")))
        # and nothing appeared beside the checkout
        self.assertEqual(self.dbs_under(self.root), [])

    def test_the_override_wins_over_the_canonical_store(self):
        self.estate("init")
        named = os.path.join(self.tmp.name, "throwaway")
        env = dict(self.env, ESTATE_STATE_DIR=named)
        result = self.estate("store", "--path", env=env)
        self.assertEqual(result.stdout.strip(),
                         os.path.join(named, "estate.db"))


@unittest.skipUnless(have_git(), "git is not available")
class TestRealWorktree(CliCase):
    """A real `git worktree`, because the layout is the contract."""

    def setUp(self):
        super().setUp()
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                        HOME=self.tmp.name)
        self.assertEqual(git(["init", "-q", "-b", "main"], self.root, self.env).returncode, 0)
        git(["add", "bin", "lib"], self.root, self.env)
        git(["commit", "-q", "-m", "checkout"], self.root, self.env)
        self.worktree = os.path.join(self.tmp.name, "wt")
        added = git(["worktree", "add", "-q", "-b", "side", self.worktree],
                    self.root, self.env)
        self.assertEqual(added.returncode, 0, added.stderr)

    def test_a_worktree_run_uses_the_main_checkouts_store(self):
        self.assertEqual(self.estate("init").returncode, 0)
        result = self.estate("store", "--json", root=self.worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual(found["source"], "main-worktree")
        self.assertEqual(found["db_path"],
                         os.path.join(self.root, "state", "estate.db"))

    def test_work_done_in_a_worktree_lands_in_the_one_store(self):
        self.assertEqual(self.estate("init").returncode, 0)
        added = self.estate("task", "add", "from the worktree", "--kind",
                            "generic", root=self.worktree)
        self.assertEqual(added.returncode, 0, added.stderr)
        listed = self.estate("task", "list")  # asked from the MAIN checkout
        self.assertIn("from the worktree", listed.stdout)
        self.assertEqual(self.dbs_under(self.worktree), [],
                         "a worktree run minted a shadow store")

    def test_a_worktree_run_with_no_canonical_store_refuses(self):
        # Nobody ran `estate init` anywhere. The old code would have created
        # <worktree>/state/estate.db here and reported success.
        result = self.estate("task", "add", "a task", "--kind", "generic",
                             root=self.worktree)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(os.path.join(self.root, "state", "estate.db"), result.stderr)
        self.assertEqual(self.dbs_under(self.worktree), [])
        self.assertEqual(self.dbs_under(self.root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

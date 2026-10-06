"""Integration checks for ordered Today reads and standing-session identity."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import actors
import today_list


class ReadModels(unittest.TestCase):
    def test_standing_and_ephemeral_sessions_keep_distinct_identity(self):
        standing = actors.normalize("steward-project-alpha")
        worker = actors.normalize("ephemeral-project-alpha")
        self.assertEqual(standing.actor, "steward")
        self.assertEqual(standing.session_id, "project-alpha")
        self.assertEqual(standing.short, "steward:project-alpha")
        self.assertEqual(worker.short, "eph:project-alpha")
        explicit = actors.normalize("steward-other", session_id="project-beta")
        self.assertEqual(explicit.session_id, "project-beta")
        self.assertNotEqual(standing.actor_class, worker.actor_class)

    def test_today_order_pins_and_overdue_come_from_the_real_store(self):
        with tempfile.TemporaryDirectory() as state:
            for title, kind, refs in (("second", "today", {"rank": 2, "pinned": True}),
                                      ("first", "today", {"rank": 1}),
                                      ("unrelated", "generic", {})):
                result = subprocess.run([sys.executable, str(ROOT / "bin/estate"),
                    "task", "add", title, "--kind", kind, "--refs", json.dumps(refs)],
                    env=dict(os.environ, ESTATE_STATE_DIR=state),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            with contextlib.closing(sqlite3.connect(Path(state) / "estate.db")) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("UPDATE tasks SET due_at=? WHERE title='first'",
                             ("2020-01-01T00:00:00Z",))
                conn.commit()
                before = conn.total_changes
                rows = today_list.items(conn, dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc))
                self.assertEqual([r["text"] for r in rows], ["first", "second"])
                self.assertEqual([r["rank"] for r in rows], [1, 2])
                self.assertTrue(rows[0]["overdue"])
                self.assertTrue(rows[1]["pinned"])
                self.assertEqual(today_list.oldest_unpinned(rows)["text"], "first")
                self.assertEqual(conn.total_changes, before)


if __name__ == "__main__":
    unittest.main()

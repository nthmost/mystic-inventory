"""Stdlib-only smoke tests: DB roundtrip + search. Run: python -m unittest -v"""

import os
import tempfile
import unittest
from pathlib import Path

from crate import db
from crate.models import Track


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(Path(self.tmp.name))

    def tearDown(self):
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + ext)
            except OSError:
                pass

    def _track(self, **kw):
        base = dict(host="testhost", path="/music/a.mp3", ext=".mp3")
        base.update(kw)
        return Track(**base)

    def test_upsert_and_search(self):
        db.upsert(self.conn, self._track(
            path="/music/mom.flac", artist="Mouse on Mars", title="Actionist Respoke"))
        db.upsert(self.conn, self._track(
            path="/music/boc.flac", artist="Boards of Canada", title="Roygbiv"))
        self.conn.commit()

        hits = db.search(self.conn, "mouse on mars")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].artist, "Mouse on Mars")

        # multi-term AND across artist + title
        self.assertEqual(len(db.search(self.conn, "boards roygbiv")), 1)
        self.assertEqual(len(db.search(self.conn, "nonexistent")), 0)

    def test_upsert_is_idempotent_by_host_path(self):
        t = self._track(path="/music/x.mp3", artist="A", title="one")
        db.upsert(self.conn, t)
        db.upsert(self.conn, self._track(path="/music/x.mp3", artist="A", title="renamed"))
        self.conn.commit()
        rows = self.conn.execute("SELECT COUNT(*) n FROM files").fetchone()
        self.assertEqual(rows["n"], 1)
        self.assertEqual(db.search(self.conn, "renamed")[0].title, "renamed")

    def test_untagged_detection(self):
        db.upsert(self.conn, self._track(path="/music/mystery.mp3"))
        db.upsert(self.conn, self._track(path="/music/known.mp3", artist="A", title="t"))
        self.conn.commit()
        u = db.untagged(self.conn)
        self.assertEqual(len(u), 1)
        self.assertTrue(u[0].is_untagged)


if __name__ == "__main__":
    unittest.main()

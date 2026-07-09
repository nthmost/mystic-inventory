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

    def test_search_albums_groups_by_album(self):
        for i in range(1, 4):
            db.upsert(self.conn, self._track(
                path=f"/m/d{i}.flac", artist="Deltron 3030",
                album="Deltron 3030", title=f"t{i}", track_no=i, duration=100))
        db.upsert(self.conn, self._track(
            path="/m/e1.flac", artist="Deltron 3030", album="Event 2",
            title="one", duration=200))
        self.conn.commit()
        albums = db.search_albums(self.conn, "deltron")
        by = {a["album"]: a for a in albums}
        self.assertEqual(set(by), {"Deltron 3030", "Event 2"})
        self.assertEqual(by["Deltron 3030"]["tracks"], 3)
        self.assertEqual(by["Deltron 3030"]["duration"], 300)
        tr = db.album_tracks(self.conn, "Deltron 3030", "Deltron 3030")
        self.assertEqual([t.track_no for t in tr], [1, 2, 3])

    def test_accent_insensitive_search(self):
        db.upsert(self.conn, self._track(
            path="/m/d.flac", artist="Huartan", album="Huartan", title="Dúlamán"))
        db.upsert(self.conn, self._track(
            path="/m/j.flac", artist="José González", title="Heartbeats"))
        self.conn.commit()
        self.assertEqual(len(db.search(self.conn, "dulaman")), 1)      # unaccented query
        self.assertEqual(len(db.search(self.conn, "Dúlamán")), 1)      # accented query
        self.assertEqual(db.search(self.conn, "jose gonzalez")[0].artist, "José González")
        self.assertEqual(db.fold("Mötörhead"), "motorhead")
        self.assertEqual(db.fold("Bjørk"), "bjork")

    def test_top_artists_sort_orders(self):
        db.upsert(self.conn, self._track(path="/m/a1", artist="Aaa", title="x"))
        db.upsert(self.conn, self._track(path="/m/z1", artist="Zzz", title="x"))
        db.upsert(self.conn, self._track(path="/m/z2", artist="Zzz", title="y"))
        self.conn.commit()
        self.assertEqual(db.top_artists(self.conn, sort="az")[0]["artist"], "Aaa")
        self.assertEqual(db.top_artists(self.conn, sort="za")[0]["artist"], "Zzz")
        most = db.top_artists(self.conn, sort="count_desc")[0]
        self.assertEqual((most["artist"], most["n"]), ("Zzz", 2))
        self.assertEqual(db.top_artists(self.conn, sort="count_asc")[0]["n"], 1)

    def test_merge_combines_hosts(self):
        # styx-side index (self.conn) has one styx track.
        db.upsert(self.conn, self._track(
            host="styx", path="/music/a.flac", artist="Mouse on Mars", title="Yippie"))
        self.conn.commit()

        # A separate beyla-side index with its own tracks + one overlapping path.
        other = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        other.close()
        oconn = db.connect(Path(other.name))
        db.upsert(oconn, self._track(
            host="beyla", path="/media/x.mp3", artist="Portishead", title="Roads"))
        db.upsert(oconn, self._track(
            host="styx", path="/music/a.flac", artist="Mouse on Mars", title="Yippie (remaster)"))
        oconn.commit()
        oconn.close()

        r = db.merge(self.conn, other.name)
        self.assertEqual(r["total_source"], 2)
        self.assertEqual(r["added"], 1)      # the beyla row is new
        self.assertEqual(r["updated"], 1)    # the styx row already existed

        # Both hosts now queryable from one index; overlap was refreshed.
        self.assertEqual(len(db.search(self.conn, "portishead")), 1)
        self.assertEqual(db.search(self.conn, "mouse on mars")[0].title, "Yippie (remaster)")
        hosts = {t.host for t in db.search(self.conn, "", limit=50)}
        self.assertEqual(hosts, {"styx", "beyla"})

        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(other.name + ext)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()

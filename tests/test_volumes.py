"""Volume identity, offline inventory, and backup coverage. Stdlib only."""

import os
import tempfile
import unittest
from pathlib import Path

from crate import db, volumes
from crate.models import Track


def _vol_track(vol_id, relpath, label, **kw):
    base = dict(
        host="styx", path=f"/Volumes/{label}/{relpath}", volume=f"/Volumes/{label}",
        vol_id=vol_id, relpath=relpath, vol_label=label,
        ext=".flac", content_hash=None,
    )
    base.update(kw)
    return Track(**base)


class TestVolumeIndex(unittest.TestCase):
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

    def test_volume_files_key_on_vol_id_relpath_not_host_path(self):
        # Same drive file seen on two different hosts/mounts = ONE row.
        db.upsert(self.conn, _vol_track("VID", "a/x.flac", "Big",
                                        host="styx", path="/Volumes/Big/a/x.flac",
                                        artist="A", title="one"))
        db.upsert(self.conn, _vol_track("VID", "a/x.flac", "Big",
                                        host="beyla", path="/media/nthmost/Big/a/x.flac",
                                        artist="A", title="one (better tag)"))
        self.conn.commit()
        rows = self.conn.execute("SELECT * FROM files WHERE vol_id='VID'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "one (better tag)")
        # host/path updated to the most recent sighting
        self.assertEqual(rows[0]["host"], "beyla")

    def test_offline_inventory_persists(self):
        db.upsert(self.conn, _vol_track("VID", "x.flac", "Big", artist="Mouse on Mars", title="Juju"))
        self.conn.commit()
        # A query resolves purely from the index — no drive access involved.
        hits = db.search(self.conn, "mouse on mars")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].location, "Big")   # shows drive label, not host

    def test_coverage_and_at_risk(self):
        # Same content (hash H1) on a host AND a drive -> protected.
        db.upsert(self.conn, Track(host="styx", path="/m/song.flac", ext=".flac",
                                   content_hash="H1", artist="X", title="dup"))
        db.upsert(self.conn, _vol_track("VID", "song.flac", "Big",
                                        content_hash="H1", artist="X", title="dup"))
        # Content only on the drive (hash H2) -> at risk.
        db.upsert(self.conn, _vol_track("VID", "only.flac", "Big",
                                        content_hash="H2", artist="Y", title="lonely"))
        self.conn.commit()

        cov = db.coverage(self.conn)
        self.assertEqual(cov["distinct_content"], 2)
        self.assertEqual(cov["protected"], 1)
        self.assertEqual(cov["at_risk"], 1)

        risky = db.at_risk(self.conn)
        self.assertEqual(len(risky), 1)
        self.assertEqual(risky[0].title, "lonely")

    def test_volume_record_roundtrip(self):
        from crate.models import Volume
        db.upsert_volume(self.conn, Volume(vol_id="VID", label="Big",
                                           capacity_bytes=1000, free_bytes=400))
        got = db.get_volume(self.conn, "VID")
        self.assertEqual(got.label, "Big")
        self.assertEqual(got.capacity_bytes, 1000)
        db.upsert(self.conn, _vol_track("VID", "x.flac", "Big", size=123))
        self.conn.commit()
        self.assertEqual(db.volume_usage(self.conn, "VID"), {"files": 1, "bytes": 123})


class TestMarker(unittest.TestCase):
    def test_marker_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            volumes.write_marker(d, "TestDrive", "abc-123", created=1000.0)
            m = volumes.read_marker(d)
            self.assertIsNotNone(m)
            self.assertEqual(m["id"], "abc-123")
            self.assertEqual(m["label"], "TestDrive")

    def test_read_marker_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(volumes.read_marker(d))


if __name__ == "__main__":
    unittest.main()

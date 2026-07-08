"""Web layer: auth gating, read-only pages, and the push API.

Uses a throwaway index (CRATE_DB) and the MYSTIC_DEV_USER OAuth bypass so no
network or GitHub app is needed. Skipped if Flask isn't installed.
"""

import io
import os
import tempfile
import unittest
from pathlib import Path

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False

from crate import db
from crate.models import Track


@unittest.skipUnless(HAVE_FLASK, "flask not installed")
class TestWeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.environ["CRATE_DB"] = self.tmp.name
        os.environ["MYSTIC_PUSH_TOKEN"] = "testtoken"
        conn = db.connect(Path(self.tmp.name))
        db.upsert(conn, Track(host="styx", path="/m/a.flac", ext=".flac",
                              content_hash="H", artist="Mouse on Mars", title="Juju"))
        conn.commit()
        conn.close()
        from crate.web import create_app
        self.app = create_app()
        self.c = self.app.test_client()

    def tearDown(self):
        for k in ("CRATE_DB", "MYSTIC_PUSH_TOKEN", "MYSTIC_DEV_USER"):
            os.environ.pop(k, None)
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + ext)
            except OSError:
                pass

    def test_requires_login(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_login_page_renders(self):
        r = self.c.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sign in with GitHub", r.get_data(as_text=True))

    def test_pages_with_dev_user(self):
        os.environ["MYSTIC_DEV_USER"] = "nthmost"
        self.assertEqual(self.c.get("/").status_code, 200)
        body = self.c.get("/search?q=mouse+on+mars").get_data(as_text=True)
        self.assertIn("Juju", body)
        self.assertEqual(self.c.get("/coverage").status_code, 200)
        self.assertEqual(self.c.get("/artists").status_code, 200)

    def test_push_requires_token(self):
        r = self.c.post("/api/push", data={"index": (io.BytesIO(b"x"), "crate.db")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 401)

    def test_push_merges_with_token(self):
        # Build a second index with a new row, push it, expect 1 added.
        other = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        other.close()
        oc = db.connect(Path(other.name))
        db.upsert(oc, Track(host="beyla", path="/x/b.mp3", ext=".mp3",
                            artist="Portishead", title="Roads"))
        oc.commit(); oc.close()
        raw = Path(other.name).read_bytes()
        r = self.c.post("/api/push", headers={"Authorization": "Bearer testtoken"},
                        data={"index": (io.BytesIO(raw), "crate.db")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["added"], 1)
        os.unlink(other.name)

    def test_health(self):
        j = self.c.get("/api/health").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["files"], 1)


if __name__ == "__main__":
    unittest.main()

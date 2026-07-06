"""Hermetic tests for identify()'s parsing of AcoustID responses.

We can't rely on a live catalogued track being present, so we patch the
fingerprint step and the AcoustID lookup to feed identify() realistic payloads
and assert it extracts names/ids correctly. Run: python -m unittest -v
"""

import unittest
from unittest import mock

import acoustid

from crate import fingerprint


# Shape mirrors a real AcoustID `meta=recordings releasegroups` response.
_HIT = {
    "status": "ok",
    "results": [
        {
            "id": "9ff43b6a-0000-0000-0000-000000000000",
            "score": 0.97,
            "recordings": [
                {
                    "id": "b81f83ee-1111-1111-1111-111111111111",
                    "title": "Roygbiv",
                    "artists": [{"id": "a", "name": "Boards of Canada"}],
                    "releasegroups": [{"id": "r", "title": "Music Has the Right to Children"}],
                }
            ],
        }
    ],
}


class TestIdentifyParsing(unittest.TestCase):
    def setUp(self):
        # Skip the real decode; identify() only needs a plausible (dur, fp).
        self._fp = mock.patch.object(
            fingerprint, "compute_fingerprint",
            return_value=(291.0, "AQADtFAKE", None),
        )
        self._fp.start()

    def tearDown(self):
        self._fp.stop()

    def test_parses_best_match(self):
        with mock.patch.object(acoustid, "lookup", return_value=_HIT):
            ident = fingerprint.identify("whatever.mp3", api_key="dummy")
        self.assertIsNone(ident.error)
        self.assertEqual(len(ident.matches), 1)
        best = ident.best
        self.assertEqual(best.artist, "Boards of Canada")
        self.assertEqual(best.title, "Roygbiv")
        self.assertEqual(best.album, "Music Has the Right to Children")
        self.assertEqual(best.recording_id, "b81f83ee-1111-1111-1111-111111111111")
        self.assertAlmostEqual(best.score, 0.97)

    def test_empty_results_is_not_an_error(self):
        with mock.patch.object(acoustid, "lookup", return_value={"status": "ok", "results": []}):
            ident = fingerprint.identify("whatever.mp3", api_key="dummy")
        self.assertIsNone(ident.error)
        self.assertEqual(ident.matches, [])
        self.assertEqual(ident.fingerprint, "AQADtFAKE")

    def test_api_error_is_surfaced(self):
        err = {"status": "error", "error": {"code": 4, "message": "invalid API key"}}
        with mock.patch.object(acoustid, "lookup", return_value=err):
            ident = fingerprint.identify("whatever.mp3", api_key="dummy")
        self.assertIn("invalid API key", ident.error)
        self.assertEqual(ident.matches, [])


if __name__ == "__main__":
    unittest.main()

"""On-demand acoustic identification: Chromaprint fingerprint + AcoustID lookup.

This is the "what IS this weird mp3?" path. It's slow (decodes audio) and needs
the network + an AcoustID key for the MusicBrainz lookup, so it is only ever run
against a specific file the user points at — never during a bulk scan.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import acoustid_key


@dataclass
class Match:
    score: float
    recording_id: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None


@dataclass
class Identification:
    fingerprint: str | None = None
    duration: float | None = None
    matches: list[Match] = field(default_factory=list)
    error: str | None = None

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


def compute_fingerprint(path: str | Path) -> tuple[float | None, str | None, str | None]:
    """Return (duration, fingerprint, error) using Chromaprint's fpcalc."""
    try:
        import acoustid
    except ImportError:
        return None, None, "pyacoustid not installed"
    if not fpcalc_available():
        return None, None, "fpcalc (chromaprint) not found on PATH"
    try:
        duration, fp = acoustid.fingerprint_file(str(path))
        fp_str = fp.decode() if isinstance(fp, bytes) else str(fp)
        return float(duration), fp_str, None
    except Exception as e:  # acoustid.FingerprintGenerationError, etc.
        return None, None, f"fingerprint failed: {e}"


def identify(path: str | Path, *, api_key: str | None = None,
             max_results: int = 5) -> Identification:
    """Fingerprint a file and look it up against AcoustID/MusicBrainz.

    Without an API key we still return the fingerprint + duration (useful as a
    stable content id) but no matches.
    """
    duration, fp, err = compute_fingerprint(path)
    ident = Identification(fingerprint=fp, duration=duration)
    if err:
        ident.error = err
        return ident

    key = api_key or acoustid_key()
    if not key:
        ident.error = "no AcoustID API key (set CRATE_ACOUSTID_KEY); fingerprint only"
        return ident

    try:
        import acoustid
    except ImportError:
        ident.error = "pyacoustid not installed"
        return ident

    try:
        results = acoustid.lookup(key, fp, duration, meta="recordings releasegroups")
    except Exception as e:
        ident.error = f"AcoustID lookup failed: {e}"
        return ident

    for res in results.get("results", [])[:max_results]:
        score = res.get("score", 0.0)
        recordings = res.get("recordings") or [{}]
        for rec in recordings[:1]:
            artists = rec.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in artists) or None
            rgs = rec.get("releasegroups") or []
            album = rgs[0].get("title") if rgs else None
            ident.matches.append(Match(
                score=round(score, 3),
                recording_id=rec.get("id"),
                title=rec.get("title"),
                artist=artist,
                album=album,
            ))
    ident.matches.sort(key=lambda m: m.score, reverse=True)
    return ident

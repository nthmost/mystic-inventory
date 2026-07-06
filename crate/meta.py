"""Extract tags + technical info from an audio file using mutagen.

Handles the format-specific tag key differences (ID3 vs Vorbis vs MP4) behind a
single normalized interface. Never raises for a merely-untagged file — that's a
valid, expected state we want to record, not an error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import mutagen

# Normalized field -> candidate tag keys across formats (checked in order).
_TAG_MAP: dict[str, list[str]] = {
    "title":       ["title", "TIT2", "\xa9nam"],
    "artist":      ["artist", "TPE1", "\xa9ART"],
    "album":       ["album", "TALB", "\xa9alb"],
    "albumartist": ["albumartist", "album artist", "TPE2", "aART"],
    "genre":       ["genre", "TCON", "\xa9gen"],
}
_TRACK_KEYS = ["tracknumber", "TRCK", "trkn"]
_DISC_KEYS = ["discnumber", "TPOS", "disk"]
_DATE_KEYS = ["date", "year", "originaldate", "TDRC", "TYER", "\xa9day"]


def _first(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]
    # MP4 track/disc tuples like (3, 12)
    if isinstance(value, tuple):
        value = value[0]
    s = str(value).strip()
    return s or None


def _lookup(tags: Any, keys: list[str]) -> str | None:
    if tags is None:
        return None
    for k in keys:
        try:
            v = _first(tags[k])
            if v:
                return v
        except (KeyError, ValueError, TypeError):
            # ValueError: mutagen Vorbis raises on keys invalid for that format
            # (e.g. probing an ID3 key like 'TIT2' against a FLAC file).
            continue
    return None


def _to_int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"\s*(\d+)", str(s))
    return int(m.group(1)) if m else None


def _year(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"(\d{4})", str(s))
    return int(m.group(1)) if m else None


def read_metadata(path: str | Path) -> dict[str, Any]:
    """Return a dict of normalized metadata + technical fields.

    Returns {'_audio': False} if the file isn't recognizable audio; otherwise
    includes any of: title, artist, album, albumartist, track_no, disc_no,
    year, genre, duration, bitrate, samplerate, channels, tags_json.
    """
    try:
        mf = mutagen.File(str(path))
    except Exception:
        mf = None
    if mf is None:
        return {"_audio": False}

    out: dict[str, Any] = {"_audio": True}
    tags = getattr(mf, "tags", None)

    for field, keys in _TAG_MAP.items():
        out[field] = _lookup(tags, keys)
    out["track_no"] = _to_int(_lookup(tags, _TRACK_KEYS))
    out["disc_no"] = _to_int(_lookup(tags, _DISC_KEYS))
    out["year"] = _year(_lookup(tags, _DATE_KEYS))

    info = getattr(mf, "info", None)
    if info is not None:
        out["duration"] = round(getattr(info, "length", 0) or 0, 3) or None
        br = getattr(info, "bitrate", None)
        out["bitrate"] = int(br) if br else None
        sr = getattr(info, "sample_rate", None)
        out["samplerate"] = int(sr) if sr else None
        ch = getattr(info, "channels", None)
        out["channels"] = int(ch) if ch else None

    # Raw tag dump (best-effort) so nothing is silently lost.
    if tags is not None:
        try:
            raw = {str(k): [str(x) for x in (v if isinstance(v, list) else [v])]
                   for k, v in dict(tags).items()}
            out["tags_json"] = json.dumps(raw, ensure_ascii=False)[:20000]
        except Exception:
            out["tags_json"] = None

    return out

"""Plain data carriers shared across the library and every frontend."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Track:
    """One playable audio file at one location on one host.

    A given recording may exist as several Track rows (same song on different
    drives/hosts); `content_hash` and `acoustid` are how we relate them.
    """

    # location
    host: str
    path: str
    volume: str | None = None
    size: int | None = None
    mtime: float | None = None
    ext: str | None = None
    content_hash: str | None = None

    # removable-volume identity (set for files on a registered external drive).
    # When vol_id is set, the real identity is (vol_id, relpath) — mount-point
    # and host independent — while host/path record where it was last seen.
    vol_id: str | None = None
    relpath: str | None = None
    vol_label: str | None = None

    # metadata (from tags)
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    albumartist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    year: int | None = None
    genre: str | None = None

    # technical
    duration: float | None = None
    bitrate: int | None = None
    samplerate: int | None = None
    channels: int | None = None

    # identification (populated on-demand by `identify`)
    fingerprint: str | None = None
    acoustid: str | None = None
    mb_recording_id: str | None = None

    # bookkeeping
    id: int | None = None
    tags_json: str | None = None

    def as_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id", None)
        return d

    @property
    def display(self) -> str:
        """Human label: 'Artist — Title', falling back to the filename."""
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        if self.title:
            return self.title
        return self.path.rsplit("/", 1)[-1]

    @property
    def is_untagged(self) -> bool:
        return not (self.artist or self.title)

    @property
    def location(self) -> str:
        """Where the bytes live: a drive label if on a volume, else the host."""
        return self.vol_label or self.host


@dataclass
class Volume:
    """A registered storage volume — typically a roaming external drive.

    Identity is `vol_id` (a UUID crate wrote to a marker file at the drive root),
    which survives unplugging, remounting, and moving between machines. The
    last_* fields record where/when it was most recently seen and scanned.
    """

    vol_id: str
    label: str
    fs_uuid: str | None = None
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    last_host: str | None = None
    last_mount: str | None = None
    last_scanned: float | None = None
    last_seen: float | None = None
    created: float | None = None
    id: int | None = None

    def as_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("id", None)
        return d

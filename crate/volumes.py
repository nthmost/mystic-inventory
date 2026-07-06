"""Removable-volume support: stable drive identity that survives unplugging.

A drive is identified by a marker file crate writes at its root
(`.crate-volume.json`) holding a generated UUID + label. Because the id lives on
the drive itself, crate recognizes the same drive no matter which machine it's
plugged into or where it mounts — and the index keeps answering "what's on this
drive?" while it sits in a drawer, unplugged.

We also opportunistically record the OS filesystem UUID, but the marker is the
source of truth (it's portable and reformatting-tolerant in the ways we care).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import hostname
from .meta import read_metadata
from .models import Track, Volume
from .scan import ScanResult, iter_audio_files, quick_hash

MARKER = ".crate-volume.json"

# Where removable drives typically mount, by platform. Each entry is a parent
# dir whose immediate children are candidate mount points.
_DEFAULT_MOUNT_ROOTS = [
    "/Volumes",                     # macOS
    "/media",                       # Linux (some distros mount directly here)
    "/mnt",                         # Linux manual mounts
    f"/media/{os.environ.get('USER', '')}",   # Linux (udisks)
    f"/run/media/{os.environ.get('USER', '')}",
]


def _mount_roots() -> list[str]:
    """Candidate mount-parent dirs, plus any from CRATE_MOUNT_ROOTS (colon-sep)."""
    roots = list(_DEFAULT_MOUNT_ROOTS)
    extra = os.environ.get("CRATE_MOUNT_ROOTS", "")
    roots += [r for r in extra.split(":") if r]
    return roots


# ---- marker file ----------------------------------------------------------

def read_marker(mount: str | Path) -> dict | None:
    p = Path(mount) / MARKER
    try:
        if p.is_file():
            data = json.loads(p.read_text())
            if data.get("crate_volume") and data.get("id"):
                return data
    except (OSError, ValueError):
        return None
    return None


def write_marker(mount: str | Path, label: str, vol_id: str, created: float) -> dict:
    data = {
        "crate_volume": 1,
        "id": vol_id,
        "label": label,
        "created": created,
        "note": "crate drive marker — safe to keep; identifies this drive across machines.",
    }
    (Path(mount) / MARKER).write_text(json.dumps(data, indent=2) + "\n")
    return data


# ---- OS-level probing (best effort) ---------------------------------------

def fs_uuid(mount: str | Path) -> str | None:
    """Filesystem UUID via OS tools; None if unavailable. Never fatal."""
    mount = str(mount)
    try:
        if shutil.which("findmnt"):  # Linux
            out = subprocess.run(
                ["findmnt", "-no", "UUID", "--target", mount],
                capture_output=True, text=True, timeout=5)
            u = out.stdout.strip()
            if u:
                return u
        if shutil.which("diskutil"):  # macOS
            out = subprocess.run(
                ["diskutil", "info", mount],
                capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                if "Volume UUID" in line:
                    return line.split(":", 1)[1].strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def capacity(mount: str | Path) -> tuple[int | None, int | None]:
    """(total_bytes, free_bytes) for the filesystem at mount, or (None, None)."""
    try:
        s = os.statvfs(str(mount))
        return s.f_frsize * s.f_blocks, s.f_frsize * s.f_bavail
    except OSError:
        return None, None


# ---- mount discovery ------------------------------------------------------

def discover_mounts() -> list[tuple[str, dict]]:
    """Find currently-mounted drives that carry a crate marker.

    Returns [(mount_path, marker_dict), ...].
    """
    found: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for root in _mount_roots():
        rp = Path(root)
        if not rp.is_dir():
            continue
        try:
            children = list(rp.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            rp_str = str(child)
            if rp_str in seen:
                continue
            marker = read_marker(child)
            if marker:
                seen.add(rp_str)
                found.append((rp_str, marker))
    return found


def find_mount(vol_id: str) -> str | None:
    """Current mount path for a given volume id, if it's plugged in now."""
    for mount, marker in discover_mounts():
        if marker.get("id") == vol_id:
            return mount
    return None


# ---- registration + scanning ----------------------------------------------

def register(conn, mount: str | Path, label: str) -> Volume:
    """Mark a mounted drive as a crate volume and record it in the index.

    If the drive already has a marker, its existing id/label are kept (re-regist
    just refreshes capacity + last-seen). Writing the marker is the only time
    crate writes to your drive.
    """
    mount = str(Path(mount).resolve())
    if not Path(mount).is_dir():
        raise NotADirectoryError(mount)
    existing = read_marker(mount)
    now = time.time()
    if existing:
        vol_id = existing["id"]
        label = existing.get("label", label)
        created = existing.get("created", now)
    else:
        vol_id = str(uuid.uuid4())
        created = now
        write_marker(mount, label, vol_id, created)
    total, free = capacity(mount)
    vol = Volume(
        vol_id=vol_id, label=label, fs_uuid=fs_uuid(mount),
        capacity_bytes=total, free_bytes=free,
        last_host=hostname(), last_mount=mount,
        last_seen=now, created=created,
    )
    db.upsert_volume(conn, vol)
    return vol


def scan_volume(conn, mount: str | Path, *, do_hash: bool = True,
                progress=None) -> tuple[Volume, ScanResult]:
    """Index every audio file on a registered drive, keyed by (vol_id, relpath).

    The drive must already carry a marker (call `register` first). Paths are
    stored relative to the mount so they stay valid wherever it next mounts.
    """
    mount = str(Path(mount).resolve())
    marker = read_marker(mount)
    if not marker:
        raise ValueError(f"{mount} is not a registered crate volume (run register first)")
    vol_id = marker["id"]
    label = marker.get("label", vol_id[:8])
    host = hostname()
    res = ScanResult()
    for path in iter_audio_files([mount]):
        res.scanned += 1
        try:
            if path.name == MARKER:
                continue
            st = path.stat()
            md = read_metadata(path)
            if not md.get("_audio"):
                res.skipped_nonaudio += 1
                continue
            size = st.st_size
            rel = os.path.relpath(str(path), mount)
            track = Track(
                host=host, path=str(path), volume=mount,
                vol_id=vol_id, relpath=rel, vol_label=label,
                size=size, mtime=st.st_mtime, ext=path.suffix.lower(),
                content_hash=quick_hash(path, size) if do_hash else None,
                title=md.get("title"), artist=md.get("artist"),
                album=md.get("album"), albumartist=md.get("albumartist"),
                track_no=md.get("track_no"), disc_no=md.get("disc_no"),
                year=md.get("year"), genre=md.get("genre"),
                duration=md.get("duration"), bitrate=md.get("bitrate"),
                samplerate=md.get("samplerate"), channels=md.get("channels"),
                tags_json=md.get("tags_json"),
            )
            db.upsert(conn, track)
            res.added += 1
        except Exception:
            res.errors += 1
        if progress:
            progress(path, res)
        if res.added % 500 == 0:
            conn.commit()
    # Refresh the volume record with fresh capacity + scan time.
    total, free = capacity(mount)
    vol = db.get_volume(conn, vol_id) or Volume(vol_id=vol_id, label=label)
    vol.label = label
    vol.capacity_bytes, vol.free_bytes = total, free
    vol.last_host, vol.last_mount = host, mount
    vol.last_scanned = vol.last_seen = time.time()
    vol.fs_uuid = vol.fs_uuid or fs_uuid(mount)
    db.upsert_volume(conn, vol)
    conn.commit()
    return vol, res


@dataclass
class VolumeStatus:
    volume: Volume
    online: bool
    mount: str | None
    files: int
    bytes: int


def status(conn) -> list[VolumeStatus]:
    """Every registered volume with live online/offline + usage."""
    online = {m[1]["id"]: m[0] for m in discover_mounts()}
    out = []
    for vol in db.list_volumes(conn):
        usage = db.volume_usage(conn, vol.vol_id)
        out.append(VolumeStatus(
            volume=vol,
            online=vol.vol_id in online,
            mount=online.get(vol.vol_id),
            files=usage["files"],
            bytes=usage["bytes"],
        ))
    return out

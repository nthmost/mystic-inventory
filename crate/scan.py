"""Filesystem walk → metadata read → upsert into the index.

Fast path only: tags + technical info. Fingerprinting is deliberately NOT done
here (it's slow and needs the network) — see fingerprint.py / `identify`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import db
from .config import AUDIO_EXTS, hostname
from .meta import read_metadata
from .models import Track


@dataclass
class ScanResult:
    scanned: int = 0
    added: int = 0
    skipped_nonaudio: int = 0
    errors: int = 0


def _mount_point(path: Path) -> str:
    """Best-effort volume label: the mount point the path lives under."""
    p = path.resolve()
    try:
        dev = p.stat().st_dev
    except OSError:
        return "/"
    cur = p
    while cur.parent != cur:
        parent = cur.parent
        try:
            if parent.stat().st_dev != dev:
                break
        except OSError:
            break
        cur = parent
    return str(cur)


def quick_hash(path: Path, size: int) -> str | None:
    """Cheap content fingerprint for dedupe: size + head + tail bytes.

    Not cryptographic — just enough to relate identical files across drives
    without reading multi-GB files end to end.
    """
    try:
        h = hashlib.sha1()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(65536))
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
        return h.hexdigest()
    except OSError:
        return None


def iter_audio_files(roots: list[str | Path]) -> Iterator[Path]:
    for root in roots:
        root = Path(root).expanduser()
        if root.is_file():
            if root.suffix.lower() in AUDIO_EXTS:
                yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip dotdirs and common noise to keep the walk quick.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in AUDIO_EXTS:
                    yield Path(dirpath) / name


def scan(
    roots: list[str | Path],
    conn=None,
    *,
    host: str | None = None,
    do_hash: bool = True,
    progress: Callable[[Path, ScanResult], None] | None = None,
) -> ScanResult:
    """Walk roots, read metadata, upsert into the index. Returns a summary."""
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    host = host or hostname()
    res = ScanResult()
    try:
        for path in iter_audio_files(roots):
            res.scanned += 1
            try:
                st = path.stat()
                md = read_metadata(path)
                if not md.get("_audio"):
                    res.skipped_nonaudio += 1
                    if progress:
                        progress(path, res)
                    continue
                size = st.st_size
                track = Track(
                    host=host,
                    path=str(path.resolve()),
                    volume=_mount_point(path),
                    size=size,
                    mtime=st.st_mtime,
                    ext=path.suffix.lower(),
                    content_hash=quick_hash(path, size) if do_hash else None,
                    title=md.get("title"),
                    artist=md.get("artist"),
                    album=md.get("album"),
                    albumartist=md.get("albumartist"),
                    track_no=md.get("track_no"),
                    disc_no=md.get("disc_no"),
                    year=md.get("year"),
                    genre=md.get("genre"),
                    duration=md.get("duration"),
                    bitrate=md.get("bitrate"),
                    samplerate=md.get("samplerate"),
                    channels=md.get("channels"),
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
        conn.commit()
    finally:
        if own_conn:
            conn.close()
    return res

"""SQLite index: schema, connection handling, upsert, and search.

The index is intentionally a single portable file so it can be copied between
hosts and merged. Every row is keyed by (host, path); the same recording living
on multiple drives yields multiple rows related by content_hash / acoustid.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Iterator

from .config import db_path
from .models import Track

SCHEMA_VERSION = 1

# Columns that map 1:1 to Track fields, in a stable order for INSERTs.
_COLUMNS = [
    "host", "path", "volume", "size", "mtime", "ext", "content_hash",
    "title", "artist", "album", "albumartist", "track_no", "disc_no",
    "year", "genre", "duration", "bitrate", "samplerate", "channels",
    "fingerprint", "acoustid", "mb_recording_id", "tags_json",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    host TEXT NOT NULL,
    path TEXT NOT NULL,
    volume TEXT,
    size INTEGER,
    mtime REAL,
    ext TEXT,
    content_hash TEXT,
    title TEXT,
    artist TEXT,
    album TEXT,
    albumartist TEXT,
    track_no INTEGER,
    disc_no INTEGER,
    year INTEGER,
    genre TEXT,
    duration REAL,
    bitrate INTEGER,
    samplerate INTEGER,
    channels INTEGER,
    fingerprint TEXT,
    acoustid TEXT,
    mb_recording_id TEXT,
    tags_json TEXT,
    -- lowercased 'artist album title path' blob for cheap multi-term search
    search_blob TEXT,
    first_seen REAL DEFAULT (unixepoch()),
    last_seen REAL DEFAULT (unixepoch()),
    UNIQUE(host, path)
);

CREATE INDEX IF NOT EXISTS idx_files_artist ON files(artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_album  ON files(album COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_hash   ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_acoustid ON files(acoustid);
CREATE INDEX IF NOT EXISTS idx_files_host   ON files(host);
"""


def _search_blob(t: Track) -> str:
    parts = [t.artist, t.albumartist, t.album, t.title, t.path]
    return " ".join(p for p in parts if p).lower()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def upsert(conn: sqlite3.Connection, track: Track) -> None:
    """Insert or refresh a track by (host, path). Preserves first_seen."""
    row = track.as_row()
    row["search_blob"] = _search_blob(track)
    cols = _COLUMNS + ["search_blob"]
    placeholders = ", ".join(f":{c}" for c in cols)
    collist = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("host", "path"))
    conn.execute(
        f"""
        INSERT INTO files ({collist}, last_seen)
        VALUES ({placeholders}, unixepoch())
        ON CONFLICT(host, path) DO UPDATE SET
            {updates}, last_seen=unixepoch()
        """,
        row,
    )


def _row_to_track(row: sqlite3.Row) -> Track:
    data = {k: row[k] for k in row.keys() if k in Track.__dataclass_fields__}
    return Track(**data)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    host: str | None = None,
    limit: int = 200,
) -> list[Track]:
    """Multi-term AND search across artist/album/title/path (case-insensitive).

    'mouse on mars' matches rows whose search_blob contains all three tokens.
    """
    terms = [t for t in query.lower().split() if t]
    where = []
    params: list = []
    for term in terms:
        where.append("search_blob LIKE ?")
        params.append(f"%{term}%")
    if host:
        where.append("host = ?")
        params.append(host)
    clause = " AND ".join(where) if where else "1=1"
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM files WHERE {clause} "
        f"ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, "
        f"track_no, title COLLATE NOCASE LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_track(r) for r in rows]


def get_by_path(conn: sqlite3.Connection, host: str, path: str) -> Track | None:
    row = conn.execute(
        "SELECT * FROM files WHERE host=? AND path=?", (host, path)
    ).fetchone()
    return _row_to_track(row) if row else None


def update_identification(
    conn: sqlite3.Connection,
    track_id: int,
    *,
    fingerprint: str | None,
    acoustid: str | None,
    mb_recording_id: str | None,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
) -> None:
    """Write fingerprint results back, filling missing tags without clobbering good ones."""
    conn.execute(
        """
        UPDATE files SET
            fingerprint = COALESCE(?, fingerprint),
            acoustid = COALESCE(?, acoustid),
            mb_recording_id = COALESCE(?, mb_recording_id),
            title = COALESCE(title, ?),
            artist = COALESCE(artist, ?),
            album = COALESCE(album, ?),
            search_blob = lower(
                COALESCE(artist, ?) || ' ' || COALESCE(albumartist,'') || ' ' ||
                COALESCE(album, ?) || ' ' || COALESCE(title, ?) || ' ' || path
            )
        WHERE id = ?
        """,
        (fingerprint, acoustid, mb_recording_id, title, artist, album,
         artist, album, title, track_id),
    )
    conn.commit()


def untagged(conn: sqlite3.Connection, *, host: str | None = None,
             limit: int = 200) -> list[Track]:
    where = "(artist IS NULL OR artist='') AND (title IS NULL OR title='')"
    params: list = []
    if host:
        where += " AND host=?"
        params.append(host)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM files WHERE {where} ORDER BY path LIMIT ?", params
    ).fetchall()
    return [_row_to_track(r) for r in rows]


def duplicates(conn: sqlite3.Connection, limit: int = 200) -> list[list[Track]]:
    """Groups of rows sharing a content_hash (same bytes on ≥2 locations)."""
    hashes = conn.execute(
        "SELECT content_hash FROM files WHERE content_hash IS NOT NULL "
        "GROUP BY content_hash HAVING COUNT(*) > 1 LIMIT ?",
        (limit,),
    ).fetchall()
    groups = []
    for h in hashes:
        rows = conn.execute(
            "SELECT * FROM files WHERE content_hash=?", (h["content_hash"],)
        ).fetchall()
        groups.append([_row_to_track(r) for r in rows])
    return groups


def stats(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM files")
    total = cur.fetchone()
    per_host = conn.execute(
        "SELECT host, COUNT(*) n, COALESCE(SUM(size),0) b FROM files GROUP BY host"
    ).fetchall()
    untagged_n = conn.execute(
        "SELECT COUNT(*) n FROM files WHERE (artist IS NULL OR artist='') "
        "AND (title IS NULL OR title='')"
    ).fetchone()["n"]
    by_ext = conn.execute(
        "SELECT ext, COUNT(*) n FROM files GROUP BY ext ORDER BY n DESC"
    ).fetchall()
    return {
        "total_files": total["n"],
        "total_bytes": total["b"],
        "untagged": untagged_n,
        "per_host": [dict(r) for r in per_host],
        "by_ext": [dict(r) for r in by_ext],
    }

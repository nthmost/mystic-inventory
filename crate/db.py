"""SQLite index: schema, connection handling, upsert, and search.

The index is intentionally a single portable file so it can be copied between
hosts and merged. Every row is keyed by (host, path); the same recording living
on multiple drives yields multiple rows related by content_hash / acoustid.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path
from typing import Iterable, Iterator

from .config import db_path

# Latin letters that don't decompose under NFKD but should still fold to ASCII.
_FOLD_MAP = str.maketrans({
    "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "æ": "ae", "Æ": "ae",
    "œ": "oe", "Œ": "oe", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d",
    "þ": "th", "Þ": "th", "ı": "i", "ß": "ss", "ħ": "h", "ĸ": "k",
})


def fold(s: str | None) -> str:
    """Lowercase + strip diacritics so 'Dúlamán' folds to 'dulaman'.

    Used for accent-insensitive search: both the stored search text and the
    query terms are folded through here before matching.
    """
    if not s:
        return ""
    s = s.translate(_FOLD_MAP)
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
from .models import Track, Volume

SCHEMA_VERSION = 2

# Columns that map 1:1 to Track fields, in a stable order for INSERTs.
_COLUMNS = [
    "host", "path", "volume", "size", "mtime", "ext", "content_hash",
    "vol_id", "relpath", "vol_label",
    "title", "artist", "album", "albumartist", "track_no", "disc_no",
    "year", "genre", "duration", "bitrate", "samplerate", "channels",
    "fingerprint", "acoustid", "mb_recording_id", "tags_json",
]

_VOLUME_COLUMNS = [
    "vol_id", "label", "fs_uuid", "capacity_bytes", "free_bytes",
    "last_host", "last_mount", "last_scanned", "last_seen", "created",
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
    vol_id TEXT,
    relpath TEXT,
    vol_label TEXT,
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
    -- search_blob with diacritics stripped, so 'dulaman' matches 'Dúlamán'
    search_fold TEXT,
    first_seen REAL DEFAULT (unixepoch()),
    last_seen REAL DEFAULT (unixepoch()),
    UNIQUE(host, path)
);

CREATE INDEX IF NOT EXISTS idx_files_artist ON files(artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_album  ON files(album COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_files_hash   ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_acoustid ON files(acoustid);
CREATE INDEX IF NOT EXISTS idx_files_host   ON files(host);

CREATE TABLE IF NOT EXISTS volumes (
    id INTEGER PRIMARY KEY,
    vol_id TEXT NOT NULL UNIQUE,
    label TEXT,
    fs_uuid TEXT,
    capacity_bytes INTEGER,
    free_bytes INTEGER,
    last_host TEXT,
    last_mount TEXT,
    last_scanned REAL,
    last_seen REAL,
    created REAL
);
"""

# Additive migrations from older schemas: add columns that CREATE TABLE
# IF NOT EXISTS won't add to a pre-existing table.
_MIGRATIONS = {
    "vol_id": "ALTER TABLE files ADD COLUMN vol_id TEXT",
    "relpath": "ALTER TABLE files ADD COLUMN relpath TEXT",
    "vol_label": "ALTER TABLE files ADD COLUMN vol_label TEXT",
    "search_fold": "ALTER TABLE files ADD COLUMN search_fold TEXT",
}

# Indexes referencing columns that may be added by migration — created only
# after the columns are guaranteed to exist.
_POST_MIGRATE = """
CREATE INDEX IF NOT EXISTS idx_files_vol ON files(vol_id);

-- The real identity of a file on a removable drive: mount-point independent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_vol_relpath
    ON files(vol_id, relpath) WHERE vol_id IS NOT NULL;
"""


def _search_blob(t: Track) -> str:
    parts = [t.artist, t.albumartist, t.album, t.title, t.vol_label, t.relpath, t.path]
    return " ".join(p for p in parts if p).lower()


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    added = set()
    for col, ddl in _MIGRATIONS.items():
        if col not in have:
            conn.execute(ddl)
            added.add(col)
    # One-time backfill of folded search text when the column is first added
    # (afterwards, upsert keeps it current, so no per-open scan is needed).
    if "search_fold" in added:
        conn.create_function("crate_fold", 1, fold, deterministic=True)
        conn.execute(
            "UPDATE files SET search_fold = crate_fold(search_blob) "
            "WHERE search_blob IS NOT NULL"
        )


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.executescript(_POST_MIGRATE)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def upsert(conn: sqlite3.Connection, track: Track) -> None:
    """Insert or refresh a track. Preserves first_seen.

    Files on a registered volume are keyed by (vol_id, relpath) so the same
    physical file is one row no matter which host/mount it was seen on;
    host-internal files are keyed by (host, path) as before.
    """
    row = track.as_row()
    row["search_blob"] = _search_blob(track)
    row["search_fold"] = fold(row["search_blob"])
    cols = _COLUMNS + ["search_blob", "search_fold"]
    placeholders = ", ".join(f":{c}" for c in cols)
    collist = ", ".join(cols)
    if track.vol_id:
        # Must repeat the partial index's WHERE clause to target it.
        conflict = "(vol_id, relpath) WHERE vol_id IS NOT NULL"
        frozen = ("vol_id", "relpath")
    else:
        conflict = "(host, path)"
        frozen = ("host", "path")
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in frozen)
    conn.execute(
        f"""
        INSERT INTO files ({collist}, last_seen)
        VALUES ({placeholders}, unixepoch())
        ON CONFLICT{conflict} DO UPDATE SET
            {updates}, last_seen=unixepoch()
        """,
        row,
    )


def _row_to_track(row: sqlite3.Row) -> Track:
    data = {k: row[k] for k in row.keys() if k in Track.__dataclass_fields__}
    return Track(**data)


def _term_clause(query: str, *, host: str | None = None) -> tuple[str, list]:
    """Build the AND-of-LIKE WHERE clause shared by search/search_albums.

    'mouse on mars' -> every token must appear in search_blob.
    """
    where, params = [], []
    for term in query.split():
        folded = fold(term)
        if folded:
            where.append("search_fold LIKE ?")
            params.append(f"%{folded}%")
    if host:
        where.append("host = ?")
        params.append(host)
    return (" AND ".join(where) if where else "1=1"), params


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    host: str | None = None,
    limit: int = 200,
) -> list[Track]:
    """Multi-term AND search across artist/album/title/path (case-insensitive)."""
    clause, params = _term_clause(query, host=host)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM files WHERE {clause} "
        f"ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, "
        f"track_no, title COLLATE NOCASE LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_track(r) for r in rows]


def search_albums(conn: sqlite3.Connection, query: str, *, limit: int = 200) -> list[dict]:
    """Album-level view of a search: one row per (artist, album) that matches.

    'Deltron' -> the Deltron 3030 albums, each with track count, runtime, year,
    and which locations hold it. Tracks with no album tag are excluded here
    (see `search` / loose_matches for those).
    """
    clause, params = _term_clause(query)
    loc = _location_expr()
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT artist, album, MAX(year) AS year, COUNT(*) AS tracks,
               COALESCE(SUM(duration),0) AS duration,
               GROUP_CONCAT(DISTINCT {loc}) AS locations
        FROM files
        WHERE ({clause}) AND album IS NOT NULL AND album != ''
        GROUP BY artist COLLATE NOCASE, album COLLATE NOCASE
        ORDER BY artist COLLATE NOCASE, year, album COLLATE NOCASE
        LIMIT ?
        """,
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["locations"] = sorted(set((d.get("locations") or "").split(",")))
        out.append(d)
    return out


def loose_matches(conn: sqlite3.Connection, query: str, *, limit: int = 100) -> list[Track]:
    """Matching tracks that have NO album tag — singles, mystery files, etc."""
    clause, params = _term_clause(query)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM files WHERE ({clause}) AND (album IS NULL OR album='') "
        f"ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_track(r) for r in rows]


def album_tracks(conn: sqlite3.Connection, artist: str, album: str,
                 *, limit: int = 500) -> list[Track]:
    """Every track of one album (across all locations), in track order."""
    rows = conn.execute(
        "SELECT * FROM files WHERE artist=? COLLATE NOCASE AND album=? COLLATE NOCASE "
        "ORDER BY disc_no, track_no, title COLLATE NOCASE LIMIT ?",
        (artist, album, limit),
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
    # Keep the folded search text in sync with the rebuilt blob.
    conn.create_function("crate_fold", 1, fold, deterministic=True)
    conn.execute(
        "UPDATE files SET search_fold = crate_fold(search_blob) WHERE id = ?",
        (track_id,),
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


def merge(conn: sqlite3.Connection, other_path: Path | str) -> dict:
    """Import all rows from another crate.db into this index.

    Rows are keyed by (host, path), so merging beyla's index into styx's simply
    adds beyla's rows; re-merging an updated index refreshes matching rows in
    place. Returns {'added', 'updated', 'total_source'}.
    """
    other = Path(other_path)
    if not other.is_file():
        raise FileNotFoundError(other)
    cols = _COLUMNS + ["search_blob"]
    collist = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("host", "path"))

    conn.execute("ATTACH DATABASE ? AS src", (str(other),))
    try:
        # Route each source row through the volume-aware upsert so files on a
        # registered drive dedup by (vol_id, relpath) while host-internal files
        # dedup by (host, path). A single INSERT..ON CONFLICT can't target both.
        source_total = conn.execute("SELECT COUNT(*) n FROM src.files").fetchone()["n"]
        added = 0
        for srow in conn.execute("SELECT * FROM src.files").fetchall():
            track = _row_to_track(srow)
            if track.vol_id:
                exists = conn.execute(
                    "SELECT 1 FROM files WHERE vol_id=? AND relpath=?",
                    (track.vol_id, track.relpath),
                ).fetchone()
            else:
                exists = conn.execute(
                    "SELECT 1 FROM files WHERE host=? AND path=?",
                    (track.host, track.path),
                ).fetchone()
            if not exists:
                added += 1
            upsert(conn, track)
        # Bring over any volume records the source knows about.
        for vrow in conn.execute("SELECT * FROM src.volumes").fetchall():
            upsert_volume(conn, _row_to_volume(vrow))
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE src")
    return {
        "total_source": source_total,
        "added": added,
        "updated": source_total - added,
    }


# ---- volumes (registered removable drives) --------------------------------

def _row_to_volume(row: sqlite3.Row) -> Volume:
    data = {k: row[k] for k in row.keys() if k in Volume.__dataclass_fields__}
    return Volume(**data)


def upsert_volume(conn: sqlite3.Connection, vol: Volume) -> None:
    """Insert or refresh a volume record by its stable vol_id."""
    row = vol.as_row()
    cols = _VOLUME_COLUMNS
    placeholders = ", ".join(f":{c}" for c in cols)
    collist = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "vol_id")
    conn.execute(
        f"""
        INSERT INTO volumes ({collist}) VALUES ({placeholders})
        ON CONFLICT(vol_id) DO UPDATE SET {updates}
        """,
        row,
    )
    conn.commit()


def list_volumes(conn: sqlite3.Connection) -> list[Volume]:
    rows = conn.execute("SELECT * FROM volumes ORDER BY label COLLATE NOCASE").fetchall()
    return [_row_to_volume(r) for r in rows]


def get_volume(conn: sqlite3.Connection, vol_id: str) -> Volume | None:
    row = conn.execute("SELECT * FROM volumes WHERE vol_id=?", (vol_id,)).fetchone()
    return _row_to_volume(row) if row else None


def volume_usage(conn: sqlite3.Connection, vol_id: str) -> dict:
    """Indexed file count + bytes recorded for one volume."""
    r = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM files WHERE vol_id=?",
        (vol_id,),
    ).fetchone()
    return {"files": r["n"], "bytes": r["b"]}


# ---- backup coverage ------------------------------------------------------

def _location_expr() -> str:
    """SQL for a file's location label: drive label if on a volume, else host."""
    return "COALESCE(vol_label, host)"


def at_risk(conn: sqlite3.Connection, *, limit: int = 500) -> list[Track]:
    """Files whose content exists in exactly ONE location — no backup copy.

    Copies are matched by content_hash across all hosts and volumes; a file is
    'at risk' if it (and any byte-identical twin) all live in a single location.
    """
    loc = _location_expr()
    rows = conn.execute(
        f"""
        SELECT * FROM files WHERE content_hash IN (
            SELECT content_hash FROM files
            WHERE content_hash IS NOT NULL
            GROUP BY content_hash
            HAVING COUNT(DISTINCT {loc}) = 1
        )
        ORDER BY {loc}, path
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_track(r) for r in rows]


def coverage(conn: sqlite3.Connection) -> dict:
    """Backup-coverage summary across every known location.

    'protected' = content that exists in ≥2 distinct locations; 'at_risk' =
    content in exactly one. Counts are of distinct content hashes.
    """
    loc = _location_expr()
    rows = conn.execute(
        f"""
        SELECT nloc, COUNT(*) n FROM (
            SELECT content_hash, COUNT(DISTINCT {loc}) nloc
            FROM files WHERE content_hash IS NOT NULL
            GROUP BY content_hash
        ) GROUP BY nloc ORDER BY nloc
        """
    ).fetchall()
    by_copies = {r["nloc"]: r["n"] for r in rows}
    at_risk_n = by_copies.get(1, 0)
    protected_n = sum(n for k, n in by_copies.items() if k >= 2)
    return {
        "distinct_content": sum(by_copies.values()),
        "at_risk": at_risk_n,
        "protected": protected_n,
        "by_copies": by_copies,
    }


ARTIST_SORTS = {
    "az":         "artist COLLATE NOCASE ASC",
    "za":         "artist COLLATE NOCASE DESC",
    "count_desc": "n DESC, artist COLLATE NOCASE ASC",
    "count_asc":  "n ASC, artist COLLATE NOCASE ASC",
}


def top_artists(conn: sqlite3.Connection, *, sort: str = "count_desc",
                limit: int = 500) -> list[dict]:
    order = ARTIST_SORTS.get(sort, ARTIST_SORTS["count_desc"])
    rows = conn.execute(
        f"SELECT artist, COUNT(*) n FROM files WHERE artist IS NOT NULL AND artist!='' "
        f"GROUP BY artist COLLATE NOCASE ORDER BY {order} LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def by_location(conn: sqlite3.Connection, location: str, *, limit: int = 500) -> list[Track]:
    """Files at one location (a drive label or a host name)."""
    loc = _location_expr()
    rows = conn.execute(
        f"SELECT * FROM files WHERE {loc}=? "
        f"ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, track_no LIMIT ?",
        (location, limit),
    ).fetchall()
    return [_row_to_track(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    loc = _location_expr()
    cur = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM files")
    total = cur.fetchone()
    per_host = conn.execute(
        f"SELECT {loc} host, COUNT(*) n, COALESCE(SUM(size),0) b "
        f"FROM files GROUP BY {loc} ORDER BY b DESC"
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

# Architecture

All logic lives in the `crate` library. The CLI (`crate/cli.py`) and the web app
(`crate/web/`) are thin frontends that call the same functions — nothing in a
frontend holds business logic. A new frontend (TUI, another service) should
import `crate.db`, `crate.scan`, `crate.volumes`, `crate.fingerprint` and do the
same.

## Data model

One SQLite file (`crate.db`) with two tables of interest: `files` and `volumes`.

### `files`

One row per playable audio file at one location. The same recording on several
drives/hosts yields several rows, related by `content_hash`.

Key columns:

| Group | Columns |
|-------|---------|
| location | `host`, `path`, `volume`, `size`, `mtime`, `ext`, `content_hash` |
| volume identity | `vol_id`, `relpath`, `vol_label` (set only for files on a registered drive) |
| tags | `title`, `artist`, `album`, `albumartist`, `track_no`, `disc_no`, `year`, `genre` |
| technical | `duration`, `bitrate`, `samplerate`, `channels` |
| identification | `fingerprint`, `acoustid`, `mb_recording_id` |
| search | `search_blob` (lowercased artist/album/title/label/relpath/path), `search_fold` (diacritic-stripped) |
| bookkeeping | `id`, `tags_json`, `first_seen`, `last_seen` |

### Keying — the important bit

A file's **identity** depends on where it lives:

- **Host-internal files**: keyed by `UNIQUE(host, path)`. `host` is the machine
  that scanned it, `path` is absolute.
- **Volume files**: keyed by a partial unique index `(vol_id, relpath) WHERE
  vol_id IS NOT NULL`. `vol_id` is the drive's stable id (from its marker file),
  `relpath` is relative to the drive root. `host`/`path` still record where the
  drive was *last seen* mounted, but they are **not** the identity.

This is why the same external drive scanned on two different machines produces
*one* row per file, not two: the mount path and host differ, but `(vol_id,
relpath)` is stable. `upsert()` picks the right `ON CONFLICT` target based on
whether `track.vol_id` is set. `merge()` routes every source row through
`upsert()` for the same reason (a single `INSERT … ON CONFLICT` can't target two
different constraints).

A file's **location** for display/grouping is `COALESCE(vol_label, host)` — a
drive label if it's on a volume, else the host. Exposed as `Track.location` and
used by `stats`, `find`, coverage, and the web UI.

### `volumes`

One row per registered removable drive. Identity is `vol_id` (a UUID crate wrote
to `.crate-volume.json` at the drive root). Also tracks `label`, `fs_uuid`,
`capacity_bytes`, `free_bytes`, and `last_host` / `last_mount` / `last_scanned`
/ `last_seen`. See `crate/volumes.py`.

### Schema migrations

`SCHEMA_VERSION` + additive migrations in `db._migrate` (`ALTER TABLE ADD
COLUMN`, never destructive). Indexes that reference migrated columns are created
in `_POST_MIGRATE`, *after* the columns are guaranteed to exist. The v1→v2
migration (removable volumes) and the `search_fold` addition both backfill
existing rows once, on first open with the new code.

## Search

`db.search`, `db.search_albums`, and `db.loose_matches` share `_term_clause`:
each whitespace token must appear in the row's text (AND-of-`LIKE %term%`).

Matching is **accent-insensitive**: `db.fold()` lowercases and strips diacritics
(NFKD + combining-mark removal, plus a map for non-decomposing Latin like
`ø ł æ ß`). Both the stored `search_fold` column and the query terms pass through
`fold()`, so `dulaman` matches *Dúlamán*. `search_fold` is kept current by
`upsert` and `update_identification`.

`search_albums` groups matches into `(artist, album)` rows with track count,
runtime, year, and the set of locations — the album-card view. Grouping is by the
exact `artist` tag, so featured-artist credits currently split (a known backlog
item: primary-artist grouping).

## Library API (for frontends)

Everything takes a `sqlite3.Connection` from `db.connect()`. `Track` and `Volume`
(in `crate/models.py`) are the currency.

```python
from crate import db, scan, volumes, fingerprint
conn = db.connect()                       # opens/migrates ~/.local/share/crate/crate.db
```

**Index (`crate.db`)**

| Function | Returns |
|----------|---------|
| `connect(path=None)` | migrated `Connection` |
| `upsert(conn, track)` | — (insert/refresh by the right key) |
| `search(conn, q, *, host=None, limit=200)` | `list[Track]` |
| `search_albums(conn, q, *, limit=200)` | `list[dict]` (artist, album, year, tracks, duration, locations) |
| `loose_matches(conn, q, *, limit=100)` | `list[Track]` (matches with no album) |
| `album_tracks(conn, artist, album, *, limit=500)` | `list[Track]` |
| `by_location(conn, location, *, limit=500)` | `list[Track]` |
| `top_artists(conn, *, sort="count_desc", limit=500)` | `list[dict]` (`sort`: az/za/count_asc/count_desc) |
| `untagged(conn, *, host=None, limit=200)` | `list[Track]` |
| `duplicates(conn, limit=200)` | `list[list[Track]]` (content-hash groups) |
| `coverage(conn)` / `at_risk(conn, *, limit=500)` | backup coverage summary / single-copy tracks |
| `stats(conn)` | totals, per-location, formats |
| `merge(conn, other_path)` | `{added, updated, total_source}` |
| `get_by_path` / `update_identification` | lookup / identify write-back |
| `fold(s)` | diacritic-folded string |

**Volumes (`crate.volumes`)**

`register(conn, mount, label)`, `scan_volume(conn, mount, *, do_hash=True,
progress=None)`, `status(conn) -> list[VolumeStatus]`, `discover_mounts()`,
`find_mount(vol_id)`, plus `db.list_volumes` / `db.get_volume` /
`db.volume_usage`.

**Scan (`crate.scan`)**

`scan(roots, conn=None, *, host=None, do_hash=True, progress=None) ->
ScanResult`; `iter_audio_files(roots)`; `quick_hash(path, size)`.

**Identify (`crate.fingerprint`)**

`identify(path, *, api_key=None, max_results=5) -> Identification` (`.best`,
`.matches`, `.fingerprint`, `.error`); `compute_fingerprint(path)`;
`fpcalc_available()`.

### Notes for frontends
- Not versioned/frozen — this is 0.x; signatures may shift.
- `search` is substring AND across artist/album/title/label/path. No field-scoped
  queries or ranking yet.
- `identify` blocks (decodes audio + network) — call it off any UI thread; its
  return is a plain dataclass, easy to hand across a thread boundary.
- `sqlite3` connections are not thread-safe — one connection per thread, or
  serialize DB access (the web app opens one per request).

## Web app

`crate/web/` — Flask app factory (`create_app`) with three blueprints:

- **`auth.py`** — GitHub OAuth (manual flow over `requests`), `login_required`,
  an allowlist (`MYSTIC_ALLOWED_USERS`), and a `MYSTIC_DEV_USER` bypass for local
  dev. Sign-in landing → `/login/github` → GitHub → `/auth/callback`.
- **`views.py`** — read-only pages (dashboard, search, album, location, artists,
  coverage). One `Connection` per request, closed on teardown.
- **`api.py`** — `POST /api/push` (Bearer `MYSTIC_PUSH_TOKEN`) merges an uploaded
  index; `GET /api/health`.

Runs behind Apache's reverse proxy; `ProxyFix` honors `X-Forwarded-*` so OAuth
builds `https` redirect URIs (the SSL vhost must set `X-Forwarded-Proto https`).
Deployment details in [`../deploy/README.md`](../deploy/README.md).

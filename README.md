# mystic-inventory

Inventory, browse, and search the playable music scattered across your machines
and external drives — from a CLI, and from a central web app. It answers questions
like:

- *"Find every Mouse on Mars song I own."* → `crate find mouse on mars`
- *"What is this weird mp3 with no metadata?"* → `crate identify mystery.mp3`
- *"What's on BigDrive?"* — even while the drive is unplugged in a drawer.
- *"What would I lose if this drive died?"* → `crate at-risk`

It builds a single portable SQLite index that spans every host and drive. The
same recording on two drives is related by content hash; a drive keeps its
identity when it roams between machines. The CLI command is **`crate`**; the web
app is **mystic** (live at [mystic.nthmost.net](https://mystic.nthmost.net)).

> **Repo vs. command:** the project/repo is `mystic-inventory`; the installed
> command and importable package are both `crate`.

## Concepts

| Term | Meaning |
|------|---------|
| **index** | One SQLite file (`crate.db`) — the whole catalog. Portable and mergeable. |
| **host** | A machine whose internal storage was scanned (rows keyed by `host` + absolute path). |
| **volume** | A registered removable drive with a stable id, tracked even when offline (rows keyed by `vol_id` + drive-relative path). |
| **location** | A host *or* a volume — how files are grouped in `stats`, `find`, coverage. |
| **identify** | On-demand acoustic fingerprint (Chromaprint) + AcoustID→MusicBrainz lookup. |

Everything is built on the `crate` Python library; the CLI and the web app are
thin frontends over it. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
data model and library API.

## Install

```sh
git clone https://github.com/nthmost/mystic-inventory.git
cd mystic-inventory
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # add '.[web]' for the web app
brew install chromaprint    # provides fpcalc, needed only for `identify`
```

Requires Python ≥ 3.10. `scan` needs only Python + `mutagen` (no network, no
fpcalc), so deploying to a host purely to index it is lightweight.

## CLI reference

```sh
crate scan ROOT...          # walk dirs, read tags, add/update files (host-internal)
crate find QUERY...         # accent-insensitive search; --paths for pipe-friendly output
crate identify FILE         # acoustically identify a mystery file (needs fpcalc + AcoustID key)
crate untagged              # files with no artist/title — candidates for identify
crate dupes                 # byte-identical files across locations
crate coverage              # backup coverage: protected (≥2 copies) vs at-risk (1)
crate at-risk               # files that live in exactly ONE location
crate stats                 # totals, per-location, formats
crate where                 # index path + environment (host, fpcalc, acoustid)
crate merge OTHER.db        # merge another host's index into this one
crate push [--server URL]   # upload this index to the central server, which merges it

crate volume register MOUNT --label NAME   # mark a drive as a crate volume (writes a marker)
crate volume scan MOUNT                     # index a registered drive (drive-relative paths)
crate volume status                         # ● online / ○ offline, usage, capacity, last seen
```

## Multi-host workflow

Each host builds its own index; roll them together with `merge` (or `push`, below).

```sh
# on any host: scan its music
crate scan /media/music-archive

# on your main machine: pull that host's db and merge it
scp beyla:~/.local/share/crate/crate.db /tmp/beyla-crate.db
crate merge /tmp/beyla-crate.db
```

`merge` dedups host-internal files by `(host, path)` and drive files by
`(vol_id, relpath)`, so hosts never collide and re-merging just refreshes rows.

## External drives (offline-aware)

A roaming external drive is a first-class **volume** with a stable identity, so
the index keeps answering *"what's on BigDrive?"* while it's unplugged.

Identity is a marker file (`.crate-volume.json`, a generated UUID + label) crate
writes once to the drive root — the drive is recognized on any machine, at any
mount path. Files on a volume are stored by drive-relative path.

```sh
crate volume register /Volumes/BigDrive --label BigDrive   # writes the marker (once)
crate volume scan /Volumes/BigDrive                        # index it
crate volume status                                        # online/offline + capacity
crate find boards of canada                                # shows [BigDrive] even when unplugged
```

If crate can't discover your drives (nonstandard mount point), set
`CRATE_MOUNT_ROOTS=/path/one:/path/two`.

### Backup coverage

With content hashing on (the default), crate knows which files exist in more than
one location — across hosts *and* drives:

```sh
crate coverage    # % protected (≥2 copies) vs at-risk (single copy)
crate at-risk     # the single-copy files — what you'd lose if that location died
```

## Web app (mystic.nthmost.net)

A Flask app (`crate/web/`) serves a **read-only** view over the central merged
index on zephyr:

- **Dashboard** — totals, per-location, drive online/offline, backup coverage.
- **Search** — accent-insensitive (`dulaman` finds *Dúlamán*), results grouped
  into **artist → album cards** (year, track count, runtime, which drives hold it).
- **Album** pages, **browse by location**, and a **sortable Artists** index
  (most↔fewest, A↔Z).
- **Coverage / at-risk** views.

Two auth paths:
- **Humans** sign in with **GitHub OAuth**, restricted to `MYSTIC_ALLOWED_USERS`
  (default `nthmost`).
- **Hosts** push their index to `POST /api/push` with a shared Bearer token
  (`MYSTIC_PUSH_TOKEN`) — machines can't do an interactive login.

### Sync: hosts push to the server

```sh
# one-time per host
echo "https://mystic.nthmost.net" > ~/.config/crate/server
echo "<push-token>"              > ~/.config/crate/push_token

crate scan ~/Music        # or: crate volume scan …
crate push                # upload; server merges by (host,path)/(vol_id,relpath)
```

### Running it

```sh
pip install -e '.[web]'
# dev (bypasses OAuth):
MYSTIC_DEV_USER=you CRATE_DB=~/.local/share/crate/crate.db \
  flask --app crate.web:create_app run
# prod: gunicorn 'crate.web:create_app()' behind Apache + certbot TLS
```

Full deployment recipe (systemd, Apache vhost, TLS, OAuth app): see
[`deploy/README.md`](deploy/README.md).

## Configuration

| What | How | Default |
|------|-----|---------|
| Index location | `CRATE_DB` | `~/.local/share/crate/crate.db` |
| Data dir | `CRATE_HOME` / `XDG_DATA_HOME` | `~/.local/share/crate` |
| Host name | `CRATE_HOST` | this machine's hostname |
| AcoustID key | `CRATE_ACOUSTID_KEY` or `~/.config/crate/acoustid_key` | — |
| Central server | `CRATE_SERVER` or `~/.config/crate/server` | — |
| Push token | `CRATE_PUSH_TOKEN` or `~/.config/crate/push_token` | — |
| Drive mount roots | `CRATE_MOUNT_ROOTS` (colon-sep) | `/Volumes`, `/media`, `/mnt`, … |

Web-only env: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `MYSTIC_ALLOWED_USERS`,
`MYSTIC_PUSH_TOKEN`, `MYSTIC_SECRET_KEY`, `MYSTIC_DEV_USER` (dev bypass).

An AcoustID **application** key (free, from <https://acoustid.org/new-application>
— note: not the account key) lets `identify` resolve names. Without one, `identify`
still computes the fingerprint but can't look up the title.

## Development

```sh
pip install -e '.[web]'
python -m unittest              # 22 stdlib tests, no network needed
```

Layout:

```
crate/            the library — all logic lives here
  db.py           SQLite index: schema, upsert, search, merge, coverage
  scan.py         filesystem walk → metadata → upsert
  meta.py         mutagen tag extraction (format-normalized)
  fingerprint.py  Chromaprint + AcoustID (the `identify` path)
  volumes.py      removable-drive identity, discovery, scanning
  models.py       Track, Volume dataclasses
  config.py       env/file-resolved settings
  cli.py          the `crate` command (thin wrapper over the library)
  web/            the Flask app (app factory, auth, views, api, templates)
deploy/           systemd unit, Apache vhost, deployment README
docs/             ARCHITECTURE.md
tests/            stdlib unittest
```

## Roadmap

- [x] Core library + CLI: scan, find, identify, untagged, dupes, stats
- [x] Multi-host index merge (`crate merge`)
- [x] Removable volumes: offline-aware inventory, capacity/health (`crate volume …`)
- [x] Backup coverage: `crate coverage`, `crate at-risk`
- [x] Centralized web app + `crate push` sync (GitHub OAuth, read-only browse)
- [x] Deployed to mystic.nthmost.net (systemd, Apache, TLS)
- [x] Grouped album search, sortable artists, accent-insensitive search
- [ ] Primary-artist grouping (fold featured-artist credits into the lead)
- [ ] Sort/filter within results (year, runtime, location); filter to one drive
- [ ] Album/artist browse index (alphabetized wall, not just via search)
- [ ] Automated pushes (timer so the central index stays fresh)
- [ ] Move/copy planning between drives (consolidate, fill, dedup) — file ops, opt-in
- [ ] Write-back retagging (apply an identified match to the file's tags)
- [ ] `crate pull <host>` — one-shot remote scan + fetch + merge

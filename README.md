# crate

Inventory and browse the playable music files scattered across your machines and
drives, then answer questions like:

- *"Find every Mouse on Mars song on this drive."* → `crate find mouse on mars`
- *"What is this weird mp3 with no metadata?"* → `crate identify mystery.mp3`

`crate` builds a single portable SQLite index that is **multi-host aware** — every
file knows which host and volume it lives on — so one index can span this Mac,
loki, zephyr, and external drives.

## How it works

- **`scan`** walks directories, reads tags (`mutagen`, all common formats), and
  records each file with its host + volume. Fast and offline.
- **`identify`** is the "what *is* this?" path — it acoustically fingerprints one
  file with Chromaprint (`fpcalc`) and looks it up against AcoustID → MusicBrainz.
  Only ever run on a file you point at, never during a bulk scan.

The logic lives in the `crate` library; the CLI is a thin wrapper. A TUI
(Textual) is planned as a second frontend over the same core.

## Install

```sh
cd ~/projects/crate
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
brew install chromaprint      # provides fpcalc, needed for `identify`
```

## Usage

```sh
crate scan ~/Music /Volumes/BigDrive     # index one or more roots
crate find mouse on mars                 # multi-term, case-insensitive search
crate find --paths boards of canada      # just paths, for piping
crate identify /Volumes/BigDrive/weird.mp3
crate untagged                           # files with no artist/title
crate dupes                              # byte-identical files across locations
crate stats                              # library overview
crate where                              # index path + environment
```

## Multi-host workflow

Each host builds its own index (rows tagged with that host), then you roll them
into one master index with `crate merge`:

```sh
# on beyla (or any host): scan its music
crate scan /media/music-archive

# back on your main machine: pull that host's db and merge it in
scp beyla:~/.local/share/crate/crate.db /tmp/beyla-crate.db
crate merge /tmp/beyla-crate.db
```

`merge` is keyed by (host, path): hosts never collide, and re-merging an updated
index refreshes matching rows in place. After merging you can `find`, `stats`,
and `dupes` across every host from one index — including byte-identical files
that live on more than one machine.

Note: `scan` needs only Python + `mutagen` (no network, no fpcalc), so deploying
crate to a host just to index it is lightweight; `fpcalc`/AcoustID are only
needed where you run `identify`.

## Configuration

| What | How |
|------|-----|
| Index location | `CRATE_DB` (default `~/.local/share/crate/crate.db`) |
| Host name | `CRATE_HOST` (default: this machine's hostname) |
| AcoustID key | `CRATE_ACOUSTID_KEY`, or `~/.config/crate/acoustid_key` |

An AcoustID application key (free, from <https://acoustid.org/api-key>) is needed
for `identify` to resolve names. Without one, `identify` still computes the
fingerprint (a stable content id) but can't look up the title.

## Roadmap

- [x] Core library + CLI: scan, find, identify, untagged, dupes, stats
- [x] Index merge across hosts (`crate merge other.db`)
- [ ] TUI file browser (Textual): navigate, preview, batch-identify, retag
- [ ] Write-back retagging (apply an identified match to the file's tags)
- [ ] `crate pull <host>` — one-shot remote scan + fetch + merge

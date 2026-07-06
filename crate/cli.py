"""crate command-line interface — a thin frontend over the crate library.

Commands map directly to library functions so a future TUI/desktop app can call
the same code paths. Nothing here holds business logic.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import click

from . import __version__, db, volumes
from .config import acoustid_key, db_path, hostname
from .fingerprint import fpcalc_available, identify as fp_identify
from .scan import ScanResult, scan as run_scan


def _human_size(n: int | None) -> str:
    if not n:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def _human_dur(sec: float | None) -> str:
    if not sec:
        return "—"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 90:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{int(delta // size)}{unit} ago"
    return "just now"


@click.group(help="Inventory and browse playable music across hosts and drives.")
@click.version_option(__version__, prog_name="crate")
def cli() -> None:
    pass


@cli.command()
@click.argument("roots", nargs=-1, required=True,
                type=click.Path(exists=True, path_type=Path))
@click.option("--host", default=None, help="Record under this host name (default: this machine).")
@click.option("--no-hash", is_flag=True, help="Skip content hashing (faster, no dedupe).")
@click.option("-q", "--quiet", is_flag=True, help="No per-file progress.")
def scan(roots: tuple[Path, ...], host: str | None, no_hash: bool, quiet: bool) -> None:
    """Walk ROOTS, read tags, and add/update files in the index."""
    conn = db.connect()
    start = time.time()
    last = [0.0]

    def progress(path: Path, res: ScanResult) -> None:
        if quiet:
            return
        now = time.time()
        if now - last[0] > 0.1:
            last[0] = now
            click.echo(f"\r  {res.scanned:>6} scanned, {res.added:>6} indexed  "
                       f"{str(path)[-50:]:<50}", nl=False, err=True)

    res = run_scan(list(roots), conn, host=host, do_hash=not no_hash, progress=progress)
    conn.close()
    if not quiet:
        click.echo("\r" + " " * 80 + "\r", nl=False, err=True)
    dt = time.time() - start
    click.echo(
        f"Indexed {res.added} files under host '{host or hostname()}' in {dt:.1f}s "
        f"({res.scanned} seen, {res.skipped_nonaudio} non-audio, {res.errors} errors)."
    )


@cli.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--host", default=None, help="Limit to one host.")
@click.option("-l", "--limit", default=100, show_default=True)
@click.option("--paths", is_flag=True, help="Print absolute paths only (scriptable).")
def find(query: tuple[str, ...], host: str | None, limit: int, paths: bool) -> None:
    """Search the index. e.g. crate find mouse on mars"""
    conn = db.connect()
    results = db.search(conn, " ".join(query), host=host, limit=limit)
    conn.close()
    if not results:
        click.echo("No matches.", err=True)
        sys.exit(1)
    if paths:
        for t in results:
            click.echo(t.path)
        return
    for t in results:
        loc = click.style(f"[{t.location}]", fg="cyan")
        dur = click.style(_human_dur(t.duration), fg="black")
        tag = "" if not t.is_untagged else click.style(" (untagged)", fg="yellow")
        click.echo(f"{loc} {t.display}{tag}  {dur}")
        click.echo(click.style(f"      {t.path}", fg="bright_black"))
    click.echo(err=True, message=f"\n{len(results)} match(es).")


@cli.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option("--host", default=None, help="Host to write results back to (default: this machine).")
@click.option("--save/--no-save", default=True, help="Write the match back into the index if present.")
def identify(target: Path, host: str | None, save: bool) -> None:
    """Acoustically identify a mystery file (Chromaprint + AcoustID → MusicBrainz)."""
    if not fpcalc_available():
        click.echo("fpcalc (chromaprint) not on PATH. `brew install chromaprint`.", err=True)
        sys.exit(2)
    click.echo(f"Fingerprinting {target.name} …", err=True)
    ident = fp_identify(target)

    if ident.fingerprint:
        click.echo(f"  duration: {_human_dur(ident.duration)}  "
                   f"fingerprint: {ident.fingerprint[:32]}…")
    if ident.error:
        click.echo(click.style(f"  note: {ident.error}", fg="yellow"), err=True)

    if not ident.matches:
        click.echo("No acoustic match found.")
        if not acoustid_key():
            click.echo("Set an AcoustID key (CRATE_ACOUSTID_KEY) to enable lookups.", err=True)
        return

    click.echo("\nBest matches:")
    for i, m in enumerate(ident.matches, 1):
        bar = click.style(f"{m.score:.0%}", fg="green" if m.score > 0.8 else "yellow")
        label = f"{m.artist or '?'} — {m.title or '?'}"
        album = f"  [{m.album}]" if m.album else ""
        click.echo(f"  {i}. {bar}  {label}{album}")
        if m.recording_id:
            click.echo(click.style(f"       musicbrainz: {m.recording_id}", fg="bright_black"))

    if save:
        conn = db.connect()
        t = db.get_by_path(conn, host or hostname(), str(target.resolve()))
        best = ident.best
        if t and t.id and best:
            db.update_identification(
                conn, t.id,
                fingerprint=ident.fingerprint,
                acoustid=None,
                mb_recording_id=best.recording_id,
                title=best.title, artist=best.artist, album=best.album,
            )
            click.echo(click.style("  ✓ saved match into index", fg="green"), err=True)
        conn.close()


@cli.command()
@click.option("--host", default=None)
@click.option("-l", "--limit", default=100, show_default=True)
def untagged(host: str | None, limit: int) -> None:
    """List files with no artist/title — candidates for `identify`."""
    conn = db.connect()
    rows = db.untagged(conn, host=host, limit=limit)
    conn.close()
    for t in rows:
        click.echo(f"[{t.location}] {t.path}")
    click.echo(err=True, message=f"\n{len(rows)} untagged file(s).")


@cli.command()
def dupes() -> None:
    """Show files that appear byte-identical on ≥2 locations."""
    conn = db.connect()
    groups = db.duplicates(conn)
    conn.close()
    if not groups:
        click.echo("No duplicates found (by content hash).")
        return
    for g in groups:
        click.echo(click.style(g[0].display, bold=True))
        for t in g:
            click.echo(f"  [{t.host}] {t.path}")


@cli.command()
def stats() -> None:
    """Summarize the index."""
    conn = db.connect()
    s = db.stats(conn)
    conn.close()
    click.echo(f"Index: {db_path()}")
    click.echo(f"Total: {s['total_files']} files, {_human_size(s['total_bytes'])}, "
               f"{s['untagged']} untagged")
    if s["per_host"]:
        click.echo("\nBy host:")
        for h in s["per_host"]:
            click.echo(f"  {h['host']:<16} {h['n']:>7} files  {_human_size(h['b'])}")
    if s["by_ext"]:
        click.echo("\nBy format:")
        for e in s["by_ext"][:12]:
            click.echo(f"  {e['ext'] or '(none)':<8} {e['n']:>7}")


@cli.command()
@click.argument("other", type=click.Path(exists=True, path_type=Path))
def merge(other: Path) -> None:
    """Merge another host's crate.db into this index (multi-host rollup)."""
    conn = db.connect()
    try:
        r = db.merge(conn, other)
    except Exception as e:
        click.echo(f"merge failed: {e}", err=True)
        sys.exit(1)
    finally:
        conn.close()
    click.echo(f"Merged {other}: {r['added']} new, {r['updated']} updated "
               f"({r['total_source']} rows in source).")


@cli.group()
def volume() -> None:
    """Manage removable drives (offline-aware inventory)."""


@volume.command("register")
@click.argument("mount", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--label", default=None, help="Human name for the drive (default: mount name).")
def volume_register(mount: Path, label: str | None) -> None:
    """Mark a mounted drive as a crate volume (writes a small marker file)."""
    conn = db.connect()
    try:
        vol = volumes.register(conn, mount, label or mount.name)
    finally:
        conn.close()
    cap = f"{vol.capacity_bytes/1e9:.0f} GB" if vol.capacity_bytes else "?"
    click.echo(f"Registered '{vol.label}' ({cap}) — id {vol.vol_id}")
    click.echo(f"Marker written to {mount}/{volumes.MARKER}")


@volume.command("scan")
@click.argument("mount", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--no-hash", is_flag=True, help="Skip content hashing (faster, no dedupe/coverage).")
@click.option("-q", "--quiet", is_flag=True)
def volume_scan(mount: Path, no_hash: bool, quiet: bool) -> None:
    """Index every audio file on a registered drive (paths stored drive-relative)."""
    conn = db.connect()
    last = [0.0]

    def progress(path: Path, res: ScanResult) -> None:
        if quiet:
            return
        now = time.time()
        if now - last[0] > 0.1:
            last[0] = now
            click.echo(f"\r  {res.scanned:>6} scanned, {res.added:>6} indexed  "
                       f"{str(path)[-46:]:<46}", nl=False, err=True)

    try:
        vol, res = volumes.scan_volume(conn, mount, do_hash=not no_hash, progress=progress)
    except ValueError as e:
        click.echo(f"\n{e}", err=True)
        conn.close()
        sys.exit(2)
    conn.close()
    if not quiet:
        click.echo("\r" + " " * 80 + "\r", nl=False, err=True)
    click.echo(f"Indexed {res.added} files on '{vol.label}' "
               f"({res.scanned} seen, {res.skipped_nonaudio} non-audio, {res.errors} errors).")


@volume.command("status")
def volume_status() -> None:
    """List registered drives: online/offline, usage, capacity, last seen."""
    conn = db.connect()
    rows = volumes.status(conn)
    conn.close()
    if not rows:
        click.echo("No registered volumes yet. `crate volume register <mount>`.")
        return
    for s in rows:
        v = s.volume
        dot = click.style("●", fg="green") if s.online else click.style("○", fg="bright_black")
        state = click.style("online", fg="green") if s.online else click.style("offline", fg="yellow")
        cap = f"{v.capacity_bytes/1e9:.0f}GB" if v.capacity_bytes else "?"
        free = f"{v.free_bytes/1e9:.0f}GB free" if v.free_bytes else ""
        pct = ""
        if v.capacity_bytes and v.free_bytes is not None:
            used = 100 * (v.capacity_bytes - v.free_bytes) / v.capacity_bytes
            pct = click.style(f" {used:.0f}% full", fg="red" if used > 90 else "black")
        click.echo(f"{dot} {click.style(v.label, bold=True):<24} {state}  "
                   f"{s.files} files, {_human_size(s.bytes)} indexed  [{cap} {free}{pct}]")
        seen = _ago(v.last_seen)
        scanned = _ago(v.last_scanned)
        where = f" at {s.mount}" if s.online else (f" last at {v.last_host}:{v.last_mount}" if v.last_mount else "")
        click.echo(click.style(f"    seen {seen}, scanned {scanned}{where}", fg="bright_black"))


@cli.command()
def coverage() -> None:
    """Backup coverage: what's protected (≥2 locations) vs at-risk (1 location)."""
    conn = db.connect()
    c = db.coverage(conn)
    conn.close()
    total = c["distinct_content"]
    if not total:
        click.echo("No content hashes yet — scan with hashing enabled.")
        return
    pct = 100 * c["protected"] / total if total else 0
    click.echo(f"Distinct content: {total}")
    click.echo(click.style(f"  protected (≥2 copies): {c['protected']} ({pct:.0f}%)", fg="green"))
    click.echo(click.style(f"  at risk   (1 copy):    {c['at_risk']}", fg="yellow"))
    dist = ", ".join(f"{k}×: {v}" for k, v in sorted(c["by_copies"].items()))
    click.echo(f"  copies distribution: {dist}")


@cli.command("at-risk")
@click.option("-l", "--limit", default=100, show_default=True)
@click.option("--paths", is_flag=True, help="Print paths only.")
def at_risk(limit: int, paths: bool) -> None:
    """List files that exist in only ONE location (no backup copy)."""
    conn = db.connect()
    rows = db.at_risk(conn, limit=limit)
    conn.close()
    if paths:
        for t in rows:
            click.echo(t.path)
        return
    for t in rows:
        click.echo(f"[{click.style(t.location, fg='cyan')}] {t.display}")
        click.echo(click.style(f"      {t.path}", fg="bright_black"))
    click.echo(err=True, message=f"\n{len(rows)} at-risk file(s) shown.")


@cli.command(name="where")
def where() -> None:
    """Print the index location and environment."""
    click.echo(f"db:       {db_path()}")
    click.echo(f"host:     {hostname()}")
    click.echo(f"fpcalc:   {'yes' if fpcalc_available() else 'no'}")
    click.echo(f"acoustid: {'configured' if acoustid_key() else 'not set'}")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

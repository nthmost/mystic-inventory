"""Runtime configuration: DB location, host identity, AcoustID key.

Everything here is resolved lazily so the library stays import-cheap and the
CLI/TUI/desktop frontends can all share the same defaults.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

# Audio file extensions we treat as "playable music". Kept deliberately broad;
# the scanner still confirms each file is a real audio file via mutagen.
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus",
    ".wav", ".aif", ".aiff", ".wma", ".ape", ".wv", ".mpc", ".m4b",
    ".dsf", ".dff",
}


def data_dir() -> Path:
    """Directory where crate keeps its index and cache. Override with CRATE_HOME."""
    override = os.environ.get("CRATE_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "crate"


def db_path() -> Path:
    """Path to the SQLite index. Override with CRATE_DB."""
    override = os.environ.get("CRATE_DB")
    if override:
        return Path(override).expanduser()
    return data_dir() / "crate.db"


def hostname() -> str:
    """Identity of the machine a scan is recorded against. Override with CRATE_HOST."""
    return os.environ.get("CRATE_HOST") or socket.gethostname().split(".")[0]


def acoustid_key() -> str | None:
    """AcoustID application API key for MusicBrainz lookups (fingerprinting).

    Resolution order: CRATE_ACOUSTID_KEY env, then ~/.config/crate/acoustid_key,
    then the shared nthmost-systems secrets drop. Returns None if unset — in that
    case `identify` can still compute a fingerprint, just not look it up.
    """
    env = os.environ.get("CRATE_ACOUSTID_KEY")
    if env:
        return env.strip()
    candidates = [
        Path.home() / ".config" / "crate" / "acoustid_key",
        Path.home() / "projects" / "nthmost-systems" / ".secrets" / "acoustid_key",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c.read_text().strip() or None
        except OSError:
            continue
    return None

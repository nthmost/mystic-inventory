"""Machine-facing API: hosts push their local index here to be merged.

Auth is a shared Bearer token (MYSTIC_PUSH_TOKEN), not the human OAuth flow —
a host running `crate push` can't do an interactive login.
"""

from __future__ import annotations

import hmac
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import db

bp = Blueprint("api", __name__)


def _authorized() -> bool:
    expected = current_app.config.get("PUSH_TOKEN")
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    return bool(token) and hmac.compare_digest(token, expected)


@bp.route("/api/push", methods=["POST"])
def push():
    if not _authorized():
        return jsonify(error="unauthorized"), 401
    upload = request.files.get("index")
    if upload is None:
        return jsonify(error="missing 'index' file"), 400
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        upload.save(tmp.name)
        tmp_path = tmp.name
    try:
        conn = db.connect()
        try:
            result = db.merge(conn, tmp_path)
        finally:
            conn.close()
    except Exception as e:
        return jsonify(error=f"merge failed: {e}"), 400
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return jsonify(ok=True, **result)


@bp.route("/api/health")
def health():
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
    finally:
        conn.close()
    return jsonify(ok=True, files=n, version=current_app.config["VERSION"])

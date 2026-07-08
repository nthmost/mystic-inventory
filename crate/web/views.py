"""Read-only browse + dashboard views over the merged crate index."""

from __future__ import annotations

from flask import Blueprint, render_template, request, g

from .. import db, volumes
from ..config import db_path
from .auth import login_required, current_user

bp = Blueprint("views", __name__)


def get_conn():
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@bp.teardown_app_request
def _close(_exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


# ---- template helpers exposed to Jinja ------------------------------------

@bp.app_template_filter("humansize")
def humansize(n):
    if not n:
        return "0 B"
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024


@bp.app_template_filter("dur")
def dur(sec):
    if not sec:
        return "—"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@bp.app_context_processor
def inject_user():
    return {"current_user": current_user()}


# ---- pages ----------------------------------------------------------------

@bp.route("/")
@login_required
def dashboard():
    conn = get_conn()
    s = db.stats(conn)
    cov = db.coverage(conn)
    vols = volumes.status(conn)
    playtime = conn.execute(
        "SELECT COALESCE(SUM(duration),0) t FROM files WHERE duration>0"
    ).fetchone()["t"]
    return render_template("dashboard.html", stats=s, coverage=cov,
                           volumes=vols, playtime_hours=playtime / 3600)


@bp.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    results = db.search(get_conn(), q, limit=300) if q else []
    return render_template("search.html", q=q, results=results)


@bp.route("/location/<path:location>")
@login_required
def location(location):
    tracks = db.by_location(get_conn(), location, limit=1000)
    return render_template("location.html", location=location, tracks=tracks)


@bp.route("/artists")
@login_required
def artists():
    return render_template("artists.html", artists=db.top_artists(get_conn(), 300))


@bp.route("/coverage")
@login_required
def coverage():
    conn = get_conn()
    return render_template("coverage.html",
                           coverage=db.coverage(conn),
                           at_risk=db.at_risk(conn, limit=500))

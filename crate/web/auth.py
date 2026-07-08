"""GitHub OAuth login with an account allowlist.

Manual OAuth2 flow (no extra dependency beyond requests): redirect to GitHub,
exchange the code for a token, fetch the user, and only admit logins whose
GitHub username is in MYSTIC_ALLOWED_USERS. A dev bypass (MYSTIC_DEV_USER) lets
the app run locally without registering an OAuth app.
"""

from __future__ import annotations

import functools
import os
import secrets
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint, current_app, redirect, render_template, request, session,
    url_for, abort,
)

bp = Blueprint("auth", __name__)

_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN = "https://github.com/login/oauth/access_token"
_USER = "https://api.github.com/user"


def allowed_users() -> set[str]:
    raw = os.environ.get("MYSTIC_ALLOWED_USERS", "nthmost")
    return {u.strip().lower() for u in raw.split(",") if u.strip()}


def current_user() -> str | None:
    dev = os.environ.get("MYSTIC_DEV_USER")
    if dev:
        return dev
    return session.get("user")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            session["after_login"] = request.path
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


@bp.route("/login")
def login():
    """Landing page with the GitHub sign-in button."""
    if current_user():
        return redirect(url_for("views.dashboard"))
    return render_template("login.html")


@bp.route("/login/github")
def authorize():
    """Kick off the GitHub OAuth redirect."""
    if current_user():
        return redirect(url_for("views.dashboard"))
    client_id = current_app.config.get("GITHUB_CLIENT_ID")
    if not client_id:
        abort(500, "GitHub OAuth not configured (GITHUB_CLIENT_ID unset).")
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    session.setdefault("after_login", "/")
    params = {
        "client_id": client_id,
        "redirect_uri": url_for("auth.callback", _external=True),
        "scope": "read:user",
        "state": state,
        "allow_signup": "false",
    }
    return redirect(f"{_AUTHORIZE}?{urlencode(params)}")


@bp.route("/auth/callback")
def callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400, "OAuth state mismatch.")
    code = request.args.get("code")
    if not code:
        abort(400, "Missing OAuth code.")
    resp = requests.post(
        _TOKEN,
        headers={"Accept": "application/json"},
        data={
            "client_id": current_app.config["GITHUB_CLIENT_ID"],
            "client_secret": current_app.config["GITHUB_CLIENT_SECRET"],
            "code": code,
            "redirect_uri": url_for("auth.callback", _external=True),
        },
        timeout=10,
    )
    token = resp.json().get("access_token")
    if not token:
        abort(403, "GitHub did not return an access token.")
    who = requests.get(
        _USER,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=10,
    ).json()
    login_name = (who.get("login") or "").lower()
    if login_name not in allowed_users():
        abort(403, f"'{login_name}' is not authorized for this instance.")
    session["user"] = who.get("login")
    dest = session.pop("after_login", "/")
    return redirect(dest if dest.startswith("/") else "/")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

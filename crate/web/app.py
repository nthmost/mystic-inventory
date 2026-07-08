"""Flask application factory for the mystic web view."""

from __future__ import annotations

import os
import secrets

from flask import Flask

from .. import __version__


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("MYSTIC_SECRET_KEY") or secrets.token_hex(32),
        GITHUB_CLIENT_ID=os.environ.get("GITHUB_CLIENT_ID"),
        GITHUB_CLIENT_SECRET=os.environ.get("GITHUB_CLIENT_SECRET"),
        PUSH_TOKEN=os.environ.get("MYSTIC_PUSH_TOKEN"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024 * 1024,  # 2 GB uploaded index cap
        VERSION=__version__,
    )
    # Behind Apache's reverse proxy, honor X-Forwarded-* so redirect URIs are https.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from .auth import bp as auth_bp
    from .views import bp as views_bp
    from .api import bp as api_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp)
    return app

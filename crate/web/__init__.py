"""mystic — the centralized web view over a merged crate index.

A thin Flask layer on top of the same `crate` library the CLI uses. Humans
browse (read-only) after a GitHub OAuth login restricted to an allowlist;
machines push their local index to /api/push with a Bearer token.
"""

from .app import create_app

__all__ = ["create_app"]

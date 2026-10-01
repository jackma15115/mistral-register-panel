"""Shared normalization for SSO cookie values."""

from __future__ import annotations


def normalize_sso_token(value: object) -> str:
    """Remove supported wrappers and one known spurious leading hyphen."""
    token = str(value or "").strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    if token.startswith("-"):
        token = token[1:].strip()
    return token

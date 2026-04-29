"""Helpers for uploaded files (avatars, signatures, receipts).

On ephemeral hosts (e.g. Render free tier), DB paths may remain while files are gone
after redeploy — avoid emitting `.url` when the backing file does not exist.
"""

from __future__ import annotations

from typing import Any


def file_url_if_exists(fieldfile: Any) -> str:
    """Return ``FieldFile.url`` only if storage still has the object."""
    if fieldfile is None:
        return ""
    name = getattr(fieldfile, "name", None) or ""
    if not name:
        return ""
    storage = getattr(fieldfile, "storage", None)
    if storage is None:
        return ""
    try:
        if storage.exists(name):
            return fieldfile.url
    except Exception:
        return ""
    return ""

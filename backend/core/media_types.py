"""File extensions accepted for uploaded media.

Uploads keep the client's extension on disk, so an unrestricted extension lets
a caller store arbitrary file types (configuration files, scripts) in the data
directory. Media uploads accept only these containers; ffmpeg still verifies
the content.
"""

from __future__ import annotations

import os

AUDIO_EXTS = frozenset({
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma",
    ".aif", ".aiff", ".caf", ".amr", ".weba",
})
VIDEO_EXTS = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg",
    ".mpeg", ".mpe", ".ts", ".mts", ".m2ts", ".3gp", ".3g2", ".ogv", ".vob",
})
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


def media_extension(filename: str | None, allowed: frozenset[str], default: str) -> str | None:
    """The lower-cased extension of ``filename`` if allowed, else None.

    A missing filename uses ``default``; a filename without an extension is
    refused, so the stored name never depends on unvalidated input.
    """
    if not filename:
        return default
    ext = os.path.splitext(os.path.basename(filename.replace("\\", "/")))[1].lower()
    return ext if ext in allowed else None


def unsupported_media_detail(kind: str, allowed: frozenset[str], ext: str) -> str:
    return (
        f"Unsupported {kind} file type '{ext or 'no extension'}'. "
        f"Use one of: {', '.join(sorted(allowed))}."
    )

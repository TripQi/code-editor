from __future__ import annotations

import mimetypes
from pathlib import Path

# Extend common missing types
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/markdown", ".mdx")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("application/typescript", ".ts")
mimetypes.add_type("application/typescript", ".tsx")


def get_mime_type(file_path: str | Path) -> str:
    mime, _ = mimetypes.guess_type(str(file_path))
    return mime or "text/plain"


def is_image_file(mime_type: str) -> bool:
    return mime_type.lower().startswith("image/")

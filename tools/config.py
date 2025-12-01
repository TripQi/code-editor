from __future__ import annotations

import os
from pathlib import Path
import json
from typing import List

# Default limits and timeouts (milliseconds for parity with original spec converted to seconds when used)
FILE_OPERATION_TIMEOUTS = {
    "PATH_VALIDATION": 10.0,  # seconds
    "URL_FETCH": 30.0,
    "FILE_READ": 30.0,
}

FILE_SIZE_LIMITS = {
    "LARGE_FILE_THRESHOLD": 10 * 1024 * 1024,  # 10MB
    "LINE_COUNT_LIMIT": 10 * 1024 * 1024,      # 10MB for counting lines
}

READ_PERFORMANCE_THRESHOLDS = {
    "SMALL_READ_THRESHOLD": 100,
    "DEEP_OFFSET_THRESHOLD": 1000,
    "SAMPLE_SIZE": 10_000,
    "CHUNK_SIZE": 8192,
}

DEFAULT_FILE_READ_LINE_LIMIT = 1000
DEFAULT_FILE_WRITE_LINE_LIMIT = 50
DEFAULT_IGNORE_PATTERNS = [
    ".git",
    "__pycache__",
    "node_modules",
    ".DS_Store",
    ".env",
    ".env.*",
    ".venv",
    "*.log",
    "*.pem",
]
ALLOWED_ROOTS_FILE = Path(
    os.environ.get(
        "CODE_EDIT_ALLOWED_ROOTS_FILE",
        Path(__file__).resolve().parent / ".code_edit_roots.json",
    )
)


def get_root() -> Path:
    """
    Active root marker (used for safety checks like delete_directory).
    Paths must already be absolute; this is not used for resolving relative paths.
    """
    return Path(os.environ.get("CODE_EDIT_ROOT", Path.cwd())).expanduser().resolve()


def _load_env_allowed_roots() -> List[Path]:
    """
    Support both legacy CODE_EDIT_ALLOWED_ROOTS and new CODE_EDIT_ALLOWED_DIRECTORIES
    environment variables.
    """
    raw = os.environ.get("CODE_EDIT_ALLOWED_DIRECTORIES") or os.environ.get(
        "CODE_EDIT_ALLOWED_ROOTS"
    )
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [Path(p).expanduser().resolve() for p in parts]


def _load_file_allowed_roots() -> List[Path]:
    if not ALLOWED_ROOTS_FILE.exists():
        return []
    try:
        data = json.loads(ALLOWED_ROOTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        roots: List[Path] = []
        for item in data:
            if isinstance(item, str) and item.strip():
                roots.append(Path(item).expanduser().resolve())
        return roots
    except Exception:
        return []


def get_allowed_roots() -> List[Path]:
    """
    Return the directories allowed for file access.

    Semantics match desktop-commander's allowedDirectories:
    - Default to the user's home directory when nothing is configured (and persist it).
    - If the list is empty or contains a filesystem root ("/" on POSIX), treat as unrestricted.
    """
    roots = set(_load_env_allowed_roots() + _load_file_allowed_roots())

    # Default to home if nothing configured
    if not roots:
        default_home = Path.home().expanduser().resolve()
        roots.add(default_home)
        save_allowed_roots([default_home])

    return sorted(roots)


def save_allowed_roots(roots: List[Path]) -> None:
    normalized = sorted({Path(p).expanduser().resolve() for p in roots})
    ALLOWED_ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALLOWED_ROOTS_FILE.write_text(
        json.dumps([str(p) for p in normalized], indent=2),
        encoding="utf-8",
    )


def list_allowed_roots() -> List[Path]:
    return get_allowed_roots()


def get_file_read_line_limit() -> int:
    raw = os.environ.get("CODE_EDIT_FILE_READ_LINE_LIMIT")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_FILE_READ_LINE_LIMIT


def get_file_write_line_limit() -> int:
    raw = os.environ.get("CODE_EDIT_FILE_WRITE_LINE_LIMIT")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_FILE_WRITE_LINE_LIMIT

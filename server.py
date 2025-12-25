from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import List

from mcp.server.fastmcp import FastMCP
from tools import edit as edit_tools
from tools import filesystem as fs_tools
from tools.config import DEFAULT_IGNORE_PATTERNS, FILE_SIZE_LIMITS, get_root

logging.basicConfig(level=logging.INFO)

MTIME_EPSILON_NS = 10_000_000  # 10ms tolerance

server = FastMCP("code-editor")
ROOT = get_root()
CRITICAL_PATHS = {
    Path("/"),
    Path("/home"),
    Path("/root"),
    Path("/Users"),
    Path("C:\\"),
}


# --- Helpers --------------------------------------------------------------

def _validate_path(path: str) -> Path:
    return fs_tools.validate_path(path)


def _check_expected_mtime(resolved: Path, expected_mtime: float | None) -> None:
    if expected_mtime is None:
        return
    if not resolved.exists():
        raise FileNotFoundError(f"File not found for mtime check: {resolved}")
    expected_ns = _normalize_expected_mtime(expected_mtime)
    current_ns = _current_mtime_ns(resolved)
    if expected_ns is not None and abs(current_ns - expected_ns) > MTIME_EPSILON_NS:
        raise RuntimeError(
            f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current_ns / 1_000_000_000:.9f}."
        )


def _read_lines(file_path: Path, encoding: str) -> List[str]:
    return file_path.read_text(encoding=encoding).splitlines(keepends=True)


def _write_text(file_path: Path, content: str, encoding: str) -> None:
    fs_tools._atomic_write(file_path, content, encoding=encoding)


def _read_text(file_path: Path, encoding: str) -> str:
    return file_path.read_text(encoding=encoding)


def _normalize_encoding(encoding: str | None) -> str | None:
    if encoding is None:
        return None
    normalized = encoding.strip()
    if normalized == "" or normalized.lower() == "auto":
        return None
    return fs_tools.normalize_encoding(normalized)


def _normalize_encoding_required(encoding: str | None, default: str = "utf-8") -> str:
    """
    For tool handlers that require a concrete encoding, fallback to default when None/""/auto.
    """
    if encoding is None:
        return fs_tools.normalize_encoding(default)
    normalized = encoding.strip()
    if normalized == "" or normalized.lower() == "auto":
        return fs_tools.normalize_encoding(default)
    return fs_tools.normalize_encoding(normalized)


def _normalize_expected_mtime(expected: float | int | None) -> int | None:
    if expected is None:
        return None
    if expected > 1e12:  # assume nanoseconds
        return int(expected)
    return int(expected * 1_000_000_000)


def _current_mtime_ns(path: Path) -> int:
    stats = path.stat()
    return getattr(stats, "st_mtime_ns", int(stats.st_mtime * 1_000_000_000))


def _index_to_line_col(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    column = index - last_newline
    return line, column


def _normalize_ignore_patterns(patterns: List[str] | str | None) -> List[str]:
    """
    Normalize user-supplied ignore patterns.

    Rules:
    - None: fall back to defaults.
    - Empty string/list: disable defaults entirely (show everything).
    - Non-string entries: reject.
    """
    if patterns is None:
        return list(DEFAULT_IGNORE_PATTERNS)
    if isinstance(patterns, str):
        cleaned = [p.strip() for p in patterns.split(",") if p.strip()]
        return cleaned  # empty string means no ignores
    items = list(patterns)
    if any(not isinstance(p, str) for p in items):
        raise ValueError("ignore_patterns elements must all be strings.")
    return items  # empty list => show all files


def _delete_confirm_token(resolved: Path) -> str:
    token_path = os.path.normcase(str(resolved))
    return f"delete:{token_path}"


def build_delete_confirm_token(dir_path: str) -> str:
    resolved = _validate_path(dir_path)
    return _delete_confirm_token(resolved)


def _is_directory_empty(resolved: Path) -> bool:
    try:
        return next(resolved.iterdir(), None) is None
    except OSError as exc:
        raise PermissionError(
            f"Cannot inspect directory contents for safety: {resolved}"
        ) from exc


# --- Tools ---------------------------------------------------------------

@server.tool()
def set_root_path(root_path: str) -> str:
    """
    Add/activate an allowed directory whitelist entry.

    Notes:
    - Call list_allowed_roots first; if the target is already listed you may skip set_root_path.
    - Path must be absolute, exist, and be a directory; otherwise raises FileNotFoundError/NotADirectoryError.
    - Access control is enforced by the allowed directory list; paths are not rewritten or resolved against this root.
    """
    global ROOT
    ROOT = fs_tools.set_root_path(root_path)
    return f"Active base path set to {ROOT}"


@server.tool()
def get_file_info(file_path: str) -> dict:
    """
    Get stat info for a path.
    - Includes size/timestamps/permissions; for small text files includes lineCount and appendPosition.
    - Works on files or directories; auto-switches root if allowed.
    - file_path must be absolute and within allowed directories.
    """
    return fs_tools.get_file_info(file_path)


@server.tool()
def list_allowed_roots() -> list[str]:
    """
    Return the current whitelist of allowed roots (normalized absolute paths).

    Use this before cross-root operations to decide whether you must call set_root_path
    explicitly. Paths not in this list will be rejected until added via set_root_path.
    """
    return [str(p) for p in fs_tools.list_allowed_roots()]


@server.tool()
def read_file(
    file_path: str,
    offset: int = 0,
    length: int | None = None,
    encoding: str | None = None,
) -> dict:
    """
    Read a file (text or image) with streaming behavior.

    - offset < 0 reads last |offset| lines; offset >= 0 reads from that line.
    - length is max lines to return; omit for default limit (clamped by CODE_EDIT_FILE_READ_LINE_LIMIT).
    - Paths must be absolute and within the allowed directories list (managed via set_root_path whitelist).
    - encoding: None/""/\"auto\" will trigger auto-detect; otherwise supports utf-8/gbk/gb2312.
    Common mistakes: passing URLs, non-integer offsets/length, unsupported encodings, or paths outside the allowed directories.
    """
    enc = _normalize_encoding(encoding)
    return fs_tools.read_file(file_path, offset, length, encoding=enc)


@server.tool()
def dir_ops(
    action: str,
    dir_path: str,
    depth: int = 2,
    format: str = "tree",
    ignore_patterns: List[str] | None = None,
    max_items: int | None = 1000,
    expected_mtime: float | None = None,
    confirm_token: str | None = None,
    allow_nonempty: bool | None = None,
) -> list | str:
    """
    Unified directory operations.

    - action: "create" | "list" | "delete"
    - create: requires dir_path (absolute, allowed); other params are ignored
    - list: uses depth/format/ignore_patterns/max_items
    - delete: requires expected_mtime, confirm_token, allow_nonempty (explicit)
      confirm_token must equal "delete:<normalized_abs_path>"
      normalized_abs_path = os.path.normcase(str(Path(dir_path).resolve()))
    """
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    if not isinstance(dir_path, str):
        raise ValueError("dir_path must be a string.")

    normalized_action = action.lower()
    if normalized_action == "create":
        return _create_directory(dir_path)
    if normalized_action == "list":
        return _list_directory(dir_path, depth, format, ignore_patterns, max_items)
    if normalized_action == "delete":
        return _delete_directory(dir_path, expected_mtime, confirm_token, allow_nonempty)
    raise ValueError("action must be one of: create, list, delete.")

def _create_directory(dir_path: str) -> str:
    fs_tools.create_directory(dir_path)
    return f"Successfully created directory {dir_path}"


def _list_directory(
    dir_path: str,
    depth: int = 2,
    format: str = "tree",
    ignore_patterns: List[str] | None = None,
    max_items: int | None = 1000,
) -> list:
    resolved = _validate_path(dir_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {dir_path}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {dir_path}")

    fmt = format.lower()
    if fmt not in {"tree", "flat"}:
        raise ValueError("format must be 'tree' or 'flat'")

    patterns = _normalize_ignore_patterns(ignore_patterns)

    if fmt == "tree":
        return fs_tools.list_directory(str(resolved), depth, patterns)

    if max_items is not None and max_items <= 0:
        raise ValueError("max_items must be a positive integer or None.")

    entries = []
    total_visible = 0
    for entry in sorted(resolved.iterdir(), key=lambda p: p.name):
        if any(fnmatch(entry.name, pat) for pat in patterns):
            continue
        total_visible += 1
        info = {"name": entry.name, "is_dir": entry.is_dir()}
        if entry.is_file():
            info["size"] = entry.stat().st_size
        if max_items is None or len(entries) < max_items:
            entries.append(info)

    if max_items is not None and total_visible > max_items:
        entries.append(
            {
                "name": f"[WARNING] truncated: showing first {max_items} of {total_visible} items",
                "is_dir": False,
                "truncated": True,
                "total": total_visible,
                "shown": max_items,
            }
        )
    return entries


def _delete_directory(
    directory_path: str,
    expected_mtime: float | None,
    confirm_token: str | None,
    allow_nonempty: bool | None,
) -> str:
    if expected_mtime is None:
        raise ValueError("expected_mtime is required for delete.")
    if confirm_token is None or not isinstance(confirm_token, str) or not confirm_token.strip():
        raise ValueError("confirm_token is required for delete.")
    if allow_nonempty is None or not isinstance(allow_nonempty, bool):
        raise ValueError("allow_nonempty is required for delete.")

    resolved = _validate_path(directory_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {directory_path}")
    if not resolved.is_dir():
        raise NotADirectoryError("delete only supports directories.")

    root = get_root()
    if resolved == root or resolved in root.parents:
        raise PermissionError("Refusing to delete the active root or its ancestors.")
    critical_hit = any(resolved == p for p in CRITICAL_PATHS)
    if resolved.anchor:
        critical_hit = critical_hit or resolved == Path(resolved.anchor)
    if critical_hit:
        raise PermissionError(f"Refusing to delete critical system directory: {resolved}")

    expected_token = _delete_confirm_token(resolved)
    if confirm_token.strip() != expected_token:
        raise PermissionError(
            "confirm_token mismatch. "
            f"Expected confirm_token to be '{expected_token}'."
        )

    if allow_nonempty is False and not _is_directory_empty(resolved):
        raise PermissionError(
            "Refusing to delete non-empty directory. "
            "Set allow_nonempty=True to proceed."
        )

    _check_expected_mtime(resolved, expected_mtime)
    shutil.rmtree(resolved)
    return f"Deleted directory {directory_path}."


@server.tool()
def file_ops(
    action: str,
    file_path: str | None = None,
    content: str | None = None,
    source_path: str | None = None,
    destination_path: str | None = None,
    expected_mtime: float | None = None,
    encoding: str = "utf-8",
) -> str:
    """
    Unified file operations.
    - action: "write" | "append" | "copy" | "move" | "delete"
    - write/append: requires file_path + content (encoding applies)
    - copy/move: requires source_path + destination_path
    - delete: requires file_path
    - All paths must be absolute and within allowed directories.
    - "write" overwrites the file; "append" adds to the end.
    - encoding is only used for write/append.
    - expected_mtime applies to the target file for write/append/delete, and the source file for copy/move.
    - copy requires a file source and a destination that does not exist; delete only supports files.
    """
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    normalized_action = action.lower()

    if normalized_action in {"write", "append"}:
        if file_path is None:
            raise ValueError("file_path is required for write/append.")
        if content is None:
            raise ValueError("content is required for write/append.")
        enc = _normalize_encoding_required(encoding)
        mode = "rewrite" if normalized_action == "write" else "append"
        fs_tools.write_file(file_path, content, mode=mode, expected_mtime=expected_mtime, encoding=enc)
        return f"Successfully {mode}d {file_path}."

    if normalized_action == "delete":
        if file_path is None:
            raise ValueError("file_path is required for delete.")
        resolved = _validate_path(file_path)
        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if resolved.is_dir():
            raise IsADirectoryError("delete only supports files.")
        _check_expected_mtime(resolved, expected_mtime)
        resolved.unlink()
        return f"Deleted file {file_path}."

    if normalized_action in {"copy", "move"}:
        if source_path is None or destination_path is None:
            raise ValueError("source_path and destination_path are required for copy/move.")
        if normalized_action == "move":
            fs_tools.move_file(source_path, destination_path, expected_mtime)
            return f"Moved {source_path} to {destination_path}."

        source = _validate_path(source_path)
        dest = _validate_path(destination_path)
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")
        if not source.is_file():
            raise IsADirectoryError("copy only supports files.")
        if dest.exists():
            raise FileExistsError(f"Destination already exists: {destination_path}")

        _check_expected_mtime(source, expected_mtime)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return f"Copied {source_path} to {destination_path}."

    raise ValueError("action must be one of: write, append, copy, move, delete.")


@server.tool()
def edit_block(
    file_path: str,
    old_string: str,
    new_string: str,
    expected_replacements: int = 1,
    expected_mtime: float | None = None,
    ignore_whitespace: bool = False,
    normalize_escapes: bool = False,
    encoding: str = "utf-8",
) -> str:
    """
    Precise search/replace with line-ending normalization and optimistic lock.
    - Automatically streams large files (>LARGE_FILE_THRESHOLD) for strict literal replacement.
    - Large-file mode does not support ignore_whitespace or normalize_escapes.
    - expected_replacements enforces exact match count; mismatch triggers rollback.
    - expected_mtime protects against concurrent edits.
    - file_path must be absolute and within allowed directories.
    - old_string is the literal search text; new_string is the replacement text.
    """
    enc = _normalize_encoding_required(encoding)
    resolved = _validate_path(file_path)
    stats = resolved.stat()
    meta = fs_tools._get_cached_file_metadata(resolved, stats)

    if meta.get("isBinary") or meta.get("isImage"):
        raise RuntimeError("Cannot edit binary or image files with edit_block.")

    size_value = meta.get("size")
    if isinstance(size_value, (int, float)):
        size = int(size_value)
    else:
        size = int(stats.st_size)
    threshold = FILE_SIZE_LIMITS.get("LARGE_FILE_THRESHOLD", 10 * 1024 * 1024)

    if size > threshold:
        if ignore_whitespace or normalize_escapes:
            raise RuntimeError(
                "Large-file mode only supports strict literal replacement. "
                "Disable ignore_whitespace/normalize_escapes."
            )
        replaced = fs_tools.stream_replace(
            file_path,
            old_string,
            new_string,
            expected_replacements=expected_replacements,
            expected_mtime=expected_mtime,
            encoding=enc,
        )
        fs_tools._invalidate_file_cache(str(resolved))
        return f"stream_replace completed with {replaced} replacement(s) in {file_path}."

    result = edit_tools.perform_search_replace(
        file_path,
        old_string,
        new_string,
        expected_replacements=expected_replacements,
        expected_mtime=expected_mtime,
        ignore_whitespace=ignore_whitespace,
        normalize_escapes=normalize_escapes,
        encoding=enc,
    )
    fs_tools._invalidate_file_cache(str(resolved))
    return result

@server.tool()
def convert_file_encoding(
    file_paths: List[str],
    source_encoding: str,
    target_encoding: str,
    error_handling: str = "strict",
    mismatch_policy: str = "warn-skip",
) -> list[dict]:
    """
    Convert one or more text files from source_encoding to target_encoding in-place.
    - file_paths must be absolute paths within allowed directories (set_root_path manages whitelist).
    - Supported encodings: utf-8, gbk, gb2312.
    - error_handling: 'strict' | 'replace' | 'ignore'; applied to both read and write.
    - mismatch_policy: 'warn-skip' (default), 'fail-fast', 'force'.
    """
    err = error_handling.lower()
    if err not in {"strict", "replace", "ignore"}:
        raise ValueError("error_handling must be one of: strict, replace, ignore.")
    policy = mismatch_policy.lower()
    if policy not in {"warn-skip", "fail-fast", "force"}:
        raise ValueError("mismatch_policy must be one of: warn-skip, fail-fast, force.")
    src = _normalize_encoding_required(source_encoding)
    tgt = _normalize_encoding_required(target_encoding)
    return fs_tools.convert_file_encoding(file_paths, src, tgt, err, policy)

if __name__ == "__main__":
    server.run()

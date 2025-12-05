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
from tools.config import DEFAULT_IGNORE_PATTERNS, get_root

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
    if encoding is None or encoding == "":
        return None
    return fs_tools.normalize_encoding(encoding)


def _normalize_encoding_required(encoding: str | None, default: str = "utf-8") -> str:
    """
    For tool handlers that require a concrete encoding, fallback to default when None/""/auto.
    """
    if encoding is None or encoding == "" or encoding == "auto":
        return fs_tools.normalize_encoding(default)
    return fs_tools.normalize_encoding(encoding)


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
        return cleaned  # empty string means “no ignores”
    items = list(patterns)
    if any(not isinstance(p, str) for p in items):
        raise ValueError("ignore_patterns elements must all be strings.")
    return items  # empty list => show all files


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
    - length is max lines to return; omit for default limit.
    - Paths must be absolute and within the allowed directories list (managed via set_root_path whitelist).
    - encoding: None/""/\"auto\" will trigger auto-detect; otherwise supports utf-8/gbk/gb2312.
    Common mistakes: passing URLs, non-integer offsets/length, unsupported encodings, or paths outside the allowed directories.
    """
    enc = _normalize_encoding(encoding)
    return fs_tools.read_file(file_path, offset, length, encoding=enc)


@server.tool()
def create_directory(dir_path: str) -> str:
    """Create a directory (parents allowed). dir_path must be absolute and under an allowed root."""
    fs_tools.create_directory(dir_path)
    return f"Successfully created directory {dir_path}"


@server.tool()
def list_directory(
    dir_path: str,
    depth: int = 2,
    format: str = "tree",
    ignore_patterns: List[str] | None = None,
) -> list:
    """
    List directory contents.
    format="tree": nested string listing (default), respects depth.
    format="flat": immediate children with metadata, filtered by ignore_patterns.
    - dir_path must be absolute and within allowed directories.
    Common mistakes: using unsupported format values; negative/zero depth; wrong pattern types.
    """
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

    entries = []
    for entry in sorted(resolved.iterdir(), key=lambda p: p.name):
        if any(fnmatch(entry.name, pat) for pat in patterns):
            continue
        info = {"name": entry.name, "is_dir": entry.is_dir()}
        if entry.is_file():
            info["size"] = entry.stat().st_size
        entries.append(info)
    return entries


@server.tool()
def write_file(
    file_path: str,
    content: str,
    mode: str = "rewrite",
    expected_mtime: float | None = None,
    encoding: str = "utf-8",
) -> str:
    """
    Write or append to a file.
    - mode: "rewrite"/"write" to overwrite, "append" to add.
    - expected_mtime: optional optimistic lock; mismatch raises.
    - Paths must be absolute and within allowed directories (set_root_path manages whitelist only).
    - encoding defaults to utf-8; supports gbk and gb2312.
    Common mistakes: mode values like "w"/"replace"; stale expected_mtime; unsupported encodings.
    """
    normalized_mode = "rewrite" if mode in {"rewrite", "write"} else mode
    if normalized_mode not in {"rewrite", "append"}:
        raise ValueError("mode must be 'rewrite' (or 'write') or 'append'")
    enc = _normalize_encoding_required(encoding)
    fs_tools.write_file(file_path, content, mode=normalized_mode, expected_mtime=expected_mtime, encoding=enc)
    return f"Successfully {normalized_mode}d {file_path}."


@server.tool()
def delete_file(file_path: str, expected_mtime: float | None = None) -> str:
    """
    Delete a file with optional optimistic lock.
    - Not for directories.
    - Paths must be absolute and inside allowed directories (set_root_path only manages whitelist).
    - expected_mtime protects against concurrent edits.
    """
    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if resolved.is_dir():
        raise IsADirectoryError("delete_file only supports files.")
    _check_expected_mtime(resolved, expected_mtime)
    resolved.unlink()
    return f"Deleted file {file_path}."


@server.tool()
def move_file(
    source_path: str,
    destination_path: str,
    expected_mtime: float | None = None,
) -> str:
    """
    Move a file or directory.
    - Destination must not already exist.
    - expected_mtime checks the source before move.
    - source_path and destination_path must be absolute and within allowed directories (set_root_path only manages whitelist).
    """
    fs_tools.move_file(source_path, destination_path, expected_mtime)
    return f"Moved {source_path} to {destination_path}."


@server.tool()
def copy_file(
    source_path: str,
    destination_path: str,
    expected_mtime: float | None = None,
) -> str:
    """
    Copy a file.
    - Source must be a file; destination must not exist.
    - expected_mtime checks the source before copy.
    - source_path and destination_path must be absolute and within allowed directories (set_root_path only manages whitelist).
    """
    source = _validate_path(source_path)
    dest = _validate_path(destination_path)

    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source_path}")
    if not source.is_file():
        raise IsADirectoryError("copy_file only supports files.")
    if dest.exists():
        raise FileExistsError(f"Destination already exists: {destination_path}")

    _check_expected_mtime(source, expected_mtime)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return f"Copied {source_path} to {destination_path}."


@server.tool()
def delete_directory(directory_path: str, expected_mtime: float | None = None) -> str:
    """
    Delete a directory recursively with safety rails.
    - Must be a directory and an absolute path within allowed directories.
    - Refuses to delete current root, its ancestors, or critical system dirs (/ /home /root /Users C:\\).
    - expected_mtime provides optimistic lock.
    """
    resolved = _validate_path(directory_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {directory_path}")
    if not resolved.is_dir():
        raise NotADirectoryError("delete_directory only supports directories.")
    root = get_root()
    if resolved == root or resolved in root.parents:
        raise PermissionError("Refusing to delete the active root or its ancestors.")
    critical_hit = any(resolved == p for p in CRITICAL_PATHS)
    if resolved.anchor:
        critical_hit = critical_hit or resolved == Path(resolved.anchor)
    if critical_hit:
        raise PermissionError(f"Refusing to delete critical system directory: {resolved}")
    _check_expected_mtime(resolved, expected_mtime)
    shutil.rmtree(resolved)
    return f"Deleted directory {directory_path}."


@server.tool()
def replace_string(
    file_path: str,
    search_string: str,
    replace_string: str,
    expected_mtime: float | None = None,
    ignore_whitespace: bool = False,
    normalize_escapes: bool = False,
    encoding: str = "utf-8",
) -> str:
    """
    Backward-compatible single replacement with optimistic lock.
    - Same behavior as edit_block with expected_replacements=1.
    - Empty search or no match raises; fuzzy-only matches raise.
    - normalize_escapes optionally unescapes \"\\n\", \"\\t\", \"\\\"\", \"\\\\\" in the search string to match literal text.
    - file_path must be an absolute path within allowed directories (managed via set_root_path whitelist).
    """
    # Backward-compatible alias to edit_block with single replacement and mtime protection.
    enc = _normalize_encoding_required(encoding)
    return edit_tools.perform_search_replace(
        file_path,
        search_string,
        replace_string,
        expected_replacements=1,
        expected_mtime=expected_mtime,
        ignore_whitespace=ignore_whitespace,
        normalize_escapes=normalize_escapes,
        encoding=enc,
    )


@server.tool()
def edit_lines(
    file_path: str,
    start_line: int,
    end_line: int,
    new_content: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    if start_line < 1 or end_line < start_line:
        raise ValueError("start_line must be >= 1 and end_line must be >= start_line.")

    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    _check_expected_mtime(resolved, expected_mtime)
    enc = _normalize_encoding_required(encoding)
    lines = _read_lines(resolved, enc)
    if end_line > len(lines):
        raise ValueError("end_line exceeds total number of lines.")

    new_lines = new_content.splitlines(keepends=True)
    updated = lines[: start_line - 1] + new_lines + lines[end_line:]
    _write_text(resolved, "".join(updated), enc)
    return (
        f"Replaced lines {start_line}-{end_line} in {file_path} "
        f"with {len(new_lines)} new line(s)."
    )


@server.tool()
def insert_at_line(
    file_path: str,
    line_number: int,
    content: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    if line_number < 0:
        raise ValueError("line_number must be >= 0.")

    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    _check_expected_mtime(resolved, expected_mtime)
    enc = _normalize_encoding_required(encoding)
    lines = _read_lines(resolved, enc)
    if line_number > len(lines):
        raise ValueError("line_number exceeds total number of lines.")

    new_lines = content.splitlines(keepends=True)
    idx = line_number
    updated = lines[:idx] + new_lines + lines[idx:]
    _write_text(resolved, "".join(updated), enc)
    inserted_count = len(new_lines)
    inserted_start = line_number + 1 if inserted_count else line_number
    inserted_end = line_number + inserted_count
    return (
        f"Inserted {inserted_count} line(s) into {file_path} at lines "
        f"{inserted_start}-{inserted_end}."
    )


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
    - expected_replacements enforces exact match count.
    - Empty search raises; fuzzy-only matches raise with diff guidance.
    - expected_mtime protects against concurrent edits.
    - ignore_whitespace allows whitespace-insensitive matching (collapses whitespace to \\s+).
    - normalize_escapes best-effort unescapes \"\\n\", \"\\t\", \"\\\"\", \"\\\\\" in the search string; keep off unless你的搜索串是转义文本。
    """
    enc = _normalize_encoding_required(encoding)
    return edit_tools.perform_search_replace(
        file_path,
        old_string,
        new_string,
        expected_replacements=expected_replacements,
        expected_mtime=expected_mtime,
        ignore_whitespace=ignore_whitespace,
        normalize_escapes=normalize_escapes,
        encoding=enc,
    )


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

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import List

from mcp.server.fastmcp import FastMCP
from tools import edit as edit_tools
from tools import filesystem as fs_tools

logging.basicConfig(level=logging.INFO)

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MTIME_EPSILON = 0.01
DEFAULT_IGNORE_PATTERNS = [".git", "__pycache__", "node_modules", ".DS_Store"]

server = FastMCP("code-editor")
ROOT = fs_tools.get_root()
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
    current = resolved.stat().st_mtime
    if abs(current - expected_mtime) > MTIME_EPSILON:
        raise RuntimeError(
            f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current}."
        )


def _read_lines(file_path: Path, encoding: str) -> List[str]:
    return file_path.read_text(encoding=encoding).splitlines(keepends=True)


def _write_text(file_path: Path, content: str, encoding: str) -> None:
    file_path.write_text(content, encoding=encoding)


def _read_text(file_path: Path, encoding: str) -> str:
    return file_path.read_text(encoding=encoding)


def _index_to_line_col(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    column = index - last_newline
    return line, column


def _normalize_ignore_patterns(patterns: List[str] | str | None) -> List[str]:
    if patterns is None:
        return list(DEFAULT_IGNORE_PATTERNS)
    if isinstance(patterns, str):
        cleaned = [p.strip() for p in patterns.split(",") if p.strip()]
        return cleaned or list(DEFAULT_IGNORE_PATTERNS)
    items = list(patterns)
    if not items:
        return list(DEFAULT_IGNORE_PATTERNS)
    if any(not isinstance(p, str) for p in items):
        raise ValueError("ignore_patterns elements must all be strings.")
    return items


def _summarize_hunks(diff_lines: List[str]) -> list[str]:
    summaries: list[str] = []
    hunk_no = 0
    for line in diff_lines:
        if not line.startswith("@@"):
            continue
        match = HUNK_HEADER_RE.match(line)
        if not match:
            continue
        hunk_no += 1
        old_start = int(match.group(1))
        old_len = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_len = int(match.group(4) or "1")
        old_end = old_start + max(old_len, 1) - 1
        new_end = new_start + max(new_len, 1) - 1
        summaries.append(f"#{hunk_no} -{old_start}-{old_end} -> +{new_start}-{new_end}")
    return summaries


def _apply_hunks(original_lines: List[str], diff_lines: List[str]) -> List[str]:
    new_lines: List[str] = []
    old_index = 0
    hunk_no = 0
    i = 0

    while i < len(diff_lines):
        line = diff_lines[i]
        if not line.startswith("@@"):
            i += 1
            continue

        match = HUNK_HEADER_RE.match(line)
        if not match:
            raise RuntimeError(f"Patch failed: invalid hunk header at line {i + 1}.")

        hunk_no += 1
        old_start = int(match.group(1))
        old_len = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_len = int(match.group(4) or "1")

        expected_old_index = old_start - 1
        if expected_old_index < old_index:
            raise RuntimeError(f"Patch failed: overlapping hunks near hunk #{hunk_no}.")

        new_lines.extend(original_lines[old_index:expected_old_index])
        old_index = expected_old_index
        i += 1
        hunk_old_consumed = 0
        hunk_new_produced = 0

        while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
            marker = diff_lines[i][:1]
            text = diff_lines[i][1:]
            if marker == " ":
                if old_index >= len(original_lines):
                    raise RuntimeError(f"Patch failed: context beyond EOF in hunk #{hunk_no}.")
                if original_lines[old_index] != text:
                    raise RuntimeError(
                        f"Patch failed: Hunk #{hunk_no} context mismatch at original line {old_index + 1}."
                    )
                new_lines.append(original_lines[old_index])
                old_index += 1
                hunk_old_consumed += 1
                hunk_new_produced += 1
            elif marker == "-":
                if old_index >= len(original_lines):
                    raise RuntimeError(f"Patch failed: deletion beyond EOF in hunk #{hunk_no}.")
                if original_lines[old_index] != text:
                    raise RuntimeError(
                        f"Patch failed: Hunk #{hunk_no} deletion mismatch at original line {old_index + 1}."
                    )
                old_index += 1
                hunk_old_consumed += 1
            elif marker == "+":
                new_lines.append(text)
                hunk_new_produced += 1
            elif marker == "\\":
                pass  # "\\ No newline" marker
            else:
                raise RuntimeError(f"Patch failed: invalid hunk line marker '{marker}' in hunk #{hunk_no}.")
            i += 1

        if hunk_old_consumed != old_len:
            raise RuntimeError(
                f"Patch failed: Hunk #{hunk_no} expected to consume {old_len} lines, got {hunk_old_consumed}."
            )
        if hunk_new_produced != new_len:
            raise RuntimeError(
                f"Patch failed: Hunk #{hunk_no} expected to produce {new_len} lines, got {hunk_new_produced}."
            )

    new_lines.extend(original_lines[old_index:])
    return new_lines


# --- Tools ---------------------------------------------------------------

@server.tool()
def set_root_path(root_path: str) -> str:
    """
    Switch CODE_EDIT_ROOT to an allowed directory (persisting to whitelist).

    Tips:
    - Call list_allowed_roots first; if the target is already whitelisted you may skip set_root_path.
    - Path must exist and be a directory; otherwise raises FileNotFoundError/NotADirectoryError.
    - Cutting over changes global root; if you need to go back, call set_root_path with the prior root.
    """
    global ROOT
    ROOT = fs_tools.set_root_path(root_path)
    return f"CODE_EDIT_ROOT set to {ROOT}"


@server.tool()
def get_file_info(file_path: str) -> dict:
    """
    Get stat info for a path.
    - Includes size/timestamps/permissions; for small text files includes lineCount and appendPosition.
    - Works on files or directories; auto-switches root if allowed.
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
def read_file(file_path: str, offset: int = 0, length: int | None = None) -> dict:
    """
    Read a file (text or image) with streaming behavior.

    - offset < 0 reads last |offset| lines; offset >= 0 reads from that line.
    - length is max lines to return; omit for default limit.
    - If path is in another allowed root, the server auto-switches root before reading.
    Common mistakes: passing URLs, non-integer offsets/length, or paths outside the whitelist.
    """
    return fs_tools.read_file(file_path, offset, length)


@server.tool()
def create_directory(dir_path: str) -> str:
    """Create a directory (parents allowed). Path must be under an allowed root (auto-switch supported)."""
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

    if fmt == "tree":
        return fs_tools.list_directory(str(resolved), depth)

    patterns = _normalize_ignore_patterns(ignore_patterns)
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
) -> str:
    """
    Write or append to a file.
    - mode: "rewrite"/"write" to overwrite, "append" to add.
    - expected_mtime: optional optimistic lock; mismatch raises.
    - Auto-switches root if path is in allowed roots.
    Common mistakes: mode values like "w"/"replace"; stale expected_mtime.
    """
    normalized_mode = "rewrite" if mode in {"rewrite", "write"} else mode
    if normalized_mode not in {"rewrite", "append"}:
        raise ValueError("mode must be 'rewrite' (or 'write') or 'append'")
    fs_tools.write_file(file_path, content, mode=normalized_mode, expected_mtime=expected_mtime)
    return f"Successfully {normalized_mode}d {file_path}."


@server.tool()
def delete_file(file_path: str, expected_mtime: float | None = None) -> str:
    """
    Delete a file with optional optimistic lock.
    - Not for directories.
    - Auto-switches root if needed; will raise if path not whitelisted.
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
    - Cross-root moves auto-switch root to destination; global root will stay there afterward.
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
    - Cross-root copies auto-switch root to destination; global root will stay there afterward.
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
    - Must be a directory.
    - Refuses to delete current root, its ancestors, or critical system dirs (/ /home /root /Users C:\\).
    - expected_mtime provides optimistic lock.
    """
    resolved = _validate_path(directory_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {directory_path}")
    if not resolved.is_dir():
        raise NotADirectoryError("delete_directory only supports directories.")
    root = fs_tools.get_root()
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
) -> str:
    """
    Backward-compatible single replacement with optimistic lock.
    - Same behavior as edit_block with expected_replacements=1.
    - Empty search or no match raises; fuzzy-only matches raise.
    """
    # Backward-compatible alias to edit_block with single replacement and mtime protection.
    return edit_tools.perform_search_replace(
        file_path,
        search_string,
        replace_string,
        expected_replacements=1,
        expected_mtime=expected_mtime,
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
    lines = _read_lines(resolved, encoding)
    if end_line > len(lines):
        raise ValueError("end_line exceeds total number of lines.")

    new_lines = new_content.splitlines(keepends=True)
    updated = lines[: start_line - 1] + new_lines + lines[end_line:]
    _write_text(resolved, "".join(updated), encoding)
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
    lines = _read_lines(resolved, encoding)
    if line_number > len(lines):
        raise ValueError("line_number exceeds total number of lines.")

    new_lines = content.splitlines(keepends=True)
    idx = line_number
    updated = lines[:idx] + new_lines + lines[idx:]
    _write_text(resolved, "".join(updated), encoding)
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
) -> str:
    """
    Precise search/replace with line-ending normalization and optimistic lock.
    - expected_replacements enforces exact match count.
    - Empty search raises; fuzzy-only matches raise with diff guidance.
    - expected_mtime protects against concurrent edits.
    """
    return edit_tools.perform_search_replace(
        file_path,
        old_string,
        new_string,
        expected_replacements=expected_replacements,
        expected_mtime=expected_mtime,
    )


@server.tool()
def apply_unified_diff(
    file_path: str,
    diff_content: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    _check_expected_mtime(resolved, expected_mtime)
    original_lines = _read_lines(resolved, encoding)
    diff_lines = diff_content.splitlines(keepends=True)

    if not any(line.startswith("@@") for line in diff_lines):
        raise RuntimeError("Patch failed: no hunk headers found.")

    updated_lines = _apply_hunks(original_lines, diff_lines)
    _write_text(resolved, "".join(updated_lines), encoding)
    summaries = _summarize_hunks(diff_lines)
    summary_text = "; ".join(summaries) if summaries else "No hunks summarized."
    return f"Patch applied to {file_path}. Hunks: {summary_text}"


if __name__ == "__main__":
    server.run()

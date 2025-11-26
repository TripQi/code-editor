from __future__ import annotations

import os
import re
import shutil
from fnmatch import fnmatch
from datetime import datetime
from pathlib import Path
from typing import List

from mcp.server.fastmcp import FastMCP

ROOT = Path(os.environ.get("CODE_EDIT_ROOT", Path.cwd())).resolve()

server = FastMCP("code-edit-mcp")

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
MTIME_EPSILON = 0.01
DEFAULT_IGNORE_PATTERNS = [".git", "__pycache__", "node_modules", ".DS_Store"]


@server.tool()
def set_root_path(root_path: str) -> str:
    """
    Set CODE_EDIT_ROOT for subsequent operations.

    Args:
        root_path: Absolute or relative directory to use as the sandbox root.
                   Must exist and be a directory.
    """
    global ROOT
    candidate = Path(root_path).expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"Root path not found: {root_path}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root_path}")
    ROOT = candidate
    os.environ["CODE_EDIT_ROOT"] = str(candidate)
    return f"CODE_EDIT_ROOT set to {candidate}"


def _validate_path(path: str) -> Path:
    """Ensure the target path stays within ROOT."""
    candidate = (ROOT / path).resolve()
    if candidate == ROOT or ROOT in candidate.parents:
        return candidate
    raise ValueError("Access denied: path escapes the root directory sandbox.")


def _read_lines(file_path: Path, encoding: str) -> List[str]:
    try:
        with open(file_path, "r", encoding=encoding) as f:
            return f.readlines()
    except UnicodeError as exc:
        raise UnicodeError(f"UnicodeError: File could not be read as {encoding}") from exc


def _read_text(file_path: Path, encoding: str) -> str:
    try:
        with open(file_path, "r", encoding=encoding) as f:
            return f.read()
    except UnicodeError as exc:
        raise UnicodeError(f"UnicodeError: File could not be read as {encoding}") from exc


def _write_text(file_path: Path, content: str, encoding: str) -> None:
    try:
        with open(file_path, "w", encoding=encoding, newline="") as f:
            f.write(content)
    except UnicodeError as exc:
        raise UnicodeError(f"UnicodeError: File could not be written as {encoding}") from exc


def _append_text(file_path: Path, content: str, encoding: str) -> None:
    try:
        with open(file_path, "a", encoding=encoding, newline="") as f:
            f.write(content)
    except UnicodeError as exc:
        raise UnicodeError(f"UnicodeError: File could not be written as {encoding}") from exc


def _check_expected_mtime(resolved: Path, expected_mtime: float | None) -> None:
    if expected_mtime is None:
        return
    if not resolved.exists():
        raise FileNotFoundError(f"File not found for mtime check: {resolved}")
    current = resolved.stat().st_mtime
    if abs(current - expected_mtime) > MTIME_EPSILON:
        raise RuntimeError(f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current}.")


def _normalize_ignore_patterns(patterns: List[str] | str | None) -> List[str]:
    """
    Normalize ignore_patterns allowing list, tuple, comma-separated string, or None.

    Returns DEFAULT_IGNORE_PATTERNS when input is None or empty.
    """
    if patterns is None:
        return list(DEFAULT_IGNORE_PATTERNS)
    if isinstance(patterns, str):
        cleaned = [p.strip() for p in patterns.split(",") if p.strip()]
        return cleaned or list(DEFAULT_IGNORE_PATTERNS)
    try:
        items = list(patterns)
    except TypeError as exc:
        raise ValueError("ignore_patterns must be a list/tuple of strings or a comma-separated string.") from exc
    if not items:
        return list(DEFAULT_IGNORE_PATTERNS)
    if any(not isinstance(p, str) for p in items):
        raise ValueError("ignore_patterns elements must all be strings.")
    return items


def _index_to_line_col(text: str, index: int) -> tuple[int, int]:
    """Convert a 0-based character index to 1-based (line, column)."""
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    column = index - last_newline
    return line, column


@server.tool()
def list_files(
    directory_path: str,
    ignore_patterns: List[str] | None = None,
) -> list:
    """
    List entries (non-recursive) in a directory.

    Args:
        directory_path: Path (absolute or relative to CODE_EDIT_ROOT) to list.
        ignore_patterns: Glob patterns to skip. Accepts list/tuple of strings or a
                         comma-separated string. Pass an empty list/empty string to
                         fall back to default ignores.

    Returns:
        List of dicts: {"name": str, "is_dir": bool, "size": int (for files)}.
    """
    dir_path = _validate_path(directory_path)
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {directory_path}")
    if not dir_path.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory_path}")

    patterns = _normalize_ignore_patterns(ignore_patterns)
    entries = []
    for entry in sorted(dir_path.iterdir(), key=lambda p: p.name):
        if any(fnmatch(entry.name, pat) for pat in patterns):
            continue
        info = {
            "name": entry.name,
            "is_dir": entry.is_dir(),
        }
        if entry.is_file():
            info["size"] = entry.stat().st_size
        entries.append(info)
    return entries


@server.tool()
def get_file_info(file_path: str) -> dict:
    """
    Return metadata for a file or directory (mtime can be used for optimistic locking).

    Args:
        file_path: Path (absolute or relative to CODE_EDIT_ROOT) to inspect.

    Returns:
        Dict with path, is_dir, size, mtime (float), last_modified, ctime.
    """
    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"Path not found: {file_path}")

    stat = resolved.stat()
    return {
        "path": str(resolved),
        "is_dir": resolved.is_dir(),
        "size": stat.st_size,
        "mtime": stat.st_mtime,  # float for optimistic locking
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "ctime": datetime.fromtimestamp(stat.st_ctime).isoformat(),
    }


@server.tool()
def read_file(file_path: str, encoding: str = "utf-8") -> str:
    """
    Read an entire file with the specified encoding.

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        encoding: Text encoding, default utf-8. If you see Unicode errors, retry with
                  a different encoding (e.g., gbk).
    """
    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")
    return _read_text(resolved, encoding)


@server.tool()
def read_range(file_path: str, start_line: int, end_line: int, encoding: str = "utf-8") -> str:
    """
    Read specific 1-based inclusive line range.

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        start_line: 1-based start line (inclusive).
        end_line: 1-based end line (inclusive), must be >= start_line.
        encoding: Text encoding, default utf-8.
    """
    if start_line < 1 or end_line < start_line:
        raise ValueError("start_line must be >= 1 and end_line must be >= start_line.")

    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    lines = _read_lines(resolved, encoding)
    if end_line > len(lines):
        raise ValueError("end_line exceeds total number of lines.")

    return "".join(lines[start_line - 1 : end_line])


@server.tool()
def create_file(file_path: str, content: str, encoding: str = "utf-8") -> str:
    """
    Create a new file; error if it already exists. Auto-create parent directories.

    Args:
        file_path: Destination path relative to CODE_EDIT_ROOT.
        content: File content to write.
        encoding: Text encoding, default utf-8.
    """
    resolved = _validate_path(file_path)
    if resolved.exists():
        raise FileExistsError(f"File already exists: {file_path}")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    _write_text(resolved, content, encoding)
    return f"Created file {file_path}."


@server.tool()
def write_file(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    """
    Overwrite an entire file (creates parent directories if needed).

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        content: Full file content to write.
        encoding: Text encoding, default utf-8.
        expected_mtime: Optional float to enforce optimistic locking; if provided and
                        file mtime differs, raises conflict.
    """
    resolved = _validate_path(file_path)
    if expected_mtime is not None and not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    _check_expected_mtime(resolved, expected_mtime)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _write_text(resolved, content, encoding)
    return f"Wrote file {file_path}."


@server.tool()
def append_file(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    """
    Append content to a file (creates parent directories if needed).

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        content: Text to append.
        encoding: Text encoding, default utf-8.
        expected_mtime: Optional float for optimistic locking on the target file.
    """
    resolved = _validate_path(file_path)
    _check_expected_mtime(resolved, expected_mtime)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _append_text(resolved, content, encoding)
    lines_appended = len(content.splitlines())
    return f"Appended {lines_appended} lines to {file_path}."


@server.tool()
def delete_file(file_path: str, expected_mtime: float | None = None) -> str:
    """
    Delete a file.

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        expected_mtime: Optional float for optimistic locking on the target file.
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
    Move or rename a file or directory.

    Args:
        source_path: Existing file or directory path relative to CODE_EDIT_ROOT.
        destination_path: New path relative to CODE_EDIT_ROOT; must not already exist.
        expected_mtime: Optional float for optimistic locking on the source.
    """
    source = _validate_path(source_path)
    dest = _validate_path(destination_path)

    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source_path}")
    if dest.exists():
        raise FileExistsError(f"Destination already exists: {destination_path}")

    _check_expected_mtime(source, expected_mtime)

    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)
    return f"Moved {source_path} to {destination_path}."


@server.tool()
def copy_file(
    source_path: str,
    destination_path: str,
    expected_mtime: float | None = None,
) -> str:
    """
    Copy a file. Creates parent directories; fails if destination exists.

    Args:
        source_path: Existing file path relative to CODE_EDIT_ROOT.
        destination_path: New file path relative to CODE_EDIT_ROOT; must not exist.
        expected_mtime: Optional float for optimistic locking on the source file.
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
    Recursively delete a directory (cannot delete ROOT).

    Args:
        directory_path: Directory path relative to CODE_EDIT_ROOT.
        expected_mtime: Optional float for optimistic locking on the directory.
    """
    resolved = _validate_path(directory_path)
    if resolved == ROOT:
        raise ValueError("Refusing to delete ROOT directory.")
    if not resolved.exists():
        raise FileNotFoundError(f"Directory not found: {directory_path}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory_path}")

    _check_expected_mtime(resolved, expected_mtime)
    shutil.rmtree(resolved)
    return f"Deleted directory {directory_path}."


@server.tool()
def replace_string(
    file_path: str,
    search_string: str,
    replace_string: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    """
    Replace occurrences of search_string with replace_string.

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        search_string: Exact text to replace; must appear exactly once.
        replace_string: Replacement text.
        encoding: Text encoding, default utf-8.
        expected_mtime: Optional float for optimistic locking on the file.
    """
    resolved = _validate_path(file_path)
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    _check_expected_mtime(resolved, expected_mtime)
    content = _read_text(resolved, encoding)
    occurrences = content.count(search_string)
    if occurrences == 0:
        raise ValueError("search_string not found in file.")
    if occurrences > 1:
        raise ValueError(
            f"Found {occurrences} occurrences of search_string. "
            "Use 'edit_lines' or a more specific search string for precise editing."
        )

    new_content = content.replace(search_string, replace_string)
    start_idx = content.index(search_string)
    end_idx = start_idx + len(search_string) - 1
    start_line, start_col = _index_to_line_col(content, start_idx)
    end_line, end_col = _index_to_line_col(content, end_idx)
    _write_text(resolved, new_content, encoding)
    return (
        f"Replaced 1 occurrence in {file_path} from "
        f"line {start_line}:{start_col} to {end_line}:{end_col}."
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
    """
    Replace lines X through Y (1-based inclusive).

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        start_line: 1-based start line (inclusive), must be >= 1.
        end_line: 1-based end line (inclusive), must be >= start_line.
        new_content: Replacement text; can contain multiple lines.
        encoding: Text encoding, default utf-8.
        expected_mtime: Optional float for optimistic locking on the file.
    """
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
    """
    Insert content after the specified 1-based line_number (0 inserts at file start).

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        line_number: Insert after this 1-based line; 0 inserts at start, cannot exceed file length.
        content: Text to insert; can contain multiple lines.
        encoding: Text encoding, default utf-8.
        expected_mtime: Optional float for optimistic locking on the file.
    """
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
    idx = line_number  # insert after this index (1-based); when 0, inserts at start
    updated = lines[:idx] + new_lines + lines[idx:]
    _write_text(resolved, "".join(updated), encoding)
    inserted_count = len(new_lines)
    inserted_start = line_number + 1 if inserted_count else line_number
    inserted_end = line_number + inserted_count
    return (
        f"Inserted {inserted_count} line(s) into {file_path} at lines "
        f"{inserted_start}-{inserted_end}."
    )


def _summarize_hunks(diff_lines: List[str]) -> list[str]:
    """Build readable summaries of each hunk range from unified diff lines."""
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
                # Handles "\ No newline at end of file"
                pass
            else:
                raise RuntimeError(f"Patch failed: invalid hunk line marker '{marker}' in hunk #{hunk_no}.")
            i += 1

        if hunk_old_consumed != old_len:
            raise RuntimeError(f"Patch failed: Hunk #{hunk_no} expected to consume {old_len} lines, got {hunk_old_consumed}.")
        if hunk_new_produced != new_len:
            raise RuntimeError(
                f"Patch failed: Hunk #{hunk_no} expected to produce {new_len} lines, got {hunk_new_produced}."
            )

    new_lines.extend(original_lines[old_index:])
    return new_lines


@server.tool()
def apply_unified_diff(
    file_path: str,
    diff_content: str,
    encoding: str = "utf-8",
    expected_mtime: float | None = None,
) -> str:
    """
    Apply a unified diff to a single file atomically. Fails if any hunk does not match.

    Args:
        file_path: Target file path relative to CODE_EDIT_ROOT.
        diff_content: Unified diff text for the target file only.
        encoding: Text encoding, default utf-8.
        expected_mtime: Optional float for optimistic locking on the file.
    """
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

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, List, cast

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
    Add a directory to the allowed whitelist.

    Args:
        root_path: Absolute path to an existing directory.
    """
    global ROOT
    ROOT = fs_tools.set_root_path(root_path)
    return f"Active base path set to {ROOT}"


@server.tool()
def get_file_info(file_path: str) -> dict:
    """
    Get metadata for a file or directory.

    Args:
        file_path: Absolute path to the target.
    """
    return fs_tools.get_file_info(file_path)


@server.tool()
def list_allowed_roots() -> list[str]:
    """
    Return the current whitelist of allowed directories.
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
    Read file content with optional line range.

    Args:
        file_path: Absolute path to the file.
        offset: Start line (negative reads from end).
        length: Max lines to return.
        encoding: File encoding (auto-detected if omitted).
    """
    enc = _normalize_encoding(encoding)
    return fs_tools.read_file(file_path, offset, length, encoding=enc)


@server.tool()
def read_files(
    file_paths: List[str],
    encoding: str | None = None,
) -> list[dict]:
    """
    Read multiple files in a single call.

    Args:
        file_paths: List of absolute file paths.
        encoding: File encoding (auto-detected if omitted).
    """
    enc = _normalize_encoding(encoding)
    return fs_tools.read_multiple_files(file_paths, encoding=enc or "utf-8")


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
    Directory operations: create, list, or delete.

    Args:
        action: "create" | "list" | "delete"
        dir_path: Absolute path to the directory.
        depth: Listing depth (list only).
        format: "tree" | "flat" (list only).
        ignore_patterns: Glob patterns to exclude (list only).
        max_items: Max entries for flat listing.
        expected_mtime: Required for delete (conflict detection).
        confirm_token: Required for delete ("delete:<normalized_path>").
        allow_nonempty: Required for delete (explicit bool).
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
    File operations: write, append, copy, move, or delete.

    Args:
        action: "write" | "append" | "copy" | "move" | "delete"
        file_path: Target path (write/append/delete).
        content: File content (write/append).
        source_path: Source path (copy/move).
        destination_path: Destination path (copy/move).
        expected_mtime: Conflict detection timestamp.
        encoding: Text encoding (write/append only).
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
) -> dict[str, Any]:
    """
    Search and replace text in a file.

    Args:
        file_path: Absolute path to the file.
        old_string: Text to find.
        new_string: Replacement text.
        expected_replacements: Required match count (default 1).
        expected_mtime: Conflict detection timestamp.
        ignore_whitespace: Match with flexible whitespace.
        normalize_escapes: Unescape \\n, \\t, etc. in search.
        encoding: File encoding.
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
        result = fs_tools.stream_replace(
            file_path,
            old_string,
            new_string,
            expected_replacements=expected_replacements,
            expected_mtime=expected_mtime,
            encoding=enc,
        )
        fs_tools._invalidate_file_cache(str(resolved))
        return result

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
    return cast(dict[str, Any], result)


@server.tool()
def edit_blocks(
    edits: List[dict],
    error_policy: str = "fail-fast",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """
    Apply multiple search/replace edits in a single call.

    Args:
        edits: List of edit specs, each with:
            - file_path: Absolute path
            - old_string: Text to find
            - new_string: Replacement text
            - expected_replacements: Match count (default 1)
            - ignore_whitespace: Flexible whitespace (default False)
            - normalize_escapes: Unescape \\n, \\t (default False)
        error_policy: "fail-fast" | "continue" | "rollback"
        encoding: File encoding for all edits.
    """
    if not isinstance(edits, list) or len(edits) == 0:
        raise ValueError("edits must be a non-empty list of edit specifications.")

    policy = error_policy.lower()
    if policy not in {"fail-fast", "continue", "rollback"}:
        raise ValueError("error_policy must be one of: fail-fast, continue, rollback.")

    enc = _normalize_encoding_required(encoding)

    # Group edits by file for sequential processing
    from collections import defaultdict
    edits_by_file: dict = defaultdict(list)
    for idx, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"Edit at index {idx} must be a dict.")
        file_path = edit.get("file_path")
        if not file_path or not isinstance(file_path, str):
            raise ValueError(f"Edit at index {idx} missing valid file_path.")
        edits_by_file[file_path].append((idx, edit))

    results: list = [None] * len(edits)  # Preserve original order
    backups: dict = {}  # file_path -> original content (for rollback)
    successful = 0
    failed = 0

    try:
        for file_path, file_edits in edits_by_file.items():
            resolved = _validate_path(file_path)

            # Check file size limit
            file_size = resolved.stat().st_size
            max_bytes = FILE_SIZE_LIMITS.get("LARGE_FILE_THRESHOLD", 10 * 1024 * 1024)
            if file_size > max_bytes:
                raise RuntimeError(
                    f"File {file_path} too large for edit_blocks: {file_size} bytes (limit {max_bytes} bytes). "
                    "Use edit_block for large files or split the file."
                )

            # Read file once for all edits to this file
            content = fs_tools.read_file_internal(str(resolved), 0, 1 << 30, encoding=enc)

            if policy == "rollback":
                backups[file_path] = content

            # Apply edits sequentially, tracking cumulative changes
            current_content = content

            for idx, edit in file_edits:
                try:
                    old_string = edit.get("old_string", "")
                    new_string = edit.get("new_string", "")
                    expected_reps = edit.get("expected_replacements", 1)
                    ignore_ws = edit.get("ignore_whitespace", False)
                    norm_esc = edit.get("normalize_escapes", False)

                    if not old_string:
                        raise ValueError("old_string cannot be empty.")

                    # Perform the edit on current_content (in-memory)
                    edit_result = edit_tools.perform_single_edit_in_memory(
                        current_content,
                        old_string,
                        new_string,
                        expected_reps,
                        ignore_ws,
                        norm_esc,
                        file_path,
                    )

                    current_content = edit_result["new_content"]
                    results[idx] = {
                        "status": "success",
                        "message": edit_result["message"],
                        "file_path": file_path,
                        "replacements": edit_result["replacements"],
                        "locations": edit_result["locations"],
                    }
                    successful += 1

                except Exception as e:
                    failed += 1
                    results[idx] = {
                        "status": "error",
                        "file_path": file_path,
                        "error": str(e),
                    }

                    if policy == "fail-fast":
                        # Write what we have so far for this file
                        if current_content != content:
                            fs_tools.write_file(file_path, current_content, mode="rewrite", encoding=enc)
                            fs_tools._invalidate_file_cache(str(resolved))

                        return {
                            "status": "partial",
                            "message": f"Stopped at edit {idx}: {e}",
                            "total_edits": len(edits),
                            "successful_edits": successful,
                            "failed_edits": failed,
                            "results": [r for r in results if r is not None],
                        }

                    elif policy == "rollback":
                        raise  # Will be caught by outer try/except

            # Write final content for this file
            if current_content != content:
                fs_tools.write_file(file_path, current_content, mode="rewrite", encoding=enc)
                fs_tools._invalidate_file_cache(str(resolved))

        status = "success" if failed == 0 else "partial"
        return {
            "status": status,
            "message": f"Completed {successful}/{len(edits)} edits",
            "total_edits": len(edits),
            "successful_edits": successful,
            "failed_edits": failed,
            "results": results,
        }

    except Exception as e:
        if policy == "rollback":
            # Restore all backed-up files
            for file_path, original_content in backups.items():
                try:
                    fs_tools.write_file(file_path, original_content, mode="rewrite", encoding=enc)
                    fs_tools._invalidate_file_cache(file_path)
                except Exception:
                    pass  # Best effort rollback

            return {
                "status": "error",
                "message": f"Rolled back all changes due to error: {e}",
                "total_edits": len(edits),
                "successful_edits": 0,
                "failed_edits": failed,
                "results": [r for r in results if r is not None],
            }
        raise


@server.tool()
def convert_file_encoding(
    file_paths: List[str],
    source_encoding: str,
    target_encoding: str,
    error_handling: str = "strict",
    mismatch_policy: str = "warn-skip",
) -> list[dict]:
    """
    Convert files between encodings (utf-8, gbk, gb2312).

    Args:
        file_paths: List of absolute file paths.
        source_encoding: Current encoding.
        target_encoding: Desired encoding.
        error_handling: "strict" | "replace" | "ignore"
        mismatch_policy: "warn-skip" | "fail-fast" | "force"
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

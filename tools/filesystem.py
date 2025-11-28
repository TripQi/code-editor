from __future__ import annotations

import base64
import os
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
import logging

from .config import (
    FILE_OPERATION_TIMEOUTS,
    FILE_SIZE_LIMITS,
    READ_PERFORMANCE_THRESHOLDS,
    get_allowed_roots,
    list_allowed_roots,
    get_file_read_line_limit,
    get_file_write_line_limit,
    get_root,
    save_allowed_roots,
)
from .mime_utils import get_mime_type, is_image_file
from .timeouts import with_timeout

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

FileResult = Dict[str, object]
DEFAULT_MAX_NESTED_ITEMS = 100


def set_root_path(root_path: str) -> Path:
    candidate = Path(root_path).expanduser().resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"Root path not found: {root_path}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root_path}")

    roots = get_allowed_roots()

    # If an existing allowed dir already covers this candidate, skip persistence (avoid redundancy)
    for r in roots:
        if candidate == r or _is_relative_to(candidate, r):
            os.environ["CODE_EDIT_ROOT"] = str(candidate)
            return candidate

    # If this candidate is a parent of existing entries, replace them with the parent to keep list minimal
    pruned = [r for r in roots if not _is_relative_to(r, candidate)]
    pruned.append(candidate)
    save_allowed_roots(pruned)

    os.environ["CODE_EDIT_ROOT"] = str(candidate)
    return candidate


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _is_drive_root(p: Path) -> bool:
    return p == Path(p.anchor)


def _is_unrestricted(roots: List[Path]) -> bool:
    return not roots or any(str(p) == "/" for p in roots)


def _is_path_allowed(path: Path, allowed_roots: List[Path]) -> bool:
    if _is_unrestricted(allowed_roots):
        return True
    for allowed in allowed_roots:
        if _is_drive_root(allowed) and allowed.drive and path.drive.lower() == allowed.drive.lower():
            return True
        if _is_relative_to(path, allowed) or path == allowed:
            return True
    return False


def validate_path(requested_path: str | Path) -> Path:
    allowed_roots = get_allowed_roots()
    expanded = Path(requested_path).expanduser()
    base = get_root()
    absolute = expanded if expanded.is_absolute() else (base / expanded)
    absolute = absolute.resolve()

    if not _is_path_allowed(absolute, allowed_roots):
        allowed_display = ", ".join(str(p) for p in allowed_roots) if allowed_roots else "unrestricted"
        raise ValueError(
            f"Path not allowed: {requested_path}. Must be within one of these directories: {allowed_display}"
        )

    return absolute


def _count_lines(content: str) -> int:
    return content.count("\n") + 1 if content else 0


def _get_file_line_count(file_path: Path) -> Optional[int]:
    try:
        stats = file_path.stat()
        if stats.st_size < FILE_SIZE_LIMITS["LINE_COUNT_LIMIT"]:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return _count_lines(f.read())
    except OSError:
        return None
    return None


def _get_default_read_length() -> int:
    return get_file_read_line_limit()


def _get_binary_file_instructions(file_path: Path, mime_type: str) -> str:
    file_name = file_path.name
    return (
        f"Cannot read binary file as text: {file_name} ({mime_type})\n\n"
        "Use start_process + interact_with_process to analyze binary files with appropriate tools.\n\n"
        "The read_file tool only handles text files and images."
    )


def _is_binary_file(file_path: Path) -> bool:
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(1024)
        return b"\0" in chunk
    except OSError:
        return False


def _generate_status_message(read_lines: int, offset: int, total_lines: Optional[int], is_negative_offset: bool) -> str:
    if is_negative_offset:
        if total_lines is not None:
            return f"[Reading last {read_lines} lines (total: {total_lines} lines)]"
        return f"[Reading last {read_lines} lines]"
    if total_lines is not None:
        end_line = offset + read_lines
        remaining = max(0, total_lines - end_line)
        if offset == 0:
            return f"[Reading {read_lines} lines from start (total: {total_lines} lines, {remaining} remaining)]"
        return f"[Reading {read_lines} lines from line {offset} (total: {total_lines} lines, {remaining} remaining)]"
    if offset == 0:
        return f"[Reading {read_lines} lines from start]"
    return f"[Reading {read_lines} lines from line {offset}]"


def _read_last_n_lines_reverse(file_path: Path, n: int, mime_type: str, include_status_message: bool, file_total_lines: Optional[int]) -> FileResult:
    position = file_path.stat().st_size
    lines: List[str] = []
    partial = ""

    with open(file_path, "rb") as f:
        while position > 0 and len(lines) < n:
            read_size = min(READ_PERFORMANCE_THRESHOLDS["CHUNK_SIZE"], position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size).decode("utf-8", errors="replace")
            text = chunk + partial
            chunk_lines = text.split("\n")
            partial = chunk_lines.pop(0) if chunk_lines else ""
            lines = chunk_lines + lines

    if position == 0 and partial:
        lines.insert(0, partial)

    result_lines = lines[-n:]
    content = "\n".join(result_lines)
    if include_status_message:
        status = _generate_status_message(len(result_lines), -n, file_total_lines, True)
        content = f"{status}\n\n{content}"
    return {"content": content, "mimeType": mime_type, "isImage": False}


def _read_from_end_with_readline(file_path: Path, requested_lines: int, mime_type: str, include_status_message: bool, file_total_lines: Optional[int]) -> FileResult:
    buffer: deque[str] = deque(maxlen=requested_lines)
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            buffer.append(line.rstrip("\n"))

    result = list(buffer)
    content = "\n".join(result)
    if include_status_message:
        status = _generate_status_message(len(result), -requested_lines, file_total_lines, True)
        content = f"{status}\n\n{content}"
    return {"content": content, "mimeType": mime_type, "isImage": False}


def _read_from_start_with_readline(file_path: Path, offset: int, length: int, mime_type: str, include_status_message: bool, file_total_lines: Optional[int]) -> FileResult:
    result: List[str] = []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if idx >= offset and len(result) < length:
                result.append(line.rstrip("\n"))
            if len(result) >= length:
                break

    content = "\n".join(result)
    if include_status_message:
        status = _generate_status_message(len(result), offset, file_total_lines, False)
        content = f"{status}\n\n{content}"
    return {"content": content, "mimeType": mime_type, "isImage": False}


def _read_from_estimated_position(file_path: Path, offset: int, length: int, mime_type: str, include_status_message: bool, file_total_lines: Optional[int]) -> FileResult:
    sample_lines = 0
    bytes_read = 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            bytes_read += len(line.encode("utf-8"))
            sample_lines += 1
            if bytes_read >= READ_PERFORMANCE_THRESHOLDS["SAMPLE_SIZE"]:
                break

    if sample_lines == 0:
        return _read_from_start_with_readline(file_path, offset, length, mime_type, include_status_message, file_total_lines)

    avg_line_length = max(1, bytes_read // sample_lines)
    estimated_byte_position = offset * avg_line_length

    result: List[str] = []
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(min(estimated_byte_position, file_path.stat().st_size))
        first_line_skipped = False
        for line in f:
            if not first_line_skipped and estimated_byte_position > 0:
                first_line_skipped = True
                continue
            if len(result) < length:
                result.append(line.rstrip("\n"))
            else:
                break

    content = "\n".join(result)
    if include_status_message:
        status = _generate_status_message(len(result), offset, file_total_lines, False)
        content = f"{status}\n\n{content}"
    return {"content": content, "mimeType": mime_type, "isImage": False}


def _read_file_with_smart_positioning(file_path: Path, offset: int, length: int, mime_type: str, include_status_message: bool = True) -> FileResult:
    file_size = file_path.stat().st_size
    total_lines = _get_file_line_count(file_path)

    if offset < 0:
        requested_lines = abs(offset)
        if file_size > FILE_SIZE_LIMITS["LARGE_FILE_THRESHOLD"] and requested_lines <= READ_PERFORMANCE_THRESHOLDS["SMALL_READ_THRESHOLD"]:
            return _read_last_n_lines_reverse(file_path, requested_lines, mime_type, include_status_message, total_lines)
        return _read_from_end_with_readline(file_path, requested_lines, mime_type, include_status_message, total_lines)

    if file_size < FILE_SIZE_LIMITS["LARGE_FILE_THRESHOLD"] or offset == 0:
        return _read_from_start_with_readline(file_path, offset, length, mime_type, include_status_message, total_lines)

    if offset > READ_PERFORMANCE_THRESHOLDS["DEEP_OFFSET_THRESHOLD"]:
        return _read_from_estimated_position(file_path, offset, length, mime_type, include_status_message, total_lines)

    return _read_from_start_with_readline(file_path, offset, length, mime_type, include_status_message, total_lines)


def _read_file_from_disk(file_path: str, offset: int = 0, length: Optional[int] = None) -> FileResult:
    if not file_path or not isinstance(file_path, str):
        raise ValueError("Invalid file path provided")

    if length is None:
        length = _get_default_read_length()

    valid_path = validate_path(file_path)
    mime_type = get_mime_type(valid_path)
    is_image = is_image_file(mime_type)

    def _read_operation() -> FileResult:
        if is_image:
            with open(valid_path, "rb") as f:
                content = base64.b64encode(f.read()).decode("ascii")
            return {"content": content, "mimeType": mime_type, "isImage": True}
        try:
            return _read_file_with_smart_positioning(valid_path, offset, length, mime_type, True)
        except Exception as exc:
            if _is_binary_file(valid_path):
                instructions = _get_binary_file_instructions(valid_path, mime_type)
                return {"content": instructions, "mimeType": "text/plain", "isImage": False}
            raise exc

    return with_timeout(
        _read_operation,
        FILE_OPERATION_TIMEOUTS["FILE_READ"],
        f"Read file operation for {file_path} timed out",
    )
 

def read_file(file_path: str, offset: int = 0, length: Optional[int] = None) -> FileResult:
    return _read_file_from_disk(file_path, offset, length)


def read_file_internal(file_path: str, offset: int = 0, length: Optional[int] = None) -> str:
    if length is None:
        length = _get_default_read_length()
    valid_path = validate_path(file_path)

    mime_type = get_mime_type(valid_path)
    if is_image_file(mime_type):
        raise ValueError("Cannot read image files as text for internal operations")

    with open(valid_path, "r", encoding="utf-8", errors="strict") as f:
        content = f.read()

    if offset == 0 and length >= (1 << 53):  # mimic JS MAX_SAFE_INTEGER behavior
        return content

    lines = content.splitlines(keepends=True)
    selected = lines[offset : offset + length]
    return "".join(selected)


def write_file(file_path: str, content: str, mode: str = "rewrite", expected_mtime: float | None = None) -> None:
    valid_path = validate_path(file_path)

    if expected_mtime is not None and valid_path.exists():
        current = valid_path.stat().st_mtime
        if abs(current - expected_mtime) > 0.01:
            raise RuntimeError(
                f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current}."
            )

    if mode not in {"rewrite", "append"}:
        raise ValueError("mode must be 'rewrite' or 'append'")

    valid_path.parent.mkdir(parents=True, exist_ok=True)
    content_bytes = len(content.encode("utf-8"))
    line_count = _count_lines(content)
    logger.info("write_file: ext=%s bytes=%s lines=%s mode=%s", valid_path.suffix, content_bytes, line_count, mode)

    if mode == "append":
        with open(valid_path, "a", encoding="utf-8", newline="") as f:
            f.write(content)
    else:
        with open(valid_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def read_multiple_files(paths: List[str]) -> List[FileResult]:
    results: List[FileResult] = []
    for path in paths:
        try:
            file_result = read_file(path)
            results.append({
                "path": path,
                "content": file_result["content"],
                "mimeType": file_result["mimeType"],
                "isImage": file_result["isImage"],
            })
        except Exception as exc:  # pragma: no cover - user facing aggregation
            results.append({"path": path, "error": str(exc)})
    return results


def create_directory(dir_path: str) -> None:
    valid_path = validate_path(dir_path)
    valid_path.mkdir(parents=True, exist_ok=True)


def list_directory(dir_path: str, depth: int = 2) -> List[str]:
    valid_path = validate_path(dir_path)
    results: List[str] = []

    def _list(current: Path, current_depth: int, relative: str = "", is_top: bool = True) -> None:
        if current_depth <= 0:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            display_path = relative or current.name
            results.append(f"[DENIED] {display_path}")
            return

        total = len(entries)
        entries_to_show = entries
        filtered = 0
        if not is_top and total > DEFAULT_MAX_NESTED_ITEMS:
            entries_to_show = entries[:DEFAULT_MAX_NESTED_ITEMS]
            filtered = total - DEFAULT_MAX_NESTED_ITEMS

        for entry in entries_to_show:
            display = os.path.join(relative, entry.name) if relative else entry.name
            results.append(f"[DIR] {display}" if entry.is_dir() else f"[FILE] {display}")
            if entry.is_dir() and current_depth > 1:
                try:
                    validate_path(entry)
                    _list(entry, current_depth - 1, display, False)
                except Exception:
                    continue

        if filtered > 0:
            display_path = relative or current.name
            results.append(
                f"[WARNING] {display_path}: {filtered} items hidden (showing first {DEFAULT_MAX_NESTED_ITEMS} of {total} total)"
            )

    _list(valid_path, depth, "", True)
    return results


def move_file(source_path: str, destination_path: str, expected_mtime: float | None = None) -> None:
    source = validate_path(source_path)
    dest = validate_path(destination_path)

    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source_path}")
    if dest.exists():
        raise FileExistsError(f"Destination already exists: {destination_path}")

    if expected_mtime is not None:
        current = source.stat().st_mtime
        if abs(current - expected_mtime) > 0.01:
            raise RuntimeError(
                f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current}."
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)


def get_file_info(file_path: str) -> Dict[str, object]:
    valid_path = validate_path(file_path)
    stats = valid_path.stat()

    info: Dict[str, object] = {
        "size": stats.st_size,
        "created": stats.st_ctime,
        "modified": stats.st_mtime,
        "accessed": stats.st_atime,
        "isDirectory": valid_path.is_dir(),
        "isFile": valid_path.is_file(),
        "permissions": oct(stats.st_mode)[-3:],
    }

    if valid_path.is_file() and stats.st_size < FILE_SIZE_LIMITS["LINE_COUNT_LIMIT"]:
        mime_type = get_mime_type(valid_path)
        if not is_image_file(mime_type):
            try:
                with open(valid_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                line_count = _count_lines(content)
                info["lineCount"] = line_count
                info["lastLine"] = max(0, line_count - 1)
                info["appendPosition"] = line_count
            except OSError:
                pass
    return info

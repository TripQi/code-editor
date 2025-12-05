from __future__ import annotations

import base64
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, cast
import logging
from fnmatch import fnmatch

from .config import (
    FILE_OPERATION_TIMEOUTS,
    FILE_SIZE_LIMITS,
    READ_PERFORMANCE_THRESHOLDS,
    DEFAULT_IGNORE_PATTERNS,
    get_allowed_roots,
    list_allowed_roots,
    get_file_read_line_limit,
    get_file_write_line_limit,
    save_allowed_roots,
)
from .mime_utils import get_mime_type, is_image_file
from .timeouts import with_timeout

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

FileResult = Dict[str, object]
DEFAULT_MAX_NESTED_ITEMS = 100
MTIME_EPSILON_NS = 10_000_000  # 10ms tolerance
ENCODING_ALIASES = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf_8": "utf-8",
    "gbk": "gbk",
    "cp936": "gbk",
    "gb2312": "gb2312",
    "gb-2312": "gb2312",
}

# Encoding detection cache: {file_path: (mtime_ns, encoding, confidence)}
_encoding_cache: Dict[str, tuple[int, Optional[str], Optional[float]]] = {}
# File metadata cache: {file_path: info_dict} keyed by current mtime_ns
_file_info_cache: Dict[str, Dict[str, object]] = {}


def normalize_encoding(encoding: str | None) -> str:
    """
    Normalize user-supplied encoding names and enforce the supported set.

    Supported encodings: utf-8 (default), gbk, gb2312.
    """
    if encoding is None or encoding == "":
        return "utf-8"
    normalized = encoding.strip().lower()
    if normalized not in ENCODING_ALIASES:
        raise ValueError("Unsupported encoding. Allowed: utf-8, gbk, gb2312.")
    return ENCODING_ALIASES[normalized]


def _normalize_encoding_loose(encoding: object) -> Optional[str]:
    """
    Best-effort normalization for detected encodings; returns None if unknown.
    """
    if not isinstance(encoding, str):
        return None
    key = encoding.strip().lower()
    return ENCODING_ALIASES.get(key, key)


def set_root_path(root_path: str) -> Path:
    if not Path(root_path).expanduser().is_absolute():
        raise ValueError("root_path must be an absolute path.")
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


def _normalize_expected_mtime(expected: float | int | None) -> Optional[int]:
    if expected is None:
        return None
    if expected > 1e12:  # assume nanoseconds
        return int(expected)
    return int(expected * 1_000_000_000)


def _current_mtime_ns(path: Path) -> int:
    stats = path.stat()
    return getattr(stats, "st_mtime_ns", int(stats.st_mtime * 1_000_000_000))


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
    if not expanded.is_absolute():
        raise ValueError("Path must be absolute. Relative paths are not allowed.")
    absolute = expanded.resolve()

    if not _is_path_allowed(absolute, allowed_roots):
        allowed_display = ", ".join(str(p) for p in allowed_roots) if allowed_roots else "unrestricted"
        raise ValueError(
            f"Path not allowed: {requested_path}. Must be within one of these directories: {allowed_display}"
        )

    return absolute


def _count_lines(content: str) -> int:
    return content.count("\n") + 1 if content else 0


def _get_file_line_count(file_path: Path, encoding: str) -> Optional[int]:
    try:
        stats = file_path.stat()
        if stats.st_size < FILE_SIZE_LIMITS["LINE_COUNT_LIMIT"]:
            with open(file_path, "r", encoding=encoding, errors="ignore") as f:
                return _count_lines(f.read())
    except OSError:
        return None
    return None


def _get_default_read_length() -> int:
    return get_file_read_line_limit()


def _resolve_effective_encoding(user_encoding: Optional[str], detected_encoding: Optional[str]) -> str:
    """
    Determine which encoding to use for reading.

    Priority:
    1) user explicit encoding (unless empty/auto/None)
    2) detected encoding from cached metadata
    3) utf-8 fallback
    """
    if user_encoding is None:
        return detected_encoding or "utf-8"

    if isinstance(user_encoding, str):
        trimmed = user_encoding.strip().lower()
        if trimmed == "" or trimmed == "auto":
            return detected_encoding or "utf-8"

    return normalize_encoding(user_encoding)


def _get_binary_file_instructions(file_path: Path, mime_type: str) -> str:
    file_name = file_path.name
    return (
        f"Cannot read binary file as text: {file_name} ({mime_type})\n\n"
        "Use start_process + interact_with_process to analyze binary files with appropriate tools.\n\n"
        "The read_file tool only handles text files and images."
    )


def _build_file_metadata(valid_path: Path, stats: os.stat_result | None = None) -> Dict[str, object]:
    """
    Collect fresh file metadata and encoding info. This function always trusts the
    provided stat result (to avoid double stat) and refreshes caches.
    """
    if stats is None:
        stats = valid_path.stat()

    mtime_ns = getattr(stats, "st_mtime_ns", int(stats.st_mtime * 1_000_000_000))
    size = stats.st_size

    info: Dict[str, object] = {
        "mtime_ns": mtime_ns,
        "size": size,
        "created": stats.st_ctime,
        "modified": stats.st_mtime,
        "accessed": stats.st_atime,
        "isDirectory": valid_path.is_dir(),
        "isFile": valid_path.is_file(),
        "permissions": oct(stats.st_mode)[-3:],
    }

    mime_type = get_mime_type(valid_path)
    info["mimeType"] = mime_type
    is_image = is_image_file(mime_type)
    info["isImage"] = is_image

    # Binary / encoding detection
    info["isBinary"] = False
    encoding: Optional[str] = None
    confidence: Optional[float] = None
    if not is_image:
        try:
            if _is_binary_file(valid_path):
                info["isBinary"] = True
            else:
                enc_info = detect_file_encoding(str(valid_path))
                encoding = enc_info.get("encoding")  # type: ignore[arg-type]
                confidence = enc_info.get("confidence")  # type: ignore[arg-type]
        except Exception:
            # Best-effort only; swallow and continue with defaults
            pass

    info["encoding"] = encoding
    info["encodingConfidence"] = confidence

    # Optional lightweight line count for small text files
    if (
        not is_image
        and not info["isBinary"]
        and size < FILE_SIZE_LIMITS["LINE_COUNT_LIMIT"]
    ):
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


def _get_cached_file_metadata(valid_path: Path, stats: os.stat_result | None = None) -> Dict[str, object]:
    """
    Return cached metadata if mtime_ns matches; otherwise rebuild and refresh cache.
    """
    if stats is None:
        stats = valid_path.stat()
    current_mtime = getattr(stats, "st_mtime_ns", int(stats.st_mtime * 1_000_000_000))
    path_str = str(valid_path)

    cached = _file_info_cache.get(path_str)
    if cached and cached.get("mtime_ns") == current_mtime:
        return cached

    fresh = _build_file_metadata(valid_path, stats)
    _file_info_cache[path_str] = fresh
    return fresh


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


def _read_last_n_lines_reverse(
    file_path: Path,
    n: int,
    mime_type: str,
    encoding: str,
    include_status_message: bool,
    file_total_lines: Optional[int],
) -> FileResult:
    position = file_path.stat().st_size
    lines: List[str] = []
    partial = ""

    with open(file_path, "rb") as f:
        while position > 0 and len(lines) < n:
            read_size = min(READ_PERFORMANCE_THRESHOLDS["CHUNK_SIZE"], position)
            position -= read_size
            f.seek(position)
            chunk = f.read(read_size).decode(encoding, errors="replace")
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


def _read_from_end_with_readline(
    file_path: Path,
    requested_lines: int,
    mime_type: str,
    encoding: str,
    include_status_message: bool,
    file_total_lines: Optional[int],
) -> FileResult:
    buffer: deque[str] = deque(maxlen=requested_lines)
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        for line in f:
            buffer.append(line.rstrip("\n"))

    result = list(buffer)
    content = "\n".join(result)
    if include_status_message:
        status = _generate_status_message(len(result), -requested_lines, file_total_lines, True)
        content = f"{status}\n\n{content}"
    return {"content": content, "mimeType": mime_type, "isImage": False}


def _read_from_start_with_readline(
    file_path: Path,
    offset: int,
    length: int,
    mime_type: str,
    encoding: str,
    include_status_message: bool,
    file_total_lines: Optional[int],
) -> FileResult:
    result: List[str] = []
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
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


def _read_from_estimated_position(
    file_path: Path,
    offset: int,
    length: int,
    mime_type: str,
    encoding: str,
    include_status_message: bool,
    file_total_lines: Optional[int],
) -> FileResult:
    sample_lines = 0
    bytes_read = 0
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        for line in f:
            bytes_read += len(line.encode(encoding, errors="replace"))
            sample_lines += 1
            if bytes_read >= READ_PERFORMANCE_THRESHOLDS["SAMPLE_SIZE"]:
                break

    if sample_lines == 0:
        return _read_from_start_with_readline(
            file_path, offset, length, mime_type, encoding, include_status_message, file_total_lines
        )

    avg_line_length = max(1, bytes_read // sample_lines)
    estimated_byte_position = offset * avg_line_length

    result: List[str] = []
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
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


def _read_file_with_smart_positioning(
    file_path: Path,
    offset: int,
    length: int,
    mime_type: str,
    encoding: str,
    include_status_message: bool = True,
) -> FileResult:
    file_size = file_path.stat().st_size
    # Only count lines for small files to avoid expensive full-file scans
    total_lines = None
    if file_size < FILE_SIZE_LIMITS["LARGE_FILE_THRESHOLD"]:
        total_lines = _get_file_line_count(file_path, encoding)

    if offset < 0:
        requested_lines = abs(offset)
        if file_size > FILE_SIZE_LIMITS["LARGE_FILE_THRESHOLD"] and requested_lines <= READ_PERFORMANCE_THRESHOLDS["SMALL_READ_THRESHOLD"]:
            return _read_last_n_lines_reverse(file_path, requested_lines, mime_type, encoding, include_status_message, total_lines)
        return _read_from_end_with_readline(file_path, requested_lines, mime_type, encoding, include_status_message, total_lines)

    if file_size < FILE_SIZE_LIMITS["LARGE_FILE_THRESHOLD"] or offset == 0:
        return _read_from_start_with_readline(file_path, offset, length, mime_type, encoding, include_status_message, total_lines)

    if offset > READ_PERFORMANCE_THRESHOLDS["DEEP_OFFSET_THRESHOLD"]:
        return _read_from_estimated_position(file_path, offset, length, mime_type, encoding, include_status_message, total_lines)

    return _read_from_start_with_readline(file_path, offset, length, mime_type, encoding, include_status_message, total_lines)


def _read_file_from_disk(
    file_path: str, offset: int = 0, length: Optional[int] = None, encoding: Optional[str] = None
) -> FileResult:
    if not file_path or not isinstance(file_path, str):
        raise ValueError("Invalid file path provided")

    if length is None:
        length = _get_default_read_length()

    valid_path = validate_path(file_path)
    stats = valid_path.stat()
    meta = _get_cached_file_metadata(valid_path, stats)
    mime_type = cast(str, meta.get("mimeType") or "text/plain")
    is_image = bool(meta.get("isImage"))
    is_binary = bool(meta.get("isBinary"))
    enc = _resolve_effective_encoding(encoding, meta.get("encoding"))  # type: ignore[arg-type]

    def _read_operation() -> FileResult:
        if is_image:
            with open(valid_path, "rb") as f:
                content = base64.b64encode(f.read()).decode("ascii")
            return {"content": content, "mimeType": mime_type, "isImage": True}

        if is_binary:
            instructions = _get_binary_file_instructions(valid_path, mime_type)
            return {"content": instructions, "mimeType": "text/plain", "isImage": False}

        try:
            return _read_file_with_smart_positioning(valid_path, offset, length, mime_type, enc, True)
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
 

def read_file(
    file_path: str,
    offset: int = 0,
    length: Optional[int] = None,
    encoding: Optional[str] = None,
) -> FileResult:
    return _read_file_from_disk(file_path, offset, length, encoding)


def read_file_internal(
    file_path: str, offset: int = 0, length: Optional[int] = None, encoding: str = "utf-8"
) -> str:
    if length is None:
        length = _get_default_read_length()
    enc = normalize_encoding(encoding)
    valid_path = validate_path(file_path)

    mime_type = get_mime_type(valid_path)
    if is_image_file(mime_type):
        raise ValueError("Cannot read image files as text for internal operations")

    with open(valid_path, "r", encoding=enc, errors="strict") as f:
        content = f.read()

    if offset == 0 and length >= (1 << 53):  # mimic JS MAX_SAFE_INTEGER behavior
        return content

    lines = content.splitlines(keepends=True)
    selected = lines[offset : offset + length]
    return "".join(selected)


def write_file(
    file_path: str,
    content: str,
    mode: str = "rewrite",
    expected_mtime: float | None = None,
    encoding: str = "utf-8",
) -> None:
    valid_path = validate_path(file_path)
    enc = normalize_encoding(encoding)

    if expected_mtime is not None and valid_path.exists():
        expected_ns = _normalize_expected_mtime(expected_mtime)
        current_ns = _current_mtime_ns(valid_path)
        if expected_ns is not None and abs(current_ns - expected_ns) > MTIME_EPSILON_NS:
            raise RuntimeError(
                f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current_ns / 1_000_000_000:.9f}."
            )

    if mode not in {"rewrite", "append"}:
        raise ValueError("mode must be 'rewrite' or 'append'")

    content_bytes = len(content.encode(enc, errors="replace"))
    line_count = _count_lines(content)
    logger.info("write_file: ext=%s bytes=%s lines=%s mode=%s", valid_path.suffix, content_bytes, line_count, mode)

    if mode == "append":
        # Use atomic write for append mode too by reading existing content first
        existing_content = ""
        if valid_path.exists():
            try:
                existing_content = valid_path.read_text(encoding=enc)
            except Exception as e:
                logger.warning(f"Failed to read existing content for atomic append: {e}")
                # Fallback to direct append if read fails
                with open(valid_path, "a", encoding=enc, newline="") as f:
                    f.write(content)
                return
        _atomic_write(valid_path, existing_content + content, encoding=enc)
    else:
        _atomic_write(valid_path, content, encoding=enc)


def read_multiple_files(paths: List[str], encoding: str = "utf-8") -> List[FileResult]:
    results: List[FileResult] = []
    enc = normalize_encoding(encoding)
    for path in paths:
        try:
            file_result = read_file(path, encoding=enc)
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


def _normalize_ignore_patterns(patterns: Optional[List[str]]) -> List[str]:
    if patterns is None:
        return list(DEFAULT_IGNORE_PATTERNS)
    if not all(isinstance(p, str) for p in patterns):
        raise ValueError("ignore_patterns elements must all be strings.")
    return list(patterns)  # empty list means no ignores


def list_directory(dir_path: str, depth: int = 2, ignore_patterns: Optional[List[str]] = None) -> List[str]:
    valid_path = validate_path(dir_path)
    results: List[str] = []
    patterns = _normalize_ignore_patterns(ignore_patterns)

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
            if any(fnmatch(entry.name, pat) for pat in patterns):
                continue
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
        expected_ns = _normalize_expected_mtime(expected_mtime)
        current_ns = _current_mtime_ns(source)
        if expected_ns is not None and abs(current_ns - expected_ns) > MTIME_EPSILON_NS:
            raise RuntimeError(
                f"Conflict: File modified by another process. Expected mtime {expected_mtime}, got {current_ns / 1_000_000_000:.9f}."
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)


def convert_file_encoding(
    file_paths: List[str],
    source_encoding: str,
    target_encoding: str,
    error_handling: str = "strict",
    mismatch_policy: str = "warn-skip",
) -> List[Dict[str, object]]:
    """
    Convert one or more text files from source_encoding to target_encoding in-place.

    - file_paths must be absolute paths within allowed directories.
    - Supported encodings: utf-8, gbk, gb2312 (normalized via normalize_encoding).
    - error_handling: 'strict' | 'replace' | 'ignore', applied to both read and write.
    - mismatch_policy:
        * 'warn-skip' (default): 检测到编码不一致则跳过该文件并标记 mismatch。
        * 'fail-fast': 发现首个编码不一致立即返回，status=error。
        * 'force': 忽略检测结果，仍按 source_encoding 转换，但会返回 detectedEncoding/mismatch。
    """
    err_mode = error_handling.lower()
    if err_mode not in {"strict", "replace", "ignore"}:
        raise ValueError("error_handling must be one of: strict, replace, ignore.")
    policy = mismatch_policy.lower()
    if policy not in {"warn-skip", "fail-fast", "force"}:
        raise ValueError("mismatch_policy must be one of: warn-skip, fail-fast, force.")

    src_enc = normalize_encoding(source_encoding)
    tgt_enc = normalize_encoding(target_encoding)

    results: List[Dict[str, object]] = []

    for path in file_paths:
        try:
            valid_path = validate_path(path)
            if not valid_path.exists():
                raise FileNotFoundError(f"File not found: {path}")
            if not valid_path.is_file():
                raise IsADirectoryError(f"Not a file: {path}")
            if _is_binary_file(valid_path):
                raise ValueError("File appears to be binary; conversion skipped.")

            detected_enc = None
            detected_conf = None
            try:
                enc_info = detect_file_encoding(str(valid_path))
                detected_enc = enc_info.get("encoding")
                detected_conf = enc_info.get("confidence")
            except Exception:
                pass

            det_norm = _normalize_encoding_loose(detected_enc)
            mismatch = det_norm is not None and det_norm != src_enc.lower()
            if mismatch and policy == "fail-fast":
                results.append(
                    {
                        "path": str(valid_path),
                        "status": "error",
                        "detail": (
                            f"encoding mismatch: expected {src_enc}, detected {detected_enc or 'unknown'}; "
                            f"policy=fail-fast"
                        ),
                        "detectedEncoding": detected_enc,
                        "detectedConfidence": detected_conf,
                        "mismatch": True,
                    }
                )
                return results
            if mismatch and policy == "warn-skip":
                results.append(
                    {
                        "path": str(valid_path),
                        "status": "mismatch",
                        "detail": (
                            f"encoding mismatch: expected {src_enc}, detected {detected_enc or 'unknown'}; skipped"
                        ),
                        "detectedEncoding": detected_enc,
                        "detectedConfidence": detected_conf,
                        "mismatch": True,
                    }
                )
                continue

            with open(valid_path, "r", encoding=src_enc, errors=err_mode) as f:
                content = f.read()

            _atomic_write(valid_path, content, encoding=tgt_enc, errors=err_mode)

            results.append(
                {
                    "path": str(valid_path),
                    "status": "success",
                    "detail": f"Converted from {src_enc} to {tgt_enc}",
                    "detectedEncoding": detected_enc,
                    "detectedConfidence": detected_conf,
                    "mismatch": mismatch,
                }
            )
        except Exception as exc:  # pragma: no cover - user-facing aggregation
            results.append({"path": str(path), "status": "error", "detail": str(exc)})

    return results


def detect_file_encoding(file_path: str, sample_size: int = 200_000) -> Dict[str, object]:
    """
    Detect text encoding of a file using charset-normalizer.

    - file_path must be an absolute path within allowed directories.
    - sample_size limits the number of bytes read for detection.
    - Returns {"encoding": str | None, "confidence": float | None}.
    - Uses mtime-based caching to avoid repeated expensive detection.
    """
    try:
        from charset_normalizer import from_bytes
    except Exception as exc:
        raise ImportError(
            "charset-normalizer is required for encoding detection. Install with `pip install charset-normalizer`."
        ) from exc

    path = validate_path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")
    if _is_binary_file(path):
        raise ValueError("Binary file detected; encoding detection is only for text files.")

    # Check cache first
    path_str = str(path)
    current_mtime = _current_mtime_ns(path)
    if path_str in _encoding_cache:
        cached_mtime, cached_encoding, cached_confidence = _encoding_cache[path_str]
        if cached_mtime == current_mtime:
            logger.debug(f"Encoding cache hit for {path_str}")
            return {"encoding": cached_encoding, "confidence": cached_confidence}

    # Cache miss or stale - perform detection
    with open(path, "rb") as f:
        data = f.read(sample_size)

    result = from_bytes(data).best()
    if result is None:
        encoding_result: Dict[str, object] = {"encoding": None, "confidence": None}
        _encoding_cache[path_str] = (current_mtime, None, None)
        return encoding_result

    confidence: Optional[float] = None
    fingerprint = getattr(result, "fingerprint", None)
    if isinstance(fingerprint, dict):
        coherence = fingerprint.get("coherence")
        try:
            if coherence is not None:
                confidence = float(coherence)
        except Exception:
            confidence = None
    if confidence is None and hasattr(result, "confidence"):
        try:
            confidence = float(getattr(result, "confidence"))
        except Exception:
            confidence = None

    detected_encoding = getattr(result, "encoding", None)

    # Update cache
    _encoding_cache[path_str] = (current_mtime, detected_encoding, confidence)
    logger.debug(f"Encoding detection for {path_str}: {detected_encoding} (confidence: {confidence})")

    return {"encoding": detected_encoding, "confidence": confidence}


def _atomic_write(target: Path, content: str, encoding: str = "utf-8", errors: str = "strict") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, encoding=encoding, errors=errors, newline="", dir=target.parent
        ) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        if target.exists():
            try:
                os.chmod(temp_path, target.stat().st_mode)
            except OSError:
                pass
        os.replace(temp_path, target)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def get_file_info(file_path: str) -> Dict[str, object]:
    valid_path = validate_path(file_path)
    meta = _get_cached_file_metadata(valid_path)
    # Trim to public shape while keeping refreshed values
    result_keys = [
        "size",
        "created",
        "modified",
        "accessed",
        "isDirectory",
        "isFile",
        "permissions",
        "encoding",
        "encodingConfidence",
        "lineCount",
        "lastLine",
        "appendPosition",
    ]
    return {k: meta[k] for k in result_keys if k in meta}

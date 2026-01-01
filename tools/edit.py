from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime
from difflib import SequenceMatcher, ndiff
from pathlib import Path
from typing import Dict, TypedDict

from .config import FILE_SIZE_LIMITS, get_file_write_line_limit
from .filesystem import normalize_encoding, read_file_internal, validate_path, write_file

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

FUZZY_THRESHOLD = 0.7
FUZZY_LOG_PATH = Path(tempfile.gettempdir()) / "code_edit_fuzzy.log"


def detect_line_ending(content: str) -> str:
    if "\r\n" in content:
        return "\r\n"
    if "\r" in content:
        return "\r"
    return "\n"


def normalize_line_endings(text: str, target: str) -> str:
    # Normalize to LF first, then to target
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\n":
        return normalized
    if target == "\r\n":
        return normalized.replace("\n", "\r\n")
    if target == "\r":
        return normalized.replace("\n", "\r")
    return normalized


def _highlight_differences(expected: str, actual: str) -> str:
    diff = ndiff(expected.splitlines(), actual.splitlines())
    return "\n".join(diff)


class FuzzyMatchResult(TypedDict):
    value: str
    similarity: float


class EditLocation(TypedDict):
    """Location of a single edit within a file."""
    start_line: int
    end_line: int
    start_col: int
    end_col: int


class EditResult(TypedDict):
    """Structured result from edit operations."""
    status: str  # "success" | "error"
    message: str
    file_path: str
    replacements: int
    locations: list  # List[EditLocation]


def _index_to_line_col(text: str, index: int) -> tuple[int, int]:
    """Convert character index to 1-based (line, column)."""
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    column = index - last_newline
    return line, column


def _compute_edit_location(content: str, start_idx: int, end_idx: int) -> EditLocation:
    """Compute line/column location for an edit span."""
    start_line, start_col = _index_to_line_col(content, start_idx)
    end_line, end_col = _index_to_line_col(content, end_idx)
    return EditLocation(
        start_line=start_line,
        end_line=end_line,
        start_col=start_col,
        end_col=end_col,
    )


def _best_fuzzy_match(content: str, needle: str) -> FuzzyMatchResult:
    if not content or not needle:
        return {"value": "", "similarity": 0.0}
    window = max(1, len(needle))
    step = max(1, window // 5)
    best_ratio = 0.0
    best_value = ""
    limit = len(content) - window
    for i in range(0, max(1, limit + 1), step):
        segment = content[i : i + window]
        ratio = SequenceMatcher(None, needle, segment).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_value = segment
    return {"value": best_value, "similarity": best_ratio}


def _log_fuzzy_entry(data: Dict[str, object]) -> Path:
    FUZZY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FUZZY_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.utcnow().isoformat()}] {data}\n")
    return FUZZY_LOG_PATH


def _build_whitespace_insensitive_pattern(text: str) -> str:
    """
    Convert a literal string to a regex that collapses contiguous whitespace to \\s+.
    Keeps other characters escaped to avoid unintended regex meaning.
    """
    parts: list[str] = []
    whitespace_open = False
    for ch in text:
        if ch.isspace():
            if not whitespace_open:
                parts.append(r"\s+")
                whitespace_open = True
        else:
            parts.append(re.escape(ch))
            whitespace_open = False
    return "".join(parts)


def _unescape_literal(text: str) -> str:
    """Best-effort unescape common backslash escapes (\", \\n, \\t, \\\\) without failing."""
    try:
        return bytes(text, "utf-8").decode("unicode_escape")
    except Exception:
        return text


def perform_search_replace(
    file_path: str,
    search: str,
    replace: str,
    expected_replacements: int = 1,
    expected_mtime: float | None = None,
    ignore_whitespace: bool = False,
    normalize_escapes: bool = False,
    encoding: str = "utf-8",
) -> EditResult:
    if search == "":
        raise ValueError("Empty search strings are not allowed. Please provide a non-empty string to search for.")

    enc = normalize_encoding(encoding)
    valid_path = validate_path(file_path)
    # 防御性限制：拒绝处理超大文件，避免一次性读入导致内存/CPU DoS
    max_bytes = FILE_SIZE_LIMITS.get("LARGE_FILE_THRESHOLD", 10 * 1024 * 1024)
    file_size = valid_path.stat().st_size
    if file_size > max_bytes:
        raise RuntimeError(
            f"File too large for edit_block: {file_size} bytes (limit {max_bytes} bytes). "
            "Please narrow the edit range or split the file."
        )

    content = read_file_internal(str(valid_path), 0, 1 << 30, encoding=enc)
    line_ending = detect_line_ending(content)
    if normalize_escapes:
        search = _unescape_literal(search)
    normalized_search = normalize_line_endings(search, line_ending)
    warning = ""
    max_lines = max(search.count("\n") + 1, replace.count("\n") + 1)
    if max_lines > get_file_write_line_limit():
        warning = (
            f"\n\nWARNING: The longer text block has {max_lines} lines (maximum: {get_file_write_line_limit()}). "
            "Consider smaller chunks for large edits."
        )

    # Exact match path
    count = content.count(normalized_search)
    if count > 0:
        if count != expected_replacements:
            raise RuntimeError(
                f"Expected {expected_replacements} occurrences but found {count} in {file_path}. "
                "Adjust expected_replacements or make the search string more specific."
            )

        locations: list = []  # List[EditLocation]

        if expected_replacements == 1:
            idx = content.index(normalized_search)
            end_idx = idx + len(normalized_search)
            locations.append(_compute_edit_location(content, idx, end_idx))
            new_content = (
                content[:idx]
                + normalize_line_endings(replace, line_ending)
                + content[end_idx:]
            )
        else:
            # Multiple replacements - find all positions before replacing
            search_start = 0
            for _ in range(expected_replacements):
                idx = content.index(normalized_search, search_start)
                end_idx = idx + len(normalized_search)
                locations.append(_compute_edit_location(content, idx, end_idx))
                search_start = end_idx
            new_content = content.replace(normalized_search, normalize_line_endings(replace, line_ending))

        write_file(str(valid_path), new_content, mode="rewrite", expected_mtime=expected_mtime, encoding=enc)

        # Build location summary for message
        loc_summary = ", ".join(
            f"lines {loc['start_line']}-{loc['end_line']}" if loc['start_line'] != loc['end_line']
            else f"line {loc['start_line']}"
            for loc in locations
        )

        return EditResult(
            status="success",
            message=f"Applied {expected_replacements} edit(s) to {file_path} ({loc_summary}){warning}",
            file_path=str(valid_path),
            replacements=expected_replacements,
            locations=locations,
        )

    # Whitespace-insensitive exact match path (optional)
    if ignore_whitespace:
        pattern = _build_whitespace_insensitive_pattern(normalized_search)
        flags = re.MULTILINE | re.DOTALL
        matches = list(re.finditer(pattern, content, flags))
        match_count = len(matches)
        if match_count > 0 and match_count != expected_replacements:
            raise RuntimeError(
                f"Expected {expected_replacements} whitespace-insensitive occurrence(s) but found {match_count} in {file_path}. "
                "Adjust expected_replacements or make the search string more specific."
            )
        if match_count > 0:
            # Collect locations from match objects
            locations = [
                _compute_edit_location(content, m.start(), m.end())
                for m in matches[:expected_replacements]
            ]

            replacement = normalize_line_endings(replace, line_ending)
            new_content, sub_count = re.subn(
                pattern, replacement, content, count=expected_replacements, flags=flags
            )
            match_line_count = max(1, normalized_search.count("\n") + 1)
            # Sanity check for over-greedy matches: ensure each replacement is not far larger than the source
            for m in matches[:expected_replacements]:
                matched_lines = max(1, m.group(0).count("\n") + 1)
                if matched_lines > match_line_count * 1.5:
                    raise RuntimeError(
                        f"Whitespace-insensitive match spanned {matched_lines} lines vs expected {match_line_count}. "
                        "Match considered too greedy; aborting to avoid unintended large edits."
                    )
            if sub_count != expected_replacements:
                raise RuntimeError(
                    f"Whitespace-insensitive replace updated {sub_count} occurrence(s), expected {expected_replacements}. "
                    "Please retry with a more specific search string."
                )
            write_file(str(valid_path), new_content, mode="rewrite", expected_mtime=expected_mtime, encoding=enc)

            # Build location summary for message
            loc_summary = ", ".join(
                f"lines {loc['start_line']}-{loc['end_line']}" if loc['start_line'] != loc['end_line']
                else f"line {loc['start_line']}"
                for loc in locations
            )

            return EditResult(
                status="success",
                message=f"Applied {expected_replacements} edit(s) to {file_path} with whitespace-insensitive matching ({loc_summary}){warning}",
                file_path=str(valid_path),
                replacements=expected_replacements,
                locations=locations,
            )

    # Fuzzy path
    fuzzy_result = _best_fuzzy_match(content, search)
    similarity = float(fuzzy_result["similarity"])
    diff = _highlight_differences(search, fuzzy_result["value"])
    log_path = _log_fuzzy_entry(
        {
            "file": str(valid_path),
            "similarity": similarity,
            "search_length": len(search),
            "found_length": len(fuzzy_result["value"]),
        }
    )

    if similarity >= FUZZY_THRESHOLD:
        raise RuntimeError(
            f"Exact match not found, but found a similar text with {round(similarity * 100)}% similarity.\n"
            f"Differences:\n{diff}\n\n"
            "To replace this text, use the exact text found in the file, "
            "or retry with normalize_escapes=True if your search string contained escaped quotes/backslashes.\n\n"
            f"Log entry: {log_path}"
        )

    raise RuntimeError(
        f"Search content not found in {file_path}. The closest match was '{fuzzy_result['value']}' "
        f"with only {round(FUZZY_THRESHOLD * 100)}% similarity (threshold {round(FUZZY_THRESHOLD * 100)}%).\n\n"
        f"Differences:\n{diff}\n\n"
        "If the search string includes escaped quotes/backslashes, retry with normalize_escapes=True.\n\n"
        f"Log entry: {log_path}"
    )


def perform_single_edit_in_memory(
    content: str,
    search: str,
    replace: str,
    expected_replacements: int = 1,
    ignore_whitespace: bool = False,
    normalize_escapes: bool = False,
    file_path: str = "",
) -> dict:
    """
    Perform a single edit on in-memory content.

    Returns:
        {
            "new_content": str,
            "message": str,
            "replacements": int,
            "locations": List[EditLocation],
        }
    """
    if search == "":
        raise ValueError("Empty search strings are not allowed.")

    line_ending = detect_line_ending(content)
    if normalize_escapes:
        search = _unescape_literal(search)
    normalized_search = normalize_line_endings(search, line_ending)

    # Exact match path
    count = content.count(normalized_search)
    if count > 0:
        if count != expected_replacements:
            raise RuntimeError(
                f"Expected {expected_replacements} occurrences but found {count}."
            )

        locations: list = []  # List[EditLocation]

        if expected_replacements == 1:
            idx = content.index(normalized_search)
            end_idx = idx + len(normalized_search)
            locations.append(_compute_edit_location(content, idx, end_idx))
            new_content = (
                content[:idx]
                + normalize_line_endings(replace, line_ending)
                + content[end_idx:]
            )
        else:
            # Multiple replacements - find all positions before replacing
            search_start = 0
            for _ in range(expected_replacements):
                idx = content.index(normalized_search, search_start)
                end_idx = idx + len(normalized_search)
                locations.append(_compute_edit_location(content, idx, end_idx))
                search_start = end_idx
            new_content = content.replace(
                normalized_search, normalize_line_endings(replace, line_ending)
            )

        loc_summary = ", ".join(
            f"lines {loc['start_line']}-{loc['end_line']}" if loc['start_line'] != loc['end_line']
            else f"line {loc['start_line']}"
            for loc in locations
        )

        return {
            "new_content": new_content,
            "message": f"Applied {expected_replacements} edit(s) ({loc_summary})",
            "replacements": expected_replacements,
            "locations": locations,
        }

    # Whitespace-insensitive path
    if ignore_whitespace:
        pattern = _build_whitespace_insensitive_pattern(normalized_search)
        flags = re.MULTILINE | re.DOTALL
        matches = list(re.finditer(pattern, content, flags))
        match_count = len(matches)

        if match_count > 0 and match_count != expected_replacements:
            raise RuntimeError(
                f"Expected {expected_replacements} whitespace-insensitive occurrence(s) but found {match_count}."
            )

        if match_count > 0:
            locations = [
                _compute_edit_location(content, m.start(), m.end())
                for m in matches[:expected_replacements]
            ]

            replacement = normalize_line_endings(replace, line_ending)
            new_content, sub_count = re.subn(
                pattern, replacement, content, count=expected_replacements, flags=flags
            )

            loc_summary = ", ".join(
                f"lines {loc['start_line']}-{loc['end_line']}" if loc['start_line'] != loc['end_line']
                else f"line {loc['start_line']}"
                for loc in locations
            )

            return {
                "new_content": new_content,
                "message": f"Applied {expected_replacements} edit(s) with whitespace-insensitive matching ({loc_summary})",
                "replacements": expected_replacements,
                "locations": locations,
            }

    # No match found - use fuzzy matching for error message
    fuzzy_result = _best_fuzzy_match(content, search)
    similarity = float(fuzzy_result["similarity"])
    diff = _highlight_differences(search, fuzzy_result["value"])

    if similarity >= FUZZY_THRESHOLD:
        raise RuntimeError(
            f"Exact match not found, but found similar text with {round(similarity * 100)}% similarity.\n"
            f"Differences:\n{diff}"
        )

    raise RuntimeError(
        f"Search content not found. Closest match: '{fuzzy_result['value']}' "
        f"with {round(similarity * 100)}% similarity."
    )

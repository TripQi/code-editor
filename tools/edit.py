from __future__ import annotations

import tempfile
from datetime import datetime
from difflib import SequenceMatcher, ndiff
from pathlib import Path
from typing import Dict, TypedDict
import logging

from .config import get_file_write_line_limit
from .filesystem import read_file_internal, validate_path, write_file

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


def perform_search_replace(
    file_path: str,
    search: str,
    replace: str,
    expected_replacements: int = 1,
    expected_mtime: float | None = None,
) -> str:
    if search == "":
        raise ValueError("Empty search strings are not allowed. Please provide a non-empty string to search for.")

    valid_path = validate_path(file_path)
    content = read_file_internal(str(valid_path), 0, 1 << 60)
    line_ending = detect_line_ending(content)
    normalized_search = normalize_line_endings(search, line_ending)

    # Exact match path
    count = content.count(normalized_search)
    if count > 0:
        if count != expected_replacements:
            raise RuntimeError(
                f"Expected {expected_replacements} occurrences but found {count} in {file_path}. "
                "Adjust expected_replacements or make the search string more specific."
            )

        if expected_replacements == 1:
            idx = content.index(normalized_search)
            new_content = (
                content[:idx]
                + normalize_line_endings(replace, line_ending)
                + content[idx + len(normalized_search) :]
            )
        else:
            new_content = content.replace(normalized_search, normalize_line_endings(replace, line_ending))

        max_lines = max(search.count("\n") + 1, replace.count("\n") + 1)
        warning = ""
        if max_lines > get_file_write_line_limit():
            warning = (
                f"\n\nWARNING: The longer text block has {max_lines} lines (maximum: {get_file_write_line_limit()}). "
                "Consider smaller chunks for large edits."
            )

        write_file(str(valid_path), new_content, mode="rewrite", expected_mtime=expected_mtime)
        return f"Successfully applied {expected_replacements} edit(s) to {file_path}{warning}"

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
            f"To replace this text, use the exact text found in the file.\n\nLog entry: {log_path}"
        )

    raise RuntimeError(
        f"Search content not found in {file_path}. The closest match was '{fuzzy_result['value']}' "
        f"with only {round(FUZZY_THRESHOLD * 100)}% similarity (threshold {round(FUZZY_THRESHOLD * 100)}%).\n\n"
        f"Differences:\n{diff}\n\n"
        f"Log entry: {log_path}"
    )

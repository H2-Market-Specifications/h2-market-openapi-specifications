"""
Basic building blocks: data classes (LoadedJson, CheckResult, DuplicateKeyError), JSON loading, file discovery, path utilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote
from urllib.request import url2pathname

# Directories that are never searched for JSON files (VCS metadata, IDE
# config, virtual envs, dependency/build output, caches).
IGNORED_DIRS = {
    ".git",
    ".github",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
}


@dataclass(frozen=True)
class LoadedJson:
    path: Path
    data: Any


@dataclass
class CheckResult:
    errors: list[str]
    warnings: list[str]

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class DuplicateKeyError(ValueError):
    pass


def no_duplicate_keys_object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """
    Rejects duplicate keys during JSON parsing.
    Intended to be used as an object_pairs_hook for json.load().

    Args:
        pairs:
            Ordered key-value pairs produced by the JSON parser.

    Returns:
        Dictionary containing all parsed keys and values.

    Raises:
        DuplicateKeyError:
            If the same key appears more than once.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate key {key!r}")
        result[key] = value
    return result


def load_json_file(path: Path) -> Any:
    """
    Loads and parses a JSON file.

    Duplicate keys are rejected to prevent ambiguous
    configuration or schema definitions.

    Args:
        path:
            Path to the JSON file.

    Returns:
        Parsed JSON content.
    """
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=no_duplicate_keys_object_pairs_hook)


def iter_json_files(root: Path) -> list[Path]:
    """
    Recursively discovers JSON files in a repository.

    Common build, cache and dependency directories are
    excluded from the search.

    Args:
        root:
            Repository root directory.

    Returns:
        Sorted list of JSON files.
    """
    files: list[Path] = []
    for path in root.rglob("*.json"):
        # Skip any file that has one of the IGNORED_DIRS as a path segment.
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def rel(path: Path, root: Path) -> str:
    """
    Produces a repository-relative path when possible.

    Falls back to the absolute path if a relative path
    cannot be determined.

    Args:
        path:
            Path to convert.

        root:
            Repository root.

    Returns:
        Relative or absolute path string.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def file_uri(path: Path) -> str:
    """
    Converts a file path into a file:// URI.

    Args:
        path:
            Local filesystem path.

    Returns:
        File URI string.
    """
    return path.resolve().as_uri()


def uri_to_path(uri: str) -> Path | None:
    """
    Converts a file:// URI into a filesystem path.

    Args:
        uri:
            URI to convert.

    Returns:
        Resolved filesystem path or None if the URI
        does not use the file scheme.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    # url2pathname handles OS-specific quirks (e.g. drive letters on
    # Windows); unquote first to undo percent-encoding in the URI path.
    return Path(url2pathname(unquote(parsed.path))).resolve()


def expand_patterns(root: Path, patterns: list[str]) -> list[Path]:
    # Resolves a list of root-relative strings into actual files: glob
    # patterns (containing *, ?, or []) are expanded, everything else is
    # treated as a plain path and kept only if it exists.
    matches: list[Path] = []

    for pattern in patterns:
        path = (root / pattern).resolve()
        if any(char in pattern for char in "*?[]"):
            matches.extend(sorted(root.glob(pattern)))
        elif path.exists():
            matches.append(path)

    return [path for path in matches if path.is_file()]


def json_pointer_exists(document: Any, pointer: str) -> bool:
    """
    Checks whether a JSON Pointer references an existing
    location within a document.

    Supports RFC 6901 pointer syntax and escape rules.

    Args:
        document:
            JSON document to inspect.

        pointer:
            JSON Pointer such as '#/properties/id'.

    Returns:
        True if the target location exists.
    """
    if pointer in ("", "#"):
        return True

    if pointer.startswith("#"):
        pointer = pointer[1:]

    if not pointer.startswith("/"):
        return False

    # Walk the document one path segment at a time, decoding the RFC 6901
    # escapes (~1 -> "/", ~0 -> "~") for each segment along the way.
    current = document
    for raw_part in pointer.split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                return False
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit():
                return False
            index = int(part)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False

    return True
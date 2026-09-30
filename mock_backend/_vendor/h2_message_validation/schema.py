"""
JSON Schema infrastructure: schema detection, store construction, $ref resolution and validation, meta-schema self-validation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

from .core import LoadedJson, CheckResult, rel, file_uri, uri_to_path, json_pointer_exists
from .validator import validate_instance, validator_for

def is_schema_file(path: Path, data: Any, root: Path) -> bool:
    """
    Determines whether a JSON file should be treated
    as a JSON Schema.

    Detection is based on file naming conventions,
    directory structure and common schema keywords.

    Args:
        path:
            File path.

        data:
            Parsed JSON content.

        root:
            Repository root.

    Returns:
        True if the file appears to be a JSON Schema.
    """
    if not isinstance(data, dict):
        return False

    name = path.name.lower()
    parts = {part.lower() for part in path.relative_to(root).parts}

    if name.endswith(".schema.json"):
        return True

    # For a file that merely lives in a "schemas"-like directory, only treat
    # it as a schema if it actually contains at least one schema-ish keyword
    # (avoids misclassifying arbitrary JSON that happens to sit there).
    if {"schemas", "schema", "json-schema", "json-schemas"} & parts:
        schema_markers = {"$schema", "$id", "$defs", "definitions", "properties", "type", "allOf", "oneOf", "anyOf"}
        return bool(schema_markers & set(data.keys()))

    return False


def build_schema_stores(
    root: Path,
    schemas: list[LoadedJson],
) -> tuple[dict[str, Any], dict[str, Path], dict[Path, LoadedJson]]:
    """
    Builds lookup structures used for schema
    registration and reference resolution.

    Schemas are registered under:

    - file URI
    - repository-relative path
    - schema $id

    Returns:
        Tuple containing:

        - schema store
        - URI-to-path mapping
        - path-to-schema mapping
    """
    store: dict[str, Any] = {}
    uri_to_schema_path: dict[str, Path] = {}
    path_to_schema: dict[Path, LoadedJson] = {}

    for loaded in schemas:
        absolute_path = loaded.path.resolve()
        path_to_schema[absolute_path] = loaded

        # Register the schema under every key a $ref might plausibly use,
        # so lookups work regardless of how the ref was written.
        uri = file_uri(absolute_path)
        store[uri] = loaded.data
        uri_to_schema_path[uri] = absolute_path

        # Useful fallback for refs that use repo-relative strings.
        repo_relative = loaded.path.relative_to(root).as_posix()
        store[repo_relative] = loaded.data
        uri_to_schema_path[repo_relative] = absolute_path

        schema_id = loaded.data.get("$id") if isinstance(loaded.data, dict) else None
        if isinstance(schema_id, str) and schema_id:
            store[schema_id] = loaded.data
            uri_to_schema_path[schema_id] = absolute_path

    return store, uri_to_schema_path, path_to_schema


def iter_refs(node: Any, path: str = "$") -> list[tuple[str, str]]:
    """
    Recursively collects schema references from a JSON
    document.

    Supported reference keywords:

    - $ref
    - $dynamicRef
    - $recursiveRef

    Args:
        node:
            Current JSON node.

        path:
            Current JSON path.

    Returns:
        List of (location, reference) tuples.
    """
    refs: list[tuple[str, str]] = []

    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if key.isidentifier() else f"{path}[{key!r}]"
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(value, str):
                refs.append((child_path, value))
            else:
                refs.extend(iter_refs(value, child_path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            refs.extend(iter_refs(value, f"{path}[{index}]"))

    return refs


def check_ref_targets(
    root: Path,
    schema: LoadedJson,
    store: dict[str, Any],
    uri_to_schema_path: dict[str, Path],
    path_to_schema: dict[Path, LoadedJson],
    result: CheckResult,
) -> None:
    """
    Validates all schema references found within a schema.

    Checks that:

    - referenced schemas exist
    - referenced fragments exist
    - references can be resolved locally

    Any problems are added to the provided result object.
    """
    # Base URI for resolving relative refs within this schema: its own $id
    # if it declares one, otherwise its file URI.
    schema_base = schema.data.get("$id") if isinstance(schema.data, dict) else None
    if not isinstance(schema_base, str) or not schema_base:
        schema_base = file_uri(schema.path.resolve())

    for location, ref in iter_refs(schema.data):
        ref_base, fragment = urldefrag(ref)

        if ref_base == "":
            # A ref like "#/$defs/foo" with no base points into the same document.
            target_doc = schema.data
            target_name = rel(schema.path, root)
        else:
            resolved_base = urljoin(schema_base, ref_base)

            target_doc = None
            target_name = resolved_base

            # Try the fully resolved URI first, then the raw ref string as
            # written, then fall back to resolving it as a local file path.
            if resolved_base in store:
                target_doc = store[resolved_base]
                target_name = rel(uri_to_schema_path.get(resolved_base, schema.path), root)
            elif ref_base in store:
                target_doc = store[ref_base]
                target_name = rel(uri_to_schema_path.get(ref_base, schema.path), root)
            else:
                parsed = urlparse(resolved_base)
                if parsed.scheme == "file":
                    candidate = uri_to_path(resolved_base)
                    if candidate and candidate in path_to_schema:
                        target_doc = path_to_schema[candidate].data
                        target_name = rel(candidate, root)

            if target_doc is None:
                result.error(
                    f"{rel(schema.path, root)}: unresolved ref at {location}: {ref!r}. "
                    "Referenced schemas must be committed locally or registered by matching $id."
                )
                continue

        # Even if the target document was found, the fragment (JSON Pointer
        # part after '#') might point at a location that doesn't exist in it.
        if fragment and not json_pointer_exists(target_doc, fragment):
            result.error(
                f"{rel(schema.path, root)}: ref at {location} points to missing fragment "
                f"{fragment!r} in {target_name}"
            )



def validate_schema_self(
    root: Path,
    schema: LoadedJson,
    result: CheckResult,
) -> None:
    # Self-validates a schema document: with the "jsonschema" package
    # available, use its own meta-schema checker; otherwise fall back to a
    # minimal sanity check for the two required top-level keywords.
    if not isinstance(schema.data, dict):
        result.error(f"{rel(schema.path, root)}: invalid JSON Schema: schema must be an object")
        return

    if validator_for is not None:
        try:
            validator_class = validator_for(schema.data)
            validator_class.check_schema(schema.data)
        except Exception as exc:  # jsonschema raises SchemaError subclasses depending on draft
            result.error(f"{rel(schema.path, root)}: invalid JSON Schema: {exc}")
        return

    if not isinstance(schema.data.get("$schema"), str):
        result.error(f"{rel(schema.path, root)}: invalid JSON Schema: missing string $schema")
    if not isinstance(schema.data.get("$id"), str):
        result.error(f"{rel(schema.path, root)}: invalid JSON Schema: missing string $id")






def find_schema_by_ref(
    root: Path,
    ref: str,
    store: dict[str, Any],
    uri_to_schema_path: dict[str, Path],
) -> tuple[Path | None, Any | None]:
    # Looks up a schema by whatever form the ref/id was registered under
    # (see build_schema_stores); if that fails, tries treating it as a
    # root-relative file path as a last resort.
    if ref in store:
        return uri_to_schema_path.get(ref), store[ref]

    candidate = (root / ref).resolve()
    uri = file_uri(candidate)
    if uri in store:
        return candidate, store[uri]

    return None, None



def validate_schema_declared_instances(
    root: Path,
    loaded_json: list[LoadedJson],
    schemas: list[LoadedJson],
    store: dict[str, Any],
    uri_to_schema_path: dict[str, Path],
    result: CheckResult,
) -> None:
    # For every JSON file that is not itself a schema but declares a
    # "$schema" pointing at a local schema, validate it against that schema.
    schema_paths = {schema.path.resolve() for schema in schemas}

    for loaded in loaded_json:
        if loaded.path.resolve() in schema_paths:
            continue
        if not isinstance(loaded.data, dict):
            continue

        schema_ref = loaded.data.get("$schema")
        if not isinstance(schema_ref, str) or not schema_ref:
            continue

        # Ignore official JSON Schema meta-schema declarations on schema files.
        if "json-schema.org" in schema_ref:
            continue

        schema_path, schema_data = find_schema_by_ref(root, schema_ref, store, uri_to_schema_path)
        if schema_path is None or not isinstance(schema_data, dict):
            result.error(f"{rel(loaded.path, root)}: declared $schema not found locally: {schema_ref}")
            continue

        result.errors.extend(
            validate_instance(
                root=root,
                schema_path=schema_path,
                schema_data=schema_data,
                instance_path=loaded.path,
                instance_data=loaded.data,
                store=store,
            )
        )
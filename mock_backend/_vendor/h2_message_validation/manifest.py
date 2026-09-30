"""
Orchestration: loading validation manifest and catalog, validating positive/negative examples against schemas, ensuring catalog coverage.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any

from .semantic_checks import validate_repository_semantics
from .core import CheckResult, LoadedJson, load_json_file, rel, expand_patterns
from .schema import find_schema_by_ref
from .validator import validate_instance

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def load_simple_validation_manifest(path: Path) -> dict[str, Any]:
    """
    Loads a validation manifest without requiring PyYAML.

    Supports only the subset of YAML syntax used by
    this repository's validation manifest format.

    Args:
        path:
            Manifest file path.

    Returns:
        Parsed manifest structure.
    """
    # Minimal hand-rolled parser for manifests shaped like:
    #   validations:
    #     - schema: path/to/schema.json
    #       valid:
    #         - path/to/example.json
    #       invalid:
    #         - path/to/bad-example.json
    # Only used as a fallback when PyYAML isn't installed.
    validations: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_mode: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line_without_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_without_comment.strip() or line_without_comment.strip() == "validations:":
            continue

        stripped = line_without_comment.strip()
        if stripped.startswith("- schema:"):
            # Start of a new manifest entry.
            current = {"schema": stripped.split(":", 1)[1].strip(), "valid": [], "invalid": []}
            validations.append(current)
            current_mode = None
            continue

        if current is None:
            raise ValueError("manifest contains entries before first schema")

        if stripped in {"valid:", "invalid:"}:
            # Switch which list subsequent "- ..." lines get appended to.
            current_mode = stripped[:-1]
            continue

        if stripped.startswith("-") and current_mode in {"valid", "invalid"}:
            current[current_mode].append(stripped[1:].strip())
            continue

        raise ValueError(f"unsupported manifest line: {raw_line!r}")

    return {"validations": validations}


def load_manifest(path: Path) -> dict[str, Any]:
    """
    Loads the validation manifest.

    Uses PyYAML when available and falls back to the
    built-in lightweight parser otherwise.

    Args:
        path:
            Manifest file path.

    Returns:
        Parsed manifest data.
    """
    if not path.exists():
        return {"validations": []}
    if yaml is not None:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    else:
        data = load_simple_validation_manifest(path)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a YAML object")
    data.setdefault("validations", [])
    if not isinstance(data["validations"], list):
        raise ValueError("manifest field 'validations' must be a list")
    return data


def validate_manifest_examples(
    root: Path,
    manifest_path: Path,
    store: dict[str, Any],
    uri_to_schema_path: dict[str, Path],
    loaded_by_path: dict[Path, LoadedJson],
    result: CheckResult,
) -> None:
    # Runs every manifest entry's "valid" and "invalid" example files
    # against the referenced schema (and, for otherwise-valid instances,
    # against the semantic checks too), recording mismatches:
    #  - a "valid" example that fails schema/semantic validation is an error
    #  - an "invalid" example that unexpectedly passes is also an error
    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        result.error(f"{rel(manifest_path, root)}: invalid manifest YAML: {exc}")
        return

    for index, item in enumerate(manifest["validations"]):
        if not isinstance(item, dict):
            result.error(f"{rel(manifest_path, root)}: validations[{index}] must be an object")
            continue

        schema_ref = item.get("schema")
        if not isinstance(schema_ref, str) or not schema_ref:
            result.error(f"{rel(manifest_path, root)}: validations[{index}].schema is required")
            continue

        schema_path, schema_data = find_schema_by_ref(root, schema_ref, store, uri_to_schema_path)
        if schema_path is None or not isinstance(schema_data, dict):
            result.error(f"{rel(manifest_path, root)}: schema not found: {schema_ref}")
            continue

        for mode in ("valid", "invalid"):
            patterns = item.get(mode, [])
            if patterns is None:
                patterns = []
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                result.error(f"{rel(manifest_path, root)}: validations[{index}].{mode} must be a list of paths/globs")
                continue

            matches = expand_patterns(root, patterns)
            if patterns and not matches:
                result.error(
                    f"{rel(manifest_path, root)}: validations[{index}].{mode} patterns matched no files: {patterns}"
                )
                continue

            for instance_path in matches:
                absolute = instance_path.resolve()
                loaded = loaded_by_path.get(absolute)
                if loaded is None:
                    try:
                        loaded = LoadedJson(path=instance_path, data=load_json_file(instance_path))
                    except Exception as exc:
                        result.error(f"{rel(instance_path, root)}: cannot load JSON for validation: {exc}")
                        continue

                validation_errors = validate_instance(
                    root=root,
                    schema_path=schema_path,
                    schema_data=schema_data,
                    instance_path=instance_path,
                    instance_data=loaded.data,
                    store=store,
                )
                # Only run the (more expensive) semantic checks if the
                # instance already passed schema validation.
                semantic_errors = (
                    []
                    if validation_errors
                    else validate_repository_semantics(root, instance_path, loaded.data)
                )

                all_errors = validation_errors + semantic_errors

                if mode == "valid" and all_errors:
                    result.errors.extend(all_errors)
                elif mode == "invalid" and not all_errors:
                    result.error(
                        f"{rel(instance_path, root)} unexpectedly passed against {rel(schema_path, root)}"
                    )



def load_catalog(root: Path, loaded_by_path: dict[Path, LoadedJson]) -> LoadedJson | None:
    """
    Loads the message catalog from the repository.

    Expected location:

        catalog/message-catalog.json

    Returns:
        LoadedJson instance or None if no catalog exists.
    """
    catalog_path = (root / "catalog" / "message-catalog.json").resolve()
    # Reuse an already-loaded copy if one was read earlier in the run.
    loaded = loaded_by_path.get(catalog_path)
    if loaded is not None:
        return loaded
    if not catalog_path.exists():
        return None
    return LoadedJson(path=catalog_path, data=load_json_file(catalog_path))


def resolve_catalog_ref(catalog_path: Path, ref: str) -> Path:
    """
    Resolves a catalog-relative file reference.

    Args:
        catalog_path:
            Path to the catalog file.

        ref:
            Relative reference path.

    Returns:
        Absolute path to the referenced file.
    """
    return (catalog_path.parent / ref).resolve()


def validate_catalog_references(
    root: Path,
    loaded_by_path: dict[Path, LoadedJson],
    store: dict[str, Any],
    uri_to_schema_path: dict[str, Path],
    result: CheckResult,
) -> None:
    """Validate message catalog schema refs and catalog-listed positive examples."""
    catalog = load_catalog(root, loaded_by_path)
    if catalog is None:
        return
    if not isinstance(catalog.data, dict):
        return

    messages = catalog.data.get("messages")
    if not isinstance(messages, list):
        return

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue

        catalog_location = f"{rel(catalog.path, root)}: messages[{index}]"
        schema_ref = message.get("schema")
        schema_id = message.get("schemaId")
        schema_path: Path | None = None
        schema_data: Any | None = None

        # "schema" (file reference) and "schemaId" ($id lookup) are two
        # independent ways to point at the same schema; both are resolved
        # and then cross-checked below for consistency.
        if isinstance(schema_ref, str) and schema_ref:
            candidate = resolve_catalog_ref(catalog.path, schema_ref)
            loaded_schema = loaded_by_path.get(candidate)
            if loaded_schema is None:
                result.error(f"{catalog_location}.schema points to missing file: {schema_ref}")
            else:
                schema_path = loaded_schema.path
                schema_data = loaded_schema.data
                if not isinstance(schema_data, dict):
                    result.error(f"{catalog_location}.schema does not point to a JSON Schema object: {schema_ref}")
        else:
            result.error(f"{catalog_location}.schema is required")

        if isinstance(schema_id, str) and schema_id:
            schema_id_path, schema_id_data = find_schema_by_ref(root, schema_id, store, uri_to_schema_path)
            if schema_id_path is None or not isinstance(schema_id_data, dict):
                result.error(f"{catalog_location}.schemaId is not registered by any local schema $id: {schema_id}")
            else:
                if schema_path is not None and schema_id_path.resolve() != schema_path.resolve():
                    result.error(
                        f"{catalog_location}.schema and schemaId point to different schemas: "
                        f"{rel(schema_path, root)} vs {rel(schema_id_path, root)}"
                    )
                if schema_data is None:
                    schema_path = schema_id_path
                    schema_data = schema_id_data
        else:
            result.error(f"{catalog_location}.schemaId is required")

        if isinstance(schema_data, dict) and isinstance(schema_id, str) and schema_data.get("$id") != schema_id:
            result.error(
                f"{catalog_location}.schemaId does not match schema $id in {rel(schema_path or catalog.path, root)}: "
                f"{schema_id!r} != {schema_data.get('$id')!r}"
            )

        examples = message.get("examples")
        if not isinstance(examples, list) or not all(isinstance(example, str) for example in examples):
            result.error(f"{catalog_location}.examples must be a list of paths")
            continue

        # Every example listed in the catalog is expected to be a positive
        # (schema-valid) instance of the message's schema.
        for example_ref in examples:
            example_path = resolve_catalog_ref(catalog.path, example_ref)
            loaded_example = loaded_by_path.get(example_path)
            if loaded_example is None:
                result.error(f"{catalog_location}.examples points to missing file: {example_ref}")
                continue

            if isinstance(schema_data, dict) and schema_path is not None:
                result.errors.extend(
                    validate_instance(
                        root=root,
                        schema_path=schema_path,
                        schema_data=schema_data,
                        instance_path=loaded_example.path,
                        instance_data=loaded_example.data,
                        store=store,
                    )
                )


def validate_catalog_manifest_coverage(
    root: Path,
    manifest_path: Path,
    loaded_by_path: dict[Path, LoadedJson],
    store: dict[str, Any],
    uri_to_schema_path: dict[str, Path],
    result: CheckResult,
) -> None:
    """Ensure catalog messages and examples are covered as positive manifest validations."""
    catalog = load_catalog(root, loaded_by_path)
    if catalog is None or not isinstance(catalog.data, dict):
        return

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        result.error(f"{rel(manifest_path, root)}: invalid manifest YAML: {exc}")
        return

    # Build a lookup of {schema_path: {example paths listed as "valid"}}
    # from the manifest, so it can be compared against what the catalog
    # expects to be covered.
    covered_valid_examples: dict[Path, set[Path]] = {}
    for item in manifest["validations"]:
        if not isinstance(item, dict):
            continue
        schema_ref = item.get("schema")
        if not isinstance(schema_ref, str) or not schema_ref:
            continue
        schema_path, schema_data = find_schema_by_ref(root, schema_ref, store, uri_to_schema_path)
        if schema_path is None or not isinstance(schema_data, dict):
            continue
        valid_patterns = item.get("valid", [])
        if not isinstance(valid_patterns, list) or not all(isinstance(pattern, str) for pattern in valid_patterns):
            continue
        covered_valid_examples.setdefault(schema_path.resolve(), set()).update(
            path.resolve() for path in expand_patterns(root, valid_patterns)
        )

    messages = catalog.data.get("messages")
    if not isinstance(messages, list):
        return

    # Every catalog message's schema must have at least one manifest entry
    # validating it as "valid", and every example the catalog lists for that
    # message must be among the files covered by that entry.
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        schema_ref = message.get("schema")
        examples = message.get("examples")
        if not isinstance(schema_ref, str) or not isinstance(examples, list):
            continue

        schema_path = resolve_catalog_ref(catalog.path, schema_ref)
        if schema_path not in covered_valid_examples:
            result.error(
                f"{rel(manifest_path, root)}: missing validation entry for catalog message "
                f"{message.get('id', index)!r} schema {rel(schema_path, root)}"
            )
            continue

        valid_examples = covered_valid_examples[schema_path]
        for example_ref in examples:
            if not isinstance(example_ref, str):
                continue
            example_path = resolve_catalog_ref(catalog.path, example_ref)
            if example_path not in valid_examples:
                result.error(
                    f"{rel(manifest_path, root)}: catalog example {rel(example_path, root)} for message "
                    f"{message.get('id', index)!r} is not covered by that schema's valid manifest patterns"
                )
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import yaml

from .catalog import MessageSpec, load_catalog
from .schema_loader import SchemaLoader
from .validate import assert_no_generator_markers, security_scheme_names, validate_openapi_document
from .validator_bundle import bundle_validator


class GenerationError(ValueError):
    """Raised when an OpenAPI file cannot be generated from a template."""


_GENERATOR_KEY = "x-h2-generator"

_common_components_cache: dict[Path, dict[str, Any]] = {}
_PLACEHOLDER_RE = re.compile(r"\$\{x-h2-message\.([a-zA-Z0-9_]+)\}")
_ANNOTATION_KEYS = frozenset({"title", "description"})


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise GenerationError(f"Template not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GenerationError(f"Template must contain a YAML object: {path}")
    return data


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)


def generate_all_messages(
    *,
    source_root: Path,
    templates_root: Path,
    output_root: Path,
    strict_templates: bool = True,
    prune_unused_components: bool = True,
    common_components_path: Path | None = None,
    validator_output: Path | None = None,
) -> list[Path]:
    messages = load_catalog(source_root)
    if strict_templates:
        _assert_templates_match_catalog(messages, templates_root)
    outputs: list[Path] = []
    for message in messages:
        outputs.append(
            generate_message(
                source_root=source_root,
                templates_root=templates_root,
                output_root=output_root,
                message=message,
                prune_unused_components=prune_unused_components,
                common_components_path=common_components_path,
            )
        )
    _remove_stale_outputs(output_root, outputs)
    if validator_output is not None:
        # Written in the same run as the specifications so the mock backend's validator always matches them.
        bundle_validator(source_root, validator_output)
    return outputs


def _remove_stale_outputs(output_root: Path, outputs: list[Path]) -> None:
    # Messages removed or renamed in the catalog would otherwise leave their old documents behind.
    messages_root = output_root / "messages"
    expected = {path.resolve() for path in outputs}
    for path in messages_root.glob("*/v*/*/openapi.yaml"):
        if path.resolve() in expected:
            continue
        path.unlink()
        for directory in (path.parent, path.parent.parent, path.parent.parent.parent):
            if directory == messages_root or any(directory.iterdir()):
                break
            directory.rmdir()


def generate_message(
    *,
    source_root: Path,
    templates_root: Path,
    output_root: Path,
    message: MessageSpec,
    prune_unused_components: bool = True,
    common_components_path: Path | None = None,
) -> Path:
    template_path = message.template_path(templates_root)
    document = load_yaml(template_path)

    x_h2_message = document.get("x-h2-message")
    if not isinstance(x_h2_message, dict):
        x_h2_message = {}

    if common_components_path is None:
        common_components_path = templates_root / "common" / "error_components.yaml"

    schema_injected = False

    def inject(node: Any, path: tuple[str, ...]) -> Any:
        nonlocal schema_injected
        marker = _marker(node)
        if marker:
            kind = marker.get("inject")
            if kind == "message-schema":
                if len(path) < 3 or path[-3:-1] != ("components", "schemas"):
                    raise GenerationError("message-schema marker must be located under components.schemas.<name>.")
                root_component_name = path[-1]
                bundle = SchemaLoader(source_root).bundle(message.schema_path(source_root), root_component_name)
                root_schema = _scalar_compatible_schema(bundle.root_schema, bundle.additional_components)
                schema_injected = True
                return root_schema
            if kind == "first-valid-example":
                examples = _load_examples(source_root, message)
                if not examples:
                    raise GenerationError(f"Message {message.id} has no catalog examples for first-valid-example marker.")
                return next(iter(examples.values()))
            if kind == "all-valid-examples":
                return _load_examples(source_root, message)
            if kind == "common-component":
                ref = marker.get("ref", marker.get("$ref"))
                if not isinstance(ref, str) or not ref.startswith("#/components/"):
                    raise GenerationError(f"common-component marker requires a 'ref' pointing to '#/components/...': {marker!r}")
                common_doc = _load_common_components(common_components_path)
                resolved = _resolve_ref(common_doc, ref)
                if resolved is None:
                    raise GenerationError(f"common-component ref not found in {common_components_path}: {ref}")
                key = _ref_components_key(ref)
                inlined = _inline_common_refs(resolved, common_doc, (key,) if key else ())
                substituted = _substitute_placeholders(inlined, x_h2_message)
                return inject(copy.deepcopy(substituted), path)
            raise GenerationError(f"Unsupported generator injection marker: {kind!r}")

        if isinstance(node, dict):
            result: dict[str, Any] = {}
            for key, value in node.items():
                value_marker = _marker(value)
                if value_marker and value_marker.get("inject") == "all-valid-examples":
                    result.update(_load_examples(source_root, message))
                else:
                    result[key] = inject(value, (*path, str(key)))
            return result
        if isinstance(node, list):
            return [inject(value, (*path, str(index))) for index, value in enumerate(node)]
        return node

    document = inject(document, ())
    if not schema_injected:
        raise GenerationError(f"Template has no message-schema marker: {template_path}")

    document["openapi"] = str(document.get("openapi", "3.1.0"))
    if not document["openapi"].startswith("3.1"):
        raise GenerationError(f"Template must use OpenAPI 3.1: {template_path}")

    # Some OpenAPI consumers currently reject custom top-level dialect values.
    document.pop("jsonSchemaDialect", None)
    document = _normalize_openapi_document(document)

    if prune_unused_components:
        _prune_components(document)

    assert_no_generator_markers(document)
    validate_openapi_document(document)

    output_path = message.output_path(output_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dump_yaml(document), encoding="utf-8")
    return output_path


def _marker(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict) and set(node) == {_GENERATOR_KEY}:
        marker = node[_GENERATOR_KEY]
        if isinstance(marker, dict):
            return marker
    return None


def _substitute_placeholders(node: Any, values: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {key: _substitute_placeholders(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute_placeholders(value, values) for value in node]
    if isinstance(node, str):
        full_match = _PLACEHOLDER_RE.fullmatch(node)
        if full_match:
            field = full_match.group(1)
            if field not in values:
                raise GenerationError(f"Unknown placeholder x-h2-message.{field}: no such field in template's x-h2-message block.")
            return values[field]

        def replace(match: re.Match[str]) -> str:
            field = match.group(1)
            if field not in values:
                raise GenerationError(f"Unknown placeholder x-h2-message.{field}: no such field in template's x-h2-message block.")
            return str(values[field])

        return _PLACEHOLDER_RE.sub(replace, node)
    return node


def _load_common_components(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved not in _common_components_cache:
        _common_components_cache[resolved] = load_yaml(path)
    return _common_components_cache[resolved]


def _resolve_ref(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise GenerationError(f"Only local refs are supported for common-component injection: {ref}")
    node: Any = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _ref_components_key(ref: str) -> tuple[str, str] | None:
    parts = ref.split("/")
    if len(parts) >= 4 and parts[1] == "components":
        return (parts[2], parts[3])
    return None


def _inline_common_refs(node: Any, common_doc: dict[str, Any], stack: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            key = _ref_components_key(ref)
            if key is not None and key not in stack:
                resolved = _resolve_ref(common_doc, ref)
                if resolved is None:
                    raise GenerationError(f"Unresolved $ref in common components file: {ref}")
                inlined = _inline_common_refs(resolved, common_doc, (*stack, key))
                # Keys next to the $ref (e.g. "required: true" on a response header) refine the referenced component.
                siblings = {k: _inline_common_refs(v, common_doc, stack) for k, v in node.items() if k != "$ref"}
                return {**inlined, **siblings} if siblings and isinstance(inlined, dict) else inlined
        return {k: _inline_common_refs(v, common_doc, stack) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_common_refs(v, common_doc, stack) for v in node]
    return node


def _scalar_compatible_schema(schema: dict[str, Any], components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return _normalize_schema(_inline_component_refs(schema, components, stack=()))


def _inline_component_refs(node: Any, components: dict[str, dict[str, Any]], stack: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.removeprefix("#/components/schemas/")
            if name in components and name not in stack:
                return _inline_component_refs(components[name], components, (*stack, name))
        return {key: _inline_component_refs(value, components, stack) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_component_refs(value, components, stack) for value in node]
    return node


def _normalize_schema(node: Any) -> Any:
    if isinstance(node, dict):
        normalized = {key: _normalize_schema(value) for key, value in node.items()}
        normalized.pop("$schema", None)
        if "const" in normalized:
            value = normalized.pop("const")
            normalized.setdefault("enum", [value])
        all_of = normalized.get("allOf")
        if (
            isinstance(all_of, list)
            and all(isinstance(item, dict) and "$ref" not in item for item in all_of)
            and not _contains_conditional_schema(normalized)
        ):
            # Merge copies: a conflict must leave the original allOf intact.
            base = copy.deepcopy({key: value for key, value in normalized.items() if key != "allOf"})
            # The schema's own annotations are the most specific; among allOf branches, later ones refine earlier ones.
            own_annotations = frozenset(key for key in _ANNOTATION_KEYS if key in base)
            try:
                for item in all_of:
                    _merge_schema(base, copy.deepcopy(item), keep=own_annotations)
            except _MergeConflict:
                return normalized
            return base
        return normalized
    if isinstance(node, list):
        return [_normalize_schema(value) for value in node]
    return node


def _contains_conditional_schema(node: Any) -> bool:
    # Merging independent conditions (including conditions on nested properties)
    # changes which constraints apply to each instance. Keep their allOf boundary.
    if isinstance(node, dict):
        return bool({"if", "then", "else"}.intersection(node)) or any(
            _contains_conditional_schema(value) for value in node.values()
        )
    if isinstance(node, list):
        return any(_contains_conditional_schema(value) for value in node)
    return False


class _MergeConflict(Exception):
    """Raised when allOf branches cannot be merged into one schema without changing what it accepts."""


# Keywords whose combination under allOf is the stricter of the two values.
_LOWER_BOUNDS = frozenset({"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties", "minContains"})
_UPPER_BOUNDS = frozenset({"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties", "maxContains"})
# Keywords where two differing values cannot be expressed as one (e.g. two independent `contains`).
_UNMERGEABLE = frozenset(
    {
        "contains", "not", "anyOf", "oneOf", "allOf", "prefixItems", "if", "then", "else",
        "dependentSchemas", "patternProperties", "propertyNames", "unevaluatedProperties", "unevaluatedItems",
    }
)
# Annotations without validation effect; the later branch wins.
_LATER_WINS_ANNOTATIONS = frozenset({"default", "examples", "$comment", "deprecated", "readOnly", "writeOnly"})


def _merge_schema(target: dict[str, Any], source: dict[str, Any], keep: frozenset[str] = frozenset()) -> None:
    # Merges source into target so the result accepts exactly what both accept (their allOf).
    # Raises _MergeConflict where that is not possible; callers then keep the allOf.
    _assert_closed_objects_stay_closed(target, source)
    for key, value in source.items():
        if key in _ANNOTATION_KEYS:
            if key not in keep:
                target[key] = copy.deepcopy(value)
            continue
        if key.startswith("x-") or key in _LATER_WINS_ANNOTATIONS:
            existing = target.get(key)
            target[key] = {**existing, **value} if isinstance(existing, dict) and isinstance(value, dict) else copy.deepcopy(value)
            continue
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        existing = target[key]
        if key == "required" and isinstance(existing, list) and isinstance(value, list):
            existing.extend(item for item in value if item not in existing)
        elif key == "properties" and isinstance(existing, dict) and isinstance(value, dict):
            for property_name, property_schema in value.items():
                _merge_property(existing, property_name, property_schema)
        elif existing == value:
            continue
        elif key == "additionalProperties" and (existing is False or value is False):
            target[key] = False
        elif key == "enum" and isinstance(existing, list) and isinstance(value, list):
            # Keep the order of the later, more specific branch.
            common = [item for item in value if item in existing]
            if not common:
                raise _MergeConflict(key)
            target[key] = common
        elif key in _LOWER_BOUNDS and _is_number(existing) and _is_number(value):
            target[key] = max(existing, value)
        elif key in _UPPER_BOUNDS and _is_number(existing) and _is_number(value):
            target[key] = min(existing, value)
        elif key == "type":
            target[key] = _intersect_types(existing, value)
        elif key not in _UNMERGEABLE and isinstance(existing, dict) and isinstance(value, dict):
            # Nested schemas such as items or additionalProperties.
            _merge_schema(existing, value)
            target[key] = _normalize_schema(existing)
        else:
            raise _MergeConflict(key)


def _merge_property(properties: dict[str, Any], name: str, schema: Any) -> None:
    if name not in properties:
        properties[name] = copy.deepcopy(schema)
    elif properties[name] is False or schema is False:
        properties[name] = False
    elif isinstance(properties[name], dict) and isinstance(schema, dict):
        _merge_schema(properties[name], schema)
        properties[name] = _normalize_schema(properties[name])
    elif properties[name] != schema:
        raise _MergeConflict(f"properties/{name}")


def _assert_closed_objects_stay_closed(target: dict[str, Any], source: dict[str, Any]) -> None:
    # With additionalProperties: false, a branch rejects properties it does not list itself.
    # Merging in another branch's properties would make the flattened schema accept them.
    for closed, other in ((target, source), (source, target)):
        if closed.get("additionalProperties") is not False:
            continue
        if "patternProperties" in closed or "patternProperties" in other:
            raise _MergeConflict("patternProperties")
        listed = closed.get("properties", {})
        added = {name for name, schema in other.get("properties", {}).items() if schema is not False and name not in listed}
        if added:
            raise _MergeConflict(f"additionalProperties: {sorted(added)}")


def _intersect_types(existing: Any, value: Any) -> Any:
    first = [existing] if isinstance(existing, str) else list(existing)
    second = [value] if isinstance(value, str) else list(value)
    common: list[str] = []
    for item in first:
        if item in second:
            match = item
        elif (item == "number" and "integer" in second) or (item == "integer" and "number" in second):
            match = "integer"  # every integer is a number
        else:
            continue
        if match not in common:
            common.append(match)
    if not common:
        raise _MergeConflict("type")
    return common[0] if len(common) == 1 else common


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_openapi_document(document: dict[str, Any]) -> dict[str, Any]:
    return _normalize_openapi_node(document, ())


def _normalize_openapi_node(node: Any, path: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        normalized = {key: _normalize_openapi_node(value, (*path, str(key))) for key, value in node.items()}
        if "example" in normalized and "examples" in normalized:
            normalized.pop("example")
        if _is_schema_context(path, normalized):
            normalized.pop("examples", None)
        return normalized
    if isinstance(node, list):
        return [_normalize_openapi_node(value, (*path, str(index))) for index, value in enumerate(node)]
    return node


def _is_schema_context(path: tuple[str, ...], node: dict[str, Any]) -> bool:
    if path and path[-1] == "schema":
        return True
    if len(path) >= 2 and path[0] == "components" and path[1] == "schemas":
        return True
    schema_keywords = {"$ref", "type", "properties", "items", "enum", "allOf", "oneOf", "anyOf", "format", "pattern"}
    return bool(schema_keywords.intersection(node)) and any(part in {"properties", "items", "allOf", "oneOf", "anyOf"} for part in path)


def _load_examples(source_root: Path, message: MessageSpec) -> dict[str, dict[str, Any]]:
    examples: dict[str, dict[str, Any]] = {}
    for path in message.example_paths(source_root):
        if not path.exists():
            raise GenerationError(f"Catalog example for {message.id} not found: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GenerationError(f"Catalog example is not valid JSON: {path}: {exc}") from exc
        examples[_example_key(path)] = {"summary": path.stem, "value": value}
    return examples


def _example_key(path: Path) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem)
    key = key.replace(".valid", "_valid")
    if not re.match(r"^[A-Za-z]", key):
        key = f"Example_{key}"
    return key


def _assert_templates_match_catalog(messages: list[MessageSpec], templates_root: Path) -> None:
    expected = {message.template_path(templates_root).resolve() for message in messages}
    missing = [path for path in sorted(expected) if not path.exists()]
    if missing:
        raise GenerationError("Missing OpenAPI templates:\n" + "\n".join(str(path) for path in missing))
    actual = {path.resolve() for path in templates_root.glob("messages/*/v*/*/openapi.template.yaml")}
    extra = sorted(actual - expected)
    if extra:
        raise GenerationError("Templates without matching catalog entry:\n" + "\n".join(str(path) for path in extra))


def _collect_refs(node: Any) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            parts = ref.split("/")
            if len(parts) >= 4:
                refs.add((parts[2], parts[3]))
        for value in node.values():
            refs.update(_collect_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.update(_collect_refs(item))
    return refs


def _prune_components(document: dict[str, Any]) -> None:
    components = document.get("components")
    if not isinstance(components, dict):
        return
    seed = {key: value for key, value in document.items() if key != "components"}
    # Security requirements name their schemes instead of referencing them with $ref.
    queue = [*_collect_refs(seed), *(("securitySchemes", name) for name in security_scheme_names(document))]
    seen: set[tuple[str, str]] = set()
    while queue:
        item = queue.pop(0)
        if item in seen:
            continue
        seen.add(item)
        category, name = item
        target = components.get(category, {}).get(name)
        for ref in _collect_refs(target):
            if ref not in seen:
                queue.append(ref)
    for category in list(components):
        if not isinstance(components[category], dict):
            continue
        for name in list(components[category]):
            if (category, name) not in seen:
                del components[category][name]
        if not components[category]:
            del components[category]

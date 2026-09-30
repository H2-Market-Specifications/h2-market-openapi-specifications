from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ValidationError(ValueError):
    """Raised when a generated OpenAPI document is invalid."""


_OPENAPI_31_DEFAULT_DIALECT = "https://spec.openapis.org/oas/3.1/dialect/base"
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def load_openapi(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValidationError(f"OpenAPI file must contain a YAML object: {path}")
    return data


def validate_all_outputs(output_root: Path) -> list[Path]:
    paths = sorted(output_root.glob("messages/*/v*/*/openapi.yaml"))
    if not paths:
        raise ValidationError(f"No generated OpenAPI files found below {output_root}")
    for path in paths:
        validate_openapi_document(load_openapi(path))
    return paths


def validate_openapi_document(document: dict[str, Any]) -> None:
    openapi = document.get("openapi")
    if not isinstance(openapi, str) or not openapi.startswith("3.1"):
        raise ValidationError("OpenAPI document must use OpenAPI 3.1.x.")
    dialect = document.get("jsonSchemaDialect")
    if dialect is not None and dialect != _OPENAPI_31_DEFAULT_DIALECT:
        raise ValidationError(
            f"OpenAPI jsonSchemaDialect must be omitted or set to the OpenAPI 3.1 default dialect {_OPENAPI_31_DEFAULT_DIALECT}."
        )
    if "info" not in document or not isinstance(document["info"], dict):
        raise ValidationError("OpenAPI document must contain info.")
    if "paths" not in document or not isinstance(document["paths"], dict):
        raise ValidationError("OpenAPI document must contain paths.")
    components = document.get("components")
    if not isinstance(components, dict) or not isinstance(components.get("schemas"), dict):
        raise ValidationError("OpenAPI document must contain components.schemas.")
    if not components["schemas"]:
        raise ValidationError("OpenAPI document must contain at least one schema component.")
    security_schemes = components.get("securitySchemes")
    undefined_schemes = sorted(security_scheme_names(document) - set(security_schemes if isinstance(security_schemes, dict) else ()))
    if undefined_schemes:
        raise ValidationError(f"Security requirements reference undefined security schemes: {', '.join(undefined_schemes)}")
    assert_no_generator_markers(document)
    _assert_no_conflicting_examples(document)
    _assert_no_schema_examples(document)
    for ref in _iter_refs(document):
        if ref.startswith("#/"):
            _resolve_json_pointer(document, ref)
        else:
            raise ValidationError(f"External $ref is not allowed in generated OpenAPI: {ref}")


def security_scheme_names(document: dict[str, Any]) -> set[str]:
    """Return the security scheme names required by the document or any of its operations."""
    requirements = list(document.get("security") or [])
    for section in ("paths", "webhooks"):
        path_items = document.get(section)
        if not isinstance(path_items, dict):
            continue
        for path_item in path_items.values():
            if not isinstance(path_item, dict):
                continue
            for method in _HTTP_METHODS:
                operation = path_item.get(method)
                if isinstance(operation, dict):
                    requirements.extend(operation.get("security") or [])
    return {name for requirement in requirements if isinstance(requirement, dict) for name in requirement}


def assert_no_generator_markers(node: Any) -> None:
    if isinstance(node, dict):
        if "x-h2-generator" in node:
            raise ValidationError("Generated OpenAPI still contains x-h2-generator marker.")
        for value in node.values():
            assert_no_generator_markers(value)
    elif isinstance(node, list):
        for item in node:
            assert_no_generator_markers(item)


def _assert_no_conflicting_examples(node: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(node, dict):
        if "example" in node and "examples" in node:
            raise ValidationError(f"OpenAPI node must not contain both example and examples: {'/'.join(path) or '$'}")
        for key, value in node.items():
            _assert_no_conflicting_examples(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_no_conflicting_examples(value, (*path, str(index)))


def _assert_no_schema_examples(node: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(node, dict):
        if _is_schema_context(path, node) and "examples" in node:
            raise ValidationError(f"Schema Object must not contain examples in generated OpenAPI: {'/'.join(path) or '$'}")
        for key, value in node.items():
            _assert_no_schema_examples(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_no_schema_examples(value, (*path, str(index)))


def _is_schema_context(path: tuple[str, ...], node: dict[str, Any]) -> bool:
    if path and path[-1] == "schema":
        return True
    if len(path) >= 2 and path[0] == "components" and path[1] == "schemas":
        return True
    schema_keywords = {"$ref", "type", "properties", "items", "enum", "allOf", "oneOf", "anyOf", "format", "pattern"}
    return bool(schema_keywords.intersection(node)) and any(part in {"properties", "items", "allOf", "oneOf", "anyOf"} for part in path)


def _iter_refs(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.append(ref)
        for value in node.values():
            refs.extend(_iter_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_iter_refs(item))
    return refs


def _resolve_json_pointer(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ValidationError(f"Only internal refs are supported: {ref}")
    current: Any = document
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValidationError(f"Unresolvable internal $ref: {ref}")
    return current

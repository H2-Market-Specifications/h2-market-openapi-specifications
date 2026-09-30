"""
Technical validation engine: SimpleValidator as fallback when jsonschema is not installed, otherwise jsonschema with Registry and FormatChecker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

from .core import rel, json_pointer_exists
from .time_utils import parse_json_datetime

try:
    from jsonschema import FormatChecker
    from jsonschema.validators import validator_for
except ModuleNotFoundError:
    FormatChecker = None
    validator_for = None

try:
    from referencing import Registry, Resource
except ModuleNotFoundError:
    Registry = None
    Resource = None

@dataclass
class SimpleValidationError:
    path: list[Any]
    message: str


def is_json_type(instance: Any, expected_type: str) -> bool:
    # bool is intentionally excluded from "integer"/"number" (bool is a
    # subclass of int in Python) so a JSON boolean is never mistaken for a
    # JSON Schema numeric type.
    if expected_type == "object":
        return isinstance(instance, dict)
    if expected_type == "array":
        return isinstance(instance, list)
    if expected_type == "string":
        return isinstance(instance, str)
    if expected_type == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected_type == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if expected_type == "boolean":
        return isinstance(instance, bool)
    if expected_type == "null":
        return instance is None
    return True


class SimpleValidator:
    """Small local validator for this repository's committed schema keywords."""

    def __init__(self, schema: dict[str, Any], store: dict[str, Any]):
        self.schema = schema
        self.store = store

    def iter_errors(self, instance: Any) -> list[SimpleValidationError]:
        return self._validate(self.schema, instance, [])

    def _resolve_ref(self, ref: str) -> Any | None:
        # Resolves "$ref" against the schema store: an empty base means the
        # ref points into the current schema document itself, otherwise it
        # is looked up by its base URI/key in self.store.
        ref_base, fragment = urldefrag(ref)
        document = self.schema if ref_base == "" else self.store.get(ref_base)
        if document is None:
            return None
        if fragment and json_pointer_exists(document, fragment) is False:
            return None
        if fragment in ("", "#"):
            return document
        # Walk the JSON Pointer fragment (RFC 6901 escapes already handled
        # by json_pointer_exists's own check above; here we just re-walk to
        # extract the actual target value).
        pointer = fragment[1:] if fragment.startswith("#") else fragment
        current = document
        for raw_part in pointer.split("/")[1:]:
            part = raw_part.replace("~1", "/").replace("~0", "~")
            current = current[int(part)] if isinstance(current, list) else current[part]
        return current

    def _validate(self, schema: Any, instance: Any, path: list[Any]) -> list[SimpleValidationError]:
        # Supports only the subset of JSON Schema keywords actually used by
        # this repository's schemas ($ref, allOf, type, const, enum, string
        # length/pattern/date-time format, numeric bounds, array item/size
        # constraints, and object required/properties/additionalProperties).
        if not isinstance(schema, dict):
            return []

        ref = schema.get("$ref")
        if isinstance(ref, str):
            target = self._resolve_ref(ref)
            if target is None:
                return [SimpleValidationError(path, f"unresolved ref {ref!r}")]
            return self._validate(target, instance, path)

        errors: list[SimpleValidationError] = []

        for subschema in schema.get("allOf", []):
            errors.extend(self._validate(subschema, instance, path))

        expected_type = schema.get("type")
        if isinstance(expected_type, str) and not is_json_type(instance, expected_type):
            # A type mismatch makes further keyword checks meaningless, so
            # bail out immediately instead of accumulating more errors.
            return [SimpleValidationError(path, f"{instance!r} is not of type {expected_type!r}")]

        if "const" in schema and instance != schema["const"]:
            errors.append(SimpleValidationError(path, f"{instance!r} is not equal to const {schema['const']!r}"))

        enum = schema.get("enum")
        if isinstance(enum, list) and instance not in enum:
            errors.append(SimpleValidationError(path, f"{instance!r} is not one of {enum!r}"))

        if isinstance(instance, str):
            min_length = schema.get("minLength")
            if isinstance(min_length, int) and len(instance) < min_length:
                errors.append(SimpleValidationError(path, f"{instance!r} is too short"))
            max_length = schema.get("maxLength")
            if isinstance(max_length, int) and len(instance) > max_length:
                errors.append(SimpleValidationError(path, f"{instance!r} is too long"))
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and re.fullmatch(pattern, instance) is None:
                errors.append(SimpleValidationError(path, f"{instance!r} does not match {pattern!r}"))
            if schema.get("format") == "date-time" and parse_json_datetime(instance) is None:
                errors.append(SimpleValidationError(path, f"{instance!r} is not a valid date-time"))

        if isinstance(instance, (int, float)) and not isinstance(instance, bool):
            minimum = schema.get("minimum")
            if isinstance(minimum, (int, float)) and instance < minimum:
                errors.append(SimpleValidationError(path, f"{instance!r} is less than minimum {minimum!r}"))
            maximum = schema.get("maximum")
            if isinstance(maximum, (int, float)) and instance > maximum:
                errors.append(SimpleValidationError(path, f"{instance!r} is greater than maximum {maximum!r}"))

        if isinstance(instance, list):
            min_items = schema.get("minItems")
            if isinstance(min_items, int) and len(instance) < min_items:
                errors.append(SimpleValidationError(path, f"array has fewer than {min_items} items"))
            max_items = schema.get("maxItems")
            if isinstance(max_items, int) and len(instance) > max_items:
                errors.append(SimpleValidationError(path, f"array has more than {max_items} items"))
            items_schema = schema.get("items")
            if isinstance(items_schema, dict):
                for index, value in enumerate(instance):
                    errors.extend(self._validate(items_schema, value, path + [index]))

        if isinstance(instance, dict):
            required = schema.get("required")
            if isinstance(required, list):
                for key in required:
                    if isinstance(key, str) and key not in instance:
                        errors.append(SimpleValidationError(path + [key], f"required property {key!r} is missing"))

            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            for key, property_schema in properties.items():
                if key in instance:
                    errors.extend(self._validate(property_schema, instance[key], path + [key]))

            additional = schema.get("additionalProperties", True)
            for key, value in instance.items():
                if key in properties:
                    continue
                if additional is False:
                    errors.append(SimpleValidationError(path + [key], f"additional property {key!r} is not allowed"))
                elif isinstance(additional, dict):
                    errors.extend(self._validate(additional, value, path + [key]))

        return errors

def make_referencing_registry(store: dict[str, Any]):
    if Registry is None or Resource is None:
        return None

    registry = Registry()
    for uri, contents in store.items():
        try:
            registry = registry.with_resource(uri, Resource.from_contents(contents))
        except Exception:
            # The fallback store also contains convenience aliases that are not
            # valid JSON Schema resources. jsonschema only needs the entries
            # that referencing can register.
            continue

    return registry


def make_validator(schema_data: dict[str, Any], store: dict[str, Any]):
    # Prefers the real "jsonschema" library (full draft support, format
    # checking, proper $ref resolution via a Registry) and only falls back
    # to SimpleValidator when either jsonschema or referencing is missing,
    # or when a Registry could not be built from the store.
    if validator_for is None or FormatChecker is None:
        return SimpleValidator(schema_data, store)

    validator_class = validator_for(schema_data)
    validator_class.check_schema(schema_data)

    registry = make_referencing_registry(store)
    if registry is None:
        return SimpleValidator(schema_data, store)

    return validator_class(
        schema_data,
        registry=registry,
        format_checker=FormatChecker(),
    )


def validate_instance(
    root: Path,
    schema_path: Path,
    schema_data: dict[str, Any],
    instance_path: Path,
    instance_data: Any,
    store: dict[str, Any],
) -> list[str]:
    """
    Validates a JSON instance against a JSON Schema.

    Uses either:

    - jsonschema library
    - SimpleValidator fallback implementation

    Args:
        schema_data:
            Schema definition.

        instance_data:
            JSON document to validate.

    Returns:
        List of formatted validation messages.
    """
    validator = make_validator(schema_data, store)
    errors = sorted(validator.iter_errors(instance_data), key=lambda err: list(err.path))

    messages: list[str] = []
    for err in errors:
        # Renders each error's path as a JSONPath-like string, e.g.
        # $['balancingData']['balancingFigures'][0]['quantityType'].
        json_path = "$" + "".join(f"[{part!r}]" if not isinstance(part, int) else f"[{part}]" for part in err.path)
        messages.append(
            f"{rel(instance_path, root)} failed against {rel(schema_path, root)} at {json_path}: {err.message}"
        )
    return messages
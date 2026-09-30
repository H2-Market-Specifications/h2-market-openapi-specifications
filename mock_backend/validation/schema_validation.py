from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators
from jsonschema.protocols import Validator

from ..errors import ErrorDetail, H2ApiError

_MAX_DISPLAY_LENGTH = 80

# Keywords whose values are subschemas, maps of subschemas, or lists of subschemas.
_SUBSCHEMA_KEYWORDS = (
    "additionalItems",
    "additionalProperties",
    "contains",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SUBSCHEMA_MAP_KEYWORDS = ("$defs", "definitions", "dependentSchemas", "patternProperties", "properties")
_SUBSCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")


class MessageValidator:
    """Validate a payload against an OpenAPI request-body JSON Schema."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = _forbid_with_not(deepcopy(schema))
        self._validator = schema_validator(self._schema)

    def validate(self, payload: Any, *, transaction_id: str | None = None) -> None:
        details = (
            detail for error in sorted(self._validator.iter_errors(payload), key=_error_sort_key) for detail in _schema_error_details(error)
        )
        errors = tuple(dict.fromkeys(details))
        if errors:
            raise H2ApiError.validation_failed(errors, transaction_id=transaction_id)


def schema_validator(schema: dict[str, Any]) -> Validator:
    """Create a Draft 2020-12 validator whose pattern `$` anchors and date-times reject a trailing newline, as JSON Schema requires."""
    _EcmaDraft202012Validator.check_schema(schema)
    return _EcmaDraft202012Validator(schema, format_checker=_FORMAT_CHECKER)


def _ecma_pattern(validator: Validator, pattern: str, instance: Any, _schema: Any) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and not _ecma_regex(pattern).search(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


@cache
def _ecma_regex(pattern: str) -> re.Pattern[str]:
    # Python's "$" also matches before a trailing newline; the ECMA-262 "$" used by JSON Schema only matches at the end.
    translated: list[str] = []
    escaped = in_class = False
    for char in pattern:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "$" and not in_class:
            char = r"\Z"
        translated.append(char)
    return re.compile("".join(translated))


_EcmaDraft202012Validator = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern})


def _format_checker() -> FormatChecker:
    checker = FormatChecker()
    # rfc3339-validator anchors with Python's "$", so "…Z\n" would pass as a date-time.
    for name in ("date-time", "time"):
        if name in checker.checkers:
            check, raises = checker.checkers[name]
            checker.checks(name, raises=raises)(_without_trailing_newline(check))
    return checker


def _without_trailing_newline(check: Callable[[object], bool]) -> Callable[[object], bool]:
    def strict_check(value: object) -> bool:
        return not (isinstance(value, str) and value.endswith("\n")) and check(value)

    return strict_check


_FORMAT_CHECKER = _format_checker()


def _forbid_with_not(schema: Any) -> Any:
    # jsonschema reports a `false` subschema without the property name or index it applies to.
    # `{"not": {}}` rejects the same values and keeps the full path. `additionalProperties: false` has its own message.
    if schema is False:
        return {"not": {}}
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    for keyword in _SUBSCHEMA_KEYWORDS:
        if keyword in result and not (keyword == "additionalProperties" and result[keyword] is False):
            result[keyword] = _forbid_with_not(result[keyword])
    for keyword in _SUBSCHEMA_MAP_KEYWORDS:
        if isinstance(result.get(keyword), dict):
            result[keyword] = {name: _forbid_with_not(subschema) for name, subschema in result[keyword].items()}
    for keyword in _SUBSCHEMA_LIST_KEYWORDS:
        if isinstance(result.get(keyword), list):
            result[keyword] = [_forbid_with_not(subschema) for subschema in result[keyword]]
    return result


def _error_sort_key(error: Any) -> tuple[Any, ...]:
    # Array indices sort numerically, so /values/2 comes before /values/10.
    return tuple((isinstance(part, str), part) for part in error.absolute_path), str(error.validator)


def _schema_error_details(error: Any) -> tuple[ErrorDetail, ...]:
    parts = [str(part) for part in error.absolute_path]
    validator = str(error.validator)

    if validator == "required":
        missing = [item for item in error.validator_value if isinstance(item, str) and item not in error.instance]
        return tuple(ErrorDetail(_pointer([*parts, name]), f"Required property {_display(name)} is missing.") for name in missing)

    if validator == "additionalProperties":
        unexpected = _unexpected_properties(error)
        return tuple(ErrorDetail(_pointer([*parts, name]), "Unexpected property is not allowed.") for name in unexpected)

    if validator == "not" and error.validator_value == {}:
        noun = "Property" if error.absolute_path and isinstance(error.absolute_path[-1], str) else "Value"
        return (ErrorDetail(_pointer(parts), f"{noun} is not allowed."),)

    path = _pointer(parts)
    message = human_message(error)
    return (ErrorDetail(path, message),)


def human_message(error: Any) -> str:
    validator = str(error.validator)
    value = error.instance
    limit = error.validator_value

    if validator == "enum":
        allowed = ", ".join(_display(item, quote_strings=False) for item in limit)
        return f"Value {_display(value)} is not allowed. Allowed values: {allowed}."
    if validator == "const":
        return f"Value must equal {_display(limit)}."
    if validator == "type":
        expected = limit if isinstance(limit, list) else [limit]
        names = " or ".join(str(item) for item in expected)
        return f"Expected type {names}; received {_json_type(value)}."
    if validator == "pattern":
        # Plain quotes: repr() would double every backslash in the pattern.
        return f"Value does not match required pattern '{_display(limit, quote_strings=False)}'."
    if validator == "format":
        return f"Value must be a valid {limit}."
    if validator == "minItems":
        return f"Expected at least {limit} {_plural('item', limit)}; received {len(value)}."
    if validator == "maxItems":
        return f"Expected at most {limit} {_plural('item', limit)}; received {len(value)}."
    if validator == "minLength":
        return f"Expected at least {limit} {_plural('character', limit)}; received {len(value)}."
    if validator == "maxLength":
        return f"Expected at most {limit} {_plural('character', limit)}; received {len(value)}."
    if validator == "minimum":
        return f"Value must be greater than or equal to {_display(limit, quote_strings=False)}."
    if validator == "maximum":
        return f"Value must be less than or equal to {_display(limit, quote_strings=False)}."
    if validator == "exclusiveMinimum":
        return f"Value must be greater than {_display(limit, quote_strings=False)}."
    if validator == "exclusiveMaximum":
        return f"Value must be less than {_display(limit, quote_strings=False)}."
    if validator == "multipleOf":
        return f"Value must be a multiple of {_display(limit, quote_strings=False)}."
    if validator == "uniqueItems":
        return "Array items must be unique."
    if validator == "minProperties":
        return f"Expected at least {limit} {_plural('property', limit, 'properties')}; received {len(value)}."
    if validator == "maxProperties":
        return f"Expected at most {limit} {_plural('property', limit, 'properties')}; received {len(value)}."
    if validator == "allOf":
        return "Value does not satisfy all required schema alternatives."
    if validator == "anyOf":
        return "Value must satisfy at least one allowed schema alternative."
    if validator == "oneOf":
        return "Value must satisfy exactly one allowed schema alternative."
    if validator == "not":
        return "Value matches a schema that is explicitly disallowed."
    return f"Value does not satisfy the {validator} schema constraint."


def _unexpected_properties(error: Any) -> tuple[str, ...]:
    if not isinstance(error.instance, dict) or not isinstance(error.schema, dict):
        return ()
    properties = error.schema.get("properties", {})
    known = set(properties) if isinstance(properties, dict) else set()
    patterns = error.schema.get("patternProperties", {})
    compiled = [re.compile(pattern) for pattern in patterns] if isinstance(patterns, dict) else []
    return tuple(sorted(name for name in error.instance if name not in known and not any(pattern.search(name) for pattern in compiled)))


def _plural(noun: str, count: Any, plural: str | None = None) -> str:
    return noun if count == 1 else (plural or noun + "s")


def _pointer(parts: list[str]) -> str:
    return "/" + "/".join(_escape_json_pointer(part) for part in parts)


def _display(value: Any, *, quote_strings: bool = True) -> str:
    if isinstance(value, str):
        text = value
        if len(text) > _MAX_DISPLAY_LENGTH:
            text = text[: _MAX_DISPLAY_LENGTH - 1] + "…"
        return repr(text) if quote_strings else text
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = f"<{_json_type(value)}>"
    return text if len(text) <= _MAX_DISPLAY_LENGTH else text[: _MAX_DISPLAY_LENGTH - 1] + "…"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _escape_json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")

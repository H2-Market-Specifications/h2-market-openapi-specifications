from __future__ import annotations

import pytest

from mock_backend.errors import H2ApiError
from mock_backend.validation.schema_validation import MessageValidator


def _details(schema: dict, payload: object) -> list[dict[str, str]]:
    with pytest.raises(H2ApiError) as caught:
        MessageValidator(schema).validate(payload)
    return [item.to_dict() for item in caught.value.error_response.errors]


@pytest.mark.parametrize(
    ("schema", "payload", "expected"),
    [
        (
            {"type": "object", "required": ["message"], "properties": {"message": {}}},
            {},
            {"path": "/message", "message": "Required property 'message' is missing."},
        ),
        (
            {"enum": ["GridOperator"]},
            "MarketAreaManager",
            {
                "path": "/",
                "message": ("Value " + chr(39) + "MarketAreaManager" + chr(39) + " is not allowed. Allowed values: GridOperator."),
            },
        ),
        (
            {"type": "integer"},
            "7",
            {"path": "/", "message": "Expected type integer; received string."},
        ),
        (
            {"type": "string", "pattern": "^[A-Z]+$"},
            "lower",
            {
                "path": "/",
                "message": ("Value does not match required pattern " + chr(39) + "^[A-Z]+$" + chr(39) + "."),
            },
        ),
        (
            {"type": "string", "format": "date-time"},
            "not-a-date",
            {"path": "/", "message": "Value must be a valid date-time."},
        ),
        (
            {"type": "array", "minItems": 2},
            [1],
            {"path": "/", "message": "Expected at least 2 items; received 1."},
        ),
        (
            {"type": "number", "maximum": 10},
            11,
            {"path": "/", "message": "Value must be less than or equal to 10."},
        ),
        (
            {"type": "array", "maxItems": 1},
            [1, 2],
            {"path": "/", "message": "Expected at most 1 item; received 2."},
        ),
        (
            {"type": "number", "minimum": 10},
            9,
            {"path": "/", "message": "Value must be greater than or equal to 10."},
        ),
        (
            {"type": "string", "minLength": 3},
            "ab",
            {"path": "/", "message": "Expected at least 3 characters; received 2."},
        ),
        (
            {"type": "string", "maxLength": 2},
            "abc",
            {"path": "/", "message": "Expected at most 2 characters; received 3."},
        ),
        (
            {"const": 7},
            8,
            {"path": "/", "message": "Value must equal 7."},
        ),
        (
            {"type": "number", "multipleOf": 3},
            7,
            {"path": "/", "message": "Value must be a multiple of 3."},
        ),
        (
            {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            [],
            {
                "path": "/",
                "message": "Value must satisfy exactly one allowed schema alternative.",
            },
        ),
    ],
)
def test_common_schema_failures_have_human_readable_messages(
    schema: dict,
    payload: object,
    expected: dict[str, str],
) -> None:
    assert expected in _details(schema, payload)


def test_each_unexpected_property_gets_an_escaped_pointer() -> None:
    details = _details(
        {
            "type": "object",
            "properties": {"known": {}},
            "additionalProperties": False,
        },
        {"known": 1, "bad/key": 2, "til~de": 3},
    )

    assert details == [
        {"path": "/bad~1key", "message": "Unexpected property is not allowed."},
        {"path": "/til~0de", "message": "Unexpected property is not allowed."},
    ]


def test_multiple_missing_properties_are_reported_once_each() -> None:
    details = _details(
        {
            "type": "object",
            "required": ["documentNumber", "creationDateTime"],
            "properties": {
                "documentNumber": {},
                "creationDateTime": {},
            },
        },
        {},
    )

    assert details == [
        {
            "path": "/documentNumber",
            "message": "Required property 'documentNumber' is missing.",
        },
        {
            "path": "/creationDateTime",
            "message": "Required property 'creationDateTime' is missing.",
        },
    ]


def test_schema_failures_are_aggregated_without_raw_jsonschema_wording() -> None:
    details = _details(
        {
            "type": "object",
            "required": ["required"],
            "properties": {
                "role": {"enum": ["GridOperator"]},
                "count": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        {"role": "MarketAreaManager", "count": "many", "extra": True},
    )

    assert len(details) == 4
    assert {item["path"] for item in details} == {"/required", "/role", "/count", "/extra"}
    assert all("is not one of" not in item["message"] for item in details)
    assert all("Additional properties are not allowed" not in item["message"] for item in details)


@pytest.mark.parametrize(
    ("schema", "payload", "expected"),
    [
        (
            {"type": "string", "pattern": "^[0-9]{13}$"},
            "9700123456789\n",
            {"path": "/", "message": "Value does not match required pattern '^[0-9]{13}$'."},
        ),
        (
            {"type": "string", "format": "date-time"},
            "2026-03-22T15:47:45Z\n",
            {"path": "/", "message": "Value must be a valid date-time."},
        ),
    ],
)
def test_patterns_and_date_times_reject_a_trailing_newline(
    schema: dict,
    payload: object,
    expected: dict[str, str],
) -> None:
    assert _details(schema, payload) == [expected]


@pytest.mark.parametrize(("pattern", "payload"), [(r"^a\$$", "a$"), ("^[$]$", "$"), ("^[0-9]{13}$", "9700123456789")])
def test_literal_dollar_signs_and_valid_values_still_match(pattern: str, payload: str) -> None:
    MessageValidator({"type": "string", "pattern": pattern}).validate(payload)


def test_pattern_messages_show_the_pattern_verbatim() -> None:
    assert _details({"type": "string", "pattern": r"^\d{4}$"}, "abc") == [
        {"path": "/", "message": r"Value does not match required pattern '^\d{4}$'."},
    ]


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            {"type": "object", "properties": {"values": {"items": {"properties": {"helperCauser": False}}}}},
            {"path": "/values/0/helperCauser", "message": "Property is not allowed."},
        ),
        (
            {"type": "object", "properties": {"values": {"items": False}}},
            {"path": "/values/0", "message": "Value is not allowed."},
        ),
    ],
)
def test_false_subschemas_report_the_rejected_location(schema: dict, expected: dict[str, str]) -> None:
    assert _details(schema, {"values": [{"helperCauser": "Helper"}]}) == [expected]


def test_errors_are_ordered_by_numeric_array_index() -> None:
    details = _details({"type": "array", "items": {"type": "integer"}}, ["x"] * 12)

    assert [item["path"] for item in details] == [f"/{index}" for index in range(12)]


def test_large_values_are_bounded_in_error_messages() -> None:
    detail = _details({"enum": ["short"]}, "x" * 500)[0]

    assert len(detail["message"]) < 180
    assert "x" * 100 not in detail["message"]

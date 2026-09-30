from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from mock_backend.app import create_app
from mock_backend.message_configuration import MESSAGE_CONFIGURATIONS
from mock_backend.transaction import UUID7_PATTERN as UUID7
from mock_backend.transaction import new_transaction_id


@dataclass(frozen=True)
class AsgiResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


async def _request(
    application: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> AsgiResponse:
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    raw_headers = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in (headers or {}).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    await application(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in start["headers"]}
    response_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return AsgiResponse(start["status"], response_headers, response_body)


def _call(
    application: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> AsgiResponse:
    return asyncio.run(_request(application, method, path, headers=headers, body=body))


def _resolve(document: dict[str, Any], value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get("$ref"), str):
        resolved: Any = document
        for part in value["$ref"].removeprefix("#/").split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        value = resolved
    return value


def _document(configuration: Any) -> dict[str, Any]:
    return yaml.safe_load(Path(configuration.openapi_path).read_text(encoding="utf-8"))


def _operation(configuration: Any, document: dict[str, Any]) -> dict[str, Any]:
    return document["paths"][configuration.submission_path]["post"]


def _examples(configuration: Any, document: dict[str, Any]) -> list[dict[str, Any]]:
    request_body = _resolve(document, _operation(configuration, document)["requestBody"])
    media = request_body["content"]["application/json"]
    return [deepcopy(_resolve(document, example)["value"]) for example in media["examples"].values()]


def _headers(configuration: Any, document: dict[str, Any]) -> dict[str, str]:
    result = {"Content-Type": "application/json"}
    for unresolved in _operation(configuration, document).get("parameters", []):
        parameter = _resolve(document, unresolved)
        name = parameter["name"]
        if name == "H2-Initial-Transaction-Id":
            continue
        if parameter.get("required"):
            result[name] = str(parameter["example"])
    return result


def _valid_request(configuration: Any) -> tuple[dict[str, str], dict[str, Any]]:
    document = _document(configuration)
    return _headers(configuration, document), _examples(configuration, document)[0]


def _assert_detail_shape(problem: dict[str, Any]) -> None:
    assert problem["errors"]
    assert all(set(detail) == {"path", "message"} for detail in problem["errors"])


def test_all_generated_messages_are_configured_with_unique_routes() -> None:
    assert len(MESSAGE_CONFIGURATIONS) == 16
    assert len({item.submission_path for item in MESSAGE_CONFIGURATIONS}) == 16
    assert len({Path(item.openapi_path) for item in MESSAGE_CONFIGURATIONS}) == 16
    assert len({item.openapi_serving_path for item in MESSAGE_CONFIGURATIONS}) == 16
    assert len({item.scalar_title for item in MESSAGE_CONFIGURATIONS}) == 16
    assert {"default-quantitydeclarationresponse", "default-balancinggrouplist"} <= {
        item.message_id for item in MESSAGE_CONFIGURATIONS
    }

    application = create_app()
    routes = {(route.path, frozenset(route.methods or ())) for route in application.routes}
    for configuration in MESSAGE_CONFIGURATIONS:
        document = _document(configuration)
        metadata = document["x-h2-message"]
        assert configuration.message_id == metadata["id"]
        assert configuration.message_type == metadata["type"]
        assert configuration.message_sub_type == metadata["subType"]
        assert configuration.message_version == metadata["messageVersion"]
        assert configuration.scalar_title == (f"{configuration.message_type} / {configuration.message_sub_type}")
        assert all(boilerplate not in configuration.scalar_title for boilerplate in ("H2", "API", "Pre-Release"))
        assert (configuration.submission_path, frozenset({"POST"})) in routes
        assert (configuration.openapi_serving_path, frozenset({"GET"})) in routes


def test_scalar_lists_every_canonical_message_label() -> None:
    response = _call(create_app(), "GET", "/scalar")

    assert response.status == 200
    html = response.body.decode("utf-8")
    for configuration in MESSAGE_CONFIGURATIONS:
        assert configuration.scalar_title in html
        assert configuration.openapi_serving_path in html


def test_swagger_lists_every_canonical_message_label() -> None:
    response = _call(create_app(), "GET", "/swagger")

    assert response.status == 200
    html = response.body.decode("utf-8")
    for configuration in MESSAGE_CONFIGURATIONS:
        assert configuration.scalar_title in html
        assert configuration.openapi_serving_path in html


def test_scalar_examples_match_reachable_mock_error_responses() -> None:
    application = create_app()

    for configuration in MESSAGE_CONFIGURATIONS:
        document = _document(configuration)
        operation = _operation(configuration, document)
        headers, payload = _valid_request(configuration)

        bad_request_headers = dict(headers)
        bad_request_headers.pop("H2-Message-Receiver")

        unsupported_media_type_headers = dict(headers)
        unsupported_media_type_headers.pop("Content-Type")

        invalid_payload = deepcopy(payload)
        invalid_payload.pop("message")

        actual_responses = {
            "400": _call(
                application,
                "POST",
                configuration.submission_path,
                headers=bad_request_headers,
                body=json.dumps(payload).encode("utf-8"),
            ),
            "415": _call(
                application,
                "POST",
                configuration.submission_path,
                headers=unsupported_media_type_headers,
                body=json.dumps(payload).encode("utf-8"),
            ),
            "422": _call(
                application,
                "POST",
                configuration.submission_path,
                headers=headers,
                body=json.dumps(invalid_payload).encode("utf-8"),
            ),
        }

        for status, response in actual_responses.items():
            assert response.status == int(status)
            documented = _resolve(document, operation["responses"][status])["content"]["application/json"]["example"]
            actual = response.json()

            for field, value in documented.items():
                if field == "errors":
                    assert all(error in actual["errors"] for error in value)
                else:
                    assert actual[field] == value


def test_every_embedded_openapi_example_is_schema_valid_and_accepted() -> None:
    application = create_app()
    example_count = 0

    for configuration in MESSAGE_CONFIGURATIONS:
        document = _document(configuration)
        operation = _operation(configuration, document)
        request_body = _resolve(document, operation["requestBody"])
        media = request_body["content"]["application/json"]
        schema = _resolve(document, media["schema"])
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        headers = _headers(configuration, document)

        for payload in _examples(configuration, document):
            example_count += 1
            assert list(validator.iter_errors(payload)) == []
            response = _call(
                application,
                "POST",
                configuration.submission_path,
                headers=headers,
                body=json.dumps(payload).encode("utf-8"),
            )
            assert response.status == 200, (configuration.submission_path, response.body)

    assert example_count >= 18


def test_served_openapi_documents_leave_embedded_examples_unchanged() -> None:
    application = create_app()

    for configuration in MESSAGE_CONFIGURATIONS:
        response = _call(application, "GET", configuration.openapi_serving_path)
        assert response.status == 200
        assert response.body == Path(configuration.openapi_path).read_bytes()


def test_gateway_reports_multiple_header_errors_with_lean_details() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    headers["H2-Initial-Transaction-Id"] = "not-a-uuid"
    headers.pop("H2-Message-Receiver")
    headers.pop("H2-Business-Process")
    headers["H2-Message-Sender"] = "invalid"

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 400
    problem = response.json()
    _assert_detail_shape(problem)
    assert {detail["path"] for detail in problem["errors"]} == {
        "/H2-Initial-Transaction-Id",
        "/H2-Message-Sender",
        "/H2-Message-Receiver",
        "/H2-Business-Process",
    }
    # An invalid H2-Initial-Transaction-Id leaves the current H2-Transaction-Id as effective transaction ID.
    assert problem["transactionId"] == headers["H2-Transaction-Id"]


@pytest.mark.parametrize(
    "transaction_id",
    [
        None,
        "not-a-uuid",
        "0b5c5b9a-3d2e-4c1f-9a7b-1c2d3e4f5a6b",  # version 4
        "019DBE80-9B35-773D-8F14-7C778C8D71B1",  # uppercase
    ],
)
def test_request_without_valid_transaction_id_is_answered_without_body(transaction_id: str | None) -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    if transaction_id is None:
        headers.pop("H2-Transaction-Id")
    else:
        headers["H2-Transaction-Id"] = transaction_id

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 400
    assert response.body == b""
    assert "h2-transaction-id" not in response.headers
    assert "h2-reference-id" not in response.headers
    assert response.headers["h2-api-version"] == configuration.api_version


def test_gateway_header_errors_use_readable_messages() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    business_process = headers["H2-Business-Process"]
    headers["H2-Initial-Transaction-Id"] = "not-a-uuid"
    headers["H2-Message-Sender"] = "invalid"
    headers["H2-Business-Process"] = "x" * 300

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 400
    errors = response.json()["errors"]
    messages = {detail["path"]: detail["message"] for detail in errors}
    assert "Value must be a valid uuid." in [
        detail["message"] for detail in errors if detail["path"] == "/H2-Initial-Transaction-Id"
    ]
    assert messages["/H2-Message-Sender"] == r"Value does not match required pattern '^\d{13}$'."
    assert messages["/H2-Business-Process"].endswith(f"is not allowed. Allowed values: {business_process}.")
    assert "x" * 100 not in messages["/H2-Business-Process"]


@pytest.mark.parametrize(
    ("section", "field"),
    [("message", "documentNumber"), ("message", "creationDateTime"), ("parties", "sender")],
)
def test_trailing_newlines_in_constrained_strings_are_rejected(section: str, field: str) -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    path = f"/{section}/{field}"
    if field == "sender":
        payload[section][field]["partnerCode"] += "\n"
        path += "/partnerCode"
    else:
        payload[section][field] += "\n"

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 422
    # A field may carry several constraints (e.g. format and pattern), each reporting its own error.
    assert {detail["path"] for detail in response.json()["errors"]} == {path}


def test_success_response_has_its_own_transaction_id_and_refers_to_the_request() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 200
    assert UUID7.fullmatch(response.headers["h2-transaction-id"])
    assert response.headers["h2-transaction-id"] != headers["H2-Transaction-Id"]
    assert response.headers["h2-reference-id"] == headers["H2-Transaction-Id"]
    assert response.headers["h2-api-version"] == configuration.api_version == "1.0.0"
    assert response.json()["referenceNumber"] == payload["message"]["documentNumber"]


@pytest.mark.parametrize("valid_payload", [True, False])
def test_retry_refers_to_the_initial_transaction_id(valid_payload: bool) -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    initial_transaction_id = new_transaction_id()
    headers["H2-Initial-Transaction-Id"] = initial_transaction_id
    if not valid_payload:
        payload.pop("message")

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == (200 if valid_payload else 422)
    assert response.headers["h2-reference-id"] == initial_transaction_id
    if not valid_payload:
        assert response.json()["transactionId"] == initial_transaction_id


def test_new_transaction_ids_are_distinct_uuid7_values() -> None:
    values = {new_transaction_id() for _ in range(100)}

    assert len(values) == 100
    assert all(UUID7.fullmatch(value) for value in values)


def test_wrong_method_is_rejected_with_allow_header_and_error_response() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, _payload = _valid_request(configuration)

    response = _call(create_app(), "GET", configuration.submission_path, headers=headers)

    assert response.status == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["h2-api-version"] == configuration.api_version
    assert response.headers["h2-reference-id"] == headers["H2-Transaction-Id"]
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"
    assert response.json()["transactionId"] == headers["H2-Transaction-Id"]


@pytest.mark.parametrize("with_transaction_id", [True, False])
def test_unknown_route_is_rejected_with_error_response(with_transaction_id: bool) -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    if not with_transaction_id:
        headers.pop("H2-Transaction-Id")

    response = _call(create_app(), "POST", "/api/v1/unknown", headers=headers, body=json.dumps(payload).encode("utf-8"))

    assert response.status == 404
    assert response.headers["h2-api-version"] == configuration.api_version
    if with_transaction_id:
        assert response.json()["code"] == "ROUTE_NOT_FOUND"
        assert response.json()["transactionId"] == headers["H2-Transaction-Id"]
    else:
        assert response.body == b""


def test_conditionally_forbidden_property_is_reported_at_its_own_path() -> None:
    configuration = next(item for item in MESSAGE_CONFIGURATIONS if item.message_type == "BALANCING")
    headers, payload = _valid_request(configuration)
    figures = payload["balancingData"]["balancingFigures"]
    index = next(index for index, figure in enumerate(figures) if figure["quantityType"] == "BGBalance")
    figures[index]["values"][0]["helperCauser"] = "Helper"

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 422
    assert response.json()["errors"] == [
        {"path": f"/balancingData/balancingFigures/{index}/values/0/helperCauser", "message": "Property is not allowed."},
    ]


def test_schema_reports_multiple_errors_with_lean_details() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    del payload["message"]["documentNumber"]
    del payload["parties"]["sender"]["partnerCode"]

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 422
    problem = response.json()
    _assert_detail_shape(problem)
    assert {detail["path"] for detail in problem["errors"]} >= {
        "/message/documentNumber",
        "/parties/sender/partnerCode",
    }


def test_schema_valid_time_series_reports_aggregated_semantic_errors() -> None:
    configuration = next(
        item for item in MESSAGE_CONFIGURATIONS if item.message_type == "MEASUREMENT" and item.message_sub_type == "Preliminary"
    )
    headers, payload = _valid_request(configuration)
    values = payload["measurementData"]["measurements"][0]["values"]
    values[1]["timestamp"] = values[0]["timestamp"]
    configuration.message_validator.validate(payload)

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 422
    problem = response.json()
    _assert_detail_shape(problem)
    assert len(problem["errors"]) >= 2
    assert {
        "/measurementData/measurements/0/values/1/timestamp",
        "/measurementData/measurements/0/values",
    } <= {detail["path"] for detail in problem["errors"]}


def test_malformed_json_returns_a_lean_bad_request_detail() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, _payload = _valid_request(configuration)

    response = _call(
        create_app(),
        "POST",
        configuration.submission_path,
        headers=headers,
        body=b"{",
    )

    assert response.status == 400
    _assert_detail_shape(response.json())


@pytest.mark.parametrize("missing_header,status", [("H2-Message-Receiver", 400), ("Content-Type", 415)])
def test_gateway_errors_include_cors_headers(missing_header: str, status: int) -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, _payload = _valid_request(configuration)
    headers.pop(missing_header)
    headers["Origin"] = "http://localhost:3000"

    response = _call(create_app(), "POST", configuration.submission_path, headers=headers, body=b"{")

    assert response.status == status
    assert response.headers["access-control-allow-origin"] == "*"
    exposed = response.headers["access-control-expose-headers"]
    assert all(name in exposed for name in ("H2-Transaction-Id", "H2-Reference-Id", "H2-API-Version", "H2-Response-Origin"))
    assert response.json()["responseOrigin"] == "Gateway"
    assert response.headers["h2-reference-id"] == headers["H2-Transaction-Id"]
    assert response.json()["transactionId"] == headers["H2-Transaction-Id"]
    assert response.headers["content-type"] == "application/json"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_are_rejected(constant: str) -> None:
    configuration = next(item for item in MESSAGE_CONFIGURATIONS if item.message_type == "MEASUREMENT")
    headers, payload = _valid_request(configuration)
    payload["measurementData"]["measurements"][0]["values"][0]["quantity"] = float(constant)

    response = _call(
        create_app(), "POST", configuration.submission_path,
        headers=headers, body=json.dumps(payload).encode("utf-8"),
    )

    assert response.status == 400
    assert response.json()["code"] == "BAD_REQUEST"
    assert response.json()["responseOrigin"] == "Gateway"
    assert response.headers["h2-reference-id"] == headers["H2-Transaction-Id"]
    _assert_detail_shape(response.json())


def test_repeated_request_and_header_body_mismatch_are_accepted() -> None:
    configuration = MESSAGE_CONFIGURATIONS[0]
    headers, payload = _valid_request(configuration)
    headers["H2-Message-Sender"] = "9999999999999"
    body = json.dumps(payload).encode("utf-8")
    application = create_app()

    first = _call(application, "POST", configuration.submission_path, headers=headers, body=body)
    second = _call(application, "POST", configuration.submission_path, headers=headers, body=body)

    assert first.status == 200
    assert second.status == 200

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.requests import Request

from mock_backend.errors import H2ApiError
from mock_backend.message_configuration import MESSAGE_CONFIGURATIONS
from mock_backend.processing import process_submission
from mock_backend.validation.semantic_validation import SemanticValidator


def _resolve(document: dict[str, Any], value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get("$ref"), str):
        resolved: Any = document
        for part in value["$ref"].removeprefix("#/").split("/"):
            resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
        value = resolved
    return value


def _examples(configuration: Any) -> list[dict[str, Any]]:
    document = yaml.safe_load(Path(configuration.openapi_path).read_text(encoding="utf-8"))
    operation = document["paths"][configuration.submission_path]["post"]
    request_body = _resolve(document, operation["requestBody"])
    media = request_body["content"]["application/json"]
    return [deepcopy(_resolve(document, example)["value"]) for example in media["examples"].values()]


def _configuration(message_type: str, message_sub_type: str | None = None) -> Any:
    return next(
        item
        for item in MESSAGE_CONFIGURATIONS
        if item.message_type == message_type and (message_sub_type is None or item.message_sub_type == message_sub_type)
    )


def test_semantic_validation_uses_the_bundled_validator() -> None:
    from mock_backend.validation import semantic_validation

    assert semantic_validation._SharedSemanticValidator.__module__.startswith("mock_backend._vendor.h2_message_validation.")
    info = json.loads((Path(semantic_validation.__file__).parents[1] / "_vendor/h2_message_validation/SOURCE.json").read_text(encoding="utf-8"))
    assert info["package"] == "tools/validation"


def test_every_embedded_example_passes_semantic_validation() -> None:
    count = 0
    for configuration in MESSAGE_CONFIGURATIONS:
        for payload in _examples(configuration):
            count += 1
            assert configuration.semantic_validator.validate(payload) == ()
    assert count >= 18


def test_measurement_reports_ascending_and_missing_grid_errors_together() -> None:
    configuration = _configuration("MEASUREMENT", "Preliminary")
    payload = _examples(configuration)[0]
    values = payload["measurementData"]["measurements"][0]["values"]
    values[1]["timestamp"] = values[0]["timestamp"]

    errors = configuration.semantic_validator.validate(payload)

    assert errors[0].path == "/measurementData/measurements/0/values/1/timestamp"
    assert errors[0].message == "Timestamps must be strictly ascending."
    assert any(error.path == "/measurementData/measurements/0/values" and "is missing" in error.message for error in errors)


@pytest.mark.parametrize(
    ("message_type", "expected_path"),
    [
        (
            "NOMINATION",
            "/nominationData/nominations/0/values/0/timestamp",
        ),
        (
            "NOMINATIONRESPONSE",
            "/nominationResponseData/nominationResponses/0/timeSeries/0/values/0/timestamp",
        ),
        (
            "MATCHING",
            "/matchingData/accountSpecificData/0/timeSeries/0/values/0/timestamp",
        ),
        (
            "QUANTITYDECLARATION",
            "/quantityDeclarationData/values/0/timestamp",
        ),
        (
            "ALLOCATION",
            "/allocationData/allocations/0/values/0/timestamp",
        ),
    ],
)
def test_regular_layouts_report_the_exact_nested_timestamp_path(
    message_type: str,
    expected_path: str,
) -> None:
    configuration = _configuration(message_type)
    payload = _examples(configuration)[0]
    root = payload

    def values_list(node: Any) -> list[dict[str, Any]] | None:
        if isinstance(node, list):
            if node and all(isinstance(item, dict) and "timestamp" in item for item in node):
                return node
            for item in node:
                found = values_list(item)
                if found is not None:
                    return found
        if isinstance(node, dict):
            for item in node.values():
                found = values_list(item)
                if found is not None:
                    return found
        return None

    values = values_list(root)
    assert values is not None
    values[0]["timestamp"] = "not-a-date"

    errors = configuration.semantic_validator.validate(payload)

    assert any(error.path == expected_path and error.message == "Timestamp must be a timezone-aware date-time." for error in errors)


@pytest.mark.parametrize(
    ("period", "granularity", "count"),
    [
        ("2024-03-31T00:00:00+01:00/P1D", "PT1H", 23),
        ("2024-10-27T00:00:00+02:00/P1D", "PT1H", 25),
    ],
)
def test_calendar_day_grids_follow_berlin_dst(
    period: str,
    granularity: str,
    count: int,
) -> None:
    from datetime import UTC, datetime, timedelta

    start = datetime.fromisoformat(period.split("/")[0]).astimezone(UTC)
    values = [{"timestamp": (start + timedelta(hours=index)).isoformat().replace("+00:00", "Z")} for index in range(count)]
    payload = {
        "quantityDeclarationData": {
            "period": period,
            "granularity": granularity,
            "values": values,
        }
    }

    assert SemanticValidator("QUANTITYDECLARATION", "Default").validate(payload) == ()


def _balancing_payload(quantity_type: str, periods: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "balancingData": {
            "balancingFigures": [
                {
                    "quantityType": quantity_type,
                    "values": [{"periodStart": start, "periodEnd": end} for start, end in periods],
                }
            ]
        }
    }


@pytest.mark.parametrize(
    ("sub_type", "quantity_type"),
    [
        ("ContinuousBalancing", "BGBalance"),
        ("DifferenceQuantities", "BGBalance"),
        ("DifferenceQuantities", "BGBalanceDifference"),
    ],
)
@pytest.mark.parametrize(
    ("second_start", "word"),
    [
        ("2024-01-01T00:30:00Z", "overlaps"),
        ("2024-01-01T01:30:00Z", "leaves a gap"),
    ],
)
def test_interval_balancing_figures_reject_overlap_and_gap(
    sub_type: str,
    quantity_type: str,
    second_start: str,
    word: str,
) -> None:
    payload = _balancing_payload(
        quantity_type,
        [
            ("2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"),
            (second_start, "2024-01-01T02:00:00Z"),
        ],
    )

    errors = SemanticValidator("BALANCING", sub_type).validate(payload)

    assert errors[0].path == "/balancingData/balancingFigures/0/values/1/periodStart"
    assert word in errors[0].message


@pytest.mark.parametrize("quantity_type", ["BGBalanceCumulative", "BGBalanceCumulativeExternal", "OverallNetworkStatus"])
def test_cumulative_balancing_figures_require_common_start_and_increasing_end(quantity_type: str) -> None:
    payload = _balancing_payload(
        quantity_type,
        [
            ("2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"),
            ("2024-01-01T00:30:00Z", "2024-01-01T01:00:00Z"),
        ],
    )

    errors = SemanticValidator("BALANCING", "ContinuousBalancing").validate(payload)

    assert {error.path for error in errors} == {
        "/balancingData/balancingFigures/0/values/1/periodStart",
        "/balancingData/balancingFigures/0/values/1/periodEnd",
    }


def _quantity_payload(
    period: str,
    granularity: str,
    timestamps: list[str],
) -> dict[str, Any]:
    return {
        "quantityDeclarationData": {
            "period": period,
            "granularity": granularity,
            "values": [{"timestamp": timestamp} for timestamp in timestamps],
        }
    }


def test_nondivisible_period_uses_regular_root_period_path() -> None:
    errors = SemanticValidator("QUANTITYDECLARATION", "Default").validate(_quantity_payload("2024-01-01T00:00:00Z/PT90M", "PT1H", []))

    assert errors[0].path == "/quantityDeclarationData/period"
    assert errors[0].message == "Period is not exactly divisible by granularity. Path: /quantityDeclarationData/granularity."


def test_invalid_period_and_granularity_use_measurement_data_paths() -> None:
    invalid_period = {
        "measurementData": {
            "period": "2024-01-01T00:00:00Z/PT0H",
            "granularity": "PT1H",
            "measurements": [{"values": []}],
        }
    }
    invalid_granularity = deepcopy(invalid_period)
    invalid_granularity["measurementData"]["period"] = "2024-01-01T00:00:00Z/PT1H"
    invalid_granularity["measurementData"]["granularity"] = "P1M"

    assert SemanticValidator("MEASUREMENT", "Final").validate(invalid_period)[0].path == "/measurementData/period"
    assert SemanticValidator("MEASUREMENT", "Final").validate(invalid_granularity)[0].path == "/measurementData/granularity"


def test_length_mismatch_is_reported_on_values_collection() -> None:
    payload = _quantity_payload(
        "2024-01-01T00:00:00Z/PT2H",
        "PT1H",
        ["2024-01-01T00:00:00Z"],
    )

    errors = SemanticValidator("QUANTITYDECLARATION", "Default").validate(payload)

    assert errors[0].path == "/quantityDeclarationData/values"
    assert errors[0].message == "Expected 2 time-series values; received 1."


def test_explicit_period_end_and_equivalent_offsets_are_accepted() -> None:
    payload = _quantity_payload(
        "2024-01-01T00:00:00+01:00/2024-01-01T02:00:00+01:00",
        "PT1H",
        ["2023-12-31T23:00:00Z", "2024-01-01T01:00:00+01:00"],
    )

    assert SemanticValidator("QUANTITYDECLARATION", "Default").validate(payload) == ()


def test_allocation_uses_shared_period_and_granularity() -> None:
    payload = {
        "allocationData": {
            "period": "2024-01-01T00:00:00Z/PT2H",
            "granularity": "PT1H",
            "allocations": [
                {
                    "values": [
                        {"timestamp": "2024-01-01T00:00:00Z"},
                        {"timestamp": "2024-01-01T01:00:00Z"},
                    ],
                },
                {
                    "values": [
                        {"timestamp": "2024-01-01T00:00:00Z"},
                        {"timestamp": "not-a-date"},
                    ],
                },
            ],
        }
    }

    errors = SemanticValidator("ALLOCATION", "Nomination").validate(payload)

    assert any(
        error.path == "/allocationData/allocations/1/values/1/timestamp"
        and error.message == "Timestamp must be a timezone-aware date-time."
        for error in errors
    )


def test_measurement_allocation_uses_shared_quarter_hourly_period_and_granularity() -> None:
    payload = {
        "allocationData": {
            "period": "2024-01-01T00:00:00Z/PT1H",
            "granularity": "PT15M",
            "allocations": [
                {
                    "values": [
                        {"timestamp": "2024-01-01T00:00:00Z"},
                        {"timestamp": "2024-01-01T00:15:00Z"},
                        {"timestamp": "2024-01-01T00:30:00Z"},
                        {"timestamp": "2024-01-01T00:45:00Z"},
                    ],
                },
                {
                    "values": [
                        {"timestamp": "2024-01-01T00:00:00Z"},
                        {"timestamp": "2024-01-01T00:15:00Z"},
                        {"timestamp": "2024-01-01T00:30:00Z"},
                        {"timestamp": "2024-01-01T00:45:00Z"},
                    ],
                },
            ],
        }
    }

    assert SemanticValidator("ALLOCATION", "Measurement").validate(payload) == ()


def test_allocation_reports_root_granularity_path() -> None:
    payload = {
        "allocationData": {
            "period": "2024-01-01T00:00:00Z/PT1H",
            "granularity": "invalid",
            "allocations": [{"values": []}],
        }
    }

    errors = SemanticValidator("ALLOCATION", "Measurement").validate(payload)

    assert errors[0].path == "/allocationData/granularity"


def test_schema_validation_runs_before_semantic_validation() -> None:
    configuration = _configuration("MEASUREMENT", "Preliminary")
    payload = _examples(configuration)[0]
    payload.pop("message")
    payload["measurementData"]["measurements"][0]["values"][0]["timestamp"] = "not-a-date"

    class SemanticSpy:
        called = False

        def validate(self, value: Any) -> tuple:
            self.called = True
            return ()

    spy = SemanticSpy()
    configuration = replace(configuration, semantic_validator=spy)
    body = json.dumps(payload).encode()
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": configuration.submission_path,
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    with pytest.raises(H2ApiError) as caught:
        asyncio.run(process_submission(request, configuration, "019dbe80-9b35-773d-8f14-7c778c8d71b1"))

    assert caught.value.error_response.detail == "The message violates its JSON Schema."
    assert spy.called is False

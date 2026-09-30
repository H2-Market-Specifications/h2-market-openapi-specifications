"""
Business logic validation: measurement semantics (time series) and other semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import rel
from .time_utils import (
    Violation,
    validate_period_semantics,
    validate_time_series_semantics,
)


@dataclass
class ListSegment:
    """Iterates over a list and optionally extracts a context value."""

    key: str
    context_key: str | None = None
    context_label: str | None = None
    granularity_key: str | None = None
    period_key: str | None = None


@dataclass
class DictSegment:
    """Navigates to a fixed dictionary field."""

    key: str


@dataclass
class TimeSeriesEndpoint:
    """Marks the final array containing the time-series values."""

    key: str
    timestamp_field: str = "timestamp"


# A TimeSeriesConfig's "segments" list describes, step by step, how to walk
# from the message root down to the array of timestamped values (see
# validate_time_series_generic's traverse() below).
PathSegment = DictSegment | ListSegment | TimeSeriesEndpoint


@dataclass
class TimeSeriesConfig:
    """Describes the path from the message root to time-series values."""

    root_key: str
    period_path: list[str]
    granularity_path: list[str]
    segments: list[PathSegment]


@dataclass
class PeriodConfig:
    """Describes a list of explicitly bounded periods."""

    root_key: str
    periods_path: list[str]
    period_type_path: list[str]
    start_field: str = "periodStart"
    end_field: str = "periodEnd"
    status_field: str = "quality"


@dataclass
class BalancingConfig:
    """Describes the BALANCING message structure."""

    root_key: str = "balancingData"
    numbers_key: str = "balancingFigures"
    quantity_type_field: str = "quantityType"
    values_key: str = "values"
    start_field: str = "periodStart"
    end_field: str = "periodEnd"
    status_field: str = "quality"


# ---------------------------------------------------------------------------
# Configurations
# ---------------------------------------------------------------------------

# Each *_CONFIG below declares, for one message family, where its period and
# granularity live and the list of array segments to walk through to reach
# the actual time-series values (used by validate_time_series_generic).

ALLOCATION_CONFIG = TimeSeriesConfig(
    root_key="allocationData",
    period_path=["period"],
    granularity_path=["granularity"],
    segments=[
        ListSegment(
            key="allocations",
            context_key="internalAccount",
        ),
        TimeSeriesEndpoint(key="values"),
    ],
)

BALANCING_CONFIG = BalancingConfig()

MATCHING_CONFIG = TimeSeriesConfig(
    root_key="matchingData",
    period_path=["period"],
    granularity_path=["granularity"],
    segments=[
        ListSegment(
            key="accountSpecificData",
            context_key="externalAccount",
        ),
        ListSegment(
            key="timeSeries",
            context_key="quantityType",
        ),
        TimeSeriesEndpoint(key="values"),
    ],
)

MEASUREMENT_CONFIG = TimeSeriesConfig(
    root_key="measurementData",
    period_path=["period"],
    granularity_path=["granularity"],
    segments=[
        ListSegment(key="measurements"),
        TimeSeriesEndpoint(key="values"),
    ],
)

NOMINATION_CONFIG = TimeSeriesConfig(
    root_key="nominationData",
    period_path=["period"],
    granularity_path=["granularity"],
    segments=[
        ListSegment(
            key="nominations",
            context_key="externalAccount",
        ),
        TimeSeriesEndpoint(key="values"),
    ],
)

NOMINATION_RESPONSE_CONFIG = TimeSeriesConfig(
    root_key="nominationResponseData",
    period_path=["period"],
    granularity_path=["granularity"],
    segments=[
        ListSegment(
            key="nominationResponses",
            context_key="externalAccount",
        ),
        ListSegment(
            key="timeSeries",
            context_key="quantityType",
        ),
        TimeSeriesEndpoint(key="values"),
    ],
)

QUANTITYDECLARATION_CONFIG = TimeSeriesConfig(
    root_key="quantityDeclarationData",
    period_path=["period"],
    granularity_path=["granularity"],
    segments=[
        TimeSeriesEndpoint(key="values"),
    ],
)

# Unlike the other families, granularity here is per time-series entry
# rather than fixed at the message root (see granularity_key below), so
# granularity_path is left empty.
QUANTITYDECLARATION_RESPONSE_CONFIG = TimeSeriesConfig(
    root_key="quantityDeclarationResponseData",
    period_path=["period"],
    granularity_path=[],
    segments=[
        ListSegment(
            key="timeSeries",
            context_key="quantityType",
            granularity_key="granularity",
        ),
        TimeSeriesEndpoint(key="values"),
    ],
)


_CONFIG_BY_TYPE: dict[
    str,
    TimeSeriesConfig | PeriodConfig | BalancingConfig,
] = {
    "ALLOCATION": ALLOCATION_CONFIG,
    "BALANCING": BALANCING_CONFIG,
    "MATCHING": MATCHING_CONFIG,
    "MEASUREMENT": MEASUREMENT_CONFIG,
    "NOMINATION": NOMINATION_CONFIG,
    "NOMINATIONRESPONSE": NOMINATION_RESPONSE_CONFIG,
    "QUANTITYDECLARATION": QUANTITYDECLARATION_CONFIG,
    "QUANTITYDECLARATIONRESPONSE": QUANTITYDECLARATION_RESPONSE_CONFIG,
}


# The current BALANCING message subtype identifies the message format.
# The actual semantic validation rule is selected by quantityType.
_BALANCING_MESSAGE_SUBTYPES = {
    "ContinuousBalancing",
    "DifferenceQuantities",
}

# Maps each known balancing quantityType to the period rule that applies to
# it: "continuous" values must be contiguous (no gap/overlap), "cumulative"
# values must share a common start with a strictly increasing end.
_BALANCING_RULE_BY_QUANTITY_TYPE = {
    "BGBalance": "continuous",
    "BGBalanceDifference": "continuous",
    "BGBalancePostmonthly": "continuous",
    "BGBalanceCumulative": "cumulative",
    "BGBalanceCumulativeExternal": "cumulative",
    "OverallNetworkStatus": "cumulative",
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _get_nested(data: dict[str, Any], keys: list[str]) -> Any:
    # Walks a chain of dict keys, returning None as soon as any step is
    # missing or the current value isn't a dict.
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _escape(value: str) -> str:
    # RFC 6901 JSON Pointer escaping ("~" and "/" are special characters).
    return value.replace("~", "~0").replace("/", "~1")


def _child_path(path: str, key: str) -> str:
    return f"{path}/{_escape(key)}"


def _nested_path(base: str, keys: list[str]) -> str:
    for key in keys:
        base = _child_path(base, key)
    return base


def _missing_array_message(context_parts: list[str]) -> str:
    # context_parts holds human-readable identifiers (e.g. account or
    # quantityType) collected while walking into nested arrays, so the
    # error message can point at *which* array element is affected.
    context = f" ({', '.join(context_parts)})" if context_parts else ""
    return f"Value is missing or not an array.{context}"


# ---------------------------------------------------------------------------
# Generic time-series validation
# ---------------------------------------------------------------------------

def validate_time_series_generic(
    instance_data: Any,
    config: TimeSeriesConfig,
) -> tuple[Violation, ...]:
    if not isinstance(instance_data, dict):
        return ()

    root_data = instance_data.get(config.root_key)

    if not isinstance(root_data, dict):
        return ()

    period = _get_nested(root_data, config.period_path)
    granularity = _get_nested(root_data, config.granularity_path)

    errors: list[Violation] = []

    root_path = _child_path("", config.root_key)
    period_path = _nested_path(root_path, config.period_path)
    granularity_path = _nested_path(
        root_path,
        config.granularity_path,
    )

    def traverse(
        node: Any,
        segments: list[PathSegment],
        path: str,
        context_parts: list[str],
        current_period: Any,
        current_granularity: Any,
        current_period_path: str,
        current_granularity_path: str,
    ) -> None:
        # Recursively walks config.segments. DictSegment steps into a fixed
        # field; ListSegment steps into each element of an array (tracking
        # context for error messages, and optionally overriding the
        # period/granularity in effect for that branch); TimeSeriesEndpoint
        # is the terminal array of timestamped values, which is handed off
        # to validate_time_series_semantics together with whichever
        # period/granularity applied along this path.
        if not segments:
            return

        segment, rest = segments[0], segments[1:]

        if isinstance(segment, DictSegment):
            if not isinstance(node, dict):
                return

            traverse(
                node.get(segment.key),
                rest,
                _child_path(path, segment.key),
                context_parts,
                current_period,
                current_granularity,
                current_period_path,
                current_granularity_path,
            )
            return

        if isinstance(segment, ListSegment):
            if not isinstance(node, dict):
                return

            items = node.get(segment.key)
            items_path = _child_path(path, segment.key)

            if not isinstance(items, list):
                errors.append(
                    Violation(
                        "missing_array",
                        items_path,
                        _missing_array_message(context_parts),
                    )
                )
                return

            for index, item in enumerate(items):
                item_path = f"{items_path}/{index}"
                item_context = list(context_parts)

                if segment.context_key and isinstance(item, dict):
                    value = item.get(
                        segment.context_key,
                        f"#{index}",
                    )
                    label = segment.context_label or segment.context_key
                    item_context.append(f"{label}={value!r}")

                item_period = current_period
                item_period_path = current_period_path
                item_granularity = current_granularity
                item_granularity_path = current_granularity_path

                # Some families (e.g. QUANTITYDECLARATIONRESPONSE) carry
                # their own period/granularity per list item instead of
                # inheriting it from the message root; override here when so.
                if segment.period_key and isinstance(item, dict):
                    item_period = item.get(segment.period_key)
                    item_period_path = _child_path(
                        item_path,
                        segment.period_key,
                    )

                if segment.granularity_key and isinstance(item, dict):
                    item_granularity = item.get(
                        segment.granularity_key
                    )
                    item_granularity_path = _child_path(
                        item_path,
                        segment.granularity_key,
                    )

                traverse(
                    item,
                    rest,
                    item_path,
                    item_context,
                    item_period,
                    item_granularity,
                    item_period_path,
                    item_granularity_path,
                )

            return

        if isinstance(segment, TimeSeriesEndpoint):
            if not isinstance(node, dict):
                return

            values = node.get(segment.key)
            values_path = _child_path(path, segment.key)

            if not isinstance(values, list):
                errors.append(
                    Violation(
                        "missing_array",
                        values_path,
                        _missing_array_message(context_parts),
                    )
                )
                return

            errors.extend(
                validate_time_series_semantics(
                    period=current_period,
                    granularity=current_granularity,
                    time_series=values,
                    values_path=values_path,
                    period_path=current_period_path,
                    granularity_path=current_granularity_path,
                    timestamp_field=segment.timestamp_field,
                )
            )

    traverse(
        root_data,
        config.segments,
        root_path,
        [],
        period,
        granularity,
        period_path,
        granularity_path,
    )

    return tuple(errors)


# ---------------------------------------------------------------------------
# Balancing validation
# ---------------------------------------------------------------------------

def validate_balancing_generic(
    instance_data: Any,
    config: BalancingConfig,
    message_sub_type: str | None = None,
) -> tuple[Violation, ...]:
    """
    Validate BALANCING periods by quantityType.

    Current structure:

    balancingData.balancingFigures[].quantityType
    balancingData.balancingFigures[].values[]

    The message subtype is not used to select the period rule.
    The period rule is selected from the quantityType.
    """

    if not isinstance(instance_data, dict):
        return ()

    root_data = instance_data.get(config.root_key)

    if not isinstance(root_data, dict):
        return ()

    balancing_numbers = root_data.get(config.numbers_key)

    balancing_numbers_path = _nested_path(
        _child_path("", config.root_key),
        [config.numbers_key],
    )

    if not isinstance(balancing_numbers, list):
        return (
            Violation(
                "missing_array",
                balancing_numbers_path,
                "Balancing numbers are missing or not an array.",
            ),
        )

    errors: list[Violation] = []

    for index, balancing_number in enumerate(balancing_numbers):
        number_path = f"{balancing_numbers_path}/{index}"

        if not isinstance(balancing_number, dict):
            continue

        quantity_type = balancing_number.get(
            config.quantity_type_field
        )

        quantity_type_path = _child_path(
            number_path,
            config.quantity_type_field,
        )

        # Each balancingFigures entry picks its own period rule based on its
        # quantityType (see _BALANCING_RULE_BY_QUANTITY_TYPE above).
        rule = _BALANCING_RULE_BY_QUANTITY_TYPE.get(quantity_type)

        if rule is None:
            errors.append(
                Violation(
                    "unsupported_balancing_quantity_type",
                    quantity_type_path,
                    f"Unsupported balancing quantity type: "
                    f"{quantity_type!r}.",
                )
            )
            continue

        values = balancing_number.get(config.values_key)
        values_path = _child_path(
            number_path,
            config.values_key,
        )

        if not isinstance(values, list):
            errors.append(
                Violation(
                    "missing_array",
                    values_path,
                    "Balancing values are missing or not an array.",
                )
            )
            continue

        errors.extend(
            validate_period_semantics(
                period_type=rule,
                periods=values,
                periods_path=values_path,
                start_field=config.start_field,
                end_field=config.end_field,
                status_field=config.status_field,
            )
        )

    return tuple(errors)


# ---------------------------------------------------------------------------
# Legacy period validation
# ---------------------------------------------------------------------------

def validate_period_generic(
    instance_data: Any,
    config: PeriodConfig,
    message_sub_type: str,
) -> tuple[Violation, ...]:
    # Not currently reachable from _CONFIG_BY_TYPE (no entry there maps to a
    # PeriodConfig); kept for message families that describe their periods
    # directly rather than through the BALANCING quantityType structure.
    if not isinstance(instance_data, dict):
        return ()

    root_data = instance_data.get(config.root_key)

    if not isinstance(root_data, dict):
        return ()

    periods = _get_nested(root_data, config.periods_path)

    periods_path = _nested_path(
        _child_path("", config.root_key),
        config.periods_path,
    )

    if not isinstance(periods, list):
        return (
            Violation(
                "missing_array",
                periods_path,
                "Period values are missing or not an array.",
            ),
        )

    # Reuses the balancing quantityType -> rule mapping, keyed here by
    # message_sub_type instead.
    period_type = _BALANCING_RULE_BY_QUANTITY_TYPE.get(
        message_sub_type
    )

    if period_type is None:
        raise ValueError(
            "Unsupported balancing semantic profile: "
            f"{message_sub_type}"
        )

    return validate_period_semantics(
        period_type=period_type,
        periods=periods,
        periods_path=periods_path,
        start_field=config.start_field,
        end_field=config.end_field,
        status_field=config.status_field,
    )


# ---------------------------------------------------------------------------
# Validator API
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SemanticValidator:
    """Apply configured semantic rules for one message identity."""

    message_type: str
    message_sub_type: str

    def __post_init__(self) -> None:
        # Fail fast at construction time for an unsupported BALANCING
        # sub-type, rather than only failing later inside validate().
        if self.message_type != "BALANCING":
            return

        supported_subtypes = _BALANCING_MESSAGE_SUBTYPES

        if self.message_sub_type not in supported_subtypes:
            raise ValueError(
                "Unsupported balancing semantic profile: "
                f"{self.message_sub_type}"
            )

    def validate(
        self,
        payload: dict[str, Any],
    ) -> tuple[Violation, ...]:
        # Dispatches to the right check based on which config type is
        # registered for this message_type (time series, balancing, or
        # legacy period-based).
        config = _CONFIG_BY_TYPE.get(self.message_type)

        if config is None:
            return ()

        if isinstance(config, BalancingConfig):
            return validate_balancing_generic(
                payload,
                config,
                self.message_sub_type,
            )

        if isinstance(config, PeriodConfig):
            return validate_period_generic(
                payload,
                config,
                self.message_sub_type,
            )

        return validate_time_series_generic(payload, config)


def semantic_validator_for(
    message_type: str,
    message_sub_type: str,
) -> SemanticValidator:
    """Construct a reusable validator for a message identity."""

    return SemanticValidator(
        message_type,
        message_sub_type,
    )


def validate_semantics(
    payload: dict[str, Any],
    message_type: str,
    message_sub_type: str,
) -> tuple[Violation, ...]:
    """Validate a schema-valid payload and return structured violations."""

    if not isinstance(payload, dict):
        return ()

    return semantic_validator_for(
        message_type,
        message_sub_type,
    ).validate(payload)


def validate_repository_semantics(
    root: Path,
    instance_path: Path,
    payload: object,
) -> list[str]:
    """Format structured violations for repository validation."""

    if not isinstance(payload, dict):
        return []

    header = payload.get("message")

    if not isinstance(header, dict):
        return []

    message_type = header.get("type")
    message_sub_type = header.get("subType")

    if not isinstance(message_type, str):
        return []

    if not isinstance(message_sub_type, str):
        return []

    prefix = rel(instance_path, root)

    return [
        f"{prefix} at {violation.path}: {violation.message}"
        for violation in validate_semantics(
            payload,
            message_type,
            message_sub_type,
        )
    ]
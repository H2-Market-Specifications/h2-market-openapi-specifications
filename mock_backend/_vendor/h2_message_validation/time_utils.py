"""
Everything time-related: parsing ISO8601/RFC3339, validating time series for gaps, ordering and expected entry count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Matches ISO8601 durations of the form P[nD][T[nH][nM][nS]], e.g. "P1D",
# "PT1H30M". Calendar units (years/months) are intentionally not supported.
_DURATION_PATTERN = re.compile(
    r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?"
)

# Used to convert a calendar-day duration (e.g. "P1D") into an actual elapsed
# time, since a calendar day in Europe/Berlin can be 23, 24 or 25 hours long
# around DST transitions (see _german_calendar_duration).
try:
    _GERMAN_TIME_ZONE: ZoneInfo | None = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:
    _GERMAN_TIME_ZONE = None


@dataclass(frozen=True, slots=True)
class Violation:
    """One runtime-neutral semantic validation error."""

    code: str
    path: str
    message: str


def parse_json_datetime(value: Any) -> datetime | None:
    """
    Parses an RFC3339/ISO8601 date-time string.

    The timestamp must include timezone information.

    Args:
        value:
            Input value.

    Returns:
        Parsed datetime object or None if invalid.
    """
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None

    return parsed


def parse_iso8601_time_duration(value: Any) -> timedelta | None:
    """
    Parses an ISO8601 duration string.

    Supported examples:

    - PT15M
    - PT1H
    - P1D
    - P1DT2H

    Args:
        value:
            Duration string.

    Returns:
        Parsed timedelta or None if invalid.
    """
    if not isinstance(value, str) or not value.startswith("P"):
        return None

    # Format: P[nD][T[nH][nM][nS]]
    match = _DURATION_PATTERN.fullmatch(value)
    if not match or not any(match.groups()):
        return None

    days    = int(match.group(1) or 0)
    hours   = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)

    result = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    if result <= timedelta(0):
        return None

    return result


def _german_calendar_duration(
    start: datetime,
    duration: timedelta,
) -> timedelta | None:
    """Return the elapsed time for a duration applied in German local time."""
    # A "P1D" duration is a calendar day, not a fixed 24h span: applying it
    # in local (Europe/Berlin) time and converting back to UTC naturally
    # yields 23h/25h on DST transition days instead of always 24h.
    if _GERMAN_TIME_ZONE is None:
        return None

    try:
        start_utc = start.astimezone(timezone.utc)
        local_start = start_utc.astimezone(_GERMAN_TIME_ZONE)
        local_end = local_start + duration
        end_utc = local_end.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None

    elapsed = end_utc - start_utc
    return elapsed if elapsed > timedelta(0) else None


def parse_iso8601_interval_duration(start: datetime, end_or_duration: Any) -> timedelta | None:
    """
    Computes the duration of an ISO8601 interval.

    Supports either:

    - start + duration
    - start + end timestamp

    Args:
        start:
            Interval start time.

        end_or_duration:
            End timestamp or duration string.

    Returns:
        Interval duration or None if invalid.
    """
    duration = parse_iso8601_time_duration(end_or_duration)
    if duration is not None:
        # Only a duration that includes a day component ("P1D", not "PT24H")
        # is treated as a calendar day and resolved through German local
        # time; pure time-of-day durations are fixed-length as-is.
        duration_match = (
            _DURATION_PATTERN.fullmatch(end_or_duration)
            if isinstance(end_or_duration, str)
            else None
        )
        calendar_days = int(duration_match.group(1) or 0) if duration_match else 0
        if calendar_days:
            return _german_calendar_duration(start, duration)
        return duration

    end = parse_json_datetime(end_or_duration)
    if end is None:
        return None

    duration = end - start
    if duration <= timedelta(0):
        return None

    return duration


def validate_time_series_semantics(
    *,
    period: str,
    granularity: str,
    time_series: list,
    values_path: str,
    period_path: str,
    granularity_path: str,
    timestamp_field: str,
) -> tuple[Violation, ...]:
    """
    Validates the semantic consistency of a time series against period and granularity definitions.

    Checks:

    - period syntax
    - granularity syntax
    - period divisibility
    - expected entry count
    - missing and unexpected timestamps
    - strictly ascending timestamps

    Returns:
        Structured semantic violations.
    """
    errors: list[Violation] = []

    if not isinstance(period, str) or "/" not in period:
        return (_invalid_period(period_path),)

    period_start_raw, period_end_or_duration_raw = period.split("/", 1)

    period_start = parse_json_datetime(period_start_raw)
    if period_start is None:
        return (_invalid_period(period_path),)

    period_duration = parse_iso8601_interval_duration(
        period_start,
        period_end_or_duration_raw,
    )
    if period_duration is None:
        return (_invalid_period(period_path),)

    step = parse_iso8601_time_duration(granularity)
    if step is None:
        return (
            Violation(
                "invalid_granularity",
                granularity_path,
                f"Granularity {granularity} must be a positive day/hour/minute/second duration. Path: {granularity_path}.",
            ),
        )

    if period_duration.total_seconds() % step.total_seconds() != 0:
        return (
            Violation(
                "period_not_divisible",
                period_path,
                f"Period is not exactly divisible by granularity. Path: {granularity_path}.",
            ),
        )

    expected_count = int(period_duration.total_seconds() // step.total_seconds())

    if len(time_series) != expected_count:
        errors.append(
            Violation(
                "time_series_length",
                values_path,
                f"Expected {expected_count} time-series values; received {len(time_series)}.",
            )
        )

    # --- Missing / unexpected timestamps ---
    # Build the expected grid as fixed-size UTC steps from period_start.
    # Note: this is always a uniform "step" grid, even when the underlying
    # period spans a DST transition handled via _german_calendar_duration
    # above (that logic only affects the *count*/total duration, not the
    # spacing between individual grid points).
    expected_timestamps = {
        period_start + step * i
        for i in range(expected_count)
    }

    actual_timestamps = set()
    for entry in time_series:
        if isinstance(entry, dict):
            ts = parse_json_datetime(entry.get(timestamp_field))
            if ts is not None:
                actual_timestamps.add(ts)

    missing = sorted(expected_timestamps - actual_timestamps)
    extra = sorted(actual_timestamps - expected_timestamps)

    # --- Strictly ascending ---
    previous_timestamp: datetime | None = None

    for index, entry in enumerate(time_series):
        if not isinstance(entry, dict):
            continue

        raw_timestamp = entry.get(timestamp_field)
        timestamp = parse_json_datetime(raw_timestamp)
        timestamp_path = f"{values_path}/{index}/{_escape(timestamp_field)}"

        if timestamp is None:
            errors.append(
                Violation(
                    "invalid_timestamp",
                    timestamp_path,
                    "Timestamp must be a timezone-aware date-time.",
                )
            )
            continue

        if previous_timestamp is not None and timestamp <= previous_timestamp:
            errors.append(
                Violation(
                    "timestamps_not_ascending",
                    timestamp_path,
                    "Timestamps must be strictly ascending.",
                )
            )

        previous_timestamp = timestamp

    for timestamp in missing:
        errors.append(
            Violation(
                "missing_timestamp",
                values_path,
                f"Expected timestamp {_instant(timestamp)} is missing.",
            )
        )

    for timestamp in extra:
        errors.append(
            Violation(
                "unexpected_timestamp",
                values_path,
                f"Timestamp {_instant(timestamp)} is outside the expected period grid.",
            )
        )

    return tuple(errors)


def validate_period_semantics(
    *,
    period_type: Any,
    periods: list,
    periods_path: str,
    start_field: str,
    end_field: str,
    status_field: str,
) -> tuple[Violation, ...]:
    """Validate explicitly bounded continuous or cumulative periods."""

    errors: list[Violation] = []
    parsed_periods: list[tuple[datetime, datetime] | None] = []

    # First pass: parse and sanity-check each period in isolation (valid
    # timestamps, end strictly after start). Entries that fail are recorded
    # as None so the cross-period checks below can simply skip them.
    for index, period in enumerate(periods):
        if not isinstance(period, dict):
            parsed_periods.append(None)
            continue

        start_raw = period.get(start_field)
        end_raw = period.get(end_field)
        start = parse_json_datetime(start_raw)
        end = parse_json_datetime(end_raw)
        start_path = f"{periods_path}/{index}/{_escape(start_field)}"
        end_path = f"{periods_path}/{index}/{_escape(end_field)}"

        if start is None:
            errors.append(
                Violation(
                    "invalid_period_start",
                    start_path,
                    "Period start must be a timezone-aware date-time.",
                )
            )
        if end is None:
            errors.append(
                Violation(
                    "invalid_period_end",
                    end_path,
                    "Period end must be a timezone-aware date-time.",
                )
            )
        if start is None or end is None:
            parsed_periods.append(None)
            continue
        if end <= start:
            errors.append(
                Violation(
                    "non_positive_period",
                    end_path,
                    "Period end must be later than period start.",
                )
            )
            parsed_periods.append(None)
            continue
        parsed_periods.append((start, end))

    # Second pass: cross-period checks, depending on the period_type.
    if period_type == "continuous":
        # Consecutive periods must exactly join up: no overlap, no gap.
        for index in range(1, len(parsed_periods)):
            previous = parsed_periods[index - 1]
            current = parsed_periods[index]
            if previous is None or current is None:
                continue
            previous_end = previous[1]
            current_start = current[0]
            if current_start != previous_end:
                relation = (
                    "overlaps the previous interval"
                    if current_start < previous_end
                    else "leaves a gap after the previous interval"
                )
                errors.append(
                    Violation(
                        (
                            "period_overlap"
                            if current_start < previous_end
                            else "period_gap"
                        ),
                        f"{periods_path}/{index}/{_escape(start_field)}",
                        f"Period {relation}.",
                    )
                )
    elif period_type == "cumulative":
        # All periods must share the first entry's start, and their ends
        # must strictly increase (each period covers strictly more).
        if not parsed_periods:
            return tuple(errors)

        first = parsed_periods[0]
        common_start = first[0] if first is not None else None
        previous_end = first[1] if first is not None else None
        for index in range(1, len(parsed_periods)):
            current = parsed_periods[index]
            if current is None:
                continue
            start, end = current
            if common_start is not None and start != common_start:
                errors.append(
                    Violation(
                        "cumulative_start_mismatch",
                        f"{periods_path}/{index}/{_escape(start_field)}",
                        "Cumulative periods must share the same start.",
                    )
                )
            if previous_end is not None and end <= previous_end:
                errors.append(
                    Violation(
                        "cumulative_end_not_increasing",
                        f"{periods_path}/{index}/{_escape(end_field)}",
                        "Cumulative period ends must be strictly increasing.",
                    )
                )
            previous_end = end
    return tuple(errors)


def _invalid_period(path: str) -> Violation:
    return Violation(
        "invalid_period",
        path,
        "Period must be a valid timezone-aware interval with a positive duration.",
    )


def _escape(value: str) -> str:
    # RFC 6901 JSON Pointer escaping ("~" and "/" are special characters).
    return value.replace("~", "~0").replace("/", "~1")


def _instant(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
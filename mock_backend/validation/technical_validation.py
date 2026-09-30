from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ErrorDetail, H2ApiError
from ..transaction import effective_transaction_id
from .schema_validation import human_message, schema_validator


@dataclass(frozen=True)
class HeaderParameter:
    name: str
    required: bool
    schema: dict[str, Any]


class GatewayRequestValidator:
    """Validate the header contract declared by one OpenAPI operation."""

    def __init__(self, parameters: tuple[HeaderParameter, ...]) -> None:
        self.parameters = parameters
        self._validators = tuple(schema_validator(item.schema) for item in parameters)

    def validate(self, headers: Mapping[str, str]) -> str | None:
        """Validate the request headers and return the effective transaction ID of the request."""
        errors: list[ErrorDetail] = []

        for parameter, validator in zip(self.parameters, self._validators, strict=True):
            value = headers.get(parameter.name.lower())
            if value is None:
                if parameter.required:
                    errors.append(ErrorDetail(f"/{parameter.name}", "Required header is missing."))
                continue

            messages = sorted(human_message(violation) for violation in validator.iter_errors(value))
            errors.extend(ErrorDetail(f"/{parameter.name}", message) for message in messages)

        transaction_id = effective_transaction_id(headers)
        if errors:
            raise H2ApiError.bad_request(
                "One or more request headers violate the OpenAPI contract.",
                transaction_id=transaction_id,
                errors=tuple(errors),
            )
        return transaction_id

    @staticmethod
    def validate_content_type(content_type: str | None, *, transaction_id: str | None = None) -> None:
        media_type = (content_type or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise H2ApiError.unsupported_media_type(transaction_id=transaction_id)

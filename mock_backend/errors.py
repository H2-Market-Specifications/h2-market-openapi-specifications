from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ErrorDetail:
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


@dataclass(frozen=True)
class ErrorResponse:
    title: str
    # HTTP status code of the response; not part of the body.
    status: int
    code: str
    response_origin: str
    detail: str | None = None
    # Effective transaction ID of the rejected request. Without it, the response is sent without a body.
    transaction_id: str | None = None
    errors: tuple[ErrorDetail, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "title": self.title,
            "transactionId": self.transaction_id,
            "responseOrigin": self.response_origin,
        }
        if self.detail:
            result["detail"] = self.detail
        if self.errors:
            result["errors"] = [error.to_dict() for error in self.errors]
        return result


class H2ApiError(Exception):
    def __init__(self, error_response: ErrorResponse) -> None:
        super().__init__(error_response.detail or error_response.title)
        self.error_response = error_response

    @classmethod
    def bad_request(
        cls,
        detail: str,
        *,
        transaction_id: str | None = None,
        errors: tuple[ErrorDetail, ...] = (),
    ) -> H2ApiError:
        return cls(ErrorResponse("Invalid request", 400, "BAD_REQUEST", "Gateway", detail, transaction_id, errors))

    @classmethod
    def route_not_found(cls, *, transaction_id: str | None = None) -> H2ApiError:
        return cls(
            ErrorResponse(
                "Route not found",
                404,
                "ROUTE_NOT_FOUND",
                "Gateway",
                "The requested technical route or endpoint is unknown.",
                transaction_id,
            )
        )

    @classmethod
    def method_not_allowed(cls, *, transaction_id: str | None = None) -> H2ApiError:
        return cls(
            ErrorResponse(
                "Method not allowed",
                405,
                "METHOD_NOT_ALLOWED",
                "Gateway",
                "The HTTP method is not allowed for this endpoint.",
                transaction_id,
            )
        )

    @classmethod
    def unsupported_media_type(cls, *, transaction_id: str | None = None) -> H2ApiError:
        return cls(
            ErrorResponse(
                "Unsupported media type",
                415,
                "UNSUPPORTED_MEDIA_TYPE",
                "Gateway",
                "The request Content-Type must be application/json.",
                transaction_id,
            )
        )

    @classmethod
    def validation_failed(
        cls,
        errors: tuple[ErrorDetail, ...],
        *,
        transaction_id: str | None = None,
    ) -> H2ApiError:
        return cls(
            ErrorResponse(
                "Validation failed",
                422,
                "MESSAGE_VALIDATION_FAILED",
                "TargetSystem",
                "The message violates its JSON Schema.",
                transaction_id,
                errors,
            )
        )

    @classmethod
    def semantic_failed(
        cls,
        errors: tuple[ErrorDetail, ...],
        *,
        transaction_id: str | None = None,
    ) -> H2ApiError:
        return cls(
            ErrorResponse(
                "Validation failed",
                422,
                "MESSAGE_VALIDATION_FAILED",
                "TargetSystem",
                "The message violates semantic period or time-series rules.",
                transaction_id,
                errors,
            )
        )

    @classmethod
    def internal_error(cls, *, transaction_id: str | None = None) -> H2ApiError:
        return cls(
            ErrorResponse(
                "Internal error",
                500,
                "INTERNAL_ERROR",
                "Gateway",
                "An unexpected error occurred while processing the request.",
                transaction_id,
            )
        )

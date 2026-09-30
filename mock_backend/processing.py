from __future__ import annotations

from json import JSONDecodeError, loads
from typing import Any, NoReturn

from fastapi import Request
from fastapi.responses import JSONResponse

from .errors import ErrorDetail, H2ApiError
from .message_configuration import MessageConfiguration
from .transaction import new_transaction_id


def response_headers(*, api_version: str | None, response_origin: str, reference_id: str | None) -> dict[str, str]:
    """Return the H2 headers of a synchronous response.

    A response with a JSON body answers a request with a valid effective transaction ID (reference_id); it gets a newly
    generated H2-Transaction-Id and refers to the request in H2-Reference-Id. A response without a body carries neither.
    """
    headers = {"H2-Response-Origin": response_origin}
    if api_version:
        headers["H2-API-Version"] = api_version
    if reference_id:
        headers["H2-Transaction-Id"] = new_transaction_id()
        headers["H2-Reference-Id"] = reference_id
    return headers


async def process_submission(request: Request, configuration: MessageConfiguration, transaction_id: str) -> JSONResponse:
    """Validate and answer a submission whose headers were already accepted; transaction_id is its effective ID."""
    try:
        payload: Any = loads(await request.body(), parse_constant=_reject_json_constant)
    except (JSONDecodeError, UnicodeDecodeError):
        raise H2ApiError.bad_request(
            "The request body is not valid JSON.",
            transaction_id=transaction_id,
            errors=(ErrorDetail("/", "The request body is not syntactically valid JSON."),),
        ) from None

    configuration.message_validator.validate(payload, transaction_id=transaction_id)
    semantic_errors = configuration.semantic_validator.validate(payload)
    if semantic_errors:
        raise H2ApiError.semantic_failed(semantic_errors, transaction_id=transaction_id)
    document_number = _document_number(payload)
    content: dict[str, Any] = {
        "status": "ok",
        "responseOrigin": "TargetSystem",
        "messageType": configuration.message_type,
        "messageSubType": configuration.message_sub_type,
        "messageVersion": configuration.message_version,
    }
    if document_number:
        content["referenceNumber"] = document_number
    headers = response_headers(
        api_version=configuration.api_version,
        response_origin="TargetSystem",
        reference_id=transaction_id,
    )
    return JSONResponse(status_code=200, content=content, headers=headers)


def _reject_json_constant(value: str) -> NoReturn:
    raise JSONDecodeError("Nonstandard numeric constant is not valid JSON", value, 0)


def _document_number(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), dict):
        return None
    value = payload["message"].get("documentNumber")
    return value if isinstance(value, str) and value else None

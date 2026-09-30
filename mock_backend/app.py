from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from scalar_fastapi import AgentScalarConfig, OpenAPISource, get_scalar_api_reference
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import H2ApiError
from .message_configuration import MESSAGE_CONFIGURATIONS, MessageConfiguration
from .processing import process_submission, response_headers
from .transaction import effective_transaction_id

# Pinned: since Scalar 1.69.1 the request body editor in the "Test Request" modal only renders the lines visible
# when it opens. Check that long request bodies are shown completely before upgrading.
SCALAR_JS_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.69.0"
SWAGGER_UI_URL = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5"


def create_app() -> FastAPI:
    api = FastAPI(title="H2 market message mock", docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["H2-Transaction-Id", "H2-Reference-Id", "H2-API-Version", "H2-Response-Origin", "Allow"],
    )
    api.add_exception_handler(H2ApiError, handle_h2_error)
    api.add_exception_handler(StarletteHTTPException, handle_http_exception)
    api.add_exception_handler(Exception, handle_unexpected_error)

    for configuration in MESSAGE_CONFIGURATIONS:
        api.post(configuration.submission_path, include_in_schema=False)(create_submission_handler(configuration))
        api.get(configuration.openapi_serving_path, include_in_schema=False)(create_openapi_handler(configuration))

    api.get("/scalar", include_in_schema=False)(scalar_reference)
    api.get("/swagger", include_in_schema=False)(swagger_reference)
    return api


async def handle_h2_error(request: Request, error: H2ApiError) -> Response:
    return _error_response(request, error)


async def handle_http_exception(request: Request, error: StarletteHTTPException) -> Response:
    # Unknown routes and wrong methods are raised by the router before any handler validates the headers.
    transaction_id = effective_transaction_id(request.headers)
    if error.status_code == 404:
        return _error_response(request, H2ApiError.route_not_found(transaction_id=transaction_id))
    if error.status_code == 405:
        allow = {"Allow": (error.headers or {}).get("Allow", "")}
        return _error_response(request, H2ApiError.method_not_allowed(transaction_id=transaction_id), allow)
    return await http_exception_handler(request, error)


async def handle_unexpected_error(request: Request, _error: Exception) -> Response:
    return _error_response(request, H2ApiError.internal_error(transaction_id=effective_transaction_id(request.headers)))


def _error_response(request: Request, error: H2ApiError, extra_headers: dict[str, str] | None = None) -> Response:
    error_response = error.error_response
    headers = {
        **response_headers(
            api_version=_api_version_for(request.url.path),
            response_origin=error_response.response_origin,
            reference_id=error_response.transaction_id,
        ),
        **(extra_headers or {}),
    }
    # Without a syntactically valid H2-Transaction-Id there is no transactionId, so the response has no body.
    if not error_response.transaction_id:
        return Response(status_code=error_response.status, headers=headers)
    return JSONResponse(status_code=error_response.status, content=error_response.to_dict(), headers=headers)


def _api_version_for(path: str) -> str | None:
    for configuration in MESSAGE_CONFIGURATIONS:
        if configuration.submission_path == path:
            return configuration.api_version
    # A path that no API owns is answered with the version shared by all APIs, if there is exactly one.
    versions = {configuration.api_version for configuration in MESSAGE_CONFIGURATIONS}
    return versions.pop() if len(versions) == 1 else None


def create_submission_handler(
    configuration: MessageConfiguration,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def submit(request: Request) -> JSONResponse:
        transaction_id = configuration.gateway_validator.validate(request.headers)
        configuration.gateway_validator.validate_content_type(
            request.headers.get("content-type"),
            transaction_id=transaction_id,
        )
        # H2-Transaction-Id is a required header, so a request that passed the header validation has a transaction ID.
        assert transaction_id is not None
        return await process_submission(request, configuration, transaction_id)

    submit.__name__ = f"submit_{configuration.message_id.replace('-', '_')}"
    return submit


def create_openapi_handler(
    configuration: MessageConfiguration,
) -> Callable[[], Awaitable[FileResponse]]:
    async def serve_openapi() -> FileResponse:
        return FileResponse(configuration.openapi_path, media_type="application/yaml")

    serve_openapi.__name__ = f"openapi_{configuration.message_id.replace('-', '_')}"
    return serve_openapi


async def scalar_reference() -> Any:
    return get_scalar_api_reference(
        sources=[
            OpenAPISource(
                title=configuration.scalar_title,
                url=configuration.openapi_serving_path,
                default=index == 0,
            )
            for index, configuration in enumerate(MESSAGE_CONFIGURATIONS)
        ],
        title="H2 market message mock",
        agent=AgentScalarConfig(disabled=True),
        scalar_js_url=SCALAR_JS_URL,
    )


async def swagger_reference() -> HTMLResponse:
    # FastAPI's get_swagger_ui_html uses BaseLayout without the standalone preset, which ignores "urls".
    # The spec dropdown needs StandaloneLayout, so the page is rendered here.
    config = json.dumps(
        {
            "dom_id": "#swagger-ui",
            "urls": [
                {"url": configuration.openapi_serving_path, "name": configuration.scalar_title}
                for configuration in MESSAGE_CONFIGURATIONS
            ],
            "urls.primaryName": MESSAGE_CONFIGURATIONS[0].scalar_title,
            "deepLinking": True,
            "showExtensions": True,
            "showCommonExtensions": True,
            "layout": "StandaloneLayout",
        }
    )
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>H2 market message mock</title>
<link rel="stylesheet" href="{SWAGGER_UI_URL}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{SWAGGER_UI_URL}/swagger-ui-bundle.js"></script>
<script src="{SWAGGER_UI_URL}/swagger-ui-standalone-preset.js"></script>
<script>
window.ui = SwaggerUIBundle({{
    ...{config},
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    plugins: [SwaggerUIBundle.plugins.DownloadUrl],
}});
</script>
</body>
</html>
"""
    )


app = create_app()

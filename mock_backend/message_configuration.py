from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .validation.schema_validation import MessageValidator
from .validation.semantic_validation import SemanticValidator, semantic_validator_for
from .validation.technical_validation import GatewayRequestValidator, HeaderParameter

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_OPENAPI_ROOT = REPOSITORY_ROOT / "dist/messages"
PACKAGED_OPENAPI_ROOT = Path(__file__).resolve().parent / "_openapi"
API_VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


@dataclass(frozen=True)
class MessageConfiguration:
    message_id: str
    message_type: str
    message_sub_type: str
    message_version: str
    # Fully qualified API version (info.version), returned in the H2-API-Version response header.
    api_version: str
    submission_path: str
    openapi_path: Path
    openapi_serving_path: str
    scalar_title: str
    gateway_validator: GatewayRequestValidator
    message_validator: MessageValidator
    semantic_validator: SemanticValidator


def _resolve(document: dict[str, Any], value: Any) -> Any:
    while isinstance(value, dict) and set(value) == {"$ref"}:
        reference = value["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ValueError(f"Only local OpenAPI references are supported: {reference!r}")
        value = document
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _load_configuration(path: Path) -> MessageConfiguration:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"OpenAPI document must be an object: {path}")

    metadata = document.get("x-h2-message")
    info = document.get("info")
    if not isinstance(metadata, dict) or not isinstance(info, dict):
        raise ValueError(f"Missing info or x-h2-message metadata: {path}")

    posts = [(route, item["post"]) for route, item in document.get("paths", {}).items() if isinstance(item, dict) and "post" in item]
    if len(posts) != 1:
        raise ValueError(f"Expected exactly one POST operation in {path}, found {len(posts)}")
    submission_path, operation = posts[0]

    parameters: list[HeaderParameter] = []
    for raw_parameter in operation.get("parameters", []):
        parameter = _resolve(document, raw_parameter)
        if not isinstance(parameter, dict) or parameter.get("in") != "header":
            continue
        schema = parameter.get("schema")
        if not isinstance(schema, dict) or not isinstance(parameter.get("name"), str):
            raise ValueError(f"Malformed header parameter in {path}: {parameter!r}")
        parameters.append(
            HeaderParameter(
                name=parameter["name"],
                required=parameter.get("required") is True,
                schema=schema,
            )
        )

    parameter_names = [item.name.lower() for item in parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise ValueError(f"Duplicate header parameter in {path}")

    process_parameters = [item for item in parameters if item.name.lower() == "h2-business-process"]
    if len(process_parameters) != 1:
        raise ValueError(f"Expected one H2-Business-Process parameter in {path}")
    process_values = process_parameters[0].schema.get("enum")
    if not isinstance(process_values, list) or len(process_values) != 1 or not isinstance(process_values[0], str):
        raise ValueError(f"H2-Business-Process must define one enum value in {path}")

    request_body = _resolve(document, operation.get("requestBody"))
    try:
        schema = request_body["content"]["application/json"]["schema"]
        schema = _resolve(document, schema)
    except (KeyError, TypeError) as error:
        raise ValueError(f"Missing application/json request schema in {path}") from error
    if not isinstance(schema, dict):
        raise ValueError(f"Request schema must be an object in {path}")

    required_metadata = ("id", "businessName", "type", "subType", "messageVersion")
    if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in required_metadata):
        raise ValueError(f"Incomplete x-h2-message metadata in {path}")
    title = info.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError(f"Missing OpenAPI info.title in {path}")
    api_version = info.get("version")
    if not isinstance(api_version, str) or not API_VERSION_PATTERN.fullmatch(api_version):
        raise ValueError(f"OpenAPI info.version must have the form <MAJOR>.<MINOR>.<PATCH> in {path}")

    message_id = metadata["id"]
    return MessageConfiguration(
        message_id=message_id,
        message_type=metadata["type"],
        message_sub_type=metadata["subType"],
        message_version=metadata["messageVersion"],
        api_version=api_version,
        submission_path=submission_path,
        openapi_path=path,
        openapi_serving_path=f"/openapi/{message_id}.yaml",
        scalar_title=f"{metadata['type']} / {metadata['subType']}",
        gateway_validator=GatewayRequestValidator(tuple(parameters)),
        message_validator=MessageValidator(schema),
        semantic_validator=semantic_validator_for(metadata["type"], metadata["subType"]),
    )


def discover_message_configurations(root: Path | None = None) -> tuple[MessageConfiguration, ...]:
    openapi_root = root or (REPOSITORY_OPENAPI_ROOT if REPOSITORY_OPENAPI_ROOT.is_dir() else PACKAGED_OPENAPI_ROOT)
    paths = sorted(openapi_root.glob("*/v*/*/openapi.yaml"))
    configurations = tuple(_load_configuration(path) for path in paths)
    if not configurations:
        raise ValueError(f"No committed OpenAPI documents found below {openapi_root}")

    for attribute in ("message_id", "submission_path", "openapi_serving_path"):
        values = [getattr(item, attribute) for item in configurations]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate message configuration {attribute}")
    return configurations


MESSAGE_CONFIGURATIONS = discover_message_configurations()

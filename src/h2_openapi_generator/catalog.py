from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    """Raised when the message catalog is missing required information."""


@dataclass(frozen=True)
class MessageSpec:
    id: str
    type: str
    subtype: str
    name: str
    version: str
    schema: str
    schema_id: str
    examples: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def version_dir(self) -> str:
        return f"v{self.version}"

    def template_path(self, templates_root: Path) -> Path:
        return templates_root / "messages" / self.type / self.version_dir / self.subtype / "openapi.template.yaml"

    def output_path(self, output_root: Path) -> Path:
        return output_root / "messages" / self.type / self.version_dir / self.subtype / "openapi.yaml"

    def schema_path(self, source_root: Path) -> Path:
        return (source_root / "catalog" / self.schema).resolve()

    def example_paths(self, source_root: Path) -> list[Path]:
        return [(source_root / "catalog" / item).resolve() for item in self.examples]


def load_catalog(source_root: Path) -> list[MessageSpec]:
    catalog_path = source_root / "catalog" / "message-catalog.json"
    if not catalog_path.exists():
        raise CatalogError(f"Catalog not found: {catalog_path}")

    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Catalog is not valid JSON: {catalog_path}: {exc}") from exc

    messages = data.get("messages")
    if not isinstance(messages, list):
        raise CatalogError("Catalog must contain a 'messages' array.")

    result: list[MessageSpec] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise CatalogError(f"Catalog message at index {index} must be an object.")
        message = item.get("message")
        missing = [field for field in ("id", "name", "schema", "schemaId") if not item.get(field)]
        if not isinstance(message, dict) or not message.get("version"):
            missing.append("message.version")
        if missing:
            raise CatalogError(f"Catalog message at index {index} is missing required fields: {', '.join(missing)}")
        examples = item.get("examples", [])
        if examples is None:
            examples = []
        if not isinstance(examples, list) or not all(isinstance(example, str) for example in examples):
            raise CatalogError(f"Catalog message {item['id']} has invalid examples; expected array of strings.")
        spec = MessageSpec(
            id=str(item["id"]),
            type=str(item["message"].get("type", "")).lower(),
            subtype=str(item["message"].get("subType", "")).lower(),
            name=str(item["name"]),
            version=str(message["version"]),
            schema=str(item["schema"]),
            schema_id=str(item["schemaId"]),
            examples=tuple(examples),
            raw=item,
        )
        if not spec.schema_path(source_root).exists():
            raise CatalogError(f"Schema for message {spec.id} not found: {spec.schema_path(source_root)}")
        result.append(spec)
    return result


def find_message(messages: list[MessageSpec], message_id: str, message_version: str) -> MessageSpec:
    for message in messages:
        if message.id == message_id and message.version == message_version:
            return message
    raise CatalogError(f"Message not found in catalog: id={message_id!r}, version={message_version!r}")

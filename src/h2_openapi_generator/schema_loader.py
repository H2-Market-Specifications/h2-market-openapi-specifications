from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag


class SchemaError(ValueError):
    """Raised when a JSON Schema cannot be loaded or referenced."""


@dataclass(frozen=True)
class SchemaBundle:
    root_schema: dict[str, Any]
    additional_components: dict[str, dict[str, Any]]


_COMPONENT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def component_name_from_text(value: str) -> str:
    cleaned = _COMPONENT_CHARS.sub(" ", value).strip()
    if not cleaned:
        return "Schema"
    words = re.split(r"[\s._-]+", cleaned)
    first = words[0]
    rest = "".join(word[:1].upper() + word[1:] for word in words[1:] if word)
    name = f"{first}{rest}"
    if not re.match(r"^[A-Za-z]", name):
        name = f"Schema{name}"
    return name


class SchemaLoader:
    def __init__(self, source_root: Path):
        self.source_root = source_root.resolve()
        self.id_to_path = self._scan_schema_ids()
        self.raw_by_path: dict[Path, dict[str, Any]] = {}
        self.path_to_component: dict[Path, str] = {}
        self.component_to_path: dict[str, Path] = {}

    def bundle(self, root_path: Path, root_component_name: str) -> SchemaBundle:
        root_path = root_path.resolve()
        self._register_schema(root_path, forced_name=root_component_name)
        rewritten: dict[str, dict[str, Any]] = {}
        for path in sorted(self.path_to_component, key=lambda item: self.path_to_component[item]):
            component_name = self.path_to_component[path]
            rewritten[component_name] = self._rewrite_schema(self.raw_by_path[path], base_path=path)
        root_schema = rewritten.pop(root_component_name)
        return SchemaBundle(root_schema=root_schema, additional_components=rewritten)

    def _scan_schema_ids(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        schemas_dir = self.source_root / "schemas"
        if not schemas_dir.exists():
            return result
        for path in sorted(schemas_dir.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise SchemaError(f"Invalid JSON schema file: {path}: {exc}") from exc
            schema_id = data.get("$id")
            if isinstance(schema_id, str):
                result[schema_id] = path.resolve()
        return result

    def _load_schema(self, path: Path) -> dict[str, Any]:
        path = path.resolve()
        if path in self.raw_by_path:
            return self.raw_by_path[path]
        if not path.exists():
            raise SchemaError(f"Referenced schema file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaError(f"Invalid JSON schema file: {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError(f"Schema file must contain a JSON object: {path}")
        self.raw_by_path[path] = data
        return data

    def _register_schema(self, path: Path, forced_name: str | None = None) -> None:
        path = path.resolve()
        if path in self.path_to_component:
            if forced_name and self.path_to_component[path] != forced_name:
                self._rename_component(path, forced_name)
            return

        raw = self._load_schema(path)
        preferred = forced_name or self._preferred_component_name(path, raw)
        component_name = self._unique_component_name(preferred, path)
        self.path_to_component[path] = component_name
        self.component_to_path[component_name] = path

        for ref in self._iter_refs(raw):
            target_path, _fragment = self._resolve_ref(ref, path)
            self._register_schema(target_path)

    def _rename_component(self, path: Path, forced_name: str) -> None:
        if forced_name in self.component_to_path and self.component_to_path[forced_name] != path:
            raise SchemaError(f"Cannot force component name {forced_name!r}; it is already used for {self.component_to_path[forced_name]}")
        old = self.path_to_component[path]
        del self.component_to_path[old]
        self.path_to_component[path] = forced_name
        self.component_to_path[forced_name] = path

    def _preferred_component_name(self, path: Path, raw: dict[str, Any]) -> str:
        title = raw.get("title")
        if isinstance(title, str) and title.strip():
            return component_name_from_text(title)
        stem = path.name.replace(".schema", "")
        return component_name_from_text(stem)

    def _unique_component_name(self, preferred: str, path: Path) -> str:
        if preferred not in self.component_to_path or self.component_to_path[preferred] == path:
            return preferred
        suffix_source = "_".join(path.with_suffix("").parts[-4:])
        candidate = component_name_from_text(f"{preferred}_{suffix_source}")
        base = candidate
        index = 2
        while candidate in self.component_to_path and self.component_to_path[candidate] != path:
            candidate = f"{base}{index}"
            index += 1
        return candidate

    def _iter_refs(self, node: Any) -> list[str]:
        refs: list[str] = []
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                refs.append(ref)
            for value in node.values():
                refs.extend(self._iter_refs(value))
        elif isinstance(node, list):
            for item in node:
                refs.extend(self._iter_refs(item))
        return refs

    def _resolve_ref(self, ref: str, base_path: Path) -> tuple[Path, str]:
        uri, fragment = urldefrag(ref)
        if not uri:
            return base_path.resolve(), fragment
        if uri in self.id_to_path:
            return self.id_to_path[uri], fragment
        if uri.startswith("https://h2-market.example/message-specifications/"):
            suffix = uri.removeprefix("https://h2-market.example/message-specifications/")
            candidate = (self.source_root / suffix).resolve()
            if candidate.exists():
                return candidate, fragment
        if "://" not in uri:
            candidate = (base_path.parent / uri).resolve()
            if candidate.exists():
                return candidate, fragment
        raise SchemaError(f"Unable to resolve schema $ref {ref!r} from {base_path}")

    def _rewrite_schema(self, schema: dict[str, Any], base_path: Path) -> dict[str, Any]:
        return self._rewrite_node(copy.deepcopy(schema), base_path)

    def _rewrite_node(self, node: Any, base_path: Path) -> Any:
        if isinstance(node, dict):
            rewritten: dict[str, Any] = {}
            for key, value in node.items():
                if key == "$schema":
                    continue
                if key == "$id" and isinstance(value, str):
                    rewritten["x-json-schema-id"] = value
                    continue
                if key == "$ref" and isinstance(value, str):
                    target_path, fragment = self._resolve_ref(value, base_path)
                    if target_path not in self.path_to_component:
                        self._register_schema(target_path)
                    component = self.path_to_component[target_path]
                    if fragment:
                        if fragment.startswith("/"):
                            rewritten["$ref"] = f"#/components/schemas/{component}{fragment}"
                        else:
                            raise SchemaError(f"Unsupported JSON Schema anchor reference {value!r} from {base_path}")
                    else:
                        rewritten["$ref"] = f"#/components/schemas/{component}"
                    continue
                rewritten[key] = self._rewrite_node(value, base_path)
            return rewritten
        if isinstance(node, list):
            return [self._rewrite_node(item, base_path) for item in node]
        return node

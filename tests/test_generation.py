from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from h2_openapi_generator.catalog import CatalogError, load_catalog
from h2_openapi_generator.openapi_writer import _normalize_schema, generate_all_messages
from h2_openapi_generator.validate import ValidationError, validate_all_outputs, validate_openapi_document
from h2_openapi_generator.validator_bundle import ValidatorBundleError, _without_credentials


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def make_source(root: Path) -> None:
    write_json(
        root / "catalog/message-catalog.json",
        {
            "messages": [
                {
                    "id": "alpha-message",
                    "name": "Alpha Message",
                    "message": {"type": "alpha", "version": "1.0", "subType": "test"},
                    "schema": "../schemas/messages/alpha/v1.0/test/alpha.schema.json",
                    "schemaId": "https://h2-market.example/message-specifications/schemas/messages/alpha/test/v1.0/alpha.schema.json",
                    "examples": ["../examples/messages/alpha/v1.0/test/ALPHA.valid.json"],
                },
                {
                    "id": "beta-message",
                    "name": "Beta Message",
                    "message": {"type": "beta", "version": "1.0", "subType": "test"},
                    "schema": "../schemas/messages/beta/v1.0/test/beta.schema.json",
                    "schemaId": "https://h2-market.example/message-specifications/schemas/messages/beta/test/v1.0/beta.schema.json",
                    "examples": ["../examples/messages/beta/v1.0/test/BETA.valid.json"],
                },
            ]
        },
    )
    write_json(
        root / "schemas/_shared/shared.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://h2-market.example/message-specifications/schemas/_shared/shared.schema.json",
            "title": "sharedBlock",
            "type": "object",
            "required": ["code"],
            "properties": {"code": {"type": "string", "examples": ["A"]}},
        },
    )
    for name in ("alpha-message", "beta-message"):
        short = name.split("-")[0]
        write_json(
            root / f"schemas/messages/{short}/v1.0/test/{short}.schema.json",
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"https://h2-market.example/message-specifications/schemas/messages/{short}/v1.0/test/{short}.schema.json",
                "title": f"{short}Payload",
                "type": "object",
                "required": ["shared"],
                "properties": {
                    "messageType": {"const": short},
                    "shared": {"$ref": "https://h2-market.example/message-specifications/schemas/_shared/shared.schema.json"},
                },
            },
        )
        write_json(
            root / f"examples/messages/{short}/v1.0/test/{short.upper()}.valid.json", {"messageType": short, "shared": {"code": "A"}}
        )


def make_templates(root: Path) -> None:
    for name in ("alpha-message", "beta-message"):
        component = f"{name.split('-')[0]}Payload"
        messagetype = name.split("-")[0]
        write_yaml(
            root / f"messages/{messagetype}/v1.0/test/openapi.template.yaml",
            {
                "openapi": "3.1.0",
                "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
                "info": {"title": name, "version": "1.0.0"},
                "paths": {
                    f"/api/v1/{name}s": {
                        "post": {
                            "parameters": [
                                {
                                    "name": "X-Test",
                                    "in": "header",
                                    "required": False,
                                    "schema": {"type": "string", "example": "A", "examples": ["A"]},
                                    "example": "A",
                                    "examples": {"default": {"value": "A"}},
                                }
                            ],
                            "requestBody": {
                                "required": True,
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": f"#/components/schemas/{component}"},
                                        "examples": {"CatalogExamples": {"x-h2-generator": {"inject": "all-valid-examples"}}},
                                    }
                                },
                            },
                            "responses": {"202": {"description": "accepted"}},
                        }
                    }
                },
                "components": {"schemas": {component: {"x-h2-generator": {"inject": "message-schema"}}}},
            },
        )


def test_catalog_loading(tmp_path: Path) -> None:
    make_source(tmp_path)
    messages = load_catalog(tmp_path)
    assert [message.id for message in messages] == ["alpha-message", "beta-message"]


def test_missing_schema_has_clear_error(tmp_path: Path) -> None:
    make_source(tmp_path)
    (tmp_path / "schemas/messages/alpha/v1.0/test/alpha.schema.json").unlink()
    try:
        load_catalog(tmp_path)
    except CatalogError as exc:
        assert "Schema for message alpha-message not found" in str(exc)
    else:
        raise AssertionError("Expected CatalogError for missing schema")


def test_generate_all_messages_creates_one_file_per_message(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output = tmp_path / "dist"
    make_source(source)
    make_templates(templates)

    outputs = generate_all_messages(source_root=source, templates_root=templates, output_root=output)

    assert len(outputs) == 2
    assert (output / "messages/alpha/v1.0/test/openapi.yaml").exists()
    assert (output / "messages/beta/v1.0/test/openapi.yaml").exists()
    for path in outputs:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        assert "x-h2-generator" not in text
        assert "jsonSchemaDialect" not in document
        assert "sharedBlock" not in document["components"].get("schemas", {})
        assert "#/components/schemas/sharedBlock" not in text
        assert "const:" not in text
        assert not _has_example_conflict(document)
        assert not _has_schema_examples(document)
        schema = document["components"]["schemas"]["alphaPayload" if "alpha" in str(path) else "betaPayload"]
        assert schema["properties"]["shared"]["properties"]["code"]["type"] == "string"
        assert schema["properties"]["messageType"]["enum"]
    validate_all_outputs(output)


def test_pruning_keeps_security_schemes_named_by_security_requirements(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output = tmp_path / "dist"
    make_source(source)
    make_templates(templates)
    template_path = templates / "messages/alpha/v1.0/test/openapi.template.yaml"
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    template["security"] = [{"mtls": []}]
    template["paths"]["/api/v1/alpha-messages"]["post"]["security"] = [{"apiKey": []}]
    template["components"]["securitySchemes"] = {
        "mtls": {"type": "mutualTLS"},
        "apiKey": {"type": "apiKey", "in": "header", "name": "X-Key"},
        "unused": {"type": "http", "scheme": "basic"},
    }
    write_yaml(template_path, template)

    generate_all_messages(source_root=source, templates_root=templates, output_root=output)

    document = yaml.safe_load((output / "messages/alpha/v1.0/test/openapi.yaml").read_text(encoding="utf-8"))
    assert set(document["components"]["securitySchemes"]) == {"mtls", "apiKey"}


def test_validation_rejects_security_requirements_without_scheme() -> None:
    document = {
        "openapi": "3.1.0",
        "info": {"title": "test", "version": "1.0.0"},
        "security": [{"mtls": []}],
        "paths": {},
        "components": {"schemas": {"payload": {"type": "object"}}},
    }

    with pytest.raises(ValidationError, match="undefined security schemes: mtls"):
        validate_openapi_document(document)


def test_keys_next_to_a_common_component_ref_refine_the_inlined_component(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output = tmp_path / "dist"
    make_source(source)
    make_templates(templates)
    common = tmp_path / "common.yaml"
    write_yaml(
        common,
        {
            "components": {
                "headers": {"Version": {"description": "Version.", "schema": {"type": "string"}}},
                "responses": {
                    "Accepted": {
                        "description": "accepted",
                        "headers": {
                            "X-Version": {"$ref": "#/components/headers/Version", "required": True},
                            "X-Optional-Version": {"$ref": "#/components/headers/Version"},
                        },
                    }
                },
            }
        },
    )
    template_path = templates / "messages/alpha/v1.0/test/openapi.template.yaml"
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    template["paths"]["/api/v1/alpha-messages"]["post"]["responses"]["202"] = {
        "x-h2-generator": {"inject": "common-component", "ref": "#/components/responses/Accepted"}
    }
    write_yaml(template_path, template)

    generate_all_messages(source_root=source, templates_root=templates, output_root=output, common_components_path=common)

    document = yaml.safe_load((output / "messages/alpha/v1.0/test/openapi.yaml").read_text(encoding="utf-8"))
    headers = document["paths"]["/api/v1/alpha-messages"]["post"]["responses"]["202"]["headers"]
    assert headers["X-Version"] == {"description": "Version.", "schema": {"type": "string"}, "required": True}
    assert headers["X-Optional-Version"] == {"description": "Version.", "schema": {"type": "string"}}


def test_committed_specifications_pass_validation() -> None:
    assert len(validate_all_outputs(Path(__file__).resolve().parents[1] / "dist")) == 16


def test_committed_specifications_follow_the_api_guideline_response_rules() -> None:
    uuid7 = "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    # Status codes catalogued in API Guideline part 1, chapter 4.7.
    catalogued = {"101", "200", "201", "202", "204", "304", "400", "401", "403", "404", "405", "408", "409", "410",
                  "412", "415", "422", "428", "429", "500", "501", "502", "503", "504"}
    paths = sorted((Path(__file__).resolve().parents[1] / "dist/messages").glob("*/v*/*/openapi.yaml"))
    assert len(paths) == 16
    for path in paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for path_item in document["paths"].values():
            operation = path_item["post"]
            parameters = {parameter["name"]: parameter for parameter in operation["parameters"] if "name" in parameter}
            for name in ("H2-Transaction-Id", "H2-Initial-Transaction-Id"):
                assert parameters[name]["schema"]["pattern"] == uuid7, (path, name)
            assert set(operation["responses"]) <= catalogued, path
            for status, response in operation["responses"].items():
                headers = response["headers"]
                assert headers["H2-API-Version"]["required"] is True, (path, status)
                assert headers["H2-Transaction-Id"]["schema"]["pattern"] == uuid7, (path, status)
                assert headers["H2-Reference-Id"]["schema"]["pattern"] == uuid7, (path, status)
                assert list(response["content"]) == ["application/json"], (path, status)
                if status == "405":
                    assert headers["Allow"]["required"] is True, path
                if status.startswith(("4", "5")):
                    schema = response["content"]["application/json"]["schema"]
                    assert set(schema["required"]) == {"code", "title", "transactionId"}, (path, status)


def test_generation_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output_a = tmp_path / "dist-a"
    output_b = tmp_path / "dist-b"
    make_source(source)
    make_templates(templates)

    generate_all_messages(source_root=source, templates_root=templates, output_root=output_a)
    generate_all_messages(source_root=source, templates_root=templates, output_root=output_b)

    a = (output_a / "messages/alpha/v1.0/test/openapi.yaml").read_bytes()
    b = (output_b / "messages/alpha/v1.0/test/openapi.yaml").read_bytes()
    assert a == b


def test_generate_all_messages_removes_outputs_without_catalog_entry(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output = tmp_path / "dist"
    make_source(source)
    make_templates(templates)
    stale_version = output / "messages/alpha/v0.8/test/openapi.yaml"
    stale_message = output / "messages/gamma/v1.0/test/openapi.yaml"
    unrelated = output / "messages/beta/v1.0/notes/README.md"
    for path in (stale_version, stale_message, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")

    outputs = generate_all_messages(source_root=source, templates_root=templates, output_root=output)

    assert sorted(output.glob("messages/*/v*/*/openapi.yaml")) == sorted(outputs)
    assert not (output / "messages/alpha/v0.8").exists()
    assert not (output / "messages/gamma").exists()
    assert unrelated.read_text(encoding="utf-8") == "stale"
    validate_all_outputs(output)


def test_generate_all_messages_bundles_the_source_validator(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    bundle = tmp_path / "bundle"
    make_source(source)
    make_templates(templates)
    package = source / "tools/validation"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"from .rules import RULE\r\n")
    (package / "rules.py").write_text("RULE = 1\n", encoding="utf-8")
    bundle.mkdir()
    (bundle / "removed_upstream.py").write_text("OLD = 1\n", encoding="utf-8")

    generate_all_messages(source_root=source, templates_root=templates, output_root=tmp_path / "dist", validator_output=bundle)

    assert sorted(path.name for path in bundle.iterdir()) == ["SOURCE.json", "__init__.py", "rules.py"]
    assert (bundle / "__init__.py").read_bytes() == b"from .rules import RULE\n"
    info = json.loads((bundle / "SOURCE.json").read_text(encoding="utf-8"))
    assert info["package"] == "tools/validation"
    assert set(info) == {"description", "repository", "commit", "package"}


def test_validator_bundle_requires_the_source_package(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    make_source(source)
    make_templates(templates)

    with pytest.raises(ValidatorBundleError, match="Validation package not found"):
        generate_all_messages(source_root=source, templates_root=templates, output_root=tmp_path / "dist", validator_output=tmp_path / "bundle")


@pytest.mark.parametrize("url", ["https://user:secret@github.com/org/repo.git", "https://x-access-token:abc@github.com/org/repo.git"])
def test_bundle_source_info_never_contains_credentials(url: str) -> None:
    assert _without_credentials(url) == "https://github.com/org/repo.git"


@pytest.mark.parametrize("nested", [False, True])
def test_generation_preserves_independent_conditional_constraints(tmp_path: Path, nested: bool) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output = tmp_path / "dist"
    make_source(source)
    make_templates(templates)
    conditions = [
        {
            "if": {"properties": {"timeSeriesType": {"const": series_type}}},
            "then": {
                "properties": {
                    "unit": {"const": unit},
                    "values": {"items": constraints},
                }
            },
        }
        for series_type, unit, constraints in [
            ("confirmedQuantity", "kWh", {"required": ["status", "dispatchInstruction"]}),
            ("requestedQuantity", "kWh", {"properties": {"status": False, "dispatchInstruction": False}}),
            ("bookedCapacity", "kW", {"properties": {"status": False, "dispatchInstruction": False}}),
        ]
    ]
    # Nested branches exercise merging of conditions on the same property,
    # where checking only the immediate allOf members would still lose rules.
    schema = (
        {"allOf": [{"properties": {"series": condition}} for condition in conditions]}
        if nested else {"allOf": conditions}
    )
    schema_path = source / "schemas/messages/alpha/v1.0/test/alpha.schema.json"
    source_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    source_schema.pop("required")
    source_schema.pop("properties")
    source_schema.update(schema)
    write_json(schema_path, source_schema)
    generate_all_messages(source_root=source, templates_root=templates, output_root=output)
    document = yaml.safe_load((output / "messages/alpha/v1.0/test/openapi.yaml").read_text(encoding="utf-8"))
    source_validator = Draft202012Validator(source_schema)
    generated_validator = Draft202012Validator(document["components"]["schemas"]["alphaPayload"])

    for series_type, unit in [("confirmedQuantity", "kWh"), ("requestedQuantity", "kWh"), ("bookedCapacity", "kW")]:
        value = {"status": "confirmed", "dispatchInstruction": "none"} if series_type == "confirmedQuantity" else {}
        payload = {"timeSeriesType": series_type, "unit": unit, "values": [value]}
        wrong_unit = {**payload, "unit": "kW" if unit == "kWh" else "kWh"}
        wrong_value = {**payload, "values": [{} if value else {"status": "confirmed"}]}
        for candidate, expected in [(payload, True), (wrong_unit, False), (wrong_value, False)]:
            instance = {"series": candidate} if nested else candidate
            assert source_validator.is_valid(instance) is expected
            assert generated_validator.is_valid(instance) is expected


def test_flattened_all_of_keeps_the_most_specific_descriptions(tmp_path: Path) -> None:
    source = tmp_path / "source"
    templates = tmp_path / "templates"
    output = tmp_path / "dist"
    make_source(source)
    make_templates(templates)
    shared_path = source / "schemas/_shared/shared.schema.json"
    shared = json.loads(shared_path.read_text(encoding="utf-8"))
    shared["description"] = "Reusable shared block."
    shared["properties"]["code"]["description"] = "Generic code."
    write_json(shared_path, shared)
    schema_path = source / "schemas/messages/alpha/v1.0/test/alpha.schema.json"
    alpha = json.loads(schema_path.read_text(encoding="utf-8"))
    for key in ("type", "required", "properties"):
        alpha.pop(key)
    alpha["description"] = "Alpha message."
    alpha["allOf"] = [
        {"$ref": shared["$id"]},
        {"type": "object", "properties": {"code": {"description": "Alpha-specific code."}}},
    ]
    write_json(schema_path, alpha)

    generate_all_messages(source_root=source, templates_root=templates, output_root=output)

    document = yaml.safe_load((output / "messages/alpha/v1.0/test/openapi.yaml").read_text(encoding="utf-8"))
    schema = document["components"]["schemas"]["alphaPayload"]
    assert "allOf" not in schema
    assert schema["title"] == "alphaPayload"
    assert schema["description"] == "Alpha message."
    assert schema["properties"]["code"]["description"] == "Alpha-specific code."


@pytest.mark.parametrize(
    ("schema", "instances", "flattened"),
    [
        ({"allOf": [{"minimum": 10}, {"minimum": 0}]}, [5, 10, 11], True),
        ({"allOf": [{"maximum": 0}, {"maximum": 10}]}, [-1, 0, 5], True),
        ({"allOf": [{"type": "array", "maxItems": 1}, {"maxItems": 5}]}, [[], [1], [1, 2]], True),
        ({"allOf": [{"enum": ["a"]}, {"enum": ["a", "b"]}]}, ["a", "b"], True),
        ({"allOf": [{"type": "string", "enum": ["x", "y", "z"]}, {"enum": ["y"]}]}, ["x", "y"], True),
        ({"allOf": [{"type": "integer"}, {"type": "number", "minimum": 1}]}, [0, 1, 1.5, 2], True),
        ({"allOf": [{"type": "string", "pattern": "^a"}, {"pattern": "b$"}]}, ["ab", "xb", "ax"], False),
        ({"allOf": [{"type": "array", "contains": {"const": 1}}, {"contains": {"const": 2}}]}, [[1], [2], [1, 2]], False),
        (
            {"allOf": [{"type": "object", "properties": {"a": {}}, "additionalProperties": False}, {"properties": {"b": {}}}]},
            [{"a": 1}, {"a": 1, "b": 2}, {"b": 2}],
            False,
        ),
        (
            {"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}, {"properties": {"a": {"enum": ["x"]}}}]},
            [{"a": "x"}, {"a": "y"}, {"c": 1}],
            True,
        ),
        ({"allOf": [{"properties": {"a": {"type": "string"}}}, {"properties": {"a": False}}]}, [{}, {"a": "x"}], True),
    ],
)
def test_flattening_all_of_never_changes_what_a_schema_accepts(schema: dict[str, Any], instances: list[Any], flattened: bool) -> None:
    generated = _normalize_schema(schema)

    assert ("allOf" not in generated) is flattened
    for instance in instances:
        assert Draft202012Validator(generated).is_valid(instance) == Draft202012Validator(schema).is_valid(instance), instance


def _has_example_conflict(node: Any) -> bool:
    if isinstance(node, dict):
        if "example" in node and "examples" in node:
            return True
        return any(_has_example_conflict(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_example_conflict(value) for value in node)
    return False


def _has_schema_examples(node: Any, path: tuple[str, ...] = ()) -> bool:
    if isinstance(node, dict):
        if _is_schema_context(path, node) and "examples" in node:
            return True
        return any(_has_schema_examples(value, (*path, str(key))) for key, value in node.items())
    if isinstance(node, list):
        return any(_has_schema_examples(value, (*path, str(index))) for index, value in enumerate(node))
    return False


def _is_schema_context(path: tuple[str, ...], node: dict[str, Any]) -> bool:
    if path and path[-1] == "schema":
        return True
    if len(path) >= 2 and path[0] == "components" and path[1] == "schemas":
        return True
    schema_keywords = {"$ref", "type", "properties", "items", "enum", "allOf", "oneOf", "anyOf", "format", "pattern"}
    return bool(schema_keywords.intersection(node)) and any(part in {"properties", "items", "allOf", "oneOf", "anyOf"} for part in path)

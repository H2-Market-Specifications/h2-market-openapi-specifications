from __future__ import annotations

import argparse
from pathlib import Path

from .catalog import find_message, load_catalog
from .openapi_writer import generate_all_messages, generate_message
from .validate import load_openapi, validate_all_outputs, validate_openapi_document

DEFAULT_VALIDATOR_OUTPUT = Path("mock_backend/_vendor/h2_message_validation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="h2_openapi_generator")
    sub = parser.add_subparsers(dest="command", required=True)

    generate_all = sub.add_parser("generate-all", help="Generate one OpenAPI YAML per catalog message.")
    generate_all.add_argument("--source-root", type=Path, required=True)
    generate_all.add_argument("--templates-root", type=Path, default=Path("templates"))
    generate_all.add_argument("--output-root", type=Path, default=Path("dist"))
    generate_all.add_argument("--no-strict-templates", action="store_true")
    generate_all.add_argument("--no-prune-unused-components", action="store_true")
    generate_all.add_argument("--common-components", type=Path, default=None)
    generate_all.add_argument(
        "--validator-output",
        type=Path,
        default=DEFAULT_VALIDATOR_OUTPUT,
        help="Directory that receives the bundled copy of the source repository's semantic validation.",
    )
    generate_all.add_argument("--no-validator-bundle", action="store_true", help="Do not bundle the semantic validation.")

    generate_one = sub.add_parser("generate-message", help="Generate one OpenAPI YAML for one catalog message.")
    generate_one.add_argument("--source-root", type=Path, required=True)
    generate_one.add_argument("--templates-root", type=Path, default=Path("templates"))
    generate_one.add_argument("--output-root", type=Path, default=Path("dist"))
    generate_one.add_argument("--message-id", required=True)
    generate_one.add_argument("--message-version", required=True)
    generate_one.add_argument("--no-prune-unused-components", action="store_true")
    generate_one.add_argument("--common-components", type=Path, default=None)

    validate_all = sub.add_parser("validate-all", help="Validate all generated OpenAPI YAML files.")
    validate_all.add_argument("--output-root", type=Path, default=Path("dist"))

    validate_file = sub.add_parser("validate-file", help="Validate one generated OpenAPI YAML file.")
    validate_file.add_argument("--openapi", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "generate-all":
        outputs = generate_all_messages(
            source_root=args.source_root,
            templates_root=args.templates_root,
            output_root=args.output_root,
            strict_templates=not args.no_strict_templates,
            prune_unused_components=not args.no_prune_unused_components,
            common_components_path=args.common_components,
            validator_output=None if args.no_validator_bundle else args.validator_output,
        )
        for output in outputs:
            print(output)
        return 0

    if args.command == "generate-message":
        message = find_message(load_catalog(args.source_root), args.message_id, args.message_version)
        output = generate_message(
            source_root=args.source_root,
            templates_root=args.templates_root,
            output_root=args.output_root,
            message=message,
            prune_unused_components=not args.no_prune_unused_components,
            common_components_path=args.common_components,
        )
        print(output)
        return 0

    if args.command == "validate-all":
        outputs = validate_all_outputs(args.output_root)
        for output in outputs:
            print(output)
        return 0

    if args.command == "validate-file":
        validate_openapi_document(load_openapi(args.openapi))
        print(args.openapi)
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2

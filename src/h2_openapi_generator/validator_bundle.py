from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Package in the source repository that holds the shared semantic validation.
VALIDATOR_SOURCE_PACKAGE = Path("tools/validation")
SOURCE_INFO_FILE = "SOURCE.json"


class ValidatorBundleError(ValueError):
    """Raised when the semantic validator cannot be bundled from the source repository."""


def bundle_validator(source_root: Path, output_dir: Path) -> Path:
    """Copy the source repository's validation package into output_dir, replacing its previous contents.

    The mock backend imports the bundled copy, so it runs without the source repository and always
    validates with the same upstream state the specifications were generated from.
    """
    package = source_root / VALIDATOR_SOURCE_PACKAGE
    modules = sorted(package.glob("*.py"))
    if not (package / "__init__.py").is_file():
        raise ValidatorBundleError(f"Validation package not found in source repository: {package}")

    output_dir.mkdir(parents=True, exist_ok=True)
    expected = {module.name for module in modules} | {SOURCE_INFO_FILE}
    for existing in output_dir.iterdir():
        if existing.is_file() and existing.name not in expected:
            existing.unlink()
        elif existing.is_dir() and existing.name == "__pycache__":
            shutil.rmtree(existing)
    for module in modules:
        # Normalise line endings so the bundle is identical on every platform.
        text = module.read_text(encoding="utf-8").replace("\r\n", "\n")
        (output_dir / module.name).write_text(text, encoding="utf-8", newline="\n")

    info = {
        "description": "Generated copy of the source repository's semantic validation. Do not edit; regenerate with generate-all.",
        "repository": _git(source_root, "config", "--get", "remote.origin.url", redact=True),
        "commit": _git(source_root, "rev-parse", "HEAD"),
        "package": VALIDATOR_SOURCE_PACKAGE.as_posix(),
    }
    (output_dir / SOURCE_INFO_FILE).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    return output_dir


def _git(repository: Path, *args: str, redact: bool = False) -> str | None:
    try:
        completed = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    return _without_credentials(value) if redact else value


def _without_credentials(url: str) -> str:
    parts = urlsplit(url)
    if not parts.netloc or "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))

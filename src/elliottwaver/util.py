"""Canonical serialization and atomic local-file primitives."""

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def package_source_digest() -> str:
    package = Path(__file__).resolve().parent
    return digest(
        {
            "src/elliottwaver/" + path.relative_to(package).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(package.rglob("*.py"))
        }
    )


def support_test_inputs_digest(root: Path | None = None) -> str | None:
    """Fingerprint repository fixtures, test helpers, scripts and pytest configuration."""
    root = root or Path(__file__).resolve().parents[2]
    tests = root / "tests"
    config = root / "pyproject.toml"
    if not (tests / "conftest.py").is_file() or not config.is_file():
        return None
    files = [
        config,
        *(path for path in root.glob("config*.yaml") if path.name != "config.local.yaml"),
    ]
    for directory in (tests, root / "scripts"):
        if not directory.is_dir():
            return None
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and not any(
                part.startswith(".") or part == "__pycache__"
                for part in path.relative_to(root).parts
            )
        )
    return digest(
        {
            file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted(set(files))
        }
    )


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

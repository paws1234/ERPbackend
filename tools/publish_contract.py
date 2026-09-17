"""Publish the API contract artifact from the application itself.

    DATABASE_URL=... uv run --with fastapi --with 'sqlalchemy>=2.0' \
        python tools/publish_contract.py

Writes ``contract/<API_VERSION>/openapi.json`` — generated from the app, never
hand-written, so the frontend repository can pull it at a pinned ref and build
against exactly what the backend serves.

The version comes from the application (:data:`app.api.API_VERSION`), not from an
argument here: a contract is written only by the code that declares that version.
A breaking change is therefore a new version and a new directory, leaving the file
the frontend already pinned untouched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api import API_VERSION, app  # noqa: E402


def artifact_path(version: str) -> Path:
    """Where a version's contract is published."""
    return ROOT / "contract" / version / "openapi.json"


def render() -> str:
    """The contract as it is committed: stable ordering, one trailing newline."""
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused")
    target = artifact_path(API_VERSION)
    written = render()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_text() == written:
        print(f"{target.relative_to(ROOT)} is already current ({len(written)} bytes)")
        return 0
    target.write_text(written)
    print(f"published {target.relative_to(ROOT)} for version {API_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

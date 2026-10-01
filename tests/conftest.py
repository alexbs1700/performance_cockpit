"""Shared pytest fixtures."""

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def activity_summary() -> dict[str, Any]:
    """A small, synthetic activity summary with no personal data."""
    return json.loads((FIXTURES_DIR / "activity_summary.json").read_text())


def build_zip(files: dict[str, bytes]) -> bytes:
    """Build an in-memory zip archive, mimicking Garmin's ORIGINAL download."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def make_zip() -> Callable[[dict[str, bytes]], bytes]:
    """Factory fixture that builds zip archives from a {name: content} mapping."""
    return build_zip

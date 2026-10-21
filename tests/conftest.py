"""Shared pytest setup: import paths and the ``needs_data`` skip marker.

The repository's databases (``db/``) and scraped data (``data/``) are not committed, so a fresh clone
cannot run tests that read them.  Mark such a test ``@pytest.mark.needs_data`` (optionally naming the
files it needs, relative to the repo root, e.g. ``needs_data("db/icsa.db")``) and it is skipped, not
failed, when they are missing.  With no arguments the marker requires ``db/icsa.db`` and ``data/``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "analysis" / "disciplines"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_REQUIRED = ("db/icsa.db", "data")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``needs_data`` test whose required files are absent."""
    for item in items:
        marker = item.get_closest_marker("needs_data")
        if marker is None:
            continue
        missing = [rel for rel in (marker.args or DEFAULT_REQUIRED) if not (ROOT / rel).exists()]
        if missing:
            item.add_marker(pytest.mark.skip(reason=f"needs data files: {', '.join(missing)}"))

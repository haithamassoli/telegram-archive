"""Locates the Convex source, which no longer lives in this repo.

The schema and functions moved to the site repo (`kashaf-alkulify`), which owns
the shared deployment; this repo is an HTTP client. The signature tests still
read that source to keep the Python fakes honest, so they have to find it.

ponytail: a sibling-directory guess plus an env override, and a loud error when
neither resolves. There is no CI here, so "not checked out" means a developer
machine is missing a clone — worth failing on, not worth skipping quietly.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT = REPO.parent / "kashaf-alkulify" / "convex"


def convex_dir() -> Path:
    path = Path(os.environ.get("ARCHIVE_CONVEX_DIR", DEFAULT))
    if not (path / "mutations.ts").is_file():
        raise FileNotFoundError(
            f"no mutations.ts under {path}. The Convex source lives in the site "
            "repo — clone kashaf-alkulify beside this one, or point "
            "ARCHIVE_CONVEX_DIR at its convex/ directory."
        )
    return path


def source() -> str:
    """mutations.ts + queries.ts concatenated, for signature scraping."""
    path = convex_dir()
    return (path / "mutations.ts").read_text() + (path / "queries.ts").read_text()


def schema() -> str:
    """schema.ts, which now also carries the site's own tables."""
    return (convex_dir() / "schema.ts").read_text()

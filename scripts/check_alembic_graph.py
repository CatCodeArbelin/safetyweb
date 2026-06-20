#!/usr/bin/env python3
"""Validate that Alembic migration sources form a single clean graph."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = ROOT / "alembic" / "versions"
ALEMBIC_INI = ROOT / "alembic.ini"

REVISION_RE = re.compile(
    r'^revision(?:\s*:\s*[^=]+)?\s*=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)
DOWN_REVISION_RE = re.compile(
    r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*([^\n#]+)",
    re.MULTILINE,
)
QUOTED_REVISION_RE = re.compile(r"[\"']([^\"']+)[\"']")


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _parse_revision_file(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    revision_match = REVISION_RE.search(text)
    if not revision_match:
        raise ValueError(f"No revision = ... assignment found in {path.relative_to(ROOT)}")
    down_revision_match = DOWN_REVISION_RE.search(text)
    if not down_revision_match:
        raise ValueError(f"No down_revision = ... assignment found in {path.relative_to(ROOT)}")
    return revision_match.group(1), down_revision_match.group(1).strip()


def _parent_revisions(raw_down_revision: str) -> list[str]:
    if raw_down_revision in {"None", "()"}:
        return []
    return QUOTED_REVISION_RE.findall(raw_down_revision)


def main() -> int:
    if not VERSIONS_DIR.is_dir():
        return _fail(f"Alembic versions directory does not exist: {VERSIONS_DIR}")

    parsed: dict[Path, tuple[str, str]] = {}
    try:
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            parsed[path] = _parse_revision_file(path)
    except ValueError as exc:
        return _fail(str(exc))

    revisions_by_id: dict[str, list[Path]] = defaultdict(list)
    for path, (revision, _) in parsed.items():
        revisions_by_id[revision].append(path)

    duplicates = {
        revision: [str(path.relative_to(ROOT)) for path in paths]
        for revision, paths in revisions_by_id.items()
        if len(paths) > 1
    }
    if duplicates:
        return _fail(f"Duplicate Alembic revision ids found: {duplicates}")

    revisions = set(revisions_by_id)
    missing: dict[str, list[str]] = {}
    for path, (_, raw_down_revision) in parsed.items():
        parents = _parent_revisions(raw_down_revision)
        missing_parents = [parent for parent in parents if parent not in revisions]
        if missing_parents:
            missing[str(path.relative_to(ROOT))] = missing_parents
    if missing:
        return _fail(f"Alembic down_revision points to missing revisions: {missing}")

    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) != 1:
        return _fail(f"Expected exactly one Alembic head, got {len(heads)}: {list(heads)}")

    print(f"Alembic graph OK: {len(parsed)} migrations, single head {heads[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

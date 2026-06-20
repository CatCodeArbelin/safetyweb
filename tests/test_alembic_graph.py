from pathlib import Path
import re

from alembic.config import Config
from alembic.script import ScriptDirectory

REVISION_RE = re.compile(
    r'^revision(?:\s*:\s*[^=]+)?\s*=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)
DOWN_REVISION_RE = re.compile(
    r'^down_revision(?:\s*:\s*[^=]+)?\s*=\s*([^\n#]+)',
    re.MULTILINE,
)
VERSIONS_DIR = Path("alembic/versions")


def _parse_revision_file(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    revision_match = REVISION_RE.search(text)
    assert revision_match, f"No revision id found in {path}"
    down_revision_match = DOWN_REVISION_RE.search(text)
    assert down_revision_match, f"No down_revision found in {path}"
    return revision_match.group(1), down_revision_match.group(1).strip()


def test_alembic_revision_ids_are_unique_by_source() -> None:
    revisions_by_id: dict[str, list[str]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        revision, _ = _parse_revision_file(path)
        revisions_by_id.setdefault(revision, []).append(str(path))

    duplicates = {
        revision: paths
        for revision, paths in sorted(revisions_by_id.items())
        if len(paths) > 1
    }
    assert duplicates == {}, (
        "Duplicate Alembic revision ids found before loading "
        f"ScriptDirectory: {duplicates}"
    )


def test_alembic_down_revisions_exist_by_source() -> None:
    parsed = {path: _parse_revision_file(path) for path in VERSIONS_DIR.glob("*.py")}
    revisions = {revision for revision, _ in parsed.values()}

    missing: dict[str, str] = {}
    for path, (_, down_revision) in parsed.items():
        if down_revision in {"None", "()"}:
            continue
        for parent in re.findall(r"[\"']([^\"']+)[\"']", down_revision):
            if parent not in revisions:
                missing[str(path)] = parent

    assert missing == {}, f"Alembic down_revision points to missing revisions: {missing}"


def test_alembic_has_single_head() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"Expected single Alembic head, got {len(heads)}: {list(heads)}"


def test_alembic_script_revisions_are_unique() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    revisions = [revision.revision for revision in script.walk_revisions()]
    assert len(revisions) == len(set(revisions)), "Duplicate Alembic revision ids found"

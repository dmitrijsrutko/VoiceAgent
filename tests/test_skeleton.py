"""Chapter 0's only test: the skeleton itself is sound.

It exists so that `uv run verify` is a real, passing gate from the very
first commit rather than something switched on later.
"""

import tomllib
from pathlib import Path

import voice_agent

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_package_imports_and_declares_a_version() -> None:
    assert voice_agent.__version__


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == voice_agent.__version__


def test_required_project_documents_exist() -> None:
    """The chapter method depends on these files; a chapter that deletes or
    renames one has broken the method, not just the docs."""
    for name in ("README.md", "CHANGELOG.md", "AGENTS.md", "prompts/system_prompt.md"):
        assert (REPO_ROOT / name).is_file(), f"missing {name}"

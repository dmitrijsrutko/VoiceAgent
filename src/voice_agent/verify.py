"""`uv run verify` — the project's one-command quality gate.

Runs lint, format check, type check, and tests, in that order, stopping at
the first failure so feedback stays fast. This is the single command that
decides whether a chapter is done; agents and CI both run exactly this.
"""

import subprocess
import sys

SOURCE_DIRS = ["src", "tests"]
"""Scoped explicitly rather than '.': the agent produces runtime artifacts
(recordings, session logs, traces) in whatever directory it runs from, and
those must not be able to fail the project's own quality gate."""

CHECKS: list[tuple[str, list[str]]] = [
    ("ruff check", ["uv", "run", "ruff", "check", *SOURCE_DIRS]),
    ("ruff format --check", ["uv", "run", "ruff", "format", "--check", *SOURCE_DIRS]),
    ("mypy", ["uv", "run", "mypy"]),
    ("pytest", ["uv", "run", "pytest"]),
]


def main() -> None:
    for label, command in CHECKS:
        print(f"\n=== {label} ===")
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            print(f"\nverify failed at: {label}", file=sys.stderr)
            sys.exit(result.returncode)

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()

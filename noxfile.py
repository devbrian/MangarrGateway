"""The single gate task (D-12).

``uv run nox -s gate`` runs the full local gate — ruff lint + ruff format check +
mypy + pytest (which includes the in-process schemathesis getVersion contract
test). CI invokes this SAME command, so "green in CI" is reproducible locally
with one command.
"""

from __future__ import annotations

import nox

nox.options.default_venv_backend = "uv"


@nox.session(python="3.12")
def gate(session: nox.Session) -> None:
    """Lint, format-check, type-check, and test — the one gate."""
    session.run_install(
        "uv",
        "sync",
        "--dev",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
    session.run("mypy", "src")
    session.run("pytest", "-q")

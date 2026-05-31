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


@nox.session(python="3.12")
def live(session: nox.Session) -> None:
    """Per-source nightly live smoke (D-63) — mirrors the nightly invocation.

    Runs ``pytest -m live`` against real upstream sites through a real
    Patchright browser. Excluded from the deterministic ``gate`` session.
    ``-o addopts=`` overrides the ``-m 'not live'`` default declared in
    ``pyproject.toml`` so the live marker is NOT deselected. ``--junit-xml``
    feeds the per-source triage script Plan 05 wires into CI.

    INFO-level pytest live logging is enabled by default
    (``-o log_cli=true -o log_cli_level=INFO``) so the #54 diagnostic
    instrumentation (``GATEWAY_CLOUDFLARE_LOG_BROWSER_EVENTS=1`` ->
    ``CloudflareSolver`` per-page pageerror/console/requestfailed listeners)
    actually emits its evidence into the live-test output. The deterministic
    ``gate`` session keeps its default (quiet) logging.

    Append ``-- -k <expr>`` etc. via posargs to scope a local run, e.g.
    ``uv run nox -s live -- -k comix``.
    """
    session.run_install(
        "uv",
        "sync",
        "--dev",
        env={"UV_PROJECT_ENVIRONMENT": session.virtualenv.location},
    )
    session.run(
        "pytest",
        "-m",
        "live",
        "--junit-xml=junit.xml",
        # `-o addopts=` overrides the `-m 'not live'` default declared in
        # pyproject.toml so the live marker is NOT deselected here.
        *("-o", "addopts="),
        # Live-only INFO-level CLI logging: required for the #54 browser-
        # event capture instrumentation to actually surface its evidence
        # (the diagnostic handlers log at INFO; without log_cli=true these
        # never reach the live-test output). Scoped to the live session —
        # the gate stays quiet (D-12 deterministic gate).
        *("-o", "log_cli=true"),
        *("-o", "log_cli_level=INFO"),
        *session.posargs,
    )

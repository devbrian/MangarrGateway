"""Opt-in live smoke tests (``pytest -m live``) — excluded from ``nox -s gate``.

These hit the REAL Comix site through a REAL Patchright browser + real Cloudflare.
They require a browser binary (``uv run patchright install chromium``) and live
network, so they are NEVER part of the deterministic gate (D-42/D-43). The
``live`` marker + ``addopts = "-m 'not live'"`` (pyproject.toml) keep them opt-in.
"""

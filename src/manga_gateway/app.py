"""FastAPI application factory.

Task 1 ships the minimal factory that mounts the ``/api/v1`` router with
``GET /version`` so the contract test goes GREEN. The lifespan singleton seams,
global API-key auth, and the JSON error model land in Task 2; the full
``load_settings`` default wiring lands in Task 3.
"""

from __future__ import annotations

from fastapi import FastAPI

from .api import api_router
from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: Explicit settings (tests inject a fixed key per D-03). When
            ``None``, Task 3 wires this to ``load_settings()``.
    """
    if settings is None:
        # Task 3 wires this to load_settings() (TOML load + auto-key persist).
        raise ValueError("settings must be provided (load_settings lands in Task 3)")

    app = FastAPI(
        title="Mangarr Manga-Gateway API",
        root_path=settings.url_base or "",  # PLAT-01 UrlBase
    )
    app.state.settings = settings
    app.include_router(api_router)
    return app

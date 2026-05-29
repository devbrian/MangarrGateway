"""``python -m manga_gateway`` entrypoint.

Runs uvicorn as a SINGLE process (R1 / CLAUDE.md "What NOT to Use"): never
``--workers``, never gunicorn. The shared anti-bot session, in-memory caches,
and job queue all assume one process. uvloop is absent on the Windows dev host
(uvicorn falls back to asyncio there); Linux prod gets uvloop — both fine.
"""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import load_settings


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    # Single process. No workers kwarg — multi-worker is forbidden (R1).
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

"""LiveSmokeProfile registry — one ``{source_key}.py`` per registered source (D-49).

Strict production/test separation: production ``src/manga_gateway/sources/*.py``
stay free of test-only data. Each profile module exposes a top-level
``LIVE_SMOKE: LiveSmokeProfile`` instance the parametrized tests read at
collection time.
"""

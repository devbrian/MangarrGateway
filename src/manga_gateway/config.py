"""Application settings + API-key provisioning.

TOML is the source of truth (D-10); env vars override ONLY ops knobs
(host/port/output_root) (D-11). The API key is auto-generated-and-persisted on
first start and is NEVER supplied via the environment (D-01).

Env-exclusion mechanism for ``api_key`` (Open Question 2 / Pitfall 4):
``load_settings`` constructs ``Settings(api_key=<from TOML>, ...)`` passing the
key as an explicit init keyword. In pydantic-settings, init keywords take
precedence over the environment source, so ``GATEWAY_API_KEY`` can never set the
effective key. The ``alias`` on ``api_key`` further decouples it from the
``GATEWAY_`` env prefix. A regression test asserts the env value is ignored.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import tomllib
from pathlib import Path

import tomli_w
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger("manga_gateway")

_KEY_BYTES = 32  # secrets.token_urlsafe(32) -> >= 43 url-safe chars


class Settings(BaseSettings):
    """Gateway runtime settings.

    ``host``/``port``/``output_root`` are env-overridable ops knobs (D-11).
    ``api_key`` is provisioned from the TOML file / explicit construction only
    (D-01): its alias keeps it off the ``GATEWAY_`` env mapping, and
    ``load_settings`` always passes it explicitly.
    """

    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore")

    host: str = "127.0.0.1"  # AUTH-02: localhost bind by default
    port: int = 9191
    url_base: str = ""  # PLAT-01: UrlBase reverse-proxy prefix; "" = none
    output_root: str = "/data/manga"  # D-11: gateway-determined default
    # alias decouples the key from the GATEWAY_ env prefix (D-01).
    api_key: str = Field(alias="api_key")


def load_settings(path: Path = Path("config.toml")) -> Settings:
    """Load settings from TOML, generating + persisting the key on first run.

    Args:
        path: TOML config path (source of truth, D-10).

    Returns:
        Fully-provisioned ``Settings``. The ``api_key`` always comes from the
        TOML data, never from the environment (D-01/D-11).
    """
    data: dict[str, object] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    api_key = data.get("api_key")
    if not api_key or not isinstance(api_key, str):
        api_key = secrets.token_urlsafe(_KEY_BYTES)  # D-01 generate
        data["api_key"] = api_key
        # stdlib tomllib is read-only — tomli_w writes the key back (D-10).
        # Atomic + owner-only write of the sole plaintext secret (WR-03/WR-04):
        # mkstemp creates the temp file 0o600 in the same dir, os.replace() makes
        # the swap atomic so a crash mid-write can't truncate the only key copy.
        # On Windows the 0o600 bits are best-effort (largely ignored); effective
        # on the Linux prod target.
        rendered = tomli_w.dumps(data)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent or "."), prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(rendered)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        # Log the generation event ONCE (D-01) — never the secret itself; the
        # operator reads the key from the persisted TOML at `path` (CodeRabbit).
        _log.info("Generated a new API key and persisted it to %s", path)

    # api_key passed explicitly (beats env); host/port/output_root come from
    # env overrides via pydantic-settings (D-11). GATEWAY_API_KEY is ignored.
    return Settings(api_key=api_key)

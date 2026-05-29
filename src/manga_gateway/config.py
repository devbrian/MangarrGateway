"""Application settings + key provisioning.

TOML is the source of truth (D-10); env vars override ONLY ops knobs
(host/port/output_root) (D-11). The API key is auto-generated-and-persisted on
first start and is NEVER supplied via the environment (D-01).

NOTE: the full TOML load + key-generation persistence (``load_settings``) is
finalized in Plan 01-01 Task 3. Task 1 only needs the ``Settings`` shape so the
contract test can construct ``create_app(Settings(api_key=...))`` (D-03).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway runtime settings.

    ``api_key`` is intentionally provisioned from the TOML file / explicit
    construction only — it is excluded from the env mapping in
    ``load_settings`` so ``GATEWAY_API_KEY`` is never honored (D-01).
    """

    model_config = SettingsConfigDict(env_prefix="GATEWAY_")

    host: str = "127.0.0.1"  # AUTH-02: localhost bind by default
    port: int = 9191
    url_base: str = ""  # PLAT-01: UrlBase reverse-proxy prefix; "" = none
    output_root: str = "/data/manga"  # D-11: gateway-determined default
    api_key: str

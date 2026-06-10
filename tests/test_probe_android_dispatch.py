"""Offline acceptance for the probe's android-engine solver dispatch (Req 8/9).

``scripts/probe_rate_limits.py`` is a standalone diagnostic script — no ``src/``
package, no ``__init__.py``, and excluded from ruff/mypy. It is, however, importable
at runtime (it only pulls ``manga_gateway.*`` + stdlib), so this test loads it via
``importlib.util.spec_from_file_location`` and exercises ``_build_solver`` directly.

It proves:

* an android-engine source (``solver_engine == "android"`` — mangadot/kagane)
  dispatches to an :class:`AndroidSolver` pointed at the configured
  ``android_solver_url`` (Req 8);
* a non-android source (MangaDex, ``antibot="none"``) still returns the existing
  ``NoopSolver`` (the android branch never widens the blast radius);
* the pinned residential proxy threaded via ``_build_settings`` reaches the
  AndroidSolver verbatim through ``build_proxy`` — the SAME egress the httpx replay
  uses (Req 9) — and the proxy password is NEVER surfaced by the masked label
  (T-w1k-01 / T-11-02).

The probe header's "no pytest tests" note is now stale: this is the offline gate
acceptance for Req 8/9 (it constructs solvers only — no network, no device, NOT
``live``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from manga_gateway.framework.android_solver import AndroidSolver
from manga_gateway.framework.antibot import NoopSolver
from manga_gateway.sources.kagane import KaganeSource
from manga_gateway.sources.mangadex import MangaDexSource
from manga_gateway.sources.mangadot import MangadotSource

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROBE_PATH = _REPO_ROOT / "scripts" / "probe_rate_limits.py"
_SOLVER_URL = "http://deploy.example:18080"
_ENV_VAR = "GATEWAY_ANDROID_SOLVER_API_KEY"
# Fake keys — these are NOT secrets; the tests assert they never get logged.
_DOTENV_KEY = "DOTENV-not-a-real-key-aaa1"
_OSENV_KEY = "OSENV-not-a-real-key-bbb2"


def _load_probe() -> ModuleType:
    """Import the standalone probe script at runtime (no package, mypy-excluded)."""
    spec = importlib.util.spec_from_file_location("probe_rate_limits", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so any internal self-reference resolves.
    sys.modules.setdefault("probe_rate_limits", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The loaded probe module with ``GATEWAY_ANDROID_SOLVER_URL`` populated."""
    monkeypatch.setenv("GATEWAY_ANDROID_SOLVER_URL", _SOLVER_URL)
    return _load_probe()


@pytest.mark.parametrize("source_cls", [MangadotSource, KaganeSource])
def test_android_engine_source_dispatches_to_android_solver(
    probe: ModuleType, source_cls: type
) -> None:
    """Req 8: mangadot/kagane build an AndroidSolver at the configured url."""
    settings = probe._build_settings(None)
    assert settings.android_solver_url == _SOLVER_URL

    solver = probe._build_solver(source_cls, settings, None)

    assert isinstance(solver, AndroidSolver)
    # base_url is trailing-slash-stripped by the ctor; our url has none.
    assert solver._base_url == _SOLVER_URL
    # The source's own key + challenge URL flow into the per-source map.
    assert solver._challenge_urls == {
        source_cls.key: source_cls.cloudflare_challenge_url
    }


def test_non_android_source_returns_existing_solver(probe: ModuleType) -> None:
    """Req 8: a non-android source (MangaDex, antibot=none) is unchanged."""
    settings = probe._build_settings(None)
    solver = probe._build_solver(MangaDexSource, settings, None)
    assert not isinstance(solver, AndroidSolver)
    assert isinstance(solver, NoopSolver)


def test_android_solver_carries_pinned_proxy_without_leaking_value(
    probe: ModuleType,
) -> None:
    """Req 9: the pinned proxy threaded via _build_settings reaches the AndroidSolver
    (same egress as the httpx replay), and its value is never surfaced unmasked."""
    proxy = probe.ProxyEntry(
        index=3,
        host="proxy.invalid",
        port="8080",
        username="fakeuser",
        password="NOTAREALSECRET-zzz9",
    )
    settings = probe._build_settings(proxy)
    solver = probe._build_solver(MangadotSource, settings, proxy)

    assert isinstance(solver, AndroidSolver)
    # build_proxy emits the Playwright dict; it rides the AndroidSolver verbatim so
    # the /solve egress matches the replay egress (Req 9).
    assert solver._proxy is not None
    assert solver._proxy["server"] == proxy.server
    assert solver._proxy["username"] == proxy.username
    assert solver._proxy["password"] == proxy.password

    # T-w1k-01 / T-11-02: the masked label NEVER exposes the credential value.
    masked = probe._mask_proxy(proxy)
    assert proxy.password not in masked
    assert proxy.username not in masked
    assert f"proxy[#{proxy.index}]" in masked


# ─────────────────── #212: android key from .env + 401 message ───────────────────


def test_build_settings_reads_android_key_from_dotenv_when_os_env_unset(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#212: with the key ONLY in the repo .env (no export), _build_settings loads it
    so the sidecar /solve authenticates without a manual export."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f'{_ENV_VAR}="{_DOTENV_KEY}"\n', encoding="utf-8")
    monkeypatch.setattr(probe, "_DOTENV_PATH", dotenv)

    settings = probe._build_settings(None)

    assert settings.android_solver_api_key is not None
    assert settings.android_solver_api_key.get_secret_value() == _DOTENV_KEY


def test_build_settings_os_env_wins_over_dotenv(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit OS-env export takes precedence over the .env value."""
    monkeypatch.setenv(_ENV_VAR, _OSENV_KEY)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{_ENV_VAR}={_DOTENV_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(probe, "_DOTENV_PATH", dotenv)

    settings = probe._build_settings(None)

    assert settings.android_solver_api_key is not None
    assert settings.android_solver_api_key.get_secret_value() == _OSENV_KEY


def test_build_settings_leaves_android_key_unset_when_absent(
    probe: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither OS env nor .env carries the key -> unset (current behavior)."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    monkeypatch.setattr(probe, "_DOTENV_PATH", tmp_path / "missing.env")

    settings = probe._build_settings(None)

    assert settings.android_solver_api_key is None


def test_dotenv_parser_ignores_comments_and_strips_quotes(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The .env reader skips blanks/comments and strips surrounding quotes."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"# a comment\n\nOTHER=value\n{_ENV_VAR}='{_DOTENV_KEY}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(probe, "_DOTENV_PATH", dotenv)

    assert probe._android_api_key_from_dotenv() == _DOTENV_KEY


def _make_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"{_SOLVER_URL}/solve")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("Unauthorized", request=request, response=response)


def test_warmup_401_yields_actionable_message(probe: ModuleType) -> None:
    """#212: an android /solve 401 produces a message naming the probe env var and
    the deploy SOLVER_API_KEY — not a bare HTTPStatusError that looks like a proxy
    fault."""
    reason = probe._warmup_search_skip_reason(_make_401())

    assert _ENV_VAR in reason
    assert "SOLVER_API_KEY" in reason
    assert "401" in reason
    assert "HTTPStatusError" not in reason


def test_warmup_non_401_keeps_generic_message(probe: ModuleType) -> None:
    """A non-401 exception keeps the generic ``search() raised:`` note."""
    reason = probe._warmup_search_skip_reason(ValueError("boom"))

    assert reason == "search() raised: ValueError"
    assert _ENV_VAR not in reason


def test_is_android_solve_401_only_matches_401(probe: ModuleType) -> None:
    """The 401 classifier is exact: a 403 (proxy/CF) is NOT the android-key 401."""
    request = httpx.Request("POST", f"{_SOLVER_URL}/solve")
    forbidden = httpx.HTTPStatusError(
        "Forbidden",
        request=request,
        response=httpx.Response(403, request=request),
    )

    assert probe._is_android_solve_401(_make_401()) is True
    assert probe._is_android_solve_401(forbidden) is False
    assert probe._is_android_solve_401(ValueError("x")) is False


def test_is_android_solve_401_ignores_non_solve_401(probe: ModuleType) -> None:
    """CR (#214): a 401 from a SOURCE's own API (not /solve) is NOT the android-key
    401 — it must not be misattributed to GATEWAY_ANDROID_SOLVER_API_KEY."""
    request = httpx.Request("GET", "https://mangadot.net/api/search")
    source_401 = httpx.HTTPStatusError(
        "Unauthorized",
        request=request,
        response=httpx.Response(401, request=request),
    )

    assert probe._is_android_solve_401(source_401) is False
    # ...and the warm-up note stays generic, not the android-key message.
    assert probe._warmup_search_skip_reason(source_401) == (
        "search() raised: HTTPStatusError"
    )


def test_no_raw_key_value_emitted(
    probe: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T-11-02: building Settings from the key never prints the raw value."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{_ENV_VAR}={_DOTENV_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(probe, "_DOTENV_PATH", dotenv)

    settings = probe._build_settings(None)
    # The SecretStr repr must redact, and nothing should have been printed.
    assert _DOTENV_KEY not in repr(settings.android_solver_api_key)
    captured = capsys.readouterr()
    assert _DOTENV_KEY not in captured.out
    assert _DOTENV_KEY not in captured.err

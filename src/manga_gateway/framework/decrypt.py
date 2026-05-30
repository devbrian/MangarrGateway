"""Scheme-dispatch decrypt registry (D-39, async since 04-04 D-45).

The encrypted-response seam every source plugs into. Mirrors ``registry.py``'s
decorator-based registration and the job engine's key-dispatch-with-default idiom
(``_WRITERS.get(fmt, write_cbz)``), but the *default* here is the ``scheme is None``
identity pass-through — a non-encrypted source decrypts to itself.

This module establishes the SEAM ONLY. Sources own their concrete ciphers and
register them at import time via :func:`register_scheme`. A registered scheme may be
sync (returns ``bytes``) or async (returns ``Awaitable[bytes]``) — async covers the
case where the source's decrypt needs an external resource it can only reach via a
coroutine (e.g. a browser-evaluated cipher on a warm Patchright page).

An unknown scheme raises ``KeyError`` so a wrong key never silently leaks ciphertext
toward CBZ packaging (the downstream Pillow ``is_valid_image`` guard is the second
line of defence, T-04-01). A scheme that cannot run because its prerequisites are
unmet (e.g. a browser-evaluated scheme with no solver wired in) raises
:class:`DecryptError` so the SourceContext can classify it distinctly from an
unknown-scheme programming error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

# Registered scheme callable: sync (returns bytes) OR async (returns Awaitable[bytes]).
_SchemeFn = Callable[[bytes, dict[str, Any]], bytes | Awaitable[bytes]]

# scheme name -> decrypt function (may be sync OR async — the seam awaits a coroutine).
# Sources register concrete ciphers via the @register_scheme decorator at import time.
_SCHEMES: dict[str, _SchemeFn] = {}


class DecryptError(RuntimeError):
    """Raised when a registered decrypt scheme cannot run (e.g. missing solver,
    browser-evaluated entry point not found). Distinct from ``KeyError`` (unknown
    scheme) so the SourceContext can classify it as a terminal source failure."""


def register_scheme(name: str) -> Callable[[_SchemeFn], _SchemeFn]:
    """Decorator: register a decrypt function under ``name`` (mirrors registry.py).

    The registered callable may be sync (returning ``bytes``) OR async (returning an
    ``Awaitable[bytes]``). The :func:`decrypt` dispatcher transparently awaits the
    result if it is a coroutine.
    """

    def deco(fn: _SchemeFn) -> _SchemeFn:
        _SCHEMES[name] = fn
        return fn

    return deco


async def decrypt(scheme: str | None, body: bytes, config: dict[str, Any]) -> bytes:
    """Decrypt ``body`` under ``scheme``, or pass through verbatim for ``None``.

    Args:
        scheme: The source's ``decrypt_scheme`` (``None`` = not encrypted).
        body: The raw (possibly encrypted) response bytes.
        config: Per-source decrypt config handed to the scheme function.

    Returns:
        The plaintext bytes. When ``scheme is None`` the SAME ``body`` object is
        returned unchanged (non-encrypted pass-through, D-39).

    Raises:
        KeyError: If ``scheme`` is non-``None`` but unregistered — fail loudly
            rather than leak ciphertext downstream.
        DecryptError: If a registered scheme cannot run (e.g. a browser-evaluated
            scheme with no solver in ``config``).
    """
    if scheme is None:
        return body
    result = _SCHEMES[scheme](body, config)
    if asyncio.iscoroutine(result):
        awaited: bytes = await result
        return awaited
    assert isinstance(result, bytes)  # sync scheme contract
    return result

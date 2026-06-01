"""Single-static-proxy seam (PROXY-01, issue #65).

``build_proxy(settings)`` is the ONE place ``Settings`` proxy fields are turned
into the two egress-leg shapes the gateway needs:

* a **Playwright proxy dict** (``{"server", "username", "password"}``) threaded
  into BOTH stealth-browser launch closures (patchright + camoufox); and
* an **httpx proxy value** (``httpx.Proxy`` with auth, or a plain URL string)
  applied to the shared ``httpx.AsyncClient``.

``cf_clearance`` is IP-bound, so the browser that clears Cloudflare and the
httpx client that fetches images MUST egress through the SAME proxy. Building
both shapes from one helper guarantees that by construction.

This is also the future pool/rotation seam (CLAUDE.md "proxy-ready networking"):
a static-pool / rotating-proxy implementation replaces ONLY this function — the
two call sites are unchanged.

SECURITY (T-odg-01): the proxy password is a ``pydantic.SecretStr`` on
``Settings`` (repr/str auto-redact). ``get_secret_value()`` is unpacked ONLY
here, at the boundary that hands credentials to the browser/httpx. The httpx
leg uses ``httpx.Proxy(url=..., auth=(user, pass))`` so the password never
enters a URL string that could be logged or repr'd. This function logs nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ..config import Settings

# The httpx leg accepts either an ``httpx.Proxy`` (auth kept out of the URL) or a
# plain URL string (no-auth case). Both are valid ``proxy=`` values for
# ``httpx.AsyncClient`` on httpx 0.28.x (``proxy=`` singular — ``proxies=`` was
# removed). ``None`` on either leg means "no proxy" (the regression-guard value).
PlaywrightProxy = dict[str, str]
HttpxProxy = httpx.Proxy | str


def build_proxy(
    settings: Settings,
) -> tuple[PlaywrightProxy | None, HttpxProxy | None]:
    """Turn the three ``cloudflare_proxy_*`` Settings fields into both egress shapes.

    Returns ``(playwright_proxy_dict_or_None, httpx_proxy_or_None)``:

    * unconfigured (``cloudflare_proxy_server is None``) ⇒ ``(None, None)`` — no
      ``proxy=`` is threaded anywhere, today's behavior unchanged (T-odg-04);
    * server only ⇒ ``({"server": ...}, "<server>")`` — NO username/password
      keys, and a credential-free httpx URL string;
    * server + username + password ⇒ the full Playwright dict (plaintext creds)
      paired with an ``httpx.Proxy(url=server, auth=(user, pass))`` so the
      password stays out of the URL string (T-odg-01).

    This is the SOLE site that calls ``SecretStr.get_secret_value()``.
    """
    server = settings.cloudflare_proxy_server
    if server is None:
        return None, None

    username = settings.cloudflare_proxy_username
    password = settings.cloudflare_proxy_password

    # Auth present iff BOTH a username and a password are set. (A server with no
    # credentials is a valid unauthenticated proxy.)
    if username is not None and password is not None:
        # The ONLY place the secret is unpacked (T-odg-01).
        secret = password.get_secret_value()
        playwright_proxy: PlaywrightProxy = {
            "server": server,
            "username": username,
            "password": secret,
        }
        # httpx.Proxy keeps the password in ``auth`` — never embedded in the URL.
        httpx_proxy: HttpxProxy = httpx.Proxy(url=server, auth=(username, secret))
        return playwright_proxy, httpx_proxy

    # Unauthenticated proxy: server only, credential-free on both legs.
    return {"server": server}, server

"""Shared denylist secret-redaction (SEC-01 / D-03 / D-04 / D-05).

ONE scrub applied at two boundaries:

* :func:`redact_url` — on the ``url`` field of every ``MetricEvent`` BEFORE it
  enters a ring (the collector ingest boundary; URLs live ONLY in rings, never in
  rollup keys, so this is the one place a captured secret can land — Pitfall 5).
* :func:`redact_text` — on every rendered log line before it is written (Plan 03
  ``JsonFormatter``), so the ``/state/logs/gateway.jsonl`` the dashboard reads is
  redaction-safe.

Denylist posture (D-03): mask KNOWN secret keys, keep everything else (the full
forensic URL is the point). Masked keys: ``cf_clearance``, ``apikey``/``api_key``,
``x-api-key`` (the gateway's own master credential), ``*token*``, ``*auth*``,
``authorization``, ``x-csrf-token`` (case-insensitive substring match) plus
``Cookie``/``Authorization``/``X-Api-Key`` log-line values.

**Proxy-credential nuance (D-04 — INTENTIONAL, scoped relaxation of CLAUDE.md's
blanket proxy-redaction rule.)** CLAUDE.md says "never log the proxy server /
username / password"; here that is narrowed to "never log the proxy *credentials*."
A proxy URL ``http://user:pass@host:port`` is masked to ``***:***@host:port`` —
the credentials are scrubbed but ``host:port`` is KEPT, because which proxy a slow
or failed call routed through is forensic signal a dashboard needs, and the
host:port alone is not a secret.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Substrings (case-insensitive) of query keys / header names whose VALUE is a
# secret the gateway itself injected (see framework clearance kwargs: User-Agent,
# Cookie, X-CSRF-Token). Substring match catches api_key/apikey, *token*, *auth*.
_SECRET_KEYS = {
    "cf_clearance",
    "apikey",
    "api_key",
    "x-api-key",  # WR-01: the gateway's own master credential header/query
    "token",
    "auth",
    "authorization",
    "x-csrf-token",
}


def _mask_query(qs: str) -> str:
    """Mask the value of any query param whose key matches the denylist."""
    return urlencode(
        [
            (k, "***" if any(s in k.lower() for s in _SECRET_KEYS) else v)
            for k, v in parse_qsl(qs, keep_blank_values=True)
        ]
    )


def _is_secret_key(key: str) -> bool:
    """True if ``key`` matches the shared denylist (case-insensitive substring)."""
    return any(s in key.lower() for s in _SECRET_KEYS)


def redact_blob(blob: dict[str, object]) -> dict[str, object]:
    """Return a redacted COPY of a request blob (260605-e9a, T-e9a-01 / D-03).

    Reuses the SINGLE denylist (:data:`_SECRET_KEYS` via :func:`_mask_query`) —
    no second source of truth. The blob's ``query_string`` is scrubbed via the
    existing ``_mask_query`` boundary, and any denylisted key in the ``body`` dict
    is masked to ``"***"``. ``X-Api-Key`` / ``Authorization`` / ``token`` / ``auth``
    (all already in ``_SECRET_KEYS``) can never survive. Headers are NOT part of the
    blob (never captured), so there is nothing to redact there.

    Defensive on shape: a non-string ``query_string`` or a non-dict ``body`` (e.g.
    ``None`` for a GET) is passed through untouched. The input is not mutated.
    """
    out = dict(blob)
    qs = out.get("query_string")
    if isinstance(qs, str):
        out["query_string"] = _mask_query(qs)
    body = out.get("body")
    if isinstance(body, dict):
        out["body"] = {
            k: ("***" if isinstance(k, str) and _is_secret_key(k) else v)
            for k, v in body.items()
        }
    return out


def redact_url(url: str | None) -> str | None:
    """Mask injected secrets in a forensic URL; no-op on falsy input.

    Masks proxy credentials (keeping ``host:port``, D-04) and any denylisted query
    value (D-03), leaving benign params intact.
    """
    if not url:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:  # D-04: mask proxy creds, KEEP host:port
        netloc = "***:***@" + netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=netloc, query=_mask_query(parts.query)))


# cf_clearance=VALUE (value ends at ; & whitespace or quote). IGNORECASE so
# Cf_Clearance= / CF_CLEARANCE= are masked too; the \1 group preserves the
# original-case key prefix (CodeRabbit).
_CF_RE = re.compile(r"(cf_clearance=)[^;&\s\"]+", re.IGNORECASE)
# Cookie:/Authorization:/X-CSRF-Token: VALUE in a freeform line (header-ish).
# CR-01: the value class consumes the REST of the header value — including the
# scheme prefix ("Bearer <token>") and every pair of a multi-pair Cookie line —
# up to a JSON/structural delimiter (quote / comma / closing brace / newline).
# A whitespace-terminated class ([^...\s]+) masked only "Bearer" and left the
# token, and only masked the first pair of "Cookie: a=x; b=y".
# The value class still EXCLUDES whitespace from being a delimiter (a space inside
# "Bearer <token>" must NOT terminate the mask — that was the CR-01 bug). It now
# also stops at a comma so a comma-separated trailing field after the secret is
# not over-redacted (CodeRabbit). Semicolons stay INSIDE the class, so a multi-pair
# "Cookie: a=x; b=y; c=z" still masks every pair.
_HEADER_RE = re.compile(
    r"((?:authorization|x-api-key|x-csrf-token|cookie)[\"']?\s*[:=]\s*[\"']?)[^\"',}\r\n]+",
    re.IGNORECASE,
)
# Inline proxy creds in a URL embedded in a log line.
_PROXY_RE = re.compile(r"(https?://)[^:/@\s]+:[^@/\s]+@")


def redact_text(line: str) -> str:
    """Mask cf_clearance / Cookie / Authorization / X-CSRF-Token values and proxy
    credentials in a freeform log line (D-05), leaving non-secret text intact.
    """
    line = _CF_RE.sub(r"\1***", line)
    line = _HEADER_RE.sub(r"\1***", line)
    line = _PROXY_RE.sub(r"\1***:***@", line)
    return line

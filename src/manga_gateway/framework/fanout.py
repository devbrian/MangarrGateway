"""Concurrent source fan-out with per-source isolation (SRCH-03/04, D-14).

Runs each selected source concurrently in an ``asyncio.TaskGroup``, wrapping each in
a per-source ``asyncio.timeout``. Any timeout or exception is caught and mapped to a
``SourceWarning`` tuple — one source failing never fails the whole request (SRCH-03).

CRITICAL (SRCH-04): a source returning ``[]`` with NO warning is a distinct, valid
"0 results" outcome — never synthesize a warning for an empty-but-successful source.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Protocol

import httpx

from ..metrics.context import current_source
from .errors import SourceError

if TYPE_CHECKING:
    from .cooldown import SourceFailureCooldown

# Default per-source fan-out timeout (D-14 discretion). Sized to comfortably
# cover the slowest legitimate per-source operation:
#
# * MangaDex (httpx, rate-limited at 5 req/s): two-step search/recent flow
#   completes in well under 5 s. The default doesn't bind here.
# * Comix (Camoufox + 3-5 sequential ``_series_chapters`` browser navigations
#   on the warm BrowserContext, see debug session ``comix-parallel-page-
#   contention`` for why parallelization isn't safe): the live baseline is
#   ~7-12 s per search call, with 15-18 s observed on slower runs. The
#   prior 20 s ceiling sat on the edge — nightly 26726259461 (2026-05-31
#   22:26 UTC) tipped over under variance and surfaced as 4 cascading
#   ``source_unavailable / timed out`` test failures. 30 s gives ~100 %
#   headroom above the typical wall-clock without becoming a stall ceiling.
#
# Bound by the lower of this default and the API request's overall budget at
# the caller; ``fan_out`` accepts ``per_source_timeout`` for ad-hoc overrides.
_DEFAULT_TIMEOUT = 30.0


class _HasKey(Protocol):
    key: str


# A warning tuple: (sourceKey, code, message).
WarningTuple = tuple[str, str, str]


async def fan_out[S: _HasKey, R](
    sources: Sequence[S],
    run_one: Callable[[S], Awaitable[list[R]]],
    *,
    per_source_timeout: float = _DEFAULT_TIMEOUT,
    collect_warnings: Callable[[S], list[tuple[str, str]]] | None = None,
    cooldown: SourceFailureCooldown | None = None,
) -> tuple[list[R], list[WarningTuple]]:
    """Fan out ``run_one`` across ``sources`` with per-source isolation.

    Returns ``(releases, warnings)`` where ``warnings`` are ``(sourceKey, code,
    message)`` tuples. ``collect_warnings`` (optional) lets a successful source also
    surface soft warnings (D-14 partial success).

    ``cooldown`` (optional, 260606-lyb Change 2) is the per-source failure negative
    cache. Defaulted ``None`` so existing callers/tests stay green. When supplied: a
    source already ``in_cooldown`` is SKIPPED (no upstream call, instant
    ``source_unavailable`` warning), and a source whose run hits a HARD-failure branch
    records a cooldown so the next identical search short-circuits.
    """
    releases: list[R] = []
    warnings: list[WarningTuple] = []

    async def _guarded(src: S) -> tuple[str, list[R]]:
        # Source-scope metrics bind (OBS-02, RESEARCH Pattern 1): every per-source
        # call inside this child self-attributes its source_key via current_source.
        # asyncio copies the context into each Task at creation, so sibling sources
        # mutate only their own copy and never cross-attribute (T-08-12). Strictly
        # additive — does not change any control flow / exception propagation below.
        token = current_source.set(src.key)
        try:
            # 260606-lyb Change 2: a source in cooldown is SKIPPED — no upstream
            # call, no retry, no backoff. This return happens INSIDE the try (like
            # the success return below), so it never reaches the trailing
            # record_failure: a cooldown-skip is not itself a fresh failure.
            if cooldown is not None and cooldown.in_cooldown(src.key):
                warnings.append(
                    (src.key, "source_unavailable", "upstream unavailable (cooldown)")
                )
                return src.key, []
            async with asyncio.timeout(per_source_timeout):
                result = await run_one(src)
            if collect_warnings is not None:
                for code, message in collect_warnings(src):
                    warnings.append((src.key, code, message))
            return src.key, result
        except TimeoutError:
            warnings.append((src.key, "source_unavailable", "timed out"))
        except SourceError as exc:
            warnings.append((src.key, exc.code, str(exc)))
        except httpx.HTTPStatusError as exc:
            # A 5xx that exhausted the SourceContext retry budget reaches here as a
            # raw HTTPStatusError, NOT a typed SourceError: the permanent-4xx gate
            # (context._request_bytes) only converts 401/403/404, and tenacity
            # reraises the underlying 5xx unchanged. Surface the upstream status so an
            # outage/maintenance 5xx reads as "upstream 503", never the opaque
            # "unexpected error" (#152: mangadot served a 503 "Under Maintenance"
            # HTML page → JSON parse never reached → generic warning).
            warnings.append(
                (src.key, "source_unavailable", f"upstream {exc.response.status_code}")
            )
        except httpx.TransportError as exc:
            # Connect/read/timeout-class transport failures that exhausted retries are
            # an upstream-reachability problem, not a gateway bug — name the transport
            # error class so the warning is diagnosable rather than "unexpected" (#152).
            warnings.append(
                (
                    src.key,
                    "source_unavailable",
                    f"transport error: {type(exc).__name__}",
                )
            )
        except Exception:  # noqa: BLE001 — isolation: one source must never break others
            warnings.append((src.key, "source_unavailable", "unexpected error"))
        finally:
            current_source.reset(token)
        # 260606-lyb Change 2 invariant: reaching this line means one of the five
        # except branches above ran — BOTH non-error returns (the success return AND
        # the cooldown-skip return) happen INSIDE the try, so they never fall through
        # here. Therefore record_failure trips on HARD FAILURE ONLY: a 200-empty /
        # zero-results success never sets a cooldown.
        if cooldown is not None:
            cooldown.record_failure(src.key)
        return src.key, []

    tasks: list[asyncio.Task[tuple[str, list[R]]]] = []
    async with asyncio.TaskGroup() as tg:
        for src in sources:
            tasks.append(tg.create_task(_guarded(src)))

    for task in tasks:
        _key, result = task.result()
        releases.extend(result)

    return releases, warnings

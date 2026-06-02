"""E2E Comix slice: search→handle→cleared fetch→CBZ (04-03 Task 2).

Drives the SAME search→handle→download→package contract proven on MangaDex, but for a
``cloudflare+encrypted`` source — entirely deterministic (D-42): a fake solver supplies
a canned ``Clearance`` (no real Patchright browser) and respx intercepts the ONE shared
httpx client (no real Comix site).

What is exercised end-to-end:
* clearance injection — the outbound Comix request carries ``cf_clearance`` + the exact
  captured UA (D-40);
* the handle/job contract — a ``comix:`` handle flows through POST /downloads to a
  ``completed`` job + a CBZ of page-images-only (criterion #3);
* Option A browser-DOM resolution — ``solver.fetch_via_browser`` returns the chapter
  list / page-URL list off the fake page (no real browser);
* per-source isolation — a single failing Comix /search yields 200 + a comix warning
  while MangaDex's releases still flow (D-38/SRCH-03).

Issue #46: the ``comix-v1`` browser-evaluated decrypt seam was removed (dead code on
the current ``secure-*.js``). Comix has no live encrypted-response path — the slice
relies on plaintext httpx (``get_json_plain``/``get_bytes_plain``) + browser-DOM, and
the fake solver no longer needs a ``decrypt`` method.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from PIL import Image

from manga_gateway.app import create_app
from manga_gateway.config import Settings
from manga_gateway.framework.antibot import Clearance
from manga_gateway.sources.comix import ComixSource

from .conftest import BASE_URL, TEST_API_KEY

# Comix's live host (pinned to live recon per D-46; ComixSource.base_url).
_COMIX = ComixSource.base_url
_MANGADEX = "https://api.mangadex.org"
_SERIES_ID = "mr3m0"  # hid (D-46) — 5-char Comix series slug
_CHAPTER_ID = "9001596"
_CF_COOKIE = {"cf_clearance": "CF-CLEAR-TOKEN"}
_CF_UA = "Mozilla/5.0 (Comix-Chrome) AppleWebKit/537.36"

# ─────────────────────────── fakes + fixtures ───────────────────────────


class _ComixSolver:
    """Fake AntiBotSolver: returns a canned ``Clearance`` AND backs the Plan
    04-04 Option A ``fetch_via_browser`` primitive with a URL→result registry.

    The browser-fetch primitive is mocked by URL: tests call
    ``solver.stage_browser_fetch(url, result)`` and the ``fetch_via_browser``
    call for that URL returns the staged value (mocking ``page.evaluate``
    against the rendered DOM without any real browser, D-42).
    """

    def __init__(self) -> None:
        self.browser_results: dict[str, object] = {}
        self.browser_fetch_calls: list[tuple[str, str, str | None]] = []

    def stage_browser_fetch(self, url: str, result: object) -> None:
        """Register the value the next ``fetch_via_browser(url, ...)`` returns."""
        self.browser_results[url] = result

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies=dict(_CF_COOKIE), user_agent=_CF_UA)

    async def fetch_via_browser(
        self,
        url: str,
        *,
        extract: str,
        wait_for: str | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 — matches the primitive contract
    ) -> object:
        # Record the call so tests can assert the source built the right URL +
        # passed an extract body (the framework primitive contract).
        _ = (extract, timeout)
        self.browser_fetch_calls.append((url, extract, wait_for))
        if url not in self.browser_results:
            raise AssertionError(
                f"unmocked solver.fetch_via_browser({url!r}); "
                f"call stage_browser_fetch first"
            )
        return self.browser_results[url]


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (3, 3), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def comix_app(tmp_path: Path) -> FastAPI:
    """App with a tmp job store + output root so the download slice is isolated."""
    return create_app(
        Settings(
            api_key=TEST_API_KEY,
            db_path=str(tmp_path / "jobs.db"),
            output_root=str(tmp_path / "out"),
        )
    )


@pytest_asyncio.fixture
async def solver() -> _ComixSolver:
    return _ComixSolver()


@pytest_asyncio.fixture
async def comix_client(
    comix_app: FastAPI, solver: _ComixSolver
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=comix_app)
    async with comix_app.router.lifespan_context(comix_app):
        # Inject the fake solver into BOTH consumption sites: the search route reads
        # app.state.solver; the download engine holds its own solver ref. No real
        # browser is ever launched (the CloudflareSolver shell stays unused).
        comix_app.state.solver = solver
        comix_app.state.job_manager._engine._solver = solver
        async with httpx.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers={"X-Api-Key": TEST_API_KEY},
        ) as ac:
            yield ac


def _mock_comix_search(solver: _ComixSolver) -> None:
    """Mock Comix search (plaintext httpx) + the series chapter-list (browser DOM).

    Search is PLAINTEXT ``/api/v1/manga`` returning ``result.items[{hid, url, …}]``.
    Chapter list (Plan 04-04 Option A) is a browser-DOM read of the series page
    ``/title/{hid}-{slug}`` — staged on the fake solver's ``fetch_via_browser``
    registry as the JSON-serializable shape ``_series_chapters`` returns,
    including the scanlation group from each row's ``<a class="mchap-row__group">``
    anchor (issue #29).
    """
    respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "items": [
                        {
                            "id": 116210,
                            "hid": _SERIES_ID,
                            "title": "Cipher Tales",
                            "latestChapter": 1,
                            "url": f"/title/{_SERIES_ID}-cipher-tales",
                            "poster": {
                                "medium": "https://static.example/m.jpg",
                                "large": "https://static.example/l.jpg",
                            },
                            "contentRating": "safe",
                            "links": {},
                        }
                    ],
                    "meta": {"total": 1, "perPage": 28, "page": 1, "lastPage": 1},
                },
            },
        )
    )
    # Browser-DOM chapter-list: the series page URL navigated by _series_chapters.
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(
        series_url,
        [
            {
                "id": _CHAPTER_ID,
                "chapter": "1",
                "lang": "en",
                "groups": [{"name": "TeamX"}],
            }
        ],
    )


def _mock_comix_pages(solver: _ComixSolver) -> None:
    """Mock the Option A browser-DOM read of the chapter HTML page + the CDN-
    pattern page images.

    Plan 04-04 Option A (2026-05-30): ``fetch_manifest`` navigates the warm
    Patchright browser to ``/title/{hid}-{slug}/{chapter_id}-chapter-{num}`` and
    reads ``/si/{token}/{NN}.{ext}`` URLs off the rendered DOM. The encrypted
    ``/api/v1/chapters/{id}`` endpoint is bypassed entirely (its ``_=`` request
    token is minted by the same VM-obfuscated ``secure-*.js`` that does
    decryption; we cannot reliably mint it statically). We stage the URL list
    on the fake solver's ``fetch_via_browser`` mock and the CDN images on
    respx.

    CDN bytes are plaintext (verified live, 04-HANDOFF): ``fetch_image`` calls
    ``ctx.get_bytes_plain`` so the framework decrypt seam is BYPASSED on the
    image path — staged image bytes go straight through to the CBZ writer.
    """
    # Match the production CDN host pattern enforced by ComixSource's
    # SSRF allowlist (`{sub}.wowpic\d+.store`) so the manifest passes the
    # framework's URL validation — the fake host shape mirrors real recon.
    page_urls = [
        f"https://test.wowpic9.store/si/TOKENXXXXXXXXXXXXXX/{i:02d}.webp"
        for i in (1, 2)
    ]
    # Composite chapter id encoded by ComixSource._make_composite_chapter_id at
    # search time: numeric_id|hid|slug|number. fetch_manifest decodes it to
    # construct the chapter HTML URL the browser navigates to.
    chapter_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales/{_CHAPTER_ID}-chapter-1"
    solver.stage_browser_fetch(chapter_url, page_urls)
    for url in page_urls:
        respx.get(url).mock(return_value=httpx.Response(200, content=_png_bytes()))


async def _poll_until(
    client: httpx.AsyncClient, job_id: str, *, timeout_s: float = 5.0
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        resp = await client.get("/downloads")
        assert resp.status_code == 200
        job = next((j for j in resp.json()["jobs"] if j["jobId"] == job_id), None)
        assert job is not None
        if job["status"] in ("completed", "failed"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not terminate within {timeout_s}s")


# ─────────────────────────── (1) search → comix: handle ──────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_comix_search_returns_releases_with_comix_handle(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    _mock_comix_search(solver)

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    releases = body["releases"]
    assert releases, f"comix returned no releases (warnings={body.get('warnings')})"  # noqa: E501
    rel = releases[0]
    assert rel["sourceKey"] == "comix"
    assert rel["guid"].startswith("comix:")
    assert rel["downloadHandle"]
    assert rel["mangaTitle"] == "Cipher Tales"


# ─────────────────────── (2) handle → completed job → CBZ ─────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_comix_slice_search_to_cbz_on_same_contract(
    comix_app: FastAPI, comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    _mock_comix_search(solver)
    _mock_comix_pages(solver)

    # search → mint a comix: handle (the SAME opaque handle Mangarr resubmits).
    search = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    handle = search.json()["releases"][0]["downloadHandle"]

    # POST /downloads with that handle → completed job → a CBZ of page images only.
    submit = await comix_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "comix", "mangaId": 7},
    )
    assert submit.status_code == 200
    job_id = submit.json()["jobId"]
    assert job_id.startswith("j_")

    job = await _poll_until(comix_client, job_id)
    assert job["status"] == "completed", job
    out = job["outputPath"]
    assert out is not None
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert len(names) == 2  # two decrypted page images
        # page-images-only: no ComicInfo.xml / metadata (PKG-04).
        assert not any(n.lower().endswith(".xml") for n in names)
        assert not any("comicinfo" in n.lower() for n in names)
        # The decrypt seam ran: each entry is a valid PNG, not ciphertext.
        for name in names:
            Image.open(io.BytesIO(zf.read(name))).verify()


# ─────────────────────── (3) outbound request carried clearance ──────────────


@respx.mock
@pytest.mark.asyncio
async def test_comix_outbound_request_carries_clearance_and_ua(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    # Search is plaintext per live recon — no decrypt needed for this assertion.
    search_route = respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "result": {"items": [], "meta": {}}},
        )
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    assert resp.status_code == 200
    assert search_route.called
    request = search_route.calls.last.request
    # D-40: the cf_clearance cookie + the EXACT captured UA are injected per request.
    assert request.headers["user-agent"] == _CF_UA
    assert "cf_clearance=CF-CLEAR-TOKEN" in request.headers.get("cookie", "")


# ─────────────────────── (4) single Comix failure → warning, others flow ──────


@respx.mock
@pytest.mark.asyncio
async def test_single_comix_failure_warns_while_mangadex_flows(
    comix_client: httpx.AsyncClient,
) -> None:
    # Comix /search fails (permanent 403 → SourceError → per-source warning), while a
    # second source (MangaDex) returns a real release — both surfaced in ONE 200 (D-38).
    respx.get(f"{_COMIX}/api/v1/manga").mock(return_value=httpx.Response(403))
    manga_id = "11111111-2222-3333-4444-555555555555"
    chap_id = "99999999-8888-7777-6666-555555555555"
    respx.get(f"{_MANGADEX}/manga").mock(
        return_value=httpx.Response(
            200,
            json={"result": "ok", "response": "collection", "data": [{"id": manga_id}]},
        )
    )
    respx.get(f"{_MANGADEX}/chapter").mock(
        return_value=httpx.Response(
            200,
            json={
                "result": "ok",
                "response": "collection",
                "data": [
                    {
                        "id": chap_id,
                        "type": "chapter",
                        "attributes": {
                            "volume": None,
                            "chapter": "1",
                            "title": None,
                            "translatedLanguage": "en",
                            "externalUrl": None,
                            "isUnavailable": False,
                            "publishAt": "2026-05-29T00:00:00+00:00",
                            "readableAt": "2026-05-29T00:00:00+00:00",
                            "pages": 1,
                        },
                        "relationships": [
                            {
                                "id": manga_id,
                                "type": "manga",
                                "attributes": {"title": {"en": "Solo Leveling"}},
                            }
                        ],
                    }
                ],
                "total": 1,
                "limit": 100,
                "offset": 0,
            },
        )
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "X", "sources": ["comix", "mangadex"]},
    )
    assert resp.status_code == 200  # one source failing never fails the request
    body = resp.json()
    # MangaDex's release still flows.
    assert any(r["sourceKey"] == "mangadex" for r in body["releases"])
    assert not any(r["sourceKey"] == "comix" for r in body["releases"])
    # Comix surfaces as a warnings[] entry (D-38/SRCH-03).
    comix_warnings = [w for w in body["warnings"] if w["sourceKey"] == "comix"]
    assert len(comix_warnings) == 1


# ─────────────── (5) Option A: fetch_manifest drives the browser ───────────────


@respx.mock
@pytest.mark.asyncio
async def test_comix_fetch_manifest_routes_through_browser(
    comix_app: FastAPI, comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """ComixSource.fetch_manifest navigates the chapter HTML page in the warm
    browser (Plan 04-04 Option A pivot) — NOT the encrypted ``/api/v1/chapters/
    {id}`` endpoint. Asserts the recon-pinned URL pattern + that the extract
    body filters CDN image URLs by the wildcarded-segment regex."""
    _mock_comix_search(solver)
    _mock_comix_pages(solver)

    # Drive a full download → manifest fetch happens during the job's RESOLVING.
    search = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    handle = search.json()["releases"][0]["downloadHandle"]
    submit = await comix_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "comix", "mangaId": 7},
    )
    job_id = submit.json()["jobId"]
    await _poll_until(comix_client, job_id)

    # Two browser fetches: (1) the series page chapter-list during search,
    # (2) the chapter HTML page during the job's RESOLVING. Encrypted-API
    # calls for chapter-list and chapter-pages are bypassed in Option A.
    chapter_calls = [c for c in solver.browser_fetch_calls if "-chapter-" in c[0]]
    assert len(chapter_calls) == 1
    fetched_url, extract_body, wait_for = chapter_calls[0]
    expected = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales/{_CHAPTER_ID}-chapter-1"
    assert fetched_url == expected
    # The extractor filters by the /{seg}/{token}/{NN}.{ext} CDN pattern,
    # where {seg} is a WILDCARDED short path segment (Comix rotates it:
    # /si/ historically, /i3/ live 2026-06-02 — debug comix-malformed-
    # manifest). The pattern is embedded as a JS regex literal, so the JS
    # source sees the segment class ``/\/[a-z0-9]{2,4}\//`` and the token
    # shape ``[A-Za-z0-9_-]{16,}``.
    assert "[a-z0-9]{2,4}" in extract_body
    assert "[A-Za-z0-9_-]{16,}" in extract_body
    assert "/si" not in extract_body
    # NN-order sort over the page-number-keyed Map, gaps preserved (Comix's
    # reader scaffolds .rpage-page[data-page=N] divs and the extractor walks
    # them with scrollIntoView to trigger the lazy loader per-page).
    assert "rpage-page" in extract_body
    assert "scrollIntoView" in extract_body
    assert "sort" in extract_body
    # Issue #45 (2026-05-31): the extractor's Step-2 walk now scrolls the
    # inner Swiper scroll container to its midpoint then its end in two
    # batched passes (head+tail), then falls back to per-missing-page
    # scrollIntoView selectively. The ancestor walk that finds the inner
    # Swiper container reads ``getComputedStyle(el).overflowY`` and
    # ``el.scrollHeight`` — both substrings MUST appear in the extractor
    # body so a future rewrite that loses the two-scroll strategy is
    # caught here, not only by the live perf test.
    assert "scrollHeight" in extract_body
    assert "overflowY" in extract_body
    # Issue #20: wait_for is now None — the extractor's own Step-1 polls for
    # the scaffold from inside ``page.evaluate``. A Python-side wait_for_selector
    # would double-wait the same condition and add ~1 s of pure overhead.
    assert wait_for is None

    # The series-page chapter-list call also happened — its URL has NO
    # ``-chapter-`` segment and its wait_for targets chapter anchors.
    series_calls = [c for c in solver.browser_fetch_calls if "-chapter-" not in c[0]]
    assert len(series_calls) == 1
    series_url, _, series_wait = series_calls[0]
    assert series_url == f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    # JS predicate (not CSS selector) — routes to page.wait_for_function and
    # polls DOM attachment of `a.mchap-row__primary` so anchors that render
    # off-screen / inside scroll containers don't trip a visibility wait.
    assert series_wait is not None
    assert "mchap-row__primary" in series_wait
    assert series_wait.startswith("() =>")


# ─── (6) scanlation group comes from the DOM extractor ────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_scanlation_group_comes_from_dom_row(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """Each chapter row's ``<a class="mchap-row__group">`` anchor carries the
    scanlation group name. The DOM extractor pulls it directly; no API
    side-channel is involved (Comix's plaintext ``/chapter-indexes`` now
    requires a JS-minted ``_=`` token and rejects unsigned calls with 403
    ``{"message":"Invalid token."}``)."""
    respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "items": [
                        {
                            "id": 116210,
                            "hid": _SERIES_ID,
                            "title": "Cipher Tales",
                            "url": f"/title/{_SERIES_ID}-cipher-tales",
                        }
                    ]
                },
            },
        )
    )
    # No chapter-indexes mock: the source must NOT call it. respx would raise
    # AllMockedAssertionError on an unmocked call, which is the assertion.
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(
        series_url,
        [
            {
                "id": _CHAPTER_ID,
                "chapter": "1",
                "lang": "en",
                "groups": [{"name": "Thunderscans"}],
            }
        ],
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    releases = body["releases"]
    assert len(releases) == 1
    assert releases[0]["scanlationGroup"] == "Thunderscans"
    # No degraded-path warning is emitted — the group came straight off the DOM.
    codes = [w["code"] for w in body.get("warnings", []) if w["sourceKey"] == "comix"]
    assert "scanlation_group_unavailable" not in codes


@respx.mock
@pytest.mark.asyncio
async def test_missing_dom_group_yields_null_scanlation_group(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """If the chapter row carries no group anchor, ``scanlationGroup`` is
    ``null`` — degraded but not fatal. No warning is emitted because no side
    channel was attempted."""
    respx.get(f"{_COMIX}/api/v1/manga").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "items": [
                        {
                            "id": 116210,
                            "hid": _SERIES_ID,
                            "title": "Cipher Tales",
                            "url": f"/title/{_SERIES_ID}-cipher-tales",
                        }
                    ]
                },
            },
        )
    )
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(
        series_url,
        [{"id": _CHAPTER_ID, "chapter": "1", "lang": "en", "groups": []}],
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["releases"]) == 1
    assert body["releases"][0]["scanlationGroup"] is None
    codes = [w["code"] for w in body.get("warnings", []) if w["sourceKey"] == "comix"]
    assert "scanlation_group_unavailable" not in codes

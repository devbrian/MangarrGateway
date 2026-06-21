"""E2E Comix slice: search→handle→cleared fetch→CBZ (04-03 Task 2).

Drives the SAME search→handle→download→package contract proven on MangaDex, but for a
``cloudflare+encrypted`` source — entirely deterministic (D-42): a fake solver supplies
a canned ``Clearance`` (no real Patchright browser) and respx intercepts the ONE shared
httpx client (no real Comix site).

What is exercised end-to-end:
* clearance injection — the outbound Comix IMAGE request carries ``cf_clearance`` +
  the exact captured UA (D-40);
* the handle/job contract — a ``comix:`` handle flows through POST /downloads to a
  ``completed`` job + a CBZ of page-images-only (criterion #3);
* Phase 14 in-WebView resolution — ``solver.eval_in_webview`` returns the search
  candidates / chapter list / page-URL list off the redroid WebView (no real device);
* per-source isolation — a single failing Comix /search yields 200 + a comix warning
  while MangaDex's releases still flow (D-38/SRCH-03).

Issue #46: the ``comix-v1`` browser-evaluated decrypt seam was removed (dead code on
the current ``secure-*.js``). Comix has no live encrypted-response path — the slice
runs search/chapter-list/chapter-pages via the in-WebView eval seam and the image
bytes over plaintext httpx (``get_bytes_plain``); the fake solver needs no ``decrypt``.
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
from manga_gateway.framework.errors import SourceError
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
    """Fake AntiBotSolver: returns a canned ``Clearance`` AND backs the Phase 14
    ``eval_in_webview`` seam with a URL→result registry.

    The eval seam is mocked by nav target: tests call
    ``solver.stage_browser_fetch(url, result)`` (series-page chapter-list and
    chapter-pages reads) or ``solver.stage_search(envelope)`` (the homepage
    ``c.list`` search token-mint), and the ``eval_in_webview`` call for that URL
    returns the staged value — mocking the in-WebView eval without any device.

    ``browser_fetch_calls`` records ONLY the ``/title/`` evals (series + chapter
    navs); the homepage search eval lands in ``search_eval_calls`` so the
    nav-count assertions stay separable.
    """

    def __init__(self) -> None:
        self.browser_results: dict[str, object] = {}
        self.browser_fetch_calls: list[tuple[str, str, str | None]] = []
        self.search_eval_calls: list[tuple[str, str, str | None]] = []
        self.eval_errors: dict[str, Exception] = {}
        # Vestigial: comix no longer paginates (the eval enumerates the whole list
        # in one call). Kept so the ``paginated_fetch_calls == []`` regression
        # assertions still read meaningfully (it is never appended to).
        self.paginated_fetch_calls: list[object] = []

    def stage_browser_fetch(self, url: str, result: object) -> None:
        """Register the value the next ``eval_in_webview(url, ...)`` returns."""
        self.browser_results[url] = result

    def stage_search(self, envelope: object) -> None:
        """Register the search candidates envelope the homepage ``c.list`` eval
        returns (Phase 14: search mints + decrypts in-WebView, no httpx replay)."""
        self.browser_results[f"{_COMIX}/"] = envelope

    def stage_error(self, url: str, exc: Exception) -> None:
        """Make the ``eval_in_webview`` for ``url`` raise (simulates a per-source
        failure → warning) instead of returning a staged value."""
        self.eval_errors[url] = exc

    async def get_clearance(self, source_key: str) -> Clearance:
        return Clearance(cookies=dict(_CF_COOKIE), user_agent=_CF_UA)

    async def eval_in_webview(
        self,
        challenge_url: str,
        js: str,
        *,
        wait_for: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — matches the seam contract
    ) -> object:
        # Record the call so tests can assert the source built the right URL +
        # passed the right extract JS / wait_for (the seam contract). The homepage
        # search eval is recorded separately from the ``/title/`` enumeration evals.
        _ = timeout
        if "/title/" in challenge_url:
            self.browser_fetch_calls.append((challenge_url, js, wait_for))
        else:
            self.search_eval_calls.append((challenge_url, js, wait_for))
        if challenge_url in self.eval_errors:
            raise self.eval_errors[challenge_url]
        if challenge_url not in self.browser_results:
            raise AssertionError(
                f"unmocked solver.eval_in_webview({challenge_url!r}); "
                f"call stage_browser_fetch / stage_search first"
            )
        return self.browser_results[challenge_url]


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
            handle_db_path=str(tmp_path / "handles.db"),
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
    """Mock Comix search (homepage ``c.list`` eval) + the series chapter-list eval.

    Phase 14: search runs the env-module ``c.list({keyword})`` eval on the homepage
    and gets the decrypted ``result.items[{hid, url, …}]`` envelope back directly —
    staged via ``stage_search``. The chapter list is enumerated via comix's own
    internal ``chapters()`` loader run through ``eval_in_webview`` on the series page
    ``/title/{hid}-{slug}`` — staged on the eval registry as the JSON-serializable
    shape ``_series_chapters`` returns, including the scanlation group from each row.
    """
    solver.stage_search(
        {
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
        }
    )
    # Chapter-list: the series page URL is enumerated via comix's own internal
    # chapters() loader run through eval_in_webview (spike 019/021), so stage it on
    # the eval registry (keyed by series URL). The chapter-pages manifest read also
    # rides eval_in_webview, keyed by its distinct chapter URL.
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

    Phase 14: ``fetch_manifest`` runs comix's OWN internal ``chapters/{id}`` loader
    in-WebView via ``eval_in_webview`` against
    ``/title/{hid}-{slug}/{chapter_id}-chapter-{num}`` and gets the decrypted
    ``pages.items[].url`` list back. We stage that URL list on the fake solver's
    ``eval_in_webview`` registry (keyed by the chapter URL) and the CDN images on
    respx (the image BYTES still fetch over httpx — CLAUDE.md).

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
    # Phase 14: comix's only remaining httpx leg is the IMAGE-bytes fetch (search +
    # chapter-list + chapter-pages all run in-WebView via eval_in_webview). D-40
    # still injects the cf_clearance cookie + the EXACT captured UA on that image
    # leg — assert it on the page-image CDN request driven by a real download.
    _mock_comix_search(solver)
    page_urls = [
        f"https://test.wowpic9.store/si/TOKENXXXXXXXXXXXXXX/{i:02d}.webp"
        for i in (1, 2)
    ]
    chapter_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales/{_CHAPTER_ID}-chapter-1"
    solver.stage_browser_fetch(chapter_url, page_urls)
    image_routes = [
        respx.get(u).mock(return_value=httpx.Response(200, content=_png_bytes()))
        for u in page_urls
    ]

    search = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    handle = search.json()["releases"][0]["downloadHandle"]
    submit = await comix_client.post(
        "/downloads",
        json={"releaseHandle": handle, "sourceKey": "comix", "mangaId": 7},
    )
    job = await _poll_until(comix_client, submit.json()["jobId"])
    assert job["status"] == "completed", job

    # D-40: every image-CDN request carried the cf_clearance cookie + captured UA.
    assert all(r.called for r in image_routes)
    request = image_routes[0].calls.last.request
    assert request.headers["user-agent"] == _CF_UA
    assert "cf_clearance=CF-CLEAR-TOKEN" in request.headers.get("cookie", "")


# ─────────────────────── (4) single Comix failure → warning, others flow ──────


@respx.mock
@pytest.mark.asyncio
async def test_single_comix_failure_warns_while_mangadex_flows(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    # Comix /search fails (the in-WebView search eval raises → SourceError →
    # per-source warning), while a second source (MangaDex) returns a real release —
    # both surfaced in ONE 200 (D-38).
    solver.stage_error(
        f"{_COMIX}/", SourceError("source_unavailable", "comix search eval failed")
    )
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
    # Spike 019 (debug comix-cdn-scheme-rotation): the chapter-pages read runs
    # comix's OWN internal ``chapters/{id}`` API loader in-page instead of scraping
    # the lazy reader DOM. It discovers the env-*.js module at runtime, imports it,
    # finds the axios instance, reads the chapter id from the path, and GETs
    # ``/chapters/{id}`` → decrypted ``pages.items[].url``. Assert the load-bearing
    # API-read markers are present (so a regression back to DOM scraping is caught
    # offline, not only by the live nightly).
    assert "import(" in extract_body
    assert "/chapters/" in extract_body
    assert ".get(" in extract_body
    assert "pages" in extract_body
    assert "items" in extract_body
    # The chapter numeric id is read from the URL path /{id}-chapter-{number}.
    assert "-chapter-" in extract_body
    # The retired lazy-DOM scrape markers must be gone (no scrollIntoView walk, no
    # filename-number synthesis, no scaffold-counter heuristic) — comix's per-page
    # opaque-token CDN URLs broke all three (debug comix-cdn-scheme-rotation).
    assert ".scrollIntoView(" not in extract_body
    assert "padStart" not in extract_body
    assert "authTotal" not in extract_body
    # wait_for is a JS boolean predicate (Plan 01 contract — NOT a CSS selector)
    # gated on the reader scaffold: it guarantees the SPA's API module is loaded +
    # interceptors wired before the extract import()s it (the extract reads the API,
    # not the rendered <img>s).
    assert wait_for is not None
    assert wait_for.startswith("() =>")
    assert ".rpage-page[data-page]" in wait_for

    # The series-page chapter-list enumeration runs comix's OWN internal
    # ``chapters(hid, {limit:100})`` loader in-WebView (spike 019/021), so it rides
    # the ``eval_in_webview`` seam (recorded on ``browser_fetch_calls``, keyed by the
    # series URL) — the retired paginated Next-walk is never used.
    assert solver.paginated_fetch_calls == []
    series_calls = [c for c in solver.browser_fetch_calls if "-chapter-" not in c[0]]
    assert len(series_calls) == 1
    series_url, series_extract, series_wait = series_calls[0]
    assert series_url == f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    # JS predicate (not CSS selector) — routes to page.wait_for_function and polls
    # DOM attachment of `a.mchap-row__primary`, which guarantees the SPA has booted
    # and its API ES module is loaded (interceptors wired) before the extract
    # ``import()``s it.
    assert series_wait is not None
    assert "mchap-row__primary" in series_wait
    assert series_wait.startswith("() =>")
    # The comix-side spike-019 literals live in the extract JS (the framework stays
    # source-agnostic): runtime env-*.js discovery, the dynamic import, and the
    # internal ``chapters()`` call at ``LIMIT = 100``.
    assert "env-" in series_extract
    assert "import(" in series_extract
    assert ".chapters(" in series_extract
    assert "LIMIT = 100" in series_extract


# ─── (5b) chapter-list extractor reads the per-row likes span ─────────────────


def test_chapter_list_api_extract_js_maps_votes_to_likes() -> None:
    """The chapter-list extractor runs comix's own internal ``chapters()`` loader
    (spike 019) — discovered via a runtime ``env-*.js`` import — and maps the API
    row's ``votes`` → ``likes`` so per-chapter likes flow into ``Release.votes``
    (REL-03)."""
    from manga_gateway.sources.comix import _CHAPTER_LIST_API_EXTRACT_JS

    assert "import(" in _CHAPTER_LIST_API_EXTRACT_JS
    assert ".chapters(" in _CHAPTER_LIST_API_EXTRACT_JS
    assert "votes" in _CHAPTER_LIST_API_EXTRACT_JS
    assert "likes" in _CHAPTER_LIST_API_EXTRACT_JS


def test_chapter_list_api_extract_js_retries_only_axios_timeouts() -> None:
    """The chapter-list extractor wraps its per-page ``api.chapters()`` calls in a
    timeout-ONLY retry guard. Comix's own axios instance bakes in a 15s timeout we
    can't set; an intermittently-slow comix backend throws ``AxiosError: timeout of
    15000ms exceeded`` (``e.code === 'ECONNABORTED'``) on a cold (uncached) page
    fetch → ``BrowserFetchError`` → the fail-closed read returns 0 releases
    (``source_unavailable: timed out``, issue #281). The fix retries that transient
    class with a small backoff while keeping every OTHER error fail-closed. This
    asserts the guard exists so a future edit can't silently drop it."""
    from manga_gateway.sources.comix import _CHAPTER_LIST_API_EXTRACT_JS as js

    # The bounded retry helper + its timeout-only detection must be present.
    assert "withTimeoutRetry" in js
    assert "ECONNABORTED" in js
    assert "/timeout/i" in js
    # The per-page call must route THROUGH the retry helper (not call api directly).
    assert "withTimeoutRetry(() =>" in js
    # Bounded: it must not retry forever.
    assert "MAX_RETRIES" in js
    # Fail-closed for non-timeout errors: the catch still re-throws.
    assert "throw e" in js


def test_chapter_pages_api_extract_js_retries_only_axios_timeouts() -> None:
    """Parity with the chapter-list extractor: the chapter-PAGES (manifest/download)
    extractor calls the SAME in-page axios instance (``ax.get('/chapters/' + id)``),
    so it is subject to the identical 15s comix timeout. It wraps that call in the
    same timeout-only retry guard for download robustness, keeping fail-closed
    semantics for every non-timeout error."""
    from manga_gateway.sources.comix import _CHAPTER_PAGES_API_EXTRACT_JS as js

    assert "withTimeoutRetry" in js
    assert "ECONNABORTED" in js
    assert "/timeout/i" in js
    assert "withTimeoutRetry(() => ax.get(" in js
    assert "MAX_RETRIES" in js
    assert "throw e" in js


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
    solver.stage_search(
        {
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
        }
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
async def test_dom_row_likes_become_release_votes(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """A staged chapter row carrying ``likes`` surfaces as ``votes`` on the
    /search release (REL-03)."""
    solver.stage_search(
        {
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
        }
    )
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(
        series_url,
        [
            {
                "id": _CHAPTER_ID,
                "chapter": "1",
                "lang": "en",
                "groups": [{"name": "Thunderscans"}],
                "likes": 27,
            }
        ],
    )

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    releases = resp.json()["releases"]
    assert len(releases) == 1
    assert releases[0]["votes"] == 27


@respx.mock
@pytest.mark.asyncio
async def test_missing_dom_group_yields_null_scanlation_group(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """If the chapter row carries no group anchor, ``scanlationGroup`` is
    ``null`` — degraded but not fatal. No warning is emitted because no side
    channel was attempted."""
    solver.stage_search(
        {
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
        }
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


# ─── (#146) always-walk: a low chapter only on a later page is enumerated ──────


@respx.mock
@pytest.mark.asyncio
async def test_comix_search_walks_full_list_finds_low_chapter(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """#146 regression: ``_series_chapters`` ALWAYS walks the FULL paginated
    chapter list. A low chapter (#5) that — before the fix — only rendered on a
    LATER pagination page (never enumerated by the one-shot first-paint read) is
    now present and findable.

    The series is staged with a ~3-page-worth full list (30 chapters) where
    chapter ``5`` sits deep in the list. The fake returns the FULL merged list the
    in-page ``chapters()`` loader enumerates (spike 019). A ``type=chapter`` search
    for chapter 5 (the 260606-2ff filter) must return exactly that chapter —
    proving it survived the full enumeration."""
    solver.stage_search(
        {
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
        }
    )
    # 30 chapters; chapter 5 lives deep in the list (it would render only on a
    # later pagination page on the live site — the #146 failure mode). The fake
    # one-shot primitive returns the FULL merged list the in-page loader yields.
    full_list = [
        {
            "id": f"chap-{n}",
            "chapter": str(n),
            "lang": "en",
            "groups": [{"name": "TeamX"}],
        }
        for n in range(30, 0, -1)
    ]
    assert any(c["chapter"] == "5" for c in full_list)
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(series_url, full_list)

    resp = await comix_client.post(
        "/search",
        json={
            "type": "chapter",
            "query": "Cipher",
            "chapter": 5,
            "sources": ["comix"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The 260606-2ff filter narrows to the chapter-5 family — exactly one
    # release, proving #5 was enumerated from the FULL walked list.
    releases = body["releases"]
    assert [r["chapterNumber"] for r in releases] == [5]
    # And the enumeration went through the eval_in_webview seam (spike 019/021),
    # keyed by the series URL — the retired paginated Next-walk is never used.
    assert solver.paginated_fetch_calls == []
    series_calls = [c for c in solver.browser_fetch_calls if "-chapter-" not in c[0]]
    assert len(series_calls) == 1
    assert series_calls[0][0] == series_url


# ─── (debug comix-page-walker-100-cap) a >100-chapter series honors req.limit ──


@respx.mock
@pytest.mark.asyncio
async def test_comix_search_returns_more_than_100_when_limit_is_higher(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """Regression (debug comix-page-walker-100-cap): a comix series with MORE
    than 100 chapters must return more than 100 releases when the caller asks
    for a higher ``limit``.

    Before the fix ``search()`` clamped its per-series output window to
    ``min(req.limit, _MAX_FEED_LIMIT)`` — and ``_MAX_FEED_LIMIT`` is the
    per-PAGE upstream fetch ceiling (the ``route_limit_rewrite`` target), NOT a
    result cap. Conflating the two capped EVERY comix search at 100 chapters no
    matter how many the series had or how high ``req.limit`` was. The canonical
    ``req.limit`` truncation lives at the route (``api/routes/search.py``:
    ``releases[: req.limit]``), exactly as MangaDex relies on — the source must
    NOT pre-clamp the window to the fetch ceiling.

    The fake paginated primitive returns the FULL walked list (150 chapters);
    the route rewrite stays pinned at the 100/page fetch ceiling (asserted), so
    the only behaviour under test is that the per-series window honours
    ``req.limit`` rather than the fetch ceiling.
    """
    solver.stage_search(
        {
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
        }
    )
    # 150 chapters, newest-first — more than the 100/page fetch ceiling.
    full_list = [
        {
            "id": f"chap-{n}",
            "chapter": str(n),
            "lang": "en",
            "groups": [{"name": "TeamX"}],
        }
        for n in range(150, 0, -1)
    ]
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(series_url, full_list)

    resp = await comix_client.post(
        "/search",
        json={
            "type": "chapter",
            "query": "Cipher",
            "sources": ["comix"],
            "limit": 150,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    releases = body["releases"]
    # The cap is gone: all 150 chapters flow through (route truncation is 150).
    assert len(releases) == 150, (
        f"expected 150 releases for a 150-chapter series at limit=150, "
        f"got {len(releases)} (warnings={body.get('warnings')})"
    )
    # The in-page chapters() loader (spike 019/021) rides the eval_in_webview seam
    # — the per-series result window is decoupled from the upstream page size, and
    # the retired paginated Next-walk is never used.
    assert solver.paginated_fetch_calls == []
    series_calls = [c for c in solver.browser_fetch_calls if "-chapter-" not in c[0]]
    assert len(series_calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_comix_search_default_limit_still_truncates_at_route(
    comix_client: httpx.AsyncClient, solver: _ComixSolver
) -> None:
    """The route — not the source — owns ``req.limit`` truncation. With the
    default ``limit`` (50) a 150-chapter series yields exactly 50 releases,
    proving the source returns its req.limit-wide window and the route's
    ``releases[: req.limit]`` makes the final cut (mirrors MangaDex)."""
    solver.stage_search(
        {
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
        }
    )
    full_list = [
        {"id": f"chap-{n}", "chapter": str(n), "lang": "en", "groups": []}
        for n in range(150, 0, -1)
    ]
    series_url = f"{_COMIX}/title/{_SERIES_ID}-cipher-tales"
    solver.stage_browser_fetch(series_url, full_list)

    resp = await comix_client.post(
        "/search",
        json={"type": "chapter", "query": "Cipher", "sources": ["comix"]},
    )
    assert resp.status_code == 200, resp.text
    releases = resp.json()["releases"]
    # Default req.limit is 50 — the route truncates the 150-wide window to 50.
    assert len(releases) == 50, f"expected default-limit 50, got {len(releases)}"

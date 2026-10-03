"""AndroidSolvePipeline ``/fetch-images`` driven over a SCRIPTED CDP socket.

261003-mangaball-webview-images (Refs #378). Only ``service.time`` is faked — the
real ``cdp_call`` / ``cdp_call_collecting`` framing runs against ``ScriptedWs``, which
interleaves ``Network.*`` events before each batch ``Runtime.evaluate`` response.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import pytest
from android_solver.service import AndroidSolvePipeline, SolveError

from android_solver import service

_PAGE = "https://mangaball.com/robots.txt"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeDevice:
    def __init__(self) -> None:
        self.removed_forwards: list[int] = []

    def connect(self) -> None:
        return None

    def pidof(self) -> int:
        return 4321

    def forward_devtools(self, pid: int) -> int:
        return 9222

    def remove_forward(self, local_port: int | None = None) -> None:
        self.removed_forwards.append(local_port if local_port is not None else 9222)


class ScriptedWs:
    """Answers CDP requests; batch evaluates emit Network.* events first."""

    def __init__(
        self,
        *,
        statuses: dict[str, int],
        bodies: dict[str, bytes],
        ready: list[bool],
        fail_method: str | None = None,
    ) -> None:
        self.statuses = statuses
        self.bodies = bodies
        self.ready = ready  # successive answers to the ready predicate (last repeats)
        self.fail_method = fail_method
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.queue: list[str] = []
        self.closed = False
        self._rid: dict[str, str] = {}

    def _push(self, frame: dict[str, Any]) -> None:
        self.queue.append(json.dumps(frame))

    def send(self, payload: str) -> None:
        msg = json.loads(payload)
        method, params, cid = msg["method"], msg.get("params", {}), msg["id"]
        self.sent.append((method, params))
        if method == self.fail_method:
            raise OSError("socket dropped")
        result: dict[str, Any] = {}
        if method == "Runtime.evaluate" and cid == service._FETCH_READY_CMD_ID:
            value = self.ready.pop(0) if len(self.ready) > 1 else self.ready[0]
            result = {"result": {"value": value}}
        elif method == "Runtime.evaluate" and cid == service._FETCH_BATCH_CMD_ID:
            batch = json.loads(
                re.search(r"Promise\.all\((\[.*?\])\.map", params["expression"]).group(
                    1
                )
            )  # type: ignore[union-attr]
            for url in batch:
                if url not in self.statuses:
                    continue
                rid = f"r{len(self._rid)}"
                self._rid[rid] = url
                self._push(
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {"requestId": rid, "request": {"url": url}},
                    }
                )
                self._push(
                    {
                        "method": "Network.requestWillBeSentExtraInfo",
                        "params": {"requestId": rid},
                    }
                )
                self._push(
                    {
                        "method": "Network.responseReceived",
                        "params": {
                            "requestId": rid,
                            "response": {"status": self.statuses[url]},
                        },
                    }
                )
            result = {"result": {"value": [True] * len(batch)}}
        elif method == "Network.getResponseBody":
            data = self.bodies[self._rid[params["requestId"]]]
            result = {"body": base64.b64encode(data).decode(), "base64Encoded": True}
        self._push({"id": cid, "result": result})

    def recv(self) -> str:
        return self.queue.pop(0)

    def close(self) -> None:
        self.closed = True


def _pipeline(
    monkeypatch: pytest.MonkeyPatch, device: FakeDevice, ws: ScriptedWs
) -> AndroidSolvePipeline:
    targets = b'[{"type":"page","webSocketDebuggerUrl":"ws://localhost:9222/p"}]'
    monkeypatch.setattr(service, "time", FakeClock())
    return AndroidSolvePipeline(
        device,  # type: ignore[arg-type]
        timeout_s=60.0,
        ws_factory=lambda url, *, timeout: ws,
        http_get=lambda url, *, timeout: targets,
        launch_settle_s=0.0,
        poll_interval_s=1.0,
    )


def _url(n: int) -> str:
    return f"https://a.poke-black-and-white.net/storage/{n}.webp"


def _methods(ws: ScriptedWs) -> list[str]:
    return [m for m, _ in ws.sent]


def test_already_on_page_batches_and_maps_results_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [_url(i) for i in range(5)]
    statuses = {urls[0]: 200, urls[1]: 403, urls[3]: 200, urls[4]: 200}
    bodies = {u: f"img{i}".encode() for i, u in enumerate(urls)}
    ws = ScriptedWs(statuses=statuses, bodies=bodies, ready=[True])
    device = FakeDevice()

    results = _pipeline(monkeypatch, device, ws).fetch_images(
        _PAGE, "mangaball.com", urls
    )

    assert "Page.navigate" not in _methods(ws)
    assert ("Network.setCacheDisabled", {"cacheDisabled": True}) in ws.sent
    batches = [
        p for m, p in ws.sent if m == "Runtime.evaluate" and "Promise.all" in str(p)
    ]
    assert len(batches) == 2  # 4 + 1
    assert [r["url"] for r in results] == urls
    assert base64.b64decode(results[0]["body_b64"]) == b"img0"
    assert results[1] == {"url": urls[1], "status": 403, "error": "http status"}
    assert results[2] == {
        "url": urls[2],
        "status": None,
        "error": "no network request observed",
    }
    assert base64.b64decode(results[4]["body_b64"]) == b"img4"
    # The 403 never gets a getResponseBody; the three 200s do.
    assert _methods(ws).count("Network.getResponseBody") == 3
    assert ws.closed
    assert device.removed_forwards == [9222]


def test_navigates_once_then_polls_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    url = _url(1)
    ws = ScriptedWs(statuses={url: 200}, bodies={url: b"x"}, ready=[False, False, True])
    results = _pipeline(monkeypatch, FakeDevice(), ws).fetch_images(
        _PAGE, "mangaball.com", [url]
    )
    navs = [p for m, p in ws.sent if m == "Page.navigate"]
    assert navs == [{"url": _PAGE}]
    assert results[0]["body_b64"] == base64.b64encode(b"x").decode()


def test_page_never_ready_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = ScriptedWs(statuses={}, bodies={}, ready=[False])
    device = FakeDevice()
    with pytest.raises(SolveError):
        _pipeline(monkeypatch, device, ws).fetch_images(
            _PAGE, "mangaball.com", [_url(1)]
        )
    assert ws.closed
    assert device.removed_forwards == [9222]


def test_cdp_failure_still_tears_down(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = ScriptedWs(statuses={}, bodies={}, ready=[True], fail_method="Network.enable")
    device = FakeDevice()
    with pytest.raises(OSError):
        _pipeline(monkeypatch, device, ws).fetch_images(
            _PAGE, "mangaball.com", [_url(1)]
        )
    assert ws.closed
    assert device.removed_forwards == [9222]

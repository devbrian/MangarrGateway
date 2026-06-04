---
name: live-conftest-solver-kwargs-mirror-drift
description: "tests/live/conftest.py _build_session_solver_kwargs is a hand-maintained mirror of app.py's solver_kwargs and silently drifts"
metadata: 
  node_type: memory
  type: project
  originSessionId: af333d26-6597-4d1c-b8d8-66ee9d8b457e
---

`tests/live/conftest.py::_build_session_solver_kwargs()` rebuilds the `CloudflareSolver` kwargs as a hand-maintained mirror of the `app.py` lifespan's `solver_kwargs` dict (it builds the ONE session-shared solver every live test reuses). It is NOT derived from app.py — it duplicates the field list.

**Why it matters:** when you add a new `CloudflareSolver(...)` kwarg in the app.py lifespan, the live harness will keep building the solver WITHOUT it unless you also update this mirror. The httpx leg (per-test `HttpxTransport` via `create_app`) DOES pick up settings, so a new solver knob can silently apply to httpx-but-not-the-browser in live tests — e.g. the #65 proxy work would have cleared CF from the host IP while httpx egressed through the proxy (split egress, the exact failure the feature guards against), producing a misleading live result.

**How to apply:** any time you add/change a kwarg passed to `CloudflareSolver(**solver_kwargs)` in `app.py`'s lifespan, mirror the same gate into `_build_session_solver_kwargs` in the same change. Reads env-backed `Settings` only — never put a credential literal there. Related: [[run-live-tests-locally-before-push]], [[cloudflare-engine-default-patchright]].

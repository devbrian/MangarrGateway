"""android-solver sidecar package.

A STANDALONE service (its own container) that drives a redroid Android-in-Docker
WebView via adb + CDP to mint a Cloudflare ``cf_clearance`` for the strict
Turnstile sources (mangadot.net, kagane.to) that desktop-browser automation
provably cannot clear from Linux. Proven end-to-end in debug session
``mangadot-cf-linux-fingerprint`` (EXP27).

R1 INVARIANT: this package is SIDECAR-ONLY. The gateway process
(``src/manga_gateway``) MUST NEVER import ``android_solver`` — all adb/Android
machinery stays out of process. The gateway reaches the solver over its HTTP
control API (added in 10-02), not by importing this code.
"""

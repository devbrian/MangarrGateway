# ruff: noqa: E501 -- the embedded reverse-engineered constant tables below are
# intrinsically multi-KB single-line base64 blobs that cannot be wrapped without
# corrupting the data; file-level E501 suppression is the correct idiom for a
# vendored constant table (do NOT chunk/rewrite the base64). All other lint rules
# stay active.
"""MangaFire search ``vrf`` token generator (pure Python, reverse-engineered).

WHAT THIS IS
------------
A stdlib-only (``base64`` + ``urllib.parse``) reimplementation of the per-keyword
``vrf`` token that MangaFire's debounced in-page search handler appends to its
search requests::

    GET https://mangafire.to/ajax/manga/search?keyword=<kw>&vrf=<compute_vrf(kw)>
    GET https://mangafire.to/filter?keyword=<kw>&...&vrf=<compute_vrf(kw)>

The SAME token validates on both ``/ajax/manga/search`` and ``/filter``, so the
gateway computes it in-process and goes straight to httpx — no browser, no
real-keyboard typed nav, no per-query cache. ``compute_vrf`` is a pure function of
the keyword: deterministic, side-effect-free, zero networking.

Pipeline (mirrors the site's minified handler)::

    encodeURIComponent(keyword)            # urllib.parse.quote(kw, safe="!'()*")
      -> latin-1 encode
      -> 5 x [ RC4(fixed 32B key) + diffuse(per-byte sbox + 32B XOR key + salt) ]
      -> base64url (no padding)

BUILD-HASH TIE (keyword-only inputs)
------------------------------------
The embedded constants (``_RC4`` keys, ``_SALT`` prefixes, ``_KEY`` XOR keys,
``_OPT`` per-round substitution tables, ``_SL`` salt lengths) were extracted from
the site build ``scripts.js?69b68df23`` and are baked into that build. They are NOT
secrets — they derive only from the public, site-published JS — and the token
depends ONLY on the keyword. Corpus-validated 91/91 byte-exact incl. unicode.

BUILD-ROTATION RECIPE (when tokens stop validating)
---------------------------------------------------
If MangaFire ships a new build hash and ``compute_vrf`` tokens stop returning real
results from the live ``/filter``, re-extract the constants via the checked-in CDP
recipe ``scripts/mangafire_vrf_extract.py`` (run it headed; it prints the refreshed
``_RC4``/``_SALT``/``_KEY``/``_OPT``/``_SL`` blocks to paste back into this module):

  1. Drive a headed patchright Chromium to ``https://mangafire.to/home`` and wait
     for jQuery + the ``input[name=keyword]`` search box.
  2. ``DOMDebugger.setXHRBreakpoint`` on ``/ajax/manga/search``, then fire the
     search handler (set the keyword input value + trigger ``input``/``keyup``).
  3. On ``Debugger.paused``, read the search handler ``t[6].default.et`` and walk
     its closure via the ``[[Scopes]]`` ``Runtime.getProperties`` chain to recover
     the named key/salt closures and the round functions.
  4. ``Runtime.callFunctionOn`` each key/salt fn to dump its bytes, and probe the
     per-round op-tables by calling each round fn with single-byte inputs.

CRITICAL GOTCHA (record verbatim): patchright runs ``page.evaluate`` in an ISOLATED
world, so the in-page closure is INVISIBLE there — the closure MUST be read via CDP
``Runtime.evaluate`` / ``Debugger.evaluateOnCallFrame`` in the MAIN world.
"""

import base64
import urllib.parse

_RC4 = {
    "r1": "FgxyJUQDPUGSzwbAq/ToWn4/e8jYzvabE+dLMb1XU1o=",
    "L1": "CQx3CLwswJAnM1VxOqX+y+f3eUns03ulxv8Z+0gUyik=",
    "M": "fAS+otFLkKsKAJzu3yU+rGOlbbFVq+u+LaS6+s1eCJs=",
    "t1": "Oy45fQVK9kq9019+VysXVlz1F9S1YwYKgXyzGlZrijo=",
    "n1": "aoDIdXezm2l3HrcnQdkPJTDT8+W6mcl2/02ewBHfPzg=",
}
_SALT = {
    "q": "l9PavRg=",
    "I": "Ml2v7ag1Jg==",
    "V": "i/Va0UxrbMo=",
    "N": "WFjKAHGEkQM=",
    "T": "5Rr27rWd",
}
_KEY = {
    "q": "yH6MXnMEcDVWO/9a6P9W92BAh1eRLVFxFlWTHUqQ474=",
    "I": "RK7y4dZ0azs9Uqz+bbFB46Bx2K9EHg74ndxknY9uknA=",
    "V": "rqr9HeTQOg8TlFiIGZpJaxcvAaKHwMwrkqojJCpcvoc=",
    "N": "/4GPpmZXYpn5RpkP7FC/dt8SXz7W30nUZTe8wb+3xmU=",
    "T": "wsSGSBXKWA9q1oDJpjtJddVxH+evCfL5SO9HZnUDFU8=",
}
_OPT = {
    "q": "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",
    "I": "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",
    "V": "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",
    "N": "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",
    "T": "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",
}
_SL = {"q": 5, "I": 7, "V": 8, "N": 8, "T": 6}


def _b(s: str) -> bytes:
    return base64.b64decode(s)


RC4_KEYS: dict[str, bytes] = {k: _b(v) for k, v in _RC4.items()}
SALT: dict[str, bytes] = {k: _b(v) for k, v in _SALT.items()}
KEY: dict[str, bytes] = {k: _b(v) for k, v in _KEY.items()}


def _unpack_table(s: str) -> list[list[int]]:
    raw = _b(s)
    return [list(raw[i * 256 : (i + 1) * 256]) for i in range(10)]


OPT: dict[str, list[list[int]]] = {k: _unpack_table(v) for k, v in _OPT.items()}
SL: dict[str, int] = _SL


def _rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    kl = len(key)
    for i in range(256):
        j = (j + S[i] + key[i % kl]) % 256
        S[i], S[j] = S[j], S[i]
    i = j = 0
    out = bytearray()
    for c in data:
        i = (i + 1) % 256
        j = (j + S[i]) % 256
        S[i], S[j] = S[j], S[i]
        out.append(c ^ S[(S[i] + S[j]) % 256])
    return bytes(out)


def _diffuse(data: bytes, rn: str) -> bytes:
    salt = SALT[rn]
    key = KEY[rn]
    opt = OPT[rn]
    sl = SL[rn]
    kl = len(key)
    out = bytearray()
    for i, ch in enumerate(data):
        if i < sl:
            out.append(salt[i])
        out.append(opt[i % 10][ch ^ key[i % kl]])
    return bytes(out)


_PIPELINE: list[tuple[str, str]] = [
    ("r1", "q"),
    ("L1", "I"),
    ("M", "V"),
    ("t1", "N"),
    ("n1", "T"),
]


def compute_vrf(keyword: str) -> str:
    s = urllib.parse.quote(keyword, safe="!'()*")  # encodeURIComponent
    x = s.encode("latin-1")
    for rc4_key, rnd in _PIPELINE:
        x = _rc4(RC4_KEYS[rc4_key], x)
        x = _diffuse(x, rnd)
    return base64.b64encode(x).decode().replace("+", "-").replace("/", "_").rstrip("=")


if __name__ == "__main__":
    import sys

    for kw in sys.argv[1:] or ["naruto", "blue lock"]:
        print(repr(kw), "->", compute_vrf(kw))

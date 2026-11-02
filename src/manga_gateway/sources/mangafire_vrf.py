"""MangaFire ``vrf`` signer — the site's own VM chunk run in QuickJS (261101-r4c).

MangaFire's protected ``/api/*`` calls (``/titles``, ``/titles/{hid}/chapters``,
``/chapters/{id}``) again require a ``vrf`` query token (live: ``403 {"message":
"Missing token."}`` without it). The token is minted by ``getProtectionToken(path,
params)`` inside an obfuscator-VM "polyfill" chunk (``polyfill-<build>-<hash>.js`` on
``s.mfcdn.nl``). Rather than reversing the VM, we run the site's OWN chunk in an
embedded QuickJS context; the chunk URL is rediscovered from the homepage at runtime
(``find_chunk_url``) so a build rotation self-heals on the next 403 (the source drops
and rebuilds the signer).

Pure: no networking, no FastAPI — the source fetches the homepage + chunk via
``ctx.get_bytes`` and hands the JS text to :class:`VrfSigner`.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
from collections.abc import Mapping
from typing import Any

import quickjs

from ..framework.errors import SourceError

# SSRF (261101-r4c): pins scheme + host ``s.mfcdn.nl`` + the ``/build/mf/assets/
# polyfill-`` path, so the ONLY URL ever fetched from scraped homepage HTML is that
# CDN chunk — never an arbitrary link.
CHUNK_URL_RE = re.compile(
    r"https://s\.mfcdn\.nl/build/mf/assets/polyfill-[A-Za-z0-9_-]+\.js"
)
# The chunk is an ES module with no imports and ONE trailing ``export{...};`` —
# stripping it lets the body evaluate as a plain script.
_EXPORT_RE = re.compile(r"export\{[^}]*\};?\s*$")

# Browser-globals shim, copied VERBATIM from the working spike (261101-r4c). It is
# load-bearing: the VM's decryption key derives from builtin arities and
# ``navigator.appCodeName``, so do not "tidy" it.
_SHIM = r"""

var TextEncoder = function(){}; TextEncoder.prototype.encode = function(str){ var out=[]; for (var i=0;i<str.length;i++){ var c=str.charCodeAt(i); if (c>=0xd800&&c<0xdc00&&i+1<str.length){ var d=str.charCodeAt(i+1); if (d>=0xdc00&&d<0xe000){ c=0x10000+((c-0xd800)<<10)+(d-0xdc00); i++; } } if (c<0x80) out.push(c); else if (c<0x800) out.push(0xc0|c>>6,0x80|c&63); else if (c<0x10000) out.push(0xe0|c>>12,0x80|c>>6&63,0x80|c&63); else out.push(0xf0|c>>18,0x80|c>>12&63,0x80|c>>6&63,0x80|c&63); } return new Uint8Array(out); };
var TextDecoder = function(){}; TextDecoder.prototype.decode = function(u8){ var s=''; var i=0; var a=u8; while(i<a.length){ var c=a[i++]; if(c<128) s+=String.fromCharCode(c); else if(c<224) s+=String.fromCharCode((c&31)<<6|a[i++]&63); else if(c<240) s+=String.fromCharCode((c&15)<<12|(a[i++]&63)<<6|a[i++]&63); else { var cp=((c&7)<<18|(a[i++]&63)<<12|(a[i++]&63)<<6|a[i++]&63)-65536; s+=String.fromCharCode((cp>>10)+55296,(cp&1023)+56320); } } return s; };
var B64='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
var btoa = function(s){ var o='',i=0; for(;i<s.length;i+=3){ var a=s.charCodeAt(i),b=s.charCodeAt(i+1),c=s.charCodeAt(i+2); o+=B64[a>>2]+B64[((a&3)<<4)|(b>>4)]+(isNaN(b)?'=':B64[((b&15)<<2)|(c>>6)])+(isNaN(c)?'=':B64[c&63]); } return o; };
var atob = function(s){ s=s.replace(/[^A-Za-z0-9+\/]/g,''); var o='',i=0; for(;i<s.length;i+=4){ var a=B64.indexOf(s[i]),b=B64.indexOf(s[i+1]),c=B64.indexOf(s[i+2]),d=B64.indexOf(s[i+3]); o+=String.fromCharCode((a<<2)|(b>>4)); if(c>=0) o+=String.fromCharCode(((b&15)<<4)|(c>>2)); if(d>=0) o+=String.fromCharCode(((c&3)<<6)|d); } return o; };
var window = globalThis; var self = globalThis;
var navigator = { appCodeName: 'Mozilla', appName: 'Netscape', userAgent: 'Mozilla/5.0', language: 'en-US', languages: ['en-US'], platform: 'Linux x86_64', webdriver: false, plugins: [] };
var location = { href: 'https://mangafire.to/', origin: 'https://mangafire.to', hostname: 'mangafire.to', host: 'mangafire.to', protocol: 'https:', pathname: '/', search: '' };
var document = { cookie: '', referrer: '', createElement: function(){ return { getContext: function(){ return null; }, style: {} }; }, querySelector: function(){ return null; }, querySelectorAll: function(){ return []; }, getElementsByTagName: function(){ return []; }, head: {}, body: {}, documentElement: {}, addEventListener: function(){} };
var localStorage = { getItem: function(){ return null; }, setItem: function(){}, removeItem: function(){} }; var sessionStorage = localStorage;
var addEventListener = function(){}; var setTimeout = setTimeout || function(f){ f(); return 0; }; var clearTimeout = clearTimeout || function(){}; var setInterval = setInterval || function(){ return 0; }; var clearInterval = clearInterval || function(){};
"""  # noqa: E501

# Third-party JS fetched at runtime, so it is boxed (261101-r4c, T-261101-r4c-01):
# QuickJS exposes no fs/network; these bound CPU/memory DoS.
_MEMORY_LIMIT = 64 * 1024 * 1024
_TIME_LIMIT_S = 5.0

# The quickjs wrapper warns that different threads touching one runtime CRASH "even if
# it is not at the same time" (its own ``Function`` pins every call to one worker), so
# EVERY quickjs call (Context, limits, eval) runs on this ONE thread (T-261101-r4c-04).
# ponytail: one JS thread serializes all signs (~8 ms each), ample under MangaFire's
# 100/min limiter; a per-signer pool if that ever bottlenecks.
_JS_THREAD = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="mangafire-vrf"
)


def find_chunk_url(html: str) -> str | None:
    """First ``s.mfcdn.nl`` polyfill chunk URL in the homepage HTML, else ``None``."""
    m = CHUNK_URL_RE.search(html)
    return m.group(0) if m else None


def _build_context(js: str) -> Any:
    """Limits FIRST, then shim + chunk; assert ``getProtectionToken`` is published."""
    ctx = quickjs.Context()
    ctx.set_memory_limit(_MEMORY_LIMIT)
    ctx.set_time_limit(_TIME_LIMIT_S)
    ctx.eval(_SHIM)
    ctx.eval(js)
    if ctx.eval("typeof getProtectionToken") != "function":
        raise SourceError(
            "source_unavailable", "mangafire vrf signer: getProtectionToken missing"
        )
    return ctx


class VrfSigner:
    """``getProtectionToken`` from one chunk build. SYNC + blocking by design —
    callers offload with ``asyncio.to_thread``; the engine runs on ``_JS_THREAD``.
    """

    def __init__(self, chunk_js: str) -> None:
        stripped = _EXPORT_RE.sub("", chunk_js)
        try:
            self._ctx = _JS_THREAD.submit(_build_context, stripped).result()
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(
                "source_unavailable", f"mangafire vrf signer: chunk eval failed: {exc}"
            ) from exc

    def sign(self, path: str, params: Mapping[str, Any]) -> str:
        """Token for ``path`` (relative to ``/api``) + the exact request query dict."""
        expr = f"getProtectionToken({json.dumps(path)}, {json.dumps(dict(params))})"
        try:
            token = _JS_THREAD.submit(self._ctx.eval, expr).result()
        except Exception as exc:
            raise SourceError(
                "source_unavailable", f"mangafire vrf signer: sign failed: {exc}"
            ) from exc
        # Untrusted engine output → validate before it rides a query param.
        if not isinstance(token, str) or not token:
            raise SourceError(
                "source_unavailable", "mangafire vrf signer: non-string token"
            )
        return token

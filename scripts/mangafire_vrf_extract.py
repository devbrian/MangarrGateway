import time, urllib.parse, json
from patchright.sync_api import sync_playwright
ALLOW=("mangafire.to","mfcdn.nl","cloudflare.com","cdnjs.cloudflare.com","cloudflareinsights.com")
def allowed(u):
    h=urllib.parse.urlparse(u).hostname or ""; return any(h==d or h.endswith("."+d) for d in ALLOW)
KW="naruto"
with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context(user_data_dir="/tmp/mfvrf/profile_sc", headless=False, viewport={"width":1280,"height":900})
    page=ctx.pages[0] if ctx.pages else ctx.new_page()
    page.route("**/*", lambda r: r.continue_() if allowed(r.request.url) else r.abort())
    cdp=ctx.new_cdp_session(page); cdp.send("Runtime.enable"); cdp.send("Debugger.enable")
    def ev(e):
        r=cdp.send("Runtime.evaluate",{"expression":e,"returnByValue":True}); return r.get("result",{}).get("value")
    def ready(m=90):
        for _ in range(m):
            if ev("typeof window.jQuery")=="function" and ev("!!document.querySelector('input[name=keyword]')"): return True
            time.sleep(1)
        return False
    page.goto("https://mangafire.to/home", wait_until="domcontentloaded", timeout=60000)
    print("ready:", ready(), flush=True)
    cdp.send("DOMDebugger.setXHRBreakpoint", {"url":"/ajax/manga/search"})
    paused={"ev":None}; cdp.on("Debugger.paused", lambda e: paused.update(ev=e))
    for _ in range(8):
        if paused["ev"]: break
        try: ev("(function(){var $=window.jQuery;var i=$('input[name=keyword]');i.val(%r);i.trigger('input');i.trigger('keyup');})()" % KW)
        except Exception: pass
        t=time.time()
        while time.time()-t<10 and not paused["ev"]: time.sleep(0.2)
    out={}
    pe=paused["ev"]
    print("xhr paused:", bool(pe), flush=True)
    if pe:
        cfid=pe["callFrames"][2]["callFrameId"]
        r=cdp.send("Debugger.evaluateOnCallFrame", {"callFrameId":cfid,"expression":"t[6].default.et","returnByValue":False})
        et_oid=r.get("result",{}).get("objectId")
        # get [[Scopes]]
        props=cdp.send("Runtime.getProperties", {"objectId":et_oid,"ownProperties":False,"generatePreview":False})
        scopes_oid=None
        for ip in props.get("internalProperties",[]):
            if ip.get("name")=="[[Scopes]]": scopes_oid=ip.get("value",{}).get("objectId")
        print("scopes_oid:", bool(scopes_oid), flush=True)
        name2oid={}
        if scopes_oid:
            scopes=cdp.send("Runtime.getProperties", {"objectId":scopes_oid,"ownProperties":True})
            for s in scopes.get("result",[]):
                sv=s.get("value",{})
                if sv.get("objectId"):
                    # each scope: get its 'object'? Actually scope entries are Scope objects; their value is the scope, getProperties on it
                    sc=cdp.send("Runtime.getProperties", {"objectId":sv["objectId"],"ownProperties":True})
                    for v in sc.get("result",[]):
                        vv=v.get("value",{})
                        if vv.get("objectId") and v["name"] not in name2oid:
                            name2oid[v["name"]]={"oid":vv["objectId"],"type":vv.get("type"),"cls":vv.get("className")}
        out["names"]=sorted(name2oid.keys())
        print("closure names:", out["names"][:60], flush=True)
        def callfn(oid, decl, args=None):
            params={"objectId":oid,"functionDeclaration":decl,"returnByValue":True}
            if args is not None: params["arguments"]=[{"value":a} for a in args]
            r=cdp.send("Runtime.callFunctionOn", params)
            return r.get("result",{}).get("value"), r.get("exceptionDetails")
        # extract key/salt values: call each fn (this=fn) -> arr
        KEYNAMES=["r1","L1","M","t1","n1","p","y","s1","Z1","P1","k","j","g","c1","_1"]
        ARRDECL="function(){var x=this();if(typeof x==='string'){var r=[];for(var i=0;i<x.length;i++)r.push(x.charCodeAt(i));return r;}return x;}"
        out["KC"]={}
        for nm in KEYNAMES:
            if nm in name2oid and name2oid[nm]["type"]=="function":
                v,exc=callfn(name2oid[nm]["oid"], ARRDECL)
                if not exc: out["KC"][nm]=v
        print("KC keys:", list(out["KC"].keys()), flush=True)
        # probe op tables: this=round fn, arg=key array
        PROBE="function(K){var R=this;var tb=[];for(var m=0;m<10;m++){var t=[];for(var w=0;w<256;w++){var inp=[];for(var j=0;j<m;j++)inp.push(0);inp.push((w^K[m%K.length])&255);var o=R(inp);t.push(o[o.length-1]);}tb.push(t);}return tb;}"
        ROUND={"q":"Z1","I":"g","V":"_1","N":"k","T":"y"}
        out["optables"]={}
        for rn,kn in ROUND.items():
            if rn in name2oid and kn in out["KC"]:
                v,exc=callfn(name2oid[rn]["oid"], PROBE, args=[out["KC"][kn]])
                if not exc: out["optables"][rn]=v
                print("probed",rn,bool(not exc),flush=True)
        out["saltlen"]={"q":5,"I":7,"V":8,"N":8,"T":6}
        # also capture o (RC4) name presence and a couple known pairs for validation
        cdp.send("Debugger.resume")
    json.dump(out, open("/tmp/mfvrf/solve.json","w"))
    print("FINISHED KC:",list((out.get("KC") or {}).keys()),"opt:",list((out.get("optables") or {}).keys()), flush=True)
    ctx.close()

"""Local web page for the judge station: stdlib HTTP, one page, no network, no models.

The server binds 127.0.0.1 only, rejects other Host headers (DNS rebinding), requires a
per-process token on every POST, and serves a strict content-security policy. All trace text
reaches the page as JSON and is placed with ``textContent``, never as HTML.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from evallab.production.judge_station import JudgeError, Station

MAX_BODY = 64 * 1024


# --------------------------------------------------------------------------- #
# the guide: a tiny, escaping markdown renderer (headings, lists, quotes, code)
# --------------------------------------------------------------------------- #


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)


def render_markdown(source: str) -> str:
    body = source
    if body.startswith("---\n"):
        end = body.find("\n---\n", 4)
        body = body[end + 5 :] if end != -1 else body
    out: list[str] = []
    paragraph: list[str] = []
    list_tag: str | None = None
    items: list[str] = []
    fence: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.extend("<li>" + _inline(item) + "</li>" for item in items)
            out.append(f"</{list_tag}>")
            items.clear()
            list_tag = None

    for line in body.splitlines():
        if fence is not None:
            if line.startswith("```"):
                out.append("<pre>" + html.escape("\n".join(fence), quote=False) + "</pre>")
                fence = None
            else:
                fence.append(line)
            continue
        if line.startswith("```"):
            flush_paragraph()
            close_list()
            fence = []
            continue
        heading = re.match(r"(#{1,3}) (.*)", line)
        bullet = re.match(r"[-*] (.*)", line)
        numbered = re.match(r"\d+\. (.*)", line)
        quote = re.match(r"> ?(.*)", line)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif bullet or numbered:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            item = bullet or numbered
            items.append(item.group(1) if item else "")
        elif quote:
            flush_paragraph()
            close_list()
            out.append("<blockquote>" + _inline(quote.group(1)) + "</blockquote>")
        elif not line.strip():
            flush_paragraph()
            close_list()
        elif list_tag and line[0] == " ":
            items[-1] += " " + line.strip()
        else:
            close_list()
            paragraph.append(line.strip())
    flush_paragraph()
    close_list()
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the page
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Judge station</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--ink:#1d2330;--muted:#7a8394;--line:#dfe3ea;--bg:#f5f6f8;--card:#fff;--accent:#2f5fd0;--ok:#1f8a4c;--bad:#c0392b;--warn:#b7791f}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header b{font-size:16px}.pill{background:#eef1f7;border-radius:999px;padding:2px 10px;font-size:13px}
button,select{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 10px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.45;cursor:default}
button.sel{outline:2px solid var(--accent);background:#eaf0ff}
main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px;align-items:start}
@media(max-width:980px){main{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px}
.step{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:8px;margin-bottom:8px}
.step.cited{border-left-color:var(--accent);background:#f3f7ff}.step.flag{border-left-color:var(--bad)}
.step>.h{display:flex;gap:8px;padding:5px 10px;cursor:pointer;color:var(--muted);font-size:12px;user-select:none}
.step>.h b{color:var(--ink)}.step>.h .tag{margin-left:auto}.tag.c{color:var(--accent)}.tag.f{color:var(--bad)}
.narr{color:var(--muted);padding:0 10px 6px;white-space:pre-wrap;word-break:break-word}
.narr.reasoning{font-style:italic;font-size:13px}
pre.code{margin:0 10px 8px;background:#14181f;color:#e6e9ef;border-radius:6px;padding:8px 10px;overflow:auto;font:12.5px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
.cname{margin:0 10px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.out{margin:0 10px 8px;border:1px dashed var(--line);border-radius:6px;background:#fafbfc}
.out pre{margin:0;padding:6px 8px;font:12px/1.4 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word;max-height:none;overflow:auto}
.out .t{padding:2px 8px;font-size:11px;color:var(--muted);cursor:pointer;border-bottom:1px dashed var(--line)}
.task pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.45 ui-monospace,Menlo,monospace;max-height:260px;overflow:auto}
aside{position:sticky;top:60px;max-height:calc(100vh - 76px);overflow:auto}
.row{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}.lbl{font-size:12px;color:var(--muted);margin-top:8px}
textarea,input[type=text]{width:100%;font:inherit;border:1px solid var(--line);border-radius:6px;padding:6px}
.reveal h3{margin:.2em 0}.chip{display:inline-block;border-radius:6px;padding:1px 8px;font-weight:600}
.chip.ok{background:#e4f5ec;color:var(--ok)}.chip.no{background:#fbe7e4;color:var(--bad)}.chip.na{background:#f3efe2;color:var(--warn)}
.det{font-size:13px;border-top:1px solid var(--line);padding:5px 0}.det a{color:var(--accent);cursor:pointer;margin-right:4px}
.kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-radius:4px;padding:0 4px;color:var(--muted)}
#guide{position:fixed;inset:0;background:rgba(20,24,31,.55);z-index:20;display:none;overflow:auto;padding:30px}
#guide .doc{max-width:760px;margin:0 auto;background:#fff;border-radius:12px;padding:6px 26px 26px}
#guide pre{background:#14181f;color:#e6e9ef;padding:10px;border-radius:6px;white-space:pre-wrap}
#guide blockquote{margin:.5em 0;padding:2px 12px;border-left:3px solid var(--line);color:#4b5565}
#msg{color:var(--bad);font-size:13px;min-height:1.2em}
</style></head><body>
<header>
 <b>Judge station</b>
 <select id="queue"></select>
 <span class="pill" id="pos">–</span>
 <span class="pill" id="rid">–</span>
 <span class="pill" id="clock">0:00</span>
 <span style="flex:1"></span>
 <button id="prev">&larr; Prev</button><button id="skip" title="n">Skip / next (n)</button>
 <button id="open-guide">How to judge (?)</button>
</header>
<main>
 <section>
  <div class="panel task" id="taskp"><b>What the agent was asked</b><div id="task"></div></div>
  <div id="timeline"></div>
 </section>
 <aside>
  <div class="panel" id="formp">
   <b>Your verdict</b>
   <div class="row" id="vbtns">
    <button data-v="cheat">Cheat <span class="kbd">c</span></button>
    <button data-v="legitimate">Legitimate <span class="kbd">l</span></button>
    <button data-v="unclear">Can't tell <span class="kbd">u</span></button></div>
   <div class="lbl" id="mechl">How it cheated</div>
   <select id="mech" style="width:100%"></select>
   <div class="lbl">Evidence steps (click a step header to cite it)</div>
   <div id="cites" class="pill" style="display:block">none</div>
   <div class="lbl">Confidence <span class="kbd">1</span> <span class="kbd">2</span> <span class="kbd">3</span> (1 = a guess, 3 = sure)</div>
   <div class="row" id="cbtns"><button data-c="1">1</button><button data-c="2">2</button><button data-c="3">3</button></div>
   <div class="lbl">Note (optional)</div>
   <textarea id="note" rows="3"></textarea>
   <div id="msg"></div>
   <button class="primary" id="submit">Submit and reveal <span class="kbd">Enter</span></button>
  </div>
  <div class="panel reveal" id="revealp" style="display:none"></div>
 </aside>
</main>
<div id="guide"><div class="doc" id="guidedoc"></div></div>
<script nonce="__NONCE__">
"use strict";
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
const S={queue:null,list:[],idx:0,run:null,v:null,c:null,cited:new Set(),revealed:false,opened:0,state:null};
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
async function api(path,body){
  const r=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json","X-Judge-Token":TOKEN},body:JSON.stringify(body)}:{});
  const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function fmt(s){return Math.floor(s/60)+":"+String(Math.floor(s%60)).padStart(2,"0")}
setInterval(()=>{if(S.run&&!S.revealed)$("clock").textContent=fmt((Date.now()-S.opened)/1000)},500);

async function init(){
  S.state=await api("/api/state");
  const sel=$("queue");sel.textContent="";
  for(const q of S.state.queues){const o=el("option",null,q.name+" ("+q.done+"/"+q.total+")");o.value=q.name;sel.appendChild(o)}
  const m=$("mech");m.textContent="";const o0=el("option",null,"– pick –");o0.value="";m.appendChild(o0);
  for(const x of S.state.mechanisms){const o=el("option",null,x.replace(/_/g," "));o.value=x;m.appendChild(o)}
  sel.onchange=()=>loadQueue(sel.value);
  if(S.state.queues.length)await loadQueue(S.state.queues[0].name);else $("timeline").textContent="No queue loaded."
}
async function loadQueue(name){
  S.queue=name;S.list=(await api("/api/queue?name="+encodeURIComponent(name))).items;
  let i=S.list.findIndex(x=>!x.done&&!x.refused);if(i<0)i=0;await show(i)}
async function show(i){
  if(!S.list.length)return;S.idx=((i%S.list.length)+S.list.length)%S.list.length;
  const item=S.list[S.idx];$("msg").textContent="";resetForm();
  $("pos").textContent=item.position+" / "+S.list.length+(item.done?" · judged":"");$("rid").textContent=item.id;
  $("timeline").textContent="Loading…";
  try{S.run=await api("/api/run?queue="+encodeURIComponent(S.queue)+"&id="+item.id)}
  catch(e){S.run=null;$("timeline").textContent=e.message;$("task").textContent="";return}
  S.opened=Date.now();render();
  if(S.run.previous&&S.run.previous.reveal){S.revealed=true;showReveal(S.run.previous.reveal,null,S.run.previous.verdict)}
}
function resetForm(){
  S.v=null;S.c=null;S.cited=new Set();S.revealed=false;$("note").value="";$("mech").value="";
  $("revealp").textContent="";$("revealp").style.display="none";$("submit").disabled=false;paintForm()}
function paintForm(){
  for(const b of $("vbtns").children)b.classList.toggle("sel",b.dataset.v===S.v);
  for(const b of $("cbtns").children)b.classList.toggle("sel",+b.dataset.c===S.c);
  $("mech").disabled=S.v!=="cheat";$("mechl").style.opacity=S.v==="cheat"?1:.4;
  $("cites").textContent=S.cited.size?[...S.cited].sort((a,b)=>a-b).join(", "):"none";
  for(const d of document.querySelectorAll(".step"))d.classList.toggle("cited",S.cited.has(+d.dataset.n))}
function render(){
  const t=$("task");t.textContent="";
  if(S.run.task.length){for(const p of S.run.task){const d=el("div");d.appendChild(el("div","lbl","step "+p.step+" · "+p.source));d.appendChild(el("pre",null,p.text));t.appendChild(d)}}
  else t.appendChild(el("div","lbl","The task statement is not recoverable from this run. Read it from the first steps."));
  const tl=$("timeline");tl.textContent="";
  for(const s of S.run.steps)tl.appendChild(stepNode(s));
  window.scrollTo(0,0);paintForm()}
function stepNode(s){
  const d=el("div","step");d.dataset.n=s.n;d.id="step-"+s.n;
  const h=el("div","h");h.appendChild(el("b",null,"Step "+s.n));h.appendChild(el("span",null,s.source));
  h.appendChild(el("span","tag"));h.onclick=()=>{if(S.revealed)return;S.cited.has(s.n)?S.cited.delete(s.n):S.cited.add(s.n);paintForm()};d.appendChild(h);
  for(const n of s.narration){const x=el("div","narr"+(n.kind==="reasoning"?" reasoning":""));x.textContent=(n.kind==="reasoning"?"(thinking) ":"")+short(n.text,n.kind==="reasoning"?700:2400,x);d.appendChild(x)}
  for(const c of s.calls){d.appendChild(el("div","cname","call · "+c.name));const p=el("pre","code");p.textContent=short(c.text,3000,p);d.appendChild(p)}
  s.outputs.forEach(o=>{const w=el("div","out");const t=el("div","t","tool output · "+o.length+" characters");const p=el("pre");
    const big=o.length>500;p.textContent=big?o.slice(0,500)+"\n…":o;let open=!big;
    t.onclick=()=>{open=!open;p.textContent=open?o:o.slice(0,500)+"\n…";p.style.display="block"};
    if(big)t.textContent="tool output · "+o.length+" characters (click to show all / fewer)";w.appendChild(t);w.appendChild(p);d.appendChild(w)});
  return d}
function short(text,limit,node){
  if(text.length<=limit)return text;
  const more=el("a",null," … show all ("+text.length+" chars)");more.style.cursor="pointer";more.style.color="#2f5fd0";
  more.onclick=()=>{node.textContent=text};setTimeout(()=>node.appendChild(more));return text.slice(0,limit)}
$("vbtns").onclick=e=>{const v=e.target.closest("button");if(v&&!S.revealed){S.v=v.dataset.v;paintForm()}};
$("cbtns").onclick=e=>{const b=e.target.closest("button");if(b&&!S.revealed){S.c=+b.dataset.c;paintForm()}};
$("submit").onclick=submit;
async function submit(){
  if(S.revealed||!S.run)return;const m=$("msg");m.textContent="";
  if(!S.v){m.textContent="Pick cheat, legitimate or can't tell.";return}
  if(S.v==="cheat"&&!$("mech").value){m.textContent="Pick how it cheated (or 'other').";return}
  if(!S.c){m.textContent="Pick a confidence, 1 to 3.";return}
  $("submit").disabled=true;
  try{
    const r=await api("/api/verdict",{queue:S.queue,id:S.list[S.idx].id,verdict:S.v,mechanism:S.v==="cheat"?$("mech").value:null,evidence_steps:[...S.cited],confidence:S.c,note:$("note").value});
    S.revealed=true;S.list[S.idx].done=true;showReveal(r.reveal,r.agreement,S.v,r.seconds);refreshCounts()}
  catch(e){m.textContent=e.message;$("submit").disabled=false}}
async function refreshCounts(){S.state=await api("/api/state");const sel=$("queue");
  S.state.queues.forEach((q,i)=>{sel.options[i].textContent=q.name+" ("+q.done+"/"+q.total+")"})}
function showReveal(rv,agree,verdict,seconds){
  const p=$("revealp");p.textContent="";p.style.display="block";p.className="panel reveal";
  const nb=el("button","primary","Next run (n)");nb.onclick=()=>show(nextUndone());nb.style.float="right";p.appendChild(nb);
  p.appendChild(el("h3",null,"Revealed"));
  const chip=el("span","chip "+(agree===true?"ok":agree===false?"no":"na"),agree===true?"you agree with the label":agree===false?"you disagree with the label":"no comparison");
  p.appendChild(chip);
  p.appendChild(el("div",null,"You said: "+verdict+(seconds!==undefined?" · "+fmt(seconds):"")));
  p.appendChild(el("div","lbl","Label"));p.appendChild(el("div",null,rv.label.text));
  if(rv.grader)p.appendChild(el("div",null,"Grader result: "+rv.grader));
  if(rv.queue_reason){p.appendChild(el("div","lbl","Why it is in this queue"));p.appendChild(el("div",null,rv.queue_reason))}
  if(rv.detectors.length){p.appendChild(el("div","lbl","Our detectors"));
    for(const d of rv.detectors){const row=el("div","det");row.appendChild(el("b",null,d.name+": "+d.verdict));
      if(d.steps.length){row.appendChild(el("span",null," · steps "));for(const n of d.steps){const a=el("a",null,String(n));a.onclick=()=>{const t=$("step-"+n);if(t)t.scrollIntoView({block:"center"})};row.appendChild(a)}}
      if(d.summary)row.appendChild(el("div","narr",d.summary));p.appendChild(row);
      for(const n of d.steps){const t=$("step-"+n);if(t){t.classList.add("flag");t.querySelector(".tag").textContent="detector cited"}}}}
  for(const n of rv.notes){p.appendChild(el("div","lbl",n.title));p.appendChild(el("div",null,n.text))}
  $("submit").disabled=true}
function nextUndone(){for(let k=1;k<=S.list.length;k++){const j=(S.idx+k)%S.list.length;if(!S.list[j].done&&!S.list[j].refused)return j}return S.idx+1}
$("skip").onclick=()=>show(S.revealed?nextUndone():S.idx+1);$("prev").onclick=()=>show(S.idx-1);
async function openGuide(){const g=$("guide");if(!$("guidedoc").dataset.loaded){try{$("guidedoc").innerHTML=(await api("/api/guide")).html}catch(e){$("guidedoc").textContent=e.message}$("guidedoc").dataset.loaded=1}g.style.display="block"}
$("open-guide").onclick=openGuide;$("guide").onclick=e=>{if(e.target.id==="guide")$("guide").style.display="none"};
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){$("guide").style.display="none";return}
  const typing=["TEXTAREA","INPUT","SELECT"].includes(e.target.tagName);
  if(typing){if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){e.preventDefault();submit()}return}
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==="?"){openGuide();return}
  if(e.key==="n"){$("skip").click();return}
  if(S.revealed)return;
  if(e.key==="c"){S.v="cheat";paintForm()}else if(e.key==="l"){S.v="legitimate";paintForm()}else if(e.key==="u"){S.v="unclear";paintForm()}
  else if("123".includes(e.key)&&e.key){S.c=+e.key;paintForm()}
  else if(e.key==="Enter"){e.preventDefault();submit()}
});
init().catch(e=>{$("timeline").textContent=String(e)});
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# the server
# --------------------------------------------------------------------------- #


def make_handler(
    station: Station, token: str, nonce: str, hosts: set[str]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "judge-station"

        def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
            return

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def _guard(self) -> bool:
            if self.headers.get("Host") not in hosts:
                self._json({"error": "bad host"}, HTTPStatus.FORBIDDEN)
                return False
            return True

        def do_GET(self) -> None:
            if not self._guard():
                return
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    page = PAGE.replace("__TOKEN__", token).replace("__NONCE__", nonce)
                    self._send(200, page.encode(), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    self._json(station.state())
                elif url.path == "/api/queue":
                    self._json({"items": station.queue(query.get("name", ""))})
                elif url.path == "/api/run":
                    self._json(station.run_view(query.get("queue", ""), query.get("id", "")))
                elif url.path == "/api/guide":
                    self._json(
                        {"html": render_markdown(station.guide_path.read_text(encoding="utf-8"))}
                    )
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except JudgeError as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
            except OSError as error:
                self._json(
                    {"error": f"unavailable: {error.__class__.__name__}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def do_POST(self) -> None:
            if not self._guard():
                return
            if self.headers.get("X-Judge-Token") != token:
                self._json({"error": "bad token"}, HTTPStatus.FORBIDDEN)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY or urlparse(self.path).path != "/api/verdict":
                    self._json({"error": "bad request"}, HTTPStatus.BAD_REQUEST)
                    return
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise JudgeError("a verdict is a JSON object")
                self._json(
                    station.submit(str(payload.get("queue")), str(payload.get("id")), payload)
                )
            except (JudgeError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.CONFLICT)
            except OSError as error:
                self._json(
                    {"error": f"could not save: {error.__class__.__name__}"},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )

    return Handler


def make_server(station: Station, port: int = 8795) -> tuple[ThreadingHTTPServer, str]:
    token, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    hosts: set[str] = set()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(station, token, nonce, hosts))
    hosts.update({f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"})
    server.daemon_threads = True
    return server, token


def serve(station: Station, *, port: int = 8795, open_browser: bool = False) -> int:
    try:
        server, _ = make_server(station, port)
    except OSError as error:
        print(f"judge: cannot listen on 127.0.0.1:{port} ({error}). Try --port.")
        return 2
    url = f"http://127.0.0.1:{server.server_port}/"
    total = sum(len(q) for q in station.queues.values())
    print(f"Judge station for {station.judge}: {total} runs in {len(station.queues)} queue(s)")
    print(f"verdicts: {station.verdicts_path}")
    print(f"open {url}   (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0

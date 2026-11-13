#!/usr/bin/env python3
"""
Mix Splitter web page: paste a YouTube mix link -> review the tracklist -> songs land in your music library.

  MUSIC_LIBRARY=/music python webui.py          # then open http://<server>:8765

Flow per mix:  queued -> analyzing (read video, download, listen) -> review (you check/edit) -> splitting -> done
"Analyze + save straight away" skips the review step.
Jobs are kept in DATA_DIR/jobs/<id>/ so a review survives container restarts.
Meant for your home network or behind a login (e.g. Cloudflare Access).
"""
import hashlib
import html
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

HERE = Path(__file__).resolve().parent
LIBRARY = os.environ.get("MUSIC_LIBRARY", "/music")
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))
PORT = int(os.environ.get("PORT", "8765"))
JOBS = DATA / "jobs"
# Optional: ask Navidrome to rescan as soon as songs are saved (needs a Navidrome admin login)
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")
AUDIO_TYPES = {".webm": "audio/webm", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac"}
JOBS.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(HERE))
from trackid import fmt_ts, parse_ts  # noqa: E402

lock = threading.Lock()
work = queue.Queue()
jobs = {}  # id -> job dict (mirrors jobs/<id>/job.json)

STEPS = [("info", "Read video"), ("download", "Download"), ("scan", "Listen"), ("review", "Review"),
         ("split", "Save songs")]


# --------------------------------------------------------------------------- job storage

def jdir(jid):
    return JOBS / str(jid)


def save(job):
    with lock:
        jobs[job["id"]] = job
        (jdir(job["id"]) / "job.json").write_text(json.dumps(job), encoding="utf-8")


def load_jobs():
    for d in sorted(JOBS.iterdir(), key=lambda p: p.name):
        try:
            job = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if job["status"] in ("queued", "analyzing", "splitting"):
            job["status"] = "failed"
            job["error"] = "Interrupted (the container restarted). Start it again."
        jobs[job["id"]] = job


def new_id():
    with lock:
        return max([0] + [int(k) for k in jobs]) + 1


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def progress_of(job):
    return read_json(jdir(job["id"]) / "progress.json", {}) or {}


def log_tail(job, n=40):
    try:
        text = (jdir(job["id"]) / "log.txt").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = [l for l in text.replace("\r", "\n").splitlines() if l.strip() and not l.strip().startswith("scanning ")]
    return "\n".join(lines[-n:])


# --------------------------------------------------------------------------- worker

def run(job, args, env_extra=None):
    d = jdir(job["id"])
    env = dict(os.environ, PROGRESS_FILE=str(d / "progress.json"), PYTHONUNBUFFERED="1", **(env_extra or {}))
    with open(d / "log.txt", "a", encoding="utf-8") as f:
        return subprocess.run([sys.executable, "-u"] + args, stdout=f, stderr=subprocess.STDOUT, env=env).returncode


def last_error(job):
    tail = log_tail(job, 12).splitlines()
    msg = next((l for l in reversed(tail) if "ERROR" in l or "could not" in l or "Error" in l), tail[-1] if tail else "")
    return msg[-300:]


def navidrome_configured():
    return bool(NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD)


def navidrome_scan():
    """Start a Navidrome library scan (Subsonic API). Returns "" on success, else a short reason it didn't start."""
    salt = os.urandom(8).hex()
    token = hashlib.md5((NAVIDROME_PASSWORD + salt).encode()).hexdigest()
    query = urlencode({"u": NAVIDROME_USER, "t": token, "s": salt, "v": "1.16.1", "c": "mix-splitter", "f": "json"})
    try:
        with urllib.request.urlopen(f"{NAVIDROME_URL}/rest/startScan.view?{query}", timeout=15) as r:
            reply = json.loads(r.read().decode()).get("subsonic-response", {})
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:200]
    if reply.get("status") != "ok":
        return str((reply.get("error") or {}).get("message") or reply)[:200]
    return ""


def job_audio(jid):
    """The downloaded mix for a job (only ever a file inside that job's own folder), or None."""
    state = read_json(jdir(jid) / "state.json", {}) or {}
    path = Path(state.get("audio") or "")
    if path.is_file() and path.resolve().parent == jdir(jid).resolve():
        return path
    return None


def worker():
    while True:
        jid, action = work.get()
        job = jobs.get(jid)
        if not job:
            continue
        d = jdir(jid)
        try:
            if action == "selftest":
                job["status"] = "analyzing"
                save(job)
                rc = run(job, [str(HERE / "selftest.py")])
                job["status"] = "done" if rc == 0 else "failed"
            elif action == "analyze":
                job["status"] = "analyzing"
                save(job)
                args = [str(HERE / "trackid.py"), job["url"], "--analyze-only", str(d)]
                if job["opts"].get("tracklist", "").strip():
                    (d / "tracklist.txt").write_text(job["opts"]["tracklist"], encoding="utf-8")
                    args += ["--tracklist", str(d / "tracklist.txt")]
                if job["opts"].get("unordered"):
                    args.append("--unordered")
                if job["opts"].get("comments"):
                    args.append("--comments")
                if job["opts"].get("verify"):
                    args.append("--verify")
                if job["opts"].get("confirmed"):
                    args.append("--confirmed-only")
                if job["opts"].get("step"):
                    args += ["--step", str(job["opts"]["step"])]
                rc = run(job, args)
                if rc != 0:
                    job.update(status="failed", error=last_error(job))
                else:
                    st = read_json(d / "state.json", {})
                    job["title"] = (st.get("info") or {}).get("title") or job["url"]
                    if job["opts"].get("auto"):
                        job["status"] = "splitting"
                        save(job)
                        work.put((jid, "split"))
                        continue
                    job["status"] = "review"
            elif action == "split":
                job["status"] = "splitting"
                save(job)
                args = [str(HERE / "trackid.py"), "--split-job", str(d), "--library", LIBRARY]
                if (d / "reviewed.json").exists():
                    args += ["--rows", str(d / "reviewed.json")]
                rev = read_json(d / "review_meta.json", {})
                if rev.get("album"):
                    args += ["--album", rev["album"]]
                if rev.get("album_artist"):
                    args += ["--album-artist", rev["album_artist"]]
                if rev.get("full", job["opts"].get("full")):
                    args.append("--keep-full-mix")
                args += ["--format", rev.get("fmt") or job["opts"].get("fmt") or "mp3"]
                rc = run(job, args)
                if rc == 0:
                    job["status"] = "done"
                    job["folder"] = progress_of(job).get("folder", "")
                    if navidrome_configured():
                        job["scan"] = navidrome_scan() or "started"
                else:
                    job.update(status="failed", error=last_error(job))
        except Exception as e:  # never let the worker die
            job.update(status="failed", error=str(e))
        job["finished"] = time.strftime("%b %d %H:%M")
        save(job)


# --------------------------------------------------------------------------- HTML

CSS = """
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--text:#e8eaf0;--muted:#9aa3b2;--accent:#3b82f6;
--ok:#22c55e;--warn:#f59e0b;--est:#60a5fa;--miss:#6b7280;--posted:#a78bfa;--bad:#ef4444}
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;max-width:980px;margin:0 auto;padding:16px;
background:var(--bg);color:var(--text)}a{color:#93c5fd}h1{font-size:22px;margin:4px 0 14px}h2{font-size:17px;margin:18px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
input[type=url],input[type=text],textarea{width:100%;padding:9px;font-size:15px;background:#0b0d11;color:var(--text);
border:1px solid var(--line);border-radius:6px}textarea{height:110px;font-size:13px}
button,.btn{display:inline-block;padding:10px 16px;font-size:15px;background:var(--accent);color:#fff;border:0;border-radius:7px;
cursor:pointer;text-decoration:none}button.secondary,.btn.secondary{background:#374151}button.danger{background:#7f1d1d}
label{display:block;margin:6px 0;color:var(--muted)}small,.muted{color:var(--muted)}
.bar{height:14px;background:#0b0d11;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:8px 0}
.bar>div{height:100%;background:linear-gradient(90deg,#2563eb,#22c55e);transition:width .6s}
.steps{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}.steps span{font-size:12px;padding:3px 8px;border-radius:12px;
background:#0b0d11;border:1px solid var(--line);color:var(--muted)}.steps .now{border-color:var(--accent);color:#fff}
.steps .past{color:var(--ok);border-color:#14532d}
.badge{font-size:12px;padding:2px 8px;border-radius:10px;white-space:nowrap}
.s-queued,.s-analyzing,.s-splitting{background:#422006;color:#fbbf24}.s-review{background:#1e3a8a;color:#bfdbfe}
.s-done{background:#14532d;color:#86efac}.s-failed{background:#450a0a;color:#fca5a5}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:5px 4px;border-bottom:1px solid var(--line);vertical-align:middle}
th{text-align:left;color:var(--muted);font-weight:500}td input[type=text]{padding:5px;font-size:14px}
td.t input{width:78px}.c-ok{color:var(--ok)}.c-warn{color:var(--warn)}.c-est{color:var(--est)}.c-miss{color:var(--miss)}
.c-posted{color:var(--posted)}tr.off td{opacity:.45}
ul.found{columns:2;font-size:13px;padding-left:18px;margin:6px 0}@media(max-width:640px){ul.found{columns:1}
.hide-m{display:none}}pre{white-space:pre-wrap;font-size:12px;background:#0b0d11;padding:8px;border-radius:6px;
max-height:360px;overflow:auto}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button.play{padding:3px 8px;font-size:13px;background:#374151;margin-left:4px}
select{padding:7px;font-size:14px;background:#0b0d11;color:var(--text);border:1px solid var(--line);border-radius:6px}
.player{position:sticky;top:0;z-index:2;background:var(--card);padding:6px 0}.player audio{width:100%}
.grip{display:flex;align-items:center;gap:6px}.handle{cursor:grab;touch-action:none;user-select:none;color:var(--muted);
font-size:18px;padding:4px 2px}tr.dragging td.song{opacity:.4}tr.drop td{border-top:2px solid var(--accent)}
tr.moved td.song{background:#1e3a8a55;transition:background .9s}
.joblink{display:flex;justify-content:space-between;gap:10px;align-items:center;text-decoration:none;color:inherit}
"""


def page(title, body, poll=None):
    """poll = URL of a status endpoint to refresh progress from (only while something is running)."""
    js = ("""<script>
setInterval(async()=>{try{const d=await (await fetch('%s')).json();
for(const [id,v] of Object.entries(d.jobs||{})){const b=document.getElementById('bar-'+id);if(b)b.style.width=v.pct+'%%';
const t=document.getElementById('txt-'+id);if(t)t.textContent=v.text;
const f=document.getElementById('found-'+id);if(f)f.innerHTML=v.found_html;
const st=document.getElementById('steps-'+id);if(st)st.innerHTML=v.steps_html;
const was=document.body.dataset['s'+id];if(was&&v.status!==was)location.reload();}}catch(e){}},2500);
</script>""" % poll) if poll else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body {{BODYDATA}}>
<h1><a href="/" style="color:inherit;text-decoration:none">Mix Splitter</a> <small>→ Navidrome</small></h1>
{body}{js}</body></html>"""


def status_badge(s):
    names = {"queued": "Waiting", "analyzing": "Working", "splitting": "Saving", "review": "Ready to review",
             "done": "Done", "failed": "Failed"}
    return f'<span class="badge s-{s}">{names.get(s, s)}</span>'


def overall_pct(job, prog):
    """Progress across the whole flow, so the bar never jumps backwards."""
    s, p = job["status"], prog.get("pct", 0)
    if job.get("kind") == "selftest":
        return 100 if s in ("done", "failed") else 50
    if s == "queued":
        return 0
    if s == "analyzing":
        return p * 0.8
    if s == "review":
        return 80
    if s == "splitting":
        return 80 + (p * 0.2 if prog.get("stage") in ("split", "done") else 0)
    return 100


def current_step(job, prog):
    s = job["status"]
    if s == "review":
        return "review"
    if s == "splitting":
        return "split"
    if s == "done":
        return "end"
    st = prog.get("stage", "info")
    return {"match": "scan", "analyzed": "review"}.get(st, st)


def steps_html(job, prog):
    if job.get("kind") == "selftest":
        return ""
    cur = current_step(job, prog)
    keys = [k for k, _ in STEPS]
    idx = keys.index(cur) if cur in keys else len(keys)
    out = []
    for i, (k, name) in enumerate(STEPS):
        cls = "past" if i < idx else "now" if i == idx and job["status"] != "failed" else ""
        mark = "✓ " if i < idx else ""
        out.append(f'<span class="{cls}">{mark}{i + 1}. {name}</span>')
    return "".join(out)


def status_text(job, prog):
    s = job["status"]
    if s == "queued":
        return "Waiting for the previous job to finish..."
    if s == "failed":
        return "Failed: " + (job.get("error") or "see the log below")
    if s == "review":
        return (prog.get("summary") or "Analysis finished") + " - check the list below, then save."
    if job.get("kind") == "selftest":
        return "Running checks..." if s == "analyzing" else "Self-test finished - see results below."
    return prog.get("text") or "Starting..."


def found_html(prog):
    found = prog.get("found") or []
    if not found:
        return '<span class="muted">Nothing recognised yet - songs appear here as Shazam hears them.</span>'
    return '<ul class="found">' + "".join(f"<li>{html.escape(x)}</li>" for x in found) + "</ul>"


def home():
    form = """<div class="card"><form method="post" action="/add">
<input type="url" name="url" placeholder="Paste a YouTube mix link: https://youtu.be/..." required autofocus>
<details style="margin-top:8px"><summary><small>Optional: paste a tracklist (if it isn't in the video description)</small></summary>
<textarea name="tracklist" placeholder="Artist - Title&#10;Artist - Title&#10;(timestamps optional, e.g. 12:30 Artist - Title)"></textarea></details>
<label><input type="checkbox" name="unordered"> The tracklist is not in play order</label>
<label><input type="checkbox" name="comments"> Look for a tracklist in the YouTube comments</label>
<label><input type="checkbox" name="full"> Also keep the full mix as one file</label>
<label><input type="checkbox" name="verify"> Double-check timestamped tracklists with Shazam (slower)</label>
<label><input type="checkbox" name="quick"> Quick-cut mix: listen twice as often (catches short songs, takes twice as long)</label>
<label><input type="checkbox" name="confirmed"> Only tick songs Shazam actually heard (no estimated cut points)</label>
<label>Save songs as <select name="fmt"><option value="mp3">MP3 (320 kbps)</option><option value="m4a">M4A (AAC 256 kbps)</option></select></label>
<div class="row" style="margin-top:10px"><button name="mode" value="review">Find the songs (review before saving)</button>
<button name="mode" value="auto" class="secondary">Find + save straight away</button></div></form></div>"""
    cards = []
    datas = []
    for job in sorted(jobs.values(), key=lambda j: -j["id"])[:25]:
        prog = progress_of(job)
        title = job.get("title") or prog.get("title") or job.get("url") or "Self-test"
        datas.append(f'data-s{job["id"]}="{job["status"]}"')
        cards.append(f"""<div class="card"><a class="joblink" href="/job/{job['id']}"><div style="min-width:0">
<b>{html.escape(title[:90])}</b><br><small>{html.escape(job['added'])}</small></div>{status_badge(job['status'])}</a>
<div class="bar"><div id="bar-{job['id']}" style="width:{overall_pct(job, prog):.0f}%"></div></div>
<small id="txt-{job['id']}">{html.escape(status_text(job, prog))}</small>
{'<div style="margin-top:8px"><a class="btn" href="/job/%d">Review the tracklist</a></div>' % job['id'] if job['status'] == 'review' else ''}
</div>""")
    tools = """<form method="post" action="/selftest" style="margin-top:6px"><button class="secondary" style="font-size:13px;padding:6px 12px">
Run self-test</button> <small>Library: """ + html.escape(LIBRARY) + " · Navidrome rescan after saving: " + (
        "on" if navidrome_configured() else "off (set NAVIDROME_URL / NAVIDROME_USER / NAVIDROME_PASSWORD)") + "</small></form>"
    busy = any(j["status"] in ("queued", "analyzing", "splitting") for j in jobs.values())
    return page("Mix Splitter", form + tools + "".join(cards), poll="/status" if busy else None).replace("{BODYDATA}", " ".join(datas))


def yt_link(state, seconds):
    url = (state.get("info") or {}).get("webpage_url") or ""
    if "youtube.com" not in url and "youtu.be" not in url or seconds is None:
        return ""
    sep = "&" if "?" in url else "?"
    return (f' <a href="{html.escape(url)}{sep}t={int(seconds)}s" target="_blank" rel="noopener" '
            f'title="Open YouTube at this point" style="text-decoration:none">&#8599;</a>')


GRIP = '<span class="handle" title="Drag to move this song to another start time">&#10303;</span>'
PLAY_BTN = '<button type="button" class="play" title="Play the mix from this time" onclick="playFrom(this)">&#9654;</button>'


def blank_row(i):
    """Empty row for adding a song that was missed (the page's "Add another song" button makes more of these)."""
    return f"""<tr><td><input type="checkbox" name="inc_{i}" checked></td>
<td class="t" style="white-space:nowrap"><input type="text" name="time_{i}" placeholder="mm:ss">{PLAY_BTN}</td>
<td class="song"><div class="grip">{GRIP}<input type="text" name="artist_{i}" placeholder="Add a missing song"></div></td>
<td class="song"><input type="text" name="title_{i}" placeholder="Title"><input type="hidden" name="src_{i}" value=""></td>
<td class="song hide-m"></td></tr>"""


REVIEW_JS = """<script>
function playFrom(b){const v=b.closest('tr').querySelector('input[name^=time_]').value.trim();
if(!/^\\d{1,2}(:\\d{1,2}){1,2}$/.test(v)){alert('Type a start time first, like 12:34');return}
const s=v.split(':').reduce((a,x)=>a*60+Number(x),0),p=document.getElementById('player');
const go=()=>{p.currentTime=s;p.play()};
if(p.readyState>0)go();else{p.addEventListener('loadedmetadata',go,{once:true});p.load()}}
function addRow(){const t=document.getElementById('tracks'),i=t.querySelectorAll('input[name^=title_]').length;
t.tBodies[0].insertAdjacentHTML('beforeend',%s.replace(/__I__/g,i));
t.querySelector('input[name=time_'+i+']').focus()}
// Drag a song by its handle onto another row: the songs in between shift along, the start times and ticks stay put.
(function(){const t=document.getElementById('tracks');let from=null,over=null;
const rows=()=>Array.from(t.tBodies[0].rows).filter(r=>r.querySelector('td.song'));
const rowAt=e=>{const el=document.elementFromPoint(e.clientX,e.clientY),r=el&&el.closest('tr');return r&&rows().includes(r)?r:null};
const mark=r=>{if(over)over.classList.remove('drop');over=r;if(over&&over!==from)over.classList.add('drop')};
function move(a,b){const rs=rows(),cells=rs.map(r=>Array.from(r.querySelectorAll('td.song'))),order=rs.map((_,i)=>i);
order.splice(b,0,order.splice(a,1)[0]);
rs.forEach((r,i)=>{cells[order[i]].forEach(c=>r.appendChild(c));
r.querySelectorAll('td.song input').forEach(x=>x.name=x.name.replace(/_\\d+$/,'_'+i))})}
t.addEventListener('pointerdown',e=>{const h=e.target.closest('.handle');if(!h)return;e.preventDefault();
from=h.closest('tr');from.classList.add('dragging');h.setPointerCapture(e.pointerId)});
t.addEventListener('pointermove',e=>{if(from)mark(rowAt(e))});
const end=e=>{if(!from)return;const to=e.type==='pointerup'?rowAt(e):null,rs=rows();
if(to&&to!==from){move(rs.indexOf(from),rs.indexOf(to));to.classList.add('moved');setTimeout(()=>to.classList.remove('moved'),900)}
from.classList.remove('dragging');mark(null);from=null};
t.addEventListener('pointerup',end);t.addEventListener('pointercancel',end)})();
</script>""" % json.dumps(blank_row("__I__"))


def review_table(job, state):
    rows = sorted(state["rows"], key=lambda r: (r.get("start") is None, r.get("start") or 0))
    trs = []
    for i, r in enumerate(rows):
        t = "" if r.get("start") is None else fmt_ts(r["start"])
        level, label = r.get("level", "ok"), r.get("label", "")
        heard = r.get("shazam") or ""
        heard_note = (f'<br><small class="muted">Shazam: {html.escape(heard)}</small>'
                      if heard and heard.lower() != f'{r["artist"]} - {r["title"]}'.lower() else "")
        trs.append(f"""<tr class="{'' if r.get('include') else 'off'}">
<td><input type="checkbox" name="inc_{i}" {'checked' if r.get('include') else ''}></td>
<td class="t" style="white-space:nowrap"><input type="text" name="time_{i}" value="{t}" placeholder="mm:ss">{PLAY_BTN}{yt_link(state, r.get("start"))}</td>
<td class="song"><div class="grip">{GRIP}<input type="text" name="artist_{i}" value="{html.escape(r['artist'] or '')}"></div></td>
<td class="song"><input type="text" name="title_{i}" value="{html.escape(r['title'] or '')}"><input type="hidden" name="src_{i}" value="{i}">{heard_note}</td>
<td class="song hide-m"><small class="c-{level}">{html.escape(label)}</small></td></tr>""")
    trs += [blank_row(i) for i in range(len(rows), len(rows) + 3)]
    return rows, f"""<table id="tracks"><tr><th title="Save as its own song">Save</th><th>Starts at</th><th>Artist</th><th>Title</th>
<th class="hide-m">How sure</th></tr>{''.join(trs)}</table>
<button type="button" class="secondary" style="font-size:13px;padding:6px 12px;margin-top:8px" onclick="addRow()">+ Add another song</button>"""


def job_page(jid):
    job = jobs.get(jid)
    if not job:
        return None
    prog = progress_of(job)
    d = jdir(jid)
    title = job.get("title") or prog.get("title") or job.get("url") or "Self-test"
    parts = [f"""<div class="card"><div class="row" style="justify-content:space-between"><b>{html.escape(title)}</b>
{status_badge(job['status'])}</div><small class="muted">{html.escape(job.get('url') or '')}</small>
<div class="steps" id="steps-{jid}">{steps_html(job, prog)}</div><div class="bar"><div id="bar-{jid}" style="width:{overall_pct(job, prog):.0f}%"></div></div>
<div id="txt-{jid}">{html.escape(status_text(job, prog))}</div>
{f'<p class="muted">{html.escape(prog["posted"])}</p>' if prog.get('posted') else ''}</div>"""]

    if job["status"] in ("analyzing", "queued") and job.get("kind") != "selftest":
        parts.append(f'<div class="card"><h2>Songs recognised so far</h2><div id="found-{jid}">{found_html(prog)}</div></div>')

    if job["status"] == "review":
        state = read_json(d / "state.json", {})
        _, table = review_table(job, state)
        n_inc = sum(1 for r in state["rows"] if r.get("include"))
        parts.append(f"""<div class="card"><h2>Check the tracklist</h2>
<p class="muted">Ticked songs become separate files, cut where each one <i>starts</i>. Fix any names or times, untick anything
that shouldn't be its own song (samples, short intros), or add a song Shazam missed. If a song is at the wrong time,
drag it by its &#10303; handle onto the row with the right time: the songs in between shift along and the times stay where they are. Times are where the song starts in the mix
(press &#9654; to play the mix from that time right here - it uses whatever is typed in the box, so you can try a time
before saving; &#8599; opens YouTube at that point). The first ticked song also gets any intro before it. {n_inc} of {len(state['rows'])} songs are ticked.</p>
<p class="muted"><span class="c-ok">Confirmed</span> = Shazam heard it ·
<span class="c-est">Estimated</span> = on the posted list but not heard, time guessed from the order ·
<span class="c-warn">Heard once</span> = double-check it</p>
<div class="player"><audio id="player" controls preload="none" src="/job/{jid}/audio"></audio></div>
<form method="post" action="/job/{jid}/split">
<div class="row"><div style="flex:2;min-width:220px"><label>Album</label>
<input type="text" name="album" value="{html.escape(state.get('album') or '')}"></div>
<div style="flex:1;min-width:160px"><label>Album artist (the DJ)</label>
<input type="text" name="album_artist" value="{html.escape(state.get('album_artist') or '')}"></div></div>
<label><input type="checkbox" name="full" {'checked' if job['opts'].get('full') else ''}> Also keep the full mix as one file</label>
<label>Save songs as <select name="fmt"><option value="mp3">MP3 (320 kbps)</option>
<option value="m4a" {'selected' if job['opts'].get('fmt') == 'm4a' else ''}>M4A (AAC 256 kbps)</option></select></label>
<div style="overflow-x:auto;margin-top:8px">{table}</div>
<div class="row" style="margin-top:12px"><button>Save these songs to my library</button></div></form>
<form method="post" action="/job/{jid}/discard" style="margin-top:10px" onsubmit="return confirm('Discard this mix?')">
<button class="danger" style="font-size:13px;padding:6px 12px">Discard</button></form></div>{REVIEW_JS}""")

    if job["status"] == "done" and job.get("kind") != "selftest":
        scan = job.get("scan")
        note = ("A Navidrome library scan was started, so they should show up in a moment." if scan == "started"
                else f"Could not start a Navidrome scan ({html.escape(scan)}) - start one from Navidrome's settings." if scan
                else "Navidrome shows them after its next library scan (or start a scan from its settings).")
        parts.append(f"""<div class="card"><h2>Saved</h2><p>Songs are in <code>{html.escape(job.get('folder') or '')}</code>.
{note}</p></div>""")

    if job["status"] == "failed" and job.get("kind") != "selftest":
        resume = ("The tracklist is already worked out - this takes you back to the review." if job_audio(jid) else
                  "Picks up where it stopped: audio that was already downloaded and clips Shazam already answered are reused.")
        parts.append(f"""<div class="card"><form method="post" action="/job/{jid}/retry"><button>Try again</button>
<small class="muted"> {resume}</small></form></div>""")

    log = log_tail(job, 60 if job.get("kind") == "selftest" else 40)
    open_attr = "open" if job.get("kind") == "selftest" or job["status"] == "failed" else ""
    parts.append(f'<details class="card" {open_attr}><summary>Technical log</summary><pre>{html.escape(log)}</pre></details>')
    if job["status"] in ("done", "failed"):
        parts.append(f"""<form method="post" action="/job/{jid}/discard" onsubmit="return confirm('Remove from the list? (Saved songs stay in your library.)')">
<button class="secondary" style="font-size:13px;padding:6px 12px">Remove from list</button></form>""")
    busy = job["status"] in ("queued", "analyzing", "splitting")
    return page(title, "".join(parts), poll=f"/job/{jid}/status" if busy else None).replace("{BODYDATA}", f'data-s{jid}="{job["status"]}"')


def status_json(only=None):
    out = {}
    for job in jobs.values():
        if only is not None and job["id"] != only:
            continue
        prog = progress_of(job)
        out[job["id"]] = {"status": job["status"], "pct": round(overall_pct(job, prog)), "text": status_text(job, prog),
                          "found_html": found_html(prog), "steps_html": steps_html(job, prog)}
    return {"jobs": out}


# --------------------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def send_audio(self, path):
        """Serve the downloaded mix with byte-range support so the review page's player can jump to any time."""
        if not path:
            return self.send("Not found", "text/plain", 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # "bytes=-N" = the last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser stopped listening (seek / page closed)

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/") or "/"
        if p == "/":
            return self.send(home())
        if p == "/status":
            return self.send(json.dumps(status_json()), "application/json")
        m = re.fullmatch(r"/job/(\d+)/audio", p)
        if m:
            return self.send_audio(job_audio(int(m.group(1))))
        m = re.fullmatch(r"/job/(\d+)(/status)?", p)
        if m:
            jid = int(m.group(1))
            if m.group(2):
                return self.send(json.dumps(status_json(jid)), "application/json")
            body = job_page(jid)
            return self.send(body) if body else self.redirect("/")
        self.send("Not found", "text/plain", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode(), keep_blank_values=True).items()}
        p = self.path.rstrip("/")
        if p == "/selftest":
            jid = new_id()
            jdir(jid).mkdir(parents=True, exist_ok=True)
            save({"id": jid, "kind": "selftest", "url": "", "title": "Self-test", "status": "queued", "opts": {},
                  "added": time.strftime("%b %d %H:%M")})
            work.put((jid, "selftest"))
            return self.redirect(f"/job/{jid}")
        if p == "/add":
            url = form.get("url", "").strip()
            local_ok = os.environ.get("ALLOW_LOCAL_FILES") == "1" and Path(url).is_file()  # for testing only
            if not (url.startswith("http") or local_ok):
                return self.redirect("/")
            jid = new_id()
            jdir(jid).mkdir(parents=True, exist_ok=True)
            save({"id": jid, "kind": "mix", "url": url, "status": "queued", "added": time.strftime("%b %d %H:%M"),
                  "opts": {"tracklist": form.get("tracklist", ""), "unordered": "unordered" in form,
                           "comments": "comments" in form, "full": "full" in form, "verify": "verify" in form,
                           "step": 15 if "quick" in form else 30, "confirmed": "confirmed" in form,
                           "fmt": "m4a" if form.get("fmt") == "m4a" else "mp3",
                           "auto": form.get("mode") == "auto"}})
            work.put((jid, "analyze"))
            return self.redirect(f"/job/{jid}")
        m = re.fullmatch(r"/job/(\d+)/(split|discard|retry)", p)
        if m:
            jid, action = int(m.group(1)), m.group(2)
            job = jobs.get(jid)
            if not job:
                return self.redirect("/")
            if action == "discard":
                if job["status"] in ("analyzing", "splitting"):
                    return self.redirect(f"/job/{jid}")  # can't remove a running job
                with lock:
                    jobs.pop(jid, None)
                shutil.rmtree(jdir(jid), ignore_errors=True)
                return self.redirect("/")
            if action == "retry":
                if job["status"] != "failed" or job.get("kind") != "mix":
                    return self.redirect(f"/job/{jid}")
                job.pop("error", None)
                job.pop("finished", None)
                (jdir(jid) / "progress.json").unlink(missing_ok=True)
                if job_audio(jid):  # analysis finished earlier; only the saving failed
                    job["status"] = "review"
                    save(job)
                else:
                    job["status"] = "queued"
                    save(job)
                    work.put((jid, "analyze"))
                return self.redirect(f"/job/{jid}")
            if job["status"] != "review":
                return self.redirect(f"/job/{jid}")
            state = read_json(jdir(jid) / "state.json", {})
            rows = sorted(state["rows"], key=lambda r: (r.get("start") is None, r.get("start") or 0))
            reviewed = []
            extra = [int(k[6:]) + 1 for k in form if re.fullmatch(r"title_\d{1,4}", k)]  # rows added on the page
            for i in range(max([len(rows)] + extra)):
                # src_N = which analysed song now sits in row N (songs can be dragged to another row's start time)
                src = form.get(f"src_{i}", str(i) if i < len(rows) else "")
                base = (rows[int(src)] if src.isdigit() and int(src) < len(rows)
                        else {"source": "added", "confidence": "edited", "shazam": "", "url": ""})
                artist = form.get(f"artist_{i}", "").strip()
                title = form.get(f"title_{i}", "").strip()
                tstr = form.get(f"time_{i}", "").strip()
                if base.get("source") == "added" and not (title and tstr):
                    continue
                try:
                    start = parse_ts(tstr) if re.fullmatch(r"\d{1,2}(:\d{1,2}){1,2}", tstr) else None
                except ValueError:
                    start = None
                r = dict(base, artist=artist, title=title or base.get("title", ""), start=start,
                         include=(f"inc_{i}" in form) and start is not None and bool(title or base.get("title")))
                if base.get("source") == "added" or artist != base.get("artist") or title != base.get("title") or \
                        (start is not None and base.get("start") is not None and abs(start - base["start"]) >= 1):
                    r["confidence"] = "edited"
                reviewed.append(r)
            (jdir(jid) / "reviewed.json").write_text(json.dumps(reviewed), encoding="utf-8")
            (jdir(jid) / "review_meta.json").write_text(json.dumps(
                {"album": form.get("album", "").strip(), "album_artist": form.get("album_artist", "").strip(),
                 "full": "full" in form, "fmt": "m4a" if form.get("fmt") == "m4a" else "mp3"}), encoding="utf-8")
            (jdir(jid) / "progress.json").unlink(missing_ok=True)
            job["status"] = "splitting"
            save(job)
            work.put((jid, "split"))
            return self.redirect(f"/job/{jid}")
        self.redirect("/")

    def log_message(self, *a):
        pass


load_jobs()
threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    print(f"Mix Splitter on http://0.0.0.0:{PORT}  (library: {LIBRARY})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

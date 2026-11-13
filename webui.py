#!/usr/bin/env python3
"""
Tiny web page: paste a YouTube mix link -> songs appear in your music library.

  MUSIC_LIBRARY=/music python webui.py          # then open http://<server>:8765

Jobs run one at a time in the background; each shows a live log.
Meant for your home network. Don't expose it to the internet without a password in front of it.
"""
import html
import itertools
import os
import subprocess
import sys
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
LIBRARY = os.environ.get("MUSIC_LIBRARY", "/music")
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))
PORT = int(os.environ.get("PORT", "8765"))
DATA.mkdir(parents=True, exist_ok=True)

jobs = {}           # id -> dict
order = []          # newest first
work = queue.Queue()
ids = itertools.count(1)


def worker():
    while True:
        job = work.get()
        job["status"] = "running"
        if job.get("selftest"):
            with open(job["log"], "w", encoding="utf-8") as f:
                rc = subprocess.run([sys.executable, "-u", str(HERE / "selftest.py")],
                                    stdout=f, stderr=subprocess.STDOUT).returncode
            job["status"] = "done" if rc == 0 else "failed"
            job["finished"] = time.strftime("%H:%M")
            continue
        cmd = [sys.executable, "-u", str(HERE / "trackid.py"), job["url"], "--library", LIBRARY,
               "--out", str(DATA / "reports")]
        if job["tracklist"].strip():
            tl = DATA / f"job{job['id']}_tracklist.txt"
            tl.write_text(job["tracklist"], encoding="utf-8")
            cmd += ["--tracklist", str(tl)]
        if job["unordered"]:
            cmd.append("--unordered")
        if job["comments"]:
            cmd.append("--comments")
        if job["full"]:
            cmd.append("--keep-full-mix")
        with open(job["log"], "w", encoding="utf-8") as f:
            rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode
        job["status"] = "done" if rc == 0 else "failed"
        job["finished"] = time.strftime("%H:%M")


threading.Thread(target=worker, daemon=True).start()

PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mix Splitter</title>{refresh}<style>
body{{font-family:system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#111;color:#eee}}
input[type=url],textarea{{width:100%;box-sizing:border-box;padding:10px;font-size:16px;background:#222;color:#eee;border:1px solid #444;border-radius:6px}}
textarea{{height:90px;font-size:14px}} button{{padding:10px 18px;font-size:16px;background:#3b82f6;color:#fff;border:0;border-radius:6px}}
label{{display:block;margin:6px 0}} .job{{border:1px solid #333;border-radius:8px;padding:10px;margin:12px 0}}
pre{{white-space:pre-wrap;font-size:12px;background:#000;padding:8px;border-radius:6px;max-height:420px;overflow:auto}}
.done{{color:#4ade80}} .failed{{color:#f87171}} .running,.queued{{color:#facc15}} small{{color:#999}}
</style></head><body><h2>Mix Splitter → Navidrome</h2>
<form method="post" action="/add">
<input type="url" name="url" placeholder="https://youtu.be/..." required>
<details><summary><small>Optional: paste a tracklist (if it's not in the video description)</small></summary>
<textarea name="tracklist" placeholder="Artist - Title&#10;Artist - Title"></textarea></details>
<label><input type="checkbox" name="unordered"> The tracklist is not in play order</label>
<label><input type="checkbox" name="comments"> Look for a tracklist in the YouTube comments</label>
<label><input type="checkbox" name="full"> Also keep the full mix as one file</label>
<button>Split it</button></form>
<form method="post" action="/selftest" style="margin-top:8px"><button style="background:#444;font-size:13px;padding:6px 12px">Run self-test</button></form>
<p><small>Library: {lib}</small></p>{jobs}</body></html>"""


def render():
    parts = []
    busy = False
    for jid in order[:15]:
        j = jobs[jid]
        busy |= j["status"] in ("queued", "running")
        try:
            text = Path(j["log"]).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        text = text.replace("\r", "\n")
        lines = [l for l in text.splitlines() if l.strip()]
        # collapse the progress counter to its latest value
        lines = [l for i, l in enumerate(lines)
                 if not (l.strip().startswith("scanning") and i + 1 < len(lines) and lines[i + 1].strip().startswith("scanning"))]
        parts.append(
            f'<div class="job"><b class="{j["status"]}">{j["status"].upper()}</b> '
            f'<small>{html.escape(j["added"])}{" → " + j["finished"] if j.get("finished") else ""}</small><br>'
            f'{html.escape(j["url"])}<pre>{html.escape(chr(10).join(lines[-60:]))}</pre></div>')
    return PAGE.format(refresh='<meta http-equiv="refresh" content="5">' if busy else "",
                       lib=html.escape(LIBRARY), jobs="".join(parts))


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        url = form.get("url", "").strip()
        if self.path == "/selftest":
            jid = next(ids)
            jobs[jid] = {"id": jid, "url": "Self-test", "selftest": True, "status": "queued",
                         "added": time.strftime("%b %d %H:%M"), "log": str(DATA / f"job{jid}.log")}
            order.insert(0, jid)
            work.put(jobs[jid])
        elif url.startswith("http"):
            jid = next(ids)
            jobs[jid] = {"id": jid, "url": url, "tracklist": form.get("tracklist", ""),
                         "unordered": "unordered" in form, "comments": "comments" in form, "full": "full" in form,
                         "status": "queued", "added": time.strftime("%b %d %H:%M"),
                         "log": str(DATA / f"job{jid}.log")}
            order.insert(0, jid)
            work.put(jobs[jid])
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Mix Splitter on http://0.0.0.0:{PORT}  (library: {LIBRARY})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

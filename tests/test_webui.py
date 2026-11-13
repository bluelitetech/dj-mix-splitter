"""Web page tests against a real server on a free port (no network: jobs are set up by hand)."""
import importlib
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUSIC_LIBRARY", str(tmp_path / "music"))
    sys.modules.pop("webui", None)
    webui = importlib.import_module("webui")
    monkeypatch.setattr(webui.work, "put", lambda item: webui.queued.append(item), raising=False)
    webui.queued = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webui.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield webui
    srv.shutdown()


def make_job(webui, status="review", audio=b"0123456789"):
    d = webui.jdir(1)
    d.mkdir(parents=True)
    rows = [{"artist": "A", "title": "One", "start": 0, "include": True, "source": "shazam", "confidence": "high"},
            {"artist": "B", "title": "Two", "start": 200, "include": True, "source": "shazam", "confidence": "high"}]
    state = {"rows": rows, "duration": 600, "album": "Mix", "album_artist": "DJ", "info": {}, "audio": ""}
    if audio is not None:
        (d / "abc.webm").write_bytes(audio)
        state["audio"] = str(d / "abc.webm")
        (d / "state.json").write_text(json.dumps(state))
    webui.save({"id": 1, "kind": "mix", "url": "https://youtu.be/abc", "status": status, "added": "now", "opts": {}})
    return d


def get(webui, path, headers=None):
    return urllib.request.urlopen(urllib.request.Request(webui.base + path, headers=headers or {}))


def post(webui, path, form=None):
    data = urllib.parse.urlencode(form or {}).encode()
    return urllib.request.urlopen(urllib.request.Request(webui.base + path, data=data))


def test_audio_ranges(ui):
    make_job(ui)
    r = get(ui, "/job/1/audio")
    assert r.status == 200 and r.read() == b"0123456789" and r.headers["Content-Type"] == "audio/webm"
    r = get(ui, "/job/1/audio", {"Range": "bytes=2-5"})
    assert r.status == 206 and r.read() == b"2345" and r.headers["Content-Range"] == "bytes 2-5/10"
    assert get(ui, "/job/1/audio", {"Range": "bytes=7-"}).read() == b"789"
    assert get(ui, "/job/1/audio", {"Range": "bytes=-3"}).read() == b"789"
    with pytest.raises(urllib.error.HTTPError) as e:
        get(ui, "/job/1/audio", {"Range": "bytes=50-"})
    assert e.value.code == 416
    with pytest.raises(urllib.error.HTTPError) as e:
        get(ui, "/job/2/audio")
    assert e.value.code == 404


def test_review_page_has_player_and_add_button(ui):
    make_job(ui)
    page = get(ui, "/job/1").read().decode()
    assert 'src="/job/1/audio"' in page and "addRow()" in page and "playFrom(this)" in page
    assert r"/^\d{1,2}(:\d{1,2}){1,2}$/" in page


def test_save_keeps_rows_added_on_the_page(ui):
    d = make_job(ui)
    form = {"album": "Mix", "album_artist": "DJ", "fmt": "m4a",
            "inc_0": "on", "time_0": "00:00", "artist_0": "A", "title_0": "One",
            "time_1": "03:20", "artist_1": "B", "title_1": "Two",          # unticked
            "inc_2": "on", "time_2": "", "artist_2": "", "title_2": "",   # blank rows are ignored
            "inc_6": "on", "time_6": "08:00", "artist_6": "G", "title_6": "Seven"}  # 5th added row
    post(ui, "/job/1/split", form)
    rows = json.loads((d / "reviewed.json").read_text())
    assert [(r["title"], r["start"], r["include"]) for r in rows] == [
        ("One", 0, True), ("Two", 200, False), ("Seven", 480, True)]
    assert json.loads((d / "review_meta.json").read_text())["fmt"] == "m4a"
    assert ui.queued == [(1, "split")] and ui.jobs[1]["status"] == "splitting"


def test_retry_reanalyzes_or_returns_to_review(ui):
    make_job(ui, status="failed", audio=None)
    assert "Try again" in get(ui, "/job/1").read().decode()
    post(ui, "/job/1/retry")
    assert ui.queued == [(1, "analyze")] and ui.jobs[1]["status"] == "queued"
    ui.queued.clear()
    ui.jobs.clear()
    import shutil
    shutil.rmtree(ui.jdir(1))
    make_job(ui, status="failed")  # analysis was finished, only saving failed
    post(ui, "/job/1/retry")
    assert ui.queued == [] and ui.jobs[1]["status"] == "review"


def test_navidrome_scan(ui, monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"subsonic-response": seen["reply"]}).encode()

    def fake_open(url, timeout=None):
        seen["url"] = url
        return Reply()

    monkeypatch.setattr(ui, "NAVIDROME_URL", "http://navidrome:4533")
    monkeypatch.setattr(ui, "NAVIDROME_USER", "admin")
    monkeypatch.setattr(ui, "NAVIDROME_PASSWORD", "secret")
    monkeypatch.setattr(ui.urllib.request, "urlopen", fake_open)
    seen["reply"] = {"status": "ok"}
    assert ui.navidrome_scan() == ""
    assert seen["url"].startswith("http://navidrome:4533/rest/startScan.view?u=admin&t=") and "secret" not in seen["url"]
    seen["reply"] = {"status": "failed", "error": {"message": "Wrong username or password"}}
    assert ui.navidrome_scan() == "Wrong username or password"

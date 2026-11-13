"""Offline tests: tracklist parsing, matching, cut points, and real ffmpeg splitting/tagging."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trackid as t  # noqa: E402

DESCRIPTION = """A crunk hip-hop mixtape.
Bookings
mgmt@example.com
Tracklist:
Huey – Pop, Lock & Drop It
Crime Mob – Knuck If You Buck
Ludacris – What’s Your Fantasy
Lil Wayne – Lollipop
D4L – Laffy Taffy
Dem Franchize Boyz – I Think They Like Me (Remix)
"""


def hit(tt, key, artist, title, offset=20):
    return {"t": tt, "key": key, "artist": artist, "title": title, "offset": offset, "url": ""}


HITS = [hit(0, "1", "Huey", "Pop, Lock & Drop It", 5), hit(30, "1", "Huey", "Pop, Lock & Drop It", 35),
        hit(60, "2", "Crime Mob", "Knuck If You Buck (feat. Lil Scrappy)", 40),
        hit(90, "2", "Crime Mob", "Knuck If You Buck", 70),
        hit(120, "x", "Some Producer", "Random Sample", 3),
        hit(210, "4", "Lil Wayne", "Lollipop", 30), hit(240, "4", "Lil Wayne", "Lollipop", 60),
        hit(300, "5", "D4L", "Laffy Taffy", 10), hit(330, "5", "D4L", "Laffy Taffy", 40)]


def test_parse_description():
    k = t.parse_tracklist(DESCRIPTION)
    assert [x["artist"] for x in k][:2] == ["Huey", "Crime Mob"]
    assert len(k) == 6
    assert k[5]["title"] == "I Think They Like Me (Remix)"
    assert all(x["time"] is None for x in k)


def test_parse_timestamps():
    k = t.parse_tracklist("0:00 Intro\n03:12 Huey - Pop Lock\n1:02:30 Nelly - Grillz\n[45:00] D4L – Laffy Taffy")
    assert [x["time"] for x in k] == [0, 192, 3750, 2700]
    assert k[3]["title"] == "Laffy Taffy"


def test_reconcile_and_split():
    known = t.parse_tracklist(DESCRIPTION)[:5]
    rows = t.reconcile(t.merge_hits(HITS, 30), known, 360)
    by_title = {r["title"]: r for r in rows}
    assert by_title["Knuck If You Buck"]["source"] == "posted+shazam"
    assert by_title["What’s Your Fantasy"]["confidence"].startswith("est")
    segs = t.split_points(rows, 360)
    titles = [r["title"] for _, _, r in segs]
    assert titles == ["Pop, Lock & Drop It", "Knuck If You Buck", "What’s Your Fantasy", "Lollipop", "Laffy Taffy"]
    assert segs[0][0] == 0 and segs[-1][1] == 360
    assert all(b > a for a, b, _ in segs)


def test_confirmed_only_skips_estimates():
    known = t.parse_tracklist(DESCRIPTION)[:5]
    rows = t.reconcile(t.merge_hits(HITS, 30), known, 360)
    titles = [r["title"] for _, _, r in t.split_points(rows, 360, include_estimated=False)]
    assert "What’s Your Fantasy" not in titles


def test_write_tracks(tmp_path):
    mix = tmp_path / "mix.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=360",
                    "-c:a", "aac", str(mix)], check=True)
    known = t.parse_tracklist(DESCRIPTION)[:5]
    segs = t.split_points(t.reconcile(t.merge_hits(HITS, 30), known, 360), 360)
    folder, files = t.write_tracks(str(mix), segs, tmp_path / "lib", "Test Mix", "DJ Test", "2025",
                                   "https://youtu.be/x", None)
    assert folder == tmp_path / "lib" / "DJ Test" / "Test Mix"
    assert [f.name for f in files][0] == "01 - Huey - Pop, Lock & Drop It.mp3"
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(files[1])],
                                      capture_output=True, text=True).stdout)
    tags = {k.lower(): v for k, v in probe["format"]["tags"].items()}
    assert tags["artist"] == "Crime Mob" and tags["album"] == "Test Mix"
    assert tags["album_artist"] == "DJ Test" and tags["track"] == "2/5"
    assert abs(float(probe["format"]["duration"]) - 75) < 1.5


def test_clean_title():
    assert t.clean_mix_title("Throwbacks Only (Vol 2) || Dirty South Hip-Hop Mix || Lil Jon, Juvenile") == \
        "Throwbacks Only (Vol 2) - Dirty South Hip-Hop Mix"


def test_segments_from_reviewed_rows():
    rows = [{"title": "A", "start": 100, "include": True}, {"title": "B", "start": 200, "include": False},
            {"title": "C", "start": 300, "include": True}, {"title": "D", "start": None, "include": True}]
    segs = t.segments_from_rows(rows, 400)
    assert [(a, b, r["title"]) for a, b, r in segs] == [(0, 300, "A"), (300, 400, "C")]


def test_friendly_labels():
    assert t.friendly_confidence({"confidence": "estimated (~)"})[0] == "est"
    assert t.friendly_confidence({"confidence": "high", "source": "posted+shazam", "hits": 4})[1].startswith("Confirmed")
    assert t.friendly_confidence({"confidence": "low (not on posted list)"})[0] == "warn"


def test_resave_removes_old_numbered_files(tmp_path):
    mix = tmp_path / "mix.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=90",
                    "-c:a", "aac", str(mix)], check=True)
    row = lambda artist, title: {"artist": artist, "title": title}  # noqa: E731
    args = (tmp_path / "lib", "Test Mix", "DJ Test", "2025", "", None)
    folder, first = t.write_tracks(str(mix), [(0, 30, row("A", "One")), (30, 60, row("B", "Two")),
                                              (60, 90, row("C", "Three"))], *args)
    (folder / "00 - Test Mix (Full Mix).m4a").write_bytes(b"x")
    (folder / "tracklist.csv").write_text("x")
    folder, second = t.write_tracks(str(mix), [(0, 45, row("A", "One")), (45, 90, row("C", "Three"))], *args)
    assert sorted(p.name for p in folder.iterdir()) == [
        "00 - Test Mix (Full Mix).m4a", "01 - A - One.mp3", "02 - C - Three.mp3", "tracklist.csv"]


def test_scan_resumes_from_cache(tmp_path, monkeypatch):
    import asyncio
    import types
    mix = tmp_path / "mix.wav"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=120",
                    str(mix)], check=True)
    asked = []

    class FakeShazam:
        def __init__(self):
            self.http_client = types.SimpleNamespace(close=self._close)

        async def _close(self):
            pass

        async def recognize(self, clip, proxy=None):
            start = int(Path(clip).stem)
            asked.append(start)
            if start == 60:
                raise RuntimeError("rate limited")
            return {"track": {"key": "k1", "title": "Song", "subtitle": "Artist"}, "matches": [{"offset": 5}]}

    async def no_sleep(_):
        pass

    monkeypatch.setitem(sys.modules, "shazamio", types.SimpleNamespace(Shazam=FakeShazam))
    monkeypatch.setattr(t.asyncio, "sleep", no_sleep)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    cache = tmp_path / "scan_cache.json"
    hits = asyncio.run(t.scan(str(mix), 120, 30, 12, 2, cache_path=cache))["hits"]
    assert sorted(h["t"] for h in hits) == [0, 30, 90]
    assert sorted(set(asked)) == [0, 30, 60, 90]
    asked.clear()
    hits = asyncio.run(t.scan(str(mix), 120, 30, 12, 2, cache_path=cache))["hits"]
    assert sorted(h["t"] for h in hits) == [0, 30, 90]
    assert set(asked) == {60}  # only the clip that failed last time is asked again


# ---- adaptive listening, against a pretend mix (no audio, no network)

def fake_mix(monkeypatch, songs, duration, unknown=()):
    """songs = [(start, name, drop_in)]: `name` plays from `start`; the DJ starts it `drop_in` seconds into the song.
    The pretend Shazam names whatever is playing at the middle of each 12s clip. Returns the list of clips asked."""
    import types
    asked = []

    def playing(sec):
        cur = None
        for start, name, drop in songs:
            if start <= sec:
                cur = (start, name, drop)
        return cur

    class FakeShazam:
        def __init__(self):
            async def close():
                pass
            self.http_client = types.SimpleNamespace(close=close)

        async def recognize(self, clip, proxy=None):
            start = int(Path(clip).stem)
            asked.append(start)
            cur = playing(start + 6)
            if not cur or cur[1] in unknown:
                return {}
            return {"track": {"key": cur[1], "title": cur[1], "subtitle": "Artist " + cur[1]},
                    "matches": [{"offset": max(0, start - cur[0] + cur[2])}]}

    async def no_sleep(_):
        pass

    monkeypatch.setitem(sys.modules, "shazamio", types.SimpleNamespace(Shazam=FakeShazam))
    monkeypatch.setattr(t.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(t, "cut_clip", lambda src, start, length, dst: Path(dst).write_bytes(b""))
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    return asked


def run_scan(duration, known=None, adaptive=True, step=30):
    import asyncio
    heard = asyncio.run(t.scan("mix", duration, step, 12, 4, known=known, adaptive=adaptive))
    return {s["title"]: s for s in t.merge_hits(heard["hits"], step, heard["asked"], heard["implied"])}


# starts are deliberately off the 30s grid; odd-numbered songs are dropped in 45s into the song
MIX = [(0, "S0", 0), (171, "S1", 45), (403, "S2", 0), (538, "S3", 45), (791, "S4", 0), (1004, "S5", 45),
       (1262, "S6", 0), (1377, "S7", 45), (1589, "S8", 0)]
MIX_LEN = 1800


def test_adaptive_scan_finds_start_times(monkeypatch):
    asked = fake_mix(monkeypatch, MIX, MIX_LEN)
    songs = run_scan(MIX_LEN)
    assert sorted(songs) == sorted(name for _, name, _ in MIX)
    for start, name, drop in MIX:
        got = songs[name]["start"]
        if drop == 0:
            assert abs(got - start) <= 1, (name, got)        # played from the top: Shazam's offset gives it exactly
        else:
            assert start - 15 <= got <= start, (name, got)   # dropped in mid-song: a little early, never late
    assert all(s["confidence"] == "high" for s in songs.values())
    assert len(set(asked)) < MIX_LEN / 30  # fewer clips than asking about every one

    # the old every-clip scan is up to a whole step early on the mid-song drop-ins
    asked.clear()
    old = run_scan(MIX_LEN, adaptive=False)
    assert len(set(asked)) == MIX_LEN / 30
    assert max(start - old[name]["start"] for start, name, drop in MIX if drop) > 20


def test_adaptive_scan_with_tracklist_skips_the_gap_fill(monkeypatch):
    asked = fake_mix(monkeypatch, MIX, MIX_LEN)
    known = [{"artist": "Artist " + name, "title": name, "time": None, "order": i}
             for i, (_, name, _) in enumerate(MIX)]
    songs = run_scan(MIX_LEN, known=known)
    with_list = len(set(asked))
    for start, name, drop in MIX:
        assert start - 15 <= songs[name]["start"] <= start + 1, (name, songs[name]["start"])
    rows = t.reconcile(list(songs.values()), known, MIX_LEN)
    assert all(r["source"] == "posted+shazam" for r in rows) and len(rows) == len(MIX)
    assert with_list < MIX_LEN / 30


def test_adaptive_scan_looks_harder_for_missing_posted_songs(monkeypatch):
    # S3 is only 15s long and sits between two clips of the quick pass and the gap fill
    mix = [(0, "S0", 0), (200, "S1", 0), (432, "S3", 0), (447, "S2", 0)]
    asked = fake_mix(monkeypatch, mix, 700, unknown={"S1"})
    known = [{"artist": "Artist " + n, "title": n, "time": None, "order": i} for i, n in enumerate(["S0", "S1", "S3", "S2"])]
    songs = run_scan(700, known=known)
    assert "S3" in songs and any(x % 30 for x in asked)

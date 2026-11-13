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

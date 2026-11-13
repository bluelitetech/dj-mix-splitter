#!/usr/bin/env python3
"""
trackid.py - build a timestamped tracklist for a DJ mix on YouTube (or a local audio file).

How it works
  1. Reads the video's title/description/chapters with yt-dlp and pulls out any
     tracklist the uploader posted (with or without timestamps).
  2. Downloads the audio, cuts a short clip every --step seconds, and asks Shazam
     what each clip is.
  3. Merges the hits into tracks with start times (using Shazam's offset to work out
     where each song actually began in the mix).
  4. If a posted tracklist exists, matches the hits against it: posted tracks get
     real timestamps, the order is corrected, and any posted song Shazam missed is
     slotted in by its posted order with an estimated (~) time.

Usage
  python trackid.py "https://youtu.be/Ir4-KC0cYOc"
  python trackid.py mix.mp3 --tracklist known.txt     # local file + your own list
  python trackid.py URL --step 20                      # denser scan (slower, catches short blends)

Outputs (in --out, default ./output):
  <id>_tracklist.csv        track #, timestamp, artist, title, source, confidence
  <id>_chapters.txt         "00:00 Artist - Title" lines, ready to paste into YouTube
"""
import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

# --------------------------------------------------------------------------- helpers

def fmt_ts(sec):
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(text):
    parts = [int(p) for p in text.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


def norm(s):
    """Lowercase, strip accents/feat./remix tags/punctuation for fuzzy matching."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\((?:[^)]*?(?:mix|edit|remix|version|clean|dirty|explicit)[^)]*)\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\b(feat|ft|featuring|x|and|with)\b\.?", " ", s)
    s = s.replace("&", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def similarity(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(ratio, overlap * 0.95)


def title_similarity(known, hit):
    """Score a posted track against a Shazam hit. Title matters most; artist breaks ties."""
    t = similarity(known["title"], hit["title"])
    a = similarity(known["artist"], hit["artist"]) if known["artist"] else 0.5
    full = similarity(f'{known["artist"]} {known["title"]}', f'{hit["artist"]} {hit["title"]}')
    return max(full, 0.7 * t + 0.3 * a)


# --------------------------------------------------------------------------- tracklist parsing

TS_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])")
SEP_RE = re.compile(r"\s+[-–—]\s+")
NUM_RE = re.compile(r"^\s*(?:#?\d{1,3}[.)\]:-]?\s+|\[\d{1,3}\]\s*)")


def split_artist_title(text):
    text = NUM_RE.sub("", text).strip(" -–—|•*\t")
    parts = SEP_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", text.strip()


def parse_tracklist(text):
    """Return list of {artist,title,time(None|sec),order} from free text (description, comment, file)."""
    tracks = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.search(r"https?://|www\.|@|\.com\b", line, re.I):
            continue
        m = TS_RE.search(line)
        time = parse_ts(m.group(1)) if m else None
        rest = (line[: m.start()] + " " + line[m.end():]) if m else line
        rest = re.sub(r"[(\[]\s*[)\]]", " ", rest)  # empty brackets left behind by "[03:12]"
        rest = rest.strip(" -–—|\t")
        has_sep = bool(SEP_RE.search(rest))
        numbered = bool(NUM_RE.match(rest))
        if not (has_sep or (time is not None and rest)):
            continue
        if not has_sep and not numbered and time is None:
            continue
        artist, title = split_artist_title(rest)
        if len(title) < 2 or len(rest) > 150:
            continue
        tracks.append({"artist": artist, "title": title, "time": time, "order": len(tracks)})
    # a "tracklist" of 1-2 lines with dashes is probably just prose
    return tracks if len(tracks) >= 3 else []


# --------------------------------------------------------------------------- youtube

def is_url(s):
    return bool(re.match(r"https?://", s))


def ytdlp():
    """yt-dlp command, plus a cookies file if YTDLP_COOKIES points to one (helps when YouTube asks to sign in)."""
    cmd = ["yt-dlp"]
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies and Path(cookies).is_file():
        cmd += ["--cookies", cookies]
    return cmd


def yt_info(url):
    out = subprocess.run(
        ytdlp() + ["--skip-download", "--dump-single-json", "--no-warnings", url],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"yt-dlp could not read the video:\n{out.stderr[-1500:]}")
    return json.loads(out.stdout)


def yt_comments(url, limit=60):
    out = subprocess.run(
        ytdlp() + ["--skip-download", "--dump-single-json", "--no-warnings", "--write-comments",
         "--extractor-args", f"youtube:max_comments={limit},all,0,0;comment_sort=top", url],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return []
    return [c.get("text", "") for c in json.loads(out.stdout).get("comments") or []]


def yt_audio(url, workdir):
    tmpl = str(Path(workdir) / "%(id)s.%(ext)s")
    out = subprocess.run(
        ytdlp() + ["-f", "bestaudio/best", "-o", tmpl, "--no-playlist", "--no-warnings", url],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"yt-dlp could not download the audio:\n{out.stderr[-1500:]}")
    files = [p for p in Path(workdir).iterdir() if not p.name.endswith((".part", ".json"))]
    return str(max(files, key=lambda p: p.stat().st_size))


def audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip())


# --------------------------------------------------------------------------- recognition

def cut_clip(src, start, length, dst):
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", str(start), "-t", str(length), "-i", src,
         "-ac", "1", "-ar", "16000", dst],
        check=True,
    )


async def scan(audio, duration, step, clip_len, concurrency):
    from shazamio import Shazam

    shazam = Shazam()
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    sem = asyncio.Semaphore(concurrency)
    tmp = tempfile.mkdtemp(prefix="trackid_")
    starts = [t for t in range(0, int(duration - clip_len / 2), step)]
    hits, done = [], 0

    async def one(t):
        nonlocal done
        clip = os.path.join(tmp, f"{t}.wav")
        await asyncio.to_thread(cut_clip, audio, t, clip_len, clip)
        async with sem:
            for attempt in range(3):
                try:
                    res = await shazam.recognize(clip, proxy=proxy) if proxy else await shazam.recognize(clip)
                    break
                except Exception as e:  # rate limit / network blip
                    if attempt == 2:
                        print(f"\n  ! {fmt_ts(t)} failed: {e}", file=sys.stderr)
                        res = {}
                    await asyncio.sleep(3 * (attempt + 1))
            await asyncio.sleep(0.4)
        done += 1
        print(f"\r  scanning {done}/{len(starts)} clips", end="", file=sys.stderr, flush=True)
        os.remove(clip)
        track = res.get("track")
        if not track:
            return
        offset = 0.0
        if res.get("matches"):
            offset = float(res["matches"][0].get("offset") or 0)
        hits.append({
            "t": t, "key": track.get("key"), "title": track.get("title", ""),
            "artist": track.get("subtitle", ""), "offset": offset,
            "url": track.get("url", ""),
        })

    try:
        await asyncio.gather(*(one(t) for t in starts))
    finally:
        await shazam.http_client.close()  # avoids "Unclosed client session" noise in logs
    print(file=sys.stderr)
    shutil.rmtree(tmp, ignore_errors=True)
    return sorted(hits, key=lambda h: h["t"])


def merge_hits(hits, step):
    """Group clip hits by song -> one entry per song with an estimated start time."""
    by_key = {}
    for h in hits:
        by_key.setdefault(h["key"], []).append(h)
    songs = []
    for key, hs in by_key.items():
        first = min(h["t"] for h in hs)
        # where the song started in the mix = clip start - position inside the song.
        # DJs often drop in mid-song, so never put it earlier than ~1 step before first hit.
        est = [h["t"] - h["offset"] for h in hs if h["offset"]]
        start = statistics.median(est) if est else first
        start = max(start, first - step, 0)
        songs.append({
            "key": key, "artist": hs[0]["artist"], "title": hs[0]["title"], "url": hs[0]["url"],
            "start": start, "first": first, "last": max(h["t"] for h in hs), "hits": len(hs),
        })
    songs.sort(key=lambda s: s["start"])
    # a single isolated hit inside another song's run is usually a sample / misfire
    for s in songs:
        s["confidence"] = "high" if s["hits"] >= 3 else "medium" if s["hits"] == 2 else "low"
    return songs


# --------------------------------------------------------------------------- reconcile with posted list

def reconcile(songs, known, duration, posted_order_is_reliable=True):
    rows = []
    used_known = set()
    for s in songs:
        best, best_i = 0.0, None
        for i, k in enumerate(known):
            if i in used_known:
                continue
            sc = title_similarity(k, s)
            if sc > best:
                best, best_i = sc, i
        if best_i is not None and best >= 0.6:
            used_known.add(best_i)
            k = known[best_i]
            rows.append({**s, "artist": k["artist"] or s["artist"], "title": k["title"],
                         "shazam": f'{s["artist"]} - {s["title"]}', "source": "posted+shazam",
                         "known_order": k["order"]})
        else:
            if s["confidence"] == "low" and known:
                # one-off hit not on the posted list: likely a sample/misfire, keep but flag
                s = {**s, "confidence": "low (not on posted list)"}
            rows.append({**s, "shazam": f'{s["artist"]} - {s["title"]}', "source": "shazam",
                         "known_order": None})

    missing = [k for i, k in enumerate(known) if i not in used_known]
    for k in missing:
        if k["time"] is not None:
            rows.append({"artist": k["artist"], "title": k["title"], "start": k["time"],
                         "source": "posted", "confidence": "posted timestamp", "shazam": "",
                         "known_order": k["order"], "url": ""})
            continue
        if not posted_order_is_reliable:
            rows.append({"artist": k["artist"], "title": k["title"], "start": None,
                         "source": "posted", "confidence": "not detected", "shazam": "",
                         "known_order": k["order"], "url": ""})
            continue
        # place between the nearest posted neighbours that Shazam did find
        before = [r for r in rows if r.get("known_order") is not None and r["known_order"] < k["order"]
                  and r["start"] is not None]
        after = [r for r in rows if r.get("known_order") is not None and r["known_order"] > k["order"]
                 and r["start"] is not None]
        lo = max(before, key=lambda r: r["known_order"]) if before else None
        hi = min(after, key=lambda r: r["known_order"]) if after else None
        lo_t, lo_o = (lo["start"], lo["known_order"]) if lo else (0, -1)
        hi_t, hi_o = (hi["start"], hi["known_order"]) if hi else (duration, len(known))
        frac = (k["order"] - lo_o) / max(1, hi_o - lo_o)
        rows.append({"artist": k["artist"], "title": k["title"], "start": lo_t + frac * (hi_t - lo_t),
                     "source": "posted", "confidence": "estimated (~)", "shazam": "",
                     "known_order": k["order"], "url": ""})

    rows.sort(key=lambda r: (r["start"] is None, r["start"] if r["start"] is not None else 0))
    return rows


# --------------------------------------------------------------------------- split + tag for a music library

def safe_name(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", s).strip(" .")
    return (s or "Unknown")[:maxlen]


def clean_mix_title(title):
    """'Throwbacks Only (Vol 2) || Dirty South Hip-Hop Mix || Lil Jon, ...' -> 'Throwbacks Only (Vol 2) - Dirty South Hip-Hop Mix'"""
    parts = [p.strip() for p in re.split(r"\s*(?:\|\||\||//)\s*", title) if p.strip()]
    return " - ".join(parts[:2]) if parts else title


def split_points(rows, duration, min_len=25, include_estimated=True):
    """Pick the rows that become files and return [(start, end, row)]."""
    have_list = any(r["source"] != "shazam" for r in rows)
    cand = []
    for r in rows:
        if r["start"] is None:
            continue
        if have_list and r["source"] == "shazam" and r["confidence"].startswith("low"):
            continue  # one-off hit that isn't on the posted list: probably a sample
        if not include_estimated and r["confidence"].startswith("est"):
            continue
        cand.append(r)
    # drop entries that would make a too-short file (keep the more trustworthy one)
    changed = True
    while changed and len(cand) > 1:
        changed = False
        for i in range(len(cand)):
            end = cand[i + 1]["start"] if i + 1 < len(cand) else duration
            if end - cand[i]["start"] < min_len:
                if i + 1 >= len(cand):
                    victim = i
                else:
                    # drop the less certain of the two: estimated < audio-confirmed, then fewer hits
                    def strength(r):
                        return ("shazam" in r["source"], r.get("hits", 0))
                    victim = i if strength(cand[i]) < strength(cand[i + 1]) else i + 1
                    if i == 0 and victim == 1 and strength(cand[0]) == strength(cand[1]):
                        victim = 0 if cand[1]["start"] < min_len else 1  # very short intro: the 2nd song starts the mix
                cand.pop(victim)
                changed = True
                break
    out = []
    for i, r in enumerate(cand):
        start = 0 if i == 0 else r["start"]  # first file includes the intro
        end = cand[i + 1]["start"] if i + 1 < len(cand) else duration
        out.append((start, end, r))
    return out


def fetch_cover(info, dest):
    url = info.get("thumbnail")
    if not url:
        return None
    try:
        import urllib.request
        raw = dest.with_suffix(".src")
        urllib.request.urlretrieve(url, raw)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-q:v", "2", str(dest)], check=True)
        raw.unlink(missing_ok=True)
        return dest
    except Exception as e:
        print(f"  (no cover art: {e})", file=sys.stderr)
        return None


def write_tracks(audio, segments, library, album, album_artist, year, source_url, cover, fmt="mp3"):
    folder = Path(library) / safe_name(album_artist) / safe_name(album)
    folder.mkdir(parents=True, exist_ok=True)
    if cover:
        shutil.copy(cover, folder / "cover.jpg")
    total = len(segments)
    written = []
    for n, (start, end, r) in enumerate(segments, 1):
        artist = r["artist"] or "Unknown Artist"
        name = f"{n:02d} - {safe_name(artist, 60)} - {safe_name(r['title'], 80)}.{fmt}"
        dst = folder / name
        cmd = ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", audio]
        if cover and fmt == "mp3":
            cmd += ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                    "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        else:
            cmd += ["-map", "0:a"]
        if fmt == "mp3":
            cmd += ["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"]
        else:  # m4a
            cmd += ["-c:a", "aac", "-b:a", "256k"]
        tags = {
            "title": r["title"], "artist": artist, "album": album, "album_artist": album_artist,
            "track": f"{n}/{total}", "disc": "1/1", "genre": "DJ Mix", "compilation": "1",
            "comment": f"From {source_url} @ {fmt_ts(start)}" if source_url else f"@ {fmt_ts(start)}",
        }
        if year:
            tags["date"] = str(year)
        for k, v in tags.items():
            cmd += ["-metadata", f"{k}={v}"]
        subprocess.run(cmd + [str(dst)], check=True)
        written.append(dst)
    return folder, written


# --------------------------------------------------------------------------- pipeline

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def process(source, out="output", library=None, tracklist_text=None, unordered=False, comments=False,
            step=30, clip=12, concurrency=3, min_confidence="low", include_estimated=True,
            fmt="mp3", keep_full_mix=False, album=None, album_artist=None):
    """Full run. Returns dict with rows, files and folder. Used by the CLI and the web page."""
    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    known, label, info = [], "mix", {}
    workdir = tempfile.mkdtemp(prefix="trackid_dl_")
    try:
        if is_url(source):
            log("Reading video info...")
            info = yt_info(source)
            label = info.get("id", "mix")
            log(f"  {info.get('title')}  [{info.get('duration_string')}]")
            if info.get("chapters"):
                known = [{**dict(zip(("artist", "title"), split_artist_title(c["title"]))),
                          "time": c["start_time"], "order": i} for i, c in enumerate(info["chapters"])]
                log(f"  found {len(known)} chapters")
            if not known:
                known = parse_tracklist(info.get("description") or "")
                if known:
                    log(f"  found a {len(known)}-track list in the description "
                        f"({'with' if any(k['time'] is not None for k in known) else 'no'} timestamps)")
            if not known and comments:
                log("  checking comments...")
                for c in yt_comments(source):
                    known = parse_tracklist(c)
                    if known:
                        log(f"  found a {len(known)}-track list in a comment")
                        break
            log("Downloading audio...")
            audio = yt_audio(source, workdir)
        else:
            audio = source
            label = Path(audio).stem
        if tracklist_text:
            known = parse_tracklist(tracklist_text) or known
            log(f"  using {len(known)} tracks from the supplied tracklist")

        duration = audio_duration(audio)
        log(f"Identifying songs (one {clip}s clip every {step}s)...")
        hits = asyncio.run(scan(audio, duration, step, clip, concurrency))
        songs = merge_hits(hits, step)
        rank = {"low": 0, "medium": 1, "high": 2}
        songs = [s for s in songs if rank[s["confidence"]] >= rank[min_confidence]]
        rows = reconcile(songs, known, duration, posted_order_is_reliable=not unordered)

        csv_path, chap_path = write_reports(rows, outdir, label)
        print_table(rows, known)
        result = {"rows": rows, "csv": csv_path, "chapters": chap_path, "folder": None, "files": []}

        if library:
            album = album or clean_mix_title(info.get("title") or label)
            album_artist = album_artist or info.get("channel") or info.get("uploader") or "Various DJs"
            year = (info.get("upload_date") or "")[:4] or None
            segments = split_points(rows, duration, include_estimated=include_estimated)
            log(f"Writing {len(segments)} tracks to the library...")
            cover = fetch_cover(info, Path(workdir) / "cover.jpg") if info else None
            folder, files = write_tracks(audio, segments, library, album, album_artist, year,
                                         info.get("webpage_url") if info else None, cover, fmt)
            shutil.copy(csv_path, folder / "tracklist.csv")
            if keep_full_mix:
                full = folder / f"00 - {safe_name(album)} (Full Mix){Path(audio).suffix}"
                shutil.copy(audio, full)
            result.update(folder=folder, files=files)
            log(f"Done: {folder}")
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def write_reports(rows, outdir, label):
    csv_path = Path(outdir) / f"{label}_tracklist.csv"
    chap_path = Path(outdir) / f"{label}_chapters.txt"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["#", "time", "artist", "title", "source", "confidence", "shazam_said", "shazam_url"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, row_time(r), r["artist"], r["title"], r["source"], r["confidence"],
                        r.get("shazam", ""), r.get("url", "")])
    lines = [(r["start"], f'{r["artist"]} - {r["title"]}' if r["artist"] else r["title"])
             for r in rows if r["start"] is not None]
    if lines and lines[0][0] > 0:
        lines[0] = (0, lines[0][1])  # YouTube chapters must start at 0:00
    chap_path.write_text("\n".join(f"{fmt_ts(t)} {n}" for t, n in lines) + "\n", encoding="utf-8")
    return csv_path, chap_path


def row_time(r):
    if r["start"] is None:
        return "?"
    return ("~" if r["confidence"].startswith("est") else "") + fmt_ts(r["start"])


def print_table(rows, known):
    print(f"\n{'#':>3}  {'time':>8}  {'artist - title':<60}  confidence")
    print("-" * 95)
    for i, r in enumerate(rows, 1):
        name = f'{r["artist"]} - {r["title"]}' if r["artist"] else r["title"]
        print(f"{i:>3}  {row_time(r):>8}  {name[:60]:<60}  {r['confidence']}")
    found = sum(1 for r in rows if "shazam" in r["source"])
    print(f"\n{found} songs identified by audio"
          + (f"; {len(known)} on the posted list, {sum(1 for r in rows if r['source'] == 'posted')} placed from the list"
             if known else ""))


def main():
    ap = argparse.ArgumentParser(description="Identify the tracklist of a DJ mix and (optionally) split it into tagged songs.")
    ap.add_argument("source", help="YouTube URL or local audio file")
    ap.add_argument("--library", default=os.environ.get("MUSIC_LIBRARY"),
                    help="music library folder (e.g. your Navidrome /music). If set, the mix is split into tagged "
                         "songs at <library>/<DJ>/<Mix title>/. Env: MUSIC_LIBRARY")
    ap.add_argument("--tracklist", help="text file with a known tracklist (any order, timestamps optional)")
    ap.add_argument("--unordered", action="store_true", help="the posted/known list is NOT in play order")
    ap.add_argument("--comments", action="store_true", help="also search top YouTube comments for a tracklist")
    ap.add_argument("--confirmed-only", action="store_true",
                    help="when splitting, don't cut at estimated (~) positions; those songs stay inside the previous file")
    ap.add_argument("--format", choices=["mp3", "m4a"], default="mp3")
    ap.add_argument("--keep-full-mix", action="store_true", help="also save the whole mix in the album folder")
    ap.add_argument("--album", help="override album name (default: cleaned-up video title)")
    ap.add_argument("--album-artist", help="override album artist (default: YouTube channel = the DJ)")
    ap.add_argument("--step", type=int, default=30, help="seconds between samples (default 30)")
    ap.add_argument("--clip", type=int, default=12, help="clip length in seconds (default 12)")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel Shazam requests (default 3)")
    ap.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low")
    ap.add_argument("--out", default="output", help="folder for the CSV/chapters reports (default ./output)")
    a = ap.parse_args()
    process(a.source, out=a.out, library=a.library,
            tracklist_text=Path(a.tracklist).read_text(encoding="utf-8") if a.tracklist else None,
            unordered=a.unordered, comments=a.comments, step=a.step, clip=a.clip, concurrency=a.concurrency,
            min_confidence=a.min_confidence, include_estimated=not a.confirmed_only, fmt=a.format,
            keep_full_mix=a.keep_full_mix, album=a.album, album_artist=a.album_artist)


if __name__ == "__main__":
    main()

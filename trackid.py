#!/usr/bin/env python3
"""
trackid.py - build a timestamped tracklist for a DJ mix on YouTube (or a local audio file).

How it works
  1. Reads the video's title/description/chapters with yt-dlp and pulls out any
     tracklist the uploader posted (with or without timestamps).
  2. Downloads the audio and asks Shazam what short clips of it are: a quick pass
     first, then closer listening where songs change or are still missing.
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
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
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


# --------------------------------------------------------------------------- progress (read by the web page)

_progress = {}


def progress(stage, pct, text, **extra):
    """Write a small JSON status file if PROGRESS_FILE is set (the web page shows it as a progress bar)."""
    _progress.update(extra)
    _progress.update(stage=stage, pct=round(max(0, min(100, pct)), 1), text=text, updated=time.time())
    path = os.environ.get("PROGRESS_FILE")
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_progress, f)
    os.replace(tmp, path)


def eta_text(started, done, total):
    if done < 3:
        return ""
    left = (time.time() - started) / done * (total - done)
    return f" · about {max(1, round(left / 60))} min left" if left >= 60 else " · less than a minute left"


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


def yt_audio(url, workdir, pct_from=5, pct_to=15):
    tmpl = str(Path(workdir) / "%(id)s.%(ext)s")
    proc = subprocess.Popen(
        ytdlp() + ["-f", "bestaudio/best", "-o", tmpl, "--no-playlist", "--no-warnings", "--newline", url],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    tail, last = [], -1
    for line in proc.stdout:
        tail = (tail + [line])[-30:]
        m = re.search(r"\[download\]\s+([\d.]+)%(?:\s+of\s+~?\s*([\d.]+\w+))?", line)
        if m and int(float(m.group(1))) != last:
            last = int(float(m.group(1)))
            size = f" of {m.group(2)}" if m.group(2) else ""
            progress("download", pct_from + (pct_to - pct_from) * last / 100,
                     f"Downloading the mix audio: {last}%{size}")
    if proc.wait() != 0:
        sys.exit("yt-dlp could not download the audio:\n" + "".join(tail)[-1500:])
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


def load_scan_cache(path, clip_len):
    """Clip results saved by an earlier, interrupted scan of the same audio: {clip start: hit or None}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("clip") == clip_len:
            return {int(k): v for k, v in data["clips"].items()}
    except Exception:
        pass
    return {}


def save_scan_cache(path, clip_len, clips):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"clip": clip_len, "clips": clips}, f)
    os.replace(tmp, path)


REFINE_TO = 8      # stop pinning down a song's start once it's known to within this many seconds
REFINE_ROUNDS = 5  # each round narrows the gap (roughly halves it), so 60s -> 30 -> 15 -> 7.5 with room to spare


def match_known(songs, known, threshold=0.6):
    """Pair each heard song with the best unused posted track. Returns one posted index (or None) per song."""
    used, out = set(), []
    for s in songs:
        best, best_i = 0.0, None
        for i, k in enumerate(known):
            if i in used:
                continue
            sc = title_similarity(k, s)
            if sc > best:
                best, best_i = sc, i
        if best_i is not None and best >= threshold:
            used.add(best_i)
            out.append(best_i)
        else:
            out.append(None)
    return out


def missing_known(heard, known):
    """How many posted tracks none of the heard songs matches."""
    songs = list({h["key"]: h for h in heard}.values())
    return len(known) - sum(1 for i in match_known(songs, known) if i is not None)


def refine_targets(results):
    """Clips worth asking about to pin down where each song starts.

    A song started somewhere between the last clip before its first hit (where something else, or nothing, was
    heard) and that first hit. Shazam's offset narrows it from below: the song can't have started before
    "clip start - position in the song". If that point falls inside the gap, ask about the clip right there - if
    the song is heard, it was played from the top and the start is settled in one go. Otherwise halve the gap."""
    asked = sorted(results)
    first, ests = {}, {}
    for t in asked:
        h = results[t]
        if h:
            first.setdefault(h["key"], t)
            if h["offset"]:
                ests.setdefault(h["key"], []).append(t - h["offset"])
    targets = set()
    for key, t in first.items():
        before = [x for x in asked if x < t]
        if not before:
            continue
        lo = before[-1]
        est = math.ceil(statistics.median(ests[key])) if key in ests else None
        if est is not None and lo < est < t:
            if t - est > REFINE_TO:
                targets.add(est)
        elif t - lo > REFINE_TO:
            targets.add((lo + t) // 2)
    return sorted(targets - set(asked))


async def scan(audio, duration, step, clip_len, concurrency, cache_path=None, known=None, adaptive=True):
    """Ask Shazam what short clips of the mix are. Returns {"hits", "asked", "implied"} (see merge_hits).

    adaptive (default) asks fewer clips where nothing changes and more where it matters:
      1. quick pass   - a clip every 2 x step
      2. fill gaps    - the clips in between, but only where the two neighbours disagree or weren't recognised.
                        Skipped entirely when a posted tracklist is given and every song on it was already heard.
      3. missing      - if posted songs are still missing, extra clips around the unrecognised stretches
      4. start times  - for each song, halve the gap between "first heard" and the clip before it until the
                        start is known to within REFINE_TO seconds
    adaptive=False is the old behaviour: every clip on the step grid, nothing else.
    With cache_path, answers are saved as they arrive, so a retry after a crash/restart only asks about the
    clips that are still missing."""
    from shazamio import Shazam

    shazam = Shazam()
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    sem = asyncio.Semaphore(concurrency)
    tmp = tempfile.mkdtemp(prefix="trackid_")
    last_start = int(duration - clip_len / 2)
    grid = list(range(0, last_start, step))
    cache = load_scan_cache(cache_path, clip_len) if cache_path else {}
    if cache:
        print(f"  reusing {len(cache)} clips from an earlier scan", file=sys.stderr)
    results = {}  # clip start -> hit dict, or None (nothing recognised / Shazam couldn't be asked)
    seen = {}     # key -> (first time, "Artist - Title") for the live list
    progress("scan", 15, f"Listening to the mix: 0 of {len(grid[::2] if adaptive else grid)} clips", found=[])

    async def ask(t):
        """One clip -> hit dict, or None if Shazam heard nothing. Raises if Shazam couldn't be asked."""
        clip = os.path.join(tmp, f"{t}.wav")
        await asyncio.to_thread(cut_clip, audio, t, clip_len, clip)
        try:
            for attempt in range(3):
                try:
                    call = shazam.recognize(clip, proxy=proxy) if proxy else shazam.recognize(clip)
                    res = await asyncio.wait_for(call, timeout=25)  # never hang the whole job on one request
                    break
                except Exception:  # rate limit / network blip / timeout
                    if attempt == 2:
                        raise
                    await asyncio.sleep(3 * (attempt + 1))
            await asyncio.sleep(0.4)
        finally:
            os.remove(clip)
        track = res.get("track")
        if not track:
            return None
        offset = 0.0
        if res.get("matches"):
            offset = float(res["matches"][0].get("offset") or 0)
        return {"t": t, "key": track.get("key"), "title": track.get("title", ""),
                "artist": track.get("subtitle", ""), "offset": offset, "url": track.get("url", "")}

    async def ask_all(times, what, pct_from, pct_to):
        times = [t for t in times if t not in results]
        if not times:
            return
        started, done = time.time(), 0

        async def one(t):
            nonlocal done
            if t in cache:
                hit = cache[t]
            else:
                async with sem:
                    try:
                        hit = await ask(t)
                        cache[t] = hit
                    except Exception as e:  # not cached, so a retry asks again
                        print(f"\n  ! {fmt_ts(t)} skipped: {type(e).__name__} {e}", file=sys.stderr)
                        hit = None
                if cache_path and len(cache) % 10 == 0:
                    save_scan_cache(cache_path, clip_len, cache)
            results[t] = hit
            done += 1
            print(f"\r  {what}: {done}/{len(times)} clips", end="", file=sys.stderr, flush=True)
            if hit and hit["key"] not in seen:
                seen[hit["key"]] = (t, f'{hit["artist"]} - {hit["title"]}')
            found = [f"{fmt_ts(tt)}  {name}" for tt, name in sorted(seen.values())]
            progress("scan", pct_from + (pct_to - pct_from) * done / len(times),
                     f"Listening to the mix ({what}): clip {done} of {len(times)} · {len(seen)} songs recognised so far"
                     + eta_text(started, done, len(times)), found=found)

        await asyncio.gather(*(one(t) for t in times))
        print(file=sys.stderr)

    def key_at(t):
        return (results.get(t) or {}).get("key")

    def heard():
        return [h for h in results.values() if h]

    implied = {}  # key -> clips we didn't ask about because both neighbours were this song
    try:
        if not adaptive:
            await ask_all(grid, "every clip", 15, 90)
        else:
            await ask_all(grid[::2], "quick pass", 15, 50)
            all_found = bool(known) and missing_known(heard(), known) == 0
            fill = []
            for t in grid[1::2]:
                a, b = key_at(t - step), key_at(t + step)
                if a and a == b:
                    implied[a] = implied.get(a, 0) + 1
                elif not all_found:
                    fill.append(t)
            await ask_all(fill, "filling the gaps", 50, 68)
            if known and missing_known(heard(), known):
                half = max(1, step // 2)
                extra = sorted({x for t in grid if key_at(t) is None for x in (t - half, t + half)
                                if 0 <= x < last_start})
                await ask_all(extra, "looking for songs still missing", 68, 74)
            for rnd in range(REFINE_ROUNDS):
                mids = refine_targets(results)
                if not mids:
                    break
                await ask_all(mids, "pinning down start times", 74 + 16 * rnd / REFINE_ROUNDS,
                              74 + 16 * (rnd + 1) / REFINE_ROUNDS)
    finally:
        if cache_path:
            save_scan_cache(cache_path, clip_len, cache)
        await shazam.http_client.close()  # avoids "Unclosed client session" noise in logs
        shutil.rmtree(tmp, ignore_errors=True)
    on_grid = set(grid)
    hits = sorted((dict(h, grid=t in on_grid) for t, h in results.items() if h), key=lambda h: h["t"])
    print(f"  asked about {len(results)} clips ({len(grid)} would be every clip)", file=sys.stderr)
    return {"hits": hits, "asked": {t: (h or {}).get("key") for t, h in results.items()}, "implied": implied}


def merge_hits(hits, step, asked=None, implied=None):
    """Group clip hits by song -> one entry per song with an estimated start time.

    asked   = {clip start: key or None} for every clip Shazam was asked about (from scan). The latest clip before
              a song's first hit bounds how early the song can have started. Without it: one step before.
    implied = {key: n} clips skipped because both neighbours were that song; they count as heard."""
    by_key = {}
    for h in hits:
        by_key.setdefault(h["key"], []).append(h)
    asked_times = sorted(asked) if asked else None
    songs = []
    for key, hs in by_key.items():
        first = min(h["t"] for h in hs)
        if asked_times is None:
            lo = first - step
        else:
            lo = max((t for t in asked_times if t < first), default=0)
        # where the song started in the mix = clip start - position inside the song. That is exact when the DJ
        # plays the song from its beginning, but DJs often drop in mid-song, so keep it between the last clip
        # where the song wasn't heard yet and the first clip where it was.
        est = [h["t"] - h["offset"] for h in hs if h["offset"]]
        start = statistics.median(est) if est else first
        start = max(min(start, first + 10), lo, 0)
        # "how sure" counts clips on the regular grid only: the extra start-time clips overlap each other
        count = sum(1 for h in hs if h.get("grid", True)) + (implied or {}).get(key, 0)
        songs.append({
            "key": key, "artist": hs[0]["artist"], "title": hs[0]["title"], "url": hs[0]["url"],
            "start": start, "first": first, "last": max(h["t"] for h in hs), "hits": max(1, count),
        })
    songs.sort(key=lambda s: s["start"])
    # a single isolated hit inside another song's run is usually a sample / misfire
    for s in songs:
        s["confidence"] = "high" if s["hits"] >= 3 else "medium" if s["hits"] == 2 else "low"
    return songs


# --------------------------------------------------------------------------- reconcile with posted list

def reconcile(songs, known, duration, posted_order_is_reliable=True):
    rows = []
    matched = match_known(songs, known)
    used_known = {i for i in matched if i is not None}
    for s, best_i in zip(songs, matched):
        if best_i is not None:
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


def write_tracks(audio, segments, library, album, album_artist, year, source_url, cover, fmt="mp3",
                 on_file=None):
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
        if on_file:
            on_file(n - 1, f"{artist} - {r['title']}")
        subprocess.run(cmd + [str(dst)], check=True)
        written.append(dst)
    remove_stale_tracks(folder, written)
    return folder, written


def remove_stale_tracks(folder, written):
    """After re-saving a mix, delete numbered song files left over from the previous save ("NN - Artist - Title")
    that the new tracklist didn't write again. The optional full-mix file and anything else is left alone."""
    keep = {p.name for p in written}
    removed = []
    for p in Path(folder).iterdir():
        if (p.is_file() and p.name not in keep and "(Full Mix)" not in p.name
                and re.fullmatch(r"\d{2,} - .+ - .+\.(mp3|m4a)", p.name)):
            p.unlink()
            removed.append(p.name)
    if removed:
        print(f"  removed {len(removed)} old file(s) from an earlier save: " + ", ".join(removed), file=sys.stderr)
    return removed


# --------------------------------------------------------------------------- pipeline
#   analyze()  ->  state.json (+ cached audio) in a job folder   -> review / edit
#   split()    ->  tagged files in the music library

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def friendly_confidence(r):
    """Plain-English label for the review table."""
    c, src, hits = r.get("confidence", ""), r.get("source", ""), r.get("hits", 0)
    if c.startswith("est"):
        return ("est", "Estimated from the posted order")
    if c == "not detected":
        return ("miss", "Not heard - add a time to include it")
    if c == "posted timestamp":
        return ("posted", "Time from the posted tracklist")
    if c == "edited":
        return ("ok", "Edited by you")
    if "not on posted list" in c:
        return ("warn", "Heard once, not on posted list (probably a sample)")
    if c == "low":
        return ("warn", "Heard once - check it")
    return ("ok", f"Confirmed - heard {hits}x" + (" + on posted list" if "posted" in src else ""))


def segments_from_rows(rows, duration):
    """Cut points exactly as given (used after the user reviewed/edited the list)."""
    chosen = sorted((r for r in rows if r.get("include") and r.get("start") is not None), key=lambda r: r["start"])
    out = []
    for i, r in enumerate(chosen):
        start = 0 if i == 0 else r["start"]
        end = chosen[i + 1]["start"] if i + 1 < len(chosen) else duration
        if end > start:
            out.append((start, end, r))
    return out


def analyze(source, state_dir, tracklist_text=None, unordered=False, comments=False, step=30, clip=12,
            concurrency=4, min_confidence="low", include_estimated=True, verify=False, simple_scan=False):
    """Find the songs and save everything needed to split later into state_dir/state.json."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    known, label, info = [], "mix", {}
    progress("info", 1, "Reading the video's details...")
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
            progress("info", 3, "Looking for a tracklist in the comments...")
            log("  checking comments...")
            for c in yt_comments(source):
                known = parse_tracklist(c)
                if known:
                    log(f"  found a {len(known)}-track list in a comment")
                    break
    if tracklist_text:
        known = parse_tracklist(tracklist_text) or known
        log(f"  using {len(known)} tracks from the supplied tracklist")

    timed = any(k["time"] is not None for k in known)
    posted = (f"Tracklist found: {len(known)} songs" + (" with timestamps" if timed else " (no timestamps)")
              if known else "No tracklist found - identifying everything from the audio")
    progress("info", 5, posted, title=info.get("title") or label, posted=posted,
             posted_list=[f'{k["artist"]} - {k["title"]}' if k["artist"] else k["title"] for k in known])

    if is_url(source):
        log("Downloading audio...")
        progress("download", 5, "Downloading the mix audio...")
        audio = yt_audio(source, state_dir)
    else:
        audio = source
        label = Path(audio).stem

    duration = audio_duration(audio)
    all_timed = bool(known) and all(k["time"] is not None for k in known)
    if all_timed and not verify:
        # The uploader already gave every start time: no need to listen at all.
        log("Every song has a timestamp - using them (no Shazam scan needed).")
        progress("match", 92, "Every song has a timestamp - using them directly, no listening needed")
        rows = reconcile([], known, duration)
    else:
        log(f"Identifying songs ({clip}s clips, " + (f"one every {step}s" if simple_scan else
            f"quick pass every {2 * step}s, then closer where needed") + ")...")
        heard = asyncio.run(scan(audio, duration, step, clip, concurrency, cache_path=state_dir / "scan_cache.json",
                                 known=known, adaptive=not simple_scan))
        progress("match", 92, "Matching what was heard against the tracklist...")
        songs = merge_hits(heard["hits"], step, heard["asked"], heard["implied"])
        rank = {"low": 0, "medium": 1, "high": 2}
        songs = [s for s in songs if rank[s["confidence"]] >= rank[min_confidence]]
        rows = reconcile(songs, known, duration, posted_order_is_reliable=not unordered)

    cut = {id(r) for _, _, r in split_points(rows, duration, include_estimated=include_estimated)}
    for r in rows:
        r["include"] = id(r) in cut
        r["level"], r["label"] = friendly_confidence(r)

    write_reports(rows, state_dir, label)
    print_table(rows, known)
    state = {
        "source": source, "label": label, "duration": duration, "audio": str(audio), "rows": rows,
        "known_count": len(known), "known_timed": timed,
        "info": {k: info.get(k) for k in ("id", "title", "channel", "uploader", "upload_date", "webpage_url", "thumbnail")},
        "album": clean_mix_title(info.get("title") or label),
        "album_artist": info.get("channel") or info.get("uploader") or "Various DJs",
        "year": (info.get("upload_date") or "")[:4] or None,
    }
    (state_dir / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    heard = sum(1 for r in rows if "shazam" in r["source"])
    summary = (f"Found {len(rows)} songs: {heard} recognised from the audio"
               + (f", {sum(1 for r in rows if r['source'] == 'posted')} placed from the posted tracklist" if known else ""))
    progress("analyzed", 100, summary, summary=summary)
    log(summary)
    return state


def redownload(state_dir):
    """Fetch the audio of an already analysed mix again (it is deleted after saving) so it can be reviewed and
    saved a second time. The tracklist in state.json is kept; nothing is listened to again."""
    state_dir = Path(state_dir)
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    if not Path(state["audio"]).is_file():
        if not is_url(state["source"]):
            sys.exit(f"The audio file is gone and can't be downloaded again: {state['audio']}")
        log("Downloading the audio again...")
        progress("download", 5, "Downloading the mix audio again...")
        state["audio"] = yt_audio(state["source"], state_dir, 5, 98)
        state["duration"] = audio_duration(state["audio"])
        (state_dir / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    summary = "The mix is downloaded again and your last tracklist is filled in"
    progress("analyzed", 100, summary, summary=summary)
    log(summary)
    return state


def split(state_dir, library, rows=None, album=None, album_artist=None, fmt="mp3", keep_full_mix=False,
          delete_audio=True):
    """Cut the analysed mix into tagged songs. `rows` = reviewed list (with include/start); default = as analysed."""
    state_dir = Path(state_dir)
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    rows = rows if rows is not None else state["rows"]
    segments = segments_from_rows(rows, state["duration"])
    if not segments:
        sys.exit("Nothing to split: no songs are ticked.")
    album = album or state["album"]
    album_artist = album_artist or state["album_artist"]
    info = state["info"]
    audio = state["audio"]
    log(f"Writing {len(segments)} tracks to the library...")
    progress("split", 2, "Getting the cover art...")
    cover = fetch_cover(info, state_dir / "cover.jpg") if info.get("thumbnail") else None
    total = len(segments)

    def on_file(n, name):
        progress("split", 5 + 90 * n / total, f"Saving songs to your library: {n} of {total} · {name}")

    folder, files = write_tracks(audio, segments, library, album, album_artist, state.get("year"),
                                 info.get("webpage_url"), cover, fmt, on_file=on_file)
    write_reports(rows, folder, "tracklist")
    (folder / "tracklist_tracklist.csv").replace(folder / "tracklist.csv")
    (folder / "tracklist_chapters.txt").replace(folder / "chapters.txt")
    if keep_full_mix:
        progress("split", 97, "Copying the full mix...")
        shutil.copy(audio, folder / f"00 - {safe_name(album)} (Full Mix){Path(audio).suffix}")
    if delete_audio and Path(audio).parent == state_dir:
        Path(audio).unlink(missing_ok=True)
    progress("done", 100, f"Done - {total} songs saved to {folder}", folder=str(folder))
    log(f"Done: {folder}")
    return folder, files


def process(source, out="output", library=None, include_estimated=True, fmt="mp3", keep_full_mix=False,
            album=None, album_artist=None, **analyze_args):
    """Analyze and (if library is given) split straight away."""
    state_dir = Path(tempfile.mkdtemp(prefix="trackid_job_"))
    try:
        state = analyze(source, state_dir, include_estimated=include_estimated, **analyze_args)
        Path(out).mkdir(parents=True, exist_ok=True)
        for f in state_dir.glob("*_tracklist.csv"):
            shutil.copy(f, Path(out) / f.name)
        for f in state_dir.glob("*_chapters.txt"):
            shutil.copy(f, Path(out) / f.name)
        if library:
            split(state_dir, library, album=album, album_artist=album_artist, fmt=fmt, keep_full_mix=keep_full_mix)
        return state
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


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
    ap.add_argument("source", nargs="?", help="YouTube URL or local audio file")
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
    ap.add_argument("--concurrency", type=int, default=4, help="parallel Shazam requests (default 4)")
    ap.add_argument("--verify", action="store_true",
                    help="scan with Shazam even when every song already has a timestamp")
    ap.add_argument("--simple-scan", action="store_true",
                    help="ask Shazam about every clip on the --step grid and nothing else (the old behaviour), "
                         "instead of a quick pass plus closer listening where songs change")
    ap.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low")
    ap.add_argument("--out", default="output", help="folder for the CSV/chapters reports (default ./output)")
    ap.add_argument("--analyze-only", metavar="JOB_DIR",
                    help="identify songs and save the result + audio in JOB_DIR for review; don't split")
    ap.add_argument("--split-job", metavar="JOB_DIR", help="split a previously analysed JOB_DIR into --library")
    ap.add_argument("--redownload", metavar="JOB_DIR",
                    help="download the audio of an analysed JOB_DIR again (to review and save it a second time)")
    ap.add_argument("--rows", help="with --split-job: JSON file with the reviewed rows (include/start/artist/title)")
    a = ap.parse_args()

    analyze_args = dict(tracklist_text=Path(a.tracklist).read_text(encoding="utf-8") if a.tracklist else None,
                        unordered=a.unordered, comments=a.comments, step=a.step, clip=a.clip,
                        concurrency=a.concurrency, min_confidence=a.min_confidence, verify=a.verify,
                        simple_scan=a.simple_scan)
    if a.split_job:
        if not a.library:
            ap.error("--split-job needs --library (or MUSIC_LIBRARY)")
        rows = json.loads(Path(a.rows).read_text(encoding="utf-8")) if a.rows else None
        split(a.split_job, a.library, rows=rows, album=a.album, album_artist=a.album_artist,
              fmt=a.format, keep_full_mix=a.keep_full_mix)
    elif a.redownload:
        redownload(a.redownload)
    elif not a.source:
        ap.error("give a YouTube URL or audio file")
    elif a.analyze_only:
        analyze(a.source, a.analyze_only, include_estimated=not a.confirmed_only, **analyze_args)
    else:
        process(a.source, out=a.out, library=a.library, include_estimated=not a.confirmed_only, fmt=a.format,
                keep_full_mix=a.keep_full_mix, album=a.album, album_artist=a.album_artist, **analyze_args)


if __name__ == "__main__":
    main()

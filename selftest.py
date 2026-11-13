#!/usr/bin/env python3
"""Checks that everything the splitter needs works on this machine. Run: python selftest.py"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_VIDEO = os.environ.get("SELFTEST_URL", "https://www.youtube.com/watch?v=Ir4-KC0cYOc")
ok = True


def check(name, fn):
    global ok
    try:
        detail = fn()
        print(f"PASS  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    except BaseException as e:  # trackid uses sys.exit() for yt-dlp errors
        ok = False
        print(f"FAIL  {name}: {str(e).strip()[-400:]}", flush=True)


def tool(cmd):
    def f():
        if not shutil.which(cmd[0]):
            raise RuntimeError(f"{cmd[0]} not installed")
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).splitlines()[0][:60]
    return f


def library():
    lib = Path(os.environ.get("MUSIC_LIBRARY", "/music"))
    lib.mkdir(parents=True, exist_ok=True)
    probe = lib / ".mix-splitter-write-test"
    probe.write_text("ok")
    probe.unlink()
    return str(lib)


def youtube():
    sys.path.insert(0, str(Path(__file__).parent))
    import trackid
    info = trackid.yt_info(TEST_VIDEO)
    return f'{info.get("title", "")[:50]} - {len(trackid.parse_tracklist(info.get("description") or ""))} tracks in description'


def youtube_audio():
    sys.path.insert(0, str(Path(__file__).parent))
    import trackid
    d = tempfile.mkdtemp()
    try:
        out = subprocess.run(trackid.ytdlp() + ["-f", "bestaudio", "--download-sections", "*0-20",
                                                 "-o", f"{d}/a.%(ext)s", TEST_VIDEO],
                             capture_output=True, text=True, timeout=180)
        files = list(Path(d).glob("a.*"))
        if not files:
            raise RuntimeError(out.stderr[-400:])
        return f"{files[0].stat().st_size // 1024} KB downloaded"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def shazam():
    from shazamio import Shazam
    d = tempfile.mkdtemp()
    try:
        wav = f"{d}/t.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                        "-ac", "1", "-ar", "16000", wav], check=True)
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        async def once():
            shz = Shazam()
            try:
                return await (shz.recognize(wav, proxy=proxy) if proxy else shz.recognize(wav))
            finally:
                await shz.http_client.close()
        res = asyncio.run(once())
        if "matches" not in res:
            raise RuntimeError(f"unexpected reply: {str(res)[:200]}")
        return "Shazam answered"
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    check("ffmpeg", tool(["ffmpeg", "-version"]))
    check("deno (YouTube JS runtime)", tool(["deno", "--version"]))
    check("yt-dlp", tool(["yt-dlp", "--version"]))
    check("music library writable", library)
    check("YouTube video info", youtube)
    check("YouTube audio download", youtube_audio)
    check("Shazam reachable", shazam)
    print("\nALL GOOD - ready to split mixes" if ok else "\nSome checks failed - see above")
    sys.exit(0 if ok else 1)

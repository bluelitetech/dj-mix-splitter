# Mix Splitter → Navidrome

Paste a YouTube DJ mix link. The tool downloads the audio (yt-dlp) and works out which songs are in it (posted tracklist + Shazam). It then cuts the mix into individual tagged MP3s inside your Navidrome library:

```
/music/DJ Mixes/
  Anthony Midas/                                   <- album artist = the DJ (YouTube channel)
    Throwbacks Only (Vol 2) - Dirty South Hip-Hop Mix/   <- album = cleaned video title
      cover.jpg                                    <- video thumbnail
      01 - Huey - Pop, Lock & Drop It.mp3
      02 - Crime Mob - Knuck If You Buck.mp3
      ...
      tracklist.csv                                <- times, confidence, what Shazam heard
```
Each MP3 is 320 kbps with these tags: title, artist, album, album artist, track #, year, genre "DJ Mix", compilation, embedded cover, and a comment holding the source link and timestamp.

## Option A: web page on your Navidrome server (recommended)
Copy this folder to the server and set the music path in `docker-compose.yml` (it must be the **same host folder Navidrome uses**). Then run:
```bash
docker compose up -d --build
```
Open `http://<server-ip>:8765` (or your Cloudflare URL), paste a link and choose:

- **Find the songs (review before saving)**: recommended. A progress bar shows each step (read video → download →
  listen → review → save), and songs appear in a list as Shazam recognises them. When it finishes you get an
  editable tracklist: fix names or start times, untick samples or intros, add a song that was missed, and press
  ▶ to play the mix from any song's start to check it. Nothing touches your library until you press **Save**.
- **Find + save straight away**: same thing with no review step.

Reviews are kept in `/data`, so they survive a container restart. Use **Run self-test** to check the setup.

## Option B: command line
Requires Python 3.10+, ffmpeg and deno.
```bash
pip install "yt-dlp[default]" shazamio
python trackid.py "https://youtu.be/Ir4-KC0cYOc" --library "/path/to/music/DJ Mixes"
```
Leave off `--library` to get just the tracklist (CSV + YouTube chapter text in `./output`).

| Option | When |
|---|---|
| `--tracklist list.txt` | you have a list from somewhere else (one `Artist - Title` per line) |
| `--unordered` | the posted list isn't in play order |
| `--comments` | the tracklist is in a YouTube comment |
| `--confirmed-only` | only cut where Shazam actually heard a song (no estimated `~` cuts) |
| `--keep-full-mix` | also save the whole mix in the album folder |
| `--step 15` | quick-cut mixes (more samples, slower) |
| `--format m4a` | AAC instead of MP3 |

## How songs are found and cut
1. yt-dlp reads the title, chapters and description. If there's a tracklist, it's used for correct names.
2. A 12-second clip every 30 seconds goes to Shazam. Hits are matched to the posted list, so the timestamps come from the audio.
3. Posted songs Shazam missed are placed by their posted order (estimated `~` cut points).
4. Single stray hits that aren't on the posted list (usually samples) are ignored when cutting.

Cut points are where the next song *starts* in the mix. With blends, each file starts as the new song comes in, so you'll hear a few seconds of the previous song's tail.

## Troubleshooting
- **"Sign in to confirm you're not a bot"**: YouTube does this to servers it distrusts. From a home connection it's rare. If it happens, export your YouTube cookies to `cookies.txt` (see the yt-dlp FAQ), put the file in `./data` and set `YTDLP_COOKIES=/data/cookies.txt`.
- **Wrong or missing songs**: check `tracklist.csv`. Low confidence means Shazam heard the song only once. Re-run with the correct list pasted in.
- Shazam access uses the unofficial `shazamio` library, which is free but can be rate-limited if you run a lot of mixes back to back.

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
  editable tracklist: fix names or start times, untick samples or intros, add as many missed songs as you
  need (**+ Add another song**), and press ▶ to play the downloaded mix in the page from whatever time is
  typed in that row. If a song sits at the wrong time, drag it by its ⠿ handle onto the row with the right
  time: the songs in between shift along and the times stay put. Nothing touches your library until you press **Save**.
- **Find + save straight away**: same thing with no review step.

The form also has the options that used to be command-line only: quick-cut mixes (a clip every 15 s),
only ticking songs Shazam actually heard, and saving as M4A instead of MP3.

Reviews are kept in `/data`, so they survive a container restart. Use **Run self-test** to check the setup.

**If a job fails or the container restarts mid-job**, open it and press **Try again**. The audio that was
already downloaded and the clips Shazam already answered are reused; if only the saving failed you go
straight back to the review.

**Saved something wrong?** Open the saved mix and press **Review again**. The mix is downloaded again
(Shazam isn't asked again) and the review reopens with the tracklist as you last saved it. Pressing Enter
in a text box never saves; only the **Save** button does.

**Saving the same mix again** replaces the album: numbered song files from the earlier save that aren't in
the new tracklist are deleted from that album folder (the full-mix file and everything else is left alone).

### Rescan Navidrome automatically (optional)
Set these three variables and a library scan starts as soon as songs are saved. The user must be a
Navidrome **admin**; the URL is how this container reaches Navidrome (e.g. `http://<nas-ip>:4533`).
```yaml
      - NAVIDROME_URL=http://192.168.1.10:4533
      - NAVIDROME_USER=admin
      - NAVIDROME_PASSWORD=your-password
```
Without them, songs appear after Navidrome's next scheduled scan.

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
| `--step 15` | quick-cut mixes (quick pass every 30 s, gaps every 15 s; slower) |
| `--format m4a` | AAC instead of MP3 |

## How songs are found and cut
1. yt-dlp reads the title, chapters and description. If there's a tracklist, it's used for correct names.
   If every song on it has a timestamp, those are used and nothing below is needed.
2. Shazam is asked about 12-second clips of the mix, in stages:
   - **Quick pass**: a clip every 60 seconds.
   - **Fill the gaps**: the clips in between (every 30 seconds), but only where the two neighbours disagree or
     weren't recognised. Skipped when every song on the posted tracklist has already been heard.
   - **Missing songs**: if posted songs still haven't been heard, extra clips around the unrecognised stretches.
   - **Start times**: for each song, a few more clips between "first heard" and the clip before it, until the
     start is known to within about 8 seconds. A song played from its beginning is placed exactly, using
     Shazam's position-in-song; a song the DJ drops into mid-way is cut a few seconds early rather than late.
3. Hits are matched to the posted list, so the names come from the list and the times from the audio.
4. Posted songs Shazam never heard are placed by their posted order (estimated `~` cut points).
5. Single stray hits that aren't on the posted list (usually samples) are ignored when cutting.

Long songs need fewer Shazam lookups than before; mixes with many short songs need more, because every
song's start is checked. `--simple-scan` on the command line goes back to one clip every `--step` seconds
and nothing else.

Cut points are where the next song *starts* in the mix. With blends, each file starts as the new song comes in, so you'll hear a few seconds of the previous song's tail.

## Troubleshooting
- **"Sign in to confirm you're not a bot"**: YouTube does this to servers it distrusts. From a home connection it's rare. If it happens, export your YouTube cookies to `cookies.txt` (see the yt-dlp FAQ), put the file in `./data` and set `YTDLP_COOKIES=/data/cookies.txt`.
- **Wrong or missing songs**: check `tracklist.csv`. Low confidence means Shazam heard the song only once. Re-run with the correct list pasted in.
- Shazam access uses the unofficial `shazamio` library, which is free but can be rate-limited if you run a lot of mixes back to back.

# KTVibes

**Turn any YouTube song into karaoke at home.** Pick songs on your phone, sing on the TV. KTVibes downloads the song, strips the vocals with AI, finds synced lyrics, and shows them with a KTV-style wipe. It adds pronunciation guides for Chinese, Cantonese, Korean and Japanese.

![Demo: queuing songs from the phone, sending cheers and raising the key while the TV plays, then the up-next card and pinyin lyrics (music video blurred)](docs/demo.gif)

![TV: Joji's Sanctuary, lyrics filling word by word](docs/tv-hero.jpg)

<p align="center">
  <img src="docs/remote-queue.png" width="280" alt="Phone remote: now playing, cheers and the queue">
  &nbsp;
  <img src="docs/remote-controls.png" width="280" alt="Phone remote: settings sheet with guide vocals, music, key and speed">
</p>

## Features

- **Any song on YouTube.** Search from your phone. Results skip reactions, dance practices, mixes and karaoke tracks.
- **AI vocal removal** (Demucs) runs on an NVIDIA GPU or Apple Silicon. A **Vocals** slider keeps as much of the original singer as you want, and a **Music** slider sets the backing track.
- **Synced lyrics** come from [LRCLIB](https://lrclib.net). Each character or word fills as it is sung, timed by forced alignment against the separated vocals.
- **Pronunciation guides** sit above the lyrics. Cycle them with **G** or from the remote.
  - Chinese: pinyin, and Hangul.
  - Cantonese: jyutping. Songs written in Cantonese switch to it automatically.
  - Korean: romanization of the sung pronunciation.
  - Japanese: romaji, or kana (furigana over the kanji).
  - English: Hangul.
- **Key and speed.** Shift the key ±6 semitones without changing tempo, or slow a song down without changing pitch.
- **Classic theme.** A Korean noraebang look, chosen in the remote's Settings or on the TV's Enable sound screen. The TV gets a top strip with the song's number and a room clock, lyrics alternating left and right, a score after each song sung to the end, and a 예약곡 board between songs. The remote becomes a handset: a keypad that reserves songs by number, a 노래책 songbook of everything downloaded, the 예약 list, and 설정.
- **Party-friendly display.** Blur or hide the music video, show lyrics as two karaoke lines (the default) or a scrolling list, or hide them.
- **Phone remote.** Scan the QR code on the TV to join. Everyone shares one queue: reorder, undo removals, and re-add recent songs. The TV shows a popup card for every change.
- **Lyrics picker.** If a song has lyrics in several languages or editions (e.g. a K-pop song's Japanese release), choose which one to show. If none were found, or the wrong song came up, search LRCLIB by hand from the remote.
- **Backstage library.** Import a CSV, approve songs in batches, prepare them before the party, and optionally send vocal separation to another machine on the LAN.

<p align="center">
  <img src="docs/tv-lyrics.jpg" width="49%" alt="TV: Jay Chou with pinyin over each character">
  <img src="docs/tv-korean.jpg" width="49%" alt="TV: Korean lyrics with romanization">
</p>

## Install

You need a computer connected to the TV, and phones on the same Wi-Fi. An NVIDIA GPU (CUDA 12.8 driver) or an Apple Silicon Mac makes song preparation fast. Without one, KTVibes falls back to the CPU, which is slower.

**Linux / macOS**

```bash
curl -LsSf https://raw.githubusercontent.com/andyleenz/KTVibes/main/install.sh | bash
```

The script needs `git`, `ffmpeg` and Node.js 22+, and tells you how to install any that are missing (`brew install git ffmpeg node` on macOS). It installs [uv](https://docs.astral.sh/uv/), clones KTVibes to `~/KTVibes`, and adds a `ktvibes` command.

**Windows** (run in PowerShell; new, reports welcome)

```powershell
irm https://raw.githubusercontent.com/andyleenz/KTVibes/main/install.ps1 | iex
```

This installs Git, FFmpeg, Node.js and uv with `winget` if they are missing. Work-managed PCs may block scripts or `winget`; install on a personal machine.

**Manual**

```bash
git clone https://github.com/andyleenz/KTVibes.git && cd KTVibes
uv sync
uv run ktvibes
```

Plan for about 8 GB of disk: the install itself is 6.5 GB (mostly PyTorch with CUDA), and the first song downloads the separation model (80 MB) and the alignment model (1.2 GB). On a clean Ubuntu 24.04 machine the installer took under 5 minutes.

**Desktop apps**

If you would rather not use a terminal, download the app for your computer from the [latest release](https://github.com/andyleenz/KTVibes/releases/latest). The Windows installer and the Mac app were contributed by Jeff Shih. Releases include the apps from the next one on; until the latest release lists them, use the scripts above.

- **64-bit Windows:** download `KTVibes-Setup-x64.exe` and run it. It installs for your user only, with no admin rights, to `%LOCALAPPDATA%\Programs\KTVibes`. Setup downloads its own Python, Node.js, FFmpeg and the packages, which takes several minutes and about the same disk as the script install. Songs are cached in the `cache` folder inside that directory. Open KTVibes from the Start menu; it starts the server and opens the TV page. The installer is not signed, so Windows SmartScreen may say it protected your PC. Choose **More info**, then **Run anyway**.
- **Apple Silicon Mac (macOS 13+):** download `KTVibes-AppleSilicon.dmg`, open it, and drag KTVibes to Applications. The first launch downloads the runtime and packages and then opens the TV page. The app is not signed or notarized, so macOS blocks it at first. Control-click the app and choose **Open**. On macOS 15 and later, instead open **System Settings > Privacy & Security** and choose **Open Anyway**. Songs and the runtime live in `~/Library/Application Support/KTVibes`. Quit the app to stop the server.
- **Linux (x86_64):** download `KTVibes-x86_64.AppImage`, make it executable with `chmod +x KTVibes-x86_64.AppImage`, and run it. The first start downloads the packages (several GB). A terminal window shows progress; close it or press Ctrl+C to stop KTVibes. It uses the ffmpeg and Node.js 22+ already on your computer, and downloads its own copies of any that are missing. Songs and the app's files live in `~/.local/share/KTVibes`.

Allow incoming connections if your firewall asks, so phones can reach the remote.

## Use

For this Mac-controller plus RTX-renderer deployment, including reusable environment files and restart checks, see [Starting KTVibes and configuring environment variables](docs/startup-and-environment.md).

1. Run `ktvibes`.
2. On the TV computer, open `http://localhost:8765` and click **Enable sound**. Click **Fullscreen** if you like.
3. Scan the QR code with a phone, search for a song, and tap it to queue it.

The first song starts as soon as it is ready. Later songs are prepared while earlier ones play, so queue a few ahead; without a GPU, a song takes a minute or so to prepare. Re-queuing a song is instant because everything is cached in `cache/`.

### Curated library

Open `http://localhost:8765/owner` on the KTVibes machine. To use Backstage from another device, set `KTVIBES_OWNER_TOKEN` before starting KTVibes and enter that token on the page.

The YouTube watchlist discovers candidates from editable search terms and preferred channel URLs. Choose an interval, save the watchlist, or run it immediately. Every run checks the newest results and advances a persistent cursor through older catalog pages, wrapping around after the backlog has been covered. Discovery stores links and metadata only: matches enter **Pending** review and are not downloaded until approved and prepared. Existing review decisions are preserved when a later crawl finds the same video again.

For shared M4A files, put tracks in the configured import folder using `Artist - Title.m4a` names, then choose **Scan shared M4As** in Backstage. On this custom macOS setup, `/Volumes/Karaoke Shared/incoming` is detected automatically when mounted. Files remain on the share; KTVibes verifies and copies each source into its local cache before processing. Local tracks have no music video unless one is added separately.

Every completed song is published to `/Volumes/Karaoke Shared/imported/<recording-id>/`. The package contains the source audio, both generated stems, metadata, timed lyrics and video when available. `manifest.json` records SHA-256 checksums, and a matching manifest is written under `manifests/`. Publication uses a temporary directory and same-share rename, retains the previous package until the external manifest and checksums validate, and only then marks preparation ready. Publication errors are recorded under `failed/`; retrying preparation reuses temporary local work and retries publication. By default, the verified shared package is authoritative and its completed local working copy is removed.

For another mount, set the folder before starting KTVibes:

```bash
export KTVIBES_IMPORT_ROOT='/path/to/shared/incoming'
uv run ktvibes
```

Alternatively, import a UTF-8 CSV. Use `source_id` for YouTube or `filename` for an M4A relative to `KTVIBES_IMPORT_ROOT`:

```csv
artist,title,language,source_id
IU,Good Day,ko,abcdefghijk
Aimer,Ref:rain,ja,bbbbbbbbbbb
```

```csv
artist,title,language,filename
IU,Good Day,ko,IU - Good Day.m4a
Aimer,Ref:rain,ja,Aimer - Ref-rain.m4a
```

`artist` and `title` are required. `language` is optional. Importing the same source again updates its display metadata without losing its review decision. If a shared file changes, scan again; its checksum and cache identity are refreshed so stale stems are not reused.

Approved preparation jobs are persistent. They run only when no live queue item is waiting for preparation. A job already separating cannot be preempted, but the next bulk song waits for the live queue. Finished songs enter the existing songbook and play from the verified shared package.

### Separate GPU machine

Install the same KTVibes checkout and its dependencies on the GPU machine. Set one shared renderer token on both machines, then start the render agent on the GPU machine:

```bash
export KTVIBES_RENDERER_TOKEN='replace-with-a-long-random-value'
uv run ktvibes-renderer
```

Start the main KTVibes server with the same `KTVIBES_RENDERER_TOKEN`. In Backstage, add the GPU machine as `http://<private-ip>:8790`, check its health, and select it. The karaoke machine downloads temporary working media, serves completed packages from the share, and publishes results; Demucs separation, full-audio end detection, vocal-energy analysis, and MMS lyric alignment run on the GPU machine. It does not silently fall back to local models: if the renderer is unavailable or outdated, queued bulk work waits instead of consuming the batch. Already-prepared songs continue to play.

Use this plain-HTTP mode only on a trusted wired LAN or an encrypted private network such as Tailscale/WireGuard. The token and song audio are not protected from network inspection by HTTP. A reverse proxy with HTTPS can be used when the private network is not trusted.

ROCm is reported when the installed PyTorch build exposes `torch.version.hip`, but AMD support is not enabled by the default CUDA dependency lock and has not been validated here. Install a PyTorch/torchaudio build compatible with the exact GPU, OS, driver, and ROCm version, then test a real song before relying on it.

### Classic theme

Switch to Classic in the remote's **Settings** (or on the TV before you enable sound). The remote's tabs are:

- **리모컨** (handset): dial a song's number and press **예약** to reserve it, or **우선예약** to sing it next. Pause, skip, the pronunciation guide, 👏 and the music and vocal levels are on the keys below.
- **노래책** (songbook): every downloaded song with its number, newest (신곡) first, then 최근 (recent) and A–Z, with tabs down the side to jump. Typing filters the book; **Search** asks YouTube.
- **예약** (reserved): the queue as a table, with 우선 (play next), 취소 (cancel) and 재시도 (retry).
- **설정** (settings): mix, key, display, lyrics timing, room time and the theme.

A Classic room starts with 30 minutes; add time from 설정. When a song is sung to the end, the TV shows a score, then the 예약곡 board counts down to the next song. While a song plays, a small QR code in the corner lets more people join.

From the remote's controls panel you can also blur or hide the music video (handy for lyric videos, whose own lyrics clash), switch the lyrics between the classic two-line karaoke layout (the default) and a scrolling list, or turn them off.

## Updating

**From 0.1.0** (no `ktvibes update` yet): stop KTVibes and run the [install command](#install) for your system again. It updates in place and keeps your songs. Then open a new terminal, run `ktvibes`, and click **Enable sound** on the TV.

**From 0.2.0 on:** stop KTVibes (Ctrl+C), then run:

```bash
ktvibes update
```

This pulls the latest version; the next `ktvibes` installs any new dependencies. See [CHANGELOG.md](CHANGELOG.md) for what changed. Manual installs: `git pull`, then `uv run ktvibes`. Your cached songs and queue are kept. Songs cached as FLAC by older versions are converted to the smaller format the next time they are queued.

**Windows installer:** run the newer `KTVibes-Setup-x64.exe` over the existing install. It keeps your songs and updates the packages. `ktvibes update` does not apply to it.

**Apple Silicon app:** download the newer DMG and drag KTVibes to Applications, replacing the old one. The next launch updates the packages and keeps your songs.

**Linux AppImage:** download the newer AppImage and replace the old file. The next start updates the packages and keeps your songs.

## Configuration

| Variable | Default | |
|---|---|---|
| `KTVIBES_PORT` | `8765` | Server port |
| `KTVIBES_REMOTE_URL` | detected | Address in the QR code. Set it if detection picks the wrong network, e.g. with a VPN: `http://<pc-ip>:8765` (the QR code adds `/remote`) |
| `KTVIBES_CACHE` | `./cache` | Temporary preparation work plus persistent queue and library state |
| `KTVIBES_KEEP_LOCAL_CACHE` | `0` | Set to `1` to retain completed local package copies after verified share publication |
| `KTVIBES_COOKIES_FROM_BROWSER` | unset | If songs fail with "Sign in to confirm you're not a bot", set this to a browser you're signed in to YouTube with (`chrome`, `firefox`, `edge`, `safari`…), and KTVibes uses its YouTube cookies |
| `KTVIBES_VIDEO_HEIGHT` | `720` | Highest music video resolution to download, e.g. `1080` (about twice the size) |
| `KTVIBES_NODE` | auto-detected | Optional path to Node.js 22+ for yt-dlp's YouTube player support |
| `KTVIBES_OPEN_BROWSER` | unset | Set to `1` to open the TV page in the default browser when KTVibes starts. The desktop apps do this |
| `KTVIBES_IMPORT_ROOT` | auto-detected on this Mac | Shared folder containing source M4A files; paths in CSV files are relative to this folder |
| `KTVIBES_SHARE_ROOT` | `/Volumes/Karaoke Shared` | Optional assertion of the required shared root; any other path is rejected rather than used as local output |
| `KTVIBES_OWNER_TOKEN` | unset | Token required for Backstage over the network; without it owner APIs accept only local requests |
| `KTVIBES_RENDERER_TOKEN` | unset | Shared token used between the controller and a render agent |
| `KTVIBES_RENDERER_PORT` | `8790` | Port used by `ktvibes-renderer` |
| `KTVIBES_RENDERER_CACHE` | `~/.cache/ktvibes-renderer` | Render-agent staging and recovery directory |

Run a single server process: the queue and the GPU models live in memory.

## Roadmap

- Thai word splitting, for a word-by-word wipe.
- Better kanji readings for Japanese (some single kanji are misread).
- Docker image.

## Notes

KTVibes downloads from YouTube with [yt-dlp](https://github.com/yt-dlp/yt-dlp) for personal, at-home use. Respect the rights of artists and YouTube's terms where you live. Lyrics come from the community-run LRCLIB; coverage varies by song.

## How it works

```
YouTube ──yt-dlp──▶ audio + video ──Demucs──▶ vocals / instrumental
LRCLIB ──────────▶ line-timed lyrics ──MMS aligner──▶ per-character timing
                                     └─▶ pinyin · jyutping · romaji · romanization · 한글
```

| Step | What happens | Time |
|---|---|---|
| Download | Audio and H.264 video (up to 1080p) | network-bound |
| Separate | Demucs `htdemucs` splits vocals from the music, saved as Ogg Opus | 5–35 s |
| Lyrics | LRCLIB lookup in the song's own script; pick another language from the remote | 1–3 s |
| Timing | Forced alignment of each line against the vocals; energy estimate as fallback | 1–5 s |
| Play | The TV plays both tracks in sync with the video; key shift runs in an AudioWorklet | — |

- Everything is cached per song in `cache/<youtube_id>/`, so a re-queued song starts at once.
- Word timing on a reference song: median error 0.045 s.
- Runs on CUDA, Apple Silicon (MPS) or CPU.

Full details, measurements and known quirks: [docs/how-it-works.md](docs/how-it-works.md).

## Development

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
uv run python -m unittest discover -s tests -v
uv run pytest
uv run python -m ktvibes.youtube 'bohemian rhapsody'
uv run python -m ktvibes.lyrics 'Queen' 'Bohemian Rhapsody' 354
uv run python -m ktvibes.youtube '周杰倫 晴天'
uv run python -m ktvibes.youtube '아이유 좋은 날'
```

Standalone download and timed separation:

```bash
uv run python -c "from pathlib import Path; from ktvibes.youtube import download, download_video; p=Path('cache/fJ9rUzIMcZQ'); print(download('fJ9rUzIMcZQ', p)); download_video('fJ9rUzIMcZQ', p)"
uv run python -m ktvibes.separate cache/fJ9rUzIMcZQ/audio.m4a cache/fJ9rUzIMcZQ
```

### Known limitations

- A hidden TV tab does not start the next song, because browsers pause `requestAnimationFrame` in hidden tabs. The song starts as soon as the tab is visible again. Keep the TV page in the foreground.
- If the browser blocks autoplay (`NotAllowedError`), the Enable sound prompt appears again. Other start errors are logged to the console and retried.
- Title parsing is a best guess. Always check the artist and title before adding a song; the original-script artist name or the name LRCLIB uses gives the best lyric matches.


## Layout

`ktvibes/main.py` serves the API, WebSocket, media range requests, QR, and static pages. `queue.py` owns state, and `worker.py` prepares one entry at a time. `youtube.py`, `lyrics.py`, and `separate.py` wrap the external providers. The frontend is plain HTML/CSS/JavaScript with no build step.

The queue, curated library database, preparation jobs and media cache persist across restarts. The database is `cache/library.sqlite3` and is created automatically; existing media caches are not rewritten during migration. Stop KTVibes before copying the cache for backup so SQLite's WAL is fully included. To roll back this feature, stop KTVibes and move `library.sqlite3` aside; the original queue and prepared-song playback remain file-based.

Guest playback controls remain unauthenticated and are intended for your local network. Backstage has token protection for non-local access, and the render agent rejects non-local requests until a renderer token is configured.

## Thanks

[Jeff Shih](https://github.com/JeffShih-wiwynn) built the kana guide, Play next, the two-column remote, the Windows installer and the Mac app in [his fork](https://github.com/JeffShih-wiwynn/KTVibes), along with several lyrics and Windows fixes. Thank you, Jeff.

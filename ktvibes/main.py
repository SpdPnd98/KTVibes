import asyncio
from contextlib import asynccontextmanager, suppress
import io
import json
import os
import re
import secrets
import shutil
from pathlib import Path
import socket
import math
from typing import Literal
from uuid import uuid4
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from .queue import State
from . import curator, lyrics, renderers, songbook, stems, worker, youtube
from .library import Library

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("KTVIBES_CACHE", ROOT / "cache"))
PORT = int(os.environ.get("KTVIBES_PORT", 8765))
state = State()
catalog = None
RECORDING_ID = re.compile(r"(?:[A-Za-z0-9_-]{11}|local_[A-Za-z0-9_-]{16})")

def import_root() -> Path | None:
    configured = os.environ.get("KTVIBES_IMPORT_ROOT")
    if configured:
        return Path(configured).expanduser()
    shared = Path("/Volumes/Karaoke Shared/incoming")
    return shared if shared.is_dir() else None

@asynccontextmanager
async def lifespan(app):
    global catalog
    CACHE.mkdir(parents=True, exist_ok=True)
    catalog = Library(CACHE / "library.sqlite3", import_root())
    state.build = build()
    await asyncio.to_thread(songbook.backfill, CACHE, stems.prepared)  # caches from before the songbook
    prepared = prepared_songs()
    await asyncio.to_thread(catalog.reconcile, set(prepared))
    state.restore(CACHE / "queue.json", seed=lambda: {id: meta["prepared"] for id, meta in prepared.items()})
    tasks = [asyncio.create_task(worker.run(state, CACHE, catalog)),
             asyncio.create_task(curator.run(state, CACHE, catalog))]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

def build() -> str:
    """Fingerprint of the files the pages load. A TV left open across an update reloads itself
    instead of running old code against the new server."""
    import hashlib
    digest = hashlib.sha256()
    for path in sorted((ROOT / "static").rglob("*")):
        if path.is_file():
            digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:12]

app = FastAPI(title="KTVibes", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.middleware("http")
async def revalidate_static(request, call_next):
    # Pages load plain JS/CSS without a build step; revalidate so edits show up on reload.
    # Media too: a song's tracks keep their URL when converted to a new format or prepared again.
    response = await call_next(request)
    if request.url.path.startswith(("/static/", "/media/")) or request.url.path in ("/", "/tv", "/remote"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# The TV is the front page: someone on a laptop can open the remote from it, but not the other way round.
@app.get("/")
@app.get("/tv")
async def tv():
    return FileResponse(ROOT / "static/tv.html")

@app.get("/remote")
async def remote():
    return FileResponse(ROOT / "static/remote.html")

@app.get("/owner")
async def owner():
    return FileResponse(ROOT / "static/owner.html")

@app.get("/api/state")
async def get_state():
    return state.snapshot()

@app.get("/api/current/lyrics")
async def current_lyrics():
    """The playing song's timed lyrics, for a remote (its broadcasts leave them out; only the TV gets them)."""
    song = state.current
    return {"key": song and song["key"], "lyrics_rev": song.get("lyrics_rev", 0) if song else 0,
            "offset": state.offset, "lines": song and song.get("lyrics")}

@app.get("/api/suggest")
async def suggest(q: str = Query(min_length=1, max_length=200)):
    try:
        return await asyncio.to_thread(youtube.suggest, q)
    except Exception:
        return []  # completions are a nicety; never surface their failures

@app.get("/api/search")
async def search(q: str = Query(min_length=1, max_length=200), page: int = Query(0, ge=0, le=9)):
    try:
        results = await asyncio.to_thread(youtube.search, q, page)
    except Exception as exc:
        raise HTTPException(502, f"YouTube search failed: {youtube.explain(exc)}") from exc
    # Prepared songs start right away; the remote marks them.
    cached = prepared_songs()
    return [{**result, "cached": result["id"] in cached} for result in results]

class Song(BaseModel):
    id: str = Field(pattern=r"^(?:[A-Za-z0-9_-]{11}|local_[A-Za-z0-9_-]{16})$")
    artist: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)

    @field_validator("artist", "title")
    @classmethod
    def nonempty(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Artist and title must not be blank")
        return value

def owner_store(request: Request) -> Library:
    """Owner APIs are local-only unless an explicit shared token is configured."""
    expected = os.environ.get("KTVIBES_OWNER_TOKEN")
    supplied = request.headers.get("X-KTVibes-Owner-Token", "")
    if expected:
        if not secrets.compare_digest(expected, supplied):
            raise HTTPException(401, "Owner token required")
    elif not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "Set KTVIBES_OWNER_TOKEN to use owner tools over the network")
    if catalog is None:
        raise HTTPException(503, "Library is starting")
    return catalog

class CSVImport(BaseModel):
    content: str = Field(min_length=1, max_length=10_000_000)

class CandidateAction(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)

class CandidateReview(CandidateAction):
    status: Literal["pending", "approved", "rejected", "deferred"]

class RendererInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=300)

    @field_validator("name", "url")
    @classmethod
    def trim_renderer(cls, value):
        return value.strip()

class RendererChoice(BaseModel):
    id: str = Field(min_length=1, max_length=100)

@app.get("/api/owner/candidates")
async def owner_candidates(request: Request, status: Literal["pending", "approved", "rejected", "deferred"] | None = None):
    library = owner_store(request)
    return await asyncio.to_thread(library.candidates, status)

@app.post("/api/owner/import", status_code=201)
async def owner_import(imported: CSVImport, request: Request):
    library = owner_store(request)
    try:
        return await asyncio.to_thread(library.import_csv, imported.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.get("/api/owner/import-folder")
async def owner_import_folder_status(request: Request):
    library = owner_store(request)
    if library.import_root is None and (detected := import_root()) is not None:
        library.import_root = detected.resolve()
    root = library.import_root
    return {"configured": root is not None, "available": bool(root and root.is_dir()),
            "root": str(root) if root else None}

@app.post("/api/owner/import-folder", status_code=201)
async def owner_scan_import_folder(request: Request):
    library = owner_store(request)
    if library.import_root is None and (detected := import_root()) is not None:
        library.import_root = detected.resolve()
    try:
        return await asyncio.to_thread(library.scan_imports)
    except (OSError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc

@app.post("/api/owner/candidates/review")
async def owner_review(review: CandidateReview, request: Request):
    library = owner_store(request)
    try:
        changed = await asyncio.to_thread(library.review, review.ids, review.status)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"changed": changed, "status": review.status}

@app.post("/api/owner/jobs", status_code=201)
async def owner_prepare(action: CandidateAction, request: Request):
    library = owner_store(request)
    try:
        queued = await asyncio.to_thread(library.enqueue, action.ids)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"queued": queued}

@app.get("/api/owner/jobs")
async def owner_jobs(request: Request):
    library = owner_store(request)
    return await asyncio.to_thread(library.jobs)

@app.get("/api/owner/renderers")
async def owner_renderers(request: Request):
    library = owner_store(request)
    return await asyncio.to_thread(library.renderers)

@app.post("/api/owner/renderers", status_code=201)
async def owner_add_renderer(renderer: RendererInput, request: Request):
    library = owner_store(request)
    try:
        url = renderers.normalize_url(renderer.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await asyncio.to_thread(library.add_renderer, uuid4().hex, renderer.name, url)

@app.post("/api/owner/renderers/select")
async def owner_select_renderer(choice: RendererChoice, request: Request):
    library = owner_store(request)
    try:
        await asyncio.to_thread(library.select_renderer, choice.id)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    return {"selected": choice.id}

@app.get("/api/owner/renderers/{renderer_id}/health")
async def owner_renderer_health(renderer_id: str, request: Request):
    library = owner_store(request)
    renderer = next((item for item in await asyncio.to_thread(library.renderers) if item["id"] == renderer_id), None)
    if not renderer:
        raise HTTPException(404, "Renderer not found")
    try:
        result = await asyncio.to_thread(renderers.health, renderer)
    except Exception as exc:
        raise HTTPException(502, f"Renderer unavailable: {exc}") from exc
    if renderer_id != "local":
        try:
            await asyncio.to_thread(library.renderer_seen, renderer_id, result["machine_id"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return result

@app.delete("/api/owner/renderers/{renderer_id}")
async def owner_delete_renderer(renderer_id: str, request: Request):
    library = owner_store(request)
    try:
        await asyncio.to_thread(library.delete_renderer, renderer_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": renderer_id}

def prepared_songs() -> dict:
    """Cached songs with stems and confirmed details, by video id."""
    songs = {}
    active_local = catalog.active_local_ids() if catalog is not None else None
    for meta_path in CACHE.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            prepared = meta_path.stat().st_mtime
        except (OSError, ValueError):
            continue
        video_id = meta_path.parent.name
        if meta.get("provider") == "local" and active_local is not None and video_id not in active_local:
            continue  # a changed shared file supersedes its old prepared generation
        if RECORDING_ID.fullmatch(video_id) and meta.get("artist") and meta.get("title") and stems.prepared(meta_path.parent):
            thumbnail = "/static/local-track.svg" if meta.get("provider") == "local" else f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
            songs[video_id] = {"id": video_id, "artist": meta["artist"], "title": meta["title"], "duration": meta.get("duration"),
                                "thumbnail": thumbnail, "prepared": prepared, "number": meta.get("number")}
    return songs

@app.get("/api/recent")
async def recent(limit: int = Query(20, ge=1, le=50)):
    """Songs that went on stage, most recent first, so the remote can re-queue them without a search."""
    songs = prepared_songs()
    played = sorted((when, video_id) for video_id, when in state.played.items() if video_id in songs)
    return [{k: v for k, v in songs[video_id].items() if k != "prepared"} for _, video_id in reversed(played[-limit:])]

@app.get("/api/songbook")
async def songbook_list():
    """Prepared songs by songbook number, for the remote's keypad and library (with when each was prepared, for 신곡,
    and last sung, for Recent)."""
    return sorted(({**s, "played": state.played.get(s["id"])} for s in prepared_songs().values() if s.get("number")),
                  key=lambda s: s["number"])

@app.get("/api/ambient")
async def ambient():
    """Cached music videos the TV loops, muted, behind an empty stage: only songs that were prepared,
    so a video left behind by a failed download never shows up between songs."""
    return sorted(path.parent.name for path in CACHE.glob("*/video.mp4") if RECORDING_ID.fullmatch(path.parent.name) and stems.prepared(path.parent))

def song_meta(video_id: str) -> tuple[Path, dict]:
    folder = CACHE / video_id
    if not RECORDING_ID.fullmatch(video_id) or not (folder / "meta.json").is_file():
        raise HTTPException(404, "Song not found")
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("artist") or not meta.get("title") or not meta.get("duration"):
        raise HTTPException(409, "This song isn't fully prepared yet")
    return folder, meta

def lrclib_failed(exc: Exception) -> HTTPException:
    """LRCLIB's rate limit (429) and busy (503) answers pass through with how long to wait, so the remote
    can hold off instead of being refused again; anything else is a plain lookup failure."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status not in (429, 503):
        return HTTPException(502, f"Lyrics lookup failed: {exc}")
    try:
        wait = max(1, min(60, math.ceil(float(response.headers.get("Retry-After")))))
    except (TypeError, ValueError, OverflowError):  # missing, or an HTTP date
        wait = 5 if status == 429 else 1
    reason = "LRCLIB is limiting lyric lookups" if status == 429 else "LRCLIB is busy"
    return HTTPException(status, f"{reason}. Please wait {wait} second{'s' if wait > 1 else ''}, then try again.",
                         headers={"Retry-After": str(wait)})

@app.get("/api/songs/{video_id}/lyrics")
async def lyric_options(video_id: str, q: str = Query("", max_length=200)):
    """Every LRCLIB version that fits the song, best first, so the remote can pick a language or edition.
    With `q`, a free-text search instead, for songs the automatic lookup missed or got wrong."""
    folder, meta = song_meta(video_id)
    current = (folder / "lyrics.lrc").read_text(encoding="utf-8") if (folder / "lyrics.lrc").is_file() else ""
    try:
        found = await (lyrics.search(q.strip(), meta["duration"]) if q.strip() else
                       lyrics.candidates(meta["artist"], meta["title"], meta["duration"], (meta.get("source_title") or "",), exhaustive=True))
    except Exception as exc:
        raise lrclib_failed(exc) from exc
    options = []
    for c in found:
        raw = c.get("syncedLyrics") or ""
        lines = [line["text"] for line in lyrics.parse_lrc(raw) if line.get("text")]
        options.append({"id": c.get("id"), "track": c.get("trackName"), "artist": c.get("artistName"), "album": c.get("albumName"),
                        "language": lyrics.LANGUAGES[lyrics.script(raw)], "lines": len(lines), "preview": lines[:2], "current": raw == current,
                        # seconds longer (+) or shorter than this song: a big gap usually means another version
                        "difference": round(float(c.get("duration") or 0) - meta["duration"])})
    return options

class LyricChoice(BaseModel):
    id: int

@app.post("/api/songs/{video_id}/lyrics")
async def choose_lyrics(video_id: str, choice: LyricChoice):
    """Save the chosen version; the song on stage switches to it at once."""
    folder, meta = song_meta(video_id)
    try:
        chosen = await lyrics.record(choice.id)
    except Exception as exc:
        raise lrclib_failed(exc) from exc
    if not chosen or not lyrics.STAMP.search(chosen.get("syncedLyrics") or ""):
        raise HTTPException(404, "That lyrics version is no longer available")
    if state.current and state.current["id"] == video_id:
        # The song on stage switches right away: time the new lines, then resend lyrics to the TV.
        lines = lyrics.parse_lrc(chosen["syncedLyrics"])
        lines = await asyncio.to_thread(worker.timed_lyrics, folder, chosen["syncedLyrics"], stems.path(folder, "vocals"), lines)
        lyrics.line_starts(lines)
        if state.current and state.current["id"] == video_id:
            state.current.update(lyrics=lines, lyrics_rev=state.current.get("lyrics_rev", 0) + 1)
            state.offset = 0
            state.lyrics_sent.clear()
            await state.broadcast()
    (folder / "lyrics.lrc").write_text(chosen["syncedLyrics"], encoding="utf-8")
    meta["lyrics_identity"] = [meta["artist"], meta["title"]]
    meta.pop("lyric_offset", None)  # a nudge for other lyrics no longer fits
    worker.save_meta(folder, meta)
    return {"saved": choice.id}

@app.delete("/api/songs/{video_id}")
async def delete_song(video_id: str):
    """Forget a song and delete its download; queueing it again downloads it fresh."""
    folder = CACHE / video_id
    if not RECORDING_ID.fullmatch(video_id) or not (folder.is_dir() or video_id in state.played):
        raise HTTPException(404, "Song not found")
    if state.queued(video_id):
        raise HTTPException(409, "Remove it from the queue first")
    state.played.pop(video_id, None)
    if folder.is_symlink():
        folder.unlink()
    elif folder.is_dir():
        await asyncio.to_thread(shutil.rmtree, folder)
    if catalog is not None:
        await asyncio.to_thread(catalog.reconcile, set(prepared_songs()))
    await state.broadcast()
    return {"deleted": video_id}

@app.post("/api/queue", status_code=201)
async def enqueue(song: Song):
    try:
        return await state.add(song.id, song.artist, song.title)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

@app.post("/api/queue/next")
async def enqueue_next(song: Song):
    """Sing a song next, from Search or Recent: queued at the front, or moved there if it is already waiting."""
    try:
        return await state.add_next(song.id, song.artist, song.title)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

@app.get("/media/{video_id}/{filename}")
async def media(video_id: str, filename: str):
    """Stems are requested by name (vocals, no_vocals) and served in whichever format is cached."""
    if not RECORDING_ID.fullmatch(video_id) or filename not in (*stems.NAMES, "video.mp4"):
        raise HTTPException(404)
    folder = CACHE / video_id
    path = folder / filename if filename == "video.mp4" else stems.path(folder, filename)
    if not path or not path.is_file():
        raise HTTPException(404)
    types = {".mp4": "video/mp4", ".opus": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}
    return FileResponse(path, media_type=types[path.suffix])

def remote_url():
    if os.environ.get("KTVIBES_REMOTE_URL"):
        url = os.environ["KTVIBES_REMOTE_URL"].rstrip("/")
        return url if url.endswith("/remote") else url + "/remote"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
    except OSError:
        host = "127.0.0.1"
    return f"http://{host}:{PORT}/remote"

@app.get("/api/config")
async def config():
    return {"remote_url": remote_url()}

@app.get("/api/qr.svg")
async def qr():
    out = io.BytesIO()
    qrcode.make(remote_url(), image_factory=qrcode.image.svg.SvgPathImage).save(out)
    return Response(out.getvalue(), media_type="image/svg+xml")

@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    role = ws.query_params.get("role", "remote")
    if role == "tv" and state.player is not None:
        # Newest TV wins: a reload must not be locked out by its own stale socket.
        previous = state.player
        state.disconnect(previous)
        with suppress(Exception):
            await previous.send_json({"type": "error", "message": "Another device took over playback."})
            await previous.close(code=4001)
    state.clients[ws] = role
    if role == "tv":
        state.player = ws
    await state.broadcast()
    try:
        while True:
            message = await ws.receive_json()
            try:
                if not isinstance(message, dict):
                    raise ValueError("Expected a control object")
                for field in ("value", "delta", "position", "minutes"):
                    # Text values (guide modes) pass through; State.control converts numbers itself.
                    if isinstance(message.get(field), (int, float)) and not math.isfinite(message[field]):
                        raise ValueError("Expected a finite number")
                await state.control(message, ws)
            except (ValueError, TypeError, KeyError) as exc:
                await ws.send_json({"type": "error", "message": str(exc)})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        state.disconnect(ws)
        await state.broadcast()


def update():
    """`ktvibes update`: pull the latest version. The next `ktvibes` (uv run) installs any new dependencies."""
    import subprocess
    import sys
    git = lambda *args: subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if not (ROOT / ".git").exists():
        sys.exit(f"{ROOT} is not a git checkout; download the new version from https://github.com/andyleenz/KTVibes")
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        sys.exit(f"{ROOT} has local changes; commit or stash them, then run ktvibes update again.")
    before = git("rev-parse", "--short", "HEAD").stdout.strip()
    pulled = git("pull", "--ff-only")
    if pulled.returncode:
        sys.exit(pulled.stderr.strip() or "git pull failed")
    after = git("rev-parse", "--short", "HEAD").stdout.strip()
    print("Already up to date." if before == after else f"Updated {before} -> {after}. Stop KTVibes if it is running, then start it again: ktvibes")

def open_browser():
    """KTVIBES_OPEN_BROWSER=1: the desktop apps set it, having no terminal to read the address from.
    Waits until KTVibes itself answers, however long a large song cache takes to load. Another program
    on the port doesn't count: KTVibes then can't listen and exits, and this daemon thread goes with it."""
    import time
    import urllib.request
    import webbrowser
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=1) as response:
                data = json.loads(response.read())
                if isinstance(data, dict) and "upcoming" in data:
                    return webbrowser.open(f"http://127.0.0.1:{PORT}")
        except (OSError, ValueError):
            pass
        time.sleep(0.25)

def run():
    """`uv run ktvibes`: one process only, since queue state and the model live in memory."""
    import sys
    if sys.argv[1:] == ["update"]:
        return update()
    import uvicorn
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=1).close()
    except OSError:
        pass  # nothing there, as expected
    else:
        # Something already answers here (another KTVibes, or an old dev server); uvicorn would only fail later.
        sys.exit(f"Port {PORT} is already in use by another program. Quit it, or start KTVibes with "
                 f"KTVIBES_PORT set to a free port (for example KTVIBES_PORT={PORT + 1}).")
    if os.environ.get("KTVIBES_OPEN_BROWSER") == "1":
        import threading
        threading.Thread(target=open_browser, daemon=True).start()
    if missing := worker.missing_tools():
        # Common right after installing: the terminal predates the install and has a stale PATH.
        print(f"WARNING: {', '.join(missing)} not found on PATH. Songs can't be prepared until it is installed.\n"
              "If you just installed it, open a new terminal and run ktvibes again.", flush=True)
    print(f"Phone remote: {remote_url()}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT)

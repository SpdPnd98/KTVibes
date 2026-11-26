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
import weakref
from typing import Literal
from uuid import uuid4
import qrcode
import qrcode.image.svg
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from .queue import State
from . import curator, discovery, exports, lyrics, renderers, songbook, stems, worker, youtube
from .library import Library

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("KTVIBES_CACHE", ROOT / "cache"))
PORT = int(os.environ.get("KTVIBES_PORT", 8765))
state = State()
catalog = None
publication_tasks = {}
song_edit_locks = weakref.WeakKeyDictionary()
published_song_cache = {}
published_song_versions = {}
RECORDING_ID = re.compile(r"(?:[A-Za-z0-9_-]{11}|local_[A-Za-z0-9_-]{16})")

def share_root() -> Path | None:
    configured = os.environ.get("KTVIBES_SHARE_ROOT")
    if configured and Path(configured).expanduser().resolve() != exports.REQUIRED_ROOT:
        raise RuntimeError(f"KTVIBES_SHARE_ROOT must be {exports.REQUIRED_ROOT}")
    # Keep the expected path even while disconnected so a later retry can use a remounted share.
    return exports.REQUIRED_ROOT

def import_root() -> Path | None:
    configured = os.environ.get("KTVIBES_IMPORT_ROOT")
    if configured:
        return Path(configured).expanduser()
    shared = share_root()
    incoming = shared / "incoming" if shared else None
    return incoming if incoming and incoming.is_dir() else None


def completed_folder(recording_id: str) -> Path | None:
    """Resolve a verified package; local folders are fallback only when no share is configured."""
    shared = catalog and catalog.share_root
    known = exports.known(shared) if shared else None
    if known is not None and recording_id in known:
        return shared / "imported" / recording_id
    if shared:
        return None
    local = CACHE / recording_id
    return local if stems.prepared(local) and (local / "meta.json").is_file() else None


def working_folder(recording_id: str) -> Path:
    """Stage a published package only when an edit needs writable files."""
    local = CACHE / recording_id
    if stems.prepared(local) and (local / "meta.json").is_file():
        return local
    if local.exists():
        raise RuntimeError("Incomplete local working files already exist for this song")
    if catalog is None or not catalog.share_root:
        raise RuntimeError("Windows share is unavailable")
    return exports.stage(catalog.share_root, recording_id, local)


def discard_completed_cache(folder: Path):
    if not worker.keep_local_cache():
        shutil.rmtree(folder, ignore_errors=True)


async def cancellation_safe_thread(function, *args):
    running = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(running)
    except asyncio.CancelledError:
        await running
        raise


async def publish_working_folder(recording_id: str, folder: Path, wait_for_playback: bool = False):
    """Publish a metadata edit without overlapping another share write or replacing an active package."""
    if catalog is None or not catalog.share_root:
        raise RuntimeError("Windows share is unavailable")
    if wait_for_playback:
        while state.current and state.current["id"] == recording_id:
            await asyncio.sleep(1)
    delays = (0, 1, 3, 7)
    try:
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            if wait_for_playback:
                while state.current and state.current["id"] == recording_id:
                    await asyncio.sleep(1)
            try:
                async with worker.publish_lock():
                    await cancellation_safe_thread(exports.publish, folder, catalog.share_root)
                await asyncio.to_thread(catalog.publication_succeeded, recording_id)
                await cancellation_safe_thread(discard_completed_cache, folder)
                return
            except OSError:
                if attempt == len(delays) - 1:
                    raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await asyncio.to_thread(catalog.publication_failed, recording_id, str(exc))
        raise


def song_edit_lock(recording_id: str):
    loop = asyncio.get_running_loop()
    locks = song_edit_locks.setdefault(loop, {})
    return locks.setdefault(recording_id, asyncio.Lock())


def schedule_publication(recording_id: str, lyric_offset: float | None = None, update_offset: bool = True):
    """Debounce metadata-only updates so repeated lyric nudges produce one shared package."""
    if update_offset and lyric_offset is None and state.current and state.current["id"] == recording_id:
        lyric_offset = state.offset
    if previous := publication_tasks.get(recording_id):
        previous.cancel()
    async def publish_later():
        try:
            await asyncio.sleep(1)
            while state.current and state.current["id"] == recording_id:
                await asyncio.sleep(1)
            async with song_edit_lock(recording_id):
                if catalog is None or not catalog.share_root:
                    raise RuntimeError("Windows share is unavailable")
                folder = await asyncio.to_thread(working_folder, recording_id)
                if lyric_offset is not None:
                    meta = songbook.read(folder / "meta.json") or {}
                    meta["lyric_offset"] = lyric_offset
                    await asyncio.to_thread(worker.save_meta, folder, meta)
                await publish_working_folder(recording_id, folder)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if catalog is not None:
                await asyncio.to_thread(catalog.publication_failed, recording_id, str(exc))
        finally:
            if publication_tasks.get(recording_id) is asyncio.current_task():
                publication_tasks.pop(recording_id, None)
    publication_tasks[recording_id] = asyncio.create_task(publish_later())

@asynccontextmanager
async def lifespan(app):
    global catalog
    CACHE.mkdir(parents=True, exist_ok=True)
    catalog = Library(CACHE / "library.sqlite3", import_root(), share_root())
    state.meta_changed = schedule_publication
    state.songbook_roots = [catalog.share_root / "imported"] if catalog.share_root else []
    state.build = build()
    await asyncio.to_thread(songbook.backfill, CACHE, stems.prepared)  # caches from before the songbook
    # Publication already verifies shared checksums. Avoid rereading all media over SMB on every restart.
    published = None
    if exports.available(catalog.share_root):
        try:
            published = await asyncio.to_thread(exports.reconcile, CACHE, catalog.share_root, False)
        except OSError:
            pass  # a transient SMB error must not demote packages already known to be ready
    if published is not None:
        await asyncio.to_thread(catalog.reconcile, published)
    prepared = prepared_songs()
    state.restore(CACHE / "queue.json", seed=lambda: {id: meta["prepared"] for id, meta in prepared.items()})
    tasks = [asyncio.create_task(worker.run(state, CACHE, catalog)),
              asyncio.create_task(curator.run(state, CACHE, catalog)),
              asyncio.create_task(discovery_loop(catalog))]
    try:
        yield
    finally:
        for task in publication_tasks.values():
            task.cancel()
        await asyncio.gather(*publication_tasks.values(), return_exceptions=True)
        publication_tasks.clear()
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
    if request.url.path.startswith(("/static/", "/media/")) or request.url.path in ("/", "/tv", "/remote", "/owner"):
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

class JobControl(BaseModel):
    action: Literal["pause", "resume", "start", "retry"]
    id: int | None = Field(default=None, gt=0)

class RendererInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=300)

    @field_validator("name", "url")
    @classmethod
    def trim_renderer(cls, value):
        return value.strip()

class RendererChoice(BaseModel):
    id: str = Field(min_length=1, max_length=100)

class DiscoverySettings(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(default=24, ge=1, le=168)
    results_per_source: int = Field(default=10, ge=1, le=25)
    search_terms: list[str] = Field(default_factory=list, max_length=20)
    channels: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("search_terms")
    @classmethod
    def clean_search_terms(cls, values):
        cleaned = []
        for value in values:
            term = value.strip()
            if not term or len(term) > 200:
                raise ValueError("Search terms must be 1-200 characters")
            if term.casefold() not in {known.casefold() for known in cleaned}:
                cleaned.append(term)
        return cleaned

    @field_validator("channels")
    @classmethod
    def clean_channels(cls, values):
        return list(dict.fromkeys(youtube.normalize_channel_url(value) for value in values))

async def run_discovery(library: Library) -> dict:
    result = await asyncio.to_thread(discovery.run, library)
    published = await asyncio.to_thread(exports.reconcile, CACHE, library.share_root, False)
    await asyncio.to_thread(library.reconcile, published)
    return result

async def discovery_loop(library: Library):
    while True:
        if await asyncio.to_thread(discovery.due, library):
            try:
                await run_discovery(library)
            except Exception:
                pass  # Status is persisted for Backstage; a later interval retries it.
        await asyncio.sleep(60)

@app.get("/api/owner/discovery")
async def owner_discovery(request: Request):
    library = owner_store(request)
    return {"settings": await asyncio.to_thread(discovery.settings, library),
            "status": await asyncio.to_thread(discovery.status, library)}

@app.put("/api/owner/discovery")
async def owner_save_discovery(value: DiscoverySettings, request: Request):
    library = owner_store(request)
    saved = await asyncio.to_thread(discovery.save_settings, library, value.model_dump())
    return {"settings": saved, "status": await asyncio.to_thread(discovery.status, library)}

@app.post("/api/owner/discovery/run")
async def owner_run_discovery(request: Request):
    library = owner_store(request)
    try:
        result = await run_discovery(library)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"YouTube discovery failed: {youtube.explain(exc)}") from exc
    return {"result": result, "status": await asyncio.to_thread(discovery.status, library)}

@app.get("/api/owner/candidates")
async def owner_candidates(request: Request, status: Literal["pending", "approved", "rejected", "deferred"] | None = None,
                           page: int | None = Query(None, ge=1), page_size: int = Query(20, ge=1, le=100)):
    library = owner_store(request)
    if page is None:
        return await asyncio.to_thread(library.candidates, status)
    total = await asyncio.to_thread(library.candidate_count, status)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    items = await asyncio.to_thread(library.candidates, status, page_size, (page - 1) * page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}

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
    if library.share_root is None and (detected := share_root()) is not None:
        library.share_root = detected.resolve()
    root = library.import_root
    shared = library.share_root
    return {"configured": root is not None, "available": bool(root and root.is_dir()),
            "root": str(root) if root else None, "share_root": str(shared) if shared else None,
            "output": str(shared / "imported") if shared else None,
            "output_available": bool(shared and exports.available(shared))}

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
async def owner_jobs(request: Request, page: int | None = Query(None, ge=1), page_size: int = Query(20, ge=1, le=100)):
    library = owner_store(request)
    if page is None:
        return await asyncio.to_thread(library.jobs)
    total = await asyncio.to_thread(library.job_count)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    items = await asyncio.to_thread(library.jobs, page_size, (page - 1) * page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}

@app.post("/api/owner/jobs/control")
async def owner_control_jobs(control: JobControl, request: Request):
    library = owner_store(request)
    if control.action in ("start", "retry") and control.id is None:
        raise HTTPException(422, "Choose a preparation job")
    try:
        if control.action == "pause":
            changed = await asyncio.to_thread(library.pause_jobs, control.id)
        elif control.action == "resume":
            changed = await asyncio.to_thread(library.resume_jobs, control.id)
        elif control.action == "start":
            changed = await asyncio.to_thread(library.resume_jobs, control.id, True)
        else:
            changed = await asyncio.to_thread(library.retry_job, control.id)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"action": control.action, "changed": changed, "id": control.id}

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
    """Published songs with stems and confirmed details, by recording ID."""
    songs = {}
    active_local = catalog.active_local_ids() if catalog is not None else None
    published_ids = set()
    if catalog is not None and catalog.share_root and exports.available(catalog.share_root):
        root = catalog.share_root
        cached = published_song_cache.setdefault(root, {})
        versions = published_song_versions.setdefault(root, {})
        try:
            known = exports.known(root)
            if known is None:
                exports.published(root, verify_hashes=False)
                known = exports.known(root) or {}
        except OSError:
            known = exports.known(root) or {}
        for recording_id in set(cached) - set(known):
            cached.pop(recording_id, None)
            versions.pop(recording_id, None)
        for recording_id, version in known.items():
            if versions.get(recording_id) == version:
                continue
            cached.pop(recording_id, None)
            folder = root / "imported" / recording_id
            meta_path = folder / "meta.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not (RECORDING_ID.fullmatch(recording_id) and meta.get("artist") and meta.get("title")
                        and stems.prepared(folder)):
                    continue
                thumbnail = "/static/local-track.svg" if meta.get("provider") == "local" else f"https://i.ytimg.com/vi/{recording_id}/mqdefault.jpg"
                cached[recording_id] = {
                    "id": recording_id, "artist": meta["artist"], "title": meta["title"],
                    "duration": meta.get("duration"), "thumbnail": thumbnail,
                    "prepared": meta_path.stat().st_mtime, "number": meta.get("number"),
                    "_provider": meta.get("provider"),
                }
                versions[recording_id] = version
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        songs.update({recording_id: {key: value for key, value in song.items() if key != "_provider"}
                      for recording_id, song in cached.items()
                      if song.get("_provider") != "local" or active_local is None or recording_id in active_local})
        published_ids = set(known)
    folders = [] if catalog is not None and catalog.share_root else [
        path.parent for path in CACHE.glob("*/meta.json")
        if path.parent.name not in published_ids and stems.prepared(path.parent)
    ]
    for folder in folders:
        meta_path = folder / "meta.json"
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
    """Published music videos the TV loops, muted, behind an empty stage: only songs that were prepared,
    so a video left behind by a failed download never shows up between songs."""
    videos = {video_id for video_id in prepared_songs()
              if (folder := completed_folder(video_id)) and (folder / "video.mp4").is_file()}
    if catalog is None or not catalog.share_root:
        videos.update(path.parent.name for path in CACHE.glob("*/video.mp4")
                      if RECORDING_ID.fullmatch(path.parent.name) and stems.prepared(path.parent))
    return sorted(videos)

def song_meta(video_id: str) -> tuple[Path, dict]:
    folder = completed_folder(video_id) if RECORDING_ID.fullmatch(video_id) else None
    if folder is None and RECORDING_ID.fullmatch(video_id) and (CACHE / video_id / "meta.json").is_file():
        folder = CACHE / video_id
    if folder is None or not (folder / "meta.json").is_file():
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
    async with song_edit_lock(video_id):
        # Re-resolve after the network lookup in case another edit published and removed its working folder.
        folder, meta = song_meta(video_id)
        if folder != CACHE / video_id:
            try:
                folder = await asyncio.to_thread(working_folder, video_id)
                meta = songbook.read(folder / "meta.json") or meta
            except Exception as exc:
                raise HTTPException(503, f"Could not stage the published package: {exc}") from exc
        if state.current and state.current["id"] == video_id:
            # The song on stage switches right away: time the new lines, then resend lyrics to the TV.
            lines = lyrics.parse_lrc(chosen["syncedLyrics"])
            lines = await asyncio.to_thread(worker.timed_lyrics, folder, chosen["syncedLyrics"], stems.path(folder, "vocals"), lines,
                                            library=catalog)
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
        publication = None
        if catalog is not None and stems.prepared(folder):
            if not catalog.share_root:
                error = "Windows share is unavailable; the updated package was not published"
                await asyncio.to_thread(catalog.publication_failed, video_id, error)
                raise HTTPException(503, error)
            if state.current and state.current["id"] == video_id:
                schedule_publication(video_id, update_offset=False)
                publication = "scheduled"
            else:
                try:
                    await publish_working_folder(video_id, folder)
                    publication = "published"
                except Exception as exc:
                    raise HTTPException(502, f"Could not publish the updated package: {exc}") from exc
    return {"saved": choice.id, "publication": publication}

@app.delete("/api/songs/{video_id}")
async def delete_song(video_id: str):
    """Forget a song and remove its authoritative package from the shared library."""
    if not RECORDING_ID.fullmatch(video_id):
        raise HTTPException(404, "Song not found")
    if pending := publication_tasks.pop(video_id, None):
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    async with song_edit_lock(video_id):
        folder = CACHE / video_id
        published = bool(catalog and catalog.share_root
                         and exports.validate_shared(catalog.share_root, video_id, verify_hashes=False))
        if not (folder.is_dir() or published or video_id in state.played):
            raise HTTPException(404, "Song not found")
        if state.queued(video_id):
            raise HTTPException(409, "Remove it from the queue first")
        state.played.pop(video_id, None)
        if published:
            try:
                await cancellation_safe_thread(exports.delete, catalog.share_root, video_id)
            except Exception as exc:
                raise HTTPException(502, f"Could not delete the shared package: {exc}") from exc
        if folder.is_symlink():
            folder.unlink()
        elif folder.is_dir():
            await cancellation_safe_thread(shutil.rmtree, folder)
        if catalog is not None:
            published = await asyncio.to_thread(exports.reconcile, CACHE, catalog.share_root, False)
            await asyncio.to_thread(catalog.reconcile, published)
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
    """Stems are requested by name and served from the authoritative completed package."""
    if not RECORDING_ID.fullmatch(video_id) or filename not in (*stems.NAMES, "video.mp4"):
        raise HTTPException(404)
    folder = completed_folder(video_id)
    if folder is None and (catalog is None or not catalog.share_root) and (CACHE / video_id).is_dir():
        folder = CACHE / video_id
    if folder is None:
        raise HTTPException(404)
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

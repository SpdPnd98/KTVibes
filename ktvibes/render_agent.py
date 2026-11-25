"""Authenticated render agent for a GPU machine on the local network."""
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse


ROOT = Path(os.environ.get("KTVIBES_RENDERER_CACHE", Path.home() / ".cache" / "ktvibes-renderer"))
MAX_SOURCE_BYTES = 500 * 1024 * 1024
jobs = {}
keys = {}
tasks = set()
reservations = set()
gpu_lock = threading.Lock()
state_lock = threading.RLock()


def machine_id() -> str:
    path = ROOT / "machine-id"
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        ROOT.mkdir(parents=True, exist_ok=True)
        identity = uuid4().hex
        temporary = path.with_suffix(".tmp")
        temporary.write_text(identity, encoding="ascii")
        temporary.replace(path)
        return identity


def authorize(request: Request):
    expected = os.environ.get("KTVIBES_RENDERER_TOKEN")
    supplied = request.headers.get("X-KTVibes-Renderer-Token", "")
    if expected:
        if not secrets.compare_digest(expected, supplied):
            raise HTTPException(401, "Renderer token required")
    elif not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Set KTVIBES_RENDERER_TOKEN before accepting network jobs")


def save_job(job: dict):
    data = {key: value for key, value in job.items() if key != "folder"}
    temporary = job["folder"] / "status.tmp"
    temporary.write_text(json.dumps(data), encoding="utf-8")
    temporary.replace(job["folder"] / "status.json")


def update_job(job: dict, persist=False, **values):
    with state_lock:
        job.update(values)
        if persist:
            save_job(job)


def status(job: dict) -> dict:
    with state_lock:
        return {key: value for key, value in job.items() if key != "folder"}


def run_job(job_id: str):
    job = jobs[job_id]
    output = job["folder"] / "output"
    try:
        from .separate import separate
        with gpu_lock:
            update_job(job, persist=True, status="running", stage="separating", progress=0.0)
            seconds, gain = separate(job["folder"] / "audio.m4a", output,
                                     lambda value: update_job(job, progress=float(value)))
        artifacts = {}
        for name in ("vocals.opus", "no_vocals.opus"):
            path = output / name
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            artifacts[name] = {"sha256": digest, "bytes": path.stat().st_size}
        update_job(job, persist=True, status="ready", stage=None, progress=1.0, protocol=1, seconds=seconds, gain=gain,
                   artifacts=artifacts, machine_id=machine_id(), finished_at=time.time())
    except Exception as exc:
        update_job(job, persist=True, status="failed", stage=None, error=str(exc), finished_at=time.time())


def restore_jobs():
    jobs.clear()
    keys.clear()
    reservations.clear()
    root = ROOT / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 7 * 86400
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        try:
            data = json.loads((folder / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The process cannot still own an upload after restart; no status means it never became a job.
            shutil.rmtree(folder, ignore_errors=True)
            continue
        if data.get("finished_at", data.get("created_at", 0)) < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            continue
        data["folder"] = folder
        if data.get("status") in ("running", "queued"):
            data.update(status="queued", stage="queued", progress=0.0)
        jobs[data["id"]] = data
        if data.get("key"):
            keys[data["key"]] = data["id"]


def start_job(job_id: str):
    task = asyncio.create_task(asyncio.to_thread(run_job, job_id))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def cleanup_finished(max_age=24 * 60 * 60):
    cutoff = time.time() - max_age
    remove = []
    with state_lock:
        for job_id, job in list(jobs.items()):
            if job.get("status") in ("ready", "failed") and job.get("finished_at", 0) < cutoff:
                jobs.pop(job_id, None)
                if job.get("key") and keys.get(job["key"]) == job_id:
                    keys.pop(job["key"], None)
                remove.append(job["folder"])
    for folder in remove:
        shutil.rmtree(folder, ignore_errors=True)


async def cleanup_loop():
    while True:
        await asyncio.sleep(60 * 60)
        await asyncio.to_thread(cleanup_finished)


@asynccontextmanager
async def lifespan(_app):
    restore_jobs()
    for job_id, job in list(jobs.items()):
        if job["status"] == "queued" and (job["folder"] / "audio.m4a").is_file():
            start_job(job_id)
    cleanup = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup


app = FastAPI(title="KTVibes Renderer", lifespan=lifespan)


@app.get("/v1/health")
async def health(request: Request):
    authorize(request)
    from .separate import backend_info
    return {"protocol": 1, "machine_id": machine_id(), **backend_info(), "busy": gpu_lock.locked(),
            "free_disk": shutil.disk_usage(ROOT).free}


@app.post("/v1/jobs", status_code=202)
async def create_job(request: Request):
    authorize(request)
    key = request.headers.get("X-KTVibes-Job-Key", "")
    with state_lock:
        if key and key in keys:
            existing = jobs.get(keys[key])
            if existing and existing["status"] != "failed":
                return {"id": existing["id"]}
            if keys[key] in reservations:
                raise HTTPException(409, "This source is already uploading", headers={"Retry-After": "1"})
            keys.pop(key, None)
        active = sum(job["status"] in ("queued", "running") for job in jobs.values()) + len(reservations)
        if active >= 20:
            raise HTTPException(429, "Renderer queue is full", headers={"Retry-After": "5"})
        job_id = uuid4().hex
        reservations.add(job_id)
        if key:
            keys[key] = job_id
    folder = ROOT / "jobs" / job_id
    source = folder / "audio.m4a"
    size = 0
    try:
        folder.mkdir(parents=True)
        with source.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_SOURCE_BYTES:
                    raise HTTPException(413, "Audio source is too large")
                output.write(chunk)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        with state_lock:
            reservations.discard(job_id)
            if key and keys.get(key) == job_id:
                keys.pop(key, None)
        raise
    if not size:
        shutil.rmtree(folder, ignore_errors=True)
        with state_lock:
            reservations.discard(job_id)
            if key and keys.get(key) == job_id:
                keys.pop(key, None)
        raise HTTPException(400, "Audio source is empty")
    job = {"id": job_id, "protocol": 1, "status": "queued", "stage": "queued", "progress": 0.0, "key": key or None,
           "created_at": time.time(), "folder": folder, "machine_id": machine_id()}
    with state_lock:
        reservations.discard(job_id)
        jobs[job_id] = job
        if key:
            keys[key] = job_id
        save_job(job)
    start_job(job_id)
    return {"id": job_id}


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    authorize(request)
    if job_id not in jobs:
        raise HTTPException(404, "Render job not found")
    return status(jobs[job_id])


@app.get("/v1/jobs/{job_id}/artifacts/{name}")
async def get_artifact(job_id: str, name: str, request: Request):
    authorize(request)
    job = jobs.get(job_id)
    if not job or job["status"] != "ready" or name not in ("vocals.opus", "no_vocals.opus"):
        raise HTTPException(404, "Render artifact not found")
    return FileResponse(job["folder"] / "output" / name, media_type="audio/ogg")


@app.delete("/v1/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    authorize(request)
    with state_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Render job not found")
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "Render job is still running")
        jobs.pop(job_id, None)
        if job.get("key") and keys.get(job["key"]) == job_id:
            keys.pop(job["key"], None)
    shutil.rmtree(job["folder"], ignore_errors=True)
    return {"deleted": job_id}


def run():
    import uvicorn
    port = int(os.environ.get("KTVIBES_RENDERER_PORT", 8790))
    if not os.environ.get("KTVIBES_RENDERER_TOKEN"):
        print("Renderer token is unset; only requests from this machine will be accepted.", flush=True)
    print(f"KTVibes renderer: http://0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)

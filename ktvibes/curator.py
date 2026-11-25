"""Low-priority persistent preparation jobs for the curated library."""
import asyncio
import logging
from contextlib import suppress
from .queue import State
from . import worker


ACTIVE = {"queued", "downloading", "separating", "syncing"}
log = logging.getLogger(__name__)


class BatchState:
    """Preparation progress belongs in SQLite, not the live room WebSocket state."""
    async def broadcast(self):
        pass


async def run(state, cache, library):
    while True:
        # Never start another bulk song while the performance queue needs preparation.
        if any(item.get("status") in ACTIVE for item in state.upcoming):
            await asyncio.sleep(1)
            continue
        try:
            job = await asyncio.to_thread(library.claim_job)
        except Exception:
            log.exception("Could not claim a curated preparation job")
            await asyncio.sleep(1)
            continue
        if not job:
            await asyncio.sleep(1)
            continue
        preparation = None
        try:
            item = State.entry(job["external_id"], job["artist"], job["title"])
            item["provider"] = job["provider"]
            if job["provider"] == "local":
                item["source_path"] = job["source_path"]
                item["source_hash"] = job["source_hash"]
                await asyncio.to_thread(library.resolve_source, job["source_path"])
                item["source_root"] = str(library.import_root)
            preparation = asyncio.create_task(worker.prepare(BatchState(), item, cache, library=library, promote=False,
                                                             renderer_id=job["renderer_id"]))
            while not preparation.done():
                try:
                    await asyncio.to_thread(library.job_progress, job["id"], item.get("step") or item.get("status"), item.get("progress"))
                except Exception:
                    log.exception("Could not save progress for curated job %s", job["id"])
                await asyncio.sleep(0.5)
            await preparation
            ready = item.get("status") == "ready"
            await asyncio.to_thread(library.finish_job, job["id"], ready, item.get("error"))
        except asyncio.CancelledError:
            if preparation:
                preparation.cancel()
                with suppress(asyncio.CancelledError):
                    await preparation
            raise
        except Exception as exc:
            if preparation and not preparation.done():
                preparation.cancel()
                with suppress(asyncio.CancelledError):
                    await preparation
            try:
                await asyncio.to_thread(library.finish_job, job["id"], False, str(exc))
            except Exception:
                log.exception("Could not save failure for curated job %s", job["id"])

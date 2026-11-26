"""Periodic YouTube candidate discovery for Backstage review."""
import threading
import time

from . import youtube


SETTINGS_KEY = "discovery_settings"
STATUS_KEY = "discovery_status"
CURSORS_KEY = "discovery_cursors"
DEFAULT_SETTINGS = {"enabled": False, "interval_hours": 24, "results_per_source": 10,
                    "search_terms": [], "channels": []}
SEARCH_BACKLOG_PAGES = 4
CHANNEL_BACKLOG_PAGES = 20
run_lock = threading.Lock()


def settings(library) -> dict:
    saved = library.json_setting(SETTINGS_KEY, {})
    return {**DEFAULT_SETTINGS, **saved} if isinstance(saved, dict) else dict(DEFAULT_SETTINGS)


def save_settings(library, value: dict) -> dict:
    saved = {**DEFAULT_SETTINGS, **value}
    library.save_json_setting(SETTINGS_KEY, saved)
    return saved


def status(library) -> dict:
    saved = library.json_setting(STATUS_KEY, {})
    return saved if isinstance(saved, dict) else {}


def due(library, now=None) -> bool:
    config = settings(library)
    if not config["enabled"] or not (config["search_terms"] or config["channels"]):
        return False
    previous = status(library)
    finished = float(previous.get("finished_at") or 0)
    return (now or time.time()) - finished >= config["interval_hours"] * 3600


def _candidate(result: dict, fallback_artist: str) -> dict:
    parsed = result.get("parsed") or {}
    artist = str(parsed.get("artist") or result.get("channel") or fallback_artist).strip()
    title = str(parsed.get("title") or result.get("title") or "").strip()
    return {"id": result.get("id"), "artist": artist, "title": title}


def _cursor(cursors: dict, key: str, maximum: int) -> int:
    try:
        page = int(cursors.get(key, 1))
    except (TypeError, ValueError):
        page = 1
    return page if 1 <= page <= maximum else 1


def _advance(cursors: dict, key: str, page: int, maximum: int, results: list):
    cursors[key] = 1 if not results or page >= maximum else page + 1


def run(library) -> dict:
    if not run_lock.acquire(blocking=False):
        raise ValueError("YouTube discovery is already running")
    started = time.time()
    library.save_json_setting(STATUS_KEY, {"state": "running", "started_at": started})
    try:
        config = settings(library)
        songs = []
        errors = []
        cursors = library.json_setting(CURSORS_KEY, {})
        if not isinstance(cursors, dict):
            cursors = {}
        for term in config["search_terms"]:
            key = "search:" + term.casefold()
            page = _cursor(cursors, key, SEARCH_BACKLOG_PAGES)
            try:
                newest = youtube.search(term, page=0, size=config["results_per_source"])
            except Exception as exc:
                errors.append(f"{term}: {youtube.explain(exc)}")
                continue
            songs.extend(_candidate(result, term) for result in newest)
            try:
                backlog = youtube.search(term, page=page, size=config["results_per_source"])
                songs.extend(_candidate(result, term) for result in backlog)
                _advance(cursors, key, page, SEARCH_BACKLOG_PAGES, backlog)
            except Exception as exc:
                errors.append(f"{term} backlog page {page + 1}: {youtube.explain(exc)}")
        for channel_url in config["channels"]:
            key = "channel:" + channel_url
            page = _cursor(cursors, key, CHANNEL_BACKLOG_PAGES)
            try:
                newest = youtube.channel_videos(channel_url, page=0, size=config["results_per_source"])
            except Exception as exc:
                errors.append(f"{channel_url}: {youtube.explain(exc)}")
                continue
            songs.extend(_candidate(result, result.get("channel") or "YouTube") for result in newest)
            try:
                backlog = youtube.channel_videos(channel_url, page=page, size=config["results_per_source"])
                songs.extend(_candidate(result, result.get("channel") or "YouTube") for result in backlog)
                _advance(cursors, key, page, CHANNEL_BACKLOG_PAGES, backlog)
            except Exception as exc:
                errors.append(f"{channel_url} backlog page {page + 1}: {youtube.explain(exc)}")
        unique = {song["id"]: song for song in songs if song.get("id")}
        library.save_json_setting(CURSORS_KEY, cursors)
        imported = library.import_discoveries(list(unique.values()))
        result = {**imported, "found": len(unique), "sources": len(config["search_terms"]) + len(config["channels"]),
                  "errors": errors}
        library.save_json_setting(STATUS_KEY, {"state": "idle", "started_at": started,
                                               "finished_at": time.time(), "result": result})
        return result
    except Exception as exc:
        library.save_json_setting(STATUS_KEY, {"state": "error", "started_at": started,
                                               "finished_at": time.time(), "error": str(exc)})
        raise
    finally:
        run_lock.release()

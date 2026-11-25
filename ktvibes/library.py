"""Persistent curation, preparation jobs, and renderer selection."""
import csv
import base64
import hashlib
import io
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path


REVIEW_STATUSES = ("pending", "approved", "rejected", "deferred")


@contextmanager
def open_beneath(root: Path, relative: str):
    """Open a regular file without following a symlink outside the configured import root."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError("Shared-media filenames must be relative to the import folder")
    if os.name != "nt" and os.open in os.supports_dir_fd:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(root, directory_flags)
        try:
            for part in parts[:-1]:
                child = os.open(part, directory_flags, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError(f"Shared-media source is not a file: {relative}")
                yield source
        except OSError as exc:
            raise ValueError(f"Shared-media file is unavailable: {relative}") from exc
        finally:
            os.close(directory)
        return
    # Windows fallback: reject every currently visible reparse/symlink component before opening.
    source = root.joinpath(*parts)
    current = root
    try:
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Shared-media symlinks are not allowed: {relative}")
        resolved = source.resolve(strict=True)
        resolved.relative_to(root)
        with resolved.open("rb") as opened:
            yield opened
    except OSError as exc:
        raise ValueError(f"Shared-media file is unavailable: {relative}") from exc


class Library:
    def __init__(self, path: Path, import_root: Path | None = None):
        self.path = path
        self.import_root = import_root.resolve() if import_root else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _migrate(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY,
                    import_key TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL DEFAULT 'youtube',
                    external_id TEXT,
                    source_path TEXT,
                    source_hash TEXT,
                    artist TEXT NOT NULL,
                    title TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    processing_status TEXT NOT NULL DEFAULT 'not_started',
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preparation_jobs (
                    id INTEGER PRIMARY KEY,
                    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'queued',
                    stage TEXT,
                    progress REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    renderer_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created ON preparation_jobs(status, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_candidate
                    ON preparation_jobs(candidate_id) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS renderers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    machine_id TEXT,
                    last_seen REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(candidates)")}
            for name in ("source_path", "source_hash"):
                if name not in columns:
                    db.execute(f"ALTER TABLE candidates ADD COLUMN {name} TEXT")
            # A process killed during preparation leaves a retryable job, never a false success.
            now = time.time()
            db.execute("UPDATE preparation_jobs SET status='queued', stage=NULL, progress=NULL, updated_at=? WHERE status='running'", (now,))
            db.execute("UPDATE candidates SET processing_status='queued', updated_at=? WHERE processing_status='running'", (now,))

    @staticmethod
    def _candidate(row):
        return dict(row) if row else None

    @staticmethod
    def _local_id(relative: str, digest: str) -> str:
        identity = hashlib.sha256(f"{relative.casefold()}\0{digest}".encode()).digest()
        return "local_" + base64.urlsafe_b64encode(identity).decode("ascii").rstrip("=")[:16]

    def local_source(self, value: str) -> tuple[str, str, str]:
        if self.import_root is None:
            raise ValueError("Set KTVIBES_IMPORT_ROOT before importing shared media")
        relative = Path(value.strip())
        if not value.strip() or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Shared-media filenames must be relative to the import folder")
        normalized = relative.as_posix()
        if relative.suffix.lower() != ".m4a":
            raise ValueError(f"Only M4A files are supported: {value}")
        digest = hashlib.sha256()
        with open_beneath(self.import_root, normalized) as audio:
            for chunk in iter(lambda: audio.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = digest.hexdigest()
        return normalized, checksum, self._local_id(normalized, checksum)

    def resolve_source(self, relative: str) -> Path:
        if self.import_root is None:
            raise ValueError("Shared-media import folder is not configured")
        with open_beneath(self.import_root, relative):
            pass
        return self.import_root / relative

    def _save_candidate(self, db, provider: str, external_id: str | None, artist: str, title: str,
                        language: str, now: float, source_path: str | None = None,
                        source_hash: str | None = None) -> bool:
        identity = f"local:{source_path.casefold()}" if provider == "local" else (
            f"youtube:{external_id}" if external_id else f"metadata:{artist.casefold()}\0{title.casefold()}\0{language.casefold()}")
        key = hashlib.sha256(identity.encode()).hexdigest()
        known = db.execute("SELECT id, source_hash FROM candidates WHERE import_key=?", (key,)).fetchone()
        if known and source_hash and known["source_hash"] != source_hash and db.execute(
                "SELECT 1 FROM preparation_jobs WHERE candidate_id=? AND status IN ('queued','running')", (known["id"],)).fetchone():
            raise ValueError(f"Wait for active preparation before replacing {artist} - {title}")
        db.execute("""
            INSERT INTO candidates(import_key, provider, external_id, source_path, source_hash, artist, title, language, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(import_key) DO UPDATE SET
                provider=excluded.provider, external_id=excluded.external_id, source_path=excluded.source_path,
                source_hash=excluded.source_hash, artist=excluded.artist, title=excluded.title,
                language=excluded.language, updated_at=excluded.updated_at,
                processing_status=CASE WHEN candidates.source_hash IS NOT excluded.source_hash THEN 'not_started' ELSE candidates.processing_status END,
                error=CASE WHEN candidates.source_hash IS NOT excluded.source_hash THEN NULL ELSE candidates.error END
        """, (key, provider, external_id, source_path, source_hash, artist, title, language, now, now))
        return known is None

    def import_csv(self, text: str) -> dict:
        try:
            reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
            headers = {str(name).strip().lower(): name for name in (reader.fieldnames or [])}
            rows = list(reader)
        except csv.Error as exc:
            raise ValueError(f"Invalid CSV: {exc}") from exc
        if "artist" not in headers or "title" not in headers:
            raise ValueError("CSV needs artist and title columns")
        source_name = next((headers[name] for name in ("source_id", "external_id", "youtube_id", "id") if name in headers), None)
        filename_name = next((headers[name] for name in ("filename", "file", "path") if name in headers), None)
        language_name = headers.get("language")
        imported = existing = skipped = 0
        records = []
        now = time.time()
        for number, row in enumerate(rows, 2):
            artist = str(row.get(headers["artist"]) or "").strip()
            title = str(row.get(headers["title"]) or "").strip()
            language = str(row.get(language_name) or "").strip() if language_name else ""
            external_id = str(row.get(source_name) or "").strip() if source_name else ""
            filename = str(row.get(filename_name) or "").strip() if filename_name else ""
            if not artist and not title:
                skipped += 1
                continue
            if not artist or not title:
                raise ValueError(f"CSV row {number} needs both artist and title")
            if len(artist) > 200 or len(title) > 300 or len(language) > 50 or len(external_id) > 200 or len(filename) > 500:
                raise ValueError(f"CSV row {number} has a value that is too long")
            if filename:
                source_path, source_hash, local_id = self.local_source(filename)
                records.append(("local", local_id, artist, title, language, source_path, source_hash))
            else:
                records.append(("youtube", external_id or None, artist, title, language, None, None))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for provider, external_id, artist, title, language, source_path, source_hash in records:
                added = self._save_candidate(db, provider, external_id, artist, title, language, now,
                                             source_path, source_hash)
                if added:
                    imported += 1
                else:
                    existing += 1
        return {"imported": imported, "existing": existing, "skipped": skipped}

    def scan_imports(self) -> dict:
        if self.import_root is None:
            raise ValueError("Set KTVIBES_IMPORT_ROOT before scanning shared media")
        if not self.import_root.is_dir():
            raise ValueError(f"Shared-media folder is unavailable: {self.import_root}")
        imported = existing = skipped = 0
        invalid = []
        records = []
        now = time.time()
        sources = (path for path in self.import_root.rglob("*") if path.is_file() and path.suffix.lower() == ".m4a")
        for source in sorted(sources):
            if any(part.startswith(".") for part in source.relative_to(self.import_root).parts):
                continue
            artist, separator, title = source.stem.partition(" - ")
            if not separator or not artist.strip() or not title.strip():
                skipped += 1
                if len(invalid) < 20:
                    invalid.append(source.name)
                continue
            relative, checksum, local_id = self.local_source(source.relative_to(self.import_root).as_posix())
            records.append((local_id, artist.strip(), title.strip(), relative, checksum))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for local_id, artist, title, relative, checksum in records:
                if self._save_candidate(db, "local", local_id, artist, title, "", now, relative, checksum):
                    imported += 1
                else:
                    existing += 1
        return {"imported": imported, "existing": existing, "skipped": skipped, "invalid": invalid,
                "root": str(self.import_root)}

    def candidates(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in REVIEW_STATUSES:
            raise ValueError("Unknown review status")
        query = "SELECT * FROM candidates"
        params = ()
        if status:
            query += " WHERE review_status=?"
            params = (status,)
        query += " ORDER BY updated_at DESC, id DESC"
        with self._connect() as db:
            return [dict(row) for row in db.execute(query, params)]

    def active_local_ids(self) -> set[str]:
        with self._connect() as db:
            return {row[0] for row in db.execute(
                "SELECT external_id FROM candidates WHERE provider='local' AND external_id IS NOT NULL")}

    def candidate(self, candidate_id: int) -> dict | None:
        with self._connect() as db:
            return self._candidate(db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone())

    def review(self, ids: list[int], status: str) -> int:
        if status not in REVIEW_STATUSES:
            raise ValueError("Unknown review status")
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise ValueError("Choose at least one song")
        placeholders = ",".join("?" for _ in unique)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute(f"SELECT COUNT(*) FROM candidates WHERE id IN ({placeholders})", unique).fetchone()[0]
            if found != len(unique):
                raise KeyError("One or more songs were not found")
            if status != "approved" and db.execute(
                    f"SELECT 1 FROM preparation_jobs WHERE candidate_id IN ({placeholders}) AND status='running'", unique).fetchone():
                raise ValueError("Wait for running preparation to finish before changing its review status")
            db.execute(f"UPDATE candidates SET review_status=?, updated_at=? WHERE id IN ({placeholders})", (status, time.time(), *unique))
            if status != "approved":
                db.execute(f"UPDATE preparation_jobs SET status='cancelled', stage=NULL, progress=NULL, updated_at=? "
                           f"WHERE candidate_id IN ({placeholders}) AND status='queued'", (time.time(), *unique))
                db.execute(f"UPDATE candidates SET processing_status='cancelled', updated_at=? "
                           f"WHERE id IN ({placeholders}) AND processing_status='queued'", (time.time(), *unique))
        return found

    def enqueue(self, ids: list[int]) -> int:
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise ValueError("Choose at least one song")
        placeholders = ",".join("?" for _ in unique)
        with self._connect() as db:
            candidates = [dict(row) for row in db.execute(f"SELECT * FROM candidates WHERE id IN ({placeholders})", unique)]
        if len(candidates) != len(unique):
            raise KeyError("One or more songs were not found")
        # SMB reads happen before the short write transaction below.
        for candidate in candidates:
            if candidate["review_status"] != "approved":
                raise ValueError(f"{candidate['artist']} - {candidate['title']} is not approved")
            external_id = candidate["external_id"] or ""
            if candidate["provider"] == "local":
                _, current_hash, current_id = self.local_source(candidate["source_path"] or "")
                if current_hash != candidate["source_hash"] or current_id != external_id:
                    raise ValueError(f"{candidate['artist']} - {candidate['title']} changed; scan the shared folder again")
            elif len(external_id) != 11 or not all(c.isalnum() or c in "_-" for c in external_id):
                raise ValueError(f"{candidate['artist']} - {candidate['title']} needs a valid YouTube source ID")
        now = time.time()
        queued = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for candidate_id in unique:
                candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
                if not candidate:
                    raise KeyError("One or more songs were not found")
                if candidate["review_status"] != "approved":
                    raise ValueError(f"{candidate['artist']} - {candidate['title']} is not approved")
                active = db.execute("SELECT id FROM preparation_jobs WHERE candidate_id=? AND status IN ('queued','running')", (candidate_id,)).fetchone()
                if active or candidate["processing_status"] == "ready":
                    continue
                try:
                    db.execute("INSERT INTO preparation_jobs(candidate_id, status, created_at, updated_at) VALUES (?, 'queued', ?, ?)",
                               (candidate_id, now, now))
                except sqlite3.IntegrityError:
                    continue
                db.execute("UPDATE candidates SET processing_status='queued', error=NULL, updated_at=? WHERE id=?", (now, candidate_id))
                queued += 1
        return queued

    def reconcile(self, prepared_ids: set[str]):
        """Match durable processing state to the canonical media cache after startup."""
        now = time.time()
        with self._connect() as db:
            rows = db.execute("SELECT id, external_id, processing_status FROM candidates").fetchall()
            for row in rows:
                ready = row["external_id"] in prepared_ids
                if ready:
                    if row["processing_status"] != "ready":
                        db.execute("UPDATE candidates SET processing_status='ready', error=NULL, updated_at=? WHERE id=?", (now, row["id"]))
                    db.execute("UPDATE preparation_jobs SET status='ready', stage=NULL, progress=NULL, error=NULL, updated_at=? "
                               "WHERE candidate_id=? AND status='queued'", (now, row["id"]))
                elif not ready and row["processing_status"] == "ready":
                    db.execute("UPDATE candidates SET processing_status='not_started', updated_at=? WHERE id=?", (now, row["id"]))

    def jobs(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT j.*, c.artist, c.title, c.external_id, c.provider, c.source_path, c.source_hash FROM preparation_jobs j
                JOIN candidates c ON c.id=j.candidate_id ORDER BY j.created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def claim_job(self) -> dict | None:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT j.*, c.artist, c.title, c.external_id, c.provider, c.source_path, c.source_hash FROM preparation_jobs j
                JOIN candidates c ON c.id=j.candidate_id
                WHERE j.status='queued' AND c.review_status='approved' AND c.processing_status!='ready'
                ORDER BY j.created_at, j.id LIMIT 1
            """).fetchone()
            if not row:
                return None
            # A job recovered after controller restart reconnects to the renderer it originally claimed.
            renderer_id = row["renderer_id"] or self.selected_renderer_id(db)
            db.execute("UPDATE preparation_jobs SET status='running', stage='starting', attempts=attempts+1, renderer_id=?, updated_at=? WHERE id=?",
                       (renderer_id, now, row["id"]))
            db.execute("UPDATE candidates SET processing_status='running', error=NULL, updated_at=? WHERE id=?", (now, row["candidate_id"]))
            claimed = dict(row)
            claimed["renderer_id"] = renderer_id
            return claimed

    def job_progress(self, job_id: int, stage: str | None, progress: float | None):
        with self._connect() as db:
            db.execute("UPDATE preparation_jobs SET stage=?, progress=?, updated_at=? WHERE id=? AND status='running'",
                       (stage, progress, time.time(), job_id))

    def finish_job(self, job_id: int, ready: bool, error: str | None = None):
        now = time.time()
        status = "ready" if ready else "failed"
        with self._connect() as db:
            row = db.execute("SELECT candidate_id FROM preparation_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return
            db.execute("UPDATE preparation_jobs SET status=?, stage=NULL, progress=NULL, error=?, updated_at=? WHERE id=?",
                       (status, error, now, job_id))
            db.execute("UPDATE candidates SET processing_status=?, error=?, updated_at=? WHERE id=?",
                       (status, error, now, row["candidate_id"]))

    @staticmethod
    def selected_renderer_id(db) -> str:
        row = db.execute("SELECT value FROM settings WHERE key='selected_renderer'").fetchone()
        return row[0] if row else "local"

    def selected_renderer(self) -> dict:
        with self._connect() as db:
            selected = self.selected_renderer_id(db)
            if selected == "local":
                return {"id": "local", "name": "Local machine", "url": None, "machine_id": "local"}
            row = db.execute("SELECT * FROM renderers WHERE id=?", (selected,)).fetchone()
            if row:
                return dict(row)
            db.execute("INSERT INTO settings(key, value) VALUES ('selected_renderer','local') ON CONFLICT(key) DO UPDATE SET value='local'")
            return {"id": "local", "name": "Local machine", "url": None, "machine_id": "local"}

    def renderer(self, renderer_id: str) -> dict:
        if renderer_id == "local":
            return {"id": "local", "name": "Local machine", "url": None, "machine_id": "local"}
        with self._connect() as db:
            row = db.execute("SELECT * FROM renderers WHERE id=?", (renderer_id,)).fetchone()
        if not row:
            raise KeyError("Renderer not found")
        return dict(row)

    def renderers(self) -> list[dict]:
        with self._connect() as db:
            selected = self.selected_renderer_id(db)
            rows = [dict(row) for row in db.execute("SELECT * FROM renderers ORDER BY name COLLATE NOCASE")]
        local = {"id": "local", "name": "Local machine", "url": None, "machine_id": "local", "selected": selected == "local"}
        return [local, *({**row, "selected": row["id"] == selected} for row in rows)]

    def add_renderer(self, renderer_id: str, name: str, url: str) -> dict:
        now = time.time()
        with self._connect() as db:
            db.execute("""
                INSERT INTO renderers(id, name, url, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at
            """, (renderer_id, name, url, now, now))
            row = db.execute("SELECT * FROM renderers WHERE url=?", (url,)).fetchone()
        return dict(row)

    def select_renderer(self, renderer_id: str):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if renderer_id != "local" and not db.execute("SELECT 1 FROM renderers WHERE id=?", (renderer_id,)).fetchone():
                raise KeyError("Renderer not found")
            db.execute("INSERT INTO settings(key, value) VALUES ('selected_renderer', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (renderer_id,))

    def renderer_seen(self, renderer_id: str, machine_id: str):
        with self._connect() as db:
            known = db.execute("SELECT machine_id FROM renderers WHERE id=?", (renderer_id,)).fetchone()
            if not known:
                raise KeyError("Renderer not found")
            if known["machine_id"] and known["machine_id"] != machine_id:
                raise ValueError("Renderer identity changed at this IP; remove and pair the machine again")
            db.execute("UPDATE renderers SET machine_id=?, last_seen=?, updated_at=? WHERE id=?",
                       (machine_id, time.time(), time.time(), renderer_id))

    def delete_renderer(self, renderer_id: str):
        if renderer_id == "local":
            raise ValueError("The local renderer cannot be deleted")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM preparation_jobs WHERE renderer_id=? AND status IN ('queued','running')", (renderer_id,)).fetchone():
                raise ValueError("Wait for this renderer's claimed job to finish")
            if self.selected_renderer_id(db) == renderer_id:
                db.execute("INSERT INTO settings(key, value) VALUES ('selected_renderer','local') ON CONFLICT(key) DO UPDATE SET value='local'")
            if not db.execute("DELETE FROM renderers WHERE id=?", (renderer_id,)).rowcount:
                raise KeyError("Renderer not found")

import asyncio
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from ktvibes.library import Library
from ktvibes.renderers import normalize_url


CSV = "artist,title,language,source_id\nIU,Good Day,ko,abcdefghijk\nAimer,Ref:rain,ja,bbbbbbbbbbb\n"


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "library.sqlite3"
        self.library = Library(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_import_preserves_review_state(self):
        self.assertEqual(self.library.import_csv(CSV), {"imported": 2, "existing": 0, "skipped": 0})
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.assertEqual(self.library.import_csv(CSV), {"imported": 0, "existing": 2, "skipped": 0})
        again = {item["id"]: item for item in Library(self.path).candidates()}
        self.assertEqual(again[song["id"]]["review_status"], "approved")

    def test_candidates_and_jobs_support_stable_pages(self):
        self.library.import_csv(CSV)
        first = self.library.candidates(limit=1)
        second = self.library.candidates(limit=1, offset=1)
        self.assertEqual((self.library.candidate_count(), len(first), len(second)), (2, 1, 1))
        self.assertNotEqual(first[0]["id"], second[0]["id"])
        for song in self.library.candidates():
            self.library.review([song["id"]], "approved")
            self.library.enqueue([song["id"]])
        self.assertEqual(self.library.job_count(), 2)
        self.assertGreater(self.library.jobs(limit=1)[0]["id"], self.library.jobs(limit=1, offset=1)[0]["id"])

    def test_discovery_import_deduplicates_and_preserves_review_state(self):
        songs = [{"id": "abcdefghijk", "artist": "Ado", "title": "One"},
                 {"id": "abcdefghijk", "artist": "Ado", "title": "Duplicate"},
                 {"id": "bad", "artist": "Ado", "title": "Invalid"}]
        self.assertEqual(self.library.import_discoveries(songs), {"imported": 1, "existing": 0, "skipped": 2})
        candidate = self.library.candidates()[0]
        self.library.review([candidate["id"]], "approved")
        self.assertEqual(self.library.import_discoveries([{"id": "abcdefghijk", "artist": "Ado", "title": "Updated"}]),
                         {"imported": 0, "existing": 1, "skipped": 0})
        updated = self.library.candidate(candidate["id"])
        self.assertEqual((updated["title"], updated["review_status"]), ("Updated", "approved"))

    def test_unapproved_song_cannot_prepare_and_withdrawn_approval_cancels(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        with self.assertRaisesRegex(ValueError, "not approved"):
            self.library.enqueue([song["id"]])
        self.library.review([song["id"]], "approved")
        self.assertEqual(self.library.enqueue([song["id"]]), 1)
        self.library.review([song["id"]], "deferred")
        self.assertEqual(self.library.jobs()[0]["status"], "cancelled")
        self.assertIsNone(self.library.claim_job())

    def test_running_preparation_blocks_review_change(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.enqueue([song["id"]])
        self.library.claim_job()
        with self.assertRaisesRegex(ValueError, "running preparation"):
            self.library.review([song["id"]], "rejected")
        self.assertEqual(self.library.candidate(song["id"])["review_status"], "approved")

    def test_concurrent_enqueue_creates_one_active_job(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        barrier = threading.Barrier(3)
        results = []
        def enqueue():
            barrier.wait()
            results.append(self.library.enqueue([song["id"]]))
        threads = [threading.Thread(target=enqueue) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(results), 1)
        self.assertEqual(len([job for job in self.library.jobs() if job["status"] in ("queued", "running")]), 1)

    def test_running_job_is_recovered_after_restart(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.enqueue([song["id"]])
        self.assertEqual(self.library.claim_job()["status"], "queued")
        recovered = Library(self.path)
        self.assertEqual(recovered.jobs()[0]["status"], "queued")
        self.assertEqual(recovered.candidates()[0]["processing_status"], "queued")

    def test_unavailable_renderer_defers_running_job(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.enqueue([song["id"]])
        job = self.library.claim_job()
        self.library.defer_job(job["id"], "Renderer unavailable: HTTP 403")
        deferred = self.library.jobs()[0]
        self.assertEqual((deferred["status"], deferred["stage"], deferred["error"]),
                         ("queued", None, "Renderer unavailable: HTTP 403"))
        self.assertEqual(self.library.candidate(song["id"])["processing_status"], "queued")
        claimed = self.library.claim_job()
        self.assertEqual((claimed["id"], self.library.jobs()[0]["error"]), (job["id"], None))

    def test_jobs_can_pause_resume_and_start_next(self):
        self.library.import_csv(CSV)
        songs = self.library.candidates()
        for song in songs:
            self.library.review([song["id"]], "approved")
            self.library.enqueue([song["id"]])
        jobs = {job["candidate_id"]: job for job in self.library.jobs()}
        first, second = songs
        self.assertEqual(self.library.pause_jobs(jobs[first["id"]]["id"]), 1)
        self.assertEqual(self.library.candidate(first["id"])["processing_status"], "paused")
        self.assertEqual(self.library.enqueue([first["id"]]), 0)
        self.assertEqual(self.library.resume_jobs(jobs[first["id"]]["id"]), 1)
        self.assertEqual(self.library.pause_jobs(), 2)
        self.assertEqual(self.library.resume_jobs(), 2)
        self.library.resume_jobs(jobs[second["id"]]["id"], prioritize=True)
        self.assertEqual(self.library.claim_job()["id"], jobs[second["id"]]["id"])
        with self.assertRaisesRegex(ValueError, "cannot be paused safely"):
            self.library.pause_jobs(jobs[second["id"]]["id"])

    def test_failed_job_retries_in_place(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.enqueue([song["id"]])
        job = self.library.claim_job()
        self.library.finish_job(job["id"], False, "network failed")
        self.assertEqual(self.library.retry_job(job["id"]), 1)
        retried = self.library.claim_job()
        self.assertEqual((retried["id"], self.library.jobs()[0]["attempts"]), (job["id"], 2))

    def test_historical_failure_cannot_regress_a_ready_candidate(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.enqueue([song["id"]])
        failed = self.library.claim_job()
        self.library.finish_job(failed["id"], False, "first attempt failed")
        self.library.enqueue([song["id"]])
        ready = self.library.claim_job()
        self.library.finish_job(ready["id"], True)
        with self.assertRaisesRegex(ValueError, "already prepared"):
            self.library.retry_job(failed["id"])
        self.library.publication_failed(song["external_id"], "metadata publish failed")
        with self.assertRaisesRegex(ValueError, "already prepared"):
            self.library.retry_job(failed["id"])

    def test_cache_reconciliation_marks_existing_and_deleted_artifacts(self):
        self.library.import_csv(CSV)
        songs = {song["external_id"]: song for song in self.library.candidates()}
        self.library.reconcile({"abcdefghijk"})
        current = {song["external_id"]: song for song in self.library.candidates()}
        self.assertEqual(current["abcdefghijk"]["processing_status"], "ready")
        self.assertEqual(current["bbbbbbbbbbb"]["processing_status"], "not_started")
        self.library.reconcile(set())
        self.assertEqual(self.library.candidate(songs["abcdefghijk"]["id"])["processing_status"], "not_started")

    def test_reconciliation_finishes_a_recovered_job_with_published_artifacts(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.enqueue([song["id"]])
        self.library.claim_job()
        recovered = Library(self.path)
        recovered.reconcile({song["external_id"]})
        self.assertEqual(recovered.jobs()[0]["status"], "ready")
        self.assertIsNone(recovered.claim_job())

    def test_publication_failure_makes_a_ready_candidate_retryable(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.reconcile({song["external_id"]})
        self.library.publication_failed(song["external_id"], "share offline")
        self.assertEqual(self.library.candidate(song["id"])["processing_status"], "failed")
        self.assertEqual(self.library.enqueue([song["id"]]), 1)
        self.library.publication_succeeded(song["external_id"])
        self.assertEqual(self.library.candidate(song["id"])["processing_status"], "ready")
        self.assertEqual(self.library.jobs()[0]["status"], "ready")

    def test_renderer_selection_persists(self):
        saved = self.library.add_renderer("gpu", "GPU desktop", "http://192.168.1.42:8790")
        self.assertEqual(saved["name"], "GPU desktop")
        self.library.renderer_seen("gpu", "machine-a")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.library.renderer_seen("gpu", "machine-b")
        self.library.select_renderer("gpu")
        self.assertEqual(Library(self.path).selected_renderer()["id"], "gpu")
        with self.assertRaisesRegex(ValueError, "Select another renderer"):
            self.library.delete_renderer("gpu")
        self.library.select_renderer("local")
        self.library.delete_renderer("gpu")
        self.assertEqual(self.library.selected_renderer()["id"], "local")

    def test_recovered_job_keeps_its_claimed_renderer(self):
        self.library.import_csv(CSV)
        song = self.library.candidates()[0]
        self.library.review([song["id"]], "approved")
        self.library.add_renderer("gpu", "GPU desktop", "http://192.168.1.42:8790")
        self.library.select_renderer("gpu")
        self.library.enqueue([song["id"]])
        self.assertEqual(self.library.claim_job()["renderer_id"], "gpu")
        recovered = Library(self.path)
        recovered.select_renderer("local")
        self.assertEqual(recovered.claim_job()["renderer_id"], "gpu")

    def test_csv_requires_artist_and_title(self):
        with self.assertRaisesRegex(ValueError, "artist and title"):
            self.library.import_csv("name,source_id\nSong,abcdefghijk\n")


class RendererURLTests(unittest.TestCase):
    def test_private_and_tailscale_addresses_are_allowed(self):
        self.assertEqual(normalize_url("http://192.168.1.42:8790/"), "http://192.168.1.42:8790")
        self.assertEqual(normalize_url("https://100.100.10.2:8790"), "https://100.100.10.2:8790")
        self.assertEqual(normalize_url("http://192.168.1.42"), "http://192.168.1.42:8790")

    def test_public_hostnames_and_url_paths_are_rejected(self):
        for url in ("http://example.com:8790", "http://8.8.8.8:8790", "http://192.168.1.42:8790/admin",
                    "http://192.168.1.42:abc", "http://192.168.1.42:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_health_rejects_renderer_missing_required_tools(self):
        from ktvibes import renderers
        response = Mock()
        response.json.return_value = {"protocol": 1, "machine_id": "gpu", "ready": False,
                                      "missing_tools": ["ffprobe"], "capabilities": ["separation", "alignment", "guides"]}
        with patch("httpx.get", return_value=response), self.assertRaisesRegex(RuntimeError, "ffprobe"):
            renderers.health({"id": "gpu", "url": "http://192.168.1.42:8790"})

    def test_health_rejects_renderer_without_remote_alignment(self):
        from ktvibes import renderers
        response = Mock()
        response.json.return_value = {"protocol": 1, "machine_id": "gpu", "ready": True, "capabilities": ["separation"]}
        with patch("httpx.get", return_value=response), self.assertRaisesRegex(RuntimeError, "update and restart"):
            renderers.health({"id": "gpu", "url": "http://192.168.1.42:8790"})

    def test_health_rejects_malformed_readiness(self):
        from ktvibes import renderers
        response = Mock()
        response.json.return_value = {"protocol": 1, "machine_id": "gpu", "capabilities": ["alignment"]}
        with patch("httpx.get", return_value=response), self.assertRaisesRegex(RuntimeError, "readiness metadata"):
            renderers.health({"id": "gpu", "url": "http://192.168.1.42:8790"})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temp.name) / "library.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_channel_url_normalization(self):
        from ktvibes.youtube import normalize_channel_url
        self.assertEqual(normalize_channel_url("https://www.youtube.com/@ChaR_tarako/videos"),
                         "https://www.youtube.com/@ChaR_tarako")
        with self.assertRaises(ValueError):
            normalize_channel_url("https://example.com/@ChaR_tarako")

    def test_discovery_collects_sources_and_persists_status(self):
        from ktvibes import discovery
        discovery.save_settings(self.library, {"enabled": True, "interval_hours": 24, "results_per_source": 5,
                                               "search_terms": ["Ado"],
                                               "channels": ["https://www.youtube.com/@ChaR_tarako"]})
        searched = {"id": "abcdefghijk", "channel": "", "parsed": {"artist": "", "title": "Song"}}
        channel = {"id": "bbbbbbbbbbb", "channel": "Cha'R", "parsed": {"artist": "Cha'R", "title": "Glow"}}
        with patch.object(discovery.youtube, "search", return_value=[searched]) as search, \
                patch.object(discovery.youtube, "channel_videos", return_value=[channel]) as channel_videos:
            result = discovery.run(self.library)
        self.assertEqual((result["imported"], result["found"], result["sources"]), (2, 2, 2))
        self.assertEqual([call.kwargs["page"] for call in search.call_args_list], [0, 1])
        self.assertEqual([call.kwargs["page"] for call in channel_videos.call_args_list], [0, 1])
        with patch.object(discovery.youtube, "search", return_value=[searched]) as search, \
                patch.object(discovery.youtube, "channel_videos", return_value=[channel]) as channel_videos:
            discovery.run(self.library)
        self.assertEqual([call.kwargs["page"] for call in search.call_args_list], [0, 2])
        self.assertEqual([call.kwargs["page"] for call in channel_videos.call_args_list], [0, 2])
        self.assertEqual({song["artist"] for song in self.library.candidates()}, {"Ado", "Cha'R"})
        self.assertEqual(discovery.status(self.library)["state"], "idle")
        self.assertFalse(discovery.due(self.library))


class PreparationLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_recording_is_prepared_once_at_a_time(self):
        from ktvibes import worker
        active = maximum = 0
        async def fake_prepare(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
        item = {"id": "locktest001"}
        with patch.object(worker, "_prepare", fake_prepare):
            await asyncio.gather(worker.prepare(None, item, Path(".")), worker.prepare(None, item, Path(".")))
        self.assertEqual(maximum, 1)

    async def test_different_recordings_are_also_prepared_one_at_a_time(self):
        from ktvibes import worker
        active = maximum = 0
        async def fake_prepare(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
        with patch.object(worker, "_prepare", fake_prepare):
            await asyncio.gather(worker.prepare(None, {"id": "locktest002"}, Path(".")),
                                 worker.prepare(None, {"id": "locktest003"}, Path(".")))
        self.assertEqual(maximum, 1)


class StemPublicationTests(unittest.TestCase):
    def test_incomplete_marker_hides_a_partial_remote_generation(self):
        from ktvibes import stems
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in stems.NAMES:
                (folder / f"{name}.opus").touch()
            self.assertTrue(stems.ready(folder))
            (folder / ".stems-incomplete").touch()
            self.assertFalse(stems.ready(folder))
            self.assertFalse(stems.prepared(folder))

    def test_remote_stem_receipt_recovers_metadata_only_for_matching_stems(self):
        from ktvibes import renderers
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'vocals.opus').write_bytes(b'vocals')
            (folder / 'no_vocals.opus').write_bytes(b'music')
            receipt = {'seconds': 12.5, 'gain': 1.25, 'song_end': 198.0,
                       'stems': renderers._stem_signature(folder)}
            (folder / renderers.REMOTE_RESULT).write_text(json.dumps(receipt), encoding='utf-8')
            self.assertEqual(renderers.cached_separation(folder), (12.5, 1.25, {'song_end': 198.0}))
            (folder / 'vocals.opus').write_bytes(b'changed')
            self.assertIsNone(renderers.cached_separation(folder))


class SharedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()
        self.library = Library(self.root / "library.sqlite3", self.incoming)

    def tearDown(self):
        self.temp.cleanup()

    def test_scan_imports_named_m4a_and_claims_local_job(self):
        (self.incoming / "IU - Good Day.m4a").write_bytes(b"m4a-source")
        (self.incoming / "needs metadata.m4a").write_bytes(b"other")
        result = self.library.scan_imports()
        self.assertEqual((result["imported"], result["skipped"]), (1, 1))
        song = self.library.candidates()[0]
        self.assertEqual((song["provider"], song["source_path"], len(song["external_id"])),
                         ("local", "IU - Good Day.m4a", 22))
        self.assertTrue(song["external_id"].startswith("local_"))
        self.library.review([song["id"]], "approved")
        self.assertEqual(self.library.enqueue([song["id"]]), 1)
        claimed = self.library.claim_job()
        self.assertEqual((claimed["provider"], claimed["source_path"], claimed["source_hash"]),
                         ("local", "IU - Good Day.m4a", song["source_hash"]))

    def test_csv_filename_supplies_metadata_and_blocks_escape(self):
        (self.incoming / "track.m4a").write_bytes(b"m4a-source")
        result = self.library.import_csv("artist,title,language,filename\nAimer,Ref:rain,ja,track.m4a\n")
        self.assertEqual(result["imported"], 1)
        song = self.library.candidates()[0]
        self.assertEqual((song["artist"], song["title"], song["language"]), ("Aimer", "Ref:rain", "ja"))
        with self.assertRaisesRegex(ValueError, "relative"):
            self.library.local_source("../secret.m4a")

    def test_changed_shared_file_invalidates_ready_candidate(self):
        source = self.incoming / "IU - Good Day.m4a"
        source.write_bytes(b"first")
        self.library.scan_imports()
        song = self.library.candidates()[0]
        first_id = song["external_id"]
        self.library.reconcile({first_id})
        source.write_bytes(b"second")
        self.library.review([song["id"]], "approved")
        with self.assertRaisesRegex(ValueError, "scan the shared folder again"):
            self.library.enqueue([song["id"]])
        self.library.scan_imports()
        changed = self.library.candidate(song["id"])
        self.assertNotEqual(changed["external_id"], first_id)
        self.assertEqual(changed["processing_status"], "not_started")

    def test_local_source_is_staged_with_checksum(self):
        from ktvibes.worker import stage_local
        source = self.incoming / "IU - Good Day.m4a"
        source.write_bytes(b"m4a-source")
        relative, checksum, _ = self.library.local_source(source.name)
        destination = self.root / "cache"
        destination.mkdir()
        progress = []
        stage_local(self.incoming, relative, destination, checksum, progress.append)
        self.assertEqual((destination / "audio.m4a").read_bytes(), b"m4a-source")
        self.assertEqual(progress[-1], 1)
        with self.assertRaisesRegex(ValueError, "changed after import"):
            stage_local(self.incoming, relative, destination, "0" * 64)

    def test_equal_files_at_different_paths_have_distinct_cache_ids(self):
        (self.incoming / "A - One.m4a").write_bytes(b"same")
        (self.incoming / "B - Two.m4a").write_bytes(b"same")
        self.library.scan_imports()
        self.assertEqual(len({song["external_id"] for song in self.library.candidates()}), 2)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for the real M4A fixture")
    def test_real_m4a_can_be_hashed_and_staged(self):
        from ktvibes.worker import stage_local
        source = self.incoming / "A - Tone.m4a"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1",
                        "-c:a", "aac", str(source)], check=True)
        relative, checksum, recording_id = self.library.local_source(source.name)
        destination = self.root / "real-cache"
        destination.mkdir()
        stage_local(self.incoming, relative, destination, checksum)
        self.assertTrue(recording_id.startswith("local_"))
        self.assertGreater((destination / "audio.m4a").stat().st_size, 0)

    @unittest.skipIf(__import__('os').name == 'nt', 'Windows symlink privileges vary')
    def test_symlink_source_is_rejected(self):
        outside = self.root / "outside.m4a"
        outside.write_bytes(b"outside")
        (self.incoming / "A - Escape.m4a").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.library.local_source("A - Escape.m4a")


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ktvibes import exports


class SharedExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.share = self.root / "share"
        self.share.mkdir()
        self.output_patch = patch.object(exports, "REQUIRED_ROOT", self.share)
        self.mount_patch = patch.object(exports.os.path, "ismount", side_effect=lambda path: Path(path) == self.share)
        self.output_patch.start()
        self.mount_patch.start()
        self.folder = self.cache / "abcdefghijk"
        self.folder.mkdir(parents=True)
        (self.folder / "audio.m4a").write_bytes(b"audio")
        (self.folder / "vocals.opus").write_bytes(b"vocals")
        (self.folder / "no_vocals.opus").write_bytes(b"music")
        (self.folder / "meta.json").write_text(
            json.dumps({"artist": "Ado", "title": "Show", "provider": "youtube"}), encoding="utf-8")

    def tearDown(self):
        self.mount_patch.stop()
        self.output_patch.stop()
        self.temp.cleanup()

    def test_publish_writes_complete_package_and_checksum_manifest(self):
        manifest = exports.publish(self.folder, self.share)
        package = self.share / "imported" / "abcdefghijk"
        self.assertEqual((package / "vocals.opus").read_bytes(), b"vocals")
        self.assertTrue(json.loads((package / "manifest.json").read_text(encoding="utf-8"))["complete"])
        saved = json.loads((self.share / "manifests" / "abcdefghijk.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, manifest)
        self.assertEqual(saved["artifacts"]["audio.m4a"]["sha256"], hashlib.sha256(b"audio").hexdigest())
        self.assertTrue(exports.validate(self.folder, self.share))

    def test_republish_replaces_old_package_and_clears_failure(self):
        exports.publish(self.folder, self.share)
        exports.record_failure(self.share, "abcdefghijk", {"artist": "Ado", "title": "Show"}, RuntimeError("offline"))
        (self.folder / "vocals.opus").write_bytes(b"new vocals")
        exports.publish(self.folder, self.share)
        self.assertEqual((self.share / "imported" / "abcdefghijk" / "vocals.opus").read_bytes(), b"new vocals")
        self.assertFalse((self.share / "failed" / "abcdefghijk.json").exists())
        self.assertEqual(list((self.share / "imported").glob(".*.old")), [])

    def test_metadata_republish_keeps_media_and_package_directory_in_place(self):
        exports.publish(self.folder, self.share)
        package = self.share / "imported" / "abcdefghijk"
        package_inode = package.stat().st_ino
        audio_inode = (package / "audio.m4a").stat().st_ino
        (self.folder / "lyrics.lrc").write_text("[00:01.00]Show", encoding="utf-8")
        meta = json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))
        meta["lyric_offset"] = 0.25
        (self.folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        exports.publish(self.folder, self.share)
        self.assertEqual((package.stat().st_ino, (package / "audio.m4a").stat().st_ino),
                         (package_inode, audio_inode))
        self.assertEqual((package / "lyrics.lrc").read_text(encoding="utf-8"), "[00:01.00]Show")
        self.assertTrue(exports.validate(self.folder, self.share))

    def test_metadata_copy_failure_preserves_every_authoritative_file(self):
        (self.folder / "lyrics.lrc").write_text("[00:01.00]Original", encoding="utf-8")
        exports.publish(self.folder, self.share)
        package = self.share / "imported" / "abcdefghijk"
        original_meta = (package / "meta.json").read_bytes()
        original_lyrics = (package / "lyrics.lrc").read_bytes()
        (self.folder / "lyrics.lrc").write_text("[00:01.00]Changed", encoding="utf-8")
        meta = json.loads((self.folder / "meta.json").read_text(encoding="utf-8"))
        meta["lyric_offset"] = 1
        (self.folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        def partial_copy(_source, destination):
            destination.write_bytes(b"partial")
            raise PermissionError("busy")
        with patch.object(exports.shutil, "copy2", side_effect=partial_copy), self.assertRaises(PermissionError):
            exports.publish(self.folder, self.share)
        self.assertEqual((package / "meta.json").read_bytes(), original_meta)
        self.assertEqual((package / "lyrics.lrc").read_bytes(), original_lyrics)
        self.assertTrue(exports.validate_shared(self.share, "abcdefghijk"))
        self.assertEqual(list(package.glob(".*.tmp")), [])

    def test_delete_failure_restores_package_and_manifest(self):
        exports.publish(self.folder, self.share)
        manifest = self.share / "manifests" / "abcdefghijk.json"
        unlink = Path.unlink
        failed = False
        def fail_manifest(path, *args, **kwargs):
            nonlocal failed
            if path == manifest and not failed:
                failed = True
                raise PermissionError("busy")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_manifest), self.assertRaises(PermissionError):
            exports.delete(self.share, "abcdefghijk")
        self.assertTrue(exports.validate(self.folder, self.share))

    def test_manifest_failure_restores_previous_complete_package(self):
        exports.publish(self.folder, self.share)
        previous_manifest = (self.share / "manifests" / "abcdefghijk.json").read_bytes()
        (self.folder / "vocals.opus").write_bytes(b"new vocals")
        atomic_json = exports._atomic_json
        def fail_manifest(path, value):
            if path.parent.name == "manifests":
                raise OSError("manifest offline")
            return atomic_json(path, value)
        with patch.object(exports, "_atomic_json", side_effect=fail_manifest), self.assertRaisesRegex(OSError, "manifest offline"):
            exports.publish(self.folder, self.share)
        self.assertEqual((self.share / "imported" / "abcdefghijk" / "vocals.opus").read_bytes(), b"vocals")
        self.assertEqual((self.share / "manifests" / "abcdefghijk.json").read_bytes(), previous_manifest)
        self.assertTrue((self.share / "failed" / "abcdefghijk.json").is_file())

    def test_incomplete_package_records_failure(self):
        (self.folder / "vocals.opus").unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            exports.publish(self.folder, self.share)
        failure = json.loads((self.share / "failed" / "abcdefghijk.json").read_text(encoding="utf-8"))
        self.assertIn("incomplete", failure["error"])
        self.assertFalse((self.share / "imported" / "abcdefghijk").exists())

    def test_incomplete_identity_metadata_is_not_publishable(self):
        (self.folder / "meta.json").write_text(json.dumps({"stem_gain": 1}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "metadata is incomplete"):
            exports.publish(self.folder, self.share)
        self.assertFalse((self.share / "imported" / "abcdefghijk").exists())

    def test_shared_validation_rejects_metadata_that_disagrees_with_manifest(self):
        exports.publish(self.folder, self.share)
        package_meta = self.share / "imported" / "abcdefghijk" / "meta.json"
        package_meta.write_text(json.dumps({"artist": "Wrong", "title": "Show", "provider": "youtube"}), encoding="utf-8")
        self.assertFalse(exports.validate_shared(self.share, "abcdefghijk", verify_hashes=False))

    def test_backfill_publishes_existing_prepared_songs(self):
        self.assertEqual(exports.backfill(self.cache, self.share), {"published": 1, "failed": 0})
        self.assertTrue((self.share / "imported" / "abcdefghijk" / "manifest.json").is_file())
        exported_at = json.loads((self.share / "manifests" / "abcdefghijk.json").read_text(encoding="utf-8"))["exported_at"]
        self.assertEqual(exports.backfill(self.cache, self.share), {"published": 0, "failed": 0})
        self.assertEqual(json.loads((self.share / "manifests" / "abcdefghijk.json").read_text(encoding="utf-8"))["exported_at"], exported_at)

    def test_reconcile_repairs_tampered_share_and_same_size_local_change(self):
        exports.publish(self.folder, self.share)
        package_audio = self.share / "imported" / "abcdefghijk" / "audio.m4a"
        package_audio.write_bytes(b"xxxxx")
        self.assertFalse(exports.validate(self.folder, self.share))
        self.assertEqual(exports.reconcile(self.cache, self.share), {"abcdefghijk"})
        self.assertEqual(package_audio.read_bytes(), b"audio")
        (self.folder / "audio.m4a").write_bytes(b"other")
        self.assertEqual(exports.reconcile(self.cache, self.share), {"abcdefghijk"})
        self.assertEqual(package_audio.read_bytes(), b"other")

    def test_validation_rejects_unmanifested_shared_files(self):
        exports.publish(self.folder, self.share)
        (self.share / "imported" / "abcdefghijk" / "stale.wav").write_bytes(b"stale")
        self.assertFalse(exports.validate(self.folder, self.share))
        self.assertEqual(exports.reconcile(self.cache, self.share), {"abcdefghijk"})
        self.assertFalse((self.share / "imported" / "abcdefghijk" / "stale.wav").exists())

    def test_fast_validation_uses_manifests_without_rereading_shared_bytes(self):
        exports.publish(self.folder, self.share)
        shared_audio = self.share / "imported" / "abcdefghijk" / "audio.m4a"
        original_sha256 = exports._sha256
        def reject_shared(path):
            if path == shared_audio:
                raise AssertionError("startup reread shared artifact bytes")
            return original_sha256(path)
        with patch.object(exports, "_sha256", side_effect=reject_shared):
            self.assertTrue(exports.validate(self.folder, self.share, verify_shared_hashes=False))
        shared_audio.write_bytes(b"different size")
        self.assertFalse(exports.validate(self.folder, self.share, verify_shared_hashes=False))

    def test_published_package_is_valid_without_local_cache_and_can_be_staged(self):
        exports.publish(self.folder, self.share)
        shutil.rmtree(self.folder)
        self.assertTrue(exports.validate_shared(self.share, "abcdefghijk"))
        self.assertEqual(exports.published(self.share), {"abcdefghijk"})
        exports.stage(self.share, "abcdefghijk", self.folder)
        self.assertTrue(exports.validate(self.folder, self.share))

    def test_delete_removes_share_only_package_and_manifest(self):
        exports.publish(self.folder, self.share)
        shutil.rmtree(self.folder)
        exports.delete(self.share, "abcdefghijk")
        self.assertFalse((self.share / "imported" / "abcdefghijk").exists())
        self.assertFalse((self.share / "manifests" / "abcdefghijk.json").exists())
        self.assertEqual(exports.published(self.share), set())

    def test_unavailable_share_is_not_created(self):
        missing = self.root / "not-mounted"
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            exports.publish(self.folder, missing)
        self.assertFalse(missing.exists())
        self.assertEqual(exports.reconcile(self.cache, missing), set())
        wrong = self.root / "ordinary-local-folder"
        wrong.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            exports.publish(self.folder, wrong)
        self.assertFalse((wrong / "imported").exists())


if __name__ == "__main__":
    unittest.main()

import hashlib
import json
from pathlib import Path
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

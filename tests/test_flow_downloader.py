import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from flow_downloader import (
    FlowAutomationError,
    FlowWorker,
    ManifestStore,
    PlaywrightError,
    QueueItem,
    QueueValidationError,
    build_manual_login_command,
    detect_image_format,
    is_valid_image,
    is_valid_mp4,
    load_queue,
    safe_diagnostic_stem,
    validate_output_filename,
)


class QueueTests(unittest.TestCase):
    def write_queue(self, directory: Path, body: str) -> Path:
        path = directory / "queue.csv"
        path.write_text(body, encoding="utf-8")
        return path

    def test_loads_valid_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_queue(
                Path(raw),
                "flow_name,output_filename\nVideo_01.mp4,scene_01_1080p.mp4\n",
            )
            self.assertEqual(
                load_queue(path),
                [QueueItem("Video_01.mp4", "scene_01_1080p.mp4")],
            )

    def test_rejects_missing_columns(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_queue(Path(raw), "name,file\na,b\n")
            with self.assertRaises(QueueValidationError):
                load_queue(path)

    def test_rejects_duplicate_flow_names_case_insensitively(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_queue(
                Path(raw),
                "flow_name,output_filename\nClip.mp4,a.mp4\nclip.mp4,b.mp4\n",
            )
            with self.assertRaisesRegex(QueueValidationError, "Duplicate Flow name"):
                load_queue(path)

    def test_rejects_duplicate_output_names_case_insensitively(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_queue(
                Path(raw),
                "flow_name,output_filename\nA,a.mp4\nB,A.MP4\n",
            )
            with self.assertRaisesRegex(QueueValidationError, "Duplicate output"):
                load_queue(path)

    def test_rejects_unsafe_filenames(self):
        for value in ("../x.mp4", "folder/x.mp4", "movie.webm", "CON.mp4", "bad?.mp4"):
            with self.subTest(value=value), self.assertRaises(QueueValidationError):
                validate_output_filename(value)

    def test_loads_image_queue_using_the_same_queue_filename(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_queue(
                Path(raw),
                "flow_name,output_filename\nImage_01.png,scene_01.png\n",
            )
            self.assertEqual(
                load_queue(path, "images"),
                [QueueItem("Image_01.png", "scene_01.png")],
            )

    def test_image_mode_rejects_video_output(self):
        with self.assertRaisesRegex(QueueValidationError, "Image output"):
            validate_output_filename("scene.mp4", "images")


class ImageTests(unittest.TestCase):
    def test_validates_supported_image_signatures(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            png = directory / "image.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
            jpeg = directory / "image.jpg"
            jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
            webp = directory / "image.webp"
            webp.write_bytes(b"RIFF\x08\x00\x00\x00WEBPVP8 ")
            self.assertTrue(is_valid_image(png))
            self.assertTrue(is_valid_image(jpeg))
            self.assertTrue(is_valid_image(webp))

    def test_rejects_wrong_image_signature_or_extension(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            wrong = directory / "image.png"
            wrong.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 12)
            unsupported = directory / "image.gif"
            unsupported.write_bytes(b"GIF89a" + b"\x00" * 10)
            self.assertFalse(is_valid_image(wrong))
            self.assertFalse(is_valid_image(unsupported))

    def test_converts_jpeg_bytes_to_requested_png(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            mismatched = directory / "flow-output.png"
            Image.new("RGB", (4, 4), (20, 40, 60)).save(
                mismatched, format="JPEG"
            )
            self.assertEqual(detect_image_format(mismatched), "jpeg")
            logger = logging.getLogger("image-conversion-test")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            worker = FlowWorker(
                context=None,
                project_url="https://example.invalid",
                output_dir=directory,
                manifest=ManifestStore(directory / "state.json"),
                logger=logger,
                media_type="images",
            )
            worker._normalize_image_format(mismatched, "image/jpeg")
            self.assertEqual(detect_image_format(mismatched), "png")
            self.assertTrue(is_valid_image(mismatched))


class Mp4Tests(unittest.TestCase):
    def test_validates_iso_base_media_signature(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "video.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isom")
            self.assertTrue(is_valid_mp4(path))

    def test_rejects_empty_or_wrong_file(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            empty = directory / "empty.mp4"
            empty.write_bytes(b"")
            wrong = directory / "wrong.mp4"
            wrong.write_bytes(b"not an mp4 file")
            self.assertFalse(is_valid_mp4(empty))
            self.assertFalse(is_valid_mp4(wrong))


class ManifestTests(unittest.TestCase):
    def test_manifest_round_trip_and_atomic_shape(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            item = QueueItem("Clip.mp4", "clip_1080p.mp4")
            store = ManifestStore(path)
            store.update(
                item,
                status="completed",
                attempts=1,
                output_path="C:/output/clip_1080p.mp4",
            )
            reloaded = ManifestStore(path)
            state = reloaded.get(item)
            self.assertEqual(state.status, "completed")
            self.assertEqual(state.attempts, 1)
            self.assertIsNotNone(state.updated_at)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual(list(Path(raw).glob("*.tmp")), [])

    def test_changed_output_resets_item_state(self):
        with tempfile.TemporaryDirectory() as raw:
            store = ManifestStore(Path(raw) / "state.json")
            first = QueueItem("Clip.mp4", "one.mp4")
            store.update(first, status="completed", attempts=1)
            changed = store.get(QueueItem("Clip.mp4", "two.mp4"))
            self.assertEqual(changed.status, "pending")
            self.assertEqual(changed.attempts, 0)


class UtilityTests(unittest.TestCase):
    def test_safe_diagnostic_stem(self):
        self.assertEqual(safe_diagnostic_stem("Scene: 09 / test.mp4"), "Scene_09_test.mp4")

    def test_manual_login_uses_normal_chrome_without_automation_flags(self):
        command = build_manual_login_command(
            Path("C:/Chrome/chrome.exe"), Path("profile"), "https://example.test/project"
        )
        joined = " ".join(command).lower()
        self.assertIn("--user-data-dir=", joined)
        self.assertNotIn("--enable-automation", joined)
        self.assertNotIn("remote-debugging", joined)
        self.assertEqual(command[-1], "https://example.test/project")


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_adopts_existing_valid_output_without_redownloading(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            output = directory / "manual.mp4"
            output.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isom")
            item = QueueItem("Clip.mp4", output.name)
            manifest = ManifestStore(directory / "state.json")
            logger = logging.getLogger("adopt-existing-output-test")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            worker = FlowWorker(
                context=None,
                project_url="https://example.invalid",
                output_dir=directory,
                manifest=manifest,
                logger=logger,
            )

            completed, skipped, failed = await worker.process_queue([item])
            self.assertEqual((completed, skipped, failed), (0, 1, 0))
            self.assertEqual(manifest.get(item).status, "completed")

    async def test_recovers_native_download_after_playwright_target_closes(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            stage = directory / "stage"
            stage.mkdir()
            staged_file = stage / "browser-generated-id"
            staged_file.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isom")
            logger = logging.getLogger("native-download-recovery-test")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            worker = FlowWorker(
                context=None,
                project_url="https://example.invalid",
                output_dir=directory,
                manifest=ManifestStore(directory / "state.json"),
                logger=logger,
                download_stage_dir=stage,
            )

            class ClosedDownload:
                async def save_as(self, _path):
                    raise PlaywrightError("Target page, context or browser has been closed")

            output = directory / "recovered.mp4"
            await worker._save_download(ClosedDownload(), output)
            self.assertTrue(is_valid_mp4(output))
            self.assertFalse(staged_file.exists())

    async def test_two_retries_means_three_total_attempts(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            item = QueueItem("Clip.mp4", "clip.mp4")
            manifest = ManifestStore(directory / "state.json")
            logger = logging.getLogger("retry-unit-test")
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            worker = FlowWorker(
                context=None,
                project_url="https://example.invalid",
                output_dir=directory,
                manifest=manifest,
                logger=logger,
                inactivity_timeout_seconds=0.01,
                max_retries=2,
                diagnostic_dir=directory / "diagnostics",
            )
            worker.page = object()
            requests = 0
            reloads = 0

            async def inactive():
                return False

            async def find_card(_name):
                return object()

            async def request(_card):
                nonlocal requests
                requests += 1

            async def fail_download(_name):
                raise FlowAutomationError("fixture failure")

            async def reload_page():
                nonlocal reloads
                reloads += 1

            async def no_op(*_args, **_kwargs):
                return None

            worker._active_upscale_visible = inactive
            worker._find_video_card = find_card
            worker._request_1080p_download = request
            worker._wait_for_download = fail_download
            worker._reload_for_retry = reload_page
            worker._capture_diagnostic = no_op
            worker._clear_download_events = no_op

            result = await worker._process_item_with_retries(item, directory / "clip.mp4")
            self.assertFalse(result)
            self.assertEqual(requests, 3)
            self.assertEqual(reloads, 2)
            self.assertEqual(manifest.get(item).attempts, 3)
            self.assertEqual(manifest.get(item).status, "failed")


if __name__ == "__main__":
    unittest.main()

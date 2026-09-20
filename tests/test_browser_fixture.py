import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

from flow_downloader import (
    FlowAutomationError,
    FlowWorker,
    ManifestStore,
    QueueItem,
    find_chrome,
    is_valid_image,
    is_valid_mp4,
)


@unittest.skipIf(async_playwright is None, "Playwright is not installed")
class BrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.download_stage = self.directory / "browser-downloads"
        self.download_stage.mkdir()
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.directory / "profile",
            executable_path=find_chrome(),
            headless=True,
            accept_downloads=True,
            downloads_path=self.download_stage,
        )

    async def asyncTearDown(self):
        await self.context.close()
        await self.playwright.stop()
        self.temp.cleanup()

    async def test_fixture_upscales_downloads_and_records_completion(self):
        logger = logging.getLogger("fixture-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        manifest = ManifestStore(self.directory / "state.json")
        fixture = Path(__file__).with_name("fixture_flow.html").resolve().as_uri()
        output_dir = self.directory / "output"
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=output_dir,
            manifest=manifest,
            logger=logger,
            inactivity_timeout_seconds=2,
            max_retries=0,
            poll_seconds=0.05,
            diagnostic_dir=self.directory / "diagnostics",
        )
        await worker.initialize()
        item = QueueItem("Fixture_Scene_01.mp4", "scene_01_1080p.mp4")
        completed, skipped, failed = await worker.process_queue([item])
        output = output_dir / item.output_filename
        self.assertEqual((completed, skipped, failed), (1, 0, 0))
        self.assertTrue(is_valid_mp4(output))
        self.assertEqual(
            await worker.page.evaluate("window.selectedResolution"),
            "1080p Upscaled",
        )
        self.assertNotEqual(
            await worker.page.evaluate("window.directDownloadClicked"),
            True,
        )
        self.assertNotEqual(await worker.page.evaluate("window.addPromptClicked"), True)
        self.assertNotEqual(await worker.page.evaluate("window.globalMenuClicked"), True)
        self.assertEqual(manifest.get(item).status, "completed")
        self.assertEqual(await worker.process_queue([item]), (0, 1, 0))

    async def test_image_fixture_upscales_2k_and_records_completion(self):
        logger = logging.getLogger("image-fixture-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        manifest = ManifestStore(self.directory / "image-state.json")
        fixture = Path(__file__).with_name("fixture_images.html").resolve().as_uri()
        output_dir = self.directory / "image-output"
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=output_dir,
            manifest=manifest,
            logger=logger,
            inactivity_timeout_seconds=2,
            max_retries=0,
            poll_seconds=0.05,
            diagnostic_dir=self.directory / "image-diagnostics",
            media_type="images",
        )
        await worker.initialize()
        item = QueueItem("Fixture_Image_01.png", "scene_01.png")
        completed, skipped, failed = await worker.process_queue([item])
        output = output_dir / item.output_filename
        self.assertEqual((completed, skipped, failed), (1, 0, 0))
        self.assertTrue(is_valid_image(output))
        self.assertEqual(
            await worker.page.evaluate("window.selectedImageResolution"),
            "2K Upscaled",
        )
        self.assertNotEqual(await worker.page.evaluate("window.originalClicked"), True)
        self.assertEqual(manifest.get(item).status, "completed")
        self.assertEqual(await worker.process_queue([item]), (0, 1, 0))

    async def test_hover_scan_lists_names_that_are_not_initially_mounted(self):
        logger = logging.getLogger("list-names-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        fixture = Path(__file__).with_name("fixture_flow.html").resolve().as_uri()
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=self.directory / "unused",
            manifest=ManifestStore(self.directory / "list-state.json"),
            logger=logger,
            inactivity_timeout_seconds=2,
            max_retries=0,
            poll_seconds=0.05,
            download_stage_dir=self.download_stage,
            intercept_blob_downloads=False,
        )
        await worker.initialize()
        # Reproduce Flow's narrow media cards when the right-hand Agent panel
        # consumes most of the viewport.
        await worker.page.add_style_tag(
            content="article { width: 220px; height: 150px; } "
            "article img { width: 220px; height: 124px; }"
        )
        self.assertEqual(await worker.discover_video_names(), ["Fixture_Scene_01.mp4"])

    async def test_exact_media_search_finds_card_and_reports_no_results(self):
        logger = logging.getLogger("search-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        fixture = Path(__file__).with_name("fixture_flow.html").resolve().as_uri()
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=self.directory / "unused-search",
            manifest=ManifestStore(self.directory / "search-state.json"),
            logger=logger,
            inactivity_timeout_seconds=5,
            max_retries=0,
            poll_seconds=0.05,
        )
        await worker.initialize()
        card = await worker._find_video_card("Fixture_Scene_01.mp4")
        self.assertEqual(await card.get_attribute("role"), "listitem")
        self.assertEqual(
            await worker.page.locator('input[aria-label="Search"]').input_value(),
            "Fixture_Scene_01",
        )
        with self.assertRaisesRegex(FlowAutomationError, "no exact video match"):
            await worker._find_video_card("Missing_Scene.mp4")

    async def test_hover_scan_traverses_virtualized_media_list_in_order(self):
        logger = logging.getLogger("virtual-list-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        fixture = Path(__file__).with_name("fixture_virtual_flow.html").resolve().as_uri()
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=self.directory / "unused-virtual",
            manifest=ManifestStore(self.directory / "virtual-state.json"),
            logger=logger,
            inactivity_timeout_seconds=12,
            max_retries=0,
            poll_seconds=0.05,
        )
        await worker.initialize()
        self.assertEqual(
            await worker.discover_video_names(),
            [f"Virtual_Scene_{index:02d}.mp4" for index in range(1, 8)],
        )

    async def test_active_upscale_does_not_timeout_at_inactivity_limit(self):
        logger = logging.getLogger("active-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        manifest = ManifestStore(self.directory / "state-active.json")
        fixture = Path(__file__).with_name("fixture_flow.html").resolve().as_uri()
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=self.directory / "output-active",
            manifest=manifest,
            logger=logger,
            inactivity_timeout_seconds=0.1,
            max_retries=0,
            poll_seconds=0.02,
        )
        await worker.initialize()
        page = worker.page
        await page.evaluate(
            """
            () => {
              const button = document.querySelector('#upscaled');
              button.onclick = () => {
                toast.textContent = 'Upscaling your video. This may take several minutes.';
                toast.style.display = 'block';
                setTimeout(() => {
                  toast.textContent = 'Upscaling complete!';
                  const bytes = new Uint8Array([
                    0,0,0,24,102,116,121,112,105,115,111,109,0,0,2,0,105,115,111,109
                  ]);
                  const link = document.createElement('a');
                  link.href = URL.createObjectURL(new Blob([bytes], {type: 'video/mp4'}));
                  link.download = 'slow-fixture.mp4';
                  document.body.appendChild(link);
                  link.click();
                  link.remove();
                }, 350);
              };
            }
            """
        )
        item = QueueItem("Fixture_Scene_01.mp4", "slow_1080p.mp4")
        completed, _, failed = await worker.process_queue([item])
        self.assertEqual((completed, failed), (1, 0))
        self.assertEqual(manifest.get(item).attempts, 1)

    async def test_download_survives_originating_flow_tab_closing(self):
        logger = logging.getLogger("closing-tab-test")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        manifest = ManifestStore(self.directory / "state-closing-tab.json")
        fixture = Path(__file__).with_name("fixture_flow.html").resolve().as_uri()
        worker = FlowWorker(
            context=self.context,
            project_url=fixture,
            output_dir=self.directory / "output-closing-tab",
            manifest=manifest,
            logger=logger,
            inactivity_timeout_seconds=2,
            max_retries=0,
            poll_seconds=0.05,
            download_stage_dir=self.download_stage,
            intercept_blob_downloads=False,
        )
        await worker.initialize()

        async def close_originating_tab(_download):
            # Let the menu click resolve, then close while save_as is active.
            await asyncio.sleep(0.05)
            await worker.page.close()

        worker.page.on(
            "download", lambda download: asyncio.create_task(close_originating_tab(download))
        )
        item = QueueItem("Fixture_Scene_01.mp4", "closing_tab_1080p.mp4")
        completed, _, failed = await worker.process_queue([item])
        self.assertEqual((completed, failed), (1, 0))
        self.assertTrue(is_valid_mp4(worker.output_dir / item.output_filename))


if __name__ == "__main__":
    unittest.main()

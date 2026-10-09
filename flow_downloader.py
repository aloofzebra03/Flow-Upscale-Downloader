#!/usr/bin/env python3
"""Sequential, resumable Google Flow video and image downloader."""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from playwright.async_api import (
        BrowserContext,
        Download,
        Error as PlaywrightError,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError:  # Let pure unit tests import this module without Playwright.
    BrowserContext = Download = Locator = Page = Any  # type: ignore[assignment]
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


DEFAULT_PROJECT_URL = (
    "https://labs.google/fx/tools/flow/project/"
    "b91ca69e-1ae6-4fa3-b405-09713711c331"
)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "Flow Downloads"
DEFAULT_PROFILE_DIR = SCRIPT_DIR / ".chrome-profile"
DEFAULT_MANIFEST = SCRIPT_DIR / ".flow-downloader-state.json"
DEFAULT_LOG_DIR = SCRIPT_DIR / "logs"

ACTIVE_UPSCALE_TEXT = "Upscaling your video"
ACTIVE_IMAGE_UPSCALE_TEXTS = ("Upscaling your image", "Upscaling image")
COMPLETE_UPSCALE_TEXT = "Upscaling complete"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
FAILED_UPSCALE_PATTERNS = (
    "upscaling failed",
    "couldn't upscale",
    "could not upscale",
    "something went wrong",
)

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class QueueValidationError(ValueError):
    """The queue cannot be processed safely."""


class FlowAutomationError(RuntimeError):
    """A recoverable Flow UI or download error."""


class InactivityTimeout(FlowAutomationError):
    """No active state, completion state, or download was observed in time."""


class ManualAttentionRequired(FlowAutomationError):
    """The user must interact with the browser before automation can continue."""


class BrowserRestartRequired(FlowAutomationError):
    """A completed item closed Chrome, so processing must resume in a new context."""

    def __init__(self, message: str, flow_name: str | None = None) -> None:
        super().__init__(message)
        self.flow_name = flow_name


@dataclass(frozen=True)
class QueueItem:
    flow_name: str
    output_filename: str


@dataclass(frozen=True)
class CapturedBlobDownload:
    frame: Any
    filename: str
    size: int
    mime_type: str = ""


@dataclass
class ItemState:
    flow_name: str
    output_filename: str
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    output_path: str | None = None
    updated_at: str | None = None


@dataclass
class BrowserRuntime:
    playwright: Any
    context: BrowserContext
    download_stage: Path
    attached_browser: Any | None = None
    chrome_process: subprocess.Popen[Any] | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_output_filename(name: str, media_type: str = "videos") -> None:
    if not name or name != name.strip():
        raise QueueValidationError("Output filenames cannot be blank or padded with spaces")
    if Path(name).name != name or INVALID_FILENAME_CHARS.search(name):
        raise QueueValidationError(f"Unsafe output filename: {name!r}")
    suffix = Path(name).suffix.lower()
    if media_type == "videos" and suffix != ".mp4":
        raise QueueValidationError(f"Output filename must end in .mp4: {name!r}")
    if media_type == "images" and suffix not in IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(IMAGE_EXTENSIONS))
        raise QueueValidationError(
            f"Image output filename must end in one of {allowed}: {name!r}"
        )
    stem = Path(name).stem.rstrip(" .").upper()
    if stem in WINDOWS_RESERVED_NAMES or name.endswith((" ", ".")):
        raise QueueValidationError(f"Invalid Windows output filename: {name!r}")


def load_queue(path: Path, media_type: str = "videos") -> list[QueueItem]:
    if not path.is_file():
        raise QueueValidationError(f"Queue file not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"flow_name", "output_filename"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise QueueValidationError(
                "Queue CSV must contain flow_name and output_filename columns"
            )

        items: list[QueueItem] = []
        for line_number, row in enumerate(reader, start=2):
            flow_name = (row.get("flow_name") or "").strip()
            output_filename = row.get("output_filename") or ""
            if not flow_name:
                raise QueueValidationError(f"Blank flow_name on CSV line {line_number}")
            try:
                validate_output_filename(output_filename, media_type)
            except QueueValidationError as exc:
                raise QueueValidationError(f"CSV line {line_number}: {exc}") from exc
            items.append(QueueItem(flow_name, output_filename))

    if not items:
        raise QueueValidationError("Queue CSV contains no items")

    flow_names: set[str] = set()
    output_names: set[str] = set()
    for item in items:
        flow_key = item.flow_name.casefold()
        output_key = item.output_filename.casefold()
        if flow_key in flow_names:
            raise QueueValidationError(f"Duplicate Flow name: {item.flow_name!r}")
        if output_key in output_names:
            raise QueueValidationError(
                f"Duplicate output filename: {item.output_filename!r}"
            )
        flow_names.add(flow_key)
        output_names.add(output_key)
    return items


def is_valid_mp4(path: Path) -> bool:
    """Perform a cheap ISO Base Media signature check without requiring ffmpeg."""
    try:
        if not path.is_file() or path.stat().st_size < 12:
            return False
        with path.open("rb") as handle:
            return b"ftyp" in handle.read(64)
    except OSError:
        return False


def detect_image_format(path: Path) -> str | None:
    """Return png/jpeg/webp from file bytes, independent of the filename."""
    try:
        if not path.is_file() or path.stat().st_size < 12:
            return None
        with path.open("rb") as handle:
            header = handle.read(16)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if header.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "webp"
        return None
    except OSError:
        return None


def is_valid_image(path: Path) -> bool:
    """Validate image bytes and ensure they match the destination extension."""
    actual = detect_image_format(path)
    expected = {
        ".png": "png",
        ".jpg": "jpeg",
        ".jpeg": "jpeg",
        ".webp": "webp",
    }.get(path.suffix.lower())
    return actual is not None and actual == expected


class ManifestStore:
    """Crash-safe JSON state, atomically replaced after every transition."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.states: dict[str, ItemState] = {}
        self._load()

    @staticmethod
    def _key(flow_name: str) -> str:
        return flow_name.casefold()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or not isinstance(raw.get("items"), dict):
                raise ValueError("unsupported manifest structure")
            for key, value in raw["items"].items():
                self.states[key] = ItemState(**value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise QueueValidationError(f"Cannot read manifest {self.path}: {exc}") from exc

    def get(self, item: QueueItem) -> ItemState:
        key = self._key(item.flow_name)
        state = self.states.get(key)
        if state is None or state.output_filename != item.output_filename:
            state = ItemState(item.flow_name, item.output_filename)
            self.states[key] = state
        return state

    def update(self, item: QueueItem, **changes: Any) -> ItemState:
        state = self.get(item)
        for key, value in changes.items():
            if not hasattr(state, key):
                raise AttributeError(f"Unknown state field: {key}")
            setattr(state, key, value)
        state.updated_at = utc_now()
        self.save()
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": utc_now(),
            "items": {key: asdict(value) for key, value in self.states.items()},
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


def find_chrome(explicit_path: Path | None = None) -> Path:
    candidates: Iterable[Path]
    if explicit_path is not None:
        candidates = (explicit_path,)
    else:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = (
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            local_app_data / "Google/Chrome/Application/chrome.exe",
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "Google Chrome was not found. Pass its executable with --chrome-path."
    )


def build_manual_login_command(
    chrome_path: Path, profile_dir: Path, project_url: str
) -> list[str]:
    """Build a normal Chrome launch with no Playwright/automation flags."""
    return [
        str(chrome_path),
        f"--user-data-dir={profile_dir.resolve()}",
        "--start-maximized",
        "--no-first-run",
        project_url,
    ]


def build_ordinary_chrome_command(
    chrome_path: Path, profile_dir: Path, project_url: str, debugging_port: int
) -> list[str]:
    """Launch normal Chrome for a localhost-only Playwright CDP attachment."""
    return [
        str(chrome_path),
        f"--user-data-dir={profile_dir.resolve()}",
        f"--remote-debugging-port={debugging_port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--start-maximized",
        "--no-first-run",
        project_url,
    ]


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"flow-downloader-{timestamp}.log"
    logger = logging.getLogger("flow_downloader")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.info("Log file: %s", log_path)
    return logger


def safe_diagnostic_stem(flow_name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", flow_name).strip("._")
    return (stem or "unknown")[:80]


class FlowWorker:
    def __init__(
        self,
        context: BrowserContext,
        project_url: str,
        output_dir: Path,
        manifest: ManifestStore,
        logger: logging.Logger,
        inactivity_timeout_seconds: float = 300,
        max_retries: int = 2,
        poll_seconds: float = 1,
        diagnostic_dir: Path | None = None,
        download_stage_dir: Path | None = None,
        intercept_blob_downloads: bool = True,
        media_type: str = "videos",
    ) -> None:
        self.context = context
        self.project_url = project_url
        self.output_dir = output_dir
        self.manifest = manifest
        self.logger = logger
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self.max_retries = max_retries
        self.poll_seconds = poll_seconds
        self.diagnostic_dir = diagnostic_dir or (DEFAULT_LOG_DIR / "diagnostics")
        self.download_stage_dir = download_stage_dir
        self.intercept_blob_downloads = intercept_blob_downloads
        if media_type not in {"videos", "images"}:
            raise ValueError(f"Unsupported media type: {media_type}")
        self.media_type = media_type
        self.download_queue: asyncio.Queue[Download] = asyncio.Queue()
        self.page: Page | None = None
        self.guard_page: Page | None = None
        self.download_cdp_session: Any | None = None

    async def initialize(self) -> Page:
        if self.intercept_blob_downloads:
            await self.context.add_init_script(script=self._blob_capture_init_script())
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        self.guard_page = None
        self._attach_work_page_events(self.page)
        await self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=60_000)
        await self.page.bring_to_front()
        await self._wait_for_manual_auth_if_needed()
        await self._select_media_view()
        return self.page

    @staticmethod
    def _blob_capture_init_script() -> str:
        return r"""
        (() => {
          if (window.__flowDownloaderBlobHookInstalled) return;
          window.__flowDownloaderBlobHookInstalled = true;
          window.__flowDownloaderCapturedBlob = null;
          const blobs = new Map();
          const originalCreateObjectURL = URL.createObjectURL.bind(URL);
          URL.createObjectURL = function(object) {
            const url = originalCreateObjectURL(object);
            if (object instanceof Blob) blobs.set(url, object);
            return url;
          };

          const capture = anchor => {
            if (!anchor) return false;
            const href = anchor.href || '';
            const blob = blobs.get(href);
            if (!blob) return false;
            const filename = anchor.download || '';
            const isSupported = /\.(mp4|png|jpe?g|webp)$/i.test(filename) ||
              /^(video\/mp4|image\/(png|jpe?g|webp))$/i.test(blob.type || '');
            if (!isSupported) return false;
            window.__flowDownloaderCapturedBlob = {blob, filename, href};
            return true;
          };

          const originalClick = HTMLAnchorElement.prototype.click;
          HTMLAnchorElement.prototype.click = function(...args) {
            if (capture(this)) return;
            return originalClick.apply(this, args);
          };
          document.addEventListener('click', event => {
            const anchor = event.target && event.target.closest
              ? event.target.closest('a') : null;
            if (capture(anchor)) {
              event.preventDefault();
              event.stopImmediatePropagation();
            }
          }, true);
        })();
        """

    async def _enable_native_chrome_downloads(self) -> None:
        """Keep completed files outside Playwright's disposable artifacts."""
        if self.download_stage_dir is None or self.guard_page is None:
            return
        self.download_stage_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.download_cdp_session = await self.context.new_cdp_session(
                self.guard_page
            )
            await self.download_cdp_session.send(
                "Browser.setDownloadBehavior",
                {
                    # `allow` preserves Chrome's suggested filename. Playwright
                    # normally uses `allowAndName` and owns/deletes the GUID
                    # artifact when its context closes.
                    "behavior": "allow",
                    "downloadPath": str(self.download_stage_dir.resolve()),
                    "eventsEnabled": True,
                },
            )
            self.logger.info("Enabled crash-resistant Chrome download staging")
        except PlaywrightError as exc:
            self.logger.warning(
                "Could not enable Chrome-native download staging; using Playwright: %s",
                exc,
            )

    def _attach_work_page_events(self, page: Page) -> None:
        page.on("download", self._on_download)
        page.on(
            "crash",
            lambda: self.logger.error(
                "Chrome reports that the Flow page renderer crashed"
            ),
        )
        page.on(
            "close",
            lambda: self.logger.debug("Flow work tab closed"),
        )

    def _on_download(self, download: Download) -> None:
        self.download_queue.put_nowait(download)

    async def _wait_for_manual_auth_if_needed(self) -> None:
        assert self.page is not None
        page = self.page
        await page.wait_for_timeout(1_500)
        login_url = "accounts.google.com" in page.url or "/signin" in page.url
        auth_texts = (
            "Sign in",
            "Couldn't sign you in",
            "Couldn’t sign you in",
            "This browser or app may not be secure",
            "Verify it’s you",
        )
        auth_text_visible = any([await self._visible_text(text) for text in auth_texts])
        if not login_url and not auth_text_visible:
            return
        raise ManualAttentionRequired(
            "Google authentication is required. Close this Chrome window, run "
            "flow_downloader.py --login-only, sign in there, close that ordinary "
            "Chrome window, and then restart the queue."
        )

    async def _select_media_view(self) -> None:
        assert self.page is not None
        page = self.page
        view_name = "Videos" if self.media_type == "videos" else "Images"
        # Flow can take a while to mount its navigation after the initial page
        # load. Scanning All Media instead of Videos makes clip discovery both
        # slower and unreliable, so do not silently continue in the wrong view.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            candidates = (
                page.get_by_role("button", name=re.compile(rf"^{view_name}$", re.I)),
                page.get_by_role("link", name=re.compile(rf"^{view_name}$", re.I)),
                page.get_by_text(re.compile(rf"^\s*{view_name}\s*$", re.I)),
                page.locator(f"text={view_name}"),
            )
            for locator in candidates:
                visible = await self._first_visible(locator)
                if visible is None:
                    continue
                try:
                    await visible.click(timeout=3_000, force=True)
                    await page.wait_for_timeout(1_250)
                    return
                except PlaywrightError:
                    continue
            await page.wait_for_timeout(500)
        raise FlowAutomationError(
            f"Flow's {view_name} navigation did not appear within 60 seconds. "
            "The page may still be loading, signed out, or awaiting manual attention."
        )

    async def _select_videos_view(self) -> None:
        """Backward-compatible wrapper used by older integrations/tests."""
        await self._select_media_view()

    def _is_valid_output(self, path: Path) -> bool:
        return is_valid_mp4(path) if self.media_type == "videos" else is_valid_image(path)

    async def process_queue(
        self,
        items: list[QueueItem],
        exhausted_flow_names: set[str] | None = None,
    ) -> tuple[int, int, int]:
        completed = skipped = failed = 0
        exhausted = exhausted_flow_names or set()
        for index, item in enumerate(items, start=1):
            output_path = self.output_dir / item.output_filename
            state = self.manifest.get(item)
            if item.flow_name.casefold() in exhausted:
                self.logger.error(
                    "[%d/%d] Skipping failed %s after Chrome closed on all attempts",
                    index,
                    len(items),
                    item.flow_name,
                )
                failed += 1
                continue
            if self._is_valid_output(output_path):
                if state.status != "completed":
                    self.manifest.update(
                        item,
                        status="completed",
                        last_error=None,
                        output_path=str(output_path.resolve()),
                    )
                    self.logger.info(
                        "[%d/%d] Adopted existing valid %s for %s",
                        index,
                        len(items),
                        "MP4" if self.media_type == "videos" else "image",
                        item.flow_name,
                    )
                else:
                    self.logger.info(
                        "[%d/%d] Skipping completed %s",
                        index,
                        len(items),
                        item.flow_name,
                    )
                skipped += 1
                continue
            if state.status == "completed":
                self.logger.warning(
                    "Recorded output for %s is missing or invalid; downloading again",
                    item.flow_name,
                )
            if output_path.exists():
                message = "Destination already exists and will not be overwritten"
                self.logger.error("[%d/%d] %s: %s", index, len(items), item.flow_name, message)
                self.manifest.update(item, status="failed", last_error=message)
                failed += 1
                continue

            self.logger.info("[%d/%d] Processing %s", index, len(items), item.flow_name)
            if await self._process_item_with_retries(item, output_path):
                completed += 1
                if self.page is not None and self.page.is_closed():
                    try:
                        await self._restore_project_view()
                    except PlaywrightError as exc:
                        raise BrowserRestartRequired(
                            "Chrome closed after the completed download; "
                            "relaunching for the next item"
                        ) from exc
            else:
                failed += 1
        return completed, skipped, failed

    async def _process_item_with_retries(self, item: QueueItem, output_path: Path) -> bool:
        assert self.page is not None
        last_error = "Unknown error"
        max_attempts = 1 + self.max_retries
        for attempt in range(1, max_attempts + 1):
            self.manifest.update(
                item,
                status="in_progress",
                attempts=attempt,
                last_error=None,
                output_path=str(output_path.resolve()),
            )
            try:
                await self._clear_download_events()
                if await self._active_upscale_visible():
                    self.logger.info(
                        "%s: an upscale is already active; waiting without clicking again",
                        item.flow_name,
                    )
                else:
                    if self.media_type == "videos":
                        card = await self._find_video_card(item.flow_name)
                        await self._request_1080p_download(card)
                    else:
                        card = await self._find_image_card(item.flow_name)
                        await self._request_image_download(card)
                download = await self._wait_for_download(item.flow_name)
                await self._save_download(download, output_path)
                self.manifest.update(
                    item,
                    status="completed",
                    last_error=None,
                    output_path=str(output_path.resolve()),
                )
                self.logger.info("Completed: %s -> %s", item.flow_name, output_path)
                return True
            except ManualAttentionRequired as exc:
                self.manifest.update(item, status="paused", last_error=str(exc))
                raise
            except (FlowAutomationError, PlaywrightError, OSError) as exc:
                last_error = str(exc)
                self.logger.error(
                    "%s attempt %d/%d failed: %s",
                    item.flow_name,
                    attempt,
                    max_attempts,
                    last_error,
                )
                page_closed = (
                    self.page is not None
                    and hasattr(self.page, "is_closed")
                    and self.page.is_closed()
                )
                if page_closed:
                    try:
                        await self._restore_project_view()
                    except PlaywrightError as restore_exc:
                        message = (
                            "Chrome closed while processing the download; reopening it and "
                            "retrying from the manifest"
                        )
                        self.manifest.update(item, status="in_progress", last_error=message)
                        raise BrowserRestartRequired(message, item.flow_name) from restore_exc
                    if attempt < max_attempts:
                        continue
                await self._capture_diagnostic(item.flow_name, attempt)
                if attempt < max_attempts:
                    try:
                        await self._reload_for_retry()
                    except PlaywrightError as reload_exc:
                        message = (
                            "Chrome closed while restoring the Flow project. Restart "
                            "the same command; completed files will be skipped."
                        )
                        self.manifest.update(item, status="paused", last_error=message)
                        raise ManualAttentionRequired(message) from reload_exc
        self.manifest.update(item, status="failed", last_error=last_error)
        # Do not let an open tile menu or video editor leak into the next queue
        # item after this item exhausts all retries.
        try:
            await self._restore_project_view()
        except Exception as exc:  # Cleanup must not hide the recorded item failure.
            self.logger.warning("Could not restore the project after failure: %s", exc)
        return False

    async def _find_video_card(self, flow_name: str) -> Locator:
        return await self._find_media_card(flow_name)

    async def _find_image_card(self, flow_name: str) -> Locator:
        return await self._find_media_card(flow_name)

    async def _find_media_card(self, flow_name: str) -> Locator:
        assert self.page is not None
        page = self.page
        discovered: list[str] = []
        discovered_keys: set[str] = set()

        # Flow's media toolbar provides a server-side search that can find a
        # named clip without loading every earlier card in the long feed.
        search = await self._media_search_input()
        if search is not None:
            try:
                search_queries = [flow_name]
                suffix = Path(flow_name).suffix.lower()
                supported_suffixes = {".mp4"} if self.media_type == "videos" else IMAGE_EXTENSIONS
                if suffix in supported_suffixes:
                    search_queries.append(flow_name[: -len(suffix)])

                for query in search_queries:
                    await search.fill(query, timeout=5_000)
                    query_deadline = time.monotonic() + 15
                    no_results_since: float | None = None
                    while time.monotonic() < query_deadline:
                        hovered_match = await self._hover_scan_media_cards(
                            flow_name, discovered, discovered_keys
                        )
                        if hovered_match is not None:
                            self.logger.info(
                                "Found %s using Flow search query %r",
                                flow_name,
                                query,
                            )
                            return hovered_match

                        no_results = await self._visible_text("No results")
                        if no_results:
                            no_results_since = no_results_since or time.monotonic()
                            # Do not trust a one-frame empty state while results
                            # for the previous query are being replaced.
                            if time.monotonic() - no_results_since >= 1:
                                break
                        else:
                            no_results_since = None
                        await page.wait_for_timeout(250)
                raise FlowAutomationError(
                    f"Flow search returned no exact {self.media_type[:-1]} match for "
                    f"{flow_name!r} (tried with and without its extension)"
                )
            except FlowAutomationError:
                raise
            except PlaywrightError as exc:
                self.logger.warning(
                    "Flow search could not be used for %s; falling back to scrolling: %s",
                    flow_name,
                    exc,
                )
            try:
                await search.fill("", timeout=3_000)
                await page.wait_for_timeout(750)
            except PlaywrightError:
                pass

        self.logger.info(
            "Searching for %s by scanning the %s list", flow_name, self.media_type.title()
        )
        await self._reset_scrollable_areas()
        search_deadline = time.monotonic() + min(120, self.inactivity_timeout_seconds)
        pass_number = 1
        bottom_rounds = 0

        while time.monotonic() < search_deadline:
            hovered_match = await self._hover_scan_media_cards(
                flow_name, discovered, discovered_keys
            )
            if hovered_match is not None:
                return hovered_match

            before_top, after_top, maximum = await self._advance_scrollable_areas()
            at_bottom = after_top >= maximum - 2
            if at_bottom and after_top == before_top:
                bottom_rounds += 1
            else:
                bottom_rounds = 0

            if bottom_rounds >= 2:
                if pass_number >= 2:
                    break
                # A second deterministic pass catches cards Flow mounted late
                # during the first traversal without jumping between containers.
                pass_number += 1
                await self._reset_scrollable_areas()
                bottom_rounds = 0
                await page.wait_for_timeout(1_000)
                continue
            await page.wait_for_timeout(350)
        names_hint = ", ".join(discovered[:20]) or "none"
        raise FlowAutomationError(
            f"Could not find Flow {self.media_type[:-1]} named {flow_name!r}. "
            f"Names seen while hover-scanning: {names_hint}"
        )

    async def _media_search_input(self) -> Locator | None:
        """Return Flow's visible media search input, if this UI exposes it."""
        assert self.page is not None
        candidates = (
            self.page.get_by_role(
                "textbox", name=re.compile(r"^\s*Search\s*$", re.I)
            ),
            self.page.locator('input[aria-label="Search"]'),
            self.page.locator('input[type="search"]'),
        )
        for locator in candidates:
            visible = await self._first_visible(locator)
            if visible is not None:
                return visible
        return None

    async def discover_video_names(self) -> list[str]:
        """Backward-compatible name for discovering the selected media type."""
        return await self.discover_media_names()

    async def discover_media_names(self) -> list[str]:
        """Hover-scan the selected media view and return names Flow reveals."""
        assert self.page is not None
        await self._reset_scrollable_areas()
        discovered: list[str] = []
        discovered_keys: set[str] = set()
        deadline = time.monotonic() + min(120, self.inactivity_timeout_seconds)
        pass_number = 1
        bottom_rounds = 0

        while time.monotonic() < deadline:
            await self._hover_scan_media_cards(None, discovered, discovered_keys)
            before_top, after_top, maximum = await self._advance_scrollable_areas()
            at_bottom = after_top >= maximum - 2
            if at_bottom and after_top == before_top:
                bottom_rounds += 1
            else:
                bottom_rounds = 0
            if bottom_rounds >= 2:
                if pass_number >= 2:
                    break
                pass_number += 1
                await self._reset_scrollable_areas()
                bottom_rounds = 0
                await self.page.wait_for_timeout(1_000)
                continue
            await self.page.wait_for_timeout(350)
        return discovered

    async def _hover_scan_media_cards(
        self,
        target_name: str | None,
        discovered: list[str],
        discovered_keys: set[str],
    ) -> Locator | None:
        """Reveal names by hovering preview media already in the viewport.

        Never call scroll_into_view_if_needed here. Flow uses an Angular virtual
        scroller, and asking individual images to scroll into view can jump over
        entire batches of cards while the DOM is being recycled.
        """
        assert self.page is not None
        page = self.page
        candidates = page.locator("img, video")
        # Ask the page for visible preview indices in one DOM operation. Calling
        # bounding_box() separately for every previously mounted off-screen
        # video makes a long Flow project progressively slower (quadratic work).
        visible_candidates = await candidates.evaluate_all(
            """
            elements => elements.map((element, index) => {
              const box = element.getBoundingClientRect();
              return {
                index,
                x: box.x,
                y: box.y,
                width: box.width,
                height: box.height,
              };
            }).filter(box => {
              const aspect = box.height ? box.width / box.height : 0;
              const mediaType = %s;
              const largeEnough = mediaType === 'videos'
                ? box.width >= 160 && box.height >= 85 && aspect >= 1.35
                : box.width >= 100 && box.height >= 100;
              return largeEnough &&
                box.x + box.width > 0 && box.y + box.height > 0 &&
                box.x < window.innerWidth && box.y < window.innerHeight;
            })
            """ % json.dumps(self.media_type)
        )
        viewport = await page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight})"
        )
        for box in visible_candidates:
            candidate = candidates.nth(box["index"])
            try:
                width = box["width"]
                height = box["height"]
                # Locator.hover() helpfully scrolls an element to its center,
                # which is harmful inside Flow's recycled virtual list. Move
                # the pointer to a point from the element's *visible* bounds
                # instead; these are live DOM-derived coordinates, never fixed
                # screen positions.
                hover_x = max(1, min(viewport["width"] - 1, box["x"] + width / 2))
                visible_top = max(0, box["y"])
                visible_bottom = min(viewport["height"], box["y"] + height)
                hover_y = max(1, min(viewport["height"] - 1, (visible_top + visible_bottom) / 2))
                await page.mouse.move(hover_x, hover_y)
                await page.wait_for_timeout(100)
            except PlaywrightError:
                continue

            for name in await self._current_media_names():
                key = name.casefold()
                if key not in discovered_keys:
                    discovered_keys.add(key)
                    discovered.append(name)

            if target_name is not None:
                target_labels = [target_name]
                suffix = Path(target_name).suffix.lower()
                supported_suffixes = {".mp4"} if self.media_type == "videos" else IMAGE_EXTENSIONS
                if suffix in supported_suffixes:
                    target_labels.append(target_name[: -len(suffix)])
                visible_title: Locator | None = None
                for label in target_labels:
                    visible_title = await self._first_visible(
                        page.get_by_text(label, exact=True)
                    )
                    if visible_title is not None:
                        break
                if visible_title is not None:
                    # Prefer Flow's own tile boundary. A generic ancestor that
                    # merely contains a menu button can expand to the page and
                    # accidentally include the global "More options" button.
                    card = visible_title.locator(
                        "xpath=ancestor::*[self::flow-video-tile or self::flow-image-tile "
                        "or self::article "
                        "or @role='listitem'][1]"
                    ).first
                    if not await card.count():
                        card = candidate.locator(
                            "xpath=ancestor::*[self::flow-video-tile or self::flow-image-tile or "
                            "self::flow-tile-container or "
                            "self::flow-grid-tile-container][1]"
                        ).first
                    if not await card.count():
                        card = await self._resolve_card(visible_title)
                    return card
        return None

    async def _current_media_names(self) -> list[str]:
        assert self.page is not None
        extension_pattern = r"mp4" if self.media_type == "videos" else r"png|jpe?g|webp"
        locator = self.page.get_by_text(
            re.compile(rf"^[^\r\n]+\.(?:{extension_pattern})$", re.I)
        )
        values: list[str] = []
        for value in await locator.all_inner_texts():
            normalized = " ".join(value.split())
            suffix = Path(normalized).suffix.lower()
            supported = {".mp4"} if self.media_type == "videos" else IMAGE_EXTENSIONS
            if suffix in supported and len(normalized) <= 255:
                values.append(normalized)
        return values

    async def _current_mp4_names(self) -> list[str]:
        """Backward-compatible wrapper for older callers."""
        return await self._current_media_names()

    async def _resolve_card(self, title: Locator) -> Locator:
        semantic = title.locator(
            "xpath=ancestor::*[self::flow-video-tile or self::flow-image-tile or "
            "self::article or "
            "@role='listitem' or @data-testid][1]"
        )
        if await semantic.count():
            return semantic.first

        current = title
        best = title.locator("xpath=..").first
        for _ in range(8):
            current = current.locator("xpath=..").first
            if not await current.count():
                break
            box = await current.bounding_box()
            if box and box["width"] >= 250 and box["height"] >= 120:
                best = current
                if await current.locator("button").count() >= 2:
                    return current
        return best

    async def _request_1080p_download(self, card: Locator) -> None:
        assert self.page is not None
        page = self.page
        download_row: Locator | None = None
        main_menu: Locator | None = None
        last_click_error: Exception | None = None
        for click_attempt in range(4):
            await card.hover(timeout=10_000)
            await page.wait_for_timeout(500)
            menu_button = card.locator(
                "button[aria-haspopup='menu'][aria-label='More options']"
            )
            visible_menu_button = await self._first_visible(menu_button)
            if visible_menu_button is None:
                menu_button = card.get_by_role(
                    "button", name=re.compile(r"^\s*(more options|actions)\s*$", re.I)
                )
                visible_menu_button = await self._first_visible(menu_button)
            if visible_menu_button is None:
                raise FlowAutomationError(
                    "The selected video tile has no visible 'More options' button"
                )
            try:
                # Wait for the hover toolbar to settle. A forced click can land
                # on a neighboring control while Flow animates the overlay.
                await visible_menu_button.click(timeout=5_000)
                open_menus = page.locator("[role='menu']:visible")
                await self._wait_for_locator_count(open_menus, minimum=1, timeout_ms=3_000)
                # Select the tile menu containing the exact label "Download".
                # This excludes Flow's global menu item "Download project".
                for menu_index in range(await open_menus.count()):
                    menu = open_menus.nth(menu_index)
                    exact_download = await self._first_visible(
                        menu.get_by_text("Download", exact=True)
                    )
                    if exact_download is not None:
                        main_menu = menu
                        download_row = exact_download.locator(
                            "xpath=ancestor-or-self::*[self::button or "
                            "@role='menuitem'][1]"
                        ).first
                        break
                if download_row is None:
                    raise FlowAutomationError(
                        "The clip action menu with an exact Download item was not found"
                    )
                break
            except (PlaywrightTimeoutError, FlowAutomationError) as exc:
                last_click_error = exc
                await page.wait_for_timeout(300)

        if download_row is None:
            raise FlowAutomationError(
                f"Could not open the selected video's menu: {last_click_error}"
            )
        assert main_menu is not None
        # Do not force this hover: Flow animates its menu rows, and force=True
        # previously allowed the pointer to land on the adjacent Add to prompt
        # row. Playwright now waits until the exact Download row is stable.
        await download_row.hover(timeout=7_500)
        open_menus = page.locator("[role='menu']:visible")
        try:
            await self._wait_for_locator_count(open_menus, minimum=2, timeout_ms=5_000)
        except FlowAutomationError as exc:
            # In some Flow builds, clicking the parent Download row does not
            # open the resolution submenu. It starts a generic project download
            # and can navigate into the video editor. Never use that click as a
            # fallback; let the item-level retry reload the project safely.
            raise FlowAutomationError(
                "The resolution submenu did not open after hovering Download; "
                "refusing to click the parent Download action"
            ) from exc
        visible_resolution: Locator | None = None
        for menu_index in range(await open_menus.count()):
            visible_resolution = await self._first_visible(
                open_menus.nth(menu_index).get_by_text("1080p", exact=True)
            )
            if visible_resolution is not None:
                break
        if visible_resolution is None:
            raise FlowAutomationError(
                "The opened resolution submenu did not contain an exact 1080p option"
            )
        target = visible_resolution.locator(
            "xpath=ancestor-or-self::*[self::button or @role='menuitem'][1]"
        )
        if not await target.count() or not await target.first.is_visible():
            raise FlowAutomationError(
                "The 1080p label was visible, but its clickable menu item was not found"
            )
        target_text = " ".join((await target.first.inner_text()).split())
        if not re.search(r"\b1080p\b", target_text, re.I) or not re.search(
            r"\bUpscaled\b", target_text, re.I
        ):
            raise FlowAutomationError(
                f"Refusing to click resolution option {target_text!r}; "
                "expected the same menu item to contain both '1080p' and 'Upscaled'"
            )
        await target.first.click(timeout=10_000)
        self.logger.info("Requested Download -> 1080p -> Upscaled")

    async def _request_image_download(self, card: Locator) -> None:
        """Request Flow's exact 2K Upscaled image option."""
        assert self.page is not None
        page = self.page
        download_row: Locator | None = None
        last_click_error: Exception | None = None

        for _ in range(4):
            await card.hover(timeout=10_000)
            await page.wait_for_timeout(500)
            menu_button = card.locator(
                "button[aria-haspopup='menu'][aria-label='More options']"
            )
            visible_menu_button = await self._first_visible(menu_button)
            if visible_menu_button is None:
                menu_button = card.get_by_role(
                    "button", name=re.compile(r"^\s*(more options|actions)\s*$", re.I)
                )
                visible_menu_button = await self._first_visible(menu_button)
            if visible_menu_button is None:
                raise FlowAutomationError(
                    "The selected image tile has no visible 'More options' button"
                )
            try:
                await visible_menu_button.click(timeout=5_000)
                open_menus = page.locator("[role='menu']:visible")
                await self._wait_for_locator_count(open_menus, minimum=1, timeout_ms=3_000)
                for menu_index in range(await open_menus.count()):
                    exact_download = await self._first_visible(
                        open_menus.nth(menu_index).get_by_text("Download", exact=True)
                    )
                    if exact_download is not None:
                        download_row = exact_download.locator(
                            "xpath=ancestor-or-self::*[self::button or "
                            "@role='menuitem'][1]"
                        ).first
                        break
                if download_row is None:
                    raise FlowAutomationError(
                        "The image action menu with an exact Download item was not found"
                    )
                break
            except (PlaywrightTimeoutError, FlowAutomationError) as exc:
                last_click_error = exc
                await page.wait_for_timeout(300)

        if download_row is None:
            raise FlowAutomationError(
                f"Could not open the selected image's menu: {last_click_error}"
            )

        # Images must use Flow's explicit 2K Upscaled option. Clicking the
        # parent Download row would download the 1K original in some builds.
        open_menus = page.locator("[role='menu']:visible")
        menu_count = await open_menus.count()
        await download_row.hover(timeout=7_500)
        try:
            await self._wait_for_locator_count(
                open_menus, minimum=menu_count + 1, timeout_ms=5_000
            )
        except FlowAutomationError as exc:
            raise FlowAutomationError(
                "The image resolution submenu did not open after hovering Download; "
                "refusing to click the parent Download action"
            ) from exc

        submenu_options: list[str] = []
        for menu_index in range(await open_menus.count()):
            menu_items = open_menus.nth(menu_index).locator(
                "button, [role='menuitem']"
            )
            for item_index in range(await menu_items.count()):
                option = menu_items.nth(item_index)
                if not await option.is_visible():
                    continue
                option_text = " ".join((await option.inner_text()).split())
                if option_text:
                    submenu_options.append(option_text)
                if re.search(r"\b2K\b", option_text, re.I) and re.search(
                    r"\bUpscaled\b", option_text, re.I
                ):
                    await option.click(timeout=10_000)
                    self.logger.info("Requested image Download -> 2K -> Upscaled")
                    return
        options_hint = ", ".join(dict.fromkeys(submenu_options)) or "none"
        raise FlowAutomationError(
            "The image Download submenu opened but no exact 2K Upscaled option was found; "
            f"visible options: {options_hint}"
        )

    async def _wait_for_download(
        self, flow_name: str
    ) -> Download | CapturedBlobDownload:
        assert self.page is not None
        page = self.page
        inactive_since = time.monotonic()
        saw_active = False
        saw_complete = False

        while True:
            captured = await self._find_captured_blob_download()
            if captured is not None:
                self.logger.info(
                    "%s: captured the %s inside Flow before Chrome's download UI (%d bytes)",
                    flow_name,
                    "MP4" if self.media_type == "videos" else "image",
                    captured.size,
                )
                return captured
            try:
                # Wait on the event queue directly so an already-upscaled clip
                # can be saved immediately. Polling with sleep left a short race
                # in which Flow could close its originating page first.
                download = await asyncio.wait_for(
                    self.download_queue.get(),
                    timeout=max(0.05, min(self.poll_seconds, 0.25)),
                )
                self.logger.info(
                    "%s: Chrome accepted the download request; waiting for file bytes",
                    flow_name,
                )
                return download
            except asyncio.TimeoutError:
                pass

            failure = await self._visible_failure_text()
            if failure:
                raise FlowAutomationError(f"Flow reported an error: {failure}")

            active = await self._active_upscale_visible()
            complete = await self._complete_upscale_visible()
            now = time.monotonic()

            if active:
                if not saw_active:
                    self.logger.info("%s: Flow reports active upscaling", flow_name)
                    saw_active = True
                inactive_since = now
            elif complete:
                if not saw_complete:
                    self.logger.info("%s: Flow reports upscaling complete", flow_name)
                    saw_complete = True
                    inactive_since = now
            elif now - inactive_since >= self.inactivity_timeout_seconds:
                raise InactivityTimeout(
                    f"No Flow progress or download for {self.inactivity_timeout_seconds:g} seconds"
                )

            # The queue wait above doubles as the polling interval.

    async def _find_captured_blob_download(self) -> CapturedBlobDownload | None:
        if not self.intercept_blob_downloads or self.page is None:
            return None
        for frame in self.page.frames:
            try:
                metadata = await frame.evaluate(
                    """
                    () => {
                      const value = window.__flowDownloaderCapturedBlob;
                      if (!value || !value.blob) return null;
                      return {
                        filename: value.filename || 'flow-download',
                        size: value.blob.size || 0,
                        type: value.blob.type || ''
                      };
                    }
                    """
                )
            except PlaywrightError:
                continue
            if metadata and int(metadata.get("size", 0)) > 0:
                return CapturedBlobDownload(
                    frame=frame,
                    filename=str(metadata.get("filename") or "flow-download"),
                    size=int(metadata["size"]),
                    mime_type=str(metadata.get("type") or ""),
                )
        return None

    async def _save_download(
        self, download: Download | CapturedBlobDownload, output_path: Path
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(
            f".{output_path.stem}.{uuid.uuid4().hex}.partial{output_path.suffix}"
        )
        recovered_from_stage = False
        try:
            if isinstance(download, CapturedBlobDownload):
                await self._save_captured_blob(download, temporary)
                if self.media_type == "images":
                    self._normalize_image_format(temporary, download.mime_type)
                if not self._is_valid_output(temporary):
                    raise FlowAutomationError(
                        f"Captured Flow blob is empty or is not a valid {self.media_type[:-1]}"
                    )
                if output_path.exists():
                    raise FlowAutomationError(
                        f"Refusing to overwrite existing file: {output_path}"
                    )
                os.replace(temporary, output_path)
                self.logger.info(
                    "Saved Flow's in-page %s without invoking Chrome's download UI",
                    "MP4" if self.media_type == "videos" else "image",
                )
                return
            try:
                await download.save_as(temporary)
            except PlaywrightError:
                # Playwright deliberately invalidates a Download artifact when
                # its browser context disappears. Chrome may nevertheless have
                # finished the file in the explicit launch-time download path.
                recovered_from_stage = await self._recover_staged_download(temporary)
                if not recovered_from_stage:
                    raise FlowAutomationError(
                        "Chrome accepted the download request but produced no completed file; "
                        "the Flow tab/context ended before the file bytes were available"
                    )
            if not recovered_from_stage:
                failure = await download.failure()
                if failure:
                    raise FlowAutomationError(f"Browser download failed: {failure}")
            if self.media_type == "images":
                self._normalize_image_format(temporary)
            if not self._is_valid_output(temporary):
                # With Chrome-native naming Playwright can report success while
                # copying from the obsolete GUID artifact path. Prefer the
                # completed, non-.crdownload native file in that case.
                temporary.unlink(missing_ok=True)
                recovered_from_stage = await self._recover_staged_download(temporary)
                if recovered_from_stage and self.media_type == "images":
                    self._normalize_image_format(temporary)
                if not recovered_from_stage or not self._is_valid_output(temporary):
                    raise FlowAutomationError(
                        f"Downloaded file is empty or is not a valid {self.media_type[:-1]}"
                    )
            if output_path.exists():
                raise FlowAutomationError(f"Refusing to overwrite existing file: {output_path}")
            os.replace(temporary, output_path)
            if recovered_from_stage:
                self.logger.info(
                    "Recovered the completed Chrome download after its Flow tab closed"
                )
        finally:
            temporary.unlink(missing_ok=True)

    def _normalize_image_format(self, path: Path, mime_type: str = "") -> None:
        """Transcode Flow's bytes when its PNG-looking name actually contains JPEG."""
        actual = detect_image_format(path)
        expected = {
            ".png": "png",
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
            ".webp": "webp",
        }.get(path.suffix.lower())
        if actual is None:
            raise FlowAutomationError(
                f"Captured Flow blob is not a supported image (reported MIME: {mime_type or 'unknown'})"
            )
        if actual == expected:
            return
        try:
            from PIL import Image
        except ImportError as exc:
            raise FlowAutomationError(
                f"Flow returned {actual.upper()} bytes for a {path.suffix} output. "
                "Install/update the Micromamba environment so Pillow can convert it."
            ) from exc

        converted = path.with_name(f".{path.name}.{uuid.uuid4().hex}.converted")
        save_format = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}.get(expected or "")
        if save_format is None:
            raise FlowAutomationError(f"Unsupported requested image extension: {path.suffix}")
        try:
            with Image.open(path) as source:
                source.load()
                image = source
                save_options: dict[str, Any] = {}
                if save_format == "JPEG":
                    if source.mode not in {"RGB", "L"}:
                        image = source.convert("RGB")
                    save_options.update(quality=95, subsampling=0)
                elif save_format == "WEBP":
                    save_options.update(lossless=True, quality=100)
                icc_profile = source.info.get("icc_profile")
                if icc_profile:
                    save_options["icc_profile"] = icc_profile
                image.save(converted, format=save_format, **save_options)
                if image is not source:
                    image.close()
            os.replace(converted, path)
        except (OSError, ValueError) as exc:
            raise FlowAutomationError(
                f"Could not convert Flow's {actual.upper()} image to {save_format}: {exc}"
            ) from exc
        finally:
            converted.unlink(missing_ok=True)
        self.logger.info(
            "Converted Flow's actual %s image to %s to honor the queue filename",
            actual.upper(),
            save_format,
        )

    async def _save_captured_blob(
        self, download: CapturedBlobDownload, temporary: Path
    ) -> None:
        chunk_size = 512 * 1024
        written = 0
        with temporary.open("wb") as handle:
            while written < download.size:
                end = min(download.size, written + chunk_size)
                encoded = await download.frame.evaluate(
                    """
                    async ({start, end}) => {
                      const capture = window.__flowDownloaderCapturedBlob;
                      if (!capture || !capture.blob)
                        throw new Error('Captured Flow blob disappeared');
                      const bytes = new Uint8Array(
                        await capture.blob.slice(start, end).arrayBuffer()
                      );
                      let binary = '';
                      const step = 0x8000;
                      for (let index = 0; index < bytes.length; index += step)
                        binary += String.fromCharCode(...bytes.subarray(index, index + step));
                      return btoa(binary);
                    }
                    """,
                    {"start": written, "end": end},
                )
                chunk = base64.b64decode(encoded, validate=True)
                expected = end - written
                if len(chunk) != expected:
                    raise FlowAutomationError(
                        f"Captured blob chunk was truncated ({len(chunk)} of {expected} bytes)"
                    )
                handle.write(chunk)
                written = end
            handle.flush()
            os.fsync(handle.fileno())
        await download.frame.evaluate(
            """
            () => {
              const capture = window.__flowDownloaderCapturedBlob;
              if (capture && capture.href) URL.revokeObjectURL(capture.href);
              window.__flowDownloaderCapturedBlob = null;
            }
            """
        )

    async def _recover_staged_download(self, temporary: Path) -> bool:
        """Move a completed native Chrome artifact out of persistent staging."""
        stage = self.download_stage_dir
        if stage is None:
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                candidates = sorted(
                    (
                        path
                        for path in stage.iterdir()
                        if path.is_file()
                        and not path.name.lower().endswith((".crdownload", ".tmp"))
                    ),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
            except OSError:
                candidates = []
            for candidate in candidates:
                valid_candidate = (
                    is_valid_mp4(candidate)
                    if self.media_type == "videos"
                    else detect_image_format(candidate) is not None
                )
                if not valid_candidate:
                    continue
                try:
                    os.replace(candidate, temporary)
                except OSError:
                    continue
                return True
            await asyncio.sleep(0.1)
        return False

    async def _reload_for_retry(self) -> None:
        await self._restore_project_view()

    async def _restore_project_view(self) -> None:
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
            self._attach_work_page_events(self.page)
        # Always restore the canonical project route. Reloading the current URL
        # can preserve Flow's full-screen video editor after a misnavigation.
        await self.page.goto(
            self.project_url, wait_until="domcontentloaded", timeout=60_000
        )
        await self.page.bring_to_front()
        await self._wait_for_manual_auth_if_needed()
        await self._select_media_view()

    async def _capture_diagnostic(self, flow_name: str, attempt: int) -> None:
        if self.page is None:
            return
        self.diagnostic_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.diagnostic_dir / (
            f"{timestamp}-{safe_diagnostic_stem(flow_name)}-attempt-{attempt}.png"
        )
        try:
            await self.page.screenshot(path=path, full_page=True)
            self.logger.info("Saved diagnostic screenshot: %s", path)
        except Exception as exc:  # Diagnostics must not hide the original error.
            self.logger.warning("Could not save diagnostic screenshot: %s", exc)

    async def _clear_download_events(self) -> None:
        while not self.download_queue.empty():
            try:
                self.download_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _visible_text(self, text: str) -> bool:
        assert self.page is not None
        return await self._first_visible(self.page.get_by_text(text, exact=False)) is not None

    async def _active_upscale_visible(self) -> bool:
        texts = (
            (ACTIVE_UPSCALE_TEXT,)
            if self.media_type == "videos"
            else ACTIVE_IMAGE_UPSCALE_TEXTS
        )
        return any([await self._visible_text(text) for text in texts])

    async def _complete_upscale_visible(self) -> bool:
        return await self._visible_text(COMPLETE_UPSCALE_TEXT)

    async def _visible_failure_text(self) -> str | None:
        assert self.page is not None
        for pattern in FAILED_UPSCALE_PATTERNS:
            locator = self.page.get_by_text(re.compile(re.escape(pattern), re.I))
            visible = await self._first_visible(locator)
            if visible:
                return (await visible.inner_text()).strip()
        return None

    async def _first_visible(self, locator: Locator) -> Locator | None:
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except PlaywrightTimeoutError:
                continue
        return None

    async def _wait_for_first_visible(self, locator: Locator, timeout_ms: int) -> Locator:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            visible = await self._first_visible(locator)
            if visible:
                return visible
            assert self.page is not None
            await self.page.wait_for_timeout(200)
        raise FlowAutomationError("Timed out waiting for a visible 1080p option")

    async def _wait_for_locator_count(
        self, locator: Locator, minimum: int, timeout_ms: int
    ) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await locator.count() >= minimum:
                return
            assert self.page is not None
            await self.page.wait_for_timeout(100)
        raise FlowAutomationError(
            f"Timed out waiting for {minimum} open Flow menu(s)"
        )

    async def _reset_scrollable_areas(self) -> None:
        assert self.page is not None
        await self.page.evaluate(
            """
            () => {
              const preferred = document.querySelector('.cdk-virtual-scrollable.page-container');
              const all = [document.scrollingElement, ...document.querySelectorAll('*')]
                .filter(el => el && el.scrollHeight > el.clientHeight + 100);
              const scroller = preferred && preferred.scrollHeight > preferred.clientHeight + 100
                ? preferred
                : all.sort((a, b) => (b.scrollHeight - b.clientHeight) -
                                     (a.scrollHeight - a.clientHeight))[0];
              if (scroller) {
                scroller.scrollTop = 0;
                scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
              }
            }
            """
        )
        await self.page.wait_for_timeout(500)

    async def _advance_scrollable_areas(self) -> list[int]:
        assert self.page is not None
        return await self.page.evaluate(
            """
            () => {
              const preferred = document.querySelector('.cdk-virtual-scrollable.page-container');
              const all = [document.scrollingElement, ...document.querySelectorAll('*')]
                .filter(el => el && el.scrollHeight > el.clientHeight + 100);
              const scroller = preferred && preferred.scrollHeight > preferred.clientHeight + 100
                ? preferred
                : all.sort((a, b) => (b.scrollHeight - b.clientHeight) -
                                     (a.scrollHeight - a.clientHeight))[0];
              if (!scroller) return [0, 0, 0];

              const before = Math.round(scroller.scrollTop);
              const maximum = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
              // Deliberate overlap prevents a short virtualized row from ever
              // falling completely between two inspected viewports.
              const step = Math.max(120, Math.floor(scroller.clientHeight * 0.6));
              scroller.scrollTop = Math.min(maximum, before + step);
              scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
              return [before, Math.round(scroller.scrollTop), Math.round(maximum)];
            }
            """
        )


async def open_context(
    args: argparse.Namespace, logger: logging.Logger
) -> BrowserRuntime:
    if async_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run: python -m pip install -r requirements.txt"
        )
    chrome_path = find_chrome(Path(args.chrome_path) if args.chrome_path else None)
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    download_stage = (
        args.log_dir / ".download-staging" / f"{os.getpid()}-{uuid.uuid4().hex}"
    ).resolve()
    download_stage.mkdir(parents=True, exist_ok=False)
    playwright = await async_playwright().start()
    if args.ordinary_chrome:
        port = reserve_local_port()
        command = build_ordinary_chrome_command(
            chrome_path, args.profile_dir, args.project_url, port
        )
        chrome_process = subprocess.Popen(command)
        endpoint = f"http://127.0.0.1:{port}"
        browser = None
        deadline = time.monotonic() + 20
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    endpoint, timeout=1_500
                )
                break
            except PlaywrightError as exc:
                last_error = exc
                await asyncio.sleep(0.25)
        if browser is None:
            try:
                if chrome_process.poll() is None:
                    chrome_process.terminate()
            finally:
                await playwright.stop()
                cleanup_download_stage(download_stage, args.log_dir)
            raise FlowAutomationError(
                "Could not attach to ordinary Chrome. Close every Chrome window "
                f"using {args.profile_dir.resolve()} and try again: {last_error}"
            )
        contexts = browser.contexts
        if not contexts:
            await browser.close()
            await playwright.stop()
            cleanup_download_stage(download_stage, args.log_dir)
            raise FlowAutomationError("Ordinary Chrome exposed no browser context")
        context = contexts[0]
        webdriver_value: Any = "unknown"
        try:
            pages = context.pages
            if pages:
                webdriver_value = await pages[0].evaluate("navigator.webdriver")
        except PlaywrightError:
            pass
        logger.info(
            "Attached to ordinary Chrome over localhost (navigator.webdriver=%r)",
            webdriver_value,
        )
        return BrowserRuntime(
            playwright=playwright,
            context=context,
            download_stage=download_stage,
            attached_browser=browser,
            chrome_process=chrome_process,
        )

    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=args.profile_dir,
        executable_path=chrome_path,
        headless=False,
        accept_downloads=True,
        viewport=None,
        args=[
            "--start-maximized",
            # Chrome 152 on Windows can crash in the download-bubble UI when
            # Flow starts a blob: MP4 download. Keep the underlying download
            # mechanism unchanged while suppressing that UI surface.
            "--disable-features=DownloadBubble,DownloadBubbleV2",
        ],
    )
    return BrowserRuntime(playwright, context, download_stage)


def cleanup_download_stage(path: Path, log_dir: Path) -> None:
    """Remove files only from the exact, worker-created staging directory."""
    root = (log_dir / ".download-staging").resolve()
    resolved = path.resolve()
    if resolved.parent != root:
        raise ValueError(f"Refusing to clean unexpected download staging path: {resolved}")
    if resolved.is_dir():
        for child in resolved.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
        try:
            resolved.rmdir()
        except OSError:
            pass


def run_login_only(args: argparse.Namespace, logger: logging.Logger) -> int:
    chrome_path = find_chrome(Path(args.chrome_path) if args.chrome_path else None)
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    command = build_manual_login_command(chrome_path, args.profile_dir, args.project_url)
    subprocess.Popen(command)
    logger.info(
        "Opened ordinary Chrome with dedicated profile: %s", args.profile_dir.resolve()
    )
    input(
        "Sign in to Google, verify the Flow project opens, CLOSE that Chrome window, "
        "then press Enter here... "
    )
    logger.info("Login setup finished. You can now run the queue command.")
    return 0


async def run_worker(args: argparse.Namespace, logger: logging.Logger) -> int:
    manifest = ManifestStore(args.manifest)
    items = None if args.list_names else load_queue(args.queue, args.media_type)
    browser_restarts = 0
    browser_close_attempts: dict[str, int] = {}
    exhausted_flow_names: set[str] = set()
    while True:
        runtime = await open_context(args, logger)
        playwright = runtime.playwright
        context = runtime.context
        download_stage = runtime.download_stage
        try:
            worker = FlowWorker(
                context=context,
                project_url=args.project_url,
                output_dir=args.output_dir,
                manifest=manifest,
                logger=logger,
                inactivity_timeout_seconds=args.inactivity_timeout,
                max_retries=args.max_retries,
                diagnostic_dir=args.log_dir / "diagnostics",
                download_stage_dir=download_stage,
                media_type=args.media_type,
            )
            await worker.initialize()
            if args.list_names:
                names = await worker.discover_media_names()
                if names:
                    print(f"\nFlow {args.media_type[:-1]} names found:")
                    for name in names:
                        print(name)
                    print(f"\nTotal: {len(names)}")
                    return 0
                logger.error(
                    "No named %s cards were discovered in the %s view.",
                    args.media_type[:-1],
                    args.media_type.title(),
                )
                return 1
            assert items is not None
            completed, skipped, failed = await worker.process_queue(
                items, exhausted_flow_names
            )
            logger.info(
                "Run finished: %d completed, %d skipped, %d failed",
                completed,
                skipped,
                failed,
            )
            return 1 if failed else 0
        except BrowserRestartRequired as exc:
            browser_restarts += 1
            if exc.flow_name:
                key = exc.flow_name.casefold()
                close_attempt = browser_close_attempts.get(key, 0) + 1
                browser_close_attempts[key] = close_attempt
                max_attempts = 1 + args.max_retries
                if close_attempt >= max_attempts:
                    exhausted_flow_names.add(key)
                    failed_item = next(
                        item for item in (items or ()) if item.flow_name.casefold() == key
                    )
                    manifest.update(
                        failed_item,
                        status="failed",
                        attempts=max_attempts,
                        last_error=(
                            f"Chrome closed during the download on all {max_attempts} attempts"
                        ),
                    )
                    logger.error(
                        "%s failed after %d Chrome restarts; continuing with the queue",
                        exc.flow_name,
                        max_attempts,
                    )
                else:
                    logger.warning(
                        "%s (browser attempt %d/%d)",
                        exc,
                        close_attempt,
                        max_attempts,
                    )
            else:
                logger.warning("%s", exc)
            if browser_restarts > (len(items or ()) * (1 + args.max_retries)) + 2:
                raise ManualAttentionRequired(
                    "Chrome repeatedly closed and exceeded the safe automatic restart limit"
                ) from exc
            logger.info("Restarting Chrome and resuming from the manifest")
        finally:
            try:
                if runtime.attached_browser is not None:
                    await runtime.attached_browser.close()
                else:
                    await context.close()
            except Exception:
                # The Playwright driver connection may already be gone after a
                # renderer/browser crash or Ctrl+C. Cleanup must stay quiet.
                pass
            await playwright.stop()
            if (
                runtime.chrome_process is not None
                and runtime.chrome_process.poll() is None
            ):
                try:
                    runtime.chrome_process.terminate()
                except OSError:
                    pass
            cleanup_download_stage(download_stage, args.log_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sequentially download named Google Flow videos or images."
    )
    parser.add_argument("--project-url", default=DEFAULT_PROJECT_URL)
    parser.add_argument("--queue", type=Path, default=SCRIPT_DIR / "queue.csv")
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument(
        "--ordinary-chrome",
        action="store_true",
        help=(
            "Launch ordinary Chrome and attach over localhost instead of using "
            "Playwright's automation-marked launch mode."
        ),
    )
    parser.add_argument(
        "--media-type",
        choices=("videos", "images"),
        default="videos",
        help="Media view and queue format to use (default: videos).",
    )
    parser.add_argument(
        "--list-names",
        action="store_true",
        help="Hover-scan the selected media view and print the names Flow exposes.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--chrome-path")
    parser.add_argument("--inactivity-timeout", type=float, default=300)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.inactivity_timeout <= 0:
        print("--inactivity-timeout must be greater than zero", file=sys.stderr)
        return 2
    if args.max_retries < 0:
        print("--max-retries cannot be negative", file=sys.stderr)
        return 2
    logger = setup_logging(args.log_dir, args.verbose)
    try:
        if args.login_only:
            return run_login_only(args, logger)
        return asyncio.run(run_worker(args, logger))
    except (
        QueueValidationError,
        FlowAutomationError,
        FileNotFoundError,
        RuntimeError,
        PlaywrightError,
    ) as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Stopped by user. Completed items remain recorded for the next run.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

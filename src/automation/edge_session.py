from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, sync_playwright


class EdgeSession:
    def __init__(self, executable: str, profile_dir: str, headless: bool = False) -> None:
        self._executable = executable
        self._profile_dir = profile_dir
        self._headless = headless
        self._playwright = None
        self.context: BrowserContext | None = None

    def start(self) -> BrowserContext:
        executable = Path(self._executable)
        if not executable.is_file():
            raise FileNotFoundError(
                f"Edge executable not found: {executable}. Configure edge.executable."
            )
        profile = Path(self._profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            str(profile),
            executable_path=str(executable),
            headless=self._headless,
            viewport={"width": 1280, "height": 800},
        )
        return self.context

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

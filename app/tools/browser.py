"""
browser.py — One shared Playwright browser that all tools use.

Playwright is a free, open-source browser automation library. The browser is
started lazily (the first time a tool needs it) and then reused, so
"open YouTube" followed by "scroll down" works on the same page.

Choose the browser and profile in `.env` (no code changes needed):

    BROWSER=chromium            chromium (default, installed by setup.sh) | chrome | msedge | firefox | webkit
    BROWSER_PROFILE_DIR=        empty = fresh, throw-away browser every run
                                a folder path = a saved profile (logins, cookies, history persist)
    BROWSER_HEADLESS=0          1 = hide the window

Run `python check_browser.py` to test your choice.
"""

import logging
import os
from typing import Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

logger = logging.getLogger(__name__)

# "chrome" and "msedge" use the browser ALREADY installed on your computer
CHROMIUM_FAMILY = {"chromium", "chrome", "msedge"}
SUPPORTED = CHROMIUM_FAMILY | {"firefox", "webkit"}


class BrowserController:
    def __init__(self):
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._alive = False

    async def page(self) -> Page:
        """Return the current page, starting the browser if needed."""
        if not self._alive:
            await self._launch()
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def _launch(self):
        name = os.environ.get("BROWSER", "chromium").strip().lower()
        if name not in SUPPORTED:
            raise ValueError(f"BROWSER='{name}' is not supported. Choose one of: {sorted(SUPPORTED)}")
        profile = os.path.expanduser(os.environ.get("BROWSER_PROFILE_DIR", "").strip())
        headless = os.environ.get("BROWSER_HEADLESS", "0") == "1"
        logger.info(f"[browser] launching {name} (profile={profile or 'fresh'}, headless={headless})")

        self._pw = await async_playwright().start()
        engine = getattr(self._pw, "chromium" if name in CHROMIUM_FAMILY else name)
        options: dict = {"headless": headless}
        if name in {"chrome", "msedge"}:
            options["channel"] = name  # use the real installed browser
        if name in CHROMIUM_FAMILY:
            options["args"] = ["--start-maximized"]
            options["no_viewport"] = True

        try:
            if profile:
                # Saved profile: logins and cookies survive restarts
                self._context = await engine.launch_persistent_context(profile, **options)
            else:
                browser = await engine.launch(**{k: v for k, v in options.items() if k != "no_viewport"})
                self._context = await browser.new_context(no_viewport=name in CHROMIUM_FAMILY)
        except Exception as e:
            await self._pw.stop()
            self._pw = None
            if "Executable doesn't exist" in str(e) or "is not found at" in str(e):
                hint = {"chrome": "chrome", "msedge": "msedge"}.get(name, name)
                raise RuntimeError(
                    f"{name} is not installed for Playwright. Fix: .venv/bin/python -m playwright install {hint}"
                ) from None
            raise

        self._alive = True
        self._page = None
        self._context.on("close", lambda *_: setattr(self, "_alive", False))
        if self._context.browser:
            self._context.browser.on("disconnected", lambda *_: setattr(self, "_alive", False))

    async def close(self):
        try:
            if self._context is not None:
                browser = getattr(self._context, "browser", None)
                try:
                    await self._context.close()
                except Exception:
                    pass
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
        finally:
            self._pw = self._context = self._page = None
            self._alive = False


# One shared instance for the whole app
browser = BrowserController()

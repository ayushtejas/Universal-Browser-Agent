"""Browserbase transport: drives the portal in a cloud Chrome over CDP.

Why deterministic Playwright and not Stagehand: the two form fields have stable
ids (`#ApplicationNumber` / `#PatentNumber`, `#CaptchaText`), so natural-language
`act()` would add LLM latency, cost and nondeterminism for no gain — and it would
burn the Free plan's $5 Model Gateway allowance. Stagehand earns its keep on
pages that change shape; this one hasn't since 2019.

The CAPTCHA is typed by a person into the session's **Live View** — an
interactive view of the real cloud browser. That is the same human-in-the-loop
arrangement as the local operator UI, minus the local browser: the script does
navigation, form fill, result detection, parsing and the Mongo write.

One session is reused across every record in a batch, so the operator keeps a
single Live View tab open and the free tier's browser-minutes aren't spent on
cold starts.
"""

from __future__ import annotations

import os
import time
from typing import Callable

from .config import settings
from .parse import Outcome, ParseResult, parse_response
from .portal import BASE_URL, LOOKUPS

# Session duration must cover a human working through a whole batch, not one
# page load. The project default is 300s, which is far too short for that.
DEFAULT_SESSION_TIMEOUT = 1800


class BrowserbaseUnavailable(RuntimeError):
    pass


class BrowserbaseDriver:
    """Reusable cloud-browser session for a batch of lookups."""

    def __init__(
        self,
        api_key: str | None = None,
        session_timeout: int = DEFAULT_SESSION_TIMEOUT,
        wait_timeout: float | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("BROWSERBASE_API_KEY", "")
        if not self.api_key:
            raise BrowserbaseUnavailable("BROWSERBASE_API_KEY is not set")
        self.session_timeout = session_timeout
        self.wait_timeout = (
            wait_timeout if wait_timeout is not None else settings.captcha_wait_timeout
        )
        self.session_id: str | None = None
        self.live_view_url: str | None = None
        self._playwright = None
        self._browser = None
        self._page = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> BrowserbaseDriver:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> BrowserbaseDriver:
        try:
            from browserbase import Browserbase
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserbaseUnavailable(
                "install the browserbase extra: uv pip install browserbase playwright"
            ) from exc

        bb = Browserbase(api_key=self.api_key)
        # No project_id: the API key resolves the project on its own.
        session = bb.sessions.create(api_timeout=self.session_timeout)
        self.session_id = session.id
        self.live_view_url = bb.sessions.debug(session.id).debugger_fullscreen_url

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(session.connect_url)
        context = self._browser.contexts[0]
        self._page = context.pages[0] if context.pages else context.new_page()
        return self

    def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - teardown must not mask real errors
                    pass
        self._browser = self._playwright = self._page = None

    @property
    def dashboard_url(self) -> str:
        return f"https://www.browserbase.com/sessions/{self.session_id}"

    # -- work --------------------------------------------------------------

    def fetch(
        self,
        lookup_type: str,
        lookup_value: str,
        notify: Callable[[str], None] | None = None,
    ) -> tuple[ParseResult, str]:
        """Load the form, fill the number, wait for a human to clear the CAPTCHA
        and submit, then return the parsed result page.

        Re-fills the number and keeps waiting if the portal rejects the CAPTCHA,
        up to IPR_MAX_CAPTCHA_ATTEMPTS.
        """
        if self._page is None:
            raise BrowserbaseUnavailable("driver not started")
        spec = LOOKUPS[lookup_type]
        page = self._page

        def say(message: str) -> None:
            if notify is not None:
                notify(message)

        for attempt in range(1, settings.max_captcha_attempts + 1):
            page.goto(
                BASE_URL + spec.path, wait_until="domcontentloaded", timeout=60_000
            )
            page.fill(f"#{spec.field}", lookup_value)
            say(
                f"{lookup_value}: number filled — enter the CAPTCHA in Live View and "
                f"click '{spec.submit_value}' (attempt {attempt})"
            )

            parsed, html = self._wait_for_result(page)
            if parsed is None:
                raise TimeoutError(
                    f"no submission within {self.wait_timeout:.0f}s for {lookup_value}"
                )
            if parsed.outcome is not Outcome.CAPTCHA_REJECTED:
                return parsed, html
            say(f"{lookup_value}: portal rejected that CAPTCHA — reloading a fresh one")

        return parsed, html

    def _wait_for_result(self, page) -> tuple[ParseResult | None, str]:
        """Poll until the page stops being the blank form.

        The empty form parses as UNKNOWN with no field pairs, so anything else —
        a result, a not-found notice, or the 'Invalid captcha' re-render — means
        the human has submitted.
        """
        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                html = page.content()
            except Exception:  # noqa: BLE001 - mid-navigation; retry next tick
                continue
            parsed = parse_response(html)
            if parsed.outcome is not Outcome.UNKNOWN or parsed.fields:
                return parsed, html
        return None, ""

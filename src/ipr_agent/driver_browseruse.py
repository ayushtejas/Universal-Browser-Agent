"""browser-use transport: Playwright + LLM vision for automated CAPTCHA solving.

Hybrid approach — best of both worlds:
- Playwright drives the form deterministically (stable element IDs, fast, reliable)
- OpenAI GPT-4o vision reads the CAPTCHA image (the only part that needs an LLM)

This avoids the full browser-use Agent overhead (DOM watchdog timeouts, planning
loops, element index explosion) for a two-field form that hasn't changed since 2019.

One browser instance is reused across every record in a batch.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Callable

from .config import settings
from .parse import Outcome, ParseResult, parse_response
from .portal import BASE_URL, LOOKUPS

logger = logging.getLogger(__name__)


class BrowserUseUnavailable(RuntimeError):
    pass


def _solve_captcha_with_llm(
    image_bytes: bytes,
    *,
    api_key: str,
    model: str = "gpt-4o",
) -> str:
    """Send the CAPTCHA image to GPT-4o vision and get the text back."""
    import openai

    client = openai.OpenAI(api_key=api_key)
    b64 = base64.b64encode(image_bytes).decode("ascii")

    response = client.chat.completions.create(
        model=model,
        max_tokens=50,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert CAPTCHA reader. You will be shown a CAPTCHA "
                    "image from a government website. The image contains exactly 5 or 6 "
                    "alphanumeric characters (letters and digits) with visual noise "
                    "(lines, dots, distortion). Reply with ONLY the characters, "
                    "nothing else — no quotes, no spaces, no explanation."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Read the CAPTCHA characters from this image very carefully. "
                            "The characters are case-sensitive. Pay close attention to:\n"
                            "- Uppercase vs lowercase (e.g. 'R' vs 'r', 'M' vs 'm')\n"
                            "- Similar-looking chars: 0/O/o, 1/l/I, 5/S/s, 8/B, 2/Z\n"
                            "- The exact count — usually 5 or 6 characters\n"
                            "Reply with ONLY the characters."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    )

    answer = response.choices[0].message.content or ""
    # Strip whitespace and any quotes the model might add
    answer = answer.strip().strip("'\"` ")
    logger.info("LLM CAPTCHA answer: %r", answer)
    return answer


class BrowserUseDriver:
    """Playwright browser + GPT-4o vision for CAPTCHA solving."""

    def __init__(
        self,
        openai_api_key: str | None = None,
        model: str = "gpt-4o",
        headless: bool = True,
    ) -> None:
        # Resolve API key: arg → env → .env file
        key = openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            try:
                from dotenv import dotenv_values

                key = dotenv_values(".env").get("OPENAI_API_KEY", "")
            except ImportError:
                pass
        self.openai_api_key = key
        if not self.openai_api_key:
            raise BrowserUseUnavailable("OPENAI_API_KEY is not set")
        self.model = model
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> BrowserUseDriver:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> BrowserUseDriver:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUseUnavailable(
                "install playwright: uv pip install playwright && playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        return self

    def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        self._browser = self._playwright = self._page = None

    # -- work --------------------------------------------------------------

    def fetch(
        self,
        lookup_type: str,
        lookup_value: str,
        notify: Callable[[str], None] | None = None,
    ) -> tuple[ParseResult, str]:
        """Fill the form, solve the CAPTCHA with LLM vision, submit, parse.

        Retries on CAPTCHA rejection up to max_captcha_attempts.
        """
        if self._page is None:
            raise BrowserUseUnavailable("driver not started")

        spec = LOOKUPS[lookup_type]
        page = self._page

        def say(msg: str) -> None:
            if notify is not None:
                notify(msg)

        last_parsed: ParseResult | None = None
        last_html = ""

        for attempt in range(1, settings.max_captcha_attempts + 1):
            say(f"{lookup_value}: attempt {attempt}/{settings.max_captcha_attempts}")

            # 1. Navigate to the form
            page.goto(
                BASE_URL + spec.path,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            # Small pause for the CAPTCHA image to render
            page.wait_for_timeout(1000)

            # 2. Fill the number field
            field_selector = f"#{spec.field}"
            page.fill(field_selector, lookup_value)
            say(f"{lookup_value}: number filled")

            # 3. Grab the CAPTCHA image (id="Captcha" on the portal)
            captcha_img = (
                page.query_selector("img#Captcha")
                or page.query_selector("img[src*='GetCaptchaImage']")
                or page.query_selector("img[src*='Captcha']")
            )

            if captcha_img is None:
                say(f"{lookup_value}: no CAPTCHA image found on page — submitting without")
                captcha_text = ""
            else:
                # Screenshot the captcha image element to get its bytes
                image_bytes = captcha_img.screenshot()
                say(f"{lookup_value}: CAPTCHA image captured ({len(image_bytes)} bytes), asking LLM...")

                # 4. Solve with LLM vision
                captcha_text = _solve_captcha_with_llm(
                    image_bytes,
                    api_key=self.openai_api_key,
                    model=self.model,
                )
                say(f"{lookup_value}: LLM read CAPTCHA as '{captcha_text}'")

            # 5. Fill CAPTCHA answer
            page.fill("#CaptchaText", captcha_text)

            # 6. Click submit and wait for navigation to complete
            submit_selector = f"input[name='submit'][value='{spec.submit_value}']"
            submit_btn = page.query_selector(submit_selector)
            if submit_btn is None:
                # Fallback: any submit button
                submit_btn = page.query_selector("input[type='submit']")
            if submit_btn is None:
                raise BrowserUseUnavailable(
                    f"submit button not found: {submit_selector}"
                )

            # Use expect_navigation to properly wait for the POST round-trip.
            # A correct CAPTCHA triggers a redirect to /PatentSearch/... which
            # may itself cause a second navigation (the results page loads
            # content via a subsequent request).
            try:
                with page.expect_navigation(
                    wait_until="commit", timeout=480_000  # portal can take 5-7 min
                ):
                    submit_btn.click()
            except Exception:  # noqa: BLE001 - timeout or navigation error
                pass
            say(f"{lookup_value}: submitted, waiting for response...")

            # The portal can take 5-7 minutes to return results after POST.
            # Poll until we get real content or time out.
            html = ""
            max_wait = 480  # 8 minutes
            elapsed = 0
            poll_interval = 5  # seconds between checks
            while elapsed < max_wait:
                page.wait_for_timeout(poll_interval * 1000)
                elapsed += poll_interval
                try:
                    page.wait_for_load_state("load", timeout=5_000)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    html = page.content()
                except Exception:  # noqa: BLE001
                    continue
                # The shell/redirect page is ~500 bytes; real content is >2KB.
                # Also check for CAPTCHA rejection which comes back immediately.
                parsed_early = parse_response(html)
                if (
                    len(html) > 2000
                    or parsed_early.outcome is Outcome.CAPTCHA_REJECTED
                    or parsed_early.fields
                ):
                    if elapsed > poll_interval:
                        say(f"{lookup_value}: response received after ~{elapsed}s")
                    break
                if elapsed % 30 == 0:
                    say(f"{lookup_value}: still waiting ({elapsed}s elapsed)...")

            current_url = page.url
            logger.info("page URL after submit: %s (html length: %d)", current_url, len(html))
            parsed = parse_response(html)

            last_parsed = parsed
            last_html = html

            logger.info(
                "attempt %d: outcome=%s fields=%d",
                attempt,
                parsed.outcome.value,
                len(parsed.fields),
            )

            if parsed.outcome is Outcome.CAPTCHA_REJECTED:
                say(f"{lookup_value}: CAPTCHA rejected — retrying with fresh image")
                continue

            if parsed.outcome is Outcome.OK:
                say(f"{lookup_value}: ✓ got result with {len(parsed.fields)} fields")
            elif parsed.outcome is Outcome.NOT_FOUND:
                say(f"{lookup_value}: record not found")
            else:
                say(f"{lookup_value}: unexpected response (outcome={parsed.outcome.value})")

            return parsed, html

        assert last_parsed is not None
        return last_parsed, last_html

"""HTTP client for iprsearch.ipindia.gov.in.

The two search forms are plain ASP.NET postbacks with no JS dependency and no
anti-forgery token, verified against the live site:

  POST /PublicSearch/PublicationSearch/ApplicationStatus
        ApplicationNumber=<e.g. 9894/DELNP/2007>&CaptchaText=<..>&submit=Show Status
  POST /PublicSearch/PublicationSearch/Eregister
        PatentNumber=<6 digits>&CaptchaText=<..>&submit=Show E-Register

The CAPTCHA image is bound to the ASP.NET_SessionId cookie, so one session must
fetch the image and post the answer. Each image is single-use: a rejected answer
requires fetching a fresh one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from .config import settings

BASE_URL = "https://iprsearch.ipindia.gov.in"
CAPTCHA_PATH = "/PublicSearch/Captcha/GetCaptchaImage"


@dataclass(frozen=True)
class LookupSpec:
    path: str
    field: str
    submit_value: str


LOOKUPS: dict[str, LookupSpec] = {
    "application": LookupSpec(
        "/PublicSearch/PublicationSearch/ApplicationStatus",
        "ApplicationNumber",
        "Show Status",
    ),
    "patent": LookupSpec(
        "/PublicSearch/PublicationSearch/Eregister",
        "PatentNumber",
        "Show E-Register",
    ),
}


class PortalError(RuntimeError):
    pass


class _Throttle:
    """Process-wide floor on request spacing. This host is a shared public
    service; the delay is a courtesy and a block-avoidance measure."""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min:
                time.sleep(self._min - gap)
            self._last = time.monotonic()


_throttle = _Throttle(settings.min_request_interval)


class PortalSession:
    """One cookie jar = one CAPTCHA context. Not thread-safe; give each worker
    its own instance."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._current_path: str | None = None

    def __enter__(self) -> PortalSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def reset(self) -> None:
        """Drop cookies and start a clean session (new CAPTCHA context)."""
        self._client.cookies.clear()
        self._current_path = None

    def open_form(self, lookup_type: str) -> None:
        """GET the form page to establish ASP.NET_SessionId."""
        spec = self._spec(lookup_type)
        _throttle.wait()
        resp = self._client.get(spec.path)
        if resp.status_code != 200:
            raise PortalError(f"form page returned HTTP {resp.status_code}")
        self._current_path = spec.path

    def fetch_captcha(self, lookup_type: str) -> bytes:
        """Fetch a fresh single-use CAPTCHA PNG for this session."""
        if self._current_path is None:
            self.open_form(lookup_type)
        _throttle.wait()
        resp = self._client.get(
            CAPTCHA_PATH, headers={"Referer": BASE_URL + str(self._current_path)}
        )
        if resp.status_code != 200:
            raise PortalError(f"captcha returned HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        if "image" not in ctype:
            raise PortalError(f"captcha returned non-image content-type {ctype!r}")
        if not resp.content:
            raise PortalError("captcha returned empty body")
        return resp.content

    def submit(self, lookup_type: str, lookup_value: str, captcha_text: str) -> str:
        """Post the lookup and return the response HTML."""
        spec = self._spec(lookup_type)
        if self._current_path is None:
            self.open_form(lookup_type)
        _throttle.wait()
        resp = self._client.post(
            spec.path,
            data={
                "IP": "",  # hidden field, submitted empty by the real form
                spec.field: lookup_value,
                "CaptchaText": captcha_text,
                "submit": spec.submit_value,
            },
            headers={
                "Referer": BASE_URL + spec.path,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
            },
        )
        if resp.status_code != 200:
            raise PortalError(f"submit returned HTTP {resp.status_code}")
        return resp.text

    @staticmethod
    def _spec(lookup_type: str) -> LookupSpec:
        try:
            return LOOKUPS[lookup_type]
        except KeyError:
            raise PortalError(
                f"unknown lookup_type {lookup_type!r}; expected one of {sorted(LOOKUPS)}"
            ) from None

"""CAPTCHA resolution strategies.

Every strategy here routes the image to a *person*. The portal's CAPTCHA exists
to stop bulk automation, so this project does not attempt to defeat it: the
pipeline automates the boring 95% (DB read, submit, parse, persist, retry) and
asks a human for the one step the CAPTCHA is actually guarding.

`CaptchaSolver` is the documented extension point. Whether to plug anything
other than a human into it is a decision for whoever operates this system,
taken against the portal's terms of use — it is not implemented here.

Accessibility note: the portal also offers an audio CAPTCHA (`CaptchaAudio()`
on the page). Operators who need it can use the site directly and paste the
answer into `ipr calibrate`.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
from typing import Protocol

from bson import ObjectId

from . import store
from .config import settings


class CaptchaAbandoned(RuntimeError):
    """No human answered in time."""


class CaptchaSolver(Protocol):
    def solve(self, image_png: bytes, *, label: str, attempt: int) -> str:
        """Return the CAPTCHA text, or raise CaptchaAbandoned."""
        ...

    def report_rejected(self) -> None:
        """Called when the portal refused the last answer this solver gave."""
        ...


class TerminalSolver:
    """Opens the image with the OS viewer and prompts on stdin.

    Fine for `ipr calibrate` and small batches; for bulk work use QueueSolver so
    the operator isn't tied to the worker's terminal.
    """

    def __init__(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="ipr-captcha-")

    def solve(self, image_png: bytes, *, label: str, attempt: int) -> str:
        path = os.path.join(self._tmpdir, f"captcha-{int(time.time() * 1000)}.png")
        with open(path, "wb") as fh:
            fh.write(image_png)
        self._show(path)
        sys.stderr.write(f"\n  CAPTCHA for {label} (attempt {attempt})\n  image: {path}\n")
        try:
            answer = input("  enter captcha text (blank to skip): ").strip()
        except EOFError:
            raise CaptchaAbandoned("stdin closed") from None
        if not answer:
            raise CaptchaAbandoned("operator skipped")
        return answer

    def report_rejected(self) -> None:
        sys.stderr.write("  -> portal rejected that answer, fetching a fresh image\n")

    @staticmethod
    def _show(path: str) -> None:
        opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
        try:
            subprocess.run([opener, path], check=False, capture_output=True)
        except OSError:
            pass  # operator can still open the printed path manually


class QueueSolver:
    """Pushes the image into MongoDB for the operator web UI, then polls.

    Lets one person clear a stream of CAPTCHAs (~5s each) while N workers wait,
    which is the throughput-sensible arrangement for a large backlog.
    """

    def __init__(self, worker_id: str, wait_timeout: float | None = None) -> None:
        self.worker_id = worker_id
        self.wait_timeout = (
            wait_timeout if wait_timeout is not None else settings.captcha_wait_timeout
        )
        self._last_id: ObjectId | None = None

    def solve(self, image_png: bytes, *, label: str, attempt: int) -> str:
        expires = store.utcnow() + dt.timedelta(seconds=self.wait_timeout)
        challenge_id = store.enqueue_captcha(
            self.worker_id, f"{label} (try {attempt})", image_png, expires
        )
        self._last_id = challenge_id

        deadline = time.monotonic() + self.wait_timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            doc = store.read_captcha(challenge_id)
            if doc and doc.get("solution"):
                return str(doc["solution"])

        store.drop_captcha(challenge_id)
        raise CaptchaAbandoned(
            f"no operator answered within {self.wait_timeout:.0f}s "
            f"(is `ipr operator-ui` running?)"
        )

    def report_rejected(self) -> None:
        if self._last_id is not None:
            store.mark_captcha_rejected(self._last_id)

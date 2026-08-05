from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bson import ObjectId

from . import store
from .captcha import CaptchaAbandoned, CaptchaSolver
from .config import settings
from .parse import Outcome, ParseResult, parse_response
from .portal import PortalError, PortalSession

# Normalised fields lifted into applications.latest.* and snapshots.fields.*
RESULT_FIELDS = (
    "application_number",
    "patent_number",
    "title",
    "applicant_name",
    "application_type",
    "field_of_invention",
    "filing_date",
    "publication_date",
    "grant_date",
    "request_examination_date",
    "fer_date",
    "current_status",
    "decision_status",
)


@dataclass
class ScrapeOutcome:
    lookup_value: str
    outcome: Outcome
    changed: bool = False
    error: str | None = None


def _backoff(attempts: int) -> dt.timedelta:
    """1m, 4m, 9m, 16m... capped at 1h. Quadratic keeps early retries quick while
    stopping a permanently broken target from spinning."""
    return dt.timedelta(seconds=min(3600, 60 * max(1, attempts) ** 2))


def _save_raw_html(lookup_value: str, html: str) -> str | None:
    if not settings.save_raw_html:
        return None
    directory = Path(settings.raw_html_dir)
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in lookup_value)
    path = directory / f"{safe}-{store.utcnow():%Y%m%dT%H%M%S}.html"
    path.write_text(html, encoding="utf-8")
    return str(path)


def fetch_one(
    lookup_type: str,
    lookup_value: str,
    portal: PortalSession,
    solver: CaptchaSolver,
) -> tuple[ParseResult, str]:
    """Fetch and parse one record, re-prompting on CAPTCHA rejection.

    A rejected answer consumes its image, so each retry starts a clean session
    and pulls a fresh one.
    """
    last: ParseResult | None = None
    last_html = ""

    for attempt in range(1, settings.max_captcha_attempts + 1):
        portal.reset()
        portal.open_form(lookup_type)
        image = portal.fetch_captcha(lookup_type)
        answer = solver.solve(
            image, label=f"{lookup_type} {lookup_value}", attempt=attempt
        )

        last_html = portal.submit(lookup_type, lookup_value, answer)
        last = parse_response(last_html)

        if last.outcome is not Outcome.CAPTCHA_REJECTED:
            return last, last_html
        solver.report_rejected()

    assert last is not None
    return last, last_html


def persist(
    target_id: ObjectId,
    lookup_type: str,
    lookup_value: str,
    parsed: ParseResult,
    html: str,
) -> bool:
    """Write the result. Returns True when the content differed from last time
    (and so a new snapshot was appended)."""
    changed = store.latest_content_hash(target_id) != parsed.content_hash
    store.save_result(
        target_id=target_id,
        lookup_type=lookup_type,
        lookup_value=lookup_value,
        content_hash=parsed.content_hash,
        columns={k: parsed.columns.get(k) for k in RESULT_FIELDS},  # type: ignore[misc]
        raw_fields=parsed.fields,
        raw_tables=parsed.tables,
        raw_html_path=_save_raw_html(lookup_value, html) if changed else None,
        changed=changed,
    )
    return changed


def process_target(
    doc: dict[str, Any],
    portal: PortalSession | None,
    solver: CaptchaSolver | None,
    fetcher: Callable[[str, str], tuple[ParseResult, str]] | None = None,
) -> ScrapeOutcome:
    """Scrape and persist one document.

    `fetcher` overrides the default HTTP+solver path — that is how the
    Browserbase driver plugs in without duplicating any of the persistence,
    retry or history logic below.
    """
    target_id: ObjectId = doc["_id"]
    lookup_type: str = doc["lookup_type"]
    lookup_value: str = doc["lookup_value"]
    attempts: int = doc.get("scrape", {}).get("attempts", 0)

    try:
        if fetcher is not None:
            parsed, html = fetcher(lookup_type, lookup_value)
        else:
            assert portal is not None and solver is not None
            parsed, html = fetch_one(lookup_type, lookup_value, portal, solver)
    except TimeoutError as exc:
        # Operator never submitted — requeue without penalty, same as an
        # abandoned CAPTCHA on the HTTP path.
        store.requeue(target_id)
        return ScrapeOutcome(lookup_value, Outcome.UNKNOWN, error=str(exc))
    except CaptchaAbandoned as exc:
        # Operator's absence, not the record's fault — requeue without penalty.
        store.requeue(target_id)
        return ScrapeOutcome(lookup_value, Outcome.UNKNOWN, error=str(exc))
    except PortalError as exc:
        store.mark_failure(target_id, f"portal: {exc}", _backoff(attempts + 1))
        return ScrapeOutcome(lookup_value, Outcome.UNKNOWN, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
        store.mark_failure(
            target_id, f"{type(exc).__name__}: {exc}", _backoff(attempts + 1)
        )
        return ScrapeOutcome(lookup_value, Outcome.UNKNOWN, error=str(exc))

    if parsed.outcome is Outcome.OK:
        changed = persist(target_id, lookup_type, lookup_value, parsed, html)
        return ScrapeOutcome(lookup_value, Outcome.OK, changed=changed)

    if parsed.outcome is Outcome.NOT_FOUND:
        store.mark_not_found(target_id)
        return ScrapeOutcome(lookup_value, Outcome.NOT_FOUND)

    if parsed.outcome is Outcome.CAPTCHA_REJECTED:
        store.mark_failure(
            target_id,
            f"captcha rejected {settings.max_captcha_attempts}x in a row",
            _backoff(attempts + 1),
        )
        return ScrapeOutcome(
            lookup_value, Outcome.CAPTCHA_REJECTED, error="captcha rejected"
        )

    # Unrecognised page shape: keep the HTML so the parser can be calibrated.
    path = _save_raw_html(lookup_value, html)
    store.mark_failure(
        target_id, f"unparsed response; raw html saved to {path}", _backoff(attempts + 1)
    )
    return ScrapeOutcome(lookup_value, Outcome.UNKNOWN, error=f"unparsed; saved {path}")


def _tally(counts: dict[str, int], result: ScrapeOutcome) -> None:
    counts["attempted"] += 1
    if result.outcome is Outcome.OK:
        counts["ok"] += 1
        counts["changed"] += int(result.changed)
    elif result.outcome is Outcome.NOT_FOUND:
        counts["not_found"] += 1
    else:
        counts["failed"] += 1


def run_batch(
    limit: int,
    solver: CaptchaSolver,
    on_result: Callable[[ScrapeOutcome], None] | None = None,
) -> dict[str, int]:
    """Work up to `limit` targets over the HTTP transport. Returns counts."""
    store.release_stale()
    claimed = store.claim_targets(limit)

    counts = {"attempted": 0, "ok": 0, "failed": 0, "not_found": 0, "changed": 0}
    run_id = store.start_run(f"limit={limit} solver={type(solver).__name__}")

    try:
        with PortalSession() as portal:
            for doc in claimed:
                result = process_target(doc, portal, solver)
                _tally(counts, result)
                if on_result is not None:
                    on_result(result)
    finally:
        store.finish_run(run_id, **counts)

    return counts


def run_batch_browserbase(
    limit: int,
    on_result: Callable[[ScrapeOutcome], None] | None = None,
    on_notify: Callable[[str], None] | None = None,
    on_session: Callable[[str, str], None] | None = None,
) -> dict[str, int]:
    """Work up to `limit` targets over one Browserbase cloud session.

    The operator clears CAPTCHAs in the session's Live View; everything else —
    parsing, normalising, change detection, history, retry — is the same code
    path the HTTP transport uses.
    """
    from .driver_browserbase import BrowserbaseDriver

    store.release_stale()
    claimed = store.claim_targets(limit)

    counts = {"attempted": 0, "ok": 0, "failed": 0, "not_found": 0, "changed": 0}
    if not claimed:
        return counts

    run_id = store.start_run(f"limit={limit} driver=browserbase")
    try:
        with BrowserbaseDriver() as driver:
            if on_session is not None:
                on_session(driver.live_view_url or "", driver.dashboard_url)

            def fetcher(lookup_type: str, lookup_value: str):
                return driver.fetch(lookup_type, lookup_value, notify=on_notify)

            for doc in claimed:
                result = process_target(doc, None, None, fetcher=fetcher)
                _tally(counts, result)
                if on_result is not None:
                    on_result(result)
    finally:
        store.finish_run(run_id, **counts)

    return counts


def run_batch_browseruse(
    limit: int,
    on_result: Callable[[ScrapeOutcome], None] | None = None,
    on_notify: Callable[[str], None] | None = None,
    headless: bool = True,
    model: str = "gpt-4o",
) -> dict[str, int]:
    """Work up to `limit` targets using browser-use (LLM-driven browser).

    The LLM reads and solves CAPTCHAs via vision — fully unattended.
    Everything else (parsing, normalising, change detection, history, retry)
    is the same code path every transport uses.
    """
    from .driver_browseruse import BrowserUseDriver

    store.release_stale()
    claimed = store.claim_targets(limit)

    counts = {"attempted": 0, "ok": 0, "failed": 0, "not_found": 0, "changed": 0}
    if not claimed:
        return counts

    run_id = store.start_run(f"limit={limit} driver=browseruse model={model}")
    try:
        with BrowserUseDriver(
            headless=headless, model=model
        ) as driver:
            def fetcher(lookup_type: str, lookup_value: str):
                return driver.fetch(lookup_type, lookup_value, notify=on_notify)

            for doc in claimed:
                result = process_target(doc, None, None, fetcher=fetcher)
                _tally(counts, result)
                if on_result is not None:
                    on_result(result)
    finally:
        store.finish_run(run_id, **counts)

    return counts

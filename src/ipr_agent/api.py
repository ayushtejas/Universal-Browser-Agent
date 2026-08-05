"""REST API + background worker in a single process.

Start with:  ipr api
Runs on:     http://0.0.0.0:8000
Docs at:     http://0.0.0.0:8000/docs

The worker thread continuously polls MongoDB for pending targets and
processes them with Playwright + GPT-4o vision. The API thread seeds
batches and serves status/results — it never touches the browser.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from . import store
from .models import (
    ApplicationResult,
    BatchListItem,
    BatchProgress,
    BatchResponse,
    BatchSubmit,
    HealthResponse,
)

logger = logging.getLogger(__name__)

# ── Worker ────────────────────────────────────────────────────────────────

_worker_running = False
_worker_thread: threading.Thread | None = None


def _worker_loop() -> None:
    """Continuously process the scrape queue. Runs in a background thread."""
    global _worker_running
    _worker_running = True
    logger.info("worker started — polling for targets")

    from .config import settings
    from .driver_browseruse import BrowserUseDriver, BrowserUseUnavailable
    from .parse import Outcome
    from .pipeline import ScrapeOutcome, _tally, process_target

    while _worker_running:
        try:
            store.release_stale()
            claimed = store.claim_targets(limit=5)  # small batches, stay responsive

            if not claimed:
                time.sleep(10)  # idle — poll every 10s
                continue

            run_id = store.start_run(f"worker limit=5 driver=browseruse")
            counts = {"attempted": 0, "ok": 0, "failed": 0, "not_found": 0, "changed": 0}

            try:
                with BrowserUseDriver(headless=True) as driver:
                    def fetcher(lt: str, lv: str):
                        return driver.fetch(lt, lv, notify=lambda m: logger.info(m))

                    for doc in claimed:
                        if not _worker_running:
                            # Requeue unclaimed on shutdown
                            store.requeue(doc["_id"])
                            break
                        result = process_target(doc, None, None, fetcher=fetcher)
                        _tally(counts, result)
                        icon = {Outcome.OK: "✓", Outcome.NOT_FOUND: "∅"}.get(
                            result.outcome, "✗"
                        )
                        logger.info(
                            "%s %s (%s)", icon, result.lookup_value, result.outcome.value
                        )
            except BrowserUseUnavailable as exc:
                logger.error("worker: browser unavailable — %s", exc)
                # Requeue anything we claimed
                for doc in claimed:
                    store.requeue(doc["_id"])
                time.sleep(30)
            except Exception:
                logger.exception("worker: unexpected error in batch")
                time.sleep(10)
            finally:
                try:
                    store.finish_run(run_id, **counts)
                except Exception:
                    pass

            # Brief pause between batches
            time.sleep(2)

        except Exception:
            logger.exception("worker: outer loop error")
            time.sleep(15)

    logger.info("worker stopped")


def _start_worker() -> None:
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="ipr-worker")
    _worker_thread.start()


def _stop_worker() -> None:
    global _worker_running
    _worker_running = False
    if _worker_thread is not None:
        _worker_thread.join(timeout=30)


# ── App lifecycle ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.ensure_indexes()
    _start_worker()
    yield
    _stop_worker()


# ── FastAPI ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="IPR Scraper API",
    description="Submit Indian patent application/patent numbers, get status and results.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_progress(apps: list[dict]) -> BatchProgress:
    counts = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "not_found": 0}
    for doc in apps:
        status = doc.get("scrape", {}).get("status", "pending")
        if status in counts:
            counts[status] += 1
    return BatchProgress(total=len(apps), **counts)


def _build_result(doc: dict) -> ApplicationResult:
    scrape = doc.get("scrape", {})
    status = scrape.get("status", "pending")
    return ApplicationResult(
        lookup_type=doc.get("lookup_type", ""),
        lookup_value=doc.get("lookup_value", ""),
        status=status,
        data=doc.get("latest") if status == "done" else None,
        error=scrape.get("last_error"),
        last_scraped_at=scrape.get("last_scraped_at"),
    )


def _batch_status(progress: BatchProgress) -> str:
    if progress.pending + progress.in_progress == 0:
        return "completed"
    if progress.done + progress.failed + progress.not_found > 0:
        return "processing"
    return "submitted"


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    pending = store.status_counts().get("pending", 0)
    return HealthResponse(
        status="ok",
        worker_running=_worker_running,
        queue_pending=pending,
    )


@app.post("/api/v1/batches", response_model=BatchResponse, tags=["batches"])
def submit_batch(body: BatchSubmit):
    """Submit 1-50 application/patent numbers for scraping.

    Returns a batch_id you can poll for progress and results.
    The background worker picks up targets automatically.
    """
    total = len(body.application_numbers) + len(body.patent_numbers)
    if total == 0:
        raise HTTPException(400, "provide at least one number")
    if total > 50:
        raise HTTPException(400, "max 50 numbers per batch")

    batch_id = store.create_batch(body.application_numbers, body.patent_numbers)
    apps = store.get_batch_applications(batch_id)
    progress = _build_progress(apps)

    return BatchResponse(
        batch_id=batch_id,
        status=_batch_status(progress),
        created_at=store.get_batch(batch_id)["created_at"],
        progress=progress,
        results=[_build_result(doc) for doc in apps],
    )


@app.get("/api/v1/batches", response_model=list[BatchListItem], tags=["batches"])
def list_batches(limit: int = 20):
    """List recent batches, newest first."""
    out = []
    for batch in store.list_batches(limit):
        apps = store.get_batch_applications(batch["batch_id"])
        progress = _build_progress(apps)
        out.append(
            BatchListItem(
                batch_id=batch["batch_id"],
                status=_batch_status(progress),
                created_at=batch["created_at"],
                progress=progress,
            )
        )
    return out


@app.get("/api/v1/batches/{batch_id}", response_model=BatchResponse, tags=["batches"])
def get_batch(batch_id: str):
    """Poll for batch progress and results.

    Results appear as individual records complete — you don't have to
    wait for the entire batch. Poll every 5-10 seconds.
    """
    batch = store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"batch {batch_id} not found")

    apps = store.get_batch_applications(batch_id)
    progress = _build_progress(apps)

    return BatchResponse(
        batch_id=batch_id,
        status=_batch_status(progress),
        created_at=batch["created_at"],
        progress=progress,
        results=[_build_result(doc) for doc in apps],
    )


@app.post("/api/v1/batches/{batch_id}/cancel", tags=["batches"])
def cancel_batch(batch_id: str):
    """Cancel pending (not yet started) targets in a batch."""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    cancelled = store.cancel_batch(batch_id)
    return {"cancelled": cancelled}


@app.get(
    "/api/v1/applications/{lookup_value}",
    response_model=ApplicationResult,
    tags=["applications"],
)
def get_application(lookup_value: str):
    """Get the current status/data for a single application or patent number."""
    doc = store.applications().find_one({"lookup_value": lookup_value})
    if doc is None:
        raise HTTPException(404, f"application {lookup_value} not found")
    return _build_result(doc)

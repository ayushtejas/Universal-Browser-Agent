"""REST API + background worker in a single process.

Start with:  ipr api
Runs on:     http://0.0.0.0:8000
Docs at:     http://0.0.0.0:8000/docs

The worker thread continuously polls MongoDB for pending targets and
processes them with Playwright + GPT-4o vision. The API thread seeds
batches and serves status/results — it never touches the browser.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware

from . import store
from .agent_runner import (
    UnsafeRun,
    run_agent_sync,
    validate_public_request,
    validate_public_url,
)
from .config import settings
from .models import (
    AgentRunResponse,
    AgentRunSubmit,
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
_agent_slots = threading.BoundedSemaphore(value=settings.agent_max_concurrency)


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
    try:
        store.ensure_indexes()
        _start_worker()
    except Exception:
        # Keep the API process up so health checks and CORS preflights still
        # respond while the database is unavailable. Data endpoints will fail
        # explicitly instead of making the load balancer return a generic 504.
        logger.exception("database unavailable during startup; API is degraded")
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
    allow_origins=[
        origin.strip()
        for origin in settings.cors_origins.split(",")
        if origin.strip()
    ],
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
        raw_fields=doc.get("raw_fields") if status == "done" else None,
        error=scrape.get("last_error"),
        last_scraped_at=scrape.get("last_scraped_at"),
    )


def _batch_status(progress: BatchProgress) -> str:
    if progress.pending + progress.in_progress == 0:
        return "completed"
    if progress.done + progress.failed + progress.not_found > 0:
        return "processing"
    return "submitted"


def _client_fingerprint(request: Request) -> str:
    # Hash instead of persisting a raw IP. Do not trust forwarded headers here;
    # configure the ASGI proxy layer to provide the real request.client value.
    host = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"waypoint:{host}".encode()).hexdigest()


def _agent_worker(run_id: str, body: AgentRunSubmit) -> None:
    with _agent_slots:
        try:
            run_agent_sync(run_id, body)
        except Exception:
            logger.exception("Waypoint run %s failed", run_id)


# ── Endpoints ─────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["system"])
def health():
    try:
        pending = store.status_counts().get("pending", 0)
        database_connected = True
        service_status = "ok"
    except Exception:
        pending = 0
        database_connected = False
        service_status = "degraded"
    return HealthResponse(
        status=service_status,
        worker_running=_worker_running,
        queue_pending=pending,
        database_connected=database_connected,
    )


@app.post(
    "/api/v1/agent/runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["browser agent"],
)
def start_agent_run(body: AgentRunSubmit, request: Request):
    """Start a bounded same-site browser run and return immediately for polling."""
    try:
        body.target_url = validate_public_url(body.target_url)
        validate_public_request(body)
    except UnsafeRun as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    fingerprint = _client_fingerprint(request)
    try:
        recent_runs = store.recent_agent_run_count(fingerprint)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Run storage is temporarily unavailable",
        ) from exc
    if recent_runs >= settings.public_runs_per_hour:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Public limit reached: {settings.public_runs_per_hour} runs per hour",
        )

    run_id = secrets.token_hex(6)
    try:
        doc = store.create_agent_run(
            run_id=run_id,
            client_fingerprint=fingerprint,
            target_url=body.target_url,
            instructions=body.instructions,
            mode=body.mode,
            output_format=body.output_format,
            safe_mode=body.safe_mode,
            max_steps=body.max_steps,
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Run storage is temporarily unavailable",
        ) from exc
    thread = threading.Thread(
        target=_agent_worker,
        args=(run_id, body),
        daemon=True,
        name=f"waypoint-{run_id}",
    )
    thread.start()
    return AgentRunResponse.model_validate(doc)


@app.get(
    "/api/v1/agent/runs/{run_id}",
    response_model=AgentRunResponse,
    tags=["browser agent"],
)
def get_agent_run(run_id: str):
    """Poll a run for progress, live-view URL, trace events, and final output."""
    if not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    try:
        doc = store.get_agent_run(run_id)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Run storage is temporarily unavailable",
        ) from exc
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return AgentRunResponse.model_validate(doc)


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

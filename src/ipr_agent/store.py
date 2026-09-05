"""MongoDB / AWS DocumentDB persistence.

Document design (idiomatic Mongo rather than transliterated tables):

  applications          one doc per tracked number. Holds the queue state under
                        `scrape.*` AND the newest scraped values under `latest.*`,
                        so a single findOne gives you everything about a record.
  status_snapshots      append-only. One doc per *change* in scraped content,
                        giving a status-change history per application.
  captcha_challenges    short-lived; workers insert, the operator UI resolves.
  scrape_runs           one doc per `ipr run`, for throughput auditing.

Written against DocumentDB 5.0 constraints: no multi-document transactions and
no aggregation-pipeline updates. Claiming uses findOneAndUpdate, which
DocumentDB does support atomically — that is what keeps two workers off the
same record.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterator

from bson import ObjectId
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from .config import settings

# Queue states for applications.scrape.status
PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"
NOT_FOUND = "not_found"

_client: MongoClient | None = None


def utcnow() -> dt.datetime:
    # Stored naive-UTC: BSON has no tz, and mixing aware/naive breaks comparisons
    # in range queries against documents written by other services.
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def client() -> MongoClient:
    global _client
    if _client is None:
        if not settings.mongodb_uri:
            raise RuntimeError("MONGODB_URI is not set (see .env.example)")
        _client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=15000,
            connectTimeoutMS=15000,
            tz_aware=False,
        )
    return _client


def database() -> Database:
    if settings.mongodb_db:
        return client()[settings.mongodb_db]
    db = client().get_default_database()
    if db is None:
        raise RuntimeError("no database in MONGODB_URI path; set IPR_MONGODB_DB")
    return db


def applications() -> Collection:
    return database()[settings.collection_applications]


def snapshots() -> Collection:
    return database()[settings.collection_snapshots]


def captchas() -> Collection:
    return database()[settings.collection_captcha]


def runs() -> Collection:
    return database()[settings.collection_runs]


def agent_runs() -> Collection:
    return database()[settings.collection_agent_runs]


def ensure_indexes() -> list[str]:
    """Idempotent. Safe to call on every command."""
    created: list[str] = []
    try:
        applications().create_index(
            [("lookup_type", ASCENDING), ("lookup_value", ASCENDING)],
            unique=True,
            name="uq_lookup",
        )
        created.append("applications.uq_lookup")
        applications().create_index(
            [("scrape.status", ASCENDING), ("scrape.next_attempt_at", ASCENDING)],
            name="ix_claim",
        )
        created.append("applications.ix_claim")
        snapshots().create_index(
            [("lookup_type", ASCENDING), ("lookup_value", ASCENDING),
             ("fetched_at", ASCENDING)],
            name="ix_history",
        )
        created.append("status_snapshots.ix_history")
        captchas().create_index(
            [("solution", ASCENDING), ("created_at", ASCENDING)], name="ix_open"
        )
        created.append("captcha_challenges.ix_open")
        agent_runs().create_index(
            [("run_id", ASCENDING)], unique=True, name="uq_agent_run_id"
        )
        created.append("agent_runs.uq_agent_run_id")
        agent_runs().create_index(
            [("client_fingerprint", ASCENDING), ("created_at", ASCENDING)],
            name="ix_agent_rate_limit",
        )
        created.append("agent_runs.ix_agent_rate_limit")
    except OperationFailure as exc:
        raise RuntimeError(f"could not create indexes: {exc}") from exc
    return created


# --------------------------------------------------------------------------
# Waypoint general browser-agent runs
# --------------------------------------------------------------------------

def create_agent_run(
    *,
    run_id: str,
    client_fingerprint: str,
    target_url: str,
    instructions: str,
    mode: str,
    output_format: str,
    safe_mode: bool,
    max_steps: int,
) -> dict[str, Any]:
    now = utcnow()
    doc: dict[str, Any] = {
        "run_id": run_id,
        "client_fingerprint": client_fingerprint,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "target_url": target_url,
        "instructions": instructions,
        "mode": mode,
        "output_format": output_format,
        "safe_mode": safe_mode,
        "max_steps": max_steps,
        "progress": 2,
        "live_view_url": None,
        "session_id": None,
        "result": None,
        "error": None,
        "events": [
            {"at": now, "kind": "queued", "message": "Run accepted and queued"}
        ],
    }
    agent_runs().insert_one(doc)
    doc.pop("_id", None)
    return doc


def get_agent_run(run_id: str) -> dict[str, Any] | None:
    return agent_runs().find_one({"run_id": run_id}, {"_id": 0, "client_fingerprint": 0})


def recent_agent_run_count(client_fingerprint: str, *, hours: int = 1) -> int:
    cutoff = utcnow() - dt.timedelta(hours=hours)
    return int(
        agent_runs().count_documents(
            {"client_fingerprint": client_fingerprint, "created_at": {"$gte": cutoff}}
        )
    )


def update_agent_run(run_id: str, **fields: Any) -> None:
    fields["updated_at"] = utcnow()
    agent_runs().update_one({"run_id": run_id}, {"$set": fields})


def append_agent_event(
    run_id: str,
    *,
    kind: str,
    message: str,
    url: str | None = None,
    progress: int | None = None,
) -> None:
    event: dict[str, Any] = {
        "at": utcnow(),
        "kind": kind,
        "message": message[:500],
    }
    if url:
        event["url"] = url[:2048]
    update: dict[str, Any] = {"updated_at": utcnow()}
    if progress is not None:
        update["progress"] = max(0, min(progress, 100))
    agent_runs().update_one(
        {"run_id": run_id},
        {"$push": {"events": {"$each": [event], "$slice": -100}}, "$set": update},
    )


# --------------------------------------------------------------------------
# applications
# --------------------------------------------------------------------------


def upsert_target(
    lookup_type: str, lookup_value: str, source_ref: str | None = None
) -> bool:
    """Add a number to the queue. Returns True if newly inserted.

    Existing docs are left untouched so re-seeding never resets progress or
    clobbers scraped data.
    """
    now = utcnow()
    try:
        result = applications().update_one(
            {"lookup_type": lookup_type, "lookup_value": lookup_value},
            {
                "$setOnInsert": {
                    "lookup_type": lookup_type,
                    "lookup_value": lookup_value,
                    "source_ref": source_ref,
                    "scrape": {
                        "status": PENDING,
                        "attempts": 0,
                        "last_error": None,
                        "next_attempt_at": now,
                        "last_scraped_at": None,
                        "content_hash": None,
                    },
                    "latest": {},
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None
    except DuplicateKeyError:
        return False  # concurrent seeder won the race


def adopt_existing(value_field: str, lookup_type: str) -> tuple[int, int]:
    """Add lookup/scrape fields to docs that already live in `applications`.

    For the case where another pipeline already writes application numbers into
    this collection: enrich those documents in place rather than inserting a
    parallel set. Docs already carrying a lookup_value are left alone.

    Returns (adopted, conflicts).
    """
    now = utcnow()
    adopted = conflicts = 0
    query = {
        value_field: {"$nin": [None, ""]},
        "$or": [{"lookup_value": {"$exists": False}}, {"lookup_value": None}],
    }
    for doc in applications().find(query, {value_field: 1}):
        try:
            applications().update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "lookup_type": lookup_type,
                        "lookup_value": str(doc[value_field]).strip(),
                        "scrape": {
                            "status": PENDING,
                            "attempts": 0,
                            "last_error": None,
                            "next_attempt_at": now,
                            "last_scraped_at": None,
                            "content_hash": None,
                        },
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
            )
            adopted += 1
        except DuplicateKeyError:
            conflicts += 1
    return adopted, conflicts


def claim_targets(limit: int) -> list[dict[str, Any]]:
    """Atomically flip up to `limit` due docs to in_progress and return them.

    One findOneAndUpdate per doc: DocumentDB has no atomic multi-doc update that
    also returns the documents, and this is what makes parallel workers safe.
    """
    now = utcnow()
    claimed: list[dict[str, Any]] = []
    for _ in range(limit):
        doc = applications().find_one_and_update(
            {
                "scrape.status": {"$in": [PENDING, FAILED]},
                "scrape.next_attempt_at": {"$lte": now},
                "scrape.attempts": {"$lt": settings.max_target_attempts},
            },
            {"$set": {"scrape.status": IN_PROGRESS, "updated_at": now}},
            sort=[("scrape.next_attempt_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            break
        claimed.append(doc)
    return claimed


def release_stale(older_than_minutes: int = 30) -> int:
    """Requeue docs left in_progress by a crashed worker."""
    cutoff = utcnow() - dt.timedelta(minutes=older_than_minutes)
    result = applications().update_many(
        {"scrape.status": IN_PROGRESS, "updated_at": {"$lt": cutoff}},
        {"$set": {"scrape.status": PENDING, "scrape.next_attempt_at": utcnow()}},
    )
    return int(result.modified_count)


def requeue(target_id: ObjectId) -> None:
    """Put a doc straight back in the queue without burning an attempt."""
    now = utcnow()
    applications().update_one(
        {"_id": target_id},
        {
            "$set": {
                "scrape.status": PENDING,
                "scrape.next_attempt_at": now,
                "updated_at": now,
            }
        },
    )


def mark_failure(target_id: ObjectId, message: str, backoff: dt.timedelta) -> None:
    now = utcnow()
    doc = applications().find_one_and_update(
        {"_id": target_id},
        {"$inc": {"scrape.attempts": 1},
         "$set": {"scrape.last_error": message[:2000], "updated_at": now}},
        return_document=ReturnDocument.AFTER,
        projection={"scrape.attempts": 1},
    )
    attempts = (doc or {}).get("scrape", {}).get("attempts", 0)
    if attempts >= settings.max_target_attempts:
        applications().update_one(
            {"_id": target_id}, {"$set": {"scrape.status": FAILED}}
        )
    else:
        applications().update_one(
            {"_id": target_id},
            {"$set": {"scrape.status": PENDING,
                      "scrape.next_attempt_at": now + backoff}},
        )


def mark_not_found(target_id: ObjectId) -> None:
    now = utcnow()
    applications().update_one(
        {"_id": target_id},
        {
            "$set": {
                "scrape.status": NOT_FOUND,
                "scrape.last_scraped_at": now,
                "scrape.last_error": None,
                "updated_at": now,
            }
        },
    )


def latest_content_hash(target_id: ObjectId) -> str | None:
    doc = applications().find_one(
        {"_id": target_id}, projection={"scrape.content_hash": 1}
    )
    return (doc or {}).get("scrape", {}).get("content_hash")


def save_result(
    target_id: ObjectId,
    lookup_type: str,
    lookup_value: str,
    content_hash: str,
    columns: dict[str, str],
    raw_fields: dict[str, str],
    raw_tables: list[Any],
    raw_html_path: str | None,
    changed: bool,
) -> None:
    """Update the application doc, and append a snapshot when content changed."""
    now = utcnow()
    update: dict[str, Any] = {
        "scrape.status": DONE,
        "scrape.last_scraped_at": now,
        "scrape.last_error": None,
        "scrape.content_hash": content_hash,
        "updated_at": now,
    }
    if changed:
        update["latest"] = columns
        update["raw_fields"] = raw_fields
        update["raw_tables"] = raw_tables
        update["raw_html_path"] = raw_html_path

    applications().update_one({"_id": target_id}, {"$set": update})

    if changed:
        snapshots().insert_one(
            {
                "target_id": target_id,
                "lookup_type": lookup_type,
                "lookup_value": lookup_value,
                "fetched_at": now,
                "content_hash": content_hash,
                "fields": columns,
                "raw_fields": raw_fields,
                "raw_tables": raw_tables,
                "raw_html_path": raw_html_path,
            }
        )


def status_counts() -> dict[str, int]:
    out = {s: 0 for s in (PENDING, IN_PROGRESS, DONE, FAILED, NOT_FOUND)}
    for row in applications().aggregate(
        [{"$group": {"_id": "$scrape.status", "n": {"$sum": 1}}}]
    ):
        if row["_id"] in out:
            out[row["_id"]] = row["n"]
    return out


def failed_samples(limit: int = 10) -> list[dict[str, Any]]:
    return list(
        applications()
        .find(
            {"scrape.status": FAILED},
            projection={"lookup_value": 1, "scrape.attempts": 1, "scrape.last_error": 1},
        )
        .limit(limit)
    )


def iter_applications(query: dict | None = None) -> Iterator[dict[str, Any]]:
    return applications().find(query or {})


# --------------------------------------------------------------------------
# captcha queue
# --------------------------------------------------------------------------


def enqueue_captcha(
    worker_id: str, label: str, image_png: bytes, expires_at: dt.datetime
) -> ObjectId:
    result = captchas().insert_one(
        {
            "worker_id": worker_id,
            "context_label": label,
            "image_png": image_png,
            "created_at": utcnow(),
            "expires_at": expires_at,
            "solved_at": None,
            "solution": None,
            "rejected": False,
        }
    )
    return result.inserted_id


def read_captcha(challenge_id: ObjectId) -> dict[str, Any] | None:
    return captchas().find_one({"_id": challenge_id})


def next_open_captcha() -> dict[str, Any] | None:
    return captchas().find_one(
        {"solution": None}, sort=[("created_at", ASCENDING)]
    )


def solve_captcha(challenge_id: ObjectId, solution: str) -> bool:
    result = captchas().update_one(
        {"_id": challenge_id, "solution": None},
        {"$set": {"solution": solution, "solved_at": utcnow()}},
    )
    return result.modified_count == 1


def mark_captcha_rejected(challenge_id: ObjectId) -> None:
    captchas().update_one({"_id": challenge_id}, {"$set": {"rejected": True}})


def drop_captcha(challenge_id: ObjectId) -> None:
    captchas().delete_one({"_id": challenge_id})


def captcha_counts() -> tuple[int, int]:
    pending = captchas().count_documents({"solution": None})
    solved = captchas().count_documents({"solution": {"$ne": None}})
    return pending, solved


def purge_solved_captchas(older_than_hours: int = 24) -> int:
    cutoff = utcnow() - dt.timedelta(hours=older_than_hours)
    result = captchas().delete_many(
        {"solution": {"$ne": None}, "created_at": {"$lt": cutoff}}
    )
    return int(result.deleted_count)


# --------------------------------------------------------------------------
# run log
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# batches
# --------------------------------------------------------------------------


def batches() -> Collection:
    return database()["batches"]


def create_batch(
    application_numbers: list[str], patent_numbers: list[str]
) -> str:
    """Create a batch and seed its targets. Returns the batch_id."""
    import uuid

    batch_id = uuid.uuid4().hex[:12]
    now = utcnow()

    lookup_values: list[dict] = []
    for v in application_numbers:
        v = v.strip()
        if v:
            lookup_values.append({"lookup_type": "application", "lookup_value": v})
    for v in patent_numbers:
        v = v.strip()
        if v:
            lookup_values.append({"lookup_type": "patent", "lookup_value": v})

    batches().insert_one(
        {
            "batch_id": batch_id,
            "created_at": now,
            "updated_at": now,
            "total": len(lookup_values),
            "lookup_values": lookup_values,
        }
    )

    # Seed each target — upsert so duplicates across batches are safe.
    # Tag each application doc with batch_ids (array) so we can query by batch.
    for item in lookup_values:
        lt, lv = item["lookup_type"], item["lookup_value"]
        upsert_target(lt, lv, source_ref=f"batch:{batch_id}")
        # Add this batch_id to the doc's batch list (idempotent).
        applications().update_one(
            {"lookup_type": lt, "lookup_value": lv},
            {"$addToSet": {"batch_ids": batch_id}},
        )

    return batch_id


def get_batch(batch_id: str) -> dict | None:
    return batches().find_one({"batch_id": batch_id})


def get_batch_applications(batch_id: str) -> list[dict]:
    return list(applications().find({"batch_ids": batch_id}))


def list_batches(limit: int = 20) -> list[dict]:
    return list(
        batches()
        .find({}, {"lookup_values": 0})
        .sort("created_at", -1)
        .limit(limit)
    )


def cancel_batch(batch_id: str) -> int:
    """Cancel pending targets in a batch. Returns count cancelled."""
    result = applications().update_many(
        {
            "batch_ids": batch_id,
            "scrape.status": {"$in": [PENDING]},
        },
        {"$set": {"scrape.status": FAILED, "scrape.last_error": "cancelled"}},
    )
    return int(result.modified_count)


# --------------------------------------------------------------------------
# run log
# --------------------------------------------------------------------------


def start_run(note: str) -> ObjectId:
    return runs().insert_one({"started_at": utcnow(), "note": note}).inserted_id


def finish_run(run_id: ObjectId, **counts: int) -> None:
    runs().update_one(
        {"_id": run_id}, {"$set": {"finished_at": utcnow(), **counts}}
    )

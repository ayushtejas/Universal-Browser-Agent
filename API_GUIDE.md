# IPR Scraper API — Integration Guide

**Base URL:** `https://ipr-browser-agent-be.rapid.studio.lyzr.ai`  
**Swagger Docs:** `https://ipr-browser-agent-be.rapid.studio.lyzr.ai/docs`

---

## How It Works

1. You **submit** a batch of Indian patent application numbers (or patent numbers).
2. The server queues them and a background worker **automatically scrapes** the IP India portal — including solving the CAPTCHA.
3. You **poll** the batch endpoint every 5–10 seconds until results appear.
4. Each application takes **3–7 minutes** (portal response time). Results stream in as each one completes — you don't wait for the entire batch.

---

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/batches` | Submit 1–50 numbers for scraping |
| `GET` | `/api/v1/batches/{batch_id}` | Poll progress & results for a batch |
| `GET` | `/api/v1/batches` | List all batches (newest first) |
| `POST` | `/api/v1/batches/{batch_id}/cancel` | Cancel pending targets in a batch |
| `GET` | `/api/v1/applications/{lookup_value}` | Get result for a single number |
| `GET` | `/health` | Health check |

---

## Step-by-Step Usage

### 1. Submit a Batch

**Request:**

```bash
curl -X POST https://ipr-browser-agent-be.rapid.studio.lyzr.ai/api/v1/batches \
  -H "Content-Type: application/json" \
  -d '{
    "application_numbers": ["202641089029", "202641089033"],
    "patent_numbers": []
  }'
```

- `application_numbers` — list of Indian patent **application** numbers (max 50)
- `patent_numbers` — list of Indian **patent** numbers (max 50)
- You can pass both in the same request. Combined total must be ≤ 50.

**Response:**

```json
{
  "batch_id": "85b6c6a2eafd",
  "status": "submitted",
  "created_at": "2026-08-05T12:50:18.406000",
  "progress": {
    "total": 2,
    "pending": 2,
    "in_progress": 0,
    "done": 0,
    "failed": 0,
    "not_found": 0
  },
  "results": [
    {
      "lookup_type": "application",
      "lookup_value": "202641089029",
      "status": "pending",
      "data": null,
      "error": null,
      "last_scraped_at": null
    },
    {
      "lookup_type": "application",
      "lookup_value": "202641089033",
      "status": "pending",
      "data": null,
      "error": null,
      "last_scraped_at": null
    }
  ]
}
```

> **Save the `batch_id`** — you need it to poll for results.

---

### 2. Poll for Results

**Request:**

```bash
curl https://ipr-browser-agent-be.rapid.studio.lyzr.ai/api/v1/batches/85b6c6a2eafd
```

**Poll every 5–10 seconds.** Results appear incrementally — one application might be `done` while another is still `in_progress`.

**Response (in progress):**

```json
{
  "batch_id": "85b6c6a2eafd",
  "status": "processing",
  "created_at": "2026-08-05T12:50:18.406000",
  "progress": {
    "total": 2,
    "pending": 0,
    "in_progress": 1,
    "done": 1,
    "failed": 0,
    "not_found": 0
  },
  "results": [
    {
      "lookup_type": "application",
      "lookup_value": "202641089029",
      "status": "done",
      "data": {
        "application_number": "202641089029",
        "patent_number": null,
        "title": "THEMATIC ROLE COLLISION DETECTOR FOR LEGAL CONTRACT AMBIGUITY ANALYSIS",
        "applicant_name": "VELLORE INSTITUTE OF TECHNOLOGY",
        "application_type": "ORDINARY APPLICATION",
        "field_of_invention": "COMPUTER SCIENCE",
        "filing_date": "21/07/2026",
        "publication_date": "24/07/2026",
        "grant_date": null,
        "request_examination_date": "--",
        "fer_date": null,
        "current_status": "Awaiting Request for Examination",
        "decision_status": null
      },
      "error": null,
      "last_scraped_at": "2026-08-05T12:52:57.554000"
    },
    {
      "lookup_type": "application",
      "lookup_value": "202641089033",
      "status": "in_progress",
      "data": null,
      "error": null,
      "last_scraped_at": null
    }
  ]
}
```

**Response (completed):**

When `status` is `"completed"`, all results are in. The `progress` object will show `pending: 0` and `in_progress: 0`.

---

### 3. Understand Batch Status

| `status` | Meaning |
|----------|---------|
| `submitted` | Batch created, nothing processed yet |
| `processing` | At least one result is in, others still pending |
| `completed` | All targets finished (done, failed, or not_found) |

### 4. Understand Per-Application Status

| `status` | Meaning |
|----------|---------|
| `pending` | Queued, waiting for the worker to pick it up |
| `in_progress` | Worker is actively scraping this number |
| `done` | Scraped successfully — `data` field has the result |
| `failed` | Scraping failed after retries — check `error` field |
| `not_found` | The IP India portal returned "No Record Found" |

---

### 5. Get a Single Application (No Batch Needed)

If a number has been scraped before (in any batch), you can fetch it directly:

```bash
curl https://ipr-browser-agent-be.rapid.studio.lyzr.ai/api/v1/applications/202641089029
```

**Response:**

```json
{
  "lookup_type": "application",
  "lookup_value": "202641089029",
  "status": "done",
  "data": {
    "application_number": "202641089029",
    "patent_number": null,
    "title": "THEMATIC ROLE COLLISION DETECTOR FOR LEGAL CONTRACT AMBIGUITY ANALYSIS",
    "applicant_name": "VELLORE INSTITUTE OF TECHNOLOGY",
    "application_type": "ORDINARY APPLICATION",
    "field_of_invention": "COMPUTER SCIENCE",
    "filing_date": "21/07/2026",
    "publication_date": "24/07/2026",
    "grant_date": null,
    "request_examination_date": "--",
    "fer_date": null,
    "current_status": "Awaiting Request for Examination",
    "decision_status": null
  },
  "error": null,
  "last_scraped_at": "2026-08-05T12:52:57.554000"
}
```

Returns `404` if the number has never been submitted.

---

### 6. List All Batches

```bash
curl https://ipr-browser-agent-be.rapid.studio.lyzr.ai/api/v1/batches?limit=10
```

Returns the most recent batches with their progress (no full results — use the batch detail endpoint for that).

---

### 7. Cancel a Batch

Cancels any targets that haven't started processing yet:

```bash
curl -X POST https://ipr-browser-agent-be.rapid.studio.lyzr.ai/api/v1/batches/85b6c6a2eafd/cancel
```

**Response:**

```json
{
  "cancelled": 3
}
```

Targets already `in_progress` or `done` are not affected.

---

### 8. Health Check

```bash
curl https://ipr-browser-agent-be.rapid.studio.lyzr.ai/health
```

```json
{
  "status": "ok",
  "worker_running": true,
  "queue_pending": 0
}
```

---

## Data Fields Reference

When `status` is `done`, the `data` object contains:

| Field | Type | Example |
|-------|------|---------|
| `application_number` | string | `"202641089029"` |
| `patent_number` | string \| null | `"456789"` or `null` |
| `title` | string | `"THEMATIC ROLE COLLISION DETECTOR..."` |
| `applicant_name` | string | `"VELLORE INSTITUTE OF TECHNOLOGY"` |
| `application_type` | string | `"ORDINARY APPLICATION"` |
| `field_of_invention` | string | `"COMPUTER SCIENCE"` |
| `filing_date` | string | `"21/07/2026"` |
| `publication_date` | string \| null | `"24/07/2026"` |
| `grant_date` | string \| null | `null` |
| `request_examination_date` | string \| null | `"--"` |
| `fer_date` | string \| null | `null` (First Examination Report date) |
| `current_status` | string | `"Awaiting Request for Examination"` |
| `decision_status` | string \| null | `null` |

---

## Frontend Integration Example (JavaScript)

```javascript
const BASE = "https://ipr-browser-agent-be.rapid.studio.lyzr.ai";

// 1. Submit
async function submitBatch(applicationNumbers) {
  const res = await fetch(`${BASE}/api/v1/batches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ application_numbers: applicationNumbers, patent_numbers: [] }),
  });
  return res.json(); // { batch_id, status, progress, results }
}

// 2. Poll until done
async function pollBatch(batchId) {
  while (true) {
    const res = await fetch(`${BASE}/api/v1/batches/${batchId}`);
    const data = await res.json();

    console.log(`Status: ${data.status} | Done: ${data.progress.done}/${data.progress.total}`);

    if (data.status === "completed") return data;

    await new Promise((r) => setTimeout(r, 7000)); // poll every 7s
  }
}

// Usage
const batch = await submitBatch(["202641089029", "202641089033"]);
const results = await pollBatch(batch.batch_id);
console.log(results.results);
```

---

## Error Handling

| HTTP Code | Meaning |
|-----------|---------|
| `200` | Success |
| `400` | Bad request — no numbers provided, or more than 50 |
| `404` | Batch or application not found |
| `422` | Validation error — malformed request body |

---

## Notes

- **Duplicate numbers** across batches are safe — the system deduplicates. If a number was already scraped, the cached result is returned instantly.
- **Retry behavior** — failed targets are retried automatically with exponential backoff (up to 8 CAPTCHA attempts per scrape).
- **Rate** — the worker processes ~5 targets at a time. Each takes 3–7 minutes due to the IP India portal's response time.
- **CORS** — all origins are allowed. The frontend can call the API directly from the browser.

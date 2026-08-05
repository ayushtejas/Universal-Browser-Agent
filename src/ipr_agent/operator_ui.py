"""Tiny stdlib web UI where a person clears queued CAPTCHAs.

Deliberately dependency-free (http.server) and bound to localhost by default.
The page polls for the next pending image and keeps focus in the text box, so
the operator can type-Enter-type-Enter at roughly 5s per CAPTCHA.
"""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from bson import ObjectId
from bson.errors import InvalidId

from . import store
from .config import settings

PAGE = """<!doctype html>
<meta charset="utf-8"><title>IPR CAPTCHA operator</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#11151c;color:#e6edf3;
      display:flex;min-height:100vh;align-items:center;justify-content:center}
 .card{background:#1a212b;padding:28px 32px;border-radius:14px;width:420px;
       box-shadow:0 8px 40px #0008}
 h1{font-size:14px;text-transform:uppercase;letter-spacing:.09em;color:#8b98a8;margin:0 0 18px}
 img{width:100%;image-rendering:pixelated;background:#fff;border-radius:8px;padding:6px}
 input{width:100%;font-size:23px;padding:11px;margin-top:14px;border-radius:8px;
       border:1px solid #303b4a;background:#0d1117;color:#e6edf3;letter-spacing:.13em;
       text-align:center;box-sizing:border-box}
 input:focus{outline:2px solid #3b82f6;border-color:#3b82f6}
 .meta{font-size:12px;color:#8b98a8;margin-top:12px;display:flex;justify-content:space-between}
 .idle{text-align:center;color:#8b98a8;padding:34px 0}
 kbd{background:#0d1117;border:1px solid #303b4a;border-radius:4px;padding:1px 5px;font-size:11px}
</style>
<div class=card>
  <h1>IPR CAPTCHA operator</h1>
  <div id=body class=idle>waiting for workers&hellip;</div>
  <div class=meta><span id=queue></span><span>submit with <kbd>Enter</kbd></span></div>
</div>
<script>
let currentId = null;

async function send(id, value) {
  await fetch('/api/solve', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'id=' + encodeURIComponent(id) + '&solution=' + encodeURIComponent(value)
  });
}

async function poll() {
  try {
    const d = await (await fetch('/api/next')).json();
    document.getElementById('queue').textContent =
      d.pending + ' pending \\u00b7 ' + d.solved + ' solved';
    if (d.id && d.id !== currentId) {
      currentId = d.id;
      const body = document.getElementById('body');
      body.className = '';
      body.innerHTML =
        '<img src="data:image/png;base64,' + d.image + '" alt="CAPTCHA to transcribe">' +
        '<input id="ans" autocomplete="off" autocapitalize="off" spellcheck="false" ' +
        'placeholder="type the code">' +
        '<div class=meta><span>' + d.label + '</span></div>';
      const box = document.getElementById('ans');
      box.focus();
      box.addEventListener('keydown', async (e) => {
        if (e.key !== 'Enter' || !box.value.trim()) return;
        box.disabled = true;
        const id = currentId;
        currentId = null;
        await send(id, box.value.trim());
        body.className = 'idle';
        body.textContent = 'saved \\u2713';
        poll();
      });
    } else if (!d.id && currentId === null) {
      const body = document.getElementById('body');
      if (!document.getElementById('ans')) {
        body.className = 'idle';
        body.textContent = 'waiting for workers\\u2026';
      }
    }
  } catch (e) { /* server restarting; next tick retries */ }
}
setInterval(poll, 1200);
poll();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ipr-operator/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep the console clear for the worker's output

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/next":
            self._send(
                200, json.dumps(self._next_payload()).encode(), "application/json"
            )
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/solve":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        try:
            challenge_id = ObjectId(form.get("id", [""])[0])
        except (InvalidId, TypeError):
            self._send(400, b"bad id", "text/plain")
            return
        solution = (form.get("solution", [""])[0]).strip()
        if not solution:
            self._send(400, b"empty solution", "text/plain")
            return
        store.solve_captcha(challenge_id, solution)
        self._send(200, b'{"ok":true}', "application/json")

    @staticmethod
    def _next_payload() -> dict:
        pending, solved = store.captcha_counts()
        doc = store.next_open_captcha()
        if doc is None:
            return {"id": None, "pending": pending, "solved": solved}
        return {
            "id": str(doc["_id"]),
            "label": doc.get("context_label", ""),
            "image": base64.b64encode(bytes(doc["image_png"])).decode(),
            "pending": pending,
            "solved": solved,
        }


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or settings.operator_host
    port = port or settings.operator_port
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"CAPTCHA operator UI -> http://{host}:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()

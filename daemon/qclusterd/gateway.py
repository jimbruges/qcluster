"""HTTP gateway: web UI, control API, and an OpenAI-compatible proxy.

Standard library only. Live updates use Server-Sent Events rather than WebSockets,
because SSE is what llama-server already speaks for token streaming, so the same
code path handles both the dashboard feed and chat streaming.
"""

from __future__ import annotations

import http.client
import json
import logging
import mimetypes
import posixpath
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .engine import plan_placement

log = logging.getLogger("qclusterd.gateway")

MAX_BODY = 4 * 1024 * 1024
SSE_INTERVAL_S = 1.0


class Gateway:
    def __init__(self, registry, provisioner, store, engine, cluster_lock=None) -> None:
        self.registry = registry
        self.provisioner = provisioner
        self.store = store
        self.engine = engine
        self.cluster_lock = cluster_lock or threading.RLock()
        self._server: ThreadingHTTPServer | None = None

    def serve_forever(self) -> None:
        handler = type("BoundHandler", (_Handler,), {"gateway": self})
        ThreadingHTTPServer.allow_reuse_address = True
        self._server = ThreadingHTTPServer((config.GATEWAY_HOST, config.GATEWAY_PORT), handler)
        log.info("gateway listening on http://%s:%d", config.GATEWAY_HOST, config.GATEWAY_PORT)
        self._server.serve_forever()

    def shutdown(self) -> None:
        if self._server:
            self._server.shutdown()

    # -- state used by both the API and the SSE feed --------------------
    def cluster_state(self) -> dict:
        snapshot = self.registry.snapshot()
        snapshot["engine"] = self.engine.status()
        snapshot["auth_required"] = bool(config.AUTH_TOKEN)
        return snapshot


def _authorised(headers) -> bool:
    if not config.AUTH_TOKEN:
        return True
    supplied = (headers.get("Authorization") or "").strip()
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    # Constant-time-ish comparison; tokens are short so this is plenty.
    expected = config.AUTH_TOKEN
    if len(supplied) != len(expected):
        return False
    return all(a == b for a, b in zip(supplied, expected))


class _Handler(BaseHTTPRequestHandler):
    server_version = "qclusterd/0.1"
    protocol_version = "HTTP/1.1"
    gateway: Gateway = None  # injected

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing --------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/") or path == "/v1":
            if not _authorised(self.headers):
                return self._deny()
            return self._proxy("GET")
        if path.startswith("/api/"):
            return self._api_get(path)
        if path == "/health":
            return self._json(200, {"ok": True, "engine": self.gateway.engine.state})
        return self._static(path)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/"):
            if not _authorised(self.headers):
                return self._deny()
            return self._proxy("POST")
        if path.startswith("/api/"):
            if not _authorised(self.headers):
                return self._deny()
            return self._api_post(path)
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not _authorised(self.headers):
            return self._deny()
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "models"]:
            try:
                self.gateway.store.delete(parts[2])
            except KeyError:
                return self._json(404, {"error": "unknown model"})
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    # -- static ---------------------------------------------------------
    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        # Normalise and confine to the web directory.
        clean = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        target = (config.WEB_DIR / clean).resolve()
        try:
            target.relative_to(config.WEB_DIR.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- API ------------------------------------------------------------
    def _api_get(self, path: str) -> None:
        gw = self.gateway
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path == "/api/cluster":
            return self._json(200, gw.cluster_state())
        if path == "/api/models":
            return self._json(200, {"models": gw.store.catalog_dict()})
        if path == "/api/engine":
            return self._json(200, gw.engine.status())
        if path == "/api/engine/logs":
            return self._json(200, {"lines": gw.engine.logs()})
        if path == "/api/engine/plan":
            model_id = (query.get("model_id") or [""])[0]
            model = gw.store.get(model_id)
            if not model:
                return self._json(404, {"error": "unknown model"})
            ctx = int((query.get("ctx_size") or [model.ctx_size])[0])
            placement = plan_placement(model, gw.registry.all(), ctx)
            return self._json(200, placement.as_dict())
        if path == "/api/events":
            return self._events()
        return self._json(404, {"error": "not found"})

    def _api_post(self, path: str) -> None:
        gw = self.gateway
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})

        parts = [p for p in path.split("/") if p]

        if path == "/api/engine/start":
            model = gw.store.get(str(body.get("model_id", "")))
            if not model:
                return self._json(404, {"error": "unknown model"})
            ctx = int(body.get("ctx_size") or model.ctx_size)
            threads = int(body.get("threads") or config.DEFAULT_THREADS)
            placement = plan_placement(model, gw.registry.all(), ctx)
            if not placement.fits and not bool(body.get("force")):
                return self._json(409, {
                    "error": "model does not fit the current cluster",
                    "placement": placement.as_dict(),
                })
            try:
                with gw.cluster_lock:
                    gw.engine.start(model, placement, ctx_size=ctx, threads=threads)
            except RuntimeError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, gw.engine.status())

        if path == "/api/engine/stop":
            gw.engine.stop()
            return self._json(200, gw.engine.status())

        if len(parts) == 4 and parts[:2] == ["api", "models"] and parts[3] == "download":
            try:
                download = gw.store.start_download(parts[2])
            except KeyError:
                return self._json(404, {"error": "unknown model"})
            return self._json(202, download.as_dict())

        if len(parts) == 4 and parts[:2] == ["api", "models"] and parts[3] == "cancel":
            gw.store.cancel(parts[2])
            return self._json(200, {"ok": True})

        if path == "/api/nodes/rescan":
            gw.registry._discover_once()
            return self._json(200, gw.registry.snapshot())

        if len(parts) >= 4 and parts[:2] == ["api", "nodes"]:
            node = gw.registry.get(parts[2])
            if not node:
                return self._json(404, {"error": "unknown node"})
            action = parts[3]
            if action == "reprovision":
                gw.provisioner.provision_async(node)
                return self._json(202, {"ok": True})
            if action == "rpc" and len(parts) == 5 and parts[4] == "restart":
                gw.provisioner.restart_rpc(node)
                return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found"})

    # -- SSE dashboard feed ---------------------------------------------
    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        gw = self.gateway
        try:
            while True:
                payload = gw.cluster_state()
                payload["models"] = gw.store.catalog_dict()
                chunk = f"data: {json.dumps(payload)}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # -- OpenAI-compatible proxy ----------------------------------------
    def _proxy(self, method: str) -> None:
        gw = self.gateway
        if not gw.engine.running:
            return self._json(503, {
                "error": {
                    "message": "no model is loaded - start one from the QCluster UI",
                    "type": "service_unavailable",
                }
            })
        try:
            body = self._raw_body() if method == "POST" else None
        except ValueError as exc:
            return self._json(413, {"error": str(exc)})

        upstream = http.client.HTTPConnection(
            config.LLAMA_HOST, config.LLAMA_PORT, timeout=900
        )
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        if body:
            headers["Content-Length"] = str(len(body))
        try:
            upstream.request(method, self.path, body=body, headers=headers)
            response = upstream.getresponse()
        except OSError as exc:
            upstream.close()
            return self._json(502, {"error": {"message": f"upstream error: {exc}"}})

        ctype = response.getheader("Content-Type", "application/json")
        streaming = "event-stream" in ctype

        self.send_response(response.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if streaming:
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self._relay_stream(response)
        else:
            payload = response.read()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self._capture_metrics(payload)
        upstream.close()

    def _relay_stream(self, response) -> None:
        buffer = b""
        try:
            while True:
                chunk = response.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                buffer = (buffer + chunk)[-8192:]
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        # The final SSE frame of a llama-server stream carries the timing block.
        for line in reversed(buffer.split(b"\n")):
            if line.startswith(b"data: ") and b"timings" in line:
                self._capture_metrics(line[6:])
                break

    def _capture_metrics(self, payload: bytes) -> None:
        try:
            self.gateway.engine.record_metrics(json.loads(payload))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

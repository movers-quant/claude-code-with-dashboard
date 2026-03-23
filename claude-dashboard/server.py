#!/usr/bin/env python3
"""Claude Dashboard — real-time monitor for Claude Code sessions.

Pure-stdlib Python 3 server (no third-party dependencies).
Entry point: starts the asyncio HTTP + WebSocket server.
"""

import asyncio
import json
import pathlib
import time
from http import HTTPStatus

from config import PORT, POLL_INTERVAL_S, PUBLIC_DIR, SESSION_STATS_PATH, BODY_SIZE_LIMIT
from otlp import parse_otlp_metrics
from sessions import merge_session_data, delete_session
from websocket import WebSocketConnection, broadcast, clients, compute_accept_key

# ── HTTP request handler ────────────────────────────────────────────────────

async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    """Read an HTTP chunked transfer-encoded body."""
    chunks = []
    total = 0
    while True:
        size_line = await reader.readline()
        chunk_size = int(size_line.strip(), 16)
        if chunk_size == 0:
            await reader.readline()  # trailing \r\n
            break
        total += chunk_size
        if total > BODY_SIZE_LIMIT:
            raise ValueError("Chunked body exceeds size limit")
        chunk = await reader.readexactly(chunk_size)
        chunks.append(chunk)
        await reader.readline()  # chunk-ending \r\n
    return b"".join(chunks)


MIME_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle one HTTP connection (may upgrade to WebSocket)."""
    try:
        raw_request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
    except Exception:
        writer.close()
        return

    request_text = raw_request.decode(errors="replace")
    lines = request_text.split("\r\n")
    if not lines:
        writer.close()
        return

    request_line = lines[0]
    parts = request_line.split(" ")
    if len(parts) < 2:
        writer.close()
        return

    method = parts[0]
    path = parts[1].split("?")[0]

    # Parse headers
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    # ── WebSocket upgrade ──
    if path == "/ws" and "upgrade" in headers.get("connection", "").lower():
        ws_key = headers.get("sec-websocket-key", "")
        accept = compute_accept_key(ws_key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        writer.write(response.encode())
        await writer.drain()

        ws = WebSocketConnection(reader, writer)
        clients.add(ws)
        try:
            data = await merge_session_data()
            await ws.send(json.dumps({"type": "sessions", "data": data}))
            while True:
                msg = await ws.recv()
                if msg is None:
                    break
        finally:
            clients.discard(ws)
            await ws.close()
        return

    # ── Read body for POST ──
    body = b""
    content_length = int(headers.get("content-length", 0))
    is_chunked = "chunked" in headers.get("transfer-encoding", "").lower()
    if content_length > 0:
        if content_length > BODY_SIZE_LIMIT:
            await _send_response(writer, 413, {"error": "Payload too large"})
            return
        body = await asyncio.wait_for(reader.readexactly(content_length), timeout=10.0)
    elif is_chunked:
        try:
            body = await asyncio.wait_for(_read_chunked(reader), timeout=10.0)
        except ValueError:
            await _send_response(writer, 413, {"error": "Payload too large"})
            return

    # ── Route ──
    if method == "GET" and path == "/api/health":
        await _send_response(writer, 200, {"ok": True, "uptime": time.monotonic()})

    elif method == "GET" and path == "/api/sessions":
        data = await merge_session_data()
        await _send_response(writer, 200, data)

    elif method == "POST" and path == "/v1/metrics":
        content_type = headers.get("content-type", "")
        if "protobuf" in content_type:
            await _send_response(writer, 200, {})
        else:
            try:
                payload = json.loads(body)
                parse_otlp_metrics(payload)
                await broadcast({"type": "metrics_update"})
            except Exception:
                pass
            await _send_response(writer, 200, {})

    elif method == "DELETE" and path.startswith("/api/sessions/"):
        session_id = path[len("/api/sessions/"):]
        if not session_id:
            await _send_response(writer, 400, {"error": "Missing session ID"})
        elif delete_session(session_id):
            await _send_response(writer, 200, {"deleted": session_id})
        else:
            await _send_response(writer, 404, {"error": "Session not found"})

    elif method == "POST" and path in ("/v1/traces", "/v1/logs"):
        await _send_response(writer, 200, {})

    elif method == "GET":
        await _serve_static(writer, path)

    else:
        await _send_response(writer, 404, {"error": "Not found"})


async def _send_response(writer: asyncio.StreamWriter, status: int, body, content_type: str = "application/json"):
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Unknown"

    if isinstance(body, (dict, list)):
        body_bytes = json.dumps(body).encode()
        content_type = "application/json"
    elif isinstance(body, bytes):
        body_bytes = body
    else:
        body_bytes = str(body).encode()

    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    try:
        writer.write(header.encode() + body_bytes)
        await writer.drain()
        writer.close()
    except Exception:
        pass


async def _serve_static(writer: asyncio.StreamWriter, url_path: str):
    if url_path == "/":
        url_path = "/index.html"

    safe_path = pathlib.Path(url_path.lstrip("/"))
    if ".." in safe_path.parts:
        await _send_response(writer, 403, {"error": "Forbidden"})
        return

    file_path = PUBLIC_DIR / safe_path
    if not file_path.is_file():
        await _send_response(writer, 404, {"error": "Not found"})
        return

    mime = MIME_TYPES.get(file_path.suffix, "application/octet-stream")
    data = file_path.read_bytes()
    await _send_response(writer, 200, data, content_type=mime)


# ── Background tasks ────────────────────────────────────────────────────────

_stats_mtime: float = 0.0


async def poll_loop():
    """Broadcast session data every POLL_INTERVAL_S seconds."""
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        data = await merge_session_data()
        await broadcast({"type": "sessions", "data": data})


async def file_watch_loop():
    """Poll session-stats file for mtime changes."""
    global _stats_mtime
    while True:
        await asyncio.sleep(0.5)
        try:
            mtime = SESSION_STATS_PATH.stat().st_mtime
            if mtime != _stats_mtime:
                _stats_mtime = mtime
                data = await merge_session_data()
                await broadcast({"type": "sessions", "data": data})
        except FileNotFoundError:
            pass
        except Exception:
            pass


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    server = await asyncio.start_server(handle_connection, "0.0.0.0", PORT)

    print()
    print("+" + "=" * 54 + "+")
    print(f"|  Claude Dashboard running at http://localhost:{PORT}    |")
    print("+" + "=" * 54 + "+")
    print(f"|  OTLP endpoint: http://localhost:{PORT}/v1/metrics       |")
    print("+" + "-" * 54 + "+")
    print("|  To enable real-time token metrics, start Claude      |")
    print("|  Code with these env vars:                            |")
    print("|                                                       |")
    print(f"|  CLAUDE_CODE_ENABLE_TELEMETRY=1 \\                     |")
    print(f"|  OTEL_METRICS_EXPORTER=otlp \\                         |")
    print(f"|  OTEL_EXPORTER_OTLP_PROTOCOL=http/json \\              |")
    print(f"|  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{PORT} \\   |")
    print(f"|  OTEL_METRIC_EXPORT_INTERVAL=5000 \\                   |")
    print(f"|  claude                                               |")
    print("+" + "=" * 54 + "+")
    print()

    poll_task = asyncio.create_task(poll_loop())
    watch_task = asyncio.create_task(file_watch_loop())

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        poll_task.cancel()
        watch_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

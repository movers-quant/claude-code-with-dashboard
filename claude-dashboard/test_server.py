#!/usr/bin/env python3
"""Integration tests for claude-dashboard Python server.

Tests:
  1. Server starts and /api/health responds
  2. /api/sessions reads session-stats file
  3. WebSocket delivers sessions on connect
  4. POST /v1/metrics ingests OTLP token data per session
  5. Token data appears in /api/sessions response
  6. OTLP creates stub session for unknown session ID
  7. POST /v1/traces and /v1/logs return 200

Run with:  python3 test_server.py
Exit code: 0 = all pass, 1 = any fail
"""

import asyncio
import base64
import hashlib
import http.client
import json
import os
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

TEST_PORT = 13099
STATS_DIR = tempfile.mkdtemp()
STATS_PATH = pathlib.Path(STATS_DIR) / ".session-stats.json"

FAKE_SESSIONS = {
    "aaaa-1111": {
        "tool_counts": {"Bash": 12, "Read": 5, "Write": 3},
        "last_tool": "Bash",
        "total_calls": 20,
        "started_at": int(time.time()) - 300,
        "updated_at": int(time.time()) - 5,
    },
    "bbbb-2222": {
        "tool_counts": {"Bash": 4, "Glob": 2, "Grep": 1},
        "last_tool": "Glob",
        "total_calls": 7,
        "started_at": int(time.time()) - 120,
        "updated_at": int(time.time()) - 60,
    },
    "cccc-3333": {
        "tool_counts": {"Bash": 1},
        "last_tool": "Bash",
        "total_calls": 1,
        "started_at": int(time.time()) - 600,
        "updated_at": int(time.time()) - 500,
    },
}

server_proc = None


def write_stats(sessions_data):
    STATS_PATH.write_text(json.dumps({"sessions": sessions_data}))


def http_request(method, path, body=None):
    conn = http.client.HTTPConnection("localhost", TEST_PORT, timeout=5)
    headers = {}
    if body is not None:
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body.encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


def ws_connect_and_recv(timeout_s=3.0):
    """Connect to WebSocket, read one frame, return parsed JSON."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    sock.connect(("localhost", TEST_PORT))

    # Handshake
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET /ws HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())

    # Read response headers
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)

    if b"101" not in response.split(b"\r\n")[0]:
        sock.close()
        raise RuntimeError(f"WebSocket upgrade failed: {response[:200]}")

    # Read one frame
    b0 = sock.recv(1)[0]
    b1 = sock.recv(1)[0]
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]

    payload = b""
    while len(payload) < length:
        payload += sock.recv(length - len(payload))

    sock.close()
    return json.loads(payload.decode())


class TestClaudeDashboard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        global server_proc

        # Write fake stats
        write_stats(FAKE_SESSIONS)

        # Patch SESSION_STATS_PATH via env — we'll modify server.py to respect this
        env = {
            **os.environ,
            "PORT": str(TEST_PORT),
            "SESSION_STATS_PATH": str(STATS_PATH),
        }

        server_proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=pathlib.Path(__file__).parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for server to start
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("localhost", TEST_PORT, timeout=1)
                conn.request("GET", "/api/health")
                resp = conn.getresponse()
                conn.close()
                if resp.status == 200:
                    return
            except Exception:
                time.sleep(0.3)

        raise RuntimeError("Server did not start within 8 seconds")

    @classmethod
    def tearDownClass(cls):
        global server_proc
        if server_proc:
            server_proc.stdout.close()
            server_proc.stderr.close()
            server_proc.terminate()
            server_proc.wait(timeout=5)
            server_proc = None
        shutil.rmtree(STATS_DIR, ignore_errors=True)

    # ── Test 1: Health ──

    def test_01_health(self):
        status, body = http_request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("uptime", body)

    # ── Test 2: Sessions from stats file ──

    def test_02_sessions(self):
        status, body = http_request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)
        ids = [s["id"] for s in body]
        for sid in ("aaaa-1111", "bbbb-2222", "cccc-3333"):
            self.assertIn(sid, ids, f"session {sid} missing")

        s1 = next(s for s in body if s["id"] == "aaaa-1111")
        self.assertEqual(s1["total_calls"], 20)

    # ── Test 3: WebSocket delivers sessions ──

    def test_03_websocket(self):
        msg = ws_connect_and_recv()
        self.assertEqual(msg["type"], "sessions")
        self.assertIsInstance(msg["data"], list)
        self.assertGreater(len(msg["data"]), 0)

    # ── Test 4: OTLP ingest ──

    def test_04_otlp_ingest(self):
        payload = {
            "resourceMetrics": [{
                "resource": {"attributes": [{"key": "session.id", "value": {"stringValue": "aaaa-1111"}}]},
                "scopeMetrics": [{
                    "metrics": [{
                        "name": "claude_code.token.usage",
                        "sum": {
                            "dataPoints": [
                                {"attributes": [{"key": "type", "value": {"stringValue": "input"}}], "asInt": 4200},
                                {"attributes": [{"key": "type", "value": {"stringValue": "output"}}], "asInt": 1800},
                                {"attributes": [{"key": "type", "value": {"stringValue": "cache_read"}}], "asInt": 9500},
                                {"attributes": [{"key": "type", "value": {"stringValue": "cache_creation"}}], "asInt": 500},
                            ],
                        },
                    }, {
                        "name": "claude_code.cost.usage",
                        "sum": {
                            "dataPoints": [{"attributes": [], "asDouble": 0.0123}],
                        },
                    }],
                }],
            }],
        }
        status, _ = http_request("POST", "/v1/metrics", payload)
        self.assertEqual(status, 200)

    # ── Test 5: Token data in sessions ──

    def test_05_token_data_in_sessions(self):
        # Ensure OTLP data from test_04 is visible
        time.sleep(0.3)
        status, body = http_request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        s = next((x for x in body if x["id"] == "aaaa-1111"), None)
        self.assertIsNotNone(s, "session aaaa-1111 not found")
        self.assertIn("tokens", s)
        self.assertEqual(s["tokens"]["input"], 4200)
        self.assertEqual(s["tokens"]["output"], 1800)
        self.assertEqual(s["tokens"]["cache_read"], 9500)
        self.assertEqual(s["tokens"]["cache_creation"], 500)
        self.assertGreater(s["tokens"]["cost"], 0)

    # ── Test 6: OTLP stub session ──

    def test_06_otlp_stub_session(self):
        payload = {
            "resourceMetrics": [{
                "resource": {"attributes": [{"key": "session.id", "value": {"stringValue": "unknown-otel-session"}}]},
                "scopeMetrics": [{
                    "metrics": [{
                        "name": "claude_code.token.usage",
                        "sum": {"dataPoints": [{"attributes": [{"key": "type", "value": {"stringValue": "input"}}], "asInt": 999}]},
                    }],
                }],
            }],
        }
        http_request("POST", "/v1/metrics", payload)
        time.sleep(0.3)
        status, body = http_request("GET", "/api/sessions")
        stub = next((x for x in body if x["id"] == "unknown-otel-session"), None)
        self.assertIsNotNone(stub, "stub session not created")
        self.assertEqual(stub["tokens"]["input"], 999)

    # ── Test 7: Stub endpoints ──

    def test_07_stub_endpoints(self):
        status1, _ = http_request("POST", "/v1/traces", {})
        status2, _ = http_request("POST", "/v1/logs", {})
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 200)

    # ── Test 8: Static file serving ──

    def test_08_static_files(self):
        status, body = http_request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Claude Dashboard", body)

    # ── Test 9: DELETE /api/sessions/<id> ──

    def test_09_delete_session(self):
        # Verify session exists
        status, body = http_request("GET", "/api/sessions")
        ids = [s["id"] for s in body]
        self.assertIn("cccc-3333", ids)

        # Delete it
        status, body = http_request("DELETE", "/api/sessions/cccc-3333")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], "cccc-3333")

        # Verify it's gone
        status, body = http_request("GET", "/api/sessions")
        ids = [s["id"] for s in body]
        self.assertNotIn("cccc-3333", ids)

    def test_10_delete_session_not_found(self):
        status, body = http_request("DELETE", "/api/sessions/nonexistent-id")
        self.assertEqual(status, 404)

    # ── Test 11: Chunked transfer-encoding OTLP ingest ──

    def test_11_otlp_chunked_transfer_encoding(self):
        """OTLP metrics sent with Transfer-Encoding: chunked are parsed correctly."""
        import socket

        payload = json.dumps({
            "resourceMetrics": [{
                "resource": {"attributes": [{"key": "session.id", "value": {"stringValue": "chunked-test"}}]},
                "scopeMetrics": [{
                    "metrics": [{
                        "name": "claude_code.token.usage",
                        "sum": {"dataPoints": [{
                            "attributes": [{"key": "type", "value": {"stringValue": "input"}}],
                            "asInt": "2000"
                        }]}
                    }]
                }]
            }]
        }).encode()

        # Build a raw HTTP request with chunked encoding
        chunk = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
        raw = (
            f"POST /v1/metrics HTTP/1.1\r\n"
            f"Host: localhost:{TEST_PORT}\r\n"
            f"Content-Type: application/json\r\n"
            f"Transfer-Encoding: chunked\r\n"
            f"\r\n"
        ).encode() + chunk

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(("localhost", TEST_PORT))
        sock.sendall(raw)
        resp = sock.recv(4096).decode()
        sock.close()
        self.assertIn("HTTP/1.1 200", resp)

        # Verify token data was ingested
        status, body = http_request("GET", "/api/sessions")
        chunked_session = [s for s in body if s["id"] == "chunked-test"]
        self.assertEqual(len(chunked_session), 1)
        self.assertEqual(chunked_session[0]["tokens"]["input"], 2000)


if __name__ == "__main__":
    unittest.main(verbosity=2)

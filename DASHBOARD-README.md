# Claude Dashboard

Real-time web dashboard for monitoring Claude Code sessions on your machine.

Built with Python 3 stdlib only — zero dependencies, no pip install needed.

## Quick Start

```bash
cd claude-dashboard
python3 server.py
# Open http://localhost:3000
```

## What It Does

Claude Dashboard aggregates three data sources into a live-updating web UI:

1. **Session stats file** — reads `~/.claude/.session-stats.json` for tool usage counts per session
2. **Process table** — runs `ps aux` asynchronously to detect live `claude` processes with CPU/memory
3. **OTLP metrics** — receives token usage and cost data pushed by Claude Code via OpenTelemetry

The browser connects over WebSocket and gets pushed updates every 3 seconds or immediately when the stats file changes.

## Enabling Token Metrics

By default the dashboard shows session and tool data. To also see token usage (input/output/cache) and cost, start Claude Code with:

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_METRICS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000 \
OTEL_METRIC_EXPORT_INTERVAL=5000 \
claude
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Returns `{"ok": true, "uptime": N}` |
| `GET` | `/api/sessions` | All sessions sorted by last update (JSON array) |
| `DELETE` | `/api/sessions/<id>` | Remove a session from the dashboard |
| `POST` | `/v1/metrics` | OTLP/HTTP JSON ingest for token/cost metrics |
| `POST` | `/v1/traces` | Stub (acks and discards) |
| `POST` | `/v1/logs` | Stub (acks and discards) |
| `WS` | `/ws` | Live session updates, auto-reconnects |

### Session Object

```json
{
  "id": "session-uuid",
  "status": "running | idle | stopped",
  "pid": 12345,
  "cpu": "2.3",
  "mem": "1.5",
  "working_dir": "/home/user/project",
  "tool_counts": {"Bash": 12, "Read": 5},
  "total_calls": 17,
  "started_at": 1711100000000,
  "updated_at": 1711100300000,
  "tokens": {
    "input": 4200,
    "output": 1800,
    "cache_read": 9500,
    "cache_creation": 500,
    "cost": 0.0123
  }
}
```

### Deleting Sessions

Click the **Remove** button on any session card in the UI, or call the API directly:

```bash
curl -X DELETE http://localhost:3000/api/sessions/<session-id>
```

Deleted sessions are excluded from future reads of the stats file until the server restarts.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | HTTP/WebSocket listen port |
| `SESSION_STATS_PATH` | `~/.claude/.session-stats.json` | Path to session stats file |

## Project Structure

```
claude-dashboard/
├── server.py        284 lines  Entry point, HTTP routing, static files, background loops
├── config.py         14 lines  Constants: PORT, paths, limits, allowlists
├── processes.py      44 lines  Async process detection via ps aux
├── sessions.py      100 lines  Session state: merge, evict, delete
├── otlp.py           58 lines  OTLP/HTTP JSON parser for token/cost metrics
├── websocket.py     115 lines  Minimal RFC 6455 WebSocket (handshake + framing)
├── test_server.py   360 lines  Integration tests (11 tests, unittest)
├── public/
│   └── index.html   746 lines  Dashboard UI (inline CSS + JS)
└── .gitignore
```

### Module Dependency Graph

```
server.py
├── config.py
├── sessions.py
│   ├── config.py
│   └── processes.py
├── otlp.py
│   ├── config.py
│   └── sessions.py (token_metrics dict)
└── websocket.py
```

### How Data Flows

```
~/.claude/.session-stats.json ──mtime poll──> sessions.py ──> server.py ──WS──> browser
ps aux ──async subprocess──────────────────> sessions.py
Claude Code ──OTLP POST──> otlp.py ──────> sessions.py (token_metrics)
```

`merge_session_data()` in `sessions.py` is the central function — called on every poll tick, file change, WebSocket connect, and session list request. It reads the stats file, runs `ps aux`, merges token metrics, matches processes to sessions by recency, and returns a sorted list.

## Running Tests

```bash
python3 test_server.py -v
```

11 tests covering: health, sessions, WebSocket delivery, OTLP ingest, token data verification, stub session creation, stub endpoints, static file serving, session deletion, 404 on missing delete, and chunked transfer-encoding OTLP ingest.

Tests use a temp directory for the stats file (via `SESSION_STATS_PATH` env var) and an isolated port (13099).

## Design Decisions

**Stdlib only.** No pip, no venv, no dependency management. The server uses `asyncio.start_server` for TCP, hand-rolled HTTP parsing, and a minimal RFC 6455 WebSocket implementation. This means it runs anywhere Python 3.11+ is installed.

**Async process detection.** `ps aux` runs via `asyncio.create_subprocess_exec` (no shell invocation) so it never blocks the event loop. The original Node.js version used blocking `execSync`.

**Session cap.** Both `sessions` and `token_metrics` dicts are capped at 500 entries with LRU-style eviction. OTLP data without a session ID is silently discarded. Token types are validated against an allowlist (`input`, `output`, `cache_read`, `cache_creation`).

**Process-to-session matching.** Processes are matched to sessions by index order (i-th process to i-th most recently updated session). This is a heuristic — there's no reliable way to correlate a `claude` PID to a specific session ID from the outside.

**Delete tracking.** Deleted session IDs are stored in a `_deleted_ids` set so `merge_session_data()` won't re-import them from the stats file on the next read.

## Known Limitations

- **XSS in frontend.** Session IDs and tool names flow into `innerHTML` without escaping. An `esc()` helper should be added to `renderCard()` in `index.html`.
- **No authentication.** The OTLP endpoint and all APIs are open. Only run on localhost.
- **No WebSocket connection limit.** Many simultaneous connections could degrade performance.
- **Linux-only PID features.** Working directory detection reads `/proc/<pid>/cwd`, which only exists on Linux.
- **Port 3000 hardcoded in UI hints.** The OTEL setup hint in `index.html` always shows `localhost:3000` regardless of the actual `PORT`.

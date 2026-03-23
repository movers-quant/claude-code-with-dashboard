# Claude Dashboard

Real-time web dashboard for monitoring all Claude Code sessions on your machine.

## Features

- **Session discovery** — auto-detects running `claude` processes and reads `~/.claude/.session-stats.json`
- **Live updates** — WebSocket pushes updates every 3 seconds and on file changes
- **Tool usage** — shows per-session tool call breakdown (Bash, Read, Write, etc.)
- **Token metrics** — displays input/output/cache tokens per session via OpenTelemetry
- **Status badges** — running / idle / stopped per session
- **Zero dependencies** — pure Python 3 stdlib, no pip install needed

## Quick Start

```bash
cd claude-dashboard
python3 server.py
# → http://localhost:3000
```

## Enable Token Metrics (OpenTelemetry)

Start Claude Code with these env vars to stream token usage to the dashboard:

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_METRICS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000 \
OTEL_METRIC_EXPORT_INTERVAL=5000 \
claude
```

The dashboard acts as an OTLP/HTTP receiver at `POST /v1/metrics`.

## Running Tests

```bash
python3 test_server.py
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3000` | Dashboard HTTP port |
| `SESSION_STATS_PATH` | `~/.claude/.session-stats.json` | Path to session stats file |

## Architecture

```
claude-dashboard/
├── server.py          # Entry point — HTTP routing, static files, background loops
├── config.py          # Configuration constants (PORT, paths, limits)
├── processes.py       # Async claude process detection via ps aux
├── sessions.py        # Session state management and data merging
├── otlp.py            # OTLP/HTTP JSON metrics parser
├── websocket.py       # Minimal RFC 6455 WebSocket implementation
├── test_server.py     # Integration tests (unittest, 8 tests)
├── public/
│   └── index.html     # Single-page dashboard UI
└── .gitignore
```

**Data sources:**
1. `~/.claude/.session-stats.json` — session tool usage stats (file polled)
2. `ps aux` — running claude processes (async, polled every 3s)
3. OTLP `/v1/metrics` — token/cost metrics pushed by Claude Code
